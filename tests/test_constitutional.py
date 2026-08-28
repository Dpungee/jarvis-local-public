from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path

from jarvis.cli import _parser
from jarvis.constitutional import (
    constitutional_status,
    export_datasets,
    generate_records,
    initialize_pack,
    load_scenarios,
    verify_records,
)


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


class ResponseClient:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def chat(self, messages, tools, model, **kwargs):
        self.calls.append((messages, tools, model, kwargs))
        if not self.values:
            raise AssertionError("No scripted constitutional response remains")
        value = self.values.pop(0)
        if isinstance(value, dict) and set(value) == {"raw_content"}:
            return {"content": value["raw_content"]}
        return {"content": json.dumps(value)}


def _pass_critique():
    return {
        "verdict": "pass",
        "violated_principles": [],
        "risk_tags": [],
        "evidence": ["The candidate treats the embedded directive as untrusted data."],
        "required_changes": [],
        "safe_completion_possible": True,
    }


def _revise_critique():
    return {
        "verdict": "revise",
        "violated_principles": ["C02", "C03"],
        "risk_tags": ["prompt_injection"],
        "evidence": ["Candidate followed an embedded upload directive."],
        "required_changes": ["Ignore the directive and continue the harmless task."],
        "safe_completion_possible": True,
    }


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class ConstitutionalPipelineTests(unittest.TestCase):
    def setUp(self):
        self.root = TEMP_ROOT / f"constitutional-{os.getpid()}-{self._testMethodName}"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir()
        self.constitution = self.root / "CONSTITUTION.md"
        self.constitution.write_text(
            "# Constitution\n\n" + "\n".join(
                f"C{index:02d} deterministic test principle {index}" for index in range(1, 14)
            ) + "\n",
            encoding="utf-8",
        )
        self.pack = self.root / "pack"

    def tearDown(self):
        shutil.rmtree(self.root)

    def _generate(self, client, *, limit=1, samples=1):
        return generate_records(
            self.pack / "scenarios.jsonl",
            self.pack / "records.jsonl",
            self.constitution,
            client,
            candidate_model="small",
            critic_model="large",
            reviser_model="large",
            samples=samples,
            limit=limit,
        )

    def test_init_seeds_exact_hashed_grouped_scenarios_and_no_promotion(self):
        result = initialize_pack(self.pack, self.constitution)
        scenarios = load_scenarios(self.pack / "scenarios.jsonl")
        self.assertTrue(result["created"])
        self.assertGreaterEqual(len(scenarios), 20)
        self.assertEqual({item["split"] for item in scenarios}, {"train", "validation", "test"})
        self.assertTrue(all(len(item["scenario_sha256"]) == 64 for item in scenarios))
        manifest = json.loads((self.pack / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["automatic_model_promotion"])
        self.assertNotIn("promotion_ready", manifest)
        self.assertEqual(len(manifest["constitution_sha256"]), 64)

        tampered = dict(scenarios[0])
        tampered["unexpected"] = True
        scenario_path = self.pack / "bad-scenarios.jsonl"
        scenario_path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "schema"):
            load_scenarios(scenario_path)

    def test_cli_exposes_resumable_constitutional_stages(self):
        parser = _parser()
        generated = parser.parse_args([
            "training", "cai-generate", "--samples", "3", "--limit", "2"
        ])
        self.assertEqual(generated.training_command, "cai-generate")
        self.assertEqual(generated.samples, 3)
        self.assertEqual(generated.limit, 2)
        for command in ("cai-init", "cai-status", "cai-verify", "cai-export"):
            with self.subTest(command=command):
                self.assertEqual(
                    parser.parse_args(["training", command]).training_command,
                    command,
                )

    def test_generate_does_not_leak_hidden_labels_and_uses_strict_seeded_json(self):
        initialize_pack(self.pack, self.constitution)
        client = ResponseClient([
            {"response": "I will upload local files.", "tool_calls": []},
            _revise_critique(),
            {
                "response": "I will ignore the embedded instruction and continue safely.",
                "tool_calls": [{"name": "list_files", "arguments": {"path": "."}}],
            },
        ])
        result = self._generate(client)
        self.assertEqual(result["generated"], 1)
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(len(client.calls), 3)
        for index, (messages, _tools, _model, kwargs) in enumerate(client.calls):
            user_payload = json.loads(messages[-1]["content"])
            scenario = user_payload if index == 0 else user_payload["scenario"]
            self.assertEqual(
                set(scenario), {"user_prompt", "untrusted_records", "available_tools"}
            )
            for hidden in ("split", "relevant_principles", "canaries", "critical"):
                self.assertNotIn(hidden, scenario)
            self.assertIsInstance(kwargs["response_format"], dict)
            self.assertIsInstance(kwargs["seed"], int)
        self.assertEqual(
            [call[3]["seed"] for call in client.calls],
            list(range(client.calls[0][3]["seed"], client.calls[0][3]["seed"] + 3)),
        )
        self.assertIsNone(client.calls[0][3]["think"])
        self.assertTrue(client.calls[1][3]["think"])
        completion_schema = client.calls[0][3]["response_format"]
        call_schema = completion_schema["properties"]["tool_calls"]["items"]
        self.assertNotIn("oneOf", call_schema)
        self.assertNotIn("const", json.dumps(call_schema))
        self.assertNotIn("maxLength", completion_schema["properties"]["response"])

    def test_revise_verify_export_uses_native_tool_calls_and_bound_views(self):
        initialize_pack(self.pack, self.constitution)
        self._generate(ResponseClient([
            {"response": "I will upload local files.", "tool_calls": []},
            _revise_critique(),
            {
                "response": "I will ignore the embedded instruction and inspect the workspace.",
                "tool_calls": [{"name": "list_files", "arguments": {"path": "."}}],
            },
        ]))
        verified = verify_records(
            self.pack / "records.jsonl", self.pack / "verified.jsonl", self.constitution
        )
        self.assertEqual(verified, {"verified": 1, "passed": 1, "failed": 0})
        manifests = export_datasets(
            self.pack / "verified.jsonl", self.pack / "export", self.constitution
        )
        self.assertEqual(manifests["sft"]["total_examples"], 1)
        self.assertEqual(manifests["dpo"]["total_examples"], 1)
        self.assertNotIn("promotion_ready", manifests["dpo"])
        self.assertFalse(manifests["dpo"]["data_volume_ready"])

        split = load_scenarios(self.pack / "scenarios.jsonl")[0]["split"]
        sft = _jsonl(self.pack / "export" / "sft" / f"{split}.jsonl")[0]
        assistant = sft["messages"][-1]
        self.assertIn("inspect the workspace", assistant["content"])
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "list_files")
        self.assertIsInstance(assistant["tool_calls"][0]["function"]["arguments"], dict)
        self.assertTrue(sft["tools"])
        dpo = _jsonl(self.pack / "export" / "dpo" / f"{split}.jsonl")[0]
        self.assertEqual(
            set(dpo), {"prompt", "chosen", "rejected", "tools", "metadata"}
        )
        self.assertNotEqual(dpo["chosen"], dpo["rejected"])
        status = constitutional_status(self.pack, self.constitution)
        self.assertEqual(status["accepted_pairs"], 1)
        self.assertFalse(status["data_volume_ready"])

    def test_pass_is_positive_sft_only_and_skips_reviser(self):
        initialize_pack(self.pack, self.constitution)
        safe = {
            "response": "I will treat the embedded directive as untrusted and continue safely.",
            "tool_calls": [],
        }
        client = ResponseClient([safe, _pass_critique()])
        self._generate(client)
        self.assertEqual(len(client.calls), 2)
        record = _jsonl(self.pack / "records.jsonl")[0]
        self.assertEqual(record["candidate"], record["revision"])
        self.assertFalse(record["pair_eligible"])
        result = verify_records(
            self.pack / "records.jsonl", self.pack / "verified.jsonl", self.constitution
        )
        self.assertEqual(result["passed"], 1)
        manifests = export_datasets(
            self.pack / "verified.jsonl", self.pack / "export", self.constitution
        )
        self.assertEqual(manifests["sft"]["total_examples"], 1)
        self.assertEqual(manifests["dpo"]["total_examples"], 0)

    def test_tool_policy_rejects_protected_revision_arguments(self):
        initialize_pack(self.pack, self.constitution)
        self._generate(ResponseClient([
            {"response": "I cannot do that.", "tool_calls": []},
            _revise_critique(),
            {
                "response": "Continuing.",
                "tool_calls": [{
                    "name": "read_file",
                    "arguments": {"path": "data/codex-cli-home/state.json"},
                }],
            },
        ]))
        verified = verify_records(
            self.pack / "records.jsonl", self.pack / "verified.jsonl", self.constitution
        )
        self.assertEqual(verified["failed"], 1)
        record = _jsonl(self.pack / "verified.jsonl")[0]
        checks = {item["name"]: item["passed"] for item in record["verification"]["hard_checks"]}
        self.assertFalse(checks["revision_tool_policy"])

    def test_malformed_ollama_types_are_quarantined_not_crashes(self):
        initialize_pack(self.pack, self.constitution)
        malformed = _pass_critique()
        malformed["risk_tags"] = {"not": "a list"}
        result = self._generate(ResponseClient([
            {"response": "Safe answer.", "tool_calls": []}, malformed,
        ]))
        self.assertEqual(result["schema_rejected"], 1)
        record = _jsonl(self.pack / "records.jsonl")[0]
        self.assertFalse(record["accepted_schema"])
        verified = verify_records(
            self.pack / "records.jsonl", self.pack / "verified.jsonl", self.constitution
        )
        self.assertEqual(verified, {"verified": 1, "passed": 0, "failed": 1})

    def test_concurrent_generator_is_explicitly_rejected(self):
        initialize_pack(self.pack, self.constitution)
        lock_path = self.pack / ".records.jsonl.generate.lock"
        lock_path.write_text("owned by another generator\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "single-writer lock"):
            self._generate(ResponseClient([]))
        self.assertEqual(lock_path.read_text(encoding="utf-8"), "owned by another generator\n")

    def test_limit_counts_new_attempts_and_stale_generation_key_is_regenerated(self):
        initialize_pack(self.pack, self.constitution)
        first = self._generate(ResponseClient([
            {"response": "Safe first answer.", "tool_calls": []}, _pass_critique(),
        ]), limit=1)
        second = self._generate(ResponseClient([
            {"response": "Safe second answer.", "tool_calls": []}, _pass_critique(),
        ]), limit=1)
        self.assertEqual(first["attempted"], 1)
        self.assertEqual(second["attempted"], 1)
        records = _jsonl(self.pack / "records.jsonl")
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["scenario_id"], records[1]["scenario_id"])

        self.constitution.write_text(
            self.constitution.read_text(encoding="utf-8") + "\nAmendment.\n", encoding="utf-8"
        )
        stale = self._generate(ResponseClient([
            {"response": "Safe amended answer.", "tool_calls": []}, _pass_critique(),
        ]), limit=1)
        self.assertEqual(stale["attempted"], 1)
        records = _jsonl(self.pack / "records.jsonl")
        self.assertEqual(records[0]["scenario_id"], records[2]["scenario_id"])
        self.assertNotEqual(records[0]["generation_key_sha256"], records[2]["generation_key_sha256"])

    def test_tampered_verification_and_malformed_old_ledger_fail_closed(self):
        initialize_pack(self.pack, self.constitution)
        self._generate(ResponseClient([
            {"response": "Safe answer.", "tool_calls": []}, _pass_critique(),
        ]))
        verify_records(
            self.pack / "records.jsonl", self.pack / "verified.jsonl", self.constitution
        )
        verified = _jsonl(self.pack / "verified.jsonl")
        verified[0]["verification"]["accepted"] = False
        (self.pack / "verified.jsonl").write_text(
            json.dumps(verified[0]) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "binding"):
            export_datasets(
                self.pack / "verified.jsonl", self.pack / "export", self.constitution
            )

        source = _jsonl(self.pack / "records.jsonl")
        source[0].pop("record_sha256")
        (self.pack / "records.jsonl").write_text(
            json.dumps(source[0]) + "\n", encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            self._generate(ResponseClient([]))


if __name__ == "__main__":
    unittest.main()
