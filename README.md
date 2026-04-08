# ⚠️ This project has moved!

**plutotv-proxy** has been superseded by [**wakdos-streamhub**](https://github.com/wakdo77/wakdos-streamhub) - a modular rewrite that supports multiple streaming providers, EPG, and more.

> **Note:** wakdos-streamhub is currently a private repository and will be made public soon.

## What changed?

- PlutoTV support is now a plugin within wakdos-streamhub
- Modular provider architecture (easy to add new services)
- XMLTV EPG support
- Optional FFmpeg remux mode
- Active development continues there

👉 **[github.com/wakdo77/wakdos-streamhub](https://github.com/wakdo77/wakdos-streamhub)**

---

### Original Documentation

# plutotv-proxy

![Version](https://img.shields.io/badge/version-v0.5.2-blue)

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
python server.py [--ip <IP>] [--port <PORT>] [--debug]
```

| Argument | Beschreibung | Default |
|---|---|---|
| `--ip` | IP-Adresse, die in Playlist-URLs eingebettet wird | `localhost` |
| `--port` | Port der Flask-App | `8080` |
| `--debug` | Flask Debug-Modus aktivieren | `false` |

Beispiel für den Einsatz im Netzwerk (andere Geräte sollen zugreifen):
```bash
python server.py --ip 192.168.178.65 --port 8080
```

Beim Start werden Session-Daten geladen und die verfügbaren Kanäle abgerufen.

## Endpunkte

| Endpunkt | Beschreibung |
|---|---|
| `GET /playlist.m3u` | M3U-Playlist aller Kanäle |
| `GET /live/<channel_id>.m3u8` | Live-HLS-Stream (beste Qualität, AES-128) |
| `GET /epg.xml` | XMLTV EPG-Feed (30min Cache, 12h Fenster) |

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
3. Einstellungen → EPG-URL:
```
http://<server-ip>:8080/epg.xml
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




