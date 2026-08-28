from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
import importlib.util
import io
from unittest.mock import MagicMock, patch
from pathlib import Path

from jarvis.attachments import (
    MAX_IMAGE_BYTES,
    ImageAttachment,
    MAX_IMAGE_PIXELS,
    inspect_image_attachment,
    validate_image_attachments,
)
from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.presence import PresenceRuntime


PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-image"


class ImageAttachmentTests(unittest.TestCase):
    def test_descriptor_and_repr_never_contain_image_bytes(self):
        image = ImageAttachment("image/png", PNG, "screen.png")

        self.assertEqual(image.descriptor(), {
            "mime": "image/png",
            "bytes": len(PNG),
            "sha256": hashlib.sha256(PNG).hexdigest(),
        })
        self.assertNotIn(base64.b64encode(PNG).decode("ascii"), repr(image))

    def test_count_type_size_and_signature_bounds_fail_closed(self):
        image = ImageAttachment("image/png", PNG)
        with self.assertRaisesRegex(ValueError, "at most 4"):
            validate_image_attachments([image] * 5)
        with self.assertRaisesRegex(ValueError, "not supported"):
            ImageAttachment("image/svg+xml", b"<svg/>")
        with self.assertRaisesRegex(ValueError, "5 MiB"):
            ImageAttachment(
                "image/png",
                b"\x89PNG\r\n\x1a\n" + b"x" * MAX_IMAGE_BYTES,
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            ImageAttachment("image/png", b"not-a-png")

    def test_presence_persists_only_attachment_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            data = root / "data"
            workspace.mkdir()
            data.mkdir()
            config = Config(
                root=root,
                workspace=workspace,
                data_dir=data,
                soul_path=root / "SOUL.md",
                model="auto",
                fast_model="openai:gpt-5.6-luna",
                reasoning_model="openai:gpt-5.6-terra",
                coding_model="openai:gpt-5.6-sol",
                deep_model="openai:gpt-5.6-sol",
                ollama_url="http://127.0.0.1:11434",
                ollama_api_key=None,
                max_steps=5,
                context_length=4096,
                command_timeout=30,
                autonomy="autonomous",
            )
            with Memory(data / "jarvis.db") as memory:
                conversation_id = memory.new_conversation("Vision")
            runtime = PresenceRuntime(config)
            encoded = base64.b64encode(PNG).decode("ascii")

            job_id = runtime.submit(
                conversation_id,
                "What is in this image?",
                "auto",
                [{"name": "screen.png", "mime": "image/png", "data": encoded}],
            )

            with Memory(data / "jarvis.db") as memory:
                row = memory.get_presence_job(job_id)
                serialized_rows = "\n".join(memory.db.iterdump())
            descriptors = json.loads(row["attachments_json"])
            self.assertEqual(descriptors[0]["sha256"], hashlib.sha256(PNG).hexdigest())
            self.assertNotIn(encoded, serialized_rows)
            self.assertNotIn("synthetic-image", serialized_rows)

    def test_queued_image_job_is_not_replayed_without_runtime_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with Memory(data / "jarvis.db") as memory:
                conversation_id = memory.new_conversation("Vision restart")
                image = ImageAttachment("image/png", PNG)
                memory.create_presence_job(
                    "a" * 32,
                    conversation_id=conversation_id,
                    project_id=1,
                    prompt="inspect",
                    model_override="auto",
                    attachments_json=json.dumps([image.descriptor()]),
                )

            with Memory(data / "jarvis.db") as memory:
                recovery = memory.recover_presence_jobs("presence:test")
                row = memory.get_presence_job("a" * 32)

            self.assertEqual(recovery["queued"], [])
            self.assertEqual(row["status"], "interrupted")
            self.assertIn("attach the images again", row["last_error"])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed")
    def test_visual_inspection_decodes_dimensions_and_rejects_pixel_bombs(self):
        from PIL import Image

        stream = io.BytesIO()
        Image.new("RGB", (32, 18), "navy").save(stream, format="PNG")
        metadata = inspect_image_attachment(
            ImageAttachment("image/png", stream.getvalue(), "screen.png")
        )
        self.assertEqual((metadata["width"], metadata["height"]), (32, 18))
        self.assertEqual(metadata["pixels"], 576)
        self.assertFalse(metadata["animated"])

        oversized = MagicMock()
        oversized.__enter__.return_value = oversized
        oversized.size = (MAX_IMAGE_PIXELS + 1, 1)
        oversized.n_frames = 1
        oversized.format = "PNG"
        with patch(
            "PIL.Image.open",
            return_value=oversized,
        ):
            with self.assertRaisesRegex(ValueError, "pixel visual limit"):
                inspect_image_attachment(
                    ImageAttachment("image/png", stream.getvalue(), "huge.png")
                )


if __name__ == "__main__":
    unittest.main()
