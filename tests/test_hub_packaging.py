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


if __name__ == "__main__":
    unittest.main()
