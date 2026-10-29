# -*- coding: utf-8 -*-
"""boto3 实现的 S3 客户端适配器。

对外暴露与 minio SDK 一致的方法签名（bucket_exists / put_object /
get_object / stat_object / remove_object / presigned_get_object /
fput_object / make_bucket），上层 MinioFollowupStore 无需改动。
异常统一包装为带 code 属性的 S3Error，保持 minio S3Error 的判断方式。
"""

from __future__ import annotations

import os
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


# 404 场景下 botocore 返回 "404"，这里统一映射成 minio 风格的错误码，
# 上层按 {"NoSuchKey", "NoSuchObject", "NoSuchBucket"} 判断缺失。
_MISSING_CODES = {"404", "NoSuchKey", "NoSuchObject", "NoSuchBucket"}


class S3Error(Exception):
    """模拟 minio.error.S3Error 的 code 属性。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class S3Client:
    """S3 客户端，方法签名对齐 minio.Minio。"""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        secure: bool = False,
    ) -> None:
        if "://" not in endpoint:
            endpoint = f"{'https' if secure else 'http'}://{endpoint}"
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=os.getenv("DATA_AGENT_S3_REGION", "us-east-1"),
            config=Config(
                s3={"addressing_style": "path"},
                signature_version="s3v4",
                retries={"max_attempts": 3},
            ),
        )

    @staticmethod
    def _error(exc: ClientError, action: str) -> S3Error:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in _MISSING_CODES:
            return S3Error("NoSuchKey", f"object missing during {action}: {exc}")
        return S3Error(code or "S3Error", f"{action} failed: {exc}")

    def bucket_exists(self, bucket_name: str) -> bool:
        try:
            self._s3.head_bucket(Bucket=bucket_name)
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _MISSING_CODES:
                return False
            raise self._error(exc, "head_bucket") from exc

    def make_bucket(self, bucket_name: str) -> None:
        try:
            self._s3.create_bucket(Bucket=bucket_name)
        except ClientError as exc:
            raise self._error(exc, "create_bucket") from exc

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: Any,
        length: int,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        body = data.read() if hasattr(data, "read") else data
        kwargs: dict[str, Any] = {"Bucket": bucket_name, "Key": object_name, "Body": body}
        if content_type:
            kwargs["ContentType"] = content_type
        if metadata:
            kwargs["Metadata"] = {str(k): str(v) for k, v in metadata.items()}
        try:
            self._s3.put_object(**kwargs)
        except ClientError as exc:
            raise self._error(exc, "put_object") from exc

    def fput_object(
        self,
        bucket_name: str,
        object_name: str,
        file_path: str,
        content_type: str | None = None,
    ) -> None:
        with open(file_path, "rb") as handle:
            self.put_object(bucket_name, object_name, handle, os.path.getsize(file_path), content_type)

    def get_object(self, bucket_name: str, object_name: str) -> Any:
        try:
            return self._s3.get_object(Bucket=bucket_name, Key=object_name)["Body"]
        except ClientError as exc:
            raise self._error(exc, "get_object") from exc

    def stat_object(self, bucket_name: str, object_name: str) -> Any:
        try:
            info = self._s3.head_object(Bucket=bucket_name, Key=object_name)
        except ClientError as exc:
            raise self._error(exc, "stat_object") from exc
        return SimpleNamespace(
            size=info.get("ContentLength", 0),
            etag=info.get("ETag", ""),
            last_modified=info.get("LastModified"),
            content_type=info.get("ContentType"),
            metadata=info.get("Metadata", {}),
        )

    def remove_object(self, bucket_name: str, object_name: str) -> None:
        try:
            self._s3.delete_object(Bucket=bucket_name, Key=object_name)
        except ClientError as exc:
            raise self._error(exc, "delete_object") from exc

    def presigned_get_object(
        self,
        bucket_name: str,
        object_name: str,
        expires: timedelta = timedelta(hours=1),
    ) -> str:
        try:
            return self._s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket_name, "Key": object_name},
                ExpiresIn=int(expires.total_seconds()),
            )
        except ClientError as exc:
            raise self._error(exc, "presign") from exc
