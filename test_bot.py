"""Quick smoke test for bot.py — pushes contexts and fires ticks."""

import json
import sys
import io
from pathlib import Path
from urllib import request as urlrequest

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BOT_URL = "http://localhost:8080"
DATASET = Path(__file__).parent / "dataset"
EXPANDED = DATASET / "expanded"


def post(path, body):
    data = json.dumps(body).encode()
    req = urlrequest.Request(f"{BOT_URL}{path}", data=data,
                             headers={"Content-Type": "application/json"})
    resp = urlrequest.urlopen(req, timeout=15)
    return json.loads(resp.read())


def get(path):
    req = urlrequest.Request(f"{BOT_URL}{path}")
    resp = urlrequest.urlopen(req, timeout=5)
    return json.loads(resp.read())


def main():
    print("=== HEALTHZ ===")
    print(json.dumps(get("/v1/healthz"), indent=2))

    # Push categories (from expanded dataset)
    print("\n=== PUSH CATEGORIES ===")
    cat_dir = EXPANDED / "categories"
    if not cat_dir.exists():
        cat_dir = DATASET / "categories"
    for f in cat_dir.glob("*.json"):
        cat = json.load(open(f))
        r = post("/v1/context", {
            "scope": "category", "context_id": cat["slug"],
            "version": 1, "payload": cat, "delivered_at": "2026-04-26T09:45:00Z"
        })
        print(f"  {cat['slug']}: accepted={r.get('accepted')}")

    # Push seed merchants
    print("\n=== PUSH MERCHANTS ===")
    merchants = json.load(open(DATASET / "merchants_seed.json"))["merchants"]
    for m in merchants:
        r = post("/v1/context", {
            "scope": "merchant", "context_id": m["merchant_id"],
            "version": 1, "payload": m, "delivered_at": "2026-04-26T09:45:00Z"
        })
        print(f"  {m['merchant_id'][:35]}: accepted={r.get('accepted')}")

    # Push seed customers
    print("\n=== PUSH CUSTOMERS ===")
    customers = json.load(open(DATASET / "customers_seed.json"))["customers"]
    for c in customers:
        r = post("/v1/context", {
            "scope": "customer", "context_id": c["customer_id"],
            "version": 1, "payload": c, "delivered_at": "2026-04-26T09:45:00Z"
        })
        print(f"  {c['customer_id'][:35]}: accepted={r.get('accepted')}")

    # Push seed triggers
    print("\n=== PUSH TRIGGERS ===")
    triggers = json.load(open(DATASET / "triggers_seed.json"))["triggers"]
    for t in triggers:
        r = post("/v1/context", {
            "scope": "trigger", "context_id": t["id"],
            "version": 1, "payload": t, "delivered_at": "2026-04-26T10:30:00Z"
        })
        print(f"  {t['id'][:40]}: accepted={r.get('accepted')}")

    # Verify healthz
    print("\n=== HEALTHZ AFTER PUSH ===")
    print(json.dumps(get("/v1/healthz"), indent=2))

    # Fire ticks with selected triggers
    test_triggers = [
        # Research digest (merchant-facing, dentist)
        "trg_001_research_digest_dentists",
        # Perf dip (merchant-facing, dentist)
        "trg_004_perf_dip_bharat",
        # Recall due (customer-facing)
        "trg_003_recall_due_priya",
        # IPL match (restaurant)
        "trg_010_ipl_match_delhi",
        # Supply alert (pharmacy)
        "trg_018_supply_atorvastatin_recall",
        # Active planning (restaurant)
        "trg_013_corporate_thali_planning",
        # Seasonal dip (gym)
        "trg_014_seasonal_acquisition_dip_powerhouse",
        # Customer lapsed hard (gym)
        "trg_015_winback_rashmi",
    ]

    print("\n=== TICK TEST ===")
    r = post("/v1/tick", {
        "now": "2026-04-26T10:35:00Z",
        "available_triggers": test_triggers
    })
    actions = r.get("actions", [])
    print(f"Got {len(actions)} actions\n")

    for a in actions:
        print(f"--- {a.get('trigger_id', '?')[:45]} ---")
        print(f"  send_as:  {a.get('send_as')}")
        print(f"  cta:      {a.get('cta')}")
        print(f"  body:     {a.get('body')}")
        print(f"  rationale: {a.get('rationale')[:120]}...")
        print()

    # Test reply handler: auto-reply
    print("=== AUTO-REPLY TEST ===")
    if actions:
        conv_id = actions[0]["conversation_id"]
        merchant_id = actions[0]["merchant_id"]

        auto_msg = "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly."
        for turn in range(2, 5):
            r = post("/v1/reply", {
                "conversation_id": conv_id, "merchant_id": merchant_id,
                "from_role": "merchant", "message": auto_msg,
                "received_at": "2026-04-26T10:42:00Z", "turn_number": turn
            })
            print(f"  Turn {turn}: action={r.get('action')}, "
                  f"body={r.get('body', '')[:60]}{'...' if len(r.get('body', '')) > 60 else ''}")

    # Test reply handler: intent commit
    print("\n=== INTENT COMMIT TEST ===")
    r = post("/v1/reply", {
        "conversation_id": "conv_intent_test", "merchant_id": "m_001_drmeera_dentist_delhi",
        "from_role": "merchant", "message": "Ok lets do it. Whats next?",
        "received_at": "2026-04-26T10:42:00Z", "turn_number": 2
    })
    print(f"  action={r.get('action')}, body={r.get('body', '')[:100]}")

    # Test reply handler: hostile
    print("\n=== HOSTILE TEST ===")
    r = post("/v1/reply", {
        "conversation_id": "conv_hostile_test", "merchant_id": "m_001_drmeera_dentist_delhi",
        "from_role": "merchant", "message": "Stop messaging me. This is useless spam.",
        "received_at": "2026-04-26T10:42:00Z", "turn_number": 2
    })
    print(f"  action={r.get('action')}, rationale={r.get('rationale', '')[:100]}")

    print("\n=== DONE ===")


if __name__ == "__main__":
    main()
