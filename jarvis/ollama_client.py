from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit


DEFAULT_HEALTH_TIMEOUT = 5.0
DEFAULT_GENERATION_TIMEOUT = 600.0
DEFAULT_MAX_OUTPUT_TOKENS = 2048
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF = 0.25
DEFAULT_KEEP_ALIVE = "30m"
TRANSIENT_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _bounded_float(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return parsed


def normalize_ollama_keep_alive(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("keep_alive must be an Ollama duration string")
    parsed = value.strip().lower()
    if not re.fullmatch(r"(?:0|-?[1-9][0-9]*(?:ms|s|m|h)?)", parsed):
        raise ValueError("keep_alive must be 0, a non-zero integer, or an ms/s/m/h duration")
    return parsed


def normalize_ollama_url(base_url: str, *, allow_remote: bool = False) -> str:
    """Validate an Ollama origin and return a credential-free canonical URL."""
    try:
        parsed = urlsplit(base_url.strip())
        hostname = parsed.hostname
        port = parsed.port
    except (AttributeError, ValueError):
        raise ValueError("JARVIS_OLLAMA_URL is invalid") from None

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        raise ValueError("JARVIS_OLLAMA_URL must be an http or https origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("JARVIS_OLLAMA_URL must not contain credentials")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("JARVIS_OLLAMA_URL must be an origin without a path, query, or fragment")
    if port == 0:
        raise ValueError("JARVIS_OLLAMA_URL has an invalid port")

    host = hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        is_loopback = host == "localhost"
        canonical_host = "127.0.0.1" if is_loopback else host
    else:
        is_loopback = address.is_loopback
        canonical_host = f"[{address.compressed}]" if address.version == 6 else address.compressed

    if not is_loopback:
        if not allow_remote:
            raise ValueError(
                "Remote Ollama endpoints are disabled; set JARVIS_OLLAMA_ALLOW_REMOTE=true "
                "only for an endpoint you trust"
            )
        if scheme != "https":
            raise ValueError("Trusted remote Ollama endpoints must use HTTPS")

    canonical_port = f":{port}" if port is not None else ""
    return f"{scheme}://{canonical_host}{canonical_port}"


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or default_port


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, base_url: str, allow_remote: bool) -> None:
        super().__init__()
        self._origin = _origin(base_url)
        self._allow_remote = allow_remote

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        try:
            target = urlsplit(newurl)
            candidate_origin = f"{target.scheme}://{target.netloc}"
            normalized = normalize_ollama_url(
                candidate_origin,
                allow_remote=self._allow_remote,
            )
        except ValueError:
            raise urllib.error.HTTPError(
                req.full_url, code, "Blocked unsafe Ollama redirect", headers, fp
            ) from None
        if _origin(normalized) != self._origin:
            raise urllib.error.HTTPError(
                req.full_url, code, "Blocked cross-origin Ollama redirect", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class GenerationMetrics:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_duration_ns: int | None
    load_duration_ns: int | None
    prompt_duration_ns: int | None
    completion_duration_ns: int | None


class ChatResponse(dict[str, Any]):
    """A message-compatible dict with Ollama generation metadata attached."""

    def __init__(self, message: dict[str, Any], response: dict[str, Any]) -> None:
        super().__init__(message)
        self.done = response.get("done") if isinstance(response.get("done"), bool) else None
        self.done_reason = (
            response.get("done_reason") if isinstance(response.get("done_reason"), str) else None
        )
        self.model = response.get("model") if isinstance(response.get("model"), str) else None
        self.created_at = (
            response.get("created_at") if isinstance(response.get("created_at"), str) else None
        )
        self.metrics = GenerationMetrics(
            prompt_tokens=_optional_int(response.get("prompt_eval_count")),
            completion_tokens=_optional_int(response.get("eval_count")),
            total_duration_ns=_optional_int(response.get("total_duration")),
            load_duration_ns=_optional_int(response.get("load_duration")),
            prompt_duration_ns=_optional_int(response.get("prompt_eval_duration")),
            completion_duration_ns=_optional_int(response.get("eval_duration")),
        )

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "done": self.done,
            "done_reason": self.done_reason,
            "model": self.model,
            "created_at": self.created_at,
            "metrics": self.metrics,
        }


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class OllamaError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str | None = None,
        *,
        allow_remote: bool | None = None,
        health_timeout: float | None = None,
        generation_timeout: float | None = None,
        max_output_tokens: int | None = None,
        max_response_bytes: int | None = None,
        max_retries: int | None = None,
        retry_backoff: float | None = None,
        keep_alive: str | None = None,
        num_thread: int | None = None,
        open_url: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if allow_remote is None:
            allow_remote = _env_bool("JARVIS_OLLAMA_ALLOW_REMOTE", False)
        elif not isinstance(allow_remote, bool):
            raise ValueError("allow_remote must be true or false")

        self.allow_remote = allow_remote
        self.base_url = normalize_ollama_url(base_url, allow_remote=allow_remote)
        self.model = model
        self.health_timeout = _bounded_float(
            health_timeout
            if health_timeout is not None
            else os.getenv("JARVIS_OLLAMA_HEALTH_TIMEOUT", DEFAULT_HEALTH_TIMEOUT),
            "health_timeout",
            0.1,
            60.0,
        )
        self.generation_timeout = _bounded_float(
            generation_timeout
            if generation_timeout is not None
            else os.getenv("JARVIS_OLLAMA_GENERATION_TIMEOUT", DEFAULT_GENERATION_TIMEOUT),
            "generation_timeout",
            1.0,
            3600.0,
        )
        self.max_output_tokens = _bounded_int(
            max_output_tokens
            if max_output_tokens is not None
            else os.getenv("JARVIS_OLLAMA_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS),
            "max_output_tokens",
            128,
            32768,
        )
        self.max_response_bytes = _bounded_int(
            max_response_bytes
            if max_response_bytes is not None
            else os.getenv("JARVIS_OLLAMA_MAX_RESPONSE_BYTES", DEFAULT_MAX_RESPONSE_BYTES),
            "max_response_bytes",
            1024,
            64 * 1024 * 1024,
        )
        self.max_retries = _bounded_int(
            max_retries
            if max_retries is not None
            else os.getenv("JARVIS_OLLAMA_MAX_RETRIES", DEFAULT_MAX_RETRIES),
            "max_retries",
            0,
            5,
        )
        self.retry_backoff = _bounded_float(
            retry_backoff
            if retry_backoff is not None
            else os.getenv("JARVIS_OLLAMA_RETRY_BACKOFF", DEFAULT_RETRY_BACKOFF),
            "retry_backoff",
            0.0,
            10.0,
        )
        self.keep_alive = normalize_ollama_keep_alive(
            keep_alive
            if keep_alive is not None
            else os.getenv("JARVIS_OLLAMA_KEEP_ALIVE", DEFAULT_KEEP_ALIVE)
        )
        raw_num_thread = (
            num_thread
            if num_thread is not None
            else os.getenv("JARVIS_OLLAMA_NUM_THREAD")
        )
        self.num_thread = (
            None
            if raw_num_thread is None or str(raw_num_thread).strip() == ""
            else _bounded_int(raw_num_thread, "num_thread", 1, 256)
        )
        self._sleep = sleep
        self._models_cache: tuple[str, ...] = ()
        self._models_loaded = False
        self._capabilities_cache: dict[str, frozenset[str]] = {}

        redirect_handler = _SameOriginRedirectHandler(self.base_url, allow_remote)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), redirect_handler)
        self._open_url = open_url or opener.open

    def _retry(self, attempt: int, deadline: float) -> None:
        delay = self.retry_backoff * (2**attempt)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OllamaError(
                "Ollama request exceeded its configured total deadline",
                retryable=True,
            )
        if delay:
            self._sleep(min(delay, remaining))

    @staticmethod
    def _network_error_is_transient(exc: BaseException) -> bool:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        return not isinstance(reason, (ssl.SSLError, ssl.CertificateError, ValueError))

    def _read_json(self, response: Any) -> dict[str, Any]:
        content_length = response.headers.get("Content-Length") if response.headers else None
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError):
                declared_length = None
            if declared_length is not None and declared_length > self.max_response_bytes:
                raise OllamaError("Ollama response exceeded the configured size limit")

        body = response.read(self.max_response_bytes + 1)
        if not isinstance(body, bytes) or len(body) > self.max_response_bytes:
            raise OllamaError("Ollama response exceeded the configured size limit")
        try:
            result = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OllamaError("Ollama returned malformed JSON") from None
        if not isinstance(result, dict):
            raise OllamaError("Ollama returned an unexpected JSON response")
        return result

    def _request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/") or "://" in path:
            raise ValueError("Ollama API path must be relative to the configured origin")
        effective_timeout = self.generation_timeout if timeout is None else timeout
        deadline = time.monotonic() + effective_timeout
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else None

        for attempt in range(self.max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OllamaError(
                    "Ollama request exceeded its configured total deadline",
                    retryable=True,
                )
            request = urllib.request.Request(
                f"{self.base_url}{path}",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST" if data is not None else "GET",
            )
            try:
                with self._open_url(request, timeout=max(0.1, remaining)) as response:
                    return self._read_json(response)
            except urllib.error.HTTPError as exc:
                status_code = exc.code
                try:
                    exc.close()
                except Exception:
                    pass
                retryable = status_code in TRANSIENT_HTTP_STATUS
                if retryable and attempt < self.max_retries:
                    self._retry(attempt, deadline)
                    continue
                raise OllamaError(
                    f"Ollama API request failed with HTTP {status_code}",
                    status_code=status_code,
                    retryable=retryable,
                ) from None
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
                retryable = self._network_error_is_transient(exc)
                if retryable and attempt < self.max_retries:
                    self._retry(attempt, deadline)
                    continue
                attempts = attempt + 1
                raise OllamaError(
                    f"Could not reach the Ollama endpoint after {attempts} attempt(s)",
                    retryable=retryable,
                ) from None

        raise OllamaError("Ollama request failed")

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
        context_length: int = 16384,
        think: bool | str | None = None,
        temperature: float = 0.2,
        response_format: str | dict[str, Any] | None = None,
        seed: int | None = None,
        keep_alive: str | None = None,
    ) -> ChatResponse:
        selected_model = model or self.model
        if not selected_model:
            raise OllamaError("No Ollama model was selected")
        selected_temperature = _bounded_float(temperature, "temperature", 0.0, 2.0)
        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "keep_alive": (
                self.keep_alive
                if keep_alive is None
                else normalize_ollama_keep_alive(keep_alive)
            ),
            "options": {
                "temperature": selected_temperature,
                "num_ctx": context_length,
                # Ollama defaults num_predict to -1 (unbounded). A local agent
                # must not spend minutes narrating after it already knows the
                # next tool call, especially when the model spills to CPU.
                "num_predict": self.max_output_tokens,
            },
        }
        if self.num_thread is not None:
            payload["options"]["num_thread"] = self.num_thread
        if think is not None:
            payload["think"] = think
        if response_format is not None:
            if isinstance(response_format, str):
                if response_format != "json":
                    raise ValueError("response_format string must be 'json'")
            elif isinstance(response_format, dict):
                encoded_schema = json.dumps(response_format, separators=(",", ":"))
                if len(encoded_schema.encode("utf-8")) > 64 * 1024:
                    raise ValueError("response_format schema exceeds 64 KiB")
            else:
                raise ValueError("response_format must be 'json' or a JSON schema object")
            payload["format"] = response_format
        if seed is not None:
            if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
                raise ValueError("seed must be an integer from 0 to 2147483647")
            payload["options"]["seed"] = seed
        result = self._request(
            "/api/chat",
            payload,
            timeout=self.generation_timeout,
        )
        message = result.get("message")
        if not isinstance(message, dict):
            raise OllamaError("Ollama response did not contain a valid message")
        return ChatResponse(message, result)

    def preload(self, model: str, *, context_length: int = 4096) -> GenerationMetrics:
        """Load a local model without generating text so first-use latency is paid at startup."""
        selected_model = str(model).strip()
        if not selected_model:
            raise OllamaError("No Ollama model was selected")
        options: dict[str, Any] = {
            "num_ctx": _bounded_int(context_length, "context_length", 512, 262144),
        }
        if self.num_thread is not None:
            options["num_thread"] = self.num_thread
        result = self._request(
            "/api/generate",
            {
                "model": selected_model,
                "stream": False,
                "keep_alive": self.keep_alive,
                "options": options,
            },
            timeout=self.generation_timeout,
        )
        return GenerationMetrics(
            prompt_tokens=_optional_int(result.get("prompt_eval_count")),
            completion_tokens=_optional_int(result.get("eval_count")),
            total_duration_ns=_optional_int(result.get("total_duration")),
            load_duration_ns=_optional_int(result.get("load_duration")),
            prompt_duration_ns=_optional_int(result.get("prompt_eval_duration")),
            completion_duration_ns=_optional_int(result.get("eval_duration")),
        )

    def capabilities(self, model: str, *, refresh: bool = False) -> frozenset[str]:
        selected_model = str(model).strip()
        if not selected_model:
            raise OllamaError("No Ollama model was selected")
        if not refresh and selected_model in self._capabilities_cache:
            return self._capabilities_cache[selected_model]
        result = self._request(
            "/api/show",
            {"model": selected_model},
            timeout=self.health_timeout,
        )
        raw_capabilities = result.get("capabilities")
        if not isinstance(raw_capabilities, list):
            raise OllamaError("Ollama model capabilities response was malformed")
        capabilities = frozenset(
            str(item).strip().casefold()
            for item in raw_capabilities
            if isinstance(item, str) and item.strip()
        )
        self._capabilities_cache[selected_model] = capabilities
        return capabilities

    def supports_thinking(self, model: str) -> bool:
        return "thinking" in self.capabilities(model)

    def models(self, *, refresh: bool = True) -> list[str]:
        if self._models_loaded and not refresh:
            return list(self._models_cache)
        result = self._request("/api/tags", timeout=self.health_timeout)
        raw_models = result.get("models")
        if not isinstance(raw_models, list):
            raise OllamaError("Ollama model response was malformed")
        names = [
            item["name"]
            for item in raw_models
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]
        ]
        self._models_cache = tuple(dict.fromkeys(names))
        self._models_loaded = True
        return list(self._models_cache)

    def refresh_models(self) -> list[str]:
        return self.models(refresh=True)

    @property
    def cached_models(self) -> tuple[str, ...]:
        return self._models_cache
