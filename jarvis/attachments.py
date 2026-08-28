from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


MAX_IMAGE_ATTACHMENTS = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
ALLOWED_IMAGE_MIME_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})


def _matches_image_signature(data: bytes, mime: str) -> bool:
    if mime == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if mime == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def _normalized_mime(value: Any) -> str:
    mime = str(value or "").strip().casefold()
    if mime == "image/jpg":
        mime = "image/jpeg"
    if mime not in ALLOWED_IMAGE_MIME_TYPES:
        allowed = ", ".join(sorted(ALLOWED_IMAGE_MIME_TYPES))
        raise ValueError(f"Image type is not supported; allowed types: {allowed}")
    return mime


@dataclass(frozen=True)
class ImageAttachment:
    mime: str
    data: bytes = field(repr=False)
    name: str = "image"

    def __post_init__(self) -> None:
        mime = _normalized_mime(self.mime)
        data = bytes(self.data)
        if not data:
            raise ValueError("Image attachment is empty")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(
                f"Image attachment exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MiB limit"
            )
        if not _matches_image_signature(data, mime):
            raise ValueError("Image content does not match its declared type")
        safe_name = re.sub(r"[^A-Za-z0-9._ -]", "_", str(self.name or "image"))[:120]
        object.__setattr__(self, "mime", mime)
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "name", safe_name or "image")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def descriptor(self) -> dict[str, Any]:
        return {
            "mime": self.mime,
            "bytes": len(self.data),
            "sha256": self.sha256,
        }

    def content_part(self) -> dict[str, str]:
        return {
            "type": "image",
            "mime": self.mime,
            "data": base64.b64encode(self.data).decode("ascii"),
        }

    @classmethod
    def from_payload(cls, value: Any) -> "ImageAttachment":
        if not isinstance(value, dict):
            raise ValueError("Image attachment must be an object")
        mime = _normalized_mime(value.get("mime"))
        encoded = value.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("Image attachment data is missing")
        # Reject an oversized base64 body before allocating the decoded bytes.
        max_encoded = ((MAX_IMAGE_BYTES + 2) // 3) * 4
        if len(encoded) > max_encoded:
            raise ValueError(
                f"Image attachment exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MiB limit"
            )
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("Image attachment is not valid base64") from None
        return cls(mime, data, str(value.get("name") or "image"))

    @classmethod
    def from_path(cls, value: str | os.PathLike[str]) -> "ImageAttachment":
        path = Path(value).expanduser()
        try:
            details = path.stat()
        except OSError as exc:
            raise ValueError(f"Image cannot be read: {path}") from exc
        if not path.is_file():
            raise ValueError(f"Image path is not a regular file: {path}")
        if details.st_size > MAX_IMAGE_BYTES:
            raise ValueError(
                f"Image attachment exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MiB limit: {path.name}"
            )
        mime = _normalized_mime(mimetypes.guess_type(path.name)[0])
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"Image cannot be read: {path}") from exc
        return cls(mime, data, path.name)


def validate_image_attachments(values: Iterable[ImageAttachment | dict[str, Any]] | None) -> tuple[ImageAttachment, ...]:
    if values is None:
        return ()
    raw = list(values)
    if len(raw) > MAX_IMAGE_ATTACHMENTS:
        raise ValueError(f"A turn may contain at most {MAX_IMAGE_ATTACHMENTS} images")
    attachments: list[ImageAttachment] = []
    for value in raw:
        attachments.append(
            value if isinstance(value, ImageAttachment) else ImageAttachment.from_payload(value)
        )
    return tuple(attachments)


def attachment_descriptors_json(values: Iterable[ImageAttachment]) -> str:
    return json.dumps(
        [item.descriptor() for item in values],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def inspect_image_attachment(value: ImageAttachment) -> dict[str, Any]:
    """Decode enough of an image to produce bounded visual-QA metadata.

    This is deliberately separate from basic transport validation so runtimes
    without Pillow can still accept supported image payloads.  Callers that
    intend to reason over pixels should run this check first.
    """
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise RuntimeError(
            "Visual inspection requires Pillow; install the documents extra"
        ) from exc

    import io

    try:
        with Image.open(io.BytesIO(value.data)) as image:
            width, height = (int(part) for part in image.size)
            frames = int(getattr(image, "n_frames", 1))
            image_format = str(image.format or "").casefold()
            if width <= 0 or height <= 0:
                raise ValueError("Image dimensions are invalid")
            pixels = width * height
            if pixels > MAX_IMAGE_PIXELS:
                raise ValueError(
                    f"Image exceeds the {MAX_IMAGE_PIXELS:,}-pixel visual limit"
                )
            expected_format = {
                "image/png": "png",
                "image/jpeg": "jpeg",
                "image/gif": "gif",
                "image/webp": "webp",
            }[value.mime]
            if image_format != expected_format:
                raise ValueError("Decoded image type does not match its declared type")
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("Image could not be decoded safely") from exc
    return {
        **value.descriptor(),
        "width": width,
        "height": height,
        "pixels": pixels,
        "frames": frames,
        "animated": frames > 1,
    }
