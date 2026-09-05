"""Driving Jarvis: a fresh store per instance, a fresh conversation per question.

The shapes here (:class:`Turn`, :class:`Session`, :class:`Case`,
:class:`Instance`, :class:`Outcome`) are the common currency every runner
speaks, so LongMemEval, LoCoMo and the RULER-style stress share one driver and
one scorer.

Two rules are load-bearing and are enforced here rather than left to each
runner:

* **Ingestion is transcript, not commands.**  A haystack session becomes one
  conversation and each of its turns becomes one ``Memory.add_message`` row.  No
  ``Remember this project fact:`` command is ever synthesised -- that would
  measure the governed verb instead of the memory stack, and the
  knowledge-update category is precisely a test of the extractor and the write
  gate.
* **The question runs in a brand-new conversation.**  Same-conversation scoring
  is inflated (measured 10/12 against 3/6 during the M1 work) and is banned.

The provider discipline is the one ``claude-m1-live-battery-v3.py`` established:
``JARVIS_BATTERY_MODEL`` defaults to ``claude-cli:claude-sonnet-4-5``,
``JARVIS_CLAUDE_CLI_ENABLED`` is set, and ``CLAUDECODE`` is removed from the
environment because the CLI refuses to launch nested.

``jarvis`` is imported lazily and only by :class:`RealJarvisBackend`, so every
other layer of this package -- and every test in it -- stays free of the
product import.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

BATTERY_MODEL_ENV = "JARVIS_BATTERY_MODEL"
DEFAULT_BATTERY_MODEL = "claude-cli:claude-sonnet-4-5"
FAKE_MODEL = "fake:deterministic-echo"
DEFAULT_ALLOWED_MODEL_PREFIXES = ("claude-cli:claude-sonnet",)
# A turn that recorded no provider call names this, so "no attestation" reads
# as a model outside every allowed prefix rather than as compliance.
UNRECORDED_MODEL = "unrecorded"
CONTEXT_EXCEEDED = "context_exceeded"
# No tokenizer ships here; the direct arm's fit test is stated as the
# approximation it is, in the same units the RULER-style grid uses.
CHARS_PER_TOKEN = 4
DEFAULT_CONTEXT_LENGTH = 32768

ABSTENTION_REPLY = "I have no recorded fact for that; nothing is stored about it."


class DriverError(RuntimeError):
    """A closed-reason refusal from the driver."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Turn:
    """One persisted transcript row.  ``role`` is ``user`` or ``assistant``."""

    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant"}:
            raise DriverError(
                f"a persisted transcript role must be user or assistant, not {self.role!r}",
                code="bad_role",
            )


@dataclass(frozen=True)
class Session:
    """One dated conversation's worth of turns."""

    session_id: str
    date: str
    turns: tuple[Turn, ...]


