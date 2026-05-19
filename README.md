# MINITEL_OS_V1

A networked Minitel chat and media system for TV STORE, built on three Raspberry Pis.

## Architecture

```
192.168.1.50  — Pi client A  (launcher + MinitelNet + Twitch + Coeur Poétique)
192.168.1.75  — Pi client B  (MinitelNet chat only)
192.168.1.76  — Server       (FastAPI WebSocket server + SQLite)
```

Each Pi connects to a Minitel terminal via an Arduino Mega bridge:
`Pi 9600 8N1 → Arduino USB → Arduino Serial1 1200 7E1 → Minitel DIN`

---

## Folder structure

```
server/
  server.py          FastAPI WebSocket server with auth, rooms, history
  requirements.txt   fastapi, uvicorn, websockets, aiosqlite

pi-client/
  launcher.py        Boot menu on .50: choose between 3 apps
  client.py          MinitelNet chat client (async, fixed layout)
  minitel.py         Minitel serial driver (sync + async methods)

pi-scripts/
  twitch_minitel_v2.py    Two-way Twitch IRC bridge (TLS, reconnect, echo)
  minitel_heart_search.py Apollinaire calligramme heart poem search
  minitel_keepalive.py    Screen-on keepalive daemon (XON every 30s)

systemd/
  minitelnet-server-76.service    Server service (.76)
  minitelnet-client-50.service    Launcher service (.50, KEEPALIVE=1)
  minitelnet-client-75.service    Chat client service (.75)
```

---

## Server (192.168.1.76)

**Endpoints:**
- `GET  /rooms` — list active rooms and member counts
- `GET  /auth/exists/{username}` — check if account exists
- `POST /auth/login` — login, returns token
- `POST /auth/register` — create account, returns token
- `WS   /ws` — WebSocket chat (requires token in first message)

**Features:**
- SHA-256 + salt password hashing
- In-memory session tokens (cleared on restart → clients re-auth automatically)
- SQLite persistence: accounts + full message history
- Multi-room broadcast isolation
- Last room saved per user
- Rate limiting: 3 login failures → 30s lockout; 5 messages/10s flood block
- History: last 100 messages per room, sent on join, paginated via RETOUR

**Run:**
```bash
cd /home/pi/minitelnet/server
.venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
```

---

## Pi Client — launcher (192.168.1.50 only)

Boot menu showing the **3615 TV STORE** pixel-art splash. User picks:
1. MinitelNet Chat
2. Twitch TV STORE
3. Coeur Poétique

When the chosen app exits (or `/quit` is used), the menu reappears.

### WiFi setup at boot

If no internet is detected on startup, the launcher shows a WiFi picker before the menu:

```
WIFI
----------------------------------------

 *1. MyNetwork_5G        #### WPA
  2. Livebox-ABCD        ###  WPA
  3. FreeWifi            ##   --

  0. Ignorer (pas de WiFi)

  Numero + ENVOI:
  > 
```

- Waits 10s for NetworkManager to auto-connect first
- Lists up to 7 networks sorted by signal strength (`*` = currently active)
- `####` = signal bars (75 / 50 / 25 / weak)
- Password entry masked with `*`
- `0` skips to the menu in offline mode
- Uses `nmcli` — requires NetworkManager on the Pi

---

## Pi Client — MinitelNet chat

**Fixed layout (40×24):**
- Row 1: `#room | username` status bar
- Row 2: separator
- Rows 3–23: rolling message area (21 slots, overwrites oldest)
- Row 24: `> ` input line

**Commands:**
| Input | Action |
|---|---|
| ENVOI | Send message |
| CORRECTION | Backspace |
| SOMMAIRE | List rooms (`/rooms`) |
| RETOUR | Load older history |
| GUIDE | Who's in this room (`/who`) |
| ANNULATION | Quit to launcher |
| `/join nom` + ENVOI | Switch room (clears screen) |
| `/quit` + ENVOI | Quit to launcher |

**Features:**
- Splash: 3×5 pixel-art block font (3615 + TV STORE)
- Auth: login or register with password (masked with `*`)
- Room resume: last room remembered per account
- History: 10 messages on join, RETOUR loads 10 more
- `/who` shows who is in the current room
- Keepalive XON sent every 30s (`.50` only, via `MINITEL_KEEPALIVE=1`)
- Auto-reconnect every 5s on connection loss

---

## Twitch Bridge

Two-way IRC bridge over TLS (port 6697).

- Incoming Twitch chat → Minitel (timestamped `HH:MM user: msg`)
- Typed text + ENVOI → sent as PRIVMSG
- Unicode normalized to ASCII, accents stripped cleanly
- Auto-reconnect on socket drop
- 60s socket timeout + TCP keepalive
- `/quit` + ENVOI returns to launcher

**Credentials via env vars:**
```
TWITCH_TOKEN=oauth:...
TWITCH_NICK=tv_store
TWITCH_CHANNEL=#tv_store
```

---

## Coeur Poétique

Displays French poems as Apollinaire-style heart calligrammes.
Type a word + ENVOI to search. SUITE cycles through matches.
`/quit` + ENVOI returns to launcher.

Poems live in `/home/pi/texts/poetry_corpus/`.

---

## Key technical notes

- **Arduino baud bridge:** Pi talks 9600 8N1 to Arduino; Arduino forwards at 1200 7E1 to Minitel. Rate limiter (`len(data)/100` seconds per send) prevents Arduino 64-byte TX buffer overflow.
- **Async rate limiter:** `async_send_raw` uses `await asyncio.sleep` so the event loop is free during paced writes.
- **Cursor addressing:** Minitel Videotex US format — `0x1F, 0x40+row, 0x40+col`.
- **CAN (0x18):** clears to end of line in videotex mode.

---

## Deployment

All three services run as systemd units, enabled at boot, auto-restart on crash.

```bash
# Server (.76)
sudo systemctl enable --now minitelnet-server

# Clients (.50 and .75)
sudo systemctl enable --now minitelnet-client
```
