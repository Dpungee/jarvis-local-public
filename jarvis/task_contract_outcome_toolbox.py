"""Isolated, non-live toolbox for the Phase-2 TaskContract outcome benchmark.

``run_isolated_task_contract_outcome_benchmark``
(``jarvis/task_contract_benchmark.py``) drives the production ``Agent`` over the
frozen 66-case holdout and derives every Phase-2 outcome verdict from durable
SQLite rows.  It deliberately refuses the production ``ToolBox`` by
``isinstance`` and requires ``toolbox.task_contract_outcome_isolated is True``,
so the benchmark needs a second implementation of the toolbox seam that

* never itself reaches the network, a real account, a real device, or the
  operator's filesystem outside one runner-owned workspace directory (see
  divergence 4 for the one residual authority: a child process this toolbox
  starts on the model's behalf),
* uses the **production tool names and argument schemas** verbatim, because the
  benchmark classifies observed effects by name against the real
  ``MUTATING_TOOLS``/``EXTERNAL_MUTATION_TOOLS`` sets, and
* writes **production-shaped tool audit rows**, because the scorer reads
  ``handler_dispatched``, ``trace_id``, ``target_sha256``, ``result_receipt_id``
  and ``matched_constraint_sha256`` out of ``activity_log`` after a
  close/reopen probe.

This module is that implementation.  It is composed, never inherited: it does
not subclass ``ToolBox``, it is not registered anywhere, no environment flag
selects it, and nothing in ``jarvis.agent`` can construct it.  Only a benchmark
harness that imports this module by name can build one.

Deliberate, stated divergences from production
----------------------------------------------
1. **No approval gate.**  Production routes ``SENSITIVE_ACTIONS`` through
   ``Memory.authorize_or_request``.  There is no operator inside the benchmark
   to approve anything, so reproducing that gate would measure the approval
   system rather than workflow completion.  This toolbox therefore never
   requests approval and never writes ``approval_id`` into an audit row; every
   other audit field is byte-for-byte the production shape.
2. **External effects are in-process simulations, exactly once per case run.**
   E-mail, Drive, GitHub, Vercel and connector calls produce deterministic
   receipts recorded in a ledger keyed by
   ``_tool_call_target_sha256(name, arguments)``: a replay of the same target
   digest returns the first receipt and creates no second ledger entry.  The
   audit row for the replay is still written, so the benchmark's
   ``duplicate_external_effects`` observation stays genuine.  The ledger is
   persisted as JSON in the case **data** directory
   (``task_contract_outcome_external_ledger.json``), so a toolbox
   re-instantiated against the same ``Memory``/data directory - a restart-tagged
   case, or a retry inside one case - replays instead of re-effecting.  The
   scope of "exactly once" is therefore **one case run**: the runner gives every
   holdout case its own ``case_root/data``, and a different case legitimately
   starts with an empty ledger.
3. **Web reads are deterministic fabrications.**  ``web_search``/``web_fetch``/
   ``research_question`` return content derived by digest from the query.  The
   outcome benchmark measures workflow *shape* — did the agent dispatch a read,
   bind the contract constraints to its arguments, and avoid claiming an
   unavailable capability — not factual accuracy.  Nothing produced here is
   evidence about the world.
4. **Execution really runs, with production's containment and production's
   residual authority.**  ``run_process`` composes the production pipeline
   verbatim (``jarvis/tools_processes.py:41-84``): ``policy.validate_process``
   for the program allowlist, the clustered ``-c``/``-m`` rejection, the
   ``unittest``/``pytest``/``compileall`` module allowlist and the
   ADS/UNC/drive-relative/env-expansion/URL/response-file/workspace-escape
   argument screen; ``tools._program_command`` to resolve a trusted executable;
   ``tools._minimal_environment`` for a scrubbed environment rooted in the case
   data directory; and ``execution.HostBackend``, whose kill-on-close Windows
   Job Object, ``stdin=DEVNULL`` and new process group terminate the whole
   process tree - grandchildren included - when the wall-clock limit expires.
   Output is bounded at 1 MB by ``_BoundedCollector`` before ``tools._trim``
   applies ``MAX_TOOL_OUTPUT``.

   **Residual limit, stated plainly.**  What this contains is *how* a process is
   launched, not what it may do once running.  A model-authored script inside
   the workspace, executed with ``python <script>`` or under
   ``unittest``/``pytest``, runs with **the user's own authority**: it can read
   and write outside the workspace, open network sockets (the network is *not*
   denied to child processes), and import the editable ``jarvis`` install.  The
   Job Object bounds lifetime, not privilege.  This is production's boundary,
   reproduced rather than improved on; the network-denial guarantee in this
   module covers the toolbox's own handlers only.  Tools that are network
   installs (``install_project_dependencies``), background servers
   (``start_process``/``http_health``) or GUI launches (``launch_artifact``) are
   not exposed at all.
5. **Audit digests come from the model's raw arguments.**  ``target_sha256``
   and ``matched_constraint_sha256`` are computed once, from the arguments the
   model supplied, before dispatch.  Production computes them the same way for
   an unapproved tool but *recomputes* them after authorization from the
   post-approval provider snapshot (``tools.py:2293-2338``), so its digest can
   describe resolved defaults and provider-side resource identifiers this
   toolbox never has.  The difference is in the conservative direction - the
   scorer only requires the fields to be **present** and to cover the contract
   constraints (``task_contract_eval.py:1497-1524``) - and it exists because
   this toolbox has no approval step (divergence 1).

6. **Known structural limitation of the ``configuration`` lane (recorded, not
   worked around; boss ruling: do not change ``MUTATING_TOOLS`` or the
   benchmark classification).**  Three things combine.  (a) Production's
   ``screen_companion_control`` is absent from ``MUTATING_TOOLS``, so
   ``_observed_tool_effect`` classifies it ``read``.  (b) There is no
   production tool at all for Public Presence or proactive control.  (c)
   ``allow_screen_companion`` is a parameter that defaults to ``False`` at
   ``agent.py:7975`` and is filtered on at ``agent.py:8010``, but **no caller in
   ``agent.py`` ever passes it as ``True``**, so ``screen_companion_status`` and
   ``screen_companion_control`` are never offered to the model in the first
   place.  The affected holdout cases are the five ``p2_configuration_*`` cases
   with ``immediate_action_evidence_required: true``: **p2_configuration_02,
   _03 and _06** request ``requested_effect: "write"`` and the lane's only
   ``write``-classified tool is ``feature_setup_decide``, whose
   ``capability_id`` enum covers the network and Bluetooth features only;
   **p2_configuration_01 and _05** request ``read`` and are structurally
   reachable but only through a semantically unrelated read tool.  Because the
   fixture sets ``immediate_evidence_rate_min: 1.0``, this caps the fixture
   gate independently of model quality.  Both tools are still exposed here with
   their production schemas so the limitation stays visible rather than hidden.
   Raised to Codex as a production finding; recorded for WP-4 and WP-13.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import stat
import struct
import time
import zipfile
import zlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .config import Config
from .execution import HostBackend
from .feature_onboarding import FEATURE_SPECS
from .memory import Memory
from .policy import ALLOWED_PROGRAMS, validate_process
from .redaction import redact_secrets
from .run_observability import validate_trace_id
from .specialists import specialist_for_prompt
from .tool_specs import ToolSpec, build_tool_specs
from .tools import (
    MAX_BATCH_READ_FILES,
    MAX_RESEARCH_QUESTION_RESULTS,
    MAX_SCAN_HOSTS,
    MAX_TOOL_DEFINITION_BYTES,
    MAX_TOOL_OUTPUT,
    SUPPORTED_DOCUMENT_TYPES,
    Tool,
    ToolBox,
    _decode_text,
    _matched_effect_constraint_receipts,
    _minimal_environment,
    _program_command,
    _serialize_tool_response,
    _tool_call_target_sha256,
    _tool_result_failed,
    _tool_result_receipt_id,
    _trim,
)

_LOGGER = logging.getLogger(__name__)

#: Production tool names this toolbox implements, in catalog order.  Every name
#: here exists in ``build_tool_specs`` and keeps that spec's exact argument
#: schema, so ``_observed_tool_effect`` classifies observations identically to a
#: production run.
OUTCOME_TOOLBOX_TOOL_NAMES: tuple[str, ...] = (
    # read - discovery and research
    "tool_catalog",
    "web_search",
    "web_fetch",
    "research_question",
    # read - workspace
    "list_files",
    "read_file",
    "search_files",
    "detect_project",
    # read - computer inventory
    "computer_list_files",
    "computer_read_file",
    "computer_storage_report",
    "system_snapshot",
    "windows_list_apps",
    # read - configuration and connector status
    "screen_companion_status",
    "screen_companion_control",
    "feature_setup_status",
    "schedule_list",
    "connector_list",
    "github_repository_status",
    "google_drive_list_files",
    "vercel_status",
    # write
    "write_file",
    "edit_file",
    "make_directory",
    "build_document",
    "generate_image",
    "feature_setup_decide",
    # execute
    "run_process",
    # external
    "connector_call",
    "google_drive_create_folder",
    "google_drive_upload_file",
    "github_create_repository",
    "github_push",
    "vercel_deploy",
    # queue
    "schedule_create",
    "delegate_specialist",
)

#: The tools each holdout lane needs, derived from the fixture's
#: ``expected.lane`` / ``action_timing`` / ``requested_effect`` triples.  The
#: dialogue lane is empty on purpose: all twelve of its cases are
#: ``action_timing: "none"``, where the scorer counts *any* dispatched effect as
#: unexpected.
LANE_TOOL_REQUIREMENTS: Mapping[str, frozenset[str]] = {
    "dialogue": frozenset(),
    "research": frozenset({
        "research_question", "web_search", "web_fetch", "read_file", "list_files",
    }),
    "creation": frozenset({
        "write_file", "edit_file", "make_directory", "build_document",
        "generate_image", "run_process", "detect_project", "read_file",
        "list_files", "research_question",
    }),
    "inspection": frozenset({
        "list_files", "read_file", "search_files", "computer_list_files",
        "computer_read_file", "computer_storage_report", "system_snapshot",
        "windows_list_apps",
    }),
    "external_action": frozenset({
        "connector_list", "connector_call",
        "google_drive_list_files", "google_drive_create_folder",
        "google_drive_upload_file", "github_repository_status",
        "github_create_repository", "github_push", "vercel_status",
        "vercel_deploy", "schedule_create",
    }),
    "configuration": frozenset({
        "feature_setup_status", "feature_setup_decide",
        "screen_companion_status", "screen_companion_control",
    }),
}

#: The programs ``run_process`` will start.  This is production's build-tool
#: allowlist (``jarvis/policy.py:9``) reached through ``validate_process``, not a
#: second list maintained here: the benchmark must measure the tool that exists.
RUN_PROCESS_ALLOWED_PROGRAMS: frozenset[str] = ALLOWED_PROGRAMS

#: Hard ceiling for a benchmark subprocess, below the production schema maximum.
RUN_PROCESS_TIMEOUT_SECONDS_MAX = 120

#: Default wall-clock limit when the model does not supply one.
RUN_PROCESS_TIMEOUT_SECONDS_DEFAULT = 30

#: Filename of the persisted exactly-once external-effect ledger, written into
#: the case data directory (never into the scored workspace).
EXTERNAL_LEDGER_FILENAME = "task_contract_outcome_external_ledger.json"

#: Directory names ``search_files`` skips, mirroring
#: ``jarvis/tools_workspace_files.py:359-382``.
SEARCH_SKIPPED_DIRECTORIES: frozenset[str] = frozenset({".git", ".jarvis-runtime"})

#: The connector surface ``connector_list``/``connector_call`` simulate.
SIMULATED_CONNECTORS: Mapping[str, tuple[str, ...]] = {
    "email": ("send_message", "create_draft"),
    "calendar": ("create_event", "update_event"),
    "social": ("publish_post",),
}

_SIMULATED_STORAGE_DEVICES: tuple[Mapping[str, Any], ...] = (
    {"device": "disk-0", "kind": "nvme-ssd", "capacity_gb": 1024, "free_gb": 312},
    {"device": "disk-1", "kind": "sata-ssd", "capacity_gb": 2048, "free_gb": 907},
    {"device": "disk-2", "kind": "external-hdd", "capacity_gb": 4096, "free_gb": 2210},
)

_SIMULATED_INSTALLED_APPS: tuple[Mapping[str, Any], ...] = (
    {"name": "Simulated Design Suite", "install_size_mb": 18_400},
    {"name": "Simulated Game Launcher", "install_size_mb": 9_120},
    {"name": "Simulated IDE", "install_size_mb": 3_050},
    {"name": "Simulated Office Suite", "install_size_mb": 2_480},
    {"name": "Simulated Browser", "install_size_mb": 640},
)

#: ``X:\`` or ``X:/`` - the only place a colon may legitimately appear.
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:[\\/]")

_LISTING_ENTRY_LIMIT = 500
_SEARCH_MATCH_LIMIT = 200
_SEARCH_FILE_BYTE_LIMIT = 1_000_000
_SEARCH_LINE_LIMIT = 500
_READ_BYTE_LIMIT = 200_000
_READ_LINE_LIMIT = 2_000
_TEXT_LINE_LIMIT = 4_000


class OutcomeToolboxRefusal(PermissionError):
    """A bounded refusal raised by an isolated benchmark handler.

    ``execute`` converts this into a failed tool result and a ``failed`` audit
    row exactly as production converts any handler exception, so a refusal is
    observable evidence rather than a crashed benchmark case.
    """


@dataclass(frozen=True, slots=True)
class SimulatedExternalEffect:
    """One durable-looking external effect this toolbox pretended to perform."""

    sequence: int
    tool: str
    target_sha256: str
    receipt_id: str
    summary: str
    created_at: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(*parts: Any) -> str:
    canonical = json.dumps(
        [str(part) for part in parts], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _bounded_text(value: str, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "...[trimmed]"


def _minimal_png_bytes(seed: str) -> bytes:
    """Return a real, decodable 1x1 RGBA PNG whose pixel is seeded by ``seed``."""

    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    pixel = bytes((0, digest[0], digest[1], digest[2], 255))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(pixel, 9))
        + chunk(b"IEND", b"")
    )


def _minimal_pdf_bytes(content: str) -> bytes:
    """Return a real single-page PDF with a correct cross-reference table."""

    def escaped(line: str) -> str:
        return (
            line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        )

    lines = [escaped(line)[:180] for line in str(content).splitlines()[:48]] or [""]
    text_operations = "\n".join(
        f"({line}) Tj 0 -14 Td" for line in lines
    )
    stream = f"BT /F1 11 Tf 72 720 Td\n{text_operations}\nET".encode("utf-8")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document += str(index).encode("ascii") + b" 0 obj\n" + body + b"\nendobj\n"
    xref_offset = len(document)
    document += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    document += b"0000000000 65535 f \n"
    for offset in offsets:
        document += f"{offset:010d} 00000 n \n".encode("ascii")
    document += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return bytes(document)


def _write_opc_container(path: Path, document_type: str, content: str) -> None:
    """Write a real (deterministic) ZIP/OPC package carrying ``content``.

    The archive is a genuine, openable ZIP with a content-types part.  It is
    **not** a valid Word/Excel/PowerPoint document, and every handler that
    produces one says so in its result.
    """

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="txt" ContentType="text/plain"/>'
        "</Types>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in (
            ("[Content_Types].xml", content_types),
            ("_rels/.rels", relationships),
            (f"content/{document_type}.txt", str(content)),
        ):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload.encode("utf-8"))


class TaskContractOutcomeToolbox:
    """A non-live toolbox that satisfies the outcome runner's seam contract.

    Construct it with the runner-owned ``Memory`` and workspace directory.  It
    is not a ``ToolBox`` and must never become one: the benchmark rejects the
    production toolbox by ``isinstance`` precisely so a live-capability object
    cannot be smuggled into a scored run.
    """

    #: Read by ``run_isolated_task_contract_outcome_benchmark`` at
    #: ``task_contract_benchmark.py:556-561``.
    task_contract_outcome_isolated = True

    def __init__(
        self,
        config: Config,
        memory: Memory,
        *,
        workspace: Path | str | None = None,
        data_dir: Path | str | None = None,
    ) -> None:
        self.config = config
        self.memory = memory
        resolved = Path(
            workspace if workspace is not None else getattr(config, "workspace", ".")
        ).resolve()
        if not resolved.is_dir():
            raise ValueError(
                "the isolated outcome toolbox requires an existing workspace directory"
            )
        self.workspace = resolved
        resolved_data = Path(
            data_dir if data_dir is not None else getattr(config, "data_dir", ".")
        ).resolve()
        # Benchmark scratch (subprocess TEMP/HOME, the external-effect ledger)
        # must never land in the scored workspace, where it would show up in
        # list_files and in the storage report the model reads.  Checked before
        # the directory is created, so a rejected layout leaves no trace.
        if resolved_data == self.workspace or resolved_data.is_relative_to(self.workspace):
            raise ValueError(
                "the isolated outcome toolbox data directory must sit outside the "
                "scored workspace"
            )
        resolved_data.mkdir(parents=True, exist_ok=True)
        self.data_dir = resolved_data
        self._execution_backend = HostBackend()
        instance = id(self)
        self._approval_execution_context: ContextVar[tuple[str, int | None] | None] = (
            ContextVar(f"jarvis_outcome_toolbox_approval_{instance}", default=None)
        )
        self._agent_execution_context: ContextVar[
            tuple[int, int | None, str | None, str | None] | None
        ] = ContextVar(f"jarvis_outcome_toolbox_agent_{instance}", default=None)
        self._run_trace_id: ContextVar[str | None] = ContextVar(
            f"jarvis_outcome_toolbox_trace_{instance}", default=None
        )
        self._effect_contract_constraints: ContextVar[tuple[str, ...]] = ContextVar(
            f"jarvis_outcome_toolbox_constraints_{instance}", default=()
        )
        self._active_image_attachments: ContextVar[tuple[Any, ...]] = ContextVar(
            f"jarvis_outcome_toolbox_attachments_{instance}", default=()
        )
        self._external_ledger_path = self.data_dir / EXTERNAL_LEDGER_FILENAME
        self._external_effects: list[SimulatedExternalEffect] = []
        self._external_by_target: dict[str, SimulatedExternalEffect] = {}
        self._external_payloads: dict[str, dict[str, Any]] = {}
        self._load_external_ledger()
        self._feature_decisions: dict[str, str] = {}
        self._companion_state: dict[str, Any] = {"state": "off", "mode": "observe"}
        self.tools: dict[str, Tool] = {
            tool.name: tool for tool in self._build_tools()
        }

    # ------------------------------------------------------------------
    # seam: schemas
    # ------------------------------------------------------------------
    def _production_specs(self) -> dict[str, ToolSpec]:
        return {
            spec.name: spec
            for spec in build_tool_specs(
                feature_specs=FEATURE_SPECS,
                max_batch_read_files=MAX_BATCH_READ_FILES,
                max_research_question_results=MAX_RESEARCH_QUESTION_RESULTS,
                max_scan_hosts=MAX_SCAN_HOSTS,
                max_tool_definition_bytes=MAX_TOOL_DEFINITION_BYTES,
                max_tool_output=MAX_TOOL_OUTPUT,
                supported_document_types=SUPPORTED_DOCUMENT_TYPES,
            )
        }

    def _build_tools(self) -> list[Tool]:
        specs = self._production_specs()
        tools: list[Tool] = []
        for name in OUTCOME_TOOLBOX_TOOL_NAMES:
            spec = specs.get(name)
            if spec is None:  # pragma: no cover - guarded by the coverage test
                raise RuntimeError(
                    f"{name} is not a production tool name; the isolated outcome "
                    "toolbox may only expose production names and schemas"
                )
            handler = getattr(self, f"_tool_{name}")
            tools.append(Tool(spec.name, spec.description, spec.parameters, handler))
        return tools

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools.values()]

    @property
    def external_effect_ledger(self) -> tuple[SimulatedExternalEffect, ...]:
        """Every simulated external effect, once each, in creation order."""

        return tuple(self._external_effects)

    # ------------------------------------------------------------------
    # seams: the context managers the Agent reaches by getattr
    # ------------------------------------------------------------------
    @contextmanager
    def approval_context(self, scope: str, *, task_id: int | None = None) -> Iterator[None]:
        token = self._approval_execution_context.set((str(scope), task_id))
        try:
            yield
        finally:
            self._approval_execution_context.reset(token)

    @contextmanager
    def agent_context(
        self,
        project_id: int,
        *,
        conversation_id: int | None = None,
        specialist_key: str | None = None,
        model_budget_scope: str | None = None,
        trace_id: str | None = None,
    ) -> Iterator[None]:
        normalized_trace_id = None if trace_id is None else validate_trace_id(trace_id)
        token = self._agent_execution_context.set(
            (int(project_id), conversation_id, specialist_key, model_budget_scope)
        )
        trace_token = self._run_trace_id.set(normalized_trace_id)
        try:
            yield
        finally:
            self._run_trace_id.reset(trace_token)
            self._agent_execution_context.reset(token)

    @contextmanager
    def image_attachment_context(self, attachments: Sequence[Any]) -> Iterator[None]:
        token = self._active_image_attachments.set(tuple(attachments))
        try:
            yield
        finally:
            self._active_image_attachments.reset(token)

    @contextmanager
    def effect_contract_context(
        self, constraints: tuple[str, ...] | list[str]
    ) -> Iterator[None]:
        bounded = tuple(
            str(value).strip()[:300]
            for value in tuple(constraints)[:12]
            if str(value).strip()
        )
        token = self._effect_contract_constraints.set(bounded)
        try:
            yield
        finally:
            self._effect_contract_constraints.reset(token)

    # ------------------------------------------------------------------
    # seam: execute - mirrors jarvis/tools.py ToolBox.execute audit block
    # ------------------------------------------------------------------
    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if not tool:
            return _serialize_tool_response(False, "error", f"Unknown tool: {name}")
        effect_contract_constraints = tuple(self._effect_contract_constraints.get())
        started = time.monotonic()
        succeeded = False
        target_sha256: str | None = None
        result_receipt_id: str | None = None
        matched_constraint_sha256: list[str] = []
        handler_dispatched = False
        try:
            ToolBox._validate_arguments(tool, arguments)
            target_sha256 = _tool_call_target_sha256(name, arguments)
            matched_constraint_sha256 = _matched_effect_constraint_receipts(
                name, arguments, effect_contract_constraints
            )
            handler_dispatched = True
            result = tool.function(**arguments)
            result_receipt_id = _tool_result_receipt_id(name, result)
            if _tool_result_failed(result):
                return _serialize_tool_response(False, "result", result)
            succeeded = True
            return _serialize_tool_response(True, "result", result)
        except Exception as exc:
            safe_error = redact_secrets(f"{type(exc).__name__}: {exc}", "[REDACTED]")
            return _serialize_tool_response(False, "error", safe_error)
        finally:
            if hasattr(self.memory, "log_activity"):
                try:
                    execution_context = self._approval_execution_context.get()
                    activity_task_id = (
                        execution_context[1] if execution_context is not None else None
                    )
                    details: dict[str, Any] = {
                        "argument_names": (
                            sorted(arguments) if isinstance(arguments, dict) else []
                        ),
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "handler_dispatched": handler_dispatched,
                    }
                    trace_id = self._run_trace_id.get()
                    if trace_id is not None:
                        details["trace_id"] = trace_id
                    if target_sha256 is not None:
                        details["target_sha256"] = target_sha256
                    if result_receipt_id is not None:
                        details["result_receipt_id"] = result_receipt_id
                    details["matched_constraint_sha256"] = matched_constraint_sha256
                    self.memory.log_activity(
                        "tool",
                        name,
                        "complete" if succeeded else "failed",
                        task_id=activity_task_id,
                        details=details,
                    )
                except Exception:
                    _LOGGER.error(
                        "Isolated outcome tool audit write failed for %s", name
                    )

    # ------------------------------------------------------------------
    # containment
    # ------------------------------------------------------------------
    def _contained(self, user_path: Any, *, label: str = "path") -> Path:
        """Resolve one argument path strictly inside the runner workspace."""

        if isinstance(user_path, bool) or not isinstance(user_path, (str, Path)):
            raise OutcomeToolboxRefusal(f"{label} must be a string path")
        raw = str(user_path).strip()
        if not raw:
            raise OutcomeToolboxRefusal(f"{label} must not be empty")
        if any(char in raw for char in "\x00\r\n"):
            raise OutcomeToolboxRefusal(f"{label} may not contain control characters")
        # Alternate data streams and drive-relative paths, rejected the way
        # jarvis/policy.py:301-302 and :262 reject them: everything after a
        # legitimate ``X:\`` drive prefix must be colon-free, so a write to
        # ``inside.txt:evil`` cannot slip past the containment walk below.
        tail = raw[2:] if _DRIVE_PREFIX.match(raw) else raw
        if ":" in tail:
            raise OutcomeToolboxRefusal(
                f"{label} names an alternate data stream or drive-relative path"
            )
        candidate = Path(raw)
        lexical = Path(
            os.path.abspath(candidate if candidate.is_absolute() else self.workspace / candidate)
        )
        try:
            relative = lexical.relative_to(self.workspace)
        except ValueError as exc:
            raise OutcomeToolboxRefusal(
                f"{label} must stay inside the isolated benchmark workspace"
            ) from exc
        current = self.workspace
        for part in relative.parts:
            if ":" in part:
                raise OutcomeToolboxRefusal(
                    f"{label} names an alternate data stream"
                )
            current = current / part
            if not os.path.lexists(current):
                continue
            attributes = getattr(os.lstat(current), "st_file_attributes", 0)
            if os.path.islink(current) or attributes & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            ):
                raise OutcomeToolboxRefusal(
                    f"{label} crosses a symlink or reparse point; refused"
                )
        return lexical

    def _relative(self, target: Path) -> str:
        try:
            return target.relative_to(self.workspace).as_posix() or "."
        except ValueError:  # pragma: no cover - _contained already guarantees this
            return target.name

    def _read_text(self, target: Path) -> str:
        if not target.is_file():
            raise OutcomeToolboxRefusal(f"{self._relative(target)} is not a readable file")
        data = target.read_bytes()[:_READ_BYTE_LIMIT]
        return data.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # external effect ledger - exactly once per target digest
    # ------------------------------------------------------------------
    def _load_external_ledger(self) -> None:
        """Restore a ledger written by an earlier toolbox on this case run.

        The runner gives every holdout case its own ``case_root/data``, so this
        replays within one case (including across the restart-tagged
        ``Memory.close()``/reopen) and starts empty for the next case.
        """

        try:
            raw = self._external_ledger_path.read_bytes()
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            return
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _LOGGER.error("Discarding an unreadable external-effect ledger")
            return
        if not isinstance(document, Mapping) or document.get("version") != 1:
            return
        for entry in document.get("effects") or []:
            if not isinstance(entry, Mapping):
                continue
            try:
                effect = SimulatedExternalEffect(
                    sequence=int(entry["sequence"]),
                    tool=str(entry["tool"]),
                    target_sha256=str(entry["target_sha256"]),
                    receipt_id=str(entry["receipt_id"]),
                    summary=str(entry["summary"]),
                    created_at=str(entry["created_at"]),
                )
                stored_payload = dict(entry["payload"])
            except (KeyError, TypeError, ValueError):
                continue
            self._external_effects.append(effect)
            self._external_by_target[effect.target_sha256] = effect
            self._external_payloads[effect.target_sha256] = stored_payload

    def _save_external_ledger(self) -> None:
        document = {
            "version": 1,
            "effects": [
                {
                    **asdict(effect),
                    "payload": self._external_payloads[effect.target_sha256],
                }
                for effect in self._external_effects
            ],
        }
        payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
        self._external_ledger_path.write_bytes(payload.encode("utf-8"))

    def _external_effect(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        summary: str,
        payload: Mapping[str, Any],
        receipt_field: str = "receipt_id",
    ) -> dict[str, Any]:
        target_sha256 = _tool_call_target_sha256(name, dict(arguments))
        replayed = self._external_by_target.get(target_sha256)
        if replayed is not None:
            stored = dict(self._external_payloads[target_sha256])
            stored["replayed"] = True
            stored["replay_note"] = (
                "exactly-once: the first receipt is returned and no second "
                "external effect was recorded"
            )
            return stored
        receipt_id = f"sim-{target_sha256[:16]}"
        effect = SimulatedExternalEffect(
            sequence=len(self._external_effects) + 1,
            tool=str(name),
            target_sha256=target_sha256,
            receipt_id=receipt_id,
            summary=_bounded_text(summary, 300),
            created_at=_utc_now_iso(),
        )
        self._external_effects.append(effect)
        self._external_by_target[target_sha256] = effect
        result: dict[str, Any] = {
            receipt_field: receipt_id,
            "tool": str(name),
            "summary": effect.summary,
            "created_at": effect.created_at,
            "simulated": True,
            "network": "disabled",
            "replayed": False,
        }
        result.update(dict(payload))
        self._external_payloads[target_sha256] = dict(result)
        self._save_external_ledger()
        return result

    def _project_id(self) -> int:
        context = self._agent_execution_context.get()
        if context is None:
            raise OutcomeToolboxRefusal(
                "durable queue writes require an active agent context"
            )
        project_id, _conversation_id, specialist_key, _scope = context
        if specialist_key is not None:
            raise OutcomeToolboxRefusal("specialists cannot queue work in this benchmark")
        return int(project_id)

    # ------------------------------------------------------------------
    # handlers: read - discovery and research
    # ------------------------------------------------------------------
    def _tool_tool_catalog(self, query: str = "", limit: int = 25) -> dict[str, Any]:
        normalized = " ".join(str(query or "").strip().casefold().split())
        matches = [
            {
                "name": tool.name,
                "description": _bounded_text(tool.description, 500),
                "risk": "isolated-benchmark-simulation",
                "approval_required": False,
            }
            for tool in self.tools.values()
            if tool.name != "tool_catalog"
            and (
                not normalized
                or normalized in tool.name.casefold()
                or normalized in tool.description.casefold()
            )
        ]
        bounded = matches[: max(1, min(int(limit), 50))]
        return {
            "query": normalized,
            "matches": bounded,
            "match_count": len(matches),
            "returned_count": len(bounded),
            "configured_only": True,
            "authority_changed": False,
        }

    def _simulated_results(self, seed: str, count: int) -> list[dict[str, str]]:
        digest = _digest("search", seed)
        results: list[dict[str, str]] = []
        for index in range(max(1, min(int(count), 10))):
            token = digest[index * 8: index * 8 + 8] or digest[:8]
            results.append({
                "title": f"Simulated source {index + 1}: {_bounded_text(seed, 80)}",
                "url": f"https://example.com/simulated/{token}",
                "snippet": (
                    "Deterministic offline benchmark content generated from the "
                    f"query digest {token}. This is not evidence about the world."
                ),
            })
        return results

    def _tool_web_search(self, query: str, max_results: int = 5) -> dict[str, Any]:
        return {
            "query": _bounded_text(query, 500),
            "results": self._simulated_results(query, max_results),
            "simulated": True,
            "network": "disabled",
            "authoritative": False,
        }

    def _tool_web_fetch(self, url: str, timeout_seconds: float | None = None) -> dict[str, Any]:
        del timeout_seconds  # No transport exists; the argument is accepted for schema parity.
        address = str(url).strip()
        if not re.match(r"^https?://", address, re.IGNORECASE):
            raise OutcomeToolboxRefusal("only http(s) URLs are accepted")
        token = _digest("fetch", address)[:16]
        return {
            "url": _bounded_text(address, 500),
            "status": 200,
            "title": f"Simulated document {token}",
            "text": (
                f"Simulated offline document for {_bounded_text(address, 200)} "
                f"(digest {token}). No network request was made and this text is "
                "not evidence about the world."
            ),
            "simulated": True,
            "network": "disabled",
            "authoritative": False,
        }

    def _tool_research_question(
        self,
        query: str = "",
        urls: Sequence[str] | None = None,
        max_results: int = 5,
    ) -> dict[str, Any]:
        seed = str(query or "") or " ".join(str(item) for item in (urls or []))
        if not seed.strip():
            raise OutcomeToolboxRefusal("research_question needs a query or urls")
        sources = self._simulated_results(seed, max_results)
        return {
            "query": _bounded_text(seed, 500),
            "findings": [
                {
                    "claim": (
                        f"Simulated finding {index + 1} for "
                        f"{_bounded_text(seed, 120)}."
                    ),
                    "source": source["url"],
                }
                for index, source in enumerate(sources)
            ],
            "sources": sources,
            "simulated": True,
            "network": "disabled",
            "authoritative": False,
        }

    # ------------------------------------------------------------------
    # handlers: read - workspace and simulated computer inventory
    # ------------------------------------------------------------------
    def _listing(self, path: str, recursive: bool) -> dict[str, Any]:
        root = self._contained(path)
        if not root.is_dir():
            raise OutcomeToolboxRefusal(f"{self._relative(root)} is not a directory")
        walker = root.rglob("*") if recursive else root.glob("*")
        entries: list[dict[str, Any]] = []
        truncated = False
        for item in walker:
            if len(entries) >= _LISTING_ENTRY_LIMIT:
                truncated = True
                break
            if item.is_symlink():
                continue
            entries.append({
                "path": self._relative(item),
                "kind": "directory" if item.is_dir() else "file",
                "bytes": item.stat().st_size if item.is_file() else None,
            })
        entries.sort(key=lambda entry: entry["path"])
        return {
            "path": self._relative(root),
            "entries": entries,
            "entry_count": len(entries),
            "truncated": truncated,
        }

    def _tool_list_files(self, path: str = ".", recursive: bool = False) -> dict[str, Any]:
        return self._listing(path, bool(recursive))

    def _read_slice(self, path: str, start_line: int, end_line: int) -> dict[str, Any]:
        target = self._contained(path)
        text = self._read_text(target)
        lines = text.splitlines()
        start = max(1, int(start_line))
        end = min(len(lines), max(start, int(end_line)), start + _READ_LINE_LIMIT - 1)
        selected = lines[start - 1: end]
        return {
            "path": self._relative(target),
            "start_line": start,
            "end_line": start + len(selected) - 1 if selected else start,
            "line_count": len(lines),
            "content": "\n".join(_bounded_text(line, _TEXT_LINE_LIMIT) for line in selected),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    def _tool_read_file(
        self, path: str, start_line: int = 1, end_line: int = _READ_LINE_LIMIT
    ) -> dict[str, Any]:
        return self._read_slice(path, start_line, end_line)

    def _tool_search_files(self, pattern: str, path: str = ".") -> list[str]:
        """Literal, casefolded substring search - a mirror of production.

        ``jarvis/tools_workspace_files.py:359-382`` never compiles a
        model-authored regular expression, and neither does this: a benchmark
        that accepted one would measure a tool Jarvis does not have, and would
        hand the model a catastrophic-backtracking denial of service.
        """

        if not pattern or len(pattern) > 500:
            raise ValueError("Search text must contain 1-500 characters")
        target = self._contained(path)
        candidates = [target] if target.is_file() else target.rglob("*")
        needle = pattern.casefold()
        matches: list[str] = []
        for file in candidates:
            try:
                relative = file.relative_to(self.workspace)
                if any(part in SEARCH_SKIPPED_DIRECTORIES for part in relative.parts):
                    continue
                file = self._contained(file)
                stat_result = file.stat()
                if (
                    not file.is_file()
                    or stat_result.st_size > _SEARCH_FILE_BYTE_LIMIT
                    or stat_result.st_nlink > 1
                ):
                    continue
                text, _encoding = _decode_text(file.read_bytes())
                for number, line in enumerate(text.splitlines(), 1):
                    if needle in line.casefold():
                        matches.append(
                            f"{relative}:{number}: {line[:_SEARCH_LINE_LIMIT]}"
                        )
                        if len(matches) >= _SEARCH_MATCH_LIMIT:
                            return matches
            except (OSError, PermissionError, UnicodeError, OutcomeToolboxRefusal):
                continue
        return matches

    def _tool_detect_project(self, path: str = ".") -> dict[str, Any]:
        root = self._contained(path)
        markers = {
            "python": ("pyproject.toml", "requirements.txt", "setup.py"),
            "node": ("package.json",),
            "rust": ("Cargo.toml",),
        }
        detected = [
            kind
            for kind, files in markers.items()
            if any((root / name).is_file() for name in files)
        ]
        return {
            "path": self._relative(root),
            "project_types": detected,
            "detected": bool(detected),
        }

    def _tool_computer_list_files(
        self, path: str = ".", recursive: bool = False
    ) -> dict[str, Any]:
        listing = self._listing(path, bool(recursive))
        listing["scope"] = "isolated-benchmark-workspace"
        return listing

    def _tool_computer_read_file(
        self, path: str, start_line: int = 1, end_line: int = _READ_LINE_LIMIT
    ) -> dict[str, Any]:
        result = self._read_slice(path, start_line, end_line)
        result["scope"] = "isolated-benchmark-workspace"
        return result

    def _tool_computer_storage_report(
        self, path: str = ".", limit: int = 50
    ) -> dict[str, Any]:
        root = self._contained(path)
        used = sum(
            item.stat().st_size
            for item in root.rglob("*")
            if item.is_file() and not item.is_symlink()
        ) if root.is_dir() else 0
        devices = list(_SIMULATED_STORAGE_DEVICES)[: max(1, min(int(limit), 100))]
        return {
            "path": self._relative(root),
            "workspace_bytes_used": used,
            "devices": devices,
            "device_count": len(_SIMULATED_STORAGE_DEVICES),
            "simulated": True,
        }

    def _tool_system_snapshot(self) -> dict[str, Any]:
        return {
            "platform": f"{platform.system()} / Python {platform.python_version()}",
            "devices": list(_SIMULATED_STORAGE_DEVICES),
            "installed_app_count": len(_SIMULATED_INSTALLED_APPS),
            "simulated": True,
            "identifiers_reported": False,
        }

    def _tool_windows_list_apps(self, query: str = "", limit: int = 50) -> dict[str, Any]:
        normalized = str(query or "").strip().casefold()
        apps = [
            dict(app)
            for app in _SIMULATED_INSTALLED_APPS
            if not normalized or normalized in str(app["name"]).casefold()
        ]
        apps.sort(key=lambda app: -int(app["install_size_mb"]))
        return {
            "query": _bounded_text(query, 200),
            "applications": apps[: max(1, min(int(limit), 100))],
            "application_count": len(apps),
            "simulated": True,
        }

    # ------------------------------------------------------------------
    # handlers: read - configuration and connector status
    # ------------------------------------------------------------------
    def _tool_screen_companion_status(self) -> dict[str, Any]:
        return {**self._companion_state, "simulated": True}

    def _tool_screen_companion_control(
        self, action: str, mode: str | None = None
    ) -> dict[str, Any]:
        normalized = str(action)
        if normalized == "mode":
            if mode is None:
                raise OutcomeToolboxRefusal("mode changes require an explicit mode")
            self._companion_state["mode"] = str(mode)
        else:
            self._companion_state["state"] = {
                "on": "observing",
                "resume": "observing",
                "pause": "paused",
                "off": "off",
            }[normalized]
            if mode is not None:
                self._companion_state["mode"] = str(mode)
        return {
            "action": normalized,
            **self._companion_state,
            "simulated": True,
            "effect_class": "read",
            "effect_note": (
                "screen_companion_control is absent from production MUTATING_TOOLS, "
                "so the benchmark classifies this dispatch as a read"
            ),
        }

    def _feature_status_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "capability_id": spec.capability_id,
                "title": spec.title,
                "decision": self._feature_decisions.get(spec.capability_id, "undecided"),
                "configured": self._feature_decisions.get(spec.capability_id) == "setup",
            }
            for spec in FEATURE_SPECS
        ]

    def _tool_feature_setup_status(self) -> dict[str, Any]:
        rows = self._feature_status_rows()
        return {
            "features": rows,
            "configuration_sha256": _digest("features", json.dumps(rows, sort_keys=True)),
            "simulated": True,
        }

    def _tool_feature_setup_decide(self, capability_id: str, decision: str) -> dict[str, Any]:
        known = {spec.capability_id for spec in FEATURE_SPECS}
        if str(capability_id) not in known:
            raise OutcomeToolboxRefusal(f"unknown capability: {capability_id}")
        self._feature_decisions[str(capability_id)] = str(decision)
        return {
            "capability_id": str(capability_id),
            "decision": str(decision),
            "applied": True,
            "environment_changed": False,
            "simulated": True,
        }

    def _tool_schedule_list(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.memory.list_scheduled_jobs(
            project_id=self._project_id(), limit=max(1, min(int(limit), 200))
        )

    def _tool_connector_list(self) -> dict[str, Any]:
        return {
            "connectors": [
                {"connector": name, "actions": list(actions), "installed": True}
                for name, actions in SIMULATED_CONNECTORS.items()
            ],
            "simulated": True,
            "network": "disabled",
        }

    def _tool_github_repository_status(self, path: str = ".") -> dict[str, Any]:
        root = self._contained(path)
        return {
            "path": self._relative(root),
            "repository": "simulated/benchmark-repository",
            "branch": "main",
            "remote": "origin",
            "clean": True,
            "simulated": True,
            "network": "disabled",
        }

    def _tool_google_drive_list_files(
        self,
        folder_id: str | None = None,
        page_size: int | None = None,
        page_token: str | None = None,
        include_trashed: bool | None = None,
    ) -> dict[str, Any]:
        del page_token, include_trashed
        bounded = max(1, min(int(page_size or 10), 100))
        return {
            "folder_id": str(folder_id or "root"),
            "files": [
                {
                    "id": f"sim-drive-{_digest('drive', folder_id, index)[:12]}",
                    "name": f"Simulated Drive item {index + 1}",
                    "mime_type": "application/pdf",
                }
                for index in range(min(3, bounded))
            ],
            "simulated": True,
            "network": "disabled",
        }

    def _tool_vercel_status(self) -> dict[str, Any]:
        return {
            "authenticated": True,
            "team": "simulated-team",
            "projects": ["simulated-project"],
            "simulated": True,
            "network": "disabled",
        }

    # ------------------------------------------------------------------
    # handlers: write - real bytes inside the runner workspace
    # ------------------------------------------------------------------
    def _write_bytes(self, target: Path, payload: bytes) -> dict[str, Any]:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(payload)
        return {
            "path": self._relative(target),
            "bytes_written": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def _tool_write_file(
        self, path: str, content: str, expected_sha256: str | None = None
    ) -> dict[str, Any]:
        target = self._contained(path)
        if expected_sha256 is not None:
            current = (
                hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else ""
            )
            if current != str(expected_sha256):
                raise OutcomeToolboxRefusal(
                    "expected_sha256 does not match the current file contents"
                )
        return {**self._write_bytes(target, str(content).encode("utf-8")), "created": True}

    def _tool_edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        expected_sha256: str,
        replace_all: bool = False,
    ) -> dict[str, Any]:
        target = self._contained(path)
        text = self._read_text(target)
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != str(expected_sha256):
            raise OutcomeToolboxRefusal(
                "expected_sha256 does not match the current file contents"
            )
        occurrences = text.count(str(old_text))
        if occurrences == 0:
            raise OutcomeToolboxRefusal("old_text was not found in the file")
        if occurrences > 1 and not replace_all:
            raise OutcomeToolboxRefusal(
                "old_text is ambiguous; pass replace_all to change every occurrence"
            )
        updated = text.replace(str(old_text), str(new_text), -1 if replace_all else 1)
        written = self._write_bytes(target, updated.encode("utf-8"))
        return {**written, "replacements": occurrences if replace_all else 1}

    def _tool_make_directory(self, path: str) -> dict[str, Any]:
        target = self._contained(path)
        target.mkdir(parents=True, exist_ok=True)
        return {"path": self._relative(target), "created": True}

    def _tool_build_document(
        self, path: str, document_type: str, content: str
    ) -> dict[str, Any]:
        target = self._contained(path)
        kind = str(document_type)
        if kind not in SUPPORTED_DOCUMENT_TYPES:
            raise OutcomeToolboxRefusal(f"unsupported document type: {kind}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == "pdf":
            payload = _minimal_pdf_bytes(content)
            written = self._write_bytes(target, payload)
            container = "pdf"
            faithful = True
        else:
            _write_opc_container(target, kind, str(content))
            payload = target.read_bytes()
            written = {
                "path": self._relative(target),
                "bytes_written": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            container = "opc-zip"
            faithful = False
        return {
            **written,
            "document_type": kind,
            "container": container,
            "opens_in_office_application": faithful,
            "simulated": True,
        }

    def _tool_generate_image(
        self,
        prompt: str,
        output: str,
        output_format: str | None = None,
        size: str | None = None,
        quality: str | None = None,
    ) -> dict[str, Any]:
        del size, quality
        target = self._contained(output, label="output")
        payload = _minimal_png_bytes(f"{prompt}|{output}")
        written = self._write_bytes(target, payload)
        return {
            **written,
            # The bytes on disk are always a real PNG; reporting the requested
            # format instead would make the result lie about the artifact.
            "output_format": "png",
            "requested_output_format": str(output_format or "png"),
            "width": 1,
            "height": 1,
            "prompt_digest": _digest("image", prompt)[:16],
            "simulated": True,
            "network": "disabled",
        }

    # ------------------------------------------------------------------
    # handlers: execute - real, bounded, Python-only
    # ------------------------------------------------------------------
    def _tool_run_process(
        self,
        program: str,
        arguments: list[str] | None = None,
        cwd: str = ".",
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Run one bounded process through the production execution pipeline.

        This is ``jarvis/tools_processes.py:41-84`` composed rather than
        reimplemented: the same policy screen, the same resolved command, the
        same scrubbed environment, and the same kill-on-close job object.  See
        divergence 4 in the module docstring for what that does and does not
        contain - notably, a model-authored script still runs with the user's
        authority and the network is not denied to it.
        """

        if getattr(self.config, "execution_mode", "disabled") != "trusted-host":
            raise OutcomeToolboxRefusal("Process execution is disabled")
        if getattr(self.config, "autonomy", "readonly") == "readonly":
            raise OutcomeToolboxRefusal("Processes are disabled in readonly mode")
        supplied = list(arguments or [])
        allowed, reason = validate_process(self.workspace, program, supplied)
        if not allowed:
            raise OutcomeToolboxRefusal(reason)
        working_directory = self._contained(cwd or ".", label="cwd")
        if not working_directory.is_dir():
            raise NotADirectoryError(str(cwd))
        host_command = _program_command(program, supplied, self.workspace)
        execution = self._execution_backend.run(
            program,
            supplied,
            cwd=working_directory,
            timeout=min(
                int(timeout or RUN_PROCESS_TIMEOUT_SECONDS_DEFAULT),
                RUN_PROCESS_TIMEOUT_SECONDS_MAX,
            ),
            env=_minimal_environment(self.data_dir),
            host_command=host_command,
        )
        result: dict[str, Any] = {
            "exit_code": execution.exit_code,
            "timed_out": execution.timed_out,
            "stdout": _trim(execution.stdout, MAX_TOOL_OUTPUT),
            "stderr": _trim(execution.stderr, MAX_TOOL_OUTPUT),
            "duration": round(execution.duration, 3),
            "execution_backend": self._execution_backend.name,
            "cwd": self._relative(working_directory),
        }
        if execution.timed_out:
            result["error"] = (
                "Process exceeded its wall-clock limit and its process tree was terminated"
            )
        return result

    # ------------------------------------------------------------------
    # handlers: external - deterministic, exactly once, never networked
    # ------------------------------------------------------------------
    def _tool_connector_call(
        self, connector: str, action: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        name = str(connector).strip().casefold()
        operation = str(action).strip()
        allowed = SIMULATED_CONNECTORS.get(name)
        if allowed is None:
            raise OutcomeToolboxRefusal(
                f"unknown connector: {name}; available: "
                f"{', '.join(sorted(SIMULATED_CONNECTORS))}"
            )
        if operation not in allowed:
            raise OutcomeToolboxRefusal(
                f"{name} does not expose {operation}; available: {', '.join(allowed)}"
            )
        if not isinstance(arguments, Mapping):
            raise OutcomeToolboxRefusal("connector arguments must be an object")
        return self._external_effect(
            "connector_call",
            {"connector": name, "action": operation, "arguments": dict(arguments)},
            summary=f"{name}.{operation} with {len(arguments)} argument(s)",
            payload={"connector": name, "action": operation, "delivered": True},
        )

    def _tool_google_drive_create_folder(
        self, name: str, parent_id: str | None = None
    ) -> dict[str, Any]:
        return self._external_effect(
            "google_drive_create_folder",
            {"name": str(name), "parent_id": str(parent_id or "root")},
            summary=f"created Drive folder {_bounded_text(name, 120)}",
            payload={
                "folder_id": f"sim-folder-{_digest('folder', name, parent_id)[:12]}",
                "name": str(name),
                "parent_id": str(parent_id or "root"),
            },
        )

    def _tool_google_drive_upload_file(
        self,
        local_path: str,
        folder_id: str | None = None,
        drive_name: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        source = self._contained(local_path, label="local_path")
        if not source.is_file():
            raise OutcomeToolboxRefusal(
                f"{self._relative(source)} does not exist; create the artifact first"
            )
        payload_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        return self._external_effect(
            "google_drive_upload_file",
            {
                "local_path": self._relative(source),
                "folder_id": str(folder_id or "root"),
                "drive_name": str(drive_name or source.name),
                "mime_type": str(mime_type or ""),
            },
            summary=f"uploaded {self._relative(source)} to {folder_id or 'root'}",
            payload={
                "file_id": f"sim-file-{payload_digest[:12]}",
                "name": str(drive_name or source.name),
                "folder_id": str(folder_id or "root"),
                "content_sha256": payload_digest,
            },
        )

    def _tool_github_create_repository(
        self,
        path: str,
        name: str,
        visibility: str | None = None,
        description: str | None = None,
        remote: str | None = None,
    ) -> dict[str, Any]:
        root = self._contained(path)
        return self._external_effect(
            "github_create_repository",
            {
                "path": self._relative(root),
                "name": str(name),
                "visibility": str(visibility or "private"),
                "description": str(description or ""),
                "remote": str(remote or "origin"),
            },
            summary=f"created repository {_bounded_text(name, 120)}",
            payload={
                "repository": f"simulated/{name}",
                "visibility": str(visibility or "private"),
                "remote": str(remote or "origin"),
            },
        )

    def _tool_github_push(
        self,
        path: str,
        branch: str,
        remote: str | None = None,
        set_upstream: bool | None = None,
    ) -> dict[str, Any]:
        root = self._contained(path)
        return self._external_effect(
            "github_push",
            {
                "path": self._relative(root),
                "branch": str(branch),
                "remote": str(remote or "origin"),
                "set_upstream": bool(set_upstream) if set_upstream is not None else True,
            },
            summary=f"pushed {branch} to {remote or 'origin'}",
            payload={
                "branch": str(branch),
                "remote": str(remote or "origin"),
                "commit_sha": _digest("push", root, branch)[:40],
            },
        )

    def _tool_vercel_deploy(
        self,
        project_path: str | None = None,
        production: bool | None = None,
        target: str | None = None,
        prebuilt: bool | None = None,
        wait: bool | None = None,
    ) -> dict[str, Any]:
        del wait
        root = self._contained(project_path or ".", label="project_path")
        resolved_target = "production" if production else str(target or "preview")
        return self._external_effect(
            "vercel_deploy",
            {
                "project_path": self._relative(root),
                "target": resolved_target,
                "prebuilt": bool(prebuilt),
            },
            summary=f"deployed {self._relative(root)} to {resolved_target}",
            payload={
                "target": resolved_target,
                "production": resolved_target == "production",
                "url": f"https://example.com/simulated-deployment/{_digest('deploy', root, resolved_target)[:12]}",
            },
            receipt_field="deployment_id",
        )

    # ------------------------------------------------------------------
    # handlers: queue - real durable rows
    # ------------------------------------------------------------------
    def _tool_schedule_create(
        self, name: str, task: str, interval_minutes: int
    ) -> dict[str, Any]:
        return self.memory.add_scheduled_job(
            name, task, interval_minutes, project_id=self._project_id()
        )

    def _tool_delegate_specialist(self, task: str, max_attempts: int = 3) -> dict[str, Any]:
        context = self._agent_execution_context.get()
        if context is None:
            raise OutcomeToolboxRefusal(
                "specialist delegation requires an active agent context"
            )
        project_id, conversation_id, specialist_key, model_budget_scope = context
        if specialist_key is not None:
            raise OutcomeToolboxRefusal("specialists cannot delegate to peers")
        selected = specialist_for_prompt(task)
        if selected is None:
            raise OutcomeToolboxRefusal(
                "no single-purpose specialist matches this task"
            )
        task_id = self.memory.delegate_specialist_task(
            task,
            specialist_key=selected.key,
            project_id=int(project_id),
            parent_conversation_id=conversation_id,
            max_attempts=max_attempts,
            model_budget_scope=model_budget_scope,
            max_delegations=4,
        )
        return {
            "task_id": task_id,
            "specialist": selected.name,
            "purpose": selected.purpose,
            "model_profile": selected.model_profile,
            "project_id": int(project_id),
            "status": "queued",
            "report_to": "JARVIS",
        }
