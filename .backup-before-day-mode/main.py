"""MyTrack — self-hosted calorie & points food tracker (multi-user).

FastAPI + SQLite. Each user has their own diary, weigh-ins, water, budgets and
favourites. The food database (scanned products + custom foods) is shared by the
household. Barcode lookups and product search go through Open Food Facts and are
cached locally.
"""
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "mytrack.db"
STATIC = Path(__file__).parent / "static"
LEGACY_PIN = os.environ.get("APP_PIN", "").strip()          # single-user PIN from v1 (used for migration only)
ADMIN_NAME = os.environ.get("ADMIN_NAME", "Admin").strip() or "Admin"
ADMIN_PIN = os.environ.get("ADMIN_PIN", "").strip()
SECRET = os.environ.get("APP_SECRET") or hashlib.sha256(("mytrack:" + LEGACY_PIN + ADMIN_PIN).encode()).hexdigest()
OFF_BASE = os.environ.get("OFF_BASE", "https://world.openfoodfacts.org").rstrip("/")
OFF_UA = "MyTrack/2.0 (self-hosted personal food tracker)"
COOKIE = "mytrack_session"
COLOURS = ["#2E31B7", "#14B386", "#F2545B", "#FFB020", "#8E44D9", "#1E9BD7", "#E0559C", "#5A6B7B"]

DEFAULT_SETTINGS = {
    "calorie_target": 1600,
    "daily_points": 15,
    "weekly_points": 0,       # optional weekly bonus on top of the daily allowance
    "kcal_per_point": 20,     # 1 point per this many calories
    "carry_over": True,       # unused daily points roll into the next day (reset weekly)
    "start_date": None,       # YYYY-MM-DD: carry-over only counts from this day
    "celebrations": True,     # daily welcome, quotes and goal animations
    "game_seen": {},          # which celebrations have already been shown
    "notify": {"morning": {"on": True, "time": "08:00"}, "water": {"on": True, "time": "15:00"},
               "streak": {"on": True, "time": "19:00"}},
    "notify_sent": {},
    "theme": "auto",          # auto (seasonal) | classic | halloween | bonfire
    "week_start": 1,          # 0=Sun, 1=Mon
    "weight_unit": "st",      # st | kg
    "goal_weight_kg": None,
    "water_target": 8,
    "show_macros": True,
    "track_mode": "points",   # points | calories
}

# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  pin_hash TEXT NOT NULL,
  is_admin INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1,
  colour TEXT DEFAULT '#2E31B7',
  session_ver INTEGER DEFAULT 1,
  created REAL DEFAULT 0,
  last_seen REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_settings (user_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (user_id, key));
