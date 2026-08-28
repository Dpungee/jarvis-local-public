from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import jarvis.openai_images as images
from jarvis.openai_images import (
    MODEL,
    OpenAIImageConfigurationError,
    OpenAIImageError,
    OpenAIImageValidationError,
    OpenAIImagesProvider,
)
from jarvis.attachments import MAX_IMAGE_BYTES


def image_bytes(image_format: str = "PNG") -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (8, 6), "purple").save(stream, format=image_format)
    return stream.getvalue()


class FakeResponse:
    def __init__(self, value: bytes) -> None:
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.value if limit < 0 else self.value[:limit]


class RecordingOpener:
    def __init__(self, artifact: bytes) -> None:
        self.artifact = artifact
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        payload = json.dumps({
            "created": 1,
            "data": [{"b64_json": base64.b64encode(self.artifact).decode("ascii")}],
        }).encode("utf-8")
        return FakeResponse(payload)


class OpenAIImagesGatewayTests(unittest.TestCase):
    def test_provider_output_limit_matches_inline_preview_limit(self):
        self.assertEqual(images.MAX_OUTPUT_BYTES, MAX_IMAGE_BYTES)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="jarvis-openai-images-")
        self.workspace = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_status_is_clear_and_never_exposes_key(self):
        provider = OpenAIImagesProvider(self.workspace)
        with patch.dict(os.environ, {}, clear=True):
            status = provider.status()
            self.assertFalse(status["configured"])
            self.assertIn("OPENAI_API_KEY", status["next_action"])
            with self.assertRaises(OpenAIImageConfigurationError):
                provider.generate("A small blue square", "square.png")

        secret = "sk-test-secret-value"
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=True):
            self.assertTrue(provider.status()["configured"])
            self.assertNotIn(secret, repr(provider))
            self.assertNotIn(secret, repr(provider.status()))

    def test_generate_posts_bounded_contract_and_atomically_writes_verified_png(self):
        artifact = image_bytes()
        opener = RecordingOpener(artifact)
        provider = OpenAIImagesProvider(self.workspace, opener=opener)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
            result = provider.generate(
                "A small purple rectangle", "result.png", size="1024x1024", quality="low"
            )

        self.assertEqual((self.workspace / "result.png").read_bytes(), artifact)
        self.assertEqual(result["model"], MODEL)
        self.assertEqual((result["width"], result["height"]), (8, 6))
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/images/generations")
        sent = json.loads(request.data)
        self.assertEqual(sent, {
            "model": MODEL,
            "prompt": "A small purple rectangle",
            "n": 1,
            "output_format": "png",
            "size": "1024x1024",
            "quality": "low",
        })
        self.assertEqual(timeout, 120.0)
        self.assertEqual(list(self.workspace.glob(".jarvis-image-*")), [])

    def test_edit_sends_exactly_one_validated_source_in_multipart(self):
        source = image_bytes()
        (self.workspace / "source.png").write_bytes(source)
        opener = RecordingOpener(source)
        provider = OpenAIImagesProvider(self.workspace, opener=opener)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
            result = provider.edit(
                "source.png", "Remove the background", "edited.png", quality="medium"
            )

        request, _timeout = opener.calls[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/images/edits")
        content_type = request.headers["Content-type"]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary=jarvis-"))
        self.assertEqual(request.data.count(source), 1)
        self.assertIn(b'name="model"', request.data)
        self.assertIn(MODEL.encode(), request.data)
        self.assertIn(b'name="image"; filename="source.png"', request.data)
        self.assertEqual(result["relative_path"], "edited.png")

    def test_edit_bytes_never_persists_private_source(self):
        source = image_bytes()
        opener = RecordingOpener(source)
        provider = OpenAIImagesProvider(self.workspace, opener=opener)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
            result = provider.edit_bytes(
                source,
                "image/png",
                "private-screen.png",
                "Remove the background",
                "result.png",
            )
        self.assertEqual(result["relative_path"], "result.png")
        self.assertFalse((self.workspace / "private-screen.png").exists())
        self.assertEqual(sorted(path.name for path in self.workspace.iterdir()), ["result.png"])
        self.assertEqual(opener.calls[0][0].data.count(source), 1)

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
            with self.assertRaisesRegex(OpenAIImageValidationError, "name is invalid"):
                provider.edit_bytes(
                    source, "image/png", "../secret.png", "valid prompt", "bad.png"
                )

    def test_paths_formats_and_collisions_fail_before_network(self):
        opener = RecordingOpener(image_bytes())
        provider = OpenAIImagesProvider(self.workspace, opener=opener)
        (self.workspace / "exists.png").write_bytes(image_bytes())
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
            with self.assertRaises(OpenAIImageValidationError):
                provider.generate("valid prompt", "../escape.png")
            with self.assertRaises(OpenAIImageValidationError):
                provider.generate("valid prompt", "wrong.png", output_format="webp")
            with self.assertRaises(OpenAIImageValidationError):
                provider.generate("valid prompt", "exists.png")
            with self.assertRaises(OpenAIImageValidationError):
                provider.generate("valid prompt", "new.png", size="2048x2048")
        self.assertEqual(opener.calls, [])

    def test_edit_rejects_missing_corrupt_and_escaping_sources_before_network(self):
        opener = RecordingOpener(image_bytes())
        provider = OpenAIImagesProvider(self.workspace, opener=opener)
        (self.workspace / "corrupt.png").write_bytes(b"not a png")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
            for source in ("missing.png", "corrupt.png", "../outside.png"):
                with self.subTest(source=source), self.assertRaises(
                    OpenAIImageValidationError
                ):
                    provider.edit(source, "valid prompt", "edited.png")
        self.assertEqual(opener.calls, [])

    def test_invalid_or_multiple_response_images_are_rejected_without_artifacts(self):
        responses = (
            {"data": []},
            {"data": [{"b64_json": "%%%"}]},
            {"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]},
            {"data": [
                {"b64_json": base64.b64encode(image_bytes()).decode()},
                {"b64_json": base64.b64encode(image_bytes()).decode()},
            ]},
        )
        for index, response in enumerate(responses):
            with self.subTest(index=index):
                opener = lambda *_args, value=response, **_kwargs: FakeResponse(
                    json.dumps(value).encode()
                )
                provider = OpenAIImagesProvider(self.workspace, opener=opener)
                output = f"bad-{index}.png"
                with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
                    with self.assertRaises(OpenAIImageError):
                        provider.generate("valid prompt", output)
                self.assertFalse((self.workspace / output).exists())

    def test_transport_errors_are_sanitized(self):
        secret = "sk-test-do-not-leak"

        def failed(*_args, **_kwargs):
            raise urllib.error.URLError(f"upstream contained {secret}")

        provider = OpenAIImagesProvider(self.workspace, opener=failed)
        with patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=True):
            with self.assertRaises(OpenAIImageError) as caught:
                provider.generate("valid prompt", "never.png")
        self.assertNotIn(secret, str(caught.exception))
        self.assertFalse((self.workspace / "never.png").exists())

    def test_response_and_base64_bounds_fail_closed(self):
        oversized_response = lambda *_args, **_kwargs: FakeResponse(b"x" * 21)
        provider = OpenAIImagesProvider(self.workspace, opener=oversized_response)
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True),
            patch.object(images, "MAX_RESPONSE_BYTES", 20),
            self.assertRaisesRegex(OpenAIImageError, "safe limit"),
        ):
            provider.generate("valid prompt", "response.png")

        encoded_response = lambda *_args, **_kwargs: FakeResponse(
            json.dumps({"data": [{"b64_json": "A" * 12}]}).encode()
        )
        provider = OpenAIImagesProvider(self.workspace, opener=encoded_response)
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True),
            patch.object(images, "MAX_OUTPUT_BYTES", 4),
            self.assertRaisesRegex(OpenAIImageError, "invalid image data"),
        ):
            provider.generate("valid prompt", "base64.png")
        self.assertFalse((self.workspace / "response.png").exists())
        self.assertFalse((self.workspace / "base64.png").exists())

    def test_racing_output_is_never_overwritten(self):
        artifact = image_bytes()

        def racing(_request, *, timeout):
            self.assertEqual(timeout, 120.0)
            (self.workspace / "race.png").write_bytes(b"operator file")
            return FakeResponse(json.dumps({
                "data": [{"b64_json": base64.b64encode(artifact).decode()}]
            }).encode())

        provider = OpenAIImagesProvider(self.workspace, opener=racing)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=True):
            with self.assertRaisesRegex(OpenAIImageValidationError, "appeared"):
                provider.generate("valid prompt", "race.png")
        self.assertEqual((self.workspace / "race.png").read_bytes(), b"operator file")
        self.assertEqual(list(self.workspace.glob(".jarvis-image-*")), [])


if __name__ == "__main__":
    unittest.main()
