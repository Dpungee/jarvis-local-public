from __future__ import annotations

import unittest

from jarvis.distillation import static_python_audit


class DistillationAuditTests(unittest.TestCase):
    def test_allows_pure_python_and_safe_standard_library(self):
        files = {
            "solution.py": (
                "import re\n"
                "def normalize(value):\n"
                "    return re.sub(r' +', ' ', value.replace('\\\\', '/'))\n"
            )
        }
        self.assertEqual(static_python_audit(files), [])

    def test_rejects_host_process_file_network_and_dynamic_code_access(self):
        examples = (
            "import subprocess\nsubprocess.run(['whoami'])\n",
            "import os\nos.system('whoami')\n",
            "open('secret.txt').read()\n",
            "eval('1 + 1')\n",
            "().__class__.__mro__\n",
        )
        for source in examples:
            with self.subTest(source=source):
                self.assertTrue(static_python_audit({"solution.py": source}))

    def test_rejects_syntax_errors(self):
        violations = static_python_audit({"solution.py": "def broken(:\n pass\n"})
        self.assertIn("syntax error", violations[0])


if __name__ == "__main__":
    unittest.main()
