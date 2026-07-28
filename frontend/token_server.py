"""Minimal token server + static host for the browser voice demo.

Serves the demo page and mints LiveKit access tokens so the browser can join
the same room the agent worker answers in.

Run:  python -m uvicorn frontend.token_server:app --port 8080
Open: http://localhost:8080
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from livekit import api

load_dotenv()

app = FastAPI(title="Luma Bistro Voice Demo")
FRONTEND_DIR = Path(__file__).resolve().parent


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/token")
def token(room: str | None = None, identity: str | None = None) -> dict:
    url = os.getenv("LIVEKIT_URL")
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        raise HTTPException(500, "LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET are not set")

    room = room or f"luma-{uuid.uuid4().hex[:6]}"
    identity = identity or f"caller-{uuid.uuid4().hex[:6]}"
    jwt = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name("Caller")
        .with_grants(api.VideoGrants(room_join=True, room=room))
        .to_jwt()
    )
    return {"url": url, "token": jwt, "room": room, "identity": identity}
