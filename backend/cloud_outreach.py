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
import email as email_lib
import imaplib
import logging
import os
import random
import smtplib
import time
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import parseaddr

import httpx
from sqlalchemy import Boolean, Column, DateTime, Integer, String

from database import Base, SessionLocal, engine

logger = logging.getLogger("cloud_outreach")


# ── Persistence: who we've emailed (maps reply email -> phone for the call) ──
class OutreachContact(Base):
    __tablename__ = "outreach_contacts"
    id        = Column(Integer, primary_key=True)
    email     = Column(String(200), unique=True, index=True)
    phone     = Column(String(20))
    name      = Column(String(200))
    address   = Column(String(300))
    price     = Column(String(40))
    city      = Column(String(120))
    sent_at   = Column(DateTime, default=datetime.utcnow)
    replied   = Column(Boolean, default=False)
    called    = Column(Boolean, default=False)


# Ensure the table exists (startup already ran create_tables before import)
try:
    OutreachContact.__table__.create(bind=engine, checkfirst=True)
except Exception as e:
    logger.warning("Could not ensure outreach_contacts table: %s", e)

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


def _fetch_agent_phones(city: str, limit: int, mobile_only: bool = False) -> list[dict]:
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
            mobile = next((p for p in phones if p.get("type") == "Mobile"), None)
            if mobile_only and not mobile:
                continue          # skip office-only lines (usually answering machines)
            mobile = mobile or phones[0]
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
                "email": adv.get("email", ""),
                "address": loc.get("line", ""),
                "price": listing.get("list_price", ""),
                "city": loc.get("city", ""),
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


PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://zilloagent-production.up.railway.app")

def _leadgen_task(broker_name: str, broker_company: str) -> str:
    who = broker_name or "a top local real estate agent"
    org = f" of {broker_company}" if broker_company else ""
    return (
        f"You are a friendly assistant calling on behalf of {who}{org}, a local real estate agent. "
        f"Politely and briefly ask the person if they own property in the area and would be open to "
        f"connecting with {who} — who can help them sell or find interested buyers. Be warm, natural, "
        f"and under 60 seconds. If they show any interest or say yes, say 'Great — {who} will reach out "
        f"to you shortly,' and confirm the best phone number to reach them. If they are not interested, "
        f"thank them politely and end the call. Do not be pushy."
    )


def _ensure_broker(email: str, name: str, company: str) -> int:
    """Find or create a broker account by email (resilient to ephemeral DB). Returns its id."""
    try:
        from database import Agent
        db = SessionLocal()
        b = db.query(Agent).filter(Agent.email == email.lower()).first() if email else None
        if not b:
            b = Agent(name=name or (email.split("@")[0] if email else "Broker"),
                      company=company or None, email=email.lower() if email else None,
                      password_hash="", daily_sms_limit=350, daily_call_limit=20)
            db.add(b); db.commit(); db.refresh(b)
        bid = b.id
        db.close()
        return bid
    except Exception as e:
        logger.warning("ensure_broker failed: %s", e)
        return 0


def run_broker_leadgen(broker_id: int = 0, area: str = "", limit: int = 15,
                       broker_name: str = "", broker_email: str = "", broker_company: str = "") -> dict:
    """Call property owners in `area` and offer to connect them with this broker. Interested
    answers become leads (see /webhooks/bland/result). Robust to DB wipes: the broker is
    re-ensured by email, and broker_email rides in metadata so notification never depends on the DB."""
    if not os.getenv("BLANDAI_KEY") or not os.getenv("RAPIDAPI_KEY"):
        logger.error("Missing keys — skipping leadgen")
        return {"placed": 0, "error": "missing_keys"}

    # Resolve broker name/company/id (prefer explicit args; fall back to DB lookup by id)
    if broker_email and not broker_id:
        broker_id = _ensure_broker(broker_email, broker_name, broker_company)
    if not broker_name and broker_id:
        try:
            from database import Agent
            _db = SessionLocal()
            b = _db.query(Agent).filter(Agent.id == broker_id).first()
            if b:
                broker_name, broker_company = b.name or "", b.company or ""
                broker_email = broker_email or (b.email or "")
            _db.close()
        except Exception as e:
            logger.warning("Could not load broker %s: %s", broker_id, e)
    task = _leadgen_task(broker_name, broker_company)

    contacts = _fetch_agent_phones(area, limit, mobile_only=True)   # mobiles only — fewer machines
    logger.info("Leadgen broker=%s (%s) in %s: %d mobile contacts", broker_id, broker_name, area, len(contacts))
    placed = 0
    key = os.getenv("BLANDAI_KEY", "")
    for c in contacts:
        body = {
            "phone_number": c["phone"],
            "task": task,
            "voice": "nat",
            "max_duration": 2,
            "record": True,
            "answered_by_enabled": True,
            "webhook": f"{PUBLIC_BASE_URL}/webhooks/bland/result",
            "metadata": {
                "broker_id": broker_id,
                "broker_email": broker_email,
                "broker_name": broker_name,
                "owner_name": c.get("name", ""),
                "owner_phone": c["phone"],
                "address": c.get("address", ""),
                "price": str(c.get("price", "")),
                "city": c.get("city", "") or area,
            },
        }
        try:
            r = httpx.post("https://api.bland.ai/v1/calls",
                           headers={"authorization": key, "Content-Type": "application/json"},
                           json=body, timeout=20)
            if r.json().get("call_id"):
                placed += 1
        except Exception as e:
            logger.error("Leadgen call error %s: %s", c["phone"], e)
        time.sleep(1.0)
    logger.info("Leadgen broker %s DONE: placed=%d calls", broker_id, placed)
    return {"placed": placed, "area": area}


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


