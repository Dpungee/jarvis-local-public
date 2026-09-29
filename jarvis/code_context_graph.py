"""Deterministic local code-context graphs for Python workspaces.

This is intentionally a dependency-free first slice of Jarvis's repository map.
It reads only Python syntax, never sends source text to a model, and returns
stable module/symbol/import relationships that a future context selector can use
to request a small relevant neighbourhood instead of a whole repository.
"""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


MAX_FILES = 2_000
MAX_FILE_BYTES = 1_000_000


class CodeContextGraphError(ValueError):
    """Raised when a graph request is outside its bounded local contract."""


def _relative_python_files(root: Path, *, max_files: int, max_file_bytes: int) -> list[Path]:
    if not root.is_dir():
        raise CodeContextGraphError("code graph root must be an existing directory")
    if max_files < 1 or max_file_bytes < 1:
        raise CodeContextGraphError("code graph bounds must be positive")
    resolved_root = root.resolve()
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        try:
            resolved = path.resolve()
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            # A link or path resolving outside the requested workspace is never
            # a graph input.  This is an index, not a way around path boundaries.
            continue
        if not resolved.is_file() or resolved.stat().st_size > max_file_bytes:
            continue
        files.append(resolved)
        if len(files) > max_files:
            raise CodeContextGraphError("code graph file limit exceeded")
    return files


def _module_name(relative: Path) -> str:
    parts = list(relative.with_suffix("").parts)
    if not parts:
        raise CodeContextGraphError("module path is empty")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "__root__"


def _safe_parse(path: Path) -> tuple[ast.Module | None, str | None]:
    try:
        return ast.parse(path.read_text(encoding="utf-8")), None
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        # Do not retain parser messages: they can embed source lines or absolute
        # paths.  A caller only needs to know that the module was unavailable.
        return None, type(exc).__name__


def _symbol_nodes(tree: ast.Module, module: str) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    def visit(body: Iterable[ast.stmt], parent: str, prefix: str) -> None:
        for statement in body:
            if not isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = statement.name
            qualified = f"{prefix}.{name}" if prefix else name
            node_id = f"symbol:{module}:{qualified}"
            nodes.append({
                "id": node_id,
                "kind": "class" if isinstance(statement, ast.ClassDef) else "function",
                "module": module,
                "qualified_name": qualified,
                "line": int(statement.lineno),
            })
            edges.append({"kind": "defines", "source": parent, "target": node_id})
            if isinstance(statement, ast.ClassDef):
                visit(statement.body, node_id, qualified)

    visit(tree.body, f"module:{module}", "")
    return nodes, edges


def _import_edges(tree: ast.Module, module: str, *, is_package: bool) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    for statement in ast.walk(tree):
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                edges.append({
                    "kind": "imports",
                    "source": f"module:{module}",
                    "target": f"module:{alias.name}",
                })
        elif isinstance(statement, ast.ImportFrom):
            level = int(statement.level or 0)
            if level:
                package = [] if module == "__root__" else module.split(".")
                if not is_package:
                    package = package[:-1]
                climb = level - 1
                if climb > len(package):
                    # Invalid/escaping relative imports stay out of the local
                    # graph.  They will fail at runtime instead of being treated
                    # as a dependency outside the selected workspace.
                    continue
                base = package[: len(package) - climb]
                if statement.module:
                    targets = [".".join([*base, statement.module])]
                else:
                    targets = [".".join([*base, alias.name]) for alias in statement.names]
            elif statement.module:
                targets = [statement.module]
            else:
                continue
            for target in targets:
                if target:
                    edges.append({
                        "kind": "imports",
                        "source": f"module:{module}",
                        "target": f"module:{target}",
                    })
    return edges


def build_python_code_graph(
    root: Path | str,
    *,
    max_files: int = MAX_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> dict[str, Any]:
    """Build a stable graph of local Python modules, symbols, and imports.

    Parse errors are represented as a closed error class rather than raw source
    text.  Results use paths relative to ``root`` and are deterministic for equal
    workspace contents.
    """

    base = Path(root).resolve()
    files = _relative_python_files(base, max_files=max_files, max_file_bytes=max_file_bytes)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    parse_errors: list[dict[str, str]] = []
    for path in files:
        relative = path.relative_to(base)
        module = _module_name(relative)
        module_id = f"module:{module}"
        nodes.append({
            "id": module_id,
            "kind": "module",
            "module": module,
            "path": relative.as_posix(),
        })
        tree, error_class = _safe_parse(path)
        if tree is None:
            parse_errors.append({"module": module, "error_class": str(error_class)})
            continue
        symbols, definition_edges = _symbol_nodes(tree, module)
        nodes.extend(symbols)
        edges.extend(definition_edges)
        edges.extend(_import_edges(tree, module, is_package=relative.name == "__init__.py"))
    return {
        "schema_version": 1,
        "modules": len(files),
        "nodes": sorted(nodes, key=lambda item: str(item["id"])),
        "edges": sorted(edges, key=lambda item: (item["kind"], item["source"], item["target"])),
        "parse_errors": sorted(parse_errors, key=lambda item: item["module"]),
    }


def impacted_modules(graph: Mapping[str, Any], module: str, *, max_depth: int = 3) -> list[str]:
    """Return local modules that import ``module`` directly or transitively.

    Only exact local module IDs are returned.  External imports and raw source
    lines are excluded, making this suitable for a compact context-selection
    preview.
    """

    if not isinstance(module, str) or not module or "/" in module or "\\" in module:
        raise CodeContextGraphError("module name is invalid")
    if max_depth < 0:
        raise CodeContextGraphError("max depth must be non-negative")
    module_ids = {
        str(node.get("id"))
        for node in graph.get("nodes", [])
        if isinstance(node, Mapping) and node.get("kind") == "module"
    }
    target = f"module:{module}"
    if target not in module_ids:
        return []
    reverse: dict[str, set[str]] = defaultdict(set)
    for edge in graph.get("edges", []):
        if not isinstance(edge, Mapping) or edge.get("kind") != "imports":
            continue
        source = edge.get("source")
        imported = edge.get("target")
        if isinstance(source, str) and isinstance(imported, str):
            reverse[imported].add(source)
    found: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(target, 0)])
    visited = {target}
    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for dependent in sorted(reverse.get(current, ())):
            if dependent in visited:
                continue
            visited.add(dependent)
            if dependent in module_ids:
                found.add(dependent.removeprefix("module:"))
                queue.append((dependent, depth + 1))
    return sorted(found)


__all__ = [
    "CodeContextGraphError",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "build_python_code_graph",
    "impacted_modules",
]
