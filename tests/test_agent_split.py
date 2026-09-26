"""Structural proof that the current Agent stages retain their original bodies."""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jarvis.agent import Agent
from jarvis.agent_coding_verification import CodingRunState, CodingVerifier
from tests.test_tools_split import fingerprint

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / 'tests/fixtures/phase1_structural_equivalence.json').read_text(encoding='utf8'))
for section in ('agent_unchanged_methods', 'agent_closure_bodies', 'agent_stage_bodies'):
    MANIFEST[section] = {item['method']: item['sha256'] for item in MANIFEST[section]}


class OriginalAgentBindings(ast.NodeTransformer):
    """Normalize explicit state back to former locals, preserving control structure."""
    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == '_agent':
            return ast.copy_location(ast.Name(node.attr, node.ctx), node)
        if isinstance(node.value, ast.Name) and node.value.id in {'coding_state', 'verifier'}:
            return ast.copy_location(ast.Name(node.attr, node.ctx), node)
        if isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name) and node.value.value.id == 'self' and node.value.attr == 'state':
            return ast.copy_location(ast.Name(node.attr, node.ctx), node)
        if isinstance(node.value, ast.Name) and node.value.id == 'self' and node.attr == 'agent':
            return ast.copy_location(ast.Name('self', node.ctx), node)
        if isinstance(node.value, ast.Name) and node.value.id == 'self' and node.attr in MANIFEST['agent_closures']:
            return ast.copy_location(ast.Name(node.attr, node.ctx), node)
        return self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self.generic_visit(node)
        node.simple = int(isinstance(node.target, ast.Name))
        return node

class OriginalStageControls(OriginalAgentBindings):
    def visit_Return(self, node):
        if isinstance(node.value, ast.Tuple) and isinstance(node.value.elts[0], ast.Constant):
            kind = node.value.elts[0].value
            if kind == 'return':
                return ast.Return(self.visit(node.value.elts[1]))
            if kind == 'next':
                return None
            if kind == 'continue':
                return ast.Continue()
            if kind == 'break':
                return ast.Break()
        return self.generic_visit(node)


def methods(file, cls):
    tree = ast.parse((ROOT / 'jarvis' / file).read_text(encoding='utf8'))
    owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    return {n.name: n for n in owner.body if isinstance(n, ast.FunctionDef)}


class AgentStructuralSplitTests(unittest.TestCase):
    def test_unmoved_methods_including_dropped_write_and_governance_stay_identical(self):
        actual = methods('agent.py', 'Agent')
        for name, expected in MANIFEST['agent_unchanged_methods'].items():
            with self.subTest(method=name):
                self.assertEqual(fingerprint(actual[name]), expected)

    def test_all_twelve_current_closures_preserve_bodies_and_signatures(self):
        actual = methods('agent_coding_verification.py', 'CodingVerifier')
        for name, expected in MANIFEST['agent_closure_bodies'].items():
            with self.subTest(closure=name):
                node = copy.deepcopy(actual[name])
                self.assertEqual(node.args.args.pop(0).arg, 'self')
                self.assertEqual(fingerprint(OriginalAgentBindings().visit(node)), expected)

    def test_all_eleven_current_stage_bodies_are_preserved(self):
        actual = methods('agent.py', 'Agent')
        for name, expected in MANIFEST['agent_stage_bodies'].items():
            with self.subTest(stage=name):
                body = ast.Module(copy.deepcopy(actual[name].body), [])
                self.assertEqual(fingerprint(OriginalStageControls().visit(body)), expected)

    def test_orchestrator_calls_stages_once_in_original_order(self):
        run = methods('agent.py', 'Agent')['_run']
        calls = sorted((n.lineno, n.func.attr) for n in ast.walk(run) if isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Attribute) and n.func.attr in MANIFEST['agent_stages'])
        self.assertEqual([name for _line, name in calls], MANIFEST['agent_stages'])
        self.assertLess(run.end_lineno - run.lineno, 130)

    def test_shared_state_rejects_unknown_bindings(self):
        self.assertEqual(set(CodingRunState.__slots__), set(MANIFEST['state_names']))
        with self.assertRaises(AttributeError):
            CodingRunState(not_a_run_binding=True)

    def test_stage_return_stops_before_later_stages(self):
        agent = object.__new__(Agent)
        result = object()
        with patch.object(agent, '_run_governed_project_fact', return_value=('return', result)), \
                patch.object(agent, '_run_resolve_routing') as later:
            self.assertIs(agent._run('synthetic'), result)
        later.assert_not_called()

    def test_loop_continue_skips_later_stages_without_finishing_turn(self):
        agent = object.__new__(Agent)
        agent.config = SimpleNamespace(max_steps=3)
        states = []
        result = object()
        def setup(state, _verifier):
            state.requires_launch = False
            return ('next', None)
        def model(state, _verifier):
            states.append(state.step)
            return ('continue', None) if state.step == 1 else ('return', result)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in MANIFEST['agent_stages'][:7]:
                stack.enter_context(patch.object(agent, name, side_effect=setup))
            stack.enter_context(patch.object(agent, '_run_step_model_turn', side_effect=model))
            later = stack.enter_context(patch.object(agent, '_run_step_no_tool_reply'))
            self.assertIs(agent._run('synthetic'), result)
            later.assert_not_called()
        self.assertEqual(states, [1, 2])

    def test_loop_break_runs_final_verification_and_synthesis_once(self):
        agent = object.__new__(Agent)
        agent.config = SimpleNamespace(max_steps=3)
        result = object()
        def setup(state, _verifier):
            for name, value in dict(requires_launch=False, evidence=[], route=None,
                    task_context='', total_tool_calls=0, requires_web=False,
                    requires_code_change=False, learning_task=False, deep_research_task=False,
                    successful_tools=set(), verified_urls=set(), requires_process_stop=False,
                    requires_process_logs=False).items():
                setattr(state, name, value)
            return ('next', None)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in MANIFEST['agent_stages'][:7]:
                stack.enter_context(patch.object(agent, name, side_effect=setup))
            model = stack.enter_context(patch.object(agent, '_run_step_model_turn', return_value=('break', None)))
            later = stack.enter_context(patch.object(agent, '_run_step_no_tool_reply'))
            verify = stack.enter_context(patch.object(CodingVerifier, 'replay_final_verification_if_needed'))
            synthesize = stack.enter_context(patch.object(agent, '_finalize_with_synthesis', return_value=result))
            self.assertIs(agent._run('synthetic'), result)
            model.assert_called_once()
            later.assert_not_called()
            verify.assert_called_once()
            synthesize.assert_called_once()


if __name__ == '__main__':
    unittest.main()
