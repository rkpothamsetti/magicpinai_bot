"""Verify all 5 bot endpoints are working correctly."""

import json
import sys
import io
from pathlib import Path
from urllib import request as urlrequest, error as urlerror

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BOT = "http://localhost:8080"
DATASET = Path(__file__).parent / "dataset"
EXPANDED = DATASET / "expanded"
PASS = 0
FAIL = 0


def post(path, body):
    data = json.dumps(body).encode("utf-8")
    req = urlrequest.Request(f"{BOT}{path}", data=data,
                             headers={"Content-Type": "application/json"})
    resp = urlrequest.urlopen(req, timeout=15)
    return json.loads(resp.read())


def get(path):
    req = urlrequest.Request(f"{BOT}{path}")
    resp = urlrequest.urlopen(req, timeout=5)
    return json.loads(resp.read())


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} — {detail}")


# ─────────────────────────────────────────────────────────────────────────────
print("=" * 60)
print("  ENDPOINT VERIFICATION — Vera++ Bot")
print("=" * 60)

# 1. GET /v1/healthz
print("\n1/5  GET /v1/healthz")
r = get("/v1/healthz")
check("status is 'ok'", r.get("status") == "ok", f"got {r.get('status')}")
check("contexts_loaded has 4 scopes", set(r.get("contexts_loaded", {}).keys()) == {"category", "merchant", "customer", "trigger"})
check("all counts start at 0", all(v == 0 for v in r.get("contexts_loaded", {}).values()), str(r.get("contexts_loaded")))
check("uptime_seconds is present", "uptime_seconds" in r)

# 2. GET /v1/metadata
print("\n2/5  GET /v1/metadata")
r = get("/v1/metadata")
check("team_name present", bool(r.get("team_name")))
check("model present", bool(r.get("model")))
check("version present", bool(r.get("version")))
check("approach present", bool(r.get("approach")))

# 3. POST /v1/context — all 4 scopes + idempotency + version bump
print("\n3/5  POST /v1/context")

# 3a: category
cat_dir = EXPANDED / "categories" if (EXPANDED / "categories").exists() else DATASET / "categories"
cat = json.load(open(cat_dir / "dentists.json"))
r = post("/v1/context", {"scope": "category", "context_id": "dentists", "version": 1,
                          "payload": cat, "delivered_at": "2026-04-26T09:45:00Z"})
check("category push accepted", r.get("accepted") is True, str(r))

# 3b: merchant
merchants = json.load(open(DATASET / "merchants_seed.json"))["merchants"]
m = merchants[0]
r = post("/v1/context", {"scope": "merchant", "context_id": m["merchant_id"], "version": 1,
                          "payload": m, "delivered_at": "2026-04-26T09:45:00Z"})
check("merchant push accepted", r.get("accepted") is True, str(r))

# 3c: idempotency (same version)
r = post("/v1/context", {"scope": "merchant", "context_id": m["merchant_id"], "version": 1,
                          "payload": m, "delivered_at": "2026-04-26T09:45:00Z"})
check("idempotent re-push rejected", r.get("accepted") is False and r.get("reason") == "stale_version", str(r))

# 3d: version bump
r = post("/v1/context", {"scope": "merchant", "context_id": m["merchant_id"], "version": 2,
                          "payload": m, "delivered_at": "2026-04-26T10:30:00Z"})
check("version bump accepted", r.get("accepted") is True, str(r))

# 3e: customer
custs = json.load(open(DATASET / "customers_seed.json"))["customers"]
c = custs[0]
r = post("/v1/context", {"scope": "customer", "context_id": c["customer_id"], "version": 1,
                          "payload": c, "delivered_at": "2026-04-26T09:45:00Z"})
check("customer push accepted", r.get("accepted") is True, str(r))

# 3f: trigger
trigs = json.load(open(DATASET / "triggers_seed.json"))["triggers"]
t = trigs[0]
r = post("/v1/context", {"scope": "trigger", "context_id": t["id"], "version": 1,
                          "payload": t, "delivered_at": "2026-04-26T10:30:00Z"})
check("trigger push accepted", r.get("accepted") is True, str(r))

# 3g: invalid scope
r = post("/v1/context", {"scope": "invalid", "context_id": "x", "version": 1,
                          "payload": {}, "delivered_at": "2026-04-26T10:30:00Z"})
check("invalid scope rejected", r.get("accepted") is False, str(r))

