from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .ollama_client import normalize_ollama_keep_alive, normalize_ollama_url
from .home_assistant import normalize_home_assistant_url


PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT.parent
SOURCE_CHECKOUT = (SOURCE_ROOT / "pyproject.toml").is_file()
ROOT = (
    SOURCE_ROOT
    if SOURCE_CHECKOUT
    else Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "JarvisLocal"
)
PACKAGED_SOUL = PACKAGE_ROOT / "SOUL.md"
PACKAGED_CONSTITUTION = PACKAGE_ROOT / "CONSTITUTION.md"
_DOTENV_KEYS = frozenset({
    "JARVIS_WORKSPACE",
    "JARVIS_DATA",
    "JARVIS_VAULT",
    "JARVIS_SOUL",
    "JARVIS_CONSTITUTION",
    "JARVIS_MODEL",
    "JARVIS_FAST_MODEL",
    "JARVIS_REASONING_MODEL",
    "JARVIS_CODING_MODEL",
    "JARVIS_DEEP_MODEL",
    "JARVIS_BACKGROUND_MODEL",
    "JARVIS_LEARNING_MODEL",
    "JARVIS_OLLAMA_URL",
    "JARVIS_OLLAMA_ENABLED",
    "JARVIS_OLLAMA_ALLOW_REMOTE",
    "JARVIS_OLLAMA_HEALTH_TIMEOUT",
    "JARVIS_OLLAMA_GENERATION_TIMEOUT",
    "JARVIS_OLLAMA_MAX_OUTPUT_TOKENS",
    "JARVIS_OLLAMA_MAX_RESPONSE_BYTES",
    "JARVIS_OLLAMA_MAX_RETRIES",
    "JARVIS_OLLAMA_RETRY_BACKOFF",
    "JARVIS_OLLAMA_KEEP_ALIVE",
    "JARVIS_OLLAMA_DEEP_KEEP_ALIVE",
    "JARVIS_OLLAMA_NUM_THREAD",
    "JARVIS_OLLAMA_PRELOAD",
    "JARVIS_REASONING_THINKING",
    "JARVIS_CLOUD_ENABLED",
    "JARVIS_CLOUD_GENERATION_TIMEOUT",
    "JARVIS_CLOUD_MAX_OUTPUT_TOKENS",
    "JARVIS_CLOUD_MAX_RESPONSE_BYTES",
    "JARVIS_CLOUD_MAX_RETRIES",
    "JARVIS_CLOUD_RETRY_BACKOFF",
    "JARVIS_OPENAI_API_ENABLED",
    "JARVIS_OPENAI_IMAGES_ENABLED",
    "JARVIS_ANTHROPIC_API_ENABLED",
    "JARVIS_CODEX_CLI_ENABLED",
    "JARVIS_CLAUDE_CLI_ENABLED",
    "JARVIS_COUNCIL_CHAIR_MODEL",
    "JARVIS_COUNCIL_MEMBER_MODEL",
    "JARVIS_COUNCIL_CHAIR_EFFORT",
    "JARVIS_COUNCIL_MEMBER_EFFORT",
    "JARVIS_MAX_STEPS",
    "JARVIS_CONTEXT_LENGTH",
    "JARVIS_FAST_CONTEXT_LENGTH",
    "JARVIS_REASONING_CONTEXT_LENGTH",
    "JARVIS_CODING_CONTEXT_LENGTH",
    "JARVIS_DEEP_CONTEXT_LENGTH",
    "JARVIS_COMMAND_TIMEOUT",
    "JARVIS_EXECUTION_MODE",
    "JARVIS_EXECUTION_BACKEND",
    "JARVIS_COMPUTER_ACCESS",
    "JARVIS_COMPUTER_ROOT",
    "JARVIS_NETWORK_ACCESS",
    "JARVIS_NETWORK_MONITOR_ENABLED",
    "JARVIS_NETWORK_MONITOR_INTERVAL_SECONDS",
    "JARVIS_NETWORK_DEFENSE_MODE",
    "JARVIS_NETWORK_INCIDENT_POPUPS_ENABLED",
    "JARVIS_BLUETOOTH_ACCESS",
    "JARVIS_BLUETOOTH_MONITOR_ENABLED",
    "JARVIS_BLUETOOTH_MONITOR_INTERVAL_SECONDS",
    "JARVIS_HOME_ASSISTANT_ACCESS",
    "JARVIS_HOME_ASSISTANT_NETWORK_ACCESS",
    "JARVIS_HOME_ASSISTANT_URL",
    "JARVIS_HOME_ASSISTANT_TOKEN",
    "JARVIS_HOME_ASSISTANT_ENTITIES",
    "JARVIS_EXTERNAL_ACCESS",
    "JARVIS_GOOGLE_DRIVE_ACCESS",
    "JARVIS_AUTONOMY",
    "JARVIS_PROACTIVE_ENABLED",
    "JARVIS_PROACTIVE_IDLE_SECONDS",
    "JARVIS_PROACTIVE_MAX_TASK_SECONDS",
    "JARVIS_PROACTIVE_DAILY_TASK_LIMIT",
    "JARVIS_DAILY_TOOL_LIMIT",
    "JARVIS_MODEL_CALL_LIMIT_PER_REQUEST",
    "JARVIS_PROMPT_TOKEN_LIMIT_PER_REQUEST",
    "JARVIS_COMPLETION_TOKEN_LIMIT_PER_REQUEST",
    "JARVIS_SPECIALIST_DELEGATION_LIMIT_PER_REQUEST",
    "JARVIS_MEMORY_AUTO_IMPROVE",
    "JARVIS_MEMORY_EMBEDDINGS",
    "JARVIS_MEMORY_EMBEDDING_MODEL",
    "JARVIS_MEMORY_EMBEDDING_DIMENSIONS",
    "JARVIS_MEMORY_CLAIM_CLOCK",
    "JARVIS_MEMORY_CLAIM_STALE_THRESHOLD",
    "JARVIS_APPROVAL_TTL_HOURS",
    "JARVIS_SELF_INSPECT",
    "JARVIS_SELF_REPAIR",
    "JARVIS_INITIATIVE",
    "JARVIS_INITIATIVE_QUIET_HOURS",
    "JARVIS_PRESENCE_HOST",
    "JARVIS_PRESENCE_REMOTE_ACCESS",
    "JARVIS_PRESENCE_TRUSTED_HOSTS",
    "JARVIS_PRESENCE_PORT",
    "JARVIS_PRESENCE_MAX_AGENTS",
    "JARVIS_WORKER_CONCURRENCY",
    "JARVIS_SCREEN_COMPANION",
    "JARVIS_SCREEN_COMPANION_INDICATOR",
    "JARVIS_SCREEN_COMPANION_POLL_SECONDS",
    "JARVIS_SCREEN_COMPANION_STABLE_SECONDS",
    "JARVIS_SCREEN_COMPANION_AUTO_COOLDOWN_SECONDS",
    "JARVIS_PUBLIC_PRESENCE_ENABLED",
    "JARVIS_GATEWAY_CHANNEL",
    "JARVIS_GATEWAY_TOKEN",
    "JARVIS_GATEWAY_ALLOWED_IDS",
    "OLLAMA_API_KEY",
})
MAX_DOTENV_BYTES = 64 * 1024
MAX_CONSTITUTION_BYTES = 32 * 1024
MAX_SOUL_BYTES = 16 * 1024




