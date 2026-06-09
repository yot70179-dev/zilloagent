"""
Handles inbound messages: routes to AI or human agent,
updates lead score, triggers handoff when score >= 7.
"""
import logging
from datetime import date, datetime
from typing import Any, Dict

from sqlalchemy.orm import Session

from ai_agent import AIAgent
from database import Conversation, DailyStats, Lead, LeadStatus, Message, MessageStatus
from message_sender import MessageSender

logger = logging.getLogger(__name__)

_OPT_OUT_KEYWORDS = {"stop", "unsubscribe", "remove me", "opt out", "opt-out", "don't contact"}


class ChatHandler:
    def __init__(self, db: Session):
        self.db = db
        self.ai = AIAgent()
        self.sender = MessageSender(db)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def handle_incoming_message(
        self, lead_id: int, content: str, channel: str = "sms"
    ) -> Dict[str, Any]:
        """
        Process a reply from a lead. Returns a dict with response details.
        channel: "sms" | "email"
        """
        lead = self._get_lead(lead_id)

        if self._is_opt_out(content):
            self.sender.handle_opt_out(lead)
            return {
                "response": "You've been unsubscribed. We won't contact you again.",
                "lead_status": LeadStatus.OPTED_OUT,
                "score": 0.0,
                "needs_handoff": False,
            }

        self._record_inbound(lead, content, channel)

        if lead.status == LeadStatus.CONTACTED:
            lead.status = LeadStatus.RESPONDED
            self.db.commit()

        if lead.human_takeover:
            return {
                "response": None,
                "lead_status": lead.status,
                "score": lead.score,
                "needs_handoff": False,
                "human_takeover": True,
            }

        history = self._get_history(lead_id)
        lead_ctx = {
            "budget_min": lead.budget_min,
            "budget_max": lead.budget_max,
            "timeline": lead.timeline,
            "preferred_areas": lead.preferred_areas,
            "current_score": lead.score,
        }
        response_text, new_score, needs_handoff = self.ai.handle_conversation(
            history, content, lead_ctx
        )

        self._update_score(lead, new_score)

        if needs_handoff or lead.score >= 7.0:
            self._escalate(lead)

        self._record_outbound(lead, response_text, channel)
        self._send_response(lead, response_text, channel)
        self._increment_replies()

        lead.updated_at = datetime.utcnow()
        self.db.commit()

        return {
            "response": response_text,
            "lead_status": lead.status,
            "score": lead.score,
            "needs_handoff": lead.human_takeover,
        }

    def take_human_control(self, lead_id: int, agent_name: str) -> bool:
        lead = self.db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            return False
        lead.human_takeover = True
        lead.assigned_agent = agent_name
        lead.status = LeadStatus.HANDED_OFF
        lead.updated_at = datetime.utcnow()
        self.db.commit()
        logger.info("Human takeover lead=%s agent=%s", lead_id, agent_name)
        return True

    def send_human_message(self, lead_id: int, content: str, channel: str = "sms") -> bool:
        lead = self.db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            return False
        try:
            self._send_response(lead, content, channel)
            self._record_outbound(lead, f"[HUMAN] {content}", channel)
            self.db.commit()
            return True
        except Exception as exc:
            logger.error("Human message failed lead=%s: %s", lead_id, exc)
            return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_lead(self, lead_id: int) -> Lead:
        lead = self.db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            raise ValueError(f"Lead {lead_id} not found")
        return lead

    def _is_opt_out(self, text: str) -> bool:
        lower = text.lower()
        return any(kw in lower for kw in _OPT_OUT_KEYWORDS)

    def _record_inbound(self, lead: Lead, content: str, channel: str):
        self.db.add(Message(
            lead_id=lead.id, message_type=channel, direction="inbound",
            content=content, status=MessageStatus.SENT, sent_at=datetime.utcnow(),
        ))
        self.db.add(Conversation(lead_id=lead.id, role="user", content=content))
        self.db.commit()

    def _record_outbound(self, lead: Lead, content: str, channel: str):
        self.db.add(Conversation(lead_id=lead.id, role="assistant", content=content))
        self.db.commit()

    def _get_history(self, lead_id: int):
        rows = (
            self.db.query(Conversation)
            .filter(Conversation.lead_id == lead_id)
            .order_by(Conversation.created_at)
            .all()
        )
        return [{"role": r.role, "content": r.content} for r in rows]

    def _update_score(self, lead: Lead, new_score: float):
        if new_score <= 0:
            return
        lead.score = new_score if lead.score == 0 else lead.score * 0.6 + new_score * 0.4
        self.db.commit()

    def _escalate(self, lead: Lead):
        lead.status = LeadStatus.QUALIFIED
        lead.human_takeover = True
        self.db.commit()
        logger.info("HOT LEAD lead=%s score=%.1f — escalating to human", lead.id, lead.score)
        self._increment_handoffs()

    def _send_response(self, lead: Lead, content: str, channel: str):
        try:
            if channel == "email" and lead.contact_email:
                self.sender.send_email(lead, content)
            elif channel == "sms" and lead.contact_phone:
                self.sender.send_sms(lead, content)
        except Exception as exc:
            logger.error("Could not send response lead=%s: %s", lead.id, exc)

    def _today_stats(self) -> DailyStats:
        today_start = datetime.combine(date.today(), datetime.min.time())
        stats = self.db.query(DailyStats).filter(DailyStats.date >= today_start).first()
        if not stats:
            stats = DailyStats(date=today_start, messages_sent=0, replies_received=0,
                               leads_qualified=0, leads_handed_off=0)
            self.db.add(stats)
            self.db.commit()
        return stats

    def _increment_replies(self):
        s = self._today_stats()
        s.replies_received += 1
        self.db.commit()

    def _increment_handoffs(self):
        s = self._today_stats()
        s.leads_handed_off += 1
        self.db.commit()
