"""Image generation and editing for Hub agents through an OpenRouter image model.

The operator's OpenRouter key (the same one Hub agents use for OpenRouter models) is read
from the key store at request time. It is sent only to openrouter.ai, never returned,
logged, written into an event or placed in a prompt; redirects are refused so it cannot be
forwarded elsewhere.

One call makes one image. The model's reply must carry the picture as base64 data (a remote
link is refused, never fetched); the bytes are checked against their declared type, decoded
to confirm the size, converted to PNG when the model returned WebP, and written into the
project's ``images/`` folder with an exclusive create, so nothing is overwritten.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"
# Newest general image model with a low per-image price (checked against the live catalogue on
# 2026-09-27). JARVIS_HUB_IMAGE_MODEL overrides it; an agent may also name another image model.
DEFAULT_MODEL = "google/gemini-3.1-flash-image"
KNOWN_MODELS = ("google/gemini-3.1-flash-image", "google/gemini-3.1-flash-lite-image",
                "google/gemini-3-pro-image", "openai/gpt-5.4-image-2", "openai/gpt-5-image-mini",
                "google/gemini-2.5-flash-image")
ASPECT_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
IMAGES_DIR = "images"
MAX_PROMPT_CHARS = 4_000
MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 30 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 40_000_000
TIMEOUT_SECONDS = 180.0
CATALOG_TTL = 3600.0
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._\-]{0,63}/[A-Za-z0-9][A-Za-z0-9._\-]{0,119}(?::[a-z0-9\-]{1,20})?$")
_DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|jpg|webp));base64,([A-Za-z0-9+/=\s]+)$")
_SIGNATURES = {"image/png": (b"\x89PNG\r\n\x1a\n",), "image/jpeg": (b"\xff\xd8\xff",),
               "image/webp": (b"RIFF",), "image/gif": (b"GIF87a", b"GIF89a")}
_INPUT_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
                "gif": "image/gif"}
_SECRETISH = re.compile(r"(?i)(sk-or-[A-Za-z0-9_\-]+|bearer\s+\S+|api[_-]?key\S*)")


class ImageError(RuntimeError):
    """A readable failure; never carries the key."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


_catalog_lock = threading.Lock()
_catalog: dict[str, Any] = {"at": 0.0, "models": ()}


def image_models(fetch: Callable[[str], Any] | None = None) -> tuple[str, ...]:
    """OpenRouter models that output images (the public list, cached for an hour)."""
    with _catalog_lock:
        if _catalog["models"] and time.time() - _catalog["at"] < CATALOG_TTL:
            return _catalog["models"]
    try:
        if fetch is None:
            request = urllib.request.Request(MODELS_URL, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read(16 * 1024 * 1024).decode("utf-8"))
        else:
            data = fetch(MODELS_URL)
        models = tuple(sorted(
            str(m["id"]) for m in (data or {}).get("data") or []
            if isinstance(m, dict) and _MODEL_ID.match(str(m.get("id") or ""))
            and "image" in ((m.get("architecture") or {}).get("output_modalities") or [])))
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        models = ()
    with _catalog_lock:
        if models:
            _catalog.update(at=time.time(), models=models)
        return _catalog["models"] or KNOWN_MODELS


def default_model() -> str:
    chosen = os.environ.get("JARVIS_HUB_IMAGE_MODEL", "").strip()
    return chosen if _MODEL_ID.match(chosen) else DEFAULT_MODEL


def choose_model(requested: Any, fetch: Callable[[str], Any] | None = None) -> str:
    if requested in (None, ""):
        return default_model()
    model = str(requested).strip()
    if not _MODEL_ID.match(model) or (model not in KNOWN_MODELS and model not in image_models(fetch)):
        raise ImageError(f"{model[:80]} is not an OpenRouter image model. Leave model empty to use "
                         f"{default_model()}.")
    return model


def _sniff(data: bytes, mime: str) -> bool:
    if mime == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return data.startswith(_SIGNATURES.get(mime, (b"\x00\x00\x00\x00",)))