# ══════════════════════════════════════════════════════════════════════════
# Email outreach (Gmail SMTP) — pitch the product, then reply YES -> AI call
# ══════════════════════════════════════════════════════════════════════════

_SKIP_DOMAINS = {
    "gmail.com", "hotmail.com", "yahoo.com", "aol.com", "outlook.com", "icloud.com",
    "me.com", "msn.com", "live.com", "ymail.com", "cox.net", "sbcglobal.net",
    "verizon.net", "att.net", "comcast.net", "earthlink.net",
}


def _is_business_email(e: str) -> bool:
    import re
    if not e or not re.match(r"^[\w.\-+]+@[\w.\-]+\.[A-Za-z]{2,}$", e):
        return False
    return e.split("@")[1].lower() not in _SKIP_DOMAINS


def _email_body(first: str, city: str, link: str) -> str:
    templates = [
        (f"Hi {first},\n\nI'm the founder of ZilloAgent, an AI tool built for real estate agents like you.\n\n"
         f"It automatically texts and calls every buyer and seller lead from your Zillow listings, 24/7 - "
         f"qualifies them, and only passes the hot, ready-to-close leads back to you. No more hours wasted on "
         f"cold follow-up.\n\nI'm giving free access to a handful of agents in {city} right now. Interested?\n\n"
         f"Just reply YES and I'll call you to walk you through it. Or check it out here: {link}\n\n"
         f"Best,\nAlex - ZilloAgent\n\n---\nReply STOP to unsubscribe."),
        (f"Hello {first},\n\nHow much time do you spend chasing leads that never reply?\n\n"
         f"I built ZilloAgent to fix exactly that - an AI assistant that follows up with every lead from your "
         f"listings automatically by text and call, qualifies them, and hands you only the ones ready to move.\n\n"
         f"I'd love to give you free access while I'm onboarding agents in {city}. Reply YES and I'll give you a "
         f"quick call to explain. More info: {link}\n\nBest,\nAlex - ZilloAgent\n\n---\nReply STOP to unsubscribe."),
    ]
    return random.choice(templates)


def _send_email(to_email: str, to_name: str, subject: str, body: str) -> bool:
    user = os.getenv("GMAIL_USER", "")
    pw   = os.getenv("GMAIL_APP_PASSWORD", "")
    if not user or not pw:
        logger.warning("GMAIL creds missing — email not sent")
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = f"Alex - ZilloAgent <{user}>"
        msg["To"]      = to_email
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception as e:
        logger.error("Email send failed to %s: %s", to_email, e)
        return False


