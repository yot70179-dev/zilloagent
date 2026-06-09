"""
Realty US API client (via RapidAPI → realty-us.p.rapidapi.com).
Fetches active for-sale listings with agent contact info.
"""
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# LA ZIP codes covering major neighborhoods
LA_ZIP_CODES = [
    "90028",  # Hollywood
    "90210",  # Beverly Hills
    "90401",  # Santa Monica
    "90012",  # Downtown LA
    "90026",  # Silver Lake
    "90046",  # West Hollywood
    "90049",  # Brentwood
    "90024",  # Westwood / UCLA
    "90034",  # Culver City
    "90066",  # Mar Vista
]


class ZillowClient:
    """Fetches US real estate listings via RapidAPI."""

    BASE_URL = "https://realty-us.p.rapidapi.com"

    def __init__(self):
        self.api_key = os.getenv("RAPIDAPI_KEY", "")
        self.host = os.getenv("RAPIDAPI_HOST", "realty-us.p.rapidapi.com")
        self._http = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "x-rapidapi-key": self.api_key,
                "x-rapidapi-host": self.host,
            },
        )

    async def fetch_listings(
        self,
        city: Optional[str] = None,
        state: Optional[str] = None,
        zip_code: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        property_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Fetch active for-sale listings."""
        params: Dict[str, Any] = {
            "limit": min(limit, 42),
            "offset": offset,
            "status_type": "ForSale",
            "sort": "Newest",
        }

        if zip_code:
            params["postal_code"] = zip_code
        elif city:
            params["city"] = city
            params["state_code"] = state or "CA"

        if min_price:
            params["price_min"] = int(min_price)
        if max_price:
            params["price_max"] = int(max_price)

        try:
            resp = await self._http.get(
                f"{self.BASE_URL}/properties/v2/list-for-sale", params=params
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("properties", data.get("data", data.get("results", [])))
            return self._parse(raw)
        except httpx.HTTPStatusError as exc:
            logger.error("API %s — %s", exc.response.status_code, exc.response.text[:300])
            raise
        except httpx.RequestError as exc:
            logger.error("Request error: %s", exc)
            raise

    async def fetch_la_listings(self, total: int = 50) -> List[Dict[str, Any]]:
        """Fetch listings across multiple LA ZIP codes."""
        results: List[Dict[str, Any]] = []
        per_zip = max(5, total // len(LA_ZIP_CODES))

        for zip_code in LA_ZIP_CODES:
            if len(results) >= total:
                break
            try:
                batch = await self.fetch_listings(zip_code=zip_code, limit=per_zip)
                results.extend(batch)
                logger.info("ZIP %s → %d listings (total so far: %d)", zip_code, len(batch), len(results))
            except Exception as exc:
                logger.warning("Skipping ZIP %s: %s", zip_code, exc)

        return results[:total]

    def _parse(self, raw: List[Dict]) -> List[Dict[str, Any]]:
        results = []
        for item in raw:
            try:
                # Handle different response shapes
                loc = item.get("location", {})
                addr = loc.get("address", item)
                desc = item.get("description", item)
                agents = item.get("agents", desc.get("agents", []))
                agent = agents[0] if agents else {}

                results.append({
                    "zillow_id": str(item.get("property_id") or item.get("zpid") or item.get("id", "")),
                    "address": (
                        addr.get("line") or
                        f"{item.get('streetAddress','')} {item.get('city','')}".strip()
                    ),
                    "city": addr.get("city") or item.get("city", "Los Angeles"),
                    "state": addr.get("state_code") or item.get("state", "CA"),
                    "zip_code": addr.get("postal_code") or item.get("zipCode", ""),
                    "price": float(item.get("list_price") or item.get("price") or 0),
                    "bedrooms": int(desc.get("beds") or item.get("bedrooms") or 0),
                    "bathrooms": float(desc.get("baths_consolidated") or item.get("bathrooms") or 0),
                    "sqft": int(desc.get("sqft") or item.get("livingArea") or 0),
                    "property_type": desc.get("type") or item.get("homeType", ""),
                    "description": desc.get("text") or item.get("description", ""),
                    "listing_agent_name": agent.get("name") or item.get("agentName", ""),
                    "listing_agent_email": agent.get("email") or item.get("agentEmail", ""),
                    "listing_agent_phone": agent.get("phone") or item.get("agentPhone", ""),
                    "listing_date": _parse_iso(item.get("list_date") or item.get("listingDate")),
                })
            except Exception as exc:
                logger.warning("Skipping malformed listing: %s", exc)
        return results

    async def close(self):
        await self._http.aclose()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
