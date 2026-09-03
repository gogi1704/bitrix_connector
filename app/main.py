import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from app.routes.bitrix import router as bitrix_router
from app.routes.analytics import router as analytics_router
from app.routes.max import router as max_router
from app.services.bitrix_client import BitrixApiError
from app.services.job_worker import JobWorker
from app.storage.database import MessageDatabase
import truststore
truststore.inject_into_ssl()


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# httpx includes full query strings in INFO records. Media upload URLs contain
# short-lived credentials, so only transport warnings/errors may be logged.
logging.getLogger("httpx").setLevel(logging.WARNING)
worker = JobWorker()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await worker.start()
    yield
    await worker.stop()

app = FastAPI(
    title="Bitrix Connector",
    version="0.1",
    lifespan=lifespan,
)

app.include_router(bitrix_router)
app.include_router(max_router)
app.include_router(analytics_router)


@app.exception_handler(BitrixApiError)
async def bitrix_api_error_handler(_: Request, exc: BitrixApiError):
    return JSONResponse(
        status_code=502,
        content={"detail": "Bitrix API request failed", "bitrix": exc.payload},
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "queue": MessageDatabase().queue_status(),
        "capabilities": {
            "consilium_payment_schedule_fields": True,
            "consilium_funnel_reports": True,
        },
    }


@app.get("/")
def home():
    return {
        "status": "ok",
        "message": "Bitrix Connector работает"
    }
