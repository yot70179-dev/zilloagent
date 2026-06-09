# ZilloAgent — AI Real Estate Outreach Platform

Automated outreach to real estate listing agents, powered by Claude AI, Twilio, and SendGrid.

## Architecture

```
zilloagent/
├── backend/
│   ├── main.py            FastAPI app + all REST endpoints
│   ├── database.py        SQLAlchemy models (Property, Lead, Campaign, Message, …)
│   ├── zillow_client.py   Bridge Data Output (Zillow) API client
│   ├── ai_agent.py        Claude-powered message generation & lead scoring
│   ├── message_sender.py  Twilio SMS + SendGrid email (250/day hard limit)
│   ├── chat_handler.py    Routes replies to AI or human, triggers escalation
│   ├── tasks.py           Celery tasks: fetch listings, daily outreach, follow-ups
│   ├── Dockerfile
│   ├── requirements.txt
│   └── tests/
├── .env.example
├── docker-compose.yml
└── README.md
```

## Quick start (Docker)

```bash
# 1. Copy env file and fill in your API keys
cp .env.example .env

# 2. Start everything
docker compose up --build

# API is at http://localhost:8000
# Docs at  http://localhost:8000/docs
```

## Local development (no Docker)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp ../.env.example .env   # then fill in keys
uvicorn main:app --reload
```

Start the Celery worker (separate terminal):

```bash
celery -A tasks worker --loglevel=info
celery -A tasks beat --loglevel=info   # scheduler
```

## API keys needed

| Service | Where to get it |
|---------|----------------|
| `ZILLOW_API_KEY` | [bridgedataoutput.com](https://bridgedataoutput.com) |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `TWILIO_*` | [twilio.com/console](https://twilio.com/console) |
| `SENDGRID_API_KEY` | [app.sendgrid.com](https://app.sendgrid.com) |
| `REDIS_URL` | Local Redis or [upstash.com](https://upstash.com) |

## Key endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/stats/today` | Dashboard stats |
| GET/POST | `/campaigns` | List / create campaigns |
| POST | `/campaigns/{id}/run` | Trigger listing fetch + outreach |
| GET | `/leads` | Lead pipeline (filter by status, score) |
| GET | `/leads/{id}/conversation` | Full conversation history |
| POST | `/leads/{id}/takeover` | Human agent takes over a chat |
| POST | `/leads/{id}/message` | Human sends a message |
| POST | `/inbox/message` | Manually inject an inbound reply |
| POST | `/webhooks/twilio/sms` | Twilio SMS webhook |
| POST | `/webhooks/sendgrid/email` | SendGrid inbound email webhook |

Full interactive docs: `http://localhost:8000/docs`

## Running tests

```bash
cd backend
pytest tests/ -v
```

## Daily message limit

Hard-coded to 250/day via `DAILY_MESSAGE_LIMIT` env var. The limit is checked before every send; once reached all outreach stops until midnight UTC.

## Compliance

- Every outgoing SMS includes "Reply STOP to unsubscribe."
- Every outgoing email includes an unsubscribe footer.
- Opt-out keywords (`STOP`, `UNSUBSCRIBE`, `OPT OUT`) are detected automatically on inbound messages.
- Opted-out leads are never re-contacted.
- All messages are logged with timestamps in the `messages` table.
