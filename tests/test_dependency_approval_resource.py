from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jarvis.approvals import approval_display_resource, approval_resource
from jarvis.memory import Memory
from jarvis.presence import _safe_approval


class DependencyApprovalResourceTests(unittest.TestCase):
    @staticmethod
    def _maximum_arguments() -> dict[str, object]:
        arguments: dict[str, object] = {
            "cwd": "projects/" + ("deep-directory/" * 25) + "application",
            "timeout": 3_600,
            "resolved_cwd": "C:/workspace/" + ("deep-directory/" * 25) + "application",
            "dependency_manifest_count": 5,
            "dependency_tree_sha256": "a" * 64,
            "dependency_network_access": True,
            "dependency_host_authority": True,
            "node_lifecycle_scripts": "disabled",
            "dependency_declaration_count": 120,
            "dependency_summary_omitted_count": 112,
            "dependency_node_path": (
                "C:/Program Files/Node/" + ("runtime/" * 30) + "node.exe"
            ),
            "dependency_node_bytes": 123_456,
            "dependency_node_sha256": "b" * 64,
            "dependency_npm_cli_path": (
                "C:/Program Files/Node/" + ("runtime/" * 30) + "npm-cli.js"
            ),
            "dependency_npm_cli_bytes": 654_321,
            "dependency_npm_cli_sha256": "c" * 64,
        }
        manifests = (
            "requirements.lock",
            "requirements.txt",
            "npm-shrinkwrap.json",
            "package-lock.json",
            "package.json",
        )
        for index, name in enumerate(manifests, start=1):
            arguments[f"dependency_manifest_{index:02d}"] = (
                f"{name} | 999999 bytes | sha256:" + "d" * 64
            )
        for index in range(1, 9):
            arguments[f"dependency_{index:02d}"] = (
                "node/optionalDependencies: "
                f"@example/package-with-a-long-name-{index:02d}"
                "@npm:@vendor/package-with-a-long-version@1.2.3-beta.4"
            )
        return arguments

    def test_maximum_display_is_bounded_parseable_and_target_visible(self) -> None:
        arguments = self._maximum_arguments()
        exact = approval_resource("install_project_dependencies", arguments)
        display = approval_display_resource(
            "install_project_dependencies", arguments, exact
        )

        self.assertGreater(len(exact), 2_000)
        self.assertLessEqual(len(display), 1_900)
        parsed = json.loads(display)
        exact_parsed = json.loads(exact)
        self.assertEqual(
            parsed["arguments_sha256"], exact_parsed["arguments_sha256"]
        )
        visible = parsed["arguments"]
        self.assertIn("projects/", visible["cwd"]["prefix"])
        self.assertIn("application", visible["resolved_cwd"]["suffix"])
        self.assertEqual(visible["manifest_names"], [
            "requirements.lock",
            "requirements.txt",
            "npm-shrinkwrap.json",
            "package-lock.json",
            "package.json",
        ])
        self.assertEqual(visible["manifest_tree_sha256"], "a" * 64)
        self.assertEqual(
            [(item["identity"], item["sha256"]) for item in visible["executors"]],
            [("node", "b" * 64), ("npm-cli", "c" * 64)],
        )
        self.assertGreaterEqual(len(visible["direct_dependencies"]), 1)
        self.assertEqual(
            visible["omitted_dependency_count"],
            visible["direct_dependency_count"]
            - len(visible["direct_dependencies"]),
        )

    def test_persistence_and_presence_keep_valid_json_and_exact_fingerprint(self) -> None:
        arguments = self._maximum_arguments()
        exact = approval_resource("install_project_dependencies", arguments)
        display = approval_display_resource(
            "install_project_dependencies", arguments, exact
        )
        scope = "conversation:42"
        with tempfile.TemporaryDirectory() as temporary:
            with Memory(Path(temporary) / "jarvis.db") as memory:
                allowed, approval_id = memory.authorize_or_request(
                    "install_dependencies",
                    exact,
                    "Install the exact reviewed dependency snapshot.",
                    approval_scope=scope,
                    display_resource=display,
                )
                self.assertFalse(allowed)
                stored = memory.get_approval(approval_id)
                fingerprint = memory.db.execute(
                    "SELECT fingerprint FROM approvals WHERE id=?", (approval_id,)
                ).fetchone()["fingerprint"]

        self.assertEqual(
            fingerprint,
            Memory.approval_fingerprint("install_dependencies", exact, scope),
        )
        self.assertEqual(json.loads(stored["resource"]), json.loads(display))
        rendered = _safe_approval(stored)
        self.assertLessEqual(len(rendered["resource"]), 2_000)
        ui_payload = json.loads(rendered["resource"])
        self.assertEqual(
            ui_payload["arguments"]["manifest_names"][-1], "package.json"
        )
        self.assertEqual(
            ui_payload["arguments"]["executors"][0]["sha256"], "b" * 64
        )

    def test_custom_display_must_be_bound_to_exact_resource(self) -> None:
        arguments = self._maximum_arguments()
        exact = approval_resource("install_project_dependencies", arguments)
        display = json.loads(approval_display_resource(
            "install_project_dependencies", arguments, exact
        ))
        display["arguments_sha256"] = "f" * 64
        with tempfile.TemporaryDirectory() as temporary:
            with Memory(Path(temporary) / "jarvis.db") as memory:
                with self.assertRaisesRegex(ValueError, "not bound"):
                    memory.authorize_or_request(
                        "install_dependencies",
                        exact,
                        "Install dependencies.",
                        approval_scope="foreground",
                        display_resource=json.dumps(display, separators=(",", ":")),
                    )

        changed = dict(arguments)
        changed["cwd"] = "a-different-project"
        with self.assertRaisesRegex(ValueError, "do not match authorization"):
            approval_display_resource(
                "install_project_dependencies", changed, exact
            )


if __name__ == "__main__":
    unittest.main()
