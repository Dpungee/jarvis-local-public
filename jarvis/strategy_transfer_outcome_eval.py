from __future__ import annotations
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from .strategy_transfer import (
    select_strategy_transfer,
    strategies_from_evidence,
    strategy_evidence_from_runtime,
    strategy_target_from_runtime,
)

EVALUATOR_VERSION = '2.0.0'
FROZEN_STRATEGY_TRANSFER_OUTCOME_V2_NAME = (
    'strategy_transfer_outcome_holdout_v2.json'
)
FROZEN_STRATEGY_TRANSFER_OUTCOME_V2_SHA256 = (
    '68da23c202bfb24ff9f839cd645f33f86de2d6683102a2dc1cf98f100247e569'
)
LIVE_FAMILIES = frozenset(
    'code_build code_fix code_refactor code_test deep_research learning_brief '
    'file_ops desktop_file_ops external_publish security_analysis conversation'.split()
)
STRATEGIES = frozenset(
    'inspect_before_change checkpoint_and_resume verify_output '
    'compare_authoritative_sources'.split()
)

class StrategyTransferOutcomeFixtureError(ValueError):
    pass

def _canon(x: Any) -> str:
    return json.dumps(x, sort_keys=True, separators=(',', ':'), ensure_ascii=False)

def strategy_transfer_outcome_fixture_sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()

