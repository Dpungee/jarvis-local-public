from __future__ import annotations

import ctypes
import os
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Iterable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .trusted_executables import trusted_install_file, windows_system_executable

DOCKER_IMAGE = (
    "python:3.13.15-slim-bookworm@"
    "sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca"
)
MAX_EXECUTION_OUTPUT = 1_000_000
_PROTECTED_MOUNT_COMPONENTS = frozenset({
    ".aws", ".azure", ".git", ".gnupg", ".jarvis-runtime", ".jarvis-skills",
    ".kube", ".ssh", "codex-cli-home", "credentials", "secrets", "vault",
})
_PROTECTED_MOUNT_FILES = frozenset({
    ".env", ".npmrc", ".pypirc", "constitution.md", "soul.md",
})


class _BoundedCollector:
    def __init__(self, stream: Any, limit: int = MAX_EXECUTION_OUTPUT) -> None:
        self.stream = stream
        self.limit = limit
        self.head_limit = max(1, limit // 2)
        self.tail_limit = max(0, limit - self.head_limit)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def _drain(self) -> None:
        try:
            while True:
                reader = getattr(self.stream, "read1", self.stream.read)
                chunk = reader(8192)
                if not chunk:
                    break
                self.total += len(chunk)
                remaining = self.head_limit - len(self.head)
                if remaining > 0:
                    self.head.extend(chunk[:remaining])
                    chunk = chunk[remaining:]
                if chunk and self.tail_limit:
                    self.tail.extend(chunk)
                    if len(self.tail) > self.tail_limit:
                        del self.tail[: len(self.tail) - self.tail_limit]
        finally:
            self.stream.close()

    def start(self) -> None:
        self.thread.start()

    def finish(self) -> str:
        self.thread.join(timeout=10)
        retained = len(self.head) + len(self.tail)
        if self.total <= retained:
            return bytes(self.head + self.tail).decode("utf-8", errors="replace")
        return (
            bytes(self.head).decode("utf-8", errors="replace")
            + f"\n...[discarded {self.total - retained} output bytes; retained tail]\n"
            + bytes(self.tail).decode("utf-8", errors="replace")
        )


class WindowsJob:
    """Kill-on-close containment for the host-side launcher process tree."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.handle: Any = None
        if os.name != "nt":
            return

        class _IOCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IOCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x2208
        information.BasicLimitInformation.ActiveProcessLimit = 64
        information.JobMemoryLimit = 8 * 1024 * 1024 * 1024
        configured = kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(information), ctypes.sizeof(information)
        )
        assigned = configured and kernel32.AssignProcessToJobObject(
            handle, wintypes.HANDLE(int(process._handle))
        )
        if assigned:
            self.handle = handle
        else:
            kernel32.CloseHandle(handle)

    def close(self) -> None:
        if self.handle is not None:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self.handle)
            self.handle = None


def _resume_windows_process(process: subprocess.Popen[bytes]) -> None:
    if os.name != "nt":
        return
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    status = ntdll.NtResumeProcess(wintypes.HANDLE(int(process._handle)))
    if status != 0:
        raise RuntimeError("Could not resume the contained process")


def _terminate_process_tree(process: subprocess.Popen[bytes], job: WindowsJob) -> None:
    job.close()
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            taskkill = windows_system_executable("System32", "taskkill.exe")
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(process.pid, 9)
        except OSError:
            pass
    if process.poll() is None:
        process.kill()


def _docker_install_candidates() -> tuple[Path, ...]:
    """Return fixed Docker CLI locations without consulting cwd or PATH."""
    if os.name != "nt":
        return (Path("/usr/bin/docker"), Path("/usr/sbin/docker"))

    roots: list[Path] = []
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion",
        ) as key:
            for value_name in ("ProgramFilesDir", "ProgramFilesDir (x86)"):
                try:
                    value, _kind = winreg.QueryValueEx(key, value_name)
                except OSError:
                    continue
                root = Path(str(value))
                if root.is_absolute() and root not in roots:
                    roots.append(root)
    except (ImportError, OSError):
        pass
    if not roots:
        # This is a fixed machine-wide location, not the redirectable
        # ProgramFiles environment variable.
        roots.append(Path(r"C:\Program Files"))
    return tuple(
        root / "Docker" / "Docker" / "resources" / "bin" / "docker.exe"
        for root in roots
    )


def docker_executable(configured: str | Path | None = None) -> str | None:
    """Resolve Docker only from an OS-administered absolute install path."""
    candidates: tuple[Path, ...]
    if configured is None:
        candidates = _docker_install_candidates()
    else:
        candidate = Path(configured)
        expected_name = "docker.exe" if os.name == "nt" else "docker"
        if not candidate.is_absolute() or candidate.name.casefold() != expected_name:
            return None
        candidates = (candidate,)
    for candidate in candidates:
        resolved = trusted_install_file(candidate)
        if resolved is not None:
            return str(resolved)
    return None


def _docker_subprocess_environment(
    executable: str,
    source: dict[str, str] | os._Environ[str],
) -> dict[str, str]:
    environment = dict(source)
    environment["PATH"] = str(Path(executable).parent)
    if os.name == "nt":
        environment["NoDefaultCurrentDirectoryInExePath"] = "1"
    return environment


def docker_available(
    *,
    timeout: float = 5.0,
    executable: str | Path | None = None,
) -> bool:
    resolved = docker_executable(executable)
    if resolved is None:
        return False
    directory = Path(resolved).parent
    try:
        result = subprocess.run(
            [resolved, "info", "--format", "{{.ServerVersion}}"],
            cwd=directory,
            env=_docker_subprocess_environment(resolved, os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(0.1, min(float(timeout), 15.0)),
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


@dataclass(frozen=True)
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool
    duration: float


@dataclass
class ExecutionHandle:
    process: subprocess.Popen[bytes]
    job: WindowsJob
    backend: str
    container_name: str | None = None
    docker: str | None = None

    def terminate(self) -> None:
        _terminate_process_tree(self.process, self.job)
        if self.container_name and self.docker:
            try:
                directory = Path(self.docker).resolve().parent
                subprocess.run(
                    [self.docker, "rm", "--force", self.container_name],
                    cwd=directory,
                    env=_docker_subprocess_environment(self.docker, os.environ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass

    def close(self) -> None:
        self.job.close()


class ExecutionBackend:
    name = "base"

    def start(
        self,
        program: str,
        arguments: Iterable[str],
        *,
        cwd: Path,
        env: dict[str, str],
        host_command: list[str] | None = None,
        process_name: str | None = None,
    ) -> ExecutionHandle:
        raise NotImplementedError

    def run(
        self,
        program: str,
        arguments: Iterable[str],
        *,
        cwd: Path,
        timeout: int,
        env: dict[str, str],
        host_command: list[str] | None = None,
    ) -> ExecutionResult:
        started = time.monotonic()
        handle = self.start(
            program,
            arguments,
            cwd=cwd,
            env=env,
            host_command=host_command,
            process_name=f"jarvis-run-{uuid.uuid4().hex[:16]}",
        )
        process = handle.process
        if process.stdout is None or process.stderr is None:
            handle.terminate()
            raise RuntimeError("Process output pipes were not created")
        stdout = _BoundedCollector(process.stdout)
        stderr = _BoundedCollector(process.stderr)
        stdout.start()
        stderr.start()
        timed_out = False
        try:
            process.wait(timeout=max(1, min(int(timeout), 600)))
        except subprocess.TimeoutExpired:
            timed_out = True
            handle.terminate()
            process.wait(timeout=15)
        finally:
            handle.close()
        return ExecutionResult(
            stdout=stdout.finish(),
            stderr=stderr.finish(),
            exit_code=process.returncode,
            timed_out=timed_out,
            duration=max(0.0, time.monotonic() - started),
        )

    @staticmethod
    def _start_contained(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        backend: str,
        container_name: str | None = None,
        docker: str | None = None,
    ) -> ExecutionHandle:
        creation_flags = 0
        options: dict[str, Any] = {}
        if os.name == "nt":
            creation_flags = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
                | 0x00000004
            )
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=creation_flags,
            **options,
        )
        job = WindowsJob(process)
        try:
            if os.name == "nt":
                if job.handle is None:
                    raise RuntimeError("Could not attach the process containment job")
                _resume_windows_process(process)
        except Exception:
            _terminate_process_tree(process, job)
            raise
        return ExecutionHandle(
            process=process,
            job=job,
            backend=backend,
            container_name=container_name,
            docker=docker,
        )


class HostBackend(ExecutionBackend):
    name = "host"

    def start(
        self,
        program: str,
        arguments: Iterable[str],
        *,
        cwd: Path,
        env: dict[str, str],
        host_command: list[str] | None = None,
        process_name: str | None = None,
    ) -> ExecutionHandle:
        del program, arguments, process_name
        if not host_command:
            raise ValueError("Host execution requires a prevalidated resolved command")
        return self._start_contained(
            list(host_command), cwd=cwd, env=env, backend=self.name
        )


class DockerBackend(ExecutionBackend):
    name = "docker"

    def __init__(
        self,
        workspace: Path,
        *,
        executable: str | None = None,
        verify: bool = True,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if "," in str(self.workspace):
            raise ValueError("Docker workspaces may not contain commas")
        self.docker = docker_executable(executable)
        if self.docker is None:
            raise RuntimeError(
                "JARVIS_EXECUTION_BACKEND=docker requires the Docker CLI and a running daemon"
            )
        if verify and not docker_available(executable=self.docker):
            raise RuntimeError(
                "JARVIS_EXECUTION_BACKEND=docker requires a reachable Docker daemon"
            )
        self._windows_mount_lock = threading.Lock()
        self._windows_mount_prepared = False

    @staticmethod
    def _current_windows_user_sid() -> str:
        """Return the SID carried by this process without invoking a shell."""
        if os.name != "nt":
            raise RuntimeError("Windows user SIDs are only available on Windows")

        class _SidAndAttributes(ctypes.Structure):
            _fields_ = [
                ("sid", ctypes.c_void_p),
                ("attributes", wintypes.DWORD),
            ]

        class _TokenUser(ctypes.Structure):
            _fields_ = [("user", _SidAndAttributes)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
        ):
            raise OSError(ctypes.get_last_error(), "Could not open the process token")
        try:
            required = wintypes.DWORD()
            advapi32.GetTokenInformation(
                token, 1, None, 0, ctypes.byref(required)
            )
            if not required.value:
                raise OSError(
                    ctypes.get_last_error(), "Could not size the process user token"
                )
            buffer = ctypes.create_string_buffer(required.value)
            if not advapi32.GetTokenInformation(
                token,
                1,
                buffer,
                required.value,
                ctypes.byref(required),
            ):
                raise OSError(
                    ctypes.get_last_error(), "Could not read the process user token"
                )
            token_user = ctypes.cast(
                buffer, ctypes.POINTER(_TokenUser)
            ).contents
            rendered = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(
                token_user.user.sid, ctypes.byref(rendered)
            ):
                raise OSError(
                    ctypes.get_last_error(), "Could not render the process user SID"
                )
            try:
                return str(rendered.value)
            finally:
                kernel32.LocalFree(rendered)
        finally:
            kernel32.CloseHandle(token)

    def _prepare_windows_bind_mount(self) -> None:
        """Keep Docker-created bind-mount files accessible to the host user.

        Python-created Windows directories can grant their owner access only
        through an inheritable OWNER RIGHTS entry. Docker Desktop gives a file
        created by a Linux container a different synthetic owner, so that entry
        no longer authorizes the Windows process that launched the run. Add an
        idempotent, inheritable Modify entry for that existing process identity
        before the container can create output. This grants no new identity and
        changes neither the mounted scope nor the container sandbox.
        """
        if os.name != "nt" or self._windows_mount_prepared:
            return
        with self._windows_mount_lock:
            if self._windows_mount_prepared:
                return
            try:
                icacls = windows_system_executable("System32", "icacls.exe")
            except (OSError, PermissionError, ValueError) as exc:
                raise RuntimeError(
                    "The trusted Windows ACL utility is unavailable"
                ) from exc
            sid = self._current_windows_user_sid()
            try:
                result = subprocess.run(
                    [
                        str(icacls),
                        str(self.workspace),
                        "/grant",
                        f"*{sid}:(OI)(CI)(M)",
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=15,
                    check=False,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise PermissionError(
                    "Could not prepare the Windows workspace for isolated execution"
                ) from exc
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                raise PermissionError(
                    "Could not prepare the Windows workspace for isolated execution"
                    + (f": {detail}" if detail else "")
                )
            self._windows_mount_prepared = True

    @staticmethod
    def _container_program(program: str, arguments: list[str]) -> list[str]:
        name = Path(program).name.casefold()
        for suffix in (".exe", ".cmd", ".bat", ".com"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        if name in {"python", "python3", "py"}:
            return ["python", *arguments]
        if name in {"pytest", "mypy", "ruff"}:
            return ["python", "-m", name, *arguments]
        return [name, *arguments]

    def _validate_mount(self) -> None:
        if not self.workspace.is_dir():
            raise NotADirectoryError(self.workspace)
        seen = 0
        for root, directories, files in os.walk(self.workspace, followlinks=False):
            seen += len(directories) + len(files)
            if seen > 200_000:
                raise RuntimeError("Docker workspace safety scan exceeded its entry bound")
            for name in tuple(directories):
                path = Path(root) / name
                details = os.lstat(path)
                attributes = getattr(details, "st_file_attributes", 0)
                folded = name.rstrip(" .").casefold()
                if folded in _PROTECTED_MOUNT_COMPONENTS:
                    raise PermissionError(
                        f"Docker execution refuses protected workspace path: {name}"
                    )
                if stat.S_ISLNK(details.st_mode) or attributes & getattr(
                    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
                ):
                    raise PermissionError("Docker execution refuses workspace links")
            for name in files:
                path = Path(root) / name
                details = os.lstat(path)
                attributes = getattr(details, "st_file_attributes", 0)
                if stat.S_ISLNK(details.st_mode) or attributes & getattr(
                    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
                ):
                    raise PermissionError("Docker execution refuses workspace links")
                folded = name.rstrip(" .").casefold()
                if folded in _PROTECTED_MOUNT_FILES or folded.startswith(".env."):
                    raise PermissionError(
                        f"Docker execution refuses protected workspace file: {name}"
                    )

    def command_for(
        self,
        program: str,
        arguments: Iterable[str],
        *,
        cwd: Path,
        process_name: str,
    ) -> list[str]:
        self._validate_mount()
        self._prepare_windows_bind_mount()
        working = Path(cwd).resolve()
        relative = working.relative_to(self.workspace).as_posix()
        container_cwd = "/workspace" if relative == "." else f"/workspace/{relative}"
        safe_name = "jarvis-" + "".join(
            char for char in str(process_name).casefold() if char.isalnum() or char in "_.-"
        )[:48]
        if safe_name == "jarvis-":
            raise ValueError("Docker process name is invalid")
        # Docker Desktop accepts backslash paths but can create files with an
        # unreadable Windows ACL when a Linux container writes through that
        # spelling. The canonical forward-slash drive path preserves the same
        # exact mount while keeping container output readable by the host user.
        mount_source = str(self.workspace)
        if os.name == "nt":
            mount_source = mount_source.replace("\\", "/")
        mount = f"type=bind,source={mount_source},target=/workspace"
        return [
            self.docker,
            "run",
            "--rm",
            "--name",
            safe_name,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--pids-limit",
            "256",
            "--user",
            "65532:65532",
            "--mount",
            mount,
            "--workdir",
            container_cwd,
            "--env",
            "PYTHONIOENCODING=utf-8",
            "--env",
            "PYTHONUTF8=1",
            DOCKER_IMAGE,
            *self._container_program(program, list(arguments)),
        ]

    def start(
        self,
        program: str,
        arguments: Iterable[str],
        *,
        cwd: Path,
        env: dict[str, str],
        host_command: list[str] | None = None,
        process_name: str | None = None,
    ) -> ExecutionHandle:
        del host_command
        name = process_name or f"run-{uuid.uuid4().hex[:16]}"
        command = self.command_for(program, arguments, cwd=cwd, process_name=name)
        container_name = command[command.index("--name") + 1]
        docker_directory = str(Path(self.docker).resolve().parent)
        docker_environment = _docker_subprocess_environment(self.docker, env)
        return self._start_contained(
            command,
            cwd=Path(docker_directory),
            env=docker_environment,
            backend=self.name,
            container_name=container_name,
            docker=self.docker,
        )


def build_execution_backend(config: Any) -> ExecutionBackend:
    backend = str(getattr(config, "execution_backend", "host")).casefold()
    if backend == "host":
        return HostBackend()
    if backend == "docker":
        return DockerBackend(Path(config.workspace))
    raise ValueError("Unknown execution backend")
