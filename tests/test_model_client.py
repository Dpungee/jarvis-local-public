from __future__ import annotations

import io
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.attachments import ImageAttachment
from jarvis.model_client import (
    ANTHROPIC_MESSAGES_URL,
    DEFAULT_CLAUDE_CLI_MODEL,
    DEFAULT_CODEX_CLI_MODEL,
    OPENAI_RESPONSES_URL,
    AnthropicClient,
    ClaudeCLIClient,
    CodexCLIClient,
    ModelClient,
    ModelProviderError,
    OpenAIClient,
    _anthropic_messages,
    _openai_input,
    _HTTPConnectionCancellation,
    _claude_cli_launchable,
    _codex_cli_launchable,
    _codex_cli_skill_config_override,
    _CodexAppServerConversation,
    _CodexAppServerTransport,
    _CodexAppServerTurn,
    _resolved_winget_link,
    _validated_native_executable,
    _windows_cli_publisher_matches,
    build_model_client,
    isolated_codex_cli_home,
    model_conversation_scope,
    resolve_claude_cli_executable,
    resolve_codex_cli_executable,
    split_model_reference,
    user_model_error_message,
)
from jarvis.ollama_client import ChatResponse, OllamaError


class FakeResponse:
    def __init__(self, payload, *, headers=None):
        self.body = json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Length": str(len(self.body))} if headers is None else headers
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class BrokenReadResponse(FakeResponse):
    def __init__(self):
        self.body = b""
        self.headers = {}

    def read(self, size=-1):
        del size
        raise http.client.IncompleteRead(b"partial")


class FakeSSEResponse:
    def __init__(self, events, *, fail_after=None):
        self.headers = {}
        self.lines = []
        for event in events:
            encoded = json.dumps(event, separators=(",", ":")).encode("utf-8")
            self.lines.extend((b"data: " + encoded + b"\n", b"\n"))
        self.fail_after = fail_after

    def __iter__(self):
        for index, line in enumerate(self.lines):
            if self.fail_after is not None and index >= self.fail_after:
                raise http.client.IncompleteRead(b"partial stream")
            yield line

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class SequenceOpen:
    def __init__(self, *items):
        self.items = list(items)
        self.requests = []
        self.timeouts = []

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def schema(name="lookup"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Look up a value",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def history():
    return [
        {"role": "system", "content": "Trusted system contract"},
        {"role": "user", "content": "Find it"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "lookup", "arguments": {"query": "alpha"}}}
            ],
        },
        {"role": "tool", "tool_name": "lookup", "content": '{"ok":true}'},
    ]