def run_email_campaign(cities: list[str] | None = None, target: int = 50) -> dict:
    """Fetch agents across cities and email a product pitch to new business contacts."""
    if not os.getenv("RAPIDAPI_KEY") or not os.getenv("GMAIL_USER"):
        logger.error("Missing RAPIDAPI_KEY/GMAIL creds — skipping email campaign")
        return {"sent": 0, "error": "missing_keys"}

    cities = cities or ["New York, NY", "Los Angeles, CA", "Austin, TX"]
    link = PRODUCT_LINK
    per_city = target // len(cities) + 10
    db = SessionLocal()
    sent = 0
    try:
        existing = {c.email for c in db.query(OutreachContact.email).all() if c.email}
        for city in cities:
            if sent >= target:
                break
            for a in _fetch_agent_phones(city, per_city):
                if sent >= target:
                    break
                em = (a.get("email") or "").lower()
                if not _is_business_email(em) or em in existing:
                    continue
                first = (a.get("name") or "there").split()[0]
                subject = f"Free AI tool for your {a.get('city') or city} listings, {first}?"
                if _send_email(em, a.get("name", ""), subject, _email_body(first, a.get("city") or city, link)):
                    existing.add(em)
                    db.add(OutreachContact(
                        email=em, phone=a.get("phone", ""), name=a.get("name", ""),
                        address=a.get("address", ""), price=str(a.get("price", "")),
                        city=a.get("city") or city,
                    ))
                    db.commit()
                    sent += 1
                    logger.info("Email %d/%d -> %s (%s)", sent, target, em, city)
                time.sleep(1.2)
    except Exception as e:
        logger.error("run_email_campaign error: %s", e)
    finally:
        db.close()
    logger.info("Email campaign done: sent=%d", sent)
    return {"sent": sent}


# ── Reply handling: YES -> AI call, NO -> nothing ──────────────────────────
_POSITIVE = ("interested", "yes", "sure", "absolutely", "sounds good", "tell me more",
             "call me", "let's talk", "lets talk", "would like", "schedule", "set up a",
             "more info", "how does", "send me", "open to")
_NEGATIVE = ("not interested", "no thanks", "no thank you", "stop", "unsubscribe",
             "remove me", "do not contact", "don't contact", "not looking", "already have")


def _sentiment(text: str) -> str:
    t = text.lower()
    if any(k in t for k in _NEGATIVE):
        return "NEGATIVE"
    if any(k in t for k in _POSITIVE):
        return "POSITIVE"
    return "NEUTRAL"


def check_replies_and_call() -> dict:
    """Read unseen Gmail replies; YES -> place AI call to that agent. Runs in cloud."""
    user = os.getenv("GMAIL_USER", "")
    pw   = os.getenv("GMAIL_APP_PASSWORD", "")
    if not user or not pw:
        return {"checked": 0, "error": "missing_gmail"}

    db = SessionLocal()
    positive = negative = processed = 0
    try:
        contacts = {c.email.lower(): c for c in db.query(OutreachContact).all() if c.email}
        if not contacts:
            return {"checked": 0, "note": "no contacts yet"}

        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(user, pw)
        M.select("INBOX")
        typ, data = M.search(None, "UNSEEN")
        ids = data[0].split() if data and data[0] else []
        for num in ids:
            typ, msg_data = M.fetch(num, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            msg = email_lib.message_from_bytes(msg_data[0][1])
            from_hdr = parseaddr(msg.get("From", ""))[1].lower()
            if from_hdr not in contacts:
                continue
            subject = msg.get("Subject", "")
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        try:
                            body = part.get_payload(decode=True).decode(errors="ignore")
                        except Exception:
                            pass
                        break
            else:
                try:
                    body = msg.get_payload(decode=True).decode(errors="ignore")
                except Exception:
                    body = ""
            verdict = _sentiment(f"{subject} {body}")
            processed += 1
            c = contacts[from_hdr]
            first = (c.name or "there").split()[0]
            if verdict == "POSITIVE":
                positive += 1
                if c.phone and not c.called:
                    try:
                        res = _place_bland_call(c.phone)
                        if res.get("call_id"):
                            c.called = True
                            logger.info("Reply YES -> calling %s %s id=%s", c.name, c.phone, res["call_id"])
                    except Exception as e:
                        logger.error("Reply call failed %s: %s", c.phone, e)
                _send_email(from_hdr, c.name or "", f"Re: {subject}",
                            f"Hi {first},\n\nGreat - I'll call you shortly to walk you through ZilloAgent and get "
                            f"you free access. Talk soon!\n\nBest,\nAlex - ZilloAgent")
                c.replied = True
            elif verdict == "NEGATIVE":
                negative += 1
                c.replied = True
                _send_email(from_hdr, c.name or "", f"Re: {subject}",
                            f"Hi {first},\n\nNo problem at all - I've removed you from the list. Wishing you the "
                            f"best!\n\nBest,\nAlex - ZilloAgent")
            db.commit()
            M.store(num, "+FLAGS", "\\Seen")
        M.logout()
    except Exception as e:
        logger.error("check_replies_and_call error: %s", e)
    finally:
        db.close()
    logger.info("Replies checked: processed=%d positive=%d negative=%d", processed, positive, negative)
    return {"checked": processed, "positive": positive, "negative": negative}
