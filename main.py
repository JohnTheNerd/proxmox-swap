"""FastAPI application entry point with lifespan for crash recovery and watchdog."""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles

from app import config
from app.watchdog import Watchdog
from app.routes import create_router, create_public_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("proxmox-swap")

watchdog = Watchdog()

app = FastAPI(title="Proxmox Swap Manager")
if config.config.AUTH_MODE == "oidc":
    app.add_middleware(SessionMiddleware, secret_key=config.config.SESSION_SECRET)
app.mount("/static", StaticFiles(directory="static"), name="static")
@asynccontextmanager
async def lifespan(app):
    errors = config.validate(config.config)
    if errors:
        logger.error("Config validation failed:\n%s", "\n".join(errors))
        sys.exit(1)

    logger.info("Starting Proxmox Swap Manager")
    logger.info("PVE hosts: %s", ", ".join(config.config.PVE_HOSTS))
    logger.info("Host CT: %s, Guest CT: %s", config.config.HOST_CT_ID, config.config.GUEST_CT_ID)
    await watchdog.start()
    app.include_router(create_public_router())
    app.include_router(create_router(watchdog))
    yield
    logger.info("Shutting down")
    await watchdog.stop()


app.router.lifespan_context = lifespan


def main():
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