class ProviderReferenceTests(unittest.TestCase):
    def test_unprefixed_ollama_and_explicit_cloud_references(self):
        self.assertEqual(split_model_reference("qwen3.5:9b"), ("ollama", "qwen3.5:9b"))
        self.assertEqual(split_model_reference("ollama:qwen3.5:9b"), ("ollama", "qwen3.5:9b"))
        self.assertEqual(split_model_reference("openai:gpt-5.6"), ("openai", "gpt-5.6"))
        self.assertEqual(
            split_model_reference("anthropic:claude-sonnet-5"),
            ("anthropic", "claude-sonnet-5"),
        )
        self.assertEqual(
            split_model_reference("claude-cli:sonnet"),
            ("claude-cli", "sonnet"),
        )
        self.assertEqual(
            split_model_reference("codex-cli:gpt-5.5"),
            ("codex-cli", "gpt-5.5"),
        )
        for value in (
            "", "openai:", "claude-cli:", "codex-cli:", "anthropic:bad model", "x\nmodel"
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                split_model_reference(value)

    def test_user_model_error_never_exposes_raw_provider_failure(self):
        raw = ModelProviderError(
            "OpenAI",
            "request failed with HTTP 400 secret-provider-detail",
            status_code=400,
        )

        rendered = user_model_error_message(raw)

        self.assertIn("available model fallbacks", rendered)
        self.assertIn("request was preserved", rendered)
        self.assertNotIn("HTTP 400", rendered)
        self.assertNotIn("secret-provider-detail", rendered)

    def test_user_model_error_explains_rate_limit_without_raw_provider_failure(self):
        raw = ModelProviderError(
            "OpenAI",
            "HTTP 429 private quota identifier",
            status_code=429,
            retryable=True,
        )

        rendered = user_model_error_message(raw)

        self.assertIn("rate-limited", rendered)
        self.assertIn("preserved", rendered)
        self.assertNotIn("private quota", rendered)


class ClaudeCLIProviderTests(unittest.TestCase):
    def test_factory_isolates_cli_from_the_jarvis_repository(self):
        config = SimpleNamespace(
            root=Path("C:/sensitive/jarvis-repository"),
            data_dir=Path("C:/sensitive/jarvis-repository/data"),
            cloud_enabled=True,
            cloud_generation_timeout=600.0,
            cloud_max_output_tokens=8192,
            cloud_max_response_bytes=8 * 1024 * 1024,
            cloud_max_retries=0,
            cloud_retry_backoff=0.5,
            claude_cli_enabled=True,
            fast_model="claude-cli:sonnet",
            reasoning_model="claude-cli:sonnet",
            coding_model="claude-cli:sonnet",
            deep_model="claude-cli:sonnet",
            model="auto",
            learning_model=None,
            ollama_enabled=False,
        )
        with patch.dict("os.environ", {}, clear=True), patch(
            "jarvis.model_client.resolve_claude_cli_executable",
            return_value=Path("C:/trusted/claude.exe"),
        ):
            client = build_model_client(config)

        self.assertIsNotNone(client.claude_cli)
        self.addCleanup(client.claude_cli._working_directory_owner.cleanup)
        cli_directory = Path(client.claude_cli.working_directory).resolve()
        self.assertTrue(cli_directory.is_dir())
        self.assertNotEqual(cli_directory, config.root.resolve())
        self.assertNotIn(str(config.root.resolve()).casefold(), str(cli_directory).casefold())

    def test_tool_free_chat_uses_one_plain_turn(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({
                    "is_error": False,
                    "result": "Natural answer.",
                    "usage": {"input_tokens": 12, "output_tokens": 3},
                }),
                "",
            )

        client = ClaudeCLIClient("claude.exe", working_directory=".", runner=runner)

        response = client.chat(history(), [], "sonnet")

        args, _options = calls[0]
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(args[args.index("--max-turns") + 1], "1")
        self.assertNotIn("--allowedTools", args)
        self.assertNotIn("--json-schema", args)
        self.assertEqual(response["content"], "Natural answer.")
        self.assertNotIn("tool_calls", response)

    def test_cli_transport_round_trips_unicode_as_utf8(self):
        client = ClaudeCLIClient(
            sys.executable,
            working_directory=".",
            generation_timeout=10,
        )
        prompt = "Weather: 21°C • 雪 🌧️"

        completed = client._run_cli(
            [
                sys.executable,
                "-c",
                (
                    "import sys; data=sys.stdin.buffer.read(); "
                    "sys.stdout.buffer.write(data)"
                ),
            ],
            prompt=prompt,
            timeout=10,
            flags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            cancellation_guard=lambda: False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, prompt)

    def test_cli_subprocess_is_killed_promptly_on_cancellation(self):
        cancel = threading.Event()
        client = ClaudeCLIClient(
            sys.executable,
            working_directory=".",
            generation_timeout=10,
        )
        timer = threading.Timer(0.15, cancel.set)
        started = time.monotonic()
        timer.start()
        try:
            with self.assertRaisesRegex(ModelProviderError, "cancelled"):
                client._run_cli(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    prompt="",
                    timeout=10,
                    flags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    cancellation_guard=cancel.is_set,
                )
        finally:
            timer.cancel()

        self.assertLess(time.monotonic() - started, 1.5)

    def test_bounded_cli_maps_structured_tool_call_without_granting_cli_tools(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            payload = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": {
                    "content": "I need the approved lookup.",
                    "tool_calls": [
                        {"name": "lookup", "arguments": {"query": "beta"}}
                    ],
                },
                "usage": {"input_tokens": 41, "output_tokens": 9},
            }
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

        client = ClaudeCLIClient(
            "C:/trusted/claude.exe",
            working_directory="C:/trusted/jarvis",
            generation_timeout=12,
            max_response_bytes=4096,
            runner=runner,
        )

        with patch.dict(
            "os.environ",
            {
                "PATH": "C:/trusted",
                "USERPROFILE": "C:/Users/test",
                "OPENAI_API_KEY": "must-not-cross",
                "ANTHROPIC_API_KEY": "must-not-cross",
            },
            clear=True,
        ):
            response = client.chat(history(), [schema()], "sonnet", think="high")

        args, options = calls[0]
        self.assertIn("--safe-mode", args)
        self.assertIn("--no-session-persistence", args)
        self.assertIn("--disable-slash-commands", args)
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertEqual(args[args.index("--tools") + 1], "StructuredOutput")
        self.assertEqual(args[args.index("--allowedTools") + 1], "StructuredOutput")
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(args[args.index("--max-turns") + 1], "6")
        self.assertIn(
            "Every name supplied in jarvis_tool_schemas_json is available",
            args[args.index("--append-system-prompt") + 1],
        )
        cli_system = args[args.index("--append-system-prompt") + 1]
        self.assertIn("only Claude Code tool you may invoke is StructuredOutput", cli_system)
        self.assertIn("Never invoke a name from jarvis_tool_schemas_json", cli_system)
        self.assertIn("StructuredOutput.tool_calls", cli_system)
        self.assertEqual(args[args.index("--effort") + 1], "high")
        self.assertNotIn("OPENAI_API_KEY", options["env"])
        self.assertNotIn("ANTHROPIC_API_KEY", options["env"])
        self.assertEqual(options["encoding"], "utf-8")
        self.assertEqual(options["errors"], "strict")
        self.assertIn('"name":"lookup"', options["input"])
        self.assertEqual(response["content"], "I need the approved lookup.")
        self.assertEqual(response["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(response.metrics.prompt_tokens, 41)
        self.assertEqual(response.metrics.completion_tokens, 9)

    def test_cli_authentication_failure_is_safe_and_provider_wide(self):
        calls = 0

        def runner(args, **kwargs):
            nonlocal calls
            del kwargs
            calls += 1
            return subprocess.CompletedProcess(
                args, 1, "", "Not logged in. Run claude auth login."
            )

        client = ClaudeCLIClient(
            "claude.exe",
            working_directory=".",
            max_retries=2,
            sleep=lambda _delay: None,
            runner=runner,
        )
        with self.assertRaises(ModelProviderError) as caught:
            client.chat([{"role": "user", "content": "hello"}], [], DEFAULT_CLAUDE_CLI_MODEL)
        self.assertEqual(caught.exception.status_code, 401)
        self.assertTrue(caught.exception.provider_unavailable)
        self.assertEqual(calls, 1)
        self.assertNotIn("Run claude", str(caught.exception))
        self.assertIn("claude auth login --claudeai", user_model_error_message(caught.exception))

    def test_cli_retries_transient_nonzero_exit_within_one_deadline(self):
        calls = []
        sleeps = []

        def runner(args, **kwargs):
            calls.append(kwargs["timeout"])
            if len(calls) < 3:
                return subprocess.CompletedProcess(args, 1, "", "temporary provider failure")
            payload = {
                "is_error": False,
                "result": "recovered",
            }
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

        client = ClaudeCLIClient(
            "claude.exe",
            working_directory=".",
            generation_timeout=30,
            max_retries=2,
            retry_backoff=0.25,
            runner=runner,
            sleep=sleeps.append,
        )

        response = client.chat([{"role": "user", "content": "hello"}], [], "sonnet")

        self.assertEqual(response["content"], "recovered")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(0 < timeout <= 30 for timeout in calls))
        self.assertEqual(sleeps, [0.25, 0.5])

    def test_cli_does_not_repeat_a_bounded_turn_exhaustion(self):
        calls = 0

        def runner(args, **kwargs):
            nonlocal calls
            del kwargs
            calls += 1
            return subprocess.CompletedProcess(
                args,
                1,
                json.dumps({
                    "is_error": True,
                    "terminal_reason": "max_turns",
                    "errors": ["Reached maximum number of turns (6)"],
                }),
                "",
            )

        client = ClaudeCLIClient(
            "claude.exe",
            working_directory=".",
            max_retries=2,
            runner=runner,
        )

        with self.assertRaises(ModelProviderError) as caught:
            client.chat([{"role": "user", "content": "hello"}], [], "sonnet")

        self.assertEqual(calls, 1)
        self.assertFalse(caught.exception.retryable)
        self.assertFalse(caught.exception.provider_unavailable)
        self.assertIn("bounded turn limit", str(caught.exception))

    def test_cli_timeout_is_normalized(self):
        def runner(args, **kwargs):
            del kwargs
            raise subprocess.TimeoutExpired(args, 1)

        client = ClaudeCLIClient(
            "claude.exe",
            working_directory=".",
            runner=runner,
        )
        with self.assertRaises(ModelProviderError) as caught:
            client.chat([{"role": "user", "content": "hello"}], [], "sonnet")
        self.assertTrue(caught.exception.retryable)
        self.assertTrue(caught.exception.provider_unavailable)

    def test_cli_rejects_tool_calls_not_offered_by_jarvis(self):
        def runner(args, **kwargs):
            del kwargs
            payload = {
                "is_error": False,
                "structured_output": {
                    "content": "",
                    "tool_calls": [{"name": "run_process", "arguments": {}}],
                },
            }
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

        client = ClaudeCLIClient("claude.exe", working_directory=".", runner=runner)
        with self.assertRaises(ModelProviderError) as caught:
            client.chat([{"role": "user", "content": "hello"}], [schema()], "sonnet")
        self.assertIn("unauthorized tool call", str(caught.exception))


class CodexCLIProviderTests(unittest.TestCase):
    @staticmethod
    def _write_result(args, payload, *, stdout_events=None, returncode=0, stderr=""):
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        events = stdout_events
        if events is None:
            events = [
                {"type": "thread.started", "thread_id": "synthetic"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {"id": "item_1", "type": "agent_message", "text": "done"},
                },
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 17, "output_tokens": 5},
                },
            ]
        stdout = "\n".join(json.dumps(event) for event in events)
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    def test_native_executable_validation_rejects_script_wrappers(self):
        self.assertEqual(
            _validated_native_executable(sys.executable),
            Path(sys.executable).resolve(),
        )
        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / ("codex.exe" if os.name == "nt" else "codex")
            wrapper.write_bytes(b"#!/bin/sh\nexit 0\n")
            if os.name != "nt":
                wrapper.chmod(0o755)
            self.assertIsNone(_validated_native_executable(wrapper))

    def test_bundled_system_skills_are_all_disabled_by_exact_skill_file(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            system = data / "codex-cli-home" / "skills" / ".system"
            system.mkdir(parents=True)
            (system / ".codex-system-skills.marker").write_text("bundle\n", encoding="utf-8")
            expected = []
            for name in ("imagegen", "future-bundled-skill"):
                skill = system / name / "SKILL.md"
                skill.parent.mkdir()
                skill.write_text("---\nname: test\n---\n", encoding="utf-8")
                expected.append(str(skill.resolve()))

            home = isolated_codex_cli_home(data)
            override, identities = _codex_cli_skill_config_override(home)

        self.assertEqual(identities, tuple(sorted(expected)))
        self.assertEqual(override.count("enabled=false"), 2)
        for skill_file in expected:
            self.assertIn(skill_file.replace("\\", "/"), override)
        self.assertIn("/SKILL.md", override)

    def test_custom_skill_in_isolated_profile_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            custom = data / "codex-cli-home" / "skills" / "operator-skill"
            custom.mkdir(parents=True)
            (custom / "SKILL.md").write_text("untrusted", encoding="utf-8")
            with self.assertRaisesRegex(ModelProviderError, "custom skills"):
                isolated_codex_cli_home(data)

    @staticmethod
    def _canary_records(workspace, sentinel, *, injected=False):
        def message(role, text):
            return {
                "type": "message",
                "role": role,
                "content": [{"type": "input_text", "text": text}],
            }

        records = [
            message("developer", "<permissions instructions>\nread-only\n</permissions instructions>"),
            message("user", f"<environment_context>\n<cwd>{workspace}</cwd>\n</environment_context>"),
        ]
        if injected:
            records.append(message("developer", "personal operator instructions"))
        records.append(message("user", sentinel))
        return records

    def test_context_canary_adapts_stable_flags_and_has_no_skill_catalog(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            if args[-2:] == ["features", "list"]:
                return subprocess.CompletedProcess(
                    args, 0, "shell_tool stable true\nplugins stable true\n", ""
                )
            sentinel = args[-1]
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(self._canary_records(kwargs["cwd"], sentinel)),
                "",
            )

        with tempfile.TemporaryDirectory() as directory:
            client = CodexCLIClient(
                "codex.exe", working_directory=directory, runner=runner
            )
            client.verify_context_isolation()

        canary_args = calls[-1]
        self.assertNotIn("features.view_image=false", canary_args)
        self.assertNotIn("tools.view_image=false", canary_args)

    def test_context_canary_rejects_injected_instructions(self):
        def runner(args, **kwargs):
            if args[-2:] == ["features", "list"]:
                return subprocess.CompletedProcess(
                    args, 0, "shell_tool stable true\nplugins stable true\n", ""
                )
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(self._canary_records(kwargs["cwd"], args[-1], injected=True)),
                "",
            )

        with tempfile.TemporaryDirectory() as directory:
            client = CodexCLIClient(
                "codex.exe", working_directory=directory, runner=runner
            )
            with self.assertRaisesRegex(ModelProviderError, "unexpected model-visible"):
                client.verify_context_isolation()

    def test_native_launch_probe_is_shell_free_and_secret_scrubbed(self):
        with patch.dict(
            "os.environ",
            {
                "PATH": "C:/trusted",
                "USERPROFILE": "C:/Users/test",
                "OPENAI_API_KEY": "must-not-cross",
                "CODEX_API_KEY": "must-not-cross",
                "SSH_AUTH_SOCK": "must-not-cross",
            },
            clear=True,
        ), patch("jarvis.model_client.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, "codex-cli 0.146.1\n", ""
            )
            self.assertTrue(_codex_cli_launchable(Path("codex.exe")))

        args, options = run.call_args
        self.assertEqual(args[0], ["codex.exe", "--version"])
        self.assertNotIn("OPENAI_API_KEY", options["env"])
        self.assertNotIn("CODEX_API_KEY", options["env"])
        self.assertNotIn("SSH_AUTH_SOCK", options["env"])
        self.assertNotIn("shell", options)
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        self.assertTrue(options["capture_output"])

    def test_claude_native_launch_probe_requires_vendor_version_output(self):
        with patch("jarvis.model_client.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, "2.1.56 (Claude Code)\n", ""
            )
            self.assertTrue(_claude_cli_launchable(Path("C:/trusted/claude.exe")))
            run.return_value = subprocess.CompletedProcess([], 0, "Python 3.13.7\n", "")
            self.assertFalse(_claude_cli_launchable(Path("C:/trusted/claude.exe")))

    def test_windows_cli_publisher_requires_valid_exact_vendor(self):
        if os.name != "nt":
            self.skipTest("Windows Authenticode validation")
        powershell = Path(
            "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        outcomes = (
            ("Valid\nOpenAI OpCo, LLC\n", True),
            ("NotSigned\n\n", False),
            ("HashMismatch\nOpenAI OpCo, LLC\n", False),
            ("Valid\nUnrelated Publisher LLC\n", False),
        )
        with patch(
            "jarvis.model_client.windows_directory", return_value=Path("C:/Windows")
        ), patch(
            "jarvis.model_client.windows_system_executable", return_value=powershell
        ), patch(
            "jarvis.model_client.subprocess.run"
        ) as run:
            for output, accepted in outcomes:
                with self.subTest(output=output):
                    run.return_value = subprocess.CompletedProcess([], 0, output, "")
                    self.assertEqual(
                        _windows_cli_publisher_matches(
                            Path("C:/fixed/codex.exe"), "OpenAI OpCo, LLC"
                        ),
                        accepted,
                    )
        args, options = run.call_args
        self.assertEqual(args[0][0], str(powershell))
        self.assertNotIn("C:/fixed/codex.exe", args[0])
        self.assertEqual(
            options["env"]["JARVIS_CLI_SIGNATURE_TARGET"],
            "C:\\fixed\\codex.exe",
        )

    def test_unsigned_fixed_cli_is_rejected_before_version_probe(self):
        if os.name != "nt":
            self.skipTest("Windows Authenticode validation")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = root / "claude.exe"
            codex = root / "codex.exe"
            claude.write_bytes(b"MZ\x00\x00synthetic")
            codex.write_bytes(b"MZ\x00\x00synthetic")
            with patch.dict("os.environ", {}, clear=True), patch(
                "jarvis.model_client._resolved_winget_link",
                side_effect=lambda name: claude if name == "claude.exe" else codex,
            ), patch(
                "jarvis.model_client.trusted_path_executable", return_value=None
            ), patch(
                "jarvis.model_client._windows_cli_publisher_matches", return_value=False
            ), patch(
                "jarvis.model_client._claude_cli_launchable",
                side_effect=AssertionError("unsigned Claude binary was executed"),
            ), patch(
                "jarvis.model_client._codex_cli_launchable",
                side_effect=AssertionError("unsigned Codex binary was executed"),
            ):
                self.assertIsNone(resolve_claude_cli_executable())
                self.assertIsNone(resolve_codex_cli_executable())

    def test_cli_resolvers_never_execute_current_directory_or_path_binaries(self):
        if os.name != "nt":
            self.skipTest("Windows executable search order")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("claude.exe", "codex.exe"):
                shutil.copy2(sys.executable, root / name)
            previous = Path.cwd()
            os.chdir(root)
            try:
                with patch.dict(
                    "os.environ", {"PATH": str(root)}, clear=True
                ), patch(
                    "jarvis.model_client._resolved_winget_link", return_value=None
                ), patch(
                    "jarvis.model_client.subprocess.run",
                    side_effect=AssertionError("poisoned CLI must never execute"),
                ) as run:
                    self.assertIsNone(resolve_claude_cli_executable())
                    self.assertIsNone(resolve_codex_cli_executable())
                    run.assert_not_called()
            finally:
                os.chdir(previous)

    def test_winget_link_uses_only_the_fixed_per_user_location(self):
        if os.name != "nt":
            self.skipTest("Windows WinGet link")
        with tempfile.TemporaryDirectory() as directory:
            link = (
                Path(directory)
                / "Microsoft"
                / "WinGet"
                / "Links"
                / "codex.exe"
            )
            link.parent.mkdir(parents=True)
            link.write_bytes(b"MZ\x00\x00synthetic")
            with patch.dict(
                "os.environ", {"LOCALAPPDATA": directory}, clear=True
            ):
                self.assertEqual(_resolved_winget_link("codex.exe"), link.resolve())

    def test_claude_resolver_prefers_public_winget_target(self):
        if os.name != "nt":
            self.skipTest("Windows native Claude path")
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "package" / "claude.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"MZ\x00\x00synthetic")
            with patch.dict("os.environ", {}, clear=True), patch(
                "jarvis.model_client._resolved_winget_link", return_value=executable
            ), patch(
                "jarvis.model_client.trusted_path_executable", return_value=None
            ), patch(
                "jarvis.model_client._windows_cli_publisher_matches", return_value=True
            ), patch(
                "jarvis.model_client._claude_cli_launchable", return_value=True
            ):
                self.assertEqual(
                    resolve_claude_cli_executable(), executable.resolve()
                )

    def test_resolver_uses_authenticated_plugin_appserver_binary_as_fallback(self):
        if os.name != "nt":
            self.skipTest("Windows native Codex path")
        with tempfile.TemporaryDirectory() as directory:
            executable = (
                Path(directory)
                / ".codex"
                / "plugins"
                / ".plugin-appserver"
                / "codex.exe"
            )
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"MZ\x00\x00synthetic")
            with patch.dict(
                "os.environ",
                {"USERPROFILE": directory},
                clear=True,
            ), patch(
                "jarvis.model_client.trusted_path_executable", return_value=None
            ), patch(
                "jarvis.model_client._windows_cli_publisher_matches", return_value=True
            ), patch(
                "jarvis.model_client._codex_cli_launchable", return_value=True
            ):
                self.assertEqual(resolve_codex_cli_executable(), executable.resolve())

    def test_codex_resolver_prefers_public_winget_target(self):
        if os.name != "nt":
            self.skipTest("Windows native Codex path")
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "package" / "codex.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"MZ\x00\x00synthetic")
            with patch.dict("os.environ", {}, clear=True), patch(
                "jarvis.model_client._resolved_winget_link", return_value=executable
            ), patch(
                "jarvis.model_client.trusted_path_executable", return_value=None
            ), patch(
                "jarvis.model_client._windows_cli_publisher_matches", return_value=True
            ), patch(
                "jarvis.model_client._codex_cli_launchable", return_value=True
            ):
                self.assertEqual(
                    resolve_codex_cli_executable(), executable.resolve()
                )

    def test_resolver_prefers_public_npm_binary_over_desktop_plugin_fallback(self):
        if os.name != "nt":
            self.skipTest("Windows native Codex paths")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            npm_executable = (
                root
                / "npm-root"
                / "npm"
                / "node_modules"
                / "@openai"
                / "codex"
                / "vendor"
                / "x86_64-pc-windows-msvc"
                / "codex"
                / "codex.exe"
            )
            plugin_executable = (
                root
                / "profile"
                / ".codex"
                / "plugins"
                / ".plugin-appserver"
                / "codex.exe"
            )
            for executable in (npm_executable, plugin_executable):
                executable.parent.mkdir(parents=True)
                executable.write_bytes(b"MZ\x00\x00synthetic")
            with patch.dict(
                "os.environ",
                {
                    "APPDATA": str(root / "npm-root"),
                    "USERPROFILE": str(root / "profile"),
                },
                clear=True,
            ), patch(
                "jarvis.model_client.trusted_path_executable", return_value=None
            ), patch(
                "jarvis.model_client._windows_cli_publisher_matches", return_value=True
            ), patch(
                "jarvis.model_client._codex_cli_launchable", return_value=True
            ):
                self.assertEqual(
                    resolve_codex_cli_executable(), npm_executable.resolve()
                )

    def test_resolver_skips_inaccessible_public_alias_before_plugin_fallback(self):
        if os.name != "nt":
            self.skipTest("Windows native Codex paths")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public_alias = root / "WindowsApps" / "codex.exe"
            plugin_executable = (
                root
                / "profile"
                / ".codex"
                / "plugins"
                / ".plugin-appserver"
                / "codex.exe"
            )
            for executable in (public_alias, plugin_executable):
                executable.parent.mkdir(parents=True)
                executable.write_bytes(b"MZ\x00\x00synthetic")

            def launchable(executable):
                return executable == plugin_executable.resolve()

            with patch.dict(
                "os.environ",
                {"USERPROFILE": str(root / "profile")},
                clear=True,
            ), patch(
                "jarvis.model_client._resolved_winget_link", return_value=public_alias
            ), patch(
                "jarvis.model_client.trusted_path_executable", return_value=None
            ), patch(
                "jarvis.model_client._windows_cli_publisher_matches", return_value=True
            ), patch(
                "jarvis.model_client._codex_cli_launchable", side_effect=launchable
            ):
                self.assertEqual(
                    resolve_codex_cli_executable(), plugin_executable.resolve()
                )

    def test_factory_isolates_codex_from_the_jarvis_repository(self):
        config = SimpleNamespace(
            root=Path("C:/sensitive/jarvis-repository"),
            data_dir=Path("C:/sensitive/jarvis-repository/data"),
            cloud_enabled=True,
            cloud_generation_timeout=600.0,
            cloud_max_output_tokens=8192,
            cloud_max_response_bytes=8 * 1024 * 1024,
            cloud_max_retries=0,
            cloud_retry_backoff=0.5,
            claude_cli_enabled=False,
            codex_cli_enabled=True,
            fast_model="codex-cli:gpt-5.5",
            reasoning_model="codex-cli:gpt-5.5",
            coding_model="codex-cli:gpt-5.5",
            deep_model="codex-cli:gpt-5.5",
            model="auto",
            learning_model=None,
            ollama_enabled=False,
        )
        with patch.dict("os.environ", {}, clear=True), patch(
            "jarvis.model_client.resolve_codex_cli_executable",
            return_value=Path("C:/trusted/codex.exe"),
        ):
            client = build_model_client(config)

        self.assertIsNotNone(client.codex_cli)
        self.addCleanup(client.codex_cli._working_directory_owner.cleanup)
        cli_directory = Path(client.codex_cli.working_directory).resolve()
        self.assertTrue(cli_directory.is_dir())
        self.assertNotEqual(cli_directory, config.root.resolve())
        self.assertNotIn(str(config.root.resolve()).casefold(), str(cli_directory).casefold())

    def test_codex_clients_isolate_sqlite_state_while_sharing_attested_login_home(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            first = CodexCLIClient(
                "codex.exe", working_directory=directory, codex_home=data
            )
            second = CodexCLIClient(
                "codex.exe", working_directory=directory, codex_home=data
            )
            try:
                first_environment = first._subprocess_environment()
                second_environment = second._subprocess_environment()

                self.assertEqual(
                    first_environment["CODEX_HOME"],
                    second_environment["CODEX_HOME"],
                )
                self.assertNotEqual(
                    first_environment["CODEX_SQLITE_HOME"],
                    second_environment["CODEX_SQLITE_HOME"],
                )
                self.assertTrue(Path(first_environment["CODEX_SQLITE_HOME"]).is_dir())
                self.assertTrue(Path(second_environment["CODEX_SQLITE_HOME"]).is_dir())
            finally:
                first.close()
                second.close()

    def test_plain_conversation_is_ephemeral_toolless_and_secret_scrubbed(self):
        calls = []
        interface_paths = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            schema_path = Path(args[args.index("--output-schema") + 1])
            output_path = Path(args[args.index("--output-last-message") + 1])
            interface_paths.extend((schema_path, output_path))
            schema_value = json.loads(schema_path.read_text(encoding="utf-8"))
            self.assertEqual(schema_value["properties"]["tool_calls"]["maxItems"], 0)
            return self._write_result(
                args, {"content": "Natural answer.", "tool_calls": []}
            )

        client = CodexCLIClient("codex.exe", working_directory=".", runner=runner)
        with patch.dict(
            "os.environ",
            {
                "PATH": "C:/trusted",
                "USERPROFILE": "C:/Users/test",
                "OPENAI_API_KEY": "must-not-cross",
                "CODEX_API_KEY": "must-not-cross",
                "ANTHROPIC_API_KEY": "must-not-cross",
                "JARVIS_SECRET": "must-not-cross",
                "SSH_AUTH_SOCK": "must-not-cross",
            },
            clear=True,
        ):
            response = client.chat(history(), [], "default")

        args, options = calls[0]
        self.assertEqual(
            args[:5],
            ["codex.exe", "--strict-config", "--ask-for-approval", "never", "exec"],
        )
        for flag in (
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--json",
        ):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertEqual(args[-1], "-")
        self.assertNotIn("--model", args)
        self.assertNotIn("--yolo", args)
        self.assertNotIn("--full-auto", args)
        overrides = [
            args[index + 1]
            for index, value in enumerate(args[:-1])
            if value == "--config"
        ]
        for expected in (
            'forced_login_method="chatgpt"',
            'approval_policy="never"',
            "allow_login_shell=false",
            "features.shell_tool=false",
            "features.unified_exec=false",
            "agents.enabled=false",
            "apps._default.enabled=false",
            "features.apps=false",
            "features.auth_elicitation=false",
            "features.browser_use=false",
            "features.browser_use_external=false",
            "features.browser_use_full_cdp_access=false",
            "features.computer_use=false",
            "features.guardian_approval=false",
            "features.image_generation=false",
            "features.in_app_browser=false",
            "features.plugin_sharing=false",
            "features.plugins=false",
            "features.skill_search=false",
            "features.tool_call_mcp_elicitation=false",
            "features.tool_suggest=false",
            "features.view_image=false",
            "features.workspace_dependencies=false",
            'web_search="disabled"',
            "tools.web_search=false",
        ):
            self.assertIn(expected, overrides)
        # These names look plausible but are not accepted by Codex 0.148.
        # Strict configuration must never be weakened to accommodate them.
        self.assertNotIn("apps._default.default_tools_enabled=false", overrides)
        self.assertNotIn("tools.view_image=false", overrides)
        for secret_name in (
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "ANTHROPIC_API_KEY",
            "JARVIS_SECRET",
            "SSH_AUTH_SOCK",
        ):
            self.assertNotIn(secret_name, options["env"])
        self.assertEqual(options["encoding"], "utf-8")
        self.assertEqual(options["errors"], "strict")
        self.assertIn("<jarvis_conversation_json>", options["input"])
        self.assertEqual(response["content"], "Natural answer.")
        self.assertEqual(response.metrics.prompt_tokens, 17)
        self.assertEqual(response.metrics.completion_tokens, 5)
        self.assertTrue(all(not path.exists() for path in interface_paths))

    def test_image_attachment_uses_isolated_cli_file_and_never_prompt_base64(self):
        observed_paths = []
        image_bytes = b"\x89PNG\r\n\x1a\nsynthetic-vision-pixels"
        image_part = ImageAttachment("image/png", image_bytes).content_part()

        def runner(args, **kwargs):
            image_path = Path(args[args.index("--image") + 1])
            observed_paths.append(image_path)
            self.assertTrue(image_path.is_file())
            self.assertEqual(image_path.read_bytes(), image_bytes)
            self.assertNotIn(image_part["data"], kwargs["input"])
            self.assertIn("Image attachment 1", kwargs["input"])
            return self._write_result(
                args, {"content": "The image has a red region.", "tool_calls": []}
            )

        with tempfile.TemporaryDirectory() as directory:
            client = CodexCLIClient(
                "codex.exe", working_directory=directory, runner=runner
            )
            response = client.chat(
                [{"role": "user", "content": [
                    {"type": "text", "text": "Describe it"}, image_part,
                ]}],
                [],
                "auto",
            )

        self.assertEqual(response["content"], "The image has a red region.")
        self.assertEqual(len(observed_paths), 1)
        self.assertFalse(observed_paths[0].exists())

    def test_auto_and_default_models_omit_explicit_model_override(self):
        commands = []

        def runner(args, **kwargs):
            del kwargs
            commands.append(args)
            return self._write_result(args, {"content": "ok", "tool_calls": []})

        client = CodexCLIClient("codex.exe", working_directory=".", runner=runner)
        for model in ("auto", "default"):
            with self.subTest(model=model):
                client.chat(history(), [], model)

        self.assertEqual(len(commands), 2)
        self.assertTrue(all("--model" not in args for args in commands))

    def test_tool_free_codex_dialogue_disables_reasoning(self):
        commands = []

        def runner(args, **kwargs):
            del kwargs
            commands.append(args)
            return self._write_result(args, {"content": "ok", "tool_calls": []})

        client = CodexCLIClient("codex.exe", working_directory=".", runner=runner)
        client.chat(history(), [], "auto", think=False)

        overrides = [
            commands[0][index + 1]
            for index, value in enumerate(commands[0][:-1])
            if value == "--config"
        ]
        self.assertIn('model_reasoning_effort="none"', overrides)

    def test_structured_jarvis_tool_call_round_trips_without_codex_tools(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            schema_path = Path(args[args.index("--output-schema") + 1])
            output_schema = json.loads(schema_path.read_text(encoding="utf-8"))
            name_schema = output_schema["properties"]["tool_calls"]["items"][
                "properties"
            ]["name"]
            self.assertEqual(name_schema["enum"], ["lookup"])
            self.assertEqual(
                output_schema["properties"]["tool_calls"]["items"]["properties"][
                    "arguments"
                ]["type"],
                "string",
            )
            return self._write_result(
                args,
                {
                    "content": "I need the approved lookup.",
                    "tool_calls": [
                        {
                            "name": "lookup",
                            "arguments": json.dumps({"query": "beta"}),
                        }
                    ],
                },
            )

        client = CodexCLIClient("codex.exe", working_directory=".", runner=runner)
        response = client.chat(history(), [schema()], "gpt-5.5", think="high")

        args, _options = calls[0]
        self.assertEqual(args[args.index("--model") + 1], "gpt-5.5")
        overrides = [
            args[index + 1]
            for index, value in enumerate(args[:-1])
            if value == "--config"
        ]
        self.assertIn('model_reasoning_effort="high"', overrides)
        self.assertEqual(response["content"], "I need the approved lookup.")
        self.assertEqual(response["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(
            response["tool_calls"][0]["function"]["arguments"], {"query": "beta"}
        )

    def test_requested_response_schema_is_written_into_codex_output_schema(self):
        response_schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }

        def runner(args, **kwargs):
            del kwargs
            schema_path = Path(args[args.index("--output-schema") + 1])
            output_schema = json.loads(schema_path.read_text(encoding="utf-8"))
            self.assertEqual(
                output_schema["properties"]["content"], response_schema
            )
            return self._write_result(
                args, {"content": {"answer": "yes"}, "tool_calls": []}
            )

        client = CodexCLIClient("codex.exe", working_directory=".", runner=runner)
        response = client.chat(
            history(), [], "default", response_format=response_schema
        )

        self.assertEqual(response["content"], '{"answer":"yes"}')

    def test_structured_response_allows_empty_content_for_tool_call_branch(self):
        response_schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }

        def runner(args, **kwargs):
            del kwargs
            schema_path = Path(args[args.index("--output-schema") + 1])
            output_schema = json.loads(schema_path.read_text(encoding="utf-8"))
            self.assertEqual(
                output_schema["properties"]["content"],
                {
                    "anyOf": [
                        response_schema,
                        {"type": "string", "maxLength": 0},
                    ]
                },
            )
            return self._write_result(
                args,
                {
                    "content": "",
                    "tool_calls": [
                        {"name": "lookup", "arguments": '{"query":"beta"}'}
                    ],
                },
            )

        client = CodexCLIClient("codex.exe", working_directory=".", runner=runner)
        response = client.chat(
            history(), [schema()], "default", response_format=response_schema
        )

        self.assertEqual(response["content"], "")
        self.assertEqual(response["tool_calls"][0]["function"]["name"], "lookup")

    def test_cancellation_kills_codex_subprocess_promptly(self):
        cancel = threading.Event()
        client = CodexCLIClient(
            sys.executable,
            working_directory=".",
            generation_timeout=10,
        )
        timer = threading.Timer(0.15, cancel.set)
        started = time.monotonic()
        timer.start()
        try:
            with self.assertRaisesRegex(ModelProviderError, "cancelled"):
                client._run_cli(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    prompt="",
                    timeout=10,
                    flags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    cancellation_guard=cancel.is_set,
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 1.5)

    def test_app_server_protocol_assembles_fragmented_agent_message(self):
        deltas = []
        transport = _CodexAppServerTransport(
            "codex.exe",
            working_directory=".",
            environment={},
            config_overrides=(),
            skill_override="",
            generation_timeout=10,
            max_response_bytes=1024,
        )
        state = _CodexAppServerTurn("thr_test", deltas.append, 1024)
        transport._turns["thr_test"] = state
        transport._bind_turn(state, "turn_test")

        transport._handle_message({
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thr_test",
                "turnId": "turn_test",
                "itemId": "item_test",
                "delta": "Hello ",
            },
        })
        transport._handle_message({
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thr_test",
                "turnId": "turn_test",
                "itemId": "item_test",
                "delta": "world",
            },
        })
        transport._handle_message({
            "method": "item/completed",
            "params": {
                "threadId": "thr_test",
                "turnId": "turn_test",
                "completedAtMs": 1,
                "item": {
                    "type": "agentMessage",
                    "id": "item_test",
                    "text": "Hello world",
                },
            },
        })
        transport._handle_message({
            "method": "turn/completed",
            "params": {
                "threadId": "thr_test",
                "turn": {"id": "turn_test", "status": "completed", "items": []},
            },
        })

        self.assertEqual(deltas, ["Hello ", "world"])
        self.assertEqual("".join(state.fragments), "Hello world")
        self.assertEqual(state.final_text, "Hello world")
        self.assertTrue(state.done.is_set())
        self.assertIsNone(state.error)

    def test_app_server_ignores_all_same_turn_events_after_completion(self):
        deltas: list[str] = []
        transport = _CodexAppServerTransport(
            "codex.exe",
            working_directory=".",
            environment={},
            config_overrides=(),
            skill_override="",
            generation_timeout=10,
            max_response_bytes=1024,
        )
        state = _CodexAppServerTurn("thr_test", deltas.append, 1024)
        transport._turns["thr_test"] = state
        transport._bind_turn(state, "turn_test")

        def notify(method, **params):
            transport._handle_message({
                "method": method,
                "params": {"threadId": "thr_test", **params},
            })

        notify(
            "item/completed",
            turnId="turn_test",
            item={"type": "agentMessage", "id": "item_test", "text": "ORIGINAL"},
        )
        notify(
            "turn/completed",
            turn={"id": "turn_test", "status": "completed", "items": []},
        )
        original_completion = state.completed

        notify(
            "item/completed",
            turnId="turn_test",
            item={"type": "agentMessage", "id": "item_test", "text": "MUTATED"},
        )
        notify(
            "item/agentMessage/delta",
            turnId="turn_test",
            itemId="item_test",
            delta="LATE",
        )
        notify("error", turnId="turn_test", error={"message": "late error"})

        self.assertEqual(state.final_text, "ORIGINAL")
        self.assertEqual(state.fragments, [])
        self.assertEqual(deltas, [])
        self.assertIs(state.completed, original_completion)
        self.assertEqual(state.terminal_state, "completed")
        self.assertIsNone(state.error)

    def test_app_server_prewarm_initializes_without_starting_a_turn(self):
        transport = _CodexAppServerTransport(
            "codex.exe",
            working_directory=".",
            environment={},
            config_overrides=(),
            skill_override="",
            generation_timeout=10,
            max_response_bytes=1024,
        )
        deadlines = []
        with patch.object(
            transport,
            "_ensure_started",
            side_effect=lambda deadline: deadlines.append(deadline),
        ), patch.object(transport, "_request") as request:
            transport.prewarm()

        self.assertEqual(len(deadlines), 1)
        self.assertGreater(deadlines[0], time.monotonic())
        request.assert_not_called()

    def test_app_server_protocol_rejects_codex_tool_activity(self):
        transport = _CodexAppServerTransport(
            "codex.exe",
            working_directory=".",
            environment={},
            config_overrides=(),
            skill_override="",
            generation_timeout=10,
            max_response_bytes=1024,
        )
        state = _CodexAppServerTurn("thr_test", lambda _text: None, 1024)
        transport._turns["thr_test"] = state
        transport._bind_turn(state, "turn_test")

        transport._handle_message({
            "method": "item/started",
            "params": {
                "threadId": "thr_test",
                "turnId": "turn_test",
                "item": {"type": "commandExecution", "id": "cmd_test"},
            },
        })

        self.assertTrue(state.done.is_set())
        self.assertIsNotNone(state.error)
        self.assertIn("unauthorized", str(state.error))

    def test_app_server_reuses_exact_conversation_and_sends_only_new_turn(self):
        transport = _CodexAppServerTransport(
            "codex.exe",
            working_directory=".",
            environment={},
            config_overrides=(),
            skill_override="",
            generation_timeout=10,
            max_response_bytes=1024,
        )
        requests = []
        answers = iter(("First answer", "Second answer"))

        def request(method, params, **_kwargs):
            requests.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "thr_reused"}}
            if method == "turn/start":
                answer = next(answers)
                turn_id = f"turn_{len([item for item in requests if item[0] == 'turn/start'])}"
                transport._handle_message({
                    "method": "item/completed",
                    "params": {
                        "threadId": params["threadId"],
                        "turnId": turn_id,
                        "item": {
                            "type": "agentMessage",
                            "id": f"item_{turn_id}",
                            "text": answer,
                        },
                    },
                })
                transport._handle_message({
                    "method": "turn/completed",
                    "params": {
                        "threadId": params["threadId"],
                        "turn": {"id": turn_id, "status": "completed", "items": []},
                    },
                })
                return {"turn": {"id": turn_id}}
            raise AssertionError(f"unexpected request: {method}")

        first_messages = [
            {"role": "system", "content": "Stable contract"},
            {"role": "user", "content": "First private prompt"},
        ]
        second_messages = [
            *first_messages,
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Follow up naturally"},
        ]
        with model_conversation_scope("test:conversation:1"), patch.object(
            transport, "_ensure_started"
        ), patch.object(transport, "_request", side_effect=request):
            first = transport.chat_stream(
                first_messages, "auto", lambda _text: None,
                think=False, cancellation_guard=None,
            )
            second = transport.chat_stream(
                second_messages, "auto", lambda _text: None,
                think=False, cancellation_guard=None,
            )

        self.assertEqual(first["content"], "First answer")
        self.assertEqual(second["content"], "Second answer")
        self.assertEqual(
            [method for method, _params in requests].count("thread/start"), 1
        )
        turn_requests = [params for method, params in requests if method == "turn/start"]
        self.assertEqual([item["threadId"] for item in turn_requests], [
            "thr_reused", "thr_reused"
        ])
        second_input = turn_requests[1]["input"][0]["text"]
        self.assertIn("Follow up naturally", second_input)
        self.assertNotIn("First private prompt", second_input)
        self.assertNotIn("First answer", second_input)

    def test_app_server_discards_delayed_prior_turn_before_new_turn_is_bound(self):
        transport = _CodexAppServerTransport(
            "codex.exe",
            working_directory=".",
            environment={},
            config_overrides=(),
            skill_override="",
            generation_timeout=10,
            max_response_bytes=1024,
        )
        turn_starts = 0
        first_deltas: list[str] = []
        second_deltas: list[str] = []

        def complete_turn(thread_id, turn_id, item_id, answer, *, delta=None):
            if delta is not None:
                transport._handle_message({
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "itemId": item_id,
                        "delta": delta,
                    },
                })
            transport._handle_message({
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {
                        "type": "agentMessage",
                        "id": item_id,
                        "text": answer,
                    },
                },
            })
            transport._handle_message({
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "status": "completed", "items": []},
                },
            })

        def request(method, params, **_kwargs):
            nonlocal turn_starts
            if method == "thread/start":
                return {"thread": {"id": "thr_correlated"}}
            if method != "turn/start":
                raise AssertionError(f"unexpected request: {method}")
            turn_starts += 1
            state = transport._turns[params["threadId"]]
            self.assertIsNone(state.turn_id)
            if turn_starts == 1:
                complete_turn(
                    params["threadId"],
                    "turn_old",
                    "item_old",
                    "First answer",
                )
                return {"turn": {"id": "turn_old"}}

            # The app server may flush delayed events from the preceding turn
            # after Jarvis installs the new state but before turn/start returns.
            complete_turn(
                params["threadId"],
                "turn_old",
                "item_old_late",
                "STALE ANSWER",
                delta="STALE ",
            )
            complete_turn(
                params["threadId"],
                "turn_current",
                "item_current",
                "Current answer",
                delta="Current ",
            )
            return {"turn": {"id": "turn_current"}}

        first_messages = [
            {"role": "system", "content": "Stable contract"},
            {"role": "user", "content": "First prompt"},
        ]
        second_messages = [
            *first_messages,
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Different current prompt"},
        ]
        with model_conversation_scope("test:conversation:correlated"), patch.object(
            transport, "_ensure_started"
        ), patch.object(transport, "_request", side_effect=request):
            first = transport.chat_stream(
                first_messages,
                "auto",
                first_deltas.append,
                think=False,
                cancellation_guard=None,
            )
            second = transport.chat_stream(
                second_messages,
                "auto",
                second_deltas.append,
                think=False,
                cancellation_guard=None,
            )

        self.assertEqual(first["content"], "First answer")
        self.assertEqual(second["content"], "Current answer")
        self.assertEqual(first_deltas, [])
        self.assertEqual(second_deltas, ["Current "])

    def test_dialogue_memory_enrichment_does_not_break_exact_history_matching(self):
        transport = _CodexAppServerTransport(
            "codex.exe",
            working_directory=".",
            environment={},
            config_overrides=(),
            skill_override="",
            generation_timeout=10,
            max_response_bytes=1024,
        )
        raw = {"role": "user", "content": "What should I train today?"}
        enriched = {
            "role": "user",
            "content": (
                "What should I train today?\n\n"
                "<jarvis_runtime_dialogue_context>\n"
                "<untrusted_memory_records>[{\"content\":\"prefers short workouts\"}]"
                "</untrusted_memory_records>\n"
                "</jarvis_runtime_dialogue_context>"
            ),
        }

        self.assertEqual(
            transport._message_fingerprints([raw]),
            transport._message_fingerprints([enriched]),
        )

    def test_app_server_continuation_cache_fails_closed_on_context_change(self):
        transport = _CodexAppServerTransport(
            "codex.exe",
            working_directory=".",
            environment={},
            config_overrides=(),
            skill_override="",
            generation_timeout=10,
            max_response_bytes=1024,
        )
        baseline = [
            {"role": "system", "content": "Stable contract"},
            {"role": "user", "content": "Hello"},
        ]
        fingerprints = transport._message_fingerprints(baseline)
        transport._remember_conversation_thread(
            _CodexAppServerConversation(
                "thr_old", "test:conversation:2", "auto", (), time.monotonic(), busy=True
            ),
            fingerprints,
            "Hello back",
        )
        changed = [
            {"role": "system", "content": "Changed contract"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hello back"},
            {"role": "user", "content": "Continue"},
        ]

        with model_conversation_scope("test:conversation:2"):
            conversation, _fingerprints = transport._claim_conversation_thread(
                changed, "auto"
            )

        self.assertIsNone(conversation)
        transport._stop_locked()
        self.assertEqual(transport._conversations, {})

    def test_app_server_continuation_is_isolated_by_jarvis_conversation(self):
        transport = _CodexAppServerTransport(
            "codex.exe",
            working_directory=".",
            environment={},
            config_overrides=(),
            skill_override="",
            generation_timeout=10,
            max_response_bytes=1024,
        )
        initial = [{"role": "user", "content": "Same words"}]
        with model_conversation_scope("presence:1:10"):
            fingerprints = transport._message_fingerprints(initial)
            transport._remember_conversation_thread(
                _CodexAppServerConversation(
                    "thr_private", "presence:1:10", "auto", (),
                    time.monotonic(), busy=True,
                ),
                fingerprints,
                "Same answer",
            )
        continued = [
            *initial,
            {"role": "assistant", "content": "Same answer"},
            {"role": "user", "content": "Same follow-up"},
        ]

        with model_conversation_scope("presence:1:11"):
            foreign, _ = transport._claim_conversation_thread(continued, "auto")
        with model_conversation_scope("presence:1:10"):
            own, _ = transport._claim_conversation_thread(continued, "auto")

        self.assertIsNone(foreign)
        self.assertIsNotNone(own)
        self.assertEqual(own.thread_id, "thr_private")

    def test_app_server_timeout_interrupts_and_discards_uncertain_thread(self):
        transport = _CodexAppServerTransport(
            "codex.exe",
            working_directory=".",
            environment={},
            config_overrides=(),
            skill_override="",
            generation_timeout=0.02,
            max_response_bytes=1024,
        )
        methods = []

        def request(method, _params, **_kwargs):
            methods.append(method)
            if method == "thread/start":
                return {"thread": {"id": "thr_timeout"}}
            if method == "turn/start":
                return {"turn": {"id": "turn_timeout"}}
            if method == "turn/interrupt":
                return {}
            raise AssertionError(f"unexpected request: {method}")

        with model_conversation_scope("test:timeout"), patch.object(
            transport, "_ensure_started"
        ), patch.object(transport, "_request", side_effect=request):
            with self.assertRaisesRegex(ModelProviderError, "timed out"):
                transport.chat_stream(
                    [{"role": "user", "content": "Wait forever"}],
                    "auto",
                    lambda _text: None,
                    think=False,
                    cancellation_guard=None,
                )

        self.assertEqual(methods[-1], "turn/interrupt")
        self.assertEqual(transport._conversations, {})

    def test_app_server_stream_failure_falls_back_to_complete_exec_answer(self):
        class BrokenAppServer:
            _process = None

            def __init__(self):
                self.closed = False

            def chat_stream(self, messages, model, on_delta, **kwargs):
                del messages, model, kwargs
                on_delta("Partial")
                raise ModelProviderError(
                    "codex-cli", "stream broke", provider_unavailable=True
                )

            def close(self):
                self.closed = True

        client = CodexCLIClient("codex.exe", working_directory=".")
        broken = BrokenAppServer()
        client._app_server = broken
        complete = ChatResponse(
            {"role": "assistant", "content": "Complete answer"},
            {"done": True, "model": "auto"},
        )
        deltas = []
        with patch.object(client, "chat", return_value=complete) as fallback:
            response = client.chat_stream(
                history(), [], "auto", deltas.append, think=False
            )

        self.assertEqual(deltas, ["Partial"])
        self.assertEqual(response["content"], "Complete answer")
        self.assertTrue(broken.closed)
        fallback.assert_called_once()

    def test_authentication_failure_is_stable_and_not_retried(self):
        calls = 0

        def runner(args, **kwargs):
            nonlocal calls
            del kwargs
            calls += 1
            return subprocess.CompletedProcess(
                args, 1, "", "Authentication required. Please run codex login."
            )

        client = CodexCLIClient(
            "codex.exe", working_directory=".", max_retries=2, runner=runner
        )
        with self.assertRaises(ModelProviderError) as caught:
            client.chat([{"role": "user", "content": "hello"}], [], "default")
        self.assertEqual(caught.exception.status_code, 401)
        self.assertTrue(caught.exception.provider_unavailable)
        self.assertEqual(calls, 1)
        self.assertNotIn("Authentication required", str(caught.exception))
        self.assertIn("codex login", user_model_error_message(caught.exception))

    def test_login_probe_distinguishes_chatgpt_without_forwarding_secrets(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(
                args, 0, "Logged in using ChatGPT\n", ""
            )

        client = CodexCLIClient("codex.exe", working_directory=".", runner=runner)
        with patch.dict(
            "os.environ",
            {
                "PATH": "C:/trusted",
                "USERPROFILE": "C:/Users/test",
                "OPENAI_API_KEY": "must-not-cross",
                "CODEX_API_KEY": "must-not-cross",
            },
            clear=True,
        ):
            method = client.probe_authentication()

        self.assertEqual(method, "chatgpt")
        self.assertEqual(calls[0][0][-2:], ["login", "status"])
        self.assertIn('cli_auth_credentials_store="keyring"', calls[0][0])
        self.assertIn("CODEX_HOME", calls[0][1]["env"])
        self.assertNotIn("OPENAI_API_KEY", calls[0][1]["env"])
        self.assertNotIn("CODEX_API_KEY", calls[0][1]["env"])

    def test_api_key_login_is_rejected_before_model_invocation(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, "Logged in using API key", "")

        client = CodexCLIClient("codex.exe", working_directory=".", runner=runner)
        self.assertEqual(client.probe_authentication(), "api-key")
        with self.assertRaises(ModelProviderError) as caught:
            client.chat(history(), [], "default")

        self.assertEqual(caught.exception.status_code, 401)
        self.assertTrue(caught.exception.provider_unavailable)
        self.assertEqual(len(calls), 1)

    def test_login_probe_does_not_misclassify_negative_chatgpt_status(self):
        def runner(args, **kwargs):
            del kwargs
            return subprocess.CompletedProcess(
                args, 0, "Not logged in using ChatGPT", ""
            )

        client = CodexCLIClient("codex.exe", working_directory=".", runner=runner)
        self.assertEqual(client.probe_authentication(), "signed-out")

    def test_login_probe_rejects_oversized_status_output(self):
        def runner(args, **kwargs):
            del kwargs
            return subprocess.CompletedProcess(
                args, 0, "Logged in using ChatGPT\n" + ("x" * 2048), ""
            )

        client = CodexCLIClient(
            "codex.exe",
            working_directory=".",
            runner=runner,
            max_response_bytes=1024,
        )
        self.assertEqual(client.probe_authentication(), "unknown")

    def test_unknown_security_override_fails_closed_without_retry(self):
        calls = 0

        def runner(args, **kwargs):
            nonlocal calls
            del kwargs
            calls += 1
            return subprocess.CompletedProcess(
                args,
                1,
                "",
                "Error loading config.toml: unknown configuration field `unsafe`",
            )

        client = CodexCLIClient(
            "codex.exe", working_directory=".", max_retries=2, runner=runner
        )
        with self.assertRaises(ModelProviderError) as caught:
            client.chat(history(), [], "default")

        self.assertEqual(caught.exception.status_code, 400)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(calls, 1)

    def test_malformed_final_output_fails_closed(self):
        def runner(args, **kwargs):
            del kwargs
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("{not-json", encoding="utf-8")
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({"type": "turn.completed", "usage": {}}),
                "",
            )

        client = CodexCLIClient("codex.exe", working_directory=".", runner=runner)
        with self.assertRaisesRegex(ModelProviderError, "malformed JSON"):
            client.chat([{"role": "user", "content": "hello"}], [], "default")

    def test_event_stream_and_final_file_are_independently_size_bounded(self):
        def oversized_events(args, **kwargs):
            del kwargs
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text(
                '{"content":"ok","tool_calls":[]}', encoding="utf-8"
            )
            return subprocess.CompletedProcess(args, 0, "x" * 1025, "")

        client = CodexCLIClient(
            "codex.exe",
            working_directory=".",
            runner=oversized_events,
            max_response_bytes=1024,
        )
        with self.assertRaisesRegex(ModelProviderError, "size limit"):
            client.chat(history(), [], "default")

        def oversized_final(args, **kwargs):
            del kwargs
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("x" * 1025, encoding="utf-8")
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps({"type": "turn.completed", "usage": {}}),
                "",
            )

        client = CodexCLIClient(
            "codex.exe",
            working_directory=".",
            runner=oversized_final,
            max_response_bytes=1024,
        )
        with self.assertRaisesRegex(ModelProviderError, "size limit"):
            client.chat(history(), [], "default")

    def test_any_codex_agent_tool_event_fails_closed_even_on_zero_exit(self):
        for item_type in (
            "command_execution",
            "file_change",
            "mcp_tool_call",
            "web_search",
            "computer_use",
        ):
            with self.subTest(item_type=item_type):
                def runner(args, item_type=item_type, **kwargs):
                    del kwargs
                    return self._write_result(
                        args,
                        {"content": "claimed success", "tool_calls": []},
                        stdout_events=[
                            {"type": "turn.started"},
                            {
                                "type": "item.completed",
                                "item": {"id": "item_1", "type": item_type},
                            },
                        ],
                    )

                client = CodexCLIClient(
                    "codex.exe", working_directory=".", runner=runner
                )
                with self.assertRaisesRegex(
                    ModelProviderError, "unauthorized agent-tool"
                ):
                    client.chat(
                        [{"role": "user", "content": "hello"}], [], "default"
                    )


