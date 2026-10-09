"""Read the current platform-registered description; never use a local snapshot.

The platform download menu uses semantic_model.semantic_desc_file_url. File
names and storage locations are platform metadata, not derived from model IDs.
Only the logical reference label uses the ID (for existing prompt consumers).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)


def _description_url(settings: Any, model_id: int) -> str:
    """Fresh, parameterized, read-only lookup in the platform catalog."""
    import pymysql

    password = settings.mysql_password
    if hasattr(password, "get_secret_value"):
        password = password.get_secret_value()
    connection = pymysql.connect(
        host=settings.mysql_host,
        port=getattr(settings, "mysql_port", 3306),
        user=settings.mysql_user,
        password=password,
        database=settings.mysql_database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=getattr(settings, "mysql_connect_timeout_seconds", 5),
        read_timeout=getattr(settings, "mysql_read_timeout_seconds", 10),
        write_timeout=getattr(settings, "mysql_write_timeout_seconds", 10),
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT semantic_desc_file_url FROM semantic_model "
                "WHERE id = %s AND is_deleted = 0 LIMIT 1",
                (model_id,),
            )
            row = cursor.fetchone()
            value = row.get("semantic_desc_file_url") if row else None
            return value.strip() if isinstance(value, str) else ""
    finally:
        connection.close()


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment):
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (
            443 if parsed.scheme == "https" else 80
        )
    except ValueError:
        return None


def _trusted_url(settings: Any, url: str) -> bool:
    # Download URLs are database-owned, not accepted from questions/history.
    # Restrict their origin to configured description storage or MinIO, without
    # logging signed URLs. Do not follow redirects to another origin.
    trusted = {getattr(settings, "semantic_description_base_url", "") or ""}
    endpoint = getattr(settings, "minio_endpoint", None)
    if endpoint:
        if "://" not in endpoint:
            scheme = "https" if getattr(settings, "minio_secure", False) else "http"
            endpoint = f"{scheme}://{endpoint}"
        trusted.add(endpoint)
    origin = _origin(url)
    return origin is not None and origin in {_origin(item) for item in trusted}


async def load_semantic_description(
    settings: Any, semantic_model_id: int | None
) -> dict[str, Any]:
    """Every use re-resolves metadata and fetches the file, including after failure.

    Unavailable reference material does not replace the authorized ASL catalog
    and must never cause another model's local business spec to be injected.
    The existing reference shape is preserved for planning and synthesis.
    """
    valid_id = type(semantic_model_id) is int and semantic_model_id > 0
    reference = {
        "source": f"semantic_model_{semantic_model_id}.md" if valid_id else "",
        "available": False,
        "content": "",
    }
    if not valid_id:
        return reference
    if not all(getattr(settings, name, None) for name in (
        "mysql_host", "mysql_user", "mysql_password", "mysql_database"
    )):
        logger.warning("Semantic description catalog is not configured: model_id=%s", semantic_model_id)
        return reference
    try:
        url = await asyncio.to_thread(_description_url, settings, semantic_model_id)
        if not url or not _trusted_url(settings, url):
            logger.warning("Semantic description URL missing or untrusted: model_id=%s", semantic_model_id)
            return reference
        maximum = getattr(settings, "semantic_description_max_bytes", 1024 * 1024)
        async with httpx.AsyncClient(
            timeout=getattr(settings, "semantic_description_timeout_seconds", 3.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream("GET", url, headers={"Cache-Control": "no-cache"}) as response:
                response.raise_for_status()
                if "html" in response.headers.get("content-type", "").lower():
                    raise ValueError("not a semantic document")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > maximum:
                        raise ValueError("semantic document exceeds size limit")
                    data.extend(chunk)
        content = data.decode("utf-8-sig").strip()
        if not content or "\x00" in content or content.lower().startswith(("<!doctype html", "<html")):
            raise ValueError("empty or invalid semantic document")
        reference.update(available=True, content=content)
    except Exception as exc:
        # A catalog or storage outage is not missing user input; retain the
        # unavailable reference and never leak credentials/URLs/SQL in errors.
        logger.warning("Semantic description unavailable: model_id=%s error=%s", semantic_model_id, type(exc).__name__)
    return reference
