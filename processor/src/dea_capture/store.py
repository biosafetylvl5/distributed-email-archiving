"""Where derived files go: an S3 bucket (R2 in production) or a directory.

All state lives in the store, so every stage can be re-run safely.
``put(..., if_absent=True)`` is the claim primitive: on S3 it is a
conditional ``If-None-Match: *`` PUT, so two workers can't both win.
"""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Listed:
    """One object in a listing."""

    key: str
    size: int
    etag: str  # changes whenever the object is rewritten
    modified: datetime


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

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        """Write ``key`` from a file, without holding it in memory."""
        ...

    def delete(self, key: str) -> None:
        """Remove ``key``; a missing key is not an error."""
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

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
        self._meta_path(key).unlink(missing_ok=True)

    def _meta_path(self, key: str) -> Path:
        return self._path(f".meta/{key}.json")

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)

    def metadata(self, key: str) -> dict[str, str] | None:
        """What ``put`` was given as ``metadata``, kept beside the store."""
        path = self._meta_path(key)
        if path.is_file():
            got: dict[str, str] = json.loads(path.read_text())
            return got
        return {} if self.exists(key) else None

    def listing(self, prefix: str) -> list[Listed]:
        root = self.root.resolve()
        found = []
        for path in sorted(root.rglob("*")):
            key = path.relative_to(root).as_posix()
            if not path.is_file() or not key.startswith(prefix) or key.startswith(".meta/"):
                continue
            st = path.stat()
            found.append(Listed(key, st.st_size, f"{st.st_mtime_ns}-{st.st_size}",
                                datetime.fromtimestamp(st.st_mtime, UTC)))  # fmt: skip
        return found

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

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        from boto3.s3.transfer import TransferConfig

        # multipart in 64 MiB parts, two at a time: about 128 MiB in memory
        config = TransferConfig(multipart_threshold=64 << 20, multipart_chunksize=64 << 20,
                                max_concurrency=2)  # fmt: skip
        self.client.upload_file(str(path), self.bucket, key, Config=config,
                                ExtraArgs={"ContentType": content_type})  # fmt: skip

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def metadata(self, key: str) -> dict[str, str] | None:
        """The object's user metadata (R2 ``customMetadata``), or None if it's gone."""
        from botocore.exceptions import ClientError

        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return dict(head.get("Metadata") or {})

    def listing(self, prefix: str) -> list[Listed]:
        found = []
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix
        ):
            for o in page.get("Contents", []):
                found.append(Listed(o["Key"], o["Size"], o["ETag"], o["LastModified"]))
        return found


# types servers still send that the mimetypes table doesn't know
_LEGACY_TYPES = {
    "application/font-woff": ".woff",
    "application/x-font-woff": ".woff",
    "application/font-woff2": ".woff2",
    "application/x-font-ttf": ".ttf",
    "application/x-font-truetype": ".ttf",
    "application/x-font-otf": ".otf",
    "application/x-font-opentype": ".otf",
}
# types that say nothing about the body: the URL's extension is better
_GENERIC_TYPES = frozenset(
    {"application/octet-stream", "binary/octet-stream", "application/unknown"}
)


def ext_for(content_type: str | None, url: str) -> str:
    """A file extension for a body, from its type, else from its URL."""
    base = (content_type or "").split(";")[0].strip().lower()
    from_url = mimetypes.guess_extension(mimetypes.guess_type(urlsplit(url).path)[0] or "")
    if base in _GENERIC_TYPES and from_url:
        ext = from_url
    else:
        from_type = _LEGACY_TYPES.get(base) or (mimetypes.guess_extension(base) if base else None)
        ext = from_type or from_url or ""
    return {".jpe": ".jpg", ".jpeg": ".jpg"}.get(ext, ext)


def split_s3_uri(uri: str) -> tuple[str, str]:
    parts = urlsplit(uri)
    if parts.scheme != "s3" or not parts.netloc or not parts.path.strip("/"):
        raise ValueError(f"not an s3://bucket/key URI: {uri}")
    return parts.netloc, parts.path.lstrip("/")
