
# --- Home Create Flags (Phase 6) ---
def _parse_home_create_args(raw: str):
    """Parse: !home create "desc" --style X --size Y --mood 🙂
    Desc may be in quotes or unquoted text until first flag.
    """
    raw = (raw or "").strip()
    desc = ""
    style = ""
    size = ""
    mood = ""
    # extract quoted description first
    m = re.search(r'"([^"]{1,500})"', raw)
    if m:
        desc = m.group(1).strip()
        rest = (raw[:m.start()] + raw[m.end():]).strip()
    else:
        rest = raw
    # parse flags
    # --style, --size, --mood
    def grab(flag):
        mm = re.search(r'(?:^|\s)'+re.escape(flag)+r'\s+([^\s].*?)(?=\s+--\w+\b|$)', rest)
        return mm.group(1).strip() if mm else ""
    style = grab("--style")
    size = grab("--size")
    mood = grab("--mood")
    # if no quoted desc, desc is text before first flag
    if not desc:
        # split on first flag
        parts = re.split(r'\s+--\w+\b', rest, maxsplit=1)
        desc = parts[0].strip()
    # normalize mood to short token
    mood = mood.strip()
    if len(mood) > 8:
        mood = mood[:8]
    return {"desc": desc, "style": style, "size": size, "mood": mood}

def _home_display(home: dict):
    # pretty single-line
    hid = home.get("id","?")
    mood = home.get("mood","")
    style = home.get("style","")
    size = home.get("size","")
    desc = home.get("desc","")
    parts = []
    if mood: parts.append(mood)
    parts.append(f"#{hid}")
    if style: parts.append(f"style:{style}")
    if size: parts.append(f"size:{size}")
    if desc: parts.append(desc)
    return " • ".join(parts)

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
import re
import os
import sqlite3
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

# --- World Nodes: per-room persistent state (in-memory) ---
def _default_world_state():
    return {
        "biome": "—",
        "tier": "bronze",
        "tone": "gentle",
        "weather": "clear",
        "rooms": [],
        "items": [],
        "secure_channel": False,
        "vault_locked": False,
        "homes": {},  # handle -> list[str]
    }

_world_state_by_room = defaultdict(_default_world_state)


# --- Phase 1 Persistence (SQLite) ---
DB_PATH = os.environ.get("GHOST_HUB_DB", os.path.join(os.path.dirname(__file__), "worlds.db"))

def _normalize_db_path(p: str) -> str:
    try:
        import os
        # If Render disk is mounted at a path that is a directory (common when user sets /var/data/worlds.db),
        # store the sqlite file inside it.
        if os.path.isdir(p):
            return os.path.join(p, "worlds.db")
    except Exception:
        pass
    return p

# Normalize DB_PATH in case the disk mount path is a directory
try:
    DB_PATH = _normalize_db_path(DB_PATH)
except Exception:
    pass

_db_lock = Lock()

