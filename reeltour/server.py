"""
ReelTour backend — turn a real-estate listing link into a continuous cinematic tour video.

Pipeline (proven end-to-end during development):
  listing link ->  pull photos  ->  UPSCALE to >=1024px (critical!)  ->
  import to Higgsfield -> generate continuous start->end transitions -> return clip URLs

Why the upscale step matters: listing APIs hand back tiny 120x80 thumbnails, and the
video model rejects them *silently* (no charge, just "failed"). Forcing every image to
a large rdcpix render (1024x575) is what made generation succeed.

Run locally:   uvicorn server:app --reload --port 8000
Deploy:        see RUNBOOK.md (Render one-click)
"""

import os
import re
import time
import uuid
import threading
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ----------------------------------------------------------------------------
# Config / credentials (from environment — never hard-code)
# ----------------------------------------------------------------------------
HF_KEY_ID      = os.environ.get("HF_KEY_ID", "")
HF_KEY_SECRET  = os.environ.get("HF_KEY_SECRET", "")
RAPIDAPI_KEY   = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST  = os.environ.get("RAPIDAPI_HOST", "us-real-estate-listings.p.rapidapi.com")

HF_BASE        = "https://platform.higgsfield.ai/v1"
HF_MODEL       = os.environ.get("HF_MODEL", "dop-turbo")   # dop-lite | dop-preview | dop-turbo
MAX_PHOTOS     = int(os.environ.get("MAX_PHOTOS", "6"))    # 6 photos -> 5 continuous transitions

TOUR_PROMPT = (
    "slow continuous cinematic drone glide moving forward through the home, "
    "smooth flowing camera motion from one room into the next, photorealistic "
    "real estate walkthrough, no people, natural daylight"
)

app = FastAPI(title="ReelTour API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job store (fine for MVP; swap for Redis/DB when scaling)
JOBS: dict[str, dict] = {}


# ----------------------------------------------------------------------------
# 1. Photo extraction from a listing link
# ----------------------------------------------------------------------------
def upscale_photo_url(url: str) -> str:
    """Force any rdcpix / zillowstatic thumbnail to a large render.

    rdcpix encodes size in the suffix before .jpg (e.g. '...s.jpg' = 120x80).
    Rewriting it to 'rd-w1280_h960.jpg' yields ~1024px. Verified during dev.
    """
    if "rdcpix.com" in url:
        # strip any existing size suffix ( s / od / rd-w.._h.. / x ) then add a big one
        url = re.sub(r"(-m\d+)(s|od|x|rd-w\d+_h\d+)?\.jpg", r"\1rd-w1280_h960.jpg", url)
    elif "zillowstatic.com" in url:
        # zillow uses -cc_ft_<w>.jpg ; bump to 1536
        url = re.sub(r"-cc_ft_\d+\.jpg", "-cc_ft_1536.jpg", url)
        url = re.sub(r"-p_[a-z]\.jpg", "-p_f.jpg", url)
    return url


def photos_from_rapidapi(listing_url: str) -> list[str]:
    """Resolve a listing link to photo URLs via the RapidAPI real-estate feed.

    Accepts a full realtor.com/zillow URL or a property id. We pull the property
    id out of the URL and request its detail; fall back to a raw HTML scrape.
    """
    if not RAPIDAPI_KEY:
        return []
    headers = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST}

    # realtor.com listing URLs end in .../<address>_M<propertyId> or _<id>
    m = re.search(r"[_/](M?\d{6,})", listing_url)
    prop_id = m.group(1).lstrip("M") if m else None

    try:
        if prop_id:
            r = requests.get(
                f"https://{RAPIDAPI_HOST}/propertyDetails",
                headers=headers, params={"id": prop_id}, timeout=25,
            )
            if r.ok:
                data = r.json()
                photos = _dig_photos(data)
                if photos:
                    return photos
    except Exception:
        pass
    return []


def photos_from_html(listing_url: str) -> list[str]:
    """Last-resort: fetch the listing page and regex out image URLs."""
    try:
        r = requests.get(
            listing_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/120.0 Safari/537.36"},
            timeout=25,
        )
        html = r.text
        urls = re.findall(r"https://[^\"'\\ ]+?rdcpix\.com/[^\"'\\ ]+?\.jpg", html)
        urls += re.findall(r"https://photos\.zillowstatic\.com/[^\"'\\ ]+?\.jpg", html)
        # dedupe by photo id, keep order
        seen, out = set(), []
        for u in urls:
            key = re.sub(r"(s|od|x|rd-w\d+_h\d+|-cc_ft_\d+)\.jpg", "", u)
            if key not in seen:
                seen.add(key)
                out.append(u)
        return out
    except Exception:
        return []


def _dig_photos(data) -> list[str]:
    """Walk an arbitrary JSON blob collecting anything that looks like a photo href."""
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("href", "url") and isinstance(v, str) and (".jpg" in v):
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for it in node:
                walk(it)

    walk(data)
    return found