class OpenAIProviderTests(unittest.TestCase):
    def test_image_part_maps_to_responses_api_without_changing_string_fast_path(self):
        instructions, plain = _openai_input([
            {"role": "system", "content": "system"},
            {"role": "user", "content": "plain text"},
        ])
        self.assertEqual(instructions, "system")
        self.assertEqual(plain, [{"role": "user", "content": "plain text"}])

        _instructions, multimodal = _openai_input([{
            "role": "user",
            "content": [
                {"type": "text", "text": "inspect"},
                {"type": "image", "mime": "image/png", "data": "YWJj"},
            ],
        }])
        self.assertEqual(multimodal[0]["content"], [
            {"type": "input_text", "text": "inspect"},
            {"type": "input_image", "image_url": "data:image/png;base64,YWJj"},
        ])

    def test_streaming_responses_api_assembles_fragmented_deltas(self):
        completed = {
            "status": "completed",
            "model": "gpt-5.6",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "Hello world"}],
            }],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        }
        opener = SequenceOpen(FakeSSEResponse([
            {"type": "response.output_text.delta", "delta": "Hel"},
            {"type": "response.output_text.delta", "delta": "lo "},
            {"type": "response.output_text.delta", "delta": "world"},
            {"type": "response.completed", "response": completed},
        ]))
        client = OpenAIClient(
            "sk-test-openai-not-real", open_url=opener, max_retries=0
        )
        deltas = []

        response = client.chat_stream(
            [{"role": "user", "content": "hello"}],
            [],
            "gpt-5.6",
            deltas.append,
        )

        self.assertEqual("".join(deltas), "Hello world")
        self.assertEqual(response["content"], "Hello world")
        self.assertEqual(response.metrics.prompt_tokens, 4)
        self.assertEqual(response.metrics.completion_tokens, 2)
        self.assertTrue(json.loads(opener.requests[0].data)["stream"])

    def test_midstream_failure_falls_back_to_authoritative_nonstreaming_answer(self):
        stream = FakeSSEResponse(
            [
                {"type": "response.output_text.delta", "delta": "Partial"},
                {"type": "response.output_text.delta", "delta": " lost"},
            ],
            fail_after=2,
        )
        final = FakeResponse({
            "status": "completed",
            "model": "gpt-5.6",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "Complete answer"}],
            }],
            "usage": {"input_tokens": 3, "output_tokens": 2},
        })
        opener = SequenceOpen(stream, final)
        client = OpenAIClient(
            "sk-test-openai-not-real", open_url=opener, max_retries=0
        )
        deltas = []

        response = client.chat_stream(
            [{"role": "user", "content": "hello"}], [], "gpt-5.6", deltas.append
        )

        self.assertEqual(response["content"], "Complete answer")
        self.assertEqual(deltas, ["Partial"])
        self.assertTrue(json.loads(opener.requests[0].data)["stream"])
        self.assertNotIn("stream", json.loads(opener.requests[1].data))

    def test_stream_cancellation_never_reissues_a_nonstreaming_request(self):
        cancel = threading.Event()
        opener = SequenceOpen(FakeSSEResponse([
            {"type": "response.output_text.delta", "delta": "first"},
            {"type": "response.output_text.delta", "delta": "second"},
        ]))
        client = OpenAIClient(
            "sk-test-openai-not-real", open_url=opener, max_retries=0
        )

        def stop_after_first(_delta):
            cancel.set()

        with self.assertRaisesRegex(ModelProviderError, "cancelled"):
            client.chat_stream(
                [{"role": "user", "content": "hello"}],
                [],
                "gpt-5.6",
                stop_after_first,
                cancellation_guard=cancel.is_set,
            )

        self.assertEqual(len(opener.requests), 1)

    def test_pre_cancelled_request_never_reaches_provider(self):
        opener = SequenceOpen()
        client = OpenAIClient(
            "sk-test-openai-not-real",
            open_url=opener,
            max_retries=0,
        )

        with self.assertRaisesRegex(ModelProviderError, "cancelled"):
            client.chat(
                [{"role": "user", "content": "hello"}],
                [],
                "gpt-5.6",
                cancellation_guard=lambda: True,
            )

        self.assertEqual(opener.requests, [])

    def test_cancellation_watcher_closes_active_connection(self):
        cancel = threading.Event()
        closed = threading.Event()

        class Connection:
            def close(self):
                closed.set()

        state = _HTTPConnectionCancellation(cancel.is_set)
        state.register(Connection())
        state.start()
        cancel.set()
        try:
            self.assertTrue(closed.wait(1.0))
            self.assertTrue(state.cancelled.is_set())
        finally:
            state.close()

    def test_responses_api_maps_tools_history_reasoning_and_structured_output(self):
        opener = SequenceOpen(FakeResponse({
            "id": "resp_test",
            "status": "completed",
            "model": "gpt-5.6",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Calling again"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_server",
                    "name": "lookup",
                    "arguments": '{"query":"beta"}',
                },
            ],
            "usage": {"input_tokens": 31, "output_tokens": 7},
        }))
        client = OpenAIClient(
            "sk-test-openai-not-real",
            safety_identifier="jarvis_test_user",
            open_url=opener,
            generation_timeout=9,
            max_output_tokens=4096,
            max_response_bytes=4096,
            max_retries=0,
        )
        response_schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }

        response = client.chat(
            history(), [schema()], "gpt-5.6", think="high",
            response_format=response_schema, seed=42,
        )

        request = opener.requests[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, OPENAI_RESPONSES_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-test-openai-not-real")
        self.assertNotIn("sk-test-openai-not-real", request.data.decode("utf-8"))
        self.assertEqual(payload["instructions"], "Trusted system contract")
        self.assertEqual(payload["model"], "gpt-5.6")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertFalse(payload["store"])
        self.assertEqual(payload["safety_identifier"], "jarvis_test_user")
        self.assertEqual(payload["tools"][0]["name"], "lookup")
        function_call = next(item for item in payload["input"] if item.get("type") == "function_call")
        function_output = next(
            item for item in payload["input"] if item.get("type") == "function_call_output"
        )
        self.assertEqual(function_call["call_id"], function_output["call_id"])
        self.assertEqual(payload["text"]["format"]["schema"], response_schema)
        self.assertIsInstance(response, ChatResponse)
        self.assertEqual(response["content"], "Calling again")
        self.assertEqual(response["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(response.metrics.prompt_tokens, 31)
        self.assertEqual(response.metrics.completion_tokens, 7)

    def test_http_errors_do_not_echo_key_or_provider_body(self):
        error = urllib.error.HTTPError(
            OPENAI_RESPONSES_URL,
            401,
            "unauthorized",
            {},
            io.BytesIO(b'api_key="sk-test-openai-not-real"'),
        )
        client = OpenAIClient(
            "sk-test-openai-not-real",
            open_url=SequenceOpen(error),
            max_retries=0,
        )
        with self.assertRaises(ModelProviderError) as caught:
            client.chat([{"role": "user", "content": "hello"}], [], "gpt-5.6")
        rendered = str(caught.exception)
        self.assertIn("HTTP 401", rendered)
        self.assertNotIn("sk-test", rendered)
        self.assertNotIn("api_key", rendered)

    def test_incomplete_http_read_is_normalized_and_retried(self):
        opener = SequenceOpen(
            BrokenReadResponse(),
            FakeResponse({
                "status": "completed",
                "model": "gpt-5.6",
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": "recovered"}],
                }],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }),
        )
        client = OpenAIClient(
            "sk-test-openai-not-real",
            open_url=opener,
            max_retries=1,
            retry_backoff=0,
        )

        response = client.chat([{"role": "user", "content": "hello"}], [], "gpt-5.6")

        self.assertEqual(response["content"], "recovered")
        self.assertEqual(len(opener.requests), 2)

    def test_rate_limit_is_not_retried_and_preserves_retry_after(self):
        error = urllib.error.HTTPError(
            OPENAI_RESPONSES_URL,
            429,
            "rate limited",
            {"Retry-After": "120"},
            io.BytesIO(b'{"error":"synthetic"}'),
        )
        opener = SequenceOpen(error)
        client = OpenAIClient(
            "sk-test-openai-not-real",
            open_url=opener,
            max_retries=2,
            retry_backoff=0,
        )

        with self.assertRaises(ModelProviderError) as caught:
            client.chat([{"role": "user", "content": "hello"}], [], "gpt-5.6")

        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertTrue(caught.exception.provider_unavailable)
        self.assertEqual(caught.exception.retry_after_seconds, 120)

    def test_non_reasoning_openai_model_omits_reasoning_parameter(self):
        opener = SequenceOpen(FakeResponse({
            "status": "completed",
            "model": "gpt-4o",
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "done"}],
            }],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }))
        client = OpenAIClient("sk-test-openai-not-real", open_url=opener, max_retries=0)

        client.chat([{"role": "user", "content": "hello"}], [], "gpt-4o", think=False)

        self.assertNotIn("reasoning", json.loads(opener.requests[0].data))


