"""
Celery background tasks:
  - Fetch new listings from Zillow and create leads
  - Send consent SMS (PENDING → YES → AI call  |  STOP → DNC)
  - Follow-ups to non-responders after 3 days
  - Hot-lead email alerts
"""
import logging
import os
from datetime import datetime, timedelta

import pytz
from celery import Celery
from celery.schedules import crontab
from sqlalchemy.orm import Session

from database import (
    Agent, Campaign, ConsentStatus, Lead, LeadStatus,
    Message, MessageStatus, Property, SessionLocal, create_tables,
)
from message_sender import MessageSender

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

app = Celery("zilloagent", broker=REDIS_URL, backend=REDIS_URL)
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "fetch-listings-every-hour": {
            "task": "tasks.fetch_and_store_listings",
            "schedule": crontab(minute=0),          # top of every hour
        },
        # 06:00 UTC = 09:00 Israel — send consent SMS to new leads
        "send-daily-consent-sms": {
            "task": "tasks.send_daily_consent_sms_all",
            "schedule": crontab(hour=6, minute=0),
        },
        # 07:00 UTC — follow-ups to PENDING leads > 3 days old
        "send-follow-ups": {
            "task": "tasks.send_follow_ups_all",
            "schedule": crontab(hour=7, minute=0),
        },
    },
)

# ── Timezone helper ───────────────────────────────────────────────────────────
_AREA_TZ = {
    "212":"America/New_York","646":"America/New_York","917":"America/New_York",
    "718":"America/New_York","347":"America/New_York","929":"America/New_York",
    "213":"America/Los_Angeles","310":"America/Los_Angeles","323":"America/Los_Angeles",
    "424":"America/Los_Angeles","818":"America/Los_Angeles","747":"America/Los_Angeles",
    "512":"America/Chicago","737":"America/Chicago",
    "214":"America/Chicago","469":"America/Chicago","972":"America/Chicago",
    "713":"America/Chicago","832":"America/Chicago","281":"America/Chicago",
    "305":"America/New_York","786":"America/New_York",
    "404":"America/New_York","678":"America/New_York",
    "619":"America/Los_Angeles","858":"America/Los_Angeles",
}

def _tz_for_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("1"):
        digits = digits[1:]
    return _AREA_TZ.get(digits[:3], "America/New_York")

def _is_calling_hours(phone: str) -> bool:
    try:
        tz  = pytz.timezone(_tz_for_phone(phone))
        now = datetime.now(tz)
        return 9 <= now.hour < 21
    except Exception:
        return False


# ------------------------------------------------------------------
# Task: fetch and store listings
# ------------------------------------------------------------------

@app.task(bind=True, max_retries=3, default_retry_delay=60)
def fetch_and_store_listings(self, campaign_id: int | None = None):
    """Fetch listings from Zillow for all active campaigns (or a specific one)."""
    db: Session = SessionLocal()
    client = ZillowClient()
    try:
        import asyncio
        query = db.query(Campaign).filter(Campaign.is_active == True)
        if campaign_id:
            query = query.filter(Campaign.id == campaign_id)
        campaigns = query.all()

        for campaign in campaigns:
            try:
                listings = asyncio.run(
                    client.fetch_listings(
                        city=campaign.target_area,
                        min_price=campaign.min_price,
                        max_price=campaign.max_price,
                        property_type=campaign.property_type,
                        limit=100,
                    )
                )
                created = _upsert_listings_and_leads(db, listings, campaign)
                logger.info("Campaign %s: upserted %d listings", campaign.id, created)
            except Exception as exc:
                logger.error("Listing fetch failed campaign=%s: %s", campaign.id, exc)
    except Exception as exc:
        logger.error("fetch_and_store_listings error: %s", exc)
        raise self.retry(exc=exc)
    finally:
        db.close()


