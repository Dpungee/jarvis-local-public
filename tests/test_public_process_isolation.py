"""Public services must not import private agent or memory implementations."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_MODULES = (
    "jarvis.public_presence_service", "jarvis.public_presence_store",
    "jarvis.public_bridge", "jarvis.public_tools", "jarvis.moltbook_adapter",
)
PRIVATE_MODULES = tuple(sorted({
    "jarvis.agent", "jarvis.agent_coding_verification", "jarvis.tools",
    "jarvis.presence", "jarvis.cli", "jarvis.model_client",
    "jarvis.governed_memory", "jarvis.learning_ladder", "jarvis.long_horizon",
    "jarvis.screen_companion", "jarvis.desktop", "jarvis.execution",
    *("jarvis." + path.stem for pattern in ("memory*.py", "tools_*.py")
      for path in (ROOT / "jarvis").glob(pattern)),
}))


def _run_isolated(code: str, data: Path) -> dict:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("JARVIS_")}
    environment.update(PYTHONPATH=str(ROOT), JARVIS_DATA=str(data),
                       JARVIS_PUBLIC_PRESENCE_ENABLED="false")
    report = (
        "\nimport json, sys\n"
        "print('ISOLATION:' + json.dumps({'code': code, "
        "'modules': sorted(n for n in sys.modules if n.startswith('jarvis'))}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code + report],
        cwd=ROOT, env=environment, capture_output=True, text=True,
        timeout=120, check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr[-2000:])
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("ISOLATION:"):
            return json.loads(line.removeprefix("ISOLATION:"))
    raise AssertionError("Public process isolation report missing")


class PublicProcessIsolationTests(unittest.TestCase):
    def test_public_imports_do_not_load_private_core(self):
        with tempfile.TemporaryDirectory() as directory:
            result = _run_isolated(
                "".join("import " + module + "\n" for module in PUBLIC_MODULES)
                + "code = 0\n", Path(directory))
        self.assertTrue(set(PUBLIC_MODULES) <= set(result["modules"]))
        self.assertFalse(set(PRIVATE_MODULES) & set(result["modules"]))

    def test_public_health_leaves_private_database_and_imports_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            sentinel = data / "jarvis.db"
            sentinel.write_bytes(b"private-memory-sentinel")
            result = _run_isolated(
                "from jarvis import public_presence_service\n"
                "code = public_presence_service.main(['health'])\n", data)
            self.assertEqual(sentinel.read_bytes(), b"private-memory-sentinel")
        self.assertEqual(result["code"], 0)
        self.assertFalse(set(PRIVATE_MODULES) & set(result["modules"]))

    def test_public_source_has_no_static_private_imports(self):
        for module in PUBLIC_MODULES:
            tree = ast.parse((ROOT / "jarvis" / (module.split(".")[1] + ".py"))
                             .read_text(encoding="utf-8"))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    base = ("jarvis." if node.level == 1 else "") + (node.module or "")
                    imports.add(base)
                    imports.update(base.rstrip(".") + "." + alias.name for alias in node.names)
            self.assertFalse(set(PRIVATE_MODULES) & imports, module)


if __name__ == "__main__":
    unittest.main()
