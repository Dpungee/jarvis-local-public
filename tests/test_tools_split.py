"""Current tool domains preserve the full method surface and enforcement core."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

from jarvis import self_diagnosis, tools
from tests.structural_ast import structural_dump

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / 'tests/fixtures/phase1_structural_equivalence.json').read_text(encoding='utf8'))
# Explicit digest records keep method names separate from credential-shaped values.
MANIFEST['tool_methods'] = {item['method']: item['sha256'] for item in MANIFEST['tool_methods']}

# Reviewed boundary-receipt correction only; preserve the original extraction manifest.
# Regression: ExecutionBackendTests.test_tool_receipts_use_actual_result_and_managed_handle_not_backend_label
# and ProcessLifecycleTests cover the live synthetic host process/status lifecycle.
REVIEWED_BOUNDARY_RECEIPTS = {
    'run_process': (
        'b9b38c9faeb006acfd0feea0960ecf7d9042fa550e195edef967e6e08bdab9b7',
        'a85d1c6e0fe6f15610cd0a3c6bc65f4e1f153058df5c49234db0c10111aa5903',
    ),
    '_managed_status': (
        'd784ceaedefe8abdbc63c1bfcb62ec7e8838d6bac4caf4d84980e4e8dd77c72e',
        '1c4f27b317c07ee6e01e28eee0adf9f8ba2bbfd5bbcb6fa9421474bb83210236',
    ),
    'start_process': (
        '768b8d28e0ee12bd66c9f8a03768217dead4746a396a647a8c53f26c6189366e',
        '6fa514a0b14f72fc5a3f470bf252bda5861f550db3f63d8a5b8cd1d4097b4ba4',
    ),
}


def fingerprint(node):
    return hashlib.sha256(structural_dump(node).encode()).hexdigest()


class OriginalToolGlobals(ast.NodeTransformer):
    """Undo only the explicit late lookup; never erase control or binding nodes."""
    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == '_tools':
            return ast.copy_location(ast.Name(node.attr, node.ctx), node)
        return self.generic_visit(node)


class ToolRegistrySplitTests(unittest.TestCase):
    def test_each_domain_imports_without_loading_toolbox(self):
        for name in MANIFEST['tool_domains']:
            with self.subTest(module=name):
                result = subprocess.run([sys.executable, '-X', 'utf8', '-c',
                    f'import sys; import jarvis.{name}; assert "jarvis.tools" not in sys.modules'],
                    cwd=ROOT, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr[-1500:])

    def test_domain_bases_and_method_ownership_are_unique(self):
        domains = MANIFEST['tool_domains']
        bases = [base.__module__.rsplit('.', 1)[-1] for base in tools.ToolBox.__bases__]
        self.assertEqual(set(bases), set(domains))
        self.assertEqual(len(bases), len(set(bases)))
        for module, names in domains.items():
            base = next(base for base in tools.ToolBox.__bases__ if base.__module__ == 'jarvis.' + module)
            for name in names:
                with self.subTest(method=name):
                    self.assertIn(name, vars(base))
                    self.assertNotIn(name, vars(tools.ToolBox))

    def test_every_current_tool_method_body_and_signature_are_preserved(self):
        actual = {}
        for file in ['tools.py', *(name + '.py' for name in MANIFEST['tool_domains'])]:
            tree = ast.parse((ROOT / 'jarvis' / file).read_text(encoding='utf8'))
            for cls in tree.body:
                if isinstance(cls, ast.ClassDef) and (cls.name == 'ToolBox' or cls.name.endswith('ToolsMixin')):
                    for method in cls.body:
                        if isinstance(method, ast.FunctionDef):
                            self.assertNotIn(method.name, actual)
                            actual[method.name] = fingerprint(OriginalToolGlobals().visit(copy.deepcopy(method)))
        expected = dict(MANIFEST['tool_methods'])
        for name, (before, after) in REVIEWED_BOUNDARY_RECEIPTS.items():
            self.assertEqual(expected[name], before)
            expected[name] = after
        self.assertEqual(actual, expected)

    def test_new_concurrency_authorization_and_receipts_remain_in_core(self):
        for name in ('execute', '_authorize_tool_call', '_dispatch_tool_call', '_finish_tool_call'):
            self.assertIn(name, vars(tools.ToolBox))
        self.assertTrue(any('concurrent' in name for name in vars(tools.ToolBox)))

    def test_legacy_patch_targets_resolve_at_call_time(self):
        sentinel = object()
        for name in MANIFEST['tool_domains']:
            module = importlib.import_module('jarvis.' + name)
            old = tools.MAX_FILE_BYTES
            try:
                tools.MAX_FILE_BYTES = sentinel
                self.assertIs(module._tools().MAX_FILE_BYTES, sentinel)
            finally:
                tools.MAX_FILE_BYTES = old

    def test_extracted_domains_remain_immutable_to_self_repair(self):
        for name in MANIFEST['tool_domains']:
            path = 'jarvis/' + name + '.py'
            self.assertIn(path, self_diagnosis._IMMUTABLE_REPAIR_FILES)
            self.assertIsNotNone(self_diagnosis._repair_path_reason(path))


if __name__ == '__main__':
    unittest.main()