class AnthropicProviderTests(unittest.TestCase):
    def test_image_part_maps_to_anthropic_base64_source(self):
        system, messages = _anthropic_messages([{
            "role": "user",
            "content": [
                {"type": "text", "text": "inspect"},
                {"type": "image", "mime": "image/webp", "data": "YWJj"},
            ],
        }])
        self.assertEqual(system, "")
        self.assertEqual(messages[0]["content"], [
            {"type": "text", "text": "inspect"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/webp",
                    "data": "YWJj",
                },
            },
        ])

    def test_streaming_messages_api_assembles_text_deltas(self):
        opener = SequenceOpen(FakeSSEResponse([
            {
                "type": "message_start",
                "message": {
                    "model": "claude-sonnet-5",
                    "usage": {"input_tokens": 5},
                },
            },
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi "}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "there"}},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 2},
            },
            {"type": "message_stop"},
        ]))
        client = AnthropicClient(
            "sk-ant-test-not-real", open_url=opener, max_retries=0
        )
        deltas = []

        response = client.chat_stream(
            [{"role": "user", "content": "hello"}],
            [],
            "claude-sonnet-5",
            deltas.append,
        )

        self.assertEqual(deltas, ["Hi ", "there"])
        self.assertEqual(response["content"], "Hi there")
        self.assertEqual(response.metrics.prompt_tokens, 5)
        self.assertEqual(response.metrics.completion_tokens, 2)
        self.assertTrue(json.loads(opener.requests[0].data)["stream"])

    def test_messages_api_maps_tools_history_thinking_and_structured_output(self):
        opener = SequenceOpen(FakeResponse({
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "text", "text": "I need one more lookup."},
                {"type": "tool_use", "id": "toolu_server", "name": "lookup", "input": {"query": "beta"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 29, "output_tokens": 11},
        }))
        client = AnthropicClient(
            "sk-ant-test-not-real",
            open_url=opener,
            generation_timeout=9,
            max_output_tokens=4096,
            max_response_bytes=4096,
            max_retries=0,
        )
        response_schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }

        response = client.chat(
            history(), [schema()], "claude-sonnet-5", think=True,
            response_format=response_schema,
        )

        request = opener.requests[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, ANTHROPIC_MESSAGES_URL)
        self.assertEqual(request.get_header("X-api-key"), "sk-ant-test-not-real")
        self.assertEqual(request.get_header("Anthropic-version"), "2023-06-01")
        self.assertNotIn("sk-ant-test-not-real", request.data.decode("utf-8"))
        self.assertEqual(payload["system"], "Trusted system contract")
        self.assertEqual(payload["thinking"], {"type": "adaptive", "display": "omitted"})
        self.assertEqual(payload["output_config"]["effort"], "medium")
        self.assertEqual(payload["output_config"]["format"]["schema"], response_schema)
        self.assertEqual(payload["tools"][0]["input_schema"], schema()["function"]["parameters"])
        tool_use = next(
            block
            for message in payload["messages"]
            for block in message["content"]
            if block["type"] == "tool_use"
        )
        tool_result = next(
            block
            for message in payload["messages"]
            for block in message["content"]
            if block["type"] == "tool_result"
        )
        self.assertEqual(tool_use["id"], tool_result["tool_use_id"])
        self.assertNotIn("private reasoning", response["content"])
        self.assertEqual(response["content"], "I need one more lookup.")
        self.assertEqual(response["tool_calls"][0]["function"]["arguments"], {"query": "beta"})
        self.assertEqual(response.metrics.prompt_tokens, 29)
        self.assertEqual(response.metrics.completion_tokens, 11)

    def test_fast_sonnet_five_request_disables_thinking(self):
        opener = SequenceOpen(FakeResponse({
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "fast"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }))
        client = AnthropicClient("sk-ant-test-not-real", open_url=opener, max_retries=0)

        client.chat([{"role": "user", "content": "hello"}], [], "claude-sonnet-5", think=False)

        payload = json.loads(opener.requests[0].data)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["output_config"]["effort"], "low")
        self.assertNotIn("temperature", payload)