CREATE TABLE IF NOT EXISTS foods (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  barcode TEXT UNIQUE,
  name TEXT NOT NULL,
  brand TEXT DEFAULT '',
  serving_g REAL DEFAULT 100,
  serving_label TEXT DEFAULT '',
  kcal REAL DEFAULT 0, protein REAL DEFAULT 0, carbs REAL DEFAULT 0, fat REAL DEFAULT 0,
  satfat REAL DEFAULT 0, sugar REAL DEFAULT 0, fibre REAL DEFAULT 0, salt REAL DEFAULT 0,
  zero_points INTEGER DEFAULT 0,
  favourite INTEGER DEFAULT 0,
  source TEXT DEFAULT 'custom',
  image_url TEXT DEFAULT '',
  last_used REAL DEFAULT 0,
  use_count INTEGER DEFAULT 0,
  created REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_foods (
  user_id INTEGER, food_id INTEGER, favourite INTEGER DEFAULT 0, last_used REAL DEFAULT 0, use_count INTEGER DEFAULT 0,
  PRIMARY KEY (user_id, food_id)
);
CREATE TABLE IF NOT EXISTS entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  day TEXT NOT NULL,
  meal TEXT NOT NULL,
  food_id INTEGER,
  name TEXT NOT NULL,
  brand TEXT DEFAULT '',
  amount_g REAL DEFAULT 0,
  servings REAL DEFAULT 1,
  unit_label TEXT DEFAULT '',
  kcal REAL DEFAULT 0, protein REAL DEFAULT 0, carbs REAL DEFAULT 0, fat REAL DEFAULT 0,
  satfat REAL DEFAULT 0, sugar REAL DEFAULT 0,
  points INTEGER DEFAULT 0,
  created REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS weights (day TEXT PRIMARY KEY, kg REAL NOT NULL);
CREATE TABLE IF NOT EXISTS water (day TEXT PRIMARY KEY, glasses INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS user_weights (user_id INTEGER, day TEXT, kg REAL NOT NULL, PRIMARY KEY (user_id, day));
CREATE TABLE IF NOT EXISTS push_subs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, endpoint TEXT UNIQUE, p256dh TEXT, auth TEXT,
  ua TEXT DEFAULT '', created REAL DEFAULT 0, last_ok REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS withings (
  user_id INTEGER PRIMARY KEY, w_userid TEXT, access_token TEXT, refresh_token TEXT, expires_at REAL,
  scope TEXT, last_update INTEGER DEFAULT 0, last_sync REAL DEFAULT 0, last_error TEXT DEFAULT '', imported INTEGER DEFAULT 0, connected REAL
);
CREATE TABLE IF NOT EXISTS user_visits (user_id INTEGER, day TEXT, PRIMARY KEY (user_id, day));
CREATE TABLE IF NOT EXISTS user_badges (user_id INTEGER, badge TEXT, earned_at REAL, seen INTEGER DEFAULT 0, PRIMARY KEY (user_id, badge));
CREATE TABLE IF NOT EXISTS user_water (user_id INTEGER, day TEXT, glasses INTEGER DEFAULT 0, PRIMARY KEY (user_id, day));
"""


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=OFF")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def cols(c, table):
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}


# ---------------------------------------------------------------- PINs
def hash_pin(pin: str) -> str:
    salt = secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 120_000).hex()


def check_pin(pin: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 120_000).hex(), h)


def valid_pin(pin: str) -> bool:
    return pin.isdigit() and 4 <= len(pin) <= 8


def next_colour(c):
    used = [r["colour"] for r in c.execute("SELECT colour FROM users")]
    for col in COLOURS:
        if col not in used:
            return col
    return COLOURS[len(used) % len(COLOURS)]


def create_user(c, name, pin, is_admin=False):
    cur = c.execute("INSERT INTO users(name, pin_hash, is_admin, colour, created) VALUES (?,?,?,?,?)",
                    (name.strip(), hash_pin(pin), 1 if is_admin else 0, next_colour(c), time.time()))
    return cur.lastrowid


# ---------------------------------------------------------------- migration / bootstrap
def round_half_up(x: float) -> int:
    return int(x + 0.5) if x >= 0 else 0


def recompute_points(c, uid=None):
    """Re-score diary entries from their calories (sins = kcal / kcal_per_point).
    Only entries for foods marked zero-sin stay at 0."""
    users = [uid] if uid else [r[0] for r in c.execute("SELECT DISTINCT user_id FROM entries WHERE user_id IS NOT NULL")]
    for u in users:
        r = c.execute("SELECT value FROM user_settings WHERE user_id=? AND key='kcal_per_point'", (u,)).fetchone()
        k = max(1.0, float(json.loads(r[0])) if r else float(DEFAULT_SETTINGS["kcal_per_point"]))
        for e in c.execute("SELECT e.id, e.kcal, COALESCE(f.zero_points, 0) AS zero FROM entries e "
                           "LEFT JOIN foods f ON f.id=e.food_id WHERE e.user_id=?", (u,)).fetchall():
            c.execute("UPDATE entries SET points=? WHERE id=?",
                      (0 if e["zero"] else round_half_up((e["kcal"] or 0) / k), e["id"]))


def migrate():
    """Upgrade a v1 single-user database in place. Takes a backup first."""
    with db() as c:
        existed = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entries'").fetchone()
        needs_upgrade = existed and "user_id" not in cols(c, "entries")
    if needs_upgrade and DB_PATH.exists():
        shutil.copy2(DB_PATH, DATA_DIR / f"mytrack-before-multiuser-{int(time.time())}.db")
    with db() as c:
        c.executescript(SCHEMA)
        if "user_id" not in cols(c, "entries"):
            c.execute("ALTER TABLE entries ADD COLUMN user_id INTEGER")
        if "created_by" not in cols(c, "foods"):
            c.execute("ALTER TABLE foods ADD COLUMN created_by INTEGER")
        c.execute("CREATE INDEX IF NOT EXISTS idx_entries_user_day ON entries(user_id, day)")
        if "source" not in cols(c, "user_weights"):
            c.execute("ALTER TABLE user_weights ADD COLUMN source TEXT DEFAULT 'manual'")
            c.execute("ALTER TABLE user_weights ADD COLUMN fat_pct REAL")
            c.execute("ALTER TABLE user_weights ADD COLUMN measured_at REAL")

        # Bootstrap the admin account
        if not c.execute("SELECT 1 FROM users WHERE is_admin=1").fetchone():
            pin = ADMIN_PIN if valid_pin(ADMIN_PIN) else "".join(secrets.choice("0123456789") for _ in range(6))
            create_user(c, ADMIN_NAME, pin, is_admin=True)
            if not valid_pin(ADMIN_PIN):
                print(f"\n*** MyTrack: created admin '{ADMIN_NAME}' with PIN {pin} — set ADMIN_PIN to choose your own ***\n", flush=True)

        # Hand any v1 (pre-users) data to a user so nothing is lost
        orphan = (c.execute("SELECT COUNT(*) FROM entries WHERE user_id IS NULL").fetchone()[0]
                  + c.execute("SELECT COUNT(*) FROM weights").fetchone()[0]
                  + c.execute("SELECT COUNT(*) FROM water").fetchone()[0]
                  + c.execute("SELECT COUNT(*) FROM settings").fetchone()[0])
        if orphan and not c.execute("SELECT value FROM meta WHERE key='v1_migrated'").fetchone():
            old = {r["key"]: json.loads(r["value"]) for r in c.execute("SELECT key, value FROM settings")}
            name = (old.get("name") or "").strip() or "Me"
            pin = LEGACY_PIN if valid_pin(LEGACY_PIN) else "0000"
            uid = create_user(c, name, pin)
            c.execute("UPDATE entries SET user_id=? WHERE user_id IS NULL", (uid,))
            c.execute("INSERT OR IGNORE INTO user_weights(user_id, day, kg) SELECT ?, day, kg FROM weights", (uid,))
            c.execute("INSERT OR IGNORE INTO user_water(user_id, day, glasses) SELECT ?, day, glasses FROM water", (uid,))
            for k, v in old.items():
                if k in DEFAULT_SETTINGS:
                    c.execute("INSERT OR REPLACE INTO user_settings VALUES (?,?,?)", (uid, k, json.dumps(v)))
            c.execute("INSERT OR IGNORE INTO user_foods(user_id, food_id, favourite, last_used, use_count) "
                      "SELECT ?, id, favourite, last_used, use_count FROM foods WHERE favourite=1 OR last_used>0", (uid,))
            c.execute("DELETE FROM weights"); c.execute("DELETE FROM water"); c.execute("DELETE FROM settings")
            c.execute("INSERT OR REPLACE INTO meta VALUES ('v1_migrated', ?)", (json.dumps({"user_id": uid, "at": time.time()}),))
            print(f"*** MyTrack: moved existing diary to user '{name}' (id {uid}) ***", flush=True)
        # v3: points = calories / kcal_per_point, 15/day with carry-over
        if not c.execute("SELECT 1 FROM meta WHERE key='points_v4'").fetchone():
            c.execute("UPDATE user_settings SET value='15' WHERE key='daily_points' AND value='23'")
            c.execute("UPDATE user_settings SET value='0' WHERE key='weekly_points' AND value='28'")
            recompute_points(c)
            c.execute("INSERT OR REPLACE INTO meta VALUES ('points_v4', ?)", (str(time.time()),))
        c.execute("INSERT OR REPLACE INTO meta VALUES ('schema', '3')")


migrate()


def get_settings(uid: int):
    with db() as c:
        stored = {r["key"]: json.loads(r["value"]) for r in
                  c.execute("SELECT key, value FROM user_settings WHERE user_id=?", (uid,))}
        u = c.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    return {**DEFAULT_SETTINGS, **stored, "name": u["name"] if u else ""}


# ---------------------------------------------------------------- auth
def sign(value: str) -> str:
    return value + "." + hmac.new(SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()


def read_session(token: Optional[str]):
    if not token or token.count(".") != 1:
        return None
    value, _ = token.rsplit(".", 1)
    if not hmac.compare_digest(sign(value), token):
        return None
    try:
        uid, ver, _ts = value.split(":")
        uid, ver = int(uid), int(ver)
    except ValueError:
        return None
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        if not u or u["session_ver"] != ver:
            return None
        if time.time() - (u["last_seen"] or 0) > 60:
            c.execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), uid))
        return dict(u)


def public_user(u):
    return {"id": u["id"], "name": u["name"], "is_admin": bool(u["is_admin"]), "colour": u["colour"]}


app = FastAPI(title="MyTrack", docs_url=None, redoc_url=None)
OPEN_PATHS = {"/api/login", "/api/auth", "/api/logout", "/api/withings/callback"}


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    p = request.url.path
    if p.startswith("/api/") and p not in OPEN_PATHS:
        user = read_session(request.cookies.get(COOKIE))
        if not user:
            return JSONResponse({"detail": "auth"}, status_code=401)
        if p.startswith("/api/admin/") and not user["is_admin"]:
            return JSONResponse({"detail": "Admins only"}, status_code=403)
        request.state.user = user
    return await call_next(request)


def uid_of(request: Request) -> int:
    return request.state.user["id"]


@app.get("/api/auth")
def auth_status(request: Request):
    u = read_session(request.cookies.get(COOKIE))
    with db() as c:
        users = [{"id": r["id"], "name": r["name"], "colour": r["colour"], "is_admin": bool(r["is_admin"])}
                 for r in c.execute("SELECT * FROM users WHERE active=1 ORDER BY is_admin, name")]
    return {"ok": bool(u), "user": public_user(u) if u else None, "users": users}


class Login(BaseModel):
    user_id: int
    pin: str


_fails: dict = {}


@app.post("/api/login")
def login(body: Login, response: Response):
    n, until = _fails.get(body.user_id, (0, 0))
    if until > time.time():
        raise HTTPException(429, f"Too many tries — wait {int(until - time.time()) + 1}s")
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE id=? AND active=1", (body.user_id,)).fetchone()
    if not u or not check_pin(body.pin.strip(), u["pin_hash"]):
        n += 1
        _fails[body.user_id] = (n, time.time() + 60 if n >= 5 else 0)
        time.sleep(0.6)
        raise HTTPException(401, "Wrong PIN")
    _fails.pop(body.user_id, None)
    response.set_cookie(COOKIE, sign(f"{u['id']}:{u['session_ver']}:{int(time.time())}"),
                        max_age=60 * 60 * 24 * 400, httponly=True, samesite="lax")
    return {"ok": True, "user": public_user(u)}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    return public_user(request.state.user)


# ---------------------------------------------------------------- settings
@app.get("/api/settings")
def read_settings(request: Request):
    return get_settings(uid_of(request))


@app.put("/api/settings")
def write_settings(request: Request, body: dict):
    uid = uid_of(request)
    before = get_settings(uid)["kcal_per_point"]
    with db() as c:
        for k, v in body.items():
            if k in DEFAULT_SETTINGS:
                c.execute("INSERT OR REPLACE INTO user_settings VALUES (?,?,?)", (uid, k, json.dumps(v)))
        if "kcal_per_point" in body and float(body["kcal_per_point"] or 0) != float(before):
            recompute_points(c, uid)
        if isinstance(body.get("name"), str) and body["name"].strip():
            c.execute("UPDATE users SET name=? WHERE id=?", (body["name"].strip()[:40], uid))
    return get_settings(uid)


class PinChange(BaseModel):
    current: str
    new: str


@app.post("/api/me/pin")
def change_own_pin(request: Request, b: PinChange, response: Response):
    u = request.state.user
    if not check_pin(b.current.strip(), u["pin_hash"]):
        raise HTTPException(400, "Current PIN is wrong")
    if not valid_pin(b.new.strip()):
        raise HTTPException(400, "PIN must be 4–8 digits")
    with db() as c:
        c.execute("UPDATE users SET pin_hash=?, session_ver=session_ver+1 WHERE id=?", (hash_pin(b.new.strip()), u["id"]))
        ver = c.execute("SELECT session_ver FROM users WHERE id=?", (u["id"],)).fetchone()[0]
    response.set_cookie(COOKIE, sign(f"{u['id']}:{ver}:{int(time.time())}"), max_age=60 * 60 * 24 * 400,
                        httponly=True, samesite="lax")
    return {"ok": True}


# ---------------------------------------------------------------- diary
class Entry(BaseModel):
    day: str
    meal: str
    food_id: Optional[int] = None
    name: str
    brand: str = ""
    amount_g: float = 0
    servings: float = 1
    unit_label: str = ""
    kcal: float = 0
    protein: float = 0
    carbs: float = 0
    fat: float = 0
    satfat: float = 0
    sugar: float = 0
    points: int = 0


ENTRY_FIELDS = list(Entry.model_fields.keys())


def touch_food(c, uid, fid):
    c.execute("INSERT INTO user_foods(user_id, food_id, last_used, use_count) VALUES (?,?,?,1) "
              "ON CONFLICT(user_id, food_id) DO UPDATE SET last_used=excluded.last_used, use_count=use_count+1",
              (uid, fid, time.time()))


def insert_entry(c, uid, data: dict):
    c.execute(f"INSERT INTO entries({','.join(ENTRY_FIELDS)}, user_id, created) "
              f"VALUES ({','.join('?' * len(ENTRY_FIELDS))}, ?, ?)",
              [data[f] for f in ENTRY_FIELDS] + [uid, time.time()])


@app.get("/api/day/{day}")
def get_day(request: Request, day: str):
    uid = uid_of(request)
    with db() as c:
        entries = rows(c.execute("SELECT * FROM entries WHERE user_id=? AND day=? ORDER BY created", (uid, day)))
        w = c.execute("SELECT glasses FROM user_water WHERE user_id=? AND day=?", (uid, day)).fetchone()
        wt = c.execute("SELECT kg FROM user_weights WHERE user_id=? AND day=?", (uid, day)).fetchone()
    return {"day": day, "entries": entries, "water": w["glasses"] if w else 0, "weight_kg": wt["kg"] if wt else None}


@app.get("/api/week/{day}")
def get_week(request: Request, day: str):
    uid = uid_of(request)
    s = get_settings(uid)
    d = date.fromisoformat(day)
    offset = (d.weekday() + 1) % 7 if s["week_start"] == 0 else d.weekday()
    start = d - timedelta(days=offset)
    days = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    with db() as c:
        totals = {r["day"]: dict(r) for r in c.execute(
            "SELECT day, SUM(kcal) kcal, SUM(points) points, SUM(protein) protein, SUM(carbs) carbs, SUM(fat) fat "
            "FROM entries WHERE user_id=? AND day BETWEEN ? AND ? GROUP BY day", (uid, days[0], days[-1]))}
    out = []
    for dd in days:
        t = totals.get(dd, {})
        out.append({"day": dd, "kcal": t.get("kcal") or 0, "points": t.get("points") or 0,
                    "protein": t.get("protein") or 0, "carbs": t.get("carbs") or 0, "fat": t.get("fat") or 0})
    return {"start": days[0], "days": out}


@app.get("/api/history")
def history(request: Request, days: int = 30):
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    with db() as c:
        return rows(c.execute("SELECT day, SUM(kcal) kcal, SUM(points) points FROM entries "
                              "WHERE user_id=? AND day>=? GROUP BY day ORDER BY day", (uid_of(request), start)))


@app.post("/api/entries")
def add_entry(request: Request, e: Entry):
    uid = uid_of(request)
    with db() as c:
        insert_entry(c, uid, e.model_dump())
        if e.food_id:
            touch_food(c, uid, e.food_id)
        return {"id": c.execute("SELECT last_insert_rowid()").fetchone()[0]}


@app.put("/api/entries/{eid}")
def update_entry(request: Request, eid: int, e: Entry):
    with db() as c:
        c.execute(f"UPDATE entries SET {','.join(f + '=?' for f in ENTRY_FIELDS)} WHERE id=? AND user_id=?",
                  [getattr(e, f) for f in ENTRY_FIELDS] + [eid, uid_of(request)])
    return {"ok": True}


@app.delete("/api/entries/{eid}")
def delete_entry(request: Request, eid: int):
    with db() as c:
        c.execute("DELETE FROM entries WHERE id=? AND user_id=?", (eid, uid_of(request)))
    return {"ok": True}


class CopyMeal(BaseModel):
    from_day: str
    to_day: str
    meal: str


@app.post("/api/copy-meal")
def copy_meal(request: Request, b: CopyMeal):
    uid = uid_of(request)
    with db() as c:
        src = rows(c.execute("SELECT * FROM entries WHERE user_id=? AND day=? AND meal=?", (uid, b.from_day, b.meal)))
        for r in src:
            r["day"] = b.to_day
            insert_entry(c, uid, r)
    return {"copied": len(src)}


# ---------------------------------------------------------------- water & weight
class Water(BaseModel):
    glasses: int


@app.put("/api/water/{day}")
def set_water(request: Request, day: str, w: Water):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO user_water(user_id, day, glasses) VALUES (?,?,?)",
                  (uid_of(request), day, max(0, w.glasses)))
    return {"ok": True}


class Weight(BaseModel):
    day: str
    kg: float


@app.get("/api/weights")
def list_weights(request: Request):
    with db() as c:
        return rows(c.execute("SELECT day, kg, source, fat_pct FROM user_weights WHERE user_id=? ORDER BY day", (uid_of(request),)))


@app.post("/api/weights")
def add_weight(request: Request, w: Weight):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO user_weights(user_id, day, kg, source) VALUES (?,?,?,'manual')", (uid_of(request), w.day, w.kg))
    return {"ok": True}


@app.delete("/api/weights/{day}")
def del_weight(request: Request, day: str):
    with db() as c:
        c.execute("DELETE FROM user_weights WHERE user_id=? AND day=?", (uid_of(request), day))
    return {"ok": True}


# ---------------------------------------------------------------- foods (shared catalogue, per-user favourites)
class Food(BaseModel):
    barcode: Optional[str] = None
    name: str
    brand: str = ""
    serving_g: float = 100
    serving_label: str = ""
    kcal: float = 0
    protein: float = 0
    carbs: float = 0
    fat: float = 0
    satfat: float = 0
    sugar: float = 0
    fibre: float = 0
    salt: float = 0
    zero_points: bool = False
    source: str = "custom"
    image_url: str = ""


FOOD_FIELDS = list(Food.model_fields.keys())
FOOD_SELECT = ("SELECT f.*, COALESCE(uf.favourite,0) AS favourite, COALESCE(uf.last_used,0) AS last_used, "
               "COALESCE(uf.use_count,0) AS use_count FROM foods f "
               "LEFT JOIN user_foods uf ON uf.food_id=f.id AND uf.user_id=?")


def food_for(c, fid, uid):
    r = c.execute(FOOD_SELECT + " WHERE f.id=?", (uid, fid)).fetchone()
    return dict(r) if r else None


def upsert_food(c, f: dict, uid=None) -> int:
    """Insert or refresh a food; returns its id. Never overwrites a household-edited (custom) product."""
    existing = None
    if f.get("barcode"):
        existing = c.execute("SELECT * FROM foods WHERE barcode=?", (f["barcode"],)).fetchone()
    vals = {k: f.get(k) for k in FOOD_FIELDS if k in f}
    if existing:
        if existing["source"] == "custom":
            return existing["id"]
        vals.pop("zero_points", None)   # keep household's zero-point choice
        c.execute(f"UPDATE foods SET {','.join(k + '=?' for k in vals)} WHERE id=?", [*vals.values(), existing["id"]])
        return existing["id"]
    vals["created"] = time.time()
    vals["created_by"] = uid
    cur = c.execute(f"INSERT INTO foods({','.join(vals)}) VALUES ({','.join('?' * len(vals))})", list(vals.values()))
    return cur.lastrowid


@app.get("/api/foods")
def list_foods(request: Request, kind: str = "recent", q: str = ""):
    uid = uid_of(request)
    with db() as c:
        if q:
            like = f"%{q.strip()}%"
            return rows(c.execute(FOOD_SELECT + " WHERE f.name LIKE ? OR f.brand LIKE ? OR f.barcode=? "
                                  "ORDER BY use_count DESC, f.name LIMIT 40", (uid, like, like, q.strip())))
        if kind == "favourites":
            return rows(c.execute(FOOD_SELECT + " WHERE uf.favourite=1 ORDER BY f.name", (uid,)))
        if kind == "mine":
            return rows(c.execute(FOOD_SELECT + " WHERE f.source='custom' AND (f.created_by=? OR f.created_by IS NULL) "
                                  "ORDER BY f.name", (uid, uid)))
        return rows(c.execute(FOOD_SELECT + " WHERE uf.last_used>0 ORDER BY uf.last_used DESC LIMIT 40", (uid,)))


@app.get("/api/foods/{fid}")
def get_food(request: Request, fid: int):
    with db() as c:
        f = food_for(c, fid, uid_of(request))
    if not f:
        raise HTTPException(404, "not found")
    return f


@app.post("/api/foods")
def create_food(request: Request, f: Food):
    uid = uid_of(request)
    d = f.model_dump()
    d["barcode"] = d.get("barcode") or None
    with db() as c:
        ex = c.execute("SELECT id FROM foods WHERE barcode=?", (d["barcode"],)).fetchone() if d["barcode"] else None
        if ex:   # correcting a scanned product -> overwrite and mark as a household (custom) food
            c.execute(f"UPDATE foods SET {','.join(k + '=?' for k in FOOD_FIELDS)} WHERE id=?",
                      [d[k] for k in FOOD_FIELDS] + [ex["id"]])
            fid = ex["id"]
        else:
            fid = upsert_food(c, d, uid)
        return food_for(c, fid, uid)


def apply_food_update(c, fid, uid, body: dict):
    if "favourite" in body:
        c.execute("INSERT INTO user_foods(user_id, food_id, favourite) VALUES (?,?,?) "
                  "ON CONFLICT(user_id, food_id) DO UPDATE SET favourite=excluded.favourite",
                  (uid, fid, 1 if body["favourite"] else 0))
    allowed = {k: v for k, v in body.items() if k in FOOD_FIELDS}
    if "barcode" in allowed and not allowed["barcode"]:
        allowed["barcode"] = None
    if allowed:
        try:
            c.execute(f"UPDATE foods SET {','.join(k + '=?' for k in allowed)} WHERE id=?", [*allowed.values(), fid])
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Another food already uses that barcode")


@app.put("/api/foods/{fid}")
def update_food(request: Request, fid: int, body: dict):
    uid = uid_of(request)
    with db() as c:
        apply_food_update(c, fid, uid, body)
        return food_for(c, fid, uid)


@app.delete("/api/foods/{fid}")
def delete_food(request: Request, fid: int):
    u = request.state.user
    with db() as c:
        f = c.execute("SELECT created_by FROM foods WHERE id=?", (fid,)).fetchone()
        if f and not u["is_admin"] and f["created_by"] not in (None, u["id"]):
            raise HTTPException(403, "Only the person who created this food (or an admin) can delete it")
        c.execute("DELETE FROM foods WHERE id=?", (fid,))
        c.execute("DELETE FROM user_foods WHERE food_id=?", (fid,))
    return {"ok": True}


# ---------------------------------------------------------------- Open Food Facts
OFF_FIELDS = "code,product_name,product_name_en,generic_name,brands,serving_size,serving_quantity,nutriments,image_front_small_url,image_small_url,categories_tags"
ZERO_HINTS = ("en:fresh-vegetables", "en:fresh-fruits", "en:vegetables", "en:fruits", "en:eggs",
              "en:waters", "en:spring-waters", "en:mineral-waters", "en:skimmed-milks-0-fat-greek-yogurts")


def _num(n: dict, *keys) -> float:
    for k in keys:
        v = n.get(k)
        if v not in (None, ""):
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                pass
    return 0.0


def normalise_off(p: dict) -> Optional[dict]:
    n = p.get("nutriments") or {}
    name = (p.get("product_name_en") or p.get("product_name") or p.get("generic_name") or "").strip()
    if not name:
        return None
    kcal = _num(n, "energy-kcal_100g", "energy-kcal_value")
    if not kcal and n.get("energy_100g"):
        kcal = round(_num(n, "energy_100g") / 4.184, 1)
    sq = p.get("serving_quantity")
    try:
        sq = float(sq) if sq not in (None, "") else 0
    except (TypeError, ValueError):
        sq = 0
    cats = p.get("categories_tags") or []
    is_drinkish = any("beverage" in t or "drink" in t for t in cats)
    is_processed = any(w in t for t in cats for w in ("dried", "juice", "smoothie", "crisp", "chip", "jam", "dessert"))
    zero = (any(t in ZERO_HINTS for t in cats) and kcal < 120 and not is_drinkish and not is_processed) or (
        any(t in ("en:waters", "en:spring-waters", "en:mineral-waters") for t in cats) and kcal < 5)
    return {
        "barcode": str(p.get("code") or "") or None,
        "name": name,
        "brand": (p.get("brands") or "").split(",")[0].strip(),
        "serving_g": sq or 100,
        "serving_label": (p.get("serving_size") or "").strip() if sq else "",
        "kcal": kcal,
        "protein": _num(n, "proteins_100g"),
        "carbs": _num(n, "carbohydrates_100g"),
        "fat": _num(n, "fat_100g"),
        "satfat": _num(n, "saturated-fat_100g"),
        "sugar": _num(n, "sugars_100g"),
        "fibre": _num(n, "fiber_100g"),
        "salt": _num(n, "salt_100g"),
        "zero_points": bool(zero),
        "source": "off",
        "image_url": p.get("image_front_small_url") or p.get("image_small_url") or "",
    }


async def off_get(path: str, params: dict) -> dict:
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent": OFF_UA}, follow_redirects=True) as cl:
        r = await cl.get(f"{OFF_BASE}{path}", params=params)
        r.raise_for_status()
        return r.json()


@app.get("/api/barcode/{code}")
async def barcode(request: Request, code: str, refresh: bool = False):
    uid = uid_of(request)
    code = "".join(ch for ch in code if ch.isdigit())
    if not code:
        raise HTTPException(400, "Invalid barcode")
    with db() as c:
        row = c.execute("SELECT id FROM foods WHERE barcode=?", (code,)).fetchone()
        if row and not refresh:
            return {"found": True, "cached": True, "food": food_for(c, row["id"], uid)}
    try:
        data = await off_get(f"/api/v2/product/{code}.json", {"fields": OFF_FIELDS})
    except Exception as ex:  # noqa: BLE001
        if row:
            with db() as c:
                return {"found": True, "cached": True, "food": food_for(c, row["id"], uid)}
        raise HTTPException(502, f"Couldn't reach Open Food Facts ({type(ex).__name__})")
    if data.get("status") != 1 or not data.get("product"):
        return {"found": False, "barcode": code}
    f = normalise_off({**data["product"], "code": code})
    if not f:
        return {"found": False, "barcode": code}
    with db() as c:
        fid = upsert_food(c, f, uid)
        return {"found": True, "cached": False, "food": food_for(c, fid, uid)}


@app.get("/api/search")
async def search(request: Request, q: str):
    q = q.strip()
    if len(q) < 2:
        return {"local": [], "online": []}
    local = list_foods(request, q=q)
    online, error = [], None
    try:
        data = await off_get("/cgi/search.pl", {"search_terms": q, "search_simple": 1, "action": "process",
                                                "json": 1, "page_size": 30, "fields": OFF_FIELDS,
                                                "sort_by": "unique_scans_n"})
        seen = {f["barcode"] for f in local if f.get("barcode")}
        for p in data.get("products", []):
            f = normalise_off(p)
            if f and f["kcal"] and f["barcode"] not in seen:
                online.append(f)
    except Exception as ex:  # noqa: BLE001
        error = f"Online search unavailable ({type(ex).__name__})"
    return {"local": local, "online": online, "error": error}


class SaveOff(BaseModel):
    food: dict


@app.post("/api/foods/save-online")
def save_online(request: Request, b: SaveOff):
    uid = uid_of(request)
    with db() as c:
        return food_for(c, upsert_food(c, b.food, uid), uid)



# ---------------------------------------------------------------- gamification
import math
import random

BADGES = [
    ("first_log",   "📝", "First bite",      "Logged your first food"),
    ("streak_3",    "🔥", "Warming up",      "3-day streak"),
    ("streak_7",    "🔥", "On fire",         "7-day streak"),
    ("streak_14",   "⚡", "Unstoppable",     "14-day streak"),
    ("streak_30",   "🏆", "Habit hero",      "30-day streak"),
    ("streak_100",  "💎", "Legend",          "100-day streak"),
    ("track_1",     "⭐", "On track",        "Your first on-track day"),
    ("track_7",     "🌟", "Star run",        "7 on-track days in a row"),
    ("week_goal",   "🥇", "Week winner",     "Hit your weekly goal"),
    ("month_goal",  "👑", "Month master",    "Hit your monthly goal"),
    ("water_7",     "💧", "Hydration hero",  "Hit your water target on 7 days"),
    ("scanner_10",  "📷", "Scan fan",        "Logged 10 different scanned products"),
    ("weigh_4",     "⚖️", "Check-in champ",  "4 weigh-ins logged"),
    ("goal_weight", "🎯", "Goal getter",     "Reached your goal weight"),
]
BADGE_INFO = {b[0]: {"id": b[0], "icon": b[1], "name": b[2], "desc": b[3]} for b in BADGES}
MILESTONES = (3, 7, 14, 21, 30, 50, 75, 100, 150, 200, 365)
QUOTES = [
    "Small steps still move you forward.", "Consistency beats perfection, every time.",
    "You don't have to be perfect — just keep showing up.", "Every log is a vote for the person you're becoming.",
    "Today counts. So do you.", "One good choice makes the next one easier.", "A wobble isn't a fall. Keep going.",
    "Your future self is cheering you on.", "Showing up is the hardest part — and you just did it.",
    "Habits are built one ordinary day at a time.", "Be proud of how far you've come.", "Fresh day, fresh start.",
    "Slow progress is still progress.", "Tiny wins add up to big changes.", "Be kind to yourself and honest with your log.",
    "It's not about one day. It's about most days.", "Water, log, repeat.", "Keep the momentum rolling.",
    "Believe in the version of you that keeps going.", "You've done hard things before. This is one more.",
    "Look at you, being consistent!", "Momentum is built, not found.", "Every day you track, you learn something new.",
    "Strong habits, steady progress.", "Your streak is proof you can do this.",
]


def _week_start(d: date, ws: int) -> date:
    return d - timedelta(days=((d.weekday() + 1) % 7) if ws == 0 else d.weekday())


def day_statuses(c, uid, s, start: date, end: date):
    """Per-day sins / on-track status, using the same allowance + carry-over rules as the app."""
    ws = int(s["week_start"]); a = _week_start(start, ws)
    daily, weekly = int(s["daily_points"] or 0), int(s["weekly_points"] or 0)
    carry_on, sd = s.get("carry_over", True) is not False, s.get("start_date")
    tot = {r["day"]: (r["p"] or 0, r["n"]) for r in c.execute(
        "SELECT day, SUM(points) p, COUNT(*) n FROM entries WHERE user_id=? AND day BETWEEN ? AND ? GROUP BY day",
        (uid, a.isoformat(), end.isoformat()))}
    wt = int(s.get("water_target") or 8)
    water = {r["day"]: r["glasses"] for r in c.execute(
        "SELECT day, glasses FROM user_water WHERE user_id=? AND day BETWEEN ? AND ?", (uid, a.isoformat(), end.isoformat()))}
    out, d, carry, bonus = {}, a, 0, weekly
    while d <= end:
        if d == _week_start(d, ws):
            carry, bonus = 0, weekly
        iso = d.isoformat(); used, n = tot.get(iso, (0, 0))
        counts = not sd or iso >= sd
        avail = daily + (carry if counts else 0); left = avail - used
        bu = min(max(0, -left), bonus) if counts else 0; bonus -= bu
        out[iso] = {"used": used, "n": n, "avail": avail, "on_track": bool(counts and n >= 2 and left + bu >= 0),
                    "water_hit": water.get(iso, 0) >= wt, "counts": counts}
        if counts:
            carry = max(0, left) if carry_on else 0
        d += timedelta(days=1)
    return out


def goal_for(days_counted: int, ratio: float) -> int:
    return max(1, math.ceil(days_counted * ratio)) if days_counted else 0


def game_state(c, uid, today: date):
    s = get_settings(uid)
    first = c.execute("SELECT MIN(day) FROM entries WHERE user_id=?", (uid,)).fetchone()[0]
    eff_start = s.get("start_date") or first or today.isoformat()
    window_start = today - timedelta(days=130)
    st = day_statuses(c, uid, s, window_start, today)

    # streaks: a day counts if the app was opened or food was logged
    active = {r[0] for r in c.execute("SELECT day FROM user_visits WHERE user_id=?", (uid,))}
    active |= {r[0] for r in c.execute("SELECT DISTINCT day FROM entries WHERE user_id=?", (uid,))}
    streak, d = 0, today
    while d.isoformat() in active:
        streak += 1; d -= timedelta(days=1)
    best, run, prev = 0, 0, None
    for day in sorted(active):
        dd = date.fromisoformat(day)
        run = run + 1 if prev and (dd - prev).days == 1 else 1
        best, prev = max(best, run), dd
    track_streak, d = 0, today if st.get(today.isoformat(), {}).get("on_track") else today - timedelta(days=1)
    while st.get(d.isoformat(), {}).get("on_track"):
        track_streak += 1; d -= timedelta(days=1)
    best_track, run = 0, 0
    for day in sorted(st):
        run = run + 1 if st[day]["on_track"] else 0
        best_track = max(best_track, run)

    ws = int(s["week_start"])

    def week_summary(wstart: date, upto: date):
        days = [(wstart + timedelta(days=i)).isoformat() for i in range(7)]
        counted = [x for x in days if x >= eff_start]
        on = sum(1 for x in days if x <= upto.isoformat() and st.get(x, {}).get("on_track"))
        return {"start": days[0], "on_track_days": on, "goal": goal_for(len(counted), 5 / 7),
                "days": [{"day": x, "on_track": bool(st.get(x, {}).get("on_track")), "logged": st.get(x, {}).get("n", 0) > 0,
                          "future": x > today.isoformat(), "counts": x >= eff_start} for x in days]}

    this_ws = _week_start(today, ws)
    week = week_summary(this_ws, today)
    last_week = week_summary(this_ws - timedelta(days=7), this_ws - timedelta(days=1))
    last_week["achieved"] = last_week["goal"] > 0 and last_week["on_track_days"] >= last_week["goal"] and \
        any(x["logged"] for x in last_week["days"])

    def month_summary(y, m, upto: date):
        first_d = date(y, m, 1); nxt = date(y + (m == 12), m % 12 + 1, 1); last_d = nxt - timedelta(days=1)
        days = [(first_d + timedelta(days=i)).isoformat() for i in range((last_d - first_d).days + 1)]
        counted = [x for x in days if x >= eff_start]
        on = sum(1 for x in days if x <= upto.isoformat() and st.get(x, {}).get("on_track"))
        return {"month": f"{y}-{m:02d}", "on_track_days": on, "goal": goal_for(len(counted), 0.7), "days_in_month": len(days)}

    month = month_summary(today.year, today.month, today)
    lm_last = date(today.year, today.month, 1) - timedelta(days=1)
    last_month = month_summary(lm_last.year, lm_last.month, lm_last)
    last_month["achieved"] = last_month["goal"] > 0 and last_month["on_track_days"] >= last_month["goal"]

    # badges
    earned = set()
    if first: earned.add("first_log")
    for n in (3, 7, 14, 30, 100):
        if best >= n: earned.add(f"streak_{n}")
    if any(v["on_track"] for v in st.values()): earned.add("track_1")
    if best_track >= 7: earned.add("track_7")
    if last_week["achieved"] or (week["goal"] and week["on_track_days"] >= week["goal"] and week["on_track_days"] >= 5):
        earned.add("week_goal")
    if last_month["achieved"]: earned.add("month_goal")
    if c.execute("SELECT COUNT(*) FROM user_water WHERE user_id=? AND glasses>=?", (uid, int(s.get("water_target") or 8))).fetchone()[0] >= 7:
        earned.add("water_7")
    if c.execute("SELECT COUNT(DISTINCT e.food_id) FROM entries e JOIN foods f ON f.id=e.food_id "
                 "WHERE e.user_id=? AND f.barcode IS NOT NULL", (uid,)).fetchone()[0] >= 10:
        earned.add("scanner_10")
    wc = c.execute("SELECT COUNT(*) FROM user_weights WHERE user_id=?", (uid,)).fetchone()[0]
    if wc >= 4: earned.add("weigh_4")
    latest = c.execute("SELECT kg FROM user_weights WHERE user_id=? ORDER BY day DESC LIMIT 1", (uid,)).fetchone()
    if s.get("goal_weight_kg") and latest and latest[0] <= float(s["goal_weight_kg"]): earned.add("goal_weight")
    have = {r[0] for r in c.execute("SELECT badge FROM user_badges WHERE user_id=?", (uid,))}
    for b in earned - have:
        c.execute("INSERT OR IGNORE INTO user_badges(user_id, badge, earned_at, seen) VALUES (?,?,?,0)", (uid, b, time.time()))
    badges = {r["badge"]: dict(r) for r in c.execute("SELECT * FROM user_badges WHERE user_id=?", (uid,))}

    # today's progress
    t = st.get(today.isoformat(), {"used": 0, "n": 0, "avail": 0, "on_track": False, "water_hit": False})
    meals = {r[0] for r in c.execute("SELECT DISTINCT meal FROM entries WHERE user_id=? AND day=?", (uid, today.isoformat()))}
    y = st.get((today - timedelta(days=1)).isoformat(), {})

    # celebration events not yet shown
    seen = s.get("game_seen") or {}
    tiso = today.isoformat()
    events = []
    if s.get("celebrations", True):
        if seen.get("welcome") != tiso:
            events.append({"type": "welcome", "key": "welcome", "value": tiso, "streak": streak, "best": best,
                           "milestone": streak in MILESTONES, "yesterday_on_track": bool(y.get("on_track")),
                           "yesterday_logged": y.get("n", 0) > 0, "quote": random.choice(QUOTES)})
        if last_week["achieved"] and seen.get("week") != last_week["start"]:
            events.append({"type": "week", "key": "week", "value": last_week["start"], **{k: last_week[k] for k in ("on_track_days", "goal")}})
        if last_month["achieved"] and seen.get("month") != last_month["month"]:
            events.append({"type": "month", "key": "month", "value": last_month["month"], **{k: last_month[k] for k in ("on_track_days", "goal", "month")}})
        if {"breakfast", "lunch", "dinner"} <= meals and t["on_track"] and seen.get("daygoal") != tiso:
            events.append({"type": "daygoal", "key": "daygoal", "value": tiso, "used": t["used"], "avail": t["avail"]})
        if t["water_hit"] and seen.get("water") != tiso:
            events.append({"type": "water", "key": "water", "value": tiso, "target": int(s.get("water_target") or 8)})
        for b in BADGES:
            if b[0] in badges and not badges[b[0]]["seen"]:
                events.append({"type": "badge", "key": "badge:" + b[0], "value": b[0], "badge": BADGE_INFO[b[0]]})
    elif any(not b["seen"] for b in badges.values()):
        c.execute("UPDATE user_badges SET seen=1 WHERE user_id=?", (uid,))

    nxt = next((m for m in MILESTONES if m > streak), None)
    return {
        "streak": streak, "best_streak": best, "track_streak": track_streak, "next_milestone": nxt,
        "today": {"used": t["used"], "avail": t["avail"], "entries": t["n"], "on_track": t["on_track"], "meals": sorted(meals),
                  "water_hit": t["water_hit"]},
        "week": week, "month": month, "quote": random.choice(QUOTES),
        "badges": [{**BADGE_INFO[b[0]], "earned": b[0] in badges, "earned_at": badges.get(b[0], {}).get("earned_at")} for b in BADGES],
        "events": events,
    }


def _today_from(q: Optional[str]) -> date:
    try:
        return date.fromisoformat(q) if q else date.today()
    except ValueError:
        return date.today()


@app.post("/api/checkin")
def checkin(request: Request, today: Optional[str] = None):
    uid, d = uid_of(request), _today_from(today)
    with db() as c:
        c.execute("INSERT OR IGNORE INTO user_visits(user_id, day) VALUES (?,?)", (uid, d.isoformat()))
        return game_state(c, uid, d)


@app.get("/api/game")
def game(request: Request, today: Optional[str] = None):
    with db() as c:
        return game_state(c, uid_of(request), _today_from(today))


class Seen(BaseModel):
    keys: dict


@app.post("/api/game/seen")
def game_seen(request: Request, b: Seen):
    uid = uid_of(request)
    s = get_settings(uid)
    seen = dict(s.get("game_seen") or {})
    with db() as c:
        for k, v in b.keys.items():
            if k.startswith("badge:"):
                c.execute("UPDATE user_badges SET seen=1 WHERE user_id=? AND badge=?", (uid, k[6:]))
            else:
                seen[k] = v
        c.execute("INSERT OR REPLACE INTO user_settings VALUES (?,?,?)", (uid, "game_seen", json.dumps(seen)))
    return {"ok": True}


# ---------------------------------------------------------------- push notifications
import asyncio
import base64
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01
from pywebpush import WebPushException, webpush

log = logging.getLogger("mytrack.push")
TZ = ZoneInfo(os.environ.get("TZ") or "Europe/London")
VAPID_FILE = DATA_DIR / "vapid_private.pem"
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "https://mytrack.marvelcloud.uk")


def _vapid():
    if not VAPID_FILE.exists():
        v = Vapid01(); v.generate_keys(); v.save_key(str(VAPID_FILE))
    v = Vapid01.from_file(str(VAPID_FILE))
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


VAPID_PUBLIC = _vapid()


def send_push(sub: dict, payload: dict) -> str:
    """Returns 'ok', 'gone' (subscription expired) or an error string."""
    try:
        webpush(subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                data=json.dumps(payload), vapid_private_key=str(VAPID_FILE),
                vapid_claims={"sub": VAPID_SUBJECT}, ttl=6 * 3600, timeout=10)
        return "ok"
    except WebPushException as ex:
        code = getattr(ex.response, "status_code", None)
        if code in (404, 410):
            return "gone"
        return f"error {code or ''} {str(ex)[:120]}"
    except Exception as ex:  # noqa: BLE001
        return f"error {type(ex).__name__}: {str(ex)[:120]}"


def push_user(uid: int, payload: dict) -> list:
    with db() as c:
        subs = rows(c.execute("SELECT * FROM push_subs WHERE user_id=?", (uid,)))
    results = []
    for sub in subs:
        r = send_push(sub, payload)
        results.append(r)
        with db() as c:
            if r == "gone":
                c.execute("DELETE FROM push_subs WHERE id=?", (sub["id"],))
            elif r == "ok":
                c.execute("UPDATE push_subs SET last_ok=? WHERE id=?", (time.time(), sub["id"]))
        if r not in ("ok", "gone"):
            log.warning("push to user %s failed: %s", uid, r)
    return results


@app.get("/api/push/key")
def push_key():
    return {"key": VAPID_PUBLIC}


class PushSub(BaseModel):
    subscription: dict
    ua: str = ""


@app.post("/api/push/subscribe")
def push_subscribe(request: Request, b: PushSub):
    sub = b.subscription
    keys = sub.get("keys") or {}
    if not sub.get("endpoint") or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "Invalid subscription")
    with db() as c:   # a device belongs to whoever enabled it last
        c.execute("INSERT INTO push_subs(user_id, endpoint, p256dh, auth, ua, created) VALUES (?,?,?,?,?,?) "
                  "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth, ua=excluded.ua",
                  (uid_of(request), sub["endpoint"], keys["p256dh"], keys["auth"], b.ua[:200], time.time()))
    return {"ok": True}


class PushUnsub(BaseModel):
    endpoint: str


@app.post("/api/push/unsubscribe")
def push_unsubscribe(request: Request, b: PushUnsub):
    with db() as c:
        c.execute("DELETE FROM push_subs WHERE endpoint=? AND user_id=?", (b.endpoint, uid_of(request)))
    return {"ok": True}


@app.get("/api/push/status")
def push_status(request: Request):
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM push_subs WHERE user_id=?", (uid_of(request),)).fetchone()[0]
    return {"devices": n}


@app.post("/api/push/test")
async def push_test(request: Request):
    u = request.state.user
    res = await asyncio.to_thread(push_user, u["id"], {
        "title": "MyTrack notifications are on 🎉", "body": f"Nice one, {u['name']} — this is what your nudges will look like.",
        "tag": "test", "url": "/"})
    if not res:
        raise HTTPException(400, "This device isn't registered for notifications yet")
    return {"sent": res.count("ok"), "results": res}


def build_notification(c, uid: int, kind: str, now: datetime):
    """Work out what (if anything) to send. Returns payload or None."""
    s = get_settings(uid)
    today = now.date(); tiso = today.isoformat()
    st = day_statuses(c, uid, s, today - timedelta(days=1), today)
    t = st[tiso]
    name = s.get("name") or ""
    if kind == "morning":
        g = game_state(c, uid, today)   # read-only use: tells us about unseen week/month wins
        types = {e["type"]: e for e in g["events"]}
        carry = t["avail"] - int(s["daily_points"] or 0)
        sins = f"{t['avail']} sins ready today" + (f" (incl. {carry} carried over)" if carry > 0 else "")
        if "month" in types:
            e = types["month"]
            return {"title": "👑 Month master!", "body": f"{e['on_track_days']} on-track days last month. {sins} — let's go again.", "tag": "morning"}
        if "week" in types:
            e = types["week"]
            return {"title": "🥇 You won last week!", "body": f"{e['on_track_days']} on-track days. {sins}.", "tag": "morning"}
        hello = "Good morning" + (f", {name}" if name else "") + " ☀️"
        return {"title": hello, "body": f"{sins}. “{random.choice(QUOTES)}”", "tag": "morning"}
    if kind == "water":
        target = int(s.get("water_target") or 8)
        w = c.execute("SELECT glasses FROM user_water WHERE user_id=? AND day=?", (uid, tiso)).fetchone()
        have = w[0] if w else 0
        if have >= target * 0.5:
            return None
        return {"title": "💧 Time for a top-up", "body": f"{have} of {target} glasses so far today. Grab a drink and tap a glass!", "tag": "water"}
    if kind == "streak":
        if t["n"] > 0:
            return None
        active = {r[0] for r in c.execute("SELECT day FROM user_visits WHERE user_id=?", (uid,))}
        active |= {r[0] for r in c.execute("SELECT DISTINCT day FROM entries WHERE user_id=?", (uid,))}
        streak, d = 0, today - timedelta(days=1)
        while d.isoformat() in active:
            streak += 1; d -= timedelta(days=1)
        if streak >= 2:
            return {"title": f"🔥 Keep your {streak}-day streak alive!", "body": "Nothing logged yet today — log something before bed to keep it going.", "tag": "streak", "url": "/?add=quick"}
        return {"title": "📝 Quick check-in", "body": "Nothing logged yet today. A quick log now keeps you on track.", "tag": "streak", "url": "/?add=quick"}
    return None


async def scheduler():
    """Every minute: send any notifications that are due. Each one is sent at most once a day,
    and only within 3 hours of its time (so a restart late in the day doesn't fire a stale morning message)."""
    await asyncio.sleep(5)
    while True:
        try:
            now = datetime.now(TZ); tiso = now.date().isoformat(); mins = now.hour * 60 + now.minute
            with db() as c:
                uids = [r[0] for r in c.execute("SELECT DISTINCT p.user_id FROM push_subs p JOIN users u ON u.id=p.user_id WHERE u.active=1")]
            for uid in uids:
                s = get_settings(uid)
                cfg = {**DEFAULT_SETTINGS["notify"], **(s.get("notify") or {})}
                sent = dict(s.get("notify_sent") or {})
                changed = False
                for kind, conf in cfg.items():
                    if not conf.get("on") or sent.get(kind) == tiso:
                        continue
                    try:
                        hh, mm = map(int, str(conf.get("time", "08:00")).split(":"))
                    except ValueError:
                        continue
                    due = hh * 60 + mm
                    if not (due <= mins < due + 180):
                        continue
                    with db() as c:
                        payload = build_notification(c, uid, kind, now)
                    sent[kind] = tiso; changed = True     # decided for today, whether or not it was needed
                    if payload:
                        await asyncio.to_thread(push_user, uid, payload)
                if changed:
                    with db() as c:
                        c.execute("INSERT OR REPLACE INTO user_settings VALUES (?,?,?)", (uid, "notify_sent", json.dumps(sent)))
        except Exception:  # noqa: BLE001
            log.exception("notification scheduler error")
        await asyncio.sleep(60 - datetime.now().second + 1)


@app.on_event("startup")
async def _start_scheduler():
    asyncio.create_task(scheduler())


# ---------------------------------------------------------------- Withings weigh-in sync
from fastapi.responses import RedirectResponse
from urllib.parse import urlencode

WITHINGS_ID = os.environ.get("WITHINGS_CLIENT_ID", "").strip()
WITHINGS_SECRET = os.environ.get("WITHINGS_CLIENT_SECRET", "").strip()
WITHINGS_REDIRECT = os.environ.get("WITHINGS_REDIRECT_URI", "").strip()
WITHINGS_AUTH = os.environ.get("WITHINGS_AUTH_BASE", "https://account.withings.com").rstrip("/")
WITHINGS_API = os.environ.get("WITHINGS_API_BASE", "https://wbsapi.withings.net").rstrip("/")
WITHINGS_EVERY = 30 * 60


def withings_ready():
    return bool(WITHINGS_ID and WITHINGS_SECRET and WITHINGS_REDIRECT)


def _wpost(path: str, data: dict, token: Optional[str] = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = httpx.post(f"{WITHINGS_API}{path}", data=data, headers=headers, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("status") != 0:
        raise RuntimeError(f"Withings status {j.get('status')}: {j.get('error', '')}")
    return j.get("body") or {}


def _save_tokens(c, uid, body):
    c.execute("""INSERT INTO withings(user_id, w_userid, access_token, refresh_token, expires_at, scope, connected)
                 VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET w_userid=excluded.w_userid,
                 access_token=excluded.access_token, refresh_token=excluded.refresh_token, expires_at=excluded.expires_at,
                 scope=excluded.scope, last_error=''""",
              (uid, str(body.get("userid", "")), body["access_token"], body["refresh_token"],
               time.time() + int(body.get("expires_in", 10800)), body.get("scope", ""), time.time()))


def _access_token(uid) -> str:
    with db() as c:
        w = c.execute("SELECT * FROM withings WHERE user_id=?", (uid,)).fetchone()
    if not w:
        raise RuntimeError("not connected")
    if w["expires_at"] - 120 > time.time():
        return w["access_token"]
    body = _wpost("/v2/oauth2", {"action": "requesttoken", "grant_type": "refresh_token", "client_id": WITHINGS_ID,
                                 "client_secret": WITHINGS_SECRET, "refresh_token": w["refresh_token"]})
    with db() as c:
        _save_tokens(c, uid, body)   # Withings rotates the refresh token every time
    return body["access_token"]


def withings_sync(uid: int) -> dict:
    """Pull new weigh-ins (and body fat %) since the last sync. Latest reading per day wins;
    weigh-ins typed in by hand are never overwritten."""
    try:
        token = _access_token(uid)
        with db() as c:
            last = c.execute("SELECT last_update FROM withings WHERE user_id=?", (uid,)).fetchone()[0] or 0
        groups, offset, updatetime = [], None, int(time.time())
        while True:
            data = {"action": "getmeas", "meastypes": "1,6", "category": "1", "lastupdate": str(last)}
            if offset:
                data["offset"] = str(offset)
            body = _wpost("/measure", data, token)
            groups += body.get("measuregrps", [])
            updatetime = int(body.get("updatetime") or updatetime)
            if not body.get("more"):
                break
            offset = body.get("offset")
        added = 0
        with db() as c:
            for g in sorted(groups, key=lambda g: g.get("date", 0)):
                vals = {m["type"]: m["value"] * (10 ** m["unit"]) for m in g.get("measures", [])}
                if 1 not in vals:
                    continue
                ts = g["date"]; day = datetime.fromtimestamp(ts, TZ).date().isoformat()
                ex = c.execute("SELECT source, measured_at FROM user_weights WHERE user_id=? AND day=?", (uid, day)).fetchone()
                if ex and (ex["source"] or "manual") == "manual":
                    continue
                if ex and (ex["measured_at"] or 0) > ts:
                    continue
                c.execute("INSERT OR REPLACE INTO user_weights(user_id, day, kg, source, fat_pct, measured_at) VALUES (?,?,?,?,?,?)",
                          (uid, day, round(vals[1], 2), "withings", round(vals[6], 1) if 6 in vals else None, ts))
                added += 1
            c.execute("UPDATE withings SET last_update=?, last_sync=?, last_error='', imported=imported+? WHERE user_id=?",
                      (updatetime, time.time(), added, uid))
        return {"ok": True, "added": added}
    except Exception as ex:  # noqa: BLE001
        msg = str(ex)[:200]
        if "status 401" in msg or "status 601" in msg or "invalid_grant" in msg:
            msg = "Withings needs reconnecting — tap Disconnect, then Connect again."
        with db() as c:
            c.execute("UPDATE withings SET last_error=?, last_sync=? WHERE user_id=?", (msg, time.time(), uid))
        log.warning("withings sync for user %s failed: %s", uid, msg)
        return {"ok": False, "error": msg}


@app.get("/api/withings/status")
def withings_status(request: Request):
    with db() as c:
        w = c.execute("SELECT last_sync, last_error, imported, connected FROM withings WHERE user_id=?", (uid_of(request),)).fetchone()
    return {"configured": withings_ready(), "connected": bool(w), **(dict(w) if w else {})}


@app.get("/api/withings/connect")
def withings_connect(request: Request):
    if not withings_ready():
        raise HTTPException(400, "Withings isn't set up on this server yet")
    state = sign(f"w:{uid_of(request)}:{int(time.time())}")
    q = urlencode({"response_type": "code", "client_id": WITHINGS_ID, "scope": "user.metrics",
                   "redirect_uri": WITHINGS_REDIRECT, "state": state})
    return {"url": f"{WITHINGS_AUTH}/oauth2_user/authorize2?{q}"}


@app.get("/api/withings/callback")
async def withings_callback(code: str = "", state: str = "", error: str = ""):
    """Withings sends the browser back here. The signed, 15-minute `state` identifies the person."""
    if error or not code:
        return RedirectResponse("/?tab=settings&withings=cancelled")
    try:
        value, _ = state.rsplit(".", 1)
        tag, uid, ts = value.split(":")
        if tag != "w" or not hmac.compare_digest(sign(value), state) or time.time() - int(ts) > 900:
            raise ValueError
        uid = int(uid)
    except ValueError:
        return RedirectResponse("/?tab=settings&withings=error")
    try:
        body = await asyncio.to_thread(_wpost, "/v2/oauth2", {"action": "requesttoken", "grant_type": "authorization_code",
                                                              "client_id": WITHINGS_ID, "client_secret": WITHINGS_SECRET,
                                                              "code": code, "redirect_uri": WITHINGS_REDIRECT})
        with db() as c:
            c.execute("DELETE FROM withings WHERE user_id=?", (uid,))
            _save_tokens(c, uid, body)
    except Exception as ex:  # noqa: BLE001
        log.warning("withings token exchange failed: %s", ex)
        return RedirectResponse("/?tab=settings&withings=error")
    await asyncio.to_thread(withings_sync, uid)   # pull history straight away
    return RedirectResponse("/?tab=settings&withings=connected")


@app.post("/api/withings/sync")
async def withings_sync_now(request: Request, auto: bool = False):
    uid = uid_of(request)
    with db() as c:
        w = c.execute("SELECT last_sync FROM withings WHERE user_id=?", (uid,)).fetchone()
    if not w:
        raise HTTPException(400, "Withings isn't connected")
    if auto and time.time() - (w["last_sync"] or 0) < 300:     # app-open syncs at most every 5 min
        return {"ok": True, "added": 0, "skipped": True}
    return await asyncio.to_thread(withings_sync, uid)


@app.delete("/api/withings")
def withings_disconnect(request: Request):
    with db() as c:
        c.execute("DELETE FROM withings WHERE user_id=?", (uid_of(request),))
    return {"ok": True}


async def withings_loop():
    await asyncio.sleep(20)
    while True:
        try:
            if withings_ready():
                with db() as c:
                    due = [r[0] for r in c.execute("SELECT user_id FROM withings WHERE last_sync < ?", (time.time() - WITHINGS_EVERY,))]
                for uid in due:
                    await asyncio.to_thread(withings_sync, uid)
        except Exception:  # noqa: BLE001
            log.exception("withings loop error")
        await asyncio.sleep(120)


@app.on_event("startup")
async def _start_withings():
    asyncio.create_task(withings_loop())

# ---------------------------------------------------------------- personal export
@app.get("/api/export")
def export(request: Request):
    uid = uid_of(request)
    with db() as c:
        data = {
            "entries": rows(c.execute("SELECT * FROM entries WHERE user_id=?", (uid,))),
            "weights": rows(c.execute("SELECT day, kg FROM user_weights WHERE user_id=?", (uid,))),
            "water": rows(c.execute("SELECT day, glasses FROM user_water WHERE user_id=?", (uid,))),
            "favourites": rows(c.execute(FOOD_SELECT + " WHERE uf.favourite=1", (uid,))),
        }
    data["settings"] = get_settings(uid)
    return JSONResponse(data, headers={"Content-Disposition": f"attachment; filename=mytrack-{date.today()}.json"})


# ---------------------------------------------------------------- admin
class NewUser(BaseModel):
    name: str
    pin: str
    is_admin: bool = False


class EditUser(BaseModel):
    name: Optional[str] = None
    pin: Optional[str] = None
    is_admin: Optional[bool] = None
    active: Optional[bool] = None


def active_admins(c, excluding=None):
    q = "SELECT COUNT(*) FROM users WHERE is_admin=1 AND active=1"
    return c.execute(q + (" AND id<>?" if excluding else ""), ((excluding,) if excluding else ())).fetchone()[0]


@app.get("/api/admin/overview")
def admin_overview():
    since = (date.today() - timedelta(days=29)).isoformat()
    with db() as c:
        users = rows(c.execute("""
          SELECT u.id, u.name, u.is_admin, u.active, u.colour, u.created, u.last_seen,
            (SELECT COUNT(*) FROM entries e WHERE e.user_id=u.id) AS entries,
            (SELECT COUNT(DISTINCT day) FROM entries e WHERE e.user_id=u.id AND e.day>=?) AS days_30,
            (SELECT MAX(day) FROM entries e WHERE e.user_id=u.id) AS last_logged,
            (SELECT COUNT(*) FROM user_weights w WHERE w.user_id=u.id) AS weigh_ins
          FROM users u ORDER BY u.is_admin DESC, u.name""", (since,)))
        totals = {
            "users": len(users),
            "entries": c.execute("SELECT COUNT(*) FROM entries").fetchone()[0],
            "foods": c.execute("SELECT COUNT(*) FROM foods").fetchone()[0],
            "custom_foods": c.execute("SELECT COUNT(*) FROM foods WHERE source='custom'").fetchone()[0],
            "scanned_foods": c.execute("SELECT COUNT(*) FROM foods WHERE barcode IS NOT NULL").fetchone()[0],
            "db_kb": round(DB_PATH.stat().st_size / 1024),
        }
    return {"users": users, "totals": totals}


@app.post("/api/admin/users")
def admin_create_user(b: NewUser):
    name = b.name.strip()
    if not name:
        raise HTTPException(400, "Name is required")
    if not valid_pin(b.pin.strip()):
        raise HTTPException(400, "PIN must be 4–8 digits")
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE lower(name)=lower(?)", (name,)).fetchone():
            raise HTTPException(409, "There's already a user with that name")
        uid = create_user(c, name[:40], b.pin.strip(), b.is_admin)
    return {"id": uid}


@app.put("/api/admin/users/{uid}")
def admin_edit_user(request: Request, uid: int, b: EditUser):
    me_id = uid_of(request)
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            raise HTTPException(404, "No such user")
        if uid == me_id and (b.is_admin is False or b.active is False):
            raise HTTPException(400, "You can't remove your own admin access or deactivate yourself")
        if (b.is_admin is False or b.active is False) and u["is_admin"] and active_admins(c, excluding=uid) == 0:
            raise HTTPException(400, "There must always be at least one admin")
        bump = False
        if b.name is not None and b.name.strip():
            dup = c.execute("SELECT 1 FROM users WHERE lower(name)=lower(?) AND id<>?", (b.name.strip(), uid)).fetchone()
            if dup:
                raise HTTPException(409, "There's already a user with that name")
            c.execute("UPDATE users SET name=? WHERE id=?", (b.name.strip()[:40], uid))
        if b.pin:
            if not valid_pin(b.pin.strip()):
                raise HTTPException(400, "PIN must be 4–8 digits")
            c.execute("UPDATE users SET pin_hash=? WHERE id=?", (hash_pin(b.pin.strip()), uid))
            bump = uid != me_id      # signs that person out everywhere (not you)
        if b.is_admin is not None:
            c.execute("UPDATE users SET is_admin=? WHERE id=?", (1 if b.is_admin else 0, uid))
        if b.active is not None:
            c.execute("UPDATE users SET active=? WHERE id=?", (1 if b.active else 0, uid))
            bump = bump or not b.active
        if bump:
            c.execute("UPDATE users SET session_ver=session_ver+1 WHERE id=?", (uid,))
    return {"ok": True}


@app.delete("/api/admin/users/{uid}")
def admin_delete_user(request: Request, uid: int):
    if uid == uid_of(request):
        raise HTTPException(400, "You can't delete yourself")
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            raise HTTPException(404, "No such user")
        if u["is_admin"] and active_admins(c, excluding=uid) == 0:
            raise HTTPException(400, "There must always be at least one admin")
        for t in ("entries", "user_weights", "user_water", "user_settings", "user_foods", "user_visits", "user_badges", "push_subs", "withings"):
            c.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
        c.execute("UPDATE foods SET created_by=NULL WHERE created_by=?", (uid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))
    return {"ok": True}


@app.get("/api/admin/foods")
def admin_foods(request: Request, q: str = "", kind: str = "custom"):
    uid = uid_of(request)
    where, args = [], [uid]
    if kind == "custom":
        where.append("f.source='custom'")
    elif kind == "scanned":
        where.append("f.barcode IS NOT NULL")
    if q.strip():
        where.append("(f.name LIKE ? OR f.brand LIKE ? OR f.barcode=?)")
        args += [f"%{q.strip()}%", f"%{q.strip()}%", q.strip()]
    sql = (FOOD_SELECT.replace("SELECT f.*", "SELECT f.*, cu.name AS created_by_name")
           + " LEFT JOIN users cu ON cu.id=f.created_by"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY f.created DESC LIMIT 200")
    with db() as c:
        return rows(c.execute(sql, args))


@app.get("/api/admin/backup")
def admin_backup():
    tmp = Path(tempfile.mkdtemp()) / f"mytrack-backup-{date.today()}.db"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(tmp)
    with dst:
        src.backup(dst)
    src.close(); dst.close()
    return FileResponse(tmp, filename=tmp.name, media_type="application/octet-stream",
                        background=BackgroundTask(shutil.rmtree, tmp.parent, ignore_errors=True))


@app.get("/healthz")
def health():
    return {"ok": True}


# ---------------------------------------------------------------- static
@app.get("/sw.js")
def sw():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/", StaticFiles(directory=STATIC), name="static")
