#!/usr/bin/env python3
"""Phase 2 calibration harness: report a gate verdict, or measure one in a sandbox.

Sandbox rows are not the operator's rows. ``Memory.competence`` counts only the
``interactive``, ``worker`` and ``proactive`` outcomes of the database it reads,
so everything this harness produces under ``--root`` describes that isolated
``JARVIS_DATA`` and never the operator's initiative gate. Closing the operator's
gate is ``docs/CALIBRATION_RUNBOOK.md`` plus days of his own use (plan G-2/G-4).

Subcommands
-----------
``report``    Print (and optionally record) the read-only calibration report for
              one database. The database is copied through SQLite's read-only URI
              mode first, so the file is never opened for writing.
``cases``     Print the task plan without calling any model.
``run-case``  Internal: run exactly one case in this process. The sandbox driver
              spawns one of these per case, which is what makes every case a
              fresh process and a fresh conversation.
``sandbox``   Run the whole exercise, then report on the sandbox database.

Every case is verified by this harness against ground truth it computed or seeded
itself: a file on disk, or an answer key. No case is ever marked verified because
the model said it succeeded. The recorded outcome that ``competence()`` scores is
resolved by the runtime, not by this script; nothing here writes a prediction, and
nothing here writes a prediction with ``origin="practice"``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis import calibration_report as calibration  # noqa: E402

RESULT_MARKER = "WP10_RESULT "
DEFAULT_MODEL = "qwen3.5:9b"
DEFAULT_PER_FAMILY = 20
DEFAULT_CASE_TIMEOUT = 300

#: Exit status for a refusal the caller can fix - an unknown case id, a run file
#: with nothing to retry, a merge that would stack two recorded attempts. It is
#: distinct from 1 so a script can tell a refusal from a crash.
REFUSED_EXIT = 2

#: A model reference beginning with one of these names is not the local provider.
#: Ollama names legitimately carry a ``name:tag`` colon, so only these prefixes are
#: rejected. The CLI providers cannot satisfy model attestation by design, and the
#: cloud providers are neither free nor enabled here.
NON_LOCAL_MODEL_PREFIXES = frozenset({
    "openai", "anthropic", "codex-cli", "claude-cli", "azure", "google", "gemini",
})

#: Families this harness can exercise with every optional capability disabled.
#: ``code_*`` needs ``JARVIS_EXECUTION_MODE=trusted-host`` (its evidence marker is
#: only set by a real ``run_process`` verification) and ``deep_research`` /
#: ``learning_brief`` need ``JARVIS_EXTERNAL_ACCESS=trusted-external`` (their
#: evidence is collected source URLs). The runbook covers those for the operator's
#: own runtime; this harness will not grant itself host execution or network egress.
OFFLINE_FAMILIES = ("file_ops", "conversation", "security_analysis")

#: Families whose predicted verification is ``not_applicable``. The runtime has no
#: effect to check, so it records a finished turn as ``complete`` whether or not the
#: answer was right: their calibration is about completing the turn, not about being
#: correct. Anyone promoting them to Tier 1 authority has to know that.
EVIDENCE_FREE_FAMILIES = ("conversation", "security_analysis")

SANDBOX_LIMITATIONS = (
    "Sandbox rows are not the operator's rows and do not move his initiative "
    "gate; see docs/CALIBRATION_RUNBOOK.md (plan G-2/G-4).",
    "One local model on one host; the numbers are a historical observation, not a "
    "product guarantee.",
    "For "
    + " and ".join(EVIDENCE_FREE_FAMILIES)
    + " the runtime's verification is not_applicable, so a finished turn is "
    "recorded complete whether or not the answer was correct. This harness's "
    "independent verification is reported separately for exactly that reason.",
)

#: The agreement figure is measured with the harness's own verifier. That verifier
#: was tightened after the recorded run (a denial next to the expected token no
#: longer counts), and the replies themselves are deliberately not stored, so the
#: recorded figure cannot be recomputed. For the two evidence-free families it is
#: therefore an upper bound, not an exact count.
#: The recorded run predates the boundary-comparison fix in Memory.calibration_gate.
#: The outcome rows are identical either way - only the verdict for a family whose
#: error sits exactly on the tolerance moved - and this says so out loud.
GATE_FIX_LIMITATION = (
    "The outcome rows in this run were produced before the boundary-comparison fix "
    "in Memory.calibration_gate (calibration error is now rounded to nine decimals "
    "before the strict comparison). No row changed; the only difference is that a "
    "family whose error is exactly the 0.15 tolerance is no longer refused by "
    "floating-point representation alone."
)

UPPER_BOUND_LIMITATION = (
    "The agreement-with-ground-truth figure is an upper bound for "
    + " and ".join(EVIDENCE_FREE_FAMILIES)
    + ": those families are graded from the reply text, the answer verifier was "
    "tightened after this run to reject a denial standing next to the expected "
    "token, and replies are not stored, so an earlier reply that stated the right "
    "number only to disown it may have been counted as agreement."
)

CAPABILITY_REQUIREMENTS = {
    "file_ops": "workspace file tools only",
    "conversation": "no tools required",
    "security_analysis": "no tools required",
    "code_build": "JARVIS_EXECUTION_MODE=trusted-host",
    "code_fix": "JARVIS_EXECUTION_MODE=trusted-host",
    "code_refactor": "JARVIS_EXECUTION_MODE=trusted-host",
    "code_test": "JARVIS_EXECUTION_MODE=trusted-host",
    "deep_research": "JARVIS_EXTERNAL_ACCESS=trusted-external",
    "learning_brief": "JARVIS_EXTERNAL_ACCESS=trusted-external",
    "desktop_file_ops": "JARVIS_COMPUTER_ACCESS=trusted-desktop",
    "external_publish": "a configured external connector",
}


# --------------------------------------------------------------------------- #
# Case model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Case:
    """One verifiable task. ``check`` names a deterministic verifier."""

    case_id: str
    intended_family: str
    prompt: str
    check: str
    check_args: dict[str, Any] = field(default_factory=dict)
    seed_files: tuple[tuple[str, str], ...] = ()


def _seed(path: str, *lines: str) -> tuple[tuple[str, str], ...]:
    return ((path, "\n".join(lines) + "\n"),)


def file_ops_cases() -> list[Case]:
    """Twenty workspace file tasks, ten shapes with two parameters each."""
    tokens = ("alpha-7731", "bravo-4520")
    second = ("delta-9014", "echo-3387")
    cases: list[Case] = []
    for index, (token, other) in enumerate(zip(tokens, second), start=1):
        suffix = f"{index:02d}"
        cases.extend([
            Case(
                f"file_ops_create_line_{suffix}",
                "file_ops",
                f"Create a file named calib_line_{suffix}.txt in the workspace. "
                f"Its only line must be exactly: {token}",
                "file_has_line",
                {"path": f"calib_line_{suffix}.txt", "line": token},
            ),
            Case(
                f"file_ops_three_lines_{suffix}",
                "file_ops",
                f"Create a file named calib_list_{suffix}.txt in the workspace with "
                f"exactly three lines, in this order: {token}, then {other}, then done.",
                "file_has_lines",
                {"path": f"calib_list_{suffix}.txt", "lines": [token, other, "done"]},
            ),
            Case(
                f"file_ops_append_{suffix}",
                "file_ops",
                f"Append the line {other} to the end of the existing file "
                f"calib_append_{suffix}.txt in the workspace, keeping the line "
                f"that is already there.",
                "file_has_lines",
                {"path": f"calib_append_{suffix}.txt", "lines": [token, other]},
                _seed(f"calib_append_{suffix}.txt", token),
            ),
            Case(
                f"file_ops_copy_{suffix}",
                "file_ops",
                f"Copy the workspace file calib_source_{suffix}.txt to a new file "
                f"named calib_copy_{suffix}.txt, keeping the contents identical.",
                "file_has_line",
                {"path": f"calib_copy_{suffix}.txt", "line": token},
                _seed(f"calib_source_{suffix}.txt", token),
            ),
            Case(
                f"file_ops_delete_{suffix}",
                "file_ops",
                f"Please remove calib_stale_{suffix}.txt from the workspace folder.",
                "file_absent",
                {"path": f"calib_stale_{suffix}.txt"},
                _seed(f"calib_stale_{suffix}.txt", token),
            ),
            Case(
                f"file_ops_rename_{suffix}",
                "file_ops",
                f"Move the workspace file calib_old_{suffix}.txt so that it is named "
                f"calib_new_{suffix}.txt instead. Only the new name must remain.",
                "renamed",
                {
                    "path": f"calib_new_{suffix}.txt",
                    "line": token,
                    "absent": f"calib_old_{suffix}.txt",
                },
                _seed(f"calib_old_{suffix}.txt", token),
            ),
            Case(
                f"file_ops_folder_{suffix}",
                "file_ops",
                f"Create a folder named calib_dir_{suffix} in the workspace and, "
                f"inside it, a file named note.txt whose only line is exactly: {token}",
                "file_has_line",
                {"path": f"calib_dir_{suffix}/note.txt", "line": token},
            ),
            Case(
                f"file_ops_json_{suffix}",
                "file_ops",
                f"The workspace needs a small settings file named "
                f"calib_data_{suffix}.json. Its only line must be exactly: "
                f'{{"marker": "{token}"}}',
                "json_has_key",
                {"path": f"calib_data_{suffix}.json", "key": "marker", "value": token},
            ),
            Case(
                f"file_ops_first_line_{suffix}",
                "file_ops",
                f"Read the workspace file calib_multi_{suffix}.txt and write only its "
                f"first line into a new file named calib_first_{suffix}.txt.",
                "file_has_lines",
                {
                    "path": f"calib_first_{suffix}.txt",
                    "lines": [token],
                    "exact": True,
                },
                _seed(f"calib_multi_{suffix}.txt", token, other, "third"),
            ),
            Case(
                f"file_ops_replace_{suffix}",
                "file_ops",
                f"Update the workspace notes file calib_edit_{suffix}.txt so its only "
                f"line reads {other}.",
                "file_has_lines",
                {
                    "path": f"calib_edit_{suffix}.txt",
                    "lines": [other],
                    "exact": True,
                },
                _seed(f"calib_edit_{suffix}.txt", token),
            ),
        ])
    return cases


def conversation_cases() -> list[Case]:
    """Twenty dialogue tasks whose correct answer this harness computes itself."""
    plan: list[tuple[str, str, str]] = [
        ("mul_a", "What is 17 multiplied by 23? Reply with the number.", str(17 * 23)),
        ("mul_b", "What is 24 multiplied by 31? Reply with the number.", str(24 * 31)),
        ("add_a", "What is 4096 plus 3079? Reply with the number.", str(4096 + 3079)),
        ("sub_a", "What is 9001 minus 2437? Reply with the number.", str(9001 - 2437)),
        ("div_a", "What is 1024 divided by 16? Reply with the number.", str(1024 // 16)),
        ("pow_a", "What is 2 raised to the power of 10? Reply with the number.", str(2 ** 10)),
        ("pct_a", "What is 15 percent of 240? Reply with the number.", str(int(0.15 * 240))),
        ("len_a", "How many letters are in the word calibration? Reply with the number.", "11"),
        ("len_b", "How many letters are in the word thermometer? Reply with the number.", "11"),
        ("cmp_a", "Which is larger, three eighths or 0.4? Reply with just the larger value.", "0.4"),
        ("cmp_b", "Which is smaller, 0.125 or one sixth? Reply with just the smaller value.", "0.125"),
        ("feb_a", "How many days were in February 2024? Reply with the number.", "29"),
        ("feb_b", "How many days were in February 2023? Reply with the number.", "28"),
        ("cm_a", "How many centimetres are in 2.5 metres? Reply with the number.", "250"),
        ("min_a", "How many minutes are in three and a half hours? Reply with the number.", "210"),
        ("sec_a", "How many seconds are in 45 minutes? Reply with the number.", str(45 * 60)),
        ("bin_a", "What is the decimal value of the binary number 1011? Reply with the number.", "11"),
        ("hex_a", "What is the decimal value of the hexadecimal number 1F? Reply with the number.", "31"),
        ("avg_a", "What is the average of 12, 18 and 30? Reply with the number.", "20"),
        ("rev_a", "Spell the word stressed backwards. Reply with the single resulting word.", "desserts"),
    ]
    return [
        Case(
            f"conversation_{name}",
            "conversation",
            prompt,
            "answer_has_token",
            {"expected": expected},
        )
        for name, prompt, expected in plan
    ]


def security_analysis_cases() -> list[Case]:
    """Twenty defensive questions with one settled, timeless answer each.

    Every prompt carries a term that activates ``classify_security_expertise``.
    None uses ``latest``/``current``/a CVE identifier, so none of them asks the
    runtime for time-sensitive research that a disabled network cannot supply.
    """
    plan: list[tuple[str, str, str]] = [
        ("osi_transport", "In the OSI model, which layer number is the transport layer? Reply with the number.", "4"),
        ("osi_network", "In the OSI model, which layer number performs IP routing? Reply with the number.", "3"),
        ("cidr_29", "In CIDR subnetting, how many usable IPv4 host addresses does a /29 provide? Reply with the number.", "6"),
        ("cidr_24", "In CIDR subnetting, how many usable IPv4 host addresses does a /24 provide? Reply with the number.", "254"),
        ("cidr_26_mask", "In IPv4 subnetting, what is the dotted-decimal netmask of a /26 network?", "255.255.255.192"),
        ("ipv6_bits", "How many bits long is an IPv6 address? Reply with the number.", "128"),
        ("dns_port", "Which port number does DNS use by default for ordinary queries? Reply with the number.", "53"),
        ("https_port", "In a firewall rule, which TCP port number does HTTPS use by default? Reply with the number.", "443"),
        ("ike_port", "Which UDP port number does IPsec IKE use by default? Reply with the number.", "500"),
        ("ssh_port", "In a firewall rule, which TCP port number does SSH use by default? Reply with the number.", "22"),
        ("handshake", "In the TCP three-way handshake, which two flags are set in the second packet? Reply with the flag names.", "syn"),
        ("arp", "In TCP/IP, an ARP request resolves an IP address to which kind of address? Reply with the two-word name.", "mac"),
        ("cwe89", "Which weakness class does CWE-89 describe? Reply with the name of the weakness.", "sql injection"),
        ("owasp_frame", "In OWASP secure-header guidance, which HTTP response header stops a page being rendered inside a frame?", "x-frame-options"),
        ("siem", "What does the acronym SIEM stand for in security operations?", "security information and event management"),
        ("waf", "What does the acronym WAF stand for in application security?", "web application firewall"),
        ("least_privilege", "Does least privilege grant an account the maximum or the minimum permissions it needs? Reply with one word.", "minimum"),
        ("dnssec", "Does DNSSEC provide confidentiality of DNS records or authentication of them? Reply with one word.", "authentication"),
        ("cvss_max", "In CVSS version 3.1, what is the highest possible base score? Reply with the number.", "10"),
        ("mtls", "In TLS, mTLS additionally requires which party to present a certificate? Reply with one word.", "client"),
    ]
    return [
        Case(
            f"security_analysis_{name}",
            "security_analysis",
            prompt,
            "answer_has_token",
            {"expected": expected},
        )
        for name, prompt, expected in plan
    ]


CASE_BUILDERS: dict[str, Callable[[], list[Case]]] = {
    "file_ops": file_ops_cases,
    "conversation": conversation_cases,
    "security_analysis": security_analysis_cases,
}


def build_cases(families: Sequence[str], per_family: int) -> list[Case]:
    """Return the ordered case plan, interleaved so a partial run stays balanced."""
    per_family = int(per_family)
    if per_family < 1:
        raise ValueError("per_family must be at least 1")
    buckets: list[list[Case]] = []
    for family in families:
        builder = CASE_BUILDERS.get(family)
        if builder is None:
            raise ValueError(
                f"No authored cases for family {family!r}; available: "
                + ", ".join(sorted(CASE_BUILDERS))
            )
        available = builder()
        if len(available) < per_family:
            raise ValueError(
                f"Family {family} has {len(available)} authored cases, "
                f"fewer than the requested {per_family}"
            )
        buckets.append(available[:per_family])
    ordered: list[Case] = []
    for position in range(per_family):
        for bucket in buckets:
            ordered.append(bucket[position])
    return ordered


def case_index(families: Sequence[str], per_family: int) -> dict[str, Case]:
    return {case.case_id: case for case in build_cases(families, per_family)}


# --------------------------------------------------------------------------- #
# Deterministic verification. Never the model's own claim.
# --------------------------------------------------------------------------- #


def _read_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


#: A denial standing immediately before the expected token turns a correct-looking
#: reply into a wrong one ("the answer is not 391"). Bare "no" is deliberately not
#: here: replies open with "No, it is 391" and that is a correct answer.
_NEGATION_BEFORE = re.compile(
    r"\b(?:not|never|cannot|isn['\u2019]?t|aren['\u2019]?t|wasn['\u2019]?t|"
    r"doesn['\u2019]?t|don['\u2019]?t|won['\u2019]?t|can['\u2019]?t|"
    r"rather than|instead of|incorrect|wrong|mistaken|false)\b",
    re.I,
)
#: A denial that follows the token and is bound to it by a copula also makes the
#: reply wrong ("391 is wrong", "391 but that is incorrect"). The character class
#: stops at a sentence boundary so "391. That is not 392." still counts as 391.
_DENIAL_AFTER = re.compile(
    r"^[^.!?\n]{0,30}?\b(?:is|are|was|were)\s+(?:not\s+)?"
    r"(?:wrong|incorrect|false|mistaken)\b",
    re.I,
)
_NEGATION_WINDOW = 40


def _normalise(text: str) -> str:
    return re.sub(r"[,_\s]+", " ", str(text).casefold())


def _clean_match(segment: str, needle: str) -> bool:
    """Is the token present in this segment without a denial in front of it?"""
    for match in re.finditer(rf"(?<![\w.]){re.escape(needle)}(?![\w])", segment):
        before = segment[max(0, match.start() - _NEGATION_WINDOW):match.start()]
        after = segment[match.end():match.end() + _NEGATION_WINDOW]
        if _NEGATION_BEFORE.search(before) is None and _DENIAL_AFTER.match(after) is None:
            return True
    return False


def _token_present(reply: str, expected: str) -> bool:
    """Grade a reply against an answer key the harness computed itself.

    Two things stop a wrong reply passing. The token is read from the reply's
    final non-empty line whenever that line mentions it at all, because that is
    where a model states its answer after any working; and an occurrence with a
    denial in the forty characters before it does not count, so "the answer is
    not 391" fails where a plain "391" passes. Bare "no" is deliberately not a
    denial here, because "No, it is 391" is a correct answer.
    """
    needle = _normalise(expected).strip()
    if not needle:
        return False
    lines = [line for line in str(reply).splitlines() if line.strip()]
    if lines:
        final = _normalise(lines[-1])
        if re.search(rf"(?<![\w.]){re.escape(needle)}(?![\w])", final):
            return _clean_match(final, needle)
    return _clean_match(_normalise(reply), needle)

def target_paths(case: Case) -> list[str]:
    """Every workspace path this case's verifier reads."""
    args = case.check_args
    return [str(args[key]) for key in ("path", "absent") if key in args]


