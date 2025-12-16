#!/usr/bin/env python3
"""Ghost-enabled Sentinel Remote Access Launcher.

Starts Cloudflare quick tunnels and optionally registers them with
your Ghost Sentinel Hub (running on Render or locally). Supports:
  - Device MAC + API key handshakes
  - Extra raw metadata payload to the hub
"""

import json
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from urllib import request, parse

CONFIG_FILE = Path.home() / ".ghost_sentinel_node.json"
TUNNEL_LOG = Path.cwd() / "sentinel_tunnels.log"


def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def print_header():
    print(
        """
  ╔══════════════════════════════════════════════════════╗
  ║  ✶ Sentinel Remote Access Launcher (Ghost Mode) ✶   ║
  ║  Ryoko Link: Cloudflare Tunnel via CGNAT            ║
  ║  Ghost Sentinel: Optional Hub Registration          ║
  ╚══════════════════════════════════════════════════════╝
"""
    )


def menu(config):
    print("Select an option:\n")
    print("  [1] Start tunnel for Spellcaster Portal   (http://localhost:5001)")
    print("  [2] Start tunnel for Sentinel Console/Web (http://localhost:5000)")
    print("  [3] Start tunnel for custom local port    (you type the port)")
    print("  [4] View recent tunnel log entries")
    print("  [5] Configure Ghost Node (name + hub URL + MAC/API)")
    print("  [6] Quit\n")
    if (
        config.get("node_name")
        or config.get("hub_url")
        or config.get("device_mac")
        or config.get("api_key")
        or config.get("extra_note")
    ):
        print("Current ghost config:")
        if config.get("node_name"):
            print(f"  Node Name : {config['node_name']}")
        if config.get("hub_url"):
            print(f"  Hub URL   : {config['hub_url']}")
        if config.get("device_mac"):
            print(f"  MAC       : {config['device_mac']}")
        if config.get("api_key"):
            print("  API Key   : (set)")
        if config.get("extra_note"):
            print(f"  Note      : {config['extra_note']}")
        print("\n")
    return input("Enter choice (1-6): ").strip()


def ensure_cloudflared():
    from shutil import which

    if which("cloudflared") is None:
        print("[!] cloudflared not found on PATH. Install it first.\n")
        return False
    return True


def log_tunnel(service_label, url):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header_needed = not TUNNEL_LOG.exists() or TUNNEL_LOG.stat().st_size == 0
    with TUNNEL_LOG.open("a", encoding="utf-8") as f:
        if header_needed:
            f.write("# Sentinel Tunnel Log\n")
        f.write(f"[{ts}] {service_label} -> {url}\n")
    print("\n[LOG] Saved tunnel URL to", TUNNEL_LOG)
    print("      ", f"[{ts}] {service_label} -> {url}", "\n")


def send_to_hub(config, service_label, url):
    hub = (config.get("hub_url") or "").strip()
    node = (config.get("node_name") or "").strip()
    if not hub:
        return
    mac = (config.get("device_mac") or "").strip()
    api_key = (config.get("api_key") or "").strip()
    extra_note = (config.get("extra_note") or "").strip()

    raw_payload = {
        "sentinel": "ghost-node",
        "service": service_label,
        "note": extra_note,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }

    payload = {
        "name": node or "UNKNOWN-NODE",
        "service": service_label,
        "url": url,
        "mac": mac,
        "api_key": api_key,
        "data": json.dumps(raw_payload),
    }
    data = parse.urlencode(payload).encode("utf-8")
    req = request.Request(hub.rstrip("/") + "/register-node", data=data, method="POST")
    try:
        with request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                print("[Ghost Hub] Registration OK for", service_label)
            else:
                print("[Ghost Hub] Registration returned status", resp.status)
    except Exception as e:
        print("[Ghost Hub] Could not register node:", e)


