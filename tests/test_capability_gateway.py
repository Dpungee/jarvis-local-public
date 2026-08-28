from __future__ import annotations

import json
import os
import shutil
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.capability_gateway import CapabilityGateway, ConnectorError
from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.tools import ToolBox


TEMP_ROOT = Path(__file__).resolve().parent / ".tmp"
TEMP_ROOT.mkdir(exist_ok=True)


def connector_manifest(*, description: str = "Create one bounded example item.") -> dict:
    return {
        "schema_version": 1,
        "id": "example-service",
        "name": "Example Service",
        "version": "1.0.0",
        "description": "A bounded test connector.",
        "base_url": "https://api.example.com",
        "credential": {
            "kind": "bearer_env",
            "environment": "JARVIS_CONNECTOR_EXAMPLE_ACCESS",
        },
        "actions": [
            {
                "name": "create-item",
                "description": description,
                "method": "POST",
                "path": "/v1/items/{category}",
                "risk": "external_mutation",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "minLength": 1, "maxLength": 40},
                        "text": {"type": "string", "minLength": 1, "maxLength": 280},
                    },
                    "required": ["category", "text"],
                    "additionalProperties": False,
                },
            }
        ],
    }


class CapabilityGatewayTests(unittest.TestCase):
    def setUp(self):
        self.root = TEMP_ROOT / f"connector-{os.getpid()}-{self._testMethodName}"
        if self.root.exists():
            shutil.rmtree(self.root)
        self.workspace = self.root / "workspace"
        self.data = self.root / "data"
        self.workspace.mkdir(parents=True)
        self.data.mkdir()
        self.gateway = CapabilityGateway(self.workspace, self.data)
        self.manifest_path = self.workspace / "connector.json"
        self.manifest_path.write_text(
            json.dumps(connector_manifest(), indent=2), encoding="utf-8"
        )

    def tearDown(self):
        os.environ.pop("JARVIS_CONNECTOR_EXAMPLE_ACCESS", None)
        resolved = self.root.resolve()
        self.assertEqual(resolved.parent, TEMP_ROOT.resolve())
        shutil.rmtree(resolved)

    def install(self) -> dict:
        snapshot = self.gateway.install_snapshot("connector.json")
        return self.gateway.install("connector.json", expected_snapshot=snapshot)

    def test_in_memory_manifest_validation_does_not_write_or_install(self):
        self.manifest_path.unlink()

        result = self.gateway.validate_manifest_document(
            json.dumps(connector_manifest())
        )

        self.assertTrue(result["valid"])
        self.assertEqual(result["id"], "example-service")
        self.assertFalse(self.manifest_path.exists())
        self.assertEqual(self.gateway.list_connectors(), [])

    def test_validate_install_describe_and_call_without_exposing_credential(self):
        validated = self.gateway.validate_workspace_manifest("connector.json")
        self.assertTrue(validated["valid"])
        self.assertRegex(validated["manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(validated["credential_reference"], "JARVIS_CONNECTOR_EXAMPLE_ACCESS")

        installed = self.install()
        self.assertTrue(installed["installed"])
        self.assertEqual(installed["actions"], ["create-item"])
        self.assertFalse(self.gateway.list_connectors()[0]["credential"]["configured"])

        token = "sk-proj-" + "T" * 24
        os.environ["JARVIS_CONNECTOR_EXAMPLE_ACCESS"] = token
        snapshot = self.gateway.approval_snapshot(
            "example-service",
            "create-item",
            {"category": "notes", "text": "hello world"},
        )
        self.assertNotIn(token, json.dumps(snapshot))
        captured = {}

        def transport(url, data, headers, *, allow_redirects):
            captured.update({
                "url": url,
                "data": data,
                "headers": headers,
                "allow_redirects": allow_redirects,
            })
            return '{"id":"item-1","state":"created"}'

        result = self.gateway.call(
            "example-service",
            "create-item",
            {"category": "notes", "text": "hello world"},
            expected_snapshot=snapshot,
            transport=transport,
        )
        self.assertEqual(captured["url"], "https://api.example.com/v1/items/notes")
        self.assertEqual(json.loads(captured["data"]), {"text": "hello world"})
        self.assertEqual(captured["headers"]["Authorization"], f"Bearer {token}")
        self.assertFalse(captured["allow_redirects"])
        self.assertEqual(result["result"]["id"], "item-1")
        self.assertNotIn(token, json.dumps(result))
        self.assertTrue(self.gateway.list_connectors()[0]["credential"]["configured"])

    def test_manifest_change_and_argument_drift_fail_closed(self):
        install_snapshot = self.gateway.install_snapshot("connector.json")
        self.manifest_path.write_text(
            json.dumps(connector_manifest(description="Changed action.")), encoding="utf-8"
        )
        with self.assertRaises(PermissionError):
            self.gateway.install("connector.json", expected_snapshot=install_snapshot)

        self.manifest_path.write_text(json.dumps(connector_manifest()), encoding="utf-8")
        self.install()
        approved = self.gateway.approval_snapshot(
            "example-service", "create-item", {"category": "a", "text": "first"}
        )
        with self.assertRaises(PermissionError):
            self.gateway.call(
                "example-service",
                "create-item",
                {"category": "a", "text": "second"},
                expected_snapshot=approved,
                transport=lambda *_args, **_kwargs: "{}",
            )

    def test_executable_secret_and_open_schemas_are_rejected(self):
        cases = []
        secret = connector_manifest()
        secret["credential"]["value"] = "sk-proj-" + "A" * 24
        cases.append(secret)
        executable = connector_manifest()
        executable["actions"][0]["command"] = "powershell"
        cases.append(executable)
        open_schema = connector_manifest()
        open_schema["actions"][0]["parameters"]["additionalProperties"] = True
        cases.append(open_schema)
        credential_parameter = connector_manifest()
        credential_parameter["actions"][0]["parameters"]["properties"]["api_key"] = {
            "type": "string"
        }
        cases.append(credential_parameter)
        private_host = connector_manifest()
        private_host["base_url"] = "https://127.0.0.1"
        cases.append(private_host)
        broken_placeholder = connector_manifest()
        broken_placeholder["actions"][0]["path"] = "/v1/items/{category"
        cases.append(broken_placeholder)
        wrong_enum = connector_manifest()
        wrong_enum["actions"][0]["parameters"]["properties"]["category"]["enum"] = [1]
        cases.append(wrong_enum)
        for index, manifest in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(ConnectorError):
                    self.gateway.validate_manifest_document(json.dumps(manifest))

    def test_malformed_workspace_manifest_is_rejected_at_file_boundary(self):
        self.manifest_path.write_text("{not valid json", encoding="utf-8")

        with self.assertRaises(ConnectorError):
            self.gateway.validate_workspace_manifest("connector.json")

    def test_toolbox_uses_exact_one_shot_approval_for_connector_calls(self):
        self.install()
        config = replace(
            Config.load(),
            workspace=self.workspace,
            data_dir=self.data,
            execution_mode="trusted-host",
            external_access="trusted-external",
            autonomy="autonomous",
            computer_access="disabled",
        )
        memory = Memory(self.data / "gateway.db")
        try:
            toolbox = ToolBox(config, memory)
            arguments = {
                "connector": "example-service",
                "action": "create-item",
                "arguments": {"category": "notes", "text": "approved post"},
            }
            with toolbox.approval_context("conversation:42"):
                blocked = json.loads(toolbox.execute("connector_call", arguments))
            self.assertFalse(blocked["ok"])
            self.assertTrue(blocked["approval_required"])
            approval = memory.get_approval(blocked["approval_id"])
            self.assertIn("https://api.example.com/v1/items/notes", approval["resource"])
            self.assertIn("approved post", approval["resource"])
            self.assertTrue(memory.decide_approval(blocked["approval_id"], True))

            os.environ["JARVIS_CONNECTOR_EXAMPLE_ACCESS"] = "safe-test-value"
            with patch("jarvis.tools._fetch", return_value='{"state":"created"}') as fetch:
                with toolbox.approval_context("conversation:42"):
                    allowed = json.loads(toolbox.execute("connector_call", arguments))
            self.assertTrue(allowed["ok"])
            self.assertEqual(fetch.call_count, 1)

            with patch("jarvis.tools._fetch") as fetch_again:
                with toolbox.approval_context("conversation:42"):
                    blocked_again = json.loads(toolbox.execute("connector_call", arguments))
            self.assertFalse(blocked_again["ok"])
            self.assertTrue(blocked_again["approval_required"])
            fetch_again.assert_not_called()
        finally:
            memory.close()


if __name__ == "__main__":
    unittest.main()