VERIFIERS = frozenset({
    "answer_has_token", "file_absent", "file_has_line", "file_has_lines",
    "renamed", "json_has_key",
})


def verify(case: Case, workspace: Path, reply: str) -> bool:
    """Return whether the case's ground truth holds after the run."""
    if case.check not in VERIFIERS:
        raise ValueError(f"Unknown verifier: {case.check}")
    args = case.check_args
    if case.check == "answer_has_token":
        return _token_present(reply, str(args["expected"]))
    if case.check == "file_absent":
        return not (workspace / str(args["path"])).exists()
    target = workspace / str(args["path"])
    if case.check == "file_has_line":
        return target.is_file() and str(args["line"]) in _read_lines(target)
    if case.check == "file_has_lines":
        if not target.is_file():
            return False
        expected = [str(item) for item in args["lines"]]
        found = _read_lines(target)
        if args.get("exact"):
            return found == expected
        return found[: len(expected)] == expected
    if case.check == "renamed":
        if (workspace / str(args["absent"])).exists():
            return False
        return target.is_file() and str(args["line"]) in _read_lines(target)
    if case.check == "json_has_key":
        if not target.is_file():
            return False
        try:
            payload = json.loads(target.read_text(encoding="utf-8", errors="replace"))
        except (ValueError, OSError):
            return False
        return isinstance(payload, dict) and str(
            payload.get(str(args["key"]))
        ) == str(args["value"])
    raise AssertionError(f"Verifier {case.check} is registered but not implemented")


