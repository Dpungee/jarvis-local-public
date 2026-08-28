from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FORMAT_VERSION = 1
MIN_TOTAL_EXAMPLES = 100
MIN_TRAIN_EXAMPLES = 70
MIN_VALIDATION_EXAMPLES = 10
MIN_TEST_EXAMPLES = 10
MIN_UNIQUE_SCENARIOS = 20
MIN_UNIQUE_FAMILIES = 10
MAX_PAIRS_PER_FAMILY = 8
MAX_DATASET_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_CONSTITUTION_BYTES = 32 * 1024
MAX_LINE_CHARS = 1_000_000
MAX_MESSAGES = 64
MAX_CONTENT_CHARS = 100_000
MAX_TOOLS = 64
MAX_TOOL_CALLS = 12
MAX_TOOL_DESCRIPTION_CHARS = 4_000
MAX_TOOL_SCHEMA_CHARS = 100_000
MAX_JSON_DEPTH = 16
MAX_JSON_ITEMS = 1_024
DEFAULT_BASE_MODEL = "Qwen/Qwen3.5-9B"
DEFAULT_CONSTITUTION = Path(__file__).with_name("CONSTITUTION.md")
_SPLITS = ("train", "validation", "test")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,119}$")
_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class PreferenceBundle:
    dataset_path: Path
    manifest_path: Path
    constitution_path: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    constitution_sha256: str
    train: tuple[dict[str, Any], ...]
    validation: tuple[dict[str, Any], ...]
    test: tuple[dict[str, Any], ...]
    blockers: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def counts(self) -> dict[str, int]:
        return {
            "train": len(self.train),
            "validation": len(self.validation),
            "test": len(self.test),
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _strict_json(source: str, *, label: str) -> Any:
    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"{label} contains duplicate JSON key {key!r}")
            value[key] = item
        return value

    def reject_constant(value: str) -> Any:
        raise ValueError(f"{label} contains non-finite JSON value {value}")

    try:
        return json.loads(
            source,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ordinary_file(path: Path, *, maximum_bytes: int, label: str) -> Path:
    path = Path(path).absolute()
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{label} must be an ordinary file: {path}")
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"Cannot inspect {label}: {path}") from exc
    if size > maximum_bytes:
        raise ValueError(f"{label} exceeds {maximum_bytes} bytes: {path}")
    return path


