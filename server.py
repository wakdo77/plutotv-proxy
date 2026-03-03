"""
PlutoTV Stream Proxy Server
============================
Lokaler HTTP-Proxy, der PlutoTV-Livestreams für VLC, Kodi IPTV Simple und Enigma2 bereitstellt.

Endpunkte:
  GET /playlist.m3u          – M3U-Playlist aller Kanäle
  GET /live/<channel_id>     – Live-HLS-Stream (beste Variante, Segmente direkt vom CDN)

Ablauf pro Stream-Request:
  1. Master-Playlist von PlutoTV holen (JWT wird serverseitig ergänzt)
  2. Variante mit höchster Bandbreite wählen
  3. Varianten-Playlist holen und Segment-URLs zu absoluten CDN-URLs umschreiben
  4. Playlist an den Player zurückgeben → Player fetcht Segmente direkt vom CDN

VLC re-fetcht die Playlist-URL automatisch alle ~6 Sekunden → endloser Live-Stream.
JWT-Refresh erfolgt automatisch, wenn das Token in < 5 Minuten abläuft (~24h Gültigkeit).
"""

import re
import json
import base64
import urllib.parse
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, Response, abort

# ─── Konfiguration ───────────────────────────────────────────────────────────

HOST = "0.0.0.0"
PORT = 8080
PROXY_BASE = f"http://localhost:{PORT}"

PLUTO_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

# Boot-URL mit Platzhalter {dt} für den aktuellen Zeitstempel
PLUTO_BOOT_URL = (
    "https://boot.pluto.tv/v4/start"
    "?appName=web"
    "&appVersion=9.19.0-7a6c115631d945c4f7327de3e03b7c474b692657"
    "&deviceVersion=145.0.0"
    "&deviceModel=web"
    "&deviceMake=chrome"
    "&deviceType=web"
    "&clientID=35f42d5d-9c81-4748-bc09-d2894ae4e66f"
    "&clientModelNumber=1.0.0"
    "&channelID="
    "&serverSideAds=false"
    "&drmCapabilities=widevine%3AL3"
    "&blockingMode="
    "&notificationVersion=1"
    "&appLaunchCount=0"
    "&lastAppLaunchDate={dt}"
    "&clientTime={dt}"
)

# ─── PlutoTV Session ─────────────────────────────────────────────────────────

