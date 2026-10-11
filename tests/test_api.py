"""End-to-end API tests. Run: python3 tests/test_api.py  (starts its own server with a temp DB)."""
import subprocess, sys, os, time, json, http.client, tempfile, sqlite3, signal
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 18090; tmp = tempfile.mkdtemp(); DBP = os.path.join(tmp, "t.db")
env = dict(os.environ, PORT=str(PORT), DB_PATH=DBP, ADMIN_PASSWORD="testpw", MX_TEST_TODAY="2026-10-10", HOST="127.0.0.1")
srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env)
time.sleep(1.2)
P = F = 0
def check(name, cond, extra=""):
    global P, F
    if cond: P += 1; print("PASS", name)
    else: F += 1; print("FAIL", name, extra)
def call(path, body=None, cookie=None, admin=None, method=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT); h = {}
    if cookie: h["Cookie"] = cookie
    if admin is not None: h["X-Admin-Password"] = admin
    if body is not None or method == "POST":
        h["Content-Type"] = "application/json"; c.request("POST", path, json.dumps(body or {}), h)
    else: c.request("GET", path, headers=h)
    r = c.getresponse(); raw = r.read(); ck = r.getheader("Set-Cookie")
    try: j = json.loads(raw)
    except Exception: j = raw
    return r.status, j, (ck.split(";")[0] if ck and "mx=;" not in ck and ck.startswith("mx=") else None)
