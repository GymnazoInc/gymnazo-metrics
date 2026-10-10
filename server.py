"""Gymnazo Ability & Capacity Metrics: stdlib-only server (SQLite). Run: PORT=8090 DB_PATH=./metrics.db python3 server.py"""
import http.server, socketserver, sqlite3, json, hashlib, hmac, secrets, os, time, re, math, threading, csv, io, difflib
from datetime import date, datetime, timedelta
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("DB_PATH") or os.path.join(HERE, "metrics.db")
PORT = int(os.environ.get("PORT", 8090))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
TYPES = ("time", "weight", "reps", "distance", "height", "power", "number")
LOCK = threading.RLock()

db = sqlite3.connect(DB, check_same_thread=False, isolation_level=None)
db.execute("PRAGMA journal_mode=WAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, name_key TEXT UNIQUE, salt TEXT, pin_hash TEXT, created REAL, fails INTEGER DEFAULT 0, locked_until REAL DEFAULT 0, picks_saved INTEGER DEFAULT 0, hide_nudge_until REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER, created REAL);
CREATE TABLE IF NOT EXISTS exercises(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, description TEXT, category TEXT, sort INTEGER DEFAULT 0, active INTEGER DEFAULT 1, def TEXT);
CREATE TABLE IF NOT EXISTS tracked(user_id INTEGER, exercise_id INTEGER, created REAL, PRIMARY KEY(user_id, exercise_id));
CREATE TABLE IF NOT EXISTS results(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, exercise_id INTEGER, day TEXT, pval REAL, sval REAL, selectors TEXT, note TEXT, created REAL);
CREATE INDEX IF NOT EXISTS results_ue ON results(user_id, exercise_id);
CREATE TABLE IF NOT EXISTS badges(user_id INTEGER, code TEXT, created REAL, PRIMARY KEY(user_id, code));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
""")

def today():
    o = os.environ.get("MX_TEST_TODAY")
    return date.fromisoformat(o) if o else datetime.now(TZ).date()

# ---------- seed ----------
def seed():
    if db.execute("SELECT 1 FROM meta WHERE k='seeded'").fetchone(): return
    if db.execute("SELECT COUNT(*) FROM exercises").fetchone()[0] == 0:
        data = json.load(open(os.path.join(HERE, "exercises.json"), encoding="utf-8"))
        for e in data["exercises"]:
            d = {k: e.get(k) for k in ("primary", "secondary", "selectors", "headline")}
            db.execute("INSERT INTO exercises(name,description,category,sort,active,def) VALUES(?,?,?,?,1,?)",
                       (e["name"], e["description"], e["category"], e.get("sort", 0), json.dumps(d)))
    db.execute("INSERT OR REPLACE INTO meta VALUES('seeded','1')")
seed()

# ---------- helpers ----------
def hp(pin, salt): return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200000).hex()
def th(t): return hashlib.sha256(t.encode()).hexdigest()
def clean(s, n):
    s = re.sub(r"<[^>]*>", "", str(s or "")); return re.sub(r"\s+", " ", s).strip()[:n]

def validate_def(d):
    """Return (clean_def, error)."""
    try:
        def measure(m, secondary=False):
            t = m.get("type")
            if t not in TYPES: raise ValueError("Pick a valid measure type.")
            out = {"type": t, "label": clean(m.get("label"), 80) or t.title(), "unit": clean(m.get("unit"), 12) or ("sec" if t == "time" else "")}
            if secondary: out["optional"] = bool(m.get("optional", True))
            else: out["better"] = m.get("better") if m.get("better") in ("higher", "lower") else "higher"
            return out
        p = measure(d["primary"])
        s = measure(d["secondary"], True) if d.get("secondary") else None
        if s: s["better"] = d["secondary"].get("better") if d["secondary"].get("better") in ("higher", "lower") else "higher"
        sels = []
        for x in (d.get("selectors") or [])[:4]:
            opts = [clean(o, 30) for o in x.get("options", []) if clean(o, 30)][:12]
            key = re.sub(r"[^a-z0-9_]", "", str(x.get("key", "")).lower())[:20]
            if not key or len(opts) < 2: raise ValueError("Each choice needs a short key and at least 2 options.")
            sels.append({"key": key, "label": clean(x.get("label"), 80) or key, "options": opts, "required": bool(x.get("required", True)), "series": bool(x.get("series", True))})
        if len({x["key"] for x in sels}) != len(sels): raise ValueError("Choice keys must be different.")
        h = d.get("headline") if d.get("headline") in ("primary", "secondary") else "primary"
        if h == "secondary" and not s: h = "primary"
        return {"primary": p, "secondary": s, "selectors": sels, "headline": h}, None
    except ValueError as e: return None, str(e)
    except Exception: return None, "That exercise definition isn't valid."

def ex_row(r, full=True):
    return {"id": r[0], "name": r[1], "description": r[2], "category": r[3], "sort": r[4], "active": bool(r[5]), "def": json.loads(r[6])}

def get_ex(eid):
    r = db.execute("SELECT id,name,description,category,sort,active,def FROM exercises WHERE id=?", (eid,)).fetchone()
    return ex_row(r) if r else None

def headline_of(d, pv, sv):
    return sv if d.get("headline") == "secondary" else pv

def better_of(d):
    h = d.get("headline") or "primary"
    return (d.get("secondary") or {}).get("better", "higher") if h == "secondary" else d["primary"]["better"]

def series_key(d, sel):
    parts = [str(sel.get(x["key"], "")) for x in d["selectors"] if x["series"] and sel.get(x["key"])]
    return " · ".join(parts)

def beats(better, a, b):  # is a strictly better than b
    return a > b if better == "higher" else a < b

def load_results(uid, eid):
    rows = db.execute("SELECT id,day,pval,sval,selectors,note,created FROM results WHERE user_id=? AND exercise_id=? ORDER BY day,id", (uid, eid)).fetchall()
    return [{"id": r[0], "day": r[1], "p": r[2], "s": r[3], "sel": json.loads(r[4] or "{}"), "note": r[5] or "", "created": r[6]} for r in rows]

def analyse(d, results):
    """Annotate results (is_pb) and build per-series summaries."""
    better = better_of(d); series = {}
    for r in results:
        r["series"] = series_key(d, r["sel"])
        r["h"] = headline_of(d, r["p"], r["s"])
        g = series.setdefault(r["series"], {"label": r["series"], "results": [], "best": None})
        r["is_pb"] = False
        if r["h"] is not None:
            if g["best"] is not None and beats(better, r["h"], g["best"]["h"]): r["is_pb"] = True
            if g["best"] is None or beats(better, r["h"], g["best"]["h"]): g["best"] = r
        g["results"].append(r)
    out = {}
    for k, g in series.items():
        vals = [r for r in g["results"] if r["h"] is not None]
        if not vals: continue
        first, latest = vals[0], vals[-1]
        prev = vals[-2] if len(vals) > 1 else None
        if prev is None: trend = "new"
        elif latest["h"] == prev["h"]: trend = "same"
        else: trend = "up" if beats(better, latest["h"], prev["h"]) else "down"
        out[k] = {"label": k, "count": len(vals), "first": {"day": first["day"], "value": first["h"]}, "latest": {"day": latest["day"], "value": latest["h"], "id": latest["id"]},
                  "best": {"day": g["best"]["day"], "value": g["best"]["h"], "id": g["best"]["id"]}, "trend": trend}
    return out

# ---------- badges ----------
BADGES = [
    ("first_result", "First result logged", "You logged your very first result. The hardest step is done!", "🌱"),
    ("first_pb", "First personal best", "You beat your own best on a metric. That is real progress!", "⭐"),
    ("results3", "Getting started", "3 results logged on one metric.", "🥉"),
    ("results5", "Building a habit", "5 results logged on one metric.", "🥈"),
    ("results10", "Committed", "10 results logged on one metric.", "🥇"),
    ("track5", "Curious mover", "You are tracking 5 different metrics.", "🧭"),
    ("track10", "Well rounded", "You are tracking 10 different metrics.", "🌟"),
    ("months3", "Three months strong", "You logged results in 3 different months.", "📅"),
    ("months6", "Half a year", "You logged results in 6 different months.", "🏔️"),
    ("months12", "A full year", "You logged results in 12 different months.", "🏆"),
    ("streak3", "Three months in a row", "You logged results 3 months in a row.", "🔥"),
    ("improve10", "10% better", "You improved a metric by 10% since your first result.", "📈"),
    ("improve25", "25% better", "You improved a metric by 25% since your first result.", "🚀"),
]
BADGE_MAP = {b[0]: b for b in BADGES}
def badge_obj(code): b = BADGE_MAP[code]; return {"code": b[0], "title": b[1], "desc": b[2], "icon": b[3]}

def month_index(day): y, m = int(day[:4]), int(day[5:7]); return y * 12 + m

def evaluate_badges(uid):
    have = {r[0] for r in db.execute("SELECT code FROM badges WHERE user_id=?", (uid,))}
    earned = set()
    rows = db.execute("SELECT exercise_id,day FROM results WHERE user_id=?", (uid,)).fetchall()
    if rows: earned.add("first_result")
    per = {}
    for eid, _ in rows: per[eid] = per.get(eid, 0) + 1
    mx = max(per.values()) if per else 0
    for n in (3, 5, 10):
        if mx >= n: earned.add("results%d" % n)
    nt = db.execute("SELECT COUNT(*) FROM tracked WHERE user_id=?", (uid,)).fetchone()[0]
    for n in (5, 10):
        if nt >= n: earned.add("track%d" % n)
    months = sorted({month_index(d) for _, d in rows})
    for n in (3, 6, 12):
        if len(months) >= n: earned.add("months%d" % n)
    run = best = 0; prev = None
    for m in months:
        run = run + 1 if prev is not None and m == prev + 1 else 1
        best = max(best, run); prev = m
    if best >= 3: earned.add("streak3")
    for eid in per:
        ex = get_ex(eid)
        if not ex: continue
        d = ex["def"]; res = load_results(uid, eid); sm = analyse(d, res); better = better_of(d)
        if any(r["is_pb"] for r in res): earned.add("first_pb")
        for g in sm.values():
            f, b = g["first"]["value"], g["best"]["value"]
            if g["count"] >= 2 and f:
                pct = (b - f) / abs(f) if better == "higher" else (f - b) / abs(f)
                if pct >= 0.10: earned.add("improve10")
                if pct >= 0.25: earned.add("improve25")
    new = [c for c, *_ in BADGES if c in earned and c not in have]
    now = time.time()
    for c in new: db.execute("INSERT OR IGNORE INTO badges VALUES(?,?,?)", (uid, c, now))
    return [badge_obj(c) for c in new]

# ---------- result validation ----------
def parse_measure(m, v, required):
    if v in (None, ""):
        return (None, None) if not required else (None, "Please fill in " + m["label"].lower().rstrip("?") + ".")
    try: x = float(v)
    except Exception: return None, "Please type a number for " + m["label"].lower().rstrip("?") + "."
    if not math.isfinite(x) or x <= 0: return None, "Please type a number bigger than 0."
    if m["type"] == "time":
        if x > 86400: return None, "That time looks too long. Please check it."
        return int(round(x)), None
    if x > 100000: return None, "That number looks too big. Please check it."
    return round(x, 2), None

def parse_result(d, b):
    sel_in = b.get("selectors") or {}; sel = {}
    for x in d["selectors"]:
        v = sel_in.get(x["key"])
        if v in (None, ""):
            if x["required"]: return None, "Please choose: " + x["label"]
            continue
        if v not in x["options"]: return None, "Please choose one of the buttons for: " + x["label"]
        sel[x["key"]] = v
    p, e = parse_measure(d["primary"], b.get("primary"), True)
    if e: return None, e
    s = None
    if d.get("secondary"):
        s, e = parse_measure(d["secondary"], b.get("secondary"), not d["secondary"].get("optional", True))
        if e: return None, e
    try: dy = date.fromisoformat(str(b.get("day") or today().isoformat()))
    except Exception: return None, "Please pick a valid date."
    if dy > today() or dy < date(2000, 1, 1): return None, "Please pick today or an earlier date."
    return {"p": p, "s": s, "sel": sel, "day": dy.isoformat(), "note": clean(b.get("note"), 500)}, None


# ---------- CSV import / export ----------
MAX_IMPORT_ROWS = 2000
IMPORT_COLS = ["Date", "Exercise", "Result", "Second result (optional)", "Side / Hand / Foot", "Equipment", "Damper", "Note"]

def norm(s): return re.sub(r"[^a-z0-9]", "", str(s).casefold().replace("º", "").replace("&", "and"))

def fmt_time(sec):
    sec = int(round(sec)); h, m, x = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"
def fmt_num(v): return ("%g" % round(v, 2))

def parse_date(t):
    t = str(t or "").strip().replace(",", " "); t = re.sub(r"\s+", " ", t)
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    for f in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y", "%Y%m%d", "%m.%d.%Y"):
        try: return datetime.strptime(t, f).date()
        except Exception: pass
    return None

def parse_time_cell(t):
    t = str(t or "").strip().lower()
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}(\.\d+)?", t):
        parts = [float(x) for x in t.split(":")]
        if any(x >= 60 for x in parts[1:]): return None
        sec = 0
        for x in parts: sec = sec * 60 + x
        return sec
    m = re.fullmatch(r"(?:(\d+)\s*(?:h|hr|hrs|hours?))?\s*(?:(\d+)\s*(?:m|min|mins|minutes?))?\s*(?:(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds?))?", t)
    if m and any(m.groups()): return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)", t)
    return float(m.group(1)) if m else None

def parse_num_cell(t):
    m = re.search(r"\d+(?:\.\d+)?", str(t or "").replace(",", ""))
    return float(m.group(0)) if m else None

def match_option(options, v):
    v = str(v or "").strip()
    if not v: return None
    try:
        f = float(v)
        if f == int(f): v = str(int(f))
    except Exception: pass
    for o in options:
        if o.casefold() == v.casefold(): return o
    nv = norm(v)
    if not nv: return None
    c = [o for o in options if norm(o).startswith(nv) or nv in norm(o)]
    return c[0] if len(c) == 1 else None

def match_exercise(q, cands):
    nq = norm(q)
    if not nq: return None, []
    ex = [c for c in cands if c[2] == nq]
    if len(ex) == 1: return ex[0][0], []
    toks = [t for t in re.findall(r"[a-z0-9]+", str(q).casefold()) if len(t) >= 2]
    con = [c for c in cands if len(nq) >= 4 and nq in c[2]] or [c for c in cands if toks and all(t in c[2] for t in toks)]
    if len(con) == 1: return con[0][0], []
    rev = [c for c in cands if len(c[2]) >= 6 and c[2] in nq]
    if len(rev) == 1: return rev[0][0], []
    sc = sorted(((max(difflib.SequenceMatcher(None, nq, c[2]).ratio(), difflib.SequenceMatcher(None, nq, c[2][:len(nq) + 4]).ratio() * .9), c) for c in cands), key=lambda x: -x[0])
    sug, seen = [], set()
    for c in con + [x[1] for x in sc if x[0] >= .45]:
        if c[0] not in seen: seen.add(c[0]); sug.append({"id": c[0], "name": c[1]})
    return None, sug[:3]

def colkey(h):
    h = norm(h)
    if h.startswith("date") or h == "day" or h == "when": return "date"
    if h in ("exercise", "exercisename", "metric", "name", "test", "movement"): return "ex"
    if "second" in h or "extra" in h or h in ("result2", "secondary"): return "s"
    if h.startswith("result") or h in ("score", "value", "primary", "time", "weight", "reps", "result1"): return "p"
    if "equip" in h: return "equipment"
    if "damper" in h: return "damper"
    if "side" in h or "hand" in h or "foot" in h or "leg" in h: return "side"
    if "note" in h or "comment" in h: return "note"
    return None

def import_template():
    out = io.StringIO(); w = csv.writer(out); w.writerow(IMPORT_COLS)
    for r in [["2025-03-14", "Grip Strength", "72", "", "Right Hand", "", "", "Felt strong today"],
              ["3/14/2025", "Gymnazo Mile", "9:45", "", "", "", "", "Windy day"],
              ["March 14 2025", "Row Machine - 500m Row", "1:58", "", "", "", "5", ""],
              ["2025-04-02", "Overhead Press (in 1 minute)", "45", "20", "", "Barbell", "", "20 reps, last 5 were hard"],
              ["2025-04-02", "Single Leg Balance", "75", "10", "Left Leg", "", "", "75 seconds, holding 10 lb"],
              ["2025-05-20", "Pull-ups", "8", "", "", "", "", "With a light band"]]: w.writerow(r)
    return out.getvalue()

def import_reference():
    out = io.StringIO(); w = csv.writer(out); w.writerow(["Exercise (copy this name)", "Result is", "Second result is", "Choices to fill in"])
    for r in db.execute("SELECT id,name,description,category,sort,active,def FROM exercises WHERE active=1 ORDER BY sort,id"):
        e = ex_row(r); d = e["def"]
        def hint(m): return "time as mm:ss, h:mm:ss or seconds" if m["type"] == "time" else (m["label"] + (" (" + m["unit"] + ")" if m["unit"] else ""))
        w.writerow([e["name"], hint(d["primary"]), (hint(d["secondary"]) + (" (optional)" if d["secondary"].get("optional", True) else "")) if d["secondary"] else "",
                    "; ".join(x["label"] + ": " + " / ".join(x["options"]) + ("" if x["required"] else " (optional)") for x in d["selectors"])])
    return out.getvalue()

def cell_safe(v):
    v = str(v or ""); return "'" + v if v[:1] in ("=", "+", "-", "@") else v

def export_csv(uid):
    out = io.StringIO(); w = csv.writer(out); w.writerow(IMPORT_COLS)
    rows = db.execute("SELECT r.day,e.name,r.pval,r.sval,r.selectors,r.note,e.def FROM results r JOIN exercises e ON e.id=r.exercise_id WHERE r.user_id=? ORDER BY r.day,r.id", (uid,)).fetchall()
    for day, name, pv, sv, sel, note, dj in rows:
        d = json.loads(dj); sel = json.loads(sel or "{}")
        f = lambda m, v: "" if v is None else (fmt_time(v) if m["type"] == "time" else fmt_num(v))
        side = next((sel[x["key"]] for x in d["selectors"] if x["key"] not in ("equipment", "damper") and x["key"] in sel), "")
        w.writerow([day, cell_safe(name), f(d["primary"], pv), f(d["secondary"], sv) if d.get("secondary") else "", side, sel.get("equipment", ""), sel.get("damper", ""), cell_safe(note)])
    return out.getvalue()

def parse_import(uid, text):
    text = str(text or "").lstrip("\ufeff")
    if len(text) > 1400000: return {"error": "That file is too big. Please import fewer rows at a time."}
    lines = text.splitlines(); first = next((l for l in lines if l.strip()), "")
    delim = max([",", "\t", ";"], key=lambda c: first.count(c))
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    cmap = {"date": 0, "ex": 1, "p": 2, "s": 3, "side": 4, "equipment": 5, "damper": 6, "note": 7}; start = 0
    if rows:
        hk = [colkey(h) for h in rows[0]]
        if "date" in hk and "ex" in hk: cmap = {k: i for i, k in enumerate(hk) if k and k not in {}}; cmap = {k: hk.index(k) for k in set(hk) if k}; start = 1
    data = [(i + 1, r) for i, r in enumerate(rows) if i >= start and any(c.strip() for c in r) and not (r[0].strip().startswith("#"))]
    if len(data) > MAX_IMPORT_ROWS: return {"error": f"That's {len(data)} rows. Please import at most {MAX_IMPORT_ROWS:,} rows at a time and split the rest into another file."}
    if not data: return {"error": "We couldn't find any rows. Paste rows or choose a CSV file that has a Date and an Exercise column."}
    return {"data": data, "cmap": cmap}

def run_import(uid, b, confirm):
    pr = parse_import(uid, b.get("text"))
    if "error" in pr: return pr
    fixes = {}
    for k, v in (b.get("fixes") or {}).items():
        try: fixes[int(k)] = int(v)
        except Exception: pass
    exrows = db.execute("SELECT id,name,description,category,sort,active,def FROM exercises WHERE active=1 ORDER BY sort,id").fetchall()
    exs = {r[0]: ex_row(r) for r in exrows}; cands = [(i, e["name"], norm(e["name"])) for i, e in exs.items()]
    existing = set()
    for eid, day, pv, sv, sel in db.execute("SELECT exercise_id,day,pval,sval,selectors FROM results WHERE user_id=?", (uid,)):
        existing.add((eid, day, pv, sv, json.dumps(json.loads(sel or "{}"), sort_keys=True)))
    out = []; ok_rows = []; today_d = today()
    for rn, r in pr["data"]:
        g = lambda k: (r[pr["cmap"][k]].strip() if k in pr["cmap"] and pr["cmap"][k] < len(r) else "")
        row = {"row": rn, "exercise_input": g("ex"), "status": "problem", "suggestions": []}
        def bad(msg): row["error"] = msg; out.append(row)
        eid = fixes.get(rn) if fixes.get(rn) in exs else None; sug = []
        if eid is None: eid, sug = match_exercise(g("ex"), cands)
        if eid is None:
            row["suggestions"] = sug
            if not g("ex"): bad("There's no exercise name on this row.")
            else: bad(f"We couldn't find an exercise called \"{g('ex')[:60]}\"." + (f" Did you mean {sug[0]['name']}?" if sug else " Please choose the right one."))
            continue
        e = exs[eid]; d = e["def"]; row.update(exercise_id=eid, exercise=e["name"])
        dy = parse_date(g("date"))
        if not dy: bad(f"We couldn't read the date \"{g('date')[:30]}\". Try 2025-03-14 or 3/14/2025."); continue
        if dy > today_d or dy < date(2000, 1, 1): bad("That date is in the future or too long ago. Please check it."); continue
        row["day"] = dy.isoformat()
        def val(m, cell, required):
            if not cell.strip():
                return (None, None) if not required else (None, f"Please fill in the result for {e['name']}.")
            v = parse_time_cell(cell) if m["type"] == "time" else parse_num_cell(cell)
            if v is None: return None, f"We couldn't read the result \"{cell[:30]}\"." + (" Use minutes and seconds like 8:42." if m["type"] == "time" else " Please use a number.")
            return parse_measure(m, v, required)
        p, err = val(d["primary"], g("result") if False else g("p"), True)
        if err: bad(err); continue
        s = None
        if d.get("secondary") and g("s"):
            s, err = val(d["secondary"], g("s"), False)
            if err: bad(err); continue
        elif d.get("secondary") and not d["secondary"].get("optional", True): bad(f"Please add the second result ({d['secondary']['label']})."); continue
        sel = {}; err = None
        for x in d["selectors"]:
            raw = g("equipment") if x["key"] == "equipment" else g("damper") if x["key"] == "damper" else g("side")
            if not raw:
                if x["required"]: err = f"Please add: {x['label']} ({' / '.join(x['options'])})."; break
                continue
            o = match_option(x["options"], raw)
            if not o: err = f"\"{raw[:20]}\" isn't one of the choices for {x['label']}. Use: {' / '.join(x['options'])}."; break
            sel[x["key"]] = o
        if err: bad(err); continue
        note = g("note")
        if re.match(r"^'[=+\-@]", note): note = note[1:]
        row.update(p=p, s=s, sel=sel, note=clean(note, 500))
        key = (eid, row["day"], p, s, json.dumps(sel, sort_keys=True))
        if key in existing: row["status"] = "duplicate"
        else: row["status"] = "ok"; existing.add(key); ok_rows.append(row)
        out.append(row)
    counts = {k: sum(1 for x in out if x["status"] == k) for k in ("ok", "duplicate", "problem")}
    if not confirm: return {"rows": out, "counts": counts}
    now = time.time(); db.execute("BEGIN")
    try:
        for x in ok_rows:
            db.execute("INSERT INTO results(user_id,exercise_id,day,pval,sval,selectors,note,created) VALUES(?,?,?,?,?,?,?,?)", (uid, x["exercise_id"], x["day"], x["p"], x["s"], json.dumps(x["sel"]), x["note"], now))
            db.execute("INSERT OR IGNORE INTO tracked VALUES(?,?,?)", (uid, x["exercise_id"], now))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK"); return {"error": "Something went wrong and nothing was imported. Please try again."}
    return {"imported": len(ok_rows), "counts": counts, "newBadges": evaluate_badges(uid) if ok_rows else []}

# ---------- handler ----------
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def send(self, code, obj, ctype="application/json", cookie=None, extra=None):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store"); self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if cookie: self.send_header("Set-Cookie", cookie)
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)
    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1500000: return {}
        try: return json.loads(self.rfile.read(n) or b"{}")
        except Exception: return {}
    def user(self):
        m = re.search(r"mx=([A-Za-z0-9_\-]+)", self.headers.get("Cookie", ""))
        if not m: return None
        return db.execute("SELECT u.id,u.name,u.picks_saved FROM sessions s JOIN users u ON u.id=s.user_id WHERE token_hash=?", (th(m.group(1)),)).fetchone()
    def static(self, fname, ctype):
        return self.send(200, open(os.path.join(HERE, fname), "rb").read(), ctype)

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path); p = u.path; q = parse_qs(u.query)
        with LOCK:
            if p in ("/", "/index.html"): return self.static("index.html", "text/html; charset=utf-8")
            if p == "/admin": return self.static("admin.html", "text/html; charset=utf-8")
            if p == "/logo.png": return self.static("logo.png", "image/png")
            if p == "/logo-white.png": return self.static("logo-white.png", "image/png")
            if p == "/favicon.ico": return self.send(204, b"", "image/x-icon")
            usr = self.user()
            if not usr: return self.send(401, {"error": "auth"})
            if p == "/api/me": return self.send(200, self.me(usr))
            if p == "/api/exercises": return self.send(200, self.library(usr))
            if p == "/api/home": return self.send(200, self.home(usr))
            if p == "/api/badges": return self.send(200, self.badges(usr))
            if p == "/api/import/template": return self.send(200, import_template().encode("utf-8-sig"), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="gymnazo-results-template.csv"'})
            if p == "/api/import/reference": return self.send(200, import_reference().encode("utf-8-sig"), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="gymnazo-exercise-names.csv"'})
            if p == "/api/export.csv": return self.send(200, export_csv(usr[0]).encode("utf-8-sig"), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="my-gymnazo-results.csv"'})
            if p == "/api/metric":
                try: eid = int(q.get("exercise_id", [0])[0])
                except Exception: return self.send(400, {"error": "bad request"})
                return self.metric(usr, eid)
        self.send(404, {"error": "nf"})

    def me(self, usr):
        tr = [r[0] for r in db.execute("SELECT exercise_id FROM tracked WHERE user_id=?", (usr[0],))]
        return {"name": usr[1], "picksSaved": bool(usr[2]), "tracked": tr, "today": today().isoformat()}

    def library(self, usr):
        tr = {r[0] for r in db.execute("SELECT exercise_id FROM tracked WHERE user_id=?", (usr[0],))}
        cnt = {r[0]: r[1] for r in db.execute("SELECT exercise_id,COUNT(*) FROM results WHERE user_id=? GROUP BY exercise_id", (usr[0],))}
        rows = db.execute("SELECT id,name,description,category,sort,active,def FROM exercises WHERE active=1 ORDER BY sort,id").fetchall()
        out = []
        for r in rows:
            e = ex_row(r); e["tracked"] = e["id"] in tr; e["results"] = cnt.get(e["id"], 0); del e["active"]; out.append(e)
        order = []
        for e in out:
            if e["category"] not in order: order.append(e["category"])
        return {"exercises": out, "categories": order}

    def home(self, usr):
        uid = usr[0]; items = []; now = today(); retest = []
        for (eid,) in db.execute("SELECT exercise_id FROM tracked WHERE user_id=? ORDER BY created", (uid,)).fetchall():
            ex = get_ex(eid)
            if not ex or not ex["active"]: continue
            res = load_results(uid, eid); sm = analyse(ex["def"], res)
            item = {"id": eid, "name": ex["name"], "category": ex["category"], "def": ex["def"], "count": len(res), "latest": None}
            if res:
                last = res[-1]; g = sm.get(last["series"]) or next(iter(sm.values()), None)
                if g:
                    item["latest"] = {"day": last["day"], "value": g["latest"]["value"], "series": g["label"], "p": last["p"], "s": last["s"], "sel": last["sel"]}
                    item["best"] = g["best"]["value"]; item["trend"] = g["trend"]
                    item["isBest"] = g["latest"]["id"] == g["best"]["id"] and g["count"] > 1
                    item["spark"] = [r["h"] for r in res if r["series"] == last["series"] and r["h"] is not None][-14:]
                    item["better"] = better_of(ex["def"])
                    item["lastLogged"] = max(r["created"] for r in res)
                age = (now - date.fromisoformat(last["day"])).days
                if age >= 90: retest.append({"id": eid, "name": ex["name"], "days": age})
            items.append(item)
        hidden = db.execute("SELECT hide_nudge_until FROM users WHERE id=?", (uid,)).fetchone()[0] > time.time()
        recent = [{"id": m["id"], "name": m["name"]} for m in sorted([m for m in items if m.get("lastLogged")], key=lambda m: -m["lastLogged"])[:5]]
        return {"metrics": items, "retest": [] if hidden else retest, "recent": recent}

    def metric(self, usr, eid):
        ex = get_ex(eid)
        if not ex: return self.send(404, {"error": "That exercise isn't available."})
        res = load_results(usr[0], eid); sm = analyse(ex["def"], res)
        tracked = bool(db.execute("SELECT 1 FROM tracked WHERE user_id=? AND exercise_id=?", (usr[0], eid)).fetchone())
        return self.send(200, {"exercise": {k: ex[k] for k in ("id", "name", "description", "category", "def")}, "tracked": tracked,
                               "results": [{"id": r["id"], "day": r["day"], "p": r["p"], "s": r["s"], "sel": r["sel"], "note": r["note"], "series": r["series"], "h": r["h"], "isPb": r["is_pb"]} for r in res],
                               "series": list(sm.values()), "better": better_of(ex["def"])})

    def badges(self, usr):
        got = {r[0]: r[1] for r in db.execute("SELECT code,created FROM badges WHERE user_id=?", (usr[0],))}
        return {"badges": [dict(badge_obj(c), earned=c in got, at=got.get(c)) for c, *_ in BADGES]}

    # ---- POST ----
    def do_POST(self):
        p = urlparse(self.path).path; b = self.body()
        with LOCK:
            if p.startswith("/api/admin/"): return self.admin(p, b)
            if p == "/api/login": return self.login(b)
            if p == "/api/register": return self.register(b)
            if p == "/api/logout":
                m = re.search(r"mx=([A-Za-z0-9_\-]+)", self.headers.get("Cookie", ""))
                if m: db.execute("DELETE FROM sessions WHERE token_hash=?", (th(m.group(1)),))
                return self.send(200, {"ok": 1}, cookie="mx=; Path=/; Max-Age=0")
            usr = self.user()
            if not usr: return self.send(401, {"error": "auth"})
            if p == "/api/track": return self.track(usr, b)
            if p == "/api/track/toggle": return self.toggle(usr, b)
            if p == "/api/results": return self.add_result(usr, b)
            if p == "/api/results/update": return self.update_result(usr, b)
            if p == "/api/results/delete": return self.delete_result(usr, b)
            if p == "/api/results/note": return self.set_note(usr, b)
            if p in ("/api/import/preview", "/api/import/confirm"):
                r = run_import(usr[0], b, p.endswith("confirm"))
                return self.send(400 if "error" in r else 200, r)
            if p == "/api/nudge/hide":
                db.execute("UPDATE users SET hide_nudge_until=? WHERE id=?", (time.time() + 30 * 86400, usr[0])); return self.send(200, {"ok": 1})
            if p == "/api/account/delete": return self.delete_account(usr, b)
        self.send(404, {"error": "nf"})

    # auth
    def _parse(self, b):
        name = re.sub(r"\s+", " ", str(b.get("name", "")).strip())[:24]; pin = str(b.get("pin", ""))
        if not name or not re.fullmatch(r"\d{4}", pin):
            self.send(400, {"error": "Please type your name and a 4-digit PIN (numbers only)."}); return None
        return name, pin
    def _session(self, uid, new):
        tok = secrets.token_urlsafe(32)
        db.execute("INSERT INTO sessions VALUES(?,?,?)", (th(tok), uid, time.time()))
        r = db.execute("SELECT id,name,picks_saved FROM users WHERE id=?", (uid,)).fetchone()
        self.send(200, dict(self.me(r), new=new), cookie=f"mx={tok}; Path=/; Max-Age=7776000; HttpOnly; SameSite=Lax; Secure")
    def login(self, b):  # log in only: never creates an account
        p = self._parse(b)
        if not p: return
        name, pin = p; now = time.time()
        bad = {"error": "That name or PIN doesn't match. Please try again."}
        r = db.execute("SELECT id,salt,pin_hash,fails,locked_until FROM users WHERE name_key=?", (name.casefold(),)).fetchone()
        if not r: return self.send(401, bad)
        if r[4] > now: return self.send(429, {"error": "Too many tries. Please wait 5 minutes, then try again. Forgot your PIN? Ask the studio."})
        if not hmac.compare_digest(hp(pin, r[1]), r[2]):
            f = r[3] + 1
            if f >= 5: db.execute("UPDATE users SET fails=0,locked_until=? WHERE id=?", (now + 300, r[0]))
            else: db.execute("UPDATE users SET fails=? WHERE id=?", (f, r[0]))
            return self.send(401, bad)
        db.execute("UPDATE users SET fails=0 WHERE id=?", (r[0],)); self._session(r[0], False)
    def register(self, b):
        p = self._parse(b)
        if not p: return
        name, pin = p
        if db.execute("SELECT 1 FROM users WHERE name_key=?", (name.casefold(),)).fetchone():
            return self.send(409, {"error": "That name is already taken. If that's you, tap \"Log in\". If not, please pick a slightly different name (like adding your last initial)."})
        salt = secrets.token_hex(16)
        cur = db.execute("INSERT INTO users(name,name_key,salt,pin_hash,created) VALUES(?,?,?,?,?)", (name, name.casefold(), salt, hp(pin, salt), time.time()))
        self._session(cur.lastrowid, True)
    def delete_account(self, usr, b):
        r = db.execute("SELECT salt,pin_hash FROM users WHERE id=?", (usr[0],)).fetchone()
        if not hmac.compare_digest(hp(str(b.get("pin", "")), r[0]), r[1]):
            return self.send(401, {"error": "That PIN doesn't match. Nothing was deleted."})
        for t in ("results", "tracked", "badges", "sessions"): db.execute(f"DELETE FROM {t} WHERE user_id=?", (usr[0],))
        db.execute("DELETE FROM users WHERE id=?", (usr[0],))
        self.send(200, {"deleted": True}, cookie="mx=; Path=/; Max-Age=0")

    # tracking
    def _active_ids(self): return {r[0] for r in db.execute("SELECT id FROM exercises WHERE active=1")}
    def track(self, usr, b):
        ids = b.get("ids")
        if not isinstance(ids, list): return self.send(400, {"error": "bad request"})
        valid = self._active_ids(); keep = set()
        for x in ids:
            try:
                if int(x) in valid: keep.add(int(x))
            except Exception: pass
        old = {r[0] for r in db.execute("SELECT exercise_id FROM tracked WHERE user_id=?", (usr[0],))}
        # only the tracked list changes; saved results are never deleted
        for e in old - keep: db.execute("DELETE FROM tracked WHERE user_id=? AND exercise_id=?", (usr[0], e))
        for e in keep - old: db.execute("INSERT INTO tracked VALUES(?,?,?)", (usr[0], e, time.time()))
        db.execute("UPDATE users SET picks_saved=1 WHERE id=?", (usr[0],))
        self.send(200, {"tracked": sorted(keep), "newBadges": evaluate_badges(usr[0])})
    def toggle(self, usr, b):
        try: eid = int(b.get("exercise_id"))
        except Exception: return self.send(400, {"error": "bad request"})
        if eid not in self._active_ids(): return self.send(404, {"error": "That exercise isn't available."})
        if b.get("tracked"): db.execute("INSERT OR IGNORE INTO tracked VALUES(?,?,?)", (usr[0], eid, time.time()))
        else: db.execute("DELETE FROM tracked WHERE user_id=? AND exercise_id=?", (usr[0], eid))
        db.execute("UPDATE users SET picks_saved=1 WHERE id=?", (usr[0],))
        self.send(200, {"tracked": bool(b.get("tracked")), "newBadges": evaluate_badges(usr[0])})

    # results
    def _check_pb(self, uid, eid, rid):
        ex = get_ex(eid); res = load_results(uid, eid); analyse(ex["def"], res)
        return any(r["id"] == rid and r["is_pb"] for r in res) or self._is_best_now(ex, res, rid)
    def _is_best_now(self, ex, res, rid):
        d = ex["def"]; better = better_of(d)
        me = next((r for r in res if r["id"] == rid), None)
        if not me or me["h"] is None: return False
        others = [r["h"] for r in res if r["id"] != rid and r["series"] == me["series"] and r["h"] is not None]
        return bool(others) and all(beats(better, me["h"], o) for o in others)
    def add_result(self, usr, b):
        try: eid = int(b.get("exercise_id"))
        except Exception: return self.send(400, {"error": "bad request"})
        ex = get_ex(eid)
        if not ex or not ex["active"]: return self.send(404, {"error": "That exercise isn't available."})
        r, err = parse_result(ex["def"], b)
        if err: return self.send(400, {"error": err})
        if db.execute("SELECT COUNT(*) FROM results WHERE user_id=?", (usr[0],)).fetchone()[0] >= 20000:
            return self.send(400, {"error": "That's a lot of results! Please contact the studio."})
        cur = db.execute("INSERT INTO results(user_id,exercise_id,day,pval,sval,selectors,note,created) VALUES(?,?,?,?,?,?,?,?)",
                         (usr[0], eid, r["day"], r["p"], r["s"], json.dumps(r["sel"]), r["note"], time.time()))
        db.execute("INSERT OR IGNORE INTO tracked VALUES(?,?,?)", (usr[0], eid, time.time()))
        res = load_results(usr[0], eid); analyse(ex["def"], res)
        pb = self._is_best_now(ex, res, cur.lastrowid)
        self.send(200, {"id": cur.lastrowid, "pb": pb, "newBadges": evaluate_badges(usr[0])})
    def update_result(self, usr, b):
        try: rid = int(b.get("id"))
        except Exception: return self.send(400, {"error": "bad request"})
        row = db.execute("SELECT exercise_id FROM results WHERE id=? AND user_id=?", (rid, usr[0])).fetchone()
        if not row: return self.send(404, {"error": "We couldn't find that result."})
        ex = get_ex(row[0]); r, err = parse_result(ex["def"], b)
        if err: return self.send(400, {"error": err})
        db.execute("UPDATE results SET day=?,pval=?,sval=?,selectors=?,note=? WHERE id=?", (r["day"], r["p"], r["s"], json.dumps(r["sel"]), r["note"], rid))
        res = load_results(usr[0], row[0]); analyse(ex["def"], res)
        self.send(200, {"id": rid, "pb": self._is_best_now(ex, res, rid), "newBadges": evaluate_badges(usr[0])})
    def set_note(self, usr, b):
        try: rid = int(b.get("id"))
        except Exception: return self.send(400, {"error": "bad request"})
        n = db.execute("UPDATE results SET note=? WHERE id=? AND user_id=?", (clean(b.get("note"), 500), rid, usr[0])).rowcount
        if not n: return self.send(404, {"error": "We couldn't find that result."})
        self.send(200, {"ok": 1})
    def delete_result(self, usr, b):
        try: rid = int(b.get("id"))
        except Exception: return self.send(400, {"error": "bad request"})
        n = db.execute("DELETE FROM results WHERE id=? AND user_id=?", (rid, usr[0])).rowcount
        if not n: return self.send(404, {"error": "We couldn't find that result."})
        self.send(200, {"ok": 1})

    # ---- admin (aggregate counts only; never individual scores) ----
    FAILS = [0, 0.0]
    def admin(self, p, b):
        if not ADMIN_PASSWORD: return self.send(403, {"error": "Admin is not set up."})
        now = time.time()
        if self.FAILS[1] > now: return self.send(429, {"error": "Too many wrong passwords. Wait a few minutes."})
        if not hmac.compare_digest(self.headers.get("X-Admin-Password", "").encode(), ADMIN_PASSWORD.encode()):
            self.FAILS[0] += 1
            if self.FAILS[0] >= 8: self.FAILS[0] = 0; self.FAILS[1] = now + 300
            return self.send(401, {"error": "Wrong password."})
        self.FAILS[0] = 0
        if p == "/api/admin/stats":
            q = lambda s: db.execute(s).fetchone()[0]
            pop = db.execute("SELECT e.id,e.name,(SELECT COUNT(*) FROM tracked t WHERE t.exercise_id=e.id),(SELECT COUNT(*) FROM results r WHERE r.exercise_id=e.id) FROM exercises e ORDER BY 3 DESC,4 DESC LIMIT 15").fetchall()
            return self.send(200, {"members": q("SELECT COUNT(*) FROM users"), "results": q("SELECT COUNT(*) FROM results"), "exercises": q("SELECT COUNT(*) FROM exercises WHERE active=1"),
                                   "badgesAwarded": q("SELECT COUNT(*) FROM badges"), "popular": [{"id": r[0], "name": r[1], "trackers": r[2], "results": r[3]} for r in pop]})
        if p == "/api/admin/list":
            rows = db.execute("SELECT id,name,description,category,sort,active,def FROM exercises ORDER BY sort,id").fetchall()
            return self.send(200, {"exercises": [ex_row(r) for r in rows]})
        if p == "/api/admin/save":
            name = clean(b.get("name"), 80); desc = clean(b.get("description"), 600); cat = clean(b.get("category"), 40)
            d, err = validate_def(b.get("def") or {})
            if not name or not cat: err = err or "Please give a name and a category."
            if err: return self.send(400, {"error": err})
            active = 1 if b.get("active", True) else 0
            if b.get("id"):
                db.execute("UPDATE exercises SET name=?,description=?,category=?,active=?,def=? WHERE id=?", (name, desc, cat, active, json.dumps(d), int(b["id"])))
            else:
                mx = db.execute("SELECT COALESCE(MAX(sort),-1)+1 FROM exercises").fetchone()[0]
                db.execute("INSERT INTO exercises(name,description,category,sort,active,def) VALUES(?,?,?,?,?,?)", (name, desc, cat, mx, active, json.dumps(d)))
            return self.send(200, {"ok": 1})
        if p == "/api/admin/import":
            try: items = b["exercises"] if isinstance(b.get("exercises"), list) else json.loads(b.get("text", ""))["exercises"]
            except Exception: return self.send(400, {"error": "Please paste JSON like {\"exercises\": [ ... ]}."})
            have = {r[0].casefold() for r in db.execute("SELECT name FROM exercises")}
            added, skipped, bad = [], [], []
            for i, e in enumerate(items, 1):
                try:
                    name = clean(e["name"], 80); d, err = validate_def(e); cat = clean(e.get("category"), 40)
                    if err or not name or not cat: bad.append({"item": i, "error": err or "name and category are required"}); continue
                    if name.casefold() in have: skipped.append(name); continue
                    have.add(name.casefold()); added.append((name, clean(e.get("description"), 600), cat, d))
                except Exception: bad.append({"item": i, "error": "not a valid item"})
            if not b.get("dry"):
                for name, desc, cat, d in added:
                    mx = db.execute("SELECT COALESCE(MAX(sort),-1)+1 FROM exercises").fetchone()[0]
                    db.execute("INSERT INTO exercises(name,description,category,sort,active,def) VALUES(?,?,?,?,1,?)", (name, desc, cat, mx, json.dumps(d)))
            return self.send(200, {"dry": bool(b.get("dry")), "added": [a[0] for a in added], "skipped": skipped, "bad": bad})
        if p == "/api/admin/members":  # names only: needed to pick who an import is for
            return self.send(200, {"members": [{"id": r[0], "name": r[1]} for r in db.execute("SELECT id,name FROM users ORDER BY name_key")]})
        if p in ("/api/admin/import-preview", "/api/admin/import-confirm"):
            try: uid = int(b.get("user_id"))
            except Exception: return self.send(400, {"error": "Please choose a member."})
            if not db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone(): return self.send(404, {"error": "We couldn't find that member."})
            r = run_import(uid, b, p.endswith("confirm")); return self.send(400 if "error" in r else 200, r)
        if p == "/api/admin/reorder":
            for i, x in enumerate(b.get("ids", [])):
                try: db.execute("UPDATE exercises SET sort=? WHERE id=?", (i, int(x)))
                except Exception: pass
            return self.send(200, {"ok": 1})
        self.send(404, {"error": "nf"})

class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True; allow_reuse_address = True

if __name__ == "__main__":
    S((os.environ.get("HOST", "0.0.0.0"), PORT), H).serve_forever()
