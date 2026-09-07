"""
WhatsApp delivery for the marketing agent.

Two modes, chosen automatically per message:

  • auto   — send through the OFFICIAL WhatsApp Cloud API (Meta Graph API).
             Requires WHATSAPP_TOKEN + WHATSAPP_PHONE_ID. This is the only
             compliant way to auto-send; it respects opt-out and Meta's
             business-messaging policy.

  • manual — no Cloud API configured. We DON'T touch a personal WhatsApp
             account (that gets banned and violates Israel's Amendment 40 /
             US TCPA). Instead we build a ready-to-send wa.me click link that
             the user opens and sends by hand. Fully compliant, zero risk to
             the account.
"""
import logging
import os
import re
from urllib.parse import quote
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

GRAPH_VERSION = os.getenv("WHATSAPP_GRAPH_VERSION", "v21.0")

# Opt-out footer appended to every outbound message, per segment language.
OPT_OUT = {
    "he": "\n\nלהסרה מרשימת התפוצה השב/י \"הסר\".",
    "en": "\n\nReply STOP to opt out.",
}


def normalize_phone(raw: str, default_country: str = "") -> str:
    """Return digits-only E.164-ish phone (no '+'). default_country e.g. '972' or '1'."""
    if not raw:
        return ""
    digits = re.sub(r"[^\d]", "", raw)
    if raw.strip().startswith("+"):
        return digits
    if default_country and not digits.startswith(default_country):
        # local Israeli number like 0501234567 -> 972501234567
        if digits.startswith("0"):
            digits = digits[1:]
        digits = default_country + digits
    return digits


def wa_link(phone: str, message: str) -> str:
    """Build a click-to-send https://wa.me/<phone>?text=... link."""
    digits = re.sub(r"[^\d]", "", phone or "")
    return f"https://wa.me/{digits}?text={quote(message)}"


class WhatsAppSender:
    def __init__(self, token: Optional[str] = None, phone_id: Optional[str] = None):
        self.token    = token    or os.getenv("WHATSAPP_TOKEN", "")
        self.phone_id = phone_id or os.getenv("WHATSAPP_PHONE_ID", "")

    @property
    def auto_enabled(self) -> bool:
        return bool(self.token and self.phone_id)

    def send(self, phone: str, message: str, language: str = "he") -> Tuple[str, Optional[str], str]:
        """
        Returns (status, external_id, link).
          status == "sent"          -> delivered via Cloud API (external_id set)
          status == "queued_manual" -> not auto-sent; use returned wa.me link
          status == "failed"        -> Cloud API attempt failed; link still usable
        """
        body = message + OPT_OUT.get(language, OPT_OUT["en"])
        link = wa_link(phone, body)

        if not self.auto_enabled:
            return "queued_manual", None, link

        try:
            resp = httpx.post(
                f"https://graph.facebook.com/{GRAPH_VERSION}/{self.phone_id}/messages",
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                json={
                    "messaging_product": "whatsapp",
                    "to": re.sub(r"[^\d]", "", phone or ""),
                    "type": "text",
                    "text": {"body": body},
                },
                timeout=20,
            )
            data = resp.json()
            if resp.status_code >= 400:
                logger.error("WhatsApp Cloud API error to %s: %s", phone, data)
                return "failed", None, link
            msg_id = (data.get("messages") or [{}])[0].get("id")
            logger.info("WhatsApp sent to %s: %s", phone, msg_id)
            return "sent", msg_id, link
        except Exception as exc:
            logger.error("WhatsApp send failed to %s: %s", phone, exc)
            return "failed", None, link
