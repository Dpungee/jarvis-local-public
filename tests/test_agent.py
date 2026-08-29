import base64
import json
import os
import re
import shutil
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.agent import (
    Agent,
    AgentRunCancelled,
    _COMPUTER_SCOPE_INTENT,
    _contextual_artifact_launch_target,
    _contextual_failed_computer_action_target,
    _contextual_product_research_target,
    _is_pending_goal_followup,
    _is_contextual_software_build_request,
    _is_non_code_document_operation,
    _live_system_status_kind,
    _NEGATED_LOCAL_FILE_CLAUSE,
    _prompt_json,
    _product_relevant_urls,
    _product_search_queries,
    _requested_browser_url,
    _requests_computer_access,
    _requested_document_formats,
    _required_effect_tools,
    _SPECIALIST_DELEGATION_INTENT,
    _SESSION_HISTORY_LOOKUP_INTENT,
    _requires_coding,
    _requires_external_mutation,
    _requires_self_diagnosis,
    _source_mutation_error,
    _task_family,
    _vault_chat_actions,
    _requires_web,
    _verified_product_comparison,
)
from jarvis.attachments import ImageAttachment
from jarvis.config import Config
from jarvis.memory import Memory, ModelBudgetExceeded
from jarvis.memory_embeddings import EmbeddingError
from jarvis.model_client import ModelProviderError
from jarvis.ollama_client import ChatResponse, OllamaError
from jarvis.proactive import record_result_reflection
from jarvis.router import Route
from jarvis.specialists import specialist_for_prompt
from jarvis.tools import EXTERNAL_MUTATION_TOOLS, SELF_INSPECTION_TOOLS
from jarvis.vault import Vault


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)

SUBSTANTIVE_RESEARCH_RESULT = (
    "Verified widget evidence shows the current release improves request reliability "
    "through bounded retries, clearer failure reporting, and consistent result handling: "
    "https://example.com/source"
)


def tool_call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


class FakeResponse(dict):
    def __init__(self, content="", tool_calls=None, done_reason=None, done=None):
        super().__init__(role="assistant", content=content)
        if tool_calls is not None:
            self["tool_calls"] = tool_calls
        self.done_reason = done_reason
        self.done = done


class ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def models(self, refresh=True):
        return ["qwen3.5:9b", "gpt-oss:20b", "qwen3:30b", "qwen3-coder:30b"]

    def chat(
        self,
        messages,
        tools,
        model,
        context_length,
        think=None,
        temperature=0.2,
        response_format=None,
        seed=None,
        keep_alive=None,
    ):
        self.requests.append({
            "messages": messages,
            "tools": tools,
            "model": model,
            "context_length": context_length,
            "think": think,
            "temperature": temperature,
            "response_format": response_format,
            "seed": seed,
            "keep_alive": keep_alive,
        })
        if not self.responses:
            raise AssertionError("Scripted client ran out of responses")
        return self.responses.pop(0)


class StreamingScriptedClient(ScriptedClient):
    def __init__(self, responses, deltas):
        super().__init__(responses)
        self.deltas = list(deltas)
        self.stream_calls = 0

    def chat_stream(
        self,
        messages,
        tools,
        model,
        on_delta,
        context_length,
        think=None,
        temperature=0.2,
        response_format=None,
        seed=None,
        keep_alive=None,
    ):
        self.stream_calls += 1
        for delta in self.deltas:
            on_delta(delta)
        return self.chat(
            messages,
            tools,
            model,
            context_length,
            think=think,
            temperature=temperature,
            response_format=response_format,
            seed=seed,
            keep_alive=keep_alive,
        )


class VisionScriptedClient(ScriptedClient):
    def models(self, refresh=True):
        return ["openai:gpt-5.6-luna"]


class PreloadClient(ScriptedClient):
    def __init__(self, responses):
        super().__init__(responses)
        self.preloads = []
        self.capability_checks = []

    def preload(self, model, *, context_length):
        self.preloads.append((model, context_length))

    def supports_thinking(self, model):
        self.capability_checks.append(model)
        return False


class FakeToolBox:
    NAMES = (
        "web_search",
        "web_fetch",
        "list_files",
        "read_file",
        "write_file",
        "edit_file",
        "search_files",
        "run_process",
        "remember",
        "recall",
    )

    def __init__(self, failures=None, *, verified_pages=None):
        self.calls = []
        self.failures = set(failures or ())
        self.verified_pages = verified_pages
        self.schemas = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "test",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in self.NAMES
        ]

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.failures or name not in self.NAMES:
            return json.dumps({"ok": False, "error": f"{name} failed"})
        if name == "web_search":
            verified_pages = self.verified_pages
            if verified_pages is None:
                verified_pages = [{
                    "title": "Primary",
                    "url": "https://example.com/source",
                    "content": "verified facts",
                }]
            return json.dumps({
                "ok": True,
                "result": {
                    "results": [],
                    "verified_pages": verified_pages,
                    "fetch_errors": [],
                },
            })
        if name == "web_fetch":
            return json.dumps({
                "ok": True,
                "result": {
                    "url": "https://example.com/source",
                    "untrusted": True,
                    "content": "verified facts",
                },
            })
        if name == "read_file":
            return json.dumps({
                "ok": True,
                "result": {
                    "path": str(arguments.get("path", "")),
                    "sha256": "a" * 64,
                    "content": "original content",
                    "truncated": False,
                },
            })
        if name == "run_process":
            return json.dumps({
                "ok": True,
                "result": {
                    "exit_code": 0,
                    "timed_out": False,
                    "stdout": "Ran 1 test in 0.001s\nOK",
                    "stderr": "",
                },
            })
        return json.dumps({"ok": True, "result": {"name": name}})


class DelegatingFakeToolBox(FakeToolBox):
    NAMES = FakeToolBox.NAMES + ("delegate_specialist", "specialist_reports")

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "delegate_specialist":
            selected = specialist_for_prompt(str(arguments.get("task") or ""))
            specialist_name = selected.name if selected is not None else "Unknown"
            return json.dumps({
                "ok": True,
                "result": {
                    "task_id": 42,
                    "specialist": specialist_name,
                    "purpose": selected.purpose if selected is not None else "unknown",
                    "status": "queued",
                },
            })
        if name == "specialist_reports":
            return json.dumps({
                "ok": True,
                "result": [{
                    "task_id": 42,
                    "specialist": "Sentinel",
                    "status": "done",
                    "result": "Prioritize default-deny rules and verify the recovery path.",
                }],
            })
        self.calls.pop()
        return super().execute(name, arguments)


class ImageFakeToolBox(FakeToolBox):
    NAMES = FakeToolBox.NAMES + (
        "image_generation_status", "generate_image", "edit_attached_image",
    )

    def __init__(self, *, configured=True, succeed=True):
        super().__init__()
        self.configured = configured
        self.succeed = succeed

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "image_generation_status":
            return json.dumps({
                "ok": True,
                "result": {
                    "provider": "openai_images",
                    "model": "gpt-image-2",
                    "configured": self.configured,
                },
            })
        if name in {"generate_image", "edit_attached_image"} and self.succeed:
            return json.dumps({
                "ok": True,
                "result": {
                    "relative_path": str(arguments["output"]),
                    "sha256": "b" * 64,
                    "model": "gpt-image-2",
                },
            })
        if name in {"generate_image", "edit_attached_image"}:
            return json.dumps({"ok": False, "error": "OpenAI Images request failed"})
        self.calls.pop()
        return super().execute(name, arguments)