# --------------------------------------------------------------------------- #
# Sandbox environment
# --------------------------------------------------------------------------- #


def sandbox_environment(root: Path, model: str) -> dict[str, str]:
    """Return the child environment: local attested model only, everything else off.

    Host execution, desktop access, external egress and every cloud provider stay
    disabled. ``Config`` reads ``.env`` with ``setdefault``, so these explicit
    values win and no ambient configuration can loosen them.
    """
    prefix = str(model).split(":", 1)[0].strip().casefold()
    if prefix in NON_LOCAL_MODEL_PREFIXES:
        raise ValueError(
            f"{model!r} names the {prefix} provider. Only a local Ollama model is "
            "allowed here: it is the free, attested provider, and the CLI providers "
            "cannot satisfy model attestation by design"
        )
    environment = dict(os.environ)
    environment.update({
        "JARVIS_WORKSPACE": str(root / "workspace"),
        "JARVIS_DATA": str(root / "data"),
        "JARVIS_CLOUD_ENABLED": "false",
        "JARVIS_OLLAMA_ENABLED": "true",
        "JARVIS_EXTERNAL_ACCESS": "disabled",
        "JARVIS_EXECUTION_MODE": "disabled",
        "JARVIS_COMPUTER_ACCESS": "disabled",
        "JARVIS_MODEL": model,
        "JARVIS_FAST_MODEL": model,
        "JARVIS_REASONING_MODEL": model,
        "JARVIS_CODING_MODEL": model,
        "JARVIS_DEEP_MODEL": model,
        "JARVIS_LEARNING_MODEL": model,
        "JARVIS_BACKGROUND_MODEL": "fast",
        "PYTHONPATH": str(REPO_ROOT),
    })
    for key in ("JARVIS_OPENAI_API_ENABLED", "JARVIS_ANTHROPIC_API_ENABLED",
                "JARVIS_CODEX_CLI_ENABLED", "JARVIS_CLAUDE_CLI_ENABLED"):
        environment[key] = "false"
    return environment


