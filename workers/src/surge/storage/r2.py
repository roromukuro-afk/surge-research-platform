"""Cloudflare R2 through the S3 API, with the overwrite path closed.

R2 has no object versioning, so a PutObject that replaced an existing key would
destroy the previous bytes with nothing to recover them from. Every write here
therefore carries ``If-None-Match: *``, which makes the store itself refuse the
overwrite rather than relying on our key convention being followed. When the
object is already there R2 answers 412 and we verify, by digest, that what is
there is what we were about to write.

Credentials come from the environment and from nowhere else. They are never
logged, never written to the repository, and never passed to the database - the
database's only route to an object is a short lived signed URL.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass

from surge.storage.base import (
    ImmutableObjectConflict,
    ObjectStore,
    ObjectStoreError,
    StoredObject,
    sha256_hex,
)

# R2's S3 endpoint takes no meaningful region; "auto" is what Cloudflare documents.
R2_REGION = "auto"


@dataclass(frozen=True)
class R2Settings:
    account_id: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    endpoint_url: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> R2Settings:
        source = env if env is not None else dict(os.environ)
        missing = [
            name
            for name in ("SURGE_R2_ACCOUNT_ID", "SURGE_R2_BUCKET", "SURGE_R2_ACCESS_KEY_ID", "SURGE_R2_SECRET_ACCESS_KEY")
            if not source.get(name)
        ]
        if missing:
            # Name the variables, never their values.
            raise ObjectStoreError(
                "R2 is not configured; set " + ", ".join(missing) + " in the worker's environment"
            )
        account_id = source["SURGE_R2_ACCOUNT_ID"]
        return cls(
            account_id=account_id,
            bucket=source["SURGE_R2_BUCKET"],
            access_key_id=source["SURGE_R2_ACCESS_KEY_ID"],
            secret_access_key=source["SURGE_R2_SECRET_ACCESS_KEY"],
            endpoint_url=source.get("SURGE_R2_ENDPOINT") or f"https://{account_id}.r2.cloudflarestorage.com",
        )


class R2ObjectStore(ObjectStore):
    def __init__(self, settings: R2Settings, *, store_id: str | None = None, client=None) -> None:
        self._settings = settings
        self.store_id = store_id or f"r2:{settings.bucket}"
        self._client = client or self._build_client(settings)

    @staticmethod
    def _build_client(settings: R2Settings):
        try:
            import boto3  # noqa: PLC0415 - optional dependency, only needed when R2 is used
            from botocore.config import Config  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on the install extra
            raise ObjectStoreError(
                "boto3 is required for the R2 store; install the 'r2' extra"
            ) from exc

        return boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key,
            region_name=R2_REGION,
            config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> R2ObjectStore:
        return cls(R2Settings.from_env(env))

    # ------------------------------------------------------------------ write
    def put_immutable(self, key: str, data: bytes, content_type: str) -> StoredObject:
        digest = sha256_hex(data)
        try:
            self._client.put_object(
                Bucket=self._settings.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                # The store refuses the overwrite; we do not merely avoid it.
                IfNoneMatch="*",
                Metadata={"sha256": digest},
            )
        except Exception as exc:  # noqa: BLE001 - botocore's ClientError is not importable without boto3
            if not _is_precondition_failure(exc):
                raise
            existing = self.head(key)
            if existing is None:
                raise ObjectStoreError(
                    f"{key} was refused as already present but cannot be read back"
                ) from exc
            if existing.sha256 != digest:
                raise ImmutableObjectConflict(
                    f"{key} already exists with different content; write a new key instead of overwriting"
                ) from exc
            return StoredObject(key, self.store_id, digest, existing.bytes, content_type, created=False)

        return StoredObject(key, self.store_id, digest, len(data), content_type, created=True)

    # ------------------------------------------------------------------- read
    def get(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._settings.bucket, Key=key)
        return response["Body"].read()

    def head(self, key: str) -> StoredObject | None:
        try:
            response = self._client.head_object(Bucket=self._settings.bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - see put_immutable
            if _is_not_found(exc):
                return None
            raise
        metadata = {k.lower(): v for k, v in (response.get("Metadata") or {}).items()}
        digest = metadata.get("sha256")
        if not digest:
            # Objects written before the metadata convention, or by another tool:
            # read them back rather than trusting an ETag, which is not a SHA-256.
            digest = sha256_hex(self.get(key))
        return StoredObject(
            key,
            self.store_id,
            digest,
            int(response.get("ContentLength", 0)),
            response.get("ContentType", "application/octet-stream"),
            created=False,
        )

    def list(self, prefix: str) -> Iterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._settings.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield item["Key"]

    def delete(self, key: str) -> bool:
        if self.head(key) is None:
            return False
        self._client.delete_object(Bucket=self._settings.bucket, Key=key)
        return True

    def presign_get(self, key: str, expires_seconds: int = 900) -> str:
        """A short lived URL, which is the only form of access the database gets."""

        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._settings.bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        return str(error.get("Code", ""))
    return ""


def _status_code(exc: Exception) -> int:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return int((response.get("ResponseMetadata") or {}).get("HTTPStatusCode", 0))
    return 0


def _is_precondition_failed(exc: Exception) -> bool:
    return _status_code(exc) == 412 or _error_code(exc) == "PreconditionFailed"


def _is_precondition_failure(exc: Exception) -> bool:
    # S3 semantics return 412 for a failed If-None-Match. Some implementations
    # answer 409 Conflict on a concurrent write to the same key instead; both
    # mean "it is already there", and neither may become an overwrite.
    return _is_precondition_failed(exc) or _status_code(exc) == 409 or _error_code(exc) in {
        "ConditionalRequestConflict",
        "OperationAborted",
    }


def _is_not_found(exc: Exception) -> bool:
    return _status_code(exc) == 404 or _error_code(exc) in {"404", "NoSuchKey", "NotFound"}
