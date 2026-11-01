"""Supplements for MyTrack (merged from STACK).

Everything here is switched on per person (supp_prefs.enabled). For anyone with it off,
every /api/supps endpoint answers 403 and nothing in their app changes. Data lives only in
the supp_* tables below; the only shared table this module ever writes is `entries`, and only
for people who have Supplements on and tick a product set to count in the food diary.
"""
import asyncio
import json
import re
import time
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS supp_prefs (user_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (user_id, key));
CREATE TABLE IF NOT EXISTS supp_products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, brand TEXT DEFAULT '', barcode TEXT, serving_text TEXT DEFAULT '', unit TEXT DEFAULT 'serving',
  macros TEXT DEFAULT '{}', nutrients TEXT DEFAULT '[]', image_url TEXT DEFAULT '',
  created_by INTEGER, created REAL, updated REAL, archived INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS supp_slots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
  title TEXT NOT NULL, time TEXT NOT NULL, day_when TEXT DEFAULT 'all', note TEXT DEFAULT '',
  remind INTEGER DEFAULT 1, archived INTEGER DEFAULT 0, legacy_id TEXT
);
CREATE TABLE IF NOT EXISTS supp_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, slot_id INTEGER NOT NULL,
  product_id INTEGER, name TEXT NOT NULL, dose TEXT DEFAULT '', qty REAL DEFAULT 1,
  optional INTEGER DEFAULT 0, diary INTEGER DEFAULT 0, sort INTEGER DEFAULT 0,
  added_on TEXT, archived_on TEXT, legacy_id TEXT
);
CREATE TABLE IF NOT EXISTS supp_logs (
  user_id INTEGER, day TEXT, item_id INTEGER, time TEXT, qty REAL, entry_id INTEGER, at REAL,
  PRIMARY KEY (user_id, day, item_id)
);
CREATE TABLE IF NOT EXISTS supp_daytype (user_id INTEGER, day TEXT, type TEXT NOT NULL, PRIMARY KEY (user_id, day));
CREATE INDEX IF NOT EXISTS idx_supp_logs_user_day ON supp_logs(user_id, day);
CREATE INDEX IF NOT EXISTS idx_supp_items_user ON supp_items(user_id);
"""
USER_TABLES = ("supp_prefs", "supp_slots", "supp_items", "supp_logs", "supp_daytype")

PREF_DEFAULTS = {
    "enabled": False,
    "training_days": [0, 1, 3, 4, 5, 6],   # 0 = Sunday … 6 = Saturday
    "protein_target": None,
    "remind": True,
    "remind_sent": {},
}
WHENS = ("all", "training", "rest")
UNITS = ("mcg", "mg", "g", "iu")
TO_MG = {"g": 1000.0, "mg": 1.0, "mcg": 0.001}
MACRO_KEYS = ("kcal", "protein", "carbs", "sugar", "fat", "satfat", "fibre", "salt")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# ------------------------------------------------------------------ nutrient names
MINERALS = ["magnesium", "zinc", "selenium", "copper", "iron", "calcium", "potassium", "iodine",
            "chromium", "boron", "manganese", "molybdenum", "phosphorus", "sodium", "chloride"]
VITAMINS = [
    (r"\bvitamin\s*a\b|retinol|retinyl", "Vitamin A"),
    (r"\bvitamin\s*d\s*[23]?\b|cholecalciferol|ergocalciferol", "Vitamin D"),
    (r"\bvitamin\s*e\b|tocopher", "Vitamin E"),
    (r"\bvitamin\s*k\s*[12]?\b|menaquinone|phylloquinone|\bmk-?7\b", "Vitamin K"),
    (r"\bvitamin\s*c\b|ascorbic", "Vitamin C"),
    (r"\bvitamin\s*b\s*1\b|thiamin", "Vitamin B1"),
    (r"\bvitamin\s*b\s*2\b|riboflavin", "Vitamin B2"),
    (r"\bvitamin\s*b\s*3\b|niacin|nicotinamide", "Vitamin B3"),
    (r"\bvitamin\s*b\s*5\b|pantothen", "Vitamin B5"),
    (r"\bvitamin\s*b\s*6\b|pyridox", "Vitamin B6"),
    (r"\bvitamin\s*b\s*7\b|biotin", "Biotin"),
    (r"\bvitamin\s*b\s*9\b|folic|folate|methylfolate", "Folate"),
    (r"\bvitamin\s*b\s*12\b|cobalamin", "Vitamin B12"),
]


def canonical(raw: str) -> str:
    s = str(raw or "").strip()
    low = s.lower()
    if re.search(r"\bepa\b|eicosapentaenoic", low):
        return "EPA"
    if re.search(r"\bdha\b|docosahexaenoic", low):
        return "DHA"
    for pat, name in VITAMINS:
        if re.search(pat, low):
            return name
    for m in MINERALS:
        if re.search(r"\b" + m, low):
            return m.capitalize()
    if "caffeine" in low:
        return "Caffeine"
    if re.search(r"creatine", low):
        return "Creatine"
    return s[:1].upper() + s[1:]


def _num(v):
    if v is None or v == "":
        return None
    try:
        n = float(str(v).replace(",", "."))
    except ValueError:
        return None
    return n if n == n else None


def clean_nutrients(lst) -> list:
    out = []
    for n in (lst or [])[:60]:
        if not isinstance(n, dict):
            continue
        label = str(n.get("label") or n.get("name") or "").strip()[:80]
        amount, unit = _num(n.get("amount")), str(n.get("unit") or "").strip().lower().replace("µg", "mcg")
        if not label or amount is None or amount < 0 or unit not in UNITS:
            continue
        nrv = _num(n.get("nrv"))
        out.append({"name": canonical(n.get("name") or label), "label": label, "amount": round(amount, 4),
                    "unit": unit, "nrv": None if nrv is None else round(nrv, 1)})
    return out


def clean_macros(m) -> dict:
    out = {}
    for k in MACRO_KEYS:
        v = _num((m or {}).get(k))
        if v is not None and 0 <= v < 5000:
            out[k] = round(v, 3)
    return out


def register(app, *, db, rows, insert_entry, push_user, tz, log, points_for, off_get, off_fields):
    """Wire the supplement routes and reminder loop into MyTrack."""
    with db() as c:
        c.executescript(SCHEMA)

    r = APIRouter(prefix="/api")

    # -------------------------------------------------------------- helpers
    def prefs(c, uid) -> dict:
        stored = {row["key"]: json.loads(row["value"]) for row in
                  c.execute("SELECT key, value FROM supp_prefs WHERE user_id=?", (uid,))}
        return {**PREF_DEFAULTS, **stored}

    def set_pref(c, uid, key, value):
        c.execute("INSERT OR REPLACE INTO supp_prefs VALUES (?,?,?)", (uid, key, json.dumps(value)))

    def enabled(c, uid) -> bool:
        row = c.execute("SELECT value FROM supp_prefs WHERE user_id=? AND key='enabled'", (uid,)).fetchone()
        return bool(row and json.loads(row["value"]))

    def need_on(request: Request) -> int:
        uid = request.state.user["id"]
        with db() as c:
            if not enabled(c, uid):
                raise HTTPException(403, "Supplements are off for this person")
        return uid

    def need_admin(request: Request) -> int:
        uid = need_on(request)
        if not request.state.user["is_admin"]:
            raise HTTPException(403, "Admins only")
        return uid

    def check_day(day: str):
        if not DAY_RE.match(day or ""):
            raise HTTPException(400, "Bad date")

    def product_out(p) -> dict:
        d = dict(p)
        d["macros"] = json.loads(d.get("macros") or "{}")
        d["nutrients"] = json.loads(d.get("nutrients") or "[]")
        return d

    def day_type(c, uid, p, day: str) -> tuple:
        row = c.execute("SELECT type FROM supp_daytype WHERE user_id=? AND day=?", (uid, day)).fetchone()
        wd = (date.fromisoformat(day).weekday() + 1) % 7      # Python Mon=0 -> Sun=0
        default = "training" if wd in (p.get("training_days") or []) else "rest"
        return (row["type"] if row else default), bool(row)

    def plan_for(c, uid, day: str, p=None):
        """Slots (with items) that apply on this day, ordered by time."""
        p = p or prefs(c, uid)
        t, _ = day_type(c, uid, p, day)
        slots = rows(c.execute("SELECT * FROM supp_slots WHERE user_id=? AND archived=0 ORDER BY time, id", (uid,)))
        items = rows(c.execute(
            "SELECT * FROM supp_items WHERE user_id=? AND (added_on IS NULL OR added_on<=?) "
            "AND (archived_on IS NULL OR ?<archived_on) ORDER BY sort, id", (uid, day, day)))
        out = []
        for s in slots:
            if s["day_when"] not in ("all", t):
                continue
            its = [i for i in items if i["slot_id"] == s["id"]]
            if its:
                out.append({**s, "items": its})
        return out, t

    def products_by_id(c, ids) -> dict:
        ids = [i for i in set(ids) if i]
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        return {p["id"]: product_out(p) for p in c.execute(f"SELECT * FROM supp_products WHERE id IN ({q})", ids)}

    def meal_for(hhmm: str) -> str:
        h = int(hhmm[:2])
        return "breakfast" if h < 11 else "lunch" if h < 15 else "snacks" if h < 17 else "dinner" if h < 21 else "snacks"

    def diary_add(c, uid, day, item, product, qty, hhmm) -> Optional[int]:
        m = product.get("macros") or {}
        if not item["diary"] or not (m.get("kcal") or m.get("protein")):
            return None
        kcal = (m.get("kcal") or 0) * qty
        data = {"day": day, "meal": meal_for(hhmm), "food_id": None, "name": item["name"],
                "brand": product.get("brand") or "", "amount_g": 0, "servings": qty,
                "unit_label": f"{qty:g} × {product.get('unit') or 'serving'} · from Supps",
                "kcal": round(kcal, 1), "protein": round((m.get("protein") or 0) * qty, 1),
                "carbs": round((m.get("carbs") or 0) * qty, 1), "fat": round((m.get("fat") or 0) * qty, 1),
                "satfat": round((m.get("satfat") or 0) * qty, 1), "sugar": round((m.get("sugar") or 0) * qty, 1),
                "points": points_for(c, uid, kcal)}
        insert_entry(c, uid, data)
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def diary_remove(c, uid, entry_id):
        if entry_id:
            c.execute("DELETE FROM entries WHERE id=? AND user_id=?", (entry_id, uid))

    # -------------------------------------------------------------- status / prefs
    @r.get("/supps/status")
    def status(request: Request):
        u = request.state.user
        with db() as c:
            on = enabled(c, u["id"])
            if not on:
                return {"on": False}
            p = prefs(c, u["id"])
        return {"on": True, "admin": bool(u["is_admin"]), "protein_target": p["protein_target"],
                "training_days": p["training_days"], "remind": p["remind"]}

    class Prefs(BaseModel):
        protein_target: Optional[float] = None
        training_days: Optional[list] = None
        remind: Optional[bool] = None

    @r.put("/supps/prefs")
    def put_prefs(request: Request, b: Prefs):
        uid = need_on(request)
        with db() as c:
            fields = b.model_fields_set
            if "protein_target" in fields:
                v = b.protein_target
                set_pref(c, uid, "protein_target", None if not v or v <= 0 else round(min(v, 500)))
            if b.training_days is not None:
                set_pref(c, uid, "training_days", sorted({int(d) for d in b.training_days if 0 <= int(d) <= 6}))
            if b.remind is not None:
                set_pref(c, uid, "remind", bool(b.remind))
        return status(request)

    # -------------------------------------------------------------- day
    @r.get("/supps/day/{day}")
    def get_day(request: Request, day: str):
        uid = need_on(request); check_day(day)
        with db() as c:
            p = prefs(c, uid)
            slots, t = plan_for(c, uid, day, p)
            _, overridden = day_type(c, uid, p, day)
            logs = {l["item_id"]: l for l in rows(c.execute("SELECT * FROM supp_logs WHERE user_id=? AND day=?", (uid, day)))}
            prods = products_by_id(c, [i["product_id"] for s in slots for i in s["items"]])
            for s in slots:
                for i in s["items"]:
                    i["product"] = prods.get(i["product_id"])
                    i["log"] = logs.get(i["id"])
        return {"day": day, "type": t, "type_overridden": overridden, "slots": slots,
                "protein_target": p["protein_target"]}

    class DayType(BaseModel):
        type: Optional[str] = None

    @r.put("/supps/day/{day}/type")
    def set_type(request: Request, day: str, b: DayType):
        uid = need_on(request); check_day(day)
        with db() as c:
            if b.type is None:
                c.execute("DELETE FROM supp_daytype WHERE user_id=? AND day=?", (uid, day))
            elif b.type in ("training", "rest"):
                c.execute("INSERT OR REPLACE INTO supp_daytype VALUES (?,?,?)", (uid, day, b.type))
            else:
                raise HTTPException(400, "Bad day type")
        return {"ok": True}

    class Log(BaseModel):
        day: str
        item_id: int
        time: str
        qty: Optional[float] = None

    def _log(c, uid, b: "Log"):
        item = c.execute("SELECT * FROM supp_items WHERE id=? AND user_id=?", (b.item_id, uid)).fetchone()
        if not item:
            raise HTTPException(404, "Unknown item")
        if not TIME_RE.match(b.time or ""):
            raise HTTPException(400, "Bad time")
        qty = item["qty"] if b.qty is None else b.qty
        if not (0 < qty <= 1000):
            raise HTTPException(400, "Bad amount")
        old = c.execute("SELECT entry_id FROM supp_logs WHERE user_id=? AND day=? AND item_id=?", (uid, b.day, b.item_id)).fetchone()
        if old:
            diary_remove(c, uid, old["entry_id"])
        prod = None
        if item["product_id"]:
            pr = c.execute("SELECT * FROM supp_products WHERE id=?", (item["product_id"],)).fetchone()
            prod = product_out(pr) if pr else None
        slot = c.execute("SELECT time FROM supp_slots WHERE id=?", (item["slot_id"],)).fetchone()
        entry_id = diary_add(c, uid, b.day, dict(item), prod, qty, slot["time"] if slot else b.time) if prod else None
        c.execute("INSERT OR REPLACE INTO supp_logs VALUES (?,?,?,?,?,?,?)",
                  (uid, b.day, b.item_id, b.time, round(qty, 2), entry_id, time.time()))

    @r.put("/supps/log")
    def put_log(request: Request, b: Log):
        uid = need_on(request); check_day(b.day)
        with db() as c:
            _log(c, uid, b)
        return {"ok": True}

    class Unlog(BaseModel):
        day: str
        item_id: int

    @r.post("/supps/unlog")
    def unlog(request: Request, b: Unlog):
        uid = need_on(request); check_day(b.day)
        with db() as c:
            old = c.execute("SELECT entry_id FROM supp_logs WHERE user_id=? AND day=? AND item_id=?", (uid, b.day, b.item_id)).fetchone()
            if old:
                diary_remove(c, uid, old["entry_id"])
                c.execute("DELETE FROM supp_logs WHERE user_id=? AND day=? AND item_id=?", (uid, b.day, b.item_id))
        return {"ok": True}

    class LogAll(BaseModel):
        day: str
        slot_id: int
        time: str

    @r.post("/supps/log-all")
    def log_all(request: Request, b: LogAll):
        uid = need_on(request); check_day(b.day)
        with db() as c:
            slots, _ = plan_for(c, uid, b.day)
            slot = next((s for s in slots if s["id"] == b.slot_id), None)
            if not slot:
                raise HTTPException(404, "Unknown slot")
            done = {r_["item_id"] for r_ in c.execute("SELECT item_id FROM supp_logs WHERE user_id=? AND day=?", (uid, b.day))}
            n = 0
            for i in slot["items"]:
                if i["id"] not in done and not i["optional"]:
                    _log(c, uid, Log(day=b.day, item_id=i["id"], time=b.time)); n += 1
        return {"ok": True, "logged": n}

    # -------------------------------------------------------------- history (week / streaks / adherence)
    @r.get("/supps/history")
    def history(request: Request, end: str, days: int = 35):
        uid = need_on(request); check_day(end)
        days = max(7, min(days, 120))
        last = date.fromisoformat(end)
        with db() as c:
            p = prefs(c, uid)
            first_log = c.execute("SELECT MIN(day) FROM supp_logs WHERE user_id=?", (uid,)).fetchone()[0]
            logs = {}
            for l in c.execute("SELECT day, item_id, time FROM supp_logs WHERE user_id=? AND day BETWEEN ? AND ?",
                               (uid, (last - timedelta(days=days - 1)).isoformat(), end)):
                logs.setdefault(l["day"], {})[l["item_id"]] = l["time"]
            out, per_item = [], {}
            for k in range(days - 1, -1, -1):
                d = (last - timedelta(days=k)).isoformat()
                slots, t = plan_for(c, uid, d, p)
                req = [i for s in slots for i in s["items"] if not i["optional"]]
                got = logs.get(d, {})
                taken = sum(1 for i in req if i["id"] in got)
                tracked = bool(first_log and d >= first_log)
                out.append({"day": d, "type": t, "required": len(req), "taken": taken, "extra": len(got) - taken, "tracked": tracked})
                if tracked:
                    for s in slots:
                        for i in s["items"]:
                            e = per_item.setdefault(i["id"], {"item_id": i["id"], "name": i["name"], "slot": s["title"],
                                                              "planned": s["time"], "optional": bool(i["optional"]),
                                                              "due": 0, "taken": 0, "times": []})
                            e["due"] += 1
                            if i["id"] in got:
                                e["taken"] += 1; e["times"].append(got[i["id"]])
            # streak: consecutive complete days ending today (today counts only once complete)
            streak = best = run = 0
            for x in out:
                full = x["tracked"] and x["required"] and x["taken"] >= x["required"]
                run = run + 1 if full else 0
                best = max(best, run)
            for x in reversed(out):
                full = x["required"] and x["taken"] >= x["required"]
                if full:
                    streak += 1
                elif x["day"] == end:
                    continue
                else:
                    break
            for e in per_item.values():
                mins = [int(t[:2]) * 60 + int(t[3:]) for t in e.pop("times")]
                e["avg_time"] = f"{int(sum(mins) / len(mins)) // 60:02d}:{int(sum(mins) / len(mins)) % 60:02d}" if mins else None
        return {"days": out, "streak": streak, "best": best, "items": sorted(per_item.values(), key=lambda e: e["taken"] / max(1, e["due"]))}

    @r.get("/supps/taken")
    def taken_range(request: Request, start: str, end: str):
        """Everything ticked between two days, with product nutrients — used for the 7-day upper-limit watch."""
        uid = need_on(request); check_day(start); check_day(end)
        with db() as c:
            ls = rows(c.execute(
                "SELECT l.day, l.time, l.qty, i.name, i.product_id FROM supp_logs l JOIN supp_items i ON i.id=l.item_id "
                "WHERE l.user_id=? AND l.day BETWEEN ? AND ?", (uid, start, end)))
            prods = products_by_id(c, [l["product_id"] for l in ls])
        for l in ls:
            pr = prods.get(l["product_id"])
            l["nutrients"] = pr["nutrients"] if pr else []
        return ls

    # -------------------------------------------------------------- plan editing (own plan)
    class Slot(BaseModel):
        title: str
        time: str
        day_when: str = "all"
        note: str = ""
        remind: bool = True

    def _slot_ok(b: Slot):
        if not b.title.strip() or not TIME_RE.match(b.time) or b.day_when not in WHENS:
            raise HTTPException(400, "Check the name, time and days")

    @r.get("/supps/plan")
    def get_plan(request: Request):
        uid = need_on(request)
        today = datetime.now(tz).date().isoformat()
        with db() as c:
            slots = rows(c.execute("SELECT * FROM supp_slots WHERE user_id=? AND archived=0 ORDER BY time, id", (uid,)))
            items = rows(c.execute("SELECT * FROM supp_items WHERE user_id=? AND (archived_on IS NULL OR archived_on>?) "
                                   "ORDER BY sort, id", (uid, today)))
            prods = products_by_id(c, [i["product_id"] for i in items])
        for i in items:
            i["product"] = prods.get(i["product_id"])
        for s in slots:
            s["items"] = [i for i in items if i["slot_id"] == s["id"]]
        return {"slots": slots}

    @r.post("/supps/slots")
    def add_slot(request: Request, b: Slot):
        uid = need_on(request); _slot_ok(b)
        with db() as c:
            c.execute("INSERT INTO supp_slots(user_id,title,time,day_when,note,remind) VALUES (?,?,?,?,?,?)",
                      (uid, b.title.strip()[:40], b.time, b.day_when, b.note.strip()[:120], int(b.remind)))
            return {"id": c.execute("SELECT last_insert_rowid()").fetchone()[0]}

    @r.put("/supps/slots/{sid}")
    def edit_slot(request: Request, sid: int, b: Slot):
        uid = need_on(request); _slot_ok(b)
        with db() as c:
            c.execute("UPDATE supp_slots SET title=?, time=?, day_when=?, note=?, remind=? WHERE id=? AND user_id=?",
                      (b.title.strip()[:40], b.time, b.day_when, b.note.strip()[:120], int(b.remind), sid, uid))
        return {"ok": True}

    @r.delete("/supps/slots/{sid}")
    def del_slot(request: Request, sid: int):
        uid = need_on(request)
        today = datetime.now(tz).date().isoformat()
        with db() as c:
            c.execute("UPDATE supp_items SET archived_on=? WHERE slot_id=? AND user_id=? AND archived_on IS NULL", (today, sid, uid))
            c.execute("UPDATE supp_slots SET archived=1 WHERE id=? AND user_id=?", (sid, uid))
        return {"ok": True}

    class Item(BaseModel):
        slot_id: int
        product_id: Optional[int] = None
        name: str
        dose: str = ""
        qty: float = 1
        optional: bool = False
        diary: bool = False

    def _item_ok(c, uid, b: Item):
        if not b.name.strip() or not (0 < b.qty <= 1000):
            raise HTTPException(400, "Check the name and amount")
        if not c.execute("SELECT 1 FROM supp_slots WHERE id=? AND user_id=? AND archived=0", (b.slot_id, uid)).fetchone():
            raise HTTPException(400, "Unknown time slot")
        if b.product_id and not c.execute("SELECT 1 FROM supp_products WHERE id=?", (b.product_id,)).fetchone():
            raise HTTPException(400, "Unknown product")

    @r.post("/supps/items")
    def add_item(request: Request, b: Item):
        uid = need_on(request)
        today = datetime.now(tz).date().isoformat()
        with db() as c:
            _item_ok(c, uid, b)
            c.execute("INSERT INTO supp_items(user_id,slot_id,product_id,name,dose,qty,optional,diary,sort,added_on) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (uid, b.slot_id, b.product_id, b.name.strip()[:60], b.dose.strip()[:120], round(b.qty, 2),
                       int(b.optional), int(b.diary), int(time.time()), today))
            return {"id": c.execute("SELECT last_insert_rowid()").fetchone()[0]}

    @r.put("/supps/items/{iid}")
    def edit_item(request: Request, iid: int, b: Item):
        uid = need_on(request)
        with db() as c:
            _item_ok(c, uid, b)
            c.execute("UPDATE supp_items SET slot_id=?, product_id=?, name=?, dose=?, qty=?, optional=?, diary=? WHERE id=? AND user_id=?",
                      (b.slot_id, b.product_id, b.name.strip()[:60], b.dose.strip()[:120], round(b.qty, 2),
                       int(b.optional), int(b.diary), iid, uid))
        return {"ok": True}

    @r.delete("/supps/items/{iid}")
    def del_item(request: Request, iid: int):
        """Archived from today so past days (and streaks) still show it."""
        uid = need_on(request)
        today = datetime.now(tz).date().isoformat()
        with db() as c:
            it = c.execute("SELECT added_on FROM supp_items WHERE id=? AND user_id=?", (iid, uid)).fetchone()
            if it and it["added_on"] == today and not c.execute("SELECT 1 FROM supp_logs WHERE item_id=?", (iid,)).fetchone():
                c.execute("DELETE FROM supp_items WHERE id=? AND user_id=?", (iid, uid))
            else:
                c.execute("UPDATE supp_items SET archived_on=? WHERE id=? AND user_id=?", (today, iid, uid))
        return {"ok": True}

    # -------------------------------------------------------------- products
    @r.get("/supps/products")
    def list_products(request: Request, q: str = ""):
        uid = need_on(request)
        with db() as c:
            args, where = [uid], "p.archived=0"
            if q.strip():
                where += " AND (p.name LIKE ? OR p.brand LIKE ? OR p.barcode=?)"
                args += [f"%{q.strip()}%", f"%{q.strip()}%", q.strip()]
            ps = c.execute(
                "SELECT p.*, (SELECT COUNT(*) FROM supp_items i WHERE i.product_id=p.id AND i.user_id=? AND i.archived_on IS NULL) AS on_plan "
                f"FROM supp_products p WHERE {where} ORDER BY p.brand, p.name", args)
            return [product_out(p) for p in ps]

    class Product(BaseModel):
        name: str
        brand: str = ""
        barcode: Optional[str] = None
        serving_text: str = ""
        unit: str = "serving"
        macros: dict = {}
        nutrients: list = []
        image_url: str = ""

    def _prod_vals(b: Product):
        if not b.name.strip():
            raise HTTPException(400, "Give the product a name")
        bc = "".join(ch for ch in (b.barcode or "") if ch.isdigit()) or None
        return (b.name.strip()[:80], b.brand.strip()[:60], bc, b.serving_text.strip()[:60],
                (b.unit.strip() or "serving")[:20], json.dumps(clean_macros(b.macros)),
                json.dumps(clean_nutrients(b.nutrients)), b.image_url[:300])

    @r.post("/admin/supps/products")
    def add_product(request: Request, b: Product):
        uid = need_admin(request)
        with db() as c:
            c.execute("INSERT INTO supp_products(name,brand,barcode,serving_text,unit,macros,nutrients,image_url,created_by,created,updated) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)", _prod_vals(b) + (uid, time.time(), time.time()))
            return {"id": c.execute("SELECT last_insert_rowid()").fetchone()[0]}

    @r.put("/admin/supps/products/{pid}")
    def edit_product(request: Request, pid: int, b: Product):
        need_admin(request)
        with db() as c:
            c.execute("UPDATE supp_products SET name=?,brand=?,barcode=?,serving_text=?,unit=?,macros=?,nutrients=?,image_url=?,updated=? "
                      "WHERE id=?", _prod_vals(b) + (time.time(), pid))
        return {"ok": True}

    @r.delete("/admin/supps/products/{pid}")
    def del_product(request: Request, pid: int):
        need_admin(request)
        with db() as c:
            used = c.execute("SELECT COUNT(*) FROM supp_items WHERE product_id=?", (pid,)).fetchone()[0]
            if used:
                c.execute("UPDATE supp_products SET archived=1 WHERE id=?", (pid,))   # keep history readable
            else:
                c.execute("DELETE FROM supp_products WHERE id=?", (pid,))
        return {"ok": True}

    @r.get("/admin/supps/barcode/{code}")
    async def supp_barcode(request: Request, code: str):
        """Look a barcode up without touching the shared food list."""
        need_admin(request)
        code = "".join(ch for ch in code if ch.isdigit())
        if not code:
            raise HTTPException(400, "Invalid barcode")
        with db() as c:
            hit = c.execute("SELECT * FROM supp_products WHERE barcode=? AND archived=0", (code,)).fetchone()
            if hit:
                return {"found": True, "existing": product_out(hit)}
        try:
            data = await off_get(f"/api/v2/product/{code}.json", {"fields": off_fields})
        except Exception as ex:  # noqa: BLE001
            raise HTTPException(502, f"Couldn't reach Open Food Facts ({type(ex).__name__})")
        p = data.get("product") if data.get("status") == 1 else None
        if not p:
            return {"found": False, "barcode": code}
        n = p.get("nutriments") or {}

        def pick(suf):
            kcal = n.get("energy-kcal" + suf)
            if kcal is None and n.get("energy" + suf) is not None:
                kcal = float(n["energy" + suf]) / 4.184
            return clean_macros({"kcal": kcal, "protein": n.get("proteins" + suf), "carbs": n.get("carbohydrates" + suf),
                                 "sugar": n.get("sugars" + suf), "fat": n.get("fat" + suf), "satfat": n.get("saturated-fat" + suf),
                                 "fibre": n.get("fiber" + suf), "salt": n.get("salt" + suf)})
        per_serving = pick("_serving")
        return {"found": True, "product": {
            "barcode": code, "name": (p.get("product_name_en") or p.get("product_name") or "").strip(),
            "brand": (p.get("brands") or "").split(",")[0].strip(), "serving_text": (p.get("serving_size") or "").strip(),
            "macros": per_serving, "macros_per100": pick("_100g"),
            "image_url": p.get("image_front_small_url") or p.get("image_small_url") or ""}}

    # -------------------------------------------------------------- who has it
    @r.get("/admin/supps/access")
    def get_access(request: Request):
        need_admin(request)
        with db() as c:
            us = rows(c.execute("SELECT id, name, colour, is_admin, active FROM users ORDER BY is_admin DESC, name"))
            for u in us:
                u["enabled"] = enabled(c, u["id"])
            stats = {"products": c.execute("SELECT COUNT(*) FROM supp_products WHERE archived=0").fetchone()[0],
                     "items": c.execute("SELECT COUNT(*) FROM supp_items WHERE archived_on IS NULL").fetchone()[0],
                     "logs": c.execute("SELECT COUNT(*) FROM supp_logs").fetchone()[0]}
        return {"users": us, "stats": stats}

    class Access(BaseModel):
        user_id: int
        enabled: bool

    @r.put("/admin/supps/access")
    def put_access(request: Request, b: Access):
        me = need_admin(request)
        if b.user_id == me and not b.enabled:
            raise HTTPException(400, "Turn yours off from another admin account, so you don't lock yourself out of this page")
        with db() as c:
            if not c.execute("SELECT 1 FROM users WHERE id=?", (b.user_id,)).fetchone():
                raise HTTPException(404, "No such person")
            set_pref(c, b.user_id, "enabled", bool(b.enabled))
        return {"ok": True}

    app.include_router(r)

    # -------------------------------------------------------------- reminders
    def reminders_due(now):
        """[(uid, payload)] to send at `now`; records what was decided so each slot nudges at most once a day.
        Only people with Supplements and reminders on, within an hour of the slot's time,
        and only if something required in that slot is still unticked."""
        today = now.date().isoformat(); mins = now.hour * 60 + now.minute
        out = []
        with db() as c:
            uids = [x[0] for x in c.execute(
                "SELECT DISTINCT p.user_id FROM push_subs p JOIN users u ON u.id=p.user_id "
                "JOIN supp_prefs s ON s.user_id=p.user_id AND s.key='enabled' AND s.value='true' WHERE u.active=1")]
            for uid in uids:
                p = prefs(c, uid)
                if not p["remind"]:
                    continue
                before = {k: v for k, v in (p["remind_sent"] or {}).items() if v == today}
                sent = dict(before)
                slots, _ = plan_for(c, uid, today, p)
                done = {x[0] for x in c.execute("SELECT item_id FROM supp_logs WHERE user_id=? AND day=?", (uid, today))}
                for s in slots:
                    key = str(s["id"])
                    hh, mm = map(int, s["time"].split(":"))
                    due = hh * 60 + mm
                    if not s["remind"] or sent.get(key) or not (due <= mins < due + 60):
                        continue
                    sent[key] = today
                    left = [i["name"] for i in s["items"] if not i["optional"] and i["id"] not in done]
                    if left:
                        body = ", ".join(left[:4]) + (f" + {len(left) - 4} more" if len(left) > 4 else "")
                        out.append((uid, {"title": f"💊 {s['title']} · {s['time']}", "body": body,
                                          "tag": f"supps-{s['id']}", "url": "/?tab=supps"}))
                if sent != before:
                    set_pref(c, uid, "remind_sent", sent)
        return out

    async def reminder_loop():
        await asyncio.sleep(9)
        while True:
            try:
                for uid, payload in reminders_due(datetime.now(tz)):
                    await asyncio.to_thread(push_user, uid, payload)
            except Exception:  # noqa: BLE001
                log.exception("supplement reminder error")
            await asyncio.sleep(60 - datetime.now().second + 2)

    app.state.supps_reminders_due = reminders_due

    @app.on_event("startup")
    async def _start_reminders():
        asyncio.create_task(reminder_loop())
