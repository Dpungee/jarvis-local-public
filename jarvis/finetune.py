from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import sys
from pathlib import Path
from typing import Any


MIN_TOTAL_EXAMPLES = 100
MIN_TRAIN_EXAMPLES = 70
MIN_VALIDATION_EXAMPLES = 10
MIN_TEST_EXAMPLES = 10
MAX_DATASET_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
VERIFIED_MEMORY_FORMAT_VERSION = 4
VERIFIED_MEMORY_QUALITY_CONTRACT = 1
_ROLES = frozenset({"system", "user", "assistant", "tool"})


def _valid_messages(messages: Any) -> bool:
    if not isinstance(messages, list) or len(messages) < 2:
        return False
    roles: list[str] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") not in _ROLES:
            return False
        role = message["role"]
        content = message.get("content")
        if not isinstance(content, str):
            return False
        if role == "system" and index != 0:
            return False
        if role == "assistant" and not content and not isinstance(message.get("tool_calls"), list):
            return False
        if role == "tool" and not isinstance(message.get("tool_call_id"), str):
            return False
        roles.append(role)
    first_non_system = 1 if roles[0] == "system" else 0
    return (
        first_non_system < len(roles)
        and roles[first_non_system] == "user"
        and roles[-1] == "assistant"
        and bool(messages[-1]["content"])
        and "user" in roles
        and "assistant" in roles
    )


