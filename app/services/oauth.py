import json
from pathlib import Path

from app.config import DATA_DIR


class OAuthService:

    STORAGE = DATA_DIR / "oauth.json"
    TOKEN_URL = "https://oauth.bitrix24.tech/oauth/token/"

    def save(self, data: dict):

        self.STORAGE.parent.mkdir(exist_ok=True)

        with open(self.STORAGE, "w", encoding="utf8") as f:
            json.dump(
                data,
                f,
                indent=4,
                ensure_ascii=False
            )

    def load(self):

        if not self.STORAGE.exists():
            return {}

        with open(self.STORAGE, encoding="utf8") as f:
            return json.load(f)

    @property
    def access_token(self):

        data = self.load()
        return data.get("access_token") or data.get("AUTH_ID")

    @property
    def refresh_token(self):

        data = self.load()
        return data.get("refresh_token") or data.get("REFRESH_ID")

    @property
    def domain(self):

        data = self.load()
        return data.get("domain") or data.get("DOMAIN")

    @property
    def member_id(self):

        return self.load().get("member_id")

    @property
    def client_endpoint(self):

        return self.load().get("client_endpoint")

    async def refresh(self) -> dict:
        """Request and persist a new OAuth token pair from Bitrix24."""
        from app.config import Config
        import httpx

        auth = self.load()
        refresh_token = auth.get("refresh_token") or auth.get("REFRESH_ID")

        if not refresh_token:
            raise RuntimeError("Bitrix refresh token is not configured")
        if not Config.CLIENT_ID or not Config.CLIENT_SECRET:
            raise RuntimeError("Bitrix CLIENT_ID or CLIENT_SECRET is not configured")

        params = {
            "grant_type": "refresh_token",
            "client_id": Config.CLIENT_ID,
            "client_secret": Config.CLIENT_SECRET,
            "refresh_token": refresh_token,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(self.TOKEN_URL, params=params)
            response.raise_for_status()
            refreshed_auth = response.json()

        if refreshed_auth.get("error"):
            raise RuntimeError(
                "Bitrix token refresh failed: "
                f"{refreshed_auth.get('error_description', refreshed_auth['error'])}"
            )

        self.save({**auth, **refreshed_auth})
        return refreshed_auth
