"""Momentum partner API tests (server-to-server verify/read/log). Run: python3 tests/test_partner.py"""
import subprocess, sys, os, time, json, http.client, tempfile, sqlite3
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 18093; tmp = tempfile.mkdtemp(); DBP = os.path.join(tmp, "t.db"); SEC = "s" * 40
P = F = 0
def check(name, cond, extra=""):
    global P, F
    if cond: P += 1; print("PASS", name)
    else: F += 1; print("FAIL", name, extra)
def start(secret):
    env = dict(os.environ, PORT=str(PORT), DB_PATH=DBP, MX_TEST_TODAY="2026-10-10", HOST="127.0.0.1", MOMENTUM_API_SECRET=secret)
    env.pop("FEDERATION_SECRET", None); env.pop("PEER_URL", None)
    s = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env); time.sleep(1.2); return s
def call(path, body=None, secret=SEC, cookie=None, method="POST", ip=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT); h = {"Content-Type": "application/json"}
    if ip: h["X-Forwarded-For"] = ip
    if secret is not None: h["X-Partner-Secret"] = secret
    if cookie: h["Cookie"] = cookie
    c.request(method, path, json.dumps(body or {}) if method == "POST" else None, h)
    r = c.getresponse(); raw = r.read(); ck = r.getheader("Set-Cookie")
    try: j = json.loads(raw)
    except Exception: j = raw
    return r.status, j, (ck.split(";")[0] if ck and ck.startswith("mx=") else None)
# pre-existing data, to check the startup snapshot
c0 = sqlite3.connect(DBP); c0.execute("CREATE TABLE keepme(x)"); c0.execute("INSERT INTO keepme VALUES(42)"); c0.commit(); c0.close()
srv = start("")
try:
    check("disabled when secret unset", call("/api/partner/health")[0] == 401)