def _upsert_listings_and_leads(db: Session, listings: list, campaign: Campaign) -> int:
    count = 0
    for raw in listings:
        if not raw.get("zillow_id"):
            continue

        # Upsert property
        prop = db.query(Property).filter(Property.zillow_id == raw["zillow_id"]).first()
        if not prop:
            prop = Property(**{k: v for k, v in raw.items() if k != "id"})
            db.add(prop)
            db.flush()
            count += 1

        # Create lead only once per property+campaign
        existing_lead = (
            db.query(Lead)
            .filter(Lead.property_id == prop.id, Lead.campaign_id == campaign.id)
            .first()
        )
        if not existing_lead:
            db.add(Lead(
                property_id=prop.id,
                campaign_id=campaign.id,
                agent_id=campaign.agent_id,        # multi-tenant
                contact_name=raw.get("listing_agent_name", ""),
                contact_email=raw.get("listing_agent_email", ""),
                contact_phone=raw.get("listing_agent_phone", ""),
                status=LeadStatus.NEW,
                consent_status=ConsentStatus.PENDING,   # consent flow
            ))

    db.commit()
    return count


# ------------------------------------------------------------------
# Task: send consent SMS to a single lead
# ------------------------------------------------------------------

def _consent_sms_body(lead: Lead) -> str:
    first = (lead.contact_name or "there").split()[0]
    prop  = lead.property
    city  = prop.city if prop else "your area"
    addr  = prop.address if prop else ""
    price = f"${int(prop.price):,}" if prop and prop.price else ""
    detail = f" at {addr} ({price})" if addr and price else ""
    return (
        f"Hi {first}, I have pre-qualified buyers looking in {city}. "
        f"Interested in connecting them with your listing{detail}? "
        f"Reply YES for a call, STOP to opt out."
    )


@app.task(bind=True, name="tasks.send_consent_sms_lead", max_retries=2, default_retry_delay=60)
def send_consent_sms_lead(self, lead_id: int):
    """Send initial consent SMS to one lead."""
    db: Session = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead or not lead.contact_phone:
            return
        if lead.consent_status != ConsentStatus.PENDING:
            return

        sender = MessageSender(lead.agent)
        body   = _consent_sms_body(lead)
        sid    = sender.send_sms(lead.contact_phone, body)
        if sid:
            db.add(Message(
                lead_id=lead.id, message_type="sms", direction="outbound",
                content=body, status=MessageStatus.SENT,
                external_id=sid, sent_at=datetime.utcnow(),
            ))
            lead.status = LeadStatus.CONTACTED
            db.commit()
            logger.info("Consent SMS sent to lead %d", lead_id)
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc)
    finally:
        db.close()


# ------------------------------------------------------------------
# Task: make AI call after YES
# ------------------------------------------------------------------

def _call_script(lead: Lead) -> str:
    first = (lead.contact_name or "there").split()[0]
    prop  = lead.property
    city  = prop.city if prop else "your area"
    addr  = prop.address if prop else ""
    price = f"${int(prop.price):,}" if prop and prop.price else ""
    detail = f" at {addr} listed at {price}" if addr and price else ""
    return (
        f"Hi, may I speak with {first}? "
        f"I'm calling because you expressed interest in connecting with buyers in {city}. "
        f"I have qualified buyers looking for properties like yours{detail}. "
        f"Do you have 2 minutes to chat?"
    )


@app.task(bind=True, name="tasks.make_ai_call", max_retries=48, default_retry_delay=1800)
def make_ai_call(self, lead_id: int):
    """Call a consented lead — respects 9 AM–9 PM local time."""
    db: Session = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead or not lead.contact_phone:
            return
        if lead.consent_status != ConsentStatus.CONSENTED:
            return

        if not _is_calling_hours(lead.contact_phone):
            raise self.retry(countdown=1800)

        sender  = MessageSender(lead.agent)
        call_id = sender.make_call(lead.contact_phone, _call_script(lead))
        if call_id:
            lead.call_id = call_id
            lead.status  = LeadStatus.CONTACTED
            db.commit()
            logger.info("AI call placed lead=%d call_id=%s", lead_id, call_id)
    except self.MaxRetriesExceededError:
        logger.error("Max retries exceeded for AI call to lead %d", lead_id)
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc, countdown=120)
    finally:
        db.close()


# ------------------------------------------------------------------
# Daily: send consent SMS to all new leads for all active agents
# ------------------------------------------------------------------

