"""One-off import of STACK (schedule.json + db.json) into one person's MyTrack supplements.

    python supps_import.py /data/stack-import Kev            # dry run: prints what would happen
    python supps_import.py /data/stack-import Kev --apply    # backs up mytrack.db, then imports

Also runs once by itself at start-up if /data/stack-import/schedule.json exists and nothing has
been imported yet (see auto_import), so a deploy needs no manual step.

What comes across: supplement products (with every nutrient), the plan (time slots, doses,
optional flags, training/rest days), every tick with its time, training/rest overrides,
the 180 g protein target, and weigh-ins / body fat for days MyTrack doesn't already have.
Shakes (food items linked to a product with calories) become supplements that count in the
food diary from now on; past shake ticks are NOT added to the diary, so past sins don't change.
Left behind: plain meals (they belong in the food diary), the weigh-in item, Stack's
one-off food extras, and the Google Calendar link.
"""
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

try:
    from supps import SCHEMA, clean_macros, clean_nutrients
except ImportError:  # run from elsewhere
    sys.path.insert(0, str(Path(__file__).parent))
    from supps import SCHEMA, clean_macros, clean_nutrients

FLAG = "stack_import"


def _strip_brand(name: str, brand: str) -> str:
    n = (name or "").strip()
    if brand and n.lower().startswith(brand.lower() + " "):
        n = n[len(brand) + 1:].strip()
    return n or (name or "").strip()


def plan_import(schedule: dict, stackdb: dict) -> dict:
    """Work out everything to write, without touching a database."""
    prods_src = {p["id"]: p for p in schedule.get("products", [])}
    products, prod_key_to_idx, items, skipped = [], {}, [], []

    def product_for(it):
        if it.get("productId") and it["productId"] in prods_src:
            p = prods_src[it["productId"]]
            nut = p.get("nutrition") or it.get("nutrition") or {}
            brand, unit = p.get("brand") or nut.get("brand") or "", p.get("unit") or it.get("unit")
            name = _strip_brand(p.get("name") or it["name"], brand)
        else:
            nut = it.get("nutrition") or {}
            brand = nut.get("brand") or it.get("brand") or ""
            name = _strip_brand(nut.get("product") or it["name"], brand)
            unit = it.get("unit")
        key = (brand.lower().strip(), name.lower().strip())
        if key not in prod_key_to_idx:
            prod_key_to_idx[key] = len(products)
            products.append({"name": name, "brand": brand, "barcode": nut.get("barcode"),
                             "serving_text": nut.get("servingText") or "", "unit": unit or "serving",
                             "macros": clean_macros(nut.get("macros")), "nutrients": clean_nutrients(nut.get("nutrients"))})
        return prod_key_to_idx[key]

    for it in schedule.get("items", []):
        kind = it.get("kind", "supplement")
        if kind == "weight":
            skipped.append((it["name"], "weigh-ins live in MyTrack's Progress tab"))
            continue
        if kind == "food":
            has_cals = False
            if it.get("productId") in prods_src:
                m = (prods_src[it["productId"]].get("nutrition") or {}).get("macros") or {}
                has_cals = bool(m.get("kcal") or m.get("protein"))
            if not has_cals:
                skipped.append((it["name"], "a meal — log it in the food diary"))
                continue
        pi = product_for(it)
        items.append({"legacy_id": it["id"], "slot": it["slotId"], "product": pi, "name": it["name"],
                      "dose": it.get("dose") or "", "qty": it.get("qty") or 1, "optional": bool(it.get("optional")),
                      "diary": kind == "food", "added_on": it.get("addedOn"), "archived_on": it.get("archivedOn")})

    used_slots = {i["slot"] for i in items}
    slots = [{"legacy_id": s["id"], "title": s["title"], "time": s["time"], "day_when": s.get("when") or "all",
              "note": s.get("note") or ""} for s in schedule.get("slots", []) if s["id"] in used_slots]
    keep = {i["legacy_id"]: i for i in items}
    logs = []
    for v in (stackdb.get("logs") or {}).values():
        it = keep.get(v.get("itemId"))
        if it and v.get("date") and v.get("time"):
            logs.append({"day": v["date"], "legacy_item": it["legacy_id"], "time": v["time"][:5],
                         "qty": v.get("qty") or it["qty"]})
    return {"products": products, "slots": slots, "items": items, "logs": logs, "skipped": skipped,
            "daytypes": {d: t for d, t in (stackdb.get("plan") or {}).items() if t in ("training", "rest")},
            "weights": stackdb.get("weights") or {}, "bodyfat": stackdb.get("bodyfat") or {},
            "training_days": schedule.get("trainingDays") or [0, 1, 3, 4, 5, 6],
            "protein_target": schedule.get("proteinTargetG")}