def get_listing_photos(listing_url: str) -> list[str]:
    photos = photos_from_rapidapi(listing_url) or photos_from_html(listing_url)
    photos = [upscale_photo_url(u) for u in photos]
    # keep unique, cap
    uniq = list(dict.fromkeys(photos))
    return uniq[:MAX_PHOTOS]


# ----------------------------------------------------------------------------
# 2. Higgsfield REST — import + generate continuous transitions
# ----------------------------------------------------------------------------
def _hf_headers() -> dict:
    if not (HF_KEY_ID and HF_KEY_SECRET):
        raise HTTPException(500, "Higgsfield credentials not configured")
    return {"hf-api-key": HF_KEY_ID, "hf-secret": HF_KEY_SECRET,
            "Content-Type": "application/json"}


def hf_generate_transition(start_url: str, end_url: str) -> dict:
    """Submit one continuous start->end clip. Returns the raw job response.

    Verified request schema (dop endpoint): body wrapped in `params`, model is one
    of dop-lite/dop-preview/dop-turbo, images passed as input_images[].
    """
    body = {"params": {
        "model": HF_MODEL,
        "prompt": TOUR_PROMPT,
        "input_images": [
            {"type": "image_url", "image_url": start_url},
            {"type": "image_url", "image_url": end_url},
        ],
    }}
    r = requests.post(f"{HF_BASE}/image2video/dop", json=body,
                      headers=_hf_headers(), timeout=60)
    if r.status_code == 403:
        raise HTTPException(402, "Higgsfield Platform API is out of credits — "
                                 "top up the platform (separate from MCP/subscription).")
    if not r.ok:
        raise HTTPException(502, f"Higgsfield error {r.status_code}: {r.text[:300]}")
    return r.json()


def hf_poll(job_id: str) -> dict:
    r = requests.get(f"{HF_BASE}/image2video/dop/{job_id}",
                     headers=_hf_headers(), timeout=30)
    return r.json() if r.ok else {"status": "unknown"}


# ----------------------------------------------------------------------------
# 3. Orchestration (runs in a background thread per tour)
# ----------------------------------------------------------------------------
def build_tour(job_id: str, listing_url: str):
    job = JOBS[job_id]
    try:
        job["status"] = "fetching_photos"
        photos = get_listing_photos(listing_url)
        if len(photos) < 2:
            job.update(status="error", error="Could not find enough photos on that listing.")
            return
        job["photos"] = photos

        job["status"] = "generating"
        clip_jobs = []
        for i in range(len(photos) - 1):
            resp = hf_generate_transition(photos[i], photos[i + 1])
            cid = _extract_id(resp)
            clip_jobs.append(cid)
            job["clip_jobs"] = clip_jobs

        # poll all clips until done
        clip_urls: list[Optional[str]] = [None] * len(clip_jobs)
        deadline = time.time() + 900  # 15 min cap
        while None in clip_urls and time.time() < deadline:
            time.sleep(10)
            for idx, cid in enumerate(clip_jobs):
                if clip_urls[idx] or not cid:
                    continue
                st = hf_poll(cid)
                url = _extract_url(st)
                if url:
                    clip_urls[idx] = url
            job["clip_urls"] = clip_urls

        if all(clip_urls):
            job.update(status="done", clip_urls=clip_urls)
        else:
            job.update(status="partial", clip_urls=clip_urls)
    except HTTPException as e:
        job.update(status="error", error=e.detail)
    except Exception as e:  # pragma: no cover
        job.update(status="error", error=str(e))


def _extract_id(resp: dict) -> Optional[str]:
    for k in ("id", "job_id"):
        if isinstance(resp, dict) and resp.get(k):
            return resp[k]
    if isinstance(resp, dict):
        for v in resp.values():
            if isinstance(v, dict) and (v.get("id") or v.get("job_id")):
                return v.get("id") or v.get("job_id")
    return None


def _extract_url(st: dict) -> Optional[str]:
    if not isinstance(st, dict):
        return None
    res = st.get("results") or st.get("result") or {}
    if isinstance(res, dict):
        return res.get("rawUrl") or res.get("url")
    if isinstance(res, list) and res:
        r0 = res[0].get("results", {}) if isinstance(res[0], dict) else {}
        return r0.get("rawUrl") or r0.get("url")
    return None


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
class TourRequest(BaseModel):
    listing_url: str


@app.get("/health")
def health():
    return {"ok": True, "model": HF_MODEL,
            "hf_configured": bool(HF_KEY_ID and HF_KEY_SECRET),
            "rapidapi_configured": bool(RAPIDAPI_KEY)}


@app.post("/api/tour")
def create_tour(req: TourRequest):
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "queued", "listing_url": req.listing_url}
    threading.Thread(target=build_tour, args=(job_id, req.listing_url), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/tour/{job_id}")
def get_tour(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job
