from fastapi import FastAPI
from app.routes.bitrix import router as bitrix_router

app = FastAPI(
    title="Bitrix Connector",
    version="0.1"
)

app.include_router(bitrix_router)


@app.get("/")
def home():
    return {
        "status": "ok",
        "message": "Bitrix Connector работает"
    }