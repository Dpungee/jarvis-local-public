"""Current ToolBox methods, mechanically extracted by domain.

Shared globals are resolved through jarvis.tools to preserve runtime patches.
"""
from __future__ import annotations

from .network_inventory import DEFAULT_SCAN_HOSTS


def _tools():
    from . import tools

    return tools


class DesktopSystemToolsMixin:
    def _computer_root(self) -> _tools().Path:
        if getattr(self.config, "computer_access", "disabled") != "trusted-desktop":
            raise PermissionError("Trusted desktop access is disabled")
        return _tools().Path(getattr(self.config, "computer_root", None) or _tools().Path.home()).resolve()

    def computer_list_files(self, path: str = ".", recursive: bool = False) -> list[str]:
        root = self._computer_root()
        target = _tools().resolve_computer_path(root, path)
        if not target.is_dir():
            raise NotADirectoryError(path)
        iterator = target.rglob("*") if recursive else target.glob("*")
        results: list[str] = []
        for item in iterator:
            try:
                safe = _tools().resolve_computer_path(root, item)
                results.append(str(safe))
            except (OSError, PermissionError):
                continue
            if len(results) >= 1000:
                break
        return results

    def computer_read_file(self, path: str, start_line: int = 1, end_line: int = 2000) -> dict[str, _tools().Any]:
        target = _tools().resolve_computer_path(self._computer_root(), path)
        details = target.stat()
        if not target.is_file():
            raise FileNotFoundError(path)
        if details.st_nlink > 1 or details.st_size > _tools().MAX_FILE_BYTES:
            raise ValueError("Computer file is linked or exceeds the 2 MB read limit")
        raw = target.read_bytes()
        text, encoding = _tools()._decode_text(raw)
        lines = text.splitlines()
        start = max(1, int(start_line))
        end = min(len(lines), max(start, int(end_line)))
        return {
            "path": str(target),
            "content": "\n".join(f"{index}: {lines[index - 1]}" for index in range(start, end + 1)),
            "sha256": _tools().hashlib.sha256(raw).hexdigest(),
            "encoding": encoding,
            "start_line": start,
            "end_line": end,
            "total_lines": len(lines),
            "truncated": start > 1 or end < len(lines),
        }

    def computer_write_file(self, path: str, content: str, expected_sha256: str | None = None) -> dict[str, _tools().Any]:
        if self.config.autonomy == "readonly":
            raise PermissionError("Computer writes are disabled in readonly mode")
        if len(content.encode("utf-8")) > _tools().MAX_FILE_BYTES:
            raise ValueError("Computer file content exceeds the 2 MB write limit")
        root = self._computer_root()
        target = _tools().resolve_computer_path(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = _tools().resolve_computer_path(root, path)
        encoding, newline, backup = "utf-8", "\n", None
        if target.exists():
            details = target.stat()
            if not target.is_file() or details.st_nlink > 1 or details.st_size > _tools().MAX_FILE_BYTES:
                raise ValueError("Existing computer target is not a safe editable text file")
            original = target.read_bytes()
            actual_hash = _tools().hashlib.sha256(original).hexdigest()
            if expected_sha256 is None or expected_sha256.casefold() != actual_hash:
                raise RuntimeError("Existing computer files require a matching fresh SHA-256 read")
            original_text, encoding = _tools()._decode_text(original)
            newline = _tools()._dominant_newline(original_text)
            backup_path = target.with_name(f".{target.name}.jarvis-backup")
            _tools()._atomic_write_bytes(backup_path, original)
            backup = str(backup_path)
        elif expected_sha256 is not None:
            raise RuntimeError("Expected an existing computer file, but the target does not exist")
        encoded = _tools()._encode_text(_tools()._with_newline_style(content, newline), encoding)
        _tools()._atomic_write_bytes(target, encoded)
        verified_target = _tools().resolve_computer_path(root, path)
        verified = verified_target.read_bytes()
        if verified != encoded:
            raise RuntimeError("Computer file readback did not match the approved write")
        return {
            "path": str(verified_target), "characters": len(content),
            "sha256": _tools().hashlib.sha256(verified).hexdigest(), "backup": backup,
            "verified_readback": True,
        }

    def computer_search_files(self, pattern: str, path: str = ".") -> list[str]:
        if not pattern or len(pattern) > 500:
            raise ValueError("Search text must contain 1-500 characters")
        root = self._computer_root()
        target = _tools().resolve_computer_path(root, path)
        candidates = [target] if target.is_file() else target.rglob("*")
        needle = pattern.casefold()
        matches: list[str] = []
        for candidate in candidates:
            try:
                file = _tools().resolve_computer_path(root, candidate)
                details = file.stat()
                if not file.is_file() or details.st_size > 1_000_000 or details.st_nlink > 1:
                    continue
                text, _encoding = _tools()._decode_text(file.read_bytes())
                for number, line in enumerate(text.splitlines(), 1):
                    if needle in line.casefold():
                        matches.append(f"{file}:{number}: {line[:500]}")
                        if len(matches) >= 200:
                            return matches
            except (OSError, PermissionError, UnicodeError):
                continue
        return matches

    def computer_storage_report(self, path: str = ".", limit: int = 50) -> dict[str, _tools().Any]:
        """Inspect bounded file metadata for cleanup advice without reading contents."""
        root = self._computer_root()
        target = _tools().resolve_computer_path(root, path)
        if not target.exists():
            raise FileNotFoundError(path)
        bound = max(1, min(int(limit), 100))
        largest: list[tuple[int, int, dict[str, _tools().Any]]] = []
        directory_bytes: dict[str, int] = {}
        scanned_entries = 0
        scanned_files = 0
        scanned_bytes = 0
        truncated = False
        truncation_reason: str | None = None
        scan_started = _tools().time.monotonic()
        scan_deadline = scan_started + _tools().MAX_STORAGE_SCAN_SECONDS

        def safe_candidates() -> _tools().Iterator[_tools().Path]:
            nonlocal scanned_entries, truncated, truncation_reason
            if target.is_file():
                scanned_entries = 1
                yield target
                return
            for current, directories, filenames in _tools().os.walk(
                target, topdown=True, followlinks=False
            ):
                retained: list[str] = []
                for directory in directories:
                    if _tools().time.monotonic() >= scan_deadline:
                        truncated = True
                        truncation_reason = "time_limit"
                        directories[:] = []
                        return
                    if scanned_entries >= 100_000:
                        truncated = True
                        truncation_reason = "entry_limit"
                        directories[:] = []
                        return
                    scanned_entries += 1
                    raw_directory = _tools().Path(current) / directory
                    try:
                        _tools().resolve_computer_path(root, raw_directory)
                        details = _tools().os.lstat(raw_directory)
                        attributes = getattr(details, "st_file_attributes", 0)
                        if _tools().stat.S_ISLNK(details.st_mode) or attributes & getattr(
                            _tools().stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
                        ):
                            continue
                    except (OSError, PermissionError, ValueError):
                        continue
                    retained.append(directory)
                directories[:] = retained
                for filename in filenames:
                    if _tools().time.monotonic() >= scan_deadline:
                        truncated = True
                        truncation_reason = "time_limit"
                        directories[:] = []
                        return
                    if scanned_entries >= 100_000:
                        truncated = True
                        truncation_reason = "entry_limit"
                        directories[:] = []
                        return
                    scanned_entries += 1
                    yield _tools().Path(current) / filename

        for candidate in safe_candidates():
            try:
                file = _tools().Path(candidate)
                details = _tools().os.lstat(file)
                attributes = getattr(details, "st_file_attributes", 0)
                if (
                    not _tools().stat.S_ISREG(details.st_mode)
                    or _tools().stat.S_ISLNK(details.st_mode)
                    or attributes & getattr(_tools().stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    or details.st_nlink > 1
                ):
                    continue
                relative = file.relative_to(target) if target.is_dir() else _tools().Path(file.name)
                size = max(0, int(details.st_size))
                scanned_files += 1
                scanned_bytes += size
                top_level = relative.parts[0] if relative.parts else file.name
                directory_bytes[top_level] = directory_bytes.get(top_level, 0) + size
                record = {
                    "path": str(file),
                    "size_bytes": size,
                    "modified_at": float(details.st_mtime),
                }
                entry = (size, scanned_files, record)
                if len(largest) < bound:
                    _tools().heapq.heappush(largest, entry)
                elif size > largest[0][0]:
                    _tools().heapq.heapreplace(largest, entry)
            except (OSError, PermissionError, ValueError):
                continue
        largest_files = [
            item[2] for item in sorted(largest, key=lambda item: item[0], reverse=True)
        ]
        folders = [
            {
                "path": str(target if target.is_file() else target / name),
                "size_bytes": size,
            }
            for name, size in directory_bytes.items()
        ]
        folders.sort(key=lambda item: int(item["size_bytes"]), reverse=True)
        return {
            "root": str(target),
            "scanned_entries": scanned_entries,
            "scanned_files": scanned_files,
            "scanned_bytes": scanned_bytes,
            "truncated": truncated,
            "truncation_reason": truncation_reason,
            "scan_time_ms": round((_tools().time.monotonic() - scan_started) * 1000, 1),
            "largest_files": largest_files,
            "largest_top_level_entries": folders[:bound],
            "content_read": False,
            "files_deleted": 0,
        }

    def system_snapshot(self) -> dict[str, _tools().Any]:
        return _tools().system_snapshot(self._computer_root())

    def network_inventory(
        self,
        action: str = "status",
        max_hosts: int = DEFAULT_SCAN_HOSTS,
        include_offline: bool = True,
        scope_id: str | None = None,
        include_identifiers: bool = False,
        device_id: str | None = None,
        event_limit: int = 100,
        label: str | None = None,
        trust_state: str | None = None,
        device_type: str | None = None,
    ) -> dict[str, _tools().Any]:
        store = self.network_inventory_store
        if store is None:
            raise PermissionError(
                "Private-LAN inventory is disabled; set JARVIS_NETWORK_ACCESS=private-lan"
            )
        normalized_action = str(action or "status").strip().casefold()
        clean_scope_id = str(scope_id or "").strip() or None
        clean_device_id = str(device_id or "").strip() or None
        if clean_scope_id is not None and len(clean_scope_id) > 200:
            raise ValueError("Network scope_id is too long")
        if clean_device_id is not None and len(clean_device_id) > 200:
            raise ValueError("Network device_id is too long")
        bounded_event_limit = int(event_limit)
        if isinstance(event_limit, bool) or not 1 <= bounded_event_limit <= 500:
            raise ValueError("Network event_limit must be between 1 and 500")
        expose_identifiers = include_identifiers is True

        if normalized_action == "status":
            result = store.status(include_identifiers=expose_identifiers)
        elif normalized_action == "security":
            result = store.security_assessment(scope_id=clean_scope_id)
        elif normalized_action == "security_history":
            result = store.security_assessment_history(
                limit=bounded_event_limit,
                scope_id=clean_scope_id,
            )
        elif normalized_action == "scan":
            result = store.scan(
                max_hosts=int(max_hosts),
                include_offline=bool(include_offline),
                scope_id=clean_scope_id,
                include_identifiers=expose_identifiers,
            )
        elif normalized_action == "list":
            result = store.list_devices(
                include_offline=bool(include_offline),
                include_identifiers=expose_identifiers,
            )
        elif normalized_action == "detail":
            if clean_device_id is None:
                raise ValueError("Network detail requires device_id")
            result = store.device_detail(
                clean_device_id,
                event_limit=bounded_event_limit,
                include_identifiers=expose_identifiers,
            )
        elif normalized_action == "history":
            result = store.events(
                limit=bounded_event_limit,
                device_id=clean_device_id,
                include_identifiers=expose_identifiers,
            )
        elif normalized_action == "profile":
            if self.config.autonomy == "readonly":
                raise PermissionError("Network profile updates are disabled in readonly mode")
            if clean_device_id is None:
                raise ValueError("Network profile requires device_id")
            if label is None and trust_state is None and device_type is None:
                raise ValueError(
                    "Network profile requires label, trust_state, or device_type"
                )
            result = store.set_profile(
                clean_device_id,
                label=label,
                trust_state=trust_state,
                device_type=device_type,
            )
            if isinstance(result, dict):
                result = {
                    **result,
                    "operator_metadata_only": True,
                    "authority_added": False,
                    "access_granted": False,
                    "control_enabled": False,
                }
        else:
            raise ValueError(
                "Network inventory action must be status, security, security_history, "
                "list, scan, detail, history, or profile"
            )
        if not isinstance(result, dict):
            raise TypeError("Network inventory provider returned an invalid result")
        if (
            normalized_action in {"status", "list", "scan"}
            and self.home_assistant is not None
            and getattr(
                self.config, "home_assistant_network_access", "disabled"
            ) == "netgear-readonly"
        ):
            try:
                result["router_telemetry"] = self.home_assistant.network_telemetry()
            except Exception as exc:
                result["router_telemetry"] = {
                    "provider": "home_assistant_netgear",
                    "available": False,
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "credentials_exposed": False,
                }
        return result if expose_identifiers else _tools()._without_network_identifiers(result)

    def bluetooth_inventory(
        self,
        action: str = "status",
        include_os_metadata: bool = False,
        device_id: str | None = None,
        event_limit: int = 100,
        label: str | None = None,
        trust_state: str | None = None,
        device_type: str | None = None,
    ) -> dict[str, _tools().Any]:
        store = self.bluetooth_inventory_store
        if store is None:
            if self.bluetooth_inventory_error:
                raise _tools().BluetoothInventoryError(self.bluetooth_inventory_error)
            raise PermissionError(
                "Paired Bluetooth inventory is disabled; set "
                "JARVIS_BLUETOOTH_ACCESS=paired-readonly"
            )
        normalized_action = str(action or "status").strip().casefold()
        clean_device_id = str(device_id or "").strip() or None
        if clean_device_id is not None and len(clean_device_id) > 200:
            raise ValueError("Bluetooth device_id is too long")
        bounded_event_limit = int(event_limit)
        if isinstance(event_limit, bool) or not 1 <= bounded_event_limit <= 500:
            raise ValueError("Bluetooth event_limit must be between 1 and 500")
        expose_metadata = include_os_metadata is True

        if normalized_action == "status":
            result = store.status(include_os_metadata=expose_metadata)
        elif normalized_action == "check":
            result = store.check(include_os_metadata=expose_metadata)
        elif normalized_action == "list":
            result = store.list_devices(include_os_metadata=expose_metadata)
        elif normalized_action == "detail":
            if clean_device_id is None:
                raise ValueError("Bluetooth detail requires device_id")
            result = store.device_detail(
                clean_device_id,
                event_limit=bounded_event_limit,
                include_os_metadata=expose_metadata,
            )
        elif normalized_action == "history":
            result = store.events(
                limit=bounded_event_limit,
                device_id=clean_device_id,
            )
        elif normalized_action == "profile":
            if self.config.autonomy == "readonly":
                raise PermissionError(
                    "Bluetooth profile updates are disabled in readonly mode"
                )
            if clean_device_id is None:
                raise ValueError("Bluetooth profile requires device_id")
            if label is None and trust_state is None and device_type is None:
                raise ValueError(
                    "Bluetooth profile requires label, trust_state, or device_type"
                )
            result = store.set_profile(
                clean_device_id,
                label=label,
                trust_state=trust_state,
                device_type=device_type,
            )
            result = {
                **result,
                "operator_metadata_only": True,
                "authority_added": False,
                "access_granted": False,
                "control_enabled": False,
            }
        else:
            raise ValueError(
                "Bluetooth inventory action must be status, check, list, detail, "
                "history, or profile"
            )
        if not isinstance(result, dict):
            raise TypeError("Bluetooth inventory provider returned an invalid result")
        return result

    def home_device_status(self) -> dict[str, _tools().Any]:
        if self.home_assistant is None:
            raise PermissionError("Paired Home Assistant access is disabled")
        return self.home_assistant.status()

    def home_device_control(
        self,
        device: str,
        action: str,
        app: str | None = None,
    ) -> dict[str, _tools().Any]:
        if self.home_assistant is None:
            raise PermissionError("Paired Home Assistant access is disabled")
        approved = self._approved_arguments_for("home_device_control")
        if not approved:
            raise PermissionError("An exact approved home-device action is required")
        resolved_entity = str(approved.get("resolved_entity") or "")
        resolved_action = str(approved.get("resolved_action") or "")
        resolved_app = approved.get("resolved_app")
        if resolved_action != str(action).strip().casefold():
            raise PermissionError("Home-device action differs from the approved action")
        return self.home_assistant.control(
            entity_id=resolved_entity,
            action=resolved_action,
            app=str(resolved_app) if resolved_app is not None else None,
        )

    def windows_list_apps(self, query: str = "", limit: int = 50) -> dict[str, _tools().Any]:
        self._computer_root()
        return self.windows_apps.list_apps(query, limit)

    def windows_open_apps(self, limit: int = 50) -> dict[str, _tools().Any]:
        self._computer_root()
        return _tools().open_windows_applications(limit)

    def windows_launch_app(self, application: str) -> dict[str, _tools().Any]:
        if self.config.execution_mode != "trusted-host":
            raise PermissionError("Host application execution is disabled")
        approved = self._approved_arguments_for("windows_launch_app")
        if not approved:
            raise PermissionError("An exact approved application target is required")
        return self.windows_apps.launch_app(application, approved=approved)

    def windows_app_diagnose(
        self,
        application: str,
        symptom: str = "auto",
    ) -> dict[str, _tools().Any]:
        self._computer_root()
        try:
            result = self.windows_app_repair.diagnose(application, symptom)
        except PermissionError:
            raise
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                "The profiled application or repair target is unavailable"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                "The profiled application changed or became unavailable during diagnosis"
            ) from exc
        return {
            key: value for key, value in result.items()
            if not str(key).startswith("_")
        }

    def windows_app_repair_apply(
        self,
        application: str,
        plan_id: str,
        symptom: str = "blank_or_unrendered",
    ) -> dict[str, _tools().Any]:
        if self.config.execution_mode != "trusted-host":
            raise PermissionError("Host application execution is disabled")
        approved = self._approved_arguments_for("windows_app_repair")
        repair_plan = approved.get("repair_plan") if approved else None
        if not isinstance(repair_plan, dict):
            raise PermissionError("An exact approved application repair plan is required")
        try:
            return self.windows_app_repair.apply(
                application,
                plan_id,
                symptom=symptom,
                approved=repair_plan,
            )
        except PermissionError:
            raise
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                "The approved application repair target is no longer available"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                "The approved application repair could not complete safely"
            ) from exc

    def windows_open_url(self, url: str) -> dict[str, _tools().Any]:
        if self.config.execution_mode != "trusted-host":
            raise PermissionError("Host browser execution is disabled")
        safe_url = _tools()._public_url(url)
        approved = self._approved_arguments_for("windows_open_url")
        if not approved:
            raise PermissionError("An exact approved public browser URL is required")
        return self.windows_apps.open_url(safe_url, approved=approved)

    def desktop_active_window(self) -> dict[str, _tools().Any]:
        approved = self._approved_arguments_for("desktop_active_window")
        if not approved or not isinstance(approved.get("foreground"), dict):
            raise PermissionError("An exact approved foreground-window snapshot is required")
        return dict(approved["foreground"])

    def desktop_interact(
        self,
        actions: list[dict[str, _tools().Any]],
        expected_context_sha256: str | None = None,
    ) -> dict[str, _tools().Any]:
        if self.config.execution_mode != "trusted-host":
            raise PermissionError("Host desktop control is disabled")
        approved = self._approved_arguments_for("desktop_interact")
        if not approved:
            raise PermissionError("An exact approved desktop action batch is required")
        approved_expected = str(approved.get("expected_context_sha256") or "")
        if expected_context_sha256 is not None and (
            expected_context_sha256.casefold() != approved_expected
        ):
            raise PermissionError("Desktop context differs from the approved target")
        return self.desktop.interact(
            expected_context_sha256=approved_expected,
            actions=actions,
        )

    def photoshop_remove_background(
        self,
        input_path: str,
        output_path: str,
        overwrite: bool = False,
    ) -> dict[str, _tools().Any]:
        if self.config.execution_mode != "trusted-host":
            raise PermissionError("Host application execution is disabled")
        approved = self._approved_arguments_for("photoshop_remove_background")
        if not approved:
            raise PermissionError("Exact approved Photoshop source and output targets are required")
        return self.windows_apps.remove_photoshop_background(
            input_path,
            output_path,
            overwrite=overwrite,
            approved=approved,
        )

    def _launch_artifact_snapshot(self, path: str) -> dict[str, _tools().Any]:
        target = _tools()._safe_target(self.config.workspace, path)
        if not _tools().os.path.lexists(target):
            raise FileNotFoundError(path)
        before = _tools().os.lstat(target)
        attributes = getattr(before, "st_file_attributes", 0)
        if (
            _tools().stat.S_ISLNK(before.st_mode)
            or attributes & getattr(_tools().stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or not _tools().stat.S_ISREG(before.st_mode)
            or before.st_nlink > 1
        ):
            raise PermissionError("Launch artifacts must be ordinary, non-linked files")
        if before.st_size > _tools().MAX_LAUNCH_ARTIFACT_BYTES:
            raise ValueError("Launch artifact exceeds the 512 MiB limit")
        digest = _tools().hashlib.sha256()
        read_bytes = 0
        with target.open("rb") as stream:
            opened = _tools().os.fstat(stream.fileno())
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_nlink,
            ) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_nlink,
            ):
                raise PermissionError("Launch artifact changed while it was opened")
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                read_bytes += len(chunk)
                digest.update(chunk)
        after = _tools().os.lstat(target)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_nlink,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_nlink,
        ) or read_bytes != before.st_size:
            raise PermissionError("Launch artifact changed while its identity was verified")
        return {
            "path": str(target.relative_to(self.config.workspace)),
            "resolved_path": str(target),
            "bytes": read_bytes,
            "sha256": digest.hexdigest(),
            "suffix": target.suffix.casefold(),
        }

    def launch_artifact(
        self,
        path: str,
        arguments: list[str] | None = None,
        expected_sha256: str | None = None,
    ) -> dict[str, _tools().Any]:
        if self.config.execution_mode != "trusted-host":
            raise PermissionError("Host process execution is disabled")
        if self.config.autonomy == "readonly":
            raise PermissionError("Application launches are disabled in readonly mode")
        arguments = list(arguments or [])
        if any(not isinstance(item, str) or any(char in item for char in "\x00\r\n") for item in arguments):
            raise ValueError("Launch arguments must be plain strings without control characters")
        if sum(map(len, arguments)) > 8000:
            raise ValueError("Launch argument limit exceeded")
        snapshot = self._launch_artifact_snapshot(path)
        target = _tools().Path(snapshot["resolved_path"])
        suffix = str(snapshot["suffix"])
        if expected_sha256 is not None and (
            not _tools().re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256)
            or expected_sha256.casefold() != snapshot["sha256"]
        ):
            raise PermissionError("Launch artifact differs from the expected SHA-256")
        default_application_suffixes = {
            ".html", ".pptx", ".docx", ".xlsx", ".pdf", ".txt", ".md", ".csv",
        }
        if suffix not in {".exe", ".py", ".pyw", *default_application_suffixes}:
            raise PermissionError(
                "Only bounded executable, web, Office, PDF, and text workspace artifacts may be opened"
            )
        if suffix in default_application_suffixes:
            if arguments:
                raise ValueError("Documents and browser artifacts do not accept launch arguments")
            if _tools().os.name != "nt":
                raise RuntimeError("Default-application launch is available only on Windows")
            office_executable_names = {
                ".pptx": "powerpnt.exe",
                ".docx": "winword.exe",
                ".xlsx": "excel.exe",
            }
            expected_executable = office_executable_names.get(suffix)
            if expected_executable is not None:
                office_app = next((
                    app for app in self.windows_apps.catalog()
                    if app.executable is not None
                    and app.executable.name.casefold() == expected_executable
                ), None)
                if office_app is not None and office_app.executable is not None:
                    flags = _tools().subprocess.CREATE_NEW_PROCESS_GROUP | _tools().subprocess.DETACHED_PROCESS
                    process = _tools().subprocess.Popen(
                        [str(office_app.executable), str(target)],
                        cwd=target.parent,
                        stdin=_tools().subprocess.DEVNULL,
                        stdout=_tools().subprocess.DEVNULL,
                        stderr=_tools().subprocess.DEVNULL,
                        env=_tools()._minimal_environment(self.config.data_dir),
                        creationflags=flags,
                        close_fds=True,
                    )
                    return {
                        "path": str(target.relative_to(self.config.workspace)),
                        "bytes": snapshot["bytes"],
                        "sha256": snapshot["sha256"],
                        "launched": True,
                        "pid": process.pid,
                        "viewer": office_app.name,
                    }
            if not hasattr(_tools().os, "startfile"):
                raise RuntimeError("Default-application launch is unavailable on Windows")
            _tools().os.startfile(str(target))
            return {
                "path": str(target.relative_to(self.config.workspace)),
                "bytes": snapshot["bytes"],
                "sha256": snapshot["sha256"],
                "launched": True,
                "pid": None,
                "viewer": "default_application",
            }
        executable = target
        command = [str(target), *arguments]
        if suffix in {".py", ".pyw"}:
            executable = _tools().Path(_tools().sys.executable).with_name("pythonw.exe") if _tools().os.name == "nt" else _tools().Path(_tools().sys.executable)
            if not executable.is_file():
                executable = _tools().Path(_tools().sys.executable)
            command = [str(executable), str(target), *arguments]
        flags = 0
        if _tools().os.name == "nt":
            flags = _tools().subprocess.CREATE_NEW_PROCESS_GROUP | _tools().subprocess.DETACHED_PROCESS
        process = _tools().subprocess.Popen(
            command,
            cwd=target.parent,
            stdin=_tools().subprocess.DEVNULL,
            stdout=_tools().subprocess.DEVNULL,
            stderr=_tools().subprocess.DEVNULL,
            env=_tools()._minimal_environment(self.config.data_dir),
            creationflags=flags,
            close_fds=True,
        )
        return {
            "path": str(target.relative_to(self.config.workspace)),
            "bytes": snapshot["bytes"],
            "sha256": snapshot["sha256"],
            "launched": True,
            "pid": process.pid,
        }