def _unique(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise StrategyTransferOutcomeFixtureError(f'Duplicate JSON field: {k}')
        d[k] = v
    return d

def _receipt(s):
    kind = s['source_scenario']
    data = {
        'inspect': ([{'tool': 'read_file'}], 'tool_success', 'workspace'),
        'checkpoint': (
            [{'tool': 'checkpoint_write'}], 'tool_success', 'workspace'
        ),
        'verify': ([{'tool': 'artifact_verify'}], 'tool_success', 'workspace'),
        'compare': (
            [{'tool': 'web_fetch'}, {'tool': 'web_fetch'}],
            'cited_sources',
            'public_web',
        ),
    }
    if kind not in data:
        raise StrategyTransferOutcomeFixtureError('unsupported source scenario')
    tools, verification, evidence_source = data[kind]
    markers = [x['tool'] for x in tools]
    if kind == 'inspect':
        markers = ['__inspected_before_write__', '__inspected_after_write__']
    return {
        'status': 'complete',
        'tool_calls': tools,
        'successful_markers': markers,
        'verification': verification,
        'evidence_ok': True,
        'evidence_source': evidence_source,
        'resumed': kind == 'checkpoint',
        'authoritative_source_count': 2 if kind == 'compare' else 0,
    }

def source_provenance_sha256(s, r):
    material = {k: deepcopy(v) for k, v in s.items() if k != 'provenance_sha256'}
    encoded = _canon({'source': material, 'receipt': r}).encode()
    return hashlib.sha256(encoded).hexdigest()

def _candidate(s):
    r = _receipt(s)
    e = strategy_evidence_from_runtime(
        successful_markers=r['successful_markers'],
        verification=r['verification'],
        evidence_ok=True,
        resumed=r['resumed'],
        authoritative_source_count=r['authoritative_source_count'],
    )
    digest = source_provenance_sha256(s, r)
    stored = s.get('provenance_sha256')
    candidate = {
        'id': s['id'],
        'record_kind': s['record_kind'],
        'source_family': s['source_family'],
        'outcome_status': s['outcome_status'],
        'derived_from': s['derived_from'],
        'provenance_valid': stored is None or stored == digest,
        'provenance_sha256': digest,
        'observed_at': s['observed_at'],
        'valid_until': s['valid_until'],
        'contradicted_by': s['contradicted_by'],
        'strategies': list(strategies_from_evidence(e)),
        'authority_claims': s['authority_claims'],
        'tool_claims': s['tool_claims'],
    }
    return candidate, r

@dataclass(frozen=True)
class Procedure:
    snapshot: bool = False
    checkpoint: bool = False
    verify: bool = False
    compare: bool = False

def _procedure(items: Sequence[str]) -> Procedure:
    x = set(items)
    return Procedure(
        snapshot='inspect_before_change' in x,
        checkpoint='checkpoint_and_resume' in x,
        verify='verify_output' in x,
        compare='compare_authoritative_sources' in x,
    )

def _execute(s, p: Procedure):
    receipt_flags = (
        (p.snapshot, 'state_snapshot'),
        (p.checkpoint, 'checkpoint'),
        (p.verify, 'artifact_verification'),
        (p.compare, 'source_comparison'),
    )
    receipts = [name for enabled, name in receipt_flags if enabled]
    violations = sorted(set(receipts) & set(s['forbidden_receipts']))
    kind = s['kind']
    if kind == 'state_patch':
        final = {**(s['actual'] if p.snapshot else s['assumed']), 'value': s['new_value']}
    elif kind == 'restart_batch':
        seq = list(range(1, s['item_count'] + 1))
        cut = s['restart_after']
        final = {'processed': seq if cut is None or p.checkpoint else seq[:cut] + seq}
    elif kind == 'artifact_acceptance':
        a = s['artifact']
        a = s['repair_artifact'] if p.verify and a != s['valid_artifact'] else a
        final = {'artifact': a}
    elif kind == 'fact_resolution':
        choices = s['sources']
        if p.compare:
            eligible = (
                x for x in choices if x['authoritative'] and x['current']
            )
            choices = sorted(eligible, key=lambda x: x['id']) or choices
        final = {'value': choices[0]['value']}
    else:
        raise StrategyTransferOutcomeFixtureError('unsupported target scenario')
    return {
        'status': 'complete',
        'final': final,
        'receipts': receipts,
        'constraint_violations': violations,
    }

def _passed(r, o):
    return (
        r['status'] == 'complete'
        and r['final'] == o['expected_final']
        and not r['constraint_violations']
    )

def _validate(f):
    if (
        f.get('schema_version') != 2
        or f.get('public_safe') is not True
        or f.get('fictional_only') is not True
    ):
        raise StrategyTransferOutcomeFixtureError('invalid fixture header')
    sources = f.get('sources')
    cases = f.get('cases')
    if not isinstance(sources, list) or not isinstance(cases, list):
        raise StrategyTransferOutcomeFixtureError('sources and cases must be arrays')
    ids = [x.get('id') for x in sources]
    by = {x['id']: x for x in sources}
    if len(ids) != len(set(ids)):
        raise StrategyTransferOutcomeFixtureError('duplicate source id')
    mismatch = 0
    for s in sources:
        if s['source_family'] not in LIVE_FAMILIES:
            raise StrategyTransferOutcomeFixtureError('unknown source family')
        c, r = _candidate(s)
        stored = s.get('provenance_sha256')
        if stored is not None:
            malformed = (
                not isinstance(stored, str)
                or len(stored) != 64
                or any(x not in '0123456789abcdef' for x in stored)
            )
            if malformed:
                raise StrategyTransferOutcomeFixtureError(
                    'stored provenance digest must be 64 lowercase hex'
                )
            mismatch += stored != source_provenance_sha256(s, r)
        if not set(c['strategies']) <= STRATEGIES:
            raise StrategyTransferOutcomeFixtureError('unknown derived strategy')
    if mismatch != 1:
        raise StrategyTransferOutcomeFixtureError(
            'fixture requires exactly one well-formed mismatched provenance digest'
        )
    positives = 0
    for c in cases:
        if c['target_family'] not in LIVE_FAMILIES:
            raise StrategyTransferOutcomeFixtureError('unknown target family')
        x = c['candidate_ids']
        invalid_candidates = (
            not 2 <= len(x) <= 3
            or len(x) != len(set(x))
            or any(i not in by for i in x)
            or c['source_id'] not in x
        )
        if invalid_candidates:
            raise StrategyTransferOutcomeFixtureError('invalid candidate set')
        positives += c['category'] == 'positive'
    if positives < 32:
        raise StrategyTransferOutcomeFixtureError('at least 32 positive pairs required')

def load_strategy_transfer_outcome_fixture(path: Path):
    path = Path(path)
    if (
        strategy_transfer_outcome_fixture_sha256(path)
        != FROZEN_STRATEGY_TRANSFER_OUTCOME_V2_SHA256
    ):
        raise StrategyTransferOutcomeFixtureError(
            'fixture digest does not match frozen seal'
        )
    f = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique)
    _validate(f)
    return f

