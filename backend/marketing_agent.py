"""
Marketing agent — sells landing pages to business owners.

Channels: WhatsApp (10/day Israel + 5/day US, spaced ~1/hour), email, LinkedIn
(draft + profile link), and optional AI calls. Bilingual: Hebrew for the IL
segment, English for the US segment. Uses Claude to personalise each pitch,
with a template fallback when no API key is set.

Design principles (match the platform's existing compliance):
  • every message carries an opt-out line; opted-out prospects are never re-contacted
  • hard daily caps per segment, one message per hour
  • WhatsApp/LinkedIn default to manual (wa.me link / draft) unless an official
    API is configured — no personal-account automation
"""
import csv
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
import pytz
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import (
    Agent, MonthlyReport, Prospect, ProspectMessage, ProspectStatus, SessionLocal,
)
from message_sender import MessageSender
from whatsapp_sender import (
    TEMPLATE_NAME, WhatsAppSender, normalize_phone, render_template, wa_link,
)

logger = logging.getLogger(__name__)

# ── Config (env-overridable) ───────────────────────────────────────────────────
IL_DAILY_CAP = int(os.getenv("MK_IL_DAILY_CAP", "10"))
US_DAILY_CAP = int(os.getenv("MK_US_DAILY_CAP", "5"))
IL_TZ = pytz.timezone(os.getenv("MK_IL_TZ", "Asia/Jerusalem"))
US_TZ = pytz.timezone(os.getenv("MK_US_TZ", "America/New_York"))
BUSINESS_HOURS = (9, 21)  # inclusive-exclusive, local time
INBOX_CSV = os.getenv("MK_INBOX_CSV", str(Path(__file__).parent / "prospects_inbox.csv"))
REPORT_EMAIL = os.getenv("MK_REPORT_EMAIL", os.getenv("GMAIL_USER", ""))

_ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
_MODEL = os.getenv("MK_MODEL", "claude-sonnet-4-20250514")


# ══════════════════════════════════════════════════════════════════════════════
#  Message generation
# ══════════════════════════════════════════════════════════════════════════════

SENDER_NAME    = os.getenv("MK_SENDER_NAME", "יותם")
SENDER_NAME_EN = os.getenv("MK_SENDER_NAME_EN", "Yotam")


def _fallback_message(p: Prospect) -> str:
    name = (p.name or "").split()[0] if p.name else ""
    biz  = p.business_name or ""
    if p.segment == "US":
        hi = f"Hi {name}, " if name else "Hi, "
        forwho = f" for {biz}" if biz else ""
        return (
            f"{hi}I'm {SENDER_NAME_EN}, a landing-page designer. I built a sample landing "
            f"page{forwho} just to show how it could look — it's only an initial sample. "
            f"If you'd like, I'll happily send it over, and we can take it to something "
            f"custom and real. Thanks for your time!"
        )
    # Hebrew (IL)
    hi = f"היי {name}, " if name else "היי, "
    forwho = f" ל{biz}" if biz else ""
    return (
        f"{hi}שמי {SENDER_NAME}, מעצב דפי נחיתה. בניתי דוגמה של דף נחיתה{forwho} רק כדי "
        f"להראות איך זה יכול להיראות — חשוב לי שתדע/י שזו דוגמה ראשונית בלבד. "
        f"אם תרצה/י אשמח לשלוח לך אותה, ונוכל להתקדם למשהו מותאם ואמיתי. תודה על זמנך!"
    )


