"""
Outbound message delivery via Twilio (SMS) or SendGrid (email).
Hard limit: DAILY_MESSAGE_LIMIT messages per day across all channels.
"""
import logging
import os
from datetime import date, datetime
from typing import Tuple

from sqlalchemy.orm import Session

from database import DailyStats, Lead, Message, MessageStatus

logger = logging.getLogger(__name__)

DAILY_LIMIT = int(os.getenv("DAILY_MESSAGE_LIMIT", "250"))
OPT_OUT_FOOTER_SMS = "\n\nReply STOP to unsubscribe."
OPT_OUT_FOOTER_EMAIL = "\n\n---\nTo unsubscribe reply UNSUBSCRIBE or click the link below."


class DailyLimitReached(Exception):
    pass


class MessageSender:
    def __init__(self, db: Session):
        self.db = db
        self._twilio = None
        self._sg = None

    # ------------------------------------------------------------------
    # Daily-limit helpers
    # ------------------------------------------------------------------

    def _today_start(self) -> datetime:
        return datetime.combine(date.today(), datetime.min.time())

    def _stats_today(self) -> DailyStats:
        today_start = self._today_start()
        stats = self.db.query(DailyStats).filter(DailyStats.date >= today_start).first()
        if not stats:
            stats = DailyStats(date=today_start, messages_sent=0, replies_received=0,
                               leads_qualified=0, leads_handed_off=0)
            self.db.add(stats)
            self.db.commit()
        return stats

    def check_daily_limit(self) -> Tuple[bool, int]:
        """Returns (can_send, remaining)."""
        sent = self._stats_today().messages_sent
        remaining = DAILY_LIMIT - sent
        return remaining > 0, max(remaining, 0)

    def _increment_sent(self):
        stats = self._stats_today()
        stats.messages_sent += 1
        self.db.commit()

    # ------------------------------------------------------------------
    # Lazy-loaded SDK clients
    # ------------------------------------------------------------------

    @property
    def twilio(self):
        if self._twilio is None:
            from twilio.rest import Client
            self._twilio = Client(
                os.getenv("TWILIO_ACCOUNT_SID", ""),
                os.getenv("TWILIO_AUTH_TOKEN", ""),
            )
        return self._twilio

    @property
    def sendgrid(self):
        if self._sg is None:
            import sendgrid as sg
            self._sg = sg.SendGridAPIClient(api_key=os.getenv("SENDGRID_API_KEY", ""))
        return self._sg

    # ------------------------------------------------------------------
    # Send methods
    # ------------------------------------------------------------------

    def send_sms(self, lead: Lead, content: str, is_follow_up: bool = False) -> Message:
        can_send, _ = self.check_daily_limit()
        if not can_send:
            raise DailyLimitReached(f"Daily limit of {DAILY_LIMIT} reached")
        if lead.opted_out:
            raise ValueError(f"Lead {lead.id} has opted out")
        if not lead.contact_phone:
            raise ValueError(f"Lead {lead.id} has no phone number")

        msg = self._create_message_record(lead, "sms", content, is_follow_up=is_follow_up)

        try:
            tw_msg = self.twilio.messages.create(
                body=content + OPT_OUT_FOOTER_SMS,
                from_=os.getenv("TWILIO_PHONE_NUMBER", ""),
                to=lead.contact_phone,
            )
            msg.status = MessageStatus.SENT
            msg.external_id = tw_msg.sid
            msg.sent_at = datetime.utcnow()
            self.db.commit()
            self._increment_sent()
            logger.info("SMS sent lead=%s sid=%s", lead.id, tw_msg.sid)
        except Exception as exc:
            msg.status = MessageStatus.FAILED
            self.db.commit()
            logger.error("SMS failed lead=%s: %s", lead.id, exc)
            raise

        return msg

    def send_email(
        self,
        lead: Lead,
        content: str,
        subject: str = "Property Opportunity",
        is_follow_up: bool = False,
    ) -> Message:
        can_send, _ = self.check_daily_limit()
        if not can_send:
            raise DailyLimitReached(f"Daily limit of {DAILY_LIMIT} reached")
        if lead.opted_out:
            raise ValueError(f"Lead {lead.id} has opted out")
        if not lead.contact_email:
            raise ValueError(f"Lead {lead.id} has no email")

        msg = self._create_message_record(lead, "email", content, subject=subject, is_follow_up=is_follow_up)

        try:
            from sendgrid.helpers.mail import Mail
            mail = Mail(
                from_email=os.getenv("SENDGRID_FROM_EMAIL", ""),
                to_emails=lead.contact_email,
                subject=subject,
                plain_text_content=content + OPT_OUT_FOOTER_EMAIL,
            )
            response = self.sendgrid.send(mail)
            msg.status = MessageStatus.SENT
            msg.external_id = (response.headers or {}).get("X-Message-Id", "")
            msg.sent_at = datetime.utcnow()
            self.db.commit()
            self._increment_sent()
            logger.info("Email sent lead=%s msg_id=%s", lead.id, msg.external_id)
        except Exception as exc:
            msg.status = MessageStatus.FAILED
            self.db.commit()
            logger.error("Email failed lead=%s: %s", lead.id, exc)
            raise

        return msg

    def handle_opt_out(self, lead: Lead):
        lead.opted_out = True
        lead.status = "opted_out"
        self.db.commit()
        logger.info("Lead %s opted out", lead.id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_message_record(
        self, lead: Lead, msg_type: str, content: str,
        subject: str | None = None, is_follow_up: bool = False,
    ) -> Message:
        record = Message(
            lead_id=lead.id,
            message_type=msg_type,
            direction="outbound",
            content=content,
            subject=subject,
            status=MessageStatus.PENDING,
            is_follow_up=is_follow_up,
        )
        self.db.add(record)
        self.db.commit()
        return record
