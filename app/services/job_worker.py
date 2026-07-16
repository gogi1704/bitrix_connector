import asyncio
import logging

from app.config import Config
from app.services.message_router import MessageRouter
from app.storage.database import MessageDatabase

logger = logging.getLogger(__name__)


class JobWorker:
    """Persistent single-process worker for webhook jobs stored in SQLite."""

    def __init__(self):
        self.database = MessageDatabase()
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        self.database.recover_processing_jobs()
        self.task = asyncio.create_task(self._run(), name="webhook-job-worker")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            job = self.database.claim_job()
            if job is None:
                await asyncio.sleep(Config.JOB_POLL_SECONDS)
                continue

            try:
                await self._process(job)
            except Exception as exc:
                logger.exception("Webhook job %s failed", job["id"])
                self.database.fail_job(
                    job_id=job["id"],
                    attempts=job["attempts"],
                    error=str(exc),
                    max_attempts=Config.JOB_MAX_ATTEMPTS,
                )
            else:
                self.database.complete_job(job["id"])

    async def _process(self, job: dict) -> None:
        router = MessageRouter()
        if job["job_type"] == "max_update":
            update = job["payload"]
            if update.get("update_type") == "bot_started":
                await router.on_max_bot_started(update)
            elif update.get("update_type") == "message_created":
                await router.from_max(update)
            return

        if job["job_type"] == "bitrix_operator_message":
            await router.from_bitrix(job["payload"])
            return

        raise ValueError(f"Unsupported job type: {job['job_type']}")
