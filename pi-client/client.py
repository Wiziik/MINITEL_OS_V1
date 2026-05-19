import asyncio
import json
import logging
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

import websockets
from minitel import Minitel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pi-client")

SERVER_URL  = os.environ.get("MINITELNET_URL", "ws://localhost:8000/ws")
HTTP_URL    = SERVER_URL.replace("ws://", "http://").replace("wss://", "https://").rsplit("/ws", 1)[0]
SERIAL_PORT = os.environ.get("MINITEL_PORT", "/dev/ttyUSB0")
KEEPALIVE   = os.environ.get("MINITEL_KEEPALIVE", "0") == "1"

_write_lock = asyncio.Lock()

# ── pixel font ──

GLYPHS = {
    'T': ["###", " # ", " # ", " # ", " # "],
    'V': ["# #", "# #", "# #", " # ", " # "],
    'S': ["###", "#  ", "###", "  #", "###"],
    'O': ["###", "# #", "# #", "# #", "###"],
    'R': ["## ", "# #", "## ", "# #", "# #"],
    'E': ["###", "#  ", "## ", "#  ", "###"],
    'C': ["###", "#  ", "#  ", "#  ", "###"],
    'H': ["# #", "# #", "###", "# #", "# #"],
    'A': [" # ", "# #", "###", "# #", "# #"],
    ' ': ["   ", "   ", "   ", "   ", "   "],
    '1': [" # ", "## ", " # ", " # ", "###"],
    '3': ["###", "  #", "###", "  #", "###"],
    '5': ["###", "#  ", "###", "  #", "###"],
    '6': ["###", "#  ", "###", "# #", "###"],
}


