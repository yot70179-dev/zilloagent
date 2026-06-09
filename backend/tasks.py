"""
Celery background tasks:
  - Fetch new listings from Zillow and create leads
  - Send daily outreach batch (respects 250/day hard limit)
  - Send follow-ups to non-responders after 3 days
"""
import logging
import os
from datetime import datetime, timedelta

from celery import Celery
from celery.schedules import crontab
from sqlalchemy.orm import Session

from ai_agent import AIAgent
from database import Campaign, Lead, LeadStatus, Message, Property, SessionLocal, create_tables
from message_sender import DailyLimitReached, MessageSender
from zillow_client import ZillowClient

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
        "send-daily-outreach": {
            "task": "tasks.send_daily_outreach",
            "schedule": crontab(hour=9, minute=0),  # 09:00 UTC daily
        },
        "send-follow-ups": {
            "task": "tasks.send_follow_ups",
            "schedule": crontab(hour=10, minute=0), # 10:00 UTC daily
        },
    },
)


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
                contact_name=raw.get("listing_agent_name", ""),
                contact_email=raw.get("listing_agent_email", ""),
                contact_phone=raw.get("listing_agent_phone", ""),
                status=LeadStatus.NEW,
            ))

    db.commit()
    return count


# ------------------------------------------------------------------
# Task: daily outreach
# ------------------------------------------------------------------

@app.task(bind=True, max_retries=2, default_retry_delay=120)
def send_daily_outreach(self, campaign_id: int | None = None):
    """Send opening messages to NEW leads, respecting the daily limit."""
    db: Session = SessionLocal()
    ai = AIAgent()
    sender = MessageSender(db)
    try:
        can_send, remaining = sender.check_daily_limit()
        if not can_send:
            logger.info("Daily limit reached — skipping outreach")
            return

        query = (
            db.query(Lead)
            .join(Lead.property)
            .join(Lead.campaign)
            .filter(
                Lead.status == LeadStatus.NEW,
                Lead.opted_out == False,
                Campaign.is_active == True,
            )
            .limit(remaining)
        )
        if campaign_id:
            query = query.filter(Lead.campaign_id == campaign_id)

        leads = query.all()
        sent = 0
        for lead in leads:
            try:
                prop = lead.property
                campaign = lead.campaign
                message = ai.generate_opening_message(
                    agent_name=lead.contact_name or "",
                    address=prop.address,
                    price=prop.price or 0,
                    city=prop.city or "",
                    description=prop.description or "",
                    style=campaign.message_style,
                )
                # Zillow provides agent email — prefer email, fall back to SMS
                if lead.contact_email:
                    sender.send_email(lead, message, subject="Property Opportunity")
                elif lead.contact_phone:
                    sender.send_sms(lead, message)

                lead.status = LeadStatus.CONTACTED
                db.commit()
                sent += 1
            except DailyLimitReached:
                logger.info("Hit daily limit mid-batch after %d messages", sent)
                break
            except Exception as exc:
                logger.error("Failed outreach lead=%s: %s", lead.id, exc)

        logger.info("Daily outreach: sent %d messages", sent)
    except Exception as exc:
        logger.error("send_daily_outreach error: %s", exc)
        raise self.retry(exc=exc)
    finally:
        db.close()


# ------------------------------------------------------------------
# Task: follow-ups
# ------------------------------------------------------------------

@app.task(bind=True, max_retries=2, default_retry_delay=120)
def send_follow_ups(self):
    """Send one follow-up to leads contacted 3 days ago with no reply."""
    db: Session = SessionLocal()
    ai = AIAgent()
    sender = MessageSender(db)
    cutoff = datetime.utcnow() - timedelta(days=3)

    try:
        can_send, remaining = sender.check_daily_limit()
        if not can_send:
            return

        # Leads contacted > 3 days ago, still in CONTACTED state, no follow-up sent
        leads = (
            db.query(Lead)
            .join(Lead.property)
            .filter(
                Lead.status == LeadStatus.CONTACTED,
                Lead.opted_out == False,
                Lead.updated_at <= cutoff,
            )
            .limit(remaining)
            .all()
        )

        sent = 0
        for lead in leads:
            # Skip if a follow-up was already sent
            already_followed = (
                db.query(Message)
                .filter(Message.lead_id == lead.id, Message.is_follow_up == True)
                .first()
            )
            if already_followed:
                continue

            try:
                prop = lead.property
                msg = ai.generate_follow_up_message(
                    agent_name=lead.contact_name or "",
                    address=prop.address,
                    price=prop.price or 0,
                    city=prop.city or "",
                )
                if lead.contact_email:
                    sender.send_email(lead, msg, is_follow_up=True)
                elif lead.contact_phone:
                    sender.send_sms(lead, msg, is_follow_up=True)
                sent += 1
            except DailyLimitReached:
                break
            except Exception as exc:
                logger.error("Follow-up failed lead=%s: %s", lead.id, exc)

        logger.info("Follow-ups: sent %d messages", sent)
    except Exception as exc:
        logger.error("send_follow_ups error: %s", exc)
        raise self.retry(exc=exc)
    finally:
        db.close()
