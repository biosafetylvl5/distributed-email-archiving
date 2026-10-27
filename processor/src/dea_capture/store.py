"""Where derived files go: an S3 bucket (R2 in production) or a directory.

All state lives in the store, so every stage can be re-run safely.
``put(..., if_absent=True)`` is the claim primitive: on S3 it is a
conditional ``If-None-Match: *`` PUT, so two workers can't both win.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit


class Store(Protocol):
    def get(self, key: str) -> bytes | None: ...

    def exists(self, key: str) -> bool: ...

    def put(
        self, key: str, data: bytes, content_type: str, *, if_absent: bool = False,
        metadata: Mapping[str, str] | None = None,
    ) -> bool:  # fmt: skip
        """Write ``key``. With ``if_absent``, returns False instead of
        overwriting an existing object. ``metadata`` becomes the object's
        user metadata (R2 ``customMetadata``)."""
        ...


def put_json(
    store: Store, key: str, obj: object, *, if_absent: bool = False,
    metadata: Mapping[str, str] | None = None,
) -> bool:  # fmt: skip
    data = (json.dumps(obj, indent=2, sort_keys=False) + "\n").encode()
    return store.put(key, data, "application/json", if_absent=if_absent, metadata=metadata)


@dataclass
class LocalStore:
    root: Path

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"key escapes the store: {key}")
        return path

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def _meta_path(self, key: str) -> Path:
        return self._path(f".meta/{key}.json")

    def metadata(self, key: str) -> dict[str, str] | None:
        """What ``put`` was given as ``metadata``, kept beside the store."""
        path = self._meta_path(key)
        return json.loads(path.read_text()) if path.is_file() else None

    def put(
        self, key: str, data: bytes, content_type: str, *, if_absent: bool = False,
        metadata: Mapping[str, str] | None = None,
    ) -> bool:  # fmt: skip
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if if_absent:
            try:
                with path.open("xb") as f:
                    f.write(data)
            except FileExistsError:
                return False
        else:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        if metadata is not None:
            meta = self._meta_path(key)
            meta.parent.mkdir(parents=True, exist_ok=True)
            meta.write_text(json.dumps(dict(metadata), sort_keys=True))
        return True


@dataclass
class S3Store:
    """Credentials and endpoint come from boto3's usual chain, e.g.
    ``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY`` and ``AWS_ENDPOINT_URL``."""

    bucket: str
    client: Any = None

    def __post_init__(self) -> None:
        if self.client is None:
            import boto3

            self.client = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "auto"))

    def get(self, key: str) -> bytes | None:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
        except self.client.exceptions.NoSuchKey:
            return None
        body: bytes = obj["Body"].read()
        return body

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def put(
        self, key: str, data: bytes, content_type: str, *, if_absent: bool = False,
        metadata: Mapping[str, str] | None = None,
    ) -> bool:  # fmt: skip
        from botocore.exceptions import ClientError

        kwargs: dict[str, Any] = {
            "Bucket": self.bucket, "Key": key, "Body": data, "ContentType": content_type,
        }  # fmt: skip
        if metadata:
            kwargs["Metadata"] = dict(metadata)
        if if_absent:
            kwargs["IfNoneMatch"] = "*"
        try:
            self.client.put_object(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if if_absent and code in {"PreconditionFailed", "412"}:
                return False
            raise
        return True


def split_s3_uri(uri: str) -> tuple[str, str]:
    parts = urlsplit(uri)
    if parts.scheme != "s3" or not parts.netloc or not parts.path.strip("/"):
        raise ValueError(f"not an s3://bucket/key URI: {uri}")
    return parts.netloc, parts.path.lstrip("/")
