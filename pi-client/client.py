import asyncio
import json
import logging

import websockets
from minitel import Minitel
import device_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pi-client")

CFG          = device_config.load()
SERVER_URL   = CFG["server_url"]
HTTP_URL     = device_config.http_base(SERVER_URL)
SERIAL_PORT  = CFG["serial_port"]
KEEPALIVE    = CFG["keepalive"]
DEVICE_TOKEN = CFG["token"]
DISPLAY_NAME = CFG["display_name"]

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


# ── http helper (rooms list only) ──

def _fetch_rooms() -> dict:
    import urllib.request
    try:
        with urllib.request.urlopen(HTTP_URL + "/rooms", timeout=5) as r:
            result = json.loads(r.read())
        return {k: v for k, v in result.items() if k != "error"}
    except Exception:
        return {}


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
    mt.send_line("  Connexion...")


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
    try:
        while True:
            await asyncio.sleep(30)
            async with _write_lock:
                try:
                    await mt.async_send_raw(b"\x11")
                except Exception as e:
                    log.warning("Keepalive send failed: %s", e)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.error("Keepalive task crashed: %s", e)


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
                await _print_msg(mt, f"* {data.get('message', '')}", state)
            state["auth_error"] = True
            return

        if msg_type == "hello":
            # Server is the source of truth for our handle and resumed room.
            state["room"] = data.get("room", state["room"]) or "general"
            state["username"] = data.get("user", state["username"]) or DISPLAY_NAME
            state["msg_row"] = 3
            async with _write_lock:
                await _draw_header(mt, state["room"], state["username"])
            state["ready"] = True
            continue

        async with _write_lock:
            if msg_type == "history_start":
                await _print_msg(mt, "--- historique ---", state)
            elif msg_type == "history_end":
                await _print_msg(mt, "--- RETOUR: plus ancien ---", state)
            elif msg_type == "history":
                ts = float(data.get("ts", 0))
                if state["oldest_history_ts"] is None or ts < state["oldest_history_ts"]:
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
        # Wait for the server hello (header drawn) before showing a prompt.
        if not state.get("ready"):
            await asyncio.sleep(0.1)
            continue

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
            if before is None:
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
                state["oldest_history_ts"] = None
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
    if not DEVICE_TOKEN:
        log.error("No device token configured (%s). Run the enroll tool first.",
                  device_config.CONFIG_PATH)

    mt = Minitel(port=SERIAL_PORT)
    await asyncio.sleep(2.5)
    mt.ser.reset_input_buffer()

    if KEEPALIVE:
        asyncio.create_task(_keepalive_task(mt))

    await asyncio.to_thread(_show_splash, mt)

    state = {
        "room": "general",
        "username": DISPLAY_NAME,
        "auth_error": False,
        "ready": False,
        "oldest_history_ts": None,
        "msg_row": 3,
    }

    # Double-clear with pause: drain the Arduino pipeline before the cursor-
    # positioned chat layout draws over the splash.
    await mt.async_clear_screen()
    await asyncio.sleep(0.5)
    await mt.async_clear_screen()
    await asyncio.sleep(0.2)

    while True:
        state["ready"] = False
        state["oldest_history_ts"] = None
        try:
            async with websockets.connect(SERVER_URL, ping_interval=30) as ws:
                await ws.send(json.dumps({
                    "type": "join",
                    "token": DEVICE_TOKEN,
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
        except (OSError, websockets.exceptions.WebSocketException) as e:
            log.warning("Connection lost: %s — retrying in 5s", e)
            try:
                async with _write_lock:
                    await _print_msg(mt, "* deconnecte. Retry 5s...", state)
            except Exception:
                pass
            await asyncio.sleep(5)
            continue

        if state.get("auth_error"):
            # Token rejected/revoked — unlikely to fix itself, back off slowly.
            try:
                async with _write_lock:
                    await _print_msg(mt, "* appareil non autorise. Retry 30s.", state)
            except Exception:
                pass
            state["auth_error"] = False
            await asyncio.sleep(30)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
