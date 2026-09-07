"""
Automatic prospect sourcing — pulls NEW business numbers every day.

Legal, defensible source only: the official **Google Places API**, which returns
the phone number a business *published itself* for customers to contact it. For a
small / solo business that listed number is typically the owner's own mobile — a
number they made public specifically to be reached on. We do NOT scrape private
personal numbers from social networks or data brokers (illegal, and exactly what
gets a WhatsApp number banned under Israel's Amendment 40).

Requires GOOGLE_PLACES_API_KEY. Configure what to pull with MK_SOURCE_QUERIES,
a ';'-separated list of "segment|text query", e.g.:

    IL|מספרות בתל אביב;IL|מוסכים בחיפה;US|plumbers in Austin TX

Each daily run imports up to MK_SOURCE_DAILY_MAX new prospects into the pool;
the marketing agent then messages them within the 10 IL + 5 US caps.
"""
import logging
import os
from typing import List

import httpx
from sqlalchemy.orm import Session

from database import Prospect

logger = logging.getLogger(__name__)

PLACES_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
DAILY_MAX  = int(os.getenv("MK_SOURCE_DAILY_MAX", "40"))
QUERIES    = os.getenv("MK_SOURCE_QUERIES", "")

_TEXTSEARCH = "https://maps.googleapis.com/maps/api/place/textsearch/json"
_DETAILS    = "https://maps.googleapis.com/maps/api/place/details/json"


def _parse_queries() -> List[tuple]:
    out = []
    for chunk in QUERIES.split(";"):
        chunk = chunk.strip()
        if not chunk or "|" not in chunk:
            continue
        seg, q = chunk.split("|", 1)
        out.append(("US" if seg.strip().upper() == "US" else "IL", q.strip()))
    return out


def _place_phone_and_name(place_id: str) -> tuple:
    """Fetch the phone the business published + its name (one Details call)."""
    try:
        r = httpx.get(_DETAILS, params={
            "place_id": place_id,
            "fields": "name,formatted_phone_number,international_phone_number",
            "key": PLACES_KEY,
        }, timeout=15)
        d = (r.json() or {}).get("result", {})
        phone = d.get("international_phone_number") or d.get("formatted_phone_number") or ""
        return d.get("name", ""), phone
    except Exception as exc:
        logger.error("Places details failed for %s: %s", place_id, exc)
        return "", ""


def fetch_from_google_places(segment: str, query: str, limit: int) -> List[dict]:
    """Return [{business_name, phone, source}] for a text query."""
    if not PLACES_KEY:
        logger.warning("GOOGLE_PLACES_API_KEY not set — auto-sourcing disabled.")
        return []
    results = []
    try:
        r = httpx.get(_TEXTSEARCH, params={"query": query, "key": PLACES_KEY}, timeout=20)
        for place in (r.json() or {}).get("results", [])[:limit]:
            name, phone = _place_phone_and_name(place.get("place_id", ""))
            if not phone:
                continue
            results.append({"business_name": name or place.get("name", ""),
                            "phone": phone, "source": f"google_places:{query}"})
    except Exception as exc:
        logger.error("Places textsearch failed (%s): %s", query, exc)
    return results


def auto_source_prospects(db: Session, daily_max: int = DAILY_MAX) -> int:
    """Pull today's new business numbers across all configured queries.
    Skips numbers already in the pool (add_prospect de-dupes)."""
    from marketing_agent import add_prospect  # avoid circular import at module load

    queries = _parse_queries()
    if not queries:
        logger.info("Auto-sourcing: no MK_SOURCE_QUERIES configured.")
        return 0

    added = 0
    per_query = max(1, daily_max // max(1, len(queries)))
    for segment, query in queries:
        if added >= daily_max:
            break
        for item in fetch_from_google_places(segment, query, per_query):
            if added >= daily_max:
                break
            before = db.query(Prospect).count()
            add_prospect(db, segment=segment, business_name=item["business_name"],
                         phone=item["phone"], source=item["source"])
            if db.query(Prospect).count() > before:
                added += 1
    logger.info("Auto-sourcing added %d new prospects", added)
    return added
