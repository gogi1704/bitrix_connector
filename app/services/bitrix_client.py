import ipaddress
from urllib.parse import urlparse

import httpx

from app.config import Config
from app.services.oauth import OAuthService


class BitrixApiError(Exception):

    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.payload = payload
        super().__init__(payload.get("error_description", "Bitrix API request failed"))


class BitrixClient:

    AUTH_ERROR_CODES = {"expired_token", "invalid_token", "NO_AUTH_FOUND"}

    def __init__(self):
        oauth = OAuthService()
        self.oauth = oauth
        self.domain = oauth.domain or Config.BITRIX_DOMAIN
        self.token = oauth.access_token or Config.BITRIX_ACCESS_TOKEN

    @property
    def base_url(self):
        if self.oauth.client_endpoint:
            endpoint = self.oauth.client_endpoint.rstrip("/")
        else:
            domain = self.domain.removeprefix("https://").removeprefix("http://").rstrip("/")
            endpoint = f"https://{domain}/rest"

        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise RuntimeError("Bitrix endpoint must be an authenticated HTTPS URL")

        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise RuntimeError("Bitrix endpoint must not target localhost")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise RuntimeError("Bitrix endpoint must use a public IP address")

        return f"{endpoint}/"

    async def _post(self, method: str, params: dict, token: str) -> httpx.Response:
        url = f"{self.base_url}{method}.json"

        async with httpx.AsyncClient(timeout=30) as client:
            return await client.post(url, json={"auth": token, **params})

    @classmethod
    def _token_expired(cls, response: httpx.Response, payload: dict) -> bool:
        return response.status_code == 401 or payload.get("error") in cls.AUTH_ERROR_CODES

    async def call(self, method: str, params: dict | None = None):

        if not self.domain or not self.token:
            raise RuntimeError("Bitrix domain or access token is not configured")

        params = params or {}

        response = await self._post(method, params, self.token)
        payload = response.json()

        if self._token_expired(response, payload):
            refreshed_auth = await self.oauth.refresh()
            self.token = refreshed_auth["access_token"]
            self.domain = refreshed_auth.get("domain", self.domain)
            response = await self._post(method, params, self.token)
            payload = response.json()

        # Some Bitrix methods report application-level failures in a JSON
        # payload even when the HTTP status itself is successful.
        if response.is_error or payload.get("error"):
            raise BitrixApiError(response.status_code, payload)

        return payload
