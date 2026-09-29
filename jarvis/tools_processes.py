"""Current ToolBox methods, mechanically extracted by domain.

Shared globals are resolved through jarvis.tools to preserve runtime patches.
"""
from __future__ import annotations


def _tools():
    from . import tools

    return tools


class ProcessToolsMixin:
    def run_process(
        self,
        program: str,
        arguments: list[str] | None = None,
        cwd: str = ".",
        timeout: int | None = None,
    ) -> dict[str, _tools().Any]:
        if self.config.execution_mode != "trusted-host":
            raise PermissionError("Process execution is disabled")
        if self.config.autonomy == "readonly":
            raise PermissionError("Processes are disabled in readonly mode")
        arguments = list(arguments or [])
        allowed, reason = _tools().validate_process(self.config.workspace, program, arguments)
        if not allowed:
            raise PermissionError(reason)
        working_directory = _tools()._safe_target(self.config.workspace, cwd)
        if not working_directory.is_dir():
            raise NotADirectoryError(cwd)
        host_command: list[str] | None = None
        if isinstance(self._execution_backend, _tools().HostBackend):
            host_command = self._project_python_command(
                program, arguments, working_directory
            )
            if host_command is None:
                host_command = _tools()._program_command(
                    program, arguments, self.config.workspace
                )
        execution = self._execution_backend.run(
            program,
            arguments,
            cwd=working_directory,
            timeout=min(timeout or self.config.command_timeout, 600),
            env=_tools()._minimal_environment(self.config.data_dir),
            host_command=host_command,
        )
        result = {
            "exit_code": execution.exit_code,
            "timed_out": execution.timed_out,
            "stdout": _tools()._trim(execution.stdout),
            "stderr": _tools()._trim(execution.stderr),
            "duration": round(execution.duration, 3),
            "execution_backend": execution.backend,
            "execution_boundary": execution.boundary.as_dict(),
        }
        if execution.timed_out:
            result["error"] = "Process exceeded its wall-clock limit and its process tree was terminated"
        return result

    def _require_process_execution(self) -> None:
        if self.config.execution_mode != "trusted-host":
            raise PermissionError("Process execution is disabled")

    def _process_log_directory(self) -> _tools().Path:
        directory = self.config.data_dir.resolve() / "processes"
        directory.mkdir(parents=True, exist_ok=True)
        details = _tools().os.lstat(directory)
        attributes = getattr(details, "st_file_attributes", 0)
        if (
            _tools().stat.S_ISLNK(details.st_mode)
            or attributes & getattr(_tools().stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or not _tools().stat.S_ISDIR(details.st_mode)
        ):
            raise PermissionError("Managed process logs require an ordinary data directory")
        return directory

    def _managed_process(self, process_id: str) -> _tools()._ManagedProcess:
        if not isinstance(process_id, str) or not _tools().re.fullmatch(r"[0-9a-f]{12}", process_id):
            raise ValueError("process_id must be a 12-character managed process identifier")
        try:
            record = self._processes[process_id]
        except KeyError as exc:
            raise KeyError(f"Unknown managed process: {process_id}") from exc
        if _tools().os.path.normcase(record.workspace) != _tools().os.path.normcase(
            str(_tools().Path(self.config.workspace).resolve())
        ):
            raise KeyError(f"Unknown managed process: {process_id}")
        return record

    @staticmethod
    def _finish_managed_collectors(record: _tools()._ManagedProcess) -> None:
        if record.collectors_closed:
            return
        record.stdout_collector.finish()
        record.stderr_collector.finish()
        record.collectors_closed = True

    def _refresh_managed_process(self, record: _tools()._ManagedProcess) -> int | None:
        exit_code = record.process.poll()
        if exit_code is not None and record.ended_at is None:
            record.ended_at = _tools().time.time()
            record.execution_handle.close()
            self._finish_managed_collectors(record)
        return exit_code

    def _managed_status(self, record: _tools()._ManagedProcess) -> dict[str, _tools().Any]:
        exit_code = self._refresh_managed_process(record)
        if exit_code is None:
            state = "running"
        elif record.stopped:
            state = "stopped"
        else:
            state = "exited"
        elapsed_end = record.ended_at or _tools().time.time()
        return {
            "process_id": record.process_id,
            "name": record.name,
            "pid": record.process.pid,
            "state": state,
            "running": exit_code is None,
            "exit_code": exit_code,
            "program": record.program,
            "arguments": list(record.arguments),
            "cwd": record.cwd,
            "execution_backend": record.backend,
            "execution_boundary": record.boundary.as_dict(),
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "uptime_seconds": round(max(0.0, elapsed_end - record.started_at), 3),
            "stdout_log": str(record.stdout_path.relative_to(self.config.data_dir)).replace("\\", "/"),
            "stderr_log": str(record.stderr_path.relative_to(self.config.data_dir)).replace("\\", "/"),
        }

    def start_process(
        self,
        program: str,
        arguments: list[str] | None = None,
        cwd: str = ".",
        name: str | None = None,
    ) -> dict[str, _tools().Any]:
        self._require_process_execution()
        if self.config.autonomy == "readonly":
            raise PermissionError("Processes are disabled in readonly mode")
        arguments = list(arguments or [])
        allowed, reason = _tools().validate_process(self.config.workspace, program, arguments)
        if not allowed:
            raise PermissionError(reason)
        working_directory = _tools()._safe_target(self.config.workspace, cwd)
        if not working_directory.is_dir():
            raise NotADirectoryError(cwd)
        display_name = (name or _tools().Path(program).stem).strip()
        if (
            not display_name
            or len(display_name) > 100
            or any(char in display_name for char in "\x00\r\n")
        ):
            raise ValueError("name must contain 1-100 characters without control characters")

        with self._process_lock:
            active = 0
            for existing in self._processes.values():
                if self._refresh_managed_process(existing) is None:
                    active += 1
            if active >= _tools().MAX_MANAGED_PROCESSES:
                raise RuntimeError(f"At most {_tools().MAX_MANAGED_PROCESSES} managed processes may run at once")

            process_id = _tools().uuid.uuid4().hex[:12]
            while process_id in self._processes:
                process_id = _tools().uuid.uuid4().hex[:12]
            log_directory = self._process_log_directory()
            stdout_path = log_directory / f"{process_id}.stdout.log"
            stderr_path = log_directory / f"{process_id}.stderr.log"
            host_command: list[str] | None = None
            if isinstance(self._execution_backend, _tools().HostBackend):
                host_command = self._project_python_command(
                    program, arguments, working_directory
                )
                if host_command is None:
                    host_command = _tools()._program_command(
                        program, arguments, self.config.workspace
                    )
            execution_handle = self._execution_backend.start(
                program,
                arguments,
                cwd=working_directory,
                env=_tools()._minimal_environment(self.config.data_dir),
                host_command=host_command,
                process_name=f"managed-{process_id}",
            )
            process = execution_handle.process
            job = execution_handle.job
            stdout_collector: _tools()._FileOutputCollector | None = None
            stderr_collector: _tools()._FileOutputCollector | None = None
            try:
                if process.stdout is None or process.stderr is None:
                    raise RuntimeError("Managed process output pipes were not created")
                stdout_collector = _tools()._FileOutputCollector(process.stdout, stdout_path)
                stderr_collector = _tools()._FileOutputCollector(process.stderr, stderr_path)
                stdout_collector.start()
                stderr_collector.start()
            except Exception:
                execution_handle.terminate()
                try:
                    process.wait(timeout=10)
                except _tools().subprocess.TimeoutExpired:
                    pass
                for collector in (stdout_collector, stderr_collector):
                    if collector is not None:
                        collector.finish()
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
                raise

            now = _tools().time.time()
            record = _tools()._ManagedProcess(
                process_id=process_id,
                name=display_name,
                program=program,
                arguments=arguments,
                cwd=str(working_directory.relative_to(self.config.workspace)).replace("\\", "/") or ".",
                workspace=str(_tools().Path(self.config.workspace).resolve()),
                process=process,
                job=job,
                execution_handle=execution_handle,
                backend=execution_handle.backend,
                boundary=execution_handle.boundary,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                stdout_collector=stdout_collector,
                stderr_collector=stderr_collector,
                started_at=now,
                started_monotonic=_tools().time.monotonic(),
            )
            self._processes[process_id] = record
            return self._managed_status(record)

    def process_status(self, process_id: str | None = None) -> dict[str, _tools().Any]:
        self._require_process_execution()
        with self._process_lock:
            if process_id is not None:
                return self._managed_status(self._managed_process(process_id))
            processes = [
                self._managed_status(record)
                for record in sorted(self._processes.values(), key=lambda item: item.started_at)
                if _tools().os.path.normcase(record.workspace) == _tools().os.path.normcase(
                    str(_tools().Path(self.config.workspace).resolve())
                )
            ]
            return {
                "processes": processes,
                "count": len(processes),
                "active": sum(item["running"] for item in processes),
            }

    @staticmethod
    def _log_tail(
        collector: _tools()._FileOutputCollector,
        lines: int,
        max_characters: int,
    ) -> dict[str, _tools().Any]:
        raw, captured_bytes, total_bytes = collector.snapshot()
        text = raw.decode("utf-8", errors="replace")
        split = text.splitlines()
        content = "\n".join(split[-lines:])
        character_clipped = max(0, len(content) - max_characters)
        if character_clipped:
            content = content[-max_characters:]
        return {
            "content": content,
            "captured_bytes": captured_bytes,
            "total_bytes": total_bytes,
            "discarded_bytes": max(0, total_bytes - captured_bytes),
            "character_clipped": character_clipped,
        }

    def process_logs(
        self,
        process_id: str,
        stream: str = "both",
        lines: int = 200,
        max_characters: int = 12_000,
    ) -> dict[str, _tools().Any]:
        self._require_process_execution()
        if stream not in {"stdout", "stderr", "both"}:
            raise ValueError("stream must be stdout, stderr, or both")
        if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= 1000:
            raise ValueError("lines must be an integer between 1 and 1000")
        if (
            isinstance(max_characters, bool)
            or not isinstance(max_characters, int)
            or not 100 <= max_characters <= _tools().MAX_TOOL_OUTPUT
        ):
            raise ValueError(f"max_characters must be an integer between 100 and {_tools().MAX_TOOL_OUTPUT}")
        with self._process_lock:
            record = self._managed_process(process_id)
            status = self._managed_status(record)
            result: dict[str, _tools().Any] = {"process_id": process_id, "state": status["state"]}
            if stream in {"stdout", "both"}:
                result["stdout"] = self._log_tail(record.stdout_collector, lines, max_characters)
            if stream in {"stderr", "both"}:
                result["stderr"] = self._log_tail(record.stderr_collector, lines, max_characters)
            return result

    def stop_process(self, process_id: str) -> dict[str, _tools().Any]:
        self._require_process_execution()
        if self.config.autonomy == "readonly":
            raise PermissionError("Process stops are disabled in readonly mode")
        with self._process_lock:
            record = self._managed_process(process_id)
            if self._refresh_managed_process(record) is not None:
                result = self._managed_status(record)
                result["already_exited"] = True
                return result
            record.stopped = True
            record.execution_handle.terminate()
            try:
                record.process.wait(timeout=15)
            except _tools().subprocess.TimeoutExpired:
                record.process.kill()
                record.process.wait(timeout=5)
            self._refresh_managed_process(record)
            result = self._managed_status(record)
            result["already_exited"] = False
            return result

    def http_health(
        self,
        url: str,
        process_id: str | None = None,
        timeout: int = 5,
        retries: int = 0,
        interval_ms: int = 250,
    ) -> dict[str, _tools().Any]:
        self._require_process_execution()
        if not isinstance(url, str) or url != url.strip() or not url or len(url) > 4096:
            raise ValueError("url must be a non-empty local HTTP URL")
        if any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise ValueError("url contains control characters")
        for key, value, minimum, maximum in (
            ("timeout", timeout, 1, 10),
            ("retries", retries, 0, 10),
            ("interval_ms", interval_ms, 0, 5000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
        parsed = _tools().urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or not parsed.hostname:
            raise PermissionError("Health checks support only local plain HTTP URLs")
        if parsed.username is not None or parsed.password is not None:
            raise PermissionError("Credentials in health-check URLs are blocked")
        try:
            port = parsed.port or 80
        except ValueError as exc:
            raise ValueError("Invalid health-check URL port") from exc
        hostname = parsed.hostname
        try:
            address = _tools().ipaddress.ip_address(hostname.split("%", 1)[0])
            if not address.is_loopback:
                raise PermissionError("Health checks are limited to localhost")
            connect_host = str(address)
        except ValueError:
            if hostname.casefold() != "localhost":
                raise PermissionError("Health checks are limited to localhost") from None
            answers = _tools().socket.getaddrinfo("localhost", port, 0, _tools().socket.SOCK_STREAM)
            addresses = [_tools().ipaddress.ip_address(item[4][0].split("%", 1)[0]) for item in answers]
            if not addresses or any(not item.is_loopback for item in addresses):
                raise PermissionError(
                    "localhost resolved to a non-loopback address"
                ) from None
            connect_host = str(addresses[0])
        path = _tools().urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        last_result: dict[str, _tools().Any] = {}
        for attempt in range(1, retries + 2):
            if process_id is not None:
                with self._process_lock:
                    managed = self._managed_status(self._managed_process(process_id))
                if managed.get("running") is not True:
                    return {
                        "url": url,
                        "process_id": process_id,
                        "process_running": False,
                        "healthy": False,
                        "status": None,
                        "attempts": attempt,
                        "error": (
                            "Managed process exited before the health check "
                            f"(state={managed.get('state')}, exit_code={managed.get('exit_code')})"
                        ),
                    }
                # A process that failed to bind (for example "address already in use")
                # can still be alive for a moment while another program answers on the
                # same port. Only the managed process, or its children, may satisfy a
                # health check bound to it.
                from .local_ports import port_owner

                if port_owner(port, int(managed["pid"])) == "other":
                    return {
                        "url": url,
                        "process_id": process_id,
                        "process_running": True,
                        "healthy": False,
                        "status": None,
                        "attempts": attempt,
                        "error": (
                            f"Port {port} is answered by a different program, not managed "
                            f"process {process_id}. It probably could not bind the port; check "
                            "process_logs and start it on a free port."
                        ),
                    }
            started = _tools().time.perf_counter()
            connection = _tools().http.client.HTTPConnection(connect_host, port=port, timeout=timeout)
            try:
                connection.request("GET", path, headers={"Host": parsed.netloc, "Connection": "close"})
                response = connection.getresponse()
                preview = response.read(4096).decode("utf-8", errors="replace")
                healthy = 200 <= response.status < 400
                last_result = {
                    "url": url,
                    "process_id": process_id,
                    "healthy": healthy,
                    "status": response.status,
                    "reason": response.reason,
                    "attempts": attempt,
                    "latency_ms": round((_tools().time.perf_counter() - started) * 1000, 1),
                    "body_preview": preview[:1000],
                }
                if process_id is not None:
                    with self._process_lock:
                        managed = self._managed_status(self._managed_process(process_id))
                    last_result["process_running"] = managed.get("running") is True
                    if last_result["process_running"] is not True:
                        last_result["healthy"] = False
                        last_result["error"] = (
                            "Managed process exited during the health check "
                            f"(state={managed.get('state')}, exit_code={managed.get('exit_code')})"
                        )
                        healthy = False
                if healthy:
                    return last_result
            except (OSError, _tools().http.client.HTTPException) as exc:
                last_result = {
                    "url": url,
                    "process_id": process_id,
                    "process_running": True if process_id is not None else None,
                    "healthy": False,
                    "status": None,
                    "attempts": attempt,
                    "latency_ms": round((_tools().time.perf_counter() - started) * 1000, 1),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            finally:
                connection.close()
            if attempt <= retries and interval_ms:
                _tools().time.sleep(interval_ms / 1000)
        return last_result

    def web_app_check(
        self,
        url: str,
        actions: list[dict[str, _tools().Any]],
        process_id: str | None = None,
        settle_ms: int = 800,
        timeout_seconds: int = 60,
    ) -> dict[str, _tools().Any]:
        """Render and exercise a loopback web app in a disposable headless browser."""
        from .web_check import new_check_id, run_web_check, validate_local_url

        self._require_process_execution()
        validate_local_url(url)
        if process_id is not None:
            with self._process_lock:
                managed = self._managed_status(self._managed_process(process_id))
            if managed.get("running") is not True:
                return {
                    "url": url, "process_id": process_id, "process_running": False,
                    "verified": False,
                    "reasons_not_verified": [
                        "The managed process serving this app is not running "
                        f"(state={managed.get('state')}, exit_code={managed.get('exit_code')})."
                    ],
                }
            from .local_ports import port_owner

            owner = port_owner(validate_local_url(url)[2], int(managed["pid"]))
            if owner in {"other", "none"}:
                return {
                    "url": url, "process_id": process_id, "process_running": True,
                    "verified": False,
                    "reasons_not_verified": [
                        f"Managed process {process_id} is not the program listening at this address"
                        + (" (another program answers there)." if owner == "other" else " (nothing is listening).")
                        + " Check process_logs and serve the app on a free port."
                    ],
                }
        artifacts = self.config.data_dir.resolve() / "web-checks" / new_check_id()
        result = run_web_check(url, actions=actions, settle_ms=settle_ms,
                               timeout_seconds=timeout_seconds, artifact_dir=artifacts)
        result["process_id"] = process_id
        result["process_running"] = True if process_id is not None else None
        result["screenshots"] = [
            str(_tools().Path(path).resolve().relative_to(self.config.data_dir.resolve())).replace("\\", "/")
            for path in result.get("screenshots", [])
        ]
        return result
