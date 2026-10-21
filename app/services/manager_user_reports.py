"""Render daily manager-attributed Consilium registration reports."""

from app.config import Config
from app.services.bitrix_client import BitrixClient


class ManagerUserReportService:

    @staticmethod
    def _safe(value: object, maximum: int = 120) -> str:
        return str(value or "").replace("[", "(").replace("]", ")")[:maximum]

    @staticmethod
    def _new_users_label(value: int) -> str:
        remainder_100 = value % 100
        remainder_10 = value % 10
        if remainder_10 == 1 and remainder_100 != 11:
            noun = "новый пользователь"
        elif remainder_10 in {2, 3, 4} and remainder_100 not in {12, 13, 14}:
            noun = "новых пользователя"
        else:
            noun = "новых пользователей"
        return f"{value} {noun}"

    @classmethod
    def render(cls, payload: dict) -> str:
        prefix = "🧪 Тестовый отчёт" if payload.get("test") else "👥 Новые пользователи"
        lines = [
            f"[B]{prefix} по менеджерам[/B]",
            f"[B]Дата:[/B] {cls._safe(payload.get('date'), 10)}",
            "",
            f"Всего новых пользователей: {int(payload.get('total_users', 0) or 0)}",
            f"С меткой менеджера: {int(payload.get('attributed_users', 0) or 0)}",
            f"Без метки менеджера: {int(payload.get('unattributed_users', 0) or 0)}",
            "",
            "[B]По каждому менеджеру[/B]",
        ]
        for item in payload.get("managers") or []:
            users = int(item.get("users", 0) or 0)
            lines.append(
                f"• {cls._safe(item.get('manager_name') or item.get('from_manager'))}: "
                f"{cls._new_users_label(users)}"
            )
        lines.extend([
            "",
            "[I]Считаются пользователи, завершившие вход в Консилиум в указанный день. "
            "В отчёте нет персональных и медицинских данных.[/I]",
        ])
        return "\n".join(lines)

    @classmethod
    async def send(cls, payload: dict) -> None:
        dialog_id = Config.BITRIX_MANAGER_REPORT_DIALOG_ID or str(
            payload.get("dialog_id") or ""
        )
        if not dialog_id:
            raise RuntimeError("Bitrix manager report dialog is not configured")
        await BitrixClient().call(
            "im.message.add",
            {
                "DIALOG_ID": dialog_id,
                "MESSAGE": cls.render(payload),
                "SYSTEM": "N",
                "URL_PREVIEW": "N",
            },
        )