def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    details = os.lstat(path)
    attributes = getattr(details, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(details.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not stat.S_ISREG(details.st_mode)
    ):
        raise ValueError(".env must be an ordinary file")
    if details.st_size > MAX_DOTENV_BYTES:
        raise ValueError(f".env exceeds {MAX_DOTENV_BYTES} bytes")
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Malformed .env entry on line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in _DOTENV_KEYS:
            raise ValueError(f"Unsupported .env setting on line {line_number}: {key}")
        os.environ.setdefault(key, value.strip().strip("'\""))




def _seed_default_soul(path: Path) -> None:
    if path.exists() or not PACKAGED_SOUL.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as destination:
            destination.write(PACKAGED_SOUL.read_text(encoding="utf-8"))
    except FileExistsError:
        pass


def _seed_default_constitution(path: Path) -> None:
    if os.path.lexists(path) or not PACKAGED_CONSTITUTION.is_file():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as destination:
            destination.write(PACKAGED_CONSTITUTION.read_text(encoding="utf-8"))
    except FileExistsError:
        pass
    except (FileNotFoundError, PermissionError):
        # A read-only installation can safely use the packaged copy directly.
        pass


def load_constitution(path: Path) -> tuple[str, str]:
    """Read a bounded ordinary constitution file and return its text and SHA-256."""
    path = Path(path)
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ValueError("JARVIS constitution file is missing or inaccessible") from exc
    attributes = getattr(before, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(before.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise ValueError("JARVIS constitution must be an ordinary non-link file")
    if before.st_size > MAX_CONSTITUTION_BYTES:
        raise ValueError(
            f"JARVIS constitution exceeds {MAX_CONSTITUTION_BYTES} bytes"
        )
    try:
        raw = path.read_bytes()
        after = os.lstat(path)
    except OSError as exc:
        raise ValueError("JARVIS constitution file could not be read safely") from exc
    after_attributes = getattr(after, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(after.st_mode)
        or after_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not stat.S_ISREG(after.st_mode)
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ValueError("JARVIS constitution changed while it was being read")
    if len(raw) > MAX_CONSTITUTION_BYTES:
        raise ValueError(
            f"JARVIS constitution exceeds {MAX_CONSTITUTION_BYTES} bytes"
        )
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("JARVIS constitution must be valid UTF-8 text") from exc
    if not content.strip():
        raise ValueError("JARVIS constitution must not be empty")
    return content, hashlib.sha256(raw).hexdigest()


def load_soul(path: Path) -> str:
    """Read the bounded, operator-controlled personality file without following links."""
    path = Path(path)
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise ValueError("JARVIS Soul file is missing or inaccessible") from exc
    attributes = getattr(before, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(before.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise ValueError("JARVIS Soul must be an ordinary non-link file")
    if before.st_size > MAX_SOUL_BYTES:
        raise ValueError(f"JARVIS Soul exceeds {MAX_SOUL_BYTES} bytes")
    try:
        raw = path.read_bytes()
        after = os.lstat(path)
    except OSError as exc:
        raise ValueError("JARVIS Soul file could not be read safely") from exc
    after_attributes = getattr(after, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(after.st_mode)
        or after_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not stat.S_ISREG(after.st_mode)
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ValueError("JARVIS Soul changed while it was being read")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("JARVIS Soul must be valid UTF-8 text") from exc
    if not content.strip():
        raise ValueError("JARVIS Soul must not be empty")
    return content

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


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _normalize_presence_trusted_host(value: str) -> str:
    """Normalize one exact Host value; ports and wildcard patterns are forbidden."""
    host = str(value).strip().casefold().rstrip(".")
    if not host or len(host) > 253 or any(ch.isspace() for ch in host):
        raise ValueError("JARVIS_PRESENCE_TRUSTED_HOSTS contains an invalid host")
    try:
        return ipaddress.ip_address(host).compressed.casefold()
    except ValueError:
        pass
    labels = host.split(".")
    if any(
        not label
        or len(label) > 63
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
        for label in labels
    ):
        raise ValueError("JARVIS_PRESENCE_TRUSTED_HOSTS contains an invalid host")
    return host


def _env_presence_trusted_hosts() -> tuple[str, ...]:
    raw = os.getenv("JARVIS_PRESENCE_TRUSTED_HOSTS", "").strip()
    if not raw:
        return ()
    values = [item.strip() for item in raw.split(",")]
    if len(values) > 16:
        raise ValueError("JARVIS_PRESENCE_TRUSTED_HOSTS accepts at most 16 hosts")
    return tuple(dict.fromkeys(_normalize_presence_trusted_host(item) for item in values))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _require_ordinary_directory(path: Path, label: str) -> None:
    try:
        details = os.lstat(path)
    except OSError as exc:
        raise PermissionError(f"{label} is unavailable") from exc
    attributes = getattr(details, "st_file_attributes", 0)
    if (
        stat.S_ISLNK(details.st_mode)
        or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        or not stat.S_ISDIR(details.st_mode)
    ):
        raise PermissionError(f"{label} must be an ordinary directory")


def resolve_project_workspace(config: "Config", relative_path: str) -> Path:
    """Resolve the default workspace or one isolated sibling project workspace."""
    default_workspace = config.workspace.resolve()
    relative = str(relative_path).strip().replace("\\", "/")
    if not relative:
        raise ValueError("Project path is empty")
    if relative == ".":
        base = default_workspace
        _require_ordinary_directory(base, "Default workspace")
        candidate = base
        components: list[str] = []
    else:
        match = re.fullmatch(
            r"@projects/([a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?)",
            relative,
        )
        if match is None:
            raise ValueError("Project path is not canonical")
        projects = default_workspace.parent / f"{default_workspace.name}-projects"
        _require_ordinary_directory(projects, "Workspace projects container")
        base = projects.resolve(strict=True)
        components = [match.group(1)]
        candidate = base / components[0]
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(base)
    except (OSError, ValueError) as exc:
        raise PermissionError(
            "Project workspace is unavailable or escapes the workspace root"
        ) from exc
    current = base
    if components:
        for part in components:
            current = current / part
            _require_ordinary_directory(current, "Project workspace")
    return resolved


def create_project_workspace(config: "Config", slug: str) -> tuple[Path, str]:
    """Create one ordinary project directory outside the default workspace."""
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?", str(slug)):
        raise ValueError("Project slug must contain only lowercase letters, numbers, and hyphens")
    projects = config.workspace.parent / f"{config.workspace.name}-projects"
    if os.path.lexists(projects):
        _require_ordinary_directory(projects, "Workspace projects container")
    else:
        projects.mkdir()
    root = projects / slug
    if os.path.lexists(root):
        raise ValueError("A project directory with that name already exists")
    root.mkdir()
    relative = f"@projects/{slug}"
    try:
        resolved = resolve_project_workspace(config, relative)
    except Exception:
        try:
            root.rmdir()
        except OSError:
            pass
        raise
    return resolved, relative


@dataclass(frozen=True)
class Config:
    root: Path
    workspace: Path
    data_dir: Path
    soul_path: Path
    model: str
    fast_model: str
    reasoning_model: str
    coding_model: str
    ollama_url: str
    ollama_api_key: str | None = field(repr=False)
    max_steps: int
    context_length: int
    command_timeout: int
    autonomy: str
    deep_model: str = "qwen3-coder:30b"
    execution_mode: str = "disabled"
    execution_backend: str = "host"
    computer_access: str = "disabled"
    computer_root: Path | None = None
    network_access: str = "disabled"
    network_monitor_enabled: bool = False
    network_monitor_interval_seconds: int = 300
    network_defense_mode: str = "disabled"
    network_incident_popups_enabled: bool = False
    bluetooth_access: str = "disabled"
    bluetooth_monitor_enabled: bool = False
    bluetooth_monitor_interval_seconds: int = 60
    home_assistant_access: str = "disabled"
    home_assistant_network_access: str = "disabled"
    home_assistant_url: str = ""
    home_assistant_token: str | None = field(default=None, repr=False)
    home_assistant_entities: tuple[str, ...] = ()
    constitution_path: Path | None = None
    background_model: str = "fast"
    learning_model: str | None = None
    ollama_enabled: bool = True
    ollama_allow_remote: bool = False
    ollama_health_timeout: float = 5.0
    ollama_generation_timeout: float = 600.0
    ollama_max_output_tokens: int = 2048
    ollama_max_response_bytes: int = 8 * 1024 * 1024
    ollama_max_retries: int = 2
    ollama_retry_backoff: float = 0.25
    ollama_keep_alive: str = "30m"
    ollama_deep_keep_alive: str = "0"
    ollama_num_thread: int | None = None
    ollama_preload: bool = False
    reasoning_thinking: bool = True
    cloud_enabled: bool = True
    cloud_generation_timeout: float = 600.0
    cloud_max_output_tokens: int = 8192
    cloud_max_response_bytes: int = 8 * 1024 * 1024
    cloud_max_retries: int = 2
    cloud_retry_backoff: float = 0.5
    openai_api_enabled: bool = False
    openai_images_enabled: bool = False
    anthropic_api_enabled: bool = False
    codex_cli_enabled: bool = False
    claude_cli_enabled: bool = False
    fast_context_length: int | None = None
    reasoning_context_length: int | None = None
    coding_context_length: int | None = None
    deep_context_length: int | None = None
    proactive_enabled: bool = False
    proactive_idle_seconds: int = 300
    proactive_max_task_seconds: int = 1800
    proactive_daily_task_limit: int = 4
    daily_tool_limit: int = 500
    model_call_limit_per_request: int = 48
    prompt_token_limit_per_request: int = 400_000
    completion_token_limit_per_request: int = 40_000
    specialist_delegation_limit_per_request: int = 4
    approval_ttl_hours: int = 24
    external_access: str = "disabled"
    google_drive_access: str = "app_files"
    self_inspect: str = "disabled"
    self_repair: str = "disabled"
    initiative: str = "disabled"
    initiative_quiet_hours: str = ""
    presence_host: str = "127.0.0.1"
    presence_remote_access: str = "disabled"
    presence_trusted_hosts: tuple[str, ...] = ()
    presence_port: int = 8787
    presence_max_agents: int = 3
    screen_companion_mode: str = "disabled"
    screen_companion_indicator: bool = True
    screen_companion_poll_seconds: float = 2.0
    screen_companion_stable_seconds: float = 8.0
    screen_companion_auto_cooldown_seconds: int = 900
    public_presence_enabled: bool = False
    worker_concurrency: int = 3
    gateway_channel: str = ""
    gateway_token: str | None = field(default=None, repr=False)
    gateway_allowed_ids: tuple[str, ...] = ()
    memory_auto_improve: bool = True
    strategy_transfer: str = "observe"
    memory_embeddings: str = "disabled"
    memory_embedding_model: str = "text-embedding-3-small"
    memory_embedding_dimensions: int = 512
    memory_claim_clock: str = "shadow"
    memory_claim_stale_threshold: float = 0.70
    vault_dir: Path | None = None

    @property
    def constitution_sha256(self) -> str:
        if self.constitution_path is None:
            raise ValueError("JARVIS constitution path is not configured")
        return load_constitution(self.constitution_path)[1]

    @classmethod
    def load(cls) -> "Config":
        _load_dotenv(ROOT / ".env")
        default_constitution = Path(os.path.abspath(ROOT / "CONSTITUTION.md"))
        constitution_path = Path(
            os.path.abspath(os.getenv("JARVIS_CONSTITUTION", default_constitution))
        )
        if constitution_path == default_constitution:
            _seed_default_constitution(constitution_path)
            if not os.path.lexists(constitution_path) and PACKAGED_CONSTITUTION.is_file():
                constitution_path = PACKAGED_CONSTITUTION
        raw_context_length = os.getenv("JARVIS_CONTEXT_LENGTH")
        context_length = _env_int("JARVIS_CONTEXT_LENGTH", 32768, 4096, 262144)

        def profile_context(name: str, default: int) -> int:
            if os.getenv(name) is not None:
                return _env_int(name, default, 4096, 262144)
            if raw_context_length is not None:
                return context_length
            return default

        ollama_allow_remote = _env_bool("JARVIS_OLLAMA_ALLOW_REMOTE", False)
        raw_ollama_num_thread = os.getenv("JARVIS_OLLAMA_NUM_THREAD")
        ollama_url = normalize_ollama_url(
            os.getenv("JARVIS_OLLAMA_URL", "http://127.0.0.1:11434"),
            allow_remote=ollama_allow_remote,
        )
        raw_vault = os.getenv("JARVIS_VAULT", "").strip()
        vault_dir: Path | None = None
        if raw_vault:
            vault_lexical = Path(raw_vault)
            if not vault_lexical.is_absolute():
                raise ValueError("JARVIS_VAULT is not an absolute path")
            if os.path.lexists(vault_lexical):
                _require_ordinary_directory(vault_lexical, "JARVIS_VAULT")
                vault_dir = vault_lexical.resolve(strict=True)
                _require_ordinary_directory(vault_dir, "JARVIS_VAULT")
            else:
                vault_dir = vault_lexical.resolve(strict=False)
        cfg = cls(
            root=ROOT,
            workspace=Path(os.getenv("JARVIS_WORKSPACE", ROOT / "workspace")).resolve(),
            data_dir=Path(os.getenv("JARVIS_DATA", ROOT / "data")).resolve(),
            soul_path=Path(os.getenv("JARVIS_SOUL", ROOT / "SOUL.md")).resolve(),
            model=os.getenv("JARVIS_MODEL", "auto"),
            fast_model=os.getenv("JARVIS_FAST_MODEL", "qwen3.5:9b"),
            reasoning_model=os.getenv("JARVIS_REASONING_MODEL", "gpt-oss:20b"),
            coding_model=os.getenv("JARVIS_CODING_MODEL", "qwen3-coder:30b"),
            deep_model=os.getenv("JARVIS_DEEP_MODEL", "qwen3-coder:30b"),
            background_model=os.getenv("JARVIS_BACKGROUND_MODEL", "fast").strip(),
            learning_model=(os.getenv("JARVIS_LEARNING_MODEL") or "").strip() or None,
            ollama_enabled=_env_bool("JARVIS_OLLAMA_ENABLED", True),
            ollama_url=ollama_url,
            ollama_api_key=os.getenv("OLLAMA_API_KEY") or None,
            max_steps=_env_int("JARVIS_MAX_STEPS", 20, 1, 100),
            context_length=context_length,
            fast_context_length=profile_context("JARVIS_FAST_CONTEXT_LENGTH", 16384),
            reasoning_context_length=profile_context("JARVIS_REASONING_CONTEXT_LENGTH", 16384),
            coding_context_length=profile_context("JARVIS_CODING_CONTEXT_LENGTH", 16384),
            deep_context_length=profile_context("JARVIS_DEEP_CONTEXT_LENGTH", 4096),
            command_timeout=_env_int("JARVIS_COMMAND_TIMEOUT", 120, 5, 600),
            execution_mode=os.getenv("JARVIS_EXECUTION_MODE", "disabled").strip().lower(),
            execution_backend=os.getenv(
                "JARVIS_EXECUTION_BACKEND", "host"
            ).strip().lower(),
            computer_access=os.getenv("JARVIS_COMPUTER_ACCESS", "disabled").strip().lower(),
            computer_root=Path(os.getenv("JARVIS_COMPUTER_ROOT", ROOT.parent)).resolve(),
            network_access=os.getenv("JARVIS_NETWORK_ACCESS", "disabled").strip().lower(),
            network_monitor_enabled=_env_bool(
                "JARVIS_NETWORK_MONITOR_ENABLED", False
            ),
            network_monitor_interval_seconds=_env_int(
                "JARVIS_NETWORK_MONITOR_INTERVAL_SECONDS", 300, 60, 3_600
            ),
            network_defense_mode=os.getenv(
                "JARVIS_NETWORK_DEFENSE_MODE", "disabled"
            ).strip().lower(),
            network_incident_popups_enabled=_env_bool(
                "JARVIS_NETWORK_INCIDENT_POPUPS_ENABLED", False
            ),
            bluetooth_access=os.getenv(
                "JARVIS_BLUETOOTH_ACCESS", "disabled"
            ).strip().lower(),
            bluetooth_monitor_enabled=_env_bool(
                "JARVIS_BLUETOOTH_MONITOR_ENABLED", False
            ),
            bluetooth_monitor_interval_seconds=_env_int(
                "JARVIS_BLUETOOTH_MONITOR_INTERVAL_SECONDS", 60, 60, 3_600
            ),
            home_assistant_access=os.getenv(
                "JARVIS_HOME_ASSISTANT_ACCESS", "disabled"
            ).strip().lower(),
            home_assistant_network_access=os.getenv(
                "JARVIS_HOME_ASSISTANT_NETWORK_ACCESS", "disabled"
            ).strip().lower(),
            home_assistant_url=os.getenv("JARVIS_HOME_ASSISTANT_URL", "").strip(),
            home_assistant_token=(
                os.getenv("JARVIS_HOME_ASSISTANT_TOKEN") or ""
            ).strip() or None,
            home_assistant_entities=tuple(
                item.strip().casefold()
                for item in os.getenv("JARVIS_HOME_ASSISTANT_ENTITIES", "").split(",")
                if item.strip()
            ),
            constitution_path=constitution_path,
            autonomy=os.getenv("JARVIS_AUTONOMY", "autonomous").lower(),
            ollama_allow_remote=ollama_allow_remote,
            ollama_health_timeout=_env_float("JARVIS_OLLAMA_HEALTH_TIMEOUT", 5.0, 0.1, 60.0),
            ollama_generation_timeout=_env_float(
                "JARVIS_OLLAMA_GENERATION_TIMEOUT", 600.0, 1.0, 3600.0
            ),
            ollama_max_output_tokens=_env_int(
                "JARVIS_OLLAMA_MAX_OUTPUT_TOKENS", 2048, 128, 32768
            ),
            ollama_max_response_bytes=_env_int(
                "JARVIS_OLLAMA_MAX_RESPONSE_BYTES", 8 * 1024 * 1024, 1024, 64 * 1024 * 1024
            ),
            ollama_max_retries=_env_int("JARVIS_OLLAMA_MAX_RETRIES", 2, 0, 5),
            ollama_retry_backoff=_env_float("JARVIS_OLLAMA_RETRY_BACKOFF", 0.25, 0.0, 10.0),
            ollama_keep_alive=normalize_ollama_keep_alive(
                os.getenv("JARVIS_OLLAMA_KEEP_ALIVE", "30m")
            ),
            ollama_deep_keep_alive=normalize_ollama_keep_alive(
                os.getenv("JARVIS_OLLAMA_DEEP_KEEP_ALIVE", "0")
            ),
            ollama_num_thread=(
                None
                if raw_ollama_num_thread is None
                else _env_int("JARVIS_OLLAMA_NUM_THREAD", 1, 1, 256)
            ),
            ollama_preload=_env_bool("JARVIS_OLLAMA_PRELOAD", False),
            reasoning_thinking=_env_bool("JARVIS_REASONING_THINKING", True),
            cloud_enabled=_env_bool("JARVIS_CLOUD_ENABLED", True),
            cloud_generation_timeout=_env_float(
                "JARVIS_CLOUD_GENERATION_TIMEOUT", 600.0, 1.0, 3600.0
            ),
            cloud_max_output_tokens=_env_int(
                "JARVIS_CLOUD_MAX_OUTPUT_TOKENS", 8192, 256, 131072
            ),
            cloud_max_response_bytes=_env_int(
                "JARVIS_CLOUD_MAX_RESPONSE_BYTES", 8 * 1024 * 1024, 1024,
                64 * 1024 * 1024,
            ),
            cloud_max_retries=_env_int("JARVIS_CLOUD_MAX_RETRIES", 2, 0, 5),
            cloud_retry_backoff=_env_float(
                "JARVIS_CLOUD_RETRY_BACKOFF", 0.5, 0.0, 10.0
            ),
            openai_api_enabled=_env_bool("JARVIS_OPENAI_API_ENABLED", False),
            openai_images_enabled=_env_bool("JARVIS_OPENAI_IMAGES_ENABLED", False),
            anthropic_api_enabled=_env_bool("JARVIS_ANTHROPIC_API_ENABLED", False),
            codex_cli_enabled=_env_bool("JARVIS_CODEX_CLI_ENABLED", False),
            claude_cli_enabled=_env_bool("JARVIS_CLAUDE_CLI_ENABLED", False),
            proactive_enabled=_env_bool("JARVIS_PROACTIVE_ENABLED", False),
            proactive_idle_seconds=_env_int(
                "JARVIS_PROACTIVE_IDLE_SECONDS", 300, 5, 86_400
            ),
            proactive_max_task_seconds=_env_int(
                "JARVIS_PROACTIVE_MAX_TASK_SECONDS", 1800, 30, 86_400
            ),
            proactive_daily_task_limit=_env_int(
                "JARVIS_PROACTIVE_DAILY_TASK_LIMIT", 4, 0, 100
            ),
            daily_tool_limit=_env_int("JARVIS_DAILY_TOOL_LIMIT", 500, 10, 100_000),
            model_call_limit_per_request=_env_int(
                "JARVIS_MODEL_CALL_LIMIT_PER_REQUEST", 48, 1, 500
            ),
            prompt_token_limit_per_request=_env_int(
                "JARVIS_PROMPT_TOKEN_LIMIT_PER_REQUEST", 400_000, 1_000, 10_000_000
            ),
            completion_token_limit_per_request=_env_int(
                "JARVIS_COMPLETION_TOKEN_LIMIT_PER_REQUEST", 40_000, 256, 1_000_000
            ),
            specialist_delegation_limit_per_request=_env_int(
                "JARVIS_SPECIALIST_DELEGATION_LIMIT_PER_REQUEST", 4, 0, 32
            ),
            memory_auto_improve=_env_bool("JARVIS_MEMORY_AUTO_IMPROVE", True),
            strategy_transfer=os.getenv(
                "JARVIS_STRATEGY_TRANSFER", "observe"
            ).strip().lower(),
            memory_embeddings=os.getenv(
                "JARVIS_MEMORY_EMBEDDINGS", "disabled"
            ).strip().lower(),
            memory_embedding_model=os.getenv(
                "JARVIS_MEMORY_EMBEDDING_MODEL", "text-embedding-3-small"
            ).strip(),
            memory_embedding_dimensions=_env_int(
                "JARVIS_MEMORY_EMBEDDING_DIMENSIONS", 512, 64, 4096
            ),
            memory_claim_clock=os.getenv(
                "JARVIS_MEMORY_CLAIM_CLOCK", "shadow"
            ).strip().lower(),
            memory_claim_stale_threshold=_env_float(
                "JARVIS_MEMORY_CLAIM_STALE_THRESHOLD", 0.70, 0.5, 0.99
            ),
            vault_dir=vault_dir,
            approval_ttl_hours=_env_int("JARVIS_APPROVAL_TTL_HOURS", 24, 1, 720),
            external_access=os.getenv("JARVIS_EXTERNAL_ACCESS", "disabled").strip().lower(),
            google_drive_access=os.getenv(
                "JARVIS_GOOGLE_DRIVE_ACCESS", "app_files"
            ).strip().lower(),
            self_inspect=os.getenv("JARVIS_SELF_INSPECT", "disabled").strip().lower(),
            self_repair=os.getenv("JARVIS_SELF_REPAIR", "disabled").strip().lower(),
            initiative=os.getenv("JARVIS_INITIATIVE", "disabled").strip().lower(),
            initiative_quiet_hours=os.getenv(
                "JARVIS_INITIATIVE_QUIET_HOURS", ""
            ).strip(),
            presence_host=os.getenv("JARVIS_PRESENCE_HOST", "127.0.0.1").strip(),
            presence_remote_access=os.getenv(
                "JARVIS_PRESENCE_REMOTE_ACCESS", "disabled"
            ).strip().lower(),
            presence_trusted_hosts=_env_presence_trusted_hosts(),
            presence_port=_env_int("JARVIS_PRESENCE_PORT", 8787, 1024, 65535),
            presence_max_agents=_env_int("JARVIS_PRESENCE_MAX_AGENTS", 3, 1, 8),
            screen_companion_mode=os.getenv(
                "JARVIS_SCREEN_COMPANION", "disabled"
            ).strip().lower(),
            screen_companion_indicator=_env_bool(
                "JARVIS_SCREEN_COMPANION_INDICATOR", True
            ),
            screen_companion_poll_seconds=_env_float(
                "JARVIS_SCREEN_COMPANION_POLL_SECONDS", 2.0, 0.25, 30.0
            ),
            screen_companion_stable_seconds=_env_float(
                "JARVIS_SCREEN_COMPANION_STABLE_SECONDS", 8.0, 0.0, 300.0
            ),
            screen_companion_auto_cooldown_seconds=_env_int(
                "JARVIS_SCREEN_COMPANION_AUTO_COOLDOWN_SECONDS", 900, 30, 86_400
            ),
            public_presence_enabled=_env_bool(
                "JARVIS_PUBLIC_PRESENCE_ENABLED", False
            ),
            worker_concurrency=_env_int("JARVIS_WORKER_CONCURRENCY", 3, 1, 8),
            gateway_channel=os.getenv("JARVIS_GATEWAY_CHANNEL", "").strip().lower(),
            gateway_token=(os.getenv("JARVIS_GATEWAY_TOKEN") or "").strip() or None,
            gateway_allowed_ids=tuple(
                item.strip()
                for item in os.getenv("JARVIS_GATEWAY_ALLOWED_IDS", "").split(",")
                if item.strip()
            ),
        )
        if cfg.autonomy not in {"autonomous", "readonly"}:
            raise ValueError("JARVIS_AUTONOMY must be 'autonomous' or 'readonly'")
        if cfg.execution_mode not in {"disabled", "trusted-host"}:
            raise ValueError(
                "JARVIS_EXECUTION_MODE must be 'disabled' or 'trusted-host'"
            )
        if cfg.computer_access not in {"disabled", "trusted-desktop"}:
            raise ValueError(
                "JARVIS_COMPUTER_ACCESS must be 'disabled' or 'trusted-desktop'"
            )
        if cfg.network_access not in {"disabled", "private-lan"}:
            raise ValueError(
                "JARVIS_NETWORK_ACCESS must be 'disabled' or 'private-lan'"
            )
        if cfg.network_defense_mode not in {
            "disabled", "alert-only", "safe-readonly"
        }:
            raise ValueError(
                "JARVIS_NETWORK_DEFENSE_MODE must be 'disabled', 'alert-only', "
                "or 'safe-readonly'"
            )
        if cfg.bluetooth_access not in {"disabled", "paired-readonly"}:
            raise ValueError(
                "JARVIS_BLUETOOTH_ACCESS must be 'disabled' or 'paired-readonly'"
            )
        if cfg.home_assistant_access not in {"disabled", "paired"}:
            raise ValueError(
                "JARVIS_HOME_ASSISTANT_ACCESS must be 'disabled' or 'paired'"
            )
        if cfg.home_assistant_network_access not in {"disabled", "netgear-readonly"}:
            raise ValueError(
                "JARVIS_HOME_ASSISTANT_NETWORK_ACCESS must be 'disabled' or "
                "'netgear-readonly'"
            )
        if (
            cfg.home_assistant_access == "paired"
            or cfg.home_assistant_network_access == "netgear-readonly"
        ):
            if not cfg.home_assistant_url:
                raise ValueError("Home Assistant access requires JARVIS_HOME_ASSISTANT_URL")
            normalize_home_assistant_url(cfg.home_assistant_url)
            if cfg.home_assistant_token is None:
                raise ValueError(
                    "Home Assistant access requires JARVIS_HOME_ASSISTANT_TOKEN"
                )
        if cfg.home_assistant_access == "paired":
            if not cfg.home_assistant_entities:
                raise ValueError(
                    "Paired Home Assistant access requires JARVIS_HOME_ASSISTANT_ENTITIES"
                )
        if cfg.home_assistant_token is not None and (
            not 20 <= len(cfg.home_assistant_token) <= 4096
            or any(ord(character) < 32 for character in cfg.home_assistant_token)
        ):
            raise ValueError("JARVIS_HOME_ASSISTANT_TOKEN is invalid")
        if len(cfg.home_assistant_entities) > 64 or any(
            re.fullmatch(r"remote\.[a-z0-9_]{1,200}", entity) is None
            for entity in cfg.home_assistant_entities
        ):
            raise ValueError("JARVIS_HOME_ASSISTANT_ENTITIES must contain only remote.* IDs")
        if cfg.execution_backend not in {"host", "docker"}:
            raise ValueError(
                "JARVIS_EXECUTION_BACKEND must be 'host' or 'docker'"
            )
        if cfg.execution_backend == "docker":
            from .execution import docker_available

            if not docker_available():
                raise ValueError(
                    "JARVIS_EXECUTION_BACKEND=docker requires the Docker CLI and a running daemon"
                )
        if cfg.self_inspect not in {"disabled", "read-only"}:
            raise ValueError(
                "JARVIS_SELF_INSPECT must be 'disabled' or 'read-only'"
            )
        if cfg.self_repair not in {"disabled", "propose"}:
            raise ValueError("JARVIS_SELF_REPAIR must be 'disabled' or 'propose'")
        if cfg.self_repair == "propose" and cfg.self_inspect != "read-only":
            raise ValueError("JARVIS_SELF_REPAIR=propose requires JARVIS_SELF_INSPECT=read-only")
        if cfg.initiative not in {"disabled", "observe", "act"}:
            raise ValueError("JARVIS_INITIATIVE must be 'disabled', 'observe', or 'act'")
        if cfg.initiative_quiet_hours and re.fullmatch(
            r"(?:[01][0-9]|2[0-3]):[0-5][0-9]-(?:[01][0-9]|2[0-3]):[0-5][0-9]",
            cfg.initiative_quiet_hours,
        ) is None:
            raise ValueError("JARVIS_INITIATIVE_QUIET_HOURS must be empty or HH:MM-HH:MM")
        if cfg.external_access not in {"disabled", "trusted-external"}:
            raise ValueError(
                "JARVIS_EXTERNAL_ACCESS must be 'disabled' or 'trusted-external'"
            )
        if cfg.google_drive_access not in {"app_files", "full"}:
            raise ValueError(
                "JARVIS_GOOGLE_DRIVE_ACCESS must be 'app_files' or 'full'"
            )
        if cfg.strategy_transfer not in {"disabled", "observe", "trial", "advise"}:
            raise ValueError(
                "JARVIS_STRATEGY_TRANSFER must be 'disabled', 'observe', 'trial', or 'advise'"
            )
        if cfg.memory_embeddings not in {"disabled", "openai"}:
            raise ValueError(
                "JARVIS_MEMORY_EMBEDDINGS must be 'disabled' or 'openai'"
            )
        if cfg.memory_claim_clock not in {"disabled", "shadow", "enforce"}:
            raise ValueError(
                "JARVIS_MEMORY_CLAIM_CLOCK must be 'disabled', 'shadow', or 'enforce'"
            )
        if (
            not cfg.memory_embedding_model
            or len(cfg.memory_embedding_model) > 200
        ):
            raise ValueError("JARVIS_MEMORY_EMBEDDING_MODEL is invalid")
        if cfg.presence_host.casefold() not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "JARVIS_PRESENCE_HOST must be loopback; use Tailscale Serve for remote access"
            )
        if cfg.presence_remote_access not in {"disabled", "paired"}:
            raise ValueError(
                "JARVIS_PRESENCE_REMOTE_ACCESS must be 'disabled' or 'paired'"
            )
        if cfg.screen_companion_mode not in {
            "disabled", "observe", "suggest", "collaborate"
        }:
            raise ValueError(
                "JARVIS_SCREEN_COMPANION must be disabled, observe, suggest, or collaborate"
            )
        if cfg.gateway_channel not in {"", "telegram", "signal"}:
            raise ValueError(
                "JARVIS_GATEWAY_CHANNEL must be empty, 'telegram', or 'signal'"
            )
        if cfg.gateway_channel:
            if cfg.gateway_token is None:
                raise ValueError("An enabled gateway requires JARVIS_GATEWAY_TOKEN")
            if (
                len(cfg.gateway_token) > 4096
                or any(ord(char) < 32 for char in cfg.gateway_token)
            ):
                raise ValueError("JARVIS_GATEWAY_TOKEN is invalid")
            if not cfg.gateway_allowed_ids:
                raise ValueError(
                    "An enabled gateway requires JARVIS_GATEWAY_ALLOWED_IDS"
                )
        if len(cfg.gateway_allowed_ids) > 16 or any(
            re.fullmatch(r"[A-Za-z0-9_+:.@-]{1,128}", item) is None
            for item in cfg.gateway_allowed_ids
        ):
            raise ValueError("JARVIS_GATEWAY_ALLOWED_IDS is invalid")
        if cfg.computer_access == "trusted-desktop":
            if cfg.execution_mode != "trusted-host":
                raise ValueError("trusted-desktop access requires JARVIS_EXECUTION_MODE=trusted-host")
            computer_root = (cfg.computer_root or Path.home()).resolve()
            if not computer_root.is_absolute() or computer_root == Path(computer_root.anchor).resolve():
                raise ValueError("JARVIS_COMPUTER_ROOT must be a non-root absolute directory")
        if not cfg.background_model:
            raise ValueError("JARVIS_BACKGROUND_MODEL must not be empty")
        if cfg.learning_model is not None and len(cfg.learning_model) > 200:
            raise ValueError("JARVIS_LEARNING_MODEL is too long")
        workspace_anchor = Path(cfg.workspace.anchor).resolve()
        data_anchor = Path(cfg.data_dir.anchor).resolve()
        if cfg.workspace == workspace_anchor:
            raise ValueError("JARVIS_WORKSPACE must not be a filesystem root")
        if cfg.data_dir == data_anchor:
            raise ValueError("JARVIS_DATA must not be a filesystem root")
        constitution_lexical = Path(cfg.constitution_path)
        if not constitution_lexical.is_absolute():
            raise ValueError("JARVIS_CONSTITUTION must be an absolute file path")
        constitution_real = constitution_lexical.resolve()
        constitution_anchor = Path(constitution_real.anchor).resolve()
        if constitution_real == constitution_anchor:
            raise ValueError("JARVIS_CONSTITUTION must point to a file, not a filesystem root")
        if _is_within(cfg.data_dir, cfg.workspace) or _is_within(cfg.soul_path, cfg.workspace):
            raise ValueError("JARVIS_DATA and JARVIS_SOUL must stay outside JARVIS_WORKSPACE")
        if cfg.vault_dir is not None:
            if _is_within(cfg.vault_dir, cfg.data_dir):
                raise ValueError("JARVIS_VAULT must stay outside JARVIS_DATA")
            if cfg.computer_access == "disabled" and not _is_within(
                cfg.vault_dir, cfg.workspace
            ):
                raise ValueError(
                    "JARVIS_VAULT must stay inside JARVIS_WORKSPACE when computer access is disabled"
                )
            if not os.path.lexists(cfg.vault_dir):
                parent = cfg.vault_dir.parent
                try:
                    _require_ordinary_directory(parent, "JARVIS_VAULT parent")
                except PermissionError as exc:
                    raise ValueError(
                        "JARVIS_VAULT directory does not exist and its parent is unavailable"
                    ) from exc
                if not os.access(parent, os.W_OK):
                    raise ValueError(
                        "JARVIS_VAULT directory does not exist and its parent is not writable"
                    )
                try:
                    cfg.vault_dir.mkdir()
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ValueError(
                        "JARVIS_VAULT directory does not exist and could not be created"
                    ) from exc
            _require_ordinary_directory(cfg.vault_dir, "JARVIS_VAULT")
        if (
            _is_within(constitution_real, cfg.workspace)
            or _is_within(constitution_real, cfg.data_dir)
        ):
            raise ValueError(
                "JARVIS_CONSTITUTION must stay outside JARVIS_WORKSPACE and JARVIS_DATA"
            )
        cfg.workspace.mkdir(parents=True, exist_ok=True)
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        if cfg.soul_path == (ROOT / "SOUL.md").resolve():
            _seed_default_soul(cfg.soul_path)
        load_soul(cfg.soul_path)
        load_constitution(Path(cfg.constitution_path))
        return cfg
