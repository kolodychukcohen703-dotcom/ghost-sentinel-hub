#!/usr/bin/env python3
"""
Ghost Sentinel Hub — Lobby + Presence + DMs + Sealed Rune Cipher (v4)

Render Start Command:
  gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 ghost_hub:app

What’s new in v4:
- Single main room (#lobby) — no public multi-room UI.
- Online user list (presence) with sidebar updates.
- Direct Messages (DMs), with optional "Sealed" mode:
  - Uses browser WebCrypto (ECDH + AES-GCM) for end‑to‑end encryption.
  - Server stores/relays only ciphertext for sealed messages.
- Keeps the existing Nodes registry and Builder Bot (now bound to #lobby).

Safe by design:
- Text-only bot; no SSH, no remote execution.
- No secret material persisted server-side.
"""

from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, join_room, leave_room, emit
from datetime import datetime
import json
import os
from threading import Lock
from collections import defaultdict, deque
import shlex
from typing import Dict, Any, Tuple

app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "ghost-sentinel-dev-key")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    ping_interval=25,
    ping_timeout=60,
)

BASE_DIR = os.path.dirname(__file__)
NODES_FILE = os.path.join(BASE_DIR, "ghost_nodes.json")
STATE_FILE = os.path.join(BASE_DIR, "world_state.json")

_data_lock = Lock()
_state_lock = Lock()
_presence_lock = Lock()
_dm_lock = Lock()

MAIN_ROOM = "#lobby"
ROOM_HISTORY_MAX = 250
_room_history = defaultdict(lambda: deque(maxlen=ROOM_HISTORY_MAX))

# Presence: sid -> {"sid":..., "name":..., "room":..., "last_seen":...}
_online: Dict[str, Dict[str, Any]] = {}

# DM history (unencrypted only). Key is tuple(sorted([sidA, sidB])).
DM_HISTORY_MAX = 200
_dm_history = defaultdict(lambda: deque(maxlen=DM_HISTORY_MAX))

BOT_NAME = "ghost-bot"


def utc_ts():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def _load_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def load_nodes():
    return _load_json(NODES_FILE)


def save_nodes(nodes):
    _save_json(NODES_FILE, nodes)


def _default_state():
    return {
        "world": {
            "name": "Unnamed World",
            "biome": "unknown",
            "magic": "unknown",
            "factions": 0,
            "created_at": utc_ts(),
        },
        "home": {
            "rooms": [],
            "doors": [],
        },
        "updated_at": utc_ts(),
    }


def load_state_all():
    data = _load_json(STATE_FILE)
    return data if isinstance(data, dict) else {}


def save_state_all(data):
    _save_json(STATE_FILE, data)


def get_room_state(room: str):
    room = room or MAIN_ROOM
    with _state_lock:
        all_state = load_state_all()
        st = all_state.get(room)
        if not isinstance(st, dict):
            st = _default_state()
            all_state[room] = st
            save_state_all(all_state)
        return st


def set_room_state(room: str, st: dict):
    room = room or MAIN_ROOM
    with _state_lock:
        all_state = load_state_all()
        st["updated_at"] = utc_ts()
        all_state[room] = st
        save_state_all(all_state)


HELP_TEXT = """Commands:
  !help
  !world create --biome <name> --magic <low|med|high> --factions <N> [--name "<World Name>"]
  !home add "<Room Name>" [--style <style>] [--size <size>]
  !home door add --from "<Room A>" --to "<Room B>"
  !map
  !status
  !reset
  !users

Examples:
  !world create --name "Sanctuary-Lobby" --biome forest --magic high --factions 3
  !home add "Marble Foyer" --style gothic --size large
  !home door add --from "Marble Foyer" --to "Library"
  !map
"""


def _bot_emit(room: str, msg: str):
    payload = {"room": room, "sender": BOT_NAME, "msg": msg, "ts": utc_ts()}
    _room_history[room].append(payload)
    socketio.emit("chat_message", payload, to=room)


def _parse_args(text: str):
    return shlex.split(text)


def _get_flag(args, name, default=None):
    if name in args:
        idx = args.index(name)
        if idx + 1 < len(args):
            val = args[idx + 1]
            del args[idx:idx + 2]
            return val
        del args[idx:idx + 1]
        return default
    return default


def _world_create(room: str, args: list):
    name = _get_flag(args, "--name", None) or "Unnamed World"
    biome = _get_flag(args, "--biome", "unknown")
    magic = _get_flag(args, "--magic", "unknown")
    factions = _get_flag(args, "--factions", "0")
    try:
        factions_n = int(factions)
    except Exception:
        factions_n = 0

    st = get_room_state(room)
    st["world"] = {
        "name": name,
        "biome": biome,
        "magic": magic,
        "factions": factions_n,
        "created_at": utc_ts(),
    }
    set_room_state(room, st)
    return f"🌍 World created: {name} (biome={biome}, magic={magic}, factions={factions_n})"