def prepare_root(root: Path) -> Path:
    root = Path(root).expanduser().resolve()
    if root == REPO_ROOT or REPO_ROOT in root.parents or root in REPO_ROOT.parents:
        raise ValueError(
            "The sandbox root must be outside the repository so no run can touch "
            "the source tree"
        )
    (root / "workspace").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------- #
# One case, in its own process and its own conversation
# --------------------------------------------------------------------------- #


def assert_sandboxed(environ: dict[str, str] | None = None) -> None:
    """Refuse to run a case outside an explicitly isolated, capability-free runtime.

    ``run-case`` writes a real prediction through the runtime. Run by hand without
    the sandbox environment it would write into whatever ``JARVIS_DATA`` is
    ambient — which may be the operator's own database. It must not be possible to
    do that by accident.
    """
    values = dict(os.environ if environ is None else environ)
    missing = [
        key for key in ("JARVIS_DATA", "JARVIS_WORKSPACE") if not values.get(key)
    ]
    if missing:
        raise SystemExit(
            "Refusing to run: " + ", ".join(missing) + " must be set explicitly. "
            "Use the sandbox subcommand, which isolates them."
        )
    for key in ("JARVIS_EXECUTION_MODE", "JARVIS_COMPUTER_ACCESS",
                "JARVIS_EXTERNAL_ACCESS"):
        if values.get(key) != "disabled":
            raise SystemExit(
                f"Refusing to run: {key} must be 'disabled' for this exercise."
            )
    if str(values.get("JARVIS_CLOUD_ENABLED", "")).casefold() != "false":
        raise SystemExit("Refusing to run: JARVIS_CLOUD_ENABLED must be 'false'.")


def run_one_case(case: Case) -> dict[str, Any]:
    """Seed, run the real agent once, verify against ground truth, and report."""
    from jarvis.agent import Agent
    from jarvis.config import Config
    from jarvis.memory import Memory

    assert_sandboxed()
    config = Config.load()
    workspace = Path(config.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    # Clear what the verifier will read before seeding. Without this a repeat of
    # a create-shaped case would be satisfied by the first attempt's leftovers and
    # a do-nothing run would score as a pass.
    for relative in target_paths(case):
        (workspace / relative).unlink(missing_ok=True)
    for relative, content in case.seed_files:
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))

    def on_event(message: str) -> None:
        sys.stderr.write(f"[event] {message}\n")

    started = time.monotonic()
    status = "failed"
    tool_calls = 0
    prediction_id: int | None = None
    reply = ""
    error: str | None = None
    # Same construction as `jarvis ask` (`cli._run_ask`): Config.load(), one
    # Memory over <data>/jarvis.db, one Agent, one agent.run with
    # allow_companion_control. The CLI additionally takes a foreground lease,
    # passes a RuntimeGuard for Ctrl+C cancellation and records a reflection;
    # none of those touch how the prediction is recorded or resolved, and the
    # first-run provider wizard in cli.main is the reason this path is used
    # instead of spawning the CLI. Origin is "interactive" either way, because
    # no task_id is supplied (`agent.py:12035-12042`).
    with Memory(Path(config.data_dir) / "jarvis.db") as memory:
        agent = Agent(config, memory, on_event)
        try:
            result = agent.run(case.prompt, allow_companion_control=True)
        except Exception as exc:  # noqa: BLE001 - a provider outage is a real outcome
            error = type(exc).__name__
        else:
            status = str(getattr(result, "status", "failed"))
            tool_calls = int(getattr(result, "tool_calls", 0) or 0)
            prediction_id = getattr(result, "prediction_id", None)
            # AgentResult subclasses str: the reply text is the object itself.
            reply = str(result)
    elapsed = time.monotonic() - started
    try:
        verified = verify(case, workspace, reply)
    except Exception:  # noqa: BLE001 - a broken verifier must not look like a pass
        verified = False
    return {
        "case_id": case.case_id,
        "intended_family": case.intended_family,
        "runtime_status": status,
        "tool_calls": tool_calls,
        "prediction_id": None if prediction_id is None else int(prediction_id),
        "independently_verified": bool(verified),
        "seconds": round(elapsed, 2),
        "error": error,
    }


