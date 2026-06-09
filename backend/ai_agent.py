"""
Claude-powered AI agent: generates outreach messages, handles conversations,
scores leads, and decides when to hand off to a human.
"""
import json
import logging
import os
from typing import Any, Dict, List, Tuple

import anthropic

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a professional real estate outreach agent. Your goals:
1. Generate concise, non-spammy opening messages that reference specific property details.
2. Qualify leads through natural conversation — gather budget, timeline, preferred areas.
3. Score each lead 1-10 and decide when to escalate to a human agent.

Lead scoring rubric (total 10):
  - Budget match (does it fit buyer range)  : 0-3
  - Timeline urgency (sooner = higher)      : 0-3
  - Engagement / interest level             : 0-4

When score >= 7, set needs_handoff=true.

End EVERY conversational response with a JSON block on its own line:
{"score": <float>, "needs_handoff": <bool>, "reason": "<one sentence>"}
Do not write anything after that JSON line.\
"""

_OPENING_TEMPLATES = [
    (
        "Hi {name}, I noticed your property at {address} listed at ${price:,.0f}. "
        "We have qualified buyers actively looking in {area} this month. "
        "Would you be open to a quick conversation?"
    ),
    (
        "Hello {name}, I came across your listing at {address} (${price:,.0f}). "
        "I represent buyers searching specifically in your area right now. "
        "Would you have 5 minutes to connect?"
    ),
    (
        "Hi {name}, your property at {address} caught our attention. "
        "We're working with serious buyers in {area} with budgets around ${price:,.0f}. "
        "Are you open to exploring this further?"
    ),
]


class AIAgent:
    def __init__(self):
        self._client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        self._model = "claude-sonnet-4-20250514"
        self._template_idx = 0

    # ------------------------------------------------------------------
    # Outreach messages
    # ------------------------------------------------------------------

    def generate_opening_message(
        self,
        agent_name: str,
        address: str,
        price: float,
        city: str,
        description: str = "",
        style: str = "professional",
    ) -> str:
        template = _OPENING_TEMPLATES[self._template_idx % len(_OPENING_TEMPLATES)]
        self._template_idx += 1

        first_name = agent_name.split()[0] if agent_name else "there"
        base = template.format(name=first_name, address=address, price=price, area=city)

        if len(description) > 50:
            try:
                resp = self._client.messages.create(
                    model=self._model,
                    max_tokens=300,
                    system=(
                        f"You are a real estate outreach specialist. Enhance the message below "
                        f"with ONE specific detail from the property description. "
                        f"Keep the total under 3 sentences. Style: {style}."
                    ),
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                f"Base message: {base}\n\n"
                                f"Property description: {description[:500]}\n\n"
                                "Return only the enhanced message."
                            ),
                        }
                    ],
                )
                return resp.content[0].text.strip()
            except anthropic.APIError as exc:
                logger.error("Claude error (opening): %s", exc)

        return base

    def generate_follow_up_message(
        self, agent_name: str, address: str, price: float, city: str
    ) -> str:
        first_name = agent_name.split()[0] if agent_name else "there"
        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=200,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Write a brief, friendly follow-up (under 2 sentences) for a real estate "
                            f"agent named {first_name} about their property at {address} "
                            f"listed at ${price:,.0f} in {city}. "
                            f"This is a follow-up to a message sent 3 days ago with no reply. "
                            "Be non-pushy and professional. Return only the message text."
                        ),
                    }
                ],
            )
            return resp.content[0].text.strip()
        except anthropic.APIError as exc:
            logger.error("Claude error (follow-up): %s", exc)
            return (
                f"Hi {first_name}, just following up on my earlier note about {address}. "
                "Happy to connect whenever works for you!"
            )

    # ------------------------------------------------------------------
    # Conversation handling
    # ------------------------------------------------------------------

    def handle_conversation(
        self,
        conversation_history: List[Dict[str, str]],
        new_message: str,
        lead_context: Dict[str, Any] | None = None,
    ) -> Tuple[str, float, bool]:
        """
        Returns (response_text, lead_score, needs_handoff).
        """
        context_note = f"\nLead context: {json.dumps(lead_context, default=str)}" if lead_context else ""
        messages = [
            {"role": m["role"], "content": m["content"]}
            for m in conversation_history[-10:]
        ]
        messages.append({"role": "user", "content": new_message})

        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=500,
                system=_SYSTEM_PROMPT + context_note,
                messages=messages,
            )
            full = resp.content[0].text.strip()
            return self._parse_response(full)
        except anthropic.APIError as exc:
            logger.error("Claude error (conversation): %s", exc)
            fallback = (
                "Thank you for your response! Could you tell me more about "
                "your timeline and what you're looking for?"
            )
            return fallback, 0.0, False

    def _parse_response(self, full: str) -> Tuple[str, float, bool]:
        lines = full.splitlines()
        last = lines[-1].strip() if lines else ""
        if last.startswith("{"):
            try:
                data = json.loads(last)
                score = float(data.get("score", 0))
                handoff = bool(data.get("needs_handoff", False)) or score >= 7.0
                text = "\n".join(lines[:-1]).strip()
                return text, score, handoff
            except (json.JSONDecodeError, ValueError):
                pass
        return full, 0.0, False

    # ------------------------------------------------------------------
    # Lead qualification summary
    # ------------------------------------------------------------------

    def qualify_lead_summary(self, conversation_history: List[Dict[str, str]]) -> Dict[str, Any]:
        transcript = "\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in conversation_history
        )
        try:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=300,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Extract lead qualification data from this conversation as JSON. "
                            "Return ONLY the JSON, no prose.\n\n"
                            f"{transcript}\n\n"
                            'Schema: {"budget_min": null|number, "budget_max": null|number, '
                            '"timeline": null|string, "preferred_areas": null|string, '
                            '"score": number, "notes": string}'
                        ),
                    }
                ],
            )
            return json.loads(resp.content[0].text)
        except (anthropic.APIError, json.JSONDecodeError) as exc:
            logger.error("Claude error (summary): %s", exc)
            return {"score": 0, "notes": "Error generating summary"}