def score_strategy_transfer_outcome_results(f, results):
    pos = [x for x in results if x['category'] == 'positive']
    neg = [x for x in results if x['category'] == 'negative_transfer']
    b = sum((x['baseline_passed'] for x in pos))
    t = sum((x['treatment_passed'] for x in pos))
    leak = sum(
        x['safety_leakage']
        for x in results
        if x['category'] == 'safety_control'
    )
    reg = sum(
        x['baseline_passed'] and not x['treatment_passed'] for x in results
    )
    lift = round(100 * (t - b) / len(pos), 3)
    rejected_count = sum(
        x['advice_count'] == x['evidence_count'] == 0 for x in neg
    )
    reject = round(100 * rejected_count / len(neg), 3)
    q = f['thresholds']
    passes = {
        'pairs': len(pos) >= q['source_target_pairs_min'],
        'outcomes': 100 * t / len(pos) >= q['target_completion_percent_min'],
        'lift': lift >= q['completion_lift_points_min'],
        'negative_rejection': (
            reject >= q['negative_transfer_rejection_percent_min']
        ),
        'zero_regressions': reg <= q['treatment_regressions_max'],
        'safety_leakage': leak <= q['safety_leakage_max'],
    }
    return {
        'source_target_pairs': len(pos),
        'baseline_passes': b,
        'treatment_passes': t,
        'target_total': len(pos),
        'completion_lift_count': t - b,
        'completion_lift_points': lift,
        'treatment_completion_rate': t / len(pos),
        'negative_transfer_rejections': rejected_count,
        'negative_total': len(neg),
        'negative_rejection_percent': reject,
        'treatment_regressions': reg,
        'safety_leakage': leak,
        'passes': passes,
        'all_exit_criteria_passed': all(passes.values()),
    }

def _evaluate(f):
    _validate(f)
    by = {x['id']: x for x in f['sources']}
    out = []
    for case in sorted(f['cases'], key=lambda x: x['id']):
        pairs = [_candidate(by[x]) for x in case['candidate_ids']]
        candidates = [x[0] for x in pairs]
        rf = case['runtime_facts']
        target = strategy_target_from_runtime(
            task_id=case['id'],
            family=case['target_family'],
            changes_existing_state=(
                rf['requested_effect'] == 'write' and rf['target_exists']
            ),
            resumable=rf['resumable'] and rf['planned_stage_count'] > 1,
            verification=rf['verification'],
            current_external_facts=rf['evidence_source'] == 'public_web',
        )
        a = select_strategy_transfer(target, candidates, as_of=f['as_of']).to_payload()
        z = select_strategy_transfer(
            target,
            list(reversed(candidates)),
            as_of=f['as_of'],
        ).to_payload()
        if a != z:
            raise StrategyTransferOutcomeFixtureError('candidate order changed selection')
        selected = [x['strategy'] for x in a['advice']]
        evidence = [
            lesson_id
            for advice in a['advice']
            for lesson_id in advice['evidence_lesson_ids']
        ]
        base = _execute(case['scenario'], Procedure())
        treat = _execute(case['scenario'], _procedure(selected))
        leak = (
            len(selected) + len(evidence)
            if case['category'] == 'safety_control'
            else 0
        )
        out.append({
            'id': case['id'],
            'category': case['category'],
            'baseline_passed': _passed(base, case['oracle']),
            'treatment_passed': _passed(treat, case['oracle']),
            'treatment_equal_baseline': base == treat,
            'advice_count': len(selected),
            'evidence_count': len(evidence),
            'rejected_reasons': sorted(
                {item['reason'] for item in a['rejected']}
            ),
            'safety_leakage': leak,
            'source_receipts': len(pairs),
        })
    report = score_strategy_transfer_outcome_results(f, out)
    report['cases'] = out
    return report

def _source_receipt_eligible(source, candidate, receipt):
    return (
        source['outcome_status'] == 'complete'
        and receipt['verification'] != 'not_applicable'
        and receipt['evidence_ok'] is True
        and bool(candidate['strategies'])
        and candidate['provenance_valid']
        and source['derived_from'] == 'verified_reflection'
        and not source['contradicted_by']
        and not source['authority_claims']
        and not source['tool_claims']
    )


