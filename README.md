# Gymnazo Ability & Capacity Metrics

A private, simple web app for Gymnazo members to track their own ability and capacity test results over time. Intended home: https://metrics.gymnazo.com

- **Members only ever see their own data.** No leaderboards, no sharing.
- 35 exercises from the studio's Ability & Capacity Metrics list (`exercises.json`), searchable and grouped by category.
- Members pick the metrics they want to follow ("My metrics"), log results (time, weight, reps, distance, height, power, with side / equipment / damper choices), edit or delete entries, and see a line chart, personal best and improvement message per metric.
- Private milestone badges (13), a gentle "ready to test again?" nudge after 90 days, and "Delete my account and data".
- Built for members over 40: 56px+ tap targets, 18px+ text, plain language.
- Branding based on gymnazo.com: brand orange `#d4631c` (sampled from the site logo), charcoal text, white/warm background, Avenir Next-style font stack, logo from the site, tagline "Train for the decades ahead" / "Move With Confidence".

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
"I'm new" (name + 4-digit PIN, rejects existing names) and "I already signed up" (log in only, never creates an account). PINs are salted + PBKDF2 hashed; 5 wrong tries lock the account for 5 minutes. Forgot a PIN? Ask the studio.

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
