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

## Marketing agent — landing-page outreach

An add-on that pitches your landing-page service to business owners over WhatsApp,
email and LinkedIn, then rolls everything up at month end.

**What it does**
- Keeps a pool of prospects (Israeli + American business owners).
- Every morning builds a batch of **10 WhatsApp messages to Israeli owners + 5 to
  American owners**, each personalised by Claude (Hebrew for IL, English for US).
- Sends one message per hour during business hours (auto mode).
- When a prospect replies, **emails you the reply immediately**.
- **Sources new numbers automatically every day** via the official Google Places
  API — the phone a business *published* for contact (for a solo business, usually
  the owner's own mobile). Configure with `GOOGLE_PLACES_API_KEY` +
  `MK_SOURCE_QUERIES`. Also auto-imports from `prospects_inbox.csv`
  (see `prospects_inbox.sample.csv`).
  > We never scrape *private* personal numbers from social networks or data
  > brokers — that's illegal and gets a WhatsApp number banned.
- Emails you a full activity summary on the 1st of each month.

**Two delivery modes**
- **Manual (default, zero-risk):** no WhatsApp API configured → the agent builds a
  ready-to-send `wa.me` link per message and emails you the daily batch. You tap and
  send. Your personal WhatsApp is never automated, so it can't be banned.
- **Auto:** set `WHATSAPP_TOKEN` + `WHATSAPP_PHONE_ID` for the **official Meta
  WhatsApp Cloud API** → the agent sends by itself, one per hour, with an opt-out
  footer on every message.

> ⚠️ Automating a *personal* WhatsApp/LinkedIn account for cold bulk messaging gets
> the account banned and breaks Israel's Amendment 40 and the US TCPA. That path is
> intentionally not built. Use manual mode, or the official Cloud API with opt-in
> templates. Every message carries an opt-out line; opted-out prospects are never
> re-contacted.

**Endpoints**

| Method | Path | Description |
|--------|------|-------------|
| POST | `/marketing/source/run` | Pull new business numbers now from Google Places |
| POST | `/marketing/prospects` | Add a prospect |
| GET | `/marketing/prospects` | List prospects (filter by `segment`, `status`) |
| POST | `/marketing/batch/run` | Build today's WhatsApp batch now (guard with `ADMIN_TOKEN`) |
| GET | `/marketing/batch/today` | Today's messages + click-to-send links |
| GET | `/marketing/report/{year}/{month}` | Generate/return a monthly summary |
| GET/POST | `/webhooks/whatsapp` | Meta Cloud API verification + inbound replies |

Configure via the `MK_*` and `WHATSAPP_*` variables in `.env.example`.
To turn on auto-send from your own number, follow **`SETUP_WHATSAPP.md`**.

## Compliance

- Every outgoing SMS includes "Reply STOP to unsubscribe."
- Every outgoing email includes an unsubscribe footer.
- Opt-out keywords (`STOP`, `UNSUBSCRIBE`, `OPT OUT`) are detected automatically on inbound messages.
- Opted-out leads are never re-contacted.
- All messages are logged with timestamps in the `messages` table.