def run_tunnel_for_port(port, service_label, config):
    if not ensure_cloudflared():
        return
    print("\n======================================================================")
    print(f"[+] Starting Cloudflare Tunnel for: {service_label}")
    print(f"    Local URL:  http://localhost:{port}")
    print("    Tunnel will print a public URL like:")
    print("       https://something.trycloudflare.com")
    print("    You can share that URL to access Sentinel from anywhere.\n")
    print("    Press Ctrl+C here to stop the tunnel and return to the menu.")
    print("======================================================================\n")

    cmd = [
        "cloudflared",
        "tunnel",
        "--url",
        f"http://localhost:{port}",
        "--no-autoupdate",
        "--protocol",
        "quic",
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    def reader():
        tunnel_url = None
        for line in proc.stdout:
            line = line.rstrip()
            print(line)
            if "trycloudflare.com" in line and "https://" in line:
                parts = line.split()
                for part in parts:
                    if part.startswith("https://") and "trycloudflare.com" in part:
                        tunnel_url = part.strip("| ")
                        log_tunnel(service_label, tunnel_url)
                        send_to_hub(config, service_label, tunnel_url)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n[!] Ctrl+C detected. Stopping tunnel…\n")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        print(f"[+] Tunnel for {service_label} has stopped. Returning to menu.\n")


def view_log():
    if not TUNNEL_LOG.exists():
        print("\n[LOG] No tunnel log found yet.\n")
        return
    print("\n--- Recent tunnel log entries ---\n")
    try:
        lines = TUNNEL_LOG.read_text(encoding="utf-8").splitlines()
        for line in lines[-20:]:
            print(line)
    except Exception as e:
        print("[!] Could not read log:", e)
    print("\n-------------------------------\n")


def configure_ghost_node(config):
    print("\nConfigure Ghost Sentinel Node\n")
    existing_name = config.get("node_name") or "(none)"
    existing_hub = config.get("hub_url") or "(none)"
    existing_mac = config.get("device_mac") or "(none)"
    existing_api = "(set)" if config.get("api_key") else "(none)"
    existing_note = config.get("extra_note") or "(none)"

    print(f"Current node name : {existing_name}")
    print(f"Current hub URL   : {existing_hub}")
    print(f"Current MAC       : {existing_mac}")
    print(f"Current API key   : {existing_api}")
    print(f"Current note      : {existing_note}\n")

    new_name = input("Enter node name (or leave blank to keep): ").strip()
    new_hub = input(
        "Enter Ghost Hub base URL (e.g. https://ghost-sentinel-hub.onrender.com)\n"
        "(or leave blank to keep): "
    ).strip()
    new_mac = input("Enter device MAC address (or leave blank to keep): ").strip()
    new_api = input("Enter API/shared secret for this node (or leave blank to keep): ").strip()
    new_note = input("Enter optional note (where this node lives, etc.) (or leave blank to keep): ").strip()

    if new_name:
        config["node_name"] = new_name
    if new_hub:
        config["hub_url"] = new_hub
    if new_mac:
        config["device_mac"] = new_mac
    if new_api:
        config["api_key"] = new_api
    if new_note:
        config["extra_note"] = new_note

    save_config(config)
    print("\n[+] Ghost node configuration saved:\n")
    if config.get("node_name"):
        print("    Node Name :", config["node_name"])
    if config.get("hub_url"):
        print("    Hub URL   :", config["hub_url"])
    if config.get("device_mac"):
        print("    MAC       :", config["device_mac"])
    if config.get("api_key"):
        print("    API Key   : (set)")
    if config.get("extra_note"):
        print("    Note      :", config["extra_note"])
    print("\n")


def main():
    config = load_config()
    while True:
        print_header()
        choice = menu(config)
        if choice == "1":
            run_tunnel_for_port(5001, "spellcaster-portal", config)
        elif choice == "2":
            run_tunnel_for_port(5000, "sentinel-console", config)
        elif choice == "3":
            port_str = input("Enter local port number to expose: ").strip()
            if not port_str.isdigit():
                print("[!] Invalid port.\n")
                continue
            port = int(port_str)
            label = input("Enter a label for this service (e.g. sanctuary, pbx): ").strip() or f"port-{port}"
            run_tunnel_for_port(port, label, config)
        elif choice == "4":
            view_log()
            input("Press Enter to return to menu…")
        elif choice == "5":
            configure_ghost_node(config)
        elif choice == "6":
            print("Goodbye.")
            break
        else:
            print("[!] Invalid choice.\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting.")