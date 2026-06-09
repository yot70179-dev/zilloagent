import enum
import os
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Text, create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./zilloagent.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class LeadStatus(str, enum.Enum):
    NEW = "new"
    CONTACTED = "contacted"
    RESPONDED = "responded"
    QUALIFIED = "qualified"
    HANDED_OFF = "handed_off"
    OPTED_OUT = "opted_out"


class ConsentStatus(str, enum.Enum):
    PENDING = "pending"
    CONSENTED = "consented"
    OPTED_OUT = "opted_out"


class MessageStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"


# ── Agent (one account per real-estate agent) ─────────────────────────────────
class Agent(Base):
    __tablename__ = "agents"

    id            = Column(Integer, primary_key=True)
    name          = Column(String(200), nullable=False)
    email         = Column(String(200), unique=True, nullable=False)
    password_hash = Column(String(200), nullable=False)

    # Twilio — agent's own numbers
    twilio_sid    = Column(String(200), nullable=True)
    twilio_token  = Column(String(200), nullable=True)
    twilio_phone  = Column(String(50),  nullable=True)   # "+1XXXXXXXXXX"

    # Gmail / SMTP — agent's own email
    gmail_user     = Column(String(200), nullable=True)
    gmail_password = Column(String(200), nullable=True)   # app password

    # Bland.ai — agent's own key (optional; falls back to env)
    bland_key      = Column(String(300), nullable=True)

    # RapidAPI — agent's own key (optional; falls back to env)
    rapidapi_key   = Column(String(200), nullable=True)

    # Settings
    daily_limit   = Column(Integer, default=250)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    campaigns = relationship("Campaign", back_populates="agent")
    leads     = relationship("Lead",     back_populates="agent")


class Property(Base):
    __tablename__ = "properties"

    id                   = Column(Integer, primary_key=True)
    zillow_id            = Column(String(100), unique=True, nullable=False)
    address              = Column(String(500), nullable=False)
    city                 = Column(String(100))
    state                = Column(String(50))
    zip_code             = Column(String(20))
    price                = Column(Float)
    bedrooms             = Column(Integer)
    bathrooms            = Column(Float)
    sqft                 = Column(Integer)
    property_type        = Column(String(100))
    listing_agent_name   = Column(String(200))
    listing_agent_email  = Column(String(200))
    listing_agent_phone  = Column(String(50))
    listing_date         = Column(DateTime)
    fetched_at           = Column(DateTime, default=datetime.utcnow)

    leads = relationship("Lead", back_populates="property")


class Campaign(Base):
    __tablename__ = "campaigns"

    id            = Column(Integer, primary_key=True)
    agent_id      = Column(Integer, ForeignKey("agents.id"), nullable=True)
    name          = Column(String(200), nullable=False)
    target_area   = Column(String(200))
    min_price     = Column(Float)
    max_price     = Column(Float)
    property_type = Column(String(100))
    is_active     = Column(Boolean, default=True)
    daily_limit   = Column(Integer, default=250)
    created_at    = Column(DateTime, default=datetime.utcnow)

    agent = relationship("Agent", back_populates="campaigns")
    leads = relationship("Lead",  back_populates="campaign")


class Lead(Base):
    __tablename__ = "leads"

    id             = Column(Integer, primary_key=True)
    agent_id       = Column(Integer, ForeignKey("agents.id"), nullable=True)
    property_id    = Column(Integer, ForeignKey("properties.id"))
    campaign_id    = Column(Integer, ForeignKey("campaigns.id"))
    contact_name   = Column(String(200))
    contact_email  = Column(String(200))
    contact_phone  = Column(String(50))
    status         = Column(String(50), default=LeadStatus.NEW)
    score          = Column(Float, default=0.0)

    # Consent
    consent_status     = Column(String(20), default=ConsentStatus.PENDING)
    consent_timestamp  = Column(DateTime, nullable=True)
    followup_sent      = Column(Boolean, default=False)
    followup_sent_at   = Column(DateTime, nullable=True)

    # Call
    call_id        = Column(String(200), nullable=True)
    call_recording = Column(String(500), nullable=True)
    call_transcript= Column(Text,        nullable=True)

    # Lead timezone
    local_timezone = Column(String(100), nullable=True)

    opted_out      = Column(Boolean, default=False)
    human_takeover = Column(Boolean, default=False)
    notes          = Column(Text)
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    agent        = relationship("Agent",    back_populates="leads")
    property     = relationship("Property", back_populates="leads")
    campaign     = relationship("Campaign", back_populates="leads")
    messages     = relationship("Message",  back_populates="lead")
    conversations= relationship("Conversation", back_populates="lead")


class Message(Base):
    __tablename__ = "messages"

    id           = Column(Integer, primary_key=True)
    lead_id      = Column(Integer, ForeignKey("leads.id"))
    message_type = Column(String(20))   # sms | email | call
    direction    = Column(String(10))   # outbound | inbound
    content      = Column(Text, nullable=False)
    subject      = Column(String(500))
    status       = Column(String(50), default=MessageStatus.PENDING)
    external_id  = Column(String(200))
    sent_at      = Column(DateTime)
    is_follow_up = Column(Boolean, default=False)
    created_at   = Column(DateTime, default=datetime.utcnow)

    lead = relationship("Lead", back_populates="messages")


class Conversation(Base):
    __tablename__ = "conversations"

    id         = Column(Integer, primary_key=True)
    lead_id    = Column(Integer, ForeignKey("leads.id"))
    role       = Column(String(20))   # user | assistant
    content    = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    lead = relationship("Lead", back_populates="conversations")


class DailyStats(Base):
    __tablename__ = "daily_stats"

    id               = Column(Integer, primary_key=True)
    agent_id         = Column(Integer, ForeignKey("agents.id"), nullable=True)
    date             = Column(DateTime, nullable=False)
    messages_sent    = Column(Integer, default=0)
    calls_made       = Column(Integer, default=0)
    replies_received = Column(Integer, default=0)
    leads_qualified  = Column(Integer, default=0)
    hot_leads        = Column(Integer, default=0)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    Base.metadata.create_all(bind=engine)
