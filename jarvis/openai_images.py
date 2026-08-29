from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import mimetypes
import os
import secrets
import stat
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .attachments import MAX_IMAGE_BYTES

MODEL = "gpt-image-2"
API_ROOT = "https://api.openai.com/v1"
MAX_PROMPT_CHARS = 32_000
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = MAX_IMAGE_BYTES
MAX_RESPONSE_BYTES = ((MAX_OUTPUT_BYTES + 2) // 3) * 4 + 256 * 1024
MAX_IMAGE_PIXELS = 40_000_000
ALLOWED_FORMATS = frozenset({"png", "jpeg", "webp"})
ALLOWED_SIZES = frozenset({"auto", "1024x1024", "1024x1536", "1536x1024"})
ALLOWED_QUALITIES = frozenset({"auto", "low", "medium", "high"})
_MIME_BY_FORMAT = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}


class OpenAIImageError(RuntimeError):
    """Sanitized provider failure."""


class OpenAIImageConfigurationError(OpenAIImageError):
    """Raised when the provider has no usable API key."""


class OpenAIImageValidationError(OpenAIImageError, ValueError):
    """Raised before an invalid local or remote operation is attempted."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep provider credentials bound to the configured OpenAI origin."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str,
        headers: Any, newurl: str,
    ) -> None:
        return None


def _is_link(details: os.stat_result) -> bool:
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _inside_workspace(workspace: Path, value: str | os.PathLike[str]) -> Path:
    raw = Path(value)
    lexical = Path(os.path.abspath(raw if raw.is_absolute() else workspace / raw))
    try:
        relative = lexical.relative_to(workspace)
    except ValueError:
        raise OpenAIImageValidationError("Image paths must stay inside the workspace") from None
    current = workspace
    for part in relative.parts:
        current /= part
        if not os.path.lexists(current):
            continue
        try:
            details = os.lstat(current)
        except OSError:
            raise OpenAIImageValidationError("Image path could not be inspected safely") from None
        if _is_link(details):
            raise OpenAIImageValidationError("Image paths may not traverse links or reparse points")
    return lexical


def _format(value: str) -> str:
    result = str(value).strip().casefold()
    if result == "jpg":
        result = "jpeg"
    if result not in ALLOWED_FORMATS:
        raise OpenAIImageValidationError("Output format must be png, jpeg, or webp")
    return result


def _prompt(value: str) -> str:
    result = str(value).replace("\x00", "").strip()
    if not result or len(result) > MAX_PROMPT_CHARS:
        raise OpenAIImageValidationError(
            f"Image prompt must contain 1-{MAX_PROMPT_CHARS} characters"
        )
    return result


def _choice(value: str, allowed: frozenset[str], label: str) -> str:
    result = str(value).strip().casefold()
    if result not in allowed:
        raise OpenAIImageValidationError(f"Unsupported image {label}")
    return result


def _signature_matches(data: bytes, image_format: str) -> bool:
    if image_format == "png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if image_format == "jpeg":
        return data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _verify_image(data: bytes, image_format: str) -> dict[str, Any]:
    if not data or len(data) > MAX_OUTPUT_BYTES or not _signature_matches(data, image_format):
        raise OpenAIImageError("OpenAI returned an invalid image artifact")
    metadata: dict[str, Any] = {}
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        return metadata
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = map(int, image.size)
            decoded_format = str(image.format or "").casefold()
            expected = "jpeg" if image_format == "jpeg" else image_format
            if decoded_format != expected or width <= 0 or height <= 0:
                raise OpenAIImageError("OpenAI returned an invalid image artifact")
            if width * height > MAX_IMAGE_PIXELS:
                raise OpenAIImageError("OpenAI image exceeds the safe pixel limit")
            image.verify()
        metadata = {"width": width, "height": height, "pixels": width * height}
    except OpenAIImageError:
        raise
    except (
        UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError
    ):
        raise OpenAIImageError("OpenAI returned an invalid image artifact") from None
    return metadata


class OpenAIImagesProvider:
    """Bounded one-image generation/edit provider for GPT Image 2.

    The API key is read only at request time from the named environment variable;
    it is never accepted as an operation argument, persisted, or returned in status.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        api_key_env: str = "OPENAI_API_KEY",
        timeout_seconds: float = 120.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        details = os.lstat(self.workspace)
        if _is_link(details) or not stat.S_ISDIR(details.st_mode):
            raise OpenAIImageValidationError("Image workspace must be an ordinary directory")
        if not api_key_env or not api_key_env.replace("_", "").isalnum():
            raise OpenAIImageValidationError("API key environment name is invalid")
        if not 1 <= float(timeout_seconds) <= 300:
            raise OpenAIImageValidationError("Image request timeout must be 1-300 seconds")
        self.api_key_env = api_key_env
        self.timeout_seconds = float(timeout_seconds)
        self._opener = opener or urllib.request.build_opener(_NoRedirectHandler()).open

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(workspace={str(self.workspace)!r}, "
            f"configured={bool(os.getenv(self.api_key_env))})"
        )

    def status(self) -> dict[str, Any]:
        configured = bool(os.getenv(self.api_key_env, "").strip())
        return {
            "provider": "openai_images",
            "model": MODEL,
            "configured": configured,
            "supports": ("generate_one", "edit_one"),
            "next_action": None if configured else f"Set {self.api_key_env} outside the workspace",
        }

    def _api_key(self) -> str:
        value = os.getenv(self.api_key_env, "").strip()
        if not value:
            raise OpenAIImageConfigurationError(
                f"OpenAI Images is not configured; set {self.api_key_env} outside the workspace"
            )
        if any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise OpenAIImageConfigurationError("OpenAI Images API key is malformed")
        return value

    def _output_path(self, value: str | os.PathLike[str], image_format: str) -> Path:
        path = _inside_workspace(self.workspace, value)
        expected = ".jpg" if image_format == "jpeg" else f".{image_format}"
        accepted = {".jpg", ".jpeg"} if image_format == "jpeg" else {expected}
        if path.suffix.casefold() not in accepted:
            raise OpenAIImageValidationError(
                f"Output extension must match {image_format} format"
            )
        if os.path.lexists(path):
            raise OpenAIImageValidationError("Image output already exists")
        if not path.parent.is_dir() or _is_link(os.lstat(path.parent)):
            raise OpenAIImageValidationError("Image output directory must be an ordinary directory")
        return path

    def _source(self, value: str | os.PathLike[str]) -> tuple[Path, bytes, str]:
        path = _inside_workspace(self.workspace, value)
        try:
            before = os.lstat(path)
        except OSError:
            raise OpenAIImageValidationError("Source image is missing or inaccessible") from None
        if _is_link(before) or not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= MAX_SOURCE_BYTES:
            raise OpenAIImageValidationError("Source image must be a bounded ordinary file")
        guessed = mimetypes.guess_type(path.name)[0]
        if guessed == "image/jpg":
            guessed = "image/jpeg"
        if guessed not in _MIME_BY_FORMAT.values():
            raise OpenAIImageValidationError("Source image must be PNG, JPEG, or WebP")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                data = stream.read(MAX_SOURCE_BYTES + 1)
                after = os.fstat(stream.fileno())
        except OSError:
            raise OpenAIImageValidationError("Source image could not be read safely") from None
        identities = {
            (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
            for item in (before, opened, after)
        }
        if len(identities) != 1 or len(data) > MAX_SOURCE_BYTES:
            raise OpenAIImageValidationError("Source image changed or failed validation")
        data, source_mime, _safe_name = self._source_bytes(data, guessed, path.name)
        return path, data, source_mime

    def _source_bytes(
        self, source_data: bytes, source_mime: str, source_name: str
    ) -> tuple[bytes, str, str]:
        if not isinstance(source_data, (bytes, bytearray, memoryview)):
            raise OpenAIImageValidationError("Source image data must be bytes")
        if not 1 <= len(source_data) <= MAX_SOURCE_BYTES:
            raise OpenAIImageValidationError("Source image data exceeds the safe limit")
        data = bytes(source_data)
        mime = str(source_mime).strip().casefold()
        if mime == "image/jpg":
            mime = "image/jpeg"
        if mime not in _MIME_BY_FORMAT.values():
            raise OpenAIImageValidationError("Source image must be PNG, JPEG, or WebP")
        name = str(source_name).strip()
        if (
            not name or len(name) > 120 or Path(name).name != name
            or any(ord(char) < 32 for char in name) or '"' in name
        ):
            raise OpenAIImageValidationError("Source image name is invalid")
        source_format = {value: key for key, value in _MIME_BY_FORMAT.items()}[mime]
        if not _signature_matches(data, source_format):
            raise OpenAIImageValidationError("Source image type does not match its content")
        try:
            _verify_image(data, source_format)
        except OpenAIImageError:
            raise OpenAIImageValidationError("Source image failed decoding") from None
        return data, mime, name

    def _request(self, endpoint: str, body: bytes, content_type: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{API_ROOT}{endpoint}",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key()}",
                "Content-Type": content_type,
                "Accept": "application/json",
                "User-Agent": "jarvis-local/openai-images",
            },
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            raise OpenAIImageError("OpenAI Images request failed") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise OpenAIImageError("OpenAI Images response exceeded the safe limit")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OpenAIImageError("OpenAI Images returned an invalid response") from None
        if not isinstance(value, dict):
            raise OpenAIImageError("OpenAI Images returned an invalid response")
        return value

    def _decode(self, response: dict[str, Any], image_format: str) -> bytes:
        data = response.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise OpenAIImageError("OpenAI Images did not return exactly one image")
        encoded = data[0].get("b64_json")
        max_encoded = ((MAX_OUTPUT_BYTES + 2) // 3) * 4
        if not isinstance(encoded, str) or not encoded or len(encoded) > max_encoded:
            raise OpenAIImageError("OpenAI Images returned invalid image data")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise OpenAIImageError("OpenAI Images returned invalid image data") from None
        _verify_image(decoded, image_format)
        return decoded

    def _write(self, output: Path, data: bytes, image_format: str) -> dict[str, Any]:
        metadata = _verify_image(data, image_format)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".jarvis-image-", suffix=output.suffix, dir=output.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Hard-link publication is an atomic create-if-absent operation on
                # the same filesystem, so a racing file can never be overwritten.
                os.link(temporary, output)
            except FileExistsError:
                raise OpenAIImageValidationError(
                    "Image output appeared during generation"
                ) from None
            stored = output.read_bytes()
            if stored != data:
                output.unlink(missing_ok=True)
                raise OpenAIImageError("Stored image failed verification")
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "path": str(output),
            "relative_path": output.relative_to(self.workspace).as_posix(),
            "format": image_format,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "model": MODEL,
            **metadata,
        }

    def generate(
        self, prompt: str, output: str | os.PathLike[str], *, output_format: str = "png",
        size: str = "auto", quality: str = "auto",
    ) -> dict[str, Any]:
        image_format = _format(output_format)
        output_path = self._output_path(output, image_format)
        payload = json.dumps({
            "model": MODEL, "prompt": _prompt(prompt), "n": 1,
            "output_format": image_format,
            "size": _choice(size, ALLOWED_SIZES, "size"),
            "quality": _choice(quality, ALLOWED_QUALITIES, "quality"),
        }, separators=(",", ":")).encode("utf-8")
        response = self._request("/images/generations", payload, "application/json")
        return self._write(output_path, self._decode(response, image_format), image_format)

    def edit(
        self, source: str | os.PathLike[str], prompt: str,
        output: str | os.PathLike[str], *, output_format: str = "png",
        size: str = "auto", quality: str = "auto",
    ) -> dict[str, Any]:
        source_path, source_data, source_mime = self._source(source)
        return self.edit_bytes(
            source_data, source_mime, source_path.name, prompt, output,
            output_format=output_format, size=size, quality=quality,
        )

    def edit_bytes(
        self,
        source_data: bytes,
        source_mime: str,
        source_name: str,
        prompt: str,
        output: str | os.PathLike[str],
        *,
        output_format: str = "png",
        size: str = "auto",
        quality: str = "auto",
    ) -> dict[str, Any]:
        """Edit an in-memory image without writing the private source to disk."""
        image_format = _format(output_format)
        output_path = self._output_path(output, image_format)
        source_data, source_mime, safe_name = self._source_bytes(
            source_data, source_mime, source_name
        )
        boundary = "jarvis-" + secrets.token_hex(16)
        fields = {
            "model": MODEL, "prompt": _prompt(prompt), "n": "1",
            "output_format": image_format,
            "size": _choice(size, ALLOWED_SIZES, "size"),
            "quality": _choice(quality, ALLOWED_QUALITIES, "quality"),
        }
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend((
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode(),
                value.encode("utf-8"), b"\r\n",
            ))
        chunks.extend((
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{safe_name}\"\r\nContent-Type: {source_mime}\r\n\r\n".encode(),
            source_data, b"\r\n", f"--{boundary}--\r\n".encode(),
        ))
        response = self._request(
            "/images/edits", b"".join(chunks), f"multipart/form-data; boundary={boundary}"
        )
        return self._write(output_path, self._decode(response, image_format), image_format)


__all__ = [
    "MODEL", "OpenAIImageConfigurationError", "OpenAIImageError",
    "OpenAIImageValidationError", "OpenAIImagesProvider",
]