def _load_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Dataset must be an ordinary file: {path}")
    if path.stat().st_size > MAX_DATASET_BYTES:
        raise ValueError(f"Dataset exceeds {MAX_DATASET_BYTES} bytes: {path}")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on dataset line {line_number}") from exc
            messages = record.get("messages") if isinstance(record, dict) else None
            if not isinstance(record, dict) or not _valid_messages(messages):
                raise ValueError(f"Invalid chat record on dataset line {line_number}")
            tools = record.get("tools")
            if tools is not None and (
                not isinstance(tools, list) or not all(isinstance(tool, dict) for tool in tools)
            ):
                raise ValueError(f"Invalid tools on dataset line {line_number}")
            records.append(record)
    if not records:
        raise ValueError("The training split is empty; collect more verified examples first")
    return records


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _training_bundle_blockers(dataset_path: Path, records: list[dict[str, Any]]) -> list[str]:
    manifest_path = dataset_path.with_name("manifest.json")
    if (
        not manifest_path.is_file()
        or manifest_path.is_symlink()
        or manifest_path.stat().st_size > MAX_MANIFEST_BYTES
    ):
        return ["A manifest.json from the verified exporter is required for real training."]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest["files"]
        train = files["train"]
        validation = files["validation"]
        test = files["test"]
        total = int(manifest["total_examples"])
        counts = {
            "train": int(train["examples"]),
            "validation": int(validation["examples"]),
            "test": int(test["examples"]),
        }
        selection = manifest["selection"]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ["manifest.json is malformed or incomplete."]
    blockers: list[str] = []
    if not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("constitution_sha256", ""))):
        blockers.append("The manifest is not bound to a valid JARVIS constitution hash.")
    verified_memory_export = (
        manifest.get("format_version") == VERIFIED_MEMORY_FORMAT_VERSION
        and selection.get("verified_only") is True
        and float(selection.get("minimum_quality", 0.0)) >= 0.8
        and selection.get("authoritative_web_sources") is True
        and selection.get("current_quality_contract")
        == VERIFIED_MEMORY_QUALITY_CONTRACT
        and selection.get("prompt_grouped_splits") is True
    )
    reward_verified_export = (
        selection.get("passed_only") is True
        and float(selection.get("exact_reward", 0.0)) == 1.0
        and selection.get("family_grouped_splits") is True
    )
    if not (verified_memory_export or reward_verified_export):
        blockers.append("The manifest does not prove verified or exact-reward data selection.")
    if total != sum(counts.values()):
        blockers.append("The manifest total does not equal its split counts.")
    if dataset_path.name != train.get("file") or train.get("sha256") != _sha256(dataset_path):
        blockers.append("The training file does not match the hash recorded in manifest.json.")
    if len(records) != counts["train"]:
        blockers.append("The parsed training count does not match manifest.json.")
    for split, _minimum in (
        ("validation", MIN_VALIDATION_EXAMPLES),
        ("test", MIN_TEST_EXAMPLES),
    ):
        details = files[split]
        filename = details.get("file")
        if filename != f"{split}.jsonl":
            blockers.append(f"The {split} filename in manifest.json is unsafe.")
            continue
        split_path = dataset_path.parent / filename
        if not split_path.is_file() or split_path.is_symlink():
            blockers.append(f"The {split} file is missing or is not an ordinary file.")
            continue
        if details.get("sha256") != _sha256(split_path):
            blockers.append(f"The {split} file does not match its manifest hash.")
            continue
        try:
            parsed_count = len(_load_records(split_path))
        except ValueError:
            blockers.append(f"The {split} file contains invalid chat records.")
            continue
        if parsed_count != counts[split]:
            blockers.append(f"The parsed {split} count does not match manifest.json.")
    if total < MIN_TOTAL_EXAMPLES:
        blockers.append(f"Need at least {MIN_TOTAL_EXAMPLES} total verified examples; found {total}.")
    if counts["train"] < MIN_TRAIN_EXAMPLES:
        blockers.append(f"Need at least {MIN_TRAIN_EXAMPLES} train examples; found {counts['train']}.")
    if counts["validation"] < MIN_VALIDATION_EXAMPLES:
        blockers.append(
            f"Need at least {MIN_VALIDATION_EXAMPLES} validation examples; found {counts['validation']}."
        )
    if counts["test"] < MIN_TEST_EXAMPLES:
        blockers.append(f"Need at least {MIN_TEST_EXAMPLES} test examples; found {counts['test']}.")
    return blockers


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train a QLoRA adapter from a verified JARVIS JSONL export"
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--base-model", default="Qwen/Qwen3.5-9B")
    parser.add_argument(
        "--revision",
        help="Required immutable 40-character Hugging Face commit for real training",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--max-length", type=int, default=768)
    parser.add_argument("--gradient-accumulation", type=int, default=16)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--rank", type=int, default=16)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.dataset.is_symlink():
        raise SystemExit("Dataset must not be a symbolic link")
    dataset_path = args.dataset.resolve()
    if not dataset_path.is_file():
        raise SystemExit(f"Dataset does not exist: {dataset_path}")
    if args.epochs <= 0 or not 128 <= args.max_length <= 8192:
        raise SystemExit("epochs must be positive and max-length must be from 128 to 8192")
    if args.gradient_accumulation < 1 or not 1 <= args.rank <= 256:
        raise SystemExit("gradient accumulation and rank must be positive")
    records = _load_records(dataset_path)
    print(f"Validated {len(records)} training examples ({_sha256(dataset_path)})")
    blockers = _training_bundle_blockers(dataset_path, records)
    if args.dry_run:
        if blockers:
            print("Candidate training is gated:")
            for blocker in blockers:
                print(f"  - {blocker}")
        else:
            print("Candidate training gate passed.")
        return
    if blockers:
        raise SystemExit("Candidate training is not ready:\n- " + "\n- ".join(blockers))
    if not isinstance(args.revision, str) or re.fullmatch(
        r"[0-9a-fA-F]{40}", args.revision
    ) is None:
        raise SystemExit(
            "Real training requires --revision with an immutable 40-character "
            "Hugging Face commit hash"
        )

    try:
        import torch
        import transformers
        import peft
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        from torch.utils.data import Dataset
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
            Trainer,
            TrainingArguments,
            default_data_collator,
        )
    except ImportError as exc:
        raise SystemExit(
            "Training dependencies are missing. Install this project with its 'training' extra."
        ) from exc

    if not torch.cuda.is_available():
        raise SystemExit("QLoRA training requires a CUDA GPU in this workflow")

    tokenizer = AutoTokenizer.from_pretrained(
        args.base_model,
        revision=args.revision,
        use_fast=True,
        trust_remote_code=False,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token

    class ChatDataset(Dataset):
        def __init__(self, items: list[dict[str, Any]]) -> None:
            self.items = items

        def __len__(self) -> int:
            return len(self.items)

        @staticmethod
        def _ids(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> list[int]:
            template_args: dict[str, Any] = {
                "tokenize": True,
                "add_generation_prompt": False,
            }
            if tools:
                template_args["tools"] = tools
            rendered = tokenizer.apply_chat_template(messages, **template_args)
            if isinstance(rendered, dict):
                rendered = rendered.get("input_ids")
            if hasattr(rendered, "tolist"):
                rendered = rendered.tolist()
            if isinstance(rendered, list) and rendered and isinstance(rendered[0], list):
                rendered = rendered[0]
            if not isinstance(rendered, list) or not all(isinstance(token, int) for token in rendered):
                raise ValueError("The tokenizer returned an unsupported chat-template result")
            return rendered

        def __getitem__(self, index: int) -> dict[str, Any]:
            record = self.items[index]
            messages = record["messages"]
            tools = record.get("tools")
            full_ids = self._ids(messages, tools)
            assistant_mask = [False] * len(full_ids)
            for message_index, message in enumerate(messages):
                if message["role"] != "assistant":
                    continue
                start = len(self._ids(messages[:message_index], tools)) if message_index else 0
                end = len(self._ids(messages[: message_index + 1], tools))
                for token_index in range(min(start, len(full_ids)), min(end, len(full_ids))):
                    assistant_mask[token_index] = True
            input_ids = full_ids[: args.max_length]
            labels = [token if assistant_mask[index] else -100 for index, token in enumerate(input_ids)]
            attention_mask = [1] * len(input_ids)
            padding = args.max_length - len(input_ids)
            input_ids += [tokenizer.pad_token_id] * padding
            attention_mask += [0] * padding
            labels += [-100] * padding
            if all(token == -100 for token in labels):
                raise ValueError(
                    "max-length removed all assistant tokens; increase it or shorten the example"
                )
            return {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "labels": labels,
            }

    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.float16,
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        revision=args.revision,
        quantization_config=quantization,
        device_map={"": 0},
        torch_dtype=torch.float16,
        trust_remote_code=False,
    )
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model = get_peft_model(
        model,
        LoraConfig(
            task_type="CAUSAL_LM",
            r=args.rank,
            lora_alpha=args.rank * 2,
            lora_dropout=0.05,
            bias="none",
            target_modules="all-linear",
        ),
    )
    model.print_trainable_parameters()

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    validation_path = dataset_path.with_name("validation.jsonl")
    validation_records = (
        _load_records(validation_path)
        if validation_path.is_file() and validation_path.stat().st_size
        else []
    )
    training_args = TrainingArguments(
        output_dir=str(output / "checkpoints"),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=args.gradient_accumulation,
        learning_rate=args.learning_rate,
        logging_steps=1,
        save_strategy="epoch",
        eval_strategy="epoch",
        fp16=True,
        gradient_checkpointing=True,
        optim="paged_adamw_8bit",
        report_to=[],
        remove_unused_columns=False,
        seed=42,
        data_seed=42,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=ChatDataset(records),
        eval_dataset=ChatDataset(validation_records),
        data_collator=default_data_collator,
    )
    trainer.train()
    adapter = output / "adapter"
    model.save_pretrained(adapter, safe_serialization=True)
    tokenizer.save_pretrained(adapter)
    manifest = {
        "base_model": args.base_model,
        "base_model_revision": args.revision.lower(),
        "dataset": str(dataset_path),
        "dataset_sha256": _sha256(dataset_path),
        "examples": len(records),
        "epochs": args.epochs,
        "max_length": args.max_length,
        "gradient_accumulation": args.gradient_accumulation,
        "learning_rate": args.learning_rate,
        "rank": args.rank,
        "python": sys.version,
        "platform": platform.platform(),
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "peft": peft.__version__,
    }
    (output / "training_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Adapter saved to {adapter}")


if __name__ == "__main__":
    main()
