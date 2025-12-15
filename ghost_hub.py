from flask import Flask, request, jsonify, render_template, redirect, url_for
from datetime import datetime
import json
import os
from threading import Lock

app = Flask(__name__, template_folder="templates")

BASE_DIR = os.path.dirname(__file__)
NODES_FILE = os.path.join(BASE_DIR, "ghost_nodes.json")
DEVICES_FILE = os.path.join(BASE_DIR, "ghost_devices.json")
_data_lock = Lock()


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


@app.route("/")
def index():
    nodes = load_nodes()
    devices = load_devices()

    # Flatten nodes structure into a list for the template
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
            node_list.append({
                "node": node_name,
                "service": svc_name,
                "url": info.get("url", ""),
                "last_seen": info.get("last_seen", ""),
                "meta": meta,
            })
    node_list.sort(key=lambda x: (x["node"], x["service"]))

    # Flatten devices
    device_list = []
    for dev_name, info in devices.items():
        device_list.append({
            "name": dev_name,
            "device_type": info.get("device_type", ""),
            "mac": info.get("mac", ""),
            "created_at": info.get("created_at", ""),
            "last_seen": info.get("last_seen", ""),
            "notes": info.get("notes", ""),
            "api_key_set": bool(info.get("api_key")),
        })
    device_list.sort(key=lambda x: x["name"].lower())

    return render_template("ghost_nodes.html", nodes=node_list, devices=device_list)


@app.route("/nodes", methods=["GET"])
def list_nodes():
    return jsonify(load_nodes())


@app.route("/devices", methods=["GET"])
def list_devices():
    return jsonify(load_devices())


@app.route("/register-node", methods=["POST"])
def register_node():
    """
    Called by your local sentinel_remote_access_ghost.py script whenever
    a new tunnel is created, or refreshed.

    Accepts either application/json or form fields with:
      - name: node name (e.g. CLOAKNODE01)
      - service: service name (e.g. sentinel-console, spellcaster)
      - url: public Cloudflare URL
      - mac: optional device MAC address
      - api_key: optional API / shared secret
      - data: optional JSON string with extra raw info from Sentinel
    """
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

    # Try to parse raw as JSON if present
    raw_parsed = None
    if raw is not None:
        try:
            raw_parsed = json.loads(raw)
        except Exception:
            raw_parsed = raw  # keep raw string

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

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

        # If device with this MAC exists, update its last_seen
        if mac:
            devices = load_devices()
            for dev_name, info in devices.items():
                if info.get("mac") == mac:
                    info["last_seen"] = ts
            save_devices(devices)

    return jsonify({
        "ok": True,
        "name": name,
        "service": service,
        "url": url,
        "mac": mac,
        "api_key_present": bool(api_key),
        "last_seen": ts,
    })


@app.route("/register-device", methods=["POST"])
def register_device():
    """
    Register or update a device from the web UI.

    Form fields:
      - device_name
      - device_type
      - mac
      - api_key (optional shared secret you can also copy into your node config)
      - notes
    """
    form = request.form or {}
    dev_name = (form.get("device_name") or "").strip() or "Unnamed Device"
    dev_type = (form.get("device_type") or "").strip()
    mac = (form.get("mac") or "").strip()
    api_key = (form.get("api_key") or "").strip()
    notes = (form.get("notes") or "").strip()

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

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


if __name__ == "__main__":
    # For local testing only; on Render you'll use gunicorn
    app.run(host="0.0.0.0", port=8000, debug=True)