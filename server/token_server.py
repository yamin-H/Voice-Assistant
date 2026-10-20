"""Tiny local server for step 1.

Serves the client page and mints room-join tokens for the "voice-demo" room.
The API secret must never reach the browser, which is why tokens are created here.

Run from the repository root:
    server\\.venv\\Scripts\\python.exe server/token_server.py
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import secrets
from datetime import timedelta
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from livekit import api

ROOT_DIR = Path(__file__).resolve().parent.parent
CLIENT_PAGE = ROOT_DIR / "client" / "index.html"
ROOM_NAME = "voice-demo"
TOKEN_TTL = timedelta(hours=6)

load_dotenv(ROOT_DIR / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

logger = logging.getLogger("token-server")


def _require_env() -> None:
    missing = [
        name
        for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit(
            f"Missing {', '.join(missing)} in {ROOT_DIR / '.env'}. "
            "Add your LiveKit project credentials and restart."
        )


def _clean_identity(raw: str) -> str:
    identity = re.sub(r"[^A-Za-z0-9_-]", "", raw or "")
    return identity[:64] or f"web-{secrets.token_hex(3)}"


def _mint_token(identity: str) -> str:
    token = (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_name(identity)
        .with_ttl(TOKEN_TTL)
        .with_grants(api.VideoGrants(room_join=True, room=ROOM_NAME))
    )
    return token.to_jwt()


async def index(_: web.Request) -> web.Response:
    if not CLIENT_PAGE.is_file():
        raise web.HTTPNotFound(text=f"Client page not found at {CLIENT_PAGE}")
    return web.Response(
        text=CLIENT_PAGE.read_text(encoding="utf-8"),
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def token(request: web.Request) -> web.Response:
    identity = _clean_identity(request.query.get("identity", ""))
    return web.json_response(
        {
            "url": os.environ["LIVEKIT_URL"],
            "token": _mint_token(identity),
            "room": ROOM_NAME,
            "identity": identity,
        },
        headers={"Cache-Control": "no-store"},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the voice-demo test client.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    _require_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/token", token)

    logger.info("voice-demo client: http://%s:%s", args.host, args.port)
    logger.info("serving room %r using %s", ROOM_NAME, os.environ["LIVEKIT_URL"])
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