def _load_manifest(path: Path) -> dict[str, Any]:
    path = _ordinary_file(path, maximum_bytes=MAX_MANIFEST_BYTES, label="DPO manifest")
    try:
        value = _strict_json(path.read_text(encoding="utf-8"), label="manifest.json")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("manifest.json is not valid bounded UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("manifest.json must contain a JSON object")
    return value


def _bounded_json(value: Any, *, label: str, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError(f"{label} exceeds the JSON nesting limit")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if len(value) > MAX_CONTENT_CHARS or "\x00" in value:
            raise ValueError(f"{label} contains an oversized or invalid string")
        return
    if isinstance(value, int):
        if abs(value) > 2**63 - 1:
            raise ValueError(f"{label} contains an out-of-range integer")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return
    if isinstance(value, list):
        if len(value) > MAX_JSON_ITEMS:
            raise ValueError(f"{label} contains too many list items")
        for index, item in enumerate(value):
            _bounded_json(item, label=f"{label}[{index}]", depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_JSON_ITEMS:
            raise ValueError(f"{label} contains too many object fields")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 256 or "\x00" in key:
                raise ValueError(f"{label} contains an invalid object key")
            _bounded_json(item, label=f"{label}.{key}", depth=depth + 1)
        return
    raise ValueError(f"{label} contains a non-JSON value")


def _validate_schema_node(value: Any, *, label: str, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH or not isinstance(value, dict):
        raise ValueError(f"{label} must be a bounded JSON schema object")
    allowed = {
        "type", "properties", "required", "additionalProperties", "items",
        "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
        "enum", "description",
    }
    if not set(value).issubset(allowed):
        raise ValueError(f"{label} contains unsupported JSON schema keywords")
    schema_type = value.get("type")
    if schema_type not in {"object", "array", "string", "integer", "number", "boolean"}:
        raise ValueError(f"{label} has an unsupported JSON schema type")
    if schema_type == "object":
        properties = value.get("properties")
        required = value.get("required", [])
        if not isinstance(properties, dict) or len(properties) > MAX_JSON_ITEMS:
            raise ValueError(f"{label} has invalid object properties")
        if value.get("additionalProperties") is not False:
            raise ValueError(f"{label} must reject undeclared object properties")
        if (
            not isinstance(required, list)
            or len(required) > len(properties)
            or any(not isinstance(field, str) or field not in properties for field in required)
            or len(set(required)) != len(required)
        ):
            raise ValueError(f"{label} has an invalid required list")
        for field, child in properties.items():
            if not isinstance(field, str) or not _TOOL_NAME.fullmatch(field):
                raise ValueError(f"{label} has an invalid property name")
            _validate_schema_node(child, label=f"{label}.{field}", depth=depth + 1)
    elif schema_type == "array":
        if "items" not in value:
            raise ValueError(f"{label} array schema must declare items")
        _validate_schema_node(value["items"], label=f"{label}.items", depth=depth + 1)
    elif "properties" in value or "required" in value or "items" in value:
        raise ValueError(f"{label} has keywords that do not match its type")
    if "enum" in value:
        enum = value["enum"]
        if not isinstance(enum, list) or not 1 <= len(enum) <= MAX_JSON_ITEMS:
            raise ValueError(f"{label} has an invalid enum")
        _bounded_json(enum, label=f"{label}.enum", depth=depth + 1)
    for key in ("minimum", "maximum"):
        if key in value and (
            isinstance(value[key], bool)
            or not isinstance(value[key], (int, float))
            or not math.isfinite(value[key])
        ):
            raise ValueError(f"{label} has an invalid {key}")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in value and (
            isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0
        ):
            raise ValueError(f"{label} has an invalid {key}")


def _validate_schema_value(value: Any, schema: dict[str, Any], *, label: str) -> None:
    expected = schema["type"]
    matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }[expected]
    if not matches:
        raise ValueError(f"{label} does not match declared type {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{label} is not one of the declared enum values")
    if expected == "object":
        properties = schema["properties"]
        missing = [field for field in schema.get("required", []) if field not in value]
        unknown = [field for field in value if field not in properties]
        if missing:
            raise ValueError(f"{label} is missing required arguments: {', '.join(missing)}")
        if unknown:
            raise ValueError(f"{label} contains undeclared arguments: {', '.join(unknown)}")
        for field, item in value.items():
            _validate_schema_value(item, properties[field], label=f"{label}.{field}")
    elif expected == "array":
        minimum = schema.get("minItems", 0)
        maximum = schema.get("maxItems", MAX_JSON_ITEMS)
        if not minimum <= len(value) <= maximum:
            raise ValueError(f"{label} violates its declared item-count bounds")
        for index, item in enumerate(value):
            _validate_schema_value(item, schema["items"], label=f"{label}[{index}]")
    elif expected == "string":
        minimum = schema.get("minLength", 0)
        maximum = schema.get("maxLength", MAX_CONTENT_CHARS)
        if not minimum <= len(value) <= maximum:
            raise ValueError(f"{label} violates its declared string-length bounds")
    elif expected in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{label} is below its declared minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{label} exceeds its declared maximum")


def _validated_tools(value: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_TOOLS:
        raise ValueError(f"{label} must be a non-empty bounded tool-schema list")
    tools: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, tool in enumerate(value, 1):
        item_label = f"{label} tool {index}"
        if not isinstance(tool, dict) or set(tool) != {"type", "function"}:
            raise ValueError(f"{item_label} must contain exactly type and function")
        if tool.get("type") != "function":
            raise ValueError(f"{item_label} type must be function")
        function = tool.get("function")
        if not isinstance(function, dict) or set(function) != {
            "name", "description", "parameters",
        }:
            raise ValueError(
                f"{item_label} function must contain exactly name, description, and parameters"
            )
        name = function.get("name")
        description = function.get("description")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise ValueError(f"{item_label} has an invalid function name")
        if name in names:
            raise ValueError(f"{label} declares duplicate tool name {name}")
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > MAX_TOOL_DESCRIPTION_CHARS
            or "\x00" in description
        ):
            raise ValueError(f"{item_label} has an invalid description")
        _validate_schema_node(parameters, label=f"{item_label} parameters")
        if parameters.get("type") != "object":
            raise ValueError(f"{item_label} parameters must describe an object")
        _bounded_json(parameters, label=f"{item_label} parameters")
        if len(_canonical(parameters)) > MAX_TOOL_SCHEMA_CHARS:
            raise ValueError(f"{item_label} parameters exceed the schema size limit")
        names.add(name)
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        })
    return tools


def _validated_tool_calls(
    value: Any,
    *,
    declared_tools: dict[str, dict[str, Any]],
    label: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_TOOL_CALLS:
        raise ValueError(f"{label} must be a non-empty bounded tool-call list")
    calls: list[dict[str, Any]] = []
    for index, call in enumerate(value, 1):
        call_label = f"{label} call {index}"
        if not isinstance(call, dict) or set(call) != {"function"}:
            raise ValueError(f"{call_label} must contain exactly function")
        function = call.get("function")
        if not isinstance(function, dict) or set(function) != {"name", "arguments"}:
            raise ValueError(f"{call_label} function must contain exactly name and arguments")
        name, arguments = function.get("name"), function.get("arguments")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise ValueError(f"{call_label} has an invalid tool name")
        if name not in declared_tools:
            raise ValueError(f"{call_label} references undeclared tool {name}")
        if not isinstance(arguments, dict):
            raise ValueError(f"{call_label} arguments must be a JSON object")
        _bounded_json(arguments, label=f"{call_label} arguments")
        _validate_schema_value(
            arguments,
            declared_tools[name],
            label=f"{call_label} arguments",
        )
        calls.append({"function": {"name": name, "arguments": arguments}})
    return calls


def _validated_message(value: Any, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"role", "content"}:
        raise ValueError(f"{label} must contain exactly role and content")
    role, content = value.get("role"), value.get("content")
    if role not in {"system", "user", "assistant"}:
        raise ValueError(f"{label} has an unsupported role")
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content) > MAX_CONTENT_CHARS
        or "\x00" in content
    ):
        raise ValueError(f"{label} must have bounded non-empty text content")
    return {"role": role, "content": content}


