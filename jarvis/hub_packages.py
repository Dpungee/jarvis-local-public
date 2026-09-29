"""Operator-approved package installs for Hub agents (pip and npm).

Run-program policy refuses ``pip install``, ``python -m venv`` and ``npm install`` outright, so
a Hub agent could never add a missing dependency. ``install_packages`` is the narrow way in:

* Only plain registry packages: for pip, ``name``, ``name[extra]`` and version clauses such as
  ``==1.2.3`` or ``>=1.0,<2``; for npm, ``name`` or ``@scope/name`` with an optional
  ``@version``. URLs, VCS links, paths, archives, direct references and anything that looks
  like an option are refused, as are duplicates and entries over 100 characters.
* Every call waits for an exact operator approval that names the manager, the packages and
  where they go (see ``plan``). The approved plan is exactly what runs.
* pip installs into the Python that run_process uses (the Hub's own interpreter, shared by
  every agent, which is why it asks) with ``--disable-pip-version-check --no-input`` and, unless
  the operator approved source builds, ``--only-binary=:all:`` so no package build code runs.
  npm installs into one project folder (``--prefix`` pins it there) with ``--ignore-scripts``.
* The command runs without a shell through the same contained host backend as run_process
  (a Windows job object, process-tree kill on timeout), with run_process's minimal
  environment (no operator secrets), for at most ten minutes. The output is bounded.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_PACKAGES = 20
MAX_SPEC_CHARS = 100
# Keeps the whole approval readable and inside the approval store's size bound.
MAX_TOTAL_CHARS = 1_000
TIMEOUT_SECONDS = 600
MAX_OUTPUT_CHARS = 3_000
PIP_FLAGS = ("--disable-pip-version-check", "--no-input")
NPM_FLAGS = ("--ignore-scripts", "--no-audit", "--no-fund")

_PIP_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_PIP_CLAUSE = r"(?:===|==|!=|~=|<=|>=|<|>)[A-Za-z0-9.*+!_-]+"
_PIP_SPEC = re.compile(rf"({_PIP_NAME})(?:\[{_PIP_NAME}(?:,{_PIP_NAME})*\])?(?:{_PIP_CLAUSE}(?:,{_PIP_CLAUSE})*)?")
_NPM_NAME = r"(?:@[a-z0-9][a-z0-9._~-]*/)?[A-Za-z0-9][A-Za-z0-9._~-]*"
_NPM_SPEC = re.compile(rf"({_NPM_NAME})(?:@[A-Za-z0-9.^~<>=*+_-]+)?")
_NPM_SOURCES = ("git:", "git+", "github:", "gitlab:", "bitbucket:", "gist:", "file:", "link:", "npm:",
                "workspace:", "http:", "https:")


class PackageError(ValueError):
    """A refused install request, in words the agent can act on."""


def _common(spec: Any, manager: str) -> str:
    if not isinstance(spec, str):
        raise PackageError("Each package is a text name such as requests or requests==2.32.3.")
    value = spec.strip()
    if not value or len(value) > MAX_SPEC_CHARS:
        raise PackageError(f"Each package must be 1-{MAX_SPEC_CHARS} characters.")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise PackageError(f"{value[:60]!r}: write one package per entry, without spaces.")
    if value.startswith("-"):
        raise PackageError(f"{value[:60]}: options are not accepted, only package names.")
    if "://" in value or value.casefold().startswith(("git+", "hg+", "svn+", "bzr+", "http:", "https:", "file:")):
        raise PackageError(f"{value[:60]}: URLs and version-control links are not accepted, only registry packages.")
    if value.startswith((".", "~")) or "\\" in value or re.match(r"^[A-Za-z]:", value):
        raise PackageError(f"{value[:60]}: local paths are not accepted, only registry packages.")
    if manager == "npm" and value.casefold().endswith((".tgz", ".tar.gz", ".tar")):
        raise PackageError(f"{value[:60]}: tarballs are not accepted, only registry packages.")
    return value


def _pip(spec: Any) -> tuple[str, str]:
    value = _common(spec, "pip")
    if "@" in value:
        raise PackageError(f"{value[:60]}: direct references (@) are not accepted; use name==version.")
    if "/" in value or ";" in value:
        raise PackageError(f"{value[:60]}: only name, name[extra] and version clauses are accepted.")
    match = _PIP_SPEC.fullmatch(value)
    if match is None:
        raise PackageError(f"{value[:60]} is not a plain requirement (examples: requests, "
                           "requests==2.32.3, pandas>=2,<3, uvicorn[standard]).")
    return value, re.sub(r"[-_.]+", "-", match.group(1)).casefold()


def _npm(spec: Any) -> tuple[str, str]:
    value = _common(spec, "npm")
    if value.casefold().startswith(_NPM_SOURCES) or ":" in value:
        raise PackageError(f"{value[:60]}: git, GitHub, file, link and URL sources are not accepted.")
    if "/" in value and not value.startswith("@"):
        raise PackageError(f"{value[:60]}: paths and GitHub shorthands are not accepted; use a registry name.")
    match = _NPM_SPEC.fullmatch(value)
    if match is None:
        raise PackageError(f"{value[:60]} is not a plain npm package (examples: lodash, lodash@4.17.21, "
                           "@types/node@22).")
    return value, match.group(1).casefold()


def validate(manager: Any, packages: Any) -> list[str]:
    """The package list, validated for its manager, in the order given."""
    if manager not in ("pip", "npm"):
        raise PackageError('manager must be "pip" or "npm".')
    if not isinstance(packages, list) or not 1 <= len(packages) <= MAX_PACKAGES:
        raise PackageError(f"packages must be a list of 1-{MAX_PACKAGES} names.")
    checked, seen = [], set()
    for spec in packages:
        value, name = (_pip if manager == "pip" else _npm)(spec)
        if name in seen:
            raise PackageError(f"{name} is listed more than once.")
        seen.add(name)
        checked.append(value)
    if sum(len(p) for p in checked) > MAX_TOTAL_CHARS:
        raise PackageError(f"The package list is too long for one approval; split it (up to "
                           f"{MAX_TOTAL_CHARS} characters in total).")
    return checked


def _folder(root: Path, directory: Any) -> Path:
    base = Path(root).resolve(strict=True)
    if directory in (None, "", "."):
        return base
    if not isinstance(directory, str) or len(directory) > 400:
        raise PackageError("directory must be a project-relative folder.")
    raw = Path(directory)
    if raw.is_absolute() or re.match(r"^[A-Za-z]:", directory):
        raise PackageError("directory must be relative to the project folder.")
    target = (base / raw).resolve()
    try:
        parts = target.relative_to(base).parts
    except ValueError:
        raise PackageError("directory must stay inside the project folder.") from None
    if any(p in {"node_modules", ".git"} or p.startswith(".jarvis") for p in parts):
        raise PackageError("directory must be an ordinary project folder.")
    if not target.is_dir() or (base / raw).is_symlink():
        raise PackageError("directory must be an existing project folder.")
    return target


def plan(root: Path, arguments: dict[str, Any], *, python_command: Callable[[list[str]], list[str]],
         npm_command: Callable[[list[str]], list[str]]) -> dict[str, Any]:
    """Validate a request and build the exact command it would run.

    ``python_command``/``npm_command`` turn arguments into the resolved argv the Hub uses for
    run_process (the interpreter or node + npm-cli.js), so the approval names real programs."""
    allowed = {"manager", "packages", "directory", "allow_source_builds"}
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise PackageError("install_packages takes manager, packages, directory (npm) and allow_source_builds (pip).")
    manager = arguments.get("manager")
    packages = validate(manager, arguments.get("packages"))
    source_builds = arguments.get("allow_source_builds", False)
    if not isinstance(source_builds, bool):
        raise PackageError("allow_source_builds must be true or false.")
    base = Path(root).resolve(strict=True)
    if manager == "pip":
        if arguments.get("directory") not in (None, "", "."):
            raise PackageError("directory applies to npm only; pip installs into the Hub's Python.")
        flags = [*PIP_FLAGS, *([] if source_builds else ["--only-binary=:all:"])]
        argv = python_command(["-m", "pip", "install", *flags, *packages])
        return {"manager": "pip", "packages": packages, "source_builds": source_builds, "argv": argv,
                "cwd": None, "folder": None, "interpreter": argv[0],
                "display": "python -m pip install " + " ".join([*flags, *packages])}
    if source_builds:
        raise PackageError("allow_source_builds applies to pip only; npm always runs with --ignore-scripts.")
    folder = _folder(base, arguments.get("directory"))
    if (folder / ".npmrc").exists():
        raise PackageError("That folder has its own .npmrc, which could change where npm installs from; "
                           "remove it or pick another folder.")
    relative = folder.relative_to(base).as_posix() or "."
    argv = npm_command(["install", *NPM_FLAGS, "--prefix", str(folder), *packages])
    return {"manager": "npm", "packages": packages, "source_builds": False, "argv": argv, "cwd": str(folder),
            "folder": relative, "interpreter": None,
            "display": f"npm install {' '.join(NPM_FLAGS)} --prefix <project>/{relative} " + " ".join(packages)}


def approval_resource(entry: dict[str, Any]) -> dict[str, Any]:
    """What the operator approves: manager, exact packages, where they go and the command."""
    where = (f"the Hub's Python ({entry['interpreter']}), shared by every agent" if entry["manager"] == "pip"
             else f"project folder {entry['folder']}")
    resource = {"tool": "install_packages", "manager": entry["manager"], "packages": entry["packages"],
                "installs_into": where, "command": entry["display"]}
    if entry["manager"] == "pip":
        resource["source_builds"] = ("ALLOWED: packages without a prebuilt wheel are built from source, which "
                                     "runs their build code" if entry["source_builds"] else
                                     "not allowed (prebuilt wheels only)")
    return resource


def approval_reason(entry: dict[str, Any]) -> str:
    names = ", ".join(entry["packages"])
    if entry["manager"] == "pip":
        builds = ("SOURCE BUILDS ALLOWED: package build code will run on this computer."
                  if entry["source_builds"] else "Prebuilt wheels only; no package build code runs.")
        return (f"This downloads and installs {len(entry['packages'])} Python package(s) into the Hub's Python, "
                f"which every agent's programs use: {names}. {builds}")[:1000]
    return (f"This downloads and installs {len(entry['packages'])} npm package(s) into project folder "
            f"{entry['folder']} (install scripts are not run): {names}.")[:1000]


def _tail(text: str) -> str:
    from .redaction import redact_secrets

    text = redact_secrets(str(text or ""), "[redacted]")
    return text if len(text) <= MAX_OUTPUT_CHARS else "…" + text[-MAX_OUTPUT_CHARS:]


def run(entry: dict[str, Any], *, env: dict[str, str], cwd: Path,
        runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Run the approved command (no shell, contained, ten-minute limit) and summarise it."""
    if runner is None:
        from .execution import HostBackend

        runner = HostBackend().run
    program = "python" if entry["manager"] == "pip" else "npm"
    done = runner(program, entry["argv"][1:], cwd=Path(entry["cwd"] or cwd), timeout=TIMEOUT_SECONDS,
                  env=env, host_command=list(entry["argv"]))
    output = "\n".join(part for part in (str(done.stdout or "").strip(), str(done.stderr or "").strip()) if part)
    installed = ""
    for line in output.splitlines():
        if line.startswith(("Successfully installed", "added ", "changed ", "up to date")):
            installed = line.strip()[:500]
    result = {"manager": entry["manager"], "packages": entry["packages"], "exit_code": done.exit_code,
              "timed_out": bool(done.timed_out), "installed": installed or None, "output": _tail(output),
              "where": ("the Hub's Python (shared by every agent)" if entry["manager"] == "pip"
                        else f"project folder {entry['folder']}")}
    if done.timed_out:
        result["error"] = f"The install did not finish within {TIMEOUT_SECONDS // 60} minutes and was stopped."
    elif done.exit_code != 0:
        result["error"] = f"{program} exited with code {done.exit_code}; see output."
    return result
