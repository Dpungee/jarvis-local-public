from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from jarvis.code_context_graph import (
    CodeContextGraphError,
    build_python_code_graph,
    impacted_modules,
)
from jarvis.tools import ToolBox


class CodeContextGraphTests(unittest.TestCase):
    def _workspace(self) -> tempfile.TemporaryDirectory[str]:
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_text("from .service import Service\n", encoding="utf-8")
        (root / "pkg" / "service.py").write_text(
            "from pkg.util import helper\n\nclass Service:\n    def run(self):\n        return helper()\n",
            encoding="utf-8",
        )
        (root / "pkg" / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
        (root / "bad.py").write_text("def broken(:\n", encoding="utf-8")
        return directory

    def test_builds_deterministic_local_module_symbol_and_import_graph(self) -> None:
        temp = self._workspace()
        self.addCleanup(temp.cleanup)
        graph = build_python_code_graph(temp.name)
        self.assertEqual(graph["schema_version"], 1)
        self.assertEqual(graph["modules"], 4)
        node_ids = {item["id"] for item in graph["nodes"]}
        self.assertIn("module:pkg.service", node_ids)
        self.assertIn("symbol:pkg.service:Service", node_ids)
        self.assertIn("symbol:pkg.service:Service.run", node_ids)
        self.assertNotIn("def broken", repr(graph))
        self.assertEqual(graph["parse_errors"], [{"module": "bad", "error_class": "SyntaxError"}])
        self.assertEqual(graph, build_python_code_graph(temp.name))

    def test_finds_transitive_local_import_dependents(self) -> None:
        temp = self._workspace()
        self.addCleanup(temp.cleanup)
        graph = build_python_code_graph(temp.name)
        self.assertEqual(impacted_modules(graph, "pkg.util"), ["pkg", "pkg.service"])
        self.assertEqual(impacted_modules(graph, "pkg.util", max_depth=1), ["pkg.service"])
        self.assertEqual(impacted_modules(graph, "pkg.service"), ["pkg"])
        self.assertEqual(impacted_modules(graph, "does.not.exist"), [])
        with self.assertRaisesRegex(CodeContextGraphError, "module"):
            impacted_modules(graph, "../pkg.util")

    def test_rejects_invalid_roots_and_file_limit(self) -> None:
        with self.assertRaisesRegex(CodeContextGraphError, "directory"):
            build_python_code_graph("this-directory-does-not-exist")
        temp = self._workspace()
        self.addCleanup(temp.cleanup)
        with self.assertRaisesRegex(CodeContextGraphError, "file limit"):
            build_python_code_graph(temp.name, max_files=1)

    def test_toolbox_summary_and_focus_stay_source_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "core.py").write_text(
                "SECRET_TEXT = 'not in graph output'\ndef run():\n    return 1\n",
                encoding="utf-8",
            )
            (root / "client.py").write_text(
                "import core\ndef call():\n    return core.run()\n",
                encoding="utf-8",
            )
            toolbox = ToolBox.__new__(ToolBox)
            toolbox.config = type("Config", (), {"workspace": root})()

            summary = toolbox.code_context_graph()
            focused = toolbox.code_context_graph(module="core")

        self.assertEqual(summary["module_count"], 2)
        self.assertEqual(focused["impacted_modules"], ["client"])
        self.assertIn("run", {item["qualified_name"] for item in focused["symbols"]})
        self.assertNotIn("SECRET_TEXT", repr(summary) + repr(focused))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