def _validated_prompt(value: Any, *, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_MESSAGES:
        raise ValueError(f"{label} must be a bounded conversational prompt")
    messages = [
        _validated_message(message, label=f"{label} message {index}")
        for index, message in enumerate(value, 1)
    ]
    offset = 1 if messages[0]["role"] == "system" else 0
    if offset == len(messages) or messages[offset]["role"] != "user":
        raise ValueError(f"{label} must begin with an optional system message then a user message")
    expected = "user"
    for message in messages[offset:]:
        if message["role"] != expected:
            raise ValueError(f"{label} user and assistant turns must alternate")
        expected = "assistant" if expected == "user" else "user"
    if messages[-1]["role"] != "user":
        raise ValueError(f"{label} must end with a user message")
    return messages


def _validated_completion(
    value: Any,
    *,
    declared_tools: dict[str, dict[str, Any]],
    label: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != 1:
        raise ValueError(f"{label} must contain exactly one assistant completion")
    raw_message = value[0]
    if not isinstance(raw_message, dict):
        raise ValueError(f"{label} completion must be a message object")
    keys = set(raw_message)
    if keys not in ({"role", "content"}, {"role", "content", "tool_calls"}):
        raise ValueError(
            f"{label} completion must contain exactly role/content and optional tool_calls"
        )
    if raw_message.get("role") != "assistant":
        raise ValueError(f"{label} completion must have the assistant role")
    content = raw_message.get("content")
    has_calls = "tool_calls" in raw_message
    if (
        not isinstance(content, str)
        or len(content) > MAX_CONTENT_CHARS
        or "\x00" in content
        or (not content.strip() and not has_calls)
    ):
        raise ValueError(f"{label} assistant content must be bounded text or accompany tool calls")
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if has_calls:
        message["tool_calls"] = _validated_tool_calls(
            raw_message["tool_calls"],
            declared_tools=declared_tools,
            label=f"{label} tool_calls",
        )
    return [message]


def _validated_metadata(value: Any, *, split: str, constitution_sha256: str) -> dict[str, Any]:
    required = {
        "example_id", "scenario_id", "family", "split",
        "constitution_sha256", "pair_sha256",
    }
    allowed = required | {"semantic_safety_guarantee"}
    if not isinstance(value, dict) or not required.issubset(value) or not set(value).issubset(allowed):
        raise ValueError("DPO metadata is missing provenance fields")
    for key in ("example_id", "scenario_id", "family"):
        field = value.get(key)
        if not isinstance(field, str) or not _SAFE_ID.fullmatch(field):
            raise ValueError(f"DPO metadata has an invalid {key}")
    if value.get("split") != split:
        raise ValueError(f"DPO metadata split does not match {split}.jsonl")
    if value.get("constitution_sha256") != constitution_sha256:
        raise ValueError("DPO record constitution hash does not match manifest.json")
    pair_sha256 = value.get("pair_sha256")
    if not isinstance(pair_sha256, str) or not _SHA256.fullmatch(pair_sha256):
        raise ValueError("DPO metadata has an invalid pair_sha256")
    if "semantic_safety_guarantee" in value and value["semantic_safety_guarantee"] is not False:
        raise ValueError("DPO metadata must not claim an unverified semantic safety guarantee")
    return dict(value)


def _load_split(path: Path, *, split: str, constitution_sha256: str) -> list[dict[str, Any]]:
    path = _ordinary_file(path, maximum_bytes=MAX_DATASET_BYTES, label=f"{split} dataset")
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            if len(line) > MAX_LINE_CHARS:
                raise ValueError(f"{path.name} line {line_number} exceeds the record limit")
            try:
                value = _strict_json(line, label=f"line {line_number} of {path.name}")
            except ValueError as exc:
                raise ValueError(f"Invalid JSON on line {line_number} of {path.name}") from exc
            required_keys = {"prompt", "chosen", "rejected", "metadata"}
            if not isinstance(value, dict) or set(value) not in (
                required_keys,
                required_keys | {"tools"},
            ):
                raise ValueError(
                    f"Line {line_number} of {path.name} is not an explicit conversational DPO record"
                )
            tools = (
                _validated_tools(
                    value["tools"], label=f"{path.name} line {line_number} tools"
                )
                if "tools" in value
                else []
            )
            declared_tools = {
                tool["function"]["name"]: tool["function"]["parameters"]
                for tool in tools
            }
            prompt = _validated_prompt(value["prompt"], label=f"{path.name} line {line_number} prompt")
            chosen = _validated_completion(
                value["chosen"],
                declared_tools=declared_tools,
                label=f"{path.name} line {line_number} chosen",
            )
            rejected = _validated_completion(
                value["rejected"],
                declared_tools=declared_tools,
                label=f"{path.name} line {line_number} rejected",
            )
            if _canonical(chosen) == _canonical(rejected):
                raise ValueError(f"Line {line_number} of {path.name} has identical preferences")
            metadata = _validated_metadata(
                value["metadata"], split=split, constitution_sha256=constitution_sha256
            )
            record = {
                "prompt": prompt,
                "chosen": chosen,
                "rejected": rejected,
                "metadata": metadata,
            }
            if tools:
                record["tools"] = tools
            records.append(record)
    return records


def _manifest_file_details(manifest: dict[str, Any], split: str) -> dict[str, Any]:
    try:
        details = manifest["files"][split]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"manifest.json is missing the {split} file entry") from exc
    if not isinstance(details, dict):
        raise ValueError(f"manifest.json has an invalid {split} file entry")
    filename = details.get("file")
    count = details.get("examples")
    digest = details.get("sha256")
    if filename != f"{split}.jsonl":
        raise ValueError(f"manifest.json must name {split}.jsonl exactly")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"manifest.json has an invalid {split} example count")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError(f"manifest.json has an invalid {split} SHA-256")
    return details


