# MyTrack — food, calorie & points tracker

Self-hosted food diary with barcode scanning, a daily + weekly points budget,
zero-point foods, calories/macros, water and weigh-ins (stones & lb or kg).
Product data comes from Open Food Facts and is cached locally after the first scan.

## Run it

1. Edit `docker-compose.yml`: set `APP_PIN` (4+ digits) and `APP_SECRET` (any long random string).
2. `docker compose up -d --build`
3. Open http://<server>:2016

Her data lives in `./data/mytrack.db` (SQLite). Back that folder up, or use **Me → Download a backup**.

## Barcode scanning needs HTTPS

Phone browsers only allow the camera on https (or localhost). Put it behind your
Cloudflare Tunnel, e.g. `food.marvelcloud.uk -> http://localhost:2016`, then on her phone:

* **iPhone (Safari):** Share → *Add to Home Screen*
* **Android (Chrome):** ⋮ → *Install app*

It then opens full-screen like a normal app. The barcode number can also be typed by hand.

## Hot-swap the front end

    docker cp app/static/index.html mytrack:/app/static/index.html

No restart needed. After `main.py` changes: `docker cp app/main.py mytrack:/app/main.py && docker restart mytrack`.

## Points

MyTrack uses its own formula: `kcal/60 + sat fat/3 + sugar/6 − protein/8`, rounded, never below 0.
Foods marked zero-point always score 0. The daily allowance and weekly points are set under **Me**.
Anything over the daily allowance comes out of the weekly points, which reset on Monday (or Sunday).

## People & admin

* Everyone has their own diary, weigh-ins, water, budgets and favourites. The food list
  (scanned products + custom foods) is shared by the household.
* The first admin is created from `ADMIN_NAME` / `ADMIN_PIN` in `docker-compose.yml`
  (only when no admin exists yet). After that, manage everything in the app: **Me → Admin**.
* Admin can add people, reset PINs (signs that person out everywhere), pause or delete
  accounts, edit/delete foods, and download a full database backup.
* Anyone can change their own PIN under **Me → Change PIN**.
* Behind Cloudflare Access? Add each new person's email to the Access policy as well.

## Phone notifications

* Each person turns them on under **Me → Notifications** (on iPhone: from the Home Screen app, iOS 16.4+).
* Morning motivation, water reminder (only if behind) and streak saver (only if nothing logged), each with its own time.
* The server's push identity key lives in `data/vapid_private.pem` — keep it with your backups; if it's lost,
  everyone just turns notifications on again.

## Updating

`./app` is mounted into the container and reloads automatically when files change, so app updates go live
without a rebuild. Only a change to `requirements.txt` or the `Dockerfile` needs `docker compose up -d --build`.

## Supplements (merged from Stack)

* Per person, **off by default**. Turn it on for someone in **Me → Admin → Supplements → Supplement products → Who has Supplements**.
  People with it off see no change at all: no Supps tab, no extra cards, no reminders.
* Supps tab: daily checklist by time slot (tap to tick, Log all, swap training/rest day), Breakdown (every active
  ingredient vs daily upper limits), Week (streak, 5-week heatmap, most missed). **Edit my plan** for slots, items,
  phone reminders, protein target and training days.
* Product library (admins with Supplements on): barcode lookup via Open Food Facts (doesn't touch the food list),
  ingredient table per capsule/scoop/shot, optional calories. Items marked "count calories" add a diary entry when ticked.
* Code: `app/supps.py` (API + reminders), `app/static/supps.js` (UI), `app/supps_import.py` (one-off Stack import).
  Data only in `supp_*` tables.
* Stack import runs once at start-up if `data/stack-import/schedule.json` + `db.json` exist (backs up to
  `data/mytrack-before-supps-*.db` first). Re-running it does nothing once done.
* To undo: restore `.backup-before-supps/main.py` and `index.html` into `app/` — the `supp_*` tables are then simply unused.
