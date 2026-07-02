# ============================================================
#  CLOUD-SYNCED USB MUSIC DRIVE — BACKEND API
#  Stack: Python 3.11+ / FastAPI / Supabase (via REST + Storage)
#  Deploy to: Render (free tier) or Railway
#  File: main.py
# ============================================================

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# ── Load environment variables ───────────────────────────────
load_dotenv()

SUPABASE_URL: str = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY: str = os.environ["SUPABASE_SERVICE_KEY"]
API_SECRET: str = os.environ["API_SECRET"]

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("music-flash-api")

# ── FastAPI App ───────────────────────────────────────────────
app = FastAPI(
    title="Music Flash API",
    description="Cloud-Sync backend for USB Music Drive",
    version="1.0.0",
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)

# ── Supabase HTTP client ──────────────────────────────────────
supabase_headers = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

def supa_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{SUPABASE_URL}/rest/v1",
        headers=supabase_headers,
        timeout=30.0,
    )

# ── Security dependency ───────────────────────────────────────
async def verify_api_secret(x_api_secret: Optional[str] = Header(default=None)):
    if x_api_secret != API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid API secret.")
    return True


# ============================================================
# MODELS
# ============================================================

class AuthRequest(BaseModel):
    drive_serial: str = Field(..., description="Hardware serial or config file token")

class TrackRecord(BaseModel):
    id: str
    title: str
    artist: str
    genre: Optional[str] = None
    file_path: str
    file_size_bytes: Optional[int] = None
    download_url: str

class SyncCompleteRequest(BaseModel):
    drive_serial: str
    downloaded_track_ids: list[str]
    status: str = "success"
    error_message: Optional[str] = None


# ============================================================
# ROUTES
# ============================================================

@app.get("/health")
async def health_check():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/api/v1/auth")
async def authenticate_drive(
    body: AuthRequest,
    request: Request,
    _: bool = Depends(verify_api_secret),
):
    client_ip = request.client.host if request.client else "unknown"
    logger.info(f"Auth attempt | serial={body.drive_serial} | ip={client_ip}")

    async with supa_client() as client:
        resp = await client.get(
            "/usb_drives",
            params={
                "drive_serial": f"eq.{body.drive_serial}",
                "is_active": "eq.true",
                "select": "id,label,user_id,firmware_version,users(full_name,email,plan)",
            },
        )

    if resp.status_code != 200:
        logger.error(f"Supabase error: {resp.text}")
        raise HTTPException(status_code=502, detail="Database error.")

    drives = resp.json()
    if not drives:
        logger.warning(f"Auth FAILED — serial not found: {body.drive_serial}")
        raise HTTPException(status_code=403, detail="Drive not registered or inactive.")

    drive = drives[0]

    async with supa_client() as client:
        await client.patch(
            f"/usb_drives?id=eq.{drive['id']}",
            json={"last_seen_at": datetime.now(timezone.utc).isoformat(), "last_seen_ip": client_ip},
        )

    logger.info(f"Auth SUCCESS | drive_id={drive['id']} | user={drive['users']['email']}")

    return {
        "authenticated": True,
        "drive_id": drive["id"],
        "label": drive["label"],
        "plan": drive["users"]["plan"],
        "owner": drive["users"]["full_name"],
    }