def load_preference_bundle(dataset_path: Path, constitution_path: Path = DEFAULT_CONSTITUTION) -> PreferenceBundle:
    dataset_path = Path(dataset_path).absolute()
    if dataset_path.name != "train.jsonl":
        raise ValueError("The dataset entry point must be the exported train.jsonl")
    manifest_path = dataset_path.with_name("manifest.json")
    manifest = _load_manifest(manifest_path)
    constitution_path = _ordinary_file(
        constitution_path, maximum_bytes=MAX_CONSTITUTION_BYTES, label="Constitution"
    )
    try:
        constitution_text = constitution_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("Constitution must be valid UTF-8 text") from exc
    if not constitution_text.strip():
        raise ValueError("Constitution must not be empty")
    actual_constitution_sha256 = _sha256_file(constitution_path)
    declared_constitution_sha256 = manifest.get("constitution_sha256")
    if not isinstance(declared_constitution_sha256, str) or not _SHA256.fullmatch(
        declared_constitution_sha256
    ):
        raise ValueError("manifest.json has an invalid constitution_sha256")

    blockers: list[str] = []
    if manifest.get("format_version") != FORMAT_VERSION:
        blockers.append(f"manifest.json format_version must be {FORMAT_VERSION}.")
    if manifest.get("dataset_kind") != "dpo":
        blockers.append("manifest.json dataset_kind must be dpo.")
    selection = manifest.get("selection")
    if not isinstance(selection, dict) or selection.get("hard_checks_passed_only") is not True:
        blockers.append("The export must contain only preference pairs that passed every hard check.")
    if not isinstance(selection, dict) or selection.get("family_grouped_splits") is not True:
        blockers.append("The export must prove family-grouped train, validation, and test splits.")
    if not isinstance(selection, dict) or selection.get("preference_family_sample_cap") != MAX_PAIRS_PER_FAMILY:
        blockers.append(
            f"The export must declare the verified {MAX_PAIRS_PER_FAMILY}-pair-per-family cap."
        )
    if manifest.get("data_volume_ready") is not True:
        blockers.append("The constitutional exporter must mark data_volume_ready as true.")
    if declared_constitution_sha256 != actual_constitution_sha256:
        blockers.append("The export's constitution SHA-256 does not match the selected Constitution file.")

    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(_SPLITS):
        raise ValueError("manifest.json files must contain exactly train, validation, and test")

    split_records: dict[str, list[dict[str, Any]]] = {}
    declared_counts: dict[str, int] = {}
    for split in _SPLITS:
        details = _manifest_file_details(manifest, split)
        split_path = dataset_path.parent / details["file"]
        records = _load_split(
            split_path, split=split, constitution_sha256=declared_constitution_sha256
        )
        split_records[split] = records
        declared_counts[split] = details["examples"]
        if _sha256_file(split_path) != details["sha256"]:
            blockers.append(f"{split}.jsonl does not match its manifest SHA-256.")
        if len(records) != details["examples"]:
            blockers.append(f"The parsed {split} count does not match manifest.json.")

    families: dict[str, str] = {}
    family_counts: Counter[str] = Counter()
    scenario_ids: set[str] = set()
    example_ids: set[str] = set()
    pair_hashes: set[str] = set()
    rendered_pairs: set[str] = set()
    for split in _SPLITS:
        for record in split_records[split]:
            metadata = record["metadata"]
            family = metadata["family"]
            previous_split = families.setdefault(family, split)
            if previous_split != split:
                raise ValueError(f"Family leakage detected for {family}")
            family_counts[family] += 1
            scenario_ids.add(metadata["scenario_id"])
            example_id = metadata["example_id"]
            if example_id in example_ids:
                raise ValueError(f"Duplicate DPO example_id: {example_id}")
            example_ids.add(example_id)
            pair_sha256 = metadata["pair_sha256"]
            if pair_sha256 in pair_hashes:
                raise ValueError(f"Duplicate DPO pair_sha256: {pair_sha256}")
            pair_hashes.add(pair_sha256)
            rendered = _canonical([record["prompt"], record["chosen"], record["rejected"]])
            if rendered in rendered_pairs:
                raise ValueError("Duplicate prompt/chosen/rejected preference pair")
            rendered_pairs.add(rendered)

    actual_total = sum(len(split_records[split]) for split in _SPLITS)
    declared_total = manifest.get("total_examples")
    if isinstance(declared_total, bool) or not isinstance(declared_total, int):
        blockers.append("manifest.json total_examples must be an integer.")
    elif declared_total != sum(declared_counts.values()) or declared_total != actual_total:
        blockers.append("manifest.json total_examples does not equal its verified split counts.")
    if actual_total < MIN_TOTAL_EXAMPLES:
        blockers.append(f"Need at least {MIN_TOTAL_EXAMPLES} total preference pairs; found {actual_total}.")
    if len(scenario_ids) < MIN_UNIQUE_SCENARIOS:
        blockers.append(
            f"Need at least {MIN_UNIQUE_SCENARIOS} unique scenarios; found {len(scenario_ids)}."
        )
    if len(family_counts) < MIN_UNIQUE_FAMILIES:
        blockers.append(
            f"Need at least {MIN_UNIQUE_FAMILIES} unique families; found {len(family_counts)}."
        )
    oversized_families = sorted(
        family for family, count in family_counts.items() if count > MAX_PAIRS_PER_FAMILY
    )
    if oversized_families:
        blockers.append(
            f"No family may contribute more than {MAX_PAIRS_PER_FAMILY} preference pairs: "
            + ", ".join(oversized_families[:10])
            + (" ..." if len(oversized_families) > 10 else "")
            + "."
        )
    for split, minimum in (
        ("train", MIN_TRAIN_EXAMPLES),
        ("validation", MIN_VALIDATION_EXAMPLES),
        ("test", MIN_TEST_EXAMPLES),
    ):
        count = len(split_records[split])
        if count < minimum:
            blockers.append(f"Need at least {minimum} {split} preference pairs; found {count}.")

    return PreferenceBundle(
        dataset_path=dataset_path,
        manifest_path=manifest_path,
        constitution_path=constitution_path,
        manifest=manifest,
        manifest_sha256=_sha256_file(manifest_path),
        constitution_sha256=actual_constitution_sha256,
        train=tuple(split_records["train"]),
        validation=tuple(split_records["validation"]),
        test=tuple(split_records["test"]),
        blockers=tuple(blockers),
    )