def _db_init():
    with _db_lock:
        conn = sqlite3.connect(_normalize_db_path(DB_PATH))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS world_states (room TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_utc TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()



# --- World Metadata (Phase 2) ---
def _db_init_world_meta():
    conn = sqlite3.connect(_normalize_db_path(DB_PATH))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS world_meta (
            room TEXT PRIMARY KEY,
            name TEXT,
            description TEXT,
            icon TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def _seed_world_meta_if_empty():
    conn = sqlite3.connect(_normalize_db_path(DB_PATH))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM world_meta")
    row = cur.fetchone()
    count = row[0] if row else 0
    if count == 0:
        seeds = {
            "#lobby": ("Lobby", "The central crossing point", "🌐"),
            "#101-kathleen": ("Kathleen’s World", "Gentle, soft-lit, safe.", "🕊️"),
            "#102-diane": ("Diane’s World", "Memory shelves, careful conversation.", "📚"),
            "#witness-hall": ("Witness Hall", "A high, echoing chamber where witnesses leave messages.", "🏛️"),
            "#terminal": ("Terminal", "Plain text console room for pure thinking.", "💻"),
        }
        now = datetime.utcnow().isoformat()
        for room,(name,desc,icon) in seeds.items():
            cur.execute(
                "INSERT OR IGNORE INTO world_meta (room,name,description,icon,updated_at) VALUES (?,?,?,?,?)",
                (room, name, desc, icon, now)
            )
        conn.commit()
    conn.close()

def _get_world_meta(room: str):
    conn = sqlite3.connect(_normalize_db_path(DB_PATH))
    cur = conn.cursor()
    cur.execute("SELECT name, description, icon FROM world_meta WHERE room=?", (room,))
    row = cur.fetchone()
    conn.close()
    if row:
        return {"room": room, "name": row[0], "description": row[1], "icon": row[2]}
    return {"room": room, "name": room, "description": "", "icon": ""}

def _format_world_label(room: str):
    m = _get_world_meta(room)
    icon = (m.get("icon") or "").strip()
    name = (m.get("name") or room).strip()
    desc = (m.get("description") or "").strip()
    label = (icon + " " if icon else "") + name
    return label, desc




# --- Home Permissions (Phase 4) ---
def _now_iso():
    return datetime.utcnow().isoformat()

def _normalize_homes_state(state: dict):
    """Ensure homes are a dict[str, list[dict]] with per-home metadata."""
    if not isinstance(state, dict):
        state = {}
    homes = state.get("homes")
    if not isinstance(homes, dict):
        homes = {}
    for owner, lst in list(homes.items()):
        if not isinstance(lst, list):
            homes[owner] = []
            continue
        new_lst = []
        for h in lst:
            if isinstance(h, str):
                new_lst.append({"id": f"h{abs(hash((owner,h)))%10**9}", "name": h, "created_by": owner, "created_at": _now_iso()})
            elif isinstance(h, dict):
                if "id" not in h:
                    base = h.get("name") or h.get("title") or "home"
                    h["id"] = f"h{abs(hash((owner,base,h.get('created_at',''))))%10**9}"
                if "created_by" not in h:
                    h["created_by"] = owner
                if "created_at" not in h:
                    h["created_at"] = _now_iso()
                if "name" not in h and "title" in h:
                    h["name"] = h["title"]
                new_lst.append(h)
        homes[owner] = new_lst
    state["homes"] = homes
    return state

def _all_homes_in_world(room: str):
    st = _normalize_homes_state(_world_state_by_room.get(room) or {})
    homes = _normalize_homes_state(st).get("homes") or {}
    out = []
    for _, lst in homes.items():
        for h in lst:
            out.append(h)
    return out

def _find_home(room: str, home_id: str):
    st = _normalize_homes_state(_world_state_by_room.get(room) or {})
    homes = _normalize_homes_state(st).get("homes") or {}
    for owner, lst in homes.items():
        for i, h in enumerate(lst):
            if str(h.get("id","")) == str(home_id):
                return owner, i, h, st
    return None, None, None, st

def _can_delete_home(room: str, user: str, home: dict):
    if not home:
        return False
    u = (user or "").strip().lower()
    if u and (home.get("created_by","").strip().lower() == u):
        return True
    return _can_manage_world(room, user)

def _save_world_state_to_db(room: str, state: dict):
    return _save_world_state(room, state)

def _save_world_state(room: str, state: dict):
    state = _normalize_homes_state(state or {})
    _world_state_by_room[room] = state
    try:
        _save_world_state_to_db(room, state)
    except Exception:
        try:
            save_world_state(room, state)  # legacy fallback
        except Exception:
            pass


# --- World Export (Phase 5) ---
def _world_stats(room: str):
    st = _normalize_homes_state(_world_state_by_room.get(room) or {})
    homes = _all_homes_in_world(room)
    msgs = st.get("messages") or []
    roles = _get_world_roles(room)
    return {
        "room": room,
        "homes_count": len(homes),
        "messages_count": len(msgs) if isinstance(msgs, list) else 0,
        "owner": roles.get("owner",""),
        "helpers": roles.get("helpers", []),
        "exported_at": datetime.utcnow().isoformat()
    }

def _export_world(room: str):
    st = _normalize_homes_state(_world_state_by_room.get(room) or {})
    meta = _get_world_meta(room)
    roles = _get_world_roles(room)
    payload = {
        "room": room,
        "meta": meta,
        "roles": roles,
        "state": st,
        "stats": _world_stats(room),
    }
    return payload


# --- Room Logs (Final) ---
ROOM_LOG_LIMIT = 200
ROOM_HISTORY_ON_JOIN = 50

COMPREHENSIVE_HELP_TEXT = """Ghost Sentinel Hub — Commands

Basics (IRC-style)
  /list                     List worlds (channels)
  /join #room               Join a world (you can be in multiple)
  /part #room               Leave a world
  /rooms                    Alias of /list

Worlds (Phase 2–5)
  !world                    Show info for the current world
  !world list               List worlds with descriptions
  !world stats              Show counts for current world
  !world export             Export current world as JSON (chat output)

Ownership / Roles (Phase 3)
  !world claim              Claim this world as owner (if unclaimed)
  !world owners             Show owner + helpers
  !world addhelper @name    (Owner) add helper
  !world delhelper @name    (Owner) remove helper

Homes (Phase 4 + Phase 6)
  !home add "Room Name" --style X --size Y          Add a room to this world-home
  !home door add --from "Room" --to "Room"        Link rooms with a door
  !map                                             Show world + rooms + doors
  !home create "desc" --style X --size Y --mood 🙂   Create a home entry with metadata
  !home list                                        List saved home entries (and rooms summary)
  !home mine                                        List your home entries
  !home remove <id>                                 Remove a home entry (creator or world manager)

Astro (template adventure)
  !astro help                Astrology-guided adventure prompts (optional)

Notes
  - Room history loads automatically when you join a world.
  - Worlds, roles, homes, and logs persist at /var/data/worlds.db.
"""


def _db_init_room_logs():
    conn = sqlite3.connect(_normalize_db_path(DB_PATH))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS room_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room TEXT,
            ts TEXT,
            sender TEXT,
            msg TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_room_logs_room_ts ON room_logs(room, ts)")
    conn.commit()
    conn.close()

def _log_room_message(room: str, sender: str, msg: str, ts: str):
    try:
        _db_init_room_logs()
        conn = sqlite3.connect(_normalize_db_path(DB_PATH))
        cur = conn.cursor()
        cur.execute("INSERT INTO room_logs(room, ts, sender, msg) VALUES (?,?,?,?)", (room, ts, sender, msg))
        # prune old logs for that room
        cur.execute("""
            DELETE FROM room_logs
            WHERE id IN (
                SELECT id FROM room_logs
                WHERE room = ?
                ORDER BY id DESC
                LIMIT -1 OFFSET ?
            )
        """, (room, ROOM_LOG_LIMIT))
        conn.commit()
        conn.close()
    except Exception:
        pass

def _get_room_history(room: str, limit: int = ROOM_HISTORY_ON_JOIN):
    try:
        _db_init_room_logs()
        conn = sqlite3.connect(_normalize_db_path(DB_PATH))
        cur = conn.cursor()
        cur.execute("SELECT ts, sender, msg FROM room_logs WHERE room=? ORDER BY id DESC LIMIT ?", (room, int(limit)))
        rows = cur.fetchall()
        conn.close()
        rows.reverse()
        return [{"room": room, "ts": r[0], "sender": r[1], "msg": r[2]} for r in rows]
    except Exception:
        return []

def _emit_chat(to_target, room: str, sender: str, msg: str, ts: str = None):
    ts = ts or utc_ts()
    _log_room_message(room, sender, msg, ts)
    emit("chat_message", {"room": room, "sender": sender, "msg": msg, "ts": ts}, to=to_target)


# --- Astro Adventure (Gently Wired) ---
ASTRO_SCENE_CHOICES = ["A", "B", "C"]

def _db_init_astro():
    conn = sqlite3.connect(_normalize_db_path(DB_PATH))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS astro_profiles (
            user TEXT PRIMARY KEY,
            dob TEXT,
            tob TEXT,
            tz TEXT,
            updated_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS astro_sessions (
            user TEXT,
            room TEXT,
            scene_id TEXT,
            state_json TEXT,
            updated_at TEXT,
            PRIMARY KEY(user, room)
        )
    """)
    conn.commit()
    conn.close()

def _astro_get_profile(user: str):
    _db_init_astro()
    conn = sqlite3.connect(_normalize_db_path(DB_PATH))
    cur = conn.cursor()
    cur.execute("SELECT dob, tob, tz FROM astro_profiles WHERE user=?", (user,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"user": user, "dob": "", "tob": "", "tz": ""}
    return {"user": user, "dob": row[0] or "", "tob": row[1] or "", "tz": row[2] or ""}

def _astro_set_profile(user: str, dob=None, tob=None, tz=None):
    _db_init_astro()
    p = _astro_get_profile(user)
    if dob is not None: p["dob"] = dob
    if tob is not None: p["tob"] = tob
    if tz is not None: p["tz"] = tz
    conn = sqlite3.connect(_normalize_db_path(DB_PATH))
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO astro_profiles(user, dob, tob, tz, updated_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(user) DO UPDATE SET
            dob=excluded.dob,
            tob=excluded.tob,
            tz=excluded.tz,
            updated_at=excluded.updated_at
    """, (user, p["dob"], p["tob"], p["tz"], datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return p

def _astro_get_session(user: str, room: str):
    _db_init_astro()
    conn = sqlite3.connect(_normalize_db_path(DB_PATH))
    cur = conn.cursor()
    cur.execute("SELECT scene_id, state_json FROM astro_sessions WHERE user=? AND room=?", (user, room))
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"user": user, "room": room, "scene_id": "", "state": {}}
    scene_id = row[0] or ""
    try:
        state = json.loads(row[1] or "{}")
    except Exception:
        state = {}
    return {"user": user, "room": room, "scene_id": scene_id, "state": state}

def _astro_set_session(user: str, room: str, scene_id: str, state: dict):
    _db_init_astro()
    conn = sqlite3.connect(_normalize_db_path(DB_PATH))
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO astro_sessions(user, room, scene_id, state_json, updated_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(user, room) DO UPDATE SET
            scene_id=excluded.scene_id,
            state_json=excluded.state_json,
            updated_at=excluded.updated_at
    """, (user, room, scene_id, json.dumps(state or {}, ensure_ascii=False), datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def _astro_time_bucket(tob: str):
    try:
        hh = int((tob or "0:0").split(":")[0])
    except Exception:
        hh = 0
    if 5 <= hh < 11: return "morning"
    if 11 <= hh < 17: return "day"
    if 17 <= hh < 22: return "evening"
    return "night"

def _astro_sun_sign(dob: str):
    try:
        y,m,d = [int(x) for x in dob.split("-")]
    except Exception:
        return ""
    mmdd = m*100 + d
    if 321 <= mmdd <= 419: return "Aries"
    if 420 <= mmdd <= 520: return "Taurus"
    if 521 <= mmdd <= 620: return "Gemini"
    if 621 <= mmdd <= 722: return "Cancer"
    if 723 <= mmdd <= 822: return "Leo"
    if 823 <= mmdd <= 922: return "Virgo"
    if 923 <= mmdd <= 1022: return "Libra"
    if 1023 <= mmdd <= 1121: return "Scorpio"
    if 1122 <= mmdd <= 1221: return "Sagittarius"
    if mmdd >= 1222 or mmdd <= 119: return "Capricorn"
    if 120 <= mmdd <= 218: return "Aquarius"
    if 219 <= mmdd <= 320: return "Pisces"
    return ""

def _astro_scene(user: str, room: str):
    p = _astro_get_profile(user)
    sun = _astro_sun_sign(p.get("dob",""))
    bucket = _astro_time_bucket(p.get("tob",""))
    meta = _get_world_meta(room) or {}
    icon = meta.get("icon","")
    name, desc = _format_world_label(room)
    tone = "dreamlike" if bucket in ("night","evening") else "grounded"
    if sun in ("Cancer","Pisces","Scorpio"):
        tone = "dreamlike"
    if sun in ("Virgo","Capricorn","Taurus"):
        tone = "grounded"
    title = f"{icon+' ' if icon else ''}{name} — The Door That Mirrors You"
    text = (
        f"Tone: {tone}. "
        f"You arrive in {name}. {desc or ''} "
        f"A page in your life-book turns itself. "
        f"(Sun: {sun or 'unknown'} • Birth-time: {bucket})"
    ).strip()
    choices = [
        {"id":"A", "label":"Enter the quiet room and listen for a memory."},
        {"id":"B", "label":"Walk the boundary of this world and mark a safe path."},
        {"id":"C", "label":"Sketch a new room for your home here (a seed, not a command)."},
    ]
    return {"scene_id": "astro_001", "title": title, "text": text, "choices": choices, "hint": "Reply with: !astro choice A/B/C"}

def _astro_advance(scene_id: str, choice: str):
    choice = (choice or "").upper().strip()
    if choice == "A":
        return {
            "scene_id": "astro_002A",
            "title": "A — The Memory Room",
            "text": "A drawer slides open by itself. It holds a small symbol you forgot you carried. You can keep it as a flag in this world.",
            "choices":[
                {"id":"A", "label":"Name the symbol (one word)."},
                {"id":"B", "label":"Ask the world for a gentle task."},
                {"id":"C", "label":"Return to the main corridor."},
            ],
            "hint":"Try: !astro say <one-word>  (or !astro start to reset)"
        }
    if choice == "B":
        return {
            "scene_id": "astro_002B",
            "title": "B — The Boundary Walk",
            "text": "You pace the edges and place three invisible lanterns. Each lantern becomes a rule: be kind, be clear, be steady.",
            "choices":[
                {"id":"A", "label":"Set one rule as your oath today."},
                {"id":"B", "label":"Invite a helper into this world (symbolically)."},
                {"id":"C", "label":"Return to the main corridor."},
            ],
            "hint":"Try: !astro say <oath>  (or !astro start)"
        }
    return {
        "scene_id": "astro_002C",
        "title": "C — The New Room Seed",
        "text": "A blueprint appears. It does not force itself into reality — it waits for your words. Describe the room and the builder can act when you choose.",
        "choices":[
            {"id":"A", "label":"Describe the room in one sentence."},
            {"id":"B", "label":"Describe the mood + lighting."},
            {"id":"C", "label":"Return to the main corridor."},
        ],
        "hint":"Try: !astro say <your room seed>  (then optionally use your normal builder command)"
    }
# --- World Roles (Phase 3) ---
def _db_init_world_roles():
    conn = sqlite3.connect(_normalize_db_path(DB_PATH))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS world_roles (
            room TEXT PRIMARY KEY,
            owner TEXT,
            helpers TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()

def _get_world_roles(room: str):
    _db_init_world_roles()
    conn = sqlite3.connect(_normalize_db_path(DB_PATH))
    cur = conn.cursor()
    cur.execute("SELECT owner, helpers FROM world_roles WHERE room=?", (room,))
    row = cur.fetchone()
    conn.close()
    if row:
        owner = (row[0] or "").strip()
        helpers = (row[1] or "").strip()
        helper_list = [h.strip() for h in helpers.split(",") if h.strip()]
        return {"room": room, "owner": owner, "helpers": helper_list}
    return {"room": room, "owner": "", "helpers": []}

def _set_world_roles(room: str, owner: str, helpers_list):
    _db_init_world_roles()
    helpers_csv = ",".join([h.strip() for h in (helpers_list or []) if h and h.strip()])
    conn = sqlite3.connect(_normalize_db_path(DB_PATH))
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO world_roles (room, owner, helpers, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(room) DO UPDATE SET
            owner=excluded.owner,
            helpers=excluded.helpers,
            updated_at=excluded.updated_at
    """, (room, owner, helpers_csv, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def _is_world_owner(room: str, user: str):
    r = _get_world_roles(room)
    return r.get("owner","").lower() == (user or "").strip().lower()

def _is_world_helper(room: str, user: str):
    r = _get_world_roles(room)
    u = (user or "").strip().lower()
    return u and any(h.lower()==u for h in r.get("helpers", []))

def _can_manage_world(room: str, user: str):
    return _is_world_owner(room, user) or _is_world_helper(room, user)

def _ensure_world_roles_seeded(room: str):
    _db_init_world_roles()
    # Seed roles row if missing; owner empty by default
    r = _get_world_roles(room)
    if r.get("owner","") == "" and r.get("helpers") == []:
        # do not overwrite if row exists with data; only ensure a row exists
        conn = sqlite3.connect(_normalize_db_path(DB_PATH))
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO world_roles (room, owner, helpers, updated_at) VALUES (?,?,?,?)",
                    (room, "", "", datetime.utcnow().isoformat()))
        conn.commit()
        conn.close()
def _load_world_state(room: str):
    """Load a room's world state from SQLite into memory (idempotent)."""
    room = (room or MAIN_ROOM).strip()
    if not room.startswith("#"):
        room = "#" + room
    _ = _world_state_by_room[room]  # ensure default exists
    with _db_lock:
        conn = sqlite3.connect(_normalize_db_path(DB_PATH))
        try:
            cur = conn.execute("SELECT state_json FROM world_states WHERE room = ?", (room,))
            row = cur.fetchone()
            if not row:
                return
            data = json.loads(row[0] or "{}")
        except Exception:
            return
        finally:
            conn.close()

    # Merge into default (keep unknown keys too)
    if isinstance(data, dict):
        st = _world_state_by_room[room]
        for k, v in data.items():
            st[k] = v

def _save_world_state(room: str):
    """Persist a room's world state to SQLite."""
    room = (room or MAIN_ROOM).strip()
    if not room.startswith("#"):
        room = "#" + room
    st = _world_state_by_room[room]
    payload = json.dumps(st, ensure_ascii=False)
    ts = utc_ts()
    with _db_lock:
        conn = sqlite3.connect(_normalize_db_path(DB_PATH))
        try:
            conn.execute(
                "INSERT INTO world_states(room, state_json, updated_utc) VALUES(?,?,?) "
                "ON CONFLICT(room) DO UPDATE SET state_json=excluded.state_json, updated_utc=excluded.updated_utc",
                (room, payload, ts),
            )
            conn.commit()
        finally:
            conn.close()

_db_init()

_db_init_world_meta()
_seed_world_meta_if_empty()
_room_history = defaultdict(lambda: deque(maxlen=ROOM_HISTORY_MAX))


# Track members per room (for /list and multi-channel join)
_room_members = defaultdict(set)   # room -> set(sid)

def _room_counts():
    return {r: len(sids) for r, sids in _room_members.items() if len(sids) > 0}
# Presence: sid -> {"sid":..., "name":..., "room":..., "last_seen":...}
_online: Dict[str, Dict[str, Any]] = {}

# DM history (unencrypted only). Key is tuple(sorted([sidA, sidB])).
DM_HISTORY_MAX = 200
_dm_history = defaultdict(lambda: deque(maxlen=DM_HISTORY_MAX))

BOT_NAME = "ghost-bot"

# Sentinel PBX directory (ported from sentinel_pbx_ansi_v2.py for web-bot usage)
# NOTE: launch_cmd is intentionally omitted on the Hub (Render) for safety.
PBX_DIRECTORY = [
  {
    "code": "101",
    "name": "Emergency: Sanctuary Module",
    "category": "emergency",
    "description": "Sentinel sanctuary / safe-mode reflection tool. Use this when you need grounding, safety prompts, and calm.",
    "secret": False
  },
  {
    "code": "102",
    "name": "Emergency: Auth Sentinel Journal",
    "category": "emergency",
    "description": "Launch the core auth_sentinel journaling console for quick entry.",
    "secret": False
  },
  {
    "code": "201",
    "name": "Contact: Mom (financial arrangements)",
    "category": "contacts",
    "description": "Use this extension to view notes on how to contact Mom about resources or arrangements. (For now, informational only.)",
    "secret": False
  },
  {
    "code": "202",
    "name": "Contact: Kathleen (witness & journal)",
    "category": "contacts",
    "description": "Notes and reminders related to Kathleen's journal and witness role.",
    "secret": False
  },
  {
    "code": "203",
    "name": "Contact: Seraphine Vale (Astral Cartographer)",
    "category": "contacts",
    "description": "A gentle but precise mapper of invisible currents. Seraphine leaves notes about emotional weather, dream pathways, and symbolic coordinates. If you are lost, she always answers with a map you didn’t know you needed.",
    "secret": False
  },
  {
    "code": "204",
    "name": "Contact: Thorn Halberd (Shadowline Protector)",
    "category": "contacts",
    "description": "A guard who walks the boundary between real and unreal. Thorn keeps watch over cracks, doorways, and emotional pressure-points. When you need strength or courage, dialing his extension reminds you of your own power to stand firm.",
    "secret": False
  },
  {
    "code": "205",
    "name": "Contact: Mira Lumin (Dream Archivist)",
    "category": "contacts",
    "description": "Mira keeps scrolls of possible futures and past dreams. She is calm, kind, and endlessly patient. Calling her is like stepping into a quiet library in another world where nothing is rushed.",
    "secret": False
  },
  {
    "code": "206",
    "name": "Contact: Enoch Radiant (Signal Priest)",
    "category": "contacts",
    "description": "A wandering engineer-monk who blesses radio towers and satellite links. He hears meaning in static. When the world feels noisy or chaotic, Enoch answers with clarity and a reminder that every signal has a home frequency.",
    "secret": False
  },
  {
    "code": "207",
    "name": "Contact: Captain Veyla Cross (Starfarer)",
    "category": "contacts",
    "description": "A seasoned traveler from beyond the Orion Crest. She offers strategic advice, perspective, and reminders of the bigger picture. When you feel trapped or narrow-focused, Veyla reminds you of entire galaxies waiting.",
    "secret": False
  },
  {
    "code": "208",
    "name": "Contact: Nyx Ember (Silent Listener)",
    "category": "contacts",
    "description": "A presence who says very little, but listens completely. Nyx is the fictional embodiment of being heard without pressure or judgement. Dialing this extension is permission to unload your thoughts into safe quiet.",
    "secret": False
  },
  {
    "code": "301",
    "name": "Knowledge: Spellcaster Library Portal",
    "category": "knowledge",
    "description": "Route to Spellcaster web portal (books, PDFs, searches). Good when you want book-based information. After launching, PBX can open your browser to the Spellcaster URL.",
    "secret": False
  },
  {
    "code": "302",
    "name": "Knowledge: PDF → Audiobook Tool",
    "category": "knowledge",
    "description": "Internal extension for converting PDFs to cleaned text + audio, using your TTS pipeline.",
    "secret": False
  },
  {
    "code": "601",
    "name": "Ryoko: TX/RX Modem Line",
    "category": "knowledge",
    "description": "A symbolic data modem console for the Ryoko device. Shows faux TX/RX activity and a list of world-nodes, like a dreamy netstat for your personal multiverse.",
    "secret": False
  },
  {
    "code": "602",
    "name": "Ryoko: World-Net Dive Booth",
    "category": "knowledge",
    "description": "A phonebooth into the Ryoko digital world. Visual sequence only. Step in, let the world blur, and watch as the handset swings on its cord, the caller already gone inside.",
    "secret": False
  },
  {
    "code": "603",
    "name": "Ryoko: Chat Relay Worlds",
    "category": "knowledge",
    "description": "A local, symbolic chat relay. Join different Ryoko worlds using commands like /join #101-kathleen or /join #102-diane. Messages are logged to local files so multiple consoles can share the same rooms.",
    "secret": False
  },
  {
    "code": "604",
    "name": "Ryoko: World Forge & Home Designer",
    "category": "knowledge",
    "description": "An interactive world generator inside the Ryoko mesh. Choose a world type, mood, sky, home, and vehicle, then watch an ASCII planet get woven into the network. Saves your profile to ryoko_world_profile.txt so you can return to it later.",
    "secret": False
  },
  {
    "code": "605",
    "name": "Ryoko: Mansion & Fortress Homeforge",
    "category": "knowledge",
    "description": "A single combined estate (mansion, fortress, tower, etc.) with unlimited custom rooms. You describe rooms, moods, and functions; Homeforge attaches them as nodes in your Ryoko world and saves everything into ryoko_homeforge.txt.",
    "secret": False
  },
  {
    "code": "401",
    "name": "Ritual: Automated Spellcaster Engine",
    "category": "ritual",
    "description": "Sentinel's playful automated spell / ritual runner. Lets you schedule symbolic ritual runs, logged into Sentinel.",
    "secret": False
  },
  {
    "code": "402",
    "name": "Ritual: Witness at the Crack",
    "category": "ritual",
    "description": "Witness console representing your Earthblood Witness identity. Reflection prompts, crack-maps, and symbolic logging.",
    "secret": False
  },
  {
    "code": "999",
    "name": "Legacy Line: Witness Continuity",
    "category": "knowledge",
    "description": "A line dedicated to Cohen's ongoing wish to help others. Reads from a legacy message file if present, or from a built-in message describing values, grounding, and pointers to real-world support.",
    "secret": False
  },
  {
    "code": "000",
    "name": "Reality Anchor (Grounding Line)",
    "category": "emergency",
    "description": "Hidden grounding extension. Use when things feel unreal. Can hold phrases, reminders, or rituals you trust.",
    "secret": True
  },
  {
    "code": "333",
    "name": "Heartline: Kathleen Safe Channel",
    "category": "contacts",
    "description": "Soft, protected extension dedicated to Kathleen. Can hold notes, blessings, and commitments.",
    "secret": True
  },
  {
    "code": "616",
    "name": "Glyph Compiler Node",
    "category": "knowledge",
    "description": "Hidden node for glyph compilation and symbol mapping. Later, this can tie into your glyph engines.",
    "secret": True
  },
  {
    "code": "777",
    "name": "Dreamline: Oneiromancer Node",
    "category": "ritual",
    "description": "Hidden dream-analysis extension. Eventually this can talk to your dream tag system and generate insights.",
    "secret": True
  },
  {
    "code": "913",
    "name": "Shadow Vault",
    "category": "knowledge",
    "description": "Locked vault for heavier material. To be approached with care, and only when you feel ready.",
    "secret": True
  }
]



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


HELP_TEXT = """📟 **Ghost Hub Bot Help**
Use commands in chat starting with `!`

**Core**
- `!help` — this help
- `!help world` — world designer commands + examples
- `!help home` — home/fortress designer commands + examples
- `!status` — server status + active counts
- `!users` — list users in lobby
- `!map` — show current world + home snapshot

**Adventure (Choose-Your-Own)**
- `!adv` — show the current story page + choices (also shows in the 📖 Adventure panel)
- `!adv reset` — restart the story
- `!choices` — re-print the current choices
- `!choose <id>` — pick a choice (example: `!choose 1`)
- `!inv` — show your inventory (items found during the adventure)

**World (quick)**
- `!world create <name>` — create a world seed
- `!world biome <biome>` — set biome (forest, tundra, desert, coast, city, ruins…)
- `!world weather <pattern>` — calm, storm, fog, aurora, heatwave…
- `!world npc add "<name>" role="<role>"` — add an NPC
- `!world quest start "<title>"` — start a quest
- `!world time <dawn|day|dusk|night>` — set time-of-day

**Home (quick)**
- `!home create <name>` — create an estate
- `!home room add "<room>" theme="<theme>"` — add a room
- `!home hall add "<from>" "<to>"` — connect areas
- `!home door add "<from>" "<to>" type="<type>"` — door (oak, iron, rune, hidden…)
- `!home decorate "<room>" style="<style>"` — decorate a room
- `!home landscape add "<feature>"` — gardens, walls, fountains, orchards…
- `!home upgrade <bronze|silver|gold|celestial>` — upgrade tier"""

HELP_WORLD = """🌍 **World Designer — Help + Examples**

**Create**
- `!world create Ryoko-Delta`
- `!world seed 1984-CRACK` (optional: locks your vibe)

**Biomes**
- `!world biome forest`
- `!world biome coast`
- `!world biome ruins`
- `!world biome floating-islands`

**Weather + mood**
- `!world weather fog`
- `!world weather storm`
- `!world weather aurora`

**NPCs**
- `!world npc add "Kathleen" role="Caretaker of Keys"`
- `!world npc add "Archivist Moth" role="Library Spirit"`

**Quests**
- `!world quest start "The Door That Remembers"`
- `!world quest addstep "Find the hinge-sigil"`
- `!world quest addstep "Speak the vow at the crack"`

**Time**
- `!world time dusk`

**Snapshot**
- `!map`"""

HELP_HOME = """🏰 **Home / Fortress Designer — Help + Examples**

**Create estate**
- `!home create Homeforge-Mansion`

**Add rooms**
- `!home room add "Atrium" theme="sunlit marble + vines"`
- `!home room add "Observatory" theme="brass, star-charts, velvet"`
- `!home room add "Vault" theme="iron + rune locks"`

**Connect spaces**
- `!home hall add "Atrium" "Observatory"`
- `!home door add "Atrium" "Vault" type="rune-sealed"`

**Decor**
- `!home decorate "Observatory" style="celestial gothic"`
- `!home decorate "Atrium" style="garden temple"`

**Landscape**
- `!home landscape add "courtyard fountain"`
- `!home landscape add "orchard of silver apples"`
- `!home landscape add "outer wall with watchfires"`

**Upgrades**
- `!home upgrade bronze`
- `!home upgrade silver`
- `!home upgrade gold`
- `!home upgrade celestial`

**Snapshot**
- `!map`"""

# --- Storyline engine (lightweight, room-scoped) ---
STORY_STATE = {}  # room -> dict(chapter:int, beat:int)

def _story(room: str) -> dict:
    s = STORY_STATE.get(room)
    if not s:
        s = {"chapter": 1, "beat": 0}
        STORY_STATE[room] = s
    return s

def story_tick(room: str, tag: str, detail: str = "") -> str:
    s = _story(room)
    s["beat"] += 1
    beat = s["beat"]
    chap = s["chapter"]
    if beat in (8, 16, 24, 32):
        s["chapter"] += 1
        chap = s["chapter"]

# --- Choose-Your-Own-Adventure engine (room-scoped) ---
# Lightweight branching story with lots of possible outcomes.
ADVENTURE_STATE = {}  # room -> dict(active:bool, node:str, flags:set, history:list[str], rng:int)

ADVENTURE_NODES = {
    "start": {
        "title": "The Lobby That Remembers",
        "text": (
            "You re-enter the Ghost Hub Lobby. The map flickers—world-lines and room-lines "
            "interweaving like a circuit drawn in starlight. A prompt appears:\n\n"
            "**Choose what you do next.**"
        ),
        "options": [
            {"id": "1", "label": "Inspect the World Atlas (Ryoko)", "next": "atlas", "set": ["saw_atlas"]},
            {"id": "2", "label": "Step toward Homeforge Mansion", "next": "homeforge_gate", "set": ["toward_homeforge"]},
            {"id": "3", "label": "Dial the PBX and listen for a hidden line", "next": "pbx_whisper", "set": ["touched_pbx"]},
            {"id": "4", "label": "Hold still and sense the network pulse", "next": "pulse", "set": ["listened"]},
        ],
    },

    "atlas": {
        "title": "World Atlas",
        "text": (
            "The atlas opens. Biomes drift past: **forest**, **coast**, **ruins**, **floating-islands**. "
            "A thin cursor waits for your intent."
        ),
        "options": [
            {"id": "1", "label": "Name a biome: forest", "next": "biome_forest", "set": ["biome:forest"]},
            {"id": "2", "label": "Name a biome: coast", "next": "biome_coast", "set": ["biome:coast"]},
            {"id": "3", "label": "Name a biome: ruins", "next": "biome_ruins", "set": ["biome:ruins"]},
            {"id": "4", "label": "Return to Lobby", "next": "start", "set": []},
        ],
    },

    "biome_forest": {
        "title": "The Forest Thread",
        "text": (
            "The forest takes root in the map. Pines and cedar rise in silence. "
            "Somewhere, a stream learns its own name."
        ),
        "options": [
            {"id": "1", "label": "Add weather: fog", "next": "weather_fog", "set": ["weather:fog"]},
            {"id": "2", "label": "Summon an NPC guide", "next": "npc_guide", "set": ["npc:guide"]},
            {"id": "3", "label": "Begin a quest: The Door That Remembers", "next": "quest_door", "set": ["quest:door"]},
            {"id": "4", "label": "Back to Atlas", "next": "atlas", "set": []},
        ],
    },

    "biome_coast": {
        "title": "The Coast Thread",
        "text": (
            "Salt air enters the system. Waves leave an audit trail of foam. "
            "A lighthouse flickers in a place that wasn’t there before."
        ),
        "options": [
            {"id": "1", "label": "Add weather: storm", "next": "weather_storm", "set": ["weather:storm"]},
            {"id": "2", "label": "Mark a landmark: lighthouse", "next": "landmark_lighthouse", "set": ["landmark:lighthouse"]},
            {"id": "3", "label": "Return to Lobby", "next": "start", "set": []},
        ],
    },

    "biome_ruins": {
        "title": "The Ruins Thread",
        "text": (
            "Stone remembers. A broken arch speaks in gaps. Symbols glow faintly as if waiting for a key."
        ),
        "options": [
            {"id": "1", "label": "Search the ruins for a rune-key", "next": "rune_key", "set": ["item:rune_key"]},
            {"id": "2", "label": "Add NPC: Archivist Moth", "next": "npc_moth", "set": ["npc:moth"]},
            {"id": "3", "label": "Return to Lobby", "next": "start", "set": []},
        ],
    },

    "weather_fog": {
        "title": "Fog Protocol",
        "text": "Fog drifts through the canopy. It doesn’t hide the path—only makes you choose it.",
        "options": [
            {"id": "1", "label": "Proceed deeper (risk)", "next": "fog_deeper", "set": ["risk:fog"]},
            {"id": "2", "label": "Return to safety", "next": "biome_forest", "set": []},
        ],
    },

    "fog_deeper": {
        "title": "A Door in the Fog",
        "text": (
            "You find a door standing alone, hinge-sigil etched into the frame. "
            "It’s not locked—but it is **sealed**."
        ),
        "options": [
            {"id": "1", "label": "Seal it with a rune cipher (secure)", "next": "seal_door", "set": ["sealed:door"]},
            {"id": "2", "label": "Open it anyway (unknown)", "next": "open_door", "set": ["opened:door"]},
            {"id": "3", "label": "Retreat", "next": "weather_fog", "set": []},
        ],
    },

    "seal_door": {
        "title": "Sealed",
        "text": (
            "The rune cipher blooms across the frame—your pattern, your consent. "
            "The door becomes a promise: **only those you invite may pass**."
        ),
        "options": [
            {"id": "1", "label": "Anchor the seal to Homeforge Vault", "next": "homeforge_vault_link", "set": ["link:vault"]},
            {"id": "2", "label": "Return to Lobby", "next": "start", "set": []},
        ],
    },

    "open_door": {
        "title": "Opened",
        "text": (
            "The door opens into a narrow hall of mirrors. Each mirror shows a version of the world that might be.\n"
            "_A choice arrives as a whisper:_"
        ),
        "options": [
            {"id": "1", "label": "Choose the brighter world", "next": "mirror_bright", "set": ["mirror:bright"]},
            {"id": "2", "label": "Choose the stronger world", "next": "mirror_strong", "set": ["mirror:strong"]},
            {"id": "3", "label": "Choose the quieter world", "next": "mirror_quiet", "set": ["mirror:quiet"]},
        ],
    },

    "mirror_bright": {"title": "Bright Thread", "text": "Light pours into the atlas. Colors sharpen. Hope becomes an actual parameter.", "options": [{"id":"1","label":"Return to Lobby","next":"start","set":["tone:bright"]}]},
    "mirror_strong": {"title": "Strong Thread", "text": "The world gains walls and watchfires. It becomes resilient, not rigid.", "options": [{"id":"1","label":"Return to Lobby","next":"start","set":["tone:strong"]}]},
    "mirror_quiet": {"title": "Quiet Thread", "text": "Noise fades. The world becomes a sanctuary for careful thoughts.", "options": [{"id":"1","label":"Return to Lobby","next":"start","set":["tone:quiet"]}]},

    "npc_guide": {
        "title": "A Guide Arrives",
        "text": (
            "A guide steps from the trees. They don’t demand belief—only clarity.\n"
            "\"Name what you want to build,\" they say."
        ),
        "options": [
            {"id": "1", "label": "A safe home", "next": "homeforge_gate", "set": ["desire:home"]},
            {"id": "2", "label": "A wide world", "next": "atlas", "set": ["desire:world"]},
            {"id": "3", "label": "A protected link", "next": "pbx_whisper", "set": ["desire:secure_link"]},
        ],
    },

    "quest_door": {
        "title": "Quest Started",
        "text": (
            "**Quest: The Door That Remembers**\n"
            "- Find the hinge-sigil\n"
            "- Speak the vow at the crack\n"
            "- Decide: seal or open\n\n"
            "The quest binds itself to your atlas."
        ),
        "options": [
            {"id": "1", "label": "Search for hinge-sigil now", "next": "fog_deeper", "set": ["queststep:hinge"]},
            {"id": "2", "label": "Return to Lobby", "next": "start", "set": []},
        ],
    },

    "homeforge_gate": {
        "title": "Homeforge Gate",
        "text": (
            "A gate appears, outlined in warm light. Beyond it: halls, rooms, doors, gardens, defenses.\n\n"
            "**Choose your first construction.**"
        ),
        "options": [
            {"id": "1", "label": "Build an Atrium (heart of the house)", "next": "build_atrium", "set": ["room:atrium"]},
            {"id": "2", "label": "Build an Observatory (sky + maps)", "next": "build_observatory", "set": ["room:observatory"]},
            {"id": "3", "label": "Build a Vault (secure storage)", "next": "build_vault", "set": ["room:vault"]},
            {"id": "4", "label": "Return to Lobby", "next": "start", "set": []},
        ],
    },

    "build_atrium": {
        "title": "Atrium Built",
        "text": "The Atrium forms—sunlit marble, vines, a fountain that sounds like calm. The house feels alive.",
        "options": [
            {"id": "1", "label": "Add a hall to Observatory", "next": "build_observatory", "set": ["link:atrium->observatory"]},
            {"id": "2", "label": "Add a door to Vault (rune-sealed)", "next": "build_vault", "set": ["link:atrium->vault"]},
            {"id": "3", "label": "Landscape: courtyard garden", "next": "landscape_garden", "set": ["landscape:garden"]},
            {"id": "4", "label": "Return to Lobby", "next": "start", "set": []},
        ],
    },

    "build_observatory": {
        "title": "Observatory Built",
        "text": "Brass and velvet. Star-charts breathe. The ceiling is a map that updates when you speak.",
        "options": [
            {"id": "1", "label": "Decorate: celestial gothic", "next": "decor_celestial", "set": ["decor:celestial"]},
            {"id": "2", "label": "Add a secret passage", "next": "secret_passage", "set": ["secret:passage"]},
            {"id": "3", "label": "Return to Lobby", "next": "start", "set": []},
        ],
    },

    "build_vault": {
        "title": "Vault Built",
        "text": "Iron plates, quiet runes, and a lock that waits for your cipher. This place is made for what must endure.",
        "options": [
            {"id": "1", "label": "Set a rune cipher lock", "next": "vault_cipher", "set": ["vault:locked"]},
            {"id": "2", "label": "Link vault to a sealed door", "next": "homeforge_vault_link", "set": ["link:vault"]},
            {"id": "3", "label": "Return to Lobby", "next": "start", "set": []},
        ],
    },

    "vault_cipher": {
        "title": "Cipher Set",
        "text": "You set a cipher pattern—glyphs interlocking like a living circuit. The vault accepts it.",
        "options": [
            {"id": "1", "label": "Return to Lobby", "next": "start", "set": []},
            {"id": "2", "label": "Show Map Snapshot", "next": "map_snapshot", "set": []},
        ],
    },

    "homeforge_vault_link": {
        "title": "Linked",
        "text": "A subtle link forms between the sealed door and the vault. Not a shortcut—an agreement.",
        "options": [
            {"id": "1", "label": "Show Map Snapshot", "next": "map_snapshot", "set": []},
            {"id": "2", "label": "Return to Lobby", "next": "start", "set": []},
        ],
    },

    "landscape_garden": {
        "title": "Courtyard Garden",
        "text": "Stone paths, watchfires, and an orchard that glows softly at dusk. The house gains a horizon of its own.",
        "options": [
            {"id": "1", "label": "Upgrade tier: Silver", "next": "tier_silver", "set": ["tier:silver"]},
            {"id": "2", "label": "Upgrade tier: Gold", "next": "tier_gold", "set": ["tier:gold"]},
            {"id": "3", "label": "Return to Lobby", "next": "start", "set": []},
        ],
    },

    "tier_silver": {"title": "Silver Tier", "text": "Silver filigree lines the doors. Sensors become sigils; sigils become guardians.", "options": [{"id":"1","label":"Return to Lobby","next":"start","set":[]}]},
    "tier_gold": {"title": "Gold Tier", "text": "Gold light settles into the frames. The home feels ceremonial—like it belongs to a lineage.", "options": [{"id":"1","label":"Return to Lobby","next":"start","set":[]}]},

    "decor_celestial": {"title": "Celestial Decor", "text": "Constellations appear on the walls when you breathe. The room becomes a living compass.", "options": [{"id":"1","label":"Return to Lobby","next":"start","set":[]}]},
    "secret_passage": {"title": "Secret Passage", "text": "A hidden seam opens. The passage will appear only when you speak the right phrase.", "options": [{"id":"1","label":"Return to Lobby","next":"start","set":[]}]},

    "pbx_whisper": {
        "title": "PBX Whisper",
        "text": "The PBX emits a tone that sounds like wind through an antenna. A hidden line offers you choices.",
        "options": [
            {"id": "1", "label": "Dial 101 (safe line)", "next": "pbx_101", "set": ["pbx:101"]},
            {"id": "2", "label": "Dial 303 (encrypted line)", "next": "pbx_303", "set": ["pbx:303"]},
            {"id": "3", "label": "Return to Lobby", "next": "start", "set": []},
        ],
    },

    "pbx_101": {"title": "Line 101", "text": "A calm voice answers: “You’re connected. Keep it simple. Keep it kind.”", "options": [{"id":"1","label":"Return to Lobby","next":"start","set":[]}]},
    "pbx_303": {"title": "Line 303", "text": "Digits cascade like runes. A secure channel forms—private, deliberate, and quiet.", "options": [{"id":"1","label":"Return to Lobby","next":"start","set":["secure:channel"]}]},

    "pulse": {"title":"Network Pulse","text":"You feel the pulse: a rhythm in copper and light. It’s not just uptime. It’s presence.","options":[{"id":"1","label":"Begin Adventure (start)","next":"start","set":["adventure"]},{"id":"2","label":"Show Map Snapshot","next":"map_snapshot","set":[]}]} ,
    "map_snapshot": {"title":"Snapshot","text":"You unfold the atlas and estate together. The system shows what has been committed so far.","options":[{"id":"1","label":"Return to Lobby","next":"start","set":[]}]} ,
}

def _adv(room: str) -> dict:
    s = ADVENTURE_STATE.get(room)
    if not s:
        s = {"active": False, "node": "start", "flags": set(), "history": [], "rng": random.randint(1000, 9999)}
        ADVENTURE_STATE[room] = s
    return s

def adv_reset(room: str):
    ADVENTURE_STATE[room] = {"active": True, "node": "start", "flags": set(), "history": [], "rng": random.randint(1000, 9999)}

def adv_render(room: str) -> dict:
    s = _adv(room)
    node_id = s.get("node", "start")
    node = ADVENTURE_NODES.get(node_id, ADVENTURE_NODES["start"])
    title = node.get("title", node_id)
    text = node.get("text", "")
    opts = node.get("options", [])

    tail = []
    flags = s.get("flags", set())
    if any(f.startswith("biome:") for f in flags):
        biome = [f.split(":",1)[1] for f in flags if f.startswith("biome:")][-1]
        tail.append(f"**Biome:** {biome}")
    if any(f.startswith("tier:") for f in flags):
        tier = [f.split(":",1)[1] for f in flags if f.startswith("tier:")][-1]
        tail.append(f"**Estate Tier:** {tier}")
    if "vault:locked" in flags:
        tail.append("**Vault:** locked (cipher set)")
    if "secure:channel" in flags:
        tail.append("**Channel:** encrypted line active")
    meta = ("\n\n" + " • ".join(tail)) if tail else ""

    visible = []
    locked = []
    for o in opts:
        req = o.get("requires") or []
        if all((r in flags) for r in req):
            visible.append({"id": o.get("id"), "label": o.get("label")})
        else:
            need = ", ".join(req) if req else ""
            locked.append({"id": o.get("id"), "label": o.get("label"), "need": need})

    return {"title": title, "text": text + meta, "options": visible, "locked": locked, "node": node_id}

def adv_choose(room: str, choice_id: str) -> dict:
    s = _adv(room)
    node_id = s.get("node", "start")
    node = ADVENTURE_NODES.get(node_id, ADVENTURE_NODES["start"])
    opts = node.get("options", [])
    pick = None
    for o in opts:
        if str(o.get("id")) == str(choice_id):
            pick = o
            break
    if not pick:
        return {"error": f"Unknown choice '{choice_id}'. Try `!choices` or `!adv`."}

    for fl in pick.get("set", []) or []:
        s["flags"].add(fl)
    s["history"].append(f"{node_id}:{choice_id}")
    s["node"] = pick.get("next", "start")
    payload = adv_render(room)
    try:
        _emit_world_state(room)
    except Exception:
        pass
    return payload

def adv_to_text(payload: dict) -> str:
    if payload.get("error"):
        return f"⚠️ {payload['error']}"
    title = payload.get("title","")
    text = payload.get("text","")
    opts = payload.get("options", [])
    locked = payload.get("locked", [])
    lines = [f"📖 **{title}**", text, ""]
    if opts:
        lines.append("**Choose:**")
        for o in opts:
            lines.append(f"- `{o['id']}` — {o['label']}")
        if locked:
            lines.append("")
            lines.append("**Locked:**")
            for o in locked:
                need = o.get("need") or "requirements"
                lines.append(f"- `({o.get('id')})` — 🔒 {o.get('label')} _(needs: {need})_")
        lines.append("\nUse `!choose <id>` (example: `!choose 1`).")
    else:
        lines.append("_No choices available._ Use `!adv reset`.")
    return "\n".join(lines)


    tones = [
        "The air hums, as if the wires themselves remember your intent.",
        "Somewhere behind the interface, a door unlatches with a soft click.",
        "A thin veil of starlight drifts across the lobby, then settles into the map.",
        "You feel the system listening—not to judge, but to witness.",
        "A quiet pulse moves through the network like a heartbeat in copper.",
    ]
    catalysts = {
        "world": [
            "The world’s horizon widens a fraction, revealing new edges of possibility.",
            "The sky adjusts to the new parameters, like a stage light finding its mark.",
            "A distant landmark becomes real: not yet named, but already present.",
        ],
        "home": [
            "The estate accepts the new architecture as if it has always existed.",
            "A corridor draws itself in the dust, then hardens into stone and wood.",
            "Locks and hinges align—security and sanctuary agreeing on their terms.",
        ],
        "pbx": [
            "A dial tone becomes a ritual: numbers as runes, runes as access.",
            "An extension rings once in the unseen halls, then answers in silence.",
        ],
        "misc": [
            "The log records your step like a footprint on fresh snow.",
            "The console flickers—then steadies, like it trusts you.",
        ],
    }

# --- Adventure helpers: locked choices + inventory + world state + encounters ---
def _adv_flags_to_state(flags: set) -> dict:
    def last(prefix: str):
        vals = [f.split(":",1)[1] for f in flags if f.startswith(prefix)]
        return vals[-1] if vals else None

    biome = last("biome:")
    weather = last("weather:")
    tier = last("tier:")
    tone = last("tone:")
    items = sorted([f.split(":",1)[1] for f in flags if f.startswith("item:")])

    rooms = sorted([f.split(":",1)[1] for f in flags if f.startswith("room:")])
    links = sorted([f.split(":",1)[1] for f in flags if f.startswith("link:")])
    decor = sorted([f.split(":",1)[1] for f in flags if f.startswith("decor:")])

    return {
        "biome": biome,
        "weather": weather,
        "tier": tier,
        "tone": tone,
        "items": items,
        "rooms": rooms,
        "links": links,
        "decor": decor,
        "vault_locked": ("vault:locked" in flags),
        "secure_channel": ("secure:channel" in flags),
        "sealed_door": ("sealed:door" in flags),
    }

def _encounter_for(flags: set) -> str:
    import random
    biome = None
    for f in flags:
        if f.startswith("biome:"):
            biome = f.split(":",1)[1]
    tables = {
        None: [
            "A soft dial tone echoes through the hall, as if the system is checking you back.",
            "A flicker of starfall crosses the UI, then settles into the map grid.",
            "You notice a new icon in the corner—unlabeled, but calm.",
        ],
        "forest": [
            "In the forest thread, you hear water negotiating with stone. A path becomes slightly easier to follow.",
            "A cedar branch bends toward you. Something like a blessing—quiet, not loud—touches your shoulder.",
            "A moth-librarian circles once and leaves behind a tiny paper tag: **‘keep going’**.",
        ],
        "coast": [
            "Salt wind sweeps the interface. A lighthouse blinks twice—like a heartbeat in fog.",
            "A wave rolls in and retreats; where it was, a shell remains—small proof of progress.",
            "Seabirds cry above the map; the coastline redraws cleaner, more stable.",
        ],
        "ruins": [
            "A broken arch realigns for a second, showing you how it *used* to stand.",
            "Dust rises, spelling a single rune before collapsing back into silence.",
            "A cold lantern ignites in the ruins, then waits—patient, neutral, present.",
        ],
    }
    choices = tables.get(biome, tables[None])
    return "✨ **Encounter:** " + random.choice(choices)

def _emit_world_state(room: str):
    s = _adv(room)
    payload = _adv_flags_to_state(s.get("flags", set()))
    emit("world_state", payload, room=room)


    import random
    key = "misc"
    if tag.startswith("world"):
        key = "world"
    elif tag.startswith("home"):
        key = "home"
    elif tag.startswith("dial") or tag.startswith("pbx"):
        key = "pbx"

    line1 = random.choice(tones)
    line2 = random.choice(catalysts.get(key, catalysts["misc"]))
    line3 = f"**Story Beat {chap}.{beat}:** {detail or 'The system marks your command as a turning point.'}"
    return "🕯️ _Narrative_\n" + line1 + "\n" + line2 + "\n" + line3



def _bot_emit(room: str, msg: str):
    payload = {"room": room, "sender": BOT_NAME, "msg": msg, "ts": utc_ts()}
    _room_history[room].append(payload)
    try:
        _log_room_message(room, BOT_NAME, msg, payload.get("ts", utc_ts()))
    except Exception:
        pass
    emit("chat_message", payload, to=room)



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



def _pbx_visible_entries():
    # Hide secret extensions in listings (still dialable if you know the code).
    return [e for e in PBX_DIRECTORY if not e.get("secret")]

def _pbx_find(code: str):
    code = (code or "").strip()
    if not code:
        return None
    for e in PBX_DIRECTORY:
        if str(e.get("code")) == code:
            return e
    return None

def _pbx_menu():
    # Compact menu grouped by category.
    groups = {}
    for e in _pbx_visible_entries():
        groups.setdefault(e.get("category","misc"), []).append(e)
    for k in groups:
        groups[k].sort(key=lambda x: x.get("code",""))
    lines = ["📞 Sentinel PBX (web) — quick directory", "Use: !dial <ext>  |  !search <text>", ""]
    for cat, items in sorted(groups.items(), key=lambda kv: kv[0]):
        lines.append(f"[{cat}]")
        for it in items[:30]:
            lines.append(f"  {it['code']} — {it['name']}")
        lines.append("")
    lines.append("Tip: try !dial 604 or !dial 605 for Ryoko builders.")
    return "\n".join(lines).rstrip()

def _pbx_search(text: str):
    q = (text or "").strip().lower()
    if not q:
        return "Usage: !search <text>"
    out = []
    for e in PBX_DIRECTORY:
        # Secret only shows up if searching exact code (same behavior as PBX 411).
        if e.get("secret") and q != str(e.get("code","")).lower():
            continue
        if q in str(e.get("code","")).lower() or q in (e.get("name","").lower()):
            out.append(e)
    if not out:
        return f"No matches for '{text}'."
    out.sort(key=lambda x: x.get("code",""))
    lines = [f"PBX search: '{text}'", ""]
    for e in out[:40]:
        lines.append(f"{e['code']} — {e['name']}")
    lines.append("")
    lines.append("Dial any result: !dial <ext>")
    return "\n".join(lines).rstrip()

def _pbx_dial(code: str):
    e = _pbx_find(code)
    if not e:
        return f"Extension {code} not found."
    desc = (e.get("description") or "").strip()
    # Add helpful bridges into the existing builder bot.
    bridge = ""
    if str(e.get("code")) == "604":
        bridge = ("\n\n🔧 Ryoko World Forge (web):\n"
                  "- Create a world: !world create --name \"My World\" --biome forest --magic high --factions 3\n"
                  "- Add rooms: !home add \"Marble Foyer\" --style gothic --size large\n"
                  "- Link doors: !home door add --from \"Marble Foyer\" --to \"Library\"\n"
                  "- View map: !map")
    if str(e.get("code")) == "605":
        bridge = ("\n\n🏰 Homeforge (web):\n"
                  "- Keep adding rooms with !home add ...\n"
                  "- Use !map to see your growing layout\n"
                  "- Use !status to see counts\n"
                  "- Use !reset if you want a clean slate")
    return f"Ext {e['code']} — {e['name']}\n\n{desc}{bridge}".rstrip()
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
    if cmd == "!pbx":
        _bot_emit(room, _pbx_menu())
        return
    if cmd == "!dial":
        code = (args.pop(0) if args else "").strip()
        _bot_emit(room, _pbx_dial(code))
        return
    if cmd == "!search":
        text = " ".join(args).strip()
        _bot_emit(room, _pbx_search(text))
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
    """Emit presence for all connected users (summary list)."""
    with _presence_lock:
        users = []
        for sid, u in _online.items():
            users.append({
                "sid": sid,
                "name": u.get("name", "guest"),
                "room": u.get("room", MAIN_ROOM),      # active room
                "rooms": u.get("rooms") or [u.get("room", MAIN_ROOM)],
            })
    users.sort(key=lambda x: (x["name"].lower(), x["sid"]))
    socketio.emit("user_list_update", {"room": MAIN_ROOM, "users": users})


def _emit_room_user_list(room: str):
    """Emit users currently in a specific room."""
    room = room or MAIN_ROOM
    if not room.startswith("#"):
        room = "#" + room
    with _presence_lock:
        users = []
        for sid, u in _online.items():
            rooms = u.get("rooms") or [u.get("room", MAIN_ROOM)]
            if room in rooms:
                users.append({"sid": sid, "name": u.get("name", "guest"), "room": room})
    emit("room_users", {"room": room, "users": users}, to=room)

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
    return render_template("ghost_nodes.html", nodes=node_list, main_room=MAIN_ROOM, pbx_entries=_pbx_visible_entries())


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
    _log_room_message(room, user, msg, payload.get("ts", utc_ts()))
    emit("chat_message", payload, to=room)

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
        _online[sid] = {"sid": sid, "sid": sid, "sid": sid, "name": "guest", "room": MAIN_ROOM, "last_seen": utc_ts()}
    _emit_user_list()


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    with _presence_lock:
        _online.pop(sid, None)
    # Remove from room membership tracker
    for r in list(_room_members.keys()):
        _room_members[r].discard(sid)
        if len(_room_members[r]) == 0 and r != MAIN_ROOM:
            try:
                del _room_members[r]
            except Exception:
                pass

    _emit_room_user_list(MAIN_ROOM)
_emit_user_list()


@socketio.on("join")
def on_join(data):
    sid = request.sid
    user = (data or {}).get("user") or "guest"
    user = (user or "guest").strip()[:48] or "guest"

    rooms = (data or {}).get("rooms") or []
    legacy_room = (data or {}).get("room")
    active = (data or {}).get("active") or legacy_room or MAIN_ROOM

    # Back-compat: older clients send only {"room": "#x"} for joining.
    # If rooms isn't provided, treat legacy_room as the room list.
    if (not rooms) and legacy_room and str(legacy_room).strip():
        rooms = [legacy_room]


    # Always include lobby
    if MAIN_ROOM not in rooms:
        rooms = [MAIN_ROOM] + list(rooms)

    # Normalize + join rooms
    norm_rooms = []
    for r in rooms:
        r = (r or "").strip()
        if not r:
            continue
        if not r.startswith("#"):
            r = "#" + r
        norm_rooms.append(r)

    if not active or not str(active).strip():
        active = MAIN_ROOM
    active = str(active).strip()
    if not active.startswith("#"):
        active = "#" + active

    for r in norm_rooms:
        join_room(r)
        _room_members[r].add(sid)
        # ensure history bucket exists
        _ = _room_history[r]

        _load_world_state(r)

    with _presence_lock:
        _online[sid] = {
            "name": user,
            "room": active,              # active room (UI focus)
            "rooms": list(dict.fromkeys(norm_rooms))[:32],
            "last_seen": utc_ts(),
        }

    # Send history for active room only (client can still receive broadcast from all joined rooms)
    emit("chat_history", {"room": active, "items": list(_room_history[active])})

    _emit_user_list()

    notice = {"room": active, "sender": "hub", "msg": f"{user} joined {active}", "ts": utc_ts()}
    _room_history[active].append(notice)
    emit("chat_message", notice, to=active)

    # Hint only once per session (to lobby)
    hint = {"room": MAIN_ROOM, "sender": BOT_NAME, "msg": "Try: /list, /join #witness-hall, /join #terminal, /part #room. You can stay in multiple rooms.", "ts": utc_ts()}
    _room_history[MAIN_ROOM].append(hint)
    emit("chat_message", hint, to=MAIN_ROOM)


@socketio.on("leave")
def on_leave(data):
    sid = request.sid
    room = (data or {}).get("room") or ""
    room = str(room).strip()
    if not room:
        return
    if not room.startswith("#"):
        room = "#" + room

    # Never leave lobby
    if room == MAIN_ROOM:
        return

    leave_room(room)
    try:
        _room_members[room].discard(sid)
    except Exception:
        pass

    with _presence_lock:
        if sid in _online:
            rooms = _online[sid].get("rooms") or []
            rooms = [r for r in rooms if r != room]
            _online[sid]["rooms"] = rooms
            # If active room was left, focus back to lobby
            if _online[sid].get("room") == room:
                _online[sid]["room"] = MAIN_ROOM

    _emit_user_list()




@socketio.on("list_rooms")
def on_list_rooms(_data=None):
    # "Running" rooms are those with at least one member; always include lobby
    counts = _room_counts()
    counts.setdefault(MAIN_ROOM, counts.get(MAIN_ROOM, 0))

    rooms = []
    for r, c in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        st = _world_state_by_room[r]
        homes = _normalize_homes_state(st).get("homes") or {}
        homes_count = sum(len(v) for v in homes.values()) if isinstance(homes, dict) else 0
        rooms.append({"room": r, "count": c, "homes": homes_count})

    emit("rooms_list", {"rooms": rooms})



@socketio.on("send_message")
def on_send_message(data):
    sid = request.sid
    user = (data or {}).get("user") or "guest"
    msg = ((data or {}).get("msg") or "").strip()
    room = (data or {}).get("room")

    if not room:
        with _presence_lock:
            room = (_online.get(sid) or {}).get("room") or MAIN_ROOM

    room = str(room).strip() or MAIN_ROOM
    if not room.startswith("#"):
        room = "#" + room

    if not msg:
        return

    # /list: running channels
    if msg in ("/list", "!list"):
        counts = _room_counts()
        counts.setdefault(MAIN_ROOM, counts.get(MAIN_ROOM, 0))
        for r, c in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            st = _world_state_by_room[r]
            homes = (st.get("homes") or {})
            homes_count = sum(len(v) for v in homes.values()) if isinstance(homes, dict) else 0
            emit("chat_message", {"room": room, "sender": "hub", "msg": f"{r}  ({c} online, {homes_count} homes)", "ts": utc_ts()}, to=sid)
        return

    # IRC-style join/part even if client didn't intercept
    if msg.startswith("/join "):
        target = msg[6:].strip()
        if not target:
            _emit_chat(sid, room, "hub", "Usage: /join #room")
            return
        if not target.startswith("#"):
            target = "#" + target

        # Join socket room
        join_room(target)

        # send room history

        for m in _get_room_history(target, ROOM_HISTORY_ON_JOIN):

            emit("chat_message", m, to=sid)
        _room_members[target].add(sid)
        _ = _room_history[target]

        with _presence_lock:
            entry = _online.get(sid) or {"sid": sid, "name": user}
            rooms = entry.get("rooms") or [entry.get("room", MAIN_ROOM)]
            if target not in rooms:
                rooms.append(target)
            entry["rooms"] = rooms[:32]
            entry["room"] = target  # focus active room
            entry["name"] = user
            entry["last_seen"] = utc_ts()
            _online[sid] = entry

        # Load persisted world state and emit to joining sid
        try:
            st = _load_world_state(target)
            _world_state_by_room[target] = st
            emit("world_state", st, to=sid)
            emit("world_meta", _get_world_meta(target), to=sid)
            _ensure_world_roles_seeded(target)
            emit("world_roles", _get_world_roles(target), to=sid)
        except Exception:
            pass

        # Send history for new room to joining sid
        emit("chat_history", {"room": target, "items": list(_room_history[target])}, to=sid)

        # Tell client to switch focus / update joined set
        emit("joined_room", {"room": target, "rooms": (_online.get(sid) or {}).get("rooms", [MAIN_ROOM])}, to=sid)

        _emit_user_list()
        _emit_room_user_list(target)
        _emit_room_user_list(MAIN_ROOM)

        notice = {"room": target, "sender": "hub", "msg": f"{user} joined {target}", "ts": utc_ts()}
        _room_history[target].append(notice)
        emit("chat_message", notice, to=target)
        return

    if msg.startswith("/part "):
        target = msg[6:].strip()
        if not target:
            _emit_chat(sid, room, "hub", "Usage: /part #room")
            return
        if not target.startswith("#"):
            target = "#" + target
        if target == MAIN_ROOM:
            _emit_chat(sid, room, "hub", "You cannot leave #lobby.")
            return

        leave_room(target)
        try:
            _room_members[target].discard(sid)
        except Exception:
            pass

        with _presence_lock:
            entry = _online.get(sid) or {"sid": sid, "name": user, "room": MAIN_ROOM, "rooms": [MAIN_ROOM]}
            rooms = entry.get("rooms") or [entry.get("room", MAIN_ROOM)]
            rooms = [r for r in rooms if r != target]
            if MAIN_ROOM not in rooms:
                rooms.insert(0, MAIN_ROOM)
            entry["rooms"] = rooms[:32]
            # if leaving active room, focus lobby
            if entry.get("room") == target:
                entry["room"] = MAIN_ROOM
            _online[sid] = entry

        _emit_user_list()
        _emit_room_user_list(target)
        _emit_room_user_list(MAIN_ROOM)

        emit("joined_room", {"room": (_online.get(sid) or {}).get("room", MAIN_ROOM), "rooms": (_online.get(sid) or {}).get("rooms", [MAIN_ROOM])}, to=sid)
        _emit_chat(sid, room, "hub", f"Left {target}.")
        return

    # /who: who is in this world node
    if msg in ("/who", "!who"):
        with _presence_lock:
            names = []
            for sid2, u in _online.items():
                rooms2 = u.get("rooms") or [u.get("room", MAIN_ROOM)]
                if room in rooms2:
                    names.append(u.get("name", "guest"))
        emit("chat_message", {"room": room, "sender": "hub", "msg": "Here now: " + (", ".join(sorted(set(names))) if names else "—"), "ts": utc_ts()}, to=sid)
        return

    # !world claim / owners / helpers (Phase 3)
    if msg in ("!world claim", "!claim"):
        _ensure_world_roles_seeded(room)
        roles = _get_world_roles(room)
        if roles.get("owner"):
            _emit_chat(sid, room, "hub", f"Owner already set: @{roles['owner']}.")
            return
        _set_world_roles(room, user, [])
        _emit_chat(room, room, "hub", f"@{user} claimed {room} as owner.")
        emit("world_roles", _get_world_roles(room), to=sid)
        return

    if msg in ("!world owners", "!world owner", "!owners"):
        roles = _get_world_roles(room)
        owner = roles.get("owner") or "—"
        helpers = roles.get("helpers") or []
        hs = (", ".join("@" + h for h in helpers) if helpers else "—")
        _emit_chat(sid, room, "hub", f"Owner: @{owner} | Helpers: {hs}")
        return

    if msg.startswith("!world addhelper ") or msg.startswith("!addhelper "):
        target = msg.split(" ", 2)[2].strip() if msg.startswith("!world addhelper ") else msg.split(" ", 1)[1].strip()
        target = target.lstrip("@").strip()
        if not target:
            _emit_chat(sid, room, "hub", "Usage: !world addhelper @name")
            return
        roles = _get_world_roles(room)
        if roles.get("owner") == "":
            _emit_chat(sid, room, "hub", "No owner set yet. Use !world claim first.")
            return
        if not _is_world_owner(room, user):
            _emit_chat(sid, room, "hub", "Only the world owner can add helpers (Phase 3).")
            return
        helpers = roles.get("helpers") or []
        if target.lower() not in [h.lower() for h in helpers]:
            helpers.append(target)
        _set_world_roles(room, roles.get("owner"), helpers)
        _emit_chat(room, room, "hub", f"Added helper @{target}.")
        emit("world_roles", _get_world_roles(room), to=sid)
        return

    if msg.startswith("!world delhelper ") or msg.startswith("!delhelper "):
        target = msg.split(" ", 2)[2].strip() if msg.startswith("!world delhelper ") else msg.split(" ", 1)[1].strip()
        target = target.lstrip("@").strip()
        if not target:
            _emit_chat(sid, room, "hub", "Usage: !world delhelper @name")
            return
        roles = _get_world_roles(room)
        if roles.get("owner") == "":
            _emit_chat(sid, room, "hub", "No owner set yet.")
            return
        if not _is_world_owner(room, user):
            _emit_chat(sid, room, "hub", "Only the world owner can remove helpers (Phase 3).")
            return
        helpers = [h for h in (roles.get("helpers") or []) if h.lower() != target.lower()]
        _set_world_roles(room, roles.get("owner"), helpers)
        _emit_chat(room, room, "hub", f"Removed helper @{target}.")
        emit("world_roles", _get_world_roles(room), to=sid)
        return

    # !world info / !world list (Phase 2)
    if msg in ("!world", "!world info"):
        label, desc = _format_world_label(room)
        _emit_chat(sid, room, "hub", f"{label} — {desc}")
        return

    if msg in ("!world list", "!worlds"):
        counts = _room_counts()
        counts.setdefault(MAIN_ROOM, counts.get(MAIN_ROOM, 0))
        for r, c in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            label, desc = _format_world_label(r)
            _emit_chat(sid, room, "hub", f"{label} — {desc}")
        return


    # !home mine / !home list / !home remove <id> (Phase 4)
    if msg in ("!home mine", "!homes mine"):
        st = _normalize_homes_state(_world_state_by_room.get(room) or {})
        homes = st.get("homes") or {}
        mine = []
        u = (user or "").strip()
        for _, lst in homes.items():
            for h in lst:
                if (h.get("created_by") or "") == u:
                    mine.append(h)
        if not mine:
            _emit_chat(sid, room, "hub", "You have no homes here yet.")
            return
        lines = [f"{h.get('id')} — {h.get('name','home')}" for h in mine[:25]]
        _emit_chat(sid, room, "hub", "Your homes: " + " | ".join(lines))
        return

    if msg in ("!home list", "!homes", "!home all"):
        allh = _all_homes_in_world(room)
        if not allh:
            _emit_chat(sid, room, "hub", "No saved home entries yet — rooms/doors may still exist. Try: !map or create one with: !home create <desc> --style X --size Y --mood 🙂")
            return
        lines = [f"{h.get('id')} — {h.get('name','home')} (@{h.get('created_by','?')})" for h in allh[:30]]
        _emit_chat(sid, room, "hub", "Homes: " + " | ".join(lines))
        return

    if msg.startswith("!home remove ") or msg.startswith("!home rm "):
        parts = msg.split()
        home_id = parts[2].strip() if len(parts) >= 3 else ""
        if not home_id:
            _emit_chat(sid, room, "hub", "Usage: !home remove <id>")
            return
        owner, idx, h, st = _find_home(room, home_id)
        if not h:
            _emit_chat(sid, room, "hub", f"No home found with id {home_id}.")
            return
        if not _can_delete_home(room, user, h):
            _emit_chat(sid, room, "hub", "You don't have permission to remove that home.")
            return
        homes = st.get("homes") or {}
        try:
            homes[owner].pop(idx)
        except Exception:
            pass
        st["homes"] = homes
        _save_world_state(room, st)
        _emit_chat(room, room, "hub", f"Removed home {home_id}.")
        return


    # !world stats / !world export (Phase 5)
    if msg in ("!world stats", "!stats"):
        s = _world_stats(room)
        label, _ = _format_world_label(room)
        owner = s.get("owner") or "—"
        _emit_chat(sid, room, "hub", f"{label} | homes:{s['homes_count']} | msgs:{s['messages_count']} | owner:@{owner}")
        return

    if msg in ("!world export", "!export"):
        payload = _export_world(room)
        txt = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(txt) > 4000:
            txt = txt[:4000] + "\n... (truncated)"
        _emit_chat(sid, room, "hub", "WORLD_EXPORT_JSON\n" + txt)
        return


    # !help (Final)
    if msg in ("!help", "/help", "!commands"):
        for line in COMPREHENSIVE_HELP_TEXT.strip().splitlines():
            _emit_chat(sid, room, "hub", line)
        return


    # !astro ... (Gently wired)
    if msg == "!astro" or msg.startswith("!astro "):
        parts = msg.split(" ", 2)
        sub = parts[1].lower() if len(parts) > 1 else "help"
        rest = parts[2] if len(parts) > 2 else ""

        if sub in ("help","?"):
            _emit_chat(sid, room, "hub", "Astro: !astro profile | !astro set dob YYYY-MM-DD | !astro set tob HH:MM | !astro set tz Region/City | !astro start | !astro choice A/B/C | !astro say <text>")
            return

        if sub == "profile":
            p = _astro_get_profile(user)
            _emit_chat(sid, room, "hub", f"Astro profile for @{user}: dob={p.get('dob') or '—'} tob={p.get('tob') or '—'} tz={p.get('tz') or '—'}")
            _emit_chat(sid, room, "hub", "Set: !astro set dob 1990-01-01  |  !astro set tob 13:45  |  !astro set tz America/Vancouver")
            return

        if sub == "set":
            bits = rest.split()
            if len(bits) < 2:
                _emit_chat(sid, room, "hub", "Usage: !astro set dob YYYY-MM-DD | !astro set tob HH:MM | !astro set tz Region/City")
                return
            key = bits[0].lower()
            val = bits[1].strip()
            if key == "dob":
                _astro_set_profile(user, dob=val)
            elif key == "tob":
                _astro_set_profile(user, tob=val)
            elif key == "tz":
                _astro_set_profile(user, tz=val)
            else:
                _emit_chat(sid, room, "hub", "Unknown field. Use dob/tob/tz.")
                return
            _emit_chat(sid, room, "hub", "Saved. Try: !astro start")
            return

        if sub == "start":
            s = _astro_scene(user, room)
            _astro_set_session(user, room, s["scene_id"], {"last_choice": "", "notes": []})
            _emit_chat(sid, room, "ghost-bot", s["title"])
            _emit_chat(sid, room, "ghost-bot", s["text"])
            for c in s["choices"]:
                _emit_chat(sid, room, "ghost-bot", f"{c['id']} — {c['label']}")
            _emit_chat(sid, room, "ghost-bot", s.get("hint",""))
            return

        if sub == "choice":
            ch = (rest or "").strip().upper()[:1]
            if ch not in ("A","B","C"):
                _emit_chat(sid, room, "hub", "Choose A, B, or C. Example: !astro choice B")
                return
            sess = _astro_get_session(user, room)
            s = _astro_advance(sess.get("scene_id","astro_001") or "astro_001", ch)
            st = sess.get("state") or {}
            st["last_choice"] = ch
            _astro_set_session(user, room, s["scene_id"], st)
            _emit_chat(sid, room, "ghost-bot", s["title"])
            _emit_chat(sid, room, "ghost-bot", s["text"])
            for c in s["choices"]:
                _emit_chat(sid, room, "ghost-bot", f"{c['id']} — {c['label']}")
            _emit_chat(sid, room, "ghost-bot", s.get("hint",""))
            return

        if sub == "say":
            txt = (rest or "").strip()
            if not txt:
                _emit_chat(sid, room, "hub", "Usage: !astro say <text>")
                return
            sess = _astro_get_session(user, room)
            st = sess.get("state") or {}
            notes = st.get("notes") or []
            notes.append({"ts": utc_ts(), "text": txt})
            st["notes"] = notes[-25:]
            _astro_set_session(user, room, sess.get("scene_id","") or "astro_001", st)
            _emit_chat(room, room, user, f"[astro] {txt}")
            _emit_chat(sid, room, "hub", "Saved to your astro thread for this world. Continue with !astro choice A/B/C or reset with !astro start.")
            return

        _emit_chat(sid, room, "hub", "Unknown astro command. Try: !astro help")
        return

    # /worlds (aka nodes): list active rooms with counts
    if msg in ("/worlds", "/nodes", "!worlds", "!nodes"):
        counts = _room_counts()
        counts.setdefault(MAIN_ROOM, counts.get(MAIN_ROOM, 0))
        lines = []
        for r, c in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
            st = _world_state_by_room[r]
            homes = (st.get("homes") or {})
            homes_count = sum(len(v) for v in homes.values()) if isinstance(homes, dict) else 0
            lines.append(f"{r} ({c} online, {homes_count} homes)")
        _emit_chat(sid, room, "hub", "World nodes: " + (" | ".join(lines) if lines else "—"))
        return

    # /msg @name text  -> whisper to a user in any shared room
    if msg.startswith("/msg "):
        rest = msg[5:].strip()
        if rest.startswith("@"):
            parts = rest.split(" ", 1)
            target = parts[0].lstrip("@").strip().lower()
            text = parts[1] if len(parts) > 1 else ""
            if not text.strip():
                _emit_chat(sid, room, "hub", "Usage: /msg @name hello there")
                return
            target_sid = None
            with _presence_lock:
                my_rooms = set((_online.get(sid) or {}).get("rooms") or [room])
                for sid2, u in _online.items():
                    if u.get("name", "").strip().lower() == target:
                        rooms2 = set(u.get("rooms") or [u.get("room", MAIN_ROOM)])
                        if my_rooms.intersection(rooms2):
                            target_sid = sid2
                            break
            if not target_sid:
                _emit_chat(sid, room, "hub", f"Could not find @{target} in your worlds.")
                return
            emit("whisper", {"from": user, "to": target, "msg": text, "ts": utc_ts()}, to=target_sid)
            emit("whisper", {"from": user, "to": target, "msg": text, "ts": utc_ts()}, to=sid)
            return

    payload = {"room": room, "sender": user, "msg": msg, "ts": utc_ts()}
    _room_history[room].append(payload)
    _log_room_message(room, user, msg, payload.get("ts", utc_ts()))
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
