"""Unit tests for AIAgent — mocks the Anthropic client."""
from unittest.mock import MagicMock, patch

import pytest

from ai_agent import AIAgent


@pytest.fixture
def agent():
    with patch("ai_agent.anthropic.Anthropic"):
        return AIAgent()


def test_generate_opening_no_description(agent):
    msg = agent.generate_opening_message(
        agent_name="John Smith",
        address="123 Main St",
        price=450000,
        city="Austin",
        description="",
    )
    assert "John" in msg
    assert "123 Main St" in msg
    assert "450,000" in msg


def test_opening_templates_rotate(agent):
    msgs = {
        agent.generate_opening_message("Jane", f"{i} Oak Ave", 300000, "Dallas")
        for i in range(3)
    }
    assert len(msgs) == 3, "Templates should rotate"


def test_parse_response_valid_json(agent):
    raw = 'Thank you for reaching out!\n{"score": 8.5, "needs_handoff": true, "reason": "high budget"}'
    text, score, handoff = agent._parse_response(raw)
    assert score == 8.5
    assert handoff is True
    assert "Thank you" in text


def test_parse_response_no_json(agent):
    raw = "Sounds great, let me know more."
    text, score, handoff = agent._parse_response(raw)
    assert text == raw
    assert score == 0.0
    assert handoff is False


def test_handle_conversation_fallback_on_api_error(agent):
    agent._client.messages.create.side_effect = Exception("API down")
    text, score, handoff = agent.handle_conversation([], "hello")
    assert isinstance(text, str)
    assert score == 0.0
    assert handoff is False