def _home_add(room: str, args: list):
    if not args:
        return 'Usage: !home add "Room Name" [--style <style>] [--size <size>]'

    room_name = args.pop(0)
    style = _get_flag(args, "--style", "unknown")
    size = _get_flag(args, "--size", "unknown")

    st = get_room_state(room)
    rooms = st["home"]["rooms"]
    if any(r.get("name", "").lower() == room_name.lower() for r in rooms):
        return f"Room already exists: {room_name}"

    rooms.append({"name": room_name, "style": style, "size": size})
    set_room_state(room, st)
    return f"✅ Added room: {room_name} (style={style}, size={size})"


def _home_door_add(room: str, args: list):
    frm = _get_flag(args, "--from", None)
    to = _get_flag(args, "--to", None)
    if not frm or not to:
        return 'Usage: !home door add --from "Room A" --to "Room B"'

    st = get_room_state(room)
    room_names = {r.get("name", "") for r in st["home"]["rooms"]}
    if frm not in room_names:
        st["home"]["rooms"].append({"name": frm, "style": "unknown", "size": "unknown"})
    if to not in room_names:
        st["home"]["rooms"].append({"name": to, "style": "unknown", "size": "unknown"})

    if any(d.get("from") == frm and d.get("to") == to for d in st["home"]["doors"]):
        return f"Door already exists: {frm} -> {to}"

    st["home"]["doors"].append({"from": frm, "to": to})
    set_room_state(room, st)
    return f"🚪 Linked: {frm}  →  {to}"


def _status(room: str):
    st = get_room_state(room)
    w = st.get("world", {})
    rooms = st.get("home", {}).get("rooms", [])
    doors = st.get("home", {}).get("doors", [])
    return (
        f"Status for {room}\n"
        f"World: {w.get('name')} | biome={w.get('biome')} | magic={w.get('magic')} | factions={w.get('factions')}\n"
        f"Home: {len(rooms)} room(s), {len(doors)} door link(s)\n"
        f"Updated: {st.get('updated_at')}"
    )


def _map(room: str):
    st = get_room_state(room)
    w = st.get("world", {})
    rooms = st.get("home", {}).get("rooms", [])
    doors = st.get("home", {}).get("doors", [])

    lines = []
    lines.append(f"== {room} :: World ==")
    lines.append(f"{w.get('name')} | biome={w.get('biome')} | magic={w.get('magic')} | factions={w.get('factions')}")
    lines.append("")
    lines.append("== Home Rooms ==")
    if not rooms:
        lines.append('(no rooms yet)  try: !home add "Marble Foyer" --style gothic --size large')
    else:
        for r in rooms[:60]:
            lines.append(f"- {r.get('name')} (style={r.get('style')}, size={r.get('size')})")

    lines.append("")
    lines.append("== Doors ==")
    if not doors:
        lines.append('(no doors yet)  try: !home door add --from "Marble Foyer" --to "Library"')
    else:
        for d in doors[:120]:
            lines.append(f"* {d.get('from')}  ->  {d.get('to')}")

    return "\n".join(lines)


def _reset(room: str):
    st = _default_state()
    set_room_state(room, st)
    return "🧹 Reset complete. The lobby’s world + home state is now blank."


def _users(room: str):
    with _presence_lock:
        users = list(_online.values())
    users.sort(key=lambda u: (u.get("name", "").lower(), u.get("sid", "")))
    lines = [f"Online users in {room}: {len(users)}"]
    for u in users[:60]:
        nm = u.get("name", "guest")
        sid = u.get("sid", "")[:6]
        lines.append(f"- {nm} ({sid})")
    return "\n".join(lines)


def maybe_run_bot(room: str, user: str, msg: str):
    msg = (msg or "").strip()
    if not msg.startswith("!"):
        return
    if (user or "").strip().lower() == BOT_NAME.lower():
        return

    try:
        args = _parse_args(msg)
    except Exception:
        _bot_emit(room, "I couldn't parse that. Try: !help")
        return
    if not args:
        return

    cmd = args.pop(0).lower()
    if cmd == "!help":
        _bot_emit(room, HELP_TEXT)
        return
    if cmd == "!world":
        sub = (args.pop(0).lower() if args else "")
        if sub == "create":
            _bot_emit(room, _world_create(room, args))
        else:
            _bot_emit(room, 'Usage: !world create --biome <name> --magic <low|med|high> --factions <N> [--name "World Name"]')
        return
    if cmd == "!home":
        sub = (args.pop(0).lower() if args else "")
        if sub == "add":
            _bot_emit(room, _home_add(room, args))
        elif sub == "door":
            sub2 = (args.pop(0).lower() if args else "")
            if sub2 == "add":
                _bot_emit(room, _home_door_add(room, args))
            else:
                _bot_emit(room, 'Usage: !home door add --from "Room A" --to "Room B"')
        else:
            _bot_emit(room, 'Usage: !home add "Room Name" ... OR !home door add --from ... --to ...')
        return
    if cmd == "!map":
        _bot_emit(room, _map(room))
        return
    if cmd == "!status":
        _bot_emit(room, _status(room))
        return
    if cmd == "!reset":
        _bot_emit(room, _reset(room))
        return
    if cmd == "!users":
        _bot_emit(room, _users(room))
        return

    _bot_emit(room, "Unknown command. Try: !help")


