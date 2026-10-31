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