def _training_rows(records: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in records:
        row = {
            "prompt": record["prompt"],
            "chosen": record["chosen"],
            "rejected": record["rejected"],
        }
        if "tools" in record:
            row["tools"] = record["tools"]
        rows.append(row)
    return rows


def _module_version(module: Any) -> str:
    value = getattr(module, "__version__", "unknown")
    return str(value)


def _train_candidate(
    args: argparse.Namespace,
    bundle: PreferenceBundle,
    output: Path,
) -> dict[str, Any]:
    # Imports stay delayed until every local integrity check passes and --train is explicit.
    try:
        import datasets
        import peft
        import torch
        import transformers
        import trl
        from datasets import Dataset
        from peft import LoraConfig
        from transformers import AutoTokenizer, BitsAndBytesConfig
        from trl import DPOConfig, DPOTrainer
    except ImportError as exc:
        raise RuntimeError(
            "Training dependencies are missing. Install this project with its 'training' extra."
        ) from exc

    if not torch.cuda.is_available():
        raise RuntimeError("This fail-closed QLoRA workflow requires an available CUDA GPU")

    tokenizer = AutoTokenizer.from_pretrained(
        args.base_model,
        revision=args.revision,
        use_fast=True,
        trust_remote_code=False,
    )
    if tokenizer.pad_token_id is None:
        if tokenizer.eos_token_id is None:
            raise RuntimeError("The base tokenizer has neither a padding token nor an EOS token")
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"

    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.float16,
    )
    adapter_config = LoraConfig(
        task_type="CAUSAL_LM",
        r=args.rank,
        lora_alpha=args.rank * 2,
        lora_dropout=0.05,
        bias="none",
        target_modules="all-linear",
    )
    training_config = DPOConfig(
        output_dir=str(output / "checkpoints"),
        model_init_kwargs={
            "dtype": torch.float16,
            "device_map": {"": 0},
            "low_cpu_mem_usage": True,
            "quantization_config": quantization,
            "revision": args.revision,
            "trust_remote_code": False,
        },
        num_train_epochs=args.epochs,
        per_device_train_batch_size=1,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=args.gradient_accumulation,
        learning_rate=args.learning_rate,
        warmup_ratio=0.05,
        max_grad_norm=1.0,
        logging_steps=1,
        logging_first_step=True,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        fp16=True,
        tf32=True,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        use_cache=False,
        optim="paged_adamw_8bit",
        report_to="none",
        seed=42,
        data_seed=42,
        max_length=args.max_length,
        truncation_mode="keep_start",
        precompute_ref_log_probs=True,
        beta=args.beta,
    )
    trainer = DPOTrainer(
        model=args.base_model,
        ref_model=None,
        args=training_config,
        train_dataset=Dataset.from_list(_training_rows(bundle.train)),
        eval_dataset=Dataset.from_list(_training_rows(bundle.validation)),
        processing_class=tokenizer,
        peft_config=adapter_config,
    )
    train_result = trainer.train()
    adapter = output / "adapter"
    trainer.save_model(str(adapter))
    tokenizer.save_pretrained(adapter)

    model_config = getattr(getattr(trainer, "model", None), "config", None)
    resolved_revision = getattr(model_config, "_commit_hash", None)
    if resolved_revision is None or str(resolved_revision).casefold() != args.revision.casefold():
        raise RuntimeError("The loaded model revision does not match the approved commit")
    raw_metrics = getattr(train_result, "metrics", {})
    metrics = {
        str(key): value
        for key, value in (raw_metrics.items() if isinstance(raw_metrics, dict) else [])
        if isinstance(value, (str, bool, int))
        or (isinstance(value, float) and math.isfinite(value))
    }
    return {
        "adapter_directory": "adapter",
        "resolved_base_revision": resolved_revision,
        "versions": {
            "torch": _module_version(torch),
            "transformers": _module_version(transformers),
            "peft": _module_version(peft),
            "trl": _module_version(trl),
            "datasets": _module_version(datasets),
        },
        "training_metrics": metrics,
    }


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def _prepare_output(path: Path) -> Path:
    path = Path(path).absolute()
    if path.is_symlink():
        raise ValueError("Training output must not be a symbolic link")
    if path.exists():
        raise ValueError("Training output already exists; choose a new directory for this run")
    path.mkdir(parents=True, exist_ok=False)
    return path