@app.get("/api/v1/sync/{drive_serial}")
async def get_pending_tracks(
    drive_serial: str,
    _: bool = Depends(verify_api_secret),
):
    logger.info(f"Sync request | serial={drive_serial}")

    async with supa_client() as client:
        drive_resp = await client.get(
            "/usb_drives",
            params={
                "drive_serial": f"eq.{drive_serial}",
                "is_active": "eq.true",
                "select": "id",
            },
        )

    if drive_resp.status_code != 200 or not drive_resp.json():
        raise HTTPException(status_code=403, detail="Drive not found or inactive.")

    drive_id = drive_resp.json()[0]["id"]

    async with supa_client() as client:
        access_resp = await client.get(
            "/drive_track_access",
            params={
                "drive_id": f"eq.{drive_id}",
                "downloaded_at": "is.null",
                "select": "id,track_id,tracks(id,title,artist,genre,file_path,file_size_bytes)",
            },
        )

    if access_resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch pending tracks.")

    access_records = access_resp.json()

    if not access_records:
        logger.info(f"No pending tracks for drive {drive_id}")
        return {"pending_tracks": [], "count": 0}

    pending_tracks: list[TrackRecord] = []
    for record in access_records:
        track = record["tracks"]
        signed_url = await _generate_signed_url(track["file_path"], expires_in=3600)
        if signed_url:
            pending_tracks.append(
                TrackRecord(
                    id=track["id"],
                    title=track["title"],
                    artist=track["artist"],
                    genre=track.get("genre"),
                    file_path=track["file_path"],
                    file_size_bytes=track.get("file_size_bytes"),
                    download_url=signed_url,
                )
            )

    logger.info(f"Returning {len(pending_tracks)} tracks for drive {drive_id}")
    return {"pending_tracks": [t.model_dump() for t in pending_tracks], "count": len(pending_tracks)}


@app.post("/api/v1/sync/complete")
async def mark_sync_complete(
    body: SyncCompleteRequest,
    request: Request,
    _: bool = Depends(verify_api_secret),
):
    client_ip = request.client.host if request.client else "unknown"
    now = datetime.now(timezone.utc).isoformat()

    async with supa_client() as client:
        drive_resp = await client.get(
            "/usb_drives",
            params={"drive_serial": f"eq.{body.drive_serial}", "select": "id"},
        )

    if drive_resp.status_code != 200 or not drive_resp.json():
        raise HTTPException(status_code=403, detail="Drive not found.")

    drive_id = drive_resp.json()[0]["id"]

    if body.downloaded_track_ids:
        track_id_filter = "{" + ",".join(body.downloaded_track_ids) + "}"
        async with supa_client() as client:
            await client.patch(
                f"/drive_track_access?drive_id=eq.{drive_id}&track_id=in.{track_id_filter}",
                json={"downloaded_at": now},
            )

    async with supa_client() as client:
        await client.post(
            "/sync_logs",
            json={
                "drive_id": drive_id,
                "tracks_downloaded": len(body.downloaded_track_ids),
                "client_ip": client_ip,
                "status": body.status,
                "error_message": body.error_message,
            },
        )

    logger.info(
        f"Sync complete | drive={drive_id} | "
        f"downloaded={len(body.downloaded_track_ids)} tracks | status={body.status}"
    )

    return {"success": True, "synced_count": len(body.downloaded_track_ids)}


@app.get("/api/v1/admin/stats")
async def admin_stats(_: bool = Depends(verify_api_secret)):
    async with supa_client() as client:
        users_resp   = await client.get("/users",              params={"select": "count"})
        drives_resp  = await client.get("/usb_drives",         params={"select": "count"})
        tracks_resp  = await client.get("/tracks",             params={"select": "count", "is_published": "eq.true"})
        pending_resp = await client.get("/drive_track_access", params={"select": "count", "downloaded_at": "is.null"})

    return {
        "total_users":       users_resp.headers.get("content-range", "?"),
        "total_drives":      drives_resp.headers.get("content-range", "?"),
        "published_tracks":  tracks_resp.headers.get("content-range", "?"),
        "pending_downloads": pending_resp.headers.get("content-range", "?"),
        "timestamp":         datetime.now(timezone.utc).isoformat(),
    }


# ============================================================
# HELPERS
# ============================================================

async def _generate_signed_url(file_path: str, expires_in: int = 3600) -> Optional[str]:
    """Call Supabase Storage API to get a time-limited signed URL."""
    async with httpx.AsyncClient(headers=supabase_headers, timeout=15.0) as client:
        resp = await client.post(
            f"{SUPABASE_URL}/storage/v1/object/sign/tracks/{file_path}",
            json={"expiresIn": expires_in},
        )

    if resp.status_code == 200:
        signed = resp.json().get("signedURL", "")
        if signed.startswith("/"):
            signed = f"{SUPABASE_URL}/storage/v1{signed}"
        return signed if signed else None

    logger.error(f"Signed URL error for {file_path}: {resp.text}")
    return None


# ============================================================
# ENTRY POINT (local dev)
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