def generate_pitch(p: Prospect, channel: str = "whatsapp") -> str:
    """Personalised landing-page pitch. Falls back to a template without an API key."""
    base = _fallback_message(p)
    if not _ANTHROPIC_KEY:
        return base

    lang = "Hebrew" if p.segment == "IL" else "English"
    sender = SENDER_NAME if p.segment == "IL" else SENDER_NAME_EN
    length = "under 45 words" if channel == "whatsapp" else "under 90 words"
    facts = ", ".join(f for f in [
        f"name={p.name}" if p.name else "",
        f"business={p.business_name}" if p.business_name else "",
        f"industry={p.industry}" if p.industry else "",
    ] if f) or "no extra details"
    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": _ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _MODEL,
                "max_tokens": 300,
                "system": (
                    f"You are {sender}, a friendly, humble beginner landing-page designer "
                    f"writing a first-contact {channel} message in {lang} to a small-business owner. "
                    f"Voice: you introduce yourself by name, say you built a SAMPLE landing page just "
                    f"to show how it could look, stress it's only an initial sample, and offer to send "
                    f"it and take it further into something custom and real if they want. End warmly "
                    f"(e.g. 'thanks for your time'). "
                    f"Rules: {length}; personalise with the name/business if given; genuine and "
                    f"non-pushy; no hype, no emoji spam; no opt-out line (added separately). "
                    f"Return ONLY the message text."
                ),
                "messages": [{"role": "user", "content": f"Prospect: {facts}\n\nReference draft: {base}"}],
            },
            timeout=25,
        )
        data = resp.json()
        if resp.status_code < 400 and data.get("content"):
            return data["content"][0]["text"].strip()
        logger.warning("Claude pitch error: %s", data)
    except Exception as exc:
        logger.error("Claude pitch failed: %s", exc)
    return base


def _email_subject(p: Prospect) -> str:
    biz = p.business_name or (p.name or "your business")
    return f"רעיון לדף נחיתה ל{biz}" if p.segment == "IL" else f"A landing page idea for {biz}"


# ══════════════════════════════════════════════════════════════════════════════
#  Opt-out
# ══════════════════════════════════════════════════════════════════════════════

_OPT_OUT_WORDS = {"stop", "unsubscribe", "opt out", "optout", "הסר", "הסרה", "להסיר", "תפסיק"}

def is_opt_out(text: str) -> bool:
    t = (text or "").strip().lower()
    return any(w in t for w in _OPT_OUT_WORDS)


def mark_opted_out(db: Session, prospect: Prospect):
    prospect.opted_out = True
    prospect.status = ProspectStatus.OPTED_OUT
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  Inbound replies → alert the user immediately
# ══════════════════════════════════════════════════════════════════════════════

def find_prospect_by_phone(db: Session, phone: str) -> Optional[Prospect]:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if not digits:
        return None
    return (db.query(Prospect)
            .filter(func.replace(func.replace(func.replace(
                Prospect.phone, "+", ""), "-", ""), " ", "").like(f"%{digits[-9:]}%"))
            .first())


def handle_inbound_reply(db: Session, phone: str, text: str,
                         channel: str = "whatsapp") -> Optional[dict]:
    """Log an inbound reply, honour opt-out, and alert the user right away."""
    prospect = find_prospect_by_phone(db, phone)
    if not prospect:
        # unknown sender — still record it so nothing is lost
        prospect = add_prospect(db, segment="IL", phone=phone, source="inbound")

    db.add(ProspectMessage(
        prospect_id=prospect.id, channel=channel, direction="inbound",
        content=text or "", mode="auto", status="sent",
        sent_at=datetime.utcnow(),
    ))

    if is_opt_out(text):
        mark_opted_out(db, prospect)
        db.commit()
        return {"prospect_id": prospect.id, "opted_out": True}

    prospect.status = ProspectStatus.REPLIED
    db.commit()
    _alert_reply(prospect, text, channel)
    return {"prospect_id": prospect.id, "replied": True}