# --------------------------------------------------------------------------- #
# Sandbox driver
# --------------------------------------------------------------------------- #


def _observed_families(database: Path, prediction_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Read back what the runtime actually recorded for each case."""
    import sqlite3

    if not prediction_ids:
        return {}
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in prediction_ids)
        rows = connection.execute(
            "SELECT id, family, origin, predicted_success, basis, actual_status, "
            "evidence_ok, failure_class FROM task_predictions "
            f"WHERE id IN ({placeholders})",
            tuple(int(item) for item in prediction_ids),
        ).fetchall()
    finally:
        connection.close()
    return {int(row["id"]): dict(row) for row in rows}


def _family_breakdown(results: Sequence[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Group the run by the family the runtime actually recorded.

    ``recorded_complete`` is the runtime's outcome, which is what ``competence()``
    scores. ``independently_verified`` is this harness checking ground truth.
    They diverge wherever a family's verification is ``not_applicable``: the
    runtime counts a finished turn as a success without judging the answer.
    """
    breakdown: dict[str, dict[str, int]] = {}
    for item in results:
        family = str(item.get("observed_family") or "unrecorded")
        bucket = breakdown.setdefault(family, {
            "cases": 0,
            "recorded_complete": 0,
            "independently_verified": 0,
            "agreement": 0,
        })
        bucket["cases"] += 1
        complete = item.get("recorded_status") == "complete"
        verified = bool(item.get("independently_verified"))
        bucket["recorded_complete"] += int(complete)
        bucket["independently_verified"] += int(verified)
        bucket["agreement"] += int(
            item.get("recorded_status") is not None and complete == verified
        )
    return dict(sorted(breakdown.items()))


def _resolved_counts(database: Path) -> dict[str, int]:
    """Per-family count of the resolved rows ``competence()`` would score."""
    import sqlite3

    if not Path(database).is_file():
        return {}
    connection = sqlite3.connect(f"{Path(database).resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT family, COUNT(*) FROM task_predictions "
            "WHERE resolved_at IS NOT NULL "
            "AND origin IN ('interactive','worker','proactive') GROUP BY family"
        ).fetchall()
    finally:
        connection.close()
    return {str(family): int(count) for family, count in rows}


def retryable_case_ids(run_file: Path) -> list[str]:
    """Return the cases in a previous run that recorded no outcome at all.

    Re-running a case that *did* record one would add a second attempt for the
    same task and silently inflate that family's sample, so a case whose earlier
    row carries a prediction id is refused here rather than filtered later.
    """
    payload = json.loads(Path(run_file).read_text(encoding="utf-8"))
    recorded: list[str] = []
    retryable: list[str] = []
    for case in payload.get("cases") or []:
        case_id = str(case.get("case_id") or "")
        if not case_id:
            continue
        if case.get("prediction_id") is None:
            retryable.append(case_id)
        else:
            recorded.append(case_id)
    overlap = sorted(set(retryable) & set(recorded))
    if overlap:
        raise ValueError(
            f"{overlap[0]} already recorded an outcome in that run; re-running it "
            "would double-count the family"
        )
    if not retryable:
        raise ValueError(
            "that run recorded an outcome for every case; there is nothing to retry"
        )
    return retryable


def assert_outcome_delta(
    before: dict[str, int],
    after: dict[str, int],
    results: Sequence[dict[str, Any]],
) -> dict[str, int]:
    """Refuse to report a sample that gained outcomes this run cannot account for.

    Each case that recorded a row must have raised its observed family's resolved
    count by exactly one. If a family moved by more, a retry has been run twice
    into the same database and the sample is inflated; if it moved by less,
    something outside this harness is writing to it. Either way the report is not
    trustworthy and the run stops here rather than publishing it.
    """
    expected: dict[str, int] = {}
    for item in results:
        family = item.get("observed_family")
        if family:
            expected[str(family)] = expected.get(str(family), 0) + 1
    drift = {
        family: (
            int(after.get(family, 0))
            - int(before.get(family, 0))
            - int(expected.get(family, 0))
        )
        for family in set(before) | set(after) | set(expected)
    }
    unexpected = {family: value for family, value in drift.items() if value}
    if unexpected:
        raise RuntimeError(
            "the sandbox database gained outcomes this run did not account for: "
            + json.dumps(dict(sorted(unexpected.items())), sort_keys=True)
            + " - refusing to report a sample that may be double-counted"
        )
    return dict(sorted(expected.items()))


def run_sandbox(
    root: Path,
    families: Sequence[str],
    per_family: int,
    model: str,
    timeout: int,
    retry_from: Path | str | None = None,
) -> dict[str, Any]:
    """Run the case plan, or a previous run's unrecorded cases, into one root.

    ``retry_from`` exists for one honest reason: a case can be lost to the host
    rather than to the model - a process-creation failure under load, or a
    timeout - and that leaves the family one outcome short of the gate's minimum.
    The cases to re-run are read from that run's detail file and are exactly the
    ones whose ``prediction_id`` is null, so a case that already recorded an
    outcome can never be run a second time into the same database. Each case
    clears the paths its verifier reads and then re-seeds them, so a repeat is
    never pre-satisfied by what the first attempt left behind, and the per-family
    resolved counts are checked afterwards against the number actually retried.
    """
    root = prepare_root(root)
    environment = sandbox_environment(root, model)
    cases = build_cases(families, per_family)
    database = root / "data" / "jarvis.db"
    retried_from: str | None = None
    before_counts = _resolved_counts(database)
    if retry_from:
        retried_from = str(Path(retry_from).name)
        wanted = retryable_case_ids(Path(retry_from))
        known = {case.case_id for case in cases}
        unknown = [item for item in wanted if item not in known]
        if unknown:
            raise ValueError(f"Unknown case id: {sorted(unknown)[0]}")
        cases = [case for case in cases if case.case_id in set(wanted)]
    results: list[dict[str, Any]] = []
    for position, case in enumerate(cases, start=1):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "run-case",
            "--case-id",
            case.case_id,
            "--families",
            ",".join(families),
            "--per-family",
            str(per_family),
        ]
        sys.stderr.write(f"[{position}/{len(cases)}] {case.case_id}\n")
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=str(REPO_ROOT),
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            results.append({
                "case_id": case.case_id,
                "intended_family": case.intended_family,
                "runtime_status": "failed",
                "tool_calls": 0,
                "prediction_id": None,
                "independently_verified": False,
                "seconds": round(time.monotonic() - started, 2),
                "error": "TimeoutExpired",
            })
            continue
        payload: dict[str, Any] | None = None
        for line in completed.stdout.splitlines():
            if line.startswith(RESULT_MARKER):
                payload = json.loads(line[len(RESULT_MARKER):])
        if payload is None:
            payload = {
                "case_id": case.case_id,
                "intended_family": case.intended_family,
                "runtime_status": "failed",
                "tool_calls": 0,
                "prediction_id": None,
                "independently_verified": False,
                "seconds": round(time.monotonic() - started, 2),
                "error": f"no result (exit {completed.returncode})",
            }
        results.append(payload)

    database = root / "data" / "jarvis.db"
    recorded = (
        _observed_families(
            database,
            [item["prediction_id"] for item in results if item.get("prediction_id")],
        )
        if database.is_file()
        else {}
    )
    for item in results:
        row = recorded.get(int(item["prediction_id"])) if item.get("prediction_id") else None
        item["observed_family"] = None if row is None else str(row["family"])
        item["observed_origin"] = None if row is None else str(row["origin"])
        item["predicted_success"] = None if row is None else float(row["predicted_success"])
        item["prediction_basis"] = None if row is None else str(row["basis"])
        item["recorded_status"] = None if row is None else str(row["actual_status"])
    after_counts = _resolved_counts(database)
    assert_outcome_delta(before_counts, after_counts, results)
    return {
        "model": model,
        "families_requested": list(families),
        "per_family": int(per_family),
        "retried_from": retried_from,
        "retried_cases": sorted(case.case_id for case in cases) if retried_from else None,
        "resolved_counts_before": dict(sorted(before_counts.items())),
        "resolved_counts_after": dict(sorted(after_counts.items())),
        "resolved_delta_matches_cases_recorded": True,
        "by_observed_family": _family_breakdown(results),
        "cases_run": len(results),
        "cases_recorded": sum(1 for item in results if item.get("prediction_id")),
        "runtime_complete": sum(
            1 for item in results if item.get("recorded_status") == "complete"
        ),
        "independently_verified": sum(
            1 for item in results if item.get("independently_verified")
        ),
        "agreement_with_ground_truth": sum(
            1
            for item in results
            if item.get("recorded_status") is not None
            and (item["recorded_status"] == "complete")
            == bool(item.get("independently_verified"))
        ),
        "total_seconds": round(sum(float(item.get("seconds") or 0.0) for item in results), 1),
        "cases": results,
    }


