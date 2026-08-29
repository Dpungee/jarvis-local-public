from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "jarvis" / "presence.js"


def function_block(source: str, name: str) -> str:
    for prefix in (f"async function {name}(", f"function {name}("):
        start = source.find(prefix)
        if start >= 0:
            break
    else:
        raise AssertionError(f"Presence function {name!r} was not found")
    brace = source.find("{", start)
    depth = 0
    for index in range(brace, len(source)):
        character = source[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"Presence function {name!r} is incomplete")


class PresenceUtilityRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = SCRIPT_PATH.read_text(encoding="utf-8")

    def test_every_async_utility_renderer_is_generation_correlated(self) -> None:
        renderers = {
            "renderArtifacts": "artifacts",
            "renderSchedule": "scheduled",
            "renderNetworkInventory": "devices",
            "renderPublicPresence": "public-presence",
            "renderCompanion": "companion",
            "renderCustomize": "customize",
        }
        for function_name, view in renderers.items():
            with self.subTest(renderer=function_name):
                block = function_block(self.script, function_name)
                self.assertIn("generation = null", block)
                self.assertIn(f'beginUtilityRender("{view}", generation)', block)
                self.assertIn("if (!render) return;", block)
                self.assertIn("if (!isUtilityRenderCurrent(render)) return;", block)

    def test_navigation_invalidates_in_flight_utility_work(self) -> None:
        show_chat = function_block(self.script, "showChat")
        open_utility = function_block(self.script, "openUtility")
        self.assertIn("state.utilityGeneration += 1;", show_chat)
        self.assertIn("state.utilityGeneration = generation;", open_utility)
        for renderer in (
            "renderArtifacts",
            "renderSchedule",
            "renderNetworkInventory",
            "renderPublicPresence",
            "renderCompanion",
            "renderCustomize",
        ):
            self.assertIn(f"{renderer}(generation)", open_utility)
        self.assertIn("if (!isUtilityRenderCurrent({view, generation})) return;", open_utility)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the JS race harness")
    def test_generation_guard_rejects_old_view_same_view_and_hidden_results(self) -> None:
        helper_source = "\n".join(
            function_block(self.script, name)
            for name in ("beginUtilityRender", "isUtilityRenderCurrent")
        )
        harness = f"""
"use strict";
const state = {{activeView: "devices", utilityGeneration: 7}};
const nodes = {{
  "utility-content": {{}},
  "utility-view": {{hidden: false}},
}};
const $ = (id) => nodes[id];
{helper_source}
const devices = beginUtilityRender("devices", 7);
if (!devices || !isUtilityRenderCurrent(devices)) throw new Error("current view rejected");
state.activeView = "companion";
state.utilityGeneration = 8;
if (isUtilityRenderCurrent(devices)) throw new Error("old view remained current");
const companion = beginUtilityRender("companion", 8);
if (!companion || !isUtilityRenderCurrent(companion)) throw new Error("new view rejected");
const refresh = beginUtilityRender("companion");
if (!refresh || refresh.generation !== 9) throw new Error("refresh did not advance generation");
if (isUtilityRenderCurrent(companion)) throw new Error("older same-view result remained current");
nodes["utility-view"].hidden = true;
if (isUtilityRenderCurrent(refresh)) throw new Error("hidden utility accepted a result");
"""
        completed = subprocess.run(
            [shutil.which("node") or "node", "-e", harness],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)


if __name__ == "__main__":
    unittest.main()
