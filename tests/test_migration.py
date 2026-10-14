"""Migration test: DB seeded by the old exercises.json gets renamed SAOP (reps + equipment) and Bench Press Strength reps; old results stay attached."""
import subprocess, sys, os, json, sqlite3, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp(); DBP = os.path.join(tmp, "m.db")
old = json.loads(subprocess.check_output(["git", "-C", ROOT, "show", "cb2d11b:exercises.json"]))
c = sqlite3.connect(DBP)
c.executescript("CREATE TABLE exercises(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, description TEXT, category TEXT, sort INTEGER DEFAULT 0, active INTEGER DEFAULT 1, def TEXT);"
                "CREATE TABLE results(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, exercise_id INTEGER, day TEXT, pval REAL, sval REAL, selectors TEXT, note TEXT, created REAL);"
                "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT); INSERT INTO meta VALUES('seeded','1');")
for e in old["exercises"]:
    c.execute("INSERT INTO exercises(name,description,category,sort,active,def) VALUES(?,?,?,?,1,?)", (e["name"], e["description"], e["category"], e.get("sort", 0), json.dumps({k: e.get(k) for k in ("primary", "secondary", "selectors", "headline")})))
eid = c.execute("SELECT id FROM exercises WHERE name='Kettlebell Single Arm Overhead Press'").fetchone()[0]
c.execute("INSERT INTO results(user_id,exercise_id,day,pval,selectors,created) VALUES(1,?,'2026-01-01',35,?,0)", (eid, json.dumps({"hand": "Right Hand"})))
sid = c.execute("SELECT id FROM exercises WHERE name='Kettlebell Single Arm Snatch'").fetchone()[0]
c.execute("INSERT INTO results(user_id,exercise_id,day,pval,selectors,created) VALUES(1,?,'2026-01-02',26,?,0)", (sid, json.dumps({"hand": "Left Hand"}))); c.commit(); c.close()
code = "import sys; sys.argv=['x']; import server"
env = dict(os.environ, DB_PATH=DBP, ADMIN_PASSWORD="x", PORT="18099", HOST="127.0.0.1")
for _ in range(2):  # run twice: idempotent
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env); import time; time.sleep(1.2); p.terminate(); p.wait()
c = sqlite3.connect(DBP); F = 0
def check(n, ok):
    global F; print("PASS" if ok else "FAIL", n); F += not ok
row = c.execute("SELECT name,def FROM exercises WHERE id=?", (eid,)).fetchone(); d = json.loads(row[1])
check("renamed in place (same id)", row[0] == "Single Arm Overhead Press")
check("no leftover old name", c.execute("SELECT COUNT(*) FROM exercises WHERE name LIKE 'Kettlebell Single Arm Overhead%'").fetchone()[0] == 0)
check("reps + equipment added, hand kept", d["secondary"]["type"] == "reps" and [x["key"] for x in d["selectors"]] == ["hand", "equipment"])
r = c.execute("SELECT exercise_id,selectors FROM results WHERE exercise_id=?", (eid,)).fetchone()
sn = c.execute("SELECT name,def FROM exercises WHERE id=?", (sid,)).fetchone(); sd = json.loads(sn[1])
check("snatch renamed in place, hand kept + equipment added, no reps added", sn[0] == "Single Arm Snatch" and [x["key"] for x in sd["selectors"]] == ["hand", "equipment"] and sd["secondary"] is None)
rs = c.execute("SELECT exercise_id,selectors FROM results WHERE exercise_id=?", (sid,)).fetchone()
check("snatch old result attached, backfilled Kettlebell", rs and json.loads(rs[1]) == {"hand": "Left Hand", "equipment": "Kettlebell"})
check("old result still attached, backfilled Kettlebell", r[0] == eid and json.loads(r[1]) == {"hand": "Right Hand", "equipment": "Kettlebell"})
bd = json.loads(c.execute("SELECT def FROM exercises WHERE name='Bench Press Strength'").fetchone()[0])
check("bench strength reps added, equipment kept", bd["secondary"]["type"] == "reps" and bd["selectors"][0]["key"] == "equipment")
sq = json.loads(c.execute("SELECT def FROM exercises WHERE name='Back Squat Strength'").fetchone()[0])
check("back squat strength reps added", sq["secondary"]["type"] == "reps" and sq["secondary"]["optional"] is False)
check("exercise count unchanged", c.execute("SELECT COUNT(*) FROM exercises").fetchone()[0] == len(old["exercises"]))
sys.exit(1 if F else 0)
