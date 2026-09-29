"""Hub resources must be present in source and installed distributions."""

import fnmatch
from importlib.resources import files
from pathlib import Path
import tomllib
import unittest


class HubPackagingTests(unittest.TestCase):
    def test_hub_static_assets_are_readable_package_resources(self):
        assets = files("jarvis").joinpath("agent_hub_static")
        for name in ("index.html", "hub.css", "hub.js"):
            with self.subTest(asset=name):
                asset = assets.joinpath(name)
                self.assertTrue(asset.is_file())
                self.assertTrue(asset.read_text(encoding="utf-8").strip())

    def test_distribution_manifest_explicitly_includes_hub_assets(self):
        root = Path(__file__).resolve().parents[1]
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = config["tool"]["setuptools"]["package-data"]["jarvis"]
        for name in ("index.html", "hub.css", "hub.js"):
            with self.subTest(asset=name):
                self.assertTrue(any(fnmatch.fnmatchcase(
                    "agent_hub_static/" + name, pattern
                ) for pattern in patterns))

    def test_the_hub_has_its_own_command(self):
        root = Path(__file__).resolve().parents[1]
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["project"]["scripts"]["jarvis-hub"], "jarvis.agent_hub:main")

    def test_ci_exercises_the_installed_hub_command_outside_source(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        smoke = workflow.split("Push-Location $outside", 1)[1].split("Pop-Location", 1)[0]
        self.assertIn("'jarvis-hub'", smoke)
        self.assertIn('"jarvis-hub.exe") --help', smoke)
        self.assertIn('throw "jarvis-hub --help failed', smoke)


if __name__ == "__main__":
    unittest.main()