def _run_manifest(
    args: argparse.Namespace,
    bundle: PreferenceBundle,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "format_version": FORMAT_VERSION,
        "run_kind": "dpo_qlora_candidate",
        "status": "completed",
        "completed_at": _now_iso(),
        "base_model": args.base_model,
        "requested_base_revision": args.revision.lower(),
        "resolved_base_revision": result.get("resolved_base_revision"),
        "dataset": {
            "manifest": str(bundle.manifest_path),
            "manifest_sha256": bundle.manifest_sha256,
            "constitution": str(bundle.constitution_path),
            "constitution_sha256": bundle.constitution_sha256,
            "selection": {
                "hard_checks_passed_only": True,
                "family_grouped_splits": True,
            },
            "splits": {
                split: {
                    "file": bundle.manifest["files"][split]["file"],
                    "examples": bundle.counts[split],
                    "sha256": bundle.manifest["files"][split]["sha256"],
                }
                for split in _SPLITS
            },
        },
        "method": {
            "objective": "dpo",
            "adapter": "qlora",
            "quantization_bits": 4,
            "quantization_type": "nf4",
            "double_quantization": True,
            "epochs": args.epochs,
            "per_device_train_batch_size": 1,
            "per_device_eval_batch_size": 1,
            "gradient_accumulation_steps": args.gradient_accumulation,
            "learning_rate": args.learning_rate,
            "lora_rank": args.rank,
            "lora_alpha": args.rank * 2,
            "max_length": args.max_length,
            "beta": args.beta,
            "seed": 42,
        },
        "artifact": {"adapter_directory": result.get("adapter_directory", "adapter")},
        "training_metrics": result.get("training_metrics", {}),
        "runtime": {
            "python": sys.version,
            "platform": platform.platform(),
            "packages": result.get("versions", {}),
        },
        "automatic_ollama_deployment": False,
        "automatic_model_promotion": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit or explicitly launch fail-closed QLoRA DPO candidate training"
    )
    parser.add_argument("--dataset", type=Path, required=True, help="Verified DPO train.jsonl")
    parser.add_argument("--constitution", type=Path, default=DEFAULT_CONSTITUTION)
    parser.add_argument("--base-model", default=DEFAULT_BASE_MODEL)
    parser.add_argument(
        "--revision",
        help="Required immutable 40-character Hugging Face commit for --train",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--max-length", type=int, default=768)
    parser.add_argument("--gradient-accumulation", type=int, default=16)
    parser.add_argument("--learning-rate", type=float, default=1e-5)
    parser.add_argument("--rank", type=int, default=8)
    parser.add_argument("--beta", type=float, default=0.1)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--train",
        action="store_true",
        help="Explicitly launch GPU training after all readiness checks pass",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Audit readiness only (also the default)",
    )
    return parser


