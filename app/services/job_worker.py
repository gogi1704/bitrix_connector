import asyncio
import logging

from app.config import Config
from app.services.message_router import MessageRouter
from app.services.followups import FollowupService
from app.services.manager_tools import DialogSummaryService, ReminderService
from app.services.delivery_status import DeliveryStatusService
from app.services.payment_notifications import PaymentNotificationService
from app.services.funnel_reports import FunnelReportService
from app.storage.database import MessageDatabase

logger = logging.getLogger(__name__)


class JobWorker:
    """Persistent single-process worker for webhook jobs stored in SQLite."""

    def __init__(self):
        self.database = MessageDatabase()
        self.task: asyncio.Task | None = None
        self.background_tasks: set[asyncio.Task] = set()
        self.summary_semaphore = asyncio.Semaphore(max(1, Config.SUMMARY_AI_CONCURRENCY))

    async def start(self) -> None:
        self.database.recover_processing_jobs()
        self.database.purge_old_jobs()
        self.task = asyncio.create_task(self._run(), name="webhook-job-worker")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        for background_task in self.background_tasks:
            background_task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            job = self.database.claim_job()
            if job is None:
                await asyncio.sleep(Config.JOB_POLL_SECONDS)
                continue

            if job["job_type"] == "manager_summary":
                task = asyncio.create_task(
                    self._execute(job, background=True),
                    name=f"manager-summary-{job['id']}",
                )
                self.background_tasks.add(task)
                task.add_done_callback(self.background_tasks.discard)
                continue

            await self._execute(job)

    async def _execute(self, job: dict, *, background: bool = False) -> None:
        try:
            if background:
                async with self.summary_semaphore:
                    await self._process(job)
            else:
                await self._process(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Webhook job %s failed", job["id"])
            if (
                job["job_type"] == "followup_send"
                and job["attempts"] >= Config.JOB_MAX_ATTEMPTS
            ):
                self.database.mark_followup_failed(
                    int(job["payload"]["followup_id"]), str(exc)
                )
            if (
                job["job_type"] == "manager_reminder"
                and job["attempts"] >= Config.JOB_MAX_ATTEMPTS
            ):
                self.database.mark_manager_reminder_failed(
                    int(job["payload"]["reminder_id"]), str(exc)
                )
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

        if job["job_type"] == "followup_plan":
            await FollowupService(self.database).plan(int(job["payload"]["followup_id"]))
            return

        if job["job_type"] == "followup_send":
            await FollowupService(self.database).send(int(job["payload"]["followup_id"]))
            return

        if job["job_type"] == "manager_reminder":
            await ReminderService(self.database).send(
                int(job["payload"]["reminder_id"])
            )
            return

        if job["job_type"] == "bitrix_delivery_status":
            await DeliveryStatusService.send(job["payload"])
            return

        if job["job_type"] == "consilium_payment_notification":
            await PaymentNotificationService.send(job["payload"])
            return

        if job["job_type"] == "consilium_funnel_report":
            await FunnelReportService.send(job["payload"])
            return

        if job["job_type"] == "manager_summary":
            payload = job["payload"]
            service = DialogSummaryService(self.database)
            if payload["mode"] == "all":
                text = await service.build_all_ai(
                    dialog_id=int(payload["dialog_id"]),
                    external_user_id=str(payload["external_user_id"]),
                    external_chat_id=str(payload["external_chat_id"]),
                )
            else:
                text = await service.build_ai(
                    dialog_id=int(payload["dialog_id"]),
                    external_user_id=str(payload["external_user_id"]),
                )
            await router._send_internal_message(
                chat_id=str(payload["external_chat_id"]),
                user_id=str(payload["external_user_id"]),
                user_name=payload.get("external_user_name") or "Пользователь MAX",
                text=text,
                message_id=f"manager-summary-result-{job['id']}",
            )
            return

        raise ValueError(f"Unsupported job type: {job['job_type']}")
