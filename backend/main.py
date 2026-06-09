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
from pydantic import BaseModel, EmailStr
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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ======================================================================
# Pydantic schemas
# ======================================================================

class SignupRequest(BaseModel):
    name: str
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

class CredentialsUpdate(BaseModel):
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

@app.post("/auth/signup")
def signup(req: SignupRequest, db: Session = Depends(get_db)):
    if db.query(Agent).filter(Agent.email == req.email.lower()).first():
        raise HTTPException(400, "Email already registered")
    agent = Agent(
        name=req.name,
        email=req.email.lower(),
        password_hash=hash_password(req.password),
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return {"token": create_token(agent.id), "agent": _agent_dict(agent)}


@app.post("/auth/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.email == req.email.lower()).first()
    if not agent or not verify_password(req.password, agent.password_hash):
        raise HTTPException(401, "Invalid email or password")
    return {"token": create_token(agent.id), "agent": _agent_dict(agent)}


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
        "email": agent.email,
        "has_twilio": bool(agent.twilio_sid and agent.twilio_token and agent.twilio_phone),
        "has_gmail": bool(agent.gmail_user and agent.gmail_password),
        "has_bland": bool(agent.bland_key),
        "twilio_phone": agent.twilio_phone,
        "gmail_user": agent.gmail_user,
        "daily_limit": agent.daily_limit,
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
    }


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

    return {
        "total_leads": total_leads,
        "messages_sent_today": msgs_today,
        "consented": consented,
        "pending": pending,
        "opted_out": opted_out,
        "hot_leads": hot_leads,
        "response_rate": response_rate,
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
    background_tasks.add_task(_run_outreach_task, agent.id, req.target_area, req.limit)
    return {"ok": True, "message": f"Outreach started for {req.target_area}"}


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
        sent = 0

        for listing in listings:
            if sent >= limit:
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
