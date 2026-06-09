"""
ZilloAgent FastAPI backend.
Run: uvicorn main:app --reload
"""
import logging
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from chat_handler import ChatHandler
from database import (
    Campaign, Conversation, DailyStats, Lead, LeadStatus, Message,
    Property, SessionLocal, create_tables,
)
from message_sender import MessageSender
from tasks import fetch_and_store_listings, send_daily_outreach

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="ZilloAgent API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:3000").split(","),
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

class CampaignCreate(BaseModel):
    name: str
    target_area: str
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    property_type: Optional[str] = None
    message_style: str = "professional"
    daily_limit: int = 250


class CampaignUpdate(BaseModel):
    name: Optional[str] = None
    target_area: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    property_type: Optional[str] = None
    message_style: Optional[str] = None
    is_active: Optional[bool] = None
    daily_limit: Optional[int] = None


class InboundMessage(BaseModel):
    lead_id: int
    content: str
    channel: str = "sms"  # "sms" | "email"


class TakeoverRequest(BaseModel):
    agent_name: str


class HumanMessageRequest(BaseModel):
    content: str
    channel: str = "sms"


class WebhookSMS(BaseModel):
    From: str
    Body: str
    MessageSid: Optional[str] = None


class WebhookEmail(BaseModel):
    from_email: str
    subject: Optional[str] = None
    text: str


# ======================================================================
# Health
# ======================================================================

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# ======================================================================
# Dashboard stats
# ======================================================================

@app.get("/stats/today")
def stats_today(db: Session = Depends(get_db)):
    today_start = datetime.combine(date.today(), datetime.min.time())
    stats = db.query(DailyStats).filter(DailyStats.date >= today_start).first()

    sender = MessageSender(db)
    _, remaining = sender.check_daily_limit()

    total_leads = db.query(Lead).count()
    hot_leads = db.query(Lead).filter(Lead.score >= 7.0, Lead.human_takeover == False).count()
    pending_handoffs = db.query(Lead).filter(Lead.human_takeover == True, Lead.status == LeadStatus.QUALIFIED).count()

    return {
        "messages_sent_today": stats.messages_sent if stats else 0,
        "replies_today": stats.replies_received if stats else 0,
        "leads_handed_off_today": stats.leads_handed_off if stats else 0,
        "daily_limit_remaining": remaining,
        "total_leads": total_leads,
        "hot_leads": hot_leads,
        "pending_handoffs": pending_handoffs,
    }


@app.get("/stats/history")
def stats_history(days: int = Query(7, ge=1, le=90), db: Session = Depends(get_db)):
    rows = (
        db.query(DailyStats)
        .order_by(DailyStats.date.desc())
        .limit(days)
        .all()
    )
    return [
        {
            "date": r.date.date().isoformat(),
            "messages_sent": r.messages_sent,
            "replies": r.replies_received,
            "handed_off": r.leads_handed_off,
        }
        for r in rows
    ]


# ======================================================================
# Campaigns
# ======================================================================

@app.get("/campaigns")
def list_campaigns(db: Session = Depends(get_db)):
    campaigns = db.query(Campaign).order_by(Campaign.created_at.desc()).all()
    return [_serialize_campaign(c) for c in campaigns]


@app.post("/campaigns", status_code=201)
def create_campaign(payload: CampaignCreate, db: Session = Depends(get_db)):
    campaign = Campaign(**payload.model_dump())
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    return _serialize_campaign(campaign)


@app.patch("/campaigns/{campaign_id}")
def update_campaign(campaign_id: int, payload: CampaignUpdate, db: Session = Depends(get_db)):
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(campaign, field, value)
    db.commit()
    db.refresh(campaign)
    return _serialize_campaign(campaign)


@app.post("/campaigns/{campaign_id}/run")
def run_campaign(campaign_id: int, db: Session = Depends(get_db)):
    """Trigger listing fetch + outreach for a specific campaign."""
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(404, "Campaign not found")
    fetch_and_store_listings.delay(campaign_id=campaign_id)
    send_daily_outreach.delay(campaign_id=campaign_id)
    return {"status": "queued", "campaign_id": campaign_id}


# ======================================================================
# Leads / pipeline
# ======================================================================

@app.get("/leads")
def list_leads(
    status: Optional[str] = None,
    campaign_id: Optional[int] = None,
    min_score: Optional[float] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    q = db.query(Lead)
    if status:
        q = q.filter(Lead.status == status)
    if campaign_id:
        q = q.filter(Lead.campaign_id == campaign_id)
    if min_score is not None:
        q = q.filter(Lead.score >= min_score)
    total = q.count()
    leads = q.order_by(Lead.updated_at.desc()).offset(offset).limit(limit).all()
    return {"total": total, "items": [_serialize_lead(l) for l in leads]}


@app.get("/leads/{lead_id}")
def get_lead(lead_id: int, db: Session = Depends(get_db)):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, "Lead not found")
    return _serialize_lead(lead)


