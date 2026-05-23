# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A networked Minitel chat system for a **closed, invite-only artist network**: several
Raspberry Pis, each driving a physical Minitel terminal through an Arduino serial
bridge, all talking to one FastAPI WebSocket server reached over TLS. Read `README.md`
for user-facing feature detail; this file covers the cross-file architecture and the
non-obvious constraints.

The design is **device-token auth** (no passwords), a single config-driven launcher,
and a TLS/VPS deployment. This fully replaces the earlier LAN/password design — all
Minitels are reinstalled onto this build, so there is no backward-compatibility path to
maintain. See `memory/production-target.md` for the remaining roadmap (VPS/TLS, image).

## Running things

No build step, no test suite, no linter. Pure-Python, run directly.

```bash
# Server (on the VPS). MINITELNET_ADMIN_SECRET guards the enroll/revoke endpoints;
# if unset, those endpoints return 503 (chat still works). Bind to localhost and put
# Caddy in front for TLS in production.
cd server && pip install -r requirements.txt
MINITELNET_ADMIN_SECRET=... uvicorn server:app --host 127.0.0.1 --port 8000

# Enroll one device (mints a token server-side, writes /etc/minitelnet/device.json):
MINITELNET_ADMIN_SECRET=... python tools/enroll.py \
  --server https://minitel.example.com --name Camille --apps chat,coeur,snake

# A launcher locally (needs a real serial device or it raises on open). Config comes
# from /etc/minitelnet/device.json, overridable by env vars for dev:
MINITEL_DEVICE_TOKEN=... MINITEL_DISPLAY_NAME=dev MINITELNET_URL=ws://localhost:8000/ws \
  MINITEL_PORT=/dev/ttyUSB0 python pi-client/launcher.py
```

In production everything runs as systemd units (`systemd/*.service`), `Restart=always`.
Per-device config is **not** in the unit files anymore — it lives in
`/etc/minitelnet/device.json` (see below). The unit files are identical on every Pi.

## Per-device config — `pi-client/device_config.py`

The single source of truth for what makes one Pi differ from another. The git tree is
**byte-identical on every box**; only `/etc/minitelnet/device.json` differs, and that
file lives outside the repo so `git pull` never touches it. Both `launcher.py` and
`client.py` call `device_config.load()`.

Precedence: **env var > config file > built-in default**. Keys: `server_url` (the
`wss://…/ws` URL; HTTP base is derived by stripping `/ws`), `token` (device secret),
`display_name` (chat handle), `serial_port` (`""` ⇒ autodetect: first `/dev/ttyUSB*`
then `/dev/ttyACM*`), `apps` (subset of `chat,twitch,coeur,snake`), `show_menu`,
`show_settings`, `keepalive`, `service`. The file is written once by `tools/enroll.py`
at provisioning time.

## Serial pipeline — the core constraint

```
Pi 9600 8N1  ──USB──>  Arduino  ──Serial1 1200 7E1──>  Minitel DIN-5
```

The Arduino sketch (`pi-arduino/minitel_bridge/minitel_bridge.ino`) is a dumb byte
forwarder. The Minitel link is only **1200 baud 7E1** and the Arduino TX buffer is
~64 bytes, so flooding it drops bytes. Everything that writes to the Minitel must pace
itself:

- `Minitel.send_raw` / `async_send_raw` sleep `len(data)/100` seconds after each write.
  This pacing is load-bearing — do not remove it or batch large writes without it.
- `pin 19 INPUT_PULLUP` in the sketch is required (Minitel TX is open-collector);
  without it the keyboard never reaches the Pi.

Display is Videotex: cursor addressing is `0x1F, 0x40+row, 0x40+col` (rows 1-24,
cols 1-40); `0x18` (CAN) clears to end of line; `0x0C` (FF) clears the screen;
`0x11` (XON) is the keepalive byte.

## `pi-client/minitel.py` — the shared driver

- **Sync methods** (`send_*`, `read_input`, `read_password`) are for the
  thread/blocking phases: splash, menus, WiFi password entry.
- **Async methods** (`async_*`) are for the chat event loop.
- `read_input()` maps Minitel function keys to **sentinel command strings** the caller
  must interpret: SOMMAIRE→`/rooms`, RETOUR→`/older`, GUIDE→`/who`, ANNULATION→`/quit`,
  REPETITION→`/clear`; ENVOI ends the line. So `/quit`, `/rooms`, etc. arrive through
  the same channel as typed text — callers branch on these strings.

## `pi-client/client.py` — chat client

No login UI. The client reads its device token from config and sends it in the WS
first frame; the server replies with a `hello` frame carrying the authoritative
`user` (display name) and resumed `room`, which the client uses to draw its header.
Until `hello` arrives, `outgoing_from_minitel` withholds the prompt (`state["ready"]`).

