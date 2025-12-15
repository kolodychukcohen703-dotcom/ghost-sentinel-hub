from flask import Flask, request, jsonify, render_template
from datetime import datetime
import json
import os
from threading import Lock

app = Flask(__name__, template_folder="templates")

DATA_FILE = os.path.join(os.path.dirname(__file__), "ghost_nodes.json")
_data_lock = Lock()


def load_nodes():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_nodes(nodes):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(nodes, f, indent=2)
    os.replace(tmp, DATA_FILE)


@app.route("/")
def index():
    nodes = load_nodes()
    node_list = []
    for node_name, services in nodes.items():
        for svc_name, info in services.items():
            node_list.append({
                "node": node_name,
                "service": svc_name,
                "url": info.get("url", ""),
                "last_seen": info.get("last_seen", ""),
            })
    node_list.sort(key=lambda x: (x["node"], x["service"]))
    return render_template("ghost_nodes.html", nodes=node_list)


@app.route("/nodes", methods=["GET"])
def list_nodes():
    nodes = load_nodes()
    return jsonify(nodes)


@app.route("/register-node", methods=["POST"])
def register_node():
    if request.is_json:
        data = request.get_json() or {}
    else:
        data = request.form or {}

    name = (data.get("name") or "").strip() or "UNKNOWN-NODE"
    service = (data.get("service") or "").strip() or "default"
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"ok": False, "error": "url is required"}), 400

    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    with _data_lock:
        nodes = load_nodes()
        if name not in nodes:
            nodes[name] = {}
        nodes[name][service] = {
            "url": url,
            "last_seen": ts,
        }
        save_nodes(nodes)

    return jsonify({"ok": True, "name": name, "service": service, "url": url, "last_seen": ts})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
