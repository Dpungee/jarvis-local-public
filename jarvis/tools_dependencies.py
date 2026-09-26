"""Current ToolBox methods, mechanically extracted by domain.

Shared globals are resolved through jarvis.tools to preserve runtime patches.
"""
from __future__ import annotations


def _tools():
    from . import tools

    return tools


class DependencyToolsMixin:
    def detect_project(self, path: str = ".") -> dict[str, _tools().Any]:
        target = _tools()._safe_target(self.config.workspace, path)
        if not target.is_dir():
            raise NotADirectoryError(path)

        marker_types = {
            "package.json": "node",
            "pyproject.toml": "python",
            "requirements.txt": "python",
            "setup.py": "python",
            "Cargo.toml": "rust",
            "go.mod": "go",
            "CMakeLists.txt": "cmake",
            "pom.xml": "java-maven",
            "build.gradle": "java-gradle",
            "build.gradle.kts": "java-gradle",
        }
        markers: list[str] = []
        project_types: list[str] = []
        for marker, project_type in marker_types.items():
            candidate = _tools()._safe_target(self.config.workspace, target / marker)
            if candidate.is_file():
                markers.append(marker)
                if project_type not in project_types:
                    project_types.append(project_type)
        solution_files = sorted(item.name for item in target.glob("*.sln") if item.is_file())
        project_files = sorted(item.name for item in target.glob("*.csproj") if item.is_file())
        if solution_files or project_files:
            markers.extend(solution_files + project_files)
            project_types.append("dotnet")

        package_scripts: list[str] = []
        package_path = target / "package.json"
        if package_path.is_file() and package_path.stat().st_size <= _tools().MAX_FILE_BYTES:
            try:
                package_data = _tools().json.loads(package_path.read_text(encoding="utf-8"))
                raw_scripts = package_data.get("scripts", {}) if isinstance(package_data, dict) else {}
                if isinstance(raw_scripts, dict):
                    package_scripts = sorted(
                        str(name)[:100] for name, value in raw_scripts.items()
                        if isinstance(name, str) and isinstance(value, str)
                    )[:100]
            except (OSError, UnicodeError, _tools().json.JSONDecodeError):
                package_scripts = []

        candidates = (
            "main.py", "app.py", "server.py", "manage.py",
            "server.js", "index.js", "app.js", "server.mjs", "index.mjs",
        )
        entrypoints = [name for name in candidates if (target / name).is_file()]
        commands: list[dict[str, _tools().Any]] = []

        def add_command(purpose: str, program: str, arguments: list[str]) -> None:
            allowed, _reason = _tools().validate_process(self.config.workspace, program, arguments)
            if allowed and not any(
                command["program"] == program and command["arguments"] == arguments
                for command in commands
            ):
                commands.append({
                    "purpose": purpose,
                    "program": program,
                    "arguments": arguments,
                    "cwd": str(target.relative_to(self.config.workspace)).replace("\\", "/") or ".",
                })

        if "python" in project_types:
            if (target / "tests").is_dir():
                add_command("test", "python", ["-m", "unittest", "discover"])
            for entrypoint in entrypoints:
                if entrypoint.endswith(".py"):
                    add_command("start", "python", [entrypoint])
                    break
        if "node" in project_types:
            for script in ("test", "build", "lint", "typecheck", "check"):
                if script in package_scripts:
                    add_command(script, "npm", ["run", script])
            for entrypoint in entrypoints:
                if entrypoint.endswith((".js", ".mjs")):
                    add_command("start", "node", [entrypoint])
                    break
        if "rust" in project_types:
            add_command("test", "cargo", ["test"])
            add_command("build", "cargo", ["build"])
        if "go" in project_types:
            add_command("test", "go", ["test", "./..."])
            add_command("build", "go", ["build", "./..."])
        if "dotnet" in project_types:
            dotnet_target = (solution_files or project_files or [""])[0]
            add_command("test", "dotnet", ["test", dotnet_target] if dotnet_target else ["test"])
            add_command("build", "dotnet", ["build", dotnet_target] if dotnet_target else ["build"])
        if "cmake" in project_types:
            add_command("configure", "cmake", ["-S", ".", "-B", "build"])
            add_command("build", "cmake", ["--build", "build"])

        return {
            "path": str(target.relative_to(self.config.workspace)).replace("\\", "/") or ".",
            "detected": bool(project_types),
            "types": project_types,
            "markers": markers,
            "entrypoints": entrypoints,
            "package_scripts": package_scripts,
            "commands": commands,
        }

    def _project_environment(self, working_directory: _tools().Path, create: bool = False) -> _tools().Path:
        data_dir = self.config.data_dir.resolve()
        legacy_base = data_dir / "project-environments"
        key_material = _tools().os.path.normcase(str(working_directory.resolve()))
        key = _tools().hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:20]
        legacy_environment = legacy_base / key
        base = legacy_base
        environment = legacy_environment
        if (
            _tools().os.name == "nt"
            and not _tools().os.path.lexists(legacy_environment)
            and len(str(self._venv_python(legacy_environment))) > 245
        ):
            # A venv adds Scripts/site-packages paths below its root. Shorten the
            # internal directory name when a custom JARVIS_DATA directory is
            # deeply nested, rather than failing later with WinError 206.
            base = data_dir / "v"
            environment = base / key
            if len(str(self._venv_python(environment))) > 245:
                raise OSError(
                    "JARVIS_DATA is too deeply nested for a reliable Windows Python "
                    "environment; shorten JARVIS_DATA and retry"
                )
        if create:
            base.mkdir(parents=True, exist_ok=True)
        if not base.exists():
            return environment
        details = _tools().os.lstat(base)
        attributes = getattr(details, "st_file_attributes", 0)
        if (
            _tools().stat.S_ISLNK(details.st_mode)
            or attributes & getattr(_tools().stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or not _tools().stat.S_ISDIR(details.st_mode)
        ):
            raise PermissionError("Project environments require an ordinary JARVIS data directory")
        if _tools().os.path.lexists(environment):
            details = _tools().os.lstat(environment)
            attributes = getattr(details, "st_file_attributes", 0)
            if (
                _tools().stat.S_ISLNK(details.st_mode)
                or attributes & getattr(_tools().stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or not _tools().stat.S_ISDIR(details.st_mode)
            ):
                raise PermissionError("The project environment must be an ordinary directory")
        return environment

    @staticmethod
    def _venv_python(environment: _tools().Path) -> _tools().Path:
        if _tools().os.name == "nt":
            return environment / "Scripts" / "python.exe"
        return environment / "bin" / "python"

    def _project_python_command(
        self,
        program: str,
        arguments: list[str],
        working_directory: _tools().Path,
    ) -> list[str] | None:
        name = _tools().Path(program).name.casefold()
        for suffix in (".exe", ".cmd", ".bat", ".com"):
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                break
        modules = {"pytest", "mypy", "ruff"}
        if name not in {"python", "python3", "py", *modules}:
            return None
        environment = self._project_environment(working_directory)
        interpreter = self._venv_python(environment)
        ready = environment / ".jarvis-ready"
        if not ready.is_file() or not interpreter.is_file():
            return None
        prefix = [str(interpreter.resolve())]
        if name in modules:
            prefix.extend(["-m", name])
        return [*prefix, *arguments]

    def _dependency_manifest(self, working_directory: _tools().Path, name: str) -> _tools().Path | None:
        candidate = _tools()._safe_target(self.config.workspace, working_directory / name)
        if not candidate.exists():
            return None
        details = _tools().os.lstat(candidate)
        attributes = getattr(details, "st_file_attributes", 0)
        if (
            _tools().stat.S_ISLNK(details.st_mode)
            or attributes & getattr(_tools().stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or not _tools().stat.S_ISREG(details.st_mode)
            or details.st_nlink > 1
        ):
            raise PermissionError(f"Dependency manifest must be an ordinary file: {name}")
        if details.st_size > 64 * 1024 * 1024:
            raise ValueError(f"Dependency manifest is unreasonably large: {name}")
        try:
            raw_manifest = candidate.read_bytes()
            manifest_text = raw_manifest.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raise ValueError(
                f"Dependency manifest must be readable UTF-8 text: {name}"
            ) from None
        if "\x00" in manifest_text:
            raise ValueError(f"Dependency manifest contains invalid control data: {name}")
        if _tools().contains_secret(manifest_text) or _tools().re.search(
            r"(?i)https?://[^\s/:@]+:[^\s/@]+@", manifest_text
        ):
            raise PermissionError(
                f"Dependency manifests may not embed credentials: {name}"
            )
        if name in {"requirements.lock", "requirements.txt"}:
            self._validate_requirements_manifest(name, manifest_text)
        elif name in {"package.json", "package-lock.json", "npm-shrinkwrap.json"}:
            self._validate_node_dependency_manifest(name, manifest_text)
        return candidate

    @staticmethod
    def _validate_requirements_manifest(name: str, manifest_text: str) -> None:
        """Allow only index-hosted declarations whose exact bytes are approved."""
        allowed_hash = _tools().re.compile(r"--hash=sha256:[0-9a-fA-F]{64}\b")
        for line_number, raw_line in enumerate(manifest_text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "\\" in line:
                if (
                    name == "requirements.lock"
                    and line.endswith("\\")
                    and "\\" not in line[:-1]
                ):
                    line = line[:-1].rstrip()
                else:
                    raise PermissionError(
                        f"Requirements local paths are not supported: {name}:{line_number}"
                    )
            without_hashes = allowed_hash.sub("", line) if name == "requirements.lock" else line
            normalized_declaration = without_hashes.strip()
            if (
                normalized_declaration.startswith("-")
                or _tools().re.search(r"(?:^|\s)--?[A-Za-z]", normalized_declaration)
                or "@" in normalized_declaration
                or "/" in normalized_declaration
                or "\\" in normalized_declaration
                or "://" in normalized_declaration
                or _tools().re.search(r"(?i)\.(?:whl|zip|tar|tar\.gz|tgz|bz2|gz)(?:\s|$)", normalized_declaration)
                or _tools().re.match(r"^(?:\.|~|[A-Za-z]:)", normalized_declaration)
            ):
                raise PermissionError(
                    "Requirements directives, includes, direct URLs, and local paths are "
                    f"not supported: {name}:{line_number}"
                )
            if "--hash=" in without_hashes:
                raise PermissionError(
                    f"Only SHA-256 lock hashes are supported: {name}:{line_number}"
                )

    @staticmethod
    def _validate_node_dependency_manifest(name: str, manifest_text: str) -> None:
        """Reject local or VCS dependency sources that escape the approved bytes."""
        try:
            payload = _tools().json.loads(manifest_text)
        except _tools().json.JSONDecodeError:
            raise ValueError(f"Dependency manifest must be valid JSON: {name}") from None
        if not isinstance(payload, dict):
            raise ValueError(f"Dependency manifest root must be an object: {name}")

        def unsafe_source(value: _tools().Any) -> bool:
            if not isinstance(value, str):
                return False
            normalized = value.strip().casefold()
            if normalized.startswith("npm:"):
                return _tools().re.fullmatch(
                    r"npm:(?:@[a-z0-9._~-]+/[a-z0-9._~-]+|[a-z0-9._~-]+)"
                    r"@[a-z0-9*^~<>=| ._-]+",
                    normalized,
                ) is None
            return bool(
                normalized.startswith((
                    "file:", "link:", "workspace:", "git:", "git+", "http:",
                    "https:", "github:", "gitlab:", "bitbucket:", "./", "../",
                    "/", "\\", "~\\", "~/",
                ))
                or _tools().re.match(r"^[a-z]:[/\\]", normalized)
                or "/" in normalized
                or "\\" in normalized
            )

        def unsafe_locked_source(value: _tools().Any) -> bool:
            if not isinstance(value, str):
                return False
            normalized = value.strip().casefold()
            return bool(
                normalized.startswith((
                    "file:", "link:", "workspace:", "git:", "git+", "ssh:",
                    "github:", "gitlab:", "bitbucket:", "./", "../", "/", "\\",
                    "~\\", "~/",
                ))
                or (
                    _tools().re.match(r"^[a-z][a-z0-9+.-]*:", normalized)
                    and not normalized.startswith(("http:", "https:"))
                )
                or _tools().re.match(r"^[a-z]:[/\\]", normalized)
            )

        def validate_locked_registry_url(value: _tools().Any, integrity: _tools().Any) -> None:
            if not isinstance(value, str):
                raise PermissionError(
                    f"Node lockfile contains an invalid remote dependency: {name}"
                )
            if any(ord(character) < 32 for character in value):
                raise PermissionError(
                    f"Node lockfile contains an invalid remote dependency: {name}"
                )
            try:
                parsed = _tools().urllib.parse.urlsplit(value)
                port = parsed.port
            except ValueError:
                raise PermissionError(
                    f"Node lockfile contains an invalid remote dependency: {name}"
                ) from None
            host = (parsed.hostname or "").casefold()
            decoded_path = _tools().urllib.parse.unquote(parsed.path)
            if (
                parsed.scheme.casefold() != "https"
                or host != "registry.npmjs.org"
                or port not in (None, 443)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or not decoded_path.startswith("/")
                or not decoded_path.casefold().endswith(".tgz")
                or "\\" in decoded_path
                or ".." in _tools().PurePosixPath(decoded_path).parts
                or any(ord(character) < 32 for character in decoded_path)
            ):
                raise PermissionError(
                    "Node lockfile remote packages must use exact HTTPS "
                    f"registry.npmjs.org tarball URLs: {name}"
                )
            if not isinstance(integrity, str) or _tools().re.fullmatch(
                r"sha(?:256|384|512)-[A-Za-z0-9+/]{40,}={0,2}"
                r"(?:\s+sha(?:256|384|512)-[A-Za-z0-9+/]{40,}={0,2})*",
                integrity.strip(),
            ) is None:
                raise PermissionError(
                    f"Node lockfile remote packages require a strong integrity digest: {name}"
                )

        if name == "package.json":
            if payload.get("workspaces") not in (None, [], {}):
                raise PermissionError(
                    "Node workspaces are not supported for approved dependency installs"
                )
            for field in (
                "dependencies", "devDependencies", "optionalDependencies",
                "peerDependencies",
            ):
                dependencies = payload.get(field, {})
                if not isinstance(dependencies, dict):
                    raise ValueError(f"package.json {field} must be an object")
                for package_name, source in dependencies.items():
                    if (
                        not isinstance(package_name, str)
                        or len(package_name) > 214
                        or not _tools().re.fullmatch(
                            r"(?:@[A-Za-z0-9._~-]+/[A-Za-z0-9._~-]+|[A-Za-z0-9._~-]+)",
                            package_name,
                        )
                    ):
                        raise ValueError(
                            f"package.json {field} contains an invalid package name"
                        )
                    if (
                        not isinstance(source, str)
                        or not source
                        or len(source) > 500
                        or any(ord(character) < 32 for character in source)
                    ):
                        raise ValueError(
                            f"Node dependency {package_name!r} has an invalid version specifier"
                        )
                    if unsafe_source(source):
                        raise PermissionError(
                            f"Node dependency {package_name!s} uses an unsupported local, "
                            "VCS, or direct-URL source"
                        )
            for field in ("overrides", "resolutions"):
                stack: list[_tools().Any] = [payload.get(field, {})]
                while stack:
                    value = stack.pop()
                    if isinstance(value, dict):
                        stack.extend(value.values())
                    elif isinstance(value, list):
                        stack.extend(value)
                    elif unsafe_source(value):
                        raise PermissionError(
                            f"package.json {field} contains an unsupported dependency source"
                        )
        else:
            stack: list[_tools().Any] = [payload]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    resolved = value.get("resolved")
                    if resolved is not None:
                        if not isinstance(resolved, str) or not resolved.strip().casefold().startswith(
                            ("http:", "https:")
                        ):
                            raise PermissionError(
                                f"Node lockfile resolved entries must be exact registry URLs: {name}"
                            )
                        validate_locked_registry_url(resolved, value.get("integrity"))
                    for key, item in value.items():
                        normalized_key = str(key).strip().casefold()
                        if normalized_key == "version" and isinstance(item, str) and (
                            not item
                            or len(item) > 500
                            or any(ord(character) < 32 for character in item)
                            or "/" in item
                            or "\\" in item
                            or _tools().re.match(r"^[a-z][a-z0-9+.-]*:", item.strip().casefold())
                        ):
                            raise PermissionError(
                                f"Node lockfile version entries may not name alternate sources: {name}"
                            )
                        if normalized_key in {"resolved", "link", "version"} and (
                            item is True or unsafe_locked_source(item)
                        ):
                            raise PermissionError(
                                f"Node lockfile contains an unsupported local dependency: {name}"
                            )
                        key_parts = _tools().PurePosixPath(
                            normalized_key.replace("\\", "/")
                        ).parts
                        if (
                            normalized_key.startswith(("../", "..\\", "/", "\\"))
                            or ".." in key_parts
                            or _tools().re.match(r"^[a-z]:[/\\]", normalized_key)
                        ):
                            raise PermissionError(
                                f"Node lockfile contains an outside-workspace package path: {name}"
                            )
                        stack.append(item)
                elif isinstance(value, list):
                    stack.extend(value)

    def _reject_project_dependency_config(self, working_directory: _tools().Path) -> None:
        """Prevent project-controlled npm configuration from changing the command."""
        workspace = self.config.workspace.resolve(strict=True)
        current = working_directory.resolve(strict=True)
        while True:
            npmrc = current / ".npmrc"
            if _tools().os.path.lexists(npmrc):
                raise PermissionError(
                    "Project .npmrc files are not supported for approved dependency installs"
                )
            if current == workspace:
                return
            if not current.is_relative_to(workspace) or current.parent == current:
                raise PermissionError("Dependency project escaped the configured workspace")
            current = current.parent

    @staticmethod
    def _dependency_executor_fingerprint(path: _tools().Path, label: str) -> dict[str, _tools().Any]:
        """Bind one already-trusted dependency executor to stable bytes."""
        candidate = _tools().Path(path).resolve(strict=True)
        before = _tools().os.lstat(candidate)
        attributes = getattr(before, "st_file_attributes", 0)
        if (
            not _tools().stat.S_ISREG(before.st_mode)
            or _tools().stat.S_ISLNK(before.st_mode)
            or attributes & getattr(_tools().stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or before.st_size > 512 * 1024 * 1024
        ):
            raise PermissionError(f"{label} must be one bounded ordinary file")
        digest = _tools().hashlib.sha256()
        try:
            with candidate.open("rb") as stream:
                opened = _tools().os.fstat(stream.fileno())
                if (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                ) != (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                ):
                    raise PermissionError(f"{label} changed before it was opened")
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                after = _tools().os.fstat(stream.fileno())
        except PermissionError:
            raise
        except OSError:
            raise PermissionError(f"{label} could not be fingerprinted safely") from None
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise PermissionError(f"{label} changed while it was fingerprinted")
        return {
            "path": str(candidate),
            "bytes": int(before.st_size),
            "sha256": digest.hexdigest(),
        }

    def _dependency_declaration_summary(
        self,
        working_directory: _tools().Path,
    ) -> tuple[list[str], int]:
        """Return bounded human-readable direct declarations plus their total count."""
        declarations: list[str] = []
        requirement = next((
            item for item in (
                self._dependency_manifest(working_directory, "requirements.lock"),
                self._dependency_manifest(working_directory, "requirements.txt"),
            ) if item is not None
        ), None)
        if requirement is not None:
            allowed_hash = _tools().re.compile(r"--hash=sha256:[0-9a-fA-F]{64}\b")
            for raw_line in requirement.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.endswith("\\"):
                    line = line[:-1].rstrip()
                declaration = allowed_hash.sub("", line).strip()
                if declaration:
                    declarations.append(f"python: {declaration}")

        package = self._dependency_manifest(working_directory, "package.json")
        if package is not None:
            payload = _tools().json.loads(package.read_text(encoding="utf-8"))
            for field in (
                "dependencies", "devDependencies", "optionalDependencies",
                "peerDependencies",
            ):
                values = payload.get(field, {})
                for package_name, specifier in sorted(values.items()):
                    declarations.append(f"node/{field}: {package_name}@{specifier}")
        return declarations[:8], len(declarations)

    def _stable_dependency_manifest(
        self,
        working_directory: _tools().Path,
        name: str,
    ) -> tuple[_tools().Path, bytes, dict[str, _tools().Any]] | None:
        """Read and validate one manifest through a stable ordinary-file handle."""
        candidate = self._dependency_manifest(working_directory, name)
        if candidate is None:
            return None
        before = _tools().os.lstat(candidate)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_nlink,
        )
        try:
            with candidate.open("rb") as stream:
                opened = _tools().os.fstat(stream.fileno())
                if (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_nlink,
                ) != identity:
                    raise PermissionError(
                        f"Dependency manifest changed before it was opened: {name}"
                    )
                raw = stream.read(64 * 1024 * 1024 + 1)
                after = _tools().os.fstat(stream.fileno())
        except PermissionError:
            raise
        except OSError:
            raise PermissionError(
                f"Dependency manifest could not be read safely: {name}"
            ) from None
        if len(raw) > 64 * 1024 * 1024 or (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_nlink,
        ) != identity:
            raise PermissionError(f"Dependency manifest changed while it was read: {name}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(
                f"Dependency manifest must be readable UTF-8 text: {name}"
            ) from None
        if name in {"requirements.lock", "requirements.txt"}:
            self._validate_requirements_manifest(name, text)
        else:
            self._validate_node_dependency_manifest(name, text)
        return candidate, raw, {
            "name": name,
            "bytes": len(raw),
            "sha256": _tools().hashlib.sha256(raw).hexdigest(),
        }

    def _create_dependency_staging_snapshot(
        self,
        working_directory: _tools().Path,
    ) -> tuple[_tools().Path, dict[str, dict[str, _tools().Any]]]:
        """Copy only validated manifests into a private immutable-input directory."""
        runtime = self.config.data_dir.resolve() / "runtime"
        stage_root = runtime / "dependency-staging"
        stage_root.mkdir(parents=True, exist_ok=True)
        workspace = self.config.workspace.resolve(strict=True)
        resolved_root = stage_root.resolve(strict=True)
        if resolved_root != stage_root:
            raise PermissionError("Dependency staging may not traverse links or reparse points")
        if resolved_root.is_relative_to(workspace):
            raise PermissionError(
                "Dependency staging must be outside the model-writable workspace"
            )
        details = _tools().os.lstat(resolved_root)
        attributes = getattr(details, "st_file_attributes", 0)
        if (
            not _tools().stat.S_ISDIR(details.st_mode)
            or _tools().stat.S_ISLNK(details.st_mode)
            or attributes & getattr(_tools().stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise PermissionError("Dependency staging must be an ordinary directory")
        stage = _tools().Path(_tools().tempfile.mkdtemp(prefix="install-", dir=resolved_root))
        try:
            if _tools().os.stat(stage).st_dev != _tools().os.stat(working_directory).st_dev:
                raise PermissionError(
                    "Dependency staging and workspace must share one filesystem"
                )
            expected: dict[str, dict[str, _tools().Any]] = {}
            for name in (
                "requirements.lock", "requirements.txt",
                "npm-shrinkwrap.json", "package-lock.json", "package.json",
            ):
                record = self._stable_dependency_manifest(working_directory, name)
                if record is None:
                    continue
                _, raw, metadata = record
                destination = stage / name
                with destination.open("xb") as output:
                    output.write(raw)
                    output.flush()
                    _tools().os.fsync(output.fileno())
                expected[name] = metadata
            npmrc = stage / ".npmrc"
            with npmrc.open("xb"):
                pass
            expected[".npmrc"] = {
                "name": ".npmrc",
                "bytes": 0,
                "sha256": _tools().hashlib.sha256(b"").hexdigest(),
            }
            self._assert_dependency_staging_snapshot(stage, expected)
            return stage, expected
        except Exception:
            _tools().shutil.rmtree(stage, ignore_errors=True)
            raise

    @staticmethod
    def _assert_dependency_staging_snapshot(
        stage: _tools().Path,
        expected: dict[str, dict[str, _tools().Any]],
    ) -> None:
        """Recheck every staged input immediately before a package manager runs."""
        for name, record in expected.items():
            candidate = stage / name
            details = _tools().os.lstat(candidate)
            attributes = getattr(details, "st_file_attributes", 0)
            if (
                not _tools().stat.S_ISREG(details.st_mode)
                or _tools().stat.S_ISLNK(details.st_mode)
                or attributes & getattr(_tools().stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or details.st_nlink > 1
                or details.st_size != record["bytes"]
            ):
                raise PermissionError("A staged dependency input changed before execution")
            digest = _tools().hashlib.sha256(candidate.read_bytes()).hexdigest()
            if digest != record["sha256"]:
                raise PermissionError("A staged dependency input changed before execution")

    def _assert_dependency_staging_matches_approval(
        self,
        expected: dict[str, dict[str, _tools().Any]],
    ) -> None:
        """Bind immutable staged bytes directly to the operator-approved tree."""
        approved = self._approved_arguments_for("install_project_dependencies")
        if not approved:
            return
        records = [
            expected[name]
            for name in (
                "requirements.lock", "requirements.txt", "npm-shrinkwrap.json",
                "package-lock.json", "package.json",
            )
            if name in expected
        ]
        if approved.get("dependency_manifest_count") != len(records):
            raise PermissionError("Staged dependency inputs do not match approval")
        for index, record in enumerate(records, start=1):
            descriptor = (
                f"{record['name']} | {record['bytes']} bytes | "
                f"sha256:{record['sha256']}"
            )
            if approved.get(f"dependency_manifest_{index:02d}") != descriptor:
                raise PermissionError("Staged dependency inputs do not match approval")
        canonical = _tools().json.dumps(
            records, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if approved.get("dependency_tree_sha256") != _tools().hashlib.sha256(
            canonical
        ).hexdigest():
            raise PermissionError("Staged dependency inputs do not match approval")

    def _assert_dependency_source_matches_staging(
        self,
        working_directory: _tools().Path,
        expected: dict[str, dict[str, _tools().Any]],
    ) -> None:
        """Reject workspace manifest/config drift without letting managers consume it."""
        self._reject_project_dependency_config(working_directory)
        manifest_names = {
            "requirements.lock", "requirements.txt", "npm-shrinkwrap.json",
            "package-lock.json", "package.json",
        }
        expected_names = set(expected) & manifest_names
        current_names: set[str] = set()
        for name in manifest_names:
            record = self._stable_dependency_manifest(working_directory, name)
            if record is None:
                continue
            current_names.add(name)
            if name not in expected_names or record[2] != expected[name]:
                raise PermissionError("A dependency manifest changed after approval")
        if current_names != expected_names:
            raise PermissionError("A dependency manifest changed after approval")

    @staticmethod
    def _publish_staged_node_modules(stage: _tools().Path, working_directory: _tools().Path) -> None:
        """Atomically replace workspace node_modules with the verified manager output."""
        source = stage / "node_modules"
        target = working_directory / "node_modules"
        backup = stage / "previous-node_modules"
        if _tools().os.path.lexists(source):
            source_details = _tools().os.lstat(source)
            source_attributes = getattr(source_details, "st_file_attributes", 0)
            if (
                not _tools().stat.S_ISDIR(source_details.st_mode)
                or _tools().stat.S_ISLNK(source_details.st_mode)
                or source_attributes & getattr(_tools().stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise PermissionError("npm produced an unsafe node_modules root")
        if _tools().os.path.lexists(target):
            target_details = _tools().os.lstat(target)
            target_attributes = getattr(target_details, "st_file_attributes", 0)
            if (
                not _tools().stat.S_ISDIR(target_details.st_mode)
                or _tools().stat.S_ISLNK(target_details.st_mode)
                or target_attributes & getattr(_tools().stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise PermissionError("Existing node_modules is not an ordinary directory")
            _tools().os.replace(target, backup)
        try:
            if _tools().os.path.lexists(source):
                _tools().os.replace(source, target)
        except Exception:
            if _tools().os.path.lexists(backup) and not _tools().os.path.lexists(target):
                _tools().os.replace(backup, target)
            raise
        if _tools().os.path.lexists(backup):
            _tools().shutil.rmtree(backup)

    def _dependency_install_snapshot(self, cwd: str) -> dict[str, _tools().Any]:
        working_directory = _tools()._safe_target(self.config.workspace, cwd)
        if not working_directory.is_dir():
            raise NotADirectoryError(cwd)
        self._reject_project_dependency_config(working_directory)
        records: list[dict[str, _tools().Any]] = []
        for name in (
            "requirements.lock", "requirements.txt",
            "npm-shrinkwrap.json", "package-lock.json", "package.json",
        ):
            manifest = self._stable_dependency_manifest(working_directory, name)
            if manifest is None:
                continue
            records.append(manifest[2])
        if not records:
            raise FileNotFoundError(
                "No safe dependency manifest found "
                "(requirements.lock, requirements.txt, or package.json)"
            )
        canonical = _tools().json.dumps(
            records, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        snapshot: dict[str, _tools().Any] = {
            "resolved_cwd": str(working_directory),
            "dependency_manifest_count": len(records),
            "dependency_tree_sha256": _tools().hashlib.sha256(canonical).hexdigest(),
            "dependency_network_access": True,
            "dependency_host_authority": True,
            "node_lifecycle_scripts": "disabled",
        }
        for index, record in enumerate(records, start=1):
            snapshot[f"dependency_manifest_{index:02d}"] = (
                f"{record['name']} | {record['bytes']} bytes | sha256:{record['sha256']}"
            )
        summaries, declaration_count = self._dependency_declaration_summary(
            working_directory
        )
        snapshot["dependency_declaration_count"] = declaration_count
        snapshot["dependency_summary_omitted_count"] = max(
            0, declaration_count - len(summaries)
        )
        for index, declaration in enumerate(summaries, start=1):
            snapshot[f"dependency_{index:02d}"] = declaration

        if any(record["name"] == "package.json" for record in records):
            npm_command = _tools()._program_command("npm", [], self.config.workspace)
            if len(npm_command) != 2:
                raise PermissionError("The trusted npm executor shape is invalid")
            node = self._dependency_executor_fingerprint(
                _tools().Path(npm_command[0]), "Node.js executable"
            )
            npm_cli = self._dependency_executor_fingerprint(
                _tools().Path(npm_command[1]), "npm entry point"
            )
            for prefix, fingerprint in (("node", node), ("npm_cli", npm_cli)):
                snapshot[f"dependency_{prefix}_path"] = fingerprint["path"]
                snapshot[f"dependency_{prefix}_bytes"] = fingerprint["bytes"]
                snapshot[f"dependency_{prefix}_sha256"] = fingerprint["sha256"]
        return snapshot

    def _assert_approved_dependency_snapshot(
        self,
        working_directory: _tools().Path,
    ) -> None:
        """Rebind approved manifest/executor bytes immediately before execution."""
        approved = self._approved_arguments_for("install_project_dependencies")
        if not approved:
            return
        relative = working_directory.resolve(strict=True).relative_to(
            self.config.workspace.resolve(strict=True)
        )
        current = self._dependency_install_snapshot(
            relative.as_posix() if relative.parts else "."
        )
        if any(approved.get(key) != value for key, value in current.items()):
            raise PermissionError(
                "A dependency manifest or executor changed after approval"
            )

    def _run_dependency_command(
        self,
        command: list[str],
        working_directory: _tools().Path,
        timeout: int,
    ) -> dict[str, _tools().Any]:
        environment = _tools()._minimal_environment(self.config.data_dir)
        environment.update({
            "CI": "true",
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_FUND": "false",
            "NPM_CONFIG_GLOBAL": "false",
            "NPM_CONFIG_IGNORE_SCRIPTS": "true",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "PIP_CONFIG_FILE": _tools().os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
        })
        creation_flags = 0
        popen_options: dict[str, _tools().Any] = {}
        if _tools().os.name == "nt":
            creation_flags = (
                _tools().subprocess.CREATE_NEW_PROCESS_GROUP | _tools().subprocess.CREATE_NO_WINDOW | 0x00000004
            )
        else:
            popen_options["start_new_session"] = True
        started = _tools().time.perf_counter()
        process = _tools().subprocess.Popen(
            command,
            cwd=working_directory,
            stdin=_tools().subprocess.DEVNULL,
            stdout=_tools().subprocess.PIPE,
            stderr=_tools().subprocess.PIPE,
            env=environment,
            creationflags=creation_flags,
            **popen_options,
        )
        job = _tools()._WindowsJob(process)
        if _tools().os.name == "nt":
            try:
                if job.handle is None:
                    raise RuntimeError("Could not attach the dependency process containment job")
                _tools()._resume_windows_process(process)
            except Exception:
                _tools()._terminate_process_tree(process, job)
                try:
                    process.wait(timeout=5)
                except _tools().subprocess.TimeoutExpired:
                    pass
                raise
        if process.stdout is None or process.stderr is None:
            _tools()._terminate_process_tree(process, job)
            job.close()
            raise RuntimeError("Dependency process output pipes were not created")
        stdout = _tools()._OutputCollector(process.stdout)
        stderr = _tools()._OutputCollector(process.stderr)
        stdout.start()
        stderr.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except _tools().subprocess.TimeoutExpired:
            timed_out = True
            _tools()._terminate_process_tree(process, job)
            process.wait(timeout=15)
        finally:
            job.close()
        result = {
            "command": [_tools().Path(command[0]).name, *command[1:]],
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "duration_seconds": round(_tools().time.perf_counter() - started, 3),
            "stdout": _tools().redact_secrets(
                _tools()._trim(stdout.finish(), _tools().MAX_DEPENDENCY_STEP_OUTPUT)
            ),
            "stderr": _tools().redact_secrets(
                _tools()._trim(stderr.finish(), _tools().MAX_DEPENDENCY_STEP_OUTPUT)
            ),
        }
        if timed_out:
            result["error"] = "Dependency command exceeded the shared wall-clock limit"
        return result

    def install_project_dependencies(
        self,
        cwd: str = ".",
        timeout: int | None = None,
    ) -> dict[str, _tools().Any]:
        self._require_process_execution()
        if self.config.external_access != "trusted-external":
            raise PermissionError("Dependency network access is disabled")
        if not isinstance(self._execution_backend, _tools().HostBackend):
            raise PermissionError(
                "Dependency installation is not available in the ephemeral Docker backend"
            )
        if self.config.autonomy == "readonly":
            raise PermissionError("Dependency installation is disabled in readonly mode")
        limit = self.config.command_timeout if timeout is None else timeout
        if isinstance(limit, bool) or not isinstance(limit, int) or not 5 <= limit <= 600:
            raise ValueError("timeout must be an integer between 5 and 600")
        working_directory = _tools()._safe_target(self.config.workspace, cwd)
        if not working_directory.is_dir():
            raise NotADirectoryError(cwd)
        self._reject_project_dependency_config(working_directory)
        if not self._dependency_install_lock.acquire(
            timeout=min(1.0, float(limit))
        ):
            raise RuntimeError("Another project dependency installation is already running")
        staging_directory: _tools().Path | None = None
        try:
            self._assert_approved_dependency_snapshot(working_directory)
            staging_directory, staged_inputs = self._create_dependency_staging_snapshot(
                working_directory
            )
            self._assert_dependency_staging_matches_approval(staged_inputs)
            self._assert_dependency_source_matches_staging(
                working_directory, staged_inputs
            )
            requirement = next((
                item for item in (
                    staging_directory / "requirements.lock",
                    staging_directory / "requirements.txt",
                ) if item.name in staged_inputs
            ), None)
            package = (
                staging_directory / "package.json"
                if "package.json" in staged_inputs
                else None
            )
            npm_lock = next((
                item for item in (
                    staging_directory / "npm-shrinkwrap.json",
                    staging_directory / "package-lock.json",
                ) if item.name in staged_inputs
            ), None)
            manifests = [
                item.name for item in (requirement, package, npm_lock)
                if item is not None
            ]
            has_python = requirement is not None
            has_node = package is not None
            if not has_python and not has_node:
                raise FileNotFoundError(
                    "No safe dependency manifest found "
                    "(requirements.lock, requirements.txt, or package.json)"
                )

            deadline = _tools().time.monotonic() + limit
            steps: list[dict[str, _tools().Any]] = []

            def run_step(phase: str, command: list[str]) -> bool:
                remaining = deadline - _tools().time.monotonic()
                if remaining <= 0:
                    steps.append({
                        "phase": phase,
                        "command": [_tools().Path(command[0]).name, *command[1:]],
                        "exit_code": None,
                        "timed_out": True,
                        "stdout": "",
                        "stderr": "",
                        "error": "Dependency setup exhausted its shared wall-clock limit",
                    })
                    return False
                self._assert_approved_dependency_snapshot(working_directory)
                self._assert_dependency_source_matches_staging(
                    working_directory, staged_inputs
                )
                self._assert_dependency_staging_snapshot(
                    staging_directory, staged_inputs
                )
                self._assert_dependency_staging_matches_approval(staged_inputs)
                result = self._run_dependency_command(
                    command,
                    staging_directory,
                    max(1, min(600, int(remaining + 0.999))),
                )
                # Package managers never consume the mutable workspace inputs.
                # If those inputs drifted while a manager ran, fail before a
                # ready marker or staged node_modules can be published.
                self._assert_dependency_source_matches_staging(
                    working_directory, staged_inputs
                )
                self._assert_dependency_staging_snapshot(
                    staging_directory, staged_inputs
                )
                result["phase"] = phase
                steps.append(result)
                return result["exit_code"] == 0 and not result["timed_out"]

            environment_path: _tools().Path | None = None
            if has_python:
                environment_path = self._project_environment(working_directory, create=True)
                interpreter = self._venv_python(environment_path)
                ready_marker = environment_path / ".jarvis-ready"
                if ready_marker.exists():
                    ready_marker.unlink()
                if not interpreter.is_file():
                    if not run_step(
                        "python-venv",
                        [str(_tools().Path(_tools().sys.executable).resolve()), "-m", "venv", str(environment_path)],
                    ):
                        return self._dependency_install_result(
                            working_directory, manifests, steps, environment_path, requirement, npm_lock
                        )
                if not interpreter.is_file():
                    steps.append({
                        "phase": "python-venv-verification",
                        "exit_code": None,
                        "timed_out": False,
                        "stdout": "",
                        "stderr": "",
                        "error": "Python reported success but the virtual environment interpreter is missing",
                    })
                    return self._dependency_install_result(
                        working_directory, manifests, steps, environment_path, requirement, npm_lock
                    )
                pip_arguments = [
                    "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                ]
                pip_arguments.append("--only-binary=:all:")
                if requirement.name == "requirements.lock":
                    pip_arguments.append("--require-hashes")
                pip_arguments.extend(["-r", requirement.name])
                self._assert_approved_dependency_snapshot(working_directory)
                if not run_step("python-dependencies", [str(interpreter.resolve()), *pip_arguments]):
                    return self._dependency_install_result(
                        working_directory, manifests, steps, environment_path, requirement, npm_lock
                    )
                with ready_marker.open("x", encoding="ascii", newline="\n") as marker:
                    marker.write("ready\n")

            if has_node:
                npm_arguments = [
                    "ci" if npm_lock is not None else "install",
                    "--ignore-scripts", "--no-audit", "--no-fund",
                ]
                try:
                    self._assert_approved_dependency_snapshot(working_directory)
                    npm_command = _tools()._program_command("npm", npm_arguments, self.config.workspace)
                    approved = self._approved_arguments_for(
                        "install_project_dependencies"
                    )
                    if approved:
                        if len(npm_command) < 2:
                            raise PermissionError(
                                "The approved npm executor shape changed"
                            )
                        for prefix, current_path, label in (
                            ("node", npm_command[0], "Node.js executable"),
                            ("npm_cli", npm_command[1], "npm entry point"),
                        ):
                            current = self._dependency_executor_fingerprint(
                                _tools().Path(current_path), label
                            )
                            expected = {
                                "path": approved.get(f"dependency_{prefix}_path"),
                                "bytes": approved.get(f"dependency_{prefix}_bytes"),
                                "sha256": approved.get(f"dependency_{prefix}_sha256"),
                            }
                            if current != expected:
                                raise PermissionError(
                                    "The dependency executor changed after approval"
                                )
                except Exception as exc:
                    steps.append({
                        "phase": "node-dependencies",
                        "exit_code": None,
                        "timed_out": False,
                        "stdout": "",
                        "stderr": "",
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    return self._dependency_install_result(
                        working_directory, manifests, steps, environment_path, requirement, npm_lock
                    )
                if run_step("node-dependencies", npm_command):
                    self._publish_staged_node_modules(
                        staging_directory, working_directory
                    )

            return self._dependency_install_result(
                working_directory, manifests, steps, environment_path, requirement, npm_lock
            )
        finally:
            if staging_directory is not None:
                _tools().shutil.rmtree(staging_directory, ignore_errors=True)
            self._dependency_install_lock.release()

    def _dependency_install_result(
        self,
        working_directory: _tools().Path,
        manifests: list[str],
        steps: list[dict[str, _tools().Any]],
        environment_path: _tools().Path | None,
        requirement: _tools().Path | None,
        npm_lock: _tools().Path | None,
    ) -> dict[str, _tools().Any]:
        success = bool(steps) and all(
            step.get("exit_code") == 0 and not step.get("timed_out", False)
            for step in steps
        )
        return {
            "success": success,
            "cwd": str(working_directory.relative_to(self.config.workspace)).replace("\\", "/") or ".",
            "manifests": manifests,
            "lockfiles": [
                item.name for item in (requirement, npm_lock)
                if item is not None and (
                    item.name.endswith(".lock")
                    or item.name in {"package-lock.json", "npm-shrinkwrap.json"}
                )
            ],
            "python_environment": str(environment_path) if environment_path is not None else None,
            "steps": steps,
        }
