"""
Cloud B2B AI voice outreach — runs inside the Railway web process (APScheduler).
Fetches real-estate agent phone numbers from RapidAPI and places Bland.ai calls
that pitch ZilloAgent. No Twilio, no local machine required.

Env vars required on Railway:
  RAPIDAPI_KEY   - us-real-estate-listings RapidAPI key
  BLANDAI_KEY    - Bland.ai org key
Optional:
  PRODUCT_LINK   - signup link mentioned on the call (defaults below)
"""
import logging
import os
import time

import httpx

logger = logging.getLogger("cloud_outreach")

RAPIDAPI_HOST = "us-real-estate-listings.p.rapidapi.com"
PRODUCT_LINK = os.getenv("PRODUCT_LINK", "https://yot70179-dev.github.io/zilloagent/")
MY_NAME = "Alex"

CALL_TASK = (
    f"You are {MY_NAME}, founder of ZilloAgent. You are calling a real estate agent to introduce a "
    "free AI tool. Keep it under 90 seconds, friendly and confident. Explain: ZilloAgent automatically "
    "texts and calls every seller and buyer lead from their Zillow listings 24/7, qualifies them, and "
    "only passes hot, ready-to-close leads back to the agent. It saves hours of cold outreach every day. "
    f"Tell them you are giving free access right now and you can text them a signup link at {PRODUCT_LINK}. "
    "If they are not interested, thank them politely and end the call. Do not be pushy. If asked, say yes "
    "you are an AI assistant calling on behalf of ZilloAgent."
)


def _fetch_agent_phones(city: str, limit: int) -> list[dict]:
    key = os.getenv("RAPIDAPI_KEY")
    if not key:
        logger.error("RAPIDAPI_KEY not set — cannot fetch agents")
        return []
    headers = {"x-rapidapi-key": key, "x-rapidapi-host": RAPIDAPI_HOST}
    agents, seen, offset = [], set(), 0
    while len(agents) < limit and offset < 200:
        url = f"https://{RAPIDAPI_HOST}/for-sale"
        params = {"location": city, "offset": offset, "limit": 50, "sort": "relevance"}
        try:
            r = httpx.get(url, headers=headers, params=params, timeout=25)
            data = r.json()
        except Exception as e:
            logger.error("RapidAPI error for %s: %s", city, e)
            break
        listings = data.get("listings") or []
        if not listings:
            break
        for listing in listings:
            advs = listing.get("advertisers") or []
            adv = next((a for a in advs if a.get("type") == "seller"), None) or (advs[0] if advs else None)
            if not adv or not adv.get("phones"):
                continue
            phones = adv["phones"]
            mobile = next((p for p in phones if p.get("type") == "Mobile"), None) or phones[0]
            num = mobile.get("number")
            if not num:
                continue
            digits = "".join(c for c in str(num) if c.isdigit())
            if len(digits) == 10:
                digits = "1" + digits
            if len(digits) != 11:
                continue
            phone = "+" + digits
            if phone in seen:
                continue
            seen.add(phone)
            loc = (listing.get("location") or {}).get("address") or {}
            agents.append({
                "phone": phone,
                "name": adv.get("name", ""),
                "address": loc.get("line", ""),
                "price": listing.get("list_price", ""),
            })
            if len(agents) >= limit:
                break
        offset += 50
        time.sleep(0.7)
    return agents[:limit]


def _place_bland_call(phone: str) -> dict:
    key = os.getenv("BLANDAI_KEY", "")
    body = {
        "phone_number": phone,
        "task": CALL_TASK,
        "voice": "nat",
        "max_duration": 3,
        "record": True,
        "answered_by_enabled": True,
    }
    r = httpx.post(
        "https://api.bland.ai/v1/calls",
        headers={"authorization": key, "Content-Type": "application/json"},
        json=body,
        timeout=20,
    )
    return r.json()


def run_call_campaign(city: str, count: int) -> dict:
    """Fetch `count` agents in `city` and place an AI call to each. Returns a summary."""
    if not os.getenv("BLANDAI_KEY") or not os.getenv("RAPIDAPI_KEY"):
        logger.error("Missing BLANDAI_KEY/RAPIDAPI_KEY — skipping call campaign for %s", city)
        return {"city": city, "placed": 0, "failed": 0, "error": "missing_keys"}

    agents = _fetch_agent_phones(city, count)
    logger.info("Cloud campaign %s: fetched %d agents", city, len(agents))
    placed = failed = 0
    for a in agents:
        try:
            res = _place_bland_call(a["phone"])
            if res.get("call_id"):
                placed += 1
                logger.info("Called %s %s id=%s", a["name"], a["phone"], res["call_id"])
            else:
                failed += 1
                logger.warning("Call failed %s: %s", a["phone"], res)
        except Exception as e:
            failed += 1
            logger.error("Call error %s: %s", a["phone"], e)
        time.sleep(1.2)
    logger.info("Cloud campaign %s DONE: placed=%d failed=%d", city, placed, failed)
    return {"city": city, "placed": placed, "failed": failed}
