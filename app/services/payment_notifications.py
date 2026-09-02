"""Delivery of verified Consilium payment notifications to a Bitrix chat."""

from app.config import Config
from app.services.bitrix_client import BitrixClient


class PaymentNotificationService:
    """Render and send a single payment notification queued by Consilium."""

    @staticmethod
    def _money(kopecks: int, currency: str) -> str:
        value = int(kopecks)
        amount = f"{value // 100:,}.{value % 100:02d}".replace(",", " ")
        return f"{amount} ₽" if currency == "RUB" else f"{amount} {currency}"

    @classmethod
    def render(cls, payload: dict) -> str:
        items = payload.get("items") or []
        item_lines = [
            f"• {item['name']} — {cls._money(item['amount_kopecks'], payload['currency'])}"
            for item in items
        ]
        lines = [
            "[B]✅ Оплата подтверждена[/B]",
            "",
            f"[B]Клиент:[/B] {payload.get('client_name') or 'Не указано'}",
            f"[B]ИНН:[/B] {payload.get('company_inn') or 'Не указан'}",
            f"[B]Организация:[/B] {payload.get('organization_name') or 'Не определена'}",
            f"[B]Бригада:[/B] {payload.get('brigade') or 'Не определена'}",
            f"[B]Дата медосмотра:[/B] {payload.get('examination_date') or 'Не определена'}",
            "",
            f"[B]Заказ Консилиума:[/B] {payload['order_id']}",
            f"[B]Платёж ЮKassa:[/B] {payload['provider_payment_id']}",
            f"[B]Статус:[/B] {payload['status']}",
            f"[B]Сумма:[/B] {cls._money(payload['amount_kopecks'], payload['currency'])}",
            f"[B]Дата оплаты:[/B] {payload.get('paid_at') or 'Не указана'}",
            f"[B]Создан в ЮKassa:[/B] {payload.get('provider_created_at') or 'Не указано'}",
            f"[B]Способ оплаты:[/B] {payload.get('payment_method') or 'Не указан'}",
            f"[B]Описание ЮKassa:[/B] {payload.get('provider_description') or 'Не указано'}",
            f"[B]Тестовый платёж:[/B] {'да' if payload.get('test') else 'нет'}",
            "",
            "[B]Состав заказа:[/B]",
            *(item_lines or ["• Состав не указан"]),
        ]
        return "\n".join(lines)

    @classmethod
    async def send(cls, payload: dict) -> None:
        if not Config.BITRIX_PAYMENT_DIALOG_ID:
            raise RuntimeError("BITRIX_PAYMENT_DIALOG_ID is not configured")
        await BitrixClient().call(
            "im.message.add",
            {
                "DIALOG_ID": Config.BITRIX_PAYMENT_DIALOG_ID,
                "MESSAGE": cls.render(payload),
                "SYSTEM": "N",
                "URL_PREVIEW": "N",
            },
        )