def users(): return sqlite3.connect(DBP).execute("select count(*) from users").fetchone()[0]
try:
    # --- login rules
    s, j, _ = call("/api/login", {"name": "Ghost", "pin": "1234"}); check("unknown name -> 401 generic", s == 401 and "doesn't match" in j["error"])
    check("login never creates account", users() == 0)
    s, j, ckA = call("/api/register", {"name": "Alice", "pin": "1234"}); check("register ok", s == 200 and j["new"] is True and ckA)
    check("1 user", users() == 1)
    s, j, _ = call("/api/register", {"name": "alice", "pin": "9999"}); check("duplicate (case-insens) rejected 409", s == 409 and "already taken" in j["error"] and users() == 1)
    s, j, _ = call("/api/register", {"name": "Bob", "pin": "12"}); check("bad pin rejected", s == 400)
    s, j, ck2 = call("/api/login", {"name": "ALICE", "pin": "1234"}); check("login case-insensitive", s == 200 and j["new"] is False and ck2)
    s, j, _ = call("/api/login", {"name": "Alice", "pin": "0000"}); check("wrong pin -> same generic msg", s == 401 and "doesn't match" in j["error"])
    for _ in range(3): call("/api/login", {"name": "Alice", "pin": "0000"})
    s, j, _ = call("/api/login", {"name": "Alice", "pin": "0000"}); check("5th wrong -> still 401", s == 401)
    s, j, _ = call("/api/login", {"name": "Alice", "pin": "1234"}); check("locked even with right pin (friendly msg)", s == 429 and "any coach" in j["error"])
    _c=sqlite3.connect(DBP); _c.execute("update users set locked_until=0"); _c.commit(); _c.close()
    
    s, j, _ = call("/api/login", {"name": "Alice", "pin": "1234"}); check("login works after lock expiry", s == 200)
    s, j, ckB = call("/api/register", {"name": "Bob", "pin": "4321"}); check("register Bob", s == 200)
    check("unauth api 401", all(call(p)[0] == 401 for p in ("/api/me", "/api/exercises", "/api/home", "/api/badges", "/api/metric?exercise_id=1")))
    check("unauth post 401", call("/api/results", {"exercise_id": 1})[0] == 401 and call("/api/track", {"ids": []})[0] == 401)
    # --- library
    s, lib, _ = call("/api/exercises", cookie=ckA); ex = {e["name"]: e for e in lib["exercises"]}
    check("35 exercises seeded", len(lib["exercises"]) == 35, len(lib["exercises"]))
    check("7 categories", len(lib["categories"]) == 7, lib["categories"])
    # --- track/untrack
    s, j, _ = call("/api/track", {"ids": [ex["Grip Strength"]["id"], ex["Gymnazo Mile"]["id"], 99999]}, ckA); check("picker saves valid ids only", j["tracked"] == sorted([ex["Grip Strength"]["id"], ex["Gymnazo Mile"]["id"]]))
    s, me, _ = call("/api/me", cookie=ckA); check("picksSaved true", me["picksSaved"] is True)
    s, me, _ = call("/api/me", cookie=ckB); check("Bob picks not saved / not shared", me["picksSaved"] is False and me["tracked"] == [])
    s, j, _ = call("/api/track/toggle", {"exercise_id": ex["Deadlift"]["id"], "tracked": True}, ckA); check("toggle on", j["tracked"])
    call("/api/track/toggle", {"exercise_id": ex["Deadlift"]["id"], "tracked": False}, ckA)
    s, h, _ = call("/api/home", cookie=ckA); check("home lists 2 tracked", len(h["metrics"]) == 2)
    # --- logging types
    g = ex["Grip Strength"]["id"]; mile = ex["Gymnazo Mile"]["id"]
    s, j, _ = call("/api/results", {"exercise_id": g, "primary": 80, "day": "2026-01-10"}, ckA); check("selector required (hand)", s == 400 and "Which hand" in j["error"])
    s, j, _ = call("/api/results", {"exercise_id": g, "primary": 80, "selectors": {"hand": "Foot"}, "day": "2026-01-10"}, ckA); check("invalid option rejected", s == 400)
    s, j, _ = call("/api/results", {"exercise_id": g, "primary": "abc", "selectors": {"hand": "Right Hand"}}, ckA); check("non-number rejected", s == 400)
    s, j, _ = call("/api/results", {"exercise_id": g, "primary": -5, "selectors": {"hand": "Right Hand"}}, ckA); check("negative rejected", s == 400)
    s, j, _ = call("/api/results", {"exercise_id": g, "primary": 80, "selectors": {"hand": "Right Hand"}, "day": "2026-10-11"}, ckA); check("future date rejected", s == 400)
    allnew = []
    def log(eid, p, day, sel=None, sec=None, ck=ckA, note=""):
        s, j, _ = call("/api/results", {"exercise_id": eid, "primary": p, "secondary": sec, "selectors": sel or {}, "day": day, "note": note}, ck)
        assert s == 200, (s, j); allnew.extend(b["code"] for b in j["newBadges"]); return j
    j = log(g, 80, "2026-01-10", {"hand": "Right Hand"}); check("first result -> first_result badge, no pb", not j["pb"] and [b["code"] for b in j["newBadges"]] == ["first_result"], j)
    j = log(g, 70, "2026-02-10", {"hand": "Right Hand"}); check("higher-better: lower value not PB", not j["pb"])
    j = log(g, 90, "2026-03-10", {"hand": "Right Hand"}); check("higher-better: PB detected", j["pb"])
    check("first_pb badge awarded", "first_pb" in allnew)
    j = log(g, 60, "2026-03-11", {"hand": "Left Hand"}); check("PB is per series (left hand first result not PB)", not j["pb"])
    s, m, _ = call(f"/api/metric?exercise_id={g}", cookie=ckA)
    check("series split by hand", {x["label"] for x in m["series"]} == {"Right Hand", "Left Hand"})
    rh = [x for x in m["series"] if x["label"] == "Right Hand"][0]; check("series best/first/latest", rh["best"]["value"] == 90 and rh["first"]["value"] == 80 and rh["latest"]["value"] == 90 and rh["trend"] == "up")
    # lower is better (time)
    j = log(mile, 600, "2026-01-05"); j = log(mile, 540, "2026-04-05"); check("lower-better: faster is PB", j["pb"])
    j = log(mile, 560, "2026-05-05"); check("lower-better: slower not PB", not j["pb"])
    s, m, _ = call(f"/api/metric?exercise_id={mile}", cookie=ckA); check("lower-better best = 540", m["series"][0]["best"]["value"] == 540 and m["better"] == "lower" and m["series"][0]["trend"] == "down")
    # all metric types
    chk = lambda n, eid, p, **k: check("log " + n, call("/api/results", dict(exercise_id=eid, primary=p, day="2026-06-01", **k), ckA)[0] == 200)
    chk("time (min:sec as seconds) single leg balance + optional secondary weight", ex["Single Leg Balance"]["id"], 75, selectors={"side": "Left Leg"}, secondary=10)
    chk("distance ft (triple jump)", ex["Anterior Triple Jump"]["id"], 21.5)
    chk("distance in + side (single leg hop)", ex["Single Leg Hop"]["id"], 40, selectors={"side": "Right Foot"})
    chk("height (box jump) + reps", ex["Box Jump"]["id"], 2.5, secondary=12)
    chk("power watts + side", ex["Keiser Max Power Rotation Swing"]["id"], 450, selectors={"side": "Left Side"})
    chk("reps (pull-ups)", ex["Pull-ups"]["id"], 8)
    chk("damper selector (row 500m)", ex["Row Machine - 500m Row"]["id"], 118, selectors={"damper": "5"})
    chk("equipment + required secondary reps (overhead press)", ex["Overhead Press (in 1 minute)"]["id"], 45, selectors={"equipment": "Barbell"}, secondary=20)
    s, j, _ = call("/api/results", {"exercise_id": ex["Bench Press Endurance"]["id"], "primary": 95, "selectors": {"equipment": "Barbell"}, "day": "2026-06-01"}, ckA); check("required secondary enforced", s == 400)
    s, j, _ = call("/api/results", {"exercise_id": ex["Back Squat Endurance"]["id"], "primary": 135, "secondary": 20, "selectors": {"equipment": "Barbell"}, "day": "2026-06-01"}, ckA); check("headline=secondary exercise logs", s == 200)
    s, j, _ = call("/api/results", {"exercise_id": ex["Back Squat Endurance"]["id"], "primary": 135, "secondary": 25, "selectors": {"equipment": "Barbell"}, "day": "2026-07-01"}, ckA); check("headline=secondary PB uses reps", j["pb"])
    s, j, _ = call("/api/results", {"exercise_id": ex["Body Weight Plank on Forearms"]["id"], "primary": 90000, "day": "2026-06-01"}, ckA); check("absurd time rejected", s == 400)
    # edit / delete
    s, m, _ = call(f"/api/metric?exercise_id={g}", cookie=ckA); rid = m["results"][0]["id"]
    s, j, _ = call("/api/results/update", {"id": rid, "exercise_id": g, "primary": 85, "selectors": {"hand": "Right Hand"}, "day": "2026-01-10", "note": "<b>hi</b> there"}, ckA); check("edit own result", s == 200)
    s, m, _ = call(f"/api/metric?exercise_id={g}", cookie=ckA); r0 = [r for r in m["results"] if r["id"] == rid][0]; check("edit applied, html stripped", r0["p"] == 85 and r0["note"] == "hi there", r0)
    # privacy
    s, j, _ = call("/api/results/update", {"id": rid, "primary": 1, "selectors": {"hand": "Right Hand"}}, ckB); check("Bob cannot edit Alice's result", s == 404)
    s, j, _ = call("/api/results/delete", {"id": rid}, ckB); check("Bob cannot delete Alice's result", s == 404)
    s, m, _ = call(f"/api/metric?exercise_id={g}", cookie=ckB); check("Bob sees none of Alice's results", m["results"] == [] and m["series"] == [])
    s, b, _ = call("/api/badges", cookie=ckB); check("Bob has no badges", not any(x["earned"] for x in b["badges"]))
    s, h, _ = call("/api/home", cookie=ckB); check("Bob home empty", h["metrics"] == [])
    s, lb, _ = call("/api/exercises", cookie=ckB); check("library has no other-user data", all(e["results"] == 0 and not e["tracked"] for e in lb["exercises"]))
    check("no leaderboard endpoint", call("/api/leaderboard", cookie=ckA)[0] == 404)
    # milestones
    j = log(g, 91, "2026-07-10", {"hand": "Right Hand"}); j = log(g, 92, "2026-08-10", {"hand": "Right Hand"})  # more results on grip
    for i in range(5): log(g, 93 + i, "2026-09-%02d" % (i + 1), {"hand": "Right Hand"})
    for n in ex.values():
        if n["name"] not in ("Grip Strength",) and len(set(allnew)) < 99: pass
    ids = [e["id"] for e in lib["exercises"]][:12]
    call("/api/track", {"ids": ids}, ckA)
    s, bd, _ = call("/api/badges", cookie=ckA); got = {b["code"] for b in bd["badges"] if b["earned"]}
    want = {"first_result", "first_pb", "results3", "results5", "results10", "track5", "track10", "months3", "months6", "months12", "streak3", "improve10", "improve25"}
    # months12 needs 12 distinct months: add earlier months
    check("most badges so far", {"first_result", "first_pb", "results3", "results5", "results10", "track5", "track10", "months6", "streak3", "improve10", "improve25"} <= got, got)
    log(mile, 500, "2025-12-01"); log(mile, 500, "2025-11-01"); log(mile, 500, "2025-10-01")
    s, bd, _ = call("/api/badges", cookie=ckA); got = {b["code"] for b in bd["badges"] if b["earned"]}
    check("all 13 badges earned", got == want, want - got)
    cnt = {c: allnew.count(c) for c in set(allnew)}
    check("every badge fires once only (new-badge lists)", all(v == 1 for v in cnt.values()), cnt)
    rows = sqlite3.connect(DBP).execute("select user_id,code,count(*) from badges group by 1,2 having count(*)>1").fetchall(); check("no duplicate badge rows", not rows)
    j = log(g, 99, "2026-10-01", {"hand": "Right Hand"}); check("no re-award on later logs", j["newBadges"] == [])
    # retest nudge
    log(ex["Deadlift"]["id"], 200, "2026-05-01", {"equipment": "Barbell"})
    s, h, _ = call("/api/home", cookie=ckA); rn = [r["name"] for r in h["retest"]]; check("retest nudge (90+ days) lists old metrics", "Deadlift" in rn and "Grip Strength" not in rn, rn)
    call("/api/nudge/hide", {}, ckA); s, h, _ = call("/api/home", cookie=ckA); check("nudge can be hidden", h["retest"] == [])
    # delete result
    s, j, _ = call("/api/results/delete", {"id": rid}, ckA); check("delete own result", s == 200)
    # --- admin
    check("admin no pw -> 401", call("/api/admin/stats", {}, method="POST")[0] == 401)
    check("admin wrong pw -> 401", call("/api/admin/list", {}, admin="nope")[0] == 401)
    check("admin save needs auth", call("/api/admin/save", {"name": "x"})[0] == 401 and call("/api/admin/import", {})[0] == 401)
    s, st, _ = call("/api/admin/stats", {}, admin="testpw"); check("admin stats counts only", s == 200 and st["members"] == 2 and set(st) == {"members", "results", "exercises", "badgesAwarded", "popular"} and all(set(p) == {"id", "name", "trackers", "results"} for p in st["popular"]))
    s, al, _ = call("/api/admin/list", {}, admin="testpw"); check("admin list 35", len(al["exercises"]) == 35)
    nd = {"name": "Farmer Carry", "description": "Walk it", "category": "Strength", "active": True, "def": {"primary": {"type": "distance", "unit": "ft", "better": "higher", "label": "How far?"}, "selectors": []}}
    s, j, _ = call("/api/admin/save", nd, admin="testpw"); check("admin add exercise", s == 200)
    s, j, _ = call("/api/admin/save", {**nd, "def": {"primary": {"type": "bogus"}}}, admin="testpw"); check("admin rejects bad type", s == 400)
    s, al, _ = call("/api/admin/list", {}, admin="testpw"); fid = [e["id"] for e in al["exercises"] if e["name"] == "Farmer Carry"][0]
    s, j, _ = call("/api/admin/save", dict(nd, id=fid, active=False), admin="testpw"); s, lib2, _ = call("/api/exercises", cookie=ckA); check("hidden exercise not shown to members", "Farmer Carry" not in {e["name"] for e in lib2["exercises"]})
    s, j, _ = call("/api/admin/import", {"dry": True, "exercises": [{"name": "farmer carry", "category": "Strength", "primary": {"type": "distance", "unit": "ft", "label": "How far?"}}, {"name": "New One", "category": "Strength", "primary": {"type": "reps", "unit": "reps", "label": "Reps?"}}, {"name": "Broken"}]}, admin="testpw"); check("import preview: 1 added, 1 skipped, 1 bad", len(j["added"]) == 1 and len(j["skipped"]) == 1 and len(j["bad"]) == 1, j)
    s, j, _ = call("/api/admin/import", {"exercises": [{"name": "New One", "category": "Strength", "primary": {"type": "reps", "unit": "reps", "label": "Reps?"}}]}, admin="testpw"); s, al, _ = call("/api/admin/list", {}, admin="testpw"); check("import applied", len(al["exercises"]) == 37)
    # --- notes, import, export (Carol & Dave)
    s, j, ckC = call("/api/register", {"name": "Carol", "pin": "1111"}); s, j, ckD = call("/api/register", {"name": "Dave", "pin": "2222"})
    gx = lambda n: ex[n]["id"]
    s, tpl, _ = call("/api/import/template", cookie=ckC); tpl = tpl.decode("utf-8-sig") if isinstance(tpl, bytes) else str(tpl)
    check("template CSV has the documented columns + example rows", tpl.splitlines()[0] == "Date,Exercise,Result,Second result (optional),Side / Hand / Foot,Equipment,Damper,Note" and "Grip Strength" in tpl and len(tpl.splitlines()) >= 6)
    s, ref, _ = call("/api/import/reference", cookie=ckC); check("exercise-name reference lists all names", s == 200 and all(n in ref.decode("utf-8-sig") for n in ex))
    check("import endpoints need login", call("/api/import/template")[0] == 401 and call("/api/import/preview", {"text": "x"})[0] == 401 and call("/api/export.csv")[0] == 401 and call("/api/results/note", {"id": 1})[0] == 401)
    CSVT = "\n".join(["Date,Exercise,Result,Second result (optional),Side / Hand / Foot,Equipment,Damper,Note",
        "2025-03-14,Grip Strength,60,,right,,,First try <script>alert(1)</script>",
        "6/20/2025,grip  strength,66,,Right Hand,,,Feeling stronger",
        "March 14 2025,Gymnazo Mile,10:30,,,,,windy",
        "2025-09-02,GYMNAZO MILE,0:9:45,,,,,",
        "2025-12-01,gymnazo mile,540,,,,,seconds format",
        "2025-04-01,500m row,2:10,,,,5,",
        "2025-07-01,Pull ups,8,,,,,",
        "2025-08-01,Gripp Strenth,70,,Left Hand,,,typo name",
        "not a date,Pull-ups,5,,,,,",
        "2025-08-02,Pull-ups,abc,,,,,",
        "2025-08-03,Grip Strength,50,,Elbow,,,",
        "2025-08-04,Grip Strength,50,,,,,",
        "2025-03-14,Grip Strength,60,,right,,,duplicate in file",
        "2099-01-01,Pull-ups,5,,,,,",
        "2025-08-05,Overhead Press (in 1 minute),45,,,Barbell,,missing reps",
        "2025-08-06,Single Leg Balance,1:15,10,Left Leg,,,ok with optional weight"])
    s, pv, _ = call("/api/import/preview", {"text": CSVT}, ckC)
    byrow = {r["row"]: r for r in pv["rows"]}
    check("preview: nothing saved before confirm", call("/api/home", cookie=ckC)[1]["metrics"] == [] and sqlite3.connect(DBP).execute("select count(*) from results where user_id=(select id from users where name='Carol')").fetchone()[0] == 0)
    check("preview counts", pv["counts"] == {"ok": 8, "duplicate": 1, "problem": 7}, pv["counts"])
    check("date formats ISO / US / long", byrow[2]["day"] == "2025-03-14" and byrow[3]["day"] == "2025-06-20" and byrow[4]["day"] == "2025-03-14")
    check("time formats mm:ss, h:mm:ss, seconds", byrow[4]["p"] == 630 and byrow[5]["p"] == 585 and byrow[6]["p"] == 540, [byrow[i]["p"] for i in (4, 5, 6)])
    check("name variants matched (case/space/partial/hyphen)", byrow[3]["exercise"] == "Grip Strength" and byrow[7]["exercise"] == "Row Machine - 500m Row" and byrow[8]["exercise"] == "Pull-ups" and byrow[6]["exercise"] == "Gymnazo Mile")
    check("side 'right' matched to option", byrow[2]["sel"] == {"hand": "Right Hand"})
    check("unknown name: plain message + suggestion", byrow[9]["status"] == "problem" and "Did you mean Grip Strength" in byrow[9]["error"] and byrow[9]["suggestions"][0]["name"] == "Grip Strength", byrow[9])
    check("bad date / bad number / bad choice / missing choice / future / missing 2nd result are problems", all(byrow[i]["status"] == "problem" for i in (10, 11, 12, 13, 15, 16)))
    check("problem rows carry the row number and reason", "date" in byrow[10]["error"] and "number" in byrow[11]["error"] and "Elbow" in byrow[12]["error"] and "Please add" in byrow[13]["error"])
    check("in-file duplicate flagged", byrow[14]["status"] == "duplicate")
    check("script tags stripped from notes", "<" not in byrow[2]["note"] and "script" not in byrow[2]["note"].replace("alert(1)", ""), byrow[2]["note"])
    s, pv2, _ = call("/api/import/preview", {"text": CSVT, "fixes": {"9": gx("Grip Strength")}}, ckC)
    check("dropdown fix turns a problem row into ok", {r["row"]: r for r in pv2["rows"]}[9]["status"] == "ok" and pv2["counts"]["problem"] == 6)
    s, cf, _ = call("/api/import/confirm", {"text": CSVT, "fixes": {"9": gx("Grip Strength")}}, ckC)
    check("confirm imports 9 and skips the rest", cf["imported"] == 9 and cf["counts"]["duplicate"] == 1, cf)
    nb = [b["code"] for b in cf["newBadges"]]
    check("one summary of new badges (once each)", len(nb) == len(set(nb)) and {"first_result", "first_pb", "results3", "months3", "improve10"} <= set(nb), nb)
    s, m, _ = call(f"/api/metric?exercise_id={gx('Grip Strength')}", cookie=ckC)
    days = [r["day"] for r in m["results"]]
    check("backdated results sorted by date on chart data", days == sorted(days) and days[0] == "2025-03-14" and len(days) == 3, days)
    rh = [x for x in m["series"] if x["label"] == "Right Hand"][0]; check("PB/first/latest recomputed from history", rh["best"]["value"] == 66 and rh["first"]["value"] == 60 and rh["latest"]["day"] == "2025-06-20")
    s, mm, _ = call(f"/api/metric?exercise_id={gx('Gymnazo Mile')}", cookie=ckC); check("lower-is-better history: best is fastest", mm["series"][0]["best"]["value"] == 540 and mm["better"] == "lower")
    s, h, _ = call("/api/home", cookie=ckC); check("imported exercises auto-tracked", {x["name"] for x in h["metrics"]} >= {"Grip Strength", "Gymnazo Mile", "Pull-ups", "Row Machine - 500m Row", "Single Leg Balance"})
    check("home has sparkline data + recent row", all("spark" in x for x in h["metrics"] if x["latest"]) and len(h["recent"]) >= 3)
    s, cf2, _ = call("/api/import/confirm", {"text": CSVT, "fixes": {"9": gx("Grip Strength")}}, ckC)
    check("re-import: all duplicates, nothing added, no badges again", cf2["imported"] == 0 and cf2["newBadges"] == [] and cf2["counts"]["duplicate"] == 10, cf2)
    # notes: add / edit later / privacy
    rid = [r for r in m["results"] if r["day"] == "2025-08-01"][0]["id"]
    s, j, _ = call("/api/results/note", {"id": rid, "note": "New <b>note</b> for next time"}, ckC); check("add/edit note after saving", s == 200)
    s, m, _ = call(f"/api/metric?exercise_id={gx('Grip Strength')}", cookie=ckC); check("note edited and sanitized", [r for r in m["results"] if r["id"] == rid][0]["note"] == "New note for next time")
    s, j, _ = call("/api/results/note", {"id": rid, "note": "x" * 700}, ckC); s, m, _ = call(f"/api/metric?exercise_id={gx('Grip Strength')}", cookie=ckC); check("notes capped at 500 chars", len([r for r in m["results"] if r["id"] == rid][0]["note"]) == 500)
    check("Dave cannot edit Carol's note", call("/api/results/note", {"id": rid, "note": "hacked"}, ckD)[0] == 404)
    s, j, _ = call("/api/results", {"exercise_id": gx("Pull-ups"), "primary": 9, "day": "2025-10-01", "note": "z" * 600}, ckC); s, mp, _ = call(f"/api/metric?exercise_id={gx('Pull-ups')}", cookie=ckC)
    check("log form note up to 500", max(len(r["note"]) for r in mp["results"]) == 500)
    s, md, _ = call(f"/api/metric?exercise_id={gx('Grip Strength')}", cookie=ckD); check("Dave sees no notes/results of Carol", md["results"] == [])
    s, ex_csv, _ = call("/api/export.csv", cookie=ckC); ex_csv = ex_csv.decode("utf-8-sig")
    check("export includes Note column + notes", ex_csv.splitlines()[0].endswith(",Note") and "New note for next time" not in ex_csv and "Feeling stronger" in ex_csv)
    s, dx, _ = call("/api/export.csv", cookie=ckD); check("Dave export has only header (privacy)", len(dx.decode("utf-8-sig").strip().splitlines()) == 1)
    s, ri, _ = call("/api/import/preview", {"text": ex_csv}, ckC); check("export re-imports cleanly (all duplicates, no problems)", ri["counts"]["problem"] == 0 and ri["counts"]["ok"] == 0, ri["counts"])
    # tab-separated paste without header
    s, pt, _ = call("/api/import/preview", {"text": "2024-01-05\tPull-ups\t4\t\t\t\t\tpasted"}, ckD); check("pasted tab rows without header work", pt["counts"]["ok"] == 1 and pt["rows"][0]["note"] == "pasted")
    # cap
    big = "Date,Exercise,Result\n" + "\n".join(f"2025-01-01,Pull-ups,{i+1}" for i in range(2000))
    s, j, _ = call("/api/import/preview", {"text": big}, ckD); check("2,000 rows allowed", s == 200 and j["counts"]["ok"] == 2000)
    s, j, _ = call("/api/import/preview", {"text": big + "\n2025-01-01,Pull-ups,3000"}, ckD); check("2,001 rows rejected with plain message", s == 400 and "2,000" in j["error"])
    s, j, _ = call("/api/import/preview", {"text": "\n\n"}, ckD); check("empty input friendly error", s == 400)
    # admin import for a member
    check("admin import needs password", call("/api/admin/import-preview", {"user_id": 1, "text": "x"})[0] == 401 and call("/api/admin/import-confirm", {"user_id": 1, "text": "x"})[0] == 401 and call("/api/admin/members", {}, method="POST")[0] == 401)
    s, mem, _ = call("/api/admin/members", {}, admin="testpw"); check("admin members: names only", all(set(x) == {"id", "name"} for x in mem["members"]))
    did = [x["id"] for x in mem["members"] if x["name"] == "Dave"][0]
    s, j, _ = call("/api/admin/import-preview", {"user_id": did, "text": "Date,Exercise,Result,Note\n2024-05-05,Gymnazo Loop,3:30,admin loaded"}, admin="testpw"); check("admin preview", s == 200 and j["counts"]["ok"] == 1)
    s, j, _ = call("/api/admin/import-confirm", {"user_id": did, "text": "Date,Exercise,Result,Note\n2024-05-05,Gymnazo Loop,3:30,admin loaded"}, admin="testpw"); check("admin confirm imports for member", s == 200 and j["imported"] == 1)
    s, h, _ = call("/api/home", cookie=ckD); check("member sees admin-imported result (backdated, tracked)", any(x["name"] == "Gymnazo Loop" and x["latest"]["day"] == "2024-05-05" for x in h["metrics"]))
    s, hc, _ = call("/api/home", cookie=ckC); check("Carol unaffected by Dave's import", all(x["name"] != "Gymnazo Loop" for x in hc["metrics"]))
    blob = json.dumps([call("/api/admin/stats", {}, admin="testpw")[1], call("/api/admin/list", {}, admin="testpw")[1], mem])
    check("admin views never contain member notes", "Feeling stronger" not in blob and "New note" not in blob and "admin loaded" not in blob)
    s, j, _ = call("/api/admin/import-confirm", {"user_id": 99999, "text": "x"}, admin="testpw"); check("admin import unknown member 404", s == 404)
    call("/api/account/delete", {"pin": "1111"}, ckC); call("/api/account/delete", {"pin": "2222"}, ckD)
    # --- migration/seed idempotent: restart doesn't duplicate
    srv.terminate(); srv.wait(); srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env); time.sleep(1.2)
    s, al, _ = call("/api/admin/list", {}, admin="testpw"); check("restart keeps data, no re-seed", len(al["exercises"]) == 37)
    # --- account deletion
    s, j, _ = call("/api/account/delete", {"pin": "0000"}, ckB); check("delete account wrong pin refused", s == 401)
    s, j, _ = call("/api/account/delete", {"pin": "4321"}, ckB); check("delete account ok", s == 200)
    check("deleted session invalid", call("/api/me", cookie=ckB)[0] == 401)
    s, j, _ = call("/api/login", {"name": "Bob", "pin": "4321"}); check("deleted user cannot log in", s == 401)
    s, j, _ = call("/api/account/delete", {"pin": "1234"}, ckA); 
    c = sqlite3.connect(DBP); check("all Alice data removed", all(c.execute(f"select count(*) from {t}").fetchone()[0] == 0 for t in ("users", "results", "tracked", "badges", "sessions")))
    # xss: note stored stripped, notes are only rendered escaped client-side
finally:
    srv.terminate()
print(f"\n{P} passed, {F} failed"); sys.exit(1 if F else 0)
