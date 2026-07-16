# ReelTour — launch runbook

The product turns a real-estate listing link into a continuous cinematic tour video.
Three pieces: **frontend** (already live), **payments** (Lemon Squeezy link), **backend** (this folder).

---

## What already works (proven in dev)
- Pulling listing photos and **upscaling tiny thumbnails to 1024px** — the fix that made
  generation succeed (small 120x80 thumbnails fail silently with no charge).
- Higgsfield generating **continuous room-to-room transitions** (start→end), not a slideshow.
- Stitching clips into one vertical (9:16 / 3:4) tour with a title card (address + price).

## Two real blockers to going fully automatic
1. **Higgsfield Platform API bills a SEPARATE credit pool** from the MCP/subscription
   credits. The `dop` REST endpoint returned `403 Not enough credits` even with 223
   subscription credits. → Fund the Platform API at platform.higgsfield.ai before the
   backend can generate.
2. The backend needs a host (Render/Railway) — it does not run on the laptop.

---

## Recommended launch order

### Day 1 — sell manually (no backend needed)
1. Frontend is live: `reeltour-home.html` (marketing) + `reeltour.html` (app).
2. **Payments:** create ONE product in Lemon Squeezy (a "Property Tour Video – $49").
   Copy its **Buy Link** and paste it into the `LEMON_LINK` constant in `reeltour.html`.
   (Lemon Squeezy is Merchant-of-Record → it handles VAT/receipts; no company needed.)
3. Buyer pays → sends you the listing link → you run the proven pipeline (via the
   Higgsfield MCP, on the cheap subscription credits) → send back the finished video.
   This validates demand with zero infra.

### Day 2+ — automate (after the first paying customers)
1. Deploy this backend:
   - Push this `reeltour/` folder to a GitHub repo.
   - On Render: New → Blueprint → pick the repo (`render.yaml` is detected).
   - Set env vars `HF_KEY_ID`, `HF_KEY_SECRET`, `RAPIDAPI_KEY` (values from your `.env`).
2. **Fund the Higgsfield Platform API** (blocker #1).
3. Point the frontend at the deployed API: set `API_BASE` in `reeltour.html` to the
   Render URL. The "Create tour (Pro)" button then calls `POST /api/tour`.
4. Wire the Lemon Squeezy **webhook** → only unlock a generation after `order_created`.

---

## Photo sources (important)
- **Realtor.com link** → works (RapidAPI feed, verified live: 120x80 thumbnails → 1280x853).
- **Zillow link** → BLOCKED. Zillow denies automated access from both server and
  browser (bot protection). Do not rely on it.
- **Direct upload** → the universal fallback (covers Zillow and everything else). The
  customer saves the photos and uploads them; needs `PUBLIC_BASE_URL` set so Higgsfield
  can fetch them (Render provides `RENDER_EXTERNAL_URL` automatically).

## API
- `GET  /health` — config check (shows whether uploads are reachable).
- `POST /api/tour` `{ "listing_url": "..." }` → `{ "job_id": "..." }`  (Realtor.com link)
- `POST /api/tour/upload` multipart `files=@a.jpg&files=@b.jpg ...` → `{ "job_id": "..." }`
- `GET  /api/tour/{job_id}` → `{ status, photos, meta{title,loc}, clip_urls[], final_url }`
  statuses: `queued → fetching_photos → generating → stitching → done`
  (`done_unstitched` = clips ok but ffmpeg concat failed; `partial`/`error` as before)
  `final_url` is the ONE stitched tour video (title card with auto-pulled address+price
  + all room transitions). ffmpeg is bundled via `imageio-ffmpeg` (no system install).
  Needs `PUBLIC_BASE_URL`/`RENDER_EXTERNAL_URL` so the final video is reachable.

## One thing to verify with the first funded run
The polling endpoint (`GET /image2video/dop/{id}`) and the success-response shape are
best-effort — `_extract_url()` handles the common shapes. Run one funded tour and
adjust `_extract_id` / `_extract_url` if the live JSON differs. The *request* schema is
already verified against the live API.
