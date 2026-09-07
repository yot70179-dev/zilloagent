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

# Pre-approved marketing template for the FIRST (cold) contact. Cloud API refuses
# free-form text to anyone who hasn't messaged you in the last 24h, so cold outreach
# MUST use a template. Submit the matching copy in WhatsApp Manager (see
# SETUP_WHATSAPP.md) and set WHATSAPP_TEMPLATE_NAME to its name.
TEMPLATE_NAME  = os.getenv("WHATSAPP_TEMPLATE_NAME", "")
TEMPLATE_LANG  = {"he": os.getenv("WHATSAPP_TEMPLATE_LANG_HE", "he"),
                  "en": os.getenv("WHATSAPP_TEMPLATE_LANG_EN", "en_US")}

SENDER_NAME    = os.getenv("MK_SENDER_NAME", "יותם")       # Hebrew
SENDER_NAME_EN = os.getenv("MK_SENDER_NAME_EN", "Yotam")   # English

# Local copy of the template body — must MATCH the text approved on Meta, with
# {{1}}=name, {{2}}=business. Used to render what we store/preview locally.
TEMPLATE_TEXT = {
    "he": "היי {name}, שמי " + SENDER_NAME + ", מעצב דפי נחיתה. בניתי דוגמה של דף נחיתה "
          "ל{business} רק כדי להראות איך זה יכול להיראות — חשוב לי שתדע/י שזו דוגמה ראשונית בלבד. "
          "אם תרצה/י אשמח לשלוח לך אותה, ונוכל להתקדם למשהו מותאם ואמיתי. תודה על זמנך!",
    "en": "Hi {name}, I'm " + SENDER_NAME_EN + ", a landing-page designer. I built a sample landing "
          "page for {business} just to show how it could look — it's only an initial sample. "
          "If you'd like, I'll happily send it over and we can take it to something custom and real. "
          "Thanks for your time!",
}

# Opt-out footer appended to every outbound message, per segment language.
OPT_OUT = {
    "he": "\n\nלהסרה מרשימת התפוצה השב/י \"הסר\".",
    "en": "\n\nReply STOP to opt out.",
}


# Follow-up templates (also need Meta approval — a non-replier is outside the 24h
# window). {{1}}=name, {{2}}=business. Set the *_NAME vars once approved.
FOLLOWUP1_NAME = os.getenv("WHATSAPP_TEMPLATE_FOLLOWUP1", "")
FOLLOWUP2_NAME = os.getenv("WHATSAPP_TEMPLATE_FOLLOWUP2", "")

FOLLOWUP_TEXT = {
    1: {
        "he": "היי {name}, רק מוודא שההודעה הקודמת הגיעה 🙂 אשמח לשלוח לך את דוגמת דף "
              "הנחיתה שהכנתי ל{business} — בלי שום התחייבות. שאשלח?",
        "en": "Hi {name}, just checking my earlier message reached you 🙂 I'd be glad to "
              "send over the sample landing page I made for {business} — no obligation at all. "
              "Want me to?",
    },
    2: {
        "he": "היי {name}, לא רוצה להציק — אם זה לא רלוונטי כרגע, לגמרי בסדר. הדוגמה עדיין "
              "מוכנה אם תרצה/י להציץ. בהצלחה עם {business}! 🙏",
        "en": "Hi {name}, I won't keep bugging you — if it's not relevant right now, that's "
              "totally fine. The sample is still ready if you'd like a peek. Wishing you lots "
              "of success with {business}! 🙏",
    },
}


def _fill(tmpl: str, language: str, name: str, business: str) -> str:
    return tmpl.format(name=name or ("there" if language == "en" else "שלום"),
                       business=business or ("your business" if language == "en" else "העסק שלך"))


def render_template(language: str, name: str = "", business: str = "") -> str:
    return _fill(TEMPLATE_TEXT.get(language, TEMPLATE_TEXT["en"]), language, name, business)


def render_followup(stage: int, language: str, name: str = "", business: str = "") -> str:
    stage = 2 if stage >= 2 else 1
    return _fill(FOLLOWUP_TEXT[stage].get(language, FOLLOWUP_TEXT[stage]["en"]),
                 language, name, business)


def followup_template_name(stage: int) -> str:
    return FOLLOWUP2_NAME if stage >= 2 else FOLLOWUP1_NAME


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

    def verify(self) -> dict:
        """Live check that token + phone_id work against the Graph API.
        Returns a dict with 'ok' plus the verified number info (no secrets)."""
        if not self.auto_enabled:
            return {"ok": False, "reason": "WHATSAPP_TOKEN / WHATSAPP_PHONE_ID not set"}
        try:
            r = httpx.get(
                f"https://graph.facebook.com/{GRAPH_VERSION}/{self.phone_id}",
                params={"fields": "display_phone_number,verified_name,quality_rating"},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=15,
            )
            data = r.json()
            if r.status_code >= 400:
                return {"ok": False, "reason": data.get("error", {}).get("message", str(data))}
            return {"ok": True, "number": data.get("display_phone_number"),
                    "verified_name": data.get("verified_name"),
                    "quality_rating": data.get("quality_rating")}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

    def send(self, phone: str, message: str, language: str = "he",
             template_params: Optional[list] = None,
             template_name: Optional[str] = None) -> Tuple[str, Optional[str], str]:
        """
        Returns (status, external_id, link).
          status == "sent"          -> delivered via Cloud API (external_id set)
          status == "queued_manual" -> not auto-sent; use returned wa.me link
          status == "failed"        -> Cloud API attempt failed; link still usable

        Cold first-contact needs a template: if a template is configured AND
        template_params is given, we send the approved template (positional
        {{1}},{{2}} = template_params). Otherwise we send free-form text (valid
        only inside the 24h customer-service window, e.g. replying to a reply).
        """
        body = message + OPT_OUT.get(language, OPT_OUT["en"])
        link = wa_link(phone, body)

        if not self.auto_enabled:
            return "queued_manual", None, link

        to = re.sub(r"[^\d]", "", phone or "")
        tmpl = template_name if template_name is not None else TEMPLATE_NAME
        use_template = bool(tmpl and template_params is not None)
        if use_template:
            payload = {
                "messaging_product": "whatsapp", "to": to, "type": "template",
                "template": {
                    "name": tmpl,
                    "language": {"code": TEMPLATE_LANG.get(language, "en_US")},
                    "components": [{
                        "type": "body",
                        "parameters": [{"type": "text", "text": str(x)} for x in template_params],
                    }],
                },
            }
        else:
            payload = {"messaging_product": "whatsapp", "to": to,
                       "type": "text", "text": {"body": body}}

        try:
            resp = httpx.post(
                f"https://graph.facebook.com/{GRAPH_VERSION}/{self.phone_id}/messages",
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                json=payload, timeout=20,
            )
            data = resp.json()
            if resp.status_code >= 400:
                logger.error("WhatsApp Cloud API error to %s: %s", phone, data)
                return "failed", None, link
            msg_id = (data.get("messages") or [{}])[0].get("id")
            logger.info("WhatsApp sent to %s (%s): %s", phone,
                        "template" if use_template else "text", msg_id)
            return "sent", msg_id, link
        except Exception as exc:
            logger.error("WhatsApp send failed to %s: %s", phone, exc)
            return "failed", None, link
