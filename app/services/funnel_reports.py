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
        daily = payload.get("new_users_daily") or {}
        daily_current = daily.get("current") or {}
        daily_previous = daily.get("comparison") or {}
        if daily_current:
            lines.extend([
                "",
                f"[B]Новые пользователи за {cls._safe(daily.get('date'), 10)}[/B]",
                f"Впервые открыли Консилиум: "
                f"{int(daily_current.get('first_visitors', 0) or 0)} "
                f"(днём ранее {int(daily_previous.get('first_visitors', 0) or 0)})",
                f"Завершили вход: {int(daily_current.get('registered_users', 0) or 0)} "
                f"(днём ранее {int(daily_previous.get('registered_users', 0) or 0)})",
                f"Конверсия новых посетителей во вход: "
                f"{float(daily_current.get('registration_conversion', 0) or 0):g}% "
                f"(днём ранее "
                f"{float(daily_previous.get('registration_conversion', 0) or 0):g}%)",
                f"Привязали Telegram или MAX: "
                f"{int(daily_current.get('messenger_linked_users', 0) or 0)}",
                f"Открыли новый диалог: {int(daily_current.get('chat_users', 0) or 0)} · "
                f"написали сообщение: {int(daily_current.get('message_users', 0) or 0)} · "
                f"открыли психологический чат: "
                f"{int(daily_current.get('psychology_chat_users', 0) or 0)}",
            ])
            routes = daily_current.get("routes") or []
            if routes:
                lines.append("[B]Куда пришли:[/B] " + " · ".join(
                    f"{cls._safe(item.get('label'), 80)} — {int(item.get('users', 0) or 0)}"
                    for item in routes[:10]
                ))
            methods = daily_current.get("registration_methods") or []
            if methods:
                lines.append("[B]Как вошли:[/B] " + " · ".join(
                    f"{cls._safe(item.get('label'), 80)} — {int(item.get('users', 0) or 0)}"
                    for item in methods[:10]
                ))
        for flow in payload.get("flows") or []:
            flow_summary = flow.get("summary") or {}
            start_users = int(flow_summary.get("start_users", 0) or 0)
            previous_start_users = int(flow_summary.get("previous_start_users", 0) or 0)
            completion = int(flow_summary.get("reached_completion", 0) or 0)
            previous_completion = int(
                flow_summary.get("previous_reached_completion", 0) or 0
            )
            completion_percent = round(completion / start_users * 100, 1) if start_users else 0.0
            previous_completion_percent = (
                round(previous_completion / previous_start_users * 100, 1)
                if previous_start_users else 0.0
            )
            lines.extend([
                "",
                f"[B]{cls._safe(flow.get('label') or flow.get('id'), 100)}[/B]",
                f"Текущий период: стартовали {start_users} · дошли до результата "
                f"{completion} ({completion_percent}%)",
                f"Период сравнения: стартовали {previous_start_users} · дошли до результата "
                f"{previous_completion} ({previous_completion_percent}%)",
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
                current_quality = (
                    " · неполные связи" if screen.get("data_quality") == "incomplete" else ""
                )
                previous_quality = (
                    " · неполные связи"
                    if screen.get("previous_data_quality") == "incomplete" else ""
                )
                if screen.get("terminal"):
                    current_outcome = previous_outcome = "финальный экран"
                else:
                    current_outcome = (
                        f"остановились {int(screen.get('stopped_users', 0) or 0)}"
                    )
                    previous_outcome = (
                        "остановились "
                        f"{int(screen.get('previous_stopped_users', 0) or 0)}"
                    )
                lines.extend([
                    f"• [B]{cls._safe(screen.get('title'))}[/B]",
                    f"  Сейчас: {int(screen.get('users', 0) or 0)} чел. · "
                    f"{float(screen.get('percent_of_parent', 0) or 0):g}% от предыдущего экрана · "
                    f"{float(screen.get('percent_of_start', 0) or 0):g}% от старта · "
                    f"база {int(screen.get('comparison_users', 0) or 0)} · "
                    f"{current_outcome}{current_quality}",
                    f"  Сравнение: {int(screen.get('previous_users', 0) or 0)} чел. · "
                    f"{float(screen.get('previous_percent_of_parent', 0) or 0):g}% от предыдущего экрана · "
                    f"{float(screen.get('previous_percent_of_start', 0) or 0):g}% от старта · "
                    f"база {int(screen.get('previous_comparison_users', 0) or 0)} · "
                    f"{previous_outcome}{previous_quality}",
                    f"  Изменение конверсии: "
                    f"{float(screen.get('change_pp', 0) or 0):+g} п.п.",
                ])
        payments = current.get("payments")
        if isinstance(payments, dict):
            old_payments = previous.get("payments") or {}
            lines.extend([
                "",
                "[B]Оплата[/B]",
                f"Попытки онлайн-оплаты: {int(payments.get('attempts', 0) or 0)} "
                f"(ранее {int(old_payments.get('attempts', 0) or 0)})",
                f"Успешно оплатили онлайн: {int(payments.get('successful_users', 0) or 0)} · "
                f"конверсия среди пользователей с онлайн-заказом "
                f"{float(payments.get('conversion', 0) or 0):g}% "
                f"(сравнение: {int(old_payments.get('successful_users', 0) or 0)} · "
                f"{float(old_payments.get('conversion', 0) or 0):g}%)",
                f"Целевое действие «Оплатить на медосмотре»: "
                f"{int(payments.get('at_exam_users', 0) or 0)} пользователей "
                f"(ранее {int(old_payments.get('at_exam_users', 0) or 0)})",
                f"Выручка без тестовых платежей: {cls._money(payments.get('revenue_kopecks'))} "
                f"(сравнение: {cls._money(old_payments.get('revenue_kopecks'))})",
                f"Онлайн-заказы не завершены/с ошибкой: {int(payments.get('unsuccessful', 0) or 0)} · "
                f"в ожидании: {int(payments.get('pending', 0) or 0)} "
                f"(сравнение: {int(old_payments.get('unsuccessful', 0) or 0)} · "
                f"в ожидании {int(old_payments.get('pending', 0) or 0)})",
            ])
        errors = current.get("errors") or []
        previous_errors = previous.get("errors") or []
        if errors or previous_errors:
            lines.extend(["", "[B]Технические ошибки[/B]"])
            current_by_label = {str(item.get("label", "")): item for item in errors}
            previous_by_label = {
                str(item.get("label", "")): item for item in previous_errors
            }
            labels = list(current_by_label)
            labels.extend(label for label in previous_by_label if label not in current_by_label)
            for label in labels[:10]:
                item = current_by_label.get(label, {})
                old_item = previous_by_label.get(label, {})
                lines.append(
                    f"• {cls._safe(label, 100)}: сейчас "
                    f"{int(item.get('events', 0) or 0)} событий, "
                    f"{int(item.get('users', 0) or 0)} пользователей; сравнение "
                    f"{int(old_item.get('events', 0) or 0)} событий, "
                    f"{int(old_item.get('users', 0) or 0)} пользователей"
                )
        lines.extend([
            "",
            "[B]Задание для Bitrix AI[/B]",
            cls._safe(payload.get("ai_instruction"), 2_000),
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
