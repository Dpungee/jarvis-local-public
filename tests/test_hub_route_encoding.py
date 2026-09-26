"""Browser-controlled route values cannot become markup in Hub navigation."""
from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.test_presence_js_behavior import function_block


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for Hub route behavior checks")
class HubRouteEncodingTests(unittest.TestCase):
    def evaluate(self, section: str, agent: str, *, use_default: bool = False) -> str:
        source = (ROOT / "jarvis/agent_hub_static/hub.js").read_text(encoding="utf-8")
        code = "const selectedId = " + json.dumps(agent) + ";\n" + function_block(source, "spaceHref")
        arguments = json.dumps(section) + ("" if use_default else "," + json.dumps(agent))
        code += "\nconsole.log(JSON.stringify(spaceHref(" + arguments + ")));"
        result = subprocess.run([NODE, "-e", code], capture_output=True, text=True,
                                timeout=10, check=True)
        return json.loads(result.stdout)

    def test_normal_links_and_empty_agent_are_unchanged(self):
        self.assertEqual(self.evaluate("chat", "agt_example"), "#/agents/agt_example")
        self.assertEqual(self.evaluate("goals", "agt_example"), "#/agents/agt_example/goals")
        self.assertEqual(self.evaluate("goals", ""), "#/overview")

    def test_browser_storage_payload_stays_one_encoded_path_segment(self):
        payload = '\"><img src=x onerror="globalThis.injected=true">/../?x=1&y=2#next'
        result = self.evaluate("goals", payload)
        self.assertTrue(result.startswith("#/agents/%22%3E%3Cimg"))
        self.assertTrue(result.endswith("/goals"))
        self.assertEqual(result.count("/"), 3)
        self.assertNotRegex(result, r'[<>"&]')

    def test_route_section_cannot_escape_the_double_quoted_href(self):
        result = self.evaluate('tasks\" autofocus onfocus=\"alert(1)', "agt_example")
        self.assertIn("%22%20autofocus", result)
        self.assertNotRegex(result, r'[<>"\s]')

    def test_default_storage_value_cannot_add_html_elements_or_attributes(self):
        class Links(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = []

            def handle_starttag(self, tag, attrs):
                self.tags.append((tag, attrs))

        for payload in ('\"><svg onload=alert(1)>', '&quot; autofocus onfocus=alert(1)',
                        '%22%3E%3Cscript%3E', '../other?x=1#route', "a'b", 'javascript:alert(1)', 'agent-λ'):
            with self.subTest(payload=payload):
                href = self.evaluate("goals", payload, use_default=True)
                parser = Links()
                parser.feed('<a href="' + href + '">Open</a>')
                self.assertEqual(parser.tags, [('a', [('href', href)])])
                self.assertTrue(href.startswith("#/agents/"))


if __name__ == "__main__":
    unittest.main()
