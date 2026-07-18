import ipaddress
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import Config


class MediaTransferError(RuntimeError):
    """A media file could not be transferred safely."""


class MediaTooLargeError(MediaTransferError):
    """A media file exceeds the connector limit."""


def safe_file_name(value: str | None, fallback: str = "attachment") -> str:
    name = Path((value or "").replace("\\", "/")).name
    name = re.sub(r"[^\w.()\- ]", "_", name, flags=re.UNICODE).strip(" .")
    return name[:180] or fallback


def media_type(file_name: str, content_type: str | None = None) -> str:
    content_type = (content_type or "").split(";", 1)[0].lower()
    suffix = Path(file_name).suffix.lower()
    if content_type.startswith("image/") or suffix in {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic"
    }:
        return "image"
    if content_type.startswith("video/") or suffix in {".mp4", ".mov", ".mkv", ".webm"}:
        return "video"
    if content_type.startswith("audio/") or suffix in {".mp3", ".wav", ".m4a", ".ogg"}:
        return "audio"
    return "file"


def attachment_metadata(attachments: list[dict]) -> list[dict]:
    """Keep audit metadata, never remote URLs or reusable media tokens."""
    result = []
    for attachment in attachments:
        payload = attachment.get("payload") or {}
        result.append(
            {
                "type": attachment.get("type") or "file",
                "name": safe_file_name(
                    attachment.get("name") or payload.get("name") or payload.get("filename")
                ),
                "size": attachment.get("size") or payload.get("size"),
                "status": attachment.get("status") or "forwarded",
            }
        )
    return result


def _find_url(value) -> str | None:
    if isinstance(value, dict):
        for key in ("url", "download_url", "downloadUrl"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith("https://"):
                return candidate
        for key in ("payload", "file", "photo", "photos", "video", "audio"):
            candidate = _find_url(value.get(key))
            if candidate:
                return candidate
    elif isinstance(value, list):
        # MAX often lists several photo sizes; the last/largest variant is preferable.
        for item in reversed(value):
            candidate = _find_url(item)
            if candidate:
                return candidate
    return None


def max_attachments_to_bitrix_files(attachments: list[dict]) -> list[dict]:
    files = []
    for index, attachment in enumerate(attachments, start=1):
        payload = attachment.get("payload") or {}
        declared_size = attachment.get("size") or payload.get("size")
        try:
            if declared_size and int(declared_size) > Config.MEDIA_MAX_BYTES:
                continue
        except (TypeError, ValueError):
            pass
        url = _find_url(attachment)
        if not url:
            continue
        kind = attachment.get("type") or "file"
        extension = {"image": ".jpg", "video": ".mp4", "audio": ".mp3"}.get(kind, "")
        name = safe_file_name(
            attachment.get("name")
            or payload.get("name")
            or payload.get("filename"),
            f"{kind}-{index}{extension}",
        )
        files.append({"url": url, "name": name})
    return files


def bitrix_files_from_form(form: dict) -> list[dict]:
    """Read flattened files[] fields from an ONIMCONNECTORMESSAGEADD callback."""
    pattern = re.compile(
        r"^data\[MESSAGES\]\[0\]\[message\]\[files\]\[(\d+)\]\[(url|urlDownload|DOWNLOAD_URL|name|size|type)\]$",
        re.IGNORECASE,
    )
    indexed: dict[int, dict] = {}
    for key, value in form.items():
        match = pattern.match(key)
        if match:
            field = match.group(2).lower()
            if field in {"urldownload", "download_url"}:
                field = "url"
            indexed.setdefault(int(match.group(1)), {})[field] = value
    return [indexed[index] for index in sorted(indexed) if indexed[index].get("url")]


def bitrix_file_ids_from_form(form: dict) -> list[str]:
    """Extract Bitrix Disk IDs from message params or DISK BBCode."""
    result = []
    for key, value in form.items():
        normalized_key = key.casefold()
        if "[message][params][file_id]" in normalized_key:
            result.extend(re.findall(r"\d+", str(value)))

    text = str(form.get("data[MESSAGES][0][message][text]", ""))
    result.extend(
        re.findall(r"\[(?:disk\s+)?file[^\]]*?id\s*=\s*[\"']?(\d+)", text, re.IGNORECASE)
    )
    return list(dict.fromkeys(result))


def bitrix_api_file(data: dict) -> dict | None:
    url = data.get("DOWNLOAD_URL") or data.get("urlDownload") or data.get("url")
    if not url:
        return None
    return {
        "url": url,
        "name": data.get("NAME") or data.get("name") or "attachment",
        "size": data.get("SIZE") or data.get("size"),
        "type": data.get("TYPE") or data.get("type") or "file",
    }


def attachment_field_names(form: dict) -> list[str]:
    """Return field names for diagnostics without leaking URLs or file contents."""
    markers = ("[file", "file_", "disk", "attach")
    return sorted(key for key in form if any(marker in key.casefold() for marker in markers))


def validate_remote_https_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise MediaTransferError("Media URL must be an authenticated HTTPS URL")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise MediaTransferError("Media URL must not target localhost")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise MediaTransferError("Media URL must use a public IP address")


async def download_to_temporary_file(url: str, suggested_name: str | None = None) -> tuple[Path, str, str]:
    validate_remote_https_url(url)
    temporary_path: Path | None = None
    try:
        async with httpx.AsyncClient(
            timeout=Config.MEDIA_DOWNLOAD_TIMEOUT,
            follow_redirects=True,
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                declared_size = int(response.headers.get("content-length") or 0)
                if declared_size > Config.MEDIA_MAX_BYTES:
                    raise MediaTooLargeError("Media file exceeds 50 MB")

                content_type = response.headers.get("content-type", "application/octet-stream")
                name = safe_file_name(suggested_name)
                if not Path(name).suffix:
                    extension = mimetypes.guess_extension(content_type.split(";", 1)[0]) or ""
                    name += extension

                descriptor, raw_path = tempfile.mkstemp(prefix="connector-media-", suffix=Path(name).suffix)
                temporary_path = Path(raw_path)
                total = 0
                with os.fdopen(descriptor, "wb") as output:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > Config.MEDIA_MAX_BYTES:
                            raise MediaTooLargeError("Media file exceeds 50 MB")
                        output.write(chunk)
        return temporary_path, name, content_type
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