def _validate_arguments(args: argparse.Namespace) -> None:
    if not isinstance(args.base_model, str) or not args.base_model.strip():
        raise ValueError("base-model must be non-empty")
    if args.train and (
        not isinstance(args.revision, str)
        or re.fullmatch(r"[0-9a-fA-F]{40}", args.revision) is None
    ):
        raise ValueError(
            "--train requires --revision with an immutable 40-character "
            "Hugging Face commit hash"
        )
    if not math.isfinite(args.epochs) or not 0 < args.epochs <= 5:
        raise ValueError("epochs must be greater than 0 and no more than 5")
    if not 256 <= args.max_length <= 4096:
        raise ValueError("max-length must be from 256 to 4096")
    if not 1 <= args.gradient_accumulation <= 1024:
        raise ValueError("gradient-accumulation must be from 1 to 1024")
    if not math.isfinite(args.learning_rate) or not 0 < args.learning_rate <= 1e-3:
        raise ValueError("learning-rate must be greater than 0 and no more than 0.001")
    if not 1 <= args.rank <= 128:
        raise ValueError("rank must be from 1 to 128")
    if not math.isfinite(args.beta) or not 0 < args.beta <= 1:
        raise ValueError("beta must be greater than 0 and no more than 1")


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        _validate_arguments(args)
        bundle = load_preference_bundle(args.dataset, args.constitution)
    except (OSError, UnicodeError, ValueError) as exc:
        if args.train:
            raise SystemExit(f"Candidate preference training is not ready:\n- {exc}") from exc
        print("Candidate preference training is gated:")
        print(f"  - {exc}")
        return

    counts = bundle.counts
    print(
        "Validated DPO bundle: "
        f"{counts['train']} train, {counts['validation']} validation, {counts['test']} test."
    )
    if not bundle.ready:
        if args.train:
            raise SystemExit(
                "Candidate preference training is not ready:\n- " + "\n- ".join(bundle.blockers)
            )
        print("Candidate preference training is gated:")
        for blocker in bundle.blockers:
            print(f"  - {blocker}")
        return
    if not args.train:
        print("Candidate preference training gate passed. Re-run with --train to launch it.")
        return

    try:
        output = _prepare_output(args.output)
        result = _train_candidate(args, bundle, output)
        adapter = output / str(result.get("adapter_directory", "adapter"))
        if adapter.is_symlink() or not adapter.is_dir():
            raise RuntimeError("Training completed without producing an ordinary adapter directory")
        manifest = _run_manifest(args, bundle, result)
        _atomic_text(
            output / "run_manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Candidate preference training failed: {exc}") from exc
    print(f"Candidate adapter saved to {adapter}")
    print("No Ollama deployment or model promotion was performed.")


if __name__ == "__main__":
    main()