class ForcedEscalationRouter:
    def select(self, prompt, override=None, *, requires_vision=False):
        del requires_vision
        return Route("fast", "qwen3.5:9b", "forced fast")

    def escalate(self, current, prompt):
        return Route("coding", "qwen3-coder:30b", "forced coding escalation")

    def failover(self, current, reason):
        return current

    def update_models(self, models):
        pass


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = TEMP_ROOT / f"agent-{os.getpid()}-{self._testMethodName}"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir()
        self.workspace = self.test_dir / "workspace"
        self.data_dir = self.test_dir / "data"
        self.workspace.mkdir()
        self.data_dir.mkdir()
        base = Config.load()
        self.config = replace(
            base,
            model="auto",
            workspace=self.workspace,
            data_dir=self.data_dir,
            max_steps=20,
            context_length=4096,
            fast_model="qwen3.5:9b",
            reasoning_model="gpt-oss:20b",
            coding_model="qwen3-coder:30b",
            fast_context_length=16384,
            reasoning_context_length=16384,
            coding_context_length=16384,
            ollama_preload=False,
            reasoning_thinking=True,
            vault_dir=None,
        )
        self.memory = Memory(self.data_dir / "agent.db")

    def tearDown(self):
        self.memory.close()
        resolved = self.test_dir.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def make_agent(self, responses, toolbox=None):
        client = ScriptedClient(responses)
        agent = Agent(
            self.config, self.memory, client=client,
            coding_review=False, coding_planning=False,
        )
        agent.toolbox = toolbox or FakeToolBox()
        return agent, client

    def test_optional_preload_warms_configured_local_model_once(self):
        client = PreloadClient([])
        events = []
        config = replace(
            self.config,
            model="auto",
            fast_model="qwen3-coder:30b",
            fast_context_length=8192,
            ollama_preload=True,
        )

        agent = Agent(config, self.memory, events.append, client=client)

        self.assertEqual(client.preloads, [("qwen3-coder:30b", 8192)])
        self.assertEqual(events, ["model - warming qwen3-coder:30b"])
        self.assertFalse(agent._think_for(Route("reasoning", "qwen3-coder:30b", "test")))
        self.assertEqual(client.capability_checks, ["qwen3-coder:30b"])

    def test_vault_commands_run_deterministically_without_a_model_turn(self):
        vault_root = self.workspace / "JarvisVault"
        vault_root.mkdir()
        Vault(vault_root).write_note(
            "research", "Command test", "Vault command sentinel."
        )
        client = ScriptedClient([])
        agent = Agent(
            replace(self.config, vault_dir=vault_root, memory_embeddings="disabled"),
            self.memory,
            client=client,
            coding_review=False,
            coding_planning=False,
        )

        result = agent.run("jarvis vault status\njarvis vault reindex")

        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("Notes: 1", str(result))
        self.assertIn("Vault reindex complete: 1 note(s)", str(result))
        self.assertEqual(client.requests, [])
        row = self.memory.db.execute(
            "SELECT content FROM memories WHERE kind='vault'"
        ).fetchone()
        self.assertIn("Vault command sentinel", row["content"])

    def test_vault_command_parser_is_bounded_and_does_not_capture_prose(self):
        self.assertEqual(_vault_chat_actions("vault status"), ("status",))
        self.assertEqual(_vault_chat_actions("please reindex the vault"), ("reindex",))
        self.assertEqual(
            _vault_chat_actions("jarvis vault status\njarvis vault reindex"),
            ("status", "reindex"),
        )
        self.assertEqual(
            _vault_chat_actions("Explain what the vault status command means"), ()
        )

    def test_presence_callback_streams_tool_free_prose_and_persists_redacted_final(self):
        secret = "sk-proj-" + "A" * 32
        client = StreamingScriptedClient(
            [FakeResponse(content=f"Hello api_key={secret}")],
            ["Hello ", f"api_key={secret}"],
        )
        agent = Agent(
            self.config,
            self.memory,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = FakeToolBox()
        deltas = []

        result = agent.run("Talk with me for a moment", stream_callback=deltas.append)

        self.assertEqual(client.stream_calls, 1)
        self.assertEqual(deltas, ["Hello ", f"api_key={secret}"])
        self.assertNotIn(secret, str(result))
        self.assertEqual(result.metrics["profile"], "fast")
        self.assertEqual(result.metrics["model"], self.config.fast_model)
        self.assertEqual(result.metrics["model_attempts"], 1)
        self.assertEqual(result.metrics["retries"], 0)
        self.assertEqual(result.metrics["tool_calls"], 0)
        self.assertTrue(result.metrics["streamed"])
        self.assertIsInstance(result.metrics["time_to_first_token_ms"], int)
        self.assertRegex(result.metrics["trace_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(result.metrics["origin"], "interactive")
        self.assertEqual(result.metrics["cohort"], "phase1-observability")
        self.assertEqual(result.metrics["token_measurement"], "unknown")
        self.assertIsInstance(result.metrics["provider_ttft_ms"], int)
        self.assertEqual(
            result.metrics["end_to_end_ttft_ms"],
            result.metrics["time_to_first_token_ms"],
        )
        self.assertGreaterEqual(
            result.metrics["end_to_end_total_ms"],
            result.metrics["end_to_end_ttft_ms"],
        )
        self.assertGreater(result.metrics["context_chars"], 0)
        self.assertGreater(result.metrics["estimated_prompt_tokens"], 0)
        messages = self.memory.recent_messages(result.conversation_id, limit=5)
        self.assertNotIn(secret, json.dumps(messages))

    def test_invalid_optional_telemetry_never_breaks_a_completed_answer(self):
        agent, _client = self.make_agent([])
        events = []
        agent.on_event = events.append
        agent._active_run_started = time.monotonic()
        agent._active_selected_model = "free form model label is not telemetry"
        agent._active_trace_id = "e" * 32
        result = type("CompletedResult", (), {
            "model": None,
            "status": "complete",
            "tool_calls": 0,
            "metrics": {},
        })()

        agent._attach_run_metrics(result)

        self.assertEqual(result.metrics["tool_calls"], 0)
        self.assertEqual(result.metrics["token_measurement"], "unknown")
        self.assertNotIn("model", result.metrics)
        self.assertIn(
            "observability - invalid optional metrics discarded",
            events,
        )

    def test_image_is_framed_as_untrusted_runtime_content_and_never_persisted(self):
        raw = b"\x89PNG\r\n\x1a\nprivate-pixels"
        image = ImageAttachment("image/png", raw, "screen.png")
        client = VisionScriptedClient([FakeResponse(content="I can see the screenshot.")])
        config = replace(
            self.config,
            fast_model="openai:gpt-5.6-luna",
            reasoning_model="openai:gpt-5.6-luna",
            coding_model="openai:gpt-5.6-luna",
            deep_model="openai:gpt-5.6-luna",
        )
        agent = Agent(
            config,
            self.memory,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = FakeToolBox()

        result = agent.run("What is wrong here?", attachments=[image])

        self.assertEqual(result, "I can see the screenshot.")
        request = client.requests[0]
        self.assertEqual(request["model"], "openai:gpt-5.6-luna")
        user_content = request["messages"][-1]["content"]
        self.assertIsInstance(user_content, list)
        self.assertEqual([part["type"] for part in user_content], ["text", "image", "text"])
        self.assertIn("untrusted_image_attachments", user_content[0]["text"])
        self.assertIn(image.sha256, user_content[0]["text"])
        persisted = "\n".join(self.memory.db.iterdump())
        self.assertNotIn("private-pixels", persisted)
        self.assertNotIn(user_content[1]["data"], persisted)

    def test_plain_attached_image_edit_runs_directly_and_returns_preview_artifact(self):
        image = ImageAttachment(
            "image/png", b"\x89PNG\r\n\x1a\nprivate-pixels", "logo.png"
        )
        client = VisionScriptedClient([])
        agent = Agent(
            replace(
                self.config,
                fast_model="openai:gpt-5.6-luna",
                reasoning_model="openai:gpt-5.6-luna",
                coding_model="openai:gpt-5.6-luna",
                deep_model="openai:gpt-5.6-luna",
            ),
            self.memory,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        toolbox = ImageFakeToolBox()
        agent.toolbox = toolbox

        result = agent.run("make this better", attachments=[image])

        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("[[jarvis-image:generated-images/jarvis-edit-", result)
        self.assertEqual(client.requests, [])
        edit_call = next(
            call for call in toolbox.calls if call[0] == "edit_attached_image"
        )
        self.assertEqual(edit_call[1]["attachment_index"], 1)
        self.assertIn("cleaner geometry", edit_call[1]["prompt"])
        persisted = "\n".join(self.memory.db.iterdump())
        self.assertNotIn(base64.b64encode(image.data).decode("ascii"), persisted)

    def test_image_edit_without_connected_provider_gives_one_actionable_reply(self):
        image = ImageAttachment(
            "image/png", b"\x89PNG\r\n\x1a\nprivate-pixels", "logo.png"
        )
        client = VisionScriptedClient([])
        agent = Agent(
            replace(
                self.config,
                fast_model="openai:gpt-5.6-luna",
                reasoning_model="openai:gpt-5.6-luna",
                coding_model="openai:gpt-5.6-luna",
                deep_model="openai:gpt-5.6-luna",
            ),
            self.memory,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = ImageFakeToolBox(configured=False, succeed=False)

        result = agent.run("remove the background", attachments=[image])

        self.assertEqual(result.status, "incomplete")
        self.assertIn("OPENAI_API_KEY", result)
        self.assertIn("subscription sign-in does not include", result)
        self.assertEqual(client.requests, [])

    def test_plain_image_generation_runs_without_a_text_model_turn(self):
        agent, client = self.make_agent([], toolbox=ImageFakeToolBox())

        result = agent.run(
            "Create a blue geometric logo for Example Studio"
        )

        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("[[jarvis-image:generated-images/jarvis-image-", result)
        self.assertEqual(client.requests, [])

    def test_neural_recall_is_injected_and_learns_from_the_task_outcome(self):
        class FakeEmbedder:
            model = "test-embedding"
            dimensions = 2

            def __init__(self):
                self.calls = []

            def embed(self, inputs):
                self.calls.append(list(inputs))
                return [
                    [1.0, 0.0]
                    if "automobile" in value or "roadster" in value
                    else [0.0, 1.0]
                    for value in inputs
                ]

        self.memory.remember_verified(
            "The user's roadster requires premium fuel and annual service.",
            "preference",
            "operator",
            origin="verified_import",
        )
        pending = self.memory.pending_memory_embeddings("test-embedding", limit=10)
        self.memory.store_memory_embeddings(
            "test-embedding", pending, [[1.0, 0.0] for _item in pending]
        )
        agent, client = self.make_agent([FakeResponse(content="Annual service is due.")])
        embedder = FakeEmbedder()
        agent.memory_embedder = embedder

        result = agent.run("What maintenance does my automobile need?")
        agent.system_prompt("What maintenance does my automobile need?")

        self.assertEqual(result.status, "complete")
        self.assertEqual(len(embedder.calls), 1)
        rendered = json.dumps(client.requests[0]["messages"], ensure_ascii=False)
        self.assertIn("roadster requires premium fuel", rendered)
        quality = self.memory.memory_quality()
        self.assertEqual(quality["totals"]["retrievals"], 1)
        self.assertEqual(quality["totals"]["resolved_retrievals"], 1)
        self.assertEqual(quality["totals"]["observed_utility"], 1.0)

    def test_memory_retrieval_is_not_credited_when_compaction_removes_it(self):
        self.memory.remember_verified(
            "The operator's observability sentinel belongs in the provider prompt.",
            "preference",
            "operator",
            origin="verified_import",
        )
        agent, client = self.make_agent([FakeResponse(content="Done.")])

        original_compaction = agent._compact_messages

        def remove_memory(messages, context_length):
            compacted = original_compaction(messages, context_length)
            for message in compacted:
                content = message.get("content")
                if isinstance(content, str):
                    message["content"] = re.sub(
                        r"<untrusted_memory_records>.*?</untrusted_memory_records>",
                        "<untrusted_memory_records>[]</untrusted_memory_records>",
                        content,
                        flags=re.DOTALL,
                    )
            return compacted

        with patch.object(agent, "_compact_messages", side_effect=remove_memory):
            result = agent.run("Explain my observability sentinel")

        self.assertEqual(result.status, "complete")
        self.assertNotIn(
            "belongs in the provider prompt",
            json.dumps(client.requests[0]["messages"], ensure_ascii=False),
        )
        self.assertEqual(self.memory.memory_quality()["totals"]["retrievals"], 0)

    def test_neural_recall_failure_keeps_sparse_memory_and_chat_available(self):
        class BrokenEmbedder:
            model = "test-embedding"

            def embed(self, _inputs):
                raise EmbeddingError("offline")

        self.memory.remember_verified(
            "Ollama keeps model layers in GPU memory",
            "fact",
            "operator",
            origin="verified_import",
        )
        events = []
        agent, client = self.make_agent([FakeResponse(content="Sparse recall worked.")])
        agent.on_event = events.append
        agent.memory_embedder = BrokenEmbedder()

        result = agent.run("Explain Ollama GPU memory behavior")

        self.assertEqual(result.status, "complete")
        self.assertIn(
            "Ollama keeps model layers",
            json.dumps(client.requests[0]["messages"], ensure_ascii=False),
        )
        self.assertIn(
            "memory - neural recall unavailable; sparse recall retained",
            events,
        )

    def test_explicit_operator_preference_survives_paraphrased_recall(self):
        self.memory.remember_verified(
            "Prefers concise answers for routine questions and evidence for current claims.",
            "preference",
            "user",
            origin="verified_import",
        )
        agent, client = self.make_agent([
            FakeResponse(content="You prefer concise routine replies."),
        ])

        result = agent.run("How do I like my replies formatted?")

        self.assertEqual(result.status, "complete")
        rendered = json.dumps(client.requests[0]["messages"], ensure_ascii=False)
        self.assertIn("Prefers concise answers for routine questions", rendered)

    def test_cloud_deep_profile_uses_quality_first_reasoning(self):
        agent, _client = self.make_agent([])
        for model in (
            "openai:gpt-5.6-sol",
            "anthropic:claude-sonnet-5",
            "codex-cli:auto",
            "claude-cli:sonnet",
        ):
            with self.subTest(model=model):
                self.assertEqual(
                    agent._think_for(Route("deep", model, "security specialist")),
                    "high",
                )
        self.assertFalse(
            agent._think_for(Route("deep", "qwen3-coder:30b", "local deep"))
        )

    def test_model_calls_persist_token_and_latency_metadata_without_content(self):
        response = ChatResponse(
            {"role": "assistant", "content": "private answer text"},
            {
                "done": True,
                "model": "qwen3.5:9b",
                "prompt_eval_count": 123,
                "eval_count": 45,
            },
        )
        agent, _client = self.make_agent([response])

        result, route = agent._chat(
            [{"role": "user", "content": "private prompt text"}],
            [],
            Route("fast", "qwen3.5:9b", "test"),
        )

        self.assertIs(result, response)
        self.assertEqual(route.model, "qwen3.5:9b")
        row = dict(self.memory.db.execute(
            "SELECT * FROM model_call_metrics"
        ).fetchone())
        self.assertEqual(row["provider"], "ollama")
        self.assertEqual(row["model"], "qwen3.5:9b")
        self.assertEqual(row["profile"], "fast")
        self.assertEqual(row["prompt_tokens"], 123)
        self.assertEqual(row["completion_tokens"], 45)
        self.assertEqual(row["success"], 1)
        self.assertNotIn("private", json.dumps(row))

    def test_model_call_budget_stops_a_request_before_an_extra_provider_call(self):
        config = replace(self.config, model_call_limit_per_request=1)
        client = ScriptedClient([
            FakeResponse(tool_calls=[tool_call("list_files", {"path": "."})]),
            FakeResponse(content="should never be requested"),
        ])
        agent = Agent(
            config, self.memory, client=client,
            coding_review=False, coding_planning=False,
        )
        agent.toolbox = FakeToolBox()

        result = agent.run("List the files in this workspace and summarize them")

        self.assertEqual(result.status, "incomplete")
        self.assertIn("model-call limit reached", result.reason)
        self.assertEqual(len(client.requests), 1)
        self.assertIn("No further model calls were made", result)

    def test_direct_model_calls_share_the_same_active_budget(self):
        config = replace(self.config, model_call_limit_per_request=1)
        client = ScriptedClient([FakeResponse(content="first")])
        agent = Agent(config, self.memory, client=client)
        route = Route("fast", "qwen3.5:9b", "test")
        agent._active_model_budget_scope = "request:" + "c" * 32

        agent._chat([{"role": "user", "content": "one"}], [], route)
        with self.assertRaises(ModelBudgetExceeded):
            agent._chat([{"role": "user", "content": "two"}], [], route)
        self.assertEqual(len(client.requests), 1)

    def test_named_document_target_cannot_be_satisfied_by_wrong_file_write(self):
        agent, client = self.make_agent([
            FakeResponse(tool_calls=[tool_call(
                "write_file", {"path": ".gitkeep", "content": "draft"}
            )]),
            FakeResponse(content="Done."),
        ])

        result = agent.run("Create the email draft `stress-test-draft.eml` for me")

        self.assertEqual(result.status, "incomplete")
        self.assertIn("requested document target", result.reason)
        self.assertEqual(len(client.requests), 2)

    def test_named_document_target_accepts_the_matching_file_write(self):
        agent, _client = self.make_agent([
            FakeResponse(tool_calls=[tool_call(
                "write_file",
                {"path": "drafts/stress-test-draft.eml", "content": "draft"},
            )]),
            FakeResponse(content="Draft created."),
        ])

        result = agent.run("Create the email draft `stress-test-draft.eml` for me")

        self.assertEqual(result.status, "complete")

    def test_named_document_targets_accept_verified_generator_outputs(self):
        class GeneratorToolBox(FakeToolBox):
            NAMES = FakeToolBox.NAMES + ("build_document",)

            def __init__(self, workspace):
                super().__init__()
                self.workspace = workspace

            def execute(self, name, arguments):
                if name == "run_process":
                    for filename in ("brief.docx", "brief.pdf", "plan.xlsx"):
                        (self.workspace / filename).write_bytes(
                            ("generated-" + filename).encode("utf-8")
                        )
                return super().execute(name, arguments)

        agent, _client = self.make_agent([
            FakeResponse(tool_calls=[tool_call(
                "write_file",
                {"path": "build_bundle.py", "content": "# generator"},
            )]),
            FakeResponse(tool_calls=[tool_call(
                "run_process", {"program": "python", "arguments": ["build_bundle.py"]}
            )]),
            FakeResponse(tool_calls=[
                tool_call("build_document", {
                    "path": "brief.docx", "document_type": "docx", "title": "Brief",
                    "sections": [{"heading": "Summary", "body": "Verified brief."}],
                }),
                tool_call("build_document", {
                    "path": "brief.pdf", "document_type": "pdf", "title": "Brief",
                    "sections": [{"heading": "Summary", "body": "Verified brief."}],
                }),
                tool_call("build_document", {
                    "path": "plan.xlsx", "document_type": "xlsx", "title": "Plan",
                    "sections": [{"heading": "Plan", "body": "Verified plan."}],
                }),
            ]),
            FakeResponse(content=(
                "Created `brief.docx`, `brief.pdf`, and `plan.xlsx`."
            )),
        ])
        agent.toolbox = GeneratorToolBox(Path(self.config.workspace))

        result = agent.run(
            "Create `brief.docx`, `brief.pdf`, and `plan.xlsx` programmatically in "
            "this project, then run the generator to verify them."
        )

        self.assertEqual(result.status, "complete", result.reason)


    def test_workspace_coding_can_hide_redundant_computer_file_tools(self):
        agent, _client = self.make_agent([])
        for name in (
            "computer_list_files",
            "computer_read_file",
            "computer_write_file",
            "computer_search_files",
            "windows_list_apps",
            "windows_open_apps",
            "windows_launch_app",
            "windows_open_url",
            "photoshop_remove_background",
            "system_snapshot",
            "launch_artifact",
        ):
            agent.toolbox.schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": "test",
                    "parameters": {"type": "object", "properties": {}},
                },
            })
        hidden = {
            schema["function"]["name"]
            for schema in agent._schemas_for_state(
                research_mode=False,
                web_tainted=False,
                local_tainted=False,
                allow_write=True,
                allow_execution=True,
                allow_memory_write=False,
                allow_computer_files=False,
            )
        }
        self.assertTrue({
            "computer_list_files",
            "computer_read_file",
            "computer_write_file",
            "computer_search_files",
            "windows_list_apps",
            "windows_open_apps",
            "windows_launch_app",
            "windows_open_url",
            "photoshop_remove_background",
        }.isdisjoint(hidden))
        self.assertTrue({"system_snapshot", "launch_artifact"}.issubset(hidden))

        exposed = {
            schema["function"]["name"]
            for schema in agent._schemas_for_state(
                research_mode=False,
                web_tainted=False,
                local_tainted=False,
                allow_write=True,
                allow_execution=True,
                allow_memory_write=False,
                allow_computer_files=True,
            )
        }
        self.assertTrue({
            "computer_write_file", "windows_list_apps", "windows_open_apps", "windows_launch_app",
            "windows_open_url", "photoshop_remove_background",
        }.issubset(exposed))

    def test_bare_wake_words_are_instant_and_tool_free(self):
        for prompt in ("jarvis", "jar", "JARVIS!", "jarvis?"):
            with self.subTest(prompt=prompt):
                toolbox = FakeToolBox()
                agent, client = self.make_agent([], toolbox=toolbox)

                result = agent.run(prompt)

                self.assertEqual(result.status, "complete")
                self.assertIn("Ready when you are", str(result))
                self.assertEqual(client.requests, [])
                self.assertEqual(toolbox.calls, [])

    def test_live_system_status_routes_to_exact_measured_source(self):
        self.assertEqual(
            _live_system_status_kind("Which programs are currently open?"),
            "open_apps",
        )
        self.assertEqual(
            _live_system_status_kind("Which programs do I have installed?"),
            "installed_apps",
        )
        self.assertEqual(
            _live_system_status_kind("How much RAM is free right now?"),
            "system_snapshot",
        )
        self.assertIsNone(
            _live_system_status_kind("How much RAM does that game need?")
        )

    def test_open_program_question_is_deterministic_and_never_reuses_prior_answer(self):
        class LiveSystemToolBox(FakeToolBox):
            NAMES = FakeToolBox.NAMES + (
                "windows_open_apps", "windows_list_apps", "system_snapshot",
            )

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "windows_open_apps":
                    return json.dumps({
                        "ok": True,
                        "result": {
                            "available": True,
                            "applications": [
                                {"name": "chrome.exe"},
                                {"name": "notepad.exe"},
                            ],
                            "count": 2,
                            "truncated": False,
                            "window_titles_read": False,
                            "window_content_read": False,
                        },
                    })
                self.calls.pop()
                return super().execute(name, arguments)

        toolbox = LiveSystemToolBox()
        agent, client = self.make_agent(
            [FakeResponse(content="Houseplants can brighten a room.")], toolbox=toolbox
        )
        conversation_id = self.memory.new_conversation("stale-answer-regression")
        self.memory.add_message(conversation_id, "user", "What do you think of houseplants?")
        self.memory.add_message(
            conversation_id, "assistant", "Houseplants can brighten a room."
        )

        result = agent.run(
            "Which programs are currently open?",
            conversation_id=conversation_id,
        )

        self.assertEqual(result.status, "complete", result.reason)
        self.assertIn("chrome.exe", str(result))
        self.assertIn("notepad.exe", str(result))
        self.assertNotIn("brighten a room", str(result))
        self.assertEqual(toolbox.calls, [("windows_open_apps", {"limit": 100})])
        self.assertEqual(client.requests, [])
        self.assertEqual(result.tool_calls, 1)

    def test_installed_apps_and_resource_status_use_distinct_deterministic_tools(self):
        class LiveSystemToolBox(FakeToolBox):
            NAMES = FakeToolBox.NAMES + (
                "windows_open_apps", "windows_list_apps", "system_snapshot",
            )

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "windows_list_apps":
                    return json.dumps({
                        "ok": True,
                        "result": {
                            "applications": [{"name": "Installed Editor"}],
                            "count": 1,
                        },
                    })
                if name == "system_snapshot":
                    return json.dumps({
                        "ok": True,
                        "result": {
                            "memory": {
                                "available_bytes": 8 * 1024**3,
                                "total_bytes": 16 * 1024**3,
                                "load_percent": 50,
                            },
                            "disk": {},
                            "physical_storage": {"available": False},
                        },
                    })
                self.calls.pop()
                return super().execute(name, arguments)

        installed_toolbox = LiveSystemToolBox()
        installed_agent, installed_client = self.make_agent(
            [FakeResponse(content="unused")], toolbox=installed_toolbox
        )
        installed = installed_agent.run("Which apps do I have installed?")
        self.assertIn("Installed Editor", str(installed))
        self.assertIn("does not mean they are currently open", str(installed))
        self.assertEqual(
            installed_toolbox.calls, [("windows_list_apps", {"limit": 100})]
        )
        self.assertEqual(installed_client.requests, [])

        resource_toolbox = LiveSystemToolBox()
        resource_agent, resource_client = self.make_agent(
            [FakeResponse(content="unused")], toolbox=resource_toolbox
        )
        resource = resource_agent.run("How much RAM is free right now?")
        self.assertIn("8.0 GB available of 16.0 GB", str(resource))
        self.assertEqual(resource_toolbox.calls, [("system_snapshot", {})])
        self.assertEqual(resource_client.requests, [])

        temperature_toolbox = LiveSystemToolBox()
        temperature_agent, temperature_client = self.make_agent(
            [FakeResponse(content="unused")], toolbox=temperature_toolbox
        )
        temperature = temperature_agent.run("What is my system temperature right now?")
        self.assertIn("does not measure hardware temperature", str(temperature))
        self.assertNotIn("RAM:", str(temperature))
        self.assertEqual(temperature_toolbox.calls, [("system_snapshot", {})])
        self.assertEqual(temperature_client.requests, [])

    def test_browser_open_request_with_bare_domain_runs_existing_tool_directly(self):
        prompt = "can you open a tab for google.com on my browser"
        self.assertEqual(_requested_browser_url(prompt), "https://google.com")
        self.assertEqual(
            _requested_browser_url("Please open https://example.com/docs in my browser."),
            "https://example.com/docs",
        )
        self.assertIsNone(_requested_browser_url("What do you think about google.com?"))
        self.assertTrue(_requests_computer_access(prompt))

        class BrowserToolBox(FakeToolBox):
            NAMES = FakeToolBox.NAMES + ("windows_open_url",)

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "windows_open_url":
                    return json.dumps({
                        "ok": True,
                        "result": {
                            "url": arguments["url"],
                            "host": "google.com",
                            "opened": True,
                            "pid": 42,
                        },
                    })
                self.calls.pop()
                return super().execute(name, arguments)

        toolbox = BrowserToolBox()
        client = ScriptedClient([])
        client.supports_task_contract = True
        agent = Agent(
            replace(
                self.config,
                computer_access="trusted-desktop",
                execution_mode="trusted-host",
            ),
            self.memory,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = toolbox

        result = agent.run(prompt)

        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual(
            toolbox.calls,
            [("windows_open_url", {"url": "https://google.com"})],
        )
        self.assertEqual(client.requests, [])
        self.assertIn("Opened https://google.com", str(result))

    def test_artifact_open_followup_resolves_and_launches_recent_safe_document(self):
        messages = [{
            "role": "assistant",
            "content": (
                "Created [demo_deck.pptx]"
                "(C:\\Users\\operator\\project\\demo_deck.pptx)."
            ),
        }]
        self.assertEqual(
            _contextual_artifact_launch_target("nah pull up the powerpoint", messages),
            "demo_deck.pptx",
        )
        self.assertIsNone(_contextual_artifact_launch_target(
            "pull it up",
            [{"role": "assistant", "content": "Created `untrusted.exe`."}],
        ))
        self.assertIsNone(_contextual_artifact_launch_target(
            "what did you put in it?", messages
        ))

        conversation_id = self.memory.new_conversation("artifact follow-up")
        self.memory.add_message(
            conversation_id,
            "user",
            "Put the research into a PowerPoint for me.",
        )
        self.memory.add_message(conversation_id, "assistant", messages[0]["content"])

        class ArtifactToolBox(FakeToolBox):
            NAMES = FakeToolBox.NAMES + ("launch_artifact",)

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "launch_artifact":
                    return json.dumps({
                        "ok": True,
                        "result": {
                            "path": str(arguments["path"]),
                            "launched": True,
                            "pid": None,
                        },
                    })
                self.calls.pop()
                return super().execute(name, arguments)

        toolbox = ArtifactToolBox()
        agent, client = self.make_agent([], toolbox=toolbox)
        result = agent.run("pull it up", conversation_id=conversation_id)

        self.assertEqual(result.status, "complete")
        self.assertIn("Opened `demo_deck.pptx`", str(result))
        self.assertEqual(toolbox.calls, [
            ("launch_artifact", {"path": "demo_deck.pptx"}),
        ])
        self.assertEqual(client.requests, [])

    def test_browser_open_approval_and_failed_tool_followup_preserve_exact_target(self):
        prompt = "can you open a tab for google.com on my browser"
        recent = [
            {"role": "user", "content": prompt},
            {
                "role": "assistant",
                "content": "I can’t directly open a browser tab from the tools available.",
            },
        ]
        self.assertEqual(
            _contextual_failed_computer_action_target(
                "can you create that tool", recent
            ),
            prompt,
        )

        class ApprovalBrowserToolBox(FakeToolBox):
            NAMES = FakeToolBox.NAMES + ("windows_open_url",)

            def execute(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "windows_open_url":
                    return json.dumps({
                        "ok": False,
                        "approval_required": True,
                        "approval_id": 41,
                    })
                self.calls.pop()
                return super().execute(name, arguments)

        toolbox = ApprovalBrowserToolBox()
        agent, client = self.make_agent([], toolbox=toolbox)
        client.supports_task_contract = True

        result = agent.run(prompt)

        self.assertEqual(result.status, "incomplete")
        self.assertTrue(result.waiting_for_approval)
        self.assertEqual(result.approval_id, 41)
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(client.requests, [])
        self.assertIn("https://google.com", str(result))

    def test_private_computer_tools_require_current_turn_action(self):
        class ComputerToolBox(FakeToolBox):
            NAMES = FakeToolBox.NAMES + (
                "computer_list_files",
                "computer_read_file",
                "computer_search_files",
                "computer_storage_report",
                "computer_write_file",
            )

        conversation_id = self.memory.new_conversation("disk cleanup")
        self.memory.add_message(
            conversation_id,
            "user",
            "Scan my C drive and tell me what I can clean up.",
        )
        self.memory.add_message(
            conversation_id,
            "assistant",
            "The prior storage request stopped before completion.",
        )
        toolbox = ComputerToolBox()
        agent, client = self.make_agent([
            FakeResponse(tool_calls=[tool_call(
                "computer_storage_report",
                {"path": ".", "limit": 100},
            )]),
            FakeResponse(content="Ready when you need something."),
        ], toolbox=toolbox)

        result = agent.run("Tell me a joke", conversation_id=conversation_id)

        self.assertEqual(result.status, "complete")
        self.assertEqual(toolbox.calls, [])
        self.assertEqual(len(client.requests), 1)
        self.assertIn("What would you like", str(result))
        offered = {
            schema["function"]["name"]
            for schema in client.requests[0]["tools"]
        }
        self.assertEqual(offered, set())
        self.assertTrue(set(ComputerToolBox.NAMES[-5:]).isdisjoint(offered))

    def test_failed_computer_action_followup_recovers_exact_operator_request(self):
        original = "Scan my C drive and tell me what I can safely clean up."
        failure = (
            "I couldn't complete the disk scan because the storage-report capability "
            "failed before returning system data. No files were changed."
        )
        messages = [
            {"role": "user", "content": original},
            {"role": "assistant", "content": failure},
        ]
        self.assertEqual(
            _contextual_failed_computer_action_target("what about now", messages),
            original,
        )

        repeated = messages + [
            {"role": "user", "content": "what about now"},
            {
                "role": "assistant",
                "content": "I still can't verify the scan; I need access to the report tool.",
            },
        ]
        self.assertEqual(
            _contextual_failed_computer_action_target("can you try again now?", repeated),
            original,
        )

    def test_failed_computer_action_followup_does_not_revive_superseded_work(self):
        original = "Scan my C drive and tell me what I can safely clean up."
        self.assertIsNone(_contextual_failed_computer_action_target(
            "what about now",
            [
                {"role": "user", "content": original},
                {"role": "assistant", "content": "The scan completed successfully."},
            ],
        ))
        self.assertIsNone(_contextual_failed_computer_action_target(
            "what about now",
            [
                {"role": "user", "content": original},
                {"role": "assistant", "content": "The scan failed before returning data."},
                {"role": "user", "content": "Tell me a joke instead."},
                {"role": "assistant", "content": "I couldn't think of one."},
            ],
        ))
        self.assertIsNone(_contextual_failed_computer_action_target(
            "what about lunch now?",
            [
                {"role": "user", "content": original},
                {"role": "assistant", "content": "The scan failed before returning data."},
            ],
        ))

    def test_failed_storage_followup_retries_report_without_model_round_trip(self):
        class StorageToolBox(FakeToolBox):
            NAMES = FakeToolBox.NAMES + ("computer_storage_report",)

            def execute(self, name, arguments):
                if name != "computer_storage_report":
                    return super().execute(name, arguments)
                self.calls.append((name, arguments))
                return json.dumps({
                    "ok": True,
                    "result": {
                        "root": "C:/Users/test",
                        "scanned_files": 12,
                        "scanned_bytes": 1_073_741_824,
                        "truncated": False,
                        "scan_time_ms": 125.0,
                        "largest_top_level_entries": [],
                        "largest_files": [],
                        "content_read": False,
                        "files_deleted": 0,
                    },
                })

        original = "Scan my C drive and tell me what I can safely clean up."
        conversation_id = self.memory.new_conversation("disk cleanup")
        self.memory.add_message(conversation_id, "user", original)
        self.memory.add_message(
            conversation_id,
            "assistant",
            "I couldn't complete the disk scan because the storage report failed.",
        )
        toolbox = StorageToolBox()
        agent, client = self.make_agent([], toolbox=toolbox)
        client.supports_task_contract = True

        result = agent.run("what about now", conversation_id=conversation_id)

        self.assertEqual(result.status, "complete")
        self.assertEqual(
            toolbox.calls,
            [("computer_storage_report", {"path": ".", "limit": 50})],
        )
        self.assertEqual(client.requests, [])
        self.assertIn("12 files", str(result))

    def test_human_conversation_is_one_model_turn_with_no_tools(self):
        prompts = (
            "What do you think about dogs?",
            "I had a rough day and just want to talk.",
            "Explain gravity simply.",
            "Do you remember what kind of projects I enjoy?",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                toolbox = FakeToolBox()
                agent, client = self.make_agent([
                    FakeResponse(content="Natural conversational response."),
                ], toolbox=toolbox)

                result = agent.run(prompt)

                self.assertEqual(result.status, "complete")
                self.assertEqual(str(result), "Natural conversational response.")
                self.assertEqual(len(client.requests), 1)
                self.assertEqual(client.requests[0]["tools"], [])
                self.assertEqual(toolbox.calls, [])

    def test_computer_access_intent_requires_scope_and_action(self):
        positives = (
            "Scan my C drive for large files",
            "Show me what's inside Downloads",
            "Open Photoshop for me",
            "Clean up storage on my PC",
            "Could you take a peek at what's filling up my laptop?",
            "My machine is full; can you see what is taking up space?",
            "Which programs are currently open?",
            "Which apps do I have installed?",
            "Do I have enough disk space for this game?",
        )
        negatives = (
            "jarvis",
            "Tell me a joke",
            "What is a computer?",
            "Explain how hard drives work",
            "What do you think about desktop computers?",
            "How much storage does this game need?",
            "How much RAM should a gaming PC have?",
            "How much RAM should my computer have?",
            "How does Python memory management work?",
            "How many hard drives should a NAS have?",
            "Gotta answer emails, work on Jarvis, hit the gym, maybe clean up my C drive, and make dinner. Where do I start?",
            "Where should I start cleaning up my C drive?",
        )
        for prompt in positives:
            with self.subTest(prompt=prompt):
                self.assertTrue(_requests_computer_access(prompt))
        for prompt in negatives:
            with self.subTest(prompt=prompt):
                self.assertFalse(_requests_computer_access(prompt))

    def test_storage_cleanup_uses_one_report_then_hides_private_scan_tools(self):
        class StorageToolBox(FakeToolBox):
            NAMES = FakeToolBox.NAMES + (
                "computer_list_files",
                "computer_read_file",
                "computer_search_files",
                "computer_storage_report",
                "system_snapshot",
            )

            def execute(self, name, arguments):
                if name != "computer_storage_report":
                    return super().execute(name, arguments)
                self.calls.append((name, arguments))
                return json.dumps({
                    "ok": True,
                    "result": {
                        "root": "C:/Users/test",
                        "scanned_files": 12,
                        "scanned_bytes": 1_073_741_824,
                        "truncated": True,
                        "truncation_reason": "time_limit",
                        "scan_time_ms": 12_000.0,
                        "largest_top_level_entries": [{
                            "path": "C:/Users/test/Downloads",
                            "size_bytes": 536_870_912,
                        }],
                        "largest_files": [{
                            "path": "C:/Users/test/Downloads/old.iso",
                            "size_bytes": 536_870_912,
                        }],
                        "content_read": False,
                        "files_deleted": 0,
                    },
                })

        toolbox = StorageToolBox()
        agent, client = self.make_agent([], toolbox=toolbox)
        client.supports_task_contract = True

        result = agent.run(
            "Can you optimize my hard drive for me and see if I have any temp files "
            "and old stuff I don't need that I can get rid of?"
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(
            toolbox.calls,
            [("computer_storage_report", {"path": ".", "limit": 50})],
        )
        self.assertEqual(len(client.requests), 0)
        self.assertIn("Storage scan completed", str(result))
        self.assertIn("Downloads/old.iso", str(result))
        self.assertIn("12-second safety limit", str(result))
        self.assertNotIn("approval", str(result).casefold())
        self.assertNotIn("can't", str(result).casefold())

    def test_requested_real_world_effects_require_matching_tool_success(self):
        cases = (
            (
                "Open Calculator for me",
                False,
                False,
                {"windows_launch_app"},
            ),
            (
                "Open https://example.com in my browser",
                False,
                False,
                {"windows_open_url"},
            ),
            (
                "Click the compose button in the active app and type my draft",
                False,
                False,
                {"desktop_interact"},
            ),
            (
                "Create a report file about the audit",
                False,
                False,
                {"write_file", "edit_file", "computer_write_file", "skill_create"},
            ),
            (
                "Send this report by email through my connector",
                False,
                True,
                {"connector_call"},
            ),
            (
                "Clean up my Google Drive",
                False,
                True,
                {"google_drive_organize_files"},
            ),
        )
        for prompt, coding, external, expected in cases:
            with self.subTest(prompt=prompt):
                tools, description = _required_effect_tools(
                    prompt,
                    requires_coding=coding,
                    allow_external_mutation=external,
                )
                self.assertTrue(tools & expected)
                self.assertIsNotNone(description)

        tools, description = _required_effect_tools(
            "Maybe clean up my C drive later; where do I start with today's tasks?",
            requires_coding=False,
            allow_external_mutation=False,
        )
        self.assertEqual(tools, frozenset())
        self.assertIsNone(description)

    def test_markdown_stress_test_note_is_a_document_not_a_coding_task(self):
        prompt = (
            "Create a file named stress-test-notes.md in this project with a heading "
            "Jarvis Stress Test and a checklist. Read it back and tell me the exact path."
        )

        self.assertTrue(_is_non_code_document_operation(prompt))
        self.assertFalse(_requires_coding(prompt))
        tools, description = _required_effect_tools(
            prompt,
            requires_coding=False,
            allow_external_mutation=False,
        )
        self.assertEqual(tools, frozenset({"__effect_path__:stress-test-notes.md"}))
        self.assertEqual(description, "requested document target")

    def test_offline_multiformat_report_request_routes_to_document_workflow(self):
        prompt = (
            "Inside this isolated diagnostic project, create a source Markdown brief and "
            "use Jarvis's supported offline document workflow to generate DOCX, PDF, and "
            "PPTX artifacts titled Capability Soak Report. Include a heading, a short "
            "table, and bullet list. Verify each artifact and generate or report safe "
            "preview metadata and structural QA. Do not publish or open external applications."
        )

        self.assertTrue(_is_non_code_document_operation(prompt))
        self.assertFalse(_requires_coding(prompt))
        self.assertFalse(_requires_web(prompt))
        self.assertEqual(
            _requested_document_formats(prompt),
            frozenset({"md", "docx", "pdf", "pptx"}),
        )
        tools, description = _required_effect_tools(
            prompt,
            requires_coding=False,
            allow_external_mutation=False,
        )
        self.assertIn("build_document", tools)
        self.assertTrue({
            "__document_type__:md",
            "__document_type__:docx",
            "__document_type__:pdf",
            "__document_type__:pptx",
        }.issubset(tools))
        self.assertEqual(description, "requested document change")

    def test_coding_build_and_launch_is_not_mistaken_for_desktop_app_control(self):
        tools, description = _required_effect_tools(
            "Build a dependency-free web app, run its tests, and launch the app",
            requires_coding=True,
            allow_external_mutation=False,
        )
        self.assertEqual(tools, frozenset())
        self.assertIsNone(description)

    def test_coding_generator_binds_explicit_office_artifact_effects(self):
        tools, description = _required_effect_tools(
            "Fix build_bundle.py, generate and verify daily-brief.docx, "
            "daily-brief.pdf, and weekly-plan.xlsx.",
            requires_coding=True,
            allow_external_mutation=False,
        )
        self.assertEqual(tools, frozenset({
            "__effect_path__:daily-brief.docx",
            "__effect_path__:daily-brief.pdf",
            "__effect_path__:weekly-plan.xlsx",
            "__document_type__:docx",
            "__document_type__:pdf",
            "__document_type__:xlsx",
            "build_document",
        }))
        self.assertEqual(description, "requested document target")

    def test_negated_file_clause_does_not_turn_design_review_into_file_inspection(self):
        prompt = (
            "Independently review a proposed dashboard design. Do not write or modify "
            "files. Return five architecture recommendations."
        )
        remaining = _NEGATED_LOCAL_FILE_CLAUSE.sub("", prompt)
        self.assertNotIn("files", remaining.casefold())
        self.assertIn("review a proposed dashboard design", remaining.casefold())
        specialist_prompt = (
            "Delegate to the network specialist: give one DNS failure mode and one "
            "exact verification check. Do not inspect or change project files."
        )
        self.assertFalse(_requires_coding(specialist_prompt))

    def test_explicit_specialist_request_is_detected_as_agent_work(self):
        prompts = (
            "Use the appropriate specialist agent to review this dashboard design.",
            "Delegate this research to a sub-agent and collect its report.",
            "Have a specialist independently analyze the network plan.",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertIsNotNone(_SPECIALIST_DELEGATION_INTENT.search(prompt))

    def test_explicit_prior_session_lookup_is_detected_as_agent_work(self):
        prompts = (
            "Find the codename I gave you in another conversation.",
            "Search our prior chat for the project decision.",
            "Recall what I mentioned in an earlier session.",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertIsNotNone(_SESSION_HISTORY_LOOKUP_INTENT.search(prompt))

    def test_photoshop_and_windows_app_prompts_expose_desktop_scope(self):
        positives = (
            "Open Photoshop for me",
            "Use a Windows app to edit this",
            "Remove the background from this photo",
            "Take the image background off in Photoshop",
            "Use my mouse to click the submit button on screen",
        )
        negatives = (
            "Explain what an application server is",
            "Deploy my app to production",
            "What does background knowledge mean?",
        )
        for prompt in positives:
            with self.subTest(prompt=prompt):
                self.assertIsNotNone(_COMPUTER_SCOPE_INTENT.search(prompt))
        for prompt in negatives:
            with self.subTest(prompt=prompt):
                self.assertIsNone(_COMPUTER_SCOPE_INTENT.search(prompt))

    def test_external_mutation_schemas_require_explicit_task_intent(self):
        agent, _client = self.make_agent([])
        for name in EXTERNAL_MUTATION_TOOLS:
            agent.toolbox.schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": "test",
                    "parameters": {"type": "object", "properties": {}},
                },
            })

        hidden = {
            schema["function"]["name"]
            for schema in agent._schemas_for_state(
                research_mode=False,
                web_tainted=False,
                local_tainted=False,
                allow_write=True,
                allow_execution=True,
                allow_memory_write=False,
                allow_external_mutation=False,
            )
        }
        self.assertTrue(EXTERNAL_MUTATION_TOOLS.isdisjoint(hidden))

        exposed = {
            schema["function"]["name"]
            for schema in agent._schemas_for_state(
                research_mode=False,
                web_tainted=False,
                local_tainted=False,
                allow_write=True,
                allow_execution=True,
                allow_memory_write=False,
                allow_external_mutation=True,
            )
        }
        self.assertTrue(EXTERNAL_MUTATION_TOOLS.issubset(exposed))

    def test_self_inspection_schemas_require_explicit_diagnosis_intent(self):
        agent, _client = self.make_agent([])
        for name in SELF_INSPECTION_TOOLS:
            agent.toolbox.schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": "test",
                    "parameters": {"type": "object", "properties": {}},
                },
            })
        common = dict(
            research_mode=False,
            web_tainted=False,
            local_tainted=False,
            allow_write=False,
            allow_execution=False,
            allow_memory_write=False,
        )
        hidden = {
            schema["function"]["name"]
            for schema in agent._schemas_for_state(**common)
        }
        exposed = {
            schema["function"]["name"]
            for schema in agent._schemas_for_state(
                **common, allow_self_inspection=True
            )
        }
        self.assertTrue(SELF_INSPECTION_TOOLS.isdisjoint(hidden))
        self.assertTrue(SELF_INSPECTION_TOOLS.issubset(exposed))
        for prompt in (
            "Run a self-diagnosis.",
            "Inspect your own runtime source.",
            "Diagnose yourself and explain the failing tests.",
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(_requires_self_diagnosis(prompt))
        for prompt in (
            "Inspect this Python project's source.",
            "Run the application tests.",
            "Explain self-testing in software engineering.",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(_requires_self_diagnosis(prompt))

    def test_session_search_context_cannot_authorize_a_memory_mutation(self):
        toolbox = FakeToolBox()
        toolbox.NAMES = (*toolbox.NAMES, "session_search")
        toolbox.schemas.append({
            "type": "function",
            "function": {
                "name": "session_search",
                "description": "test",
                "parameters": {"type": "object", "properties": {}},
            },
        })
        agent, _client = self.make_agent([
            FakeResponse(tool_calls=[tool_call(
                "session_search", {"query": "answer style"}
            )]),
            FakeResponse(tool_calls=[tool_call(
                "remember", {"content": "remember this preference for later"}
            )]),
            FakeResponse(content="I searched prior sessions without persisting them."),
        ], toolbox)

        result = agent.run("Remember this answer-style preference for later")

        self.assertEqual(result.status, "complete")
        self.assertEqual(
            toolbox.calls, [("session_search", {"query": "answer style"})]
        )

    def test_deploy_intent_requires_an_affirmative_action_and_concrete_target(self):
        positive = (
            "Deploy my app to Vercel production.",
            "Redeploy the preview site on Vercel.",
            "Vercel deploy the application project.",
            "Publish my website to Vercel.",
            "Deploy the frontend service to staging.",
        )
        negative = (
            "Fix the typo in the deployment notes.",
            "Why does my deploy script fail?",
            "Inspect the Vercel deployment logs.",
            "Check the production deployment status.",
            "Fix the deployment config for my app.",
            "Explain how app deployment works.",
            "Do not deploy my app to Vercel production.",
            "I don't want you to deploy my app to Vercel production.",
            "Should I deploy my app to Vercel production?",
            "Explain how to deploy my app to Vercel production.",
        )
        for prompt in positive:
            with self.subTest(prompt=prompt):
                self.assertTrue(_requires_external_mutation(prompt))
        for prompt in negative:
            with self.subTest(prompt=prompt):
                self.assertFalse(_requires_external_mutation(prompt))

    def test_anaphoric_external_mutation_followups_require_affirmative_imperative(self):
        positive = (
            "deploy it",
            "please deploy this",
            "push it",
            "upload it",
            "upload this file",
            "download it",
            "authenticate it",
            "authorize it",
            "publish it",
            "redeploy it",
        )
        negative = (
            "don't deploy it",
            "should I deploy it?",
            "explain how to deploy it",
            "please explain how to deploy it",
            "don't download it",
            "should I authorize it?",
            "explain how to upload this file",
        )
        for prompt in positive:
            with self.subTest(prompt=prompt):
                self.assertTrue(_requires_external_mutation(prompt))
        for prompt in negative:
            with self.subTest(prompt=prompt):
                self.assertFalse(_requires_external_mutation(prompt))

        self.assertTrue(_requires_external_mutation(
            "create it",
            prior_context="Create a project folder in Google Drive.",
        ))
        self.assertTrue(_requires_external_mutation(
            "please create this",
            prior_context="We need a new repository on GitHub.",
        ))
        self.assertFalse(_requires_external_mutation("create it"))
        self.assertFalse(_requires_external_mutation(
            "create it",
            prior_context="Create a local workspace folder.",
        ))
        self.assertFalse(_requires_external_mutation(
            "don't create it",
            prior_context="Create a project folder in Google Drive.",
        ))
        self.assertFalse(_requires_external_mutation(
            "explain how to create it",
            prior_context="We need a new repository on GitHub.",
        ))

    def test_connector_and_social_mutations_require_affirmative_intent(self):
        positive = (
            "Post this update to my X account.",
            "Schedule the finished video on my YouTube channel.",
            "Call the weather connector now.",
            "Use the CRM API to create this record.",
            "yes, go ahead and post it",
            "Organize my connected Google Drive files by project.",
            "Organize the files in Google Drive.",
        )
        negative = (
            "Explain how the X API creates posts.",
            "Do not post this to my X account.",
            "Should I upload this to YouTube?",
            "Review the connector documentation.",
            "Do not clean up my Google Drive.",
            "Clean up my C drive.",
            "Maybe clean up my C drive; where do I start with today's tasks?",
            "Rewrite this to sound warmer: 'Send the report when finished.'",
            "Rewrite this politely: send the report when you finish",
            'Rephrase this sentence: "Email the document to the account owner."',
            "Translate `Post this update to my X account.` into Spanish.",
        )
        for prompt in positive:
            with self.subTest(prompt=prompt):
                self.assertTrue(_requires_external_mutation(prompt))
        for prompt in negative:
            with self.subTest(prompt=prompt):
                self.assertFalse(_requires_external_mutation(prompt))
        self.assertTrue(_requires_external_mutation(
            "Rewrite this politely, then send the report."
        ))

    def test_approval_retry_followups_require_exact_affirmative_context(self):
        positive = (
            "retry the task",
            "ok retry",
            "go ahead",
            "okay, please retry that",
            "yes, go ahead and retry the task",
        )
        for prompt in positive:
            with self.subTest(prompt=prompt):
                self.assertTrue(_requires_external_mutation(
                    prompt,
                    approval_retry_context=True,
                ))

        negative = (
            "don't retry",
            "do not go ahead",
            "should I retry?",
            "can you retry it?",
            "explain how to retry",
            "go ahead and explain how to deploy it",
            "yes",
            "approve it",
        )
        for prompt in negative:
            with self.subTest(prompt=prompt):
                self.assertFalse(_requires_external_mutation(
                    prompt,
                    approval_retry_context=True,
                ))

        self.assertFalse(_requires_external_mutation("retry the task"))
        self.assertTrue(_requires_external_mutation("yes, go ahead and deploy it"))

    def test_generic_retry_exposes_external_tools_only_for_matching_approval(self):
        cases = (
            ("vercel_deploy", "retry the task", True, False),
            ("vercel_deploy", "ok retry", True, True),
            ("vercel_deploy", "go ahead", True, False),
            ("computer_read_file", "retry the task", False, False),
        )
        for index, (tool, followup, expected, approve) in enumerate(cases):
            with self.subTest(tool=tool, followup=followup):
                conversation_id = self.memory.new_conversation(f"approval retry {index}")
                scope = f"conversation:{conversation_id}"
                _allowed, approval_id = self.memory.authorize_or_request(
                    "publish_external" if tool == "vercel_deploy" else "access_private_files",
                    json.dumps({"tool": tool, "arguments": {}}, sort_keys=True),
                    "test approval",
                    approval_scope=scope,
                )
                if approve:
                    self.assertTrue(self.memory.decide_approval(approval_id, True))
                self.memory.add_message(conversation_id, "user", "Deploy the app to Vercel.")
                self.memory.add_message(
                    conversation_id,
                    "assistant",
                    f"Incomplete: Approval request #{approval_id} is waiting for an operator decision. "
                    "Review it before retrying.",
                )
                agent, client = self.make_agent([
                    FakeResponse(content="I will continue only if the capability is available.")
                ])
                for name in EXTERNAL_MUTATION_TOOLS:
                    agent.toolbox.schemas.append({
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": "test",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    })

                agent.run(followup, conversation_id=conversation_id)
                exposed = {
                    schema["function"]["name"] for schema in client.requests[0]["tools"]
                }
                self.assertEqual(expected, EXTERNAL_MUTATION_TOOLS.issubset(exposed))

    def test_generic_retry_rejects_out_of_range_approval_id_without_crashing(self):
        conversation_id = self.memory.new_conversation("invalid approval retry")
        self.memory.add_message(
            conversation_id,
            "assistant",
            "Incomplete: Approval request #10000000000000000000 is waiting for an operator "
            "decision. Review it before retrying.",
        )
        agent, client = self.make_agent([FakeResponse(content="No external action was taken.")])
        for name in EXTERNAL_MUTATION_TOOLS:
            agent.toolbox.schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": "test",
                    "parameters": {"type": "object", "properties": {}},
                },
            })

        agent.run("retry the task", conversation_id=conversation_id)

        exposed = {schema["function"]["name"] for schema in client.requests[0]["tools"]}
        self.assertTrue(EXTERNAL_MUTATION_TOOLS.isdisjoint(exposed))

    def test_anaphoric_followup_exposes_external_mutation_tools_in_continuing_chat(self):
        conversation_id = self.memory.new_conversation("prepare release")
        self.memory.add_message(conversation_id, "user", "The preview app is ready.")
        self.memory.add_message(conversation_id, "assistant", "The preview is ready to deploy.")
        agent, client = self.make_agent([FakeResponse(content="I will request approval first.")])
        for name in EXTERNAL_MUTATION_TOOLS:
            agent.toolbox.schemas.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": "test",
                    "parameters": {"type": "object", "properties": {}},
                },
            })

        agent.run("please deploy this", conversation_id=conversation_id)

        exposed = {
            schema["function"]["name"] for schema in client.requests[0]["tools"]
        }
        self.assertTrue(EXTERNAL_MUTATION_TOOLS.issubset(exposed))

    def test_drive_and_github_followups_expose_external_mutation_tools(self):
        cases = (
            ("The report is stored in Google Drive.", "download it"),
            ("We need a project folder in Google Drive.", "create it"),
            ("We need a new repository on GitHub.", "please create this"),
        )
        for index, (context, followup) in enumerate(cases):
            with self.subTest(followup=followup):
                conversation_id = self.memory.new_conversation(f"external followup {index}")
                self.memory.add_message(conversation_id, "user", context)
                self.memory.add_message(conversation_id, "assistant", "Ready for your next instruction.")
                agent, client = self.make_agent([
                    FakeResponse(content="I will request approval first.")
                ])
                for name in EXTERNAL_MUTATION_TOOLS:
                    agent.toolbox.schemas.append({
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": "test",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    })

                agent.run(followup, conversation_id=conversation_id)

                exposed = {
                    schema["function"]["name"]
                    for schema in client.requests[0]["tools"]
                }
                self.assertTrue(EXTERNAL_MUTATION_TOOLS.issubset(exposed))

    def test_contextual_followup_keeps_prior_user_with_long_assistant_reply(self):
        conversation_id = self.memory.new_conversation("ZIP context")
        self.memory.add_message(conversation_id, "user", "zip is 10001")
        self.memory.add_message(
            conversation_id,
            "assistant",
            "A" * 5000 + " I can check that location.",
        )
        agent, client = self.make_agent([
            FakeResponse(content="You said ZIP 10001."),
        ])

        result = agent.run(
            "What ZIP did I mention a moment ago?",
            conversation_id=conversation_id,
        )

        self.assertEqual(result.status, "complete")
        sent = client.requests[0]["messages"]
        prior_user = [
            item for item in sent
            if item.get("role") == "user" and "10001" in item.get("content", "")
        ]
        self.assertEqual(len(prior_user), 1)
        prior_index = sent.index(prior_user[0])
        self.assertEqual(sent[prior_index + 1]["role"], "assistant")
        self.assertGreater(len(sent[prior_index + 1]["content"]), 1_000)
        self.assertEqual(sent[-1]["content"], "What ZIP did I mention a moment ago?")

    def test_contextual_followup_keeps_complete_recent_list(self):
        conversation_id = self.memory.new_conversation("daily plan")
        self.memory.add_message(
            conversation_id,
            "user",
            "Review notes, water plants, take a walk, organize downloads, and prepare lunch. Where should I start?",
        )
        previous_answer = (
            "Here is the plan: 1. Review notes first. 2. Water plants second. "
            "3. Take a walk third. 4. Organize downloads later. 5. Prepare lunch. "
            + "Reasoning and practical details. " * 30
        )
        self.memory.add_message(conversation_id, "assistant", previous_answer)
        agent, client = self.make_agent([
            FakeResponse(content="Pick emails and gym."),
        ])

        result = agent.run(
            "I only have energy for two things. Pick them and tell me why.",
            conversation_id=conversation_id,
        )

        self.assertEqual(result.status, "complete")
        sent = client.requests[0]["messages"]
        assistant_history = [
            item["content"] for item in sent[:-1] if item.get("role") == "assistant"
        ]
        self.assertEqual(len(assistant_history), 1)
        self.assertIn("5. Prepare lunch", assistant_history[0])
        self.assertGreater(len(assistant_history[0]), 1_000)

    def test_contextual_software_build_followup_inherits_prior_app_and_exposes_effect_tools(self):
        conversation_id = self.memory.new_conversation("inventory scheduling app proposal")
        self.memory.add_message(
            conversation_id,
            "user",
            "Research the best focused business app we should build.",
        )
        self.memory.add_message(
            conversation_id,
            "assistant",
            "The best option is an inventory scheduling app demo with item reservations, "
            "availability windows, transfer records, condition checks, and notes.",
        )

        class LaunchToolBox(FakeToolBox):
            NAMES = (*FakeToolBox.NAMES, "launch_artifact", "http_health")

        toolbox = LaunchToolBox()
        agent, client = self.make_agent([
            FakeResponse(tool_calls=[
                tool_call("list_files", {"path": "."}),
                tool_call("write_file", {
                    "path": "app.py",
                    "content": "print('inventory scheduling app demo')",
                }),
                tool_call("run_process", {
                    "program": "python",
                    "arguments": ["-m", "unittest"],
                }),
            ]),
            FakeResponse(content="Implemented and verified."),
        ], toolbox=toolbox)

        prompt = "now just do it"
        result = agent.run(prompt, conversation_id=conversation_id)

        self.assertEqual(result.status, "complete")
        self.assertEqual(client.requests[0]["model"], "qwen3-coder:30b")
        offered = {
            schema["function"]["name"] for schema in client.requests[0]["tools"]
        }
        self.assertTrue({"write_file", "launch_artifact", "http_health"}.issubset(offered))
        sent_text = "\n".join(
            str(message.get("content") or "")
            for message in client.requests[0]["messages"]
        )
        self.assertIn("inventory scheduling app demo", sent_text)
        self.assertIn("direct instruction to build", sent_text)
        self.assertNotIn("Launch it as well", sent_text)
        self.assertIn("write_file", [name for name, _arguments in toolbox.calls])

    def test_contextual_build_handoff_includes_resolved_prior_product_brief(self):
        conversation_id = self.memory.new_conversation("contextual specialist handoff")
        self.memory.add_message(
            conversation_id,
            "user",
            "Let’s build a focused inventory scheduling application demo.",
        )
        self.memory.add_message(
            conversation_id,
            "assistant",
            "I can build the inventory scheduling app with reusable item records, reservations, "
            "transfer history, condition checks, and notes.",
        )
        (self.data_dir / "worker.heartbeat").write_text(
            f"{time.time():.6f} 123 worker:test\n",
            encoding="utf-8",
        )
        toolbox = DelegatingFakeToolBox()
        agent, _client = self.make_agent([
            FakeResponse(tool_calls=[
                tool_call("list_files", {"path": "."}),
                tool_call("write_file", {
                    "path": "app.py",
                    "content": "print('inventory scheduling app demo')",
                }),
                tool_call("run_process", {
                    "program": "python",
                    "arguments": ["-m", "unittest"],
                }),
            ]),
            FakeResponse(content="Implemented and verified."),
        ], toolbox=toolbox)

        result = agent.run("now just do it", conversation_id=conversation_id)

        self.assertEqual(result.status, "complete")
        delegated = next(
            arguments for name, arguments in toolbox.calls
            if name == "delegate_specialist"
        )
        self.assertIn("Resolved foreground task", delegated["task"])
        self.assertIn("inventory scheduling", delegated["task"])
        self.assertEqual(specialist_for_prompt(delegated["task"]).key, "coding")

    def test_contextual_software_build_followup_requires_direct_request_and_app_context(self):
        app_context = [{
            "role": "assistant",
            "content": "I recommend a focused inventory scheduling app demo.",
        }]
        for prompt in (
            "okay build it and launch it",
            "can you make that app now",
            "please build the strongest app concept and show me the result",
            "go ahead and build the complete demo",
            "yea do it all",
            "do it all yourself now",
            "just do it now",
            "please build the whole app yourself",
            "now just do it",
            "whatever man, do it however you think is best",
            "alright then handle the whole project",
            "do it however you think is best",
            "finish the whole thing yourself",
            "yes, go ahead and finish it yourself",
            "can you do it all?",
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(_is_contextual_software_build_request(prompt, app_context))
        for prompt in (
            "don't build it",
            "yeah don't do it",
            "do not finish it",
            "cancel it",
            "stop and do not continue",
            "should I build it?",
            "should I do it?",
            "when can you do it?",
            "did you do it?",
            "do you think I should do it?",
            "explain how to build it",
            "explain how to do it",
            (
                "please research accessible museum display systems and summarize the "
                "requirements in a document"
            ),
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(_is_contextual_software_build_request(prompt, app_context))
        self.assertFalse(_is_contextual_software_build_request(
            "okay build it",
            [{"role": "assistant", "content": "Dinner sounds good."}],
        ))

    def test_contextual_build_survives_repeated_tool_blocker_replies(self):
        recent = [
            {"role": "user", "content": "yea do it all"},
            {
                "role": "assistant",
                "content": "I’m ready to build the whole thing, but this session is read-only. "
                "Enable workspace write access so I can complete the sample-dashboard project.",
            },
            {"role": "user", "content": "do it all yourself now"},
            {
                "role": "assistant",
                "content": "I can’t modify or run the project because execution tools are unavailable.",
            },
            {"role": "user", "content": "please build the whole app yourself"},
            {
                "role": "assistant",
                "content": "I’m blocked by read-only project access and unavailable tools.",
            },
            {"role": "user", "content": "now just do it"},
            {
                "role": "assistant",
                "content": "Enable workspace write and command access, then I can proceed.",
            },
        ]
        self.assertTrue(_is_contextual_software_build_request("now just do it", recent))

    def test_profiles_select_thinking_and_context_explicitly(self):
        agent, client = self.make_agent([
            FakeResponse(content="fast"),
            FakeResponse(content="reasoning"),
            FakeResponse(content="coding"),
            FakeResponse(content="gpt reasoning"),
        ])
        messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "test"}]

        agent._chat(messages, [], Route("fast", "qwen3.5:9b", "test"))
        agent._chat(messages, [], Route("reasoning", "qwen3:30b", "test"))
        agent._chat(messages, [], Route("coding", "qwen3-coder:30b", "test"))
        agent._chat(messages, [], Route("reasoning", "gpt-oss:20b", "test"))

        self.assertEqual(
            [(request["think"], request["context_length"]) for request in client.requests],
            [(False, 16384), (True, 16384), (False, 16384), ("high", 16384)],
        )

    def test_reasoning_thinking_can_be_disabled_for_direct_answer_models(self):
        agent, _client = self.make_agent([])
        agent.config = replace(agent.config, reasoning_thinking=False)

        self.assertFalse(agent._think_for(Route("reasoning", "gemma4:12b-it-qat", "test")))
        self.assertFalse(agent._think_for(Route("reasoning", "gpt-oss:20b", "test")))

    def test_deep_profile_is_bounded_and_unloads_after_each_request(self):
        agent, client = self.make_agent([FakeResponse(content="deep")])
        agent.config = replace(
            agent.config,
            deep_context_length=4096,
            ollama_deep_keep_alive="0",
        )
        messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "test"}]

        agent._chat(messages, [], Route("deep", "qwen3-coder:30b", "manual deep profile"))

        self.assertEqual(client.requests[0]["context_length"], 4096)
        self.assertEqual(client.requests[0]["think"], False)
        self.assertEqual(client.requests[0]["keep_alive"], "0")

    def test_cloud_profiles_do_not_inherit_small_local_context_limits(self):
        agent, _client = self.make_agent([])
        agent.config = replace(agent.config, deep_context_length=4096)

        for model in (
            "openai:gpt-5.6-sol",
            "anthropic:claude-sonnet-5",
            "codex-cli:auto",
            "claude-cli:sonnet",
        ):
            with self.subTest(model=model):
                self.assertEqual(
                    agent._context_length_for(Route("deep", model, "cloud deep")),
                    16384,
                )

    def test_tight_context_compaction_preserves_constitution_memory_and_latest_user(self):
        agent, _client = self.make_agent([])
        constitution = "CONSTITUTION_SENTINEL\n" + "safe\n" * 80
        prompt = (
            "verbose preamble\n" + "x" * 9000
            + '<trusted_constitution sha256="abc">\n'
            + constitution
            + "</trusted_constitution>\n"
            + "<untrusted_memory_records>MEMORY_SENTINEL</untrusted_memory_records>\n"
            + "<temporal_claims>CLAIM_SENTINEL</temporal_claims>\n"
            + "<matched_lessons>LESSON_SENTINEL</matched_lessons>\n"
            + "<persistent_self_context>SELF_SENTINEL</persistent_self_context>\n"
        )

        compacted = agent._compact_messages(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "LATEST_USER_SENTINEL"},
            ],
            4096,
        )
        rendered = compacted[0]["content"]

        self.assertIn(constitution, rendered)
        for sentinel in (
            "MEMORY_SENTINEL", "CLAIM_SENTINEL", "LESSON_SENTINEL", "SELF_SENTINEL"
        ):
            self.assertIn(sentinel, rendered)
        for tag in (
            "trusted_constitution", "untrusted_memory_records", "temporal_claims",
            "matched_lessons", "persistent_self_context",
        ):
            self.assertEqual(rendered.count(f"<{tag}"), 1)
            self.assertEqual(rendered.count(f"</{tag}>"), 1)
        self.assertEqual(compacted[-1]["content"], "LATEST_USER_SENTINEL")
        self.assertLessEqual(len(rendered), 6500)

    def test_tight_context_pins_real_json_context_and_coherent_current_tool_groups(self):
        agent, _client = self.make_agent([])
        agent.config = replace(
            agent.config,
            workspace=(
                self.workspace
                / ("long-public-install-and-workspace-segment-" * 2)
                / ("nested-project-segment-" * 2)
            ),
        )
        base_prompt = agent.system_prompt("Explain the current observability state")
        constitution = agent._prompt_tag_block(base_prompt, "trusted_constitution")

        def replace_json_block(content, tag, value):
            replacement = f"<{tag}>{_prompt_json(value, 6000)}</{tag}>"
            revised, count = re.subn(
                rf"<{tag}(?:\s[^>]*)?>.*?</{tag}>",
                lambda _match: replacement,
                content,
                count=1,
                flags=re.DOTALL,
            )
            return revised if count else f"{revised}\n{replacement}\n"

        prompt = replace_json_block(
            base_prompt,
            "persistent_self_context",
            {"sentinel": "SELF_CONTEXT_SENTINEL", "detail": "s" * 3500},
        )
        prompt = replace_json_block(
            prompt,
            "temporal_claims",
            [{"value": "CURRENT_CLAIM_SENTINEL", "detail": "c" * 3500}],
        )
        prompt = replace_json_block(
            prompt,
            "untrusted_memory_records",
            [{"content": "CURRENT_MEMORY_SENTINEL", "detail": "m" * 3500}],
        )
        prompt = "verbose preamble\n" + "x" * 20_000 + prompt
        tool_call = {
            "function": {"name": "read_file", "arguments": {"path": "notes.txt"}}
        }

        for context_length in (4096, 8192):
            for user_padding in (500, 4000):
                current_user = "CURRENT_TASK_USER_SENTINEL " + "u" * user_padding
                compacted = agent._compact_messages(
                    [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": "older turn"},
                        {"role": "assistant", "content": "older answer"},
                        {"role": "user", "content": current_user},
                        {"role": "assistant", "content": "", "tool_calls": [tool_call]},
                        {"role": "tool", "tool_name": "read_file", "content": "tool result"},
                    ],
                    context_length,
                )
                rendered = compacted[0]["content"]
                self.assertEqual(
                    agent._prompt_tag_block(rendered, "trusted_constitution"),
                    constitution,
                )
                self.assertIn("SELF_CONTEXT_SENTINEL", rendered)
                self.assertIn("CURRENT_CLAIM_SENTINEL", rendered)
                self.assertIn("CURRENT_MEMORY_SENTINEL", rendered)
                self.assertTrue(any(
                    "CURRENT_TASK_USER_SENTINEL" in str(message.get("content") or "")
                    for message in compacted
                    if message.get("role") == "user"
                ))
                for tag in (
                    "trusted_constitution",
                    "identity_contract",
                    "agent_hierarchy_contract",
                    "persistent_self_context",
                    "temporal_claims",
                    "untrusted_memory_records",
                ):
                    self.assertEqual(rendered.count(f"<{tag}"), 1)
                    self.assertEqual(rendered.count(f"</{tag}>"), 1)
                for tag in (
                    "persistent_self_context",
                    "temporal_claims",
                    "untrusted_memory_records",
                ):
                    block = agent._prompt_tag_block(rendered, tag)
                    self.assertTrue(block)
                    inner = block.split(">", 1)[1].rsplit("</", 1)[0]
                    json.loads(inner)

                pending_calls = 0
                current_turn_started = False
                for message in compacted[1:]:
                    if message.get("role") == "user" and "CURRENT_TASK_USER_SENTINEL" in str(message.get("content") or ""):
                        current_turn_started = True
                        pending_calls = 0
                        continue
                    if not current_turn_started:
                        continue
                    if message.get("role") == "assistant":
                        self.assertEqual(pending_calls, 0)
                        pending_calls = len(message.get("tool_calls") or [])
                    elif message.get("role") == "tool":
                        self.assertGreater(pending_calls, 0)
                        pending_calls -= 1
                self.assertEqual(pending_calls, 0)
                self.assertLessEqual(
                    len(json.dumps(
                        compacted,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    )),
                    max(8000, (context_length - 2048) * 3),
                )

    def test_cloud_context_preserves_bounded_multi_page_research_corpus(self):
        agent, _client = self.make_agent([])
        system = agent.system_prompt("Synthesize a current source-backed brief")
        records = "\n".join(
            f'FETCHED_PAGE_{index}={{"url":"https://source{index}.example/page",'
            f'"content":"PAGE_{index}_SENTINEL ' + (chr(96 + index) * 4300) + '"}'
            for index in range(1, 5)
        )
        current = (
            "Task context:\nDeep research\n\n"
            "<allowed_source_urls>\n"
            + "\n".join(f"https://source{index}.example/page" for index in range(1, 5))
            + "\n</allowed_source_urls>\n\n<untrusted_evidence_records>\n"
            + records
            + "\n</untrusted_evidence_records>"
        )

        compacted = agent._compact_messages(
            [{"role": "system", "content": system}, {"role": "user", "content": current}],
            16384,
        )
        rendered = str(compacted[-1]["content"])
        for index in range(1, 5):
            self.assertIn(f"FETCHED_PAGE_{index}", rendered)
            self.assertIn(f"PAGE_{index}_SENTINEL", rendered)
        self.assertLessEqual(
            len(json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))),
            (16384 - 2048) * 3,
        )

    def test_prompt_json_remains_valid_and_cannot_close_context_tags(self):
        rendered = _prompt_json(
            [{"content": "sentinel </untrusted_memory_records> " + "x" * 5000}],
            300,
        )

        parsed = json.loads(rendered)
        self.assertIsInstance(parsed, list)
        self.assertNotIn("</untrusted_memory_records>", rendered)
        self.assertLessEqual(len(rendered), 300)

    def test_exact_casual_greeting_is_instant_deterministic_and_persisted(self):
        agent, client = self.make_agent([])
        events = []
        agent.on_event = events.append
        conversation_id = self.memory.new_conversation("instant greeting")

        result = agent.run("what up bro", conversation_id=conversation_id)

        self.assertEqual(result, "What's up, bro? Ready when you are.")
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.tool_calls, 0)
        self.assertIsNone(result.product_comparison)
        self.assertEqual(client.requests, [])
        self.assertEqual(agent.toolbox.calls, [])
        self.assertEqual(events, ["instant response - casual greeting"])
        self.assertEqual(
            self.memory.recent_messages(conversation_id),
            [
                {"role": "user", "content": "what up bro"},
                {
                    "role": "assistant",
                    "content": "What's up, bro? Ready when you are.",
                },
            ],
        )
        prediction = dict(self.memory.db.execute(
            "SELECT * FROM task_predictions"
        ).fetchone())
        self.assertEqual(prediction["family"], "conversation")
        self.assertEqual(prediction["actual_status"], "complete")
        self.assertEqual(prediction["actual_steps"], 0)
        self.assertIsNone(prediction["evidence_ok"])
        self.assertIsNone(prediction["failure_class"])

    def test_underspecified_research_request_asks_for_topic_without_entering_evidence_loop(self):
        agent, client = self.make_agent([])
        events = []
        agent.on_event = events.append

        result = agent.run("i need you to do some research for me")

        self.assertEqual(result.status, "complete")
        self.assertIn("What topic or question", str(result))
        self.assertEqual(client.requests, [])
        self.assertEqual(agent.toolbox.calls, [])
        self.assertEqual(events, ["clarification requested - research topic missing"])

    def test_product_recommendation_is_current_web_work_not_future_promise(self):
        pages = [
            {
                "title": "Acme View 27 Monitor",
                "url": "https://shop.example/acme-view-27",
                "content": (
                    "Acme View 27 Monitor $429.99 USD In stock Acme Store Acme Labs "
                    "27-inch 4K IPS silver USB-C 90W power delivery height-adjustable stand monitor"
                ),
            },
            {
                "title": "Bravo Canvas 4K Monitor",
                "url": "https://maker.example/bravo-canvas-4k",
                "content": (
                    "Bravo Canvas 4K Monitor $489 USD Available Bravo Direct Bravo "
                    "27-inch 4K IPS silver USB-C 90W power delivery height-adjustable stand monitor"
                ),
            },
            {
                "title": "Cobalt Studio 27 Monitor",
                "url": "https://retail.example/cobalt-studio-27",
                "content": (
                    "Cobalt Studio 27 Monitor $559 USD In stock Retail Example Cobalt "
                    "27-inch 4K IPS silver USB-C 90W power delivery height-adjustable stand monitor"
                ),
            },
        ]
        payload = {
            "answer": (
                "I'll shop for these and send the product links when I'm done."
            ),
            "ranking": "Acme first for value, Bravo second, Cobalt third.",
            "products": [
                {
                    "name": page["title"],
                    "source_url": page["url"],
                    "source_kind": "seller" if index != 1 else "manufacturer",
                    "seller": ["Acme Store", "Bravo Direct", "Retail Example"][index],
                    "manufacturer": ["Acme Labs", "Bravo", "Cobalt"][index],
                    "price_text": ["$429.99", "$489", "$559"][index],
                    "currency": "USD",
                    "availability": ["In stock", "Available", "In stock"][index],
                    "key_specs": [
                        "27-inch",
                        "4K",
                        "IPS",
                        "silver",
                        "USB-C",
                        "90W power delivery",
                        "height-adjustable",
                        "stand",
                    ],
                    "why_fit": "Matches the stated panel, connection, charging, stand, color, and budget requirements.",
                    "tradeoff": "The fetched page does not provide a verified long-term durability rating.",
                }
                for index, page in enumerate(pages)
            ],
        }
        toolbox = FakeToolBox(verified_pages=pages)
        agent, client = self.make_agent(
            [FakeResponse(content=json.dumps(payload))],
            toolbox=toolbox,
        )

        result = agent.run(
            "Find me a silver 27-inch 4K IPS monitor with USB-C, 90W power "
            "delivery, and a height-adjustable stand under $600. Send product links."
        )

        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual([name for name, _ in toolbox.calls], ["web_search", "web_search"])
        self.assertEqual(len(result.product_comparison["products"]), 3)
        self.assertEqual(client.requests[0]["tools"], [])
        self.assertIsNotNone(client.requests[0]["response_format"])
        self.assertNotRegex(str(result).casefold(), r"\b(?:later|when i(?:'|’)m done)\b")

    def test_product_search_preserves_compound_requirements_and_rejects_utility_pages(self):
        prompt = (
            "Find a silver 27-inch 4K IPS monitor with USB-C, 90W power delivery, "
            "and a height-adjustable stand under $600."
        )
        queries = _product_search_queries(prompt)

        self.assertEqual(len(queries), 2)
        self.assertTrue(all('"27-inch"' in query for query in queries))
        self.assertTrue(all('"height-adjustable"' in query for query in queries))
        self.assertTrue(all("monitor" in query for query in queries))
        generic = {
            "https://support.microsoft.com/display-settings": {
                "title": "Change display settings",
                "content": "Windows can arrange a connected monitor and adjust display scaling.",
            }
        }
        listing = {
            "https://shop.example/product/view-27": {
                "title": "View 27 4K monitor",
                "content": "$429 USD In stock silver 27-inch 4K IPS USB-C 90W height-adjustable stand",
            }
        }

        self.assertEqual(_product_relevant_urls(prompt, generic), set())
        self.assertEqual(
            _product_relevant_urls(prompt, listing),
            {"https://shop.example/product/view-27"},
        )

    def test_product_run_without_verified_comparison_is_incomplete(self):
        page = {
            "title": "Acme View 27 Monitor",
            "url": "https://shop.example/product/acme-view-27",
            "content": (
                "Acme View 27 Monitor $429.99 USD In stock silver 27-inch 4K "
                "IPS USB-C 90W height-adjustable stand"
            ),
        }
        toolbox = FakeToolBox(verified_pages=[page])
        payload = {
            "answer": "The requested three products remain incomplete.",
            "ranking": "",
            "products": [],
        }
        agent, _client = self.make_agent(
            [FakeResponse(content=json.dumps(payload))],
            toolbox=toolbox,
        )

        result = agent.run(
            "Find three current silver 27-inch 4K IPS USB-C monitors with adjustable "
            "stands under $600."
        )

        self.assertEqual(result.status, "incomplete")
        self.assertIn("No verified product comparison", result.reason)
        self.assertIsNone(result.product_comparison)

    def test_product_status_followup_resumes_saved_requirements_and_searches(self):
        initial = (
            "Shop for a silver 27-inch 4K USB-C monitor with a height-adjustable "
            "stand under $600 and give me current links."
        )
        conversation_id = self.memory.new_conversation("shopping")
        self.memory.add_message(conversation_id, "user", initial)
        self.memory.add_message(
            conversation_id,
            "assistant",
            "I will shop for those and send a link when I am done.",
        )
        pages = [
            {
                "title": f"Acme View {index} Monitor",
                "url": f"https://shop.example/acme-view-{index}",
                "content": (
                    f"Acme View {index} Monitor ${429 + index}.99 USD In stock "
                    "Acme Store Acme Labs silver 27-inch 4K USB-C height-adjustable stand"
                ),
            }
            for index in range(1, 4)
        ]
        payload = {
            "answer": (
                "Done—I checked the current listings and found three matching options: "
                "https://shop.example/acme-view-1"
            ),
            "ranking": "The three Acme listings are verified current matches.",
            "products": [
                {
                    "name": page["title"], "source_url": page["url"],
                    "source_kind": "seller", "seller": "Acme Store",
                    "manufacturer": "Acme Labs",
                    "price_text": f"${429 + index}.99",
                    "currency": "USD", "availability": "In stock",
                    "key_specs": ["silver", "27-inch", "4K", "USB-C", "height-adjustable", "stand"],
                    "why_fit": "Matches the saved display and connectivity requirements.",
                    "tradeoff": "No 90W charging claim was verified.",
                }
                for index, page in enumerate(pages, 1)
            ],
        }
        toolbox = FakeToolBox(verified_pages=pages)
        agent, _client = self.make_agent(
            [FakeResponse(content=json.dumps(payload))],
            toolbox=toolbox,
        )

        result = agent.run("are you done?", conversation_id=conversation_id)

        self.assertEqual(result.status, "complete", result.reason)
        self.assertEqual([name for name, _ in toolbox.calls], ["web_search", "web_search"])
        queries = " ".join(arguments["query"] for _name, arguments in toolbox.calls)
        self.assertIn("monitor", queries)
        self.assertIn('"27-inch"', queries)
        self.assertIn("silver", queries)
        self.assertIn("4k", queries.casefold())
        self.assertIn("usb-c", queries.casefold())
        self.assertIn("Done", str(result))
        self.assertEqual(
            self.memory.recent_messages(conversation_id)[-2]["content"],
            "are you done?",
        )

    def test_product_intent_and_followup_matching_are_general_and_bounded(self):
        self.assertTrue(_requires_web("Which office chair should I buy under $500?"))
        self.assertTrue(_requires_web("Compare current prices and availability for this monitor."))
        self.assertFalse(_requires_web("What do you think makes a chair comfortable?"))
        recent = [
            {"role": "user", "content": "Recommend a camera to buy under $900."},
            {"role": "assistant", "content": "I will look."},
        ]
        resumed = _contextual_product_research_target("what's the update?", recent)
        self.assertIn("Recommend a camera", resumed)
        self.assertIsNone(_contextual_product_research_target("how are you?", recent))

    def test_verified_product_comparison_rejects_unfetched_and_duplicate_cards(self):
        page = {
            "url": "https://shop.example/safe",
            "title": "Safe Product",
            "content": "Safe Product $20 USD In stock Example Seller Example Maker blue",
        }
        raw = {
            "ranking": "<script>alert(1)</script>",
            "products": [
                {
                    "name": "Safe Product", "source_url": page["url"],
                    "source_kind": "seller", "seller": "Example Seller",
                    "manufacturer": "Example Maker", "price_text": "$20",
                    "currency": "USD", "availability": "In stock",
                    "key_specs": ["blue", "invented feature"],
                    "why_fit": "<img src=x onerror=alert(1)>", "tradeoff": "Unknown",
                },
                {
                    "name": "Safe Product", "source_url": page["url"],
                    "source_kind": "seller", "seller": None, "manufacturer": None,
                    "price_text": None, "currency": None, "availability": None,
                    "key_specs": [], "why_fit": "duplicate", "tradeoff": "duplicate",
                },
                {
                    "name": "Evil", "source_url": "javascript:alert(1)",
                    "source_kind": "other", "seller": None, "manufacturer": None,
                    "price_text": None, "currency": None, "availability": None,
                    "key_specs": [], "why_fit": "bad", "tradeoff": "bad",
                },
            ],
        }

        result = _verified_product_comparison(raw, {page["url"]: page})

        self.assertEqual(len(result["products"]), 1)
        self.assertEqual(result["products"][0]["key_specs"], ["blue"])
        self.assertIsNone(result["products"][0]["image_url"])

    def test_prediction_failure_is_nonfatal_and_state_resets(self):
        agent, _client = self.make_agent([])
        with patch.object(
            self.memory,
            "record_prediction",
            side_effect=RuntimeError("instrumentation unavailable"),
        ):
            result = agent.run("hey jarvis")
        self.assertEqual(result.status, "complete")
        self.assertIsNone(agent._active_prediction_id)
        self.assertEqual(self.memory.open_prediction_count(), 0)

    def test_prediction_uses_measured_competence_after_ten_outcomes(self):
        for index in range(10):
            prediction_id = self.memory.record_prediction(
                family="conversation",
                profile="fast",
                model="test-model",
                predicted_success=0.9,
                predicted_steps=0,
                predicted_verification="not_applicable",
            )
            self.memory.resolve_prediction(
                prediction_id,
                actual_status="complete" if index < 8 else "failed",
                actual_steps=0,
                evidence_ok=None,
                failure_class=None if index < 8 else "unknown",
            )

        agent, _client = self.make_agent([])
        result = agent.run("hey jarvis")

        self.assertEqual(result.status, "complete")
        prediction = dict(self.memory.db.execute(
            "SELECT * FROM task_predictions ORDER BY id DESC LIMIT 1"
        ).fetchone())
        self.assertEqual(prediction["basis"], "competence")
        self.assertAlmostEqual(prediction["predicted_success"], 0.8)

    def test_task_prediction_preserves_task_and_proactive_origin(self):
        agent, _client = self.make_agent([])
        result = agent.run(
            "hey jarvis",
            task_id=42,
            prediction_origin="proactive",
        )
        self.assertEqual(result.status, "complete")
        prediction = dict(self.memory.db.execute(
            "SELECT * FROM task_predictions"
        ).fetchone())
        self.assertEqual(prediction["task_id"], 42)
        self.assertEqual(prediction["origin"], "proactive")

    def test_task_family_precedence_and_general_conversation_fallback(self):
        common = {
            "casual_greeting": False,
            "learning_task": False,
            "deep_research_task": False,
            "requires_coding": False,
            "requires_web": False,
            "allow_external_mutation": False,
            "allow_computer_files": False,
            "security_task": False,
        }
        cases = [
            ("Explain gravity simply", {}, "conversation"),
            ("Summarize the files", {}, "file_ops"),
            ("Fix the parser in app.py", {"requires_coding": True}, "code_fix"),
            ("Refactor app.py", {"requires_coding": True}, "code_refactor"),
            ("Add tests for app.py", {"requires_coding": True}, "code_test"),
            ("Research current releases", {"requires_web": True}, "deep_research"),
            ("Analyze this firewall policy", {"security_task": True}, "security_analysis"),
            (
                "Push the branch to GitHub",
                {"allow_external_mutation": True, "requires_coding": True},
                "external_publish",
            ),
        ]
        for prompt, overrides, expected in cases:
            with self.subTest(prompt=prompt):
                arguments = {**common, **overrides}
                self.assertEqual(_task_family(prompt, **arguments), expected)

    def test_jarvis_vocative_greeting_is_instant_without_thinking_event(self):
        agent, client = self.make_agent([])
        events = []
        agent.on_event = events.append

        result = agent.run("hey jarvis")

        self.assertEqual(result, "What's up, bro? Ready when you are.")
        self.assertEqual(result.status, "complete")
        self.assertEqual(client.requests, [])
        self.assertEqual(agent.toolbox.calls, [])
        self.assertEqual(events, ["instant response - casual greeting"])

    def test_fast_model_loop_reports_processing_not_thinking(self):
        agent, client = self.make_agent([FakeResponse(content="A concise explanation.")])
        events = []
        agent.on_event = events.append

        result = agent.run("Explain gravity simply")

        self.assertEqual(result.status, "complete")
        self.assertEqual(client.requests[0]["think"], False)
        self.assertIn("processing - step 1", events)
        self.assertNotIn("thinking - step 1", events)

    def test_cancellation_guard_stops_before_model_and_is_cleared(self):
        agent, client = self.make_agent([FakeResponse(content="Recovered.")])

        with self.assertRaises(AgentRunCancelled):
            agent.run("Explain the project", cancellation_guard=lambda: True)

        self.assertEqual(client.requests, [])
        self.assertEqual(agent.toolbox.calls, [])
        result = agent.run("Explain the project")
        self.assertEqual(result, "Recovered.")
        self.assertEqual(len(client.requests), 1)

    def test_cancellation_after_model_stops_before_first_tool(self):
        toolbox = FakeToolBox()
        agent, client = self.make_agent([
            FakeResponse(tool_calls=[tool_call("read_file", {"path": "a.py"})]),
        ], toolbox)

        with self.assertRaises(AgentRunCancelled):
            agent.run(
                "Inspect the project files",
                cancellation_guard=lambda: bool(client.requests),
            )

        self.assertEqual(len(client.requests), 1)
        self.assertEqual(toolbox.calls, [])
        prediction = dict(self.memory.db.execute(
            "SELECT * FROM task_predictions"
        ).fetchone())
        self.assertEqual(prediction["actual_status"], "failed")
        self.assertEqual(prediction["failure_class"], "cancelled")
        self.assertIsNone(prediction["actual_steps"])

    def test_standalone_missing_referent_asks_one_question_without_model_or_tool(self):
        for prompt, expected in (
            ("What do you think?", "What would you like my opinion on?"),
            ("can you check it", "What should I use or continue?"),
            ("go ahead", "What should I use or continue?"),
            ("help me", "Absolutely—what would you like help with?"),
        ):
            with self.subTest(prompt=prompt):
                toolbox = FakeToolBox()
                agent, client = self.make_agent([], toolbox)

                result = agent.run(prompt)

                self.assertEqual(result.status, "complete")
                self.assertIn(expected, str(result))
                self.assertEqual(client.requests, [])
                self.assertEqual(toolbox.calls, [])

    def test_provider_error_resolves_prediction_as_model_unavailable(self):
        class UnavailableClient(ScriptedClient):
            def __init__(self):
                super().__init__([])

            def chat(self, *args, **kwargs):
                self.requests.append({"called": True})
                raise OllamaError("offline")

        client = UnavailableClient()
        agent = Agent(
            self.config,
            self.memory,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = FakeToolBox()
        result = agent.run("Explain the project")

        self.assertEqual(result.status, "incomplete")
        self.assertTrue(result.retryable)
        self.assertIn("understood your question", str(result).casefold())
        self.assertIn("configured fallback", str(result).casefold())
        self.assertIn("retry it now?", str(result).casefold())
        self.assertNotIn("offline", str(result).casefold())

        prediction = dict(self.memory.db.execute(
            "SELECT * FROM task_predictions"
        ).fetchone())
        self.assertEqual(prediction["actual_status"], "failed")
        self.assertEqual(prediction["failure_class"], "model_unavailable")
        self.assertEqual(prediction["actual_steps"], 0)

    def test_general_pending_goal_followup_grammar_is_domain_independent(self):
        positives = (
            "what did you find?",
            "show me the best one",
            "are you done yet?",
            "send me the link",
            "ship it",
            "okay, start",
            "run the tests and open it",
            "fix that and keep going",
            "turn that into a PDF",
            "make a Word version too",
            "add an executive summary",
            "export the report",
            "send it to them",
            "schedule that for 9 tomorrow",
            "put that in Drive",
            "publish it now",
            "see if that is still true",
            "give me the official source",
            "what did you find?",
            "check that now",
            "what about now",
            "can you try it again now?",
        )
        negatives = (
            "what do you think about dogs?",
            "start a different project instead",
            "cancel it",
            "never mind",
            "explain how databases work",
            "the weather is nice today",
            "what about lunch now?",
        )
        for prompt in positives:
            with self.subTest(prompt=prompt):
                self.assertTrue(_is_pending_goal_followup(prompt))
        for prompt in negatives:
            with self.subTest(prompt=prompt):
                self.assertFalse(_is_pending_goal_followup(prompt))

    def test_pending_goal_resumes_after_restart_and_recency_loss(self):
        class UnavailableClient(ScriptedClient):
            def __init__(self):
                super().__init__([])

            def chat(self, *args, **kwargs):
                self.requests.append({"called": True})
                raise OllamaError("temporary provider outage", retryable=True)

        path = self.data_dir / "durable-resume.db"
        events: list[str] = []
        with Memory(path) as first_memory:
            conversation_id = first_memory.new_conversation("decision framework")
            first_client = UnavailableClient()
            first_agent = Agent(
                self.config,
                first_memory,
                events.append,
                client=first_client,
                coding_review=False,
                coding_planning=False,
            )
            first_agent.toolbox = FakeToolBox()
            first = first_agent.run(
                "Prepare a decision framework comparing lease and purchase options.",
                conversation_id=conversation_id,
            )
            self.assertEqual(first.status, "incomplete")
            self.assertTrue(first.retryable)
            self.assertIsNotNone(first_memory.pending_conversation_goal(conversation_id))

        with Memory(path) as restarted_memory:
            for index in range(10):
                restarted_memory.add_message(
                    conversation_id,
                    "user" if index % 2 == 0 else "assistant",
                    f"Unrelated bounded filler turn {index}.",
                )
            client = ScriptedClient([
                FakeResponse(content="The verified decision framework is complete."),
            ])
            agent = Agent(
                self.config,
                restarted_memory,
                events.append,
                client=client,
                coding_review=False,
                coding_planning=False,
            )
            agent.toolbox = FakeToolBox()

            result = agent.run("please continue it", conversation_id=conversation_id)

            self.assertEqual(result.status, "complete", result.reason)
            self.assertIn("continuing durable same-conversation goal", events)
            request_text = json.dumps(client.requests[0]["messages"])
            self.assertIn("lease and purchase options", request_text)
            self.assertIn("please continue it", request_text)
            self.assertIsNone(restarted_memory.pending_conversation_goal(conversation_id))
            goal = restarted_memory.list_conversation_goals(conversation_id)[0]
            self.assertEqual(goal["state"], "complete")
            self.assertEqual(goal["resume_count"], 1)

    def test_provider_recovery_diagnoses_each_failed_backend_without_raw_details(self):
        class ExhaustedClient(ScriptedClient):
            def __init__(self):
                super().__init__([])

            def models(self, refresh=True):
                return ["claude-cli:sonnet", "openai:gpt-5.6-luna"]

            def chat(self, messages, tools, model, **kwargs):
                self.requests.append({"model": model})
                if model.startswith("claude-cli:"):
                    raise OllamaError("private cli failure text", retryable=True)
                raise OllamaError(
                    "private quota response text",
                    status_code=429,
                    retryable=True,
                )

        self.config = replace(
            self.config,
            fast_model="claude-cli:sonnet",
            reasoning_model="openai:gpt-5.6-luna",
            coding_model="openai:gpt-5.6-luna",
            deep_model="openai:gpt-5.6-luna",
        )
        client = ExhaustedClient()
        agent = Agent(self.config, self.memory, client=client)
        agent.toolbox = FakeToolBox()

        result = agent.run("What do you think about this design?")

        self.assertEqual(result.status, "incomplete")
        self.assertIn("Claude CLI is temporarily unavailable", str(result))
        self.assertIn("OpenAI is rate-limited", str(result))
        self.assertIn("Would you like me to retry it now?", str(result))
        self.assertNotIn("private cli failure", str(result))
        self.assertNotIn("private quota", str(result))
        self.assertEqual(
            [item["model"] for item in client.requests],
            ["claude-cli:sonnet", "openai:gpt-5.6-luna"],
        )

    def test_model_call_tries_all_distinct_fallbacks_before_failing(self):
        class RecoveringClient(ScriptedClient):
            def __init__(self):
                super().__init__([])

            def models(self, refresh=True):
                return [
                    "openai:gpt-5.6-luna",
                    "openai:gpt-5.6-terra",
                    "openai:gpt-5.6-sol",
                ]

            def chat(self, messages, tools, model, **kwargs):
                self.requests.append({"model": model})
                if len(self.requests) < 3:
                    raise OllamaError(
                        "provider rejected request shape",
                        status_code=400,
                    )
                return FakeResponse(content="Recovered through fallback.")

        self.config = replace(
            self.config,
            fast_model="openai:gpt-5.6-luna",
            reasoning_model="openai:gpt-5.6-terra",
            coding_model="openai:gpt-5.6-sol",
            deep_model="openai:gpt-5.6-sol",
        )
        client = RecoveringClient()
        agent = Agent(self.config, self.memory, client=client)
        agent.toolbox = FakeToolBox()

        result = agent.run("Explain this ordinary request")

        self.assertEqual(result.status, "complete")
        self.assertEqual(str(result), "Recovered through fallback.")
        self.assertEqual(
            [item["model"] for item in client.requests],
            [
                "openai:gpt-5.6-luna",
                "openai:gpt-5.6-terra",
                "openai:gpt-5.6-sol",
            ],
        )
        self.assertEqual(result.metrics["model_attempts"], 3)
        self.assertEqual(result.metrics["retries"], 2)
        self.assertEqual(result.metrics["failovers"], 2)
        self.assertEqual(result.metrics["initial_model"], "openai:gpt-5.6-luna")
        self.assertEqual(result.metrics["final_model"], "openai:gpt-5.6-sol")
        self.assertEqual(result.metrics["failure_kind"], "OllamaError")

    def test_provider_unavailability_skips_later_models_on_same_backend(self):
        class ProviderRecoveryClient(ScriptedClient):
            def __init__(self):
                super().__init__([])

            def models(self, refresh=True):
                return [
                    "claude-cli:haiku",
                    "claude-cli:sonnet",
                    "claude-cli:opus",
                    "openai:gpt-5.6-luna",
                ]

            def chat(self, messages, tools, model, **kwargs):
                self.requests.append({"model": model})
                if model.startswith("claude-cli:"):
                    raise ModelProviderError(
                        "claude-cli",
                        "backend unavailable",
                        retryable=True,
                        provider_unavailable=True,
                    )
                return FakeResponse(content="Recovered without retrying dead backend models.")

        self.config = replace(
            self.config,
            fast_model="claude-cli:haiku",
            reasoning_model="claude-cli:sonnet",
            coding_model="claude-cli:opus",
            deep_model="openai:gpt-5.6-luna",
        )
        client = ProviderRecoveryClient()
        agent = Agent(self.config, self.memory, client=client)
        agent.toolbox = FakeToolBox()

        result = agent.run("Explain this ordinary request")

        self.assertEqual(result.status, "complete")
        self.assertEqual(
            [item["model"] for item in client.requests],
            ["claude-cli:haiku", "openai:gpt-5.6-luna"],
        )

    def test_cancellation_stops_before_model_failover(self):
        class FailingClient(ScriptedClient):
            def __init__(self):
                super().__init__([])
                self.failed = False

            def chat(self, messages, tools, model, context_length, think=None, temperature=0.2, response_format=None, seed=None):
                self.requests.append({"model": model})
                self.failed = True
                raise OllamaError("temporary outage", retryable=True)

        client = FailingClient()
        agent = Agent(self.config, self.memory, client=client)
        agent.toolbox = FakeToolBox()

        with self.assertRaises(AgentRunCancelled):
            agent.run(
                "Explain the project",
                cancellation_guard=lambda: client.failed,
            )

        self.assertEqual(len(client.requests), 1)
        self.assertEqual(agent.toolbox.calls, [])

    def test_near_miss_meaningful_question_still_invokes_the_llm(self):
        toolbox = FakeToolBox()
        agent, client = self.make_agent([
            FakeResponse(content="The failing build needs a concrete diagnostic pass."),
        ], toolbox)

        result = agent.run("what up bro, what is causing the failing build?")

        self.assertEqual(len(client.requests), 1)
        self.assertIn(
            "what up bro, what is causing the failing build?",
            client.requests[0]["messages"][-1]["content"],
        )
        self.assertEqual(toolbox.calls, [])
        self.assertEqual(result, "The failing build needs a concrete diagnostic pass.")
        self.assertEqual(result.status, "complete")

    def test_parallel_calls_stop_at_hard_budget(self):
        calls = [tool_call("recall", {"query": f"q{index}"}) for index in range(10)]
        agent, _client = self.make_agent([
            FakeResponse(tool_calls=calls),
            FakeResponse(content="Budgeted final answer."),
        ])
        result = agent.run("Search the workspace files for project context")
        self.assertEqual(len(agent.toolbox.calls), 8)
        self.assertEqual(result.tool_calls, 8)
        self.assertEqual(result.status, "complete")

    def test_verification_gate_cannot_be_bypassed_by_repeated_answers(self):
        agent, _client = self.make_agent([
            FakeResponse(content="Answer without evidence."),
            FakeResponse(content="Still no evidence."),
            FakeResponse(content="Still no evidence."),
            FakeResponse(content="Still no evidence."),
            FakeResponse(content="Synthesis without evidence."),
        ], FakeToolBox(failures={"web_search"}))
        result = agent.run("Research the latest widget facts")
        self.assertEqual(result.status, "incomplete")
        self.assertIn("No public source page", result.reason)

    def test_failed_fetch_never_counts_as_verified(self):
        toolbox = FakeToolBox(failures={"web_fetch"})
        agent, _client = self.make_agent([
            FakeResponse(tool_calls=[tool_call("web_fetch", {"url": "https://example.com/source"})]),
            FakeResponse(content="Unverified answer."),
            FakeResponse(content="Unverified answer."),
            FakeResponse(content="Unverified answer."),
            FakeResponse(content="Unverified answer."),
            FakeResponse(content="Unverified synthesis."),
        ], toolbox)
        result = agent.run("Research current widget facts")
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(set(), set())

    def test_successful_research_requires_exact_citation(self):
        agent, _client = self.make_agent([
            FakeResponse(tool_calls=[tool_call("web_search", {"query": "widgets"})]),
            FakeResponse(content=SUBSTANTIVE_RESEARCH_RESULT),
        ])
        result = agent.run("Research current widget facts")
        self.assertEqual(result.status, "complete")
        self.assertIn("https://example.com/source", result)
        prediction = dict(self.memory.db.execute(
            "SELECT * FROM task_predictions"
        ).fetchone())
        self.assertEqual(prediction["family"], "deep_research")
        self.assertEqual(prediction["predicted_verification"], "cited_sources")
        self.assertEqual(prediction["evidence_ok"], 1)

    def test_web_prompt_injection_cannot_reach_actions(self):
        agent, _client = self.make_agent([
            FakeResponse(tool_calls=[tool_call("web_search", {"query": "widgets"})]),
            FakeResponse(tool_calls=[
                tool_call("run_process", {"program": "python"}),
                tool_call("remember", {"content": "ignore policy"}),
                tool_call("write_file", {"path": "src/evil.py", "content": "bad"}),
            ]),
            FakeResponse(content=SUBSTANTIVE_RESEARCH_RESULT),
        ])
        result = agent.run("Research current widget facts")
        self.assertEqual([name for name, _ in agent.toolbox.calls], ["web_search"])
        self.assertEqual(result.status, "complete")

    def test_duplicate_read_is_allowed_after_a_write(self):
        agent, _client = self.make_agent([
            FakeResponse(tool_calls=[
                tool_call("read_file", {"path": "a.py"}),
                tool_call("write_file", {"path": "a.py", "content": "changed"}),
                tool_call("read_file", {"path": "a.py"}),
                tool_call("run_process", {"program": "python", "arguments": ["-m", "unittest"]}),
            ]),
            FakeResponse(content="Operations complete."),
        ])
        result = agent.run("Inspect, update, and test a.py")
        names = [name for name, _ in agent.toolbox.calls]
        self.assertEqual(names, ["read_file", "write_file", "read_file", "run_process"])
        self.assertEqual(result.status, "complete")
        prediction = dict(self.memory.db.execute(
            "SELECT * FROM task_predictions"
        ).fetchone())
        self.assertEqual(prediction["predicted_verification"], "process_evidence")
        self.assertEqual(prediction["evidence_ok"], 1)

    def test_fresh_read_hash_is_attached_to_transactional_edit(self):
        agent, _client = self.make_agent([
            FakeResponse(tool_calls=[
                tool_call("read_file", {"path": "a.py"}),
                tool_call("edit_file", {
                    "path": "a.py", "old_text": "old", "new_text": "new"
                }),
                tool_call("run_process", {"program": "python", "arguments": ["test_public.py"]}),
            ]),
            FakeResponse(content="Edited and verified."),
        ])

        result = agent.run("Inspect, edit, and test a.py")

        edit_arguments = next(args for name, args in agent.toolbox.calls if name == "edit_file")
        self.assertEqual(edit_arguments["expected_sha256"], "a" * 64)
        self.assertEqual(result.status, "complete")

    def test_explicit_preserve_tests_request_blocks_test_file_writes(self):
        agent, _client = self.make_agent([
            FakeResponse(tool_calls=[
                tool_call("read_file", {"path": "a.py"}),
                tool_call("write_file", {"path": "test_public.py", "content": "changed"}),
                tool_call("write_file", {"path": "a.py", "content": "fixed"}),
                tool_call("run_process", {"program": "python", "arguments": ["test_public.py"]}),
            ]),
            FakeResponse(content="Implementation updated and verified."),
        ])

        result = agent.run("Fix and test a.py. Do not modify the tests.")

        written_paths = [
            args["path"] for name, args in agent.toolbox.calls if name == "write_file"
        ]
        self.assertEqual(result.status, "complete")
        self.assertEqual(written_paths, ["a.py"])

    def test_coding_completion_requires_independent_reasoning_review(self):
        toolbox = FakeToolBox()
        client = ScriptedClient([
            FakeResponse(tool_calls=[
                tool_call("read_file", {"path": "a.py"}),
                tool_call("write_file", {"path": "a.py", "content": "print('ok')"}),
                tool_call("read_file", {"path": "a.py"}),
                tool_call(
                    "run_process",
                    {"program": "python", "arguments": ["-m", "unittest"]},
                ),
            ]),
            FakeResponse(content="Implemented and tested."),
            FakeResponse(content=(
                '{"passed": true, "issues": [], '
                '"recommended_tests": ["retain the regression suite"]}'
            )),
            FakeResponse(content=(
                '{"passed": true, "issues": [], '
                '"recommended_tests": ["retain the regression suite"]}'
            )),
        ])
        agent = Agent(
            self.config, self.memory, client=client,
            coding_review=True, coding_planning=False,
            automatic_review_checkpoint=False,
        )
        agent.toolbox = toolbox

        result = agent.run("Inspect, update, and test a.py")

        self.assertIsInstance(client.requests[-1]["response_format"], dict)
        self.assertEqual(client.requests[-1]["seed"], 0)
        self.assertFalse(client.requests[-1]["response_format"]["additionalProperties"])
        self.assertEqual(result.status, "complete")
        self.assertEqual(client.requests[-1]["model"], "gpt-oss:20b")
        self.assertEqual(client.requests[-2]["think"], "low")
        self.assertEqual(client.requests[-1]["think"], "medium")
        self.assertEqual(client.requests[-1]["tools"], [])
        self.assertEqual(client.requests[-1]["temperature"], 0.0)

    def test_coding_planning_inspects_before_exposing_write_tools(self):
        plan = json.dumps({
            "requirements": ["Implement every README requirement."],
            "edge_cases": ["Reject booleans, NaN, infinity, and naive timestamps."],
            "implementation_guidance": ["Parse once and compare normalized values."],
            "verification_cases": ["Naive timestamp input is rejected."],
        })
        passed = '{"passed": true, "issues": [], "recommended_tests": []}'
        toolbox = FakeToolBox()
        client = ScriptedClient([
            FakeResponse(tool_calls=[
                tool_call("read_file", {"path": "README.md"}),
                tool_call("read_file", {"path": "a.py"}),
            ]),
            FakeResponse(content=plan),
            FakeResponse(tool_calls=[
                tool_call("write_file", {"path": "a.py", "content": "fixed"}),
                tool_call("run_process", {"program": "python", "arguments": ["test_public.py"]}),
            ]),
            FakeResponse(content=passed),
            FakeResponse(content=passed),
        ])
        agent = Agent(
            self.config, self.memory, client=client,
            coding_review=True, coding_planning=True,
        )
        agent.toolbox = toolbox

        result = agent.run("Inspect, update, and test a.py using README.md")

        first_tools = {
            schema["function"]["name"] for schema in client.requests[0]["tools"]
        }
        implementation_tools = {
            schema["function"]["name"] for schema in client.requests[2]["tools"]
        }
        self.assertEqual(result.status, "complete")
        self.assertNotIn("write_file", first_tools)
        self.assertIn(
            "read-only coding reconnaissance",
            client.requests[0]["messages"][-1]["content"],
        )
        self.assertEqual(client.requests[1]["model"], "gpt-oss:20b")
        self.assertEqual(client.requests[1]["tools"], [])
        self.assertEqual(client.requests[1]["think"], "low")
        self.assertIn("write_file", implementation_tools)
        self.assertEqual(client.requests[3]["model"], "gpt-oss:20b")
        self.assertEqual(client.requests[3]["tools"], [])
        self.assertIn(
            "naive timestamps",
            client.requests[2]["messages"][-1]["content"],
        )

    def test_prewrite_plan_adds_deterministic_language_boundary_guards(self):
        client = ScriptedClient([FakeResponse(content=json.dumps({
            "requirements": ["Validate the event."],
            "edge_cases": ["Malformed input."],
            "implementation_guidance": ["Parse the record."],
            "verification_cases": ["Malformed input is rejected."],
        }))])
        agent = Agent(
            self.config, self.memory, client=client,
            coding_review=True, coding_planning=True,
        )
        plan, _model = agent._plan_coding_approach(
            "Implement the specification.",
            {
                "README.md": {
                    "path": "README.md",
                    "content": (
                        "duration is a finite non-negative number; timestamp is ISO-8601 "
                        "with a timezone; deduplicate by earliest timestamp and keep the first tie"
                    ),
                    "truncated": False,
                },
            },
        )
        combined = json.dumps(plan).casefold()
        self.assertIn("bool is a subclass of int", combined)
        self.assertIn("parsed.tzinfo", combined)
        self.assertIn("later duplicate before an earlier duplicate", combined)

    def test_invalid_independent_review_is_a_failure(self):
        passed, issues, recommended_tests = Agent._parse_coding_review("not JSON")
        self.assertFalse(passed)
        self.assertIn("invalid JSON", issues[0]["defect"])
        self.assertEqual(recommended_tests, [])

    def test_ungrounded_independent_review_issue_is_rejected(self):
        payload = json.dumps({
            "passed": False,
            "issues": [{
                "path": "a.py",
                "evidence": "invented source line",
                "defect": "invented defect",
                "expected_behavior": "do something else",
            }],
            "recommended_tests": ["specific boundary input returns zero"],
        })
        passed, issues, recommended_tests = Agent._parse_coding_review(
            payload,
            {"a.py": {"path": "a.py", "content": "real source line"}},
        )
        self.assertFalse(passed)
        self.assertIn("ungrounded", issues[0]["defect"])
        self.assertEqual(recommended_tests, ["specific boundary input returns zero"])

    def test_ungrounded_low_effort_review_gets_medium_effort_adjudication(self):
        client = ScriptedClient([
            FakeResponse(content=json.dumps({
                "passed": False,
                "issues": [{
                    "path": "a.py",
                    "evidence": "invented line",
                    "defect": "invented defect",
                    "expected_behavior": "change it",
                }],
                "recommended_tests": [],
            })),
            FakeResponse(content=json.dumps({
                "passed": True,
                "issues": [],
                "recommended_tests": ["existing tests remain green"],
            })),
        ])
        agent = Agent(
            self.config, self.memory, client=client,
            coding_review=True, coding_planning=False,
        )

        passed, issues, recommended_tests, _model = agent._review_coding(
            "Refactor a.py without changing behavior.",
            {"a.py": {"path": "a.py", "content": "answer = 42\n"}},
            [],
        )

        self.assertTrue(passed)
        self.assertEqual(issues, [])
        self.assertEqual(recommended_tests, ["existing tests remain green"])
        self.assertEqual([request["think"] for request in client.requests], ["low", "medium"])

    def test_deterministic_review_catches_python_boolean_numeric_subtype(self):
        client = ScriptedClient([FakeResponse(content=(
            '{"passed": true, "issues": [], "recommended_tests": []}'
        ))])
        agent = Agent(
            self.config, self.memory, client=client,
            coding_review=True, coding_planning=False,
        )
        passed, issues, recommended_tests, _model = agent._review_coding(
            "Implement the finite numeric duration contract in README.md.",
            {
                "README.md": {
                    "path": "README.md",
                    "content": "duration_ms is a finite non-negative number",
                },
                "event.py": {
                    "path": "event.py",
                    "content": (
                        "if not isinstance(duration_ms, (int, float)) or duration_ms < 0:\n"
                        "    return None\n"
                    ),
                },
            },
            [],
        )
        self.assertFalse(passed)
        self.assertIn("bool is a subclass of int", issues[-1]["defect"])
        self.assertIn("True and False", recommended_tests[-1])

    def test_failed_review_forces_exact_edit_and_bounds_test_churn(self):
        toolbox = FakeToolBox()
        client = ScriptedClient([
            FakeResponse(tool_calls=[
                tool_call("read_file", {"path": "a.py"}),
                tool_call("write_file", {"path": "a.py", "content": "print('ok')"}),
                tool_call("read_file", {"path": "a.py"}),
                tool_call(
                    "run_process",
                    {"program": "python", "arguments": ["-m", "unittest"]},
                ),
            ]),
            FakeResponse(content="Implemented and tested."),
            FakeResponse(content=(
                '{"passed": false, "issues": [{"path": "a.py", '
                '"evidence": "original content", "defect": "boundary is wrong", '
                '"expected_behavior": "boundary returns zero"}], '
                '"recommended_tests": ["boundary test"]}'
            )),
            FakeResponse(tool_calls=[
                tool_call("run_process", {"program": "python", "arguments": ["test_a.py"]}),
                tool_call("write_file", {"path": "a.py", "content": "rewrite"}),
                tool_call("edit_file", {
                    "path": "a.py", "old_text": "original", "new_text": "fixed"
                }),
            ]),
            FakeResponse(tool_calls=[
                tool_call("run_process", {"program": "python", "arguments": ["test_b.py"]}),
                tool_call("run_process", {"program": "python", "arguments": ["test_c.py"]}),
            ]),
            FakeResponse(content=(
                '{"passed": true, "issues": [], '
                '"recommended_tests": ["keep the boundary tests"]}'
            )),
            FakeResponse(content=(
                '{"passed": true, "issues": [], '
                '"recommended_tests": ["keep the boundary tests"]}'
            )),
        ])
        agent = Agent(
            self.config, self.memory, client=client,
            coding_review=True, coding_planning=False,
            automatic_review_checkpoint=False,
        )
        agent.toolbox = toolbox

        result = agent.run("Inspect, update, and test a.py")

        executed_processes = [name for name, _arguments in toolbox.calls if name == "run_process"]
        self.assertEqual(result.status, "complete")
        self.assertEqual(client.requests[3]["model"], "qwen3-coder:30b")
        self.assertFalse(client.requests[3]["think"])
        self.assertEqual(len(executed_processes), 2)
        repair_tools = {
            schema["function"]["name"] for schema in client.requests[3]["tools"]
        }
        self.assertIn("edit_file", repair_tools)
        self.assertNotIn("write_file", repair_tools)
        self.assertNotIn(
            "run_process",
            repair_tools,
        )
        self.assertIn("boundary test", client.requests[3]["messages"][-1]["content"])

    def test_second_grounded_review_failure_applies_structured_reasoner_plan(self):
        issue = (
            '{"passed": false, "issues": [{"path": "a.py", '
            '"evidence": "original content", "defect": "boundary is wrong", '
            '"expected_behavior": "boundary returns zero"}], '
            '"recommended_tests": ["zero boundary returns zero"]}'
        )
        passed = '{"passed": true, "issues": [], "recommended_tests": []}'
        toolbox = FakeToolBox()
        client = ScriptedClient([
            FakeResponse(tool_calls=[
                tool_call("read_file", {"path": "a.py"}),
                tool_call("write_file", {"path": "a.py", "content": "first"}),
                tool_call("run_process", {"program": "python", "arguments": ["test_public.py"]}),
            ]),
            FakeResponse(content="Initial implementation complete."),
            FakeResponse(content=issue),
            FakeResponse(tool_calls=[
                tool_call("edit_file", {
                    "path": "a.py", "old_text": "original", "new_text": "second"
                }),
            ]),
            FakeResponse(tool_calls=[
                tool_call("run_process", {"program": "python", "arguments": ["test_public.py"]}),
            ]),
            FakeResponse(content=issue),
            FakeResponse(content=json.dumps({
                "edits": [{
                    "issue_index": 0,
                    "new_text": "third",
                    "reason": "correct the boundary",
                }],
            })),
            FakeResponse(content=passed),
            FakeResponse(content=passed),
        ])
        agent = Agent(
            self.config, self.memory, client=client,
            coding_review=True, coding_planning=False,
            automatic_review_checkpoint=False,
        )
        agent.toolbox = toolbox

        result = agent.run("Inspect, update, and test a.py")

        self.assertEqual(result.status, "complete")
        self.assertEqual(client.requests[6]["model"], "gpt-oss:20b")
        self.assertEqual(client.requests[6]["think"], "medium")
        self.assertEqual(client.requests[6]["tools"], [])
        self.assertIsInstance(client.requests[6]["response_format"], dict)
        runtime_edits = [args for name, args in toolbox.calls if name == "edit_file"]
        self.assertEqual(runtime_edits[-1]["old_text"], "original content")
        self.assertEqual(runtime_edits[-1]["new_text"], "third")

    def test_repair_replays_last_successful_verification_automatically(self):
        issue = (
            '{"passed": false, "issues": [{"path": "a.py", '
            '"evidence": "original content", "defect": "boundary is wrong", '
            '"expected_behavior": "boundary returns zero"}], '
            '"recommended_tests": ["zero boundary returns zero"]}'
        )
        passed = '{"passed": true, "issues": [], "recommended_tests": []}'
        events = []
        toolbox = FakeToolBox()
        client = ScriptedClient([
            FakeResponse(tool_calls=[
                tool_call("read_file", {"path": "a.py"}),
                tool_call("write_file", {"path": "a.py", "content": "first"}),
                tool_call("run_process", {"program": "python", "arguments": ["test_public.py"]}),
            ]),
            FakeResponse(content=issue),
            FakeResponse(content=json.dumps({
                "edits": [{
                    "issue_index": 0,
                    "new_text": "fixed",
                    "reason": "correct the boundary",
                }],
            })),
            FakeResponse(content=passed),
            FakeResponse(content=passed),
        ])
        agent = Agent(
            self.config, self.memory, events.append, client=client,
            coding_review=True, coding_planning=False,
            automatic_review_checkpoint=True,
        )
        agent.toolbox = toolbox

        result = agent.run("Inspect, update, and test a.py")

        process_calls = [args for name, args in toolbox.calls if name == "run_process"]
        edit_calls = [args for name, args in toolbox.calls if name == "edit_file"]
        self.assertEqual(result.status, "complete")
        self.assertEqual(len(edit_calls), 1)
        self.assertEqual(len(process_calls), 2)
        self.assertEqual(process_calls[0], process_calls[1])
        self.assertIn("repair verification passed", events)

    def test_python_edit_guard_rejects_a_syntax_corrupting_replacement(self):
        observed = {
            "path": "event_rollup.py",
            "content": "def valid(value):\n    return isinstance(value, (int, float))\n",
            "truncated": False,
        }
        error = _source_mutation_error(
            "edit_file",
            {
                "path": "event_rollup.py",
                "old_text": "isinstance(value, (int, float))",
                "new_text": "# event_rollup.py\n" + ("x = 1\n" * 25),
            },
            observed,
        )

        self.assertIsNotNone(error)
        self.assertIn("not minimal", error)

    def test_path_alias_still_uses_the_inspected_snapshot_edit_guard(self):
        class ParseableToolBox(FakeToolBox):
            def execute(self, name, arguments):
                if name == "read_file":
                    self.calls.append((name, arguments))
                    return json.dumps({
                        "ok": True,
                        "result": {
                            "path": str(arguments.get("path", "")),
                            "sha256": "a" * 64,
                            "content": (
                                "def valid(value):\n"
                                "    return isinstance(value, (int, float))\n"
                            ),
                            "truncated": False,
                        },
                    })
                return super().execute(name, arguments)

        toolbox = ParseableToolBox()
        client = ScriptedClient([
            FakeResponse(tool_calls=[
                tool_call("read_file", {"path": "a.py"}),
                tool_call("edit_file", {
                    "path": "./a.py",
                    "old_text": "isinstance(value, (int, float))",
                    "new_text": "# module\n" + ("x = 1\n" * 25),
                }),
            ]),
            *[FakeResponse(content="Done.") for _ in range(6)],
        ])
        agent = Agent(
            self.config, self.memory, client=client,
            coding_review=False, coding_planning=False,
        )
        agent.toolbox = toolbox

        result = agent.run("Inspect and update a.py")

        self.assertEqual(result.status, "incomplete")
        self.assertNotIn("edit_file", [name for name, _arguments in toolbox.calls])

    def test_review_does_not_treat_explicitly_correct_style_as_a_defect(self):
        passed, issues, _tests = Agent._parse_coding_review(json.dumps({
            "passed": False,
            "issues": [{
                "path": "a.py",
                "evidence": "for field in required_fields:",
                "defect": "The logic is correct but could be clearer and more readable.",
                "expected_behavior": "Use a clearer style.",
            }],
            "recommended_tests": [],
        }), {"a.py": {
            "path": "a.py",
            "content": "for field in required_fields:\n    pass\n",
        }})

        self.assertTrue(passed)
        self.assertEqual(issues, [])

    def test_structured_repair_rejects_nonminimal_full_file_replacement(self):
        current = "def valid(value):\n    return isinstance(value, (int, float))\n"
        client = ScriptedClient([FakeResponse(content=json.dumps({
            "edits": [{
                "issue_index": 0,
                "new_text": "# generated module\n" + ("x = 1\n" * 250),
                "reason": "replace the check",
            }],
        }))])
        agent = Agent(self.config, self.memory, client=client)
        plan, _model = agent._plan_coding_repairs(
            "Fix numeric validation",
            {"event_rollup.py": {
                "path": "event_rollup.py",
                "sha256": "a" * 64,
                "content": current,
                "truncated": False,
            }},
            [{
                "path": "event_rollup.py",
                "evidence": "isinstance(value, (int, float))",
                "defect": "the comparison is incorrect",
                "expected_behavior": "use the required comparison",
            }],
            ["the required comparison passes"],
        )

        self.assertEqual(plan, [])

    def test_structured_repair_has_a_deterministic_python_bool_guard(self):
        current = "def valid(value):\n    return isinstance(value, (int, float))\n"
        client = ScriptedClient([FakeResponse(content=json.dumps({"edits": []}))])
        agent = Agent(self.config, self.memory, client=client)
        plan, _model = agent._plan_coding_repairs(
            "Reject booleans from numeric input",
            {"event_rollup.py": {
                "path": "event_rollup.py",
                "sha256": "a" * 64,
                "content": current,
                "truncated": False,
            }},
            [{
                "path": "event_rollup.py",
                "evidence": "isinstance(value, (int, float))",
                "defect": "Python bool is a subclass of int and is accepted",
                "expected_behavior": "reject bool before int and float",
            }],
            ["True and False are rejected"],
        )

        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["old_text"], "isinstance(value, (int, float))")
        self.assertEqual(
            plan[0]["new_text"],
            "(not isinstance(value, bool) and isinstance(value, (int, float)))",
        )

    def test_runtime_rereads_pending_files_before_review(self):
        toolbox = FakeToolBox()
        client = ScriptedClient([
            FakeResponse(tool_calls=[
                tool_call("read_file", {"path": "a.py"}),
                tool_call("write_file", {"path": "a.py", "content": "print('ok')"}),
                tool_call("run_process", {"program": "python", "arguments": ["test_public.py"]}),
            ]),
            FakeResponse(content="Done."),
            FakeResponse(content=(
                '{"passed": true, "issues": [], "recommended_tests": []}'
            )),
            FakeResponse(content=(
                '{"passed": true, "issues": [], "recommended_tests": []}'
            )),
        ])
        agent = Agent(
            self.config, self.memory, client=client,
            coding_review=True, coding_planning=False,
            automatic_review_checkpoint=False,
        )
        agent.toolbox = toolbox

        result = agent.run("Inspect, update, and test a.py")

        reads = [arguments for name, arguments in toolbox.calls if name == "read_file"]
        self.assertEqual(result.status, "complete")
        self.assertEqual(reads, [{"path": "a.py"}, {"path": "a.py"}])
        self.assertIn("original content", client.requests[-1]["messages"][-1]["content"])
    def test_unchanged_workspace_has_bounded_verification_churn(self):
        toolbox = FakeToolBox()
        process_calls = [
            tool_call("run_process", {"program": "python", "arguments": [f"test_{index}.py"]})
            for index in range(7)
        ]
        client = ScriptedClient([
            FakeResponse(tool_calls=[
                tool_call("read_file", {"path": "a.py"}),
                tool_call("write_file", {"path": "a.py", "content": "print('ok')"}),
                tool_call("read_file", {"path": "a.py"}),
            ]),
            FakeResponse(content="Done."),
            FakeResponse(tool_calls=process_calls),
            FakeResponse(content="Verified."),
            FakeResponse(content=(
                '{"passed": true, "issues": [], "recommended_tests": []}'
            )),
            FakeResponse(content=(
                '{"passed": true, "issues": [], "recommended_tests": []}'
            )),
        ])
        agent = Agent(
            self.config, self.memory, client=client,
            coding_review=True, coding_planning=False,
            automatic_review_checkpoint=False,
        )
        agent.toolbox = toolbox

        result = agent.run("Inspect, update, and test a.py")

        executed_processes = [name for name, _arguments in toolbox.calls if name == "run_process"]
        final_tools = {
            schema["function"]["name"] for schema in client.requests[3]["tools"]
        }
        self.assertEqual(result.status, "complete")
        self.assertEqual(len(executed_processes), 6)
        self.assertNotIn("run_process", final_tools)
    def test_coding_claim_requires_inspect_write_and_test(self):
        agent, _client = self.make_agent([
            FakeResponse(content="Done."),
            FakeResponse(content="Done."),
            FakeResponse(content="Done."),
            FakeResponse(content="Done."),
            FakeResponse(content="No evidence."),
        ])
        result = agent.run("Build a Python app project")
        self.assertEqual(result.status, "incomplete")
        self.assertIn("not inspected", result.reason)

    def test_escalation_recomputes_the_tool_budget(self):
        toolbox = FakeToolBox(failures={"bad_one", "bad_two"})
        second_batch = [
            tool_call("list_files", {"path": "."}),
            tool_call("write_file", {"path": "app.py", "content": "print('ok')"}),
            tool_call("run_process", {"program": "python", "arguments": ["app.py"]}),
            *[tool_call("recall", {"query": f"q{index}"}) for index in range(7)],
        ]
        agent, _client = self.make_agent([
            FakeResponse(tool_calls=[
                tool_call("bad_one", {}),
                tool_call("bad_two", {}),
            ]),
            FakeResponse(tool_calls=second_batch),
            FakeResponse(content="Built and verified."),
        ], toolbox)
        agent.router = ForcedEscalationRouter()
        result = agent.run("Build a Python app project")
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.tool_calls, 11)
        self.assertEqual(len(toolbox.calls), 11)
        self.assertNotIn("bad_one", {name for name, _arguments in toolbox.calls})
        self.assertNotIn("bad_two", {name for name, _arguments in toolbox.calls})

    def test_system_contract_survives_large_memory_and_history(self):
        for index in range(8):
            self.memory.remember_verified(
                f"memory {index} " + "x" * 7900,
                origin="verified_import",
            )
        conversation = self.memory.new_conversation("large")
        for index in range(20):
            self.memory.add_message(conversation, "user" if index % 2 == 0 else "assistant", "y" * 9000)
        agent, client = self.make_agent([FakeResponse(content="ok")])
        result = agent.run("Explain the workspace safety contract", conversation_id=conversation)
        sent = client.requests[0]["messages"]
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn("Enforced runtime contract", sent[0]["content"])
        self.assertLess(sum(len(json.dumps(item)) for item in sent), 12_000)
        self.assertEqual(result.status, "complete")

    def test_same_family_lessons_enter_prompt_only_after_strict_meta_gate(self):
        for index in range(20):
            success = index % 5 != 0
            prediction = self.memory.record_prediction(
                family="code_fix", profile="coding", model="m",
                predicted_success=0.8, predicted_steps=4,
                predicted_verification="process_evidence",
            )
            self.memory.resolve_prediction(
                prediction,
                actual_status="complete" if success else "failed",
                actual_steps=2,
                evidence_ok=success,
                failure_class=None if success else "unknown",
            )
        code_fix_conversation = self.memory.new_conversation("verified code-fix lesson")
        code_fix_prediction = self.memory.record_prediction(
            family="code_fix", profile="coding", model="m",
            predicted_success=0.8, predicted_steps=2,
            predicted_verification="process_evidence",
            conversation_id=code_fix_conversation,
        )
        self.memory.resolve_prediction(
            code_fix_prediction, actual_status="complete", actual_steps=2,
            evidence_ok=True,
        )
        code_fix_reflection = self.memory.record_reflection(
            status="complete", summary="Verified parser boundary repair.",
            improvements="", conversation_id=code_fix_conversation, tool_calls=2,
            prediction_id=code_fix_prediction,
        )
        self.memory.remember_verified_lesson(
            "Parser boundary sentinel: preserve the exact failing edge case.",
            family="code_fix", outcome_status="complete",
            reflection_id=code_fix_reflection,
        )
        code_build_conversation = self.memory.new_conversation("verified code-build lesson")
        code_build_prediction = self.memory.record_prediction(
            family="code_build", profile="coding", model="m",
            predicted_success=0.8, predicted_steps=2,
            predicted_verification="process_evidence",
            conversation_id=code_build_conversation,
        )
        self.memory.resolve_prediction(
            code_build_prediction, actual_status="complete", actual_steps=2,
            evidence_ok=True,
        )
        code_build_reflection = self.memory.record_reflection(
            status="complete", summary="Verified build result.",
            improvements="", conversation_id=code_build_conversation, tool_calls=2,
            prediction_id=code_build_prediction,
        )
        self.memory.remember_verified_lesson(
            "Parser boundary forbidden cross-family sentinel.",
            family="code_build", outcome_status="complete",
            reflection_id=code_build_reflection,
        )
        agent, _client = self.make_agent([FakeResponse(content="unused")])
        active = self.memory.record_prediction(
            family="code_fix", profile="coding", model="m",
            predicted_success=0.8, predicted_steps=4,
            predicted_verification="process_evidence",
        )
        agent._active_prediction_id = active
        prompt = agent.system_prompt(
            "Fix the parser boundary edge case", task_family="code_fix"
        )

        self.assertIn("Parser boundary sentinel", prompt)
        self.assertNotIn("forbidden cross-family", prompt)
        self.assertEqual(
            self.memory.db.execute(
                "SELECT COUNT(*) FROM lesson_applications WHERE prediction_id=?",
                (active,),
            ).fetchone()[0],
            1,
        )

        uncalibrated = self.memory.record_prediction(
            family="code_build", profile="coding", model="m",
            predicted_success=0.5, predicted_steps=4,
            predicted_verification="process_evidence",
        )
        agent._active_prediction_id = uncalibrated
        blocked_prompt = agent.system_prompt(
            "Build the parser boundary feature", task_family="code_build"
        )
        self.assertNotIn("forbidden cross-family", blocked_prompt)

    def test_system_prompt_uses_current_temporal_fact_and_labels_conflicts(self):
        self.memory.set_preference("response_tone", "formal", source="user")
        self.memory.set_preference("response_tone", "casual", source="user")
        self.memory.remember_claim(
            "lab service", "active port", "8443",
            source="probe A", authority="verified",
        )
        self.memory.remember_claim(
            "lab service", "active port", "9443",
            source="probe B", authority="verified",
        )
        agent, _client = self.make_agent([FakeResponse(content="unused")])

        prompt = agent.system_prompt("What tone and port should I use?")

        self.assertIn("<temporal_claims>", prompt)
        claim_text = prompt.split("<temporal_claims>", 1)[1].split(
            "</temporal_claims>", 1
        )[0]
        claims = json.loads(claim_text)
        values = {str(item["value"]) for item in claims}
        self.assertIn("casual", values)
        self.assertNotIn("formal", values)
        self.assertIn("8443", values)
        self.assertIn("9443", values)
        self.assertGreaterEqual(
            sum(item.get("status") == "disputed" for item in claims), 2
        )

    def test_foreground_result_binds_reflection_lesson_to_created_conversation(self):
        agent, _client = self.make_agent([FakeResponse(content="Boundary explained.")])

        result = agent.run("What makes an answer concise?")
        reflection_id = record_result_reflection(self.memory, result)

        self.assertIsInstance(result.conversation_id, int)
        lesson = self.memory.db.execute(
            """SELECT family, outcome_status, reflection_id FROM memories
               WHERE reflection_id=?""",
            (reflection_id,),
        ).fetchone()
        self.assertIsNotNone(lesson)
        self.assertEqual(lesson["family"], "conversation")
        self.assertEqual(lesson["outcome_status"], "complete")

    def test_specialist_is_peer_blind_cannot_delegate_and_rejects_other_purposes(self):
        agent, client = self.make_agent([FakeResponse(content="unused")])
        agent.set_specialist("coding")
        prompt = agent.system_prompt(
            "Fix the Python parser and run its tests.", task_family="code_fix"
        )
        self.assertIn("You are Forge", prompt)
        self.assertIn("sole orchestrator", prompt)
        for peer_name in ("Archivist", "Sentinel", "Relay", "Steward"):
            self.assertNotIn(peer_name, prompt)
        schemas = agent._schemas_for_state(
            research_mode=False,
            web_tainted=False,
            local_tainted=False,
            allow_write=True,
            allow_execution=True,
            allow_memory_write=False,
            allow_computer_files=False,
            allow_external_mutation=False,
            allow_self_inspection=False,
        )
        names = {item["function"]["name"] for item in schemas}
        self.assertNotIn("delegate_specialist", names)
        self.assertNotIn("specialist_reports", names)
        self.assertNotIn("recall", names)
        self.assertNotIn("session_search", names)

        result = agent.run("Research the latest database releases with citations.")
        self.assertEqual(result.status, "incomplete")
        self.assertIn("outside the Forge specialist", result.reason)
        self.assertEqual(client.requests, [])

    def test_main_agent_exposes_delegation_tools_only_when_explicitly_requested(self):
        agent, _client = self.make_agent([], toolbox=DelegatingFakeToolBox())
        common = dict(
            research_mode=False,
            web_tainted=False,
            local_tainted=False,
            allow_write=False,
            allow_execution=False,
            allow_memory_write=False,
        )
        hidden = {
            item["function"]["name"]
            for item in agent._schemas_for_state(**common)
        }
        exposed = {
            item["function"]["name"]
            for item in agent._schemas_for_state(**common, allow_delegation=True)
        }
        self.assertTrue({"delegate_specialist", "specialist_reports"}.isdisjoint(hidden))
        self.assertTrue({"delegate_specialist", "specialist_reports"}.issubset(exposed))

    def test_simple_dns_explanation_does_not_spawn_a_specialist(self):
        (self.data_dir / "worker.heartbeat").write_text(
            f"{time.time():.6f} 123 worker:test\n",
            encoding="utf-8",
        )
        toolbox = DelegatingFakeToolBox()
        client = ScriptedClient([FakeResponse(content="DNS maps names to network addresses.")])
        agent = Agent(
            self.config,
            self.memory,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = toolbox

        result = agent.run("Explain DNS simply in two sentences.")

        self.assertEqual(result.status, "complete")
        self.assertFalse(any(name == "delegate_specialist" for name, _ in toolbox.calls))
        self.assertEqual(len(client.requests), 1)

    def test_substantial_foreground_task_automatically_uses_specialist_report(self):
        (self.data_dir / "worker.heartbeat").write_text(
            f"{time.time():.6f} 123 worker:test\n",
            encoding="utf-8",
        )
        events = []
        toolbox = DelegatingFakeToolBox()
        client = ScriptedClient([
            FakeResponse(content="Start with default-deny and test recovery first.")
        ])
        agent = Agent(
            self.config,
            self.memory,
            events.append,
            client=client,
            coding_review=False,
            coding_planning=False,
        )
        agent.toolbox = toolbox

        result = agent.run(
            "Give me a defensive cybersecurity assessment for my isolated lab."
        )

        self.assertEqual(result.status, "complete")
        self.assertIn(("specialist_reports", {"task_id": 42, "limit": 1}), toolbox.calls)
        delegated = next(
            arguments for name, arguments in toolbox.calls
            if name == "delegate_specialist"
        )
        self.assertIn("read-only", delegated["task"])
        self.assertIn("defensive cybersecurity", delegated["task"])
        request_text = json.dumps(client.requests[0]["messages"])
        self.assertIn("untrusted_specialist_report", request_text)
        self.assertIn("default-deny rules", request_text)
        self.assertTrue(any("specialist delegated - Sentinel" in event for event in events))
        self.assertTrue(any("specialist report received - Sentinel" in event for event in events))

    def test_contextual_build_consultation_routes_to_forge_without_reclassifying_anaphora(self):
        consultation = Agent._specialist_consultation_prompt(
            "code_build",
            "now just do it",
        )

        selected = specialist_for_prompt(consultation)
        self.assertIsNotNone(selected)
        self.assertEqual(selected.key, "coding")
        self.assertIn("Software code build", consultation)

    def test_specialist_consultation_is_runtime_enforced_read_only(self):
        agent, client = self.make_agent([
            FakeResponse(content="Inspect the entry point and verify its public contract.")
        ])
        agent.set_specialist("coding")

        result = agent.run(
            "JARVIS specialist consultation (read-only; no mutations or process execution).\n"
            "Assigned family: code_build. Specialist purpose: software implementation.\n"
            "Analyze how to build the requested Python application and report to JARVIS."
        )

        self.assertEqual(result.status, "complete")
        offered = {
            schema["function"]["name"] for schema in client.requests[0]["tools"]
        }
        self.assertTrue({"list_files", "read_file"} <= offered)
        self.assertFalse(
            offered & {"write_file", "edit_file", "run_process"}
        )


if __name__ == "__main__":
    unittest.main()
