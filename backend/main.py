"""
ZilloAgent FastAPI backend — multi-tenant, consent-based.
"""
import logging
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import httpx
import pytz
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import create_token, get_current_agent, hash_password, verify_password
from database import (
    Agent, Campaign, ConsentStatus, Conversation, DailyStats,
    Lead, LeadStatus, Message, MessageStatus, Property,
    SessionLocal, create_tables,
)
from message_sender import MessageSender

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="ZilloAgent API", version="2.0.0")

# CORS — allow Railway + GitHub Pages + local dev
CORS_ORIGINS = [
    "https://yot70179-dev.github.io",
    "http://localhost:3000",
    "http://localhost:8181",
    "http://127.0.0.1:3000",
]
extra = os.getenv("CORS_ORIGINS", "")
if extra:
    CORS_ORIGINS += [o.strip() for o in extra.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_origin_regex=r"https://.*\.railway\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    create_tables()
    logger.info("ZilloAgent backend started")
    _start_outreach_scheduler()


def _start_outreach_scheduler():
    """Schedule daily AI call campaigns at 10 AM local time in each city (cloud, always-on)."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        from cloud_outreach import (run_call_campaign, run_all_brokers,
                                     call_kimberly_checkin, check_replies_and_call)
    except Exception as e:
        logger.warning("Outreach scheduler not started (import failed): %s", e)
        return

    if not os.getenv("BLANDAI_KEY") or not os.getenv("RAPIDAPI_KEY"):
        logger.warning("Outreach scheduler idle — set BLANDAI_KEY and RAPIDAPI_KEY on Railway to enable.")
        return

    sched = BackgroundScheduler(timezone="UTC")

    # ── 25 calls/day to REALTORS pitching the tool, at 10 AM each city's local time ──
    for city, count, tz in [
        ("New York, NY",     9, "America/New_York"),
        ("Austin, TX",       8, "America/Chicago"),
        ("Los Angeles, CA",  8, "America/Los_Angeles"),
    ]:
        sched.add_job(
            run_call_campaign, CronTrigger(hour=10, minute=0, timezone=tz),
            args=[city, count], id=f"toolpitch::{city}", replace_existing=True,
            misfire_grace_time=3600, coalesce=True,
        )

    # ── 25 owner lead-gen calls/day for EVERY signed-up broker (incl. Kimberly), 10 AM PT ──
    sched.add_job(
        run_all_brokers, CronTrigger(hour=10, minute=0, timezone="America/Los_Angeles"),
        kwargs={"limit_per_broker": 25}, id="leadgen::all-brokers",
        replace_existing=True, misfire_grace_time=3600, coalesce=True,
    )

    # ── Daily check-in call to Kimberly: "any clients thanks to the tool?" (11 AM PT) ──
    sched.add_job(
        call_kimberly_checkin, CronTrigger(hour=11, minute=0, timezone="America/Los_Angeles"),
        id="checkin::kimberly", replace_existing=True, misfire_grace_time=3600, coalesce=True,
    )

    # Reply poll every 10 min (handles any email replies → AI call)
    if os.getenv("GMAIL_USER") and os.getenv("GMAIL_APP_PASSWORD"):
        sched.add_job(
            check_replies_and_call, CronTrigger(minute="*/10"),
            id="replies::poll", replace_existing=True, misfire_grace_time=300, coalesce=True,
        )

    sched.start()
    app.state.scheduler = sched
    logger.info("Scheduler: 15 realtor tool-pitch calls (10 AM local) + 10 owner lead-gen for Kimberly (noon LA), daily.")


@app.get("/outreach/status")
def outreach_status():
    """Diagnostic: what's configured in this cloud environment (no secrets revealed)."""
    db_url = os.getenv("DATABASE_URL", "sqlite:///./zilloagent.db")
    running = bool(getattr(app.state, "scheduler", None) and app.state.scheduler.running)
    contacts = None
    try:
        from cloud_outreach import OutreachContact
        db = SessionLocal()
        contacts = db.query(OutreachContact).count()
        db.close()
    except Exception:
        pass
    return {
        "scheduler_running": running,
        "has_bland_key":   bool(os.getenv("BLANDAI_KEY")),
        "has_rapidapi_key": bool(os.getenv("RAPIDAPI_KEY")),
        "has_gmail":       bool(os.getenv("GMAIL_USER") and os.getenv("GMAIL_APP_PASSWORD")),
        "db_persistent":   not db_url.startswith("sqlite"),
        "contacts_stored": contacts,
    }


@app.post("/outreach/calls/run")
def trigger_call_campaign(
    background_tasks: BackgroundTasks,
    city: str = Query("New York, NY"),
    count: int = Query(5),
    token: str = Query(""),
):
    """Manually fire an AI call campaign now (for testing the cloud pipeline).
    Guard with the ADMIN_TOKEN env var set on Railway."""
    admin = os.getenv("ADMIN_TOKEN", "")
    if not admin or token != admin:
        raise HTTPException(403, "Invalid or missing admin token.")
    from cloud_outreach import run_call_campaign
    background_tasks.add_task(run_call_campaign, city, count)
    return {"status": "started", "city": city, "count": count}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ======================================================================
# Pydantic schemas
# ======================================================================

class PhoneAuthRequest(BaseModel):
    phone: str
    name: Optional[str] = None      # only needed for new accounts
    company: Optional[str] = None
    email: Optional[str] = None
    code: Optional[str] = None       # email verification code (new accounts)

class SendCodeRequest(BaseModel):
    phone: str
    email: str

class GoogleAuthRequest(BaseModel):
    credential: str      # Google ID token (JWT) from Google Identity Services

class CredentialsUpdate(BaseModel):
    name: Optional[str] = None
    company: Optional[str] = None
    twilio_sid: Optional[str] = None
    twilio_token: Optional[str] = None
    twilio_phone: Optional[str] = None
    gmail_user: Optional[str] = None
    gmail_password: Optional[str] = None
    bland_key: Optional[str] = None
    rapidapi_key: Optional[str] = None

class CampaignCreate(BaseModel):
    name: str
    target_area: str
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    property_type: Optional[str] = None
    daily_limit: int = 50

class SendMessageRequest(BaseModel):
    message: str
    message_type: str = "sms"

class OutreachRequest(BaseModel):
    target_area: str
    limit: int = 10


# ======================================================================
# Health
# ======================================================================

@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0"}


# ======================================================================
# Auth
# ======================================================================

def _normalize_phone(raw: str) -> str:
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits


# ── Email verification codes (in-memory, short-lived) ────────────────────────
import random
import smtplib
import time as _time
from email.mime.text import MIMEText

_verification_codes: Dict[str, dict] = {}   # email -> {code, phone, expires}
_CODE_TTL = 600   # 10 minutes


def _send_verification_email(to_email: str, code: str) -> bool:
    user = os.getenv("GMAIL_USER", "")
    pw   = os.getenv("GMAIL_APP_PASSWORD", "")
    if not user or not pw:
        logger.warning("GMAIL creds missing — cannot send verification code")
        return False
    try:
        msg = MIMEText(
            f"Your ZilloAgent verification code is: {code}\n\n"
            f"It expires in 10 minutes. If you didn't request this, ignore this email."
        )
        msg["Subject"] = f"{code} is your ZilloAgent code"
        msg["From"]    = f"ZilloAgent <{user}>"
        msg["To"]      = to_email
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.starttls()
            smtp.login(user, pw)
            smtp.send_message(msg)
        return True
    except Exception as e:
        logger.error("Failed to send verification email: %s", e)
        return False


@app.post("/auth/send-code")
def send_code(req: SendCodeRequest, db: Session = Depends(get_db)):
    """Send a 6-digit verification code to the email. Only needed for new accounts."""
    phone = _normalize_phone(req.phone)
    email = req.email.strip().lower()

    # Existing account → no verification needed, frontend logs in directly.
    if db.query(Agent).filter(Agent.phone == phone).first():
        return {"sent": False, "existing": True}

    code = f"{random.randint(0, 999999):06d}"
    _verification_codes[email] = {"code": code, "phone": phone, "expires": _time.time() + _CODE_TTL}
    ok = _send_verification_email(email, code)
    if not ok:
        raise HTTPException(500, "Could not send verification email. Check email address.")
    return {"sent": True, "existing": False}


# Phrases that mean we reached a machine / answering service — never a lead
_MACHINE_PHRASES = (
    "record your name", "leave a message", "leave your message", "after the tone",
    "after the beep", "is not available", "is unavailable", "not available right now",
    "the person you are trying to reach", "your call has been forwarded", "voicemail",
    "press 1", "press one", "press 2", "mailbox", "please leave", "google voice",
    "to leave a callback", "currently unavailable", "can't take your call", "cannot take your call",
)
# A real affirmative must come from the USER's own words
_USER_AFFIRM = (
    "yes", "i'm interested", "im interested", "i am interested", "i'd be interested",
    "id be interested", "sure", "sounds good", "tell me more", "i would", "definitely",
    "absolutely", "please do", "go ahead", "that works", "sounds great", "okay yes",
    "yeah i", "yes i", "i'm open", "im open", "happy to",
)
_USER_NEG = (
    "not interested", "no thanks", "no thank you", "remove", "stop calling",
    "don't call", "do not call", "take me off", "no i'm", "not right now",
)


def _is_genuine_interest(transcript: str) -> bool:
    """True only if a real human said something affirmative — not a voicemail/IVR."""
    if not transcript:
        return False
    low = transcript.lower()
    if any(p in low for p in _MACHINE_PHRASES):
        return False
    # Look only at what the USER said (lines that start with "user:")
    user_text = " ".join(
        ln.split("user:", 1)[1].strip().lower()
        for ln in transcript.splitlines() if ln.strip().lower().startswith("user:")
    )
    if not user_text or len(user_text) < 4:
        return False
    if any(n in user_text for n in _USER_NEG):
        return False
    return any(a in user_text for a in _USER_AFFIRM)


@app.post("/webhooks/bland/result")
async def bland_result(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Bland calls this when a call ends.
    - toolpitch call: a realtor said yes → auto-onboard them + start owner lead-gen in their area.
    - leadgen call: an owner said yes → create a lead for the broker and notify them."""
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False}
    meta = payload.get("metadata") or {}
    transcript = payload.get("concatenated_transcript") or payload.get("summary") or ""
    answered_by = payload.get("answered_by", "")
    if answered_by == "voicemail":
        return {"ok": True, "skip": "voicemail"}

    interested = _is_genuine_interest(transcript)

    # ── Realtor said YES to the tool → onboard them and start their lead-gen ──
    if meta.get("type") == "toolpitch":
        if not interested:
            return {"ok": True, "interested": False}
        rname  = meta.get("realtor_name", "")
        remail = meta.get("realtor_email", "")
        rphone = meta.get("realtor_phone", "")
        rcity  = meta.get("city", "") or "Los Angeles, CA"
        from cloud_outreach import run_broker_leadgen
        # Immediately call 10 property owners in their area, offering this realtor
        background_tasks.add_task(run_broker_leadgen, 0, rcity, 10, rname, remail, "", rphone)
        if remail:
            try:
                _notify_realtor_onboarded(remail, rname, rcity)
            except Exception as e:
                logger.error("Realtor onboard email failed: %s", e)
        logger.info("Onboarded realtor %s (%s) — starting 10 owner calls in %s", rname, remail, rcity)
        return {"ok": True, "onboarded": rname, "area": rcity}

    # ── Owner said YES → lead for the broker ──
    broker_id = meta.get("broker_id")
    if not broker_id:
        return {"ok": True, "skip": "no broker_id"}
    if not interested:
        return {"ok": True, "interested": False}

    owner_phone = meta.get("owner_phone", "")
    # Avoid duplicate leads for the same broker+owner
    existing = db.query(Lead).filter(Lead.agent_id == broker_id,
                                     Lead.contact_phone == owner_phone).first()
    if not existing:
        lead = Lead(
            agent_id=broker_id,
            contact_name=meta.get("owner_name", "") or "Property owner",
            contact_phone=owner_phone,
            status=LeadStatus.NEW,
            consent_status=ConsentStatus.CONSENTED,
            consent_timestamp=datetime.utcnow(),
            call_transcript=transcript[:4000],
            notes=f"Interested owner. {meta.get('address','')} {meta.get('price','')}".strip(),
        )
        db.add(lead)
        db.commit()
        db.refresh(lead)
    else:
        lead = existing

    broker = db.query(Agent).filter(Agent.id == broker_id).first() if broker_id else None

    # PRIMARY hand-off: call the broker and give them the lead details by voice
    broker_phone = meta.get("broker_phone") or (broker.phone if broker else "")
    if broker_phone:
        from cloud_outreach import call_broker_with_lead
        background_tasks.add_task(
            call_broker_with_lead, broker_phone, meta.get("broker_name", ""),
            meta.get("owner_name", ""), owner_phone, meta.get("address", ""),
        )

    # Backup hand-off: email the broker too
    to_email = meta.get("broker_email") or (broker.gmail_user or broker.email if broker else "")
    if to_email:
        try:
            _notify_broker_lead(to_email, meta, owner_phone)
        except Exception as e:
            logger.error("Broker notify email failed: %s", e)
    return {"ok": True, "interested": True, "lead_id": lead.id, "broker_called": bool(broker_phone)}


def _notify_broker_lead(to_email: str, meta: dict, owner_phone: str):
    """Send the broker an email about a newly interested lead (uses the platform Gmail)."""
    user = os.getenv("GMAIL_USER", ""); pw = os.getenv("GMAIL_APP_PASSWORD", "")
    if not user or not pw:
        return
    body = (
        f"Good news! A property owner is interested in connecting with you.\n\n"
        f"Name:  {meta.get('owner_name','—')}\n"
        f"Phone: {owner_phone}\n"
        f"Property: {meta.get('address','—')} {meta.get('price','')}\n\n"
        f"They just said yes on the call — reach out to them soon. "
        f"Full details are in your ZilloAgent dashboard."
    )
    msg = MIMEText(body)
    msg["Subject"] = "🔥 New interested lead from ZilloAgent"
    msg["From"] = f"ZilloAgent <{user}>"
    msg["To"] = to_email
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls(); s.login(user, pw); s.send_message(msg)


def _notify_realtor_onboarded(to_email: str, name: str, city: str):
    """Welcome a realtor who said yes — their lead-gen just started."""
    user = os.getenv("GMAIL_USER", ""); pw = os.getenv("GMAIL_APP_PASSWORD", "")
    if not user or not pw:
        return
    first = (name or "there").split()[0]
    body = (
        f"Hi {first},\n\nGreat to connect! You're now set up with ZilloAgent.\n\n"
        f"We've already started calling property owners in {city} on your behalf — "
        f"every owner who's interested will be sent straight to you with their number.\n\n"
        f"Sign in to see your leads as they come in: https://yot70179-dev.github.io/zilloagent/\n\n"
        f"Best,\nAlex - ZilloAgent"
    )
    msg = MIMEText(body)
    msg["Subject"] = "Welcome to ZilloAgent — your leads are on the way"
    msg["From"] = f"Alex - ZilloAgent <{user}>"
    msg["To"] = to_email
    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls(); s.login(user, pw); s.send_message(msg)


@app.post("/auth/google")
def google_auth(req: GoogleAuthRequest, db: Session = Depends(get_db)):
    """Sign in / sign up with a Google account. Verifies the Google ID token,
    then logs in (or creates) the agent keyed by their Google email."""
    try:
        r = httpx.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": req.credential}, timeout=10,
        )
        info = r.json()
    except Exception:
        raise HTTPException(400, "Could not verify Google sign-in.")
    if r.status_code != 200 or not info.get("email"):
        raise HTTPException(401, "Invalid Google token.")
    if info.get("email_verified") not in ("true", True):
        raise HTTPException(401, "Google email not verified.")

    # If a client id is configured, enforce the token audience matches it.
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    if client_id and info.get("aud") != client_id:
        raise HTTPException(401, "Google token audience mismatch.")

    email = info["email"].lower()
    agent = db.query(Agent).filter(Agent.email == email).first()
    if not agent:
        agent = Agent(
            name=info.get("name") or email.split("@")[0],
            email=email,
            password_hash="",          # no password — Google-authenticated
            daily_sms_limit=350,
            daily_call_limit=20,
        )
        db.add(agent)
        db.commit()
        db.refresh(agent)
        new = True
    else:
        new = False
    return {"token": create_token(agent.id), "agent": _agent_dict(agent), "new": new}


@app.post("/auth/enter")
def phone_enter(req: PhoneAuthRequest, db: Session = Depends(get_db)):
    """
    Single endpoint: enter with phone only.
    - Existing account → return token immediately.
    - New phone → create account (name required) → return token.
    """
    phone = _normalize_phone(req.phone)
    agent = db.query(Agent).filter(Agent.phone == phone).first()

    if agent:
        # Existing user — just log in. Backfill email if newly provided.
        if req.email and not agent.email:
            agent.email = req.email.lower()
            db.commit()
            db.refresh(agent)
        return {"token": create_token(agent.id), "agent": _agent_dict(agent), "new": False}

    # New user. If a verification code was sent for this email, enforce it
    # (new email+phone flow). Otherwise fall back to direct create (legacy flow)
    # so existing clients keep working — backward compatible.
    email = req.email.strip().lower() if req.email else None
    rec = _verification_codes.get(email) if email else None
    if rec:
        if rec["expires"] < _time.time():
            raise HTTPException(400, "Verification code expired. Request a new one.")
        if not req.code or req.code.strip() != rec["code"]:
            raise HTTPException(400, "Incorrect verification code.")
        _verification_codes.pop(email, None)   # one-time use

    if not req.name or not req.name.strip():
        req.name = email.split("@")[0] if email else "Agent"

    agent = Agent(
        name=req.name.strip(),
        company=req.company,
        phone=phone,
        email=req.email.lower() if req.email else None,
        password_hash="",          # no password
        twilio_phone=phone,
        daily_sms_limit=350,
        daily_call_limit=20,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return {"token": create_token(agent.id), "agent": _agent_dict(agent), "new": True}


# Keep old endpoints alive so existing tokens still work
@app.post("/auth/signup")
def signup_compat(req: PhoneAuthRequest, db: Session = Depends(get_db)):
    return phone_enter(req, db)

@app.post("/auth/login")
def login_compat(req: PhoneAuthRequest, db: Session = Depends(get_db)):
    return phone_enter(req, db)


@app.get("/auth/me")
def me(agent: Agent = Depends(get_current_agent)):
    return _agent_dict(agent)


@app.put("/auth/credentials")
def update_credentials(
    req: CredentialsUpdate,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    for field, val in req.model_dump(exclude_none=True).items():
        setattr(agent, field, val)
    db.commit()
    return {"ok": True, "agent": _agent_dict(agent)}


@app.post("/auth/test-credentials")
def test_credentials(agent: Agent = Depends(get_current_agent)):
    """Test that agent's Twilio + Gmail credentials work."""
    results = {}
    sender = MessageSender(agent)

    # Test Twilio
    if agent.twilio_sid and agent.twilio_token:
        try:
            from twilio.rest import Client
            c = Client(agent.twilio_sid, agent.twilio_token)
            c.api.accounts(agent.twilio_sid).fetch()
            results["twilio"] = "✅ Connected"
        except Exception as e:
            results["twilio"] = f"❌ {str(e)[:80]}"
    else:
        results["twilio"] = "⚠️ Not configured"

    # Test Gmail
    if agent.gmail_user and agent.gmail_password:
        try:
            import smtplib
            with smtplib.SMTP("smtp.gmail.com", 587) as s:
                s.starttls()
                s.login(agent.gmail_user, agent.gmail_password)
            results["gmail"] = "✅ Connected"
        except Exception as e:
            results["gmail"] = f"❌ {str(e)[:80]}"
    else:
        results["gmail"] = "⚠️ Not configured"

    return results


def _agent_dict(agent: Agent) -> dict:
    return {
        "id": agent.id,
        "name": agent.name,
        "company": agent.company,
        "email": agent.email,
        "phone": agent.phone,
        "has_twilio": bool(agent.twilio_sid and agent.twilio_token and agent.twilio_phone),
        "has_gmail":  bool(agent.gmail_user and agent.gmail_password),
        "has_bland":  bool(agent.bland_key),
        "twilio_phone": agent.twilio_phone,
        "twilio_sid":   agent.twilio_sid,
        "gmail_user":   agent.gmail_user,
        "daily_sms_limit":  getattr(agent, "daily_sms_limit",  350),
        "daily_call_limit": getattr(agent, "daily_call_limit", 20),
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
    }


def _daily_usage(agent_id: int, db: Session) -> dict:
    """Return today's SMS and call counts for an agent."""
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    msgs = (
        db.query(Message)
        .join(Lead, Message.lead_id == Lead.id)
        .filter(Lead.agent_id == agent_id,
                Message.direction == "outbound",
                Message.message_type == "sms",
                Message.sent_at >= today)
        .count()
    )
    calls = (
        db.query(Message)
        .join(Lead, Message.lead_id == Lead.id)
        .filter(Lead.agent_id == agent_id,
                Message.direction == "outbound",
                Message.message_type == "call",
                Message.sent_at >= today)
        .count()
    )
    return {"sms": msgs, "calls": calls}


# ======================================================================
# Dashboard stats
# ======================================================================

@app.get("/stats/today")
def stats_today(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    today = datetime.utcnow().date()
    leads = db.query(Lead).filter(Lead.agent_id == agent.id).all()

    total_leads    = len(leads)
    consented      = sum(1 for l in leads if l.consent_status == ConsentStatus.CONSENTED)
    opted_out      = sum(1 for l in leads if l.consent_status == ConsentStatus.OPTED_OUT)
    pending        = sum(1 for l in leads if l.consent_status == ConsentStatus.PENDING)
    hot_leads      = sum(1 for l in leads if (l.score or 0) >= 7)
    responded      = sum(1 for l in leads if l.status in [LeadStatus.RESPONDED, LeadStatus.QUALIFIED])

    msgs_today = db.query(Message).join(Lead).filter(
        Lead.agent_id == agent.id,
        Message.created_at >= datetime.utcnow().replace(hour=0, minute=0, second=0),
    ).count()

    response_rate = round((responded / total_leads * 100), 1) if total_leads else 0

    usage = _daily_usage(agent.id, db)
    return {
        "total_leads": total_leads,
        "messages_sent_today": msgs_today,
        "consented": consented,
        "pending": pending,
        "opted_out": opted_out,
        "hot_leads": hot_leads,
        "response_rate": response_rate,
        "sms_today": usage["sms"],
        "calls_today": usage["calls"],
        "sms_limit": getattr(agent, "daily_sms_limit", 350),
        "call_limit": getattr(agent, "daily_call_limit", 20),
    }


# ======================================================================
# Leads / Inbox
# ======================================================================

@app.get("/leads")
def list_leads(
    consent_status: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(100, le=500),
    offset: int = 0,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    q = db.query(Lead).filter(Lead.agent_id == agent.id)
    if consent_status:
        q = q.filter(Lead.consent_status == consent_status)
    if status:
        q = q.filter(Lead.status == status)
    leads = q.order_by(Lead.updated_at.desc()).offset(offset).limit(limit).all()
    return [_lead_dict(l) for l in leads]


@app.get("/leads/{lead_id}")
def get_lead(
    lead_id: int,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    lead = _get_lead(lead_id, agent.id, db)
    msgs = db.query(Message).filter(Message.lead_id == lead_id).order_by(Message.created_at).all()
    convs= db.query(Conversation).filter(Conversation.lead_id == lead_id).order_by(Conversation.created_at).all()
    return {
        **_lead_dict(lead),
        "messages": [_msg_dict(m) for m in msgs],
        "conversation": [{"role": c.role, "content": c.content, "time": c.created_at.isoformat()} for c in convs],
    }


@app.post("/leads/{lead_id}/takeover")
def takeover(
    lead_id: int,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    lead = _get_lead(lead_id, agent.id, db)
    lead.human_takeover = True
    db.commit()
    return {"ok": True}


@app.delete("/leads/{lead_id}/takeover")
def resume_ai(
    lead_id: int,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    lead = _get_lead(lead_id, agent.id, db)
    lead.human_takeover = False
    db.commit()
    return {"ok": True}


@app.post("/leads/{lead_id}/message")
def send_message_to_lead(
    lead_id: int,
    req: SendMessageRequest,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    lead   = _get_lead(lead_id, agent.id, db)
    sender = MessageSender(agent)

    if req.message_type == "sms" and lead.contact_phone:
        sid = sender.send_sms(lead.contact_phone, req.message)
        ok  = bool(sid)
    elif req.message_type == "email" and lead.contact_email:
        ok = sender.send_email(lead.contact_email, lead.contact_name or "", "Follow-up", req.message)
    else:
        raise HTTPException(400, "No contact info or invalid type")

    if ok:
        _log_message(db, lead.id, req.message_type, "outbound", req.message)
    return {"ok": ok}


# ======================================================================
# Outreach — manual trigger
# ======================================================================

@app.post("/outreach/run")
def run_outreach(
    req: OutreachRequest,
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    # Lead-gen: AI calls property contacts in the area and asks if they want to
    # connect with this broker. Interested answers become leads (see /webhooks/bland/result).
    from cloud_outreach import run_broker_leadgen
    background_tasks.add_task(run_broker_leadgen, agent.id, req.target_area, req.limit)
    return {"ok": True, "message": f"Calling property owners in {req.target_area} — interested ones will appear as leads."}


def _run_outreach_task(agent_id: int, target_area: str, limit: int):
    """Fetch listings and send consent SMS from agent's Twilio."""
    db = SessionLocal()
    try:
        agent = db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent:
            return
        sender = MessageSender(agent)
        rapidapi_key = agent.rapidapi_key or os.getenv("RAPIDAPI_KEY", "")
        rapidapi_host = os.getenv("RAPIDAPI_HOST", "us-real-estate-listings.p.rapidapi.com")

        slug = target_area.replace(", ", "%2C+").replace(" ", "+")
        url  = f"https://{rapidapi_host}/for-sale?location={slug}&limit=50&sort=relevance&days_on=14"
        headers = {"x-rapidapi-key": rapidapi_key, "x-rapidapi-host": rapidapi_host}

        resp = httpx.get(url, headers=headers, timeout=20)
        listings = resp.json().get("listings", [])
        # Check daily SMS limit before sending
        usage = _daily_usage(agent_id, db)
        sms_remaining = max(0, getattr(agent, "daily_sms_limit", 350) - usage["sms"])
        if sms_remaining == 0:
            logger.info("Agent %d hit daily SMS limit (%d)", agent_id, getattr(agent, "daily_sms_limit", 350))
            return

        sent = 0

        for listing in listings:
            if sent >= limit or sent >= sms_remaining:
                break
            adv = next((a for a in listing.get("advertisers", []) if a.get("type") == "seller"), None)
            if not adv:
                adv = next(iter(listing.get("advertisers", [])), None)
            if not adv:
                continue

            phone = next((p.get("number") for p in adv.get("phones", [])
                          if p.get("type") in ["Mobile", "Direct", "Office"]), None)
            if not phone:
                phone = next((p.get("number") for p in adv.get("phones", [])), None)
            if not phone:
                continue

            clean = "".join(c for c in phone if c.isdigit())
            if len(clean) == 10:
                clean = "1" + clean
            if len(clean) != 11:
                continue
            e164 = "+" + clean

            # Skip if already in DB for this agent
            existing = db.query(Lead).filter(Lead.agent_id == agent_id, Lead.contact_phone == e164).first()
            if existing:
                continue

            address = listing.get("location", {}).get("address", {}).get("line", "")
            city    = listing.get("location", {}).get("address", {}).get("city", "")
            price   = listing.get("list_price", 0)
            name    = adv.get("name", "")
            email   = adv.get("email", "")
            first   = name.split()[0] if name else "there"

            # Create property
            prop = Property(
                zillow_id=listing.get("property_id", f"{address}_{price}"),
                address=address, city=city,
                price=price,
                listing_agent_name=name,
                listing_agent_email=email,
                listing_agent_phone=e164,
            )
            db.merge(prop)
            db.flush()

            # Create lead
            lead = Lead(
                agent_id=agent_id,
                contact_name=name,
                contact_email=email,
                contact_phone=e164,
                consent_status=ConsentStatus.PENDING,
                status=LeadStatus.NEW,
            )
            db.add(lead)
            db.flush()

            # Send consent SMS
            msg = (
                f"Hi {first}, I have pre-qualified buyers looking in {city}. "
                f"Interested in connecting them with your listing at {address}? "
                f"Reply YES for a call, STOP to opt out."
            )
            sid = sender.send_sms(e164, msg)
            if sid:
                _log_message(db, lead.id, "sms", "outbound", msg, sid)
                sent += 1
                logger.info("Consent SMS sent to %s (%s)", name, e164)

        db.commit()
        logger.info("Outreach done: %d SMS sent for agent %d", sent, agent_id)
    except Exception as e:
        logger.error("Outreach task error: %s", e)
        db.rollback()
    finally:
        db.close()


# ======================================================================
# Twilio webhook — inbound SMS (YES / STOP)
# ======================================================================

@app.post("/webhooks/twilio/sms")
async def twilio_sms_webhook(request: Request, db: Session = Depends(get_db)):
    form  = await request.form()
    from_  = form.get("From", "")
    body   = form.get("Body", "").strip().upper()
    to     = form.get("To", "")   # agent's Twilio number

    # Find agent by their Twilio phone number
    agent = db.query(Agent).filter(Agent.twilio_phone == to).first()
    agent_id = agent.id if agent else None

    # Find lead
    lead = db.query(Lead).filter(Lead.contact_phone == from_)
    if agent_id:
        lead = lead.filter(Lead.agent_id == agent_id)
    lead = lead.first()

    if not lead:
        logger.warning("SMS from unknown number: %s", from_)
        return JSONResponse({"status": "unknown"})

    _log_message(db, lead.id, "sms", "inbound", form.get("Body", ""), form.get("MessageSid"))

    # YES → consent granted
    if body.startswith("YES"):
        lead.consent_status   = ConsentStatus.CONSENTED
        lead.consent_timestamp = datetime.utcnow()
        lead.status            = LeadStatus.CONTACTED
        db.commit()
        logger.info("CONSENT GRANTED by %s (lead %d)", from_, lead.id)

        # Queue a call if agent has Bland.ai key and it's 9AM-9PM
        if agent and (agent.bland_key or os.getenv("BLANDAI_KEY")):
            _queue_call_for_lead(lead, agent, db)

        return JSONResponse({"status": "consented"})

    # STOP / UNSUBSCRIBE → DNC
    if any(body.startswith(w) for w in ["STOP", "UNSUBSCRIBE", "CANCEL", "OPT OUT"]):
        lead.consent_status = ConsentStatus.OPTED_OUT
        lead.opted_out      = True
        lead.status         = LeadStatus.OPTED_OUT
        db.commit()
        # Send goodbye (no STOP footer since they opted out)
        if agent:
            sender = MessageSender(agent)
            sender.send_sms(from_, "You have been removed from our list. We won't contact you again.")
        return JSONResponse({"status": "opted_out"})

    db.commit()
    return JSONResponse({"status": "received"})


def _queue_call_for_lead(lead: Lead, agent: Agent, db: Session):
    """Make an AI call immediately if within 9 AM–9 PM lead's timezone."""
    import threading
    t = threading.Thread(target=_make_consent_call, args=(lead.id, agent.id))
    t.daemon = True
    t.start()


def _make_consent_call(lead_id: int, agent_id: int):
    db = SessionLocal()
    try:
        lead  = db.query(Lead).filter(Lead.id == lead_id).first()
        agent = db.query(Agent).filter(Agent.id == agent_id).first()
        if not lead or not agent:
            return

        # Daily call limit check
        usage = _daily_usage(agent_id, db)
        call_limit = getattr(agent, "daily_call_limit", 20)
        if usage["calls"] >= call_limit:
            logger.info("Agent %d hit daily call limit (%d)", agent_id, call_limit)
            return

        # Time-of-day check (9 AM – 9 PM)
        tz_name = lead.local_timezone or _guess_timezone(lead.contact_phone)
        try:
            tz   = pytz.timezone(tz_name)
            hour = datetime.now(tz).hour
        except Exception:
            hour = datetime.utcnow().hour

        if not (9 <= hour < 21):
            logger.info("Outside calling hours (%d) for lead %d — skipping call", hour, lead_id)
            return

        first  = (lead.contact_name or "").split()[0] or "there"
        prop   = db.query(Property).filter(Property.id == lead.property_id).first()
        addr   = prop.address if prop else "your property"
        price  = f"${prop.price:,.0f}" if prop and prop.price else ""

        task = (
            f"You are {agent.name}, a real estate agent calling {first} about their listing at {addr} {price}. "
            f"They just replied YES to receive a call — so they are expecting it. "
            f"Be warm and professional. Tell them you have pre-qualified buyers actively looking in their area. "
            f"Ask if they'd like to schedule a showing or get more details. "
            f"If asked whether you are an AI: say yes, you are an AI assistant calling on behalf of {agent.name}. "
            f"Keep it under 2 minutes. If they say not interested, thank them and hang up."
        )

        sender  = MessageSender(agent)
        call_id = sender.make_call(lead.contact_phone, task)

        if call_id:
            lead.call_id = call_id
            lead.status  = LeadStatus.CONTACTED
            _log_message(db, lead.id, "call", "outbound", f"AI call initiated (id: {call_id})")
            db.commit()
    except Exception as e:
        logger.error("Call error for lead %d: %s", lead_id, e)
    finally:
        db.close()


def _guess_timezone(phone: str) -> str:
    """Rough timezone from US area code."""
    if not phone or len(phone) < 5:
        return "America/New_York"
    ac = phone.lstrip("+1")[:3]
    eastern  = {"212","718","646","347","929","917","516","631","914","845","203","860","401","617","508","781","339","413","978","603","207","802","518","315","716","585","607","914"}
    central  = {"312","773","872","630","847","708","224","469","972","214","817","682","512","737","210","726","832","713","281","346","918","405","580","316","785","913","816","314","417","651","763","952","612","320","507","218"}
    mountain = {"303","720","970","719","505","575","307","406","208","801","435","702","775"}
    pacific  = {"213","310","323","424","818","626","562","714","949","619","858","760","805","831","707","415","628","510","925","408","669","916","209","559"}
    if ac in eastern:  return "America/New_York"
    if ac in central:  return "America/Chicago"
    if ac in mountain: return "America/Denver"
    if ac in pacific:  return "America/Los_Angeles"
    return "America/New_York"


# ======================================================================
# Campaigns
# ======================================================================

@app.get("/campaigns")
def list_campaigns(
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    camps = db.query(Campaign).filter(Campaign.agent_id == agent.id).all()
    return [{"id": c.id, "name": c.name, "target_area": c.target_area,
             "is_active": c.is_active, "daily_limit": c.daily_limit} for c in camps]


@app.post("/campaigns")
def create_campaign(
    req: CampaignCreate,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    camp = Campaign(agent_id=agent.id, **req.model_dump())
    db.add(camp)
    db.commit()
    db.refresh(camp)
    return {"id": camp.id, "name": camp.name}


# ======================================================================
# Properties
# ======================================================================

@app.get("/properties")
def list_properties(
    city: Optional[str] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    q = db.query(Property)
    if city:
        q = q.filter(Property.city.ilike(f"%{city}%"))
    props = q.order_by(Property.fetched_at.desc()).offset(offset).limit(limit).all()
    return [{"id": p.id, "address": p.address, "city": p.city, "price": p.price,
             "bedrooms": p.bedrooms, "listing_agent_name": p.listing_agent_name} for p in props]


# ======================================================================
# Helpers
# ======================================================================

def _get_lead(lead_id: int, agent_id: int, db: Session) -> Lead:
    lead = db.query(Lead).filter(Lead.id == lead_id, Lead.agent_id == agent_id).first()
    if not lead:
        raise HTTPException(404, "Lead not found")
    return lead


def _lead_dict(l: Lead) -> dict:
    return {
        "id": l.id,
        "contact_name": l.contact_name,
        "contact_email": l.contact_email,
        "contact_phone": l.contact_phone,
        "status": l.status,
        "consent_status": l.consent_status,
        "consent_timestamp": l.consent_timestamp.isoformat() if l.consent_timestamp else None,
        "score": l.score,
        "followup_sent": l.followup_sent,
        "call_recording": l.call_recording,
        "human_takeover": l.human_takeover,
        "opted_out": l.opted_out,
        "created_at": l.created_at.isoformat() if l.created_at else None,
        "updated_at": l.updated_at.isoformat() if l.updated_at else None,
    }


def _msg_dict(m: Message) -> dict:
    return {
        "id": m.id,
        "type": m.message_type,
        "direction": m.direction,
        "content": m.content,
        "status": m.status,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _log_message(db: Session, lead_id: int, msg_type: str, direction: str,
                 content: str, external_id: str = None):
    msg = Message(
        lead_id=lead_id,
        message_type=msg_type,
        direction=direction,
        content=content,
        status=MessageStatus.SENT if direction == "outbound" else MessageStatus.DELIVERED,
        external_id=external_id,
        sent_at=datetime.utcnow(),
    )
    db.add(msg)
    db.flush()


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
