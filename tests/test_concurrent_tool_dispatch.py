from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from jarvis.agent import Agent
from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.tools import ToolBox, _tool_call_target_sha256


TRACE_ID = "e" * 32


class _NoModelClient:
    def models(self, refresh=True):
        return ["qwen3.5:9b"]

    def chat(self, *args, **kwargs):
        raise AssertionError("the evidence collector must not call a model")


class _OverlappingFetch:
    """A web_fetch handler that only succeeds when every call overlaps."""

    def __init__(self, toolbox: ToolBox, parties: int):
        self.toolbox = toolbox
        self.barrier = threading.Barrier(parties, timeout=10)
        self.lock = threading.Lock()
        self.seen: list[dict[str, object]] = []

    def __call__(self, url: str, timeout_seconds: float = 45.0):
        with self.lock:
            self.seen.append({
                "url": url,
                "thread": threading.get_ident(),
                "trace_id": self.toolbox._run_trace_id.get(),
                "approval_context": self.toolbox._approval_execution_context.get(),
            })
        self.barrier.wait()
        return {
            "url": url,
            "untrusted": True,
            "content": (
                "Acme View 27 Monitor $429.99 USD In stock silver 27-inch 4K IPS "
                "USB-C 90W power delivery height-adjustable stand monitor"
            ),
            "format": "text",
        }


class ConcurrentToolDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="jarvis-concurrent-tools-")
        root = Path(self._temporary.name)
        workspace = root / "workspace"
        data = root / "data"
        workspace.mkdir()
        data.mkdir()
        self.config = replace(
            Config.load(),
            model="auto",
            workspace=workspace,
            data_dir=data,
            memory_embeddings="disabled",
            ollama_preload=False,
            vault_dir=None,
        )
        self.memory = Memory(data / "jarvis.db")

    def tearDown(self) -> None:
        self.memory.close()
        self._temporary.cleanup()

    def _fetch_rows(self) -> list[dict[str, object]]:
        return [
            {**row, "details": json.loads(str(row["details_json"]))}
            for row in self.memory.list_activity(limit=100)
            if row["category"] == "tool" and row["action"] == "web_fetch"
        ]

    def test_batch_preserves_host_permission_and_recording_wrapper(self) -> None:
        toolbox = ToolBox(self.config, self.memory)
        seen = []

        def guarded(name, arguments):
            seen.append((name, arguments))
            return json.dumps({"ok": False, "error": "permission revoked"})

        toolbox.execute = guarded
        calls = [("web_fetch", {"url": "https://example.com/"})] * 2
        results = toolbox.execute_concurrently(calls)
        self.assertEqual(seen, calls)
        self.assertTrue(all(json.loads(result)["ok"] is False for result in results))
        self.assertEqual(self._fetch_rows(), [])

    def test_concurrent_fetches_audit_every_url_on_the_owning_thread(self) -> None:
        toolbox = ToolBox(self.config, self.memory)
        urls = [f"https://shop.example/product/item-{index}" for index in range(3)]
        fetch = _OverlappingFetch(toolbox, parties=len(urls))
        toolbox.tools["web_fetch"] = replace(toolbox.tools["web_fetch"], function=fetch)
        calls = [("web_fetch", {"url": url, "timeout_seconds": 12}) for url in urls]

        with (
            self.assertNoLogs("jarvis.tools", level="ERROR"),
            toolbox.agent_context(1, trace_id=TRACE_ID),
            toolbox.approval_context("foreground"),
        ):
            responses = toolbox.execute_concurrently(calls)

        results = [json.loads(response) for response in responses]
        self.assertTrue(all(result["ok"] for result in results), results)
        self.assertEqual([result["result"]["url"] for result in results], urls)
        # The barrier only releases when all handlers are in flight together.
        self.assertNotIn(threading.get_ident(), {item["thread"] for item in fetch.seen})
        self.assertEqual({item["trace_id"] for item in fetch.seen}, {TRACE_ID})
        self.assertEqual(
            {item["approval_context"] for item in fetch.seen},
            {("foreground", None)},
        )

        rows = self._fetch_rows()
        self.assertEqual(len(rows), len(urls))
        self.assertEqual({row["status"] for row in rows}, {"complete"})
        self.assertEqual(
            {row["details"]["target_sha256"] for row in rows},
            {_tool_call_target_sha256(name, arguments) for name, arguments in calls},
        )
        for row in rows:
            self.assertEqual(row["details"]["trace_id"], TRACE_ID)
            self.assertTrue(row["details"]["handler_dispatched"])

    def test_refused_and_invalid_calls_are_audited_without_dispatch(self) -> None:
        toolbox = ToolBox(self.config, self.memory)
        fetch = _OverlappingFetch(toolbox, parties=1)
        toolbox.tools["web_fetch"] = replace(toolbox.tools["web_fetch"], function=fetch)

        with self.assertRaisesRegex(ValueError, "cannot be dispatched concurrently"):
            toolbox.execute_concurrently([
                ("web_fetch", {"url": "https://shop.example/product/a"}),
                ("list_files", {"path": "."}),
            ])
        self.assertEqual(self.memory.list_activity(limit=10), [])

        with toolbox.agent_context(1, trace_id=TRACE_ID):
            responses = toolbox.execute_concurrently([
                ("web_fetch", {"url": "https://shop.example/product/a"}),
                ("web_fetch", {"url": 7}),
            ])

        self.assertTrue(json.loads(responses[0])["ok"])
        self.assertFalse(json.loads(responses[1])["ok"])
        self.assertEqual([item["url"] for item in fetch.seen], ["https://shop.example/product/a"])
        rows = sorted(self._fetch_rows(), key=lambda row: row["id"])
        self.assertEqual([row["status"] for row in rows], ["complete", "failed"])
        self.assertEqual(
            [row["details"]["handler_dispatched"] for row in rows], [True, False]
        )
        self.assertEqual({row["details"]["trace_id"] for row in rows}, {TRACE_ID})

    def test_product_page_fetches_keep_audit_rows_and_trace_context(self) -> None:
        agent = Agent(
            self.config,
            self.memory,
            client=_NoModelClient(),
            coding_review=False,
            coding_planning=False,
        )
        toolbox = agent.toolbox
        urls = [
            "https://shop.example/product/acme-view-27",
            "https://maker.example/product/bravo-canvas-4k",
            "https://retail.example/product/cobalt-studio-27",
        ]

        def search(query: str, max_results: int = 5):
            return {
                "results": [
                    {
                        "title": "27-inch 4K IPS monitor",
                        "url": url,
                        "content": (
                            "$429 USD In stock silver 27-inch 4K IPS USB-C 90W "
                            "power delivery height-adjustable stand monitor"
                        ),
                    }
                    for url in urls
                ],
                "verified_pages": [],
                "fetch_errors": [],
            }

        fetch = _OverlappingFetch(toolbox, parties=len(urls))
        toolbox.tools["web_search"] = replace(toolbox.tools["web_search"], function=search)
        toolbox.tools["web_fetch"] = replace(toolbox.tools["web_fetch"], function=fetch)
        prompt = (
            "Find a silver 27-inch 4K IPS monitor with USB-C, 90W power delivery, "
            "and a height-adjustable stand under $600."
        )

        with (
            self.assertNoLogs("jarvis.tools", level="ERROR"),
            toolbox.agent_context(1, trace_id=TRACE_ID),
            toolbox.approval_context("foreground"),
        ):
            evidence, successful_tools, _verified, tool_calls = (
                agent._collect_quick_product_evidence(prompt)
            )

        fetch_evidence = [item for item in evidence if item["tool"] == "web_fetch"]
        self.assertEqual(
            sorted(item["arguments"]["url"] for item in fetch_evidence), sorted(urls)
        )
        self.assertTrue(all(item["success"] for item in fetch_evidence))
        self.assertIn("web_fetch", successful_tools)
        self.assertEqual(tool_calls, 2 + len(urls))
        self.assertNotIn(threading.get_ident(), {item["thread"] for item in fetch.seen})
        self.assertEqual({item["trace_id"] for item in fetch.seen}, {TRACE_ID})

        rows = self._fetch_rows()
        self.assertEqual(
            {row["details"]["target_sha256"] for row in rows},
            {
                _tool_call_target_sha256(
                    "web_fetch", {"url": url, "timeout_seconds": 12}
                )
                for url in urls
            },
        )
        self.assertEqual(len(rows), len(urls))
        for row in rows:
            self.assertEqual(row["status"], "complete")
            self.assertEqual(row["details"]["trace_id"], TRACE_ID)


if __name__ == "__main__":
    unittest.main()