finally: srv.terminate(); srv.wait()
srv = start(SEC)
try:
    bak = os.path.join(tmp, "backups", "t.db.pre-momentum-link.bak")
    check("startup snapshot taken", os.path.exists(bak) and sqlite3.connect(bak).execute("select x from keepme").fetchone() == (42,))
    s, j, _ = call("/api/partner/health"); check("health reports backup", s == 200 and j["backup"] == "t.db.pre-momentum-link.bak")
    # members sign up the normal way
    s, j, ckA = call("/api/register", {"name": "Alice", "pin": "1234"}, secret=None)
    s, j, ckB = call("/api/register", {"name": "Bob", "pin": "4321"}, secret=None)
    check("normal register still works + sets cookie", s == 200 and ckB)
    # auth
    for path in ("/api/partner/verify", "/api/partner/summary", "/api/partner/metric", "/api/partner/results", "/api/partner/register", "/api/partner/health"):
        check("no secret -> 401 " + path, call(path, {"name": "Alice", "pin": "1234", "user_id": 1}, secret=None, ip="9.9.9.9")[0] == 401)
    check("wrong secret -> 401", call("/api/partner/summary", {"user_id": 1}, secret="x" * 40, ip="9.9.9.9")[0] == 401)
    check("member cookie is not enough", call("/api/partner/summary", {"user_id": 1}, secret=None, cookie=ckA)[0] == 401)
    check("GET on partner path refused", call("/api/partner/summary", secret=SEC, method="GET")[0] in (401, 404))
    # verify
    s, j, _ = call("/api/partner/verify", {"name": "alice", "pin": "1234"}); check("verify ok (case-insensitive)", s == 200 and j.get("ok") and j["name"] == "Alice", (s, j))
    uidA = j["user_id"]
    s, j, _ = call("/api/partner/verify", {"name": "Alice", "pin": "9999"}); check("verify wrong pin -> ok false", s == 200 and j["ok"] is False)
    s, j, _ = call("/api/partner/verify", {"name": "Nobody", "pin": "1234"}); check("verify unknown -> ok false", j["ok"] is False)
    check("verify bad pin format 400", call("/api/partner/verify", {"name": "Alice", "pin": "12"})[0] == 400)
    s, j, _ = call("/api/partner/verify", {"name": "Bob", "pin": "4321"}); uidB = j["user_id"]
    for _ in range(5): call("/api/partner/verify", {"name": "Bob", "pin": "0000"})
    s, j, _ = call("/api/partner/verify", {"name": "Bob", "pin": "4321"}); check("verify shares the 5-try lockout", j["ok"] is False and j["locked"] is True)
    s, j, _ = call("/api/login", {"name": "Bob", "pin": "4321"}, secret=None); check("...and normal login sees the same lock", s == 429)
    c1 = sqlite3.connect(DBP); c1.execute("update users set locked_until=0"); c1.commit(); c1.close()
    # read + isolation
    s, lib, _ = call("/api/exercises", secret=None, cookie=ckA, method="GET"); ex = {e["name"]: e["id"] for e in lib["exercises"]}
    g = ex["Grip Strength"]
    call("/api/results", {"exercise_id": g, "primary": 80, "selectors": {"hand": "Right Hand"}, "day": "2026-09-01"}, secret=None, cookie=ckA)
    s, j, _ = call("/api/partner/summary", {"user_id": uidA}); check("summary for Alice", s == 200 and j["name"] == "Alice" and len(j["home"]["metrics"]) == 1 and len(j["library"]["exercises"]) == 35)
    s, j, _ = call("/api/partner/summary", {"user_id": uidB}); check("summary for Bob has none of Alice's data", s == 200 and j["name"] == "Bob" and j["home"]["metrics"] == [])
    s, j, _ = call("/api/partner/metric", {"user_id": uidB, "exercise_id": g}); check("Bob's metric view has no Alice results", s == 200 and j["results"] == [])
    s, j, _ = call("/api/partner/metric", {"user_id": uidA, "exercise_id": g}); check("Alice metric history", s == 200 and len(j["results"]) == 1 and j["results"][0]["p"] == 80)
    check("unknown user 404", call("/api/partner/summary", {"user_id": 99999})[0] == 404)
    check("bad user id 400", call("/api/partner/summary", {"user_id": "x"})[0] == 400)
    # log through the same validation
    s, j, _ = call("/api/partner/results", {"user_id": uidA, "exercise_id": g, "primary": 90}); check("same validation: selector required", s == 400 and "Which hand" in j["error"])
    s, j, _ = call("/api/partner/results", {"user_id": uidA, "exercise_id": g, "primary": -1, "selectors": {"hand": "Right Hand"}}); check("same validation: negative rejected", s == 400)
    s, j, _ = call("/api/partner/results", {"user_id": uidA, "exercise_id": g, "primary": 90, "selectors": {"hand": "Right Hand"}, "day": "2030-01-01"}); check("same validation: future date rejected", s == 400)
    s, j, _ = call("/api/partner/results", {"user_id": uidA, "exercise_id": g, "primary": 90, "selectors": {"hand": "Right Hand"}, "day": "2026-10-01"}); check("log ok + PB flag", s == 200 and j["pb"] is True)
    s, j, _ = call("/api/metric?exercise_id=%d" % g, secret=None, cookie=ckA, method="GET"); check("logged result visible on the Metrics site", len(j["results"]) == 2)
    s, j, _ = call("/api/metric?exercise_id=%d" % g, secret=None, cookie=ckB, method="GET"); check("...and not to Bob", j["results"] == [])
    # register
    s, j, _ = call("/api/partner/register", {"name": "alice", "pin": "1111"}); check("register taken name 409", s == 409)
    s, j, ck = call("/api/partner/register", {"name": "Carol", "pin": "2222"}); check("register new, no cookie issued", s == 200 and j["ok"] and not ck)
    s, j, ck = call("/api/login", {"name": "Carol", "pin": "2222"}, secret=None); check("new account logs in on the Metrics site", s == 200 and ck)
    # secret brute force lockout
    check("other caller unaffected by attacker lockout", call("/api/partner/health")[0] == 200)
    for _ in range(8): call("/api/partner/health", secret="y" * 40)
    check("bad-secret lockout", call("/api/partner/health")[0] == 429)
    call("/api/partner/health", secret="z" * 40, ip="9.9.9.9")
    check("attacker IP locked out", call("/api/partner/health", ip="9.9.9.9")[0] == 429)
finally:
    srv.terminate(); srv.wait()
print(f"\n{P} passed, {F} failed"); sys.exit(1 if F else 0)
