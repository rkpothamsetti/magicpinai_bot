"""
Vera++ — Deterministic merchant engagement bot.
Handles trigger, merchant, and category context to compose specific,
category-appropriate, personalized WhatsApp messages.

Run:  uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Vera++")
START = time.time()


@app.get("/")
def root():
    return {
        "bot": "Vera++",
        "status": "running",
        "endpoints": [
            "GET  /v1/healthz",
            "GET  /v1/metadata",
            "POST /v1/context",
            "POST /v1/tick",
            "POST /v1/reply",
        ],
    }

# ═══════════════════════════════════════════════════════════════════════════════
# DATA STORES
# ═══════════════════════════════════════════════════════════════════════════════

contexts: dict[tuple[str, str], dict] = {}       # (scope, context_id) → {version, payload}
conversations: dict[str, dict] = {}               # conversation_id → state
sent_suppression_keys: set[str] = set()           # dedup

# ═══════════════════════════════════════════════════════════════════════════════
# PYDANTIC MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str
    message: str
    received_at: str
    turn_number: int

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _owner(m: dict) -> str:
    return m.get("identity", {}).get("owner_first_name", "")

def _name(m: dict) -> str:
    return m.get("identity", {}).get("name", "")

def _city(m: dict) -> str:
    return m.get("identity", {}).get("city", "")

def _locality(m: dict) -> str:
    return m.get("identity", {}).get("locality", "")

def _cat_slug(m: dict) -> str:
    return m.get("category_slug", "")

def _langs(m: dict) -> list[str]:
    return m.get("identity", {}).get("languages", ["en"])

def _uses_hindi(m: dict) -> bool:
    return "hi" in _langs(m)

def _greeting(m: dict, cat: dict) -> str:
    """Category-appropriate greeting using owner first name."""
    slug = cat.get("slug", "")
    owner = _owner(m)
    if slug == "dentists" and owner:
        return f"Dr. {owner}"
    if owner:
        return owner
    return _name(m)

def _perf(m: dict) -> dict:
    return m.get("performance", {})

def _delta7d(m: dict) -> dict:
    return _perf(m).get("delta_7d", {})

def _active_offers(m: dict) -> list[dict]:
    return [o for o in m.get("offers", []) if o.get("status") == "active"]

def _signals(m: dict) -> list[str]:
    return m.get("signals", [])

def _sub(m: dict) -> dict:
    return m.get("subscription", {})

def _peer_stats(cat: dict) -> dict:
    return cat.get("peer_stats", {})

def _digest_by_id(cat: dict, item_id: str) -> dict | None:
    for d in cat.get("digest", []):
        if d.get("id") == item_id:
            return d
    return None

def _content_by_id(cat: dict, content_id: str) -> dict | None:
    for c in cat.get("patient_content_library", []):
        if c.get("id") == content_id:
            return c
    return None

def _fmt_pct(val: float) -> str:
    if val is None:
        return "N/A"
    return f"{val:+.0%}" if abs(val) < 1 else f"{val:+.1f}%"

def _fmt_pct_abs(val: float) -> str:
    if val is None:
        return "N/A"
    return f"{abs(val):.0%}" if abs(val) < 1 else f"{abs(val):.1f}%"

def _cust_name(c: dict) -> str:
    return c.get("identity", {}).get("name", "")

def _cust_lang(c: dict) -> str:
    return c.get("identity", {}).get("language_pref", "en")

def _cust_state(c: dict) -> str:
    return c.get("state", "active")

# ═══════════════════════════════════════════════════════════════════════════════
# COMPOSER — DISPATCH BY TRIGGER KIND
# ═══════════════════════════════════════════════════════════════════════════════

def compose(cat: dict, m: dict, t: dict, c: dict | None = None) -> dict | None:
    kind = t.get("kind", "")
    handler = _HANDLERS.get(kind, _compose_generic)
    return handler(cat, m, t, c)


# ── MERCHANT-FACING: Research & Compliance ──────────────────────────────────

def _compose_research_digest(cat, m, t, c):
    payload = t.get("payload", {})
    item_id = payload.get("top_item_id", "")
    digest = _digest_by_id(cat, item_id)
    greet = _greeting(m, cat)

    if digest:
        source = digest.get("source", "")
        title = digest.get("title", "")
        summary = digest.get("summary", "")
        trial_n = digest.get("trial_n")
        segment = digest.get("patient_segment", "")

        merchant_hook = ""
        cust_agg = m.get("customer_aggregate", {})
        if "high_risk" in segment and cust_agg.get("high_risk_adult_count"):
            merchant_hook = f"Relevant to your {cust_agg['high_risk_adult_count']} high-risk adult patients"
        elif cust_agg.get("total_unique_ytd"):
            merchant_hook = f"Worth checking against your {cust_agg['total_unique_ytd']} patient roster"

        trial_str = f"{trial_n:,}-patient trial: " if trial_n else ""
        body = (
            f"{greet}, {source.split(',')[0] if source else 'new research'} landed. "
            f"{merchant_hook + ' — ' if merchant_hook else ''}"
            f"{trial_str}{title}. "
            f"Worth a look (2-min read). Want me to pull the abstract + draft a patient-ed WhatsApp you can share? "
            f"— {source}"
        )
    else:
        body = (
            f"{greet}, new research digest dropped for {cat.get('display_name', cat.get('slug', ''))}. "
            f"Want me to pull the highlights relevant to your practice?"
        )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "template_name": "vera_research_digest_v1",
        "template_params": [greet, digest.get("title", "") if digest else ""],
        "rationale": f"External research digest with merchant-relevant anchor; source-cited for credibility. "
                     f"Open-ended CTA invites continuation.",
    }


def _compose_regulation_change(cat, m, t, c):
    payload = t.get("payload", {})
    item_id = payload.get("top_item_id", "")
    digest = _digest_by_id(cat, item_id)
    greet = _greeting(m, cat)
    deadline = payload.get("deadline_iso", "")

    if digest:
        title = digest.get("title", "")
        summary = digest.get("summary", "")
        actionable = digest.get("actionable", "")
        source = digest.get("source", "")
        deadline_str = f" Deadline: {deadline[:10]}." if deadline else ""
        body = (
            f"{greet}, heads up — {title}.{deadline_str} "
            f"{summary} "
            f"Action needed: {actionable} "
            f"Want me to draft a compliance checklist you can pin in your clinic? — {source}"
        )
    else:
        body = (
            f"{greet}, new regulation update for {cat.get('slug', '')}. "
            f"Deadline: {deadline[:10] if deadline else 'TBD'}. Want me to summarize the action items?"
        )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "template_name": "vera_compliance_alert_v1",
        "template_params": [greet, deadline[:10] if deadline else ""],
        "rationale": f"Compliance trigger with hard deadline; source-cited. "
                     f"Urgency {t.get('urgency', '?')} justifies proactive outreach.",
    }


def _compose_cde_opportunity(cat, m, t, c):
    payload = t.get("payload", {})
    item_id = payload.get("digest_item_id", "")
    digest = _digest_by_id(cat, item_id)
    greet = _greeting(m, cat)

    if digest:
        title = digest.get("title", "")
        source = digest.get("source", "")
        credits = payload.get("credits", digest.get("credits", ""))
        fee = payload.get("fee", "")
        date = digest.get("date", "")
        date_str = f" on {date[:10]}" if date else ""
        body = (
            f"{greet}, quick CDE heads-up: \"{title}\"{date_str}. "
            f"{credits} credits, {fee}. "
            f"Covers latest tech relevant to solo practices. Interested?"
        )
    else:
        body = f"{greet}, CDE opportunity available — want the details?"

    return {
        "body": body,
        "cta": "binary_yes_no",
        "send_as": "vera",
        "template_name": "vera_cde_v1",
        "template_params": [greet],
        "rationale": "CDE opportunity with credits and fee info; low-urgency but valuable for professional development.",
    }


def _compose_supply_alert(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    molecule = payload.get("molecule", "item")
    batches = payload.get("affected_batches", [])
    mfr = payload.get("manufacturer", "")
    batch_str = ", ".join(batches[:3]) if batches else "affected batches"

    chronic_count = m.get("customer_aggregate", {}).get("chronic_rx_count", 0)
    cust_hook = f"Your repeat-Rx list likely has customers on these batches." if chronic_count else ""

    body = (
        f"{greet}, urgent: voluntary recall on {molecule} — batches {batch_str} by {mfr}. "
        f"{cust_hook} "
        f"Want me to draft the customer notification + replacement-pickup workflow?"
    )

    return {
        "body": body,
        "cta": "binary_yes_no",
        "send_as": "vera",
        "template_name": "vera_supply_alert_v1",
        "template_params": [greet, molecule],
        "rationale": f"Urgent supply alert (urgency {t.get('urgency', 5)}); batch-specific, actionable. "
                     f"Offering to draft customer communications reduces merchant effort.",
    }


# ── MERCHANT-FACING: Performance ────────────────────────────────────────────

def _compose_perf_dip(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    metric = payload.get("metric", "views")
    delta = payload.get("delta_pct", 0)
    window = payload.get("window", "7d")
    peer_avg = _peer_stats(cat).get(f"avg_{metric}_30d", _peer_stats(cat).get(f"avg_{metric}", ""))

    peer_str = f" (peer avg: {peer_avg})" if peer_avg else ""
    offers = _active_offers(m)
    offer_hook = f" Your \"{offers[0]['title']}\" is still active — let's push it harder." if offers else ""
    hindi = " Kuch karna chahein?" if _uses_hindi(m) else ""

    body = (
        f"{greet}, your {metric} dropped {_fmt_pct_abs(delta)} this week{peer_str}. "
        f"This needs attention — fewer {metric} means fewer leads reaching you.{offer_hook} "
        f"Want me to diagnose what changed + suggest 2-3 quick fixes?{hindi}"
    )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "template_name": "vera_perf_dip_v1",
        "template_params": [greet, metric, _fmt_pct_abs(delta)],
        "rationale": f"Performance dip on {metric} ({_fmt_pct(delta)} {window}); loss aversion framing "
                     f"with concrete metric + peer benchmark. Offering diagnostic reduces merchant effort.",
    }


def _compose_perf_spike(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    metric = payload.get("metric", "views")
    delta = payload.get("delta_pct", 0)
    driver = payload.get("likely_driver", "")

    driver_str = f" Likely driver: {driver.replace('_', ' ')}." if driver else ""
    body = (
        f"{greet}, nice — your {metric} are up {_fmt_pct_abs(delta)} this week.{driver_str} "
        f"Want me to help you capitalize while the momentum is live?"
    )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "template_name": "vera_perf_spike_v1",
        "template_params": [greet, metric, _fmt_pct_abs(delta)],
        "rationale": f"Performance spike on {metric}; positive reinforcement + offer to capitalize. "
                     f"Builds engagement on good news.",
    }


def _compose_seasonal_perf_dip(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    metric = payload.get("metric", "views")
    delta = payload.get("delta_pct", 0)
    season = payload.get("season_note", "seasonal pattern")

    members = m.get("customer_aggregate", {}).get("total_active_members", "")
    member_str = f" your {members} members" if members else " your existing base"

    body = (
        f"{greet}, your {metric} are down {_fmt_pct_abs(delta)} this week — but this is the "
        f"normal {season.replace('_', ' ')} (every metro {cat.get('slug', 'business').rstrip('s')} sees this). "
        f"Action: skip ad spend now, save it for the Sept-Oct rebound. For now, focus retention on"
        f"{member_str}. Want me to draft a retention challenge to keep them through the dip?"
    )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "template_name": "vera_seasonal_dip_v1",
        "template_params": [greet, _fmt_pct_abs(delta)],
        "rationale": f"Seasonal dip reframe — the drop is expected, not alarming. "
                     f"Redirects merchant energy toward retention instead of panic.",
    }


def _compose_milestone_reached(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    metric = payload.get("metric", "reviews")
    value = payload.get("value_now", 0)
    milestone = payload.get("milestone_value", 0)
    imminent = payload.get("is_imminent", False)

    if imminent:
        gap = milestone - value
        body = (
            f"{greet}, you're at {value} {metric.replace('_', ' ')} — just {gap} away from {milestone}! "
            f"Crossing {milestone} boosts your Google ranking signal. "
            f"Want me to draft a \"leave a review\" nudge for your recent happy customers?"
        )
    else:
        body = (
            f"{greet}, congrats — you hit {value} {metric.replace('_', ' ')}! "
            f"This puts you in the top tier for {_locality(m)}. "
            f"Want me to create a celebratory Google post?"
        )

    return {
        "body": body,
        "cta": "binary_yes_no",
        "send_as": "vera",
        "template_name": "vera_milestone_v1",
        "template_params": [greet, str(milestone)],
        "rationale": f"Milestone {'approaching' if imminent else 'reached'} — "
                     f"social proof + concrete action to push past the threshold.",
    }


# ── MERCHANT-FACING: Engagement & Re-engagement ────────────────────────────

def _compose_curious_ask(cat, m, t, c):
    greet = _greeting(m, cat)
    slug = cat.get("slug", "")

    ask_map = {
        "dentists": "what procedure has been most asked-for this week at your clinic",
        "salons": "what service has been most asked-for this week",
        "restaurants": "what dish has been your top seller this week",
        "gyms": "what class or program has seen the most interest this week",
        "pharmacies": "what OTC product has been flying off your shelf this week",
    }
    question = ask_map.get(slug, "what's been most in demand this week")

    body = (
        f"{greet}, quick check — {question}? "
        f"I'll turn your answer into a Google post + a ready-to-send WhatsApp reply "
        f"for when customers ask about pricing. Takes 5 min."
    )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "template_name": "vera_curious_ask_v1",
        "template_params": [greet],
        "rationale": "Curious-ask cadence — asking the merchant drives engagement without "
                     "requiring a commitment. Reciprocity lever: offering deliverables in return.",
    }


def _compose_dormant(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    days = payload.get("days_since_last_merchant_message", 14)
    last_topic = payload.get("last_topic", "")

    perf = _perf(m)
    views = perf.get("views", 0)
    calls = perf.get("calls", 0)

    topic_hook = f" (last we spoke about {last_topic.replace('_', ' ')})" if last_topic else ""
    hindi = " Ek quick update hai." if _uses_hindi(m) else ""

    body = (
        f"{greet}, it's been {days} days{topic_hook}.{hindi} "
        f"Quick snapshot: {views} views and {calls} calls in the last 30 days. "
        f"Want a 2-min rundown of what's changed and where you stand vs peers?"
    )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "template_name": "vera_reengagement_v1",
        "template_params": [greet, str(days)],
        "rationale": f"Dormant re-engagement after {days}d silence. Uses performance data as a hook "
                     f"rather than guilt-tripping. Low-friction ask.",
    }


def _compose_review_theme(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    theme = payload.get("theme", "feedback")
    occurrences = payload.get("occurrences_30d", 0)
    trend = payload.get("trend", "")
    quote = payload.get("common_quote", "")

    quote_str = f' One customer said: "{quote}"' if quote else ""
    trend_str = f" and it's {trend}" if trend else ""

    body = (
        f"{greet}, {occurrences} reviews this month mention \"{theme.replace('_', ' ')}\"{trend_str}."
        f"{quote_str}. "
        f"Want me to draft a Google post or a review-response template that addresses this head-on?"
    )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "template_name": "vera_review_theme_v1",
        "template_params": [greet, theme],
        "rationale": f"Review theme \"{theme}\" detected ({occurrences} mentions). Proactive reputation "
                     f"management with concrete deliverable offer.",
    }


# ── MERCHANT-FACING: Business & Subscription ───────────────────────────────

def _compose_renewal_due(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    days = payload.get("days_remaining", _sub(m).get("days_remaining", 0))
    plan = payload.get("plan", _sub(m).get("plan", "Pro"))
    amount = payload.get("renewal_amount", "")

    perf = _perf(m)
    views = perf.get("views", 0)
    calls = perf.get("calls", 0)

    amount_str = f" at ₹{amount}" if amount else ""
    hindi = " Renewal simple hai — ek reply mein ho jayega." if _uses_hindi(m) else ""

    body = (
        f"{greet}, your {plan} subscription expires in {days} days. "
        f"In the last 30 days alone: {views} views, {calls} calls — "
        f"that pipeline pauses if the profile goes inactive. "
        f"Reply YES to renew{amount_str}.{hindi}"
    )

    return {
        "body": body,
        "cta": "binary_yes_no",
        "send_as": "vera",
        "template_name": "vera_renewal_v1",
        "template_params": [greet, str(days)],
        "rationale": f"Renewal due in {days}d; loss aversion (pipeline pauses) + "
                     f"concrete value anchored in their own 30d performance.",
    }


def _compose_winback(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    days_expired = payload.get("days_since_expiry", 0)
    dip_pct = payload.get("perf_dip_pct", 0)
    lapsed_added = payload.get("lapsed_customers_added_since_expiry", 0)

    body = (
        f"{greet}, it's been {days_expired} days since your subscription expired. "
        f"Since then, your profile visibility dropped {_fmt_pct_abs(dip_pct)}"
        f"{f' and {lapsed_added} customers moved to lapsed' if lapsed_added else ''}. "
        f"Reactivation takes 2 minutes — want me to walk you through it?"
    )

    return {
        "body": body,
        "cta": "binary_yes_no",
        "send_as": "vera",
        "template_name": "vera_winback_v1",
        "template_params": [greet, str(days_expired)],
        "rationale": f"Winback after {days_expired}d expiry; quantified loss ({_fmt_pct(dip_pct)} dip) "
                     f"creates urgency without being pushy.",
    }


def _compose_gbp_unverified(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    uplift = payload.get("estimated_uplift_pct", 0.30)
    path = payload.get("verification_path", "postcard or phone call")

    views = _perf(m).get("views", 0)
    projected = int(views * (1 + uplift))

    body = (
        f"{greet}, your Google Business Profile isn't verified yet — "
        f"verified profiles typically see ~{_fmt_pct_abs(uplift)} more views. "
        f"That's roughly {views} → {projected} views/month for you. "
        f"Verification is free via {path}. Want me to start the process?"
    )

    return {
        "body": body,
        "cta": "binary_yes_no",
        "send_as": "vera",
        "template_name": "vera_gbp_verify_v1",
        "template_params": [greet],
        "rationale": "GBP verification with projected uplift personalized to merchant's current views. "
                     "Concrete before/after numbers drive action.",
    }


def _compose_competitor_opened(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    competitor = payload.get("competitor_name", "a new business")
    distance = payload.get("distance_km", "")
    their_offer = payload.get("their_offer", "")

    dist_str = f" {distance}km away" if distance else " nearby"
    offer_str = f" with \"{their_offer}\"" if their_offer else ""

    my_offers = _active_offers(m)
    my_offer_str = f" Your \"{my_offers[0]['title']}\" is stronger — " if my_offers else " "

    body = (
        f"{greet}, new competitor alert: {competitor} just opened{dist_str}{offer_str}. "
        f"{my_offer_str}"
        f"Want me to audit your listing vs theirs and suggest differentiators?"
    )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "template_name": "vera_competitor_v1",
        "template_params": [greet, competitor],
        "rationale": f"Competitor intelligence — {competitor}{dist_str}. "
                     f"Loss aversion framing with concrete offer to help differentiate.",
    }


# ── MERCHANT-FACING: Events & Seasonal ──────────────────────────────────────

def _compose_festival(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    festival = payload.get("festival", "upcoming festival")
    days_until = payload.get("days_until", "")
    slug = cat.get("slug", "")

    days_str = f" in {days_until} days" if days_until else ""
    slug_advice = {
        "salons": "Bridal + festive grooming bookings spike 2-3 weeks before. Time to push your festive packages.",
        "restaurants": "Festival dinners and catering orders start picking up now. Plan your special menu early.",
        "dentists": "Post-festival period typically sees checkup bookings rise — plan your campaign now.",
        "gyms": "Pre-festival fitness push works well. Members want to look good — leverage that.",
        "pharmacies": "Festival season means higher footfall. Stock up on gift hampers, immunity kits, and OTC essentials.",
    }
    advice = slug_advice.get(slug, "Good time to plan your festive campaign.")

    body = (
        f"{greet}, {festival}{days_str}. {advice} "
        f"Want me to draft a festive offer + Google post for {_name(m)}?"
    )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "template_name": "vera_festival_v1",
        "template_params": [greet, festival],
        "rationale": f"Festival trigger ({festival}); category-specific seasonal advice. "
                     f"Early planning window for maximum impact.",
    }


def _compose_ipl_match(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    match = payload.get("match", "IPL match")
    venue = payload.get("venue", "")
    city = payload.get("city", "")
    match_time = payload.get("match_time_iso", "")
    is_weeknight = payload.get("is_weeknight", True)

    offers = _active_offers(m)
    offer_ref = f"Your \"{offers[0]['title']}\" is already active" if offers else "No active offer right now"

    if not is_weeknight:
        strategy = (
            f"Saturday IPL usually shifts dine-in traffic down ~12% (people watch at home). "
            f"Push delivery-only specials instead. {offer_ref} — "
            f"want me to reposition it as a match-night delivery deal + draft a quick Insta story?"
        )
    else:
        strategy = (
            f"Weeknight IPL typically lifts delivery orders ~20-30% in {city}. "
            f"{offer_ref}. Want me to draft a match-night deal + a Google post to go live by 5pm?"
        )

    time_str = ""
    if match_time:
        try:
            time_str = f", {match_time[11:16]}"
        except Exception:
            pass

    body = f"{greet}, {match} at {venue} tonight{time_str}. {strategy}"

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "template_name": "vera_ipl_v1",
        "template_params": [greet, match],
        "rationale": f"IPL match trigger with data-informed strategy "
                     f"({'weekend pivot to delivery' if not is_weeknight else 'weeknight uplift'}). "
                     f"References existing offer. Concrete deliverables offered.",
    }


def _compose_category_seasonal(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    season = payload.get("season", "")
    trends = payload.get("trends", [])

    trends_str = ", ".join(trends[:3]) if isinstance(trends, list) and trends else str(trends)
    body = (
        f"{greet}, {season.replace('_', ' ')} demand shift alert: {trends_str}. "
        f"Want me to help adjust your inventory/shelf placement to match the trend?"
    )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "template_name": "vera_seasonal_v1",
        "template_params": [greet, season],
        "rationale": f"Seasonal demand shift with specific trend data. "
                     f"Actionable advice for inventory/promotion adjustment.",
    }


# ── MERCHANT-FACING: Planning ───────────────────────────────────────────────

def _compose_active_planning(cat, m, t, c):
    payload = t.get("payload", {})
    greet = _greeting(m, cat)
    topic = payload.get("intent_topic", "your idea")
    last_msg = payload.get("merchant_last_message", "")

    topic_clean = topic.replace("_", " ")
    body = (
        f"{greet}, picking up on {topic_clean} — I've drafted a starter version for you. "
        f"Let me know if you'd like me to flesh it out with pricing tiers, "
        f"target audience, and a promotional plan. Ready when you are."
    )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera",
        "template_name": "vera_planning_v1",
        "template_params": [greet, topic_clean],
        "rationale": f"Continuing active planning intent ({topic_clean}). "
                     f"Effort externalization — draft already prepared, merchant just reviews.",
    }


# ── CUSTOMER-FACING ─────────────────────────────────────────────────────────

def _compose_recall_due(cat, m, t, c):
    if not c:
        return _compose_generic(cat, m, t, c)

    payload = t.get("payload", {})
    cname = _cust_name(c)
    mname = _name(m)
    owner = _owner(m)
    service = payload.get("service_due", "checkup").replace("_", " ")
    slots = payload.get("available_slots", [])
    lang = _cust_lang(c)

    offers = _active_offers(m)
    price_hook = f" {offers[0]['title']}" if offers else ""

    slot_str = ""
    if slots:
        slot_labels = [s.get("label", "") for s in slots[:2]]
        if "hi" in lang.lower():
            slot_str = f"Apke liye slots: {' ya '.join(slot_labels)}."
        else:
            slot_str = f"Available slots: {' or '.join(slot_labels)}."

    greeting_name = f"Dr. {owner}'s clinic" if cat.get("slug") == "dentists" and owner else mname

    if "hi" in lang.lower():
        body = (
            f"Hi {cname}, {greeting_name} here. "
            f"Your {service} recall is due. "
            f"{slot_str}{' ' + price_hook + '.' if price_hook else ''} "
            f"Reply to book ya apna time batayein."
        )
    else:
        body = (
            f"Hi {cname}, {greeting_name} here. "
            f"Your {service} recall is due. "
            f"{slot_str}{' ' + price_hook + '.' if price_hook else ''} "
            f"Reply to book or tell us a time that works."
        )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "merchant_on_behalf",
        "template_name": "merchant_recall_v1",
        "template_params": [cname, greeting_name],
        "rationale": f"Customer recall reminder via merchant's number. "
                     f"Language pref ({lang}) honored. Real slots and pricing from context.",
    }


def _compose_customer_lapsed_soft(cat, m, t, c):
    if not c:
        return _compose_generic(cat, m, t, c)

    cname = _cust_name(c)
    mname = _name(m)
    owner = _owner(m)
    lang = _cust_lang(c)
    offers = _active_offers(m)

    greeting_from = owner if owner else mname
    offer_str = f" {offers[0]['title']} abhi available hai." if offers and "hi" in lang.lower() else (
        f" {offers[0]['title']} is available." if offers else ""
    )

    if "hi" in lang.lower():
        body = (
            f"Hi {cname}, {greeting_from} from {mname} here. "
            f"Kaafi time ho gaya! Aapki kami mehsoos hoti hai.{offer_str} "
            f"Wapas aana chahein toh reply karein — special slot hold karwa dete hain."
        )
    else:
        body = (
            f"Hi {cname}, {greeting_from} from {mname} here. "
            f"It's been a while — we'd love to see you back.{offer_str} "
            f"Reply and we'll hold a slot for you."
        )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "merchant_on_behalf",
        "template_name": "merchant_lapsed_soft_v1",
        "template_params": [cname, greeting_from],
        "rationale": "Soft lapse re-engagement — warm tone, no guilt, real offer referenced. "
                     "Language preference honored.",
    }


def _compose_customer_lapsed_hard(cat, m, t, c):
    if not c:
        return _compose_generic(cat, m, t, c)

    payload = t.get("payload", {})
    cname = _cust_name(c)
    mname = _name(m)
    owner = _owner(m)
    lang = _cust_lang(c)
    days = payload.get("days_since_last_visit", "")
    focus = payload.get("previous_focus", "")

    greeting_from = owner if owner else mname
    focus_str = f" that fit {focus.replace('_', ' ')} goals" if focus else ""
    days_str = f"It's been about {days // 7} weeks" if days else "It's been a while"

    body = (
        f"Hi {cname} 👋 {greeting_from} from {mname} here. "
        f"{days_str} — happens to everyone, no judgment. "
        f"We've added new programs{focus_str}. "
        f"Want me to hold a free trial spot for you? Reply YES — no commitment, no auto-charge."
    )

    return {
        "body": body,
        "cta": "binary_yes_no",
        "send_as": "merchant_on_behalf",
        "template_name": "merchant_lapsed_hard_v1",
        "template_params": [cname, greeting_from],
        "rationale": f"Hard lapse winback — no-shame framing, previous interest ({focus}) acknowledged, "
                     f"barrier removal (no commitment, no auto-charge).",
    }


def _compose_appointment_tomorrow(cat, m, t, c):
    if not c:
        return _compose_generic(cat, m, t, c)

    cname = _cust_name(c)
    mname = _name(m)
    lang = _cust_lang(c)
    locality = _locality(m)

    loc_str = f" ({locality})" if locality else ""

    if "hi" in lang.lower():
        body = (
            f"Hi {cname}, reminder: aapki appointment {mname}{loc_str} mein kal hai. "
            f"Hum aapka wait karenge! Reschedule karna ho toh reply karein."
        )
    else:
        body = (
            f"Hi {cname}, reminder: your appointment at {mname}{loc_str} is tomorrow. "
            f"Looking forward to seeing you! Reply if you need to reschedule."
        )

    return {
        "body": body,
        "cta": "none",
        "send_as": "merchant_on_behalf",
        "template_name": "merchant_appointment_v1",
        "template_params": [cname, mname],
        "rationale": "Appointment reminder — friendly, informational. "
                     "Reschedule option reduces no-shows without being pushy.",
    }


def _compose_chronic_refill(cat, m, t, c):
    if not c:
        return _compose_generic(cat, m, t, c)

    payload = t.get("payload", {})
    cname = _cust_name(c)
    mname = _name(m)
    molecules = payload.get("molecule_list", [])
    stock_out = payload.get("stock_runs_out_iso", "")
    delivery_saved = payload.get("delivery_address_saved", False)
    lang = _cust_lang(c)

    mol_str = ", ".join(molecules) if molecules else "medicines"
    date_str = stock_out[:10] if stock_out else "soon"

    offers = _active_offers(m)
    discount_str = ""
    for o in offers:
        if "senior" in o.get("title", "").lower() or "delivery" in o.get("title", "").lower():
            discount_str += f" {o['title']}."

    is_senior = c.get("identity", {}).get("senior_citizen", False) or \
                c.get("identity", {}).get("age_band", "").startswith("6")
    salutation = "Namaste" if is_senior or "hi" in lang.lower() else "Hi"
    name_ref = cname.split("(")[0].strip() if "(" in cname else cname

    delivery_str = " Free home delivery to your saved address." if delivery_saved else ""

    body = (
        f"{salutation} — {mname} yahan. "
        f"{name_ref} ji ki {len(molecules)} monthly medicines ({mol_str}) {date_str} ko khatam hongi. "
        f"Same dose, same brand ready hai.{discount_str}{delivery_str} "
        f"Reply CONFIRM to dispatch."
    )

    return {
        "body": body,
        "cta": "binary_yes_no",
        "send_as": "merchant_on_behalf",
        "template_name": "merchant_refill_v1",
        "template_params": [name_ref, mname, mol_str],
        "rationale": "Chronic refill reminder — precise molecule names, exact date, "
                     "applicable discounts, saved delivery. Frictionless CONFIRM CTA.",
    }


def _compose_trial_followup(cat, m, t, c):
    if not c:
        return _compose_generic(cat, m, t, c)

    payload = t.get("payload", {})
    cname = _cust_name(c)
    mname = _name(m)
    owner = _owner(m)
    sessions = payload.get("next_session_options", [])
    lang = _cust_lang(c)

    slot_str = ""
    if sessions:
        labels = [s.get("label", "") for s in sessions[:2]]
        slot_str = f" Next session: {' or '.join(labels)}."

    greeting_from = owner if owner else mname
    body = (
        f"Hi {cname}, {greeting_from} from {mname} here. "
        f"Hope you enjoyed the trial!{slot_str} "
        f"Reply to book your next session — no commitment, just show up and try."
    )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "merchant_on_behalf",
        "template_name": "merchant_trial_followup_v1",
        "template_params": [cname, greeting_from],
        "rationale": "Trial followup — warm, no-pressure. Real next-session options from context.",
    }


def _compose_wedding_followup(cat, m, t, c):
    if not c:
        return _compose_generic(cat, m, t, c)

    payload = t.get("payload", {})
    cname = _cust_name(c)
    mname = _name(m)
    owner = _owner(m)
    days_to_wedding = payload.get("days_to_wedding", "")
    next_step = payload.get("next_step_window_open", "").replace("_", " ")

    greeting_from = owner if owner else mname
    days_str = f"{days_to_wedding} days to your wedding" if days_to_wedding else "Your wedding is coming up"

    body = (
        f"Hi {cname} 💍 {greeting_from} from {mname} here. "
        f"{days_str} — perfect window to start {next_step}. "
        f"Want me to block your preferred slot for the first session?"
    )

    return {
        "body": body,
        "cta": "binary_yes_no",
        "send_as": "merchant_on_behalf",
        "template_name": "merchant_bridal_v1",
        "template_params": [cname, greeting_from],
        "rationale": f"Bridal followup — days-to-wedding specificity, next-step window from trigger. "
                     f"Single binary CTA for low friction.",
    }


# ── GENERIC FALLBACK ────────────────────────────────────────────────────────

def _compose_generic(cat, m, t, c):
    greet = _greeting(m, cat)
    kind = t.get("kind", "update")
    payload = t.get("payload", {})

    perf = _perf(m)
    views = perf.get("views", 0)
    calls = perf.get("calls", 0)

    body = (
        f"{greet}, quick update — your profile had {views} views and {calls} calls "
        f"in the last 30 days. Want a quick rundown of what's working and what to improve?"
    )

    return {
        "body": body,
        "cta": "open_ended",
        "send_as": "vera" if not c else "merchant_on_behalf",
        "template_name": "vera_generic_v1",
        "template_params": [greet],
        "rationale": f"Fallback composition for trigger kind '{kind}'; "
                     f"anchored on merchant's own performance data.",
    }


# ── DISPATCH TABLE ──────────────────────────────────────────────────────────

_HANDLERS: dict[str, Any] = {
    # Research & compliance
    "research_digest": _compose_research_digest,
    "regulation_change": _compose_regulation_change,
    "cde_opportunity": _compose_cde_opportunity,
    "supply_alert": _compose_supply_alert,
    # Performance
    "perf_dip": _compose_perf_dip,
    "perf_spike": _compose_perf_spike,
    "seasonal_perf_dip": _compose_seasonal_perf_dip,
    "milestone_reached": _compose_milestone_reached,
    # Engagement
    "curious_ask_due": _compose_curious_ask,
    "dormant_with_vera": _compose_dormant,
    "review_theme_emerged": _compose_review_theme,
    # Business
    "renewal_due": _compose_renewal_due,
    "winback_eligible": _compose_winback,
    "gbp_unverified": _compose_gbp_unverified,
    "competitor_opened": _compose_competitor_opened,
    # Events
    "festival_upcoming": _compose_festival,
    "ipl_match_today": _compose_ipl_match,
    "category_seasonal": _compose_category_seasonal,
    # Planning
    "active_planning_intent": _compose_active_planning,
    # Customer-facing
    "recall_due": _compose_recall_due,
    "customer_lapsed_soft": _compose_customer_lapsed_soft,
    "customer_lapsed_hard": _compose_customer_lapsed_hard,
    "appointment_tomorrow": _compose_appointment_tomorrow,
    "chronic_refill_due": _compose_chronic_refill,
    "trial_followup": _compose_trial_followup,
    "wedding_package_followup": _compose_wedding_followup,
}


# ═══════════════════════════════════════════════════════════════════════════════
# REPLY HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

_AUTO_REPLY_PATTERNS = [
    "thank you for contacting",
    "our team will respond",
    "we will get back to you",
    "automated reply",
    "automated assistant",
    "auto-reply",
    "hamari team aapko jald",
    "aapki jaankari ke liye",
    "shukriya, hamari team",
]

_REJECTION_PATTERNS = [
    "not interested",
    "stop messaging",
    "stop sending",
    "don't contact",
    "don't message",
    "unsubscribe",
    "leave me alone",
    "spam",
    "useless",
    "waste of time",
    "band karo",
    "mat bhejo",
    "nahi chahiye",
]

_INTENT_COMMIT_PATTERNS = [
    "yes", "yeah", "yep", "sure", "ok let", "lets do it", "let's do it",
    "go ahead", "proceed", "haan", "ha ", "haan bhai", "kar do",
    "do it", "start", "confirm", "chalega", "chalo",
    "what's next", "whats next", "next step",
]


def _is_auto_reply(msg: str) -> bool:
    lower = msg.lower().strip()
    return any(p in lower for p in _AUTO_REPLY_PATTERNS)


def _is_rejection(msg: str) -> bool:
    lower = msg.lower().strip()
    return any(p in lower for p in _REJECTION_PATTERNS)


def _is_intent_commit(msg: str) -> bool:
    lower = msg.lower().strip()
    return any(p in lower for p in _INTENT_COMMIT_PATTERNS)


def handle_reply(body: ReplyBody, conv: dict) -> dict:
    msg = body.message
    turns = conv.get("turns", [])
    merchant = conv.get("merchant", {})
    trigger = conv.get("trigger", {})
    category = conv.get("category", {})

    greet = _greeting(merchant, category) if merchant and category else ""

    # Count total merchant auto-replies in this conversation
    auto_count = sum(
        1 for t in turns
        if t.get("from") in ("merchant", "customer") and _is_auto_reply(t.get("body", ""))
    )

    if _is_auto_reply(msg):
        auto_count += 1
        if auto_count >= 3:
            return {
                "action": "end",
                "rationale": f"Auto-reply detected {auto_count}x consecutively. "
                             f"No real engagement signal; closing conversation.",
            }
        elif auto_count >= 2:
            return {
                "action": "wait",
                "wait_seconds": 86400,
                "rationale": f"Same auto-reply {auto_count}x — owner likely not at phone. "
                             f"Waiting 24h before retry.",
            }
        else:
            return {
                "action": "send",
                "body": f"Looks like an auto-reply 😊 {greet}, when you see this just reply 'Yes' and I'll pick up.",
                "cta": "binary_yes_no",
                "rationale": "First auto-reply detected; one explicit prompt to flag for owner.",
            }

    if _is_rejection(msg):
        return {
            "action": "end",
            "rationale": "Merchant explicitly opted out. Closing conversation; "
                         "suppressing future outreach on this thread.",
        }

    if _is_intent_commit(msg):
        kind = trigger.get("kind", "")
        kind_clean = kind.replace("_", " ") if kind else "your request"

        return {
            "action": "send",
            "body": (
                f"On it{', ' + greet if greet else ''}. Working on {kind_clean} now — "
                f"I'll have the draft ready in a moment. Sit tight."
            ),
            "cta": "none",
            "rationale": "Merchant committed — switching from qualifying to action-execution. "
                         "Concrete next step communicated.",
        }

    # Default: engaged response — continue conversation
    kind = trigger.get("kind", "")
    return {
        "action": "send",
        "body": (
            f"Got it{', ' + greet if greet else ''}. "
            f"Let me look into that and get back to you with specifics."
        ),
        "cta": "open_ended",
        "rationale": "Engaged merchant reply acknowledged; continuing conversation thread.",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/v1/healthz")
async def healthz():
    counts: dict[str, int] = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _) in contexts:
        if scope in counts:
            counts[scope] += 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": counts,
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Vera++",
        "team_members": ["Krish"],
        "model": "deterministic-v1",
        "approach": "Rule-based trigger-kind dispatch with category voice matching, "
                    "merchant-specific data anchoring, and context-aware templating",
        "contact_email": "krish@example.com",
        "version": "1.0.0",
        "submitted_at": datetime.utcnow().isoformat() + "Z",
    }


@app.post("/v1/context")
async def push_context(body: CtxBody):
    valid_scopes = {"category", "merchant", "customer", "trigger"}
    if body.scope not in valid_scopes:
        return {"accepted": False, "reason": "invalid_scope", "details": f"Must be one of {valid_scopes}"}

    key = (body.scope, body.context_id)
    cur = contexts.get(key)
    if cur and cur["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}

    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.utcnow().isoformat() + "Z",
    }


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    for trg_id in body.available_triggers:
        trg_entry = contexts.get(("trigger", trg_id))
        if not trg_entry:
            continue
        trg = trg_entry["payload"]

        merchant_id = trg.get("merchant_id")
        if not merchant_id:
            continue
        m_entry = contexts.get(("merchant", merchant_id))
        if not m_entry:
            continue
        merchant = m_entry["payload"]

        cat_slug = merchant.get("category_slug", "")
        cat_entry = contexts.get(("category", cat_slug))
        category = cat_entry["payload"] if cat_entry else {}

        customer = None
        customer_id = trg.get("customer_id")
        if customer_id:
            c_entry = contexts.get(("customer", customer_id))
            customer = c_entry["payload"] if c_entry else None

        sup_key = trg.get("suppression_key", "")
        if sup_key and sup_key in sent_suppression_keys:
            continue

        result = compose(category, merchant, trg, customer)
        if not result:
            continue

        if sup_key:
            sent_suppression_keys.add(sup_key)

        conv_id = f"conv_{merchant_id}_{trg_id}"
        conversations[conv_id] = {
            "turns": [{"from": "vera", "body": result["body"]}],
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "trigger_id": trg_id,
            "trigger": trg,
            "merchant": merchant,
            "category": category,
            "customer": customer,
        }

        actions.append({
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": result["send_as"],
            "trigger_id": trg_id,
            "template_name": result.get("template_name", "vera_generic_v1"),
            "template_params": result.get("template_params", []),
            "body": result["body"],
            "cta": result["cta"],
            "suppression_key": sup_key,
            "rationale": result["rationale"],
        })

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv = conversations.get(body.conversation_id)
    if not conv:
        conv = {
            "turns": [],
            "merchant_id": body.merchant_id,
            "customer_id": body.customer_id,
            "trigger_id": "",
            "trigger": {},
            "merchant": contexts.get(("merchant", body.merchant_id), {}).get("payload", {}) if body.merchant_id else {},
            "category": {},
            "customer": None,
        }
        if conv["merchant"]:
            cat_slug = conv["merchant"].get("category_slug", "")
            cat_entry = contexts.get(("category", cat_slug))
            conv["category"] = cat_entry["payload"] if cat_entry else {}
        conversations[body.conversation_id] = conv

    result = handle_reply(body, conv)

    conv["turns"].append({"from": body.from_role, "body": body.message})
    if result.get("action") == "send" and result.get("body"):
        conv["turns"].append({"from": "vera", "body": result["body"]})

    return result
