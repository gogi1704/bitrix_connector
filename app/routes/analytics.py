import html
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import Config
from app.storage.database import MessageDatabase


router = APIRouter(prefix="/analytics", tags=["Analytics"])
basic = HTTPBasic(auto_error=True)


def require_analytics_auth(
    credentials: HTTPBasicCredentials = Depends(basic),
) -> None:
    if not Config.CONNECTOR_ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Connector admin token is not configured")
    username_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"), b"admin"
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        Config.CONNECTOR_ADMIN_TOKEN.encode("utf-8"),
    )
    if not username_ok or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid analytics credentials",
            headers={"WWW-Authenticate": 'Basic realm="Connector analytics"'},
        )


def _card(label: str, value) -> str:
    return (
        '<section class="card"><span>'
        + html.escape(label)
        + "</span><strong>"
        + html.escape(str(value))
        + "</strong></section>"
    )


@router.get("", include_in_schema=False)
def analytics_redirect():
    return RedirectResponse(url="/analytics/", status_code=307)


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_analytics_auth)])
def analytics_dashboard():
    snapshot = MessageDatabase().analytics_snapshot()
    cards = "".join(
        (
            _card("Всего диалогов", snapshot["dialogs"]["total"]),
            _card("Активных диалогов", snapshot["dialogs"]["active"]),
            _card("Сообщений за 24 часа", snapshot["messages"]["last_24h"]),
            _card("Архивов за 7 дней", snapshot["archives"]["last_7d"]),
            _card("Всего архивов", snapshot["archives"]["total"]),
            _card("Среднее сообщений в архиве", snapshot["archives"]["average_messages"]),
            _card("Задач на повторе", snapshot["queue"].get("retry", 0)),
            _card("Окончательных ошибок", snapshot["queue"].get("failed", 0)),
        )
    )
    tags = "".join(
        f"<tr><td>{html.escape(item['tag'])}</td><td>{item['count']}</td></tr>"
        for item in snapshot["tags"]
    ) or '<tr><td colspan="2">Тегов пока нет</td></tr>'
    reminders = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{count}</td></tr>"
        for name, count in sorted(snapshot["reminders"].items())
    ) or '<tr><td colspan="2">Напоминаний пока нет</td></tr>'
    failed = "".join(
        "<tr>"
        f"<td>{item['id']}</td>"
        f"<td>{html.escape(item['job_type'])}</td>"
        f"<td>{item['attempts']}</td>"
        f"<td>{html.escape(str(item['updated_at']))}</td>"
        '<td><form method="post" action="/analytics/jobs/'
        f'{item["id"]}/retry"><button type="submit">Повторить</button></form></td>'
        "</tr>"
        for item in snapshot["recent_failed_jobs"]
    ) or '<tr><td colspan="5">Окончательных ошибок нет</td></tr>'
    refresh = max(15, Config.ANALYTICS_REFRESH_SECONDS)
    body = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{refresh}">
<title>Аналитика коннектора</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;background:#f4f7fb;color:#172033}}
main{{max-width:1100px;margin:auto;padding:28px 18px 60px}}
h1{{margin:0 0 6px}} .hint{{color:#667085;margin:0 0 24px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}
.card{{background:white;border:1px solid #e5e9f2;border-radius:14px;padding:18px;display:flex;flex-direction:column;gap:8px}}
.card span{{color:#667085;font-size:14px}} .card strong{{font-size:28px}}
.tables{{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:22px}}
.panel{{background:white;border:1px solid #e5e9f2;border-radius:14px;padding:18px;overflow:auto}}
.wide{{grid-column:1/-1}} table{{width:100%;border-collapse:collapse}} th,td{{padding:10px;border-bottom:1px solid #eef1f5;text-align:left}}
button{{border:0;border-radius:8px;background:#315efb;color:white;padding:8px 12px;cursor:pointer}}
@media(max-width:700px){{.tables{{grid-template-columns:1fr}}.wide{{grid-column:auto}}}}
</style></head><body><main>
<h1>Аналитика коннектора</h1>
<p class="hint">Только агрегаты, без текстов переписки. Обновление каждые {refresh} сек.</p>
<div class="cards">{cards}</div>
<div class="tables">
<section class="panel"><h2>Теги клиентов</h2><table><tr><th>Тег</th><th>Клиентов</th></tr>{tags}</table></section>
<section class="panel"><h2>Напоминания</h2><table><tr><th>Состояние</th><th>Количество</th></tr>{reminders}</table></section>
<section class="panel wide"><h2>Очередь окончательных ошибок</h2><table><tr><th>ID</th><th>Тип</th><th>Попыток</th><th>Обновлено</th><th></th></tr>{failed}</table></section>
</div></main></body></html>"""
    response = HTMLResponse(body)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'"
    )
    response.headers["X-Frame-Options"] = "DENY"
    return response


@router.get("/data", dependencies=[Depends(require_analytics_auth)])
def analytics_data():
    return MessageDatabase().analytics_snapshot()


@router.post("/jobs/{job_id}/retry", dependencies=[Depends(require_analytics_auth)])
def retry_failed_job(job_id: int):
    if not MessageDatabase().retry_failed_job(job_id):
        raise HTTPException(status_code=409, detail="Failed job is unavailable for retry")
    return RedirectResponse(url="/analytics/", status_code=303)
