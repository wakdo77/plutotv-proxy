"""
PlutoTV Stream Proxy Server
============================
Lokaler HTTP-Proxy, der PlutoTV-Livestreams für VLC, Kodi IPTV Simple und Enigma2 bereitstellt.

Endpunkte:
  GET /playlist.m3u          - M3U-Playlist aller Kanäle
  GET /live/<channel_id>     - Live-HLS-Stream (beste Variante, Segmente direkt vom CDN)

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
from datetime import datetime, timezone, timedelta
from html import escape as _xe
import argparse

import requests
from flask import Flask, Response, abort

# ─── Konfiguration ───────────────────────────────────────────────────────────

__version__ = "0.5.1"

HOST = "0.0.0.0"

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

PLUTO_EPG_DURATION_MIN  = 720   # Minuten EPG-Dauer pro Request (kann je nach Bedarf angepasst werden)
PLUTO_EPG_BATCH_SIZE    = 100   # Kanal-IDs pro API-Request (URL-Länge begrenzen)
PLUTO_EPG_CACHE_SECONDS = 1800  # Cache-Lebensdauer in Sekunden (30 Minuten)

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

    # ── EPG-Funktionalität ─────────────────────────────

    def _fetch_epg_batch(self, channel_ids: list[str]) -> dict:
        """Holt EPG-Timelines für einen Batch von Kanal-IDs vom PlutoTV-API."""
        last_hour_iso = _iso_time(encode=False)
        url = (
            self.channels_server
            + "/v2/guide/timelines"
            + f"?start={last_hour_iso}"
            + f"&channelIds={','.join(channel_ids)}"
            + f"&duration={PLUTO_EPG_DURATION_MIN}"
        )
        resp = self.http.get(url, headers={"Authorization": f"Bearer {self.jwt}"})
        if not resp.ok:
            print(f"[EPG] Fehler {resp.status_code} für Batch ({len(channel_ids)} Kanäle)")
            return []
        return resp.json().get("data", [])

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

    Besonderheit: #EXT-X-ENDLIST wird herausgefiltert.
    Dieser Tag signalisiert dem Player das Stream-Ende (VOD-Verhalten) und tritt
    bei PlutoTV an Sendungsgrenzen auf, was den Stream in Kodi stoppt.
    """
    # Basis: Verzeichnis der Playlist-URL (ohne Query-String)
    base = playlist_url.split("?")[0].rsplit("/", 1)[0] + "/"

    result = []
    for line in playlist_content.splitlines():
        clean = line.strip()

        # #EXT-X-ENDLIST herausfiltern: PlutoTV sendet zwischen Sendungen eine leere
        # Playlist mit diesem Tag, was Kodi den Stream stoppen lässt.
        if clean == "#EXT-X-ENDLIST":
            print("[Proxy] #EXT-X-ENDLIST gefunden - entfernt, um Stream-Stop in Kodi zu verhindern.")
            continue

        # Segment-Zeilen: nicht leer, kein '#'
        if clean and not clean.startswith("#"):
            if not clean.startswith("http"):
                clean = urllib.parse.urljoin(base, clean)
            result.append(clean)
        else:
            result.append(line.rstrip("\r"))

    return "\n".join(result)


def _iso_now(encode: bool = True) -> str:
    """Gibt den aktuellen UTC-Zeitstempel URL-codiert im PlutoTV-Format zurück."""
    dt = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    if encode:
        return urllib.parse.quote(dt, safe="")
    return dt

def _iso_time(iso_time: str = None, encode: bool = False) -> str:
    """Gibt PlutoTV-ISO-Format MIT IMMER .000 Millisekunden: bei iso_time parsen, sonst UTC minus 1 Stunde."""
    if iso_time is None:
        # Aktuelle UTC minus 1 Stunde, Sekunden/Ms auf 0
        dt = datetime.now(timezone.utc) - timedelta(hours=1)
        dt = dt.replace(second=0, microsecond=0)
    else:
        # Parse und explizit Sekunden/Ms auf 0
        dt_naive = datetime.strptime(iso_time, "%Y-%m-%d %H:%M:%S").replace(microsecond=0)
        dt = dt_naive.replace(tzinfo=timezone.utc)
    
    iso_str = dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    
    if encode:
        return urllib.parse.quote(iso_str, safe="")
    return iso_str

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


# ─── EPG Builder ───────────────────────────────────────────────────────────────

def _xmltv_time(iso_str: str) -> str:
    """
    Konvertiert ISO 8601 UTC-String ins XMLTV-Zeitformat: YYYYMMDDHHmmss +0000.
    Python 3.10-kompatibel: strptime statt fromisoformat (3-stellige ms-Problem).
    """
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(iso_str, fmt).strftime("%Y%m%d%H%M%S +0000")
        except ValueError:
            continue
    # Fallback für Python 3.11+
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).strftime("%Y%m%d%H%M%S +0000")