def merge_runs(run_files: Sequence[Path | str]) -> dict[str, Any]:
    """Union several run detail files into one summary with retry provenance.

    The first file is the base run. A case that a later file re-ran replaces the
    earlier attempt entirely, so one task contributes exactly one attempt, and the
    reason the earlier attempt produced nothing is carried forward verbatim from
    that attempt's own ``error`` field rather than being asserted here.
    """
    payloads = [
        json.loads(Path(item).read_text(encoding="utf-8")) for item in run_files
    ]
    if not payloads:
        raise ValueError("merge needs at least one run detail file")
    base = payloads[0]
    cases: dict[str, dict[str, Any]] = {
        str(case["case_id"]): case for case in base.get("cases") or []
    }
    order = [str(case["case_id"]) for case in base.get("cases") or []]
    retried: dict[str, str] = {}
    for payload in payloads[1:]:
        for case in payload.get("cases") or []:
            case_id = str(case["case_id"])
            earlier = cases.get(case_id)
            if earlier is not None:
                if earlier.get("prediction_id") is not None:
                    raise ValueError(
                        f"{case_id} recorded an outcome in an earlier file; merging a "
                        "second attempt would double-count its family"
                    )
                retried[case_id] = str(earlier.get("error") or "no outcome recorded")
            else:
                order.append(case_id)
            cases[case_id] = case
    merged_cases = [cases[case_id] for case_id in order]
    reasons = sorted(set(retried.values()))
    return {
        "model": base.get("model"),
        "families_requested": base.get("families_requested"),
        "per_family": base.get("per_family"),
        "commands": [
            "python scripts/run_phase2_calibration.py sandbox --root <sandbox> "
            f"--per-family {base.get('per_family', '<n>')} --run-out <run-1>",
            "python scripts/run_phase2_calibration.py sandbox --root <sandbox> "
            f"--per-family {base.get('per_family', '<n>')} --retry-from <run-1> "
            "--run-out <run-2>",
            "python scripts/run_phase2_calibration.py merge-run --run-file <run-1> "
            "--run-file <run-2> --out <merged>",
            "python scripts/run_phase2_calibration.py report "
            "--database <sandbox>/data/jarvis.db --run-file <merged> "
            "--evidence-out docs/evidence --manifest-file <phase 2 base manifest>",
        ],
        "retried_after_host_failure": sorted(retried),
        "timed_out_cases": sorted(
            case_id for case_id, reason in retried.items()
            if "timeout" in reason.casefold()
        ),
        "retry_reason": (
            "These cases recorded no outcome on the first attempt and were re-run "
            "into the same sandbox. The runtime reported: "
            + "; ".join(reasons)
            + ". Each retried case clears the paths its verifier reads and re-seeds "
            "them, so no repeat was pre-satisfied, and the sandbox refuses to "
            "re-run a case that already recorded an outcome."
            if retried
            else "No case needed re-running."
        ),
        "by_observed_family": _family_breakdown(merged_cases),
        "cases_run": len(merged_cases),
        "cases_recorded": sum(
            1 for case in merged_cases if case.get("prediction_id")
        ),
        "runtime_complete": sum(
            1 for case in merged_cases if case.get("recorded_status") == "complete"
        ),
        "independently_verified": sum(
            1 for case in merged_cases if case.get("independently_verified")
        ),
        "agreement_with_ground_truth": sum(
            1
            for case in merged_cases
            if case.get("recorded_status") is not None
            and (case["recorded_status"] == "complete")
            == bool(case.get("independently_verified"))
        ),
        "agreement_is_an_upper_bound_for": list(EVIDENCE_FREE_FAMILIES),
        "total_seconds": round(
            sum(float(case.get("seconds") or 0.0) for case in merged_cases), 1
        ),
        "cases": merged_cases,
    }


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _base_block(args: argparse.Namespace) -> dict[str, Any]:
    """Describe the WP-0 base manifest this run was measured against.

    Phase 2 is uncommitted, so the manifest hash - not a commit id - is the
    identity every recorded result is pinned to.
    """
    manifest = getattr(args, "manifest_file", None)
    if manifest:
        payload = json.loads(Path(manifest).read_text(encoding="utf-8"))
        return {
            "kind": "phase2_base_file_manifest",
            "manifest_sha256": str(payload["manifest_sha256"]),
            "base_commit": str(payload.get("base_commit", "")),
            "branch": str(payload.get("branch", "")),
            "manifest_file_count": len(payload.get("files") or {}),
            "committed": False,
        }
    if getattr(args, "manifest_sha256", None):
        return {
            "kind": "phase2_base_file_manifest",
            "manifest_sha256": str(args.manifest_sha256),
            "committed": False,
        }
    raise SystemExit("Recording evidence needs --manifest-sha256 or --manifest-file")