class PlutoSession:
    """
    Verwaltet die PlutoTV-Session: Boot-Request, JWT-Refresh und Kanalliste.
    Thread-sicher: JWT-Refresh wird mit einem Lock geschützt.
    """

    def __init__(self):
        self._lock = threading.Lock()

        # HTTP-Session mit persistenten Cookies
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": PLUTO_USER_AGENT})

        # Session-Daten (nach boot() befüllt)
        self.jwt: str = ""
        self.jwt_exp: int = 0           # Unix-Timestamp aus JWT-Payload
        self.session_id: str = ""
        self.client_id: str = "35f42d5d-9c81-4748-bc09-d2894ae4e66f"
        self.stitcher_base: str = ""    # z.B. https://cfd-v4-...pluto.tv
        self.stitcher_params: str = ""  # z.B. sid=...&deviceId=...
        self.channels_server: str = ""
        self.channels: list = []

        self.boot()

    # ── Boot ──────────────────────────────────────────────────────────────────

    def boot(self):
        """
        Führt den PlutoTV Boot-Request durch:
        Lädt JWT, Session-ID, Stitcher-Parameter und Server-Endpunkte.
        Anschließend wird die Kanalliste geladen.
        """
        dt = _iso_now()
        url = PLUTO_BOOT_URL.format(dt=dt)

        resp = self.http.get(url)
        resp.raise_for_status()
        data = resp.json()

        self.jwt            = data.get("sessionToken", "")
        sess                = data.get("session", {})
        self.session_id     = sess.get("sessionID", "")
        self.client_id      = sess.get("clientID", self.client_id)
        self.stitcher_params = data.get("stitcherParams", "")

        servers = data.get("servers", {})
        self.stitcher_base   = servers.get("stitcher", "")
        self.channels_server = servers.get("channels", "")

        self._parse_jwt_exp()
        print(
            f"[Pluto] Session gestartet | sid={self.session_id} | "
            f"JWT läuft ab: {datetime.fromtimestamp(self.jwt_exp).strftime('%Y-%m-%d %H:%M:%S')}"
        )

        self._load_channels()

    def _parse_jwt_exp(self):
        """Liest den exp-Wert (Ablaufzeit) aus dem JWT-Payload (Base64-Decode)."""
        try:
            payload_b64 = self.jwt.split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)   # Padding ergänzen
            payload = json.loads(base64.b64decode(payload_b64))
            self.jwt_exp = payload.get("exp", 0)
        except Exception as e:
            print(f"[Pluto] JWT-Parsing fehlgeschlagen: {e}")
            self.jwt_exp = 0

    def _load_channels(self):
        """Lädt alle verfügbaren Kanäle vom Channels-API-Server."""
        if not self.channels_server:
            print("[Pluto] Kein Channels-Server gefunden.")
            return
        url = (
            self.channels_server
            + "/v2/guide/channels?channelIds=&offset=0&limit=1000&sort=number%3Aasc"
        )
        resp = self.http.get(url, headers={"Authorization": f"Bearer {self.jwt}"})
        if resp.ok:
            self.channels = resp.json().get("data", [])
            print(f"[Pluto] {len(self.channels)} Kanäle geladen.")
        else:
            print(f"[Pluto] Kanäle konnten nicht geladen werden: {resp.status_code}")

    # ── JWT-Refresh ───────────────────────────────────────────────────────────

    def ensure_valid(self):
        """
        Prüft ob das JWT noch mindestens 5 Minuten gültig ist.
        Falls nicht, wird boot() erneut aufgerufen.
        Double-Checked Locking: verhindert doppelten Refresh bei parallelen Requests.
        """
        if time.time() > self.jwt_exp - 300:
            with self._lock:
                if time.time() > self.jwt_exp - 300:
                    print("[Pluto] JWT läuft ab – erneuere Session ...")
                    self.boot()

    # ── URL-Builder ───────────────────────────────────────────────────────────

    def master_url(self, channel_id: str) -> str:
        """Baut die vollständige Master-Playlist-URL für einen Kanal."""
        return (
            f"{self.stitcher_base}/v2/stitch/hls/channel/{channel_id}/master.m3u8"
            f"?{self.stitcher_params}&jwt={self.jwt}"
        )

    def variant_url(self, channel_id: str, relative_uri: str) -> str:
        """
        Baut die vollständige URL für eine Varianten-Playlist.
        relative_uri enthält bereits sid und deviceId (vom Stitcher eingebettet).
        Wir ergänzen nur das JWT.
        """
        base = f"{self.stitcher_base}/v2/stitch/hls/channel/{channel_id}/"
        if "?" in relative_uri:
            path, qs = relative_uri.split("?", 1)
            return f"{base}{path}?{qs}&jwt={self.jwt}"
        # Fallback: stitcherParams verwenden wenn kein Query-String vorhanden
        return f"{base}{relative_uri}?{self.stitcher_params}&jwt={self.jwt}"


# ─── HLS-Helfer ──────────────────────────────────────────────────────────────

def parse_best_variant(master_content: str) -> dict | None:
    """
    Parst eine HLS Master-Playlist und gibt die Variante mit der
    höchsten Bandbreite zurück.

    Rückgabe: {'bandwidth': int, 'uri': str} oder None wenn keine Variante gefunden.
    """
    best = None
    lines = master_content.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        bw_match  = re.search(r"BANDWIDTH=(\d+)", line)
        bandwidth = int(bw_match.group(1)) if bw_match else 0

        # Nächste nicht-leere Nicht-Kommentar-Zeile ist die Varianten-URI
        for uri_line in lines[i + 1:]:
            uri_stripped = uri_line.strip()
            if uri_stripped and not uri_stripped.startswith("#"):
                if best is None or bandwidth > best["bandwidth"]:
                    best = {"bandwidth": bandwidth, "uri": uri_stripped}
                break

    return best


def make_segments_absolute(playlist_content: str, playlist_url: str) -> str:
    """
    Wandelt relative Segment-URLs in einer Varianten-Playlist in absolute URLs um.
    Der Player kann Segmente dann direkt vom PlutoTV-CDN laden (kein JWT nötig).

    playlist_url: die URL von der die Playlist abgerufen wurde (für relative Auflösung).
    """
    # Basis: Verzeichnis der Playlist-URL (ohne Query-String)
    base = playlist_url.split("?")[0].rsplit("/", 1)[0] + "/"

    result = []
    for line in playlist_content.splitlines():
        clean = line.strip()
        # Segment-Zeilen: nicht leer, kein '#'
        if clean and not clean.startswith("#"):
            if not clean.startswith("http"):
                clean = urllib.parse.urljoin(base, clean)
            result.append(clean)
        else:
            result.append(line.rstrip("\r"))

    return "\n".join(result)