def _emit_user_list():
    with _presence_lock:
        users = [{"sid": u["sid"], "name": u.get("name", "guest"), "room": u.get("room", MAIN_ROOM)} for u in _online.values()]
    users.sort(key=lambda x: (x["name"].lower(), x["sid"]))
    socketio.emit("user_list_update", {"room": MAIN_ROOM, "users": users})


def _dm_key(a: str, b: str) -> Tuple[str, str]:
    return tuple(sorted([a, b]))


def _dm_room(a: str, b: str) -> str:
    x, y = _dm_key(a, b)
    return f"dm:{x}:{y}"


@app.route("/")
def index():
    nodes = load_nodes()
    node_list = []
    for node_name, services in nodes.items():
        for svc_name, info in services.items():
            node_list.append(
                {
                    "node": node_name,
                    "service": svc_name,
                    "url": info.get("url", ""),
                    "last_seen": info.get("last_seen", ""),
                }
            )
    node_list.sort(key=lambda x: (x["node"], x["service"]))
    return render_template("ghost_nodes.html", nodes=node_list, main_room=MAIN_ROOM)


@app.route("/register-node", methods=["POST"])
def register_node():
    if request.is_json:
        data = request.get_json() or {}
    else:
        data = request.form or {}

    name = (data.get("name") or "").strip() or "UNKNOWN-NODE"
    service = (data.get("service") or "").strip() or "default"
    url = (data.get("url") or "").strip()
    raw = data.get("data")

    if not url:
        return jsonify({"ok": False, "error": "url is required"}), 400

    raw_parsed = None
    if raw is not None:
        try:
            raw_parsed = json.loads(raw)
        except Exception:
            raw_parsed = raw

    ts = utc_ts()
    with _data_lock:
        nodes = load_nodes()
        if name not in nodes:
            nodes[name] = {}
        nodes[name][service] = {
            "url": url,
            "last_seen": ts,
            "raw": raw_parsed,
        }
        save_nodes(nodes)

    return jsonify({"ok": True, "name": name, "service": service, "url": url, "last_seen": ts})


@app.route("/api/chat", methods=["POST"])
def api_chat():
    # Force everything into the lobby
    if request.is_json:
        data = request.get_json() or {}
    else:
        data = request.form or {}

    room = MAIN_ROOM
    sender = (data.get("sender") or "").strip() or "node"
    msg = (data.get("msg") or "").strip()
    if not msg:
        return jsonify({"ok": False, "error": "msg required"}), 400

    payload = {"room": room, "sender": sender, "msg": msg, "ts": utc_ts()}
    _room_history[room].append(payload)
    socketio.emit("chat_message", payload, to=room)

    maybe_run_bot(room, sender, msg)
    return jsonify({"ok": True})


@socketio.on("ping_check")
def on_ping_check(data=None):
    emit("pong_check", {"ts": utc_ts()})

@socketio.on("connect")
def on_connect():
    # sid exists here; name set on join
    sid = request.sid
    with _presence_lock:
        _online[sid] = {"sid": sid, "name": "guest", "room": MAIN_ROOM, "last_seen": utc_ts()}
    _emit_user_list()


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    with _presence_lock:
        _online.pop(sid, None)
    _emit_user_list()


@socketio.on("join")
def on_join(data):
    # Ignore requested room; always lobby
    sid = request.sid
    user = (data or {}).get("user") or "guest"
    user = (user or "guest").strip()[:48] or "guest"

    join_room(MAIN_ROOM)

    with _presence_lock:
        if sid in _online:
            _online[sid]["name"] = user
            _online[sid]["room"] = MAIN_ROOM
            _online[sid]["last_seen"] = utc_ts()

    emit("chat_history", list(_room_history[MAIN_ROOM]))
    _emit_user_list()

    notice = {"room": MAIN_ROOM, "sender": "hub", "msg": f"{user} entered {MAIN_ROOM}", "ts": utc_ts()}
    _room_history[MAIN_ROOM].append(notice)
    emit("chat_message", notice, to=MAIN_ROOM)

    hint = {"room": MAIN_ROOM, "sender": BOT_NAME, "msg": "Type !help for builder commands. DMs: click a user on the right.", "ts": utc_ts()}
    _room_history[MAIN_ROOM].append(hint)
    emit("chat_message", hint, to=MAIN_ROOM)