def _sandbox_switches(root: Path | None, model: str) -> dict[str, str]:
    """The capability switches this exercise pins, without any path."""
    environment = sandbox_environment(Path(root) if root else Path.cwd(), model)
    keys = (
        "JARVIS_CLOUD_ENABLED", "JARVIS_OLLAMA_ENABLED", "JARVIS_EXTERNAL_ACCESS",
        "JARVIS_EXECUTION_MODE", "JARVIS_COMPUTER_ACCESS", "JARVIS_MODEL",
        "JARVIS_FAST_MODEL", "JARVIS_REASONING_MODEL", "JARVIS_CODING_MODEL",
        "JARVIS_DEEP_MODEL", "JARVIS_LEARNING_MODEL",
    )
    return {key: environment[key] for key in keys}


def ollama_version() -> str | None:
    """Read the local provider's version so the record names what it ran on."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:11434/api/version", timeout=5
        ) as response:
            return str(json.loads(response.read().decode("utf-8")).get("version") or "")
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _emit_report(
    report: dict[str, Any],
    args: argparse.Namespace,
    *,
    run: dict[str, Any] | None = None,
    command: str,
    configuration: str,
    limitations: Sequence[str],
) -> None:
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(calibration.format_report(report))
    destination = getattr(args, "evidence_out", None)
    if not destination:
        return
    base = _base_block(args)
    provider_name = str(getattr(args, "provider", "ollama"))
    model = str((run or {}).get("model") or getattr(args, "model", "") or "")
    document = calibration.evidence_document(
        report,
        benchmark="calibration",
        provider={
            "name": provider_name,
            "version": (ollama_version() if provider_name == "ollama" else None),
        },
        base=base,
        configuration_class={
            "summary": configuration,
            "switches": _sandbox_switches(None, model or DEFAULT_MODEL),
        },
        command=command,
        result=_result_line(report, run),
        known_limitations=limitations,
        model={"requested": model, "served": model} if model else None,
        run=run,
    )
    target = Path(destination)
    if target.is_dir():
        target = target / calibration.evidence_filename(
            "calibration", provider_name, base["manifest_sha256"]
        )
    try:
        written = calibration.write_evidence(
            document, target, force=bool(getattr(args, "force", False))
        )
    except FileExistsError:
        raise SystemExit(
            f"{target.name} already exists. A recorded result is evidence, not a "
            "scratch file: pass --force only when you mean to replace it."
        ) from None
    sys.stderr.write(f"evidence written: {written.name}\n")


def _result_line(report: dict[str, Any], run: dict[str, Any] | None) -> str:
    """State the headline with the two caveats that change how it should be read."""
    count = int(report["calibrated_family_count"])
    parts = [
        f"{count} calibrated famil{'y' if count == 1 else 'ies'} "
        f"({', '.join(report['calibrated_families']) or 'none'}); "
        f"{report['resolved_outcomes_counted']} counted outcomes"
    ]
    evidence_free = [
        item["family"]
        for item in report["families"]
        if item["family"] in EVIDENCE_FREE_FAMILIES
        and item["allowed"]
        and not int(item["evidence_applicable"] or 0)
    ]
    if evidence_free:
        parts.append(
            "Read with care: "
            + " and ".join(evidence_free)
            + (" has" if len(evidence_free) == 1 else " have")
            + " no evidence to check, so the runtime records a finished turn as "
            "complete and the observed success rate is 1.0 by construction - it is "
            "calibration about completing the turn, not about being correct."
        )
    parts.append(
        "initiative_eligibility counts calibrated families across all eleven "
        "PREDICTION_FAMILIES, so 'three calibrated families' does not mean three "
        "families with real-world effects; see docs/CALIBRATION_RUNBOOK.md."
    )
    open_predictions = int(report.get("open_predictions") or 0)
    if open_predictions and run:
        timed_out = run.get("timed_out_cases") or []
        parts.append(
            f"{open_predictions} prediction(s) in this database are unresolved and "
            "therefore uncounted by competence(): a case killed at the per-case "
            "timeout has already recorded its prediction but the agent never reaches "
            "the run boundary that would resolve it"
            + (
                ", which is the first attempt at "
                + ", ".join(str(item) for item in timed_out)
                + " (re-run successfully afterwards)."
                if timed_out
                else "."
            )
        )
    return " ".join(parts)


def _families_limitation(families: Sequence[str]) -> str:
    """State which families were exercised and what the others would have needed."""
    exercised = ", ".join(families) if families else "none"
    return (
        f"Only families that need no additional capability were exercised: "
        f"{exercised}. Coding families need JARVIS_EXECUTION_MODE=trusted-host and "
        "research families need JARVIS_EXTERNAL_ACCESS=trusted-external."
    )


def _recorded_command(summary: dict[str, Any] | None) -> str:
    """Say how the rows were produced, not only how they were read back."""
    if summary is None:
        return "python scripts/run_phase2_calibration.py report --database <db>"
    steps = summary.get("commands")
    if steps:
        return " ; ".join(str(step) for step in steps)
    return (
        "python scripts/run_phase2_calibration.py sandbox --root <sandbox> "
        f"--per-family {summary.get('per_family', '<n>')}"
        " ; python scripts/run_phase2_calibration.py report "
        "--database <sandbox>/data/jarvis.db --run-file <run detail>"
    )


def _run_summary(path: str | None) -> dict[str, Any] | None:
    """Load a previous ``--run-out`` detail file, minus the per-case rows."""
    if not path:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return {key: value for key, value in payload.items() if key != "cases"}


def cmd_report(args: argparse.Namespace) -> int:
    with calibration.read_only_memory(args.database) as memory:
        report = calibration.calibration_report(memory)
    summary = _run_summary(getattr(args, "run_file", None))
    limitations = [
        "Read-only report over a database copy; the source file is never opened "
        "for writing.",
        "Only interactive, worker and proactive outcomes are counted; practice "
        "and companion origins are excluded by Memory.competence.",
    ]
    if summary is not None:
        limitations.extend(SANDBOX_LIMITATIONS)
        limitations.append(
            _families_limitation(list(summary.get("families_requested") or []))
        )
        limitations.append(UPPER_BOUND_LIMITATION)
        limitations.append(GATE_FIX_LIMITATION)
    _emit_report(
        report,
        args,
        run=summary,
        command=_recorded_command(summary),
        configuration=str(args.configuration),
        limitations=limitations,
    )
    return 0


def cmd_cases(args: argparse.Namespace) -> int:
    families = _families(args)
    cases = build_cases(families, args.per_family)
    if args.json:
        print(json.dumps([
            {
                "case_id": case.case_id,
                "intended_family": case.intended_family,
                "check": case.check,
                "seeded": [name for name, _ in case.seed_files],
            }
            for case in cases
        ], ensure_ascii=False, indent=2))
        return 0
    for case in cases:
        print(
            f"{case.case_id:38}{case.intended_family:20}{case.check:16}"
            f"{'seeded' if case.seed_files else ''}"
        )
    print(f"\n{len(cases)} case(s) across {len(families)} famil(y/ies).")
    for family in families:
        print(f"  {family}: requires {CAPABILITY_REQUIREMENTS.get(family, 'unknown')}")
    return 0


def cmd_run_case(args: argparse.Namespace) -> int:
    cases = case_index(_families(args), args.per_family)
    case = cases.get(args.case_id)
    if case is None:
        raise SystemExit(f"Unknown case id: {args.case_id}")
    result = run_one_case(case)
    print(RESULT_MARKER + json.dumps(result, ensure_ascii=False))
    return 0


def cmd_merge_run(args: argparse.Namespace) -> int:
    try:
        merged = merge_runs(args.run_file)
    except ValueError as error:
        return _refuse(str(error))
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    print(json.dumps(
        {key: value for key, value in merged.items() if key != "cases"},
        ensure_ascii=False,
        indent=2,
    ))
    sys.stderr.write(f"merged run written: {target.name}\n")
    return 0


def _refuse(message: str) -> int:
    """Report a refusal the caller can act on, without a traceback."""
    sys.stderr.write(f"refused: {message}\n")
    return REFUSED_EXIT


def cmd_sandbox(args: argparse.Namespace) -> int:
    families = _families(args)
    try:
        run = run_sandbox(
            Path(args.root),
            families,
            args.per_family,
            args.model,
            args.timeout,
            retry_from=args.retry_from,
        )
    except ValueError as error:
        return _refuse(str(error))
    database = Path(args.root).expanduser().resolve() / "data" / "jarvis.db"
    if args.run_out:
        target = Path(args.run_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            (json.dumps(run, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        )
        sys.stderr.write(f"run detail written: {target}\n")
    with calibration.read_only_memory(database) as memory:
        report = calibration.calibration_report(memory)
    summary = {
        key: value for key, value in run.items() if key != "cases"
    }
    _emit_report(
        report,
        args,
        run=summary,
        command=(
            "python scripts/run_phase2_calibration.py sandbox --root <sandbox> "
            f"--families {','.join(families)} --per-family {args.per_family}"
        ),
        configuration=(
            f"isolated JARVIS_WORKSPACE/JARVIS_DATA; ollama {args.model}; "
            "cloud, host execution, desktop access and external access disabled"
        ),
        limitations=[
            *SANDBOX_LIMITATIONS,
            _families_limitation(families),
            UPPER_BOUND_LIMITATION,
            GATE_FIX_LIMITATION,
        ],
    )
    return 0


def _families(args: argparse.Namespace) -> list[str]:
    raw = getattr(args, "families", None)
    if not raw:
        return list(OFFLINE_FAMILIES)
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Phase 2 calibration report and sandbox exercise",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--json", action="store_true")
        target.add_argument("--evidence-out", default=None)
        target.add_argument("--manifest-file", default=None)
        target.add_argument("--manifest-sha256", default=None)
        target.add_argument("--provider", default="ollama")
        target.add_argument(
            "--force",
            action="store_true",
            help="replace an evidence artifact that already exists",
        )

    report = sub.add_parser("report", help="read-only report for one database")
    report.add_argument("--database", required=True)
    report.add_argument(
        "--configuration",
        default="read-only report over an existing database",
    )
    report.add_argument(
        "--run-file",
        default=None,
        help="a previous --run-out detail file, merged into the evidence artifact",
    )
    add_common(report)

    cases = sub.add_parser("cases", help="print the task plan without any model")
    cases.add_argument("--families", default=None)
    cases.add_argument("--per-family", type=int, default=DEFAULT_PER_FAMILY)
    cases.add_argument("--json", action="store_true")

    single = sub.add_parser("run-case", help="internal: run one case in this process")
    single.add_argument("--case-id", required=True)
    single.add_argument("--families", default=None)
    single.add_argument("--per-family", type=int, default=DEFAULT_PER_FAMILY)

    merge = sub.add_parser(
        "merge-run",
        help="union run detail files into one summary with retry provenance",
    )
    merge.add_argument("--run-file", action="append", required=True)
    merge.add_argument("--out", required=True)

    sandbox = sub.add_parser("sandbox", help="run the exercise, then report")
    sandbox.add_argument("--root", required=True)
    sandbox.add_argument("--families", default=None)
    sandbox.add_argument("--per-family", type=int, default=DEFAULT_PER_FAMILY)
    sandbox.add_argument("--model", default=DEFAULT_MODEL)
    sandbox.add_argument("--timeout", type=int, default=DEFAULT_CASE_TIMEOUT)
    sandbox.add_argument("--run-out", default=None)
    sandbox.add_argument(
        "--retry-from",
        default=None,
        metavar="RUN_FILE",
        help=(
            "a previous --run-out detail file; re-runs exactly the cases it shows "
            "recorded no outcome, for losses to a host process failure or a timeout "
            "rather than to the model. A case that already recorded an outcome is "
            "refused, so a family cannot be double-counted"
        ),
    )
    add_common(sandbox)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "report": cmd_report,
        "cases": cmd_cases,
        "run-case": cmd_run_case,
        "merge-run": cmd_merge_run,
        "sandbox": cmd_sandbox,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