def _decoded(data: bytes, mime: str) -> tuple[bytes, str, int, int]:
    """Decode the image to confirm it; WebP and GIF become PNG. Returns (bytes, mime, width, height)."""
    try:
        from PIL import Image
    except ImportError as exc:  # Pillow ships with JARVIS; without it nothing is saved unchecked
        raise ImageError("Pillow is required to check generated images.") from exc
    try:
        with Image.open(io.BytesIO(data)) as picture:
            width, height = (int(v) for v in picture.size)
            if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                raise ImageError("The image is larger than the safe pixel limit.")
            picture.load()
            if mime in {"image/webp", "image/gif"}:
                out = io.BytesIO()
                picture.save(out, format="PNG")
                return out.getvalue(), "image/png", width, height
    except ImageError:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise ImageError("The model returned something that is not a valid image.") from None
    return data, mime, width, height


def load_input(root: Path, relative: str, resolve: Callable[[Path, str], Path | None]) -> tuple[bytes, str, str]:
    """A project image to edit: (bytes, mime, project-relative path)."""
    file = resolve(Path(root), str(relative or ""))
    if file is None:
        raise ImageError("input_image must be an image file inside this project (for example an uploaded file).")
    mime = _INPUT_TYPES.get(file.suffix.casefold().lstrip("."))
    if mime is None:
        raise ImageError("input_image must be a PNG, JPEG, WebP or GIF file.")
    if file.stat().st_size > MAX_INPUT_BYTES:
        raise ImageError(f"input_image is larger than {MAX_INPUT_BYTES // (1024 * 1024)} MB.")
    data = file.read_bytes()
    if not _sniff(data, mime):
        raise ImageError("input_image does not contain the image type its name says.")
    return data, mime, file.relative_to(Path(root).resolve()).as_posix()


