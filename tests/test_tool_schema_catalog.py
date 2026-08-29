import inspect
import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.tools import Tool, ToolBox

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ToolSchemaValidationTests(unittest.TestCase):
    def setUp(self):
        self.schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["safe", "fast"],
                },
                "name": {
                    "type": "string",
                    "pattern": "^[a-z]+$",
                    "minLength": 2,
                    "maxLength": 8,
                },
                "code": {
                    "type": "string",
                    "pattern": "^[0-9]{4}$",
                },
                "score": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "entries": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "weight": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 5,
                            },
                        },
                        "required": ["id"],
                    },
                },
            },
            "required": ["mode", "score", "entries"],
            "anyOf": [
                {"required": ["name"]},
                {"required": ["code"]},
            ],
        }

    def execute(self, arguments):
        function = Mock(return_value={"invoked": True})
        toolbox = ToolBox.__new__(ToolBox)
        toolbox.tools = {
            "schema_probe": Tool(
                "schema_probe", "Schema validation probe", self.schema, function
            )
        }
        toolbox.memory = object()
        result = json.loads(toolbox.execute("schema_probe", arguments))
        return result, function

    @staticmethod
    def valid_arguments():
        return {
            "mode": "safe",
            "name": "alpha",
            "score": 0.5,
            "entries": [{"id": "one", "weight": 3}],
        }

    def test_complete_schema_subset_accepts_a_valid_call(self):
        result, function = self.execute(self.valid_arguments())

        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], {"invoked": True})
        function.assert_called_once_with(**self.valid_arguments())

    def test_every_declared_constraint_blocks_before_tool_invocation(self):
        invalid_cases = {
            "enum": {"mode": "unsafe"},
            "pattern": {"name": "Alpha"},
            "minLength": {"name": "a"},
            "maxLength": {"name": "abcdefghij"},
            "minimum": {"score": -0.01},
            "maximum": {"score": 1.01},
            "number rejects boolean": {"score": False},
            "number rejects NaN": {"score": math.nan},
            "number rejects infinity": {"score": math.inf},
            "minItems": {"entries": []},
            "maxItems": {
                "entries": [{"id": "one"}, {"id": "two"}, {"id": "three"}]
            },
            "nested required": {"entries": [{}]},
            "nested property type": {"entries": [{"id": 7}]},
            "nested additionalProperties": {
                "entries": [{"id": "one", "unexpected": True}]
            },
            "top-level additionalProperties": {"unexpected": True},
        }
        for constraint, changes in invalid_cases.items():
            with self.subTest(constraint=constraint):
                arguments = self.valid_arguments()
                arguments.update(changes)
                result, function = self.execute(arguments)
                self.assertFalse(result["ok"], result)
                function.assert_not_called()

    def test_any_of_requires_one_complete_alternative_before_invocation(self):
        arguments = self.valid_arguments()
        arguments.pop("name")

        rejected, rejected_function = self.execute(arguments)
        self.assertFalse(rejected["ok"])
        self.assertIn("at least one allowed schema", rejected["error"])
        rejected_function.assert_not_called()

        arguments["code"] = "2048"
        accepted, accepted_function = self.execute(arguments)
        self.assertTrue(accepted["ok"])
        accepted_function.assert_called_once()


class ToolCatalogContractTests(unittest.TestCase):
    def test_every_registered_tool_schema_matches_its_runtime_signature(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            data_dir = root / "data"
            computer_root = root / "computer"
            workspace.mkdir()
            data_dir.mkdir()
            computer_root.mkdir()
            config = replace(
                Config.load(),
                workspace=workspace,
                data_dir=data_dir,
                # Exercise the complete registered catalog independently of
                # the caller's fail-closed environment. Constructing schemas
                # performs no external, host, desktop, or network action.
                autonomy="autonomous",
                execution_mode="trusted-host",
                computer_access="trusted-desktop",
                computer_root=computer_root,
                network_access="private-lan",
                bluetooth_access="paired-readonly",
                home_assistant_access="paired",
                home_assistant_url="http://127.0.0.1:8123",
                home_assistant_token="x" * 40,
                external_access="trusted-external",
                self_inspect="read-only",
                self_repair="propose",
            )
            with Memory(data_dir / "catalog.db") as memory:
                toolbox = ToolBox(config, memory)

                self.assertGreaterEqual(len(toolbox.tools), 100)
                self.assertEqual(
                    set(toolbox.tools),
                    {
                        schema["function"]["name"]
                        for schema in toolbox.schemas
                    },
                )
                for name, tool in toolbox.tools.items():
                    with self.subTest(tool=name):
                        json.dumps(tool.schema())
                        properties = set(tool.parameters.get("properties", {}))
                        required = set(tool.parameters.get("required", []))
                        signature = inspect.signature(tool.function)
                        parameters = {
                            key: value
                            for key, value in signature.parameters.items()
                            if value.kind
                            not in {
                                inspect.Parameter.VAR_POSITIONAL,
                                inspect.Parameter.VAR_KEYWORD,
                            }
                        }
                        runtime_required = {
                            key
                            for key, value in parameters.items()
                            if value.default is inspect.Parameter.empty
                        }
                        self.assertEqual(properties, set(parameters))
                        self.assertEqual(required - properties, set())
                        self.assertEqual(runtime_required, required)

                organize_items = toolbox.tools[
                    "google_drive_organize_files"
                ].parameters["properties"]["operations"]["items"]
                self.assertIs(organize_items.get("additionalProperties"), False)

    def test_screen_companion_control_is_cataloged_as_mixed_read_control(self):
        toolbox = ToolBox.__new__(ToolBox)
        toolbox.tools = {
            "screen_companion_control": Tool(
                "screen_companion_control",
                "Read or change the bounded Screen Companion state.",
                {"type": "object", "properties": {}},
                lambda: None,
            )
        }

        catalog = toolbox.tool_catalog("screen companion", limit=10)

        self.assertEqual(catalog["returned_count"], 1)
        self.assertEqual(catalog["matches"][0]["risk"], "mixed-read-control")
        self.assertFalse(catalog["matches"][0]["approval_required"])

    def test_readme_skill_inventory_matches_the_bundled_library(self):
        bundled = sorted(
            path.parent.name
            for path in (REPOSITORY_ROOT / "jarvis" / "builtin_skills").glob(
                "*/SKILL.md"
            )
        )
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertEqual(len(bundled), 13)
        self.assertIn("document-generation", bundled)
        self.assertIn("includes 13 progressively disclosed", readme)
        self.assertIn("document generation", readme)


if __name__ == "__main__":
    unittest.main()