class FakeOllama:
    def __init__(self, models=None, error=None):
        self.available = list(models or [])
        self.error = error
        self.calls = []

    def models(self, refresh=True):
        if self.error is not None:
            raise self.error
        return list(self.available)

    def chat(self, messages, tools, model, **kwargs):
        self.calls.append((messages, tools, model, kwargs))
        return ChatResponse({"role": "assistant", "content": "local"}, {"done": True, "model": model})


class FakeCloud:
    def __init__(self, default_model, answer):
        self.default_model = default_model
        self.answer = answer
        self.calls = []

    def chat(self, messages, tools, model, **kwargs):
        self.calls.append((messages, tools, model, kwargs))
        return ChatResponse({"role": "assistant", "content": self.answer}, {"done": True, "model": model})


class FakeStreamingCloud(FakeCloud):
    def chat_stream(self, messages, tools, model, on_delta, **kwargs):
        self.calls.append((messages, tools, model, kwargs))
        on_delta(self.answer)
        return ChatResponse(
            {"role": "assistant", "content": self.answer},
            {"done": True, "model": model},
        )


class MultiplexerTests(unittest.TestCase):
    def test_codex_prewarm_dispatches_without_generating_content(self):
        class WarmableCodex(FakeCloud):
            def __init__(self):
                super().__init__(DEFAULT_CODEX_CLI_MODEL, "unused")
                self.warmed = []

            def prewarm(self, model):
                self.warmed.append(model)

        codex = WarmableCodex()
        client = ModelClient(None, codex_cli=codex)

        client.prewarm("codex-cli:gpt-5.6-luna")

        self.assertEqual(codex.warmed, ["gpt-5.6-luna"])
        self.assertEqual(codex.calls, [])
        self.assertTrue(client.provider_status["codex_cli_healthy"])

    def test_codex_cli_lists_dispatches_and_reports_health(self):
        codex = FakeCloud(DEFAULT_CODEX_CLI_MODEL, "codex subscription")
        client = ModelClient(
            None,
            codex_cli=codex,
            configured_models=("codex-cli:gpt-5.5",),
        )

        self.assertIn("codex-cli:gpt-5.5", client.models())
        self.assertTrue(client.provider_status["codex_cli_configured"])
        self.assertEqual(client.provider_status["codex_cli_auth_method"], "unknown")
        self.assertIsNone(client.provider_status["codex_cli_healthy"])
        response = client.chat([], [], "codex-cli:gpt-5.5")
        self.assertEqual(response["content"], "codex subscription")
        self.assertEqual(codex.calls[0][2], "gpt-5.5")
        self.assertTrue(client.provider_status["codex_cli_healthy"])

    def test_codex_subscription_status_rejects_api_key_authentication(self):
        codex = FakeCloud(DEFAULT_CODEX_CLI_MODEL, "unused")
        codex.authentication_method = "api-key"
        client = ModelClient(None, codex_cli=codex)

        self.assertEqual(client.provider_status["codex_cli_auth_method"], "api-key")
        self.assertFalse(client.provider_status["codex_cli_healthy"])

    def test_codex_subscription_stream_dispatches_live_deltas(self):
        codex = FakeStreamingCloud(DEFAULT_CODEX_CLI_MODEL, "live text")
        client = ModelClient(None, codex_cli=codex)
        deltas = []

        response = client.chat_stream(
            [{"role": "user", "content": "hello"}],
            [],
            "codex-cli:auto",
            deltas.append,
            think=False,
        )

        self.assertEqual(deltas, ["live text"])
        self.assertEqual(response["content"], "live text")
        self.assertEqual(codex.calls[0][2], "auto")

    def test_provider_wide_failure_short_circuits_same_provider_models(self):
        opener = SequenceOpen(urllib.error.URLError("offline"))
        openai = OpenAIClient(
            "sk-test-openai-not-real",
            open_url=opener,
            max_retries=0,
        )
        client = ModelClient(
            None,
            openai=openai,
            configured_models=("openai:gpt-5.6-luna", "openai:gpt-5.6-sol"),
        )

        with self.assertRaises(ModelProviderError):
            client.chat([], [], "openai:gpt-5.6-luna")
        with self.assertRaisesRegex(ModelProviderError, "temporarily unavailable"):
            client.chat([], [], "openai:gpt-5.6-sol")

        self.assertEqual(len(opener.requests), 1)
        self.assertFalse(client.provider_status["openai_healthy"])

    def test_rate_limit_circuit_blocks_sibling_model_without_second_request(self):
        error = urllib.error.HTTPError(
            OPENAI_RESPONSES_URL,
            429,
            "rate limited",
            {"Retry-After": "90"},
            io.BytesIO(b"{}"),
        )
        opener = SequenceOpen(error)
        client = ModelClient(
            None,
            openai=OpenAIClient(
                "sk-test-openai-not-real", open_url=opener, max_retries=2
            ),
            configured_models=("openai:gpt-5.6-luna", "openai:gpt-5.6-sol"),
        )

        with self.assertRaises(ModelProviderError) as first:
            client.chat([], [], "openai:gpt-5.6-luna")
        with self.assertRaisesRegex(ModelProviderError, "temporarily unavailable"):
            client.chat([], [], "openai:gpt-5.6-sol")

        self.assertEqual(first.exception.status_code, 429)
        self.assertEqual(len(opener.requests), 1)
        self.assertFalse(client.provider_status["openai_healthy"])

    def test_request_shape_error_does_not_open_provider_circuit(self):
        error = urllib.error.HTTPError(
            OPENAI_RESPONSES_URL,
            400,
            "bad request",
            {},
            io.BytesIO(b"{}"),
        )
        opener = SequenceOpen(
            error,
            FakeResponse({
                "status": "completed",
                "model": "gpt-5.6-sol",
                "output": [{
                    "type": "message",
                    "content": [{"type": "output_text", "text": "recovered"}],
                }],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }),
        )
        client = ModelClient(
            None,
            openai=OpenAIClient(
                "sk-test-openai-not-real", open_url=opener, max_retries=0
            ),
        )

        with self.assertRaises(ModelProviderError):
            client.chat([], [], "openai:gpt-5.6-luna")
        response = client.chat([], [], "openai:gpt-5.6-sol")

        self.assertEqual(response["content"], "recovered")
        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(client.provider_status["openai_healthy"])

    def test_build_client_can_disable_cloud_even_when_keys_exist(self):
        config = SimpleNamespace(
            cloud_enabled=False,
            cloud_generation_timeout=600.0,
            cloud_max_output_tokens=8192,
            cloud_max_response_bytes=8 * 1024 * 1024,
            cloud_max_retries=2,
            cloud_retry_backoff=0.5,
            fast_model="qwen3-coder:30b",
            reasoning_model="qwen3-coder:30b",
            coding_model="qwen3-coder:30b",
            model="auto",
            ollama_url="http://127.0.0.1:11434",
            ollama_allow_remote=False,
            ollama_health_timeout=5.0,
            ollama_generation_timeout=600.0,
            ollama_max_output_tokens=1024,
            ollama_max_response_bytes=8 * 1024 * 1024,
            ollama_max_retries=2,
            ollama_retry_backoff=0.25,
            ollama_keep_alive="30m",
            ollama_num_thread=8,
        )
        with patch.dict(
            "os.environ",
            {"OPENAI_API_KEY": "sk-test-not-real", "ANTHROPIC_API_KEY": "sk-ant-test-not-real"},
            clear=True,
        ):
            client = build_model_client(config)

        self.assertIsNone(client.openai)
        self.assertIsNone(client.anthropic)
        self.assertEqual(client.ollama.keep_alive, "30m")
        self.assertEqual(client.ollama.num_thread, 8)

    def test_subscription_mode_does_not_construct_separately_billed_api_clients(self):
        config = SimpleNamespace(
            cloud_enabled=True,
            openai_api_enabled=False,
            anthropic_api_enabled=False,
            codex_cli_enabled=False,
            claude_cli_enabled=False,
            cloud_generation_timeout=600.0,
            cloud_max_output_tokens=8192,
            cloud_max_response_bytes=8 * 1024 * 1024,
            cloud_max_retries=0,
            cloud_retry_backoff=0.5,
            fast_model="codex-cli:auto",
            reasoning_model="codex-cli:auto",
            coding_model="codex-cli:auto",
            deep_model="codex-cli:auto",
            model="auto",
            learning_model=None,
            ollama_enabled=False,
            data_dir=Path("C:/jarvis/data"),
        )
        with patch.dict(
            "os.environ",
            {
                "OPENAI_API_KEY": "sk-test-not-real",
                "ANTHROPIC_API_KEY": "sk-ant-test-not-real",
            },
            clear=True,
        ):
            client = build_model_client(config)

        self.assertIsNone(client.openai)
        self.assertIsNone(client.anthropic)

    def test_lists_configured_providers_and_dispatches_without_prefix_leak(self):
        ollama = FakeOllama(["qwen3.5:9b"])
        openai = FakeCloud("gpt-5.6", "openai")
        anthropic = FakeCloud("claude-sonnet-5", "anthropic")
        client = ModelClient(
            ollama,
            openai=openai,
            anthropic=anthropic,
            configured_models=("openai:gpt-5.6-terra", "anthropic:claude-opus-5"),
        )

        models = client.models()
        self.assertIn("qwen3.5:9b", models)
        self.assertIn("ollama:qwen3.5:9b", models)
        self.assertIn("openai:gpt-5.6-terra", models)
        self.assertIn("anthropic:claude-opus-5", models)
        self.assertEqual(
            client.chat([], [], "openai:gpt-5.6-terra")["content"], "openai"
        )
        self.assertEqual(openai.calls[0][2], "gpt-5.6-terra")
        self.assertEqual(
            client.chat([], [], "anthropic:claude-sonnet-5")["content"], "anthropic"
        )
        self.assertEqual(anthropic.calls[0][2], "claude-sonnet-5")
        self.assertEqual(client.chat([], [], "qwen3.5:9b")["content"], "local")
        self.assertEqual(ollama.calls[0][2], "qwen3.5:9b")

    def test_cloud_provider_can_operate_when_ollama_is_offline(self):
        client = ModelClient(
            FakeOllama(error=OllamaError("offline")),
            openai=FakeCloud("gpt-5.6", "openai"),
        )
        self.assertEqual(client.models(), ["openai:gpt-5.6"])
        self.assertFalse(client.provider_status["ollama_online"])

    def test_cloud_provider_skips_ollama_entirely_when_disabled(self):
        client = ModelClient(
            None,
            openai=FakeCloud("gpt-5.6", "openai"),
            configured_models=("openai:gpt-5.6-luna",),
        )

        self.assertEqual(
            client.models(),
            ["openai:gpt-5.6", "openai:gpt-5.6-luna"],
        )
        self.assertFalse(client.provider_status["ollama_enabled"])
        self.assertFalse(client.provider_status["ollama_online"])
        with self.assertRaisesRegex(ModelProviderError, "disabled"):
            client.chat([], [], "ollama:qwen3.5:9b")

    def test_missing_cloud_key_fails_closed(self):
        client = ModelClient(FakeOllama(["qwen3.5:9b"]))
        with self.assertRaisesRegex(ModelProviderError, "OPENAI_API_KEY"):
            client.chat([], [], "openai:gpt-5.6")
        with self.assertRaisesRegex(ModelProviderError, "ANTHROPIC_API_KEY"):
            client.chat([], [], "anthropic:claude-sonnet-5")
        with self.assertRaisesRegex(ModelProviderError, "JARVIS_CODEX_CLI_ENABLED"):
            client.chat([], [], "codex-cli:default")


if __name__ == "__main__":
    unittest.main()
