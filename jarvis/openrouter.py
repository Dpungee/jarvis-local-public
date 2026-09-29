"""OpenRouter as a JARVIS model provider: one API key, hundreds of models.

Three parts:

* ``OpenRouterClient`` speaks OpenRouter's OpenAI-compatible Chat Completions API with
  JARVIS's chat contract (messages, tool schemas, image parts, reasoning effort, JSON
  output). Tools stay JARVIS tools: the model only proposes calls; JARVIS executes them.
* ``catalog()`` reads OpenRouter's public model list (no key needed), cached, and keeps the
  facts the Hub needs: tool support, image input, context window and whether it is free.
* ``KeyStore`` keeps the operator's API key in one file readable only by this Windows user.
  The key is entered by the operator in the Hub's settings, is never returned by any API,
  never logged and never placed in a prompt.

Model references look like ``openrouter:stealth/space-bunny-alpha``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .model_client import (
    ChatResponse,
    ModelProviderError,
    _CloudHTTPClient,
    _function_arguments,
)

API_BASE = "https://openrouter.ai/api/v1"
CHAT_URL = f"{API_BASE}/chat/completions"
MODELS_URL = f"{API_BASE}/models"
KEY_URL = f"{API_BASE}/key"
DEFAULT_MODEL = "stealth/space-bunny-alpha"
# Offered first in the model picker; the rest of the list comes from the live catalogue.
FEATURED_MODELS = ("stealth/space-bunny-alpha",)
EFFORTS = ("minimal", "low", "medium", "high")
# "~vendor/model-latest" ids are OpenRouter aliases that follow a vendor's newest model.
_MODEL_ID = re.compile(r"^~?[a-z0-9][a-z0-9._\-]{0,63}/[A-Za-z0-9][A-Za-z0-9._\-]{0,119}(?::[a-z0-9\-]{1,20})?$")
_KEY = re.compile(r"^sk-or-[A-Za-z0-9_\-]{20,200}$")
CATALOG_TTL = 3600.0


def valid_model_id(value: str) -> bool:
    return bool(_MODEL_ID.match(str(value or "")))


def valid_key(value: str) -> bool:
    return bool(_KEY.match(str(value or "").strip()))


# --------------------------------------------------------------------------- client
def _chat_content(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ModelProviderError("OpenRouter", "received unsupported message content")
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            raise ModelProviderError("OpenRouter", "received malformed content part")
        kind = str(part.get("type") or "")
        if kind == "text":
            parts.append({"type": "text", "text": str(part.get("text") or "")})
        elif kind == "image":
            mime, data = str(part.get("mime") or ""), str(part.get("data") or "")
            if not mime or not data:
                raise ModelProviderError("OpenRouter", "received malformed image content")
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
        else:
            raise ModelProviderError("OpenRouter", "received unsupported content part")
    return parts


def chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """JARVIS messages to Chat Completions messages, pairing tool results with call ids."""
    converted: list[dict[str, Any]] = []
    pending: list[str] = []
    for index, message in enumerate(messages):
        role = str(message.get("role") or "")
        if role == "system":
            content = message.get("content")
            if not isinstance(content, str):
                raise ModelProviderError("OpenRouter", "system content must be text")
            if content:
                converted.append({"role": "system", "content": content})
            continue
        if role in {"user", "assistant"}:
            item: dict[str, Any] = {"role": role, "content": _chat_content(message.get("content") or "")}
            calls = message.get("tool_calls")
            if role == "assistant" and isinstance(calls, list) and calls:
                pending = []
                item["tool_calls"] = []
                for call_index, call in enumerate(calls):
                    if not isinstance(call, dict):
                        raise ModelProviderError("OpenRouter", "received malformed tool-call history")
                    name, arguments = _function_arguments(call, "OpenRouter")
                    call_id = f"call_j{index}_{call_index}"
                    pending.append(call_id)
                    item["tool_calls"].append({"id": call_id, "type": "function", "function": {
                        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}})
                if not item["content"]:
                    item["content"] = None
            if item.get("content") in ("", []) and "tool_calls" not in item:
                continue
            converted.append(item)
            continue
        if role == "tool":
            if not pending:
                raise ModelProviderError("OpenRouter", "received an unmatched tool result")
            converted.append({"role": "tool", "tool_call_id": pending.pop(0),
                              "content": str(message.get("content") or "")})
            continue
        raise ModelProviderError("OpenRouter", "received an unsupported message role")
    return converted


def chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict) or not function.get("name") or not isinstance(
                function.get("parameters"), dict):
            raise ModelProviderError("OpenRouter", "received an invalid tool schema")
        converted.append({"type": "function", "function": {
            "name": str(function["name"]), "description": str(function.get("description") or ""),
            "parameters": function["parameters"]}})
    return converted


class OpenRouterClient(_CloudHTTPClient):
    provider = "OpenRouter"
    endpoint = CHAT_URL
    default_model = DEFAULT_MODEL

    def __init__(self, api_key: str, **kwargs: Any) -> None:
        super().__init__(api_key, **kwargs)
        self.fixed_effort: str | None = None
        self.last_usage: dict[str, Any] = {}
        # One entry per model call, in the shape the Hub's context meter reads.
        self.call_usage: list[dict[str, Any]] = []

    def set_fixed_effort(self, effort: str | None) -> None:
        if effort not in (None, "", "auto") and effort not in EFFORTS:
            raise ValueError("OpenRouter effort must be one of " + ", ".join(EFFORTS))
        self.fixed_effort = None if effort in (None, "", "auto") else effort

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # OpenRouter's optional app attribution; it carries no user data.
            "X-Title": "JARVIS Agent Hub",
        }

    def _payload(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str, *,
                 think: bool | str | None, response_format: str | dict[str, Any] | None) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": chat_messages(messages),
                                   "max_tokens": self.max_output_tokens}
        if tools:
            payload["tools"] = chat_tools(tools)
        effort = self.fixed_effort
        if effort is None:
            # Unset or "no thinking" means a quick turn: "low" measured ~1 s for a plain reply on
            # stealth/space-bunny-alpha, the provider default ~6 s and "minimal" 3-7 s.
            effort = ("medium" if think is True else "low" if think in (None, False)
                      else str(think).casefold())
            effort = {"none": "low", "minimal": "low", "xhigh": "high", "max": "high"}.get(effort, effort)
        if effort is not None:
            if effort not in EFFORTS:
                raise ValueError("OpenRouter reasoning effort is invalid")
            # Reasoning text is not needed by JARVIS; keep the answer only.
            payload["reasoning"] = {"effort": effort, "exclude": True}
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        elif isinstance(response_format, dict):
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "jarvis_response", "strict": True, "schema": response_format}}
        elif response_format is not None:
            raise ValueError("response_format must be 'json' or a JSON schema object")
        return payload

    def _response(self, result: dict[str, Any], model: str) -> ChatResponse:
        error = result.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            status = int(code) if isinstance(code, int) or str(code).isdigit() else None
            raise ModelProviderError(self.provider, f"returned an error: {str(error.get('message'))[:200]}",
                                     status_code=status, retryable=status in {408, 429, 500, 502, 503, 504},
                                     provider_unavailable=status in {401, 402, 403, 429})
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ModelProviderError(self.provider, "response did not contain a choice")
        choice = choices[0]
        raw = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = raw.get("content")
        if isinstance(content, list):
            content = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
        message: dict[str, Any] = {"role": "assistant", "content": str(content or "")}
        calls = []
        for call in raw.get("tool_calls") or []:
            function = call.get("function") if isinstance(call, dict) else None
            if not isinstance(function, dict) or not isinstance(function.get("name"), str):
                raise ModelProviderError(self.provider, "returned a malformed function call")
            arguments = function.get("arguments")
            calls.append({"function": {"name": function["name"],
                                       "arguments": arguments if isinstance(arguments, str) else json.dumps(
                                           arguments or {})}})
        if calls:
            message["tool_calls"] = calls
        finish = str(choice.get("finish_reason") or "")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        self.last_usage = {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                           "cost": usage.get("cost"), "model": result.get("model")}
        if usage:
            self.call_usage.append({"context_tokens": usage.get("prompt_tokens"),
                                    "output_tokens": usage.get("completion_tokens"), "context_window": None})
            del self.call_usage[:-200]
        reported = result.get("model") if isinstance(result.get("model"), str) and result["model"] else None
        return ChatResponse(message, {
            "done": True,
            "done_reason": "length" if finish == "length" else "tool_use" if calls else "stop",
            "model": reported or model,
            "model_attested": reported is not None,
            "prompt_eval_count": usage.get("prompt_tokens"),
            "eval_count": usage.get("completion_tokens"),
        })

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str,
             context_length: int = 16384, think: bool | str | None = None, temperature: float = 0.2,
             response_format: str | dict[str, Any] | None = None, seed: int | None = None,
             cancellation_guard: Callable[[], bool] | None = None, **_: Any) -> ChatResponse:
        del context_length, seed
        payload = self._payload(messages, tools, model, think=think, response_format=response_format)
        payload["temperature"] = temperature
        try:
            return self._response(self._request(payload, cancellation_guard=cancellation_guard), model)
        except ModelProviderError as exc:
            # Some models behind OpenRouter fail on strict JSON-schema output (a 502 "JSON error
            # injected into SSE stream", or a 400). Ask once more for plain JSON with the schema in
            # the instructions; the caller still validates the answer against the schema.
            if not isinstance(response_format, dict) or exc.status_code not in {400, 422, 500, 502, 503}:
                raise
            if cancellation_guard is not None and cancellation_guard():
                raise
            relaxed = self._payload(
                [{"role": "system", "content": "Reply with only a JSON object that matches this JSON schema "
                  "exactly (all required fields, no extra fields): " + json.dumps(response_format)}, *messages],
                tools, model, think=think, response_format="json")
            relaxed["temperature"] = temperature
            return self._response(self._request(relaxed, cancellation_guard=cancellation_guard), model)

    def chat_stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str,
                    on_delta: Callable[[str], None], context_length: int = 16384,
                    think: bool | str | None = None, temperature: float = 0.2,
                    response_format: str | dict[str, Any] | None = None, seed: int | None = None,
                    cancellation_guard: Callable[[], bool] | None = None, **_: Any) -> ChatResponse:
        del context_length, seed
        if tools or response_format is not None:
            return self.chat(messages, tools, model, think=think, temperature=temperature,
                             response_format=response_format, cancellation_guard=cancellation_guard)
        payload = self._payload(messages, tools, model, think=think, response_format=None)
        payload.update(stream=True, temperature=temperature, usage={"include": True})
        text: list[str] = []
        final: dict[str, Any] = {"choices": [{"message": {}, "finish_reason": "stop"}]}

        def handle(event: dict[str, Any]) -> None:
            if isinstance(event.get("error"), dict):
                raise ModelProviderError(self.provider, "stream reported an error")
            for choice in event.get("choices") or []:
                delta = (choice or {}).get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, str) and piece:
                    text.append(piece)
                    on_delta(piece)
                if choice.get("finish_reason"):
                    final["choices"][0]["finish_reason"] = choice["finish_reason"]
            if isinstance(event.get("usage"), dict):
                final["usage"] = event["usage"]
            if isinstance(event.get("model"), str):
                final["model"] = event["model"]

        try:
            self._request_sse(payload, handle, cancellation_guard=cancellation_guard)
        except ModelProviderError:
            if (cancellation_guard is not None and cancellation_guard()) or text:
                raise
            return self.chat(messages, tools, model, think=think, temperature=temperature,
                             cancellation_guard=cancellation_guard)
        final["choices"][0]["message"] = {"content": "".join(text)}
        return self._response(final, model)

    def close(self) -> None:
        return None


# -------------------------------------------------------------------------- catalogue
_catalog_lock = threading.Lock()
_catalog: dict[str, Any] = {"at": 0.0, "tried": 0.0, "models": {}}
CATALOG_RETRY = 300.0  # after a failed fetch, keep serving the last copy (or none) this long


def _get_json(url: str, headers: dict[str, str] | None = None, timeout: float = 20.0) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
        return json.loads(response.read(16 * 1024 * 1024).decode("utf-8"))


def _summarise(item: dict[str, Any]) -> dict[str, Any]:
    pricing = item.get("pricing") or {}
    parameters = set(item.get("supported_parameters") or [])
    modalities = set((item.get("architecture") or {}).get("input_modalities") or [])

    def zero(value: Any) -> bool:
        try:
            return float(value) == 0.0
        except (TypeError, ValueError):
            return False
    return {
        "id": str(item.get("id")), "name": str(item.get("name") or item.get("id")),
        "context_length": int(item.get("context_length") or 0),
        "free": zero(pricing.get("prompt")) and zero(pricing.get("completion")),
        "tools": "tools" in parameters, "vision": "image" in modalities,
        "reasoning": bool({"reasoning", "reasoning_effort"} & parameters),
        "created": int(item.get("created") or 0),
    }


def catalog(*, refresh: bool = False, fetch: Callable[[str], Any] | None = None,
            cached_only: bool = False) -> dict[str, dict[str, Any]]:
    """OpenRouter's public model list keyed by id; stale copies are kept if a refresh fails.

    ``cached_only`` never touches the network (for request paths the UI polls)."""
    with _catalog_lock:
        if cached_only:
            return dict(_catalog["models"])
        now = time.time()
        fresh = now - _catalog["at"] < CATALOG_TTL
        backing_off = now - _catalog["tried"] < CATALOG_RETRY
        if not refresh and ((_catalog["models"] and fresh) or backing_off):
            return dict(_catalog["models"])
        _catalog["tried"] = now
    try:
        data = (fetch or _get_json)(MODELS_URL)
        models = {str(m["id"]): _summarise(m) for m in (data or {}).get("data") or []
                  if isinstance(m, dict) and valid_model_id(str(m.get("id") or ""))}
    except (OSError, ValueError, urllib.error.URLError, KeyError, TypeError):
        models = {}
    with _catalog_lock:
        if models:
            _catalog.update(at=time.time(), models=models)
        return dict(_catalog["models"])


def model_info(model: str) -> dict[str, Any] | None:
    with _catalog_lock:
        return _catalog["models"].get(model)


def agent_models(limit: int = 80) -> list[dict[str, Any]]:
    """Models that can drive an agent (tool calling), featured first, then free, then newest."""
    models = [m for m in catalog(cached_only=True).values() if m["tools"]]
    featured = [m for m in models if m["id"] in FEATURED_MODELS]
    rest = sorted((m for m in models if m["id"] not in FEATURED_MODELS),
                  key=lambda m: (not m["free"], -m["created"]))
    return (featured + rest)[:limit]


def refresh_catalog_in_background() -> None:
    threading.Thread(target=catalog, name="openrouter-catalog", daemon=True).start()


def model_facts() -> dict[str, dict[str, Any]]:
    """What the model picker shows next to each agent-capable model."""
    return {m["id"]: {"name": m["name"], "free": m["free"], "vision": m["vision"],
                      "context_length": m["context_length"]} for m in agent_models()}


# ------------------------------------------------------------------------------- key
class KeyStore:
    """The operator's OpenRouter key in one file only this Windows account can read."""

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / "openrouter" / "api-key"

    def get(self) -> str | None:
        try:
            value = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if not value:
            value = os.environ.get("OPENROUTER_API_KEY", "").strip()
        return value if valid_key(value) else None

    def source(self) -> str | None:
        if self.path.exists():
            return "hub"
        return "environment" if valid_key(os.environ.get("OPENROUTER_API_KEY", "")) else None

    def set(self, value: str) -> None:
        value = str(value or "").strip()
        if not valid_key(value):
            raise ValueError("That does not look like an OpenRouter key (they start with sk-or-).")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(value, encoding="utf-8")
        _owner_only(temporary)
        os.replace(temporary, self.path)
        _owner_only(self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _owner_only(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R,W,D)"],
                   capture_output=True, stdin=subprocess.DEVNULL, check=False,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def check_key(key: str, fetch: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Ask OpenRouter about the key (never returns the key): label, limits, free tier."""
    try:
        data = (fetch or _get_json)(KEY_URL, {"Authorization": f"Bearer {key}"})
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "OpenRouter rejected the key." if exc.code in {401, 403}
                else f"OpenRouter answered HTTP {exc.code}."}
    except (OSError, ValueError, urllib.error.URLError):
        return {"ok": False, "error": "Could not reach OpenRouter to check the key."}
    info = (data or {}).get("data") or {}
    # The key's label is left out: OpenRouter's default label is a truncated copy of the key.
    return {"ok": True, "free_tier": bool(info.get("is_free_tier")),
            "limit": info.get("limit"), "limit_remaining": info.get("limit_remaining"),
            "usage": info.get("usage")}
