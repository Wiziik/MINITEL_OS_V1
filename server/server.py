import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Set

import aiosqlite
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("minitelnet")

_start_time = time.time()

DB_PATH = Path(__file__).parent / "minitelnet.db"
# Admin secret guards device enroll/revoke. If unset, those endpoints are disabled.
ADMIN_SECRET = os.environ.get("MINITELNET_ADMIN_SECRET", "")

_msg_times: Dict[str, deque] = defaultdict(lambda: deque(maxlen=10))

db: aiosqlite.Connection | None = None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- db helpers ----------

async def _db_init() -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            device_id    TEXT PRIMARY KEY,
            token_hash   TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL UNIQUE,
            last_room    TEXT DEFAULT '',
            created_at   REAL NOT NULL,
            last_seen    REAL DEFAULT 0,
            revoked      INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            room     TEXT    NOT NULL,
            username TEXT    NOT NULL,
            message  TEXT    NOT NULL,
            ts       REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_room_ts ON messages (room, ts);
        CREATE INDEX IF NOT EXISTS idx_devices_token ON devices (token_hash);
    """)
    await db.commit()


async def _auth_device(token: str):
    """Return (device_id, display_name, last_room) for a valid, non-revoked
    device token, else None. Updates last_seen on success."""
    if not token:
        return None
    async with db.execute(
        "SELECT device_id, display_name, last_room FROM devices "
        "WHERE token_hash=? AND revoked=0",
        (_hash_token(token),),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    await db.execute(
        "UPDATE devices SET last_seen=? WHERE device_id=?", (time.time(), row[0])
    )
    await db.commit()
    return row[0], row[1], row[2]


async def _save_last_room(device_id: str, room: str) -> None:
    await db.execute(
        "UPDATE devices SET last_room=? WHERE device_id=?", (room, device_id)
    )
    await db.commit()


async def _get_history(room: str, before: float, limit: int = 10) -> list:
    async with db.execute(
        "SELECT username, message, ts FROM messages WHERE room=? AND ts<? "
        "ORDER BY ts DESC LIMIT ?",
        (room, before, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [{"user": r[0], "message": r[1], "ts": r[2]} for r in reversed(rows)]


# ---------- rate limiting (message flood) ----------

def _check_msg_rate(name: str) -> bool:
    now = time.time()
    times = _msg_times[name]
    recent = sum(1 for t in times if now - t < 10)
    times.append(now)
    return recent < 5


# ---------- lifespan ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await aiosqlite.connect(DB_PATH)
    await _db_init()
    if not ADMIN_SECRET:
        log.warning("MINITELNET_ADMIN_SECRET unset — enroll/revoke endpoints disabled.")
    yield
    await db.close()


app = FastAPI(title="MinitelNet Server", lifespan=lifespan)


# ---------- admin: device enrollment ----------

def _require_admin(secret: str | None) -> None:
    if not ADMIN_SECRET:
        raise HTTPException(status_code=503, detail="Enrollment disabled (no admin secret).")
    if not secret or not secrets.compare_digest(secret, ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="Bad admin secret.")


def _clean_name(name: str) -> str:
    return "".join(c for c in str(name).strip()[:20]
                   if c.isalnum() or c in "-_ ").strip()


class EnrollBody(BaseModel):
    display_name: str


class RevokeBody(BaseModel):
    device_id: str


@app.post("/admin/enroll")
async def admin_enroll(body: EnrollBody, x_admin_secret: str | None = Header(default=None)):
    """Mint a new device. Returns the plaintext token ONCE (only its hash is stored).
    The enroll tool writes it to the Pi's /etc/minitelnet/device.json."""
    _require_admin(x_admin_secret)
    name = _clean_name(body.display_name)
    if not name:
        raise HTTPException(status_code=400, detail="Nom invalide (lettres, chiffres, - _).")
    device_id = "dev_" + secrets.token_hex(4)
    token = secrets.token_urlsafe(32)
    try:
        await db.execute(
            "INSERT INTO devices (device_id, token_hash, display_name, created_at) "
            "VALUES (?,?,?,?)",
            (device_id, _hash_token(token), name, time.time()),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        raise HTTPException(status_code=409, detail="Nom deja pris.")
    log.info("Enrolled device %s (%s)", device_id, name)
    return {"device_id": device_id, "token": token, "display_name": name}


@app.post("/admin/revoke")
async def admin_revoke(body: RevokeBody, x_admin_secret: str | None = Header(default=None)):
    _require_admin(x_admin_secret)
    cur = await db.execute(
        "UPDATE devices SET revoked=1 WHERE device_id=?", (body.device_id,)
    )
    await db.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Appareil inconnu.")
    log.info("Revoked device %s", body.device_id)
    return {"ok": True, "device_id": body.device_id}


@app.get("/admin/devices")
async def admin_devices(x_admin_secret: str | None = Header(default=None)):
    _require_admin(x_admin_secret)
    async with db.execute(
        "SELECT device_id, display_name, last_room, last_seen, revoked "
        "FROM devices ORDER BY created_at"
    ) as cur:
        rows = await cur.fetchall()
    return [
        {"device_id": r[0], "display_name": r[1], "last_room": r[2],
         "last_seen": r[3], "revoked": bool(r[4])}
        for r in rows
    ]


# ---------- rooms / connections ----------

def _clean_room(name: str) -> str:
    return "".join(c for c in str(name)[:30] if c.isalnum() or c in "-_ ").strip()


class ConnectionManager:
    def __init__(self) -> None:
        self.rooms: Dict[str, Set[WebSocket]] = defaultdict(set)
        self.ws_room: Dict[WebSocket, str] = {}
        self.ws_user: Dict[WebSocket, str] = {}

    def join(self, ws: WebSocket, room: str, user: str = "") -> None:
        old = self.ws_room.get(ws)
        if old:
            self.rooms[old].discard(ws)
            if not self.rooms[old]:
                del self.rooms[old]
        self.rooms[room].add(ws)
        self.ws_room[ws] = room
        if user:
            self.ws_user[ws] = user

    def leave(self, ws: WebSocket) -> str | None:
        room = self.ws_room.pop(ws, None)
        self.ws_user.pop(ws, None)
        if room:
            self.rooms[room].discard(ws)
            if not self.rooms[room]:
                del self.rooms[room]
        return room

    def list_users(self, room: str) -> list:
        return [self.ws_user[ws] for ws in self.rooms.get(room, set()) if ws in self.ws_user]

    async def broadcast(self, payload: dict, room: str, sender: WebSocket | None = None) -> None:
        dead = []
        for ws in list(self.rooms.get(room, set())):
            if ws is sender:
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.rooms[room].discard(ws)

    def list_rooms(self) -> dict:
        return {r: len(m) for r, m in self.rooms.items() if m}


manager = ConnectionManager()


@app.get("/")
async def root():
    return {"service": "MinitelNet", "rooms": manager.list_rooms()}


@app.get("/rooms")
async def get_rooms():
    return manager.list_rooms()


@app.get("/health")
async def health():
    async with db.execute("SELECT COUNT(*) FROM devices WHERE revoked=0") as cur:
        device_count = (await cur.fetchone())[0]
    return {
        "status": "ok",
        "devices": device_count,
        "rooms": manager.list_rooms(),
        "uptime": round(time.time() - _start_time),
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    user = ""
    room = "general"
    device_id = ""
    try:
        raw = await ws.receive_text()
        data = json.loads(raw)

        device = await _auth_device(str(data.get("token", "")))
        if not device:
            await ws.send_json({"type": "error", "message": "Appareil non autorise"})
            await ws.close()
            return
        device_id, user, last_room = device

        room = _clean_room(data.get("room", "")) or last_room or "general"

        manager.join(ws, room, user)
        await _save_last_room(device_id, room)
        log.info("[#%s] %s joined (%d)", room, user, len(manager.rooms[room]))

        # Tell the client who it is and where it landed, so it can draw its header.
        await ws.send_json({"type": "hello", "user": user, "room": room})

        recent = await _get_history(room, time.time())
        if recent:
            await ws.send_json({"type": "history_start"})
            for msg in recent:
                await ws.send_json({"type": "history", **msg})
            await ws.send_json({"type": "history_end"})

        await manager.broadcast(
            {"type": "sys", "room": room, "message": f"{user} a rejoint #{room}"},
            room=room, sender=ws,
        )

        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Bad JSON: %s", raw[:80])
                continue

            msg_type = data.get("type")

            if msg_type == "join":
                new_room = _clean_room(data.get("room", room)) or room
                old_room = room
                manager.join(ws, new_room, user)
                room = new_room
                await _save_last_room(device_id, room)
                log.info("%s moved #%s -> #%s", user, old_room, room)

                recent = await _get_history(room, time.time())
                if recent:
                    await ws.send_json({"type": "history_start"})
                    for msg in recent:
                        await ws.send_json({"type": "history", **msg})
                    await ws.send_json({"type": "history_end"})

                await manager.broadcast(
                    {"type": "sys", "room": room, "message": f"{user} a rejoint #{room}"},
                    room=room, sender=ws,
                )
                continue

            if msg_type == "get_history":
                before = float(data.get("before", time.time()))
                older = await _get_history(room, before)
                if older:
                    await ws.send_json({"type": "history_start"})
                    for msg in older:
                        await ws.send_json({"type": "history", **msg})
                    await ws.send_json({"type": "history_end"})
                else:
                    await ws.send_json({"type": "sys", "message": "Pas de messages plus anciens."})
                continue

            if msg_type == "who":
                users = manager.list_users(room)
                await ws.send_json({"type": "who_response", "room": room, "users": users})
                continue

            message = str(data.get("message", "")).strip()
            if not message:
                continue

            if not _check_msg_rate(user):
                await ws.send_json({"type": "sys", "message": "* Trop vite!"})
                continue

            log.info("[#%s] <%s> %s", room, user, message)
            await db.execute(
                "INSERT INTO messages (room, username, message, ts) VALUES (?,?,?,?)",
                (room, user, message, time.time()),
            )
            await db.commit()
            await manager.broadcast(
                {"type": "msg", "user": user, "room": room, "message": message},
                room=room, sender=ws,
            )

    except WebSocketDisconnect:
        manager.leave(ws)
        log.info("[#%s] %s disconnected", room, user)
    except Exception as e:
        log.exception("WS error: %s", e)
        manager.leave(ws)
