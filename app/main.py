import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI

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

from app import db  # noqa: E402
from app.channels import telegram as telegram_channel  # noqa: E402

# Bump this with each deploy-worth change. Surfaced at GET / so we can confirm
# from outside exactly which code Railway is actually running.
VERSION = "2026-10-11-suite-rehome"


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Abdo starting up, version=%s", VERSION)
    await db.init_pool()
    yield
    await db.close_pool()


app = FastAPI(lifespan=lifespan)
app.include_router(telegram_channel.router)


@app.get("/")
async def health():
    return {"ok": True, "name": "Abdo", "version": VERSION}