def _alert_reply(prospect: Prospect, text: str, channel: str):
    """Email the user immediately when a prospect answers."""
    if not REPORT_EMAIL:
        return
    who = " / ".join(x for x in [prospect.name, prospect.business_name] if x) or prospect.phone
    body = (
        f"🔔 תגובה חדשה מליד!\n\n"
        f"מ:      {who}\n"
        f"טלפון:  {prospect.phone or '—'}\n"
        f"ערוץ:   {channel}\n"
        f"מקטע:   {prospect.segment}\n\n"
        f"ההודעה:\n\"{text}\"\n\n"
        f"קישור לשיחה: {wa_link(prospect.phone or '', '')}\n"
    )
    try:
        MessageSender().send_email(
            REPORT_EMAIL, "You",
            f"🔔 תגובה מ-{who}", body)
        logger.info("Reply alert emailed for prospect %s", prospect.id)
    except Exception as exc:
        logger.error("Reply alert failed: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
#  Prospect pool
# ══════════════════════════════════════════════════════════════════════════════

def add_prospect(db: Session, *, segment: str, name: str = "", business_name: str = "",
                 phone: str = "", email: str = "", linkedin_url: str = "",
                 industry: str = "", source: str = "manual", agent_id: Optional[int] = None) -> Prospect:
    segment = "US" if str(segment).upper() == "US" else "IL"
    country = "1" if segment == "US" else "972"
    phone = normalize_phone(phone, country) if phone else ""

    # de-dupe on phone or email within segment
    if phone or email:
        q = db.query(Prospect).filter(Prospect.segment == segment)
        existing = q.filter(
            (Prospect.phone == phone) if phone else (Prospect.email == email)
        ).first()
        if existing:
            return existing

    p = Prospect(
        agent_id=agent_id, segment=segment,
        language="en" if segment == "US" else "he",
        name=name or None, business_name=business_name or None,
        industry=industry or None, phone=phone or None, email=email or None,
        linkedin_url=linkedin_url or None, source=source,
        status=ProspectStatus.NEW,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def import_inbox_csv(db: Session, path: str = INBOX_CSV) -> int:
    """Optional: import new prospects the user drops into prospects_inbox.csv.

    Columns (header row): segment,name,business_name,phone,email,linkedin_url,industry,source
    """
    if not os.path.exists(path):
        return 0
    added = 0
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if not (row.get("phone") or row.get("email") or row.get("linkedin_url")):
                    continue
                before = db.query(Prospect).count()
                add_prospect(
                    db,
                    segment=row.get("segment", "IL"),
                    name=row.get("name", ""),
                    business_name=row.get("business_name", ""),
                    phone=row.get("phone", ""),
                    email=row.get("email", ""),
                    linkedin_url=row.get("linkedin_url", ""),
                    industry=row.get("industry", ""),
                    source=row.get("source", "csv"),
                )
                if db.query(Prospect).count() > before:
                    added += 1
    except Exception as exc:
        logger.error("import_inbox_csv failed: %s", exc)
    logger.info("Imported %d new prospects from %s", added, path)
    return added


def _sent_today(db: Session, segment: str, channel: str = "whatsapp") -> int:
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(func.count(ProspectMessage.id))
        .join(Prospect, ProspectMessage.prospect_id == Prospect.id)
        .filter(Prospect.segment == segment,
                ProspectMessage.channel == channel,
                ProspectMessage.direction == "outbound",
                ProspectMessage.status.in_(["sent", "queued_manual"]),
                ProspectMessage.created_at >= start)
        .scalar() or 0
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Daily plan + hourly send
# ══════════════════════════════════════════════════════════════════════════════

def build_daily_batch(db: Session) -> dict:
    """Pick today's prospects (10 IL + 5 US), generate their WhatsApp pitch,
    store them as pending ProspectMessages. In manual mode also emails the user
    the batch with click-to-send links. Idempotent per day."""
    # pull new business numbers automatically (Google Places), then the CSV inbox
    try:
        from sources import auto_source_prospects
        auto_source_prospects(db)
    except Exception as exc:
        logger.error("auto_source_prospects failed: %s", exc)
    import_inbox_csv(db)
    wa = WhatsAppSender()
    result = {"IL": 0, "US": 0, "mode": "auto" if wa.auto_enabled else "manual"}

    for segment, cap in (("IL", IL_DAILY_CAP), ("US", US_DAILY_CAP)):
        already = _sent_today(db, segment) + _pending_today(db, segment)
        remaining = max(0, cap - already)
        if remaining == 0:
            continue
        prospects = (
            db.query(Prospect)
            .filter(Prospect.segment == segment,
                    Prospect.status == ProspectStatus.NEW,
                    Prospect.opted_out == False,          # noqa: E712
                    Prospect.phone.isnot(None))
            .order_by(Prospect.created_at.asc())
            .limit(remaining).all()
        )
        for p in prospects:
            first = (p.name or "").split()[0] if p.name else ""
            biz = p.business_name or ""
            if wa.auto_enabled:
                # Cloud API cold contact must use the approved template; store the
                # rendered template text (what the recipient will see) for preview.
                content = (render_template(p.language, first, biz) if TEMPLATE_NAME
                           else generate_pitch(p, channel="whatsapp"))
                link = wa_link(p.phone, content)
                db.add(ProspectMessage(
                    prospect_id=p.id, channel="whatsapp", direction="outbound",
                    content=content, wa_link=link, mode="auto", status="pending",
                ))
                p.status = ProspectStatus.QUEUED
            else:
                # manual mode: handed to the user (batch email + /batch/today), so
                # it counts as an outbound touch right away
                pitch = generate_pitch(p, channel="whatsapp")
                db.add(ProspectMessage(
                    prospect_id=p.id, channel="whatsapp", direction="outbound",
                    content=pitch, wa_link=wa_link(p.phone, pitch),
                    mode="manual", status="queued_manual",
                ))
                p.status = ProspectStatus.CONTACTED
                p.last_contacted_at = datetime.utcnow()
            result[segment] += 1
        db.commit()

    if result["mode"] == "manual" and (result["IL"] or result["US"]) and REPORT_EMAIL:
        _email_manual_batch(db, REPORT_EMAIL)
    return result


def _pending_today(db: Session, segment: str) -> int:
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(func.count(ProspectMessage.id))
        .join(Prospect, ProspectMessage.prospect_id == Prospect.id)
        .filter(Prospect.segment == segment,
                ProspectMessage.channel == "whatsapp",
                ProspectMessage.status == "pending",
                ProspectMessage.created_at >= start)
        .scalar() or 0
    )


def _in_business_hours(segment: str) -> bool:
    tz = IL_TZ if segment == "IL" else US_TZ
    hour = datetime.now(tz).hour
    return BUSINESS_HOURS[0] <= hour < BUSINESS_HOURS[1]


def send_next_whatsapp(db: Session) -> Optional[dict]:
    """Send ONE pending WhatsApp message (≈ one per hourly tick). IL prioritised.
    Only auto-sends when the official Cloud API is configured; otherwise the
    morning batch email already carries the links, so this is a no-op."""
    wa = WhatsAppSender()
    if not wa.auto_enabled:
        return None  # manual mode: nothing to auto-send

    for segment in ("IL", "US"):
        if not _in_business_hours(segment):
            continue
        cap = IL_DAILY_CAP if segment == "IL" else US_DAILY_CAP
        if _sent_today(db, segment) >= cap:
            continue
        msg = (
            db.query(ProspectMessage)
            .join(Prospect, ProspectMessage.prospect_id == Prospect.id)
            .filter(Prospect.segment == segment,
                    ProspectMessage.channel == "whatsapp",
                    ProspectMessage.status == "pending",
                    Prospect.opted_out == False)          # noqa: E712
            .order_by(ProspectMessage.created_at.asc())
            .first()
        )
        if not msg:
            continue
        p = msg.prospect
        status, ext_id, link = wa.send(p.phone, msg.content, p.language)
        msg.status = status
        msg.external_id = ext_id
        msg.wa_link = link
        if status == "sent":
            msg.sent_at = datetime.utcnow()
            p.status = ProspectStatus.CONTACTED
            p.last_contacted_at = datetime.utcnow()
        db.commit()
        return {"prospect_id": p.id, "segment": segment, "status": status}
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  Email + LinkedIn channels
# ══════════════════════════════════════════════════════════════════════════════

def send_email_to_prospect(db: Session, prospect: Prospect, agent: Optional[Agent] = None) -> bool:
    if prospect.opted_out or not prospect.email:
        return False
    body = generate_pitch(prospect, channel="email")
    sender = MessageSender(agent)
    ok = sender.send_email(prospect.email, prospect.name or "there",
                           _email_subject(prospect), body)
    db.add(ProspectMessage(
        prospect_id=prospect.id, channel="email", direction="outbound",
        content=body, subject=_email_subject(prospect),
        mode="auto", status="sent" if ok else "failed",
        sent_at=datetime.utcnow() if ok else None,
    ))
    if ok:
        prospect.last_contacted_at = datetime.utcnow()
    db.commit()
    return ok


def prepare_linkedin_message(db: Session, prospect: Prospect) -> dict:
    """LinkedIn has no compliant cold-DM API, so we generate a draft + profile
    link for the user to send by hand. Logged as a manual touch."""
    body = generate_pitch(prospect, channel="linkedin")
    db.add(ProspectMessage(
        prospect_id=prospect.id, channel="linkedin", direction="outbound",
        content=body, mode="manual", status="queued_manual",
    ))
    db.commit()
    return {"message": body, "profile_url": prospect.linkedin_url}


# ══════════════════════════════════════════════════════════════════════════════
#  Manual-batch email + monthly report
# ══════════════════════════════════════════════════════════════════════════════

def _pending_batch(db: Session):
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(ProspectMessage)
        .filter(ProspectMessage.channel == "whatsapp",
                ProspectMessage.status.in_(["pending", "queued_manual"]),
                ProspectMessage.created_at >= start)
        .all()
    )