def apply_import(con: sqlite3.Connection, uid: int, plan: dict) -> dict:
    c = con
    c.executescript(SCHEMA)
    now = time.time()
    prod_ids = []
    for p in plan["products"]:
        hit = None
        if p["barcode"]:
            hit = c.execute("SELECT id FROM supp_products WHERE barcode=?", (p["barcode"],)).fetchone()
        if not hit:
            hit = c.execute("SELECT id FROM supp_products WHERE lower(name)=? AND lower(brand)=?",
                            (p["name"].lower(), p["brand"].lower())).fetchone()
        if hit:
            prod_ids.append(hit[0]); continue
        c.execute("INSERT INTO supp_products(name,brand,barcode,serving_text,unit,macros,nutrients,created_by,created,updated) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)", (p["name"], p["brand"], p["barcode"], p["serving_text"], p["unit"],
                                                   json.dumps(p["macros"]), json.dumps(p["nutrients"]), uid, now, now))
        prod_ids.append(c.execute("SELECT last_insert_rowid()").fetchone()[0])
    slot_ids = {}
    for s in plan["slots"]:
        c.execute("INSERT INTO supp_slots(user_id,title,time,day_when,note,remind,legacy_id) VALUES (?,?,?,?,?,1,?)",
                  (uid, s["title"], s["time"], s["day_when"], s["note"], s["legacy_id"]))
        slot_ids[s["legacy_id"]] = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    item_ids = {}
    for n, i in enumerate(plan["items"]):
        c.execute("INSERT INTO supp_items(user_id,slot_id,product_id,name,dose,qty,optional,diary,sort,added_on,archived_on,legacy_id) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (uid, slot_ids[i["slot"]], prod_ids[i["product"]], i["name"], i["dose"], i["qty"], int(i["optional"]),
                   int(i["diary"]), n, i["added_on"], i["archived_on"], i["legacy_id"]))
        item_ids[i["legacy_id"]] = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    for l in plan["logs"]:
        c.execute("INSERT OR IGNORE INTO supp_logs(user_id,day,item_id,time,qty,entry_id,at) VALUES (?,?,?,?,?,NULL,?)",
                  (uid, l["day"], item_ids[l["legacy_item"]], l["time"], l["qty"], now))
    for d, t in plan["daytypes"].items():
        c.execute("INSERT OR REPLACE INTO supp_daytype VALUES (?,?,?)", (uid, d, t))
    w_new = w_fat = 0
    for d, kg in plan["weights"].items():
        fat = plan["bodyfat"].get(d)
        row = c.execute("SELECT kg, fat_pct FROM user_weights WHERE user_id=? AND day=?", (uid, d)).fetchone()
        if not row:
            c.execute("INSERT INTO user_weights(user_id, day, kg, source, fat_pct) VALUES (?,?,?,?,?)", (uid, d, kg, "stack", fat))
            w_new += 1
        elif row[1] is None and fat:
            c.execute("UPDATE user_weights SET fat_pct=? WHERE user_id=? AND day=?", (fat, uid, d))
            w_fat += 1
    for k, v in (("enabled", True), ("training_days", plan["training_days"]), ("protein_target", plan["protein_target"])):
        c.execute("INSERT OR REPLACE INTO supp_prefs VALUES (?,?,?)", (uid, k, json.dumps(v)))
    c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (FLAG, json.dumps({"user_id": uid, "at": now})))
    return {"products": len(set(prod_ids)), "slots": len(slot_ids), "items": len(item_ids), "logs": len(plan["logs"]),
            "day_types": len(plan["daytypes"]), "weigh_ins_added": w_new, "body_fat_filled": w_fat}


def summary(plan: dict) -> str:
    out = [f"Products: {len(plan['products'])}"]
    for p in plan["products"]:
        out.append(f"  - {p['brand'] + ' ' if p['brand'] else ''}{p['name']} (per {p['unit']}; {len(p['nutrients'])} actives"
                   + (f"; {p['macros'].get('kcal', 0):g} kcal" if p['macros'] else "") + ")")
    out.append(f"Time slots: {len(plan['slots'])} — " + ", ".join(f"{s['time']} {s['title']}" for s in plan["slots"]))
    out.append(f"Plan items: {len(plan['items'])} ({sum(i['diary'] for i in plan['items'])} count calories in the diary)")
    out.append(f"Ticks: {len(plan['logs'])}  ·  training/rest overrides: {len(plan['daytypes'])}  ·  weigh-ins in Stack: {len(plan['weights'])}")
    out.append(f"Protein target: {plan['protein_target']} g  ·  training days: {plan['training_days']}")
    out.append("Left behind:")
    out += [f"  - {n}: {why}" for n, why in plan["skipped"]]
    return "\n".join(out)


def load(folder: Path):
    return (json.loads((folder / "schedule.json").read_text(encoding="utf-8")),
            json.loads((folder / "db.json").read_text(encoding="utf-8")))


def auto_import(db_path: Path, folder: Path, user_name: str, log=print):
    """Called at start-up: import once if the Stack files are there and it hasn't been done."""
    if not (folder / "schedule.json").exists() or not (folder / "db.json").exists():
        return
    con = sqlite3.connect(db_path)
    try:
        if con.execute("SELECT 1 FROM meta WHERE key=?", (FLAG,)).fetchone():
            return
        u = con.execute("SELECT id FROM users WHERE lower(name)=lower(?) AND active=1", (user_name,)).fetchone()
        if not u:
            log(f"Stack import skipped: no person called {user_name!r}")
            return
        backup = db_path.parent / f"mytrack-before-supps-{int(time.time())}.db"
        bcon = sqlite3.connect(backup)
        with bcon:
            con.backup(bcon)
        bcon.close()
        plan = plan_import(*load(folder))
        with con:
            res = apply_import(con, u[0], plan)
        log(f"Stack import done for {user_name}: {res} (backup: {backup.name})")
    finally:
        con.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    folder, who, apply = Path(sys.argv[1]), sys.argv[2], "--apply" in sys.argv
    plan = plan_import(*load(folder))
    print(summary(plan))
    if apply:
        db_path = Path(__import__("os").environ.get("DATA_DIR", "/data")) / "mytrack.db"
        auto_import(db_path, folder, who)
    else:
        print("\nDry run only. Add --apply to import.")
