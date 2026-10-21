import asyncio
import logging
from datetime import datetime, timezone

from app.config import Config
from app.services.message_router import MessageRouter
from app.services.followups import FollowupService
from app.services.manager_tools import DialogSummaryService, ReminderService
from app.services.delivery_status import DeliveryStatusService
from app.services.payment_notifications import PaymentNotificationService
from app.services.funnel_reports import FunnelReportService
from app.services.manager_user_reports import ManagerUserReportService
from app.storage.database import MessageDatabase

logger = logging.getLogger(__name__)


class JobWorker:
    """Persistent single-process worker for webhook jobs stored in SQLite."""

    def __init__(self):
        self.database = MessageDatabase()
        self.task: asyncio.Task | None = None
        self.background_tasks: set[asyncio.Task] = set()
        self.summary_semaphore = asyncio.Semaphore(max(1, Config.SUMMARY_AI_CONCURRENCY))
        self.started_at: str | None = None
        self.last_poll_at: str | None = None
        self.last_successful_poll_at: str | None = None
        self.last_job_completed_at: str | None = None
        self.last_error: str | None = None
        self.poll_errors = 0

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def health(self) -> dict:
        return {
            "alive": bool(self.task and not self.task.done()),
            "started_at": self.started_at,
            "last_poll_at": self.last_poll_at,
            "last_successful_poll_at": self.last_successful_poll_at,
            "last_job_completed_at": self.last_job_completed_at,
            "last_error": self.last_error,
            "poll_errors": self.poll_errors,
        }

    async def start(self) -> None:
        if self.task and not self.task.done():
            return
        self.database.recover_processing_jobs()
        self.database.purge_old_jobs()
        self.started_at = self._now()
        self.last_error = None
        self.task = asyncio.create_task(self._run(), name="webhook-job-worker")
        self.task.add_done_callback(self._log_unexpected_stop)

    def _log_unexpected_stop(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.last_error = f"{type(error).__name__}: {error}"
            logger.error(
                "Webhook job worker stopped unexpectedly",
                exc_info=(type(error), error, error.__traceback__),
            )
        else:
            self.last_error = "Worker stopped unexpectedly without an exception"
            logger.error(self.last_error)

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
            self.last_poll_at = self._now()
            try:
                job = self.database.claim_job()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.poll_errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "Webhook job queue polling failed; worker will retry"
                )
                await asyncio.sleep(max(0.1, Config.JOB_POLL_SECONDS))
                continue

            self.last_successful_poll_at = self._now()
            self.last_error = None
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

            try:
                await self._execute(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Processing errors are normally handled by _execute. Keep this
                # final boundary so a bookkeeping/SQLite error cannot silently
                # terminate the entire persistent worker.
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "Webhook job %s escaped the worker boundary; continuing",
                    job.get("id"),
                )
                await asyncio.sleep(max(0.1, Config.JOB_POLL_SECONDS))
                continue

            self.last_job_completed_at = self._now()

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

        if job["job_type"] == "consilium_manager_user_report":
            await ManagerUserReportService.send(job["payload"])
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
