"""Render privacy-safe Consilium funnel snapshots for a Bitrix project chat."""

from app.config import Config
from app.services.bitrix_client import BitrixClient


class FunnelReportService:

    @staticmethod
    def _safe(value: object, maximum: int = 200) -> str:
        return str(value or "").replace("[", "(").replace("]", ")")[:maximum]

    @staticmethod
    def _money(kopecks: object) -> str:
        value = max(0, int(kopecks or 0))
        return f"{value / 100:,.2f} ₽".replace(",", " ").replace(".00", "")

    @classmethod
    def _period(cls, value: dict) -> str:
        start = cls._safe(value.get("date_from"), 10)
        end = cls._safe(value.get("date_to"), 10)
        return start if start == end else f"{start} — {end}"

    @classmethod
    def render(cls, payload: dict) -> str:
        current = payload.get("current") or {}
        previous = payload.get("comparison") or {}
        summary = current.get("summary") or {}
        previous_summary = previous.get("summary") or {}
        prefix = "🧪 Тестовый отчёт" if payload.get("test") else "📊 Мониторинг воронки"
        lines = [
            f"[B]{prefix} Консилиума[/B]",
            f"[B]Вид анализа:[/B] {cls._safe(payload.get('analysis_label') or 'Полный отчёт', 100)}",
            "",
            f"[B]Текущий период:[/B] {cls._period(payload.get('current_period') or {})}",
            f"[B]Сравнение:[/B] {cls._period(payload.get('comparison_period') or {})}",
            f"[B]Посетители:[/B] {int(summary.get('visitors', 0) or 0)} "
            f"(ранее {int(previous_summary.get('visitors', 0) or 0)})",
            f"[B]Зарегистрировались:[/B] {int(summary.get('users', 0) or 0)} "
            f"(ранее {int(previous_summary.get('users', 0) or 0)})",
        ]
        for flow in payload.get("flows") or []:
            flow_summary = flow.get("summary") or {}
            start_users = int(flow_summary.get("start_users", 0) or 0)
            completion = int(flow_summary.get("reached_completion", 0) or 0)
            completion_percent = round(completion / start_users * 100, 1) if start_users else 0.0
            lines.extend([
                "",
                f"[B]{cls._safe(flow.get('label') or flow.get('id'), 100)}[/B]",
                f"Стартовали: {start_users} · дошли до результата: {completion} ({completion_percent}%)",
            ])
            if not flow.get("sample_sufficient"):
                lines.append("⚠️ Выборка ниже установленного минимума — выводы предварительные.")
            alerts = flow.get("alerts") or []
            if alerts:
                lines.append("[B]Критические отклонения:[/B]")
                for item in alerts[:10]:
                    lines.append(
                        f"🔻 {cls._safe(item.get('title'))}: "
                        f"{float(item.get('current_percent', 0) or 0):g}% вместо "
                        f"{float(item.get('previous_percent', 0) or 0):g}% "
                        f"({float(item.get('change_pp', 0) or 0):+g} п.п.)"
                    )
            lines.append("[B]Экраны:[/B]")
            for screen in (flow.get("screens") or [])[:60]:
                quality = " · неполные связи" if screen.get("data_quality") == "incomplete" else ""
                outcome = "финальный экран" if screen.get("terminal") else f"остановились {int(screen.get('stopped_users', 0) or 0)}"
                lines.append(
                    f"• {cls._safe(screen.get('title'))}: {int(screen.get('users', 0) or 0)} чел. · "
                    f"{float(screen.get('percent_of_parent', 0) or 0):g}% от предыдущего · "
                    f"Δ {float(screen.get('change_pp', 0) or 0):+g} п.п. · "
                    f"{outcome}{quality}"
                )
        payments = current.get("payments")
        if isinstance(payments, dict):
            old_payments = previous.get("payments") or {}
            lines.extend([
                "",
                "[B]Оплата[/B]",
                f"Попытки: {int(payments.get('attempts', 0) or 0)} "
                f"(ранее {int(old_payments.get('attempts', 0) or 0)})",
                f"Успешные пользователи: {int(payments.get('successful_users', 0) or 0)} · "
                f"конверсия {float(payments.get('conversion', 0) or 0):g}% "
                f"(ранее {float(old_payments.get('conversion', 0) or 0):g}%)",
                f"Выручка без тестовых платежей: {cls._money(payments.get('revenue_kopecks'))}",
                f"Не завершено/ошибка: {int(payments.get('unsuccessful', 0) or 0)} · "
                f"в ожидании: {int(payments.get('pending', 0) or 0)}",
            ])
        errors = current.get("errors") or []
        if errors:
            lines.extend(["", "[B]Технические ошибки[/B]"])
            for item in errors[:10]:
                lines.append(
                    f"• {cls._safe(item.get('label'), 100)}: "
                    f"{int(item.get('events', 0) or 0)} событий, "
                    f"{int(item.get('users', 0) or 0)} пользователей"
                )
        lines.extend([
            "",
            "[B]Задание для Bitrix AI[/B]",
            cls._safe(payload.get("ai_instruction"), 1_000),
            "",
            "[I]В отчёте только обезличенные агрегаты. Причины отклонений, не подтверждённые данными, являются гипотезами.[/I]",
        ])
        return "\n".join(lines)

    @classmethod
    async def send(cls, payload: dict) -> None:
        dialog_id = Config.BITRIX_METRICS_DIALOG_ID or str(payload.get("dialog_id") or "")
        if not dialog_id:
            raise RuntimeError("Bitrix metrics dialog is not configured")
        await BitrixClient().call(
            "im.message.add",
            {
                "DIALOG_ID": dialog_id,
                "MESSAGE": cls.render(payload),
                "SYSTEM": "N",
                "URL_PREVIEW": "N",
            },
        )
