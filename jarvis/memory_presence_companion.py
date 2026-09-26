"""Current Memory domain extracted without changing method control flow.

Global dependencies resolve through jarvis.memory at call time. The facade keeps
public imports, patch seams, constants and authorization helpers authoritative.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
import sqlite3
from .memory_runtime import (_memory)


class PresenceCompanionMemoryMixin:
    """Mechanically extracted current Memory methods."""

    @staticmethod
    def _presence_job_id(value: Any) -> str:
        job_id = str(value).strip().casefold()
        if _memory().re.fullmatch(r"[0-9a-f]{32}", job_id) is None:
            raise ValueError("Presence job id must be 32 lowercase hexadecimal characters")
        return job_id

    def create_presence_job(
        self,
        job_id: str,
        *,
        conversation_id: int,
        project_id: int,
        prompt: str,
        model_override: str,
        attachments_json: str = "[]",
        run_origin: str = "interactive",
        replayable: bool = True,
    ) -> dict[str, Any]:
        """Durably accept one Presence turn before it enters the in-memory queue."""
        normalized_id = self._presence_job_id(job_id)
        if not self.conversation_exists(conversation_id):
            raise ValueError("Conversation does not exist")
        normalized_project = self._project_id(project_id)
        conversation_project = self.conversation_project(conversation_id)
        if (
            conversation_project is None
            or int(conversation_project["id"]) != normalized_project
            or not bool(conversation_project.get("enabled"))
        ):
            raise ValueError("Conversation project does not exist or is disabled")
        safe_prompt = _memory()._bounded_persisted_text(
            _memory().redact_secrets(str(prompt).strip()), 50_000, "presence prompt"
        )
        if not safe_prompt:
            raise ValueError("Presence prompt must not be empty")
        model = str(model_override).strip().casefold()
        if model not in {"auto", "fast", "reasoning", "coding", "deep"}:
            raise ValueError("Invalid Presence model profile")
        normalized_origin = str(run_origin).strip().casefold()
        if normalized_origin not in {
            "interactive", "companion_suggestion", "companion_action",
        }:
            raise ValueError("Invalid Presence run origin")
        if not isinstance(replayable, bool):
            raise TypeError("Presence replayable flag must be boolean")
        if normalized_origin != "interactive" and replayable:
            raise ValueError("Companion Presence jobs must not be replayable")
        if normalized_origin != "interactive":
            # Enforce the privacy boundary at the durable sink, not only at the
            # current Presence caller. Future callers cannot accidentally persist
            # screen-derived prompts or even image descriptors.
            safe_prompt = "[ephemeral Screen Companion request]"
            attachments_json = "[]"
        try:
            descriptors = _memory().json.loads(str(attachments_json))
        except (TypeError, ValueError, _memory().json.JSONDecodeError):
            raise ValueError("Presence image descriptors are invalid") from None
        if not isinstance(descriptors, list) or len(descriptors) > 4:
            raise ValueError("Presence image descriptors are invalid")
        for descriptor in descriptors:
            if (
                not isinstance(descriptor, dict)
                or set(descriptor) != {"mime", "bytes", "sha256"}
                or descriptor.get("mime") not in {
                    "image/png", "image/jpeg", "image/webp", "image/gif"
                }
                or isinstance(descriptor.get("bytes"), bool)
                or not isinstance(descriptor.get("bytes"), int)
                or not 0 < descriptor["bytes"] <= 5 * 1024 * 1024
                or _memory().re.fullmatch(r"[0-9a-f]{64}", str(descriptor.get("sha256") or "")) is None
            ):
                raise ValueError("Presence image descriptors are invalid")
        safe_attachments_json = _memory().json.dumps(
            descriptors, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        stamp = _memory().now_iso()
        try:
            with self._immediate_transaction():
                self.db.execute(
                    """INSERT INTO presence_jobs(
                           job_id, created_at, updated_at, conversation_id,
                           project_id, prompt, attachments_json, model_override,
                           status, run_origin, replayable
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                    (
                        normalized_id, stamp, stamp, int(conversation_id),
                        normalized_project, safe_prompt, safe_attachments_json, model,
                        normalized_origin, int(replayable),
                    ),
                )
        except _memory().sqlite3.IntegrityError as exc:
            if "presence_jobs.conversation_id" in str(exc):
                raise RuntimeError(
                    "That conversation already has an active or queued request"
                ) from exc
            raise
        return self.get_presence_job(normalized_id) or {}

    def get_presence_job(self, job_id: str) -> dict[str, Any] | None:
        normalized_id = self._presence_job_id(job_id)
        row = self.db.execute(
            """SELECT job_id, created_at, updated_at, conversation_id, project_id,
                      prompt, attachments_json, model_override, status, lease_owner, started_at,
                      finished_at, cancel_requested, last_error, metrics_json,
                      run_origin, replayable
               FROM presence_jobs WHERE job_id=?""",
            (normalized_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_presence_jobs(
        self,
        *,
        statuses: tuple[str, ...] = ("queued", "running"),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        allowed = {
            "queued", "running", "completed", "failed", "cancelled", "interrupted"
        }
        normalized = tuple(dict.fromkeys(str(item).strip().casefold() for item in statuses))
        if not normalized or any(item not in allowed for item in normalized):
            raise ValueError("Invalid Presence job status filter")
        placeholders = ",".join("?" for _ in normalized)
        rows = self.db.execute(
            f"""SELECT job_id, created_at, updated_at, conversation_id, project_id,
                       prompt, attachments_json, model_override, status, lease_owner, started_at,
                       finished_at, cancel_requested, last_error, metrics_json,
                       run_origin, replayable
                FROM presence_jobs WHERE status IN ({placeholders})
                ORDER BY created_at, job_id LIMIT ?""",
            (*normalized, _memory()._bounded_limit(limit, 1_000)),
        ).fetchall()
        return [dict(row) for row in rows]

    def recover_presence_jobs(self, runtime_id: str) -> dict[str, Any]:
        """Recover never-started turns and stop uncertain active turns at-most-once."""
        owner = _memory()._validated_worker_id(runtime_id)
        stamp = _memory().now_iso()
        interrupted_message = (
            "Presence restarted while this request was active. The request was preserved "
            "but was not replayed automatically because its effects may already have occurred. "
            "Review the conversation and explicitly retry if needed."
        )
        with self._immediate_transaction():
            running = self.db.execute(
                """SELECT job_id, conversation_id FROM presence_jobs
                   WHERE status='running' ORDER BY created_at, job_id"""
            ).fetchall()
            for row in running:
                self.db.execute(
                    """UPDATE presence_jobs
                       SET status='interrupted', updated_at=?, finished_at=?,
                           lease_owner=NULL, last_error=?
                       WHERE job_id=? AND status='running'""",
                    (stamp, stamp, interrupted_message, row["job_id"]),
                )
                self.db.execute(
                    """INSERT INTO messages(conversation_id, created_at, role, content)
                       VALUES (?, ?, 'assistant', ?)""",
                    (int(row["conversation_id"]), stamp, interrupted_message),
                )
            queued = self.db.execute(
                """SELECT job_id, created_at, updated_at, conversation_id, project_id,
                          prompt, attachments_json, model_override, status, lease_owner, started_at,
                          finished_at, cancel_requested, last_error, metrics_json,
                          run_origin, replayable
                   FROM presence_jobs WHERE status='queued'
                   ORDER BY created_at, job_id"""
            ).fetchall()
            recoverable: list[sqlite3.Row] = []
            for row in queued:
                if (
                    bool(int(row["replayable"]))
                    and str(row["attachments_json"] or "[]") == "[]"
                ):
                    recoverable.append(row)
                    continue
                interruption_message = (
                    "Presence restarted before this Companion request began. The stale "
                    "observation was not replayed; wait for a fresh observation."
                    if not bool(int(row["replayable"]))
                    else (
                        "Presence restarted before this image request began. The image bytes "
                        "were intentionally not persisted; attach the images again and retry."
                    )
                )
                self.db.execute(
                    """UPDATE presence_jobs
                       SET status='interrupted', updated_at=?, finished_at=?,
                           lease_owner=NULL, last_error=?
                       WHERE job_id=? AND status='queued'""",
                    (stamp, stamp, interruption_message, row["job_id"]),
                )
                self.db.execute(
                    """INSERT INTO messages(conversation_id, created_at, role, content)
                       VALUES (?, ?, 'assistant', ?)""",
                    (int(row["conversation_id"]), stamp, interruption_message),
                )
        return {
            "runtime_id": owner,
            "interrupted": [str(row["job_id"]) for row in running],
            "queued": [dict(row) for row in recoverable],
        }

    def claim_presence_job(self, job_id: str, runtime_id: str) -> bool:
        normalized_id = self._presence_job_id(job_id)
        owner = _memory()._validated_worker_id(runtime_id)
        stamp = _memory().now_iso()
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE presence_jobs
                   SET status='running', updated_at=?, started_at=?, lease_owner=?
                   WHERE job_id=? AND status='queued' AND cancel_requested=0""",
                (stamp, stamp, owner, normalized_id),
            )
        return updated.rowcount == 1

    def request_presence_job_cancel(
        self,
        job_id: str,
        *,
        persist_confirmation: bool = False,
    ) -> str | None:
        """Request cancellation and durably finish a never-started chat turn.

        Queued jobs cannot rely on a worker to publish their terminal state: once
        cancelled here they are intentionally no longer claimable.  Persist the
        interactive assistant confirmation in the same transaction as that
        state transition so a restart cannot leave a silently cancelled turn.
        """
        normalized_id = self._presence_job_id(job_id)
        stamp = _memory().now_iso()
        with self._immediate_transaction():
            row = self.db.execute(
                """SELECT status, conversation_id, run_origin
                   FROM presence_jobs WHERE job_id=?""",
                (normalized_id,),
            ).fetchone()
            if row is None or row["status"] not in {"queued", "running"}:
                return None
            if row["status"] == "queued":
                self.db.execute(
                    """UPDATE presence_jobs
                       SET status='cancelled', updated_at=?, finished_at=?,
                           cancel_requested=1, last_error='Request cancelled before execution'
                       WHERE job_id=? AND status='queued'""",
                    (stamp, stamp, normalized_id),
                )
                if (
                    persist_confirmation
                    and str(row["run_origin"] or "").strip().casefold()
                    == "interactive"
                ):
                    self.db.execute(
                        """INSERT INTO messages(conversation_id, created_at, role, content)
                           VALUES (?, ?, 'assistant', ?)""",
                        (
                            int(row["conversation_id"]),
                            stamp,
                            "Request cancelled before execution.",
                        ),
                    )
                return "cancelled"
            self.db.execute(
                """UPDATE presence_jobs SET updated_at=?, cancel_requested=1
                   WHERE job_id=? AND status='running'""",
                (stamp, normalized_id),
            )
            return "cancelling"

    def abandon_unqueued_companion_action(self, job_id: str) -> bool:
        """Atomically cancel a never-enqueued action and release its feedback bind."""
        normalized_id = self._presence_job_id(job_id)
        action_job_digest = self._screen_companion_action_job_digest(normalized_id)
        stamp = _memory().now_iso()
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE presence_jobs
                   SET status='cancelled', updated_at=?, finished_at=?,
                       cancel_requested=1,
                       last_error='Request could not enter the full work queue'
                   WHERE job_id=? AND status='queued'""",
                (stamp, stamp, normalized_id),
            )
            self.db.execute(
                """DELETE FROM screen_companion_feedback
                   WHERE action_job_sha256=? AND decision='accepted'
                     AND id NOT IN (
                         SELECT feedback_id FROM screen_companion_action_outcomes
                     )""",
                (action_job_digest,),
            )
        return updated.rowcount == 1

    def finish_presence_job(
        self,
        job_id: str,
        status: str,
        *,
        runtime_id: str,
        error: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> bool:
        normalized_id = self._presence_job_id(job_id)
        owner = _memory()._validated_worker_id(runtime_id)
        terminal = str(status).strip().casefold()
        if terminal not in {"completed", "failed", "cancelled", "interrupted"}:
            raise ValueError("Presence job requires a terminal status")
        stamp = _memory().now_iso()
        safe_error = (
            None
            if error is None
            else _memory()._bounded_persisted_text(
                _memory().redact_secrets(str(error)), _memory().MAX_TASK_ERROR_CHARS, "presence error"
            )
        )
        try:
            safe_metrics = _memory().sanitize_run_metrics(metrics)
        except ValueError as exc:
            if str(exc) == "unsupported run metric field":
                raise ValueError("Unsupported Presence metric") from None
            raise
        metrics_json = _memory().json.dumps(
            safe_metrics,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE presence_jobs
                   SET status=?, updated_at=?, finished_at=?, lease_owner=NULL,
                       last_error=?, metrics_json=?
                   WHERE job_id=? AND status='running' AND lease_owner=?""",
                (
                    terminal, stamp, stamp, safe_error, metrics_json,
                    normalized_id, owner,
                ),
            )
        return updated.rowcount == 1

    def presence_performance_summary(
        self,
        *,
        hours: int = 24,
        limit: int = 5_000,
        build_id: str | None = None,
        cohort: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate bounded prompt-free Presence telemetry for operators."""

        if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 720:
            raise ValueError("hours must be an integer between 1 and 720")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20_000:
            raise ValueError("limit must be an integer between 1 and 20000")
        cutoff = (_memory()._as_utc() - _memory().timedelta(hours=hours)).isoformat()
        rows = self.db.execute(
            """SELECT status, metrics_json FROM presence_jobs
               WHERE finished_at IS NOT NULL AND finished_at>=?
               ORDER BY finished_at DESC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        records: list[dict[str, Any]] = []
        discarded = 0
        for row in rows:
            try:
                decoded = _memory().json.loads(str(row["metrics_json"] or "{}"))
                if not isinstance(decoded, dict):
                    raise ValueError("metrics row is not an object")
                decoded.setdefault("status", str(row["status"]))
                records.append(_memory().sanitize_run_metrics(decoded))
            except (TypeError, ValueError, _memory().json.JSONDecodeError):
                discarded += 1
        summary = _memory().aggregate_run_metrics(
            records,
            build_id=build_id,
            cohort=cohort,
        )
        summary.update({
            "window_hours": hours,
            "row_limit": limit,
            "discarded_records": discarded,
            "truncated": len(rows) == limit,
        })
        return summary

    @staticmethod
    def _presence_pairing_code(value: Any) -> str:
        code = _memory().re.sub(r"[\s-]+", "", str(value or "")).upper()
        if len(code) != 12 or any(char not in _memory()._PAIRING_ALPHABET for char in code):
            raise ValueError("Pairing code is invalid")
        return code

    @staticmethod
    def _presence_session_id(value: Any) -> str:
        session_id = str(value or "").strip().casefold()
        if _memory().re.fullmatch(r"[0-9a-f]{32}", session_id) is None:
            raise ValueError("Presence session id is invalid")
        return session_id

    @staticmethod
    def _pairing_digest(code: str, salt: bytes) -> bytes:
        return _memory().hashlib.pbkdf2_hmac(
            "sha256", code.encode("ascii"), salt, _memory()._PAIRING_PBKDF2_ROUNDS
        )

    def create_presence_pairing_code(
        self,
        label: str = "remote device",
        *,
        ttl_minutes: int = 10,
    ) -> dict[str, Any]:
        """Create a short-lived code; persist only its salted slow hash."""
        safe_label = _memory().redact_secrets(str(label).strip())[:120] or "remote device"
        ttl = max(1, min(int(ttl_minutes), 30))
        created = _memory()._as_utc()
        expires = created + _memory().timedelta(minutes=ttl)
        code = "".join(_memory().secrets.choice(_memory()._PAIRING_ALPHABET) for _ in range(12))
        salt = _memory().secrets.token_bytes(16)
        digest = self._pairing_digest(code, salt)
        with self._immediate_transaction():
            self.db.execute(
                """UPDATE presence_pairing_codes SET status='revoked'
                   WHERE status='pending'"""
            )
            cursor = self.db.execute(
                """INSERT INTO presence_pairing_codes(
                       created_at, expires_at, label, code_salt, code_digest, status
                   ) VALUES (?, ?, ?, ?, ?, 'pending')""",
                (created.isoformat(), expires.isoformat(), safe_label, salt, digest),
            )
        return {
            "pairing_id": int(cursor.lastrowid),
            "code": f"{code[:4]}-{code[4:8]}-{code[8:]}",
            "label": safe_label,
            "expires_at": expires.isoformat(),
        }

    def consume_presence_pairing_code(
        self,
        code: str,
        *,
        session_ttl_hours: int = 24 * 30,
    ) -> dict[str, Any] | None:
        """Atomically exchange one unexpired code for one high-entropy session."""
        try:
            normalized = self._presence_pairing_code(code)
        except ValueError:
            # Invalid shapes take one slow hash too, reducing the timing oracle.
            self._pairing_digest("2" * 12, b"\0" * 16)
            return None
        current = _memory()._as_utc()
        ttl = max(1, min(int(session_ttl_hours), 24 * 90))
        with self._immediate_transaction():
            rows = self.db.execute(
                """SELECT id, label, code_salt, code_digest
                   FROM presence_pairing_codes
                   WHERE status='pending' AND expires_at>? ORDER BY id DESC LIMIT 8""",
                (current.isoformat(),),
            ).fetchall()
            matched = None
            for row in rows:
                candidate = self._pairing_digest(normalized, bytes(row["code_salt"]))
                if _memory().secrets.compare_digest(candidate, bytes(row["code_digest"])):
                    matched = row
                    break
            if matched is None:
                return None
            consumed = self.db.execute(
                """UPDATE presence_pairing_codes
                   SET status='consumed', consumed_at=?
                   WHERE id=? AND status='pending' AND expires_at>?""",
                (current.isoformat(), int(matched["id"]), current.isoformat()),
            )
            if consumed.rowcount != 1:
                return None
            token = _memory().secrets.token_urlsafe(32)
            session_id = _memory().uuid4().hex
            expires = current + _memory().timedelta(hours=ttl)
            self.db.execute(
                """INSERT INTO presence_sessions(
                       session_id, session_digest, created_at, expires_at,
                       last_seen_at, label, pairing_code_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    _memory().hashlib.sha256(token.encode("ascii")).hexdigest(),
                    current.isoformat(),
                    expires.isoformat(),
                    current.isoformat(),
                    str(matched["label"]),
                    int(matched["id"]),
                ),
            )
        return {
            "session_id": session_id,
            "token": token,
            "label": str(matched["label"]),
            "expires_at": expires.isoformat(),
        }

    def authenticate_presence_session(self, token: Any) -> bool:
        raw = str(token or "")
        if len(raw) < 32 or len(raw) > 128 or _memory().re.fullmatch(r"[A-Za-z0-9_-]+", raw) is None:
            return False
        digest = _memory().hashlib.sha256(raw.encode("ascii")).hexdigest()
        current_dt = _memory()._as_utc()
        current = current_dt.isoformat()
        row = self.db.execute(
            """SELECT last_seen_at FROM presence_sessions
               WHERE session_digest=? AND revoked_at IS NULL AND expires_at>?""",
            (digest, current),
        ).fetchone()
        if row is None:
            return False
        # Authentication polling is read-only in the common case. Persist a
        # coarse last-seen heartbeat without turning every event poll into a write.
        heartbeat_cutoff = (current_dt - _memory().timedelta(minutes=5)).isoformat()
        if str(row["last_seen_at"]) < heartbeat_cutoff:
            with self._immediate_transaction():
                self.db.execute(
                    """UPDATE presence_sessions SET last_seen_at=?
                       WHERE session_digest=? AND revoked_at IS NULL AND expires_at>?
                         AND last_seen_at<?""",
                    (current, digest, current, heartbeat_cutoff),
                )
        return True

    def list_presence_sessions(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT session_id, created_at, expires_at, last_seen_at,
                      revoked_at, label
               FROM presence_sessions ORDER BY created_at DESC, session_id"""
        ).fetchall()
        return [dict(row) for row in rows]

    def revoke_presence_session(self, session_id: str) -> bool:
        normalized = self._presence_session_id(session_id)
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE presence_sessions SET revoked_at=?
                   WHERE session_id=? AND revoked_at IS NULL""",
                (_memory().now_iso(), normalized),
            )
        return updated.rowcount == 1

    def revoke_all_presence_sessions(self) -> int:
        with self._immediate_transaction():
            updated = self.db.execute(
                """UPDATE presence_sessions SET revoked_at=? WHERE revoked_at IS NULL""",
                (_memory().now_iso(),),
            )
        return int(updated.rowcount)

    @staticmethod
    def _screen_companion_app(value: Any) -> str:
        app = _memory().Path(str(value or "").strip()).name.casefold()
        if not app or len(app) > 120 or _memory().re.fullmatch(r"[a-z0-9._ +()-]+", app) is None:
            raise ValueError("Screen Companion application name is invalid")
        if _memory().contains_secret(app):
            raise ValueError("Potential secret detected in application name")
        return app

    def screen_companion_state(self) -> dict[str, Any]:
        row = self.db.execute(
            """SELECT mode, paused, auto_suggest, excluded_apps_json, updated_at
               FROM screen_companion_state WHERE id=1"""
        ).fetchone()
        if row is None:
            raise RuntimeError("Screen Companion state is unavailable")
        try:
            raw_excluded = _memory().json.loads(str(row["excluded_apps_json"]))
        except _memory().json.JSONDecodeError:
            raw_excluded = []
        excluded = [
            str(item) for item in raw_excluded
            if isinstance(item, str)
        ][:64]
        return {
            "mode": str(row["mode"]),
            "paused": bool(int(row["paused"])),
            "auto_suggest": bool(int(row["auto_suggest"])),
            "excluded_apps": excluded,
            "updated_at": str(row["updated_at"]),
        }

    def set_screen_companion_state(
        self,
        *,
        mode: str,
        paused: bool,
        auto_suggest: bool,
        excluded_apps: list[str],
    ) -> dict[str, Any]:
        normalized_mode = str(mode).strip().casefold()
        if normalized_mode not in {"disabled", "observe", "suggest", "collaborate"}:
            raise ValueError("Screen Companion mode is invalid")
        if not isinstance(paused, bool) or not isinstance(auto_suggest, bool):
            raise TypeError("Screen Companion switches must be boolean")
        if not isinstance(excluded_apps, list) or len(excluded_apps) > 64:
            raise ValueError("Screen Companion exclusions must contain at most 64 apps")
        normalized_excluded = sorted({
            self._screen_companion_app(item) for item in excluded_apps
        })
        if normalized_mode == "disabled":
            paused = True
            auto_suggest = False
        elif normalized_mode == "observe":
            auto_suggest = False
        stamp = _memory().now_iso()
        with self._immediate_transaction():
            self.db.execute(
                """UPDATE screen_companion_state
                   SET mode=?, paused=?, auto_suggest=?, excluded_apps_json=?,
                       updated_at=? WHERE id=1""",
                (
                    normalized_mode,
                    int(paused),
                    int(auto_suggest),
                    _memory().json.dumps(normalized_excluded, separators=(",", ":")),
                    stamp,
                ),
            )
        return self.screen_companion_state()

    def control_screen_companion_state(
        self,
        *,
        action: str,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Apply one small Companion control change without replacing its settings."""
        normalized_action = str(action or "").strip().casefold()
        if normalized_action not in {"on", "pause", "resume", "off", "mode"}:
            raise ValueError(
                "Screen Companion action must be on, pause, resume, off, or mode"
            )
        normalized_mode = None if mode is None else str(mode).strip().casefold()
        if normalized_action == "mode":
            if normalized_mode not in {"observe", "suggest", "collaborate"}:
                raise ValueError(
                    "Screen Companion mode control must select observe, suggest, or collaborate"
                )
        elif normalized_mode is not None:
            raise ValueError("Screen Companion mode is only valid for the mode action")

        stamp = _memory().now_iso()
        with self._immediate_transaction():
            row = self.db.execute(
                """SELECT mode, paused, auto_suggest
                   FROM screen_companion_state WHERE id=1"""
            ).fetchone()
            if row is None:
                raise RuntimeError("Screen Companion state is unavailable")
            current_mode = str(row["mode"])
            paused = bool(int(row["paused"]))
            auto_suggest = bool(int(row["auto_suggest"]))

            if normalized_action in {"on", "resume"}:
                if current_mode == "disabled":
                    current_mode = "observe"
                    auto_suggest = False
                paused = False
            elif normalized_action == "pause":
                paused = True
            elif normalized_action == "off":
                current_mode = "disabled"
                paused = True
                auto_suggest = False
            else:
                current_mode = str(normalized_mode)
                paused = False
                if current_mode == "observe":
                    auto_suggest = False

            self.db.execute(
                """UPDATE screen_companion_state
                   SET mode=?, paused=?, auto_suggest=?, updated_at=? WHERE id=1""",
                (current_mode, int(paused), int(auto_suggest), stamp),
            )
        return self.screen_companion_state()

    def add_screen_companion_rule(
        self,
        *,
        trigger_app: str,
        action_prompt: str,
        action_mode: str = "suggest",
        title_contains: str | None = None,
        cooldown_seconds: int = 300,
    ) -> int:
        app = self._screen_companion_app(trigger_app)
        prompt = _memory().redact_secrets(str(action_prompt).strip())
        if not prompt or len(prompt) > 4_000:
            raise ValueError("Screen Companion rule prompt must contain 1-4000 characters")
        if _memory().contains_secret(str(action_prompt)):
            raise ValueError("Potential secret detected; Screen Companion rule refused")
        mode = str(action_mode).strip().casefold()
        if mode not in {"suggest", "collaborate"}:
            raise ValueError("Screen Companion rule mode is invalid")
        title = None
        if title_contains is not None and str(title_contains).strip():
            raw_title = str(title_contains).strip()
            if _memory().contains_secret(raw_title):
                raise ValueError("Potential secret detected in title matcher")
            title = _memory().redact_secrets(raw_title)[:200].casefold()
        if (
            isinstance(cooldown_seconds, bool)
            or not isinstance(cooldown_seconds, int)
            or not 30 <= cooldown_seconds <= 86_400
        ):
            raise ValueError("Screen Companion cooldown must be 30-86400 seconds")
        stamp = _memory().now_iso()
        with self._immediate_transaction():
            cursor = self.db.execute(
                """INSERT INTO screen_companion_rules(
                       created_at, updated_at, trigger_app, title_contains,
                       action_prompt, action_mode, cooldown_seconds, enabled
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                (stamp, stamp, app, title, prompt, mode, cooldown_seconds),
            )
        return int(cursor.lastrowid)

    def list_screen_companion_rules(self) -> list[dict[str, Any]]:
        return [
            {
                **dict(row),
                "enabled": bool(int(row["enabled"])),
            }
            for row in self.db.execute(
                """SELECT id, created_at, updated_at, trigger_app,
                          title_contains, action_prompt, action_mode,
                          cooldown_seconds, enabled, last_triggered_at
                   FROM screen_companion_rules ORDER BY id"""
            ).fetchall()
        ]

    def set_screen_companion_rule_enabled(self, rule_id: int, enabled: bool) -> bool:
        normalized = self._prediction_optional_id(rule_id, "rule_id")
        if normalized is None:
            raise ValueError("rule_id is required")
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be boolean")
        with self._immediate_transaction():
            cursor = self.db.execute(
                """UPDATE screen_companion_rules
                   SET enabled=?, updated_at=? WHERE id=?""",
                (int(enabled), _memory().now_iso(), normalized),
            )
        return cursor.rowcount == 1

    def delete_screen_companion_rule(self, rule_id: int) -> bool:
        normalized = self._prediction_optional_id(rule_id, "rule_id")
        if normalized is None:
            raise ValueError("rule_id is required")
        with self._immediate_transaction():
            self.db.execute(
                "DELETE FROM screen_companion_receipts WHERE rule_id=?",
                (normalized,),
            )
            cursor = self.db.execute(
                "DELETE FROM screen_companion_rules WHERE id=?", (normalized,)
            )
        return cursor.rowcount == 1

    def claim_screen_companion_rule(
        self,
        rule_id: int,
        *,
        application: str,
        context_sha256: str,
        now: datetime | None = None,
    ) -> int | None:
        normalized = self._prediction_optional_id(rule_id, "rule_id")
        app = self._screen_companion_app(application)
        digest = str(context_sha256).strip().casefold()
        if _memory().re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("Screen Companion context digest is invalid")
        current = _memory()._as_utc(now)
        stamp = current.isoformat()
        with self._immediate_transaction():
            row = self.db.execute(
                """SELECT action_mode, cooldown_seconds, enabled,
                          last_triggered_at
                   FROM screen_companion_rules WHERE id=?""",
                (normalized,),
            ).fetchone()
            if row is None or not bool(int(row["enabled"])):
                return None
            last = row["last_triggered_at"]
            if last is not None:
                try:
                    last_at = _memory()._as_utc(_memory().datetime.fromisoformat(str(last)))
                except ValueError:
                    return None
                if current < last_at + _memory().timedelta(seconds=int(row["cooldown_seconds"])):
                    return None
            self.db.execute(
                """UPDATE screen_companion_rules
                   SET last_triggered_at=?, updated_at=? WHERE id=?""",
                (stamp, stamp, normalized),
            )
            cursor = self.db.execute(
                """INSERT INTO screen_companion_receipts(
                       created_at, rule_id, application_sha256, context_sha256,
                       action_mode, status
                   ) VALUES (?, ?, ?, ?, ?, 'claimed')""",
                (
                    stamp,
                    normalized,
                    _memory().hashlib.sha256(app.encode("utf-8")).hexdigest(),
                    digest,
                    str(row["action_mode"]),
                ),
            )
        return int(cursor.lastrowid)

    def finish_screen_companion_receipt(
        self,
        receipt_id: int,
        *,
        status: str,
        job_id: str | None = None,
    ) -> bool:
        normalized = self._prediction_optional_id(receipt_id, "receipt_id")
        safe_status = str(status).strip().casefold()
        if safe_status not in {"suggested", "queued", "skipped", "failed"}:
            raise ValueError("Screen Companion receipt status is invalid")
        safe_job = None
        if job_id is not None:
            safe_job = _memory()._validated_nonsecret_metadata(job_id, "Screen Companion job ID")[:100]
        with self._immediate_transaction():
            cursor = self.db.execute(
                """UPDATE screen_companion_receipts SET status=?, job_id=?
                   WHERE id=? AND status='claimed'""",
                (safe_status, safe_job, normalized),
            )
        return cursor.rowcount == 1

    def claim_screen_companion_auto(
        self,
        *,
        context_sha256: str,
        cooldown_seconds: int,
        daily_limit: int = 6,
        now: datetime | None = None,
    ) -> int | None:
        """Atomically reserve one automatic suggestion across process restarts.

        Only a context digest and timestamps are retained.  This receipt grants no
        tool or action authority; it solely makes the privacy/rate limit durable.
        """
        digest = self._screen_companion_learning_digest(
            context_sha256, "Screen Companion context digest"
        )
        if (
            isinstance(cooldown_seconds, bool)
            or not isinstance(cooldown_seconds, int)
            or not 30 <= cooldown_seconds <= 86_400
        ):
            raise ValueError("Screen Companion automatic cooldown is invalid")
        if (
            isinstance(daily_limit, bool)
            or not isinstance(daily_limit, int)
            or not 1 <= daily_limit <= 100
        ):
            raise ValueError("Screen Companion automatic daily limit is invalid")
        current = _memory()._as_utc(now or _memory().datetime.now(_memory().timezone.utc))
        stamp = current.isoformat()
        day_key = current.date().isoformat()
        with self._immediate_transaction():
            # Keep the privacy receipt store bounded while retaining enough history
            # for restart-safe daily and cooldown enforcement.
            self.db.execute(
                "DELETE FROM screen_companion_auto_receipts WHERE created_at < ?",
                ((current - _memory().timedelta(days=31)).isoformat(),),
            )
            if self.db.execute(
                """SELECT 1 FROM screen_companion_auto_receipts
                   WHERE day_key=? AND context_sha256=?""",
                (day_key, digest),
            ).fetchone() is not None:
                return None
            count = int(self.db.execute(
                """SELECT COUNT(*) FROM screen_companion_auto_receipts
                   WHERE day_key=?""",
                (day_key,),
            ).fetchone()[0])
            if count >= daily_limit:
                return None
            latest = self.db.execute(
                """SELECT created_at FROM screen_companion_auto_receipts
                   ORDER BY created_at DESC, id DESC LIMIT 1"""
            ).fetchone()
            if latest is not None:
                try:
                    latest_at = _memory()._as_utc(_memory().datetime.fromisoformat(str(latest[0])))
                except ValueError:
                    return None
                if current < latest_at + _memory().timedelta(seconds=cooldown_seconds):
                    return None
            cursor = self.db.execute(
                """INSERT INTO screen_companion_auto_receipts(
                       created_at, day_key, context_sha256
                   ) VALUES (?, ?, ?)""",
                (stamp, day_key, digest),
            )
        return int(cursor.lastrowid)

    @staticmethod
    def _screen_companion_learning_digest(value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{label} must be a SHA-256 string")
        digest = value.strip().casefold()
        if _memory().re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(f"{label} must be a lowercase SHA-256 digest")
        return digest

    @staticmethod
    def _screen_companion_action_job_digest(action_job_id: Any) -> str:
        if not isinstance(action_job_id, str):
            raise TypeError("Screen Companion action job ID must be a string")
        normalized = action_job_id.strip()
        if (
            not normalized
            or len(normalized) > 200
            or _memory().re.fullmatch(r"[A-Za-z0-9._:-]+", normalized) is None
            or _memory().contains_secret(normalized)
        ):
            raise ValueError("Screen Companion action job ID is invalid")
        return _memory().hashlib.sha256(
            ("jarvis-screen-companion-action-v1\0" + normalized).encode("utf-8")
        ).hexdigest()

    def record_screen_companion_feedback(
        self,
        *,
        suggestion_sha256: str,
        context_sha256: str,
        application_sha256: str,
        decision: str,
        category: str = "general",
        action_mode: str = "suggest",
        action_job_id: str | None = None,
    ) -> int:
        """Record content-free operator feedback exactly once.

        The three caller-provided digests are opaque identifiers. The optional
        action job identifier is hashed before persistence and is required only
        for an accepted suggestion. No screen, title, prompt, or suggestion text
        enters this table.
        """
        suggestion_digest = self._screen_companion_learning_digest(
            suggestion_sha256, "Screen Companion suggestion digest"
        )
        context_digest = self._screen_companion_learning_digest(
            context_sha256, "Screen Companion context digest"
        )
        application_digest = self._screen_companion_learning_digest(
            application_sha256, "Screen Companion application digest"
        )
        normalized_decision = str(decision).strip().casefold()
        normalized_category = str(category).strip().casefold()
        normalized_mode = str(action_mode).strip().casefold()
        if normalized_decision not in self.SCREEN_COMPANION_LEARNING_DECISIONS:
            raise ValueError("Screen Companion feedback decision is invalid")
        if normalized_category not in self.SCREEN_COMPANION_LEARNING_CATEGORIES:
            raise ValueError("Screen Companion feedback category is invalid")
        if normalized_mode not in {"suggest", "collaborate"}:
            raise ValueError("Screen Companion feedback action mode is invalid")
        action_job_digest = None
        if normalized_decision == "accepted":
            if action_job_id is None:
                raise ValueError("Accepted Companion feedback requires an action job ID")
            action_job_digest = self._screen_companion_action_job_digest(action_job_id)
        elif action_job_id is not None:
            raise ValueError("Dismissed Companion feedback must not bind an action job")

        exact = (
            suggestion_digest,
            context_digest,
            application_digest,
            normalized_category,
            normalized_mode,
            normalized_decision,
            action_job_digest,
        )
        try:
            with self._immediate_transaction():
                existing = self.db.execute(
                    """SELECT id, suggestion_sha256, context_sha256,
                              application_sha256, category, action_mode,
                              decision, action_job_sha256
                       FROM screen_companion_feedback
                       WHERE suggestion_sha256=? AND context_sha256=?
                         AND application_sha256=?""",
                    exact[:3],
                ).fetchone()
                if existing is not None:
                    observed = tuple(existing[key] for key in (
                        "suggestion_sha256", "context_sha256", "application_sha256",
                        "category", "action_mode", "decision", "action_job_sha256",
                    ))
                    if observed != exact:
                        raise ValueError(
                            "Companion feedback identifier is already bound differently"
                        )
                    return int(existing["id"])
                cursor = self.db.execute(
                    """INSERT INTO screen_companion_feedback(
                           created_at, suggestion_sha256, context_sha256,
                           application_sha256, category, action_mode, decision,
                           action_job_sha256
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (_memory().now_iso(), *exact),
                )
                return int(cursor.lastrowid)
        except _memory().sqlite3.IntegrityError:
            raise ValueError(
                "Companion feedback conflicts with an existing protected binding"
            ) from None

    def screen_companion_feedback_for_action_job(
        self, action_job_id: str
    ) -> dict[str, Any] | None:
        """Find content-free feedback after restart using a hashed action job ID."""
        action_job_digest = self._screen_companion_action_job_digest(action_job_id)
        row = self.db.execute(
            """SELECT f.id AS feedback_id, f.created_at,
                      f.suggestion_sha256, f.context_sha256,
                      f.application_sha256, f.category, f.action_mode,
                      f.decision, o.recorded_at AS outcome_recorded_at,
                      o.outcome, o.evidence_kind, o.prediction_id, o.reusable
               FROM screen_companion_feedback AS f
               LEFT JOIN screen_companion_action_outcomes AS o
                 ON o.feedback_id=f.id
               WHERE f.action_job_sha256=?""",
            (action_job_digest,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if result["reusable"] is not None:
            result["reusable"] = bool(int(result["reusable"]))
        return result

    def discard_screen_companion_feedback_for_action_job(
        self, action_job_id: str
    ) -> bool:
        """Remove an accepted binding whose action never entered the work queue."""
        action_job_digest = self._screen_companion_action_job_digest(action_job_id)
        with self._immediate_transaction():
            cursor = self.db.execute(
                """DELETE FROM screen_companion_feedback
                   WHERE action_job_sha256=? AND decision='accepted'
                     AND id NOT IN (
                         SELECT feedback_id FROM screen_companion_action_outcomes
                     )""",
                (action_job_digest,),
            )
        return cursor.rowcount == 1

    def bind_screen_companion_outcome(
        self,
        *,
        action_job_id: str,
        prediction_id: int,
    ) -> bool:
        """Bind one exact resolved Companion prediction to accepted feedback.

        A positive reusable outcome requires a resolved ``companion_action``
        prediction, ``actual_status='complete'``, and ``evidence_ok=1``. Failed
        or incomplete predictions remain useful negative signals but are marked
        permanently non-reusable.
        """
        action_job_digest = self._screen_companion_action_job_digest(action_job_id)
        normalized_prediction = self._prediction_optional_id(
            prediction_id, "prediction_id"
        )
        with self._immediate_transaction():
            feedback = self.db.execute(
                """SELECT id, decision FROM screen_companion_feedback
                   WHERE action_job_sha256=?""",
                (action_job_digest,),
            ).fetchone()
            if feedback is None or str(feedback["decision"]) != "accepted":
                raise ValueError(
                    "Companion outcome requires exact accepted feedback"
                )
            prediction = self.db.execute(
                """SELECT origin, predicted_verification, resolved_at,
                          actual_status, evidence_ok, run_id_sha256
                   FROM task_predictions WHERE id=?""",
                (normalized_prediction,),
            ).fetchone()
            if prediction is None or prediction["resolved_at"] is None:
                raise ValueError("Companion outcome requires a resolved prediction")
            if str(prediction["origin"]) != "companion_action":
                raise ValueError("Prediction is not a Companion action outcome")
            if str(prediction["run_id_sha256"] or "") != str(
                self._prediction_run_digest(action_job_id) or ""
            ):
                raise ValueError(
                    "Companion prediction is not bound to this exact action job"
                )
            outcome = str(prediction["actual_status"] or "")
            if outcome not in self.SCREEN_COMPANION_LEARNING_OUTCOMES:
                raise ValueError("Companion prediction outcome is invalid")
            if outcome == "complete":
                evidence_kind = str(prediction["predicted_verification"] or "")
                if (
                    int(prediction["evidence_ok"] or 0) != 1
                    or evidence_kind not in {
                        "cited_sources", "process_evidence", "tool_success",
                    }
                ):
                    raise ValueError(
                        "Completed Companion outcome lacks verified evidence"
                    )
                reusable = 1
            else:
                evidence_kind = "failure_observed"
                reusable = 0
            existing = self.db.execute(
                """SELECT feedback_id, outcome, evidence_kind, prediction_id,
                          reusable
                   FROM screen_companion_action_outcomes WHERE feedback_id=?""",
                (int(feedback["id"]),),
            ).fetchone()
            expected = (
                int(feedback["id"]), outcome, evidence_kind,
                int(normalized_prediction), reusable,
            )
            if existing is not None:
                observed = tuple(existing[key] for key in (
                    "feedback_id", "outcome", "evidence_kind", "prediction_id",
                    "reusable",
                ))
                if observed != expected:
                    raise ValueError(
                        "Companion feedback already has a different outcome binding"
                    )
                return True
            try:
                self.db.execute(
                    """INSERT INTO screen_companion_action_outcomes(
                           feedback_id, recorded_at, outcome, evidence_kind,
                           prediction_id, reusable
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        int(feedback["id"]), _memory().now_iso(), outcome, evidence_kind,
                        int(normalized_prediction), reusable,
                    ),
                )
            except _memory().sqlite3.IntegrityError:
                raise ValueError(
                    "Companion prediction is already bound to different feedback"
                ) from None
        return True

    def screen_companion_learning_policy(
        self,
        *,
        suggestion_sha256: str,
        application_sha256: str,
        category: str,
    ) -> dict[str, Any]:
        """Return a suppression-only policy signal from content-free feedback."""
        suggestion_digest = self._screen_companion_learning_digest(
            suggestion_sha256, "Screen Companion suggestion digest"
        )
        application_digest = self._screen_companion_learning_digest(
            application_sha256, "Screen Companion application digest"
        )
        normalized_category = str(category).strip().casefold()
        if normalized_category not in self.SCREEN_COMPANION_LEARNING_CATEGORIES:
            raise ValueError("Screen Companion feedback category is invalid")
        row = self.db.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN decision='accepted' THEN 1 ELSE 0 END) AS accepted,
                      SUM(CASE WHEN decision='dismissed' THEN 1 ELSE 0 END) AS dismissed
               FROM screen_companion_feedback
               WHERE suggestion_sha256=? AND application_sha256=? AND category=?""",
            (suggestion_digest, application_digest, normalized_category),
        ).fetchone()
        total = int(row["total"] or 0)
        accepted = int(row["accepted"] or 0)
        dismissed = int(row["dismissed"] or 0)
        category_row = self.db.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN f.decision='accepted' THEN 1 ELSE 0 END)
                          AS accepted,
                      SUM(CASE WHEN f.decision='dismissed' THEN 1 ELSE 0 END)
                          AS dismissed,
                      SUM(CASE WHEN o.reusable=1 THEN 1 ELSE 0 END)
                          AS reusable
               FROM screen_companion_feedback AS f
               LEFT JOIN screen_companion_action_outcomes AS o
                 ON o.feedback_id=f.id
               WHERE f.application_sha256=? AND f.category=?""",
            (application_digest, normalized_category),
        ).fetchone()
        category_accepted = int(category_row["accepted"] or 0)
        category_dismissed = int(category_row["dismissed"] or 0)
        category_reusable = int(category_row["reusable"] or 0)
        return {
            "total": total,
            "accepted": accepted,
            "dismissed": dismissed,
            "acceptance_rate": accepted / total if total else None,
            "category_accepted": category_accepted,
            "category_dismissed": category_dismissed,
            "category_reusable": category_reusable,
            # Feedback can only remove an automatic suggestion. It never grants
            # authority or bypasses existing mode, approval, or policy gates.
            "suppress_auto": (
                category_reusable == 0
                and (
                    (dismissed >= 3 and accepted == 0)
                    or (category_dismissed >= 3 and category_accepted == 0)
                )
            ),
        }

    def screen_companion_learning_ranking(
        self, *, application_sha256: str
    ) -> dict[str, Any]:
        """Return content-free, app-scoped category ranking signals.

        Only independently verified outcomes receive a positive score. Merely
        accepting a suggestion is never enough to teach a successful pattern.
        Scores may rank or suppress suggestions but confer no new authority.
        """
        application_digest = self._screen_companion_learning_digest(
            application_sha256, "Screen Companion application digest"
        )
        rows = self.db.execute(
            """SELECT f.category,
                      SUM(CASE WHEN f.decision='dismissed' THEN 1 ELSE 0 END)
                          AS dismissed,
                      SUM(CASE WHEN o.reusable=1 THEN 1 ELSE 0 END)
                          AS reusable,
                      SUM(CASE WHEN o.reusable=0 THEN 1 ELSE 0 END)
                          AS failed
               FROM screen_companion_feedback AS f
               LEFT JOIN screen_companion_action_outcomes AS o
                 ON o.feedback_id=f.id
               WHERE f.application_sha256=?
               GROUP BY f.category""",
            (application_digest,),
        ).fetchall()
        categories: dict[str, dict[str, int]] = {}
        for row in rows:
            reusable = int(row["reusable"] or 0)
            failed = int(row["failed"] or 0)
            dismissed = int(row["dismissed"] or 0)
            categories[str(row["category"])] = {
                "reusable": reusable,
                "failed": failed,
                "dismissed": dismissed,
                "score": reusable * 4 - failed * 3 - dismissed,
            }
        preferred = [
            category for category, values in sorted(
                categories.items(),
                key=lambda item: (-item[1]["score"], item[0]),
            )
            if values["reusable"] > 0 and values["score"] > 0
        ][:3]
        avoided = [
            category for category, values in sorted(categories.items())
            if values["reusable"] == 0
            and (values["dismissed"] >= 3 or values["score"] < 0)
        ][:3]
        return {
            "preferred": preferred,
            "avoided": avoided,
            "categories": categories,
        }

    def screen_companion_learning_stats(self) -> dict[str, Any]:
        """Return bounded aggregate feedback/outcome statistics, never content."""
        overall = self.db.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN decision='accepted' THEN 1 ELSE 0 END) AS accepted,
                      SUM(CASE WHEN decision='dismissed' THEN 1 ELSE 0 END) AS dismissed
               FROM screen_companion_feedback"""
        ).fetchone()
        outcome = self.db.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN reusable=1 THEN 1 ELSE 0 END) AS reusable,
                      SUM(CASE WHEN reusable=0 THEN 1 ELSE 0 END) AS non_reusable
               FROM screen_companion_action_outcomes"""
        ).fetchone()
        category_rows = self.db.execute(
            """SELECT f.category, COUNT(*) AS total,
                      SUM(CASE WHEN f.decision='accepted' THEN 1 ELSE 0 END) AS accepted,
                      SUM(CASE WHEN f.decision='dismissed' THEN 1 ELSE 0 END) AS dismissed,
                      COUNT(o.feedback_id) AS verified_outcomes,
                      SUM(CASE WHEN o.reusable=1 THEN 1 ELSE 0 END) AS reusable_outcomes
               FROM screen_companion_feedback AS f
               LEFT JOIN screen_companion_action_outcomes AS o
                 ON o.feedback_id=f.id
               GROUP BY f.category ORDER BY f.category"""
        ).fetchall()
        total = int(overall["total"] or 0)
        accepted = int(overall["accepted"] or 0)
        verified_outcomes = int(outcome["total"] or 0)
        reusable_outcomes = int(outcome["reusable"] or 0)
        return {
            "feedback": total,
            "accepted": accepted,
            "dismissed": int(overall["dismissed"] or 0),
            "acceptance_rate": accepted / total if total else None,
            "verified_outcomes": verified_outcomes,
            "reusable_outcomes": reusable_outcomes,
            "non_reusable_outcomes": int(outcome["non_reusable"] or 0),
            "verified_success_rate": (
                reusable_outcomes / verified_outcomes if verified_outcomes else None
            ),
            "by_category": {
                str(row["category"]): {
                    "total": int(row["total"]),
                    "accepted": int(row["accepted"] or 0),
                    "dismissed": int(row["dismissed"] or 0),
                    "verified_outcomes": int(row["verified_outcomes"] or 0),
                    "reusable_outcomes": int(row["reusable_outcomes"] or 0),
                }
                for row in category_rows
            },
        }

    def forget_screen_companion_receipts(self) -> int:
        with self._immediate_transaction():
            outcome_count = int(self.db.execute(
                "SELECT COUNT(*) FROM screen_companion_action_outcomes"
            ).fetchone()[0])
            feedback_cursor = self.db.execute(
                "DELETE FROM screen_companion_feedback"
            )
            receipt_cursor = self.db.execute("DELETE FROM screen_companion_receipts")
            auto_cursor = self.db.execute(
                "DELETE FROM screen_companion_auto_receipts"
            )
            companion_selector = (
                "SELECT id FROM task_predictions WHERE origin IN "
                "('companion_suggestion','companion_action')"
            )
            self.db.execute(
                f"DELETE FROM lesson_provenance WHERE prediction_id IN ({companion_selector})"
            )
            self.db.execute(
                f"DELETE FROM lesson_applications WHERE prediction_id IN ({companion_selector})"
            )
            self.db.execute(
                f"DELETE FROM memory_retrievals WHERE prediction_id IN ({companion_selector})"
            )
            reflection_cursor = self.db.execute(
                f"DELETE FROM reflections WHERE prediction_id IN ({companion_selector})"
            )
            prediction_cursor = self.db.execute(
                """DELETE FROM task_predictions
                   WHERE origin IN ('companion_suggestion','companion_action')"""
            )
        return (
            outcome_count
            + int(feedback_cursor.rowcount)
            + int(receipt_cursor.rowcount)
            + int(auto_cursor.rowcount)
            + int(reflection_cursor.rowcount)
            + int(prediction_cursor.rowcount)
        )