def _email_manual_batch(db: Session, to_email: str) -> bool:
    rows = _pending_batch(db)
    if not rows:
        return False
    lines = ["הודעות ה-WhatsApp להיום — לחצ/י על הלינק לשליחה:\n"]
    for i, m in enumerate(rows, 1):
        p = m.prospect
        who = " / ".join(x for x in [p.name, p.business_name] if x) or p.phone
        lines.append(f"{i}. [{p.segment}] {who}\n   {m.wa_link}\n")
    body = "\n".join(lines)
    sender = MessageSender()
    return sender.send_email(to_email, "You", f"WhatsApp batch — {datetime.utcnow():%Y-%m-%d}", body)


def generate_monthly_report(db: Session, year: int, month: int,
                            agent_id: Optional[int] = None) -> MonthlyReport:
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)

    def _count(channel, segment=None, direction="outbound", statuses=("sent", "queued_manual")):
        q = (db.query(func.count(ProspectMessage.id))
             .join(Prospect, ProspectMessage.prospect_id == Prospect.id)
             .filter(ProspectMessage.channel == channel,
                     ProspectMessage.direction == direction,
                     ProspectMessage.status.in_(list(statuses)),
                     ProspectMessage.created_at >= start,
                     ProspectMessage.created_at < end))
        if segment:
            q = q.filter(Prospect.segment == segment)
        return q.scalar() or 0

    added = (db.query(func.count(Prospect.id))
             .filter(Prospect.created_at >= start, Prospect.created_at < end).scalar() or 0)
    replies = (db.query(func.count(ProspectMessage.id))
               .filter(ProspectMessage.direction == "inbound",
                       ProspectMessage.created_at >= start,
                       ProspectMessage.created_at < end).scalar() or 0)
    opt_outs = (db.query(func.count(Prospect.id))
                .filter(Prospect.opted_out == True,                       # noqa: E712
                        Prospect.created_at >= start, Prospect.created_at < end).scalar() or 0)

    rpt = MonthlyReport(
        agent_id=agent_id, year=year, month=month,
        prospects_added=added,
        whatsapp_sent_il=_count("whatsapp", "IL"),
        whatsapp_sent_us=_count("whatsapp", "US"),
        emails_sent=_count("email"),
        linkedin_sent=_count("linkedin"),
        calls_made=_count("call"),
        replies=replies, opt_outs=opt_outs,
    )
    rpt.summary_text = _report_summary_text(rpt)
    db.add(rpt)
    db.commit()
    db.refresh(rpt)

    if REPORT_EMAIL:
        MessageSender().send_email(
            REPORT_EMAIL, "You",
            f"סיכום חודשי — {month:02d}/{year}", rpt.summary_text)
    return rpt


def _report_summary_text(r: MonthlyReport) -> str:
    total_wa = r.whatsapp_sent_il + r.whatsapp_sent_us
    return (
        f"סיכום פעילות שיווק — {r.month:02d}/{r.year}\n"
        f"{'='*40}\n"
        f"לידים חדשים שנוספו:      {r.prospects_added}\n"
        f"WhatsApp (ישראל):        {r.whatsapp_sent_il}\n"
        f"WhatsApp (ארה\"ב):        {r.whatsapp_sent_us}\n"
        f"WhatsApp סה\"כ:            {total_wa}\n"
        f"מיילים:                   {r.emails_sent}\n"
        f"LinkedIn:                {r.linkedin_sent}\n"
        f"שיחות:                    {r.calls_made}\n"
        f"תגובות שהתקבלו:           {r.replies}\n"
        f"הסרות מרשימה:             {r.opt_outs}\n"
    )
