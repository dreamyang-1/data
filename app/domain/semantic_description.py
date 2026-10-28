"""Read the current platform-registered description; never use a local snapshot.

Platform uploads use semantic_model.semantic_desc_file_url. The existing
model_sync producer writes files/semantic_model_<id>.md without registering
that URL. Read either producer's actual file; registered locations take priority.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

# 平台库时间是本地时间，和文件服务返回的 GMT 时间比较前先补时区
_CATALOG_TZ_OFFSET_SECONDS = 8 * 3600
# 小于这个差值不判陈旧，避免两边时钟抖动误判
_STALE_TOLERANCE_SECONDS = 60


def _description_record(settings: Any, model_id: int) -> tuple[str, float | None]:
    """返回平台登记地址和模型更新时间（epoch 秒）；没有记录时返回空地址。"""
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
        read_timeout=None,
        write_timeout=None,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT semantic_desc_file_url, update_time FROM semantic_model "
                "WHERE id = %s AND is_deleted = 0 LIMIT 1",
                (model_id,),
            )
            row = cursor.fetchone()
            if not row:
                # Deleted/missing models may still have old files on storage.
                return "", None
            updated_at: float | None = None
            raw_time = row.get("update_time")
            if isinstance(raw_time, datetime):
                if raw_time.tzinfo is None:
                    # 库里存本地时间，按 UTC 求解再减时区偏移，避免依赖系统时区
                    updated_at = (
                        raw_time.replace(tzinfo=timezone.utc).timestamp()
                        - _CATALOG_TZ_OFFSET_SECONDS
                    )
                else:
                    updated_at = raw_time.timestamp()
            value = row.get("semantic_desc_file_url")
            if isinstance(value, str) and value.strip():
                return value.strip(), updated_at
            if value is not None and not isinstance(value, str):
                return "", updated_at
            # model_sync.refresh_model owns this filename contract. This is a
            # real remote document, not a static or synthesized catalog fallback.
            base = (getattr(settings, "semantic_description_base_url", "") or "").rstrip("/")
            return (
                f"{base}/files/semantic_model_{model_id}.md" if base else "",
                updated_at,
            )
    finally:
        connection.close()


def _description_url(settings: Any, model_id: int) -> str:
    """只需要地址时的兼容入口。"""
    return _description_record(settings, model_id)[0]


def _last_modified(headers: Any) -> float | None:
    """文件服务返回的 Last-Modified（GMT），缺失或格式异常返回 None。"""
    try:
        raw = headers.get("last-modified")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return parsedate_to_datetime(str(raw)).timestamp()
    except (TypeError, ValueError):
        return None


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


def _canonical_url(settings: Any, model_id: int) -> str:
    """描述文件生成服务固定的文件名，与平台登记值无关。"""
    base = (getattr(settings, "semantic_description_base_url", "") or "").rstrip("/")
    return f"{base}/files/semantic_model_{model_id}.md" if base else ""


def _candidate_urls(settings: Any, model_id: int) -> list[str]:
    """登记地址优先，生成服务的固定地址兜底；非信任来源直接剔除。"""
    candidates: list[str] = []
    registered = _description_url(settings, model_id)
    for url in (registered, _canonical_url(settings, model_id)):
        if url and _trusted_url(settings, url) and url not in candidates:
            candidates.append(url)
    return candidates


def _url_name(url: str) -> str:
    return url.rsplit("/", 1)[-1]


async def _fetch_document(settings: Any, url: str) -> tuple[str, float | None]:
    """加载一份描述文件正文；格式或体积不合法的内容按失败处理。"""
    maximum = getattr(settings, "semantic_description_max_bytes", 1024 * 1024)
    async with httpx.AsyncClient(
        timeout=None,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        async with client.stream("GET", url, headers={"Cache-Control": "no-cache"}) as response:
            response.raise_for_status()
            if "html" in response.headers.get("content-type", "").lower():
                raise ValueError("not a semantic document")
            modified_at = _last_modified(response.headers)
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > maximum:
                    raise ValueError("semantic document exceeds size limit")
                data.extend(chunk)
    content = data.decode("utf-8-sig").strip()
    if not content or "\x00" in content or content.lower().startswith(("<!doctype html", "<html")):
        raise ValueError("empty or invalid semantic document")
    return content, modified_at


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
        registered, updated_at = await asyncio.to_thread(
            _description_record, settings, semantic_model_id
        )
    except Exception as exc:
        logger.warning(
            "Semantic description URL resolve failed: model_id=%s error=%s",
            semantic_model_id, type(exc).__name__,
        )
        return reference
    canonical = _canonical_url(settings, semantic_model_id)
    candidates = [
        url for url in dict.fromkeys((registered, canonical))
        if url and _trusted_url(settings, url)
    ]
    if not candidates:
        logger.warning("Semantic description URL missing or untrusted: model_id=%s", semantic_model_id)
        return reference
    last_error: Exception | None = None
    stale_content = ""
    for index, url in enumerate(candidates):
        try:
            content, modified_at = await _fetch_document(settings, url)
        except Exception as exc:
            last_error = exc
            if index + 1 < len(candidates):
                # 平台登记的文件名可能过期或被删，换生成服务固定地址再试一次
                logger.warning(
                    "Semantic description fallback: model_id=%s file=%s error=%s",
                    semantic_model_id, _url_name(url), type(exc).__name__,
                )
            continue
        if (
            updated_at is not None
            and modified_at is not None
            and updated_at - modified_at > _STALE_TOLERANCE_SECONDS
            and url != canonical
            and canonical in candidates
        ):
            # 登记文件是模型改动前的旧快照，改读生成服务那份；拉不到再退回旧内容
            logger.warning(
                "Semantic description stale: model_id=%s file=%s modified=%s updated=%s",
                semantic_model_id, _url_name(url),
                int(modified_at), int(updated_at),
            )
            stale_content = content
            continue
        reference.update(available=True, content=content)
        return reference
    if stale_content:
        logger.warning(
            "Semantic description stale fallback used: model_id=%s", semantic_model_id
        )
        reference.update(available=True, content=stale_content)
        return reference
    # A catalog or storage outage is not missing user input; retain the
    # unavailable reference and never leak credentials/URLs/SQL in errors.
    logger.warning(
        "Semantic description unavailable: model_id=%s error=%s",
        semantic_model_id,
        type(last_error).__name__ if last_error else "no-url",
    )
    return reference