Single asyncio app with two coroutines (`incoming_to_minitel`, `outgoing_from_minitel`)
racing via `asyncio.wait(FIRST_COMPLETED)`. **All Minitel writes go through the global
`_write_lock`** — the fixed 40×24 layout (row 1 header, rows 3-23 rolling messages via
`state["msg_row"]`, row 24 prompt) corrupts if two coroutines interleave cursor moves.
Blocking serial reads run in `asyncio.to_thread`. Auto-reconnects every 5s; a rejected
token (revoked/invalid) surfaces as an `error` frame → 30s back-off then retry.

## `pi-client/launcher.py` — single config-driven launcher

One launcher for every Pi (the old `launcher_58/75/78.py` forks are deleted).
`APP_REGISTRY` maps app keys to command builders; `CFG["apps"]` selects which appear.
`show_menu=False` is the chat-only kiosk mode (old `.75`): no menu, just (re)launch
`client.py` forever. `show_settings` toggles the Reglages entry. `keepalive` toggles
the XON daemon thread.

### Launcher ↔ child-app serial handoff

Only one process may hold the serial device. When the launcher starts a child app
(`run_app`): it sets the keepalive reference to `None`, calls `mt.close()`, then
`subprocess.run(...)` the child (which opens the port itself), and on return `_reopen()`
makes a fresh `Minitel` and restores the keepalive reference. The keepalive runs in a
daemon thread reading a shared `_ka_ref[0]` under `_ka_lock`; `None` means "a child owns
the port, don't touch it." Preserve this close→spawn→reopen sequence for any new entry.

`launcher_settings.py` Reglages (imported when `show_settings`): WiFi (re-runs the
launcher's `wifi_setup` via callback), Mise a jour (`git pull --rebase` in `REPO_DIR`
then `systemctl restart` the unit named by `CFG["service"]`), Addons (toggle `.py` from
`/home/pi/addons/`, persisted in `/home/pi/addons.json`), A propos.

## On-Pi deployment layout (paths are hardcoded)

- Repo checkout: `/home/pi/minitelnet/`. `pi-client/` runs from
  `/home/pi/minitelnet/pi-client/` with a `.venv`.
- Per-device config: `/etc/minitelnet/device.json` (root-owned, 0600).
- `pi-scripts/*.py` deploy to `/home/pi/` directly (the launcher invokes
  `/home/pi/twitch_minitel_v2.py`, `/home/pi/minitel_heart_search.py`) — NOT from the
  repo tree. Addons: `/home/pi/addons/*.py`. Poetry: `/home/pi/texts/poetry_corpus/`.
- Server (separate VPS): `/opt/minitelnet/server/` with `/etc/minitelnet/server.env`
  holding `MINITELNET_ADMIN_SECRET`; Caddy terminates TLS in front of uvicorn.

"Mise a jour" self-updates a Pi by `git pull` + service restart, so committed changes
to `pi-client/` propagate; changes to `pi-scripts/` need manual copy to `/home/pi/`.

## `server/server.py` — FastAPI WebSocket server

- **Device-bound auth, no passwords.** `devices` table: `device_id` (public, e.g.
  `dev_ab12cd…`), `token_hash` (SHA-256 of the secret token; only the hash is stored),
  unique `display_name`, `last_room`, `last_seen`, `revoked`. The WS first frame's
  `token` is hashed and looked up; a valid non-revoked row authorizes the connection.
  There are no in-memory session tokens — the device token is the long-lived credential
  validated against the DB on every connect.
- **Admin endpoints** (guarded by `X-Admin-Secret` header == `MINITELNET_ADMIN_SECRET`,
  constant-time compared; disabled with 503 if the env var is unset): `POST /admin/enroll`
  (mints device_id + token, returns the plaintext token **once**, 409 on duplicate name),
  `POST /admin/revoke` (`{device_id}` → sets `revoked=1`), `GET /admin/devices`.
- SQLite via `aiosqlite` (`minitelnet.db`, auto-created). `ConnectionManager` keeps
  `room → {ws}`, `ws → room`, `ws → user`; broadcasts are room-isolated. Room names
  sanitized to alnum/`-_ `. Message flood limit: 5 msgs / 10s per user.
- WS protocol (JSON): client first frame carries `token` (+ optional `room`); server
  emits `hello` (user+room), then `history_start/history/history_end`, `sys`,
  `who_response`, `msg`, `error`. Client message `type`s: `join`, `get_history`, `who`,
  and plain `msg`. History paginated backward via `before` timestamp (10 per page).

When changing the WS message schema, update both `server.py` and the consumers in
`client.py` (`incoming_to_minitel` / `outgoing_from_minitel`). There are no legacy
clients to keep compatible — the schema can change freely.

## Provisioning a new Minitel (the fleet workflow)

1. Flash the base image, wire Arduino + Pi (identical for every box).
2. Once, at your bench / over Tailscale: `python tools/enroll.py --server https://… --name <Artist>`
   → writes `/etc/minitelnet/device.json` and joins Tailscale.
3. The artist plugs in at home and picks WiFi on the Minitel; nothing else.

Auto-update for the home fleet must pull **pinned release tags** (never bare `main` —
one bad commit bricks every headless box at once) with a post-update health check and
rollback. Tailscale is the recovery backdoor. See `memory/fleet-scalability-constraint.md`.
