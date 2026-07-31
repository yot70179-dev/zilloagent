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
import shutil
import subprocess
import threading
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ----------------------------------------------------------------------------
# Config / credentials (from environment — never hard-code)
# ----------------------------------------------------------------------------
HF_KEY_ID      = os.environ.get("HF_KEY_ID", "")
HF_KEY_SECRET  = os.environ.get("HF_KEY_SECRET", "")
RAPIDAPI_KEY   = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST  = os.environ.get("RAPIDAPI_HOST", "us-real-estate-listings.p.rapidapi.com")

HF_BASE        = "https://platform.higgsfield.ai/v1"
HF_MODEL       = os.environ.get("HF_MODEL", "dop-preview")  # dop-lite | dop-preview (best) | dop-turbo
MAX_PHOTOS     = int(os.environ.get("MAX_PHOTOS", "20"))    # use as many rooms as the listing has (N photos -> N-1 clips)

TOUR_PROMPT = (
    "slow continuous cinematic drone glide moving forward through the home, "
    "smooth flowing camera motion from one room into the next, photorealistic "
    "real estate walkthrough, no people, natural daylight"
)

# Public base URL of THIS server — needed so Higgsfield can fetch uploaded photos.
# Render sets RENDER_EXTERNAL_URL automatically; override with PUBLIC_BASE_URL if needed.
PUBLIC_BASE = (os.environ.get("PUBLIC_BASE_URL")
               or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
UPLOAD_DIR  = os.environ.get("UPLOAD_DIR", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="ReelTour API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
# Serve uploaded photos publicly so Higgsfield's media_import can fetch them
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

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


def get_listing_meta(listing_url: str) -> dict:
    """Pull address + price from the listing so the title card fills itself in."""
    meta = {"title": "Property Tour", "loc": ""}
    if not RAPIDAPI_KEY:
        return meta
    m = re.search(r"[_/](M?\d{6,})", listing_url)
    if not m:
        return meta
    prop_id = m.group(1).lstrip("M")
    try:
        r = requests.get(f"https://{RAPIDAPI_HOST}/propertyDetails",
                         headers={"x-rapidapi-key": RAPIDAPI_KEY,
                                  "x-rapidapi-host": RAPIDAPI_HOST},
                         params={"id": prop_id}, timeout=25)
        d = r.json() if r.ok else {}
        # dig for address line / city / price anywhere in the blob
        line = _first(d, ("line",)) or ""
        city = _first(d, ("city",)) or ""
        price = _first(d, ("list_price", "price"))
        if line:
            meta["title"] = line
        loc_parts = [p for p in (city,) if p]
        if price:
            loc_parts.append("$" + f"{int(price):,}")
        meta["loc"] = " · ".join(loc_parts)
    except Exception:
        pass
    return meta


def _first(node, keys):
    """Depth-first search for the first value under any of `keys`."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in keys and isinstance(v, (str, int, float)) and v not in ("", None):
                return v
        for v in node.values():
            r = _first(v, keys)
            if r not in (None, "", 0):
                return r
    elif isinstance(node, list):
        for it in node:
            r = _first(it, keys)
            if r not in (None, "", 0):
                return r
    return None


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
def build_tour_from_link(job_id: str, listing_url: str):
    """Resolve a listing link to photos + price/address, then run the pipeline."""
    job = JOBS[job_id]
    job["status"] = "fetching_photos"
    photos = get_listing_photos(listing_url)
    if len(photos) < 2:
        job.update(status="error",
                   error="Could not find enough photos on that listing. "
                         "Zillow links are blocked — use a Realtor.com link or upload photos.")
        return
    job["meta"] = get_listing_meta(listing_url)   # {title, loc} auto-pulled
    build_tour(job_id, photos, job["meta"])


def build_tour(job_id: str, photos: list[str], meta: Optional[dict] = None):
    """Core pipeline: photos -> continuous clips -> ONE stitched tour video."""
    job = JOBS[job_id]
    try:
        photos = [upscale_photo_url(u) for u in photos][:MAX_PHOTOS]
        if len(photos) < 2:
            job.update(status="error", error="Need at least 2 photos.")
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

        job["clip_urls"] = clip_urls
        ready = [u for u in clip_urls if u]
        if not ready:
            job.update(status="error", error="No clips were generated.")
            return

        # stitch every finished clip into ONE tour video with a title card
        job["status"] = "stitching"
        try:
            final = stitch_clips(job_id, ready, meta or job.get("meta") or {})
            job.update(status="done", final_url=final,
                       clip_urls=clip_urls)
        except Exception as e:
            # clips are fine even if stitching fails — return them so nothing is lost
            job.update(status="done_unstitched", clip_urls=clip_urls,
                       stitch_error=str(e))
    except HTTPException as e:
        job.update(status="error", error=e.detail)
    except Exception as e:  # pragma: no cover
        job.update(status="error", error=str(e))


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _esc(t: str) -> str:
    """Escape text for ffmpeg drawtext."""
    return (t or "").replace("\\", "").replace(":", r"\:").replace("'", "")


def stitch_clips(job_id: str, clip_urls: list[str], meta: dict) -> str:
    """Download clips, prepend a title card, concat into one 1080p tour MP4.

    Returns the public URL of the final video (served from /uploads).
    Requires PUBLIC_BASE so the dashboard can play it; ffmpeg via imageio-ffmpeg.
    """
    if not PUBLIC_BASE:
        raise RuntimeError("PUBLIC_BASE_URL not set — cannot publish the final video.")
    ff = _ffmpeg()
    work = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(work, exist_ok=True)

    # 1. download each clip, normalise to 1920x1080 / 30fps / same codec so concat is clean
    norm_files = []
    for i, url in enumerate(clip_urls):
        raw = os.path.join(work, f"raw{i}.mp4")
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(raw, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        norm = os.path.join(work, f"n{i}.mp4")
        subprocess.run([ff, "-y", "-i", raw,
                        "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,"
                               "crop=1920:1080,fps=30",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", norm],
                       check=True, capture_output=True)
        norm_files.append(norm)

    # 2. title card (address + price) as a 2s clip matching the format
    title = _esc(meta.get("title", "Property Tour"))
    loc   = _esc(meta.get("loc", ""))
    card = os.path.join(work, "card.mp4")
    draw = (f"drawtext=text='{title}':fontcolor=white:fontsize=64:x=(w-tw)/2:y=(h/2)-40:"
            f"box=1:boxcolor=black@0.0")
    if loc:
        draw += (f",drawtext=text='{loc}':fontcolor=0xD8A24E:fontsize=40:"
                 f"x=(w-tw)/2:y=(h/2)+40")
    subprocess.run([ff, "-y", "-f", "lavfi",
                    "-i", "color=c=0x141414:s=1920x1080:d=2:r=30",
                    "-vf", draw, "-c:v", "libx264", "-pix_fmt", "yuv420p", card],
                   check=True, capture_output=True)

    # 3. concat card + all clips
    listing = os.path.join(work, "list.txt")
    with open(listing, "w") as f:
        f.write(f"file '{os.path.abspath(card)}'\n")
        for n in norm_files:
            f.write(f"file '{os.path.abspath(n)}'\n")
    out = os.path.join(work, "tour.mp4")
    subprocess.run([ff, "-y", "-f", "concat", "-safe", "0", "-i", listing,
                    "-c", "copy", out], check=True, capture_output=True)

    return f"{PUBLIC_BASE}/uploads/{job_id}/tour.mp4"


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
            "rapidapi_configured": bool(RAPIDAPI_KEY),
            "public_base": PUBLIC_BASE or "(unset — uploads need PUBLIC_BASE_URL)"}


@app.post("/api/tour")
def create_tour(req: TourRequest):
    """Create a tour from a listing link (Realtor.com works; Zillow is blocked)."""
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "queued", "source": "link", "listing_url": req.listing_url}
    threading.Thread(target=build_tour_from_link, args=(job_id, req.listing_url),
                     daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/tour/upload")
async def create_tour_upload(files: list[UploadFile] = File(...)):
    """Create a tour from directly uploaded photos (covers Zillow & any other source).

    Files are saved and served from /uploads so Higgsfield can fetch them by URL.
    Requires PUBLIC_BASE_URL (or RENDER_EXTERNAL_URL) so the URLs are reachable.
    """
    if not PUBLIC_BASE:
        raise HTTPException(500, "PUBLIC_BASE_URL not set — cannot expose uploads to Higgsfield.")
    if len(files) < 2:
        raise HTTPException(400, "Upload at least 2 photos.")

    job_id = uuid.uuid4().hex
    folder = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(folder, exist_ok=True)
    urls: list[str] = []
    for idx, f in enumerate(files[:MAX_PHOTOS]):
        ext = os.path.splitext(f.filename or "")[1].lower() or ".jpg"
        name = f"{idx:02d}{ext}"
        with open(os.path.join(folder, name), "wb") as out:
            shutil.copyfileobj(f.file, out)
        urls.append(f"{PUBLIC_BASE}/uploads/{job_id}/{name}")

    JOBS[job_id] = {"status": "queued", "source": "upload", "photos": urls}
    threading.Thread(target=build_tour, args=(job_id, urls), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/tour/{job_id}")
def get_tour(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job
