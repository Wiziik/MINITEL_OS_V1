import hashlib
import json
import logging
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Set

import aiosqlite
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("minitelnet")

DB_PATH = Path(__file__).parent / "minitelnet.db"
TOKENS: Dict[str, str] = {}
_login_failures: Dict[str, list] = {}
_msg_times: Dict[str, deque] = defaultdict(lambda: deque(maxlen=10))

db: aiosqlite.Connection | None = None


# ---------- db helpers ----------

async def _db_init() -> None:
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            username     TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            last_room    TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS messages (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            room     TEXT    NOT NULL,
            username TEXT    NOT NULL,
            message  TEXT    NOT NULL,
            ts       REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_room_ts ON messages (room, ts);
    """)
    await db.commit()


async def _migrate_accounts_json() -> None:
    old = Path(__file__).parent / "accounts.json"
    if not old.exists():
        return
    try:
        data = json.loads(old.read_text())
        rows = [(u, v["password_hash"], v.get("last_room", "")) for u, v in data.items()]
        await db.executemany(
            "INSERT OR IGNORE INTO users (username, password_hash, last_room) VALUES (?,?,?)",
            rows,
        )
        await db.commit()
        old.rename(old.with_suffix(".json.bak"))
        log.info("Migrated %d accounts from accounts.json", len(data))
    except Exception as e:
        log.warning("Migration failed: %s", e)


async def _save_last_room(username: str, room: str) -> None:
    await db.execute("UPDATE users SET last_room=? WHERE username=?", (room, username))
    await db.commit()


async def _get_history(room: str, before: float, limit: int = 10) -> list:
    async with db.execute(
        "SELECT username, message, ts FROM messages WHERE room=? AND ts<? ORDER BY ts DESC LIMIT ?",
        (room, before, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [{"user": r[0], "message": r[1], "ts": r[2]} for r in reversed(rows)]


# ---------- rate limiting ----------

def _check_login_rate(username: str) -> None:
    now = time.time()
    recent = [t for t in _login_failures.get(username, []) if now - t < 30]
    _login_failures[username] = recent
    if len(recent) >= 3:
        raise HTTPException(status_code=429, detail="Trop de tentatives. Attendez 30s.")


def _record_login_failure(username: str) -> None:
    _login_failures.setdefault(username, []).append(time.time())


def _clear_login_failures(username: str) -> None:
    _login_failures.pop(username, None)


def _check_msg_rate(username: str) -> bool:
    now = time.time()
    times = _msg_times[username]
    recent = sum(1 for t in times if now - t < 10)
    times.append(now)
    return recent < 5


# ---------- passwords ----------

def _hash_pw(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def _verify_pw(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        return hashlib.sha256(f"{salt}{password}".encode()).hexdigest() == h
    except Exception:
        return False


# ---------- lifespan ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = await aiosqlite.connect(DB_PATH)
    await _db_init()
    await _migrate_accounts_json()
    yield
    await db.close()


app = FastAPI(title="MinitelNet Server", lifespan=lifespan)


# ---------- auth ----------

class AuthBody(BaseModel):
    username: str
    password: str


@app.get("/auth/exists/{username}")
async def auth_exists(username: str):
    async with db.execute(
        "SELECT 1 FROM users WHERE username=?", (username.strip().lower(),)
    ) as cur:
        row = await cur.fetchone()
    return {"exists": row is not None}


@app.post("/auth/login")
async def auth_login(body: AuthBody):
    user = body.username.strip().lower()[:20]
    _check_login_rate(user)
    async with db.execute(
        "SELECT password_hash, last_room FROM users WHERE username=?", (user,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        _record_login_failure(user)
        raise HTTPException(status_code=401, detail="Utilisateur inconnu")
    if not _verify_pw(body.password, row[0]):
        _record_login_failure(user)
        raise HTTPException(status_code=401, detail="Mot de passe incorrect")
    _clear_login_failures(user)
    token = secrets.token_hex(32)
    TOKENS[token] = user
    return {"ok": True, "token": token, "username": user, "last_room": row[1] or ""}


@app.post("/auth/register")
async def auth_register(body: AuthBody):
    user = body.username.strip().lower()[:20]
    if not user or not all(c.isalnum() or c in "-_" for c in user):
        raise HTTPException(status_code=400, detail="Nom invalide (lettres, chiffres, - _)")
    if len(body.password) < 3:
        raise HTTPException(status_code=400, detail="Mot de passe trop court (min 3)")
    try:
        await db.execute(
            "INSERT INTO users (username, password_hash, last_room) VALUES (?,?,?)",
            (user, _hash_pw(body.password), ""),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        raise HTTPException(status_code=409, detail="Nom deja pris")
    token = secrets.token_hex(32)
    TOKENS[token] = user
    log.info("New account: %s", user)
    return {"ok": True, "token": token, "username": user, "last_room": ""}


# ---------- rooms ----------

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


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    user = ""
    room = "general"
    try:
        raw = await ws.receive_text()
        data = json.loads(raw)

        token = str(data.get("token", ""))
        if token not in TOKENS:
            await ws.send_json({"type": "error", "message": "Non autorise"})
            await ws.close()
            return

        user = TOKENS[token]
        room = _clean_room(data.get("room", "general")) or "general"

        manager.join(ws, room, user)
        await _save_last_room(user, room)
        log.info("[#%s] %s joined (%d)", room, user, len(manager.rooms[room]))

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
                await _save_last_room(user, room)
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
