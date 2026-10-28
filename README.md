# Gymnazo Ability & Capacity Metrics

A private, simple web app for Gymnazo members to track their own ability and capacity test results over time. Intended home: https://metrics.gymnazo.com

- **Members only ever see their own data.** No leaderboards, no sharing.
- 35 exercises from the studio's Ability & Capacity Metrics list (`exercises.json`), searchable and grouped by category.
- Members pick the metrics they want to follow ("My metrics"), log results (time, weight, reps, distance, height, power, with side / equipment / damper choices), edit or delete entries, and see a line chart, personal best and improvement message per metric.
- Private milestone badges (13), a gentle "ready to test again?" nudge after 90 days, and "Delete my account and data".
- Built for members over 40: 56px+ tap targets, 18px+ text, plain language.
- Branding based on gymnazo.com: brand orange `#d4631c` (sampled from the site logo), charcoal text, white/warm background, Avenir Next-style font stack, logo from the site, tagline "Train for the decades ahead" / "Move With Confidence".

## Search, graphs, notes, import (v2)
- Always-visible search bar on every logged-in screen searches the whole library (name, description, category); Enter opens the first hit, Esc closes. "My metrics" also has search, a category filter and a "recently used" row.
- Every exercise page has a graph: date axis, tappable points (value, date, note), personal-best marker, dashed trend line, First/Latest/Best/Results tiles, 3 months / 1 year / All time, a Compare-all toggle for side/equipment lines, correct lower-is-better handling and mm:ss times. Cards show a sparkline and a "See graph" button.
- Every result has a private note (up to 500 characters), editable any time from the results list or the chart popup.
- Import past results (Me tab or My Progress): download the CSV template, fill in, upload or paste, preview, fix names with a dropdown, confirm. Results keep their original dates, exercises are auto-tracked, badges are awarded once with one summary. Max 2,000 rows per import. "Download my results (CSV)" exports everything including notes in the same format.
- CSV columns: `Date, Exercise, Result, Second result (optional), Side / Hand / Foot, Equipment, Damper, Note`.
- Admin can import a member's history from /admin (choose member by name, preview, confirm). Admin never sees existing scores or notes.

## Stack
Python 3 standard library only (`server.py`), single-file front end (`index.html`), SQLite. No CDNs, no dependencies.

## Run locally
```
PORT=8090 DB_PATH=./metrics.db ADMIN_PASSWORD=choose-one python3 server.py
```
Env vars: `PORT`, `HOST`, `DB_PATH` (SQLite file), `ADMIN_PASSWORD` (enables `/admin`; never commit it).

## Admin (`/admin`)
Protected by `ADMIN_PASSWORD` (sent as the `X-Admin-Password` header). Manage the exercise library (add, edit, hide, reorder, metric types/units, choices), bulk-import exercises from JSON, and see aggregate counts only (members, results, most-tracked exercises). Individual scores are never shown.

## Login
"I'm new" (name + 4-digit PIN, rejects existing names) and "I already signed up" (log in only, never creates an account). PINs are salted + PBKDF2 hashed; 5 wrong tries lock the account for 5 minutes. Forgot a PIN? Members email Memberservice@gymnazo.com or ask any coach.

## Exercise definition format (`exercises.json`)
`name`, `description`, `category`, `primary` `{type, label, unit, better: higher|lower}`, optional `secondary` (+ `optional`), optional `selectors` `[{key,label,options,required,series}]` (series=true gives each choice its own chart/personal best), `headline` (`primary` or `secondary`, which measure drives the chart and personal best). Types: time, weight, reps, distance, height, power, number. Times are stored in seconds.

## Milestones
First result · first personal best · 3 / 5 / 10 results on one metric · tracking 5 / 10 metrics · results in 3 / 6 / 12 different months · 3 months in a row · 10% / 25% improvement.

## Tests
```
python3 tests/test_api.py                                  # API tests (own temp DB)
node --experimental-websocket tools/ui_test.js             # headless Chrome UI test + screenshots (screens/)
```

## Deploy (Render)
`render.yaml` defines a Starter web service named `metrics` with a 1 GB disk at `/var/data`. Set `ADMIN_PASSWORD` in the Render dashboard. Starter plan + disk costs money, so deploy only with the owner's approval.

## Shared login across the Gymnazo apps
The Winter Solstice tracker and the Ability & Capacity Metrics app share name + PIN logins (simple model, not single sign-on): sign up once on either app, then enter the same name and PIN on the other one. Only credentials are shared; members' data stays separate in each app.

- `federation.py` (identical in both repos) talks server to server. Env vars on **both** services: `FEDERATION_SECRET` (same strong random value; never commit it) and `PEER_URL` (the other app's onrender.com address).
- Each app exposes `POST /api/federation/verify`, `/exists` and `/set-pin`, all requiring the `X-Federation-Secret` header (constant-time compare, 401 otherwise, locked for 5 minutes after repeated bad secrets). `verify` answers only `{ok, name, pin_changed}`. Hashes are never returned.
- **Login:** local check first. If the name is unknown locally or the PIN does not match, the peer is asked (3 second timeout). If the peer says OK, the local account is created (if missing) with that PIN, or the local PIN is updated when the peer's `pin_changed` is newer. Otherwise: "That name or PIN doesn't match." and no account is ever created. Wrong PINs count against the name on both sides and lock it after 5 tries; a name locked on the peer is not checked.
- **Register:** the peer is asked whether the name exists; if so the member is told it is taken (log in with the same PIN, or pick a slightly different name). If the peer is unreachable, sign-up pauses with a friendly message (fails closed so two different people cannot end up sharing a name with different PINs). Logging in locally always keeps working.
- **PIN reset:** an admin reset is pushed to the peer (`/api/federation/set-pin`, existing accounts only, newer timestamps only) from a persistent outbox that is retried every 30 seconds until the peer answers, and as a fallback the newer `pin_changed` is pulled lazily at the next login.
- Tests: `python3 tests/test_federation.py` runs both apps against each other (set `SOLSTICE_DIR` / `METRICS_DIR` if the repos are not siblings).

## Momentum partner API (Fitness tests inside Gymnazo Momentum)
Server-to-server only, for the Momentum member app. All `POST /api/partner/*`, header `X-Partner-Secret` = env `MOMENTUM_API_SECRET`
(>= 24 chars; unset = API disabled, every call 401). Bad secrets lock out the calling IP for 5 minutes after 8 tries.
- `verify {name,pin}` -> `{ok,user_id,name}`: the same check and 5-try lockout as logging in here (incl. Solstice federation).
- `register {name,pin}` -> creates a Metrics account exactly like `/api/register` (no session/cookie is issued).
- `summary {user_id}`, `metric {user_id,exercise_id}`: read-only, same data as `/api/home`, `/api/exercises`, `/api/metric`.
- `results {user_id,exercise_id,...}`: logs through the same `add_result()` validation members use here.
- `health`: reports the one-time startup DB snapshot (`backups/metrics.db.pre-momentum-link.bak` next to the DB).
Member login, sessions and write paths on this site are unchanged. Tests: `python3 tests/test_partner.py`.
