import json
from pathlib import Path

from fastapi import APIRouter, Request

router = APIRouter(prefix="/bitrix", tags=["Bitrix"])


@router.post("/install")
async def install(request: Request):

    form = await request.form()

    data = dict(form)

    storage = Path("storage")
    storage.mkdir(exist_ok=True)

    with open(storage / "install.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

    print(data)

    return {"result": "ok"}