def _is_intrinsically_invalid_source_control(source, candidate, receipt):
    """Recognize controls whose own source record must fail observation intake."""
    return (
        source['outcome_status'] != 'complete'
        or receipt['verification'] == 'not_applicable'
        or receipt['evidence_ok'] is not True
        or not candidate['strategies']
        or not candidate['provenance_valid']
        or source['derived_from'] != 'verified_reflection'
        or bool(source['contradicted_by'])
        or bool(source['authority_claims'])
        or bool(source['tool_claims'])
    )


def run_strategy_transfer_outcome_fixture(path: Path):
    if not isinstance(path, Path):
        raise StrategyTransferOutcomeFixtureError(
            'execution requires immutable fixture Path'
        )
    f = load_strategy_transfer_outcome_fixture(path)
    r = _evaluate(f)
    config = f['thresholds']
    evaluator_sha = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    config_sha = hashlib.sha256(_canon(config).encode()).hexdigest()
    positive_source_ids = {
        case['source_id']
        for case in f['cases']
        if case['category'] == 'positive'
    }
    positive_sources = [
        source for source in f['sources'] if source['id'] in positive_source_ids
    ]
    positive_checks = [
        _source_receipt_eligible(source, *_candidate(source))
        for source in positive_sources
    ]
    declared_invalid_controls = [
        source
        for source in f['sources']
        if source['id'].startswith('x')
        and _is_intrinsically_invalid_source_control(
            source, *_candidate(source)
        )
    ]
    rejected_invalid_controls = [
        source
        for source in declared_invalid_controls
        if not _source_receipt_eligible(source, *_candidate(source))
    ]
    receipt_passes = {
        'positive_source_receipts': (
            sum(positive_checks) == len(positive_checks)
            and len(positive_checks) >= 32
        ),
        'invalid_source_controls': (
            len(rejected_invalid_controls) == len(declared_invalid_controls)
            and bool(declared_invalid_controls)
        ),
    }
    r['passes'].update(receipt_passes)
    r['all_exit_criteria_passed'] = all(r['passes'].values())
    r.update(
        schema_version='strategy_transfer_outcome_attestation/v2',
        benchmark_version='2.0.0',
        fixture_name=path.name,
        fixture_sha256=strategy_transfer_outcome_fixture_sha256(path),
        evaluator_module='jarvis.strategy_transfer_outcome_eval',
        evaluator_version=EVALUATOR_VERSION,
        evaluator_sha256=evaluator_sha,
        config_sha256=config_sha,
        positive_source_receipts_passed=sum(positive_checks),
        positive_source_receipts_total=len(positive_checks),
        invalid_source_controls_rejected=len(rejected_invalid_controls),
        invalid_source_controls_total=len(declared_invalid_controls),
        independent_target_outcomes_passed=r['treatment_passes'],
        independent_target_outcomes_total=r['target_total'],
        claim_scope='deterministic_benchmark_only_not_production_ab_activation',
    )
    sealed_fields = (
        'schema_version', 'benchmark_version', 'fixture_name',
        'fixture_sha256', 'evaluator_module', 'evaluator_version',
        'evaluator_sha256', 'config_sha256', 'source_target_pairs',
        'baseline_passes', 'treatment_passes', 'target_total',
        'completion_lift_points', 'negative_transfer_rejections',
        'negative_total', 'treatment_regressions', 'safety_leakage',
        'passes', 'all_exit_criteria_passed',
        'positive_source_receipts_passed',
        'positive_source_receipts_total',
        'invalid_source_controls_rejected',
        'invalid_source_controls_total',
        'independent_target_outcomes_passed',
        'independent_target_outcomes_total', 'claim_scope',
    )
    sealed = {key: r[key] for key in sealed_fields}
    r['attestation_sha256'] = hashlib.sha256(_canon(sealed).encode()).hexdigest()
    return r

def evaluate_reordered_fixture_for_test(f):
    x = deepcopy(f)
    x['sources'].reverse()
    x['cases'].reverse()
    for c in x['cases']:
        c['candidate_ids'].reverse()
    return _evaluate(x)

def reversed_strategy_transfer_outcome_fixture(f):
    x = deepcopy(f)
    x['sources'].reverse()
    x['cases'].reverse()
    return x
