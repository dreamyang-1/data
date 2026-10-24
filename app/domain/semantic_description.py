"""Read the current platform-registered description; never use a local snapshot.

Platform uploads use semantic_model.semantic_desc_file_url. The existing
model_sync producer writes files/semantic_model_<id>.md without registering
that URL. Read either producer's actual file; registered locations take priority.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)


def without_embedded_semantic_spec(agent_prompt: str) -> str:
    """Keep role/style and general rules, excluding pasted model snapshots.

    Semantic documents come from the current model's platform registration.
    A configured prompt may predate a model switch; its explicit semantic
    section must not compete with that document, even when storage is down.
    """
    from app.services.general_rules import rule_section, without_rule_section

    rules = rule_section(agent_prompt)
    text = without_rule_section(agent_prompt)
    headings = list(re.finditer(r"(?m)^[ \t]*(#{1,6})[ \t]+([^\r\n]+)", text))
    ranges = []
    for index, heading in enumerate(headings):
        if heading.group(2).strip().strip("# ") != "业务语义规范":
            continue
        if ranges and heading.start() < ranges[-1][1]:
            continue
        end = next((other.start() for other in headings[index + 1:]
                    if len(other.group(1)) <= len(heading.group(1))), len(text))
        ranges.append((heading.start(), end))
    for start, end in reversed(ranges):
        text = text[:start] + text[end:]
    return text.rstrip() + rules if ranges else agent_prompt


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
            if not row:
                # Deleted/missing models may still have old files on storage.
                return ""
            value = row.get("semantic_desc_file_url")
            if isinstance(value, str) and value.strip():
                return value.strip()
            if value is not None and not isinstance(value, str):
                return ""
            # model_sync.refresh_model owns this filename contract. This is a
            # real remote document, not a static or synthesized catalog fallback.
            base = (getattr(settings, "semantic_description_base_url", "") or "").rstrip("/")
            return f"{base}/files/semantic_model_{model_id}.md" if base else ""
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
