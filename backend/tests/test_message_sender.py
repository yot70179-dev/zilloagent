"""Unit tests for MessageSender daily-limit logic."""
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base, Campaign, DailyStats, Lead, LeadStatus, Property
from message_sender import DAILY_LIMIT, DailyLimitReached, MessageSender

# In-memory SQLite for tests
engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
TestSession = sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db():
    session = TestSession()
    yield session
    session.close()


@pytest.fixture
def lead(db):
    prop = Property(zillow_id="Z1", address="1 Test St", city="Austin", state="TX",
                    price=300000, listing_agent_name="Bob")
    db.add(prop)
    db.flush()
    campaign = Campaign(name="Test Campaign", target_area="Austin")
    db.add(campaign)
    db.flush()
    l = Lead(
        property_id=prop.id,
        campaign_id=campaign.id,
        contact_name="Bob Agent",
        contact_phone="+15551234567",
        contact_email="bob@example.com",
        status=LeadStatus.NEW,
    )
    db.add(l)
    db.commit()
    return l


def test_check_daily_limit_fresh(db):
    sender = MessageSender(db)
    can_send, remaining = sender.check_daily_limit()
    assert can_send is True
    assert remaining == DAILY_LIMIT


def test_daily_limit_reached(db):
    today_start = datetime.combine(date.today(), datetime.min.time())
    db.add(DailyStats(date=today_start, messages_sent=DAILY_LIMIT))
    db.commit()

    sender = MessageSender(db)
    can_send, remaining = sender.check_daily_limit()
    assert can_send is False
    assert remaining == 0


def test_send_sms_raises_when_opted_out(db, lead):
    lead.opted_out = True
    db.commit()
    sender = MessageSender(db)
    with pytest.raises(ValueError, match="opted out"):
        sender.send_sms(lead, "Hello!")


def test_send_sms_raises_at_daily_limit(db, lead):
    today_start = datetime.combine(date.today(), datetime.min.time())
    db.add(DailyStats(date=today_start, messages_sent=DAILY_LIMIT))
    db.commit()
    sender = MessageSender(db)
    with pytest.raises(DailyLimitReached):
        sender.send_sms(lead, "Hello!")


def test_send_sms_success(db, lead):
    sender = MessageSender(db)
    with patch.object(sender, "twilio") as mock_twilio:
        mock_twilio.messages.create.return_value = MagicMock(sid="SM123")
        msg = sender.send_sms(lead, "Hi Bob!")

    assert msg.status == "sent"
    assert msg.external_id == "SM123"

    _, remaining = sender.check_daily_limit()
    assert remaining == DAILY_LIMIT - 1