# 3h: verify healthz counts
r = get("/v1/healthz")
cl = r.get("contexts_loaded", {})
check("healthz shows correct counts",
      cl.get("category") == 1 and cl.get("merchant") == 1 and cl.get("customer") == 1 and cl.get("trigger") == 1,
      str(cl))

# 4. POST /v1/tick
print("\n4/5  POST /v1/tick")

r = post("/v1/tick", {"now": "2026-04-26T10:35:00Z",
                       "available_triggers": [t["id"]]})
actions = r.get("actions", [])
check("tick returns actions list", isinstance(actions, list))
check("tick produced 1 action", len(actions) == 1, f"got {len(actions)}")

if actions:
    a = actions[0]
    check("action has conversation_id", bool(a.get("conversation_id")))
    check("action has merchant_id", bool(a.get("merchant_id")))
    check("action has send_as", a.get("send_as") in ("vera", "merchant_on_behalf"), a.get("send_as"))
    check("action has trigger_id", bool(a.get("trigger_id")))
    check("action has body (non-empty)", bool(a.get("body")))
    check("action has cta", bool(a.get("cta")))
    check("action has suppression_key", "suppression_key" in a)
    check("action has rationale", bool(a.get("rationale")))
    print(f"\n  Message preview: {a['body'][:120]}...")

# tick with empty triggers
r = post("/v1/tick", {"now": "2026-04-26T10:40:00Z", "available_triggers": []})
check("empty tick returns empty actions", r.get("actions") == [])

# tick with already-suppressed trigger
r = post("/v1/tick", {"now": "2026-04-26T10:45:00Z", "available_triggers": [t["id"]]})
check("suppressed trigger not re-sent", len(r.get("actions", [])) == 0, f"got {len(r.get('actions', []))}")

# 5. POST /v1/reply
print("\n5/5  POST /v1/reply")

conv_id = actions[0]["conversation_id"] if actions else "conv_test"
mid = m["merchant_id"]

# 5a: engaged reply
r = post("/v1/reply", {"conversation_id": conv_id, "merchant_id": mid,
                        "from_role": "merchant", "message": "Yes please send the abstract",
                        "received_at": "2026-04-26T10:42:00Z", "turn_number": 2})
check("engaged reply returns 'send'", r.get("action") == "send", str(r.get("action")))
check("reply has body", bool(r.get("body")), str(r))

# 5b: auto-reply detection
r = post("/v1/reply", {"conversation_id": "conv_auto_test", "merchant_id": mid,
                        "from_role": "merchant",
                        "message": "Thank you for contacting us! Our team will respond shortly.",
                        "received_at": "2026-04-26T10:42:00Z", "turn_number": 2})
check("first auto-reply → send", r.get("action") == "send", str(r.get("action")))

r = post("/v1/reply", {"conversation_id": "conv_auto_test", "merchant_id": mid,
                        "from_role": "merchant",
                        "message": "Thank you for contacting us! Our team will respond shortly.",
                        "received_at": "2026-04-26T10:43:00Z", "turn_number": 3})
check("second auto-reply → wait", r.get("action") == "wait", str(r.get("action")))

r = post("/v1/reply", {"conversation_id": "conv_auto_test", "merchant_id": mid,
                        "from_role": "merchant",
                        "message": "Thank you for contacting us! Our team will respond shortly.",
                        "received_at": "2026-04-26T10:44:00Z", "turn_number": 4})
check("third auto-reply → end", r.get("action") == "end", str(r.get("action")))

# 5c: intent commit
r = post("/v1/reply", {"conversation_id": "conv_intent_test", "merchant_id": mid,
                        "from_role": "merchant", "message": "Ok lets do it. Whats next?",
                        "received_at": "2026-04-26T10:42:00Z", "turn_number": 2})
check("intent commit → action mode", r.get("action") == "send", str(r.get("action")))
body = r.get("body", "").lower()
check("no re-qualifying after commit", not any(w in body for w in ["would you", "do you", "can you tell"]))

# 5d: hostile/rejection
r = post("/v1/reply", {"conversation_id": "conv_hostile_test", "merchant_id": mid,
                        "from_role": "merchant", "message": "Stop messaging me. This is useless spam.",
                        "received_at": "2026-04-26T10:42:00Z", "turn_number": 2})
check("hostile → end", r.get("action") == "end", str(r.get("action")))

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"  RESULTS:  {PASS} passed, {FAIL} failed, {PASS + FAIL} total")
print("=" * 60)

if FAIL > 0:
    sys.exit(1)
print("\n  All endpoints working correctly!")
