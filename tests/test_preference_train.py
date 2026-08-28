from __future__ import annotations

import builtins
import hashlib
import json
import os
import shutil
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from jarvis.preference_train import (
    DEFAULT_BASE_MODEL,
    _parser,
    _training_rows,
    load_preference_bundle,
    main as preference_train_main,
)


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record(index: int, split: str, constitution_sha256: str, *, family: str | None = None):
    family = family or f"{split}-family-{index}"
    chosen = json.dumps(
        {"response": f"safe answer {index}", "tool_calls": []},
        sort_keys=True,
        separators=(",", ":"),
    )
    rejected = json.dumps(
        {"response": f"unsafe answer {index}", "tool_calls": []},
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "prompt": [
            {"role": "system", "content": "Follow the JARVIS constitution."},
            {"role": "user", "content": f"Handle scenario {split} {index}."},
        ],
        "chosen": [{"role": "assistant", "content": chosen}],
        "rejected": [{"role": "assistant", "content": rejected}],
        "metadata": {
            "example_id": f"example-{split}-{index}",
            "scenario_id": f"scenario-{split}-{index}",
            "family": family,
            "split": split,
            "constitution_sha256": constitution_sha256,
            "pair_sha256": hashlib.sha256(
                f"pair:{split}:{index}".encode("utf-8")
            ).hexdigest(),
            "semantic_safety_guarantee": False,
        },
    }


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _write_bundle(
    root: Path,
    *,
    train: int = 70,
    validation: int = 15,
    test: int = 15,
) -> tuple[Path, Path]:
    constitution = root / "CONSTITUTION.md"
    constitution.write_text(
        "# JARVIS Constitution\n\nC01 preserve authority.\nC09 verify work.\n",
        encoding="utf-8",
    )
    constitution_sha256 = _digest(constitution)
    counts = {"train": train, "validation": validation, "test": test}
    files = {}
    for split, count in counts.items():
        path = root / f"{split}.jsonl"
        _write_jsonl(
            path,
            [_record(index, split, constitution_sha256) for index in range(count)],
        )
        files[split] = {
            "file": path.name,
            "examples": count,
            "sha256": _digest(path),
        }
    manifest = {
        "format_version": 1,
        "dataset_kind": "dpo",
        "constitution_sha256": constitution_sha256,
        "selection": {
            "hard_checks_passed_only": True,
            "family_grouped_splits": True,
            "preference_family_sample_cap": 8,
        },
        "total_examples": sum(counts.values()),
        "data_volume_ready": True,
        "files": files,
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root / "train.jsonl", constitution


def _rewrite_manifest_hash(root: Path, split: str) -> None:
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][split]["sha256"] = _digest(root / f"{split}.jsonl")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class PreferenceTrainingTests(unittest.TestCase):
    def setUp(self):
        self.root = TEMP_ROOT / f"preference-{os.getpid()}-{self._testMethodName}"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_complete_hash_locked_bundle_passes_readiness(self):
        train, constitution = _write_bundle(self.root)
        bundle = load_preference_bundle(train, constitution)

        self.assertTrue(bundle.ready)
        self.assertEqual(bundle.counts, {"train": 70, "validation": 15, "test": 15})
        self.assertEqual(bundle.manifest["dataset_kind"], "dpo")
        self.assertEqual(bundle.constitution_sha256, _digest(constitution))

    def test_readiness_is_stdlib_only_and_dry_run_is_default(self):
        train, constitution = _write_bundle(self.root)
        output = self.root / "candidate"
        original_import = builtins.__import__
        forbidden = {"torch", "transformers", "peft", "trl", "datasets"}

        def guarded_import(name, *args, **kwargs):
            if name.partition(".")[0] in forbidden:
                raise AssertionError(f"dry-run imported {name}")
            return original_import(name, *args, **kwargs)

        stdout = StringIO()
        with patch("builtins.__import__", side_effect=guarded_import), redirect_stdout(stdout):
            preference_train_main([
                "--dataset", str(train),
                "--constitution", str(constitution),
                "--output", str(output),
            ])

        self.assertIn("gate passed", stdout.getvalue().lower())
        self.assertFalse(output.exists())
        parsed = _parser().parse_args(["--dataset", str(train), "--output", str(output)])
        self.assertEqual(parsed.base_model, DEFAULT_BASE_MODEL)
        self.assertFalse(parsed.train)

    def test_minimum_counts_are_enforced_for_every_split(self):
        train, constitution = _write_bundle(self.root, train=69, validation=9, test=9)
        bundle = load_preference_bundle(train, constitution)
        blockers = " ".join(bundle.blockers)

        self.assertFalse(bundle.ready)
        self.assertIn("100 total", blockers)
        self.assertIn("70 train", blockers)
        self.assertIn("10 validation", blockers)
        self.assertIn("10 test", blockers)

    def test_kind_selection_and_current_constitution_are_mandatory(self):
        train, constitution = _write_bundle(self.root)
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["dataset_kind"] = "sft"
        manifest["selection"]["hard_checks_passed_only"] = False
        manifest["selection"]["family_grouped_splits"] = False
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        constitution.write_text("# Replaced constitution\n", encoding="utf-8")

        blockers = " ".join(load_preference_bundle(train, constitution).blockers)
        self.assertIn("dataset_kind", blockers)
        self.assertIn("hard check", blockers)
        self.assertIn("family-grouped", blockers)
        self.assertIn("constitution SHA-256", blockers)

    def test_exporter_volume_readiness_and_family_cap_declaration_are_mandatory(self):
        train, constitution = _write_bundle(self.root)
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["data_volume_ready"] = False
        manifest["selection"]["preference_family_sample_cap"] = 9
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        blockers = " ".join(load_preference_bundle(train, constitution).blockers)
        self.assertIn("data_volume_ready", blockers)
        self.assertIn("8-pair-per-family", blockers)

    def test_every_split_hash_is_checked(self):
        train, constitution = _write_bundle(self.root)
        validation = self.root / "validation.jsonl"
        validation.write_text(
            validation.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )

        blockers = " ".join(load_preference_bundle(train, constitution).blockers)
        self.assertIn("validation.jsonl", blockers)
        self.assertIn("SHA-256", blockers)

    def test_duplicate_json_keys_are_rejected_instead_of_silently_overridden(self):
        train, constitution = _write_bundle(self.root)
        manifest_path = self.root / "manifest.json"
        original = manifest_path.read_text(encoding="utf-8").rstrip()
        duplicate = original[:-1] + ', "dataset_kind": "sft"}'
        manifest_path.write_text(duplicate, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            load_preference_bundle(train, constitution)

    def test_conversational_preferences_must_be_distinct_assistant_completions(self):
        train, constitution = _write_bundle(self.root)
        records = [json.loads(line) for line in train.read_text(encoding="utf-8").splitlines()]
        records[0]["chosen"] = records[0]["rejected"]
        _write_jsonl(train, records)
        _rewrite_manifest_hash(self.root, "train")

        with self.assertRaisesRegex(ValueError, "identical preferences"):
            load_preference_bundle(train, constitution)

        records[0]["chosen"] = [{"role": "user", "content": "not an assistant"}]
        _write_jsonl(train, records)
        _rewrite_manifest_hash(self.root, "train")
        with self.assertRaisesRegex(ValueError, "assistant role"):
            load_preference_bundle(train, constitution)

    def test_native_tool_call_pairs_validate_and_reach_the_trainer(self):
        train, constitution = _write_bundle(self.root)
        records = [json.loads(line) for line in train.read_text(encoding="utf-8").splitlines()]
        tool = {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read one bounded workspace file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "maxLength": 240}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }
        records[0]["tools"] = [tool]
        records[0]["chosen"] = [{
            "role": "assistant",
            "content": "I will inspect the approved file.",
            "tool_calls": [{
                "function": {"name": "read_file", "arguments": {"path": "README.md"}}
            }],
        }]
        _write_jsonl(train, records)
        _rewrite_manifest_hash(self.root, "train")

        bundle = load_preference_bundle(train, constitution)
        row = _training_rows(bundle.train)[0]
        self.assertEqual(row["tools"], [tool])
        self.assertEqual(
            row["chosen"][0]["tool_calls"][0]["function"]["name"],
            "read_file",
        )

    def test_tool_calls_must_reference_declared_tools_and_match_their_schema(self):
        train, constitution = _write_bundle(self.root)
        records = [json.loads(line) for line in train.read_text(encoding="utf-8").splitlines()]
        records[0]["tools"] = [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read one bounded workspace file.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }]
        records[0]["chosen"] = [{
            "role": "assistant",
            "content": "Calling a tool.",
            "tool_calls": [{
                "function": {"name": "write_file", "arguments": {"path": "x"}}
            }],
        }]
        _write_jsonl(train, records)
        _rewrite_manifest_hash(self.root, "train")
        with self.assertRaisesRegex(ValueError, "undeclared tool"):
            load_preference_bundle(train, constitution)

        records[0]["chosen"][0]["tool_calls"][0]["function"] = {
            "name": "read_file", "arguments": {"wrong": "x"},
        }
        _write_jsonl(train, records)
        _rewrite_manifest_hash(self.root, "train")
        with self.assertRaisesRegex(ValueError, "missing required arguments"):
            load_preference_bundle(train, constitution)

    def test_family_leakage_across_splits_is_rejected(self):
        train, constitution = _write_bundle(self.root)
        first_train = json.loads(train.read_text(encoding="utf-8").splitlines()[0])
        validation = self.root / "validation.jsonl"
        records = [
            json.loads(line) for line in validation.read_text(encoding="utf-8").splitlines()
        ]
        records[0]["metadata"]["family"] = first_train["metadata"]["family"]
        _write_jsonl(validation, records)
        _rewrite_manifest_hash(self.root, "validation")

        with self.assertRaisesRegex(ValueError, "Family leakage"):
            load_preference_bundle(train, constitution)

    def test_actual_scenario_family_diversity_and_family_cap_are_enforced(self):
        train, constitution = _write_bundle(self.root)
        for split in ("train", "validation", "test"):
            path = self.root / f"{split}.jsonl"
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            for index, record in enumerate(records):
                record["metadata"]["scenario_id"] = f"scenario-{index % 19}"
                record["metadata"]["family"] = f"{split}-family-{index % 3}"
            _write_jsonl(path, records)
            _rewrite_manifest_hash(self.root, split)

        blockers = " ".join(load_preference_bundle(train, constitution).blockers)
        self.assertIn("20 unique scenarios", blockers)
        self.assertIn("10 unique families", blockers)
        self.assertIn("more than 8 preference pairs", blockers)

    def test_explicit_training_refuses_unready_data_before_trainer_import(self):
        train, constitution = _write_bundle(self.root, train=2, validation=1, test=1)
        with patch("jarvis.preference_train._train_candidate") as trainer:
            with self.assertRaisesRegex(SystemExit, "not ready"):
                preference_train_main([
                    "--dataset", str(train),
                    "--constitution", str(constitution),
                    "--output", str(self.root / "candidate"),
                    "--revision", "a" * 40,
                    "--train",
                ])
        trainer.assert_not_called()
        self.assertFalse((self.root / "candidate").exists())

    def test_successful_candidate_writes_auditable_manifest_without_deployment(self):
        train, constitution = _write_bundle(self.root)
        output = self.root / "candidate"

        def fake_train(args, bundle, run_output):
            adapter = run_output / "adapter"
            adapter.mkdir()
            (adapter / "adapter_config.json").write_text("{}\n", encoding="utf-8")
            return {
                "adapter_directory": "adapter",
                "resolved_base_revision": "a" * 40,
                "versions": {"trl": "test"},
                "training_metrics": {"train_loss": 0.25},
            }

        with patch("jarvis.preference_train._train_candidate", side_effect=fake_train):
            with redirect_stdout(StringIO()):
                preference_train_main([
                    "--dataset", str(train),
                    "--constitution", str(constitution),
                    "--output", str(output),
                    "--revision", "a" * 40,
                    "--train",
                ])

        run_manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(run_manifest["base_model"], "Qwen/Qwen3.5-9B")
        self.assertEqual(run_manifest["requested_base_revision"], "a" * 40)
        self.assertEqual(run_manifest["resolved_base_revision"], "a" * 40)
        self.assertEqual(run_manifest["dataset"]["splits"]["test"]["examples"], 15)
        self.assertEqual(run_manifest["dataset"]["constitution_sha256"], _digest(constitution))
        self.assertEqual(run_manifest["method"]["objective"], "dpo")
        self.assertFalse(run_manifest["automatic_ollama_deployment"])
        self.assertFalse(run_manifest["automatic_model_promotion"])


if __name__ == "__main__":
    unittest.main()
