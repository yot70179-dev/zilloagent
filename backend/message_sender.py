"""Send SMS and email using the agent's own credentials."""
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)

STOP_FOOTER_SMS   = "\nReply STOP to opt out."
STOP_FOOTER_EMAIL = "\n\n---\nReply STOP to unsubscribe."


class MessageSender:
    def __init__(self, agent=None):
        """
        If agent is provided, use agent's credentials.
        Otherwise fall back to env variables.
        """
        if agent:
            self.twilio_sid   = agent.twilio_sid   or os.getenv("TWILIO_ACCOUNT_SID", "")
            self.twilio_token = agent.twilio_token or os.getenv("TWILIO_AUTH_TOKEN", "")
            self.twilio_from  = agent.twilio_phone  or os.getenv("TWILIO_PHONE_NUMBER", "")
            self.gmail_user   = agent.gmail_user   or os.getenv("GMAIL_USER", "")
            self.gmail_pass   = agent.gmail_password or os.getenv("GMAIL_APP_PASSWORD", "")
            self.agent_name   = agent.name
            self.bland_key    = agent.bland_key or os.getenv("BLANDAI_KEY", "")
        else:
            self.twilio_sid   = os.getenv("TWILIO_ACCOUNT_SID", "")
            self.twilio_token = os.getenv("TWILIO_AUTH_TOKEN", "")
            self.twilio_from  = os.getenv("TWILIO_PHONE_NUMBER", "")
            self.gmail_user   = os.getenv("GMAIL_USER", "")
            self.gmail_pass   = os.getenv("GMAIL_APP_PASSWORD", "")
            self.agent_name   = "Real Estate Acquisitions Team"
            self.bland_key    = os.getenv("BLANDAI_KEY", "")

    # ── SMS via agent's Twilio ────────────────────────────────────────────────
    def send_sms(self, to: str, body: str) -> Optional[str]:
        if not all([self.twilio_sid, self.twilio_token, self.twilio_from]):
            logger.warning("Twilio credentials missing — SMS not sent to %s", to)
            return None
        try:
            from twilio.rest import Client
            client = Client(self.twilio_sid, self.twilio_token)
            msg = client.messages.create(
                body=body + STOP_FOOTER_SMS,
                from_=self.twilio_from,
                to=to,
            )
            logger.info("SMS sent to %s: %s", to, msg.sid)
            return msg.sid
        except Exception as exc:
            logger.error("SMS failed to %s: %s", to, exc)
            return None

    # ── Email via agent's Gmail ───────────────────────────────────────────────
    def send_email(self, to_email: str, to_name: str, subject: str, body: str) -> bool:
        if not all([self.gmail_user, self.gmail_pass]):
            logger.warning("Gmail credentials missing — email not sent to %s", to_email)
            return False
        try:
            msg = MIMEMultipart()
            msg["From"]    = f"{self.agent_name} <{self.gmail_user}>"
            msg["To"]      = f"{to_name} <{to_email}>"
            msg["Subject"] = subject
            msg.attach(MIMEText(body + STOP_FOOTER_EMAIL, "plain"))
            with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
                smtp.starttls()
                smtp.login(self.gmail_user, self.gmail_pass)
                smtp.send_message(msg)
            logger.info("Email sent to %s", to_email)
            return True
        except Exception as exc:
            logger.error("Email failed to %s: %s", to_email, exc)
            return False

    # ── Bland.ai call via agent's key ────────────────────────────────────────
    def make_call(self, to: str, task: str, from_number: Optional[str] = None,
                  record: bool = True, max_duration: int = 3) -> Optional[str]:
        frm = from_number or self.twilio_from or os.getenv("FROM_NUMBER", "")
        if not self.bland_key:
            logger.warning("No Bland.ai key — call not made to %s", to)
            return None
        try:
            import httpx
            resp = httpx.post(
                "https://api.bland.ai/v1/calls",
                headers={"authorization": self.bland_key, "Content-Type": "application/json"},
                json={
                    "phone_number": to,
                    "from": frm,
                    "task": task,
                    "voice": "nat",
                    "wait_for_greeting": True,
                    "record": record,
                    "max_duration": max_duration,
                    "answered_by_enabled": True,
                },
                timeout=15,
            )
            data = resp.json()
            call_id = data.get("call_id")
            logger.info("Call initiated to %s: %s", to, call_id)
            return call_id
        except Exception as exc:
            logger.error("Call failed to %s: %s", to, exc)
            return None
