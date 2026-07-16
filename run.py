import uvicorn

from app.config import Config

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=Config.APP_HOST,
        port=Config.APP_PORT,
        reload=True
    )