@socketio.on("send_message")
def on_send_message(data):
    room = MAIN_ROOM
    user = (data or {}).get("user") or "guest"
    msg = ((data or {}).get("msg") or "").strip()
    if not msg:
        return

    payload = {"room": room, "sender": user, "msg": msg, "ts": utc_ts()}
    _room_history[room].append(payload)
    emit("chat_message", payload, to=room)

    maybe_run_bot(room, user, msg)


@socketio.on("dm_open")
def on_dm_open(data):
    """Join a DM room and return history (plaintext only)."""
    sid = request.sid
    other = (data or {}).get("to_sid") or ""
    other = other.strip()
    if not other or other == sid:
        return

    dm_room = _dm_room(sid, other)
    join_room(dm_room)

    # Send plaintext history (sealed messages are client-side only)
    key = _dm_key(sid, other)
    with _dm_lock:
        hist = list(_dm_history[key])

    emit("dm_history", {"to_sid": other, "items": hist})


@socketio.on("dm_send")
def on_dm_send(data):
    """Plaintext DM (server stores small rolling history)."""
    sid = request.sid
    to_sid = (data or {}).get("to_sid") or ""
    to_sid = to_sid.strip()
    msg = ((data or {}).get("msg") or "").strip()
    if not to_sid or to_sid == sid or not msg:
        return

    with _presence_lock:
        sender_name = _online.get(sid, {}).get("name", "guest")
        to_name = _online.get(to_sid, {}).get("name", "guest")

    payload = {
        "kind": "dm",
        "from_sid": sid,
        "from_name": sender_name,
        "to_sid": to_sid,
        "to_name": to_name,
        "msg": msg,
        "ts": utc_ts(),
    }

    key = _dm_key(sid, to_sid)
    with _dm_lock:
        _dm_history[key].append(payload)

    dm_room = _dm_room(sid, to_sid)
    socketio.emit("dm_message", payload, to=dm_room)


@socketio.on("dm_sealed")
def on_dm_sealed(data):
    """
    Sealed DM relay:
    The server does NOT decrypt. It just relays ciphertext+iv+meta.
    """
    sid = request.sid
    to_sid = (data or {}).get("to_sid") or ""
    to_sid = to_sid.strip()
    if not to_sid or to_sid == sid:
        return

    with _presence_lock:
        sender_name = _online.get(sid, {}).get("name", "guest")
        to_name = _online.get(to_sid, {}).get("name", "guest")

    payload = {
        "kind": "sealed",
        "from_sid": sid,
        "from_name": sender_name,
        "to_sid": to_sid,
        "to_name": to_name,
        "ciphertext_b64": (data or {}).get("ciphertext_b64"),
        "iv_b64": (data or {}).get("iv_b64"),
        "glyphset": (data or {}).get("glyphset"),
        "ts": utc_ts(),
    }

    dm_room = _dm_room(sid, to_sid)
    socketio.emit("dm_sealed", payload, to=dm_room)


@socketio.on("seal_request")
def on_seal_request(data):
    """
    ECDH handshake message relay (public key only).
    """
    sid = request.sid
    to_sid = (data or {}).get("to_sid") or ""
    to_sid = to_sid.strip()
    if not to_sid or to_sid == sid:
        return

    with _presence_lock:
        sender_name = _online.get(sid, {}).get("name", "guest")

    payload = {
        "from_sid": sid,
        "from_name": sender_name,
        "to_sid": to_sid,
        "pubkey_jwk": (data or {}).get("pubkey_jwk"),
        "ts": utc_ts(),
    }

    dm_room = _dm_room(sid, to_sid)
    socketio.emit("seal_request", payload, to=dm_room)


@socketio.on("seal_accept")
def on_seal_accept(data):
    """
    ECDH handshake accept relay (public key only).
    """
    sid = request.sid
    to_sid = (data or {}).get("to_sid") or ""
    to_sid = to_sid.strip()
    if not to_sid or to_sid == sid:
        return

    with _presence_lock:
        sender_name = _online.get(sid, {}).get("name", "guest")

    payload = {
        "from_sid": sid,
        "from_name": sender_name,
        "to_sid": to_sid,
        "pubkey_jwk": (data or {}).get("pubkey_jwk"),
        "ts": utc_ts(),
    }

    dm_room = _dm_room(sid, to_sid)
    socketio.emit("seal_accept", payload, to=dm_room)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8000, debug=True)
