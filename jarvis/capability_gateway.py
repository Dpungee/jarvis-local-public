from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from .policy import resolve_workspace_path
from .redaction import contains_secret, is_sensitive_key, redact_secrets


MAX_CONNECTOR_BYTES = 64 * 1024
MAX_CONNECTORS = 64
MAX_ACTIONS = 32
MAX_ARGUMENTS = 24
MAX_RESPONSE_CHARACTERS = 24_000
_IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z")
_ENVIRONMENT_NAME = re.compile(r"JARVIS_CONNECTOR_[A-Z0-9_]{1,80}\Z")
_PATH_PARAMETER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_HEADER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}\Z")
_BLOCKED_HEADERS = frozenset({
    "connection", "content-length", "cookie", "host", "proxy-authorization",
    "proxy-connection", "set-cookie", "transfer-encoding",
})
_PROPERTY_TYPES = frozenset({"boolean", "integer", "number", "string"})


class ConnectorError(ValueError):
    """A connector manifest or invocation failed closed."""


def _ordinary_file(path: Path) -> bytes:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ConnectorError("Connector manifest is missing or inaccessible") from exc
    attributes = getattr(before, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(before.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise ConnectorError("Connector manifests must be ordinary non-link files")
    if before.st_size > MAX_CONNECTOR_BYTES:
        raise ConnectorError("Connector manifest exceeds the 64 KB limit")
    try:
        raw = path.read_bytes()
        after = os.lstat(path)
    except OSError as exc:
        raise ConnectorError("Connector manifest could not be read safely") from exc
    after_attributes = getattr(after, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(after.st_mode)
        or after_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not stat.S_ISREG(after.st_mode)
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ConnectorError("Connector manifest changed while it was being read")
    if len(raw) > MAX_CONNECTOR_BYTES:
        raise ConnectorError("Connector manifest exceeds the 64 KB limit")
    return raw


def _strict_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ConnectorError(
            f"Unknown {label} field(s): {', '.join(sorted(str(item) for item in unknown))}"
        )


def _bounded_text(value: Any, label: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise ConnectorError(f"{label} must be a string")
    text = value.strip()
    if not minimum <= len(text) <= maximum or any(
        ord(character) < 32 or ord(character) == 127 for character in text
    ):
        raise ConnectorError(f"{label} is empty, too long, or contains control characters")
    return text


def _validate_base_url(value: Any) -> str:
    text = _bounded_text(value, "base_url", 1, 2048)
    parsed = urllib.parse.urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConnectorError(
            "Connector base_url must be a credential-free HTTPS origin or path"
        )
    try:
        if parsed.port not in {None, 443}:
            raise ConnectorError("Connector base_url must use standard HTTPS port 443")
    except ValueError as exc:
        raise ConnectorError("Connector base_url contains an invalid port") from exc
    host = parsed.hostname.casefold()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise ConnectorError("Connector base_url must use a public host")
    try:
        literal_ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        raise ConnectorError("Connector base_url must use a public host")
    normalized_path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit(("https", parsed.netloc, normalized_path, "", ""))


def _validate_parameter_schema(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConnectorError("Action parameters must be a JSON object schema")
    _strict_keys(value, {"type", "properties", "required", "additionalProperties"}, "parameters")
    if value.get("type") != "object" or value.get("additionalProperties") is not False:
        raise ConnectorError(
            "Action parameters must be an object with additionalProperties=false"
        )
    properties = value.get("properties")
    required = value.get("required", [])
    if not isinstance(properties, dict) or len(properties) > MAX_ARGUMENTS:
        raise ConnectorError(f"Action properties must contain at most {MAX_ARGUMENTS} fields")
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ConnectorError("Action required must be a list of property names")
    if len(required) != len(set(required)) or not set(required).issubset(properties):
        raise ConnectorError("Action required contains duplicate or unknown properties")
    clean_properties: dict[str, Any] = {}
    for name, schema in properties.items():
        if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
            raise ConnectorError("Action property names must be bounded identifiers")
        if is_sensitive_key(name):
            raise ConnectorError("Connector action parameters cannot accept credential fields")
        if not isinstance(schema, dict):
            raise ConnectorError(f"Schema for {name} must be an object")
        _strict_keys(
            schema,
            {"type", "description", "enum", "minLength", "maxLength", "minimum", "maximum"},
            f"property {name}",
        )
        property_type = schema.get("type")
        if property_type not in _PROPERTY_TYPES:
            raise ConnectorError(f"Property {name} has an unsupported type")
        clean = {"type": property_type}
        if "description" in schema:
            clean["description"] = _bounded_text(
                schema["description"], f"description for {name}", 1, 300
            )
        if "enum" in schema:
            enum = schema["enum"]
            if not isinstance(enum, list) or not 1 <= len(enum) <= 50:
                raise ConnectorError(f"Enum for {name} must contain 1 to 50 values")
            expected_type = {
                "string": str,
                "boolean": bool,
                "integer": int,
                "number": (int, float),
            }[property_type]
            if not all(
                isinstance(item, expected_type)
                and not (property_type in {"integer", "number"} and isinstance(item, bool))
                for item in enum
            ):
                raise ConnectorError(f"Enum for {name} contains a value of the wrong type")
            clean["enum"] = enum
        for bound in ("minLength", "maxLength"):
            if bound in schema:
                if property_type != "string" or not isinstance(schema[bound], int):
                    raise ConnectorError(f"{bound} for {name} is invalid")
                clean[bound] = schema[bound]
        for bound in ("minimum", "maximum"):
            if bound in schema:
                if property_type not in {"integer", "number"} or not isinstance(
                    schema[bound], (int, float)
                ) or isinstance(schema[bound], bool):
                    raise ConnectorError(f"{bound} for {name} is invalid")
                clean[bound] = schema[bound]
        if clean.get("minLength", 0) < 0 or clean.get("maxLength", 100_000) > 100_000:
            raise ConnectorError(f"String bounds for {name} are invalid")
        if clean.get("minLength", 0) > clean.get("maxLength", 100_000):
            raise ConnectorError(f"String bounds for {name} are reversed")
        if clean.get("minimum", float("-inf")) > clean.get("maximum", float("inf")):
            raise ConnectorError(f"Numeric bounds for {name} are reversed")
        clean_properties[name] = clean
    return {
        "type": "object",
        "properties": clean_properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _validate_credential(value: Any) -> dict[str, str]:
    if value is None:
        return {"kind": "none"}
    if not isinstance(value, dict):
        raise ConnectorError("credential must be an object")
    _strict_keys(value, {"kind", "environment", "header"}, "credential")
    kind = value.get("kind", "none")
    if kind == "none":
        if set(value) != {"kind"}:
            raise ConnectorError("A credential-free connector cannot declare secret fields")
        return {"kind": "none"}
    if kind not in {"bearer_env", "api_key_header_env"}:
        raise ConnectorError("Unsupported connector credential kind")
    environment = value.get("environment")
    if not isinstance(environment, str) or not _ENVIRONMENT_NAME.fullmatch(environment):
        raise ConnectorError(
            "Connector credentials must reference a JARVIS_CONNECTOR_* environment variable"
        )
    clean = {"kind": kind, "environment": environment}
    if kind == "api_key_header_env":
        header = value.get("header")
        if (
            not isinstance(header, str)
            or not _HEADER_NAME.fullmatch(header)
            or header.casefold() in _BLOCKED_HEADERS
        ):
            raise ConnectorError("Connector API-key header is invalid or protected")
        clean["header"] = header
    elif "header" in value:
        raise ConnectorError("bearer_env always uses the Authorization header")
    return clean


def _manifest_contains_secret(value: Any, path: tuple[str, ...] = ()) -> bool:
    if isinstance(value, str):
        return contains_secret(value)
    if isinstance(value, list):
        return any(_manifest_contains_secret(item, path) for item in value)
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if is_sensitive_key(key_text) and not (not path and key_text == "credential"):
                return True
            if _manifest_contains_secret(item, (*path, key_text)):
                return True
    return False


def _parse_manifest(raw: bytes, *, expected_id: str | None = None) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
        value = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorError("Connector manifest must be valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ConnectorError("Connector manifest must be a JSON object")
    if _manifest_contains_secret(value):
        raise ConnectorError("Connector manifests must never contain credentials or secrets")
    _strict_keys(
        value,
        {"schema_version", "id", "name", "version", "description", "base_url", "credential", "actions"},
        "manifest",
    )
    if value.get("schema_version") != 1:
        raise ConnectorError("Connector schema_version must be 1")
    connector_id = value.get("id")
    if not isinstance(connector_id, str) or not _IDENTIFIER.fullmatch(connector_id):
        raise ConnectorError("Connector id must be a lowercase bounded identifier")
    if expected_id is not None and connector_id != expected_id:
        raise ConnectorError("Connector directory and manifest id do not match")
    actions = value.get("actions")
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
        raise ConnectorError(f"Connector must declare 1 to {MAX_ACTIONS} actions")
    clean_actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for action in actions:
        if not isinstance(action, dict):
            raise ConnectorError("Every connector action must be an object")
        _strict_keys(action, {"name", "description", "method", "path", "risk", "parameters"}, "action")
        action_name = action.get("name")
        if not isinstance(action_name, str) or not _IDENTIFIER.fullmatch(action_name):
            raise ConnectorError("Action names must be bounded identifiers")
        if action_name in seen:
            raise ConnectorError(f"Duplicate connector action: {action_name}")
        seen.add(action_name)
        method = str(action.get("method", "")).upper()
        risk = action.get("risk")
        if method not in {"GET", "POST"}:
            raise ConnectorError("Declarative connectors currently support GET and POST only")
        expected_risk = "external_read" if method == "GET" else "external_mutation"
        if risk != expected_risk:
            raise ConnectorError(f"{method} action {action_name} must use risk={expected_risk}")
        path = _bounded_text(action.get("path"), f"path for {action_name}", 1, 1000)
        parsed_path = urllib.parse.urlsplit(path)
        if (
            not path.startswith("/")
            or path.startswith("//")
            or parsed_path.scheme
            or parsed_path.netloc
            or parsed_path.query
            or parsed_path.fragment
            or any(part == ".." for part in parsed_path.path.split("/"))
        ):
            raise ConnectorError("Action paths must be fixed absolute URL paths without a query")
        parameters = _validate_parameter_schema(action.get("parameters"))
        path_parameters = _PATH_PARAMETER.findall(path)
        if len(path_parameters) != len(set(path_parameters)):
            raise ConnectorError("Action path parameters cannot repeat")
        if not set(path_parameters).issubset(parameters["required"]):
            raise ConnectorError("Every path parameter must be required by the action schema")
        if "{" in _PATH_PARAMETER.sub("", path) or "}" in _PATH_PARAMETER.sub("", path):
            raise ConnectorError("Action path contains an invalid parameter placeholder")
        clean_actions.append({
            "name": action_name,
            "description": _bounded_text(
                action.get("description"), f"description for {action_name}", 1, 500
            ),
            "method": method,
            "path": path,
            "risk": risk,
            "parameters": parameters,
        })
    return {
        "schema_version": 1,
        "id": connector_id,
        "name": _bounded_text(value.get("name"), "connector name", 1, 100),
        "version": _bounded_text(value.get("version"), "connector version", 1, 40),
        "description": _bounded_text(value.get("description"), "connector description", 1, 500),
        "base_url": _validate_base_url(value.get("base_url")),
        "credential": _validate_credential(value.get("credential", {"kind": "none"})),
        "actions": clean_actions,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _validate_arguments(schema: dict[str, Any], arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ConnectorError("Connector arguments must be a JSON object")
    properties = schema["properties"]
    unknown = set(arguments) - set(properties)
    missing = set(schema["required"]) - set(arguments)
    if unknown:
        raise ConnectorError(f"Unknown connector argument(s): {', '.join(sorted(unknown))}")
    if missing:
        raise ConnectorError(f"Missing connector argument(s): {', '.join(sorted(missing))}")
    clean: dict[str, Any] = {}
    for name, value in arguments.items():
        spec = properties[name]
        expected = spec["type"]
        valid = (
            expected == "string" and isinstance(value, str)
            or expected == "boolean" and isinstance(value, bool)
            or expected == "integer" and isinstance(value, int) and not isinstance(value, bool)
            or expected == "number" and isinstance(value, (int, float)) and not isinstance(value, bool)
        )
        if not valid:
            raise ConnectorError(f"Connector argument {name} must be {expected}")
        if isinstance(value, str):
            if contains_secret(value):
                raise ConnectorError("Connector arguments cannot contain credentials or secrets")
            if len(value) < spec.get("minLength", 0) or len(value) > spec.get("maxLength", 100_000):
                raise ConnectorError(f"Connector argument {name} violates its length bound")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value < spec.get("minimum", float("-inf")) or value > spec.get("maximum", float("inf")):
                raise ConnectorError(f"Connector argument {name} violates its numeric bound")
        if "enum" in spec and value not in spec["enum"]:
            raise ConnectorError(f"Connector argument {name} is not an allowed value")
        clean[name] = value
    return clean


class CapabilityGateway:
    """Load and invoke declarative operator-installed HTTPS connectors."""

    def __init__(self, workspace: Path, data_dir: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.root = (Path(data_dir).resolve() / "connectors")

    @staticmethod
    def validate_manifest_document(document: str) -> dict[str, Any]:
        """Validate an in-memory connector draft before it touches the workspace."""
        if not isinstance(document, str):
            raise ConnectorError("Connector manifest must be a UTF-8 JSON string")
        raw = document.encode("utf-8")
        if len(raw) > MAX_CONNECTOR_BYTES:
            raise ConnectorError("Connector manifest exceeds the 64 KB limit")
        manifest = _parse_manifest(raw)
        return {
            "id": manifest["id"],
            "name": manifest["name"],
            "version": manifest["version"],
            "description": manifest["description"],
            "actions": [
                {key: action[key] for key in ("name", "method", "risk")}
                for action in manifest["actions"]
            ],
            "credential_reference": manifest["credential"].get("environment"),
            "manifest_sha256": manifest["manifest_sha256"],
            "valid": True,
        }

    def _ensure_root(self, *, create: bool = False) -> bool:
        if not os.path.lexists(self.root):
            if not create:
                return False
            self.root.mkdir(parents=True)
        details = os.lstat(self.root)
        attributes = getattr(details, "st_file_attributes", 0)
        if (
            stat.S_ISLNK(details.st_mode)
            or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or not stat.S_ISDIR(details.st_mode)
        ):
            raise ConnectorError("Connector root must be an ordinary non-link directory")
        return True

    def _installed_manifest(self, connector_id: str) -> tuple[dict[str, Any], bytes]:
        normalized = str(connector_id).strip()
        if not _IDENTIFIER.fullmatch(normalized):
            raise ConnectorError("Connector id must be a lowercase bounded identifier")
        if not self._ensure_root():
            raise ConnectorError(f"Unknown connector: {normalized}")
        directory = self.root / normalized
        try:
            directory_details = os.lstat(directory)
        except OSError as exc:
            raise ConnectorError(f"Unknown connector: {normalized}") from exc
        attributes = getattr(directory_details, "st_file_attributes", 0)
        if (
            stat.S_ISLNK(directory_details.st_mode)
            or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or not stat.S_ISDIR(directory_details.st_mode)
        ):
            raise ConnectorError("Connector directories must be ordinary non-link directories")
        raw = _ordinary_file(directory / "connector.json")
        return _parse_manifest(raw, expected_id=normalized), raw

    def list_connectors(self) -> list[dict[str, Any]]:
        if not self._ensure_root():
            return []
        results: list[dict[str, Any]] = []
        for directory in sorted(self.root.iterdir(), key=lambda item: item.name)[:MAX_CONNECTORS]:
            try:
                manifest, _raw = self._installed_manifest(directory.name)
            except (OSError, ConnectorError):
                continue
            results.append({
                "id": manifest["id"],
                "name": manifest["name"],
                "version": manifest["version"],
                "description": manifest["description"],
                "actions": [
                    {key: action[key] for key in ("name", "description", "method", "risk")}
                    for action in manifest["actions"]
                ],
                "credential": {
                    "kind": manifest["credential"]["kind"],
                    "configured": self._credential_configured(manifest["credential"]),
                },
                "trust": "operator-installed declarative connector",
            })
        return results

    def describe(self, connector_id: str) -> dict[str, Any]:
        manifest, _raw = self._installed_manifest(connector_id)
        return {
            key: manifest[key]
            for key in ("id", "name", "version", "description", "base_url", "actions", "manifest_sha256")
        } | {
            "credential": {
                "kind": manifest["credential"]["kind"],
                "reference": manifest["credential"].get("environment"),
                "configured": self._credential_configured(manifest["credential"]),
            },
            "trust": "operator-installed declarative connector",
        }

    @staticmethod
    def _credential_configured(credential: dict[str, str]) -> bool:
        if credential["kind"] == "none":
            return True
        return bool(os.getenv(credential["environment"]))

    def validate_workspace_manifest(self, path: str) -> dict[str, Any]:
        source = resolve_workspace_path(self.workspace, path)
        raw = _ordinary_file(source)
        manifest = _parse_manifest(raw)
        return {
            "path": str(source),
            "id": manifest["id"],
            "name": manifest["name"],
            "version": manifest["version"],
            "description": manifest["description"],
            "actions": [
                {key: action[key] for key in ("name", "method", "risk")}
                for action in manifest["actions"]
            ],
            "credential_reference": manifest["credential"].get("environment"),
            "manifest_sha256": manifest["manifest_sha256"],
            "valid": True,
        }

    def install_snapshot(self, path: str) -> dict[str, Any]:
        return self.validate_workspace_manifest(path)

    def install(self, path: str, *, expected_snapshot: dict[str, Any] | None) -> dict[str, Any]:
        snapshot = self.install_snapshot(path)
        if expected_snapshot is None or snapshot != expected_snapshot:
            raise PermissionError("Connector manifest changed after approval")
        connector_id = snapshot["id"]
        destination = self.root / connector_id
        if os.path.lexists(destination):
            raise ConnectorError(
                "Connector is already installed; connector replacement is intentionally unsupported"
            )
        raw = _ordinary_file(Path(snapshot["path"]))
        if hashlib.sha256(raw).hexdigest() != snapshot["manifest_sha256"]:
            raise PermissionError("Connector manifest changed after approval")
        self._ensure_root(create=True)
        destination.mkdir()
        manifest_path = destination / "connector.json"
        try:
            with manifest_path.open("xb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            try:
                manifest_path.unlink(missing_ok=True)
                destination.rmdir()
            except OSError:
                pass
            raise
        installed, _installed_raw = self._installed_manifest(connector_id)
        if installed["manifest_sha256"] != snapshot["manifest_sha256"]:
            raise PermissionError("Installed connector failed its integrity recheck")
        return {
            "installed": True,
            "id": connector_id,
            "name": installed["name"],
            "version": installed["version"],
            "actions": [action["name"] for action in installed["actions"]],
            "manifest_sha256": installed["manifest_sha256"],
        }

    def _prepare(self, connector_id: str, action_name: str, arguments: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        manifest, _raw = self._installed_manifest(connector_id)
        action = next(
            (item for item in manifest["actions"] if item["name"] == action_name),
            None,
        )
        if action is None:
            raise ConnectorError(f"Unknown action for {manifest['id']}: {action_name}")
        clean_arguments = _validate_arguments(action["parameters"], arguments)
        path = action["path"]
        consumed: set[str] = set()
        for parameter in _PATH_PARAMETER.findall(path):
            path = path.replace(
                "{" + parameter + "}",
                urllib.parse.quote(str(clean_arguments[parameter]), safe=""),
            )
            consumed.add(parameter)
        payload = {
            key: value for key, value in clean_arguments.items() if key not in consumed
        }
        url = manifest["base_url"].rstrip("/") + path
        data: bytes | None = None
        if action["method"] == "GET" and payload:
            url += "?" + urllib.parse.urlencode(payload)
        elif action["method"] == "POST":
            data = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        snapshot = {
            "connector_id": manifest["id"],
            "connector_name": manifest["name"],
            "connector_version": manifest["version"],
            "connector_manifest_sha256": manifest["manifest_sha256"],
            "action": action["name"],
            "action_description": action["description"],
            "risk": action["risk"],
            "request_method": action["method"],
            "request_url": url,
            "request_arguments_json": json.dumps(
                clean_arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "credential_reference": manifest["credential"].get("environment"),
        }
        request = {
            "url": url,
            "data": data,
            "credential": manifest["credential"],
            "snapshot": snapshot,
        }
        return snapshot, request

    def approval_snapshot(self, connector_id: str, action: str, arguments: Any) -> dict[str, Any]:
        snapshot, _request = self._prepare(connector_id, action, arguments)
        return snapshot

    def call(
        self,
        connector_id: str,
        action: str,
        arguments: Any,
        *,
        expected_snapshot: dict[str, Any] | None,
        transport: Callable[..., str],
    ) -> dict[str, Any]:
        snapshot, request = self._prepare(connector_id, action, arguments)
        if expected_snapshot is None or snapshot != expected_snapshot:
            raise PermissionError("Connector target changed after approval")
        headers = {"Accept": "application/json, text/plain;q=0.8"}
        if request["data"] is not None:
            headers["Content-Type"] = "application/json"
        credential = request["credential"]
        if credential["kind"] != "none":
            secret = os.getenv(credential["environment"])
            if not secret:
                raise ConnectorError(
                    f"Connector credential is not configured: {credential['environment']}"
                )
            if credential["kind"] == "bearer_env":
                headers["Authorization"] = f"Bearer {secret}"
            else:
                headers[credential["header"]] = secret
        raw = transport(
            request["url"],
            request["data"],
            headers,
            allow_redirects=False,
        )
        safe = redact_secrets(raw)
        if len(safe) > MAX_RESPONSE_CHARACTERS:
            safe = safe[:12_000] + "\n...[connector response clipped]...\n" + safe[-12_000:]
        try:
            result: Any = json.loads(safe)
        except json.JSONDecodeError:
            result = safe
        return {
            "connector": snapshot["connector_id"],
            "action": snapshot["action"],
            "method": snapshot["request_method"],
            "url": snapshot["request_url"],
            "result": result,
        }