def _slug(prompt: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", prompt.casefold())[:6]
    return "-".join(words)[:48] or "image"


def _save(root: Path, data: bytes, mime: str, prompt: str) -> Path:
    base = Path(root).resolve(strict=True)
    folder = base / IMAGES_DIR
    folder.mkdir(exist_ok=True)
    if folder.is_symlink() or not folder.is_dir():
        raise ImageError("The project's images folder is not an ordinary folder.")
    suffix = ".jpg" if mime == "image/jpeg" else ".png"
    stem = f"{_slug(prompt)}-{time.strftime('%Y%m%d-%H%M%S')}"
    for attempt in range(1, 200):
        target = folder / (f"{stem}{suffix}" if attempt == 1 else f"{stem}-{attempt}{suffix}")
        try:
            handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
        except FileExistsError:
            continue
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        return target
    raise ImageError("Could not find a free file name in images/.")


def _clean_error(value: Any) -> str:
    return _SECRETISH.sub("[redacted]", " ".join(str(value or "").split()))[:240]


class ImageGenerator:
    """One-image-per-call generation and editing through OpenRouter."""

    def __init__(self, key: Callable[[], str | None], *, opener: Callable[..., Any] | None = None,
                 timeout: float = TIMEOUT_SECONDS, catalog_fetch: Callable[[str], Any] | None = None) -> None:
        self._key = key
        self._open = opener or urllib.request.build_opener(_NoRedirect()).open
        self.timeout = float(timeout)
        self._catalog_fetch = catalog_fetch

    def configured(self) -> bool:
        return bool(self._key())

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = self._key()
        if not key:
            raise ImageError("Add your OpenRouter API key in the Hub's Settings to create images.")
        request = urllib.request.Request(CHAT_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
                                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                                  "Accept": "application/json", "X-Title": "JARVIS Agent Hub"})
        try:
            with self._open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                body = json.loads(exc.read(64 * 1024).decode("utf-8", "replace"))
                detail = (body.get("error") or {}).get("message") if isinstance(body, dict) else ""
            except (OSError, ValueError, AttributeError):
                detail = ""
            if exc.code == 402:
                raise ImageError("The OpenRouter account is out of credits for image generation.") from None
            if exc.code in {401, 403}:
                raise ImageError("OpenRouter refused the API key; check it in Settings.") from None
            raise ImageError(f"OpenRouter answered HTTP {exc.code}"
                             + (f": {_clean_error(detail)}" if detail else ".")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ImageError(f"Could not reach OpenRouter: {_clean_error(getattr(exc, 'reason', exc))}") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ImageError("OpenRouter's reply was larger than the safe limit.")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ImageError("OpenRouter returned an unreadable reply.") from None
        if not isinstance(value, dict):
            raise ImageError("OpenRouter returned an unreadable reply.")
        if isinstance(value.get("error"), dict):
            raise ImageError(f"OpenRouter reported an error: {_clean_error(value['error'].get('message'))}")
        return value

    def generate(self, root: Path, prompt: str, *, model: Any = None, aspect_ratio: Any = None,
                 source: tuple[bytes, str, str] | None = None) -> dict[str, Any]:
        """Create (or, with ``source``, edit) one image and save it under images/."""
        prompt = str(prompt or "").replace("\x00", "").strip()
        if not prompt or len(prompt) > MAX_PROMPT_CHARS:
            raise ImageError(f"Describe the image in 1-{MAX_PROMPT_CHARS} characters.")
        if aspect_ratio not in (None, "") and aspect_ratio not in ASPECT_RATIOS:
            raise ImageError("aspect_ratio must be one of " + ", ".join(ASPECT_RATIOS) + ".")
        chosen = choose_model(model, self._catalog_fetch)
        content: Any = prompt
        if source is not None:
            data, mime, _name = source
            if not data or len(data) > MAX_INPUT_BYTES or not _sniff(data, mime):
                raise ImageError("The image to edit is missing, too large or not the type it claims.")
            content = [{"type": "text", "text": prompt},
                       {"type": "image_url", "image_url": {
                           "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}}]
        payload: dict[str, Any] = {"model": chosen, "messages": [{"role": "user", "content": content}],
                                   "modalities": ["image", "text"]}
        if aspect_ratio:
            payload["image_config"] = {"aspect_ratio": aspect_ratio}
        reply = self._request(payload)
        choices = reply.get("choices")
        message = (choices[0] or {}).get("message") if isinstance(choices, list) and choices else None
        images = (message or {}).get("images") if isinstance(message, dict) else None
        url = None
        for item in images or []:
            candidate = ((item or {}).get("image_url") or {}).get("url") if isinstance(item, dict) else None
            if isinstance(candidate, str):
                url = candidate
                break
        if url is None:
            text = _clean_error((message or {}).get("content") if isinstance(message, dict) else "")
            raise ImageError("The model did not return an image" + (f" (it said: {text})" if text else "") + ".")
        if len(url) > (MAX_OUTPUT_BYTES * 4) // 3 + 1024:
            raise ImageError("The model returned an image larger than the safe limit.")
        match = _DATA_URL.match(url)
        if match is None:
            raise ImageError("The model returned a link instead of image data; it was not fetched.")
        mime = "image/jpeg" if match.group(1) == "image/jpg" else match.group(1)
        try:
            data = base64.b64decode("".join(match.group(2).split()), validate=True)
        except (binascii.Error, ValueError):
            raise ImageError("The model returned image data that could not be decoded.") from None
        if not data or len(data) > MAX_OUTPUT_BYTES or not _sniff(data, mime):
            raise ImageError("The model returned image data that does not match its type.")
        data, mime, width, height = _decoded(data, mime)
        target = _save(root, data, mime, prompt)
        relative = target.relative_to(Path(root).resolve()).as_posix()
        usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
        cost = usage.get("cost")
        result = {"path": relative, "relative_path": relative, "mime": mime, "bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(), "width": width, "height": height,
                  "model": str(reply.get("model") or chosen)[:120],
                  "edited_from": source[2] if source is not None else None,
                  "shown": "The image appears inline under your reply in the Hub chat; mention it, do not "
                           "paste its path as a Markdown image."}
        if isinstance(cost, (int, float)):
            result["cost_usd"] = round(float(cost), 6)
        note = (message or {}).get("content") if isinstance(message, dict) else None
        if isinstance(note, str) and note.strip():
            result["model_note"] = note.strip()[:500]
        return result
