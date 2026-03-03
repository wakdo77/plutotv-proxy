# plutotv-proxy

Lokaler HTTP-Proxy, der PlutoTV FAST-Kanäle als Standard-HLS-Streams bereitstellt.  
Kompatibel mit **VLC**, **Kodi (IPTV Simple Client)** und **Enigma2**.

## Wie es funktioniert

PlutoTV-Streams benötigen ein JWT-Token zur Authentifizierung, das nach ~24 Stunden abläuft.  
Der Proxy übernimmt das vollständig automatisch: Session beim Start holen, JWT bei allen Requests ergänzen, rechtzeitig erneuern.

```
Player (VLC / Kodi / Enigma2)
    ↓  GET /playlist.m3u  oder  /live/<channel_id>.m3u8
plutotv-proxy  (localhost:8080)
    ↓  Master + Varianten-Playlist (mit JWT)
PlutoTV Stitcher
    ↓  .ts Segmente (direkt vom CDN, kein Proxy-Overhead)
Player
```

## Voraussetzungen

- Python 3.10+
- pip

## Installation

```bash
git clone https://github.com/wakdo77/plutotv-proxy.git
cd plutotv-proxy
python -m venv venv

# Windows
venv\Scripts\activate.ps1

# Linux / macOS
source venv/bin/activate

pip install -r requirements.txt
```

## Starten

```bash
python server.py
```

Beim Start werden Session-Daten geladen und die verfügbaren Kanäle abgerufen.  
Der Server läuft dann auf `http://0.0.0.0:8080`.

## Endpunkte

| Endpunkt | Beschreibung |
|---|---|
| `GET /playlist.m3u` | M3U-Playlist aller Kanäle |
| `GET /live/<channel_id>.m3u8` | Live-HLS-Stream (beste Qualität, AES-128) |

## Einrichtung in Playern

### VLC
Medien → Netzwerkstream öffnen (Strg+N):
```
http://localhost:8080/live/<channel_id>.m3u8
```
Oder alle Kanäle auf einmal:
```
http://localhost:8080/playlist.m3u
```

### Kodi – PVR IPTV Simple Client
1. Add-on installieren: *PVR IPTV Simple Client*
2. Einstellungen → M3U-Playlist-URL:
```
http://<server-ip>:8080/playlist.m3u
```

### Enigma2 (z.B. VU+)
Plugin `e2m3u2bouquet` oder direkt als M3U-Quelle:
```
http://<server-ip>:8080/playlist.m3u
```

## Hinweise

- Der Server muss laufen, solange du streamst.
- PlutoTV ist nur in bestimmten Regionen verfügbar (u.a. DE, US, UK).
- Segmente werden direkt vom PlutoTV-CDN geladen – der Proxy leitet nur die Playlists durch.

## Lizenz

MIT