@app.get("/leads/{lead_id}/conversation")
def get_conversation(lead_id: int, db: Session = Depends(get_db)):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, "Lead not found")
    convs = (
        db.query(Conversation)
        .filter(Conversation.lead_id == lead_id)
        .order_by(Conversation.created_at)
        .all()
    )
    return {
        "lead_id": lead_id,
        "human_takeover": lead.human_takeover,
        "messages": [
            {"role": c.role, "content": c.content, "timestamp": c.created_at.isoformat()}
            for c in convs
        ],
    }


@app.post("/leads/{lead_id}/takeover")
def takeover_lead(lead_id: int, payload: TakeoverRequest, db: Session = Depends(get_db)):
    handler = ChatHandler(db)
    ok = handler.take_human_control(lead_id, payload.agent_name)
    if not ok:
        raise HTTPException(404, "Lead not found")
    return {"status": "success", "lead_id": lead_id, "agent": payload.agent_name}


@app.post("/leads/{lead_id}/message")
def send_human_message(lead_id: int, payload: HumanMessageRequest, db: Session = Depends(get_db)):
    handler = ChatHandler(db)
    ok = handler.send_human_message(lead_id, payload.content, payload.channel)
    if not ok:
        raise HTTPException(404, "Lead not found or message failed")
    return {"status": "sent"}


# ======================================================================
# Inbox — inbound messages (manual or from webhooks)
# ======================================================================

@app.post("/inbox/message")
def receive_message(payload: InboundMessage, db: Session = Depends(get_db)):
    handler = ChatHandler(db)
    try:
        result = handler.handle_incoming_message(
            lead_id=payload.lead_id,
            content=payload.content,
            channel=payload.channel,
        )
        return result
    except ValueError as exc:
        raise HTTPException(404, str(exc))


# ------------------------------------------------------------------
# Webhooks — Twilio SMS
# ------------------------------------------------------------------

@app.post("/webhooks/twilio/sms")
def twilio_sms_webhook(payload: WebhookSMS, db: Session = Depends(get_db)):
    """Twilio calls this when a reply SMS is received."""
    phone = payload.From
    lead = db.query(Lead).filter(Lead.contact_phone == phone).first()
    if not lead:
        logger.warning("Received SMS from unknown number: %s", phone)
        return {"status": "ignored"}

    handler = ChatHandler(db)
    result = handler.handle_incoming_message(lead.id, payload.Body, "sms")
    return {"status": "processed", "lead_id": lead.id, "score": result.get("score")}


# ------------------------------------------------------------------
# Webhooks — SendGrid inbound email
# ------------------------------------------------------------------

@app.post("/webhooks/sendgrid/email")
def sendgrid_email_webhook(payload: WebhookEmail, db: Session = Depends(get_db)):
    """SendGrid Inbound Parse webhook."""
    email = payload.from_email
    lead = db.query(Lead).filter(Lead.contact_email == email).first()
    if not lead:
        logger.warning("Received email from unknown address: %s", email)
        return {"status": "ignored"}

    handler = ChatHandler(db)
    result = handler.handle_incoming_message(lead.id, payload.text, "email")
    return {"status": "processed", "lead_id": lead.id, "score": result.get("score")}


# ======================================================================
# Properties
# ======================================================================

@app.get("/properties")
def list_properties(
    city: Optional[str] = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    q = db.query(Property)
    if city:
        q = q.filter(Property.city.ilike(f"%{city}%"))
    total = q.count()
    props = q.order_by(Property.fetched_at.desc()).offset(offset).limit(limit).all()
    return {"total": total, "items": [_serialize_property(p) for p in props]}


# ======================================================================
# Serializers
# ======================================================================

def _serialize_campaign(c: Campaign) -> Dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "target_area": c.target_area,
        "min_price": c.min_price,
        "max_price": c.max_price,
        "property_type": c.property_type,
        "message_style": c.message_style,
        "is_active": c.is_active,
        "daily_limit": c.daily_limit,
        "created_at": c.created_at.isoformat(),
    }


def _serialize_lead(l: Lead) -> Dict[str, Any]:
    prop = l.property
    return {
        "id": l.id,
        "contact_name": l.contact_name,
        "contact_email": l.contact_email,
        "contact_phone": l.contact_phone,
        "status": l.status,
        "score": round(l.score or 0, 2),
        "opted_out": l.opted_out,
        "human_takeover": l.human_takeover,
        "assigned_agent": l.assigned_agent,
        "budget_min": l.budget_min,
        "budget_max": l.budget_max,
        "timeline": l.timeline,
        "campaign_id": l.campaign_id,
        "property": _serialize_property(prop) if prop else None,
        "created_at": l.created_at.isoformat(),
        "updated_at": l.updated_at.isoformat() if l.updated_at else None,
    }


def _serialize_property(p: Property) -> Dict[str, Any]:
    return {
        "id": p.id,
        "zillow_id": p.zillow_id,
        "address": p.address,
        "city": p.city,
        "state": p.state,
        "zip_code": p.zip_code,
        "price": p.price,
        "bedrooms": p.bedrooms,
        "bathrooms": p.bathrooms,
        "sqft": p.sqft,
        "property_type": p.property_type,
        "listing_agent_name": p.listing_agent_name,
        "listing_agent_email": p.listing_agent_email,
        "listing_agent_phone": p.listing_agent_phone,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
    }
