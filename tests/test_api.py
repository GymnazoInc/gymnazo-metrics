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
    s, j, _ = call("/api/login", {"name": "Alice", "pin": "1234"}); check("locked even with right pin (friendly msg)", s == 429 and "Ask the studio" in j["error"])
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