@app.task(name="tasks.send_daily_consent_sms_all")
def send_daily_consent_sms_all():
    """06:00 UTC — queue consent SMS for new leads across all agents."""
    db: Session = SessionLocal()
    try:
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        agents = db.query(Agent).filter(Agent.is_active == True).all()
        for agent in agents:
            if not agent.twilio_sid or not agent.twilio_phone:
                continue
            sent_today = (
                db.query(Message)
                .join(Lead, Message.lead_id == Lead.id)
                .filter(Lead.agent_id == agent.id, Message.direction == "outbound",
                        Message.sent_at >= today)
                .count()
            )
            remaining = max(0, (agent.daily_limit or 250) - sent_today)
            if remaining == 0:
                continue
            leads = (
                db.query(Lead)
                .filter(Lead.agent_id == agent.id, Lead.status == LeadStatus.NEW,
                        Lead.consent_status == ConsentStatus.PENDING,
                        Lead.opted_out == False, Lead.contact_phone != None)
                .limit(remaining).all()
            )
            for lead in leads:
                send_consent_sms_lead.delay(lead.id)
            logger.info("Agent %d: queued %d consent SMSes", agent.id, len(leads))
    except Exception as exc:
        logger.error("send_daily_consent_sms_all error: %s", exc)
    finally:
        db.close()


# ------------------------------------------------------------------
# Daily: follow-ups to PENDING leads > 3 days old
# ------------------------------------------------------------------

def _followup_body(lead: Lead) -> str:
    first = (lead.contact_name or "there").split()[0]
    city  = lead.property.city if lead.property else "your area"
    return (
        f"Hi {first}, just following up — I still have buyers looking in {city}. "
        f"Reply YES to connect, or STOP to opt out."
    )


@app.task(name="tasks.send_follow_ups_all")
def send_follow_ups_all():
    """07:00 UTC — send one follow-up to PENDING leads older than 3 days."""
    db: Session = SessionLocal()
    cutoff = datetime.utcnow() - timedelta(days=3)
    try:
        leads = (
            db.query(Lead)
            .filter(Lead.consent_status == ConsentStatus.PENDING,
                    Lead.followup_sent == False, Lead.opted_out == False,
                    Lead.created_at <= cutoff, Lead.contact_phone != None)
            .all()
        )
        logger.info("Follow-ups: %d eligible leads", len(leads))
        for lead in leads:
            try:
                sender = MessageSender(lead.agent)
                body   = _followup_body(lead)
                sid    = sender.send_sms(lead.contact_phone, body)
                if sid:
                    lead.followup_sent    = True
                    lead.followup_sent_at = datetime.utcnow()
                    db.add(Message(
                        lead_id=lead.id, message_type="sms", direction="outbound",
                        content=body, status=MessageStatus.SENT,
                        external_id=sid, sent_at=datetime.utcnow(), is_follow_up=True,
                    ))
                    db.commit()
            except Exception as exc:
                logger.error("Follow-up failed lead=%d: %s", lead.id, exc)
                db.rollback()
    except Exception as exc:
        logger.error("send_follow_ups_all error: %s", exc)
    finally:
        db.close()


# ------------------------------------------------------------------
# Hot-lead email alert
# ------------------------------------------------------------------

@app.task(bind=True, name="tasks.hot_lead_alert", max_retries=2, default_retry_delay=60)
def hot_lead_alert(self, lead_id: int):
    db: Session = SessionLocal()
    try:
        lead  = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead or not lead.agent:
            return
        agent  = lead.agent
        sender = MessageSender(agent)
        prop   = lead.property
        body   = (
            f"🔥 Hot lead!\n\n"
            f"Name:  {lead.contact_name or '—'}\n"
            f"Phone: {lead.contact_phone or '—'}\n"
            f"Email: {lead.contact_email or '—'}\n"
            f"Score: {lead.score}/10\n"
            + (f"Property: {prop.address}, {prop.city} — ${int(prop.price):,}\n" if prop and prop.price else "")
            + f"\nLog in to ZilloAgent to take over."
        )
        sender.send_email(
            to_email=agent.gmail_user or agent.email,
            to_name=agent.name,
            subject=f"🔥 Hot Lead: {lead.contact_name or 'Unknown'} — {lead.score}/10",
            body=body,
        )
    except Exception as exc:
        db.rollback()
        raise self.retry(exc=exc)
    finally:
        db.close()
