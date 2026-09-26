"""Current ToolBox methods, mechanically extracted by domain.

Shared globals are resolved through jarvis.tools to preserve runtime patches.
"""
from __future__ import annotations


def _tools():
    from . import tools

    return tools


class WorkspaceFileToolsMixin:
    def list_files(self, path: str = ".", recursive: bool = False) -> list[str]:
        target = _tools()._safe_target(self.config.workspace, path)
        if not target.is_dir():
            raise NotADirectoryError(path)
        iterator = target.rglob("*") if recursive else target.glob("*")
        results: list[str] = []
        for item in iterator:
            relative = item.relative_to(self.config.workspace)
            if any(part in {".git", ".jarvis-runtime"} for part in relative.parts):
                continue
            try:
                _tools()._safe_target(self.config.workspace, item)
            except (OSError, PermissionError):
                continue
            results.append(str(relative))
            if len(results) >= 1000:
                break
        return results

    def read_file(self, path: str, start_line: int = 1, end_line: int = 2000) -> dict[str, _tools().Any]:
        target = _tools()._safe_target(self.config.workspace, path)
        stat_result = target.stat()
        if not target.is_file():
            raise FileNotFoundError(path)
        if stat_result.st_nlink > 1:
            raise PermissionError("Hard-linked files are blocked")
        if stat_result.st_size > _tools().MAX_FILE_BYTES:
            raise ValueError("File is larger than the 2 MB read limit")
        raw = target.read_bytes()
        text, encoding = _tools()._decode_text(raw)
        lines = text.splitlines()
        start = max(1, int(start_line))
        end = min(len(lines), max(start, int(end_line)))
        return {
            "path": str(target.relative_to(self.config.workspace)),
            "content": "\n".join(f"{index}: {lines[index - 1]}" for index in range(start, end + 1)),
            "sha256": _tools().hashlib.sha256(raw).hexdigest(),
            "encoding": encoding,
            "start_line": start,
            "end_line": end,
            "total_lines": len(lines),
            "truncated": start > 1 or end < len(lines),
        }

    def read_files(
        self,
        paths: list[str],
        start_line: int = 1,
        end_line: int = 2000,
    ) -> dict[str, _tools().Any]:
        if not paths:
            raise ValueError("paths must contain at least one workspace file")
        if len(paths) > _tools().MAX_BATCH_READ_FILES:
            raise ValueError(f"paths may contain at most {_tools().MAX_BATCH_READ_FILES} files")
        normalized = [str(path).replace("\\", "/").casefold() for path in paths]
        if len(set(normalized)) != len(normalized):
            raise ValueError("paths must not contain duplicate files")
        if end_line < start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        # Validate every boundary before starting work, then preserve caller order.
        for path in paths:
            _tools()._safe_target(self.config.workspace, path)
        workers = min(8, len(paths))
        with _tools().ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(
                lambda path: self.read_file(path, start_line, end_line),
                paths,
            ))
        per_file_limit = max(500, _tools().MAX_BATCH_READ_CHARACTERS // len(results))
        clipped = 0
        for result in results:
            content = str(result.get("content", ""))
            if len(content) > per_file_limit:
                result["content"] = content[:per_file_limit]
                result["truncated"] = True
                result["batch_content_truncated"] = True
                clipped += 1
        return {
            "files": results,
            "count": len(results),
            "content_character_limit": _tools().MAX_BATCH_READ_CHARACTERS,
            "files_content_truncated": clipped,
        }

    def write_file(
        self,
        path: str,
        content: str,
        expected_sha256: str | None = None,
    ) -> dict[str, _tools().Any]:
        if self.config.autonomy == "readonly":
            raise PermissionError("File writes are disabled in readonly mode")
        if len(content.encode("utf-8")) > _tools().MAX_FILE_BYTES:
            raise ValueError("File content exceeds the 2 MB write limit")
        target = _tools()._safe_target(self.config.workspace, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = _tools()._safe_target(self.config.workspace, path)
        encoding = "utf-8"
        newline = "\n"
        backup: str | None = None
        if target.exists():
            stat_result = target.stat()
            if not target.is_file():
                raise IsADirectoryError(path)
            if stat_result.st_nlink > 1:
                raise PermissionError("Hard-linked files are blocked")
            if stat_result.st_size > _tools().MAX_FILE_BYTES:
                raise ValueError("Existing file is larger than the 2 MB edit limit")
            original = target.read_bytes()
            actual_hash = _tools().hashlib.sha256(original).hexdigest()
            if expected_sha256 is None:
                raise RuntimeError("Existing files require expected_sha256 from a fresh read_file result")
            if expected_sha256.casefold() != actual_hash:
                raise RuntimeError("File changed since it was inspected; read it again before writing")
            original_text, encoding = _tools()._decode_text(original)
            newline = _tools()._dominant_newline(original_text)
            backup_path = target.with_name(f".{target.name}.jarvis-backup")
            _tools()._atomic_write_bytes(backup_path, original)
            backup = str(backup_path.relative_to(self.config.workspace))
        elif expected_sha256 is not None:
            raise RuntimeError("Expected an existing file, but the target does not exist")
        rendered = _tools()._with_newline_style(content, newline)
        encoded = _tools()._encode_text(rendered, encoding)
        if len(encoded) > _tools().MAX_FILE_BYTES:
            raise ValueError("Encoded file exceeds the 2 MB write limit")
        _tools()._atomic_write_bytes(target, encoded)
        return {
            "path": str(target.relative_to(self.config.workspace)),
            "characters": len(content),
            "sha256": _tools().hashlib.sha256(encoded).hexdigest(),
            "backup": backup,
            "encoding": encoding,
            "newline": "CRLF" if newline == "\r\n" else "CR" if newline == "\r" else "LF",
        }

    def edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        expected_sha256: str,
        replace_all: bool = False,
    ) -> dict[str, _tools().Any]:
        """Atomically apply a bounded exact-text edit with optimistic concurrency."""
        if self.config.autonomy == "readonly":
            raise PermissionError("File writes are disabled in readonly mode")
        if not old_text:
            raise ValueError("old_text must not be empty")
        if len(old_text.encode("utf-8")) > _tools().MAX_FILE_BYTES:
            raise ValueError("old_text exceeds the 2 MB edit limit")
        if len(new_text.encode("utf-8")) > _tools().MAX_FILE_BYTES:
            raise ValueError("new_text exceeds the 2 MB edit limit")

        target = _tools()._safe_target(self.config.workspace, path)
        details = target.stat()
        if not target.is_file():
            raise FileNotFoundError(path)
        if details.st_nlink > 1:
            raise PermissionError("Hard-linked files are blocked")
        if details.st_size > _tools().MAX_FILE_BYTES:
            raise ValueError("Existing file is larger than the 2 MB edit limit")

        original = target.read_bytes()
        actual_hash = _tools().hashlib.sha256(original).hexdigest()
        if expected_sha256.casefold() != actual_hash:
            raise RuntimeError("File changed since it was inspected; read it again before editing")
        original_text, encoding = _tools()._decode_text(original)
        newline = _tools()._dominant_newline(original_text)
        normalized = original_text.replace("\r\n", "\n").replace("\r", "\n")
        old_normalized = old_text.replace("\r\n", "\n").replace("\r", "\n")
        new_normalized = new_text.replace("\r\n", "\n").replace("\r", "\n")
        occurrences = normalized.count(old_normalized)
        if occurrences == 0:
            raise ValueError("old_text was not found; read the current file and use an exact fragment")
        if occurrences > 1 and not replace_all:
            raise ValueError("old_text is ambiguous; provide a larger unique fragment or set replace_all")

        replacements = occurrences if replace_all else 1
        edited = normalized.replace(
            old_normalized,
            new_normalized,
            -1 if replace_all else 1,
        )
        rendered = _tools()._with_newline_style(edited, newline)
        encoded = _tools()._encode_text(rendered, encoding)
        if len(encoded) > _tools().MAX_FILE_BYTES:
            raise ValueError("Edited file exceeds the 2 MB write limit")

        backup_path = target.with_name(f".{target.name}.jarvis-backup")
        _tools()._atomic_write_bytes(backup_path, original)
        _tools()._atomic_write_bytes(target, encoded)
        return {
            "path": str(target.relative_to(self.config.workspace)),
            "replacements": replacements,
            "characters": len(edited),
            "sha256": _tools().hashlib.sha256(encoded).hexdigest(),
            "backup": str(backup_path.relative_to(self.config.workspace)),
            "encoding": encoding,
            "newline": "CRLF" if newline == "\r\n" else "CR" if newline == "\r" else "LF",
        }

    def make_directory(self, path: str) -> dict[str, _tools().Any]:
        if self.config.autonomy == "readonly":
            raise PermissionError("Directory creation is disabled in readonly mode")
        target = _tools()._mutable_workspace_target(self.config.workspace, path)
        if target.exists() and not target.is_dir():
            raise FileExistsError(path)
        created = not target.exists()
        target.mkdir(parents=True, exist_ok=True)
        target = _tools()._mutable_workspace_target(self.config.workspace, path)
        return {
            "path": str(target.relative_to(self.config.workspace)),
            "created": created,
        }

    def copy_path(self, source: str, destination: str) -> dict[str, _tools().Any]:
        if self.config.autonomy == "readonly":
            raise PermissionError("Path copying is disabled in readonly mode")
        source_target = _tools()._safe_target(self.config.workspace, source)
        if source_target == self.config.workspace.resolve():
            raise PermissionError("The workspace root cannot be copied")
        destination_target = _tools()._mutable_workspace_target(self.config.workspace, destination)
        if destination_target.exists():
            raise FileExistsError("copy_path never overwrites an existing destination")
        if source_target.is_dir():
            try:
                destination_target.relative_to(source_target)
            except ValueError:
                pass
            else:
                raise ValueError("A directory cannot be copied inside itself")
        stats = _tools()._path_tree_stats(self.config.workspace, source_target)
        destination_target.parent.mkdir(parents=True, exist_ok=True)
        destination_target = _tools()._mutable_workspace_target(self.config.workspace, destination)
        if destination_target.exists():
            raise FileExistsError("copy_path never overwrites an existing destination")
        if source_target.is_dir():
            _tools().shutil.copytree(source_target, destination_target)
        else:
            _tools().shutil.copy2(source_target, destination_target)
        return {
            "source": str(source_target.relative_to(self.config.workspace)),
            "destination": str(destination_target.relative_to(self.config.workspace)),
            **stats,
        }

    def move_path(self, source: str, destination: str) -> dict[str, _tools().Any]:
        if self.config.autonomy == "readonly":
            raise PermissionError("Path moving is disabled in readonly mode")
        source_target = _tools()._mutable_workspace_target(self.config.workspace, source)
        destination_target = _tools()._mutable_workspace_target(self.config.workspace, destination)
        if destination_target.exists():
            raise FileExistsError("move_path never overwrites an existing destination")
        if source_target.is_dir():
            try:
                destination_target.relative_to(source_target)
            except ValueError:
                pass
            else:
                raise ValueError("A directory cannot be moved inside itself")
        stats = _tools()._path_tree_stats(
            self.config.workspace,
            source_target,
            protect_mutations=True,
        )
        destination_target.parent.mkdir(parents=True, exist_ok=True)
        destination_target = _tools()._mutable_workspace_target(self.config.workspace, destination)
        if destination_target.exists():
            raise FileExistsError("move_path never overwrites an existing destination")
        _tools().shutil.move(str(source_target), str(destination_target))
        return {
            "source": str(source_target.relative_to(self.config.workspace)),
            "destination": str(destination_target.relative_to(self.config.workspace)),
            **stats,
        }

    def trash_path(self, path: str) -> dict[str, _tools().Any]:
        if self.config.autonomy == "readonly":
            raise PermissionError("Path trashing is disabled in readonly mode")
        source_target = _tools()._mutable_workspace_target(self.config.workspace, path)
        stats = _tools()._path_tree_stats(
            self.config.workspace,
            source_target,
            protect_mutations=True,
        )
        trash_root = (self.config.data_dir.resolve() / "trash")
        try:
            trash_root.relative_to(source_target)
        except ValueError:
            pass
        else:
            raise PermissionError("The JARVIS data trash cannot be inside the trashed path")
        trash_id = (
            _tools().time.strftime("%Y%m%dT%H%M%SZ", _tools().time.gmtime())
            + "-"
            + _tools().uuid.uuid4().hex[:12]
        )
        entry = trash_root / trash_id
        relative = source_target.relative_to(self.config.workspace)
        destination = entry / "files" / relative
        destination.parent.mkdir(parents=True, exist_ok=False)
        manifest_path = entry / "manifest.json"
        manifest = {
            "trash_id": trash_id,
            "status": "pending",
            "original_workspace": str(self.config.workspace.resolve()),
            "original_path": str(relative),
            "trashed_at_utc": _tools().time.strftime("%Y-%m-%dT%H:%M:%SZ", _tools().time.gmtime()),
            **stats,
        }
        _tools()._atomic_write_bytes(
            manifest_path,
            _tools().json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"),
        )
        _tools().shutil.move(str(source_target), str(destination))
        manifest["status"] = "trashed"
        _tools()._atomic_write_bytes(
            manifest_path,
            _tools().json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"),
        )
        return {
            "trash_id": trash_id,
            "original_path": str(relative),
            "trash_path": str(destination.relative_to(self.config.data_dir.resolve())),
            "manifest": str(manifest_path.relative_to(self.config.data_dir.resolve())),
            "recoverable": True,
            **stats,
        }

    def search_files(self, pattern: str, path: str = ".") -> list[str]:
        if not pattern or len(pattern) > 500:
            raise ValueError("Search text must contain 1-500 characters")
        target = _tools()._safe_target(self.config.workspace, path)
        candidates = [target] if target.is_file() else target.rglob("*")
        needle = pattern.casefold()
        matches: list[str] = []
        for file in candidates:
            try:
                relative = file.relative_to(self.config.workspace)
                if any(part in {".git", ".jarvis-runtime"} for part in relative.parts):
                    continue
                file = _tools()._safe_target(self.config.workspace, file)
                stat_result = file.stat()
                if not file.is_file() or stat_result.st_size > 1_000_000 or stat_result.st_nlink > 1:
                    continue
                text, _encoding = _tools()._decode_text(file.read_bytes())
                for number, line in enumerate(text.splitlines(), 1):
                    if needle in line.casefold():
                        matches.append(f"{relative}:{number}: {line[:500]}")
                        if len(matches) >= 200:
                            return matches
            except (OSError, PermissionError, UnicodeError):
                continue
        return matches
