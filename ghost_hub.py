#!/usr/bin/env python3
"""
Ghost Sentinel Hub — Theme-preserved v3 (Nodes + World Chat + Builder Bot)

Render Start Command:
  gunicorn -k geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 ghost_hub:app

Bot commands (in chat):
  !help
  !world create --biome forest --magic high --factions 3 [--name "World Name"]
  !home add "Room Name" [--style gothic] [--size large]
  !home door add --from "Room A" --to "Room B"
  !map
  !status
  !reset

Safe by design:
- Text-only bot; no SSH, no remote execution.
- Per-room state saved to world_state.json.
"""

from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, join_room, leave_room, emit
from datetime import datetime
import json
import os
from threading import Lock
from collections import defaultdict, deque
import shlex

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

ROOM_HISTORY_MAX = 200
_room_history = defaultdict(lambda: deque(maxlen=ROOM_HISTORY_MAX))

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
    room = room or "#101"
    with _state_lock:
        all_state = load_state_all()
        st = all_state.get(room)
        if not isinstance(st, dict):
            st = _default_state()
            all_state[room] = st
            save_state_all(all_state)
        return st


def set_room_state(room: str, st: dict):
    room = room or "#101"
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

Examples:
  !world create --name "Sanctuary-101" --biome forest --magic high --factions 3
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
    return "🧹 Reset complete. This room’s world + home state is now blank."


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

    _bot_emit(room, "Unknown command. Try: !help")


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
    return render_template("ghost_nodes.html", nodes=node_list)


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
    if request.is_json:
        data = request.get_json() or {}
    else:
        data = request.form or {}

    room = (data.get("room") or "").strip() or "#101"
    sender = (data.get("sender") or "").strip() or "node"
    msg = (data.get("msg") or "").strip()
    if not msg:
        return jsonify({"ok": False, "error": "msg required"}), 400

    payload = {"room": room, "sender": sender, "msg": msg, "ts": utc_ts()}
    _room_history[room].append(payload)
    socketio.emit("chat_message", payload, to=room)

    maybe_run_bot(room, sender, msg)
    return jsonify({"ok": True})


@socketio.on("join")
def on_join(data):
    room = (data or {}).get("room") or "#101"
    user = (data or {}).get("user") or "guest"
    join_room(room)

    emit("chat_history", list(_room_history[room]))

    notice = {"room": room, "sender": "hub", "msg": f"{user} entered {room}", "ts": utc_ts()}
    _room_history[room].append(notice)
    emit("chat_message", notice, to=room)

    hint = {"room": room, "sender": BOT_NAME, "msg": "Type !help for builder commands.", "ts": utc_ts()}
    _room_history[room].append(hint)
    emit("chat_message", hint, to=room)


@socketio.on("leave")
def on_leave(data):
    room = (data or {}).get("room") or "#101"
    user = (data or {}).get("user") or "guest"
    leave_room(room)
    notice = {"room": room, "sender": "hub", "msg": f"{user} left {room}", "ts": utc_ts()}
    _room_history[room].append(notice)
    emit("chat_message", notice, to=room)


@socketio.on("send_message")
def on_send_message(data):
    room = (data or {}).get("room") or "#101"
    user = (data or {}).get("user") or "guest"
    msg = ((data or {}).get("msg") or "").strip()
    if not msg:
        return

    payload = {"room": room, "sender": user, "msg": msg, "ts": utc_ts()}
    _room_history[room].append(payload)
    emit("chat_message", payload, to=room)

    maybe_run_bot(room, user, msg)


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8000, debug=True)
