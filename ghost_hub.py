#!/usr/bin/env python3
"""
Ghost Sentinel Hub (v2) — Nodes Registry + Devices + World Chat (WebSocket)

Deploy on Render with:
  Start Command:
    gunicorn -k eventlet -w 1 ghost_hub:app

Environment variables (optional):
  HUB_API_KEY       -> If set, requires X-API-Key header for /api/chat
  FLASK_SECRET_KEY  -> Optional secret for sessions
"""

import eventlet
eventlet.monkey_patch()

from flask import Flask, request, jsonify, render_template, redirect, url_for
from flask_socketio import SocketIO, join_room, leave_room, emit
from datetime import datetime
import json
import os
from threading import Lock
from collections import defaultdict, deque

app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "ghost-sentinel-dev-key")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    ping_interval=25,
    ping_timeout=60,
)

BASE_DIR = os.path.dirname(__file__)
NODES_FILE = os.path.join(BASE_DIR, "ghost_nodes.json")
DEVICES_FILE = os.path.join(BASE_DIR, "ghost_devices.json")
_data_lock = Lock()

# Chat storage (in-memory; resets on redeploy)
ROOM_HISTORY_MAX = 200
_room_history = defaultdict(lambda: deque(maxlen=ROOM_HISTORY_MAX))


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


def load_devices():
    return _load_json(DEVICES_FILE)


def save_devices(devices):
    _save_json(DEVICES_FILE, devices)


def utc_ts():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


@app.route("/")
def index():
    nodes = load_nodes()
    devices = load_devices()

    node_list = []
    for node_name, services in nodes.items():
        for svc_name, info in services.items():
            meta_bits = []
            if info.get("mac"):
                meta_bits.append("MAC")
            if info.get("api_key_set"):
                meta_bits.append("API key")
            if info.get("raw") is not None:
                meta_bits.append("raw")
            meta = ", ".join(meta_bits) if meta_bits else "—"
            node_list.append(
                {
                    "node": node_name,
                    "service": svc_name,
                    "url": info.get("url", ""),
                    "last_seen": info.get("last_seen", ""),
                    "meta": meta,
                }
            )
    node_list.sort(key=lambda x: (x["node"], x["service"]))

    device_list = []
    for dev_name, info in devices.items():
        device_list.append(
            {
                "name": dev_name,
                "device_type": info.get("device_type", ""),
                "mac": info.get("mac", ""),
                "created_at": info.get("created_at", ""),
                "last_seen": info.get("last_seen", ""),
                "notes": info.get("notes", ""),
                "api_key_set": bool(info.get("api_key")),
            }
        )
    device_list.sort(key=lambda x: x["name"].lower())

    default_rooms = ["#101", "#sanctuary", "#ops", "#ryoko"]

    return render_template(
        "ghost_nodes.html",
        nodes=node_list,
        devices=device_list,
        default_rooms=default_rooms,
    )


@app.route("/nodes", methods=["GET"])
def list_nodes():
    return jsonify(load_nodes())


@app.route("/devices", methods=["GET"])
def list_devices():
    return jsonify(load_devices())


@app.route("/register-node", methods=["POST"])
def register_node():
    if request.is_json:
        data = request.get_json() or {}
    else:
        data = request.form or {}

    name = (data.get("name") or "").strip() or "UNKNOWN-NODE"
    service = (data.get("service") or "").strip() or "default"
    url = (data.get("url") or "").strip()
    mac = (data.get("mac") or "").strip()
    api_key = (data.get("api_key") or "").strip()
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
            "mac": mac,
            "api_key_set": bool(api_key),
            "raw": raw_parsed,
        }
        save_nodes(nodes)

        if mac:
            devices = load_devices()
            for dev_name, info in devices.items():
                if info.get("mac") == mac:
                    info["last_seen"] = ts
            save_devices(devices)

    return jsonify(
        {
            "ok": True,
            "name": name,
            "service": service,
            "url": url,
            "mac": mac,
            "api_key_present": bool(api_key),
            "last_seen": ts,
        }
    )


@app.route("/register-device", methods=["POST"])
def register_device():
    form = request.form or {}
    dev_name = (form.get("device_name") or "").strip() or "Unnamed Device"
    dev_type = (form.get("device_type") or "").strip()
    mac = (form.get("mac") or "").strip()
    api_key = (form.get("api_key") or "").strip()
    notes = (form.get("notes") or "").strip()

    ts = utc_ts()

    with _data_lock:
        devices = load_devices()
        existing = devices.get(dev_name, {})
        created_at = existing.get("created_at") or ts
        devices[dev_name] = {
            "device_type": dev_type,
            "mac": mac,
            "api_key": api_key,
            "notes": notes,
            "created_at": created_at,
            "last_seen": ts,
        }
        save_devices(devices)

    return redirect(url_for("index"))


@app.route("/api/chat", methods=["POST"])
def api_chat():
    required_key = (os.environ.get("HUB_API_KEY") or "").strip()
    if required_key:
        got = (request.headers.get("X-API-Key") or "").strip()
        if got != required_key:
            return jsonify({"ok": False, "error": "unauthorized"}), 401

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


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8000, debug=True)
