import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

# uvicorn doesn't attach a handler to the root logger, so a bare getLogger()
# at INFO emits nothing. Attach our own handler so app logs actually appear.
# (Configured before importing the app modules that log.)
log = logging.getLogger("abdo")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:abdo: %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)
log.propagate = False


class RedactWebhookSecret(logging.Filter):
    """uvicorn's access log prints the request path, and the Telegram webhook path
    carries the secret (/tg/<secret>). Keep the access log, drop the secret."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) \
                and args[2].startswith("/tg/"):
            record.args = (*args[:2], "/tg/<redacted>", *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(RedactWebhookSecret())

from app import db, scheduler  # noqa: E402
from app.config import settings  # noqa: E402
from app.channels import telegram as telegram_channel  # noqa: E402
from app.channels import web as web_channel  # noqa: E402

# Bump this with each deploy-worth change. Surfaced at GET /api/health so we can
# confirm from outside exactly which code Railway is actually running.
VERSION = "2026-10-15-voice-fixes"

# The built React app (web/dist). Absent in a backend-only checkout: then GET /
# falls back to the health JSON.
WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Abdo starting up, version=%s", VERSION)
    await db.init_pool()
    task = asyncio.create_task(scheduler.run()) if settings.scheduler_enabled else None
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await db.close_pool()


app = FastAPI(lifespan=lifespan)
app.include_router(telegram_channel.router)
app.include_router(web_channel.router)


@app.get("/api/health")
async def health():
    return {"ok": True, "name": "Abdo", "version": VERSION}


# --- the web app (SPA) -----------------------------------------------------------
# Registered last so every API/webhook route wins. Real files (hashed assets, the
# service worker, icons) are served as-is; any other path gets index.html so
# client-side routes (/chat, /lists) work on refresh and from the home screen.
_NO_CACHE = {"Cache-Control": "no-cache"}


@app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def spa(full_path: str):
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(404, "not found")
    index = WEB_DIST / "index.html"
    if not index.exists():
        return await health()           # backend-only checkout
    if full_path:
        candidate = (WEB_DIST / full_path).resolve()
        if candidate.is_file() and WEB_DIST in candidate.parents:
            # index.html and the service worker must always be revalidated, or an
            # installed app can get stuck on an old version. Hashed assets can't go stale.
            if candidate.name in ("sw.js", "index.html", "manifest.webmanifest"):
                return FileResponse(candidate, headers=_NO_CACHE)
            return FileResponse(candidate)
    return FileResponse(index, headers=_NO_CACHE)