def _big_text(text: str, width: int = 40) -> list:
    rows = [""] * 5
    chars = list(text.upper())
    for i, c in enumerate(chars):
        g = GLYPHS.get(c, ["   "] * 5)
        for j in range(5):
            rows[j] += g[j]
            if i < len(chars) - 1:
                rows[j] += " "
    pad = max(0, (width - len(rows[0])) // 2)
    return [" " * pad + r for r in rows]


def _clean_room(name: str) -> str:
    return "".join(c for c in name.strip()[:30] if c.isalnum() or c in "-_ ").strip()


def _truncate(text: str, width: int = 38) -> str:
    return text if len(text) <= width else text[:width - 1] + "~"


# ── http helpers ──

def _http_get(path: str) -> dict:
    try:
        with urllib.request.urlopen(HTTP_URL + path, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def _http_post(path: str, data: dict) -> dict:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        HTTP_URL + path, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return {"ok": False, "error": json.loads(e.read()).get("detail", str(e))}
        except Exception:
            return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _fetch_rooms() -> dict:
    result = _http_get("/rooms")
    return {k: v for k, v in result.items() if k != "error"}


# ── splash (sync, runs in thread) ──

def _show_splash(mt: Minitel) -> None:
    mt.clear_screen()
    mt.send_line("")
    for row in _big_text("3615"):
        mt.send_line(row)
    mt.send_line("")
    for row in _big_text("TV STORE"):
        mt.send_line(row)
    mt.send_line("")
    mt.send_line("----------------------------------------")


# ── fixed layout helpers (async, called within _write_lock) ──

async def _draw_header(mt: Minitel, room: str, username: str) -> None:
    """Draw the fixed header (rows 1-2) and place cursor at row 24 prompt."""
    header = _truncate(f"#{room} | {username}", 40)
    await mt.async_move_cursor(1, 1)
    await mt.async_clear_to_eol()
    await mt.async_send_text(header)
    await mt.async_move_cursor(2, 1)
    await mt.async_send_text("-" * 40)
    await mt.async_move_cursor(24, 1)
    await mt.async_clear_to_eol()
    await mt.async_send_text("> ")


async def _print_msg(mt: Minitel, text: str, state: dict) -> None:
    """Write one line to the rolling message area (rows 3-23).
    Caller MUST hold _write_lock. Mutates state['msg_row']."""
    row = state["msg_row"]
    await mt.async_move_cursor(row, 1)
    await mt.async_clear_to_eol()
    await mt.async_send_text(_truncate(text))
    state["msg_row"] = 3 if row >= 23 else row + 1
    # Keep input prompt visible at row 24
    await mt.async_move_cursor(24, 1)
    await mt.async_send_text("> ")


# ── keepalive ──

async def _keepalive_task(mt: Minitel) -> None:
    while True:
        await asyncio.sleep(30)
        async with _write_lock:
            try:
                await mt.async_send_raw(b"\x11")
            except Exception:
                pass


# ── auth (sync MT calls — sequential, acceptable blocking) ──

async def authenticate(mt: Minitel) -> tuple:
    """Returns (username, token, last_room).
    Does NOT clear the screen — the splash logo above stays visible."""
    while True:
        mt.send_line("")
        mt.send_line("  CONNEXION")
        mt.send_line("")
        mt.send_line("  Nom d'utilisateur:")
        mt.send_text("  > ")

        username = (await asyncio.to_thread(mt.read_input)).strip().lower()
        if not username or username in ("/older", "/rooms", "/who", "/clear", "/quit"):
            continue

        exists_data = await asyncio.to_thread(
            _http_get, f"/auth/exists/{urllib.parse.quote(username)}"
        )

        if exists_data.get("exists"):
            mt.send_line("")
            mt.send_line("  Mot de passe:")
            mt.send_text("  > ")
            password = await asyncio.to_thread(mt.read_password)
            result = await asyncio.to_thread(
                _http_post, "/auth/login", {"username": username, "password": password}
            )
            if result.get("ok"):
                mt.send_line("")
                mt.send_line(f"  Bonjour {result['username']}!")
                await asyncio.sleep(1.5)
                return result["username"], result["token"], result.get("last_room", "")
            else:
                mt.send_line("")
                mt.send_line(f"  * {result.get('error', 'Erreur')}")
                await asyncio.sleep(2)
        else:
            mt.send_line("")
            mt.send_line(f"  Nouveau compte: {username}")
            mt.send_line("  Mot de passe:")
            mt.send_text("  > ")
            password = await asyncio.to_thread(mt.read_password)
            if not password:
                mt.send_line("")
                mt.send_line("  * Mot de passe vide!")
                await asyncio.sleep(2)
                continue
            mt.send_line("")
            mt.send_line("  Confirmez:")
            mt.send_text("  > ")
            confirm = await asyncio.to_thread(mt.read_password)
            if password != confirm:
                mt.send_line("")
                mt.send_line("  * Mots de passe differents!")
                await asyncio.sleep(2)
                continue
            result = await asyncio.to_thread(
                _http_post, "/auth/register", {"username": username, "password": password}
            )
            if result.get("ok"):
                mt.send_line("")
                mt.send_line(f"  Bienvenue {result['username']}!")
                await asyncio.sleep(1.5)
                return result["username"], result["token"], result.get("last_room", "")
            else:
                mt.send_line("")
                mt.send_line(f"  * {result.get('error', 'Erreur')}")
                await asyncio.sleep(2)


# ── room picker (sync MT calls) ──

async def pick_room(mt: Minitel, last_room: str = "") -> str:
    mt.clear_screen()
    mt.send_line("")
    if last_room:
        mt.send_line(f"  Reprendre: #{last_room}")
        mt.send_line("  (ENVOI vide pour reprendre)")
        mt.send_line("")
    rooms = await asyncio.to_thread(_fetch_rooms)
    room_list = list(rooms.items())
    if room_list:
        mt.send_line("  Salles actives:")
        for i, (name, count) in enumerate(room_list[:6], 1):
            mt.send_line(f"  {i}. {name} ({count})")
    else:
        mt.send_line("  Aucune salle active.")
    mt.send_line("")
    mt.send_line("  Nom de la salle puis ENVOI:")
    mt.send_text("  > ")

    raw = (await asyncio.to_thread(mt.read_input)).strip()

    if raw == "/clear":
        return await pick_room(mt, last_room=last_room)  # redraw
    if raw in ("", "/older") and last_room:
        return last_room
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(room_list):
            return room_list[idx][0]
    return _clean_room(raw) or last_room or "general"


# ── chat coroutines ──

async def incoming_to_minitel(ws, mt: Minitel, state: dict):
    async for raw in ws:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        msg_type = data.get("type")

        if msg_type == "error":
            async with _write_lock:
                await _print_msg(mt, f"* ERREUR: {data.get('message', '')}", state)
            state["auth_error"] = True
            return

        async with _write_lock:
            if msg_type == "history_start":
                await _print_msg(mt, "--- historique ---", state)
            elif msg_type == "history_end":
                await _print_msg(mt, "--- RETOUR: plus ancien ---", state)
            elif msg_type == "history":
                ts = float(data.get("ts", 0))
                if ts < state.get("oldest_history_ts", float("inf")):
                    state["oldest_history_ts"] = ts
                user = str(data.get("user", "???"))[:12]
                msg  = str(data.get("message", ""))
                await _print_msg(mt, f"[{user}] {msg}", state)
            elif msg_type == "sys":
                await _print_msg(mt, f"* {data.get('message', '')}", state)
            elif msg_type == "who_response":
                users = data.get("users", [])
                room  = data.get("room", "")
                await _print_msg(mt, f"#{room}: {', '.join(users) or 'personne'}", state)
            else:
                user = str(data.get("user", "???"))[:12]
                msg  = str(data.get("message", ""))
                await _print_msg(mt, f"<{user}> {msg}", state)


async def outgoing_from_minitel(ws, mt: Minitel, state: dict):
    while True:
        # Ensure prompt is visible before blocking on input
        async with _write_lock:
            await mt.async_move_cursor(24, 1)
            await mt.async_clear_to_eol()
            await mt.async_send_text("> ")

        line = (await asyncio.to_thread(mt.read_input)).strip()
        if not line:
            continue

        if line.lower() in ("/quit", "/q"):
            async with _write_lock:
                await _print_msg(mt, "Au revoir.", state)
            await ws.close()
            raise SystemExit(0)

        if line.lower() == "/rooms":
            rooms = await asyncio.to_thread(_fetch_rooms)
            async with _write_lock:
                if rooms:
                    for name, count in rooms.items():
                        await _print_msg(mt, f"  #{name} ({count})", state)
                else:
                    await _print_msg(mt, "  Aucune salle.", state)
            continue

        if line.lower() == "/older":
            before = state.get("oldest_history_ts")
            if before is None or before == float("inf"):
                async with _write_lock:
                    await _print_msg(mt, "  Pas encore d'historique.", state)
                continue
            await ws.send(json.dumps({"type": "get_history", "before": before}))
            continue

        if line.lower() == "/who":
            await ws.send(json.dumps({"type": "who"}))
            continue

        if line.lower() == "/clear":
            state["msg_row"] = 3
            async with _write_lock:
                await mt.async_clear_screen()
                await asyncio.sleep(0.2)
                await _draw_header(mt, state["room"], state["username"])
            continue

        if line.lower().startswith("/join "):
            new_room = _clean_room(line[6:])
            if new_room:
                await ws.send(json.dumps({"type": "join", "room": new_room}))
                state["room"] = new_room
                state["oldest_history_ts"] = float("inf")
                state["msg_row"] = 3
                async with _write_lock:
                    await _draw_header(mt, new_room, state["username"])
            continue

        await ws.send(json.dumps({
            "type": "msg",
            "user": state["username"],
            "room": state["room"],
            "message": line,
        }))
        async with _write_lock:
            await _print_msg(mt, f"<{state['username']}> {line}", state)


# ── main loop ──

async def run():
    mt = Minitel(port=SERIAL_PORT)
    await asyncio.sleep(2.5)
    mt.ser.reset_input_buffer()

    if KEEPALIVE:
        asyncio.create_task(_keepalive_task(mt))

    while True:
        # Splash stays on screen during auth — no clear inside authenticate()
        await asyncio.to_thread(_show_splash, mt)
        username, token, last_room = await authenticate(mt)
        room = await pick_room(mt, last_room=last_room)
        state = {
            "room": room,
            "token": token,
            "username": username,
            "auth_error": False,
            "oldest_history_ts": float("inf"),
            "msg_row": 3,
        }

        # Double-clear with pause: ensures Arduino pipeline is drained before
        # cursor-positioned chat layout draws over any previous content.
        await mt.async_clear_screen()
        await asyncio.sleep(0.5)
        await mt.async_clear_screen()
        await asyncio.sleep(0.2)
        await _draw_header(mt, room, username)

        while not state.get("auth_error"):
            try:
                async with websockets.connect(SERVER_URL, ping_interval=30) as ws:
                    await ws.send(json.dumps({
                        "type": "join",
                        "token": token,
                        "room": state["room"],
                    }))
                    tasks = [
                        asyncio.create_task(incoming_to_minitel(ws, mt, state)),
                        asyncio.create_task(outgoing_from_minitel(ws, mt, state)),
                    ]
                    done, pending = await asyncio.wait(
                        tasks, return_when=asyncio.FIRST_COMPLETED
                    )
                    for t in pending:
                        t.cancel()
                    if state.get("auth_error"):
                        break
            except (OSError, websockets.exceptions.WebSocketException) as e:
                log.warning("Connection lost: %s — retrying in 5s", e)
                try:
                    async with _write_lock:
                        await _print_msg(mt, "* deconnecte. Retry 5s...", state)
                except Exception:
                    pass
                await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
