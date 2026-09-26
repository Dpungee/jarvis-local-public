"""Current ToolBox methods, mechanically extracted by domain.

Shared globals are resolved through jarvis.tools to preserve runtime patches.
"""
from __future__ import annotations


def _tools():
    from . import tools

    return tools


class DocumentMediaToolsMixin:
    def build_document(
        self,
        path: str,
        document_type: str,
        content: str,
    ) -> dict[str, _tools().Any]:
        """Build a verified office/PDF artifact without model-authored generator code."""
        if self.config.autonomy == "readonly":
            raise PermissionError("Document creation is disabled in readonly mode")
        kind = str(document_type).strip().casefold()
        if kind not in _tools().SUPPORTED_DOCUMENT_TYPES:
            raise ValueError("Document type must be pptx, docx, xlsx, or pdf")
        encoded = str(content).encode("utf-8")
        if len(encoded) > _tools().MAX_FILE_BYTES:
            raise ValueError("Document source exceeds the 2 MB content limit")
        # Validate the output before creating the temporary source. The offline
        # builder repeats this check and atomically installs a new file only.
        _tools()._safe_target(self.config.workspace, path)
        source_path: _tools().Path | None = None
        content_text = str(content)
        source_suffix = ".md"
        try:
            structured_content = _tools().json.loads(content_text)
        except (_tools().json.JSONDecodeError, TypeError, ValueError):
            structured_content = None
        if isinstance(structured_content, dict):
            source_suffix = ".json"
        try:
            with _tools().tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                suffix=source_suffix,
                prefix=".jarvis-document-source-",
                dir=self.config.workspace,
                delete=False,
            ) as stream:
                stream.write(content_text)
                source_path = _tools().Path(stream.name)
            source = source_path.relative_to(self.config.workspace).as_posix()
            result = _tools().build_offline_document(
                self.config.workspace,
                source,
                path,
                kind,
            )
            result["verified"] = True
            return result
        finally:
            if source_path is not None:
                source_path.unlink(missing_ok=True)

    def build_document_preview(
        self,
        source: str,
        output: str,
    ) -> dict[str, _tools().Any]:
        if self.config.autonomy == "readonly":
            raise PermissionError("Document preview creation is disabled in readonly mode")
        return _tools().build_document_preview(
            self.config.workspace,
            source,
            output,
        )

    def image_visual_qa(self, path: str) -> dict[str, _tools().Any]:
        target = _tools()._safe_target(self.config.workspace, path)
        before = target.stat()
        if not target.is_file():
            raise FileNotFoundError(path)
        if before.st_nlink > 1:
            raise PermissionError("Hard-linked image files are blocked")
        if before.st_size > _tools().MAX_IMAGE_BYTES:
            raise ValueError(
                f"Image exceeds the {_tools().MAX_IMAGE_BYTES // (1024 * 1024)} MiB limit"
            )
        attachment = _tools().ImageAttachment.from_path(target)
        after = target.stat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PermissionError("Image changed while it was being inspected")
        result = _tools().inspect_image_attachment(attachment)
        result["path"] = str(target.relative_to(self.config.workspace))
        return result

    def image_generation_status(self) -> dict[str, _tools().Any]:
        status = dict(self.openai_images.status())
        enabled = bool(
            getattr(self.config, "cloud_enabled", True)
            and getattr(self.config, "openai_images_enabled", False)
        )
        status["enabled"] = enabled
        if not enabled:
            status["configured"] = False
            status["next_action"] = (
                "Enable JARVIS_CLOUD_ENABLED and JARVIS_OPENAI_IMAGES_ENABLED"
            )
        return status

    def _prepare_image_output(self, output: str) -> None:
        if self.config.autonomy == "readonly":
            raise PermissionError("Image generation is disabled in readonly mode")
        if not (
            getattr(self.config, "cloud_enabled", True)
            and getattr(self.config, "openai_images_enabled", False)
        ):
            raise PermissionError("OpenAI image generation is disabled")
        if not bool(self.openai_images.status().get("configured")):
            raise PermissionError(
                "OpenAI Images is not configured; set OPENAI_API_KEY outside the workspace"
            )
        target = _tools()._mutable_workspace_target(self.config.workspace, output)
        target.parent.mkdir(parents=True, exist_ok=True)

    def generate_image(
        self,
        prompt: str,
        output: str,
        output_format: str = "png",
        size: str = "auto",
        quality: str = "auto",
    ) -> dict[str, _tools().Any]:
        self._prepare_image_output(output)
        return self.openai_images.generate(
            prompt,
            output,
            output_format=output_format,
            size=size,
            quality=quality,
        )

    def edit_attached_image(
        self,
        attachment_index: int,
        prompt: str,
        output: str,
        output_format: str = "png",
        size: str = "auto",
        quality: str = "auto",
    ) -> dict[str, _tools().Any]:
        attachments = self._active_image_attachments.get()
        if not 1 <= int(attachment_index) <= len(attachments):
            raise ValueError("Attached image index is not available in this request")
        self._prepare_image_output(output)
        source = attachments[int(attachment_index) - 1]
        _tools().inspect_image_attachment(source)
        return self.openai_images.edit_bytes(
            source.data,
            source.mime,
            source.name,
            prompt,
            output,
            output_format=output_format,
            size=size,
            quality=quality,
        )