def _iso_now() -> str:
    """Gibt den aktuellen UTC-Zeitstempel URL-codiert im PlutoTV-Format zurück."""
    dt = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    return urllib.parse.quote(dt, safe="")


def get_logo(ch: dict) -> str:
    """Extrahiert die Logo-URL aus einem Channel-Objekt (robust für verschiedene Formate)."""
    images = ch.get("images", {})
    if isinstance(images, dict):
        for key in ("logo", "thumbnail", "featuredImage", "poster"):
            img = images.get(key)
            if isinstance(img, dict):
                return img.get("path") or img.get("url") or ""
            if isinstance(img, str) and img:
                return img
    elif isinstance(images, list):
        for img in images:
            if isinstance(img, dict) and img.get("type") in ("logo", "thumbnail"):
                return img.get("url", "")
    return ""


# ─── Flask App ────────────────────────────────────────────────────────────────

app = Flask(__name__)
pluto = PlutoSession()   # Einmalig beim Start initialisieren


@app.route("/playlist.m3u")
def playlist_m3u():
    """
    M3U-Playlist aller PlutoTV-Kanäle.
    Kompatibel mit VLC, Kodi IPTV Simple Client und Enigma2.
    """
    pluto.ensure_valid()

    lines = ["#EXTM3U"]
    for ch in pluto.channels:
        ch_id  = ch.get("id", "")
        name   = ch.get("name", ch_id)
        number = ch.get("number", 0)
        logo   = get_logo(ch)

        # Kategorie: kann String oder Dict sein
        cat      = ch.get("category")
        category = cat.get("name", "") if isinstance(cat, dict) else (cat or "")

        lines.append(
            f'#EXTINF:-1 tvg-id="{ch_id}" tvg-name="{name}" '
            f'tvg-logo="{logo}" tvg-chno="{number}" '
            f'group-title="{category}",{name}'
        )
        lines.append(f"{PROXY_BASE}/live/{ch_id}.m3u8")

    return Response(
        "\n".join(lines),
        mimetype="audio/x-mpegurl",
        headers={"Content-Disposition": 'inline; filename="plutotv.m3u"'},
    )


@app.route("/live/<channel_id>")
@app.route("/live/<channel_id>.m3u8")
def live_stream(channel_id: str):
    """
    Live-Stream-Endpunkt für einen einzelnen Kanal.

    Ablauf:
      1. Master-Playlist von PlutoTV fetchen (JWT wird ergänzt)
      2. Variante mit höchster Bandbreite wählen
      3. Varianten-Playlist fetchen und Segment-URLs → absolute CDN-URLs
      4. Fertige Playlist zurückgeben

    VLC und andere Player re-fetchen diese URL automatisch (live HLS) → Stream läuft endlos.
    """
    pluto.ensure_valid()

    # 1. Master-Playlist holen
    m_url = pluto.master_url(channel_id)
    resp  = pluto.http.get(m_url)
    if not resp.ok:
        print(f"[Proxy] Master-Fehler {resp.status_code} für {channel_id}")
        abort(resp.status_code)

    # 2. Beste Variante wählen
    best = parse_best_variant(resp.text)
    if not best:
        print(f"[Proxy] Keine Varianten gefunden für {channel_id}")
        abort(404)

    # 3. Varianten-Playlist fetchen
    v_url   = pluto.variant_url(channel_id, best["uri"])
    v_resp  = pluto.http.get(v_url)
    if not v_resp.ok:
        print(f"[Proxy] Varianten-Fehler {v_resp.status_code} ({best['bandwidth']} bps)")
        abort(v_resp.status_code)

    # 4. Segment-URLs zu absoluten CDN-URLs umschreiben
    content = make_segments_absolute(v_resp.text, v_url)

    return Response(content, mimetype="application/vnd.apple.mpegurl")


# ─── Start ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"  PlutoTV Stream Proxy")
    print(f"{'='*50}")
    print(f"  Playlist : http://localhost:{PORT}/playlist.m3u")
    print(f"  Test-Stream: http://localhost:{PORT}/live/69776b58036e883f39e5ab8a.m3u8")
    print(f"{'='*50}\n")
    app.run(host=HOST, port=PORT, threaded=True)