def build_epg_xml() -> str:
    """
    Holt EPG-Daten für alle Kanäle in Batches und gibt einen XMLTV-String zurück.
    Zeitfenster: 2h zurück bis +10h (via _iso_time Default + PLUTO_EPG_DURATION_MIN).
    """
    all_ids = [ch.get("id", "") for ch in pluto.channels if ch.get("id")]

    # EPG in Batches abrufen, positional zu channel_ids zuordnen
    timelines_by_channel: dict[str, list] = {}
    for i in range(0, len(all_ids), PLUTO_EPG_BATCH_SIZE):
        batch_ids  = all_ids[i : i + PLUTO_EPG_BATCH_SIZE]
        batch_data = pluto._fetch_epg_batch(batch_ids)
        for j, item in enumerate(batch_data):
            ch_id = item.get("channelId") or (batch_ids[j] if j < len(batch_ids) else None)
            if ch_id:
                timelines_by_channel[ch_id] = item.get("timelines", [])

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<tv generator-info-name="plutotv-proxy">',
    ]

    # <channel>-Einträge
    for ch in pluto.channels:
        ch_id = ch.get("id", "")
        name  = _xe(ch.get("name", ch_id))
        logo  = get_logo(ch)
        lines.append(f'  <channel id="{_xe(ch_id, quote=True)}">')
        lines.append(f'    <display-name>{name}</display-name>')
        if logo:
            lines.append(f'    <icon src="{_xe(logo, quote=True)}"/>')
        lines.append('  </channel>')

    # <programme>-Einträge
    for ch in pluto.channels:
        ch_id = ch.get("id", "")
        for tl in timelines_by_channel.get(ch_id, []):
            start = _xmltv_time(tl.get("start", ""))
            stop  = _xmltv_time(tl.get("stop",  ""))
            title = _xe(tl.get("title", ""))

            ep      = tl.get("episode", {})
            desc    = _xe(ep.get("description", ""))
            genre   = _xe(ep.get("genre", ""))
            ep_name = _xe(ep.get("name", ""))
            season  = ep.get("season",  0)
            ep_num  = ep.get("number",  0)
            rating  = _xe(ep.get("rating", ""))
            thumb   = ep.get("thumbnail", {}).get("path", "")
            series  = ep.get("series", {}).get("name", "")

            lines.append(f'  <programme start="{start}" stop="{stop}" channel="{_xe(ch_id, quote=True)}">')
            lines.append(f'    <title>{title}</title>')
            if ep_name:
                lines.append(f'    <sub-title>{ep_name}</sub-title>')
            if desc:
                lines.append(f'    <desc>{desc}</desc>')
            if series and ep_num:
                s = max(0, season - 1)
                e = max(0, ep_num  - 1)
                lines.append(f'    <episode-num system="xmltv_ns">{s}.{e}.0/1</episode-num>')
            if genre:
                lines.append(f'    <category>{genre}</category>')
            if thumb:
                lines.append(f'    <icon src="{_xe(thumb, quote=True)}"/>')
            if rating:
                lines.append(f'    <rating><value>{rating}</value></rating>')
            lines.append('  </programme>')

    lines.append('</tv>')
    return '\n'.join(lines)


# ─── Flask App ───────────────────────────────────────────────────────────────────

# EPG Cache
_epg_cache: str = ""
_epg_cache_time: float = 0.0

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

    # 4.
    content = make_segments_absolute(v_resp.text, v_url)

    return Response(content, mimetype="application/vnd.apple.mpegurl")

@app.route("/epg.xml")
def epg_xml():
    """
    XMLTV EPG-Feed für alle PlutoTV-Kanäle.
    Kompatibel mit Kodi IPTV Simple Client (EPG-URL-Einstellung).
    Gecacht für PLUTO_EPG_CACHE_SECONDS Sekunden (Standard: 30 Minuten).
    """
    global _epg_cache, _epg_cache_time

    pluto.ensure_valid()
    if not _epg_cache or time.time() - _epg_cache_time > PLUTO_EPG_CACHE_SECONDS:
        print("[EPG] Aktualisiere EPG-Cache ...")
        _epg_cache      = build_epg_xml()
        _epg_cache_time = time.time()
        print(f"[EPG] Cache aktualisiert ({len(_epg_cache):,} Bytes).")

    return Response(_epg_cache, mimetype="application/xml")
# ─── Start ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PlutoTV Stream Proxy")
    parser.add_argument(
        "--ip",
        default="localhost",
        help="IP-Adresse für Playlist-URLs (default: localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port der Flask-App (default: 8080)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Flask Debug-Modus aktivieren",
    )
    args = parser.parse_args()

    # Globale Konfiguration aus CLI-Argumenten setzen
    PORT       = args.port
    PROXY_BASE = f"http://{args.ip}:{args.port}"

    print(f"\n{'='*50}")
    print(f"  PlutoTV Stream Proxy  v{__version__}")
    print(f"{'='*50}")
    print(f"  Playlist   : {PROXY_BASE}/playlist.m3u")
    print(f"  EPG        : {PROXY_BASE}/epg.xml")
    print(f"  Test-Stream: {PROXY_BASE}/live/69776b58036e883f39e5ab8a.m3u8")
    print(f"  Debug      : {args.debug}")
    print(f"{'='*50}\n")
    app.run(host=HOST, port=PORT, threaded=not args.debug, debug=args.debug)