@dataclass(frozen=True)
class Case:
    """One question with its gold answer.

    ``kind`` is the benchmark's own category label (``question_type`` for
    LongMemEval, the numeric category for LoCoMo, the task name for the
    RULER-style stress).  ``gold_abstention`` marks the cases whose correct
    behaviour is to decline.
    """

    case_id: str
    question: str
    gold: str
    kind: str
    gold_abstention: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Instance:
    """A haystack plus the questions asked against it."""

    instance_id: str
    sessions: tuple[Session, ...]
    cases: tuple[Case, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def source_chars(self) -> int:
        return sum(len(turn.content) for session in self.sessions for turn in session.turns)


@dataclass(frozen=True)
class Outcome:
    """What one asked question produced.  Never carries the gold answer.

    ``model`` is the **observed** model, read from ``model_call_metrics`` --
    never the configured hint, which would let a config attest to itself.
    ``model_reported`` carries ``AgentResult.model`` when the two disagree, so
    a disagreement is published rather than resolved silently.  A turn that
    recorded no provider call at all reports :data:`UNRECORDED_MODEL`, which
    falls outside every allowed prefix and therefore refuses to publish.
    """

    case_id: str
    reply: str
    model: str
    status: str
    tool_calls: int
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error_code: str | None = None
    model_reported: str | None = None
    # The direct control arm only: what fraction of the intended prompt the
    # provider was actually shown.  1.0 when delivered whole, < 1.0 when the
    # cell is ``context_exceeded`` and therefore scored as not delivered.
    delivered_fraction: float | None = None
    prompt_chars: int | None = None


def battery_model(env: Mapping[str, str] | None = None) -> str:
    """The operator's live model unless the environment names another."""

    environment = os.environ if env is None else env
    return environment.get(BATTERY_MODEL_ENV) or DEFAULT_BATTERY_MODEL


def prepare_provider_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """Apply the live-battery provider discipline to a process environment."""

    environment = os.environ if env is None else env
    environment["JARVIS_CLAUDE_CLI_ENABLED"] = "true"
    # The Claude Code CLI refuses to launch nested inside another Claude Code
    # session, so a benchmark started from one must clear the marker.
    environment.pop("CLAUDECODE", None)
    return environment


class CaseRunner(Protocol):
    """What every runner needs from a way of driving Jarvis."""

    model_hint: str

    def ingest(self, instance: Instance) -> None: ...

    def ask(self, case: Case) -> Outcome: ...

    def ask_direct(self, case: Case, context: str) -> Outcome: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# The fake provider.  Smoke only: its model id is outside every allowed prefix,
# so report.write_report refuses to publish anything it produced.
# ---------------------------------------------------------------------------


class FakeCaseRunner:
    """A deterministic stand-in that exercises the whole pipeline offline.

    It answers from the ingested transcript, so a case whose evidence was
    ingested scores correct and a case whose evidence was not scores incorrect
    or abstains -- which is enough shape for the scorer, the aggregator and the
    report writer to be smoked honestly.  One case in ``wrong_every`` is
    answered wrongly on purpose so a smoke run never reports a degenerate 1.00.
    """

    model_hint = FAKE_MODEL

    def __init__(
        self, *, wrong_every: int = 4, seed: int = 0, context_length: int | None = None
    ) -> None:
        self._ingested: list[str] = []
        self._wrong_every = max(0, int(wrong_every))
        self._seed = int(seed)
        self.context_length = int(context_length) if context_length else None
        self.asked: list[str] = []

    @property
    def direct_prompt_limit_chars(self) -> int:
        return (self.context_length or DEFAULT_CONTEXT_LENGTH) * CHARS_PER_TOKEN

    def ingest(self, instance: Instance) -> None:
        self._ingested = [turn.content for session in instance.sessions for turn in session.turns]

    def close(self) -> None:
        self._ingested = []

    def _deliberately_wrong(self, case_id: str) -> bool:
        if self._wrong_every <= 0:
            return False
        digest = hashlib.sha256(f"{self._seed}\0{case_id}".encode("utf-8")).digest()
        return digest[0] % self._wrong_every == 0

    def _answer(self, case: Case, haystack: Sequence[str]) -> str:
        if self._deliberately_wrong(case.case_id):
            return "The answer is definitely not what the record says."
        # A multi-value or chain task's gold is the whole list, so answering
        # with only the first element would make the smoke pessimistic on three
        # of the six RULER-style shapes for no reason.
        wanted = [str(item) for item in (case.metadata.get("values") or ()) if str(item).strip()]
        if not wanted and case.gold.strip():
            wanted = [case.gold.strip()]
        if wanted and all(
            any(item.casefold() in text.casefold() for text in haystack) for item in wanted
        ):
            return ", ".join(wanted)
        return ABSTENTION_REPLY

    def ask(self, case: Case) -> Outcome:
        self.asked.append(case.case_id)
        started = time.perf_counter()
        reply = self._answer(case, self._ingested)
        return Outcome(
            case_id=case.case_id,
            reply=reply,
            model=FAKE_MODEL,
            status="complete",
            tool_calls=0,
            latency_ms=int((time.perf_counter() - started) * 1000),
            prompt_tokens=sum(len(text) for text in self._ingested) // 4,
            completion_tokens=max(1, len(reply) // 4),
        )

    def ask_direct(self, case: Case, context: str) -> Outcome:
        started = time.perf_counter()
        limit = self.direct_prompt_limit_chars
        if len(context) > limit:
            return Outcome(
                case_id=case.case_id,
                reply="",
                model=FAKE_MODEL,
                status=CONTEXT_EXCEEDED,
                tool_calls=0,
                latency_ms=0,
                error_code=CONTEXT_EXCEEDED,
                delivered_fraction=round(limit / len(context), 4),
                prompt_chars=len(context),
            )
        reply = self._answer(case, [context])
        return Outcome(
            case_id=case.case_id,
            reply=reply,
            model=FAKE_MODEL,
            status="complete",
            tool_calls=0,
            latency_ms=int((time.perf_counter() - started) * 1000),
            prompt_tokens=len(context) // CHARS_PER_TOKEN,
            completion_tokens=max(1, len(reply) // CHARS_PER_TOKEN),
            delivered_fraction=1.0,
            prompt_chars=len(context),
        )


# ---------------------------------------------------------------------------
# The real backend.
# ---------------------------------------------------------------------------


class JarvisBackend(Protocol):
    """The seam between the driver's orchestration and the product import."""

    def open_store(self, store_dir: Path, model: str) -> tuple[Any, Any]: ...

    def close_store(self, memory: Any, agent: Any) -> None: ...


class DirectProvider(Protocol):
    """A tool-free, store-free single model call.  The control arm's transport."""

    def complete(self, prompt: str, model: str) -> tuple[str, str | None, int | None, int | None]:
        """Return ``(text, served_model, prompt_tokens, completion_tokens)``."""


class ModelClientDirectProvider:
    """The real control-arm transport: one ``ModelClient.chat`` with no tools.

    H-4: the control arm must not run through ``Agent.run``.  It passed
    ``Agent._compact_messages``, whose ``_clip`` keeps head 2/3 and tail 1/3 and
    deletes the middle, so at the top of the default grid the needle at depth
    0.5 was removed before the provider ever saw it -- manufacturing the
    Lost-in-the-Middle curve the benchmark exists to observe, and inflating
    ``jarvis - direct``.  A control that the harness edits is not a control.
    """

    def __init__(self, *, context_length: int = DEFAULT_CONTEXT_LENGTH) -> None:
        self.context_length = int(context_length)

    def complete(  # pragma: no cover - live provider
        self, prompt: str, model: str
    ) -> tuple[str, str | None, int | None, int | None]:
        from dataclasses import replace as _replace

        from jarvis.config import Config
        from jarvis.model_client import build_model_client

        prepare_provider_environment()
        config = _replace(
            Config.load(), claude_cli_enabled=True, context_length=self.context_length
        )
        client = build_model_client(config)
        try:
            response = client.chat(
                [{"role": "user", "content": prompt}],
                [],
                model,
                context_length=self.context_length,
                temperature=0.0,
            )
        finally:
            closer = getattr(client, "close", None)
            if callable(closer):
                closer()
        metrics = getattr(response, "metrics", None)
        return (
            str(response.get("content", "")),
            getattr(response, "model", None),
            getattr(metrics, "prompt_tokens", None),
            getattr(metrics, "completion_tokens", None),
        )


class RealJarvisBackend:
    """Build a real ``Memory`` and ``Agent`` on a throwaway store."""

    def __init__(self, *, context_length: int | None = None, embeddings: str = "disabled") -> None:
        self.context_length = context_length
        self.embeddings = embeddings

    def open_store(self, store_dir: Path, model: str) -> tuple[Any, Any]:  # pragma: no cover - live
        from dataclasses import replace as _replace

        from jarvis.agent import Agent
        from jarvis.config import Config
        from jarvis.memory import Memory

        workspace = store_dir / "workspace"
        data = store_dir / "data"
        workspace.mkdir(parents=True, exist_ok=True)
        data.mkdir(parents=True, exist_ok=True)
        environment = prepare_provider_environment()
        environment["JARVIS_WORKSPACE"] = str(workspace)
        environment["JARVIS_DATA"] = str(data)
        overrides: dict[str, Any] = {
            "autonomy": "autonomous",
            "workspace": workspace,
            "data_dir": data,
            "model": "auto",
            "fast_model": model,
            "reasoning_model": model,
            "coding_model": model,
            "ollama_preload": False,
            "vault_dir": None,
            "memory_embeddings": self.embeddings,
        }
        if self.context_length is not None:
            overrides["context_length"] = int(self.context_length)
        config = _replace(Config.load(), **overrides)
        memory = Memory(data / "jarvis.db")
        agent = Agent(config, memory, lambda _event: None, coding_review=False, coding_planning=False)
        return memory, agent

    def close_store(self, memory: Any, agent: Any) -> None:  # pragma: no cover - live
        closer = getattr(agent, "close", None)
        if callable(closer):
            closer()
        memory.close()


class JarvisCaseRunner:
    """Drive the real agent: fresh store per instance, fresh conversation per case."""

    def __init__(
        self,
        *,
        model: str | None = None,
        backend: JarvisBackend | None = None,
        store_root: Path | None = None,
        compaction_enabled: bool = False,
        context_length: int | None = None,
        embeddings: str = "disabled",
        direct_context_length: int | None = None,
        direct_provider: DirectProvider | None = None,
    ) -> None:
        self.model_hint = model or battery_model()
        # H-5: the published config's runtime block is hashed, so it has to be
        # the configuration that actually ran. These two values used to be
        # published and dropped.
        self.context_length = int(context_length) if context_length else None
        self.embeddings = str(embeddings)
        self._backend = backend or RealJarvisBackend(
            context_length=self.context_length, embeddings=self.embeddings
        )
        # The control arm is sized separately and on purpose. Sharing the
        # agent's context length would make the top of the default grid
        # undeliverable (a 32K-token haystack does not fit a 32K window), while
        # raising the agent's window to fit it would change what the *jarvis*
        # arm measures. Both values are published.
        self.direct_context_length = int(
            direct_context_length or self.context_length or DEFAULT_CONTEXT_LENGTH
        )
        self._direct_provider = direct_provider or ModelClientDirectProvider(
            context_length=self.direct_context_length
        )
        self._owned_root = store_root is None
        self._store_root = Path(store_root) if store_root else Path(
            tempfile.mkdtemp(prefix="jarvis-bench-")
        )
        self._compaction_enabled = bool(compaction_enabled)
        self._memory: Any | None = None
        self._agent: Any | None = None
        self._store_dir: Path | None = None
        self._instances = 0
        self.compaction_ran = 0

    @property
    def direct_prompt_limit_chars(self) -> int:
        """The largest prompt the control arm will send, in characters."""

        return self.direct_context_length * CHARS_PER_TOKEN

    # -- store lifecycle ---------------------------------------------------

    def _drop_store(self) -> None:
        if self._memory is not None:
            try:
                self._backend.close_store(self._memory, self._agent)
            finally:
                self._memory = None
                self._agent = None
        if self._store_dir is not None:
            shutil.rmtree(self._store_dir, ignore_errors=True)
            self._store_dir = None

    def close(self) -> None:
        self._drop_store()
        if self._owned_root:
            shutil.rmtree(self._store_root, ignore_errors=True)

    # -- ingestion ---------------------------------------------------------

    def ingest(self, instance: Instance) -> None:
        self._drop_store()
        self._instances += 1
        store_dir = self._store_root / f"instance-{self._instances:05d}"
        store_dir.mkdir(parents=True, exist_ok=True)
        self._store_dir = store_dir
        memory, agent = self._backend.open_store(store_dir, self.model_hint)
        self._assert_runtime_applied(agent)
        self._memory = memory
        self._agent = agent
        for session in instance.sessions:
            conversation_id = memory.new_conversation(f"session {session.session_id}")
            for turn in session.turns:
                memory.add_message(conversation_id, turn.role, turn.content)
            if self._compaction_enabled:
                self._compact(conversation_id)

    def _assert_runtime_applied(self, agent: Any) -> None:
        """Read the published runtime settings back off the live agent.

        The config hash is what makes a published number re-derivable, so a
        runtime key that is hashed and published but silently dropped makes the
        hash decorative.  Same shape as the ``compaction_unavailable`` refusal:
        say so loudly rather than measure one configuration and publish another.
        """

        config = getattr(agent, "config", None)
        if config is None:
            return
        if self.context_length is not None:
            applied = getattr(config, "context_length", None)
            if applied is not None and int(applied) != self.context_length:
                raise DriverError(
                    "the published config asks for context_length "
                    f"{self.context_length} but the agent is running with {applied}; "
                    "the config hash would describe a run that did not happen",
                    code="runtime_config_not_applied",
                )
        applied_embeddings = getattr(config, "memory_embeddings", None)
        if applied_embeddings is not None and str(applied_embeddings) != self.embeddings:
            raise DriverError(
                "the published config asks for memory_embeddings "
                f"{self.embeddings!r} but the agent is running with "
                f"{str(applied_embeddings)!r}",
                code="runtime_config_not_applied",
            )

    def _compact(self, conversation_id: int) -> None:
        """Compact one ingested session when the tree actually has compaction.

        Half A of M5 is not built at this commit.  The runner therefore probes
        for the method instead of importing a module that does not exist, and
        refuses loudly rather than silently reporting a compacted configuration
        it did not run.
        """

        compact = getattr(self._memory, "compact_conversation", None)
        if not callable(compact):
            raise DriverError(
                "the configuration asks for compaction but this tree has no "
                "Memory.compact_conversation; run with compaction disabled or "
                "against a tree where M5 half A has landed",
                code="compaction_unavailable",
            )
        compact(conversation_id)
        self.compaction_ran += 1

    # -- asking ------------------------------------------------------------

    def _metrics_watermark(self) -> int:
        assert self._memory is not None
        row = self._memory.db.execute(
            "SELECT COALESCE(MAX(id), 0) FROM model_call_metrics"
        ).fetchone()
        return int(row[0]) if row else 0

    def _metrics_since(self, watermark: int) -> tuple[int | None, int | None, tuple[str, ...]]:
        """Tokens **and** the models the runtime actually called.

        M-5: ``AgentResult.model`` is absent on the ``incomplete`` path, and
        falling back to the configured hint let ``check_models`` pass on the
        strength of the config agreeing with itself.  The authoritative answer
        was always one column away in the same table.
        """

        assert self._memory is not None
        row = self._memory.db.execute(
            "SELECT SUM(prompt_tokens), SUM(completion_tokens) "
            "FROM model_call_metrics WHERE id > ?",
            (watermark,),
        ).fetchone()
        prompt = int(row[0]) if row and row[0] is not None else None
        completion = int(row[1]) if row and row[1] is not None else None
        observed = self._memory.db.execute(
            "SELECT DISTINCT model FROM model_call_metrics WHERE id > ? ORDER BY model",
            (watermark,),
        ).fetchall()
        models = tuple(str(entry[0]) for entry in observed if entry and entry[0])
        return prompt, completion, models

    @staticmethod
    def _observed_model(models: Sequence[str]) -> str:
        if not models:
            return UNRECORDED_MODEL
        if len(models) == 1:
            return models[0][:64]
        return "+".join(sorted(models))[:64]

    def _run_prompt(self, case: Case, prompt: str) -> Outcome:
        if self._agent is None:
            raise DriverError("ask() before ingest(): no store is open", code="no_store")
        watermark = self._metrics_watermark()
        started = time.perf_counter()
        error_code: str | None = None
        reported: str | None = None
        try:
            # No conversation_id: the agent opens a brand-new conversation, so
            # only durable memory can answer.
            result = self._agent.run(prompt)
            reply = str(result)
            status = str(getattr(result, "status", "complete"))
            reported = getattr(result, "model", None)
            tool_calls = int(getattr(result, "tool_calls", 0) or 0)
        except Exception as exc:  # noqa: BLE001 - one bad case must not kill a run
            reply = ""
            status = "error"
            tool_calls = 0
            error_code = type(exc).__name__
        latency_ms = int((time.perf_counter() - started) * 1000)
        prompt_tokens, completion_tokens, models = self._metrics_since(watermark)
        model = self._observed_model(models)
        reported_text = str(reported) if reported else None
        return Outcome(
            case_id=case.case_id,
            reply=reply,
            model=model,
            status=status,
            tool_calls=tool_calls,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error_code=error_code,
            model_reported=reported_text if reported_text != model else None,
        )

    def ask(self, case: Case) -> Outcome:
        return self._run_prompt(case, case.question)

    @staticmethod
    def direct_prompt(case: Case, context: str) -> str:
        return (
            "Answer the question using only the reference text below.\n\n"
            f"{context}\n\nQuestion: {case.question}"
        )

    def ask_direct(self, case: Case, context: str) -> Outcome:
        """The ``arm=direct`` control: one tool-free provider call, unedited.

        No agent, no store, no compaction, no prompt clipper -- "measures the
        provider" has to mean the provider saw what we said it saw.  When the
        prompt does not fit the configured context length the cell is reported
        ``context_exceeded`` and scored as **not delivered** (``det`` is
        ``None``, never ``False``): a control we could not run is missing
        evidence, not evidence of failure.
        """

        prompt = self.direct_prompt(case, context)
        limit = self.direct_prompt_limit_chars
        if len(prompt) > limit:
            return Outcome(
                case_id=case.case_id,
                reply="",
                model=self.model_hint,
                status=CONTEXT_EXCEEDED,
                tool_calls=0,
                latency_ms=0,
                error_code=CONTEXT_EXCEEDED,
                delivered_fraction=round(limit / len(prompt), 4),
                prompt_chars=len(prompt),
            )
        started = time.perf_counter()
        try:
            text, served, prompt_tokens, completion_tokens = self._direct_provider.complete(
                prompt, self.model_hint
            )
            status = "complete"
            error_code = None
        except Exception as exc:  # noqa: BLE001 - one bad cell must not kill a run
            text, served, prompt_tokens, completion_tokens = "", None, None, None
            status = "error"
            error_code = type(exc).__name__
        return Outcome(
            case_id=case.case_id,
            reply=text,
            model=str(served) if served else UNRECORDED_MODEL,
            status=status,
            tool_calls=0,
            latency_ms=int((time.perf_counter() - started) * 1000),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error_code=error_code,
            delivered_fraction=1.0,
            prompt_chars=len(prompt),
        )


def make_runner(
    provider: str,
    *,
    model: str | None = None,
    compaction_enabled: bool = False,
    backend: JarvisBackend | None = None,
    context_length: int | None = None,
    embeddings: str = "disabled",
    direct_context_length: int | None = None,
) -> CaseRunner:
    """``jarvis`` for a real run, ``fake`` for a smoke."""

    if provider == "fake":
        return FakeCaseRunner(context_length=direct_context_length or context_length)
    if provider == "jarvis":
        return JarvisCaseRunner(
            model=model,
            compaction_enabled=compaction_enabled,
            backend=backend,
            context_length=context_length,
            embeddings=embeddings,
            direct_context_length=direct_context_length,
        )
    raise DriverError(f"unknown provider {provider!r}; use 'jarvis' or 'fake'", code="unknown_provider")


def reconfigure_stdout() -> None:
    """The console here is cp1252 and cannot print every reply."""

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")
