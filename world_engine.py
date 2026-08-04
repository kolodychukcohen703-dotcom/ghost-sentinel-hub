"""Interactive Sentinel city/world engine add-on.

The engine is intentionally server-light: the browser renders and simulates the
tile world while this module persists maps and player presence in the same
SQLite database used by Ghost Sentinel Hub.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import random
import re
import sqlite3
from threading import Lock

from flask import jsonify, render_template, request


ENGINE_LOCK = Lock()
DEFAULT_STATE = {
    "version": 1,
    "worldName": "Sentinel Sanctuary City",
    "width": 32,
    "height": 24,
    "speed": 1,
    "month": 1,
    "year": 1,
    "funds": 50000,
    "population": 0,
    "tiles": {},
    "buildings": {},
    "avatar": {"x": 15, "y": 11, "inside": None},
}

SHOWCASE_WORLDS = [
    ("aurora-harbor", "Aurora Harbor", "aurora", "Modern glass"),
    ("emerald-vale", "Emerald Vale", "emerald", "Forest cabin"),
    ("sunfire-metropolis", "Sunfire Metropolis", "sunfire", "Modern glass"),
    ("moonstone-citadel", "Moonstone Citadel", "moonstone", "Stone fortress"),
    ("sapphire-coast", "Sapphire Coast", "sapphire", "Cozy cottage"),
    ("rosewood-gardens", "Rosewood Gardens", "rosewood", "Victorian manor"),
    ("crystal-heights", "Crystal Heights", "crystal", "Fantasy tower"),
    ("golden-prairie", "Golden Prairie", "golden", "Cozy cottage"),
    ("starlight-basin", "Starlight Basin", "starlight", "Fantasy tower"),
    ("frostpine-capital", "Frostpine Capital", "frostpine", "Forest cabin"),
]


def _showcase_state(index: int, name: str, theme: str, home_style: str) -> dict:
    """Build one deterministic, developed 64x48 showcase city."""
    rng = random.Random(9173 + index * 7919)
    width, height = 64, 48
    tiles, buildings, interiors = {}, {}, {}
    # Curving river/coast unique to each world.
    for y in range(height):
        water_x = 5 + int(3 * __import__("math").sin((y + index * 2) / 6))
        for x in range(max(0, water_x - 2), min(width, water_x + 2)):
            tiles[f"{x},{y}"] = "water"
    # Full city road lattice with waterfront avenues.
    for x in range(10, width - 3):
        if x % 8 == 0 or x in (12, 32, 52):
            for y in range(3, height - 3):
                tiles[f"{x},{y}"] = "road"
    for y in range(4, height - 3):
        if y % 7 == 0 or y in (10, 24, 38):
            for x in range(9, width - 3):
                tiles[f"{x},{y}"] = "road"
    # Parks, plazas, civic waterfront, dense developed blocks.
    for x in range(9, width - 3):
        for y in range(3, height - 3):
            key = f"{x},{y}"
            if key in tiles:
                continue
            if (18 <= x <= 22 and 17 <= y <= 21) or (43 <= x <= 47 and 28 <= y <= 32):
                tiles[key] = "park"
                continue
            district = "commercial" if 25 <= x <= 42 and 15 <= y <= 34 else (
                "industrial" if x > 48 and y > 29 else "residential"
            )
            tiles[key] = district
            if rng.random() < (0.83 if district == "commercial" else 0.70):
                level = rng.randint(5, 12) if district == "commercial" else rng.randint(1, 5)
                buildings[key] = {
                    "zone": district, "level": level, "style": home_style,
                    "name": f"{name} {district.title()} {x}-{y}", "door": True,
                }
    # Landmark and four decorated showcase homes.
    landmarks = [(32, 24, "Celestial Tower"), (20, 11, "Grand Station"), (45, 18, "World Gallery")]
    for x, y, title in landmarks:
        key = f"{x},{y}"
        tiles[key] = "commercial"
        buildings[key] = {"zone": "commercial", "level": 15 - landmarks.index((x, y, title)), "style": theme, "name": title, "landmark": True, "door": True}
    for n, (x, y) in enumerate(((13, 6), (23, 37), (42, 8), (55, 22)), 1):
        key = f"{x},{y}"
        tiles[key] = "home"
        hname = f"{name} Showcase Home {n}"
        buildings[key] = {"zone": "home", "level": 2 + n % 2, "floors": 2 + n % 2, "style": home_style, "name": hname, "door": True}
        interiors[key] = {"name": hname, "roomType": "Bedroom", "items": [
            {"type": "bed", "x": 3, "y": 3}, {"type": "headboard", "x": 3, "y": 2},
            {"type": "endtable", "x": 2, "y": 3}, {"type": "endtable", "x": 6, "y": 3},
            {"type": "dresser", "x": 12, "y": 3}, {"type": "rug", "x": 7, "y": 7},
            {"type": "sofa", "x": 10, "y": 8}, {"type": "plant", "x": 17, "y": 2},
            {"type": "toilet", "x": 15, "y": 8}, {"type": "sink", "x": 17, "y": 8},
            {"type": "tub", "x": 15, "y": 10},
        ]}
    kinds = ["person", "person", "person", "person", "person", "deer", "rabbit", "fox", "dog", "bird"]
    entities = [{"kind": kinds[i % len(kinds)], "x": rng.uniform(10, 60), "y": rng.uniform(4, 44), "dx": rng.uniform(-.3, .3), "dy": rng.uniform(-.3, .3)} for i in range(70)]
    return {
        "version": 4, "showcase": True, "theme": theme, "worldName": name,
        "width": width, "height": height, "camera": {"x": 32, "y": 24},
        "speed": 1, "month": 1 + index % 12, "year": 12 + index * 3,
        "funds": 2_500_000 + index * 175_000, "population": 185_000 + index * 41_000,
        "tiles": tiles, "buildings": buildings, "interiors": interiors,
        "entities": entities, "avatar": {"x": 32, "y": 24, "inside": None, "facing": index % 4},
    }


def _seed_showcase_worlds(db_path: str) -> int:
    created = 0
    now = _iso()
    with ENGINE_LOCK, _connect(db_path) as conn:
        for index, (world_id, name, theme, home_style) in enumerate(SHOWCASE_WORLDS):
            exists = conn.execute("SELECT 1 FROM engine_worlds WHERE world_id=?", (world_id,)).fetchone()
            if exists:
                continue
            state = _showcase_state(index, name, theme, home_style)
            conn.execute("INSERT INTO engine_worlds(world_id,state_json,updated_at) VALUES(?,?,?)", (world_id, json.dumps(state, separators=(",", ":")), now))
            created += 1
    return created


def _slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return value[:55] or "new-world"


def _wizard_world(config: dict) -> tuple[str, dict]:
    name = str(config.get("name") or "New Sentinel World").strip()[:80]
    themes = {x[2] for x in SHOWCASE_WORLDS}
    theme = str(config.get("theme") or "aurora").lower()
    if theme not in themes:
        theme = "aurora"
    terrain = str(config.get("terrain") or "river").lower()
    if terrain not in {"river", "coast", "lakes", "islands", "mountains"}:
        terrain = "river"
    development = str(config.get("development") or "developed").lower()
    if development not in {"starter", "developed", "metropolis"}:
        development = "developed"
    styles = {"Cozy cottage", "Modern glass", "Victorian manor", "Forest cabin", "Stone fortress", "Fantasy tower"}
    home_style = str(config.get("home_style") or "Cozy cottage")
    if home_style not in styles:
        home_style = "Cozy cottage"
    size_map = {"large": (64, 48), "huge": (80, 64), "realm": (96, 80)}
    width, height = size_map.get(str(config.get("size") or "large"), (64, 48))
    try:
        population = max(0, min(10_000_000, int(config.get("population", 125000))))
        speed = max(1, min(8, int(config.get("speed", 1))))
    except (TypeError, ValueError):
        population, speed = 125000, 1
    seed_index = sum(ord(c) for c in name) % 97
    state = _showcase_state(seed_index, name, theme, home_style)
    state.update({"showcase": False, "wizard_created": True, "terrain": terrain, "development": development, "width": width, "height": height, "population": population, "speed": speed, "camera": {"x": 32, "y": 24}})
    tiles, buildings = state["tiles"], state["buildings"]
    # Repaint the requested water/terrain without damaging roads and landmarks.
    if terrain != "river":
        for key in [key for key, value in tiles.items() if value == "water"]:
            tiles[key] = "grass"
    if terrain == "coast":
        for x in range(0, 11):
            for y in range(height):
                tiles[f"{x},{y}"] = "water"; buildings.pop(f"{x},{y}", None)
    elif terrain == "lakes":
        for cx, cy, radius in ((16, 15, 5), (48, 34, 7), (28, 39, 4)):
            for x in range(max(0, cx-radius), min(width, cx+radius+1)):
                for y in range(max(0, cy-radius), min(height, cy+radius+1)):
                    if (x-cx)**2 + (y-cy)**2 <= radius**2:
                        tiles[f"{x},{y}"] = "water"; buildings.pop(f"{x},{y}", None)
    elif terrain == "islands":
        for x in range(width):
            for y in range(height):
                if x < 5 or y < 4 or x >= width-5 or y >= height-4 or (x % 21 in (0, 1)):
                    tiles[f"{x},{y}"] = "water"; buildings.pop(f"{x},{y}", None)
    elif terrain == "mountains":
        for x in range(8, width, 13):
            for y in range(5, height, 11):
                for dx, dy in ((0,0),(1,0),(0,1),(1,1),(2,1)):
                    key = f"{x+dx},{y+dy}"; tiles[key] = "mountain"; buildings.pop(key, None)
    if development == "starter":
        rng = random.Random(seed_index)
        for key in list(buildings):
            if not buildings[key].get("landmark") and rng.random() < .72:
                buildings.pop(key, None)
        state["population"] = min(population, 25000)
    elif development == "metropolis":
        for b in buildings.values():
            if b.get("zone") == "commercial":
                b["level"] = min(20, int(b.get("level", 1)) + 5)
    state["entities"] = state["entities"][:35 if development == "starter" else 70]
    world_id = _slugify(name)
    return world_id, state


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat()


def _clean_id(value: str, fallback: str = "lobby") -> str:
    value = "".join(c for c in (value or "") if c.isalnum() or c in "-_#")[:80]
    return value or fallback


def _connect(db_path: str):
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_engine(app, db_path: str):
    """Register routes and initialize engine tables on an existing Flask app."""
    with ENGINE_LOCK, _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS engine_worlds (
                world_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS engine_players (
                world_id TEXT NOT NULL,
                player_key TEXT NOT NULL,
                display_name TEXT NOT NULL,
                x INTEGER NOT NULL DEFAULT 0,
                y INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                last_seen TEXT NOT NULL,
                archived_at TEXT,
                PRIMARY KEY (world_id, player_key)
            );
            CREATE TABLE IF NOT EXISTS engine_sweep_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                swept_at TEXT NOT NULL,
                world_id TEXT,
                archived_count INTEGER NOT NULL,
                purged_count INTEGER NOT NULL
            );
            """
        )
    _seed_showcase_worlds(db_path)

    @app.get("/world-engine")
    def world_engine_page():
        return render_template("world_engine.html")

    @app.get("/api/world-engine/<world_id>")
    def engine_load(world_id):
        wid = _clean_id(world_id)
        with ENGINE_LOCK, _connect(db_path) as conn:
            row = conn.execute(
                "SELECT state_json, updated_at FROM engine_worlds WHERE world_id=?", (wid,)
            ).fetchone()
        if not row:
            return jsonify({"ok": True, "world_id": wid, "state": DEFAULT_STATE, "new": True})
        try:
            state = json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError):
            state = DEFAULT_STATE
        return jsonify({"ok": True, "world_id": wid, "state": state, "updated_at": row["updated_at"]})

    @app.get("/api/world-engine-worlds")
    def engine_world_list():
        worlds = []
        with ENGINE_LOCK, _connect(db_path) as conn:
            rows = conn.execute("SELECT world_id,state_json,updated_at FROM engine_worlds ORDER BY updated_at DESC").fetchall()
        for row in rows:
            try:
                st = json.loads(row["state_json"])
            except (TypeError, json.JSONDecodeError):
                st = {}
            worlds.append({"world_id": row["world_id"], "name": st.get("worldName", row["world_id"]), "theme": st.get("theme", "custom"), "population": st.get("population", 0), "showcase": bool(st.get("showcase")), "updated_at": row["updated_at"]})
        return jsonify({"ok": True, "worlds": worlds})

    @app.post("/api/world-engine-showcases")
    def engine_generate_showcases():
        return jsonify({"ok": True, "created": _seed_showcase_worlds(db_path), "total": len(SHOWCASE_WORLDS)})

    @app.post("/api/world-engine-create")
    def engine_create_world():
        try:
            config = request.get_json(silent=True) or {}
            world_id, state = _wizard_world(config)
            now = _iso()
            with ENGINE_LOCK, _connect(db_path) as conn:
                base, suffix = world_id, 2
                while conn.execute("SELECT 1 FROM engine_worlds WHERE world_id=?", (world_id,)).fetchone():
                    world_id = f"{base}-{suffix}"
                    suffix += 1
                conn.execute("INSERT INTO engine_worlds(world_id,state_json,updated_at) VALUES(?,?,?)", (world_id, json.dumps(state, separators=(",", ":")), now))
            return jsonify({"ok": True, "world_id": world_id, "name": state["worldName"], "state": state}), 201
        except Exception as exc:
            return jsonify({"ok": False, "error": f"World forge failed: {type(exc).__name__}"}), 500

    @app.post("/api/world-engine/<world_id>/save")
    def engine_save(world_id):
        wid = _clean_id(world_id)
        body = request.get_json(silent=True) or {}
        state = body.get("state")
        if not isinstance(state, dict):
            return jsonify({"ok": False, "error": "state object required"}), 400
        encoded = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
        if len(encoded) > 2_000_000:
            return jsonify({"ok": False, "error": "world state too large"}), 413
        now = _iso()
        with ENGINE_LOCK, _connect(db_path) as conn:
            conn.execute(
                """INSERT INTO engine_worlds(world_id,state_json,updated_at) VALUES(?,?,?)
                ON CONFLICT(world_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at""",
                (wid, encoded, now),
            )
        return jsonify({"ok": True, "saved_at": now})

    @app.post("/api/world-engine/<world_id>/heartbeat")
    def engine_heartbeat(world_id):
        wid = _clean_id(world_id)
        body = request.get_json(silent=True) or {}
        key = _clean_id(str(body.get("player_key") or "guest"), "guest")
        name = str(body.get("display_name") or "guest").strip()[:60] or "guest"
        try:
            x, y = int(body.get("x", 0)), int(body.get("y", 0))
        except (TypeError, ValueError):
            x, y = 0, 0
        now = _iso()
        with ENGINE_LOCK, _connect(db_path) as conn:
            conn.execute(
                """INSERT INTO engine_players(world_id,player_key,display_name,x,y,status,last_seen,archived_at)
                VALUES(?,?,?,?,?,'active',?,NULL)
                ON CONFLICT(world_id,player_key) DO UPDATE SET
                  display_name=excluded.display_name,x=excluded.x,y=excluded.y,
                  status='active',last_seen=excluded.last_seen,archived_at=NULL""",
                (wid, key, name, x, y, now),
            )
            rows = conn.execute(
                "SELECT player_key,display_name,x,y,last_seen FROM engine_players WHERE world_id=? AND status='active'",
                (wid,),
            ).fetchall()
        return jsonify({"ok": True, "players": [dict(r) for r in rows]})

    @app.post("/api/world-engine/sweep")
    def engine_sweep():
        """Archive inactive players, then purge only long-archived records."""
        body = request.get_json(silent=True) or {}
        wid = _clean_id(str(body.get("world_id") or ""), "") if body.get("world_id") else None
        try:
            inactive_minutes = max(5, min(10080, int(body.get("inactive_minutes", 30))))
            purge_days = max(1, min(3650, int(body.get("purge_days", 30))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid sweep interval"}), 400
        inactive_before = _iso(_utc_now() - timedelta(minutes=inactive_minutes))
        purge_before = _iso(_utc_now() - timedelta(days=purge_days))
        scope, params = (" AND world_id=?", [wid]) if wid else ("", [])
        now = _iso()
        with ENGINE_LOCK, _connect(db_path) as conn:
            cur = conn.execute(
                f"UPDATE engine_players SET status='archived', archived_at=? WHERE status='active' AND last_seen<?{scope}",
                [now, inactive_before, *params],
            )
            archived = cur.rowcount
            cur = conn.execute(
                f"DELETE FROM engine_players WHERE status='archived' AND archived_at<?{scope}",
                [purge_before, *params],
            )
            purged = cur.rowcount
            conn.execute(
                "INSERT INTO engine_sweep_log(swept_at,world_id,archived_count,purged_count) VALUES(?,?,?,?)",
                (now, wid, archived, purged),
            )
        return jsonify({"ok": True, "archived": archived, "purged": purged, "swept_at": now})

    @app.get("/api/world-engine/<world_id>/players")
    def engine_players(world_id):
        wid = _clean_id(world_id)
        with ENGINE_LOCK, _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT player_key,display_name,x,y,status,last_seen FROM engine_players WHERE world_id=? ORDER BY status,last_seen DESC",
                (wid,),
            ).fetchall()
        return jsonify({"ok": True, "players": [dict(r) for r in rows]})
