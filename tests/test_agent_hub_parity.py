"""Agent Hub ChatGPT/Codex parity: files, images, data analysis, Git/GitHub, regenerate/edit,
feedback, export, personalization, workspace status and tool-row detail.

No provider, OpenRouter or GitHub call is made: models are fake runners, the image model is a
fake HTTP opener and ``gh`` is never started. Git steps run the real ``git`` on a temporary
repository when it is installed (skipped otherwise)."""
import base64
import bz2
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import hub_github, hub_images, hub_packages, hub_uploads
from jarvis.agent_hub import HubAuth, HubHTTPServer, HubService
from jarvis.agent_hub_runtime import (
    DEFAULT_PERMISSIONS,
    PERMISSION_LABELS,
    TaskError,
    allowed_tools,
)
from jarvis.tools import Tool, _serialize_tool_response

KEY = "sk-or-" + "t" * 40
HAS_GIT = shutil.which("git") is not None


def png(color=(200, 30, 30), size=(8, 8), fmt="PNG"):
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format=fmt)
    return out.getvalue()


def b64(data):
    return base64.b64encode(data).decode("ascii")


class Result(str):
    status = "complete"
    model = "codex-cli:gpt-5.6-sol"
    tool_calls = 0


class FakeMemory:
    """The agent-memory calls the Hub makes, plus exact approvals decided by the test."""

    def __init__(self):
        self.conversations = {}
        self.decisions = []
        self.approvals = []

    def conversation_exists(self, conversation_id):
        return conversation_id in self.conversations

    def new_conversation(self, title):
        conversation_id = len(self.conversations) + 1
        self.conversations[conversation_id] = []
        return conversation_id

    def add_message(self, conversation_id, role, content):
        self.conversations[conversation_id].append((role, content))

    def authorize_or_request(self, action, resource, reason, *, approval_scope, task_id=None):
        self.approvals.append({"action": action, "resource": json.loads(resource), "reason": reason,
                               "scope": approval_scope})
        return self.decisions.pop(0) if self.decisions else (True, 1)


class FakeToolbox:
    """Enough of ToolBox: a tools mapping and an execute that serialises like the real one."""

    def __init__(self, tools):
        self.tools = dict(tools)
        self._approval_execution_context = SimpleNamespace(get=lambda: ("hub-test-scope", 1))

    def execute(self, name, arguments):
        tool = self.tools.get(name)
        if tool is None:
            return _serialize_tool_response(False, "error", f"Unknown tool: {name}")
        try:
            return _serialize_tool_response(True, "result", tool.function(**arguments))
        except Exception as exc:  # noqa: BLE001 - mirrors ToolBox.execute
            return _serialize_tool_response(False, "error", f"{type(exc).__name__}: {exc}")


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self, limit=-1):
        return self.payload if limit < 0 else self.payload[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpenRouter:
    """Records image requests and answers with a picture (or a planned failure)."""

    def __init__(self):
        self.requests = []
        self.replies = []

    def image_reply(self, data=None, mime="image/png", text="Here it is."):
        data = png() if data is None else data
        return {"model": "google/gemini-3.1-flash-image", "usage": {"cost": 0.0039},
                "choices": [{"message": {"content": text, "images": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64(data)}"}}]}}]}

    def __call__(self, request, timeout=None):
        self.requests.append({"url": request.full_url, "auth": request.get_header("Authorization"),
                              "body": json.loads(request.data.decode("utf-8"))})
        reply = self.replies.pop(0) if self.replies else self.image_reply()
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(json.dumps(reply).encode("utf-8"))


class ParityBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "profile" / "openrouter").mkdir(parents=True)
        (self.root / "profile" / "openrouter" / "api-key").write_text(KEY, encoding="utf-8")
        self.service = HubService(state_dir=self.root, provider_profile_dir=self.root / "profile",
                                  runtime_kwargs={"autostart": False, "provider_probe": lambda _: {
                                      "installed": True, "authenticated": True, "detail": "t", "version": "t"}})
        self.runtime = self.service.runtime
        self.opener = FakeOpenRouter()
        self.runtime._image_opener = self.opener
        self.runtime._image_catalog_fetch = lambda url: {"data": []}
        self.auth = HubAuth(self.root)
        self.server = HubHTTPServer(("127.0.0.1", 0), self.service, self.auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.memory = FakeMemory()
        self.runs = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()
        self.tmp.cleanup()

    # --------------------------------------------------------------- HTTP
    def raw(self, path, body=None, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.auth.operator_token
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}" + path,
                                     data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def request(self, path, body=None, authenticated=True):
        code, data, _ = self.raw(path, body, authenticated)
        return code, json.loads(data)

    def ok(self, path, body=None):
        code, data = self.request(path, body)
        self.assertEqual(code, 200, data)
        return data

    def agent(self, groups, **extra):
        return self.ok("/api/agents", {"name": "Atlas", "role": "Research", "provider": "codex-cli",
                                       "model": "gpt-5.6-sol", "enable": True, "tool_groups": groups, **extra})

    def new_chat(self, agent_id):
        return self.ok(f"/api/agents/{agent_id}/chats", {"title": "Numbers"})["chat_id"]

    def send(self, agent_id, chat_id, body, **extra):
        return self.request(f"/api/agents/{agent_id}/messages", {
            "chat_id": chat_id, "body": body, "request_id": extra.pop("request_id", str(uuid.uuid4())), **extra})

    def conversation(self, agent_id, chat_id):
        return self.ok(f"/api/agents/{agent_id}/chat?chat={chat_id}")

    def project(self, agent):
        return self.service.project_root(agent["project_id"])

    # ------------------------------------------------------------ running turns
    def execute(self, actions=None, tools=None, text="Done."):
        """Run every queued task with a fake agent that calls tools through the Hub's wrapper."""
        case, runtime = self, self.runtime

        class Runner:
            def __init__(self):
                self.toolbox = FakeToolbox(tools or {})
                self.operator_brief = None

            def run(self, prompt, **kwargs):
                outputs = [action(self.toolbox) for action in (actions or [])]
                case.runs.append({"prompt": prompt, "brief": self.operator_brief, "tools": sorted(self.toolbox.tools),
                                  "outputs": outputs, **kwargs})
                return Result(text)

        with patch.object(runtime, "_agent_config", return_value=SimpleNamespace()), \
                patch.object(runtime, "_open_memory", return_value=self.memory), \
                patch.object(runtime, "_make_client", return_value=SimpleNamespace()), \
                patch.object(runtime, "_make_agent", side_effect=lambda *a, **k: Runner()):
            runtime.dispatch_once()
            deadline = time.monotonic() + 15
            while runtime.running_ids() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(runtime.running_ids())

    @staticmethod
    def file_payload(name, data, mime="application/octet-stream"):
        return {"name": name, "mime": mime, "data": b64(data)}


# =============================================================================== G1
class UploadValidationTests(unittest.TestCase):
    def test_safe_names(self):
        cases = {
            "../../etc/passwd": "passwd", "..\\..\\boot.ini": "boot.ini", "CON.txt": "_CON.txt",
            "com1": "_com1", "lpt9.tar.gz": "_lpt9.tar.gz", " .hidden ": "hidden", "a<b>c:d|e?.csv": "abcde.csv",
            "evil\u202eexe.txt": "evilexe.txt", "tab\tname.md": "tabname.md", "": "file", "...": "file",
            "report.pdf.": "report.pdf",
        }
        for raw, expected in cases.items():
            self.assertEqual(hub_uploads.safe_name(raw), expected, raw)
        long = hub_uploads.safe_name("x" * 400 + ".xlsx")
        self.assertTrue(long.endswith(".xlsx"))
        self.assertLessEqual(len(long), hub_uploads.MAX_NAME_CHARS)

    def test_parse_refusals_and_limits(self):
        good = {"name": "data.csv", "mime": "text/csv", "data": b64(b"a,b\n1,2\n")}
        self.assertEqual(hub_uploads.parse([good])[0].mime, "text/csv")
        # Text that merely starts with "MZ" is not a program; a PE header is.
        mz_text = b"MZ,region,total\n" + b"row,1,2\n" * 20
        self.assertEqual(len(hub_uploads.parse([{"name": "codes.csv", "data": b64(mz_text)}])), 1)
        program = bytearray(b"MZ" + b"\x00" * 126)
        program[0x3C:0x40] = (0x80).to_bytes(4, "little")
        program += b"PE\x00\x00" + b"\x00" * 64
        refused = [
            [good] * (hub_uploads.MAX_UPLOAD_FILES + 1),
            [{"name": "a.txt", "data": "not base64!!"}],
            [{"name": "a.txt", "data": ""}],
            [{"name": "a.txt", "mime": 5, "data": b64(b"x")}],
            [{"name": "a.txt", "data": b64(b"x"), "path": "C:/x"}],
            [{"name": "setup.exe", "data": b64(b"hello")}],
            [{"name": "run.ps1", "data": b64(b"Write-Host hi")}],
            [{"name": "notes.txt", "data": b64(bytes(program))}],
            [{"name": "tool.bin", "data": b64(b"\x7fELF\x02\x01")}],
            [{"name": "backup.7z", "data": b64(b"7z\xbc\xaf\x27\x1c")}],
            [{"name": "a.zip", "data": b64(b"PK\x03\x04 not really a zip")}],
            "not a list",
        ]
        for value in refused:
            with self.assertRaises(hub_uploads.UploadError, msg=str(value)[:80]):
                hub_uploads.parse(value)
        with patch.object(hub_uploads, "MAX_UPLOAD_BYTES", 10):
            with self.assertRaises(hub_uploads.UploadError):
                hub_uploads.parse([{"name": "a.txt", "data": b64(b"x" * 11)}])
        with patch.object(hub_uploads, "MAX_UPLOAD_TOTAL", 15):
            with self.assertRaises(hub_uploads.UploadError):
                hub_uploads.parse([{"name": "a.txt", "data": b64(b"x" * 10)}] * 2)

    def zip_bytes(self, entries, compression=zipfile.ZIP_DEFLATED):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression) as archive:
            for name, data in entries:
                archive.writestr(name, data)
        return out.getvalue()

    def test_archive_guards(self):
        check = hub_uploads.check
        check("ok.zip", self.zip_bytes([("a/readme.md", b"hi"), ("b.csv", b"1,2")]))
        check("report.docx", self.zip_bytes([("word/document.xml", b"<w/>")]))
        bomb = self.zip_bytes([("zeros.bin", b"\x00" * (20 * 1024 * 1024))])
        refusals = {
            "bomb.zip": bomb,
            "slip.zip": self.zip_bytes([("../../outside.txt", b"x")]),
            "abs.zip": self.zip_bytes([("C:/Windows/evil.txt", b"x")]),
            "tools.zip": self.zip_bytes([("bin/setup.exe", b"x")]),
            "fake.docx": bomb,
            "zeros.gz": gzip.compress(b"\x00" * (20 * 1024 * 1024)),
            "zeros.bz2": bz2.compress(b"\x00" * (20 * 1024 * 1024)),
        }
        for name, data in refusals.items():
            with self.assertRaises(hub_uploads.UploadError, msg=name):
                check(name, data)
        tar_out = io.BytesIO()
        with tarfile.open(fileobj=tar_out, mode="w") as archive:
            link = tarfile.TarInfo("link")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
            archive.addfile(link)
        with self.assertRaises(hub_uploads.UploadError):
            check("links.tar", tar_out.getvalue())
        good_tar = io.BytesIO()
        with tarfile.open(fileobj=good_tar, mode="w:gz") as archive:
            info = tarfile.TarInfo("data/a.csv")
            info.size = 3
            archive.addfile(info, io.BytesIO(b"1,2"))
        check("data.tar.gz", good_tar.getvalue())
        with patch.object(hub_uploads, "MAX_ARCHIVE_ENTRIES", 2):
            with self.assertRaises(hub_uploads.UploadError):
                check("many.zip", self.zip_bytes([(f"{i}.txt", b"x") for i in range(3)]))

    def test_save_never_overwrites_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = hub_uploads.parse([{"name": "a.csv", "data": b64(b"1")}, {"name": "a.csv", "data": b64(b"2")}])
            saved = hub_uploads.save(root, uploads, day="2026-09-27")
            self.assertEqual([s["path"] for s in saved], ["uploads/2026-09-27/a.csv", "uploads/2026-09-27/a (2).csv"])
            self.assertEqual((root / "uploads/2026-09-27/a (2).csv").read_bytes(), b"2")
            hub_uploads.remove(root, saved + [{"path": "../outside.txt"}])
            self.assertFalse(any((root / "uploads/2026-09-27").iterdir()))


class UploadRouteTests(ParityBase):
    def setUp(self):
        super().setUp()
        self.agent_view = self.agent(["files_read", "files_write", "run_commands"])
        self.agent_id = self.agent_view["agent_id"]
        self.chat_id = self.new_chat(self.agent_id)

    def test_files_with_a_message_are_saved_recorded_and_named_in_the_brief(self):
        files = [self.file_payload("sales.csv", b"month,total\nJan,10\nFeb,12\n", "text/csv"),
                 self.file_payload("../brief.pdf", b"%PDF-1.4 minimal", "application/pdf")]
        code, task = self.send(self.agent_id, self.chat_id, "Summarise these", files=files)
        self.assertEqual(code, 200, task)
        day = time.strftime("%Y-%m-%d")
        self.assertEqual([f["path"] for f in task["files"]], [f"uploads/{day}/sales.csv", f"uploads/{day}/brief.pdf"])
        self.assertEqual(set(task["files"][0]), {"path", "name", "size", "mime", "artifact_id"})
        root = self.project(self.agent_view)
        self.assertEqual((root / task["files"][0]["path"]).read_bytes(), b"month,total\nJan,10\nFeb,12\n")
        # A second message with the same names never overwrites the first files.
        code, second = self.send(self.agent_id, self.chat_id, "And these", files=files[:1])
        self.assertEqual(second["files"][0]["path"], f"uploads/{day}/sales (2).csv")
        artifacts = self.runtime.artifacts(task_id=task["task_id"])
        self.assertEqual(sorted(a["path"] for a in artifacts), sorted(f["path"] for f in task["files"]))
        data = self.conversation(self.agent_id, self.chat_id)
        operator = data["messages"][0]
        self.assertEqual(operator["text"], "Summarise these")
        self.assertIn("📎 Attached: sales.csv, brief.pdf", operator["body"])
        self.assertEqual([f["name"] for f in operator["files"]], ["sales.csv", "brief.pdf"])
        self.execute()
        first = self.runs[0]
        # The paths and how to read them go in the brief, never into the words the router sees.
        self.assertNotIn("uploads/", first["prompt"])
        self.assertIn(f"uploads/{day}/sales.csv", first["brief"])
        self.assertIn("read_file", first["brief"])
        self.assertIn("read_document", first["brief"])
        self.assertIn("pandas", first["brief"])
        # A later turn still knows about files sent earlier in the chat.
        self.send(self.agent_id, self.chat_id, "What was the Feb total?")
        self.execute()
        self.assertIn("attached earlier in this chat", self.runs[-1]["brief"])
        self.assertIn(f"uploads/{day}/sales.csv", self.runs[-1]["brief"])

    def test_files_only_messages_replays_and_refusals(self):
        code, task = self.send(self.agent_id, self.chat_id, "", files=[self.file_payload("a.txt", b"hello")])
        self.assertEqual(code, 200, task)
        self.assertEqual(self.runtime.task(task["task_id"])["request"], "📎 Attached: a.txt")
        self.assertEqual(self.send(self.agent_id, self.chat_id, "   ")[0], 400)
        request_id = str(uuid.uuid4())
        payload = [self.file_payload("same.txt", b"once")]
        first = self.send(self.agent_id, self.chat_id, "Keep", files=payload, request_id=request_id)[1]
        again = self.send(self.agent_id, self.chat_id, "Keep", files=payload, request_id=request_id)[1]
        self.assertEqual(first["task_id"], again["task_id"])
        folder = self.project(self.agent_view) / "uploads" / time.strftime("%Y-%m-%d")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["a.txt", "same.txt"])
        for files in ([self.file_payload("x.exe", b"MZ")], [self.file_payload("a.txt", b"x")] * 11,
                      [{"name": "a.txt", "data": b64(b"x"), "extra": 1}], "files"):
            code, refused = self.send(self.agent_id, self.chat_id, "Bad", files=files)
            self.assertEqual(code, 400, refused)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["a.txt", "same.txt"])

    def test_picture_files_are_also_shown_to_the_model(self):
        files = [self.file_payload("photo.png", png(), "image/png"),
                 self.file_payload("broken.png", b"not a picture", "image/png"),
                 self.file_payload("data.csv", b"1,2")]
        code, task = self.send(self.agent_id, self.chat_id, "   ", files=files)
        self.assertEqual(code, 200, task)
        self.assertEqual(len(task["files"]), 3)
        self.assertEqual(self.runtime.task(task["task_id"])["request"], "📎 Attached: photo.png, broken.png, data.csv")
        self.execute()
        self.assertEqual([a.name for a in self.runs[0]["attachments"]], ["photo.png"])

    def test_images_still_travel_with_the_message(self):
        code, task = self.send(self.agent_id, self.chat_id, "What is this?",
                               images=[{"name": "shot.png", "mime": "image/png", "data": b64(png())}])
        self.assertEqual(code, 200, task)
        self.assertEqual(self.runtime.task(task["task_id"])["request"], "What is this?\n\n📎 Attached: shot.png")
        self.execute()
        self.assertEqual(len(self.runs[0]["attachments"]), 1)


# =============================================================================== G2
class ImageGeneratorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.opener = FakeOpenRouter()
        self.generator = hub_images.ImageGenerator(lambda: KEY, opener=self.opener,
                                                   catalog_fetch=lambda url: {"data": []})

    def test_one_image_is_saved_under_images_and_the_key_never_leaves_the_header(self):
        result = self.generator.generate(self.root, "A red lighthouse at dawn, watercolour", aspect_ratio="16:9")
        sent = self.opener.requests[0]
        self.assertEqual(sent["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(sent["auth"], f"Bearer {KEY}")
        self.assertEqual(sent["body"]["model"], hub_images.default_model())
        self.assertEqual(sent["body"]["modalities"], ["image", "text"])
        self.assertEqual(sent["body"]["image_config"], {"aspect_ratio": "16:9"})
        self.assertTrue(result["path"].startswith("images/a-red-lighthouse-at-dawn-watercolour-"))
        self.assertEqual((self.root / result["path"]).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual((result["width"], result["height"], result["cost_usd"]), (8, 8, 0.0039))
        self.assertNotIn(KEY, json.dumps(result))
        second = self.generator.generate(self.root, "A red lighthouse at dawn, watercolour")
        self.assertNotEqual(second["path"], result["path"])

    def test_editing_sends_the_picture_and_webp_becomes_png(self):
        (self.root / "uploads").mkdir()
        (self.root / "uploads" / "cat.jpg").write_bytes(png(fmt="JPEG"))
        from jarvis.agent_hub_runtime import _project_file

        source = hub_images.load_input(self.root, "uploads/cat.jpg", _project_file)
        self.opener.replies.append(self.opener.image_reply(png(fmt="WEBP"), "image/webp"))
        result = self.generator.generate(self.root, "Give the cat a hat", source=source)
        parts = self.opener.requests[0]["body"]["messages"][0]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "Give the cat a hat"})
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual((result["mime"], result["edited_from"]), ("image/png", "uploads/cat.jpg"))
        self.assertTrue(result["path"].endswith(".png"))
        for bad in ("../outside.png", "missing.png", "uploads"):
            with self.assertRaises(hub_images.ImageError):
                hub_images.load_input(self.root, bad, _project_file)
        (self.root / "fake.png").write_bytes(b"not a png")
        with self.assertRaises(hub_images.ImageError):
            hub_images.load_input(self.root, "fake.png", _project_file)

    def test_refusals_are_readable_and_carry_no_key(self):
        def error(code, message="nope"):
            return urllib.error.HTTPError("https://openrouter.ai", code, "x", {},
                                          io.BytesIO(json.dumps({"error": {"message": f"{message} {KEY}"}}).encode()))
        cases = [
            (error(402), "out of credits"), (error(401), "refused the API key"), (error(500, "boom"), "HTTP 500"),
            ({"choices": [{"message": {"content": "I cannot draw that"}}]}, "did not return an image"),
            ({"choices": [{"message": {"images": [{"image_url": {"url": "https://evil.example/x.png"}}]}}]},
             "link instead of image data"),
            (self.opener.image_reply(b"\x89PNG\r\n\x1a\n not really a picture"), "not a valid image"),
            (self.opener.image_reply(b"GIF89a", "image/png"), "does not match its type"),
            ({"error": {"message": f"bad {KEY}"}}, "reported an error"),
        ]
        for reply, words in cases:
            self.opener.replies.append(reply)
            with self.assertRaises(hub_images.ImageError) as caught:
                self.generator.generate(self.root, "a boat")
            self.assertIn(words, str(caught.exception))
            self.assertNotIn(KEY, str(caught.exception))
        for kwargs in ({"aspect_ratio": "7:3"}, {"model": "someone/not-an-image-model"}, {"model": "bad id"}):
            with self.assertRaises(hub_images.ImageError):
                self.generator.generate(self.root, "a boat", **kwargs)
        with self.assertRaises(hub_images.ImageError):
            self.generator.generate(self.root, "")
        keyless = hub_images.ImageGenerator(lambda: None, opener=self.opener)
        with self.assertRaises(hub_images.ImageError) as caught:
            keyless.generate(self.root, "a boat")
        self.assertIn("Settings", str(caught.exception))
        self.assertFalse(any((self.root / "images").glob("*")) if (self.root / "images").exists() else False)

    def test_model_choice(self):
        self.assertEqual(hub_images.choose_model(None), hub_images.default_model())
        self.assertEqual(hub_images.choose_model("openai/gpt-5-image-mini"), "openai/gpt-5-image-mini")
        listed = {"data": [{"id": "vendor/new-image", "architecture": {"output_modalities": ["image", "text"]}},
                           {"id": "vendor/text-only", "architecture": {"output_modalities": ["text"]}}]}
        with patch.dict(hub_images._catalog, {"at": 0.0, "models": ()}):
            self.assertEqual(hub_images.choose_model("vendor/new-image", lambda url: listed), "vendor/new-image")
            with self.assertRaises(hub_images.ImageError):
                hub_images.choose_model("vendor/text-only", lambda url: listed)
        with patch.dict(os.environ, {"JARVIS_HUB_IMAGE_MODEL": "google/gemini-3-pro-image"}):
            self.assertEqual(hub_images.default_model(), "google/gemini-3-pro-image")


class ImageToolTests(ParityBase):
    def test_create_image_in_a_turn_is_saved_recorded_and_shown_inline(self):
        agent = self.agent(["files_read", "images"])
        chat = self.new_chat(agent["agent_id"])
        self.send(agent["agent_id"], chat, "Draw me a lighthouse")
        self.execute([lambda tb: tb.execute("create_image", {"prompt": "A lighthouse", "aspect_ratio": "1:1"})])
        run = self.runs[0]
        self.assertIn("create_image", run["tools"])
        self.assertNotIn("generate_image", run["tools"])  # the lane names are not offered separately
        self.assertTrue(json.loads(run["outputs"][0])["ok"], run["outputs"][0])
        self.assertIn("create_image", run["brief"])
        turn = self.conversation(agent["agent_id"], chat)["agent_turns"][0]
        self.assertEqual(len(turn["images"]), 1)
        image = turn["images"][0]
        self.assertTrue(image["path"].startswith("images/a-lighthouse-"))
        self.assertEqual(image["url"], f"/api/artifacts/{image['artifact_id']}/content")
        code, data, headers = self.raw(image["url"])
        self.assertEqual((code, headers["Content-Type"]), (200, "image/png"))
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        tool = next(e for e in self.runtime.events(task_id=turn["task_id"]) if e["kind"] == "tool")
        self.assertEqual(tool["detail"]["path"], image["path"])
        self.assertEqual(tool["detail"]["artifact_id"], image["artifact_id"])
        # The key is never stored by the Hub: not in its database, events or audit log.
        self.assertNotIn(KEY.encode(), (self.root / "hub.db").read_bytes())

    def test_jarvis_image_lane_names_run_on_openrouter(self):
        agent = self.agent(["images"])
        chat = self.new_chat(agent["agent_id"])
        self.send(agent["agent_id"], chat, "Make it better",
                  images=[{"name": "logo.png", "mime": "image/png", "data": b64(png())}])
        self.execute([
            lambda tb: tb.execute("image_generation_status", {}),
            lambda tb: tb.execute("generate_image", {"prompt": "A logo", "output": "generated-images/x.png",
                                                     "size": "1536x1024"}),
            lambda tb: tb.execute("edit_attached_image", {"attachment_index": 1, "prompt": "Cleaner",
                                                          "output": "generated-images/y.png"}),
            lambda tb: tb.execute("edit_attached_image", {"attachment_index": 2, "prompt": "x", "output": "z.png"}),
        ])
        status, generated, edited, missing = (json.loads(o) for o in self.runs[0]["outputs"])
        self.assertEqual((status["result"]["provider"], status["result"]["configured"]), ("openrouter", True))
        self.assertTrue(generated["result"]["relative_path"].startswith("images/"))
        self.assertEqual(self.opener.requests[0]["body"]["image_config"], {"aspect_ratio": "3:2"})
        self.assertEqual(edited["result"]["edited_from"], "logo.png")
        self.assertEqual(self.opener.requests[1]["body"]["messages"][0]["content"][1]["type"], "image_url")
        self.assertFalse(missing["ok"])

    def test_new_images_permission_requires_explicit_grant_for_saved_agents(self):
        self.assertTrue(DEFAULT_PERMISSIONS["images"])
        self.assertIn("images", PERMISSION_LABELS)
        self.assertEqual(allowed_tools({"images": True}), frozenset({"create_image"}))
        full = self.agent([key for key in PERMISSION_LABELS])
        limited = self.agent(["files_read"])
        with self.runtime.db() as db:
            for agent in (full, limited):
                row = db.execute("SELECT permissions FROM hub_agent_settings WHERE agent_id=?",
                                 (agent["agent_id"],)).fetchone()
                saved = json.loads(row["permissions"])
                saved.pop("images")
                db.execute("UPDATE hub_agent_settings SET permissions=? WHERE agent_id=?",
                           (json.dumps(saved), agent["agent_id"]))
        self.assertFalse(self.runtime.agent_settings(full["agent_id"])["permissions"]["images"])
        self.assertFalse(self.runtime.agent_settings(limited["agent_id"])["permissions"]["images"])
        chat = self.new_chat(limited["agent_id"])
        self.send(limited["agent_id"], chat, "Draw a cat")
        self.execute([lambda tb: tb.execute("generate_image", {"prompt": "cat", "output": "cat.png"})])
        self.assertNotIn("create_image", self.runs[0]["tools"])
        self.assertFalse(json.loads(self.runs[0]["outputs"][0])["ok"])
        self.assertEqual(self.opener.requests, [])


class PermissionMigrationTests(ParityBase):
    def test_missing_permissions_never_inherit_authority_from_old_grants(self):
        agent = self.agent(["files_read", "web_research"])
        agent_id = agent["agent_id"]
        for saved in ({"files_read": True, "web_research": True},
                      {"files_read": True, "web_research": False}, {}):
            with self.subTest(saved=saved):
                with self.runtime.db() as db:
                    db.execute("UPDATE hub_agent_settings SET permissions=? WHERE agent_id=?",
                               (json.dumps(saved), agent_id))
                expected = {key: saved.get(key, False) for key in DEFAULT_PERMISSIONS}
                self.assertEqual(self.runtime.agent_settings(agent_id)["permissions"], expected)
                # Unrelated saves must not persist a silent capability upgrade.
                self.runtime.save_agent_settings(agent_id, instructions="Keep the existing scope.")
                self.assertEqual(self.runtime.agent_settings(agent_id)["permissions"], expected)
                with self.runtime.db() as db:
                    row = db.execute("SELECT permissions FROM hub_agent_settings WHERE agent_id=?",
                                     (agent_id,)).fetchone()
                self.assertEqual(json.loads(row["permissions"]), expected)

    def test_explicit_operator_grant_can_enable_a_new_capability(self):
        agent = self.agent(["files_read"])
        agent_id = agent["agent_id"]
        with self.runtime.db() as db:
            db.execute("UPDATE hub_agent_settings SET permissions=? WHERE agent_id=?",
                       (json.dumps({"files_read": True}), agent_id))
        permissions = self.runtime.agent_settings(agent_id)["permissions"]
        self.assertFalse(permissions["images"])
        permissions["images"] = True
        self.runtime.save_agent_settings(agent_id, permissions=permissions)
        expected = {key: key in {"files_read", "images"} for key in DEFAULT_PERMISSIONS}
        self.assertEqual(self.runtime.agent_settings(agent_id)["permissions"], expected)


# =============================================================================== G3
class DataAnalysisTests(ParityBase):
    def test_brief_explains_analysis_and_charts_show_inline_but_uploads_do_not(self):
        agent = self.agent(["files_read", "files_write", "run_commands"])
        chat = self.new_chat(agent["agent_id"])
        root = self.project(agent)
        self.send(agent["agent_id"], chat, "Chart this", files=[self.file_payload("photo.png", png(), "image/png"),
                                                                 self.file_payload("data.csv", b"x,y\n1,2\n")])

        def write_chart(_toolbox):
            (root / "charts").mkdir(exist_ok=True)
            (root / "charts" / "totals.png").write_bytes(png((0, 90, 200)))
            return "written"

        self.execute([write_chart])
        brief = self.runs[0]["brief"]
        for words in ("pandas", "run_process", "savefig", "never plt.show()", "inline"):
            self.assertIn(words, brief)
        images = self.conversation(agent["agent_id"], chat)["agent_turns"][0]["images"]
        self.assertEqual([i["path"] for i in images], ["charts/totals.png"])

    def test_a_redrawn_identical_chart_still_shows_when_the_reply_links_it(self):
        agent = self.agent(["files_read", "files_write", "run_commands"])
        chat = self.new_chat(agent["agent_id"])
        root = self.project(agent)

        def write_chart(_toolbox):
            (root / "charts").mkdir(exist_ok=True)
            (root / "charts" / "totals.png").write_bytes(png((0, 90, 200)))
            return "written"

        self.send(agent["agent_id"], chat, "Chart this")
        self.execute([write_chart])
        self.send(agent["agent_id"], chat, "Chart it again")
        # Same bytes: no new version is recorded, but the reply links the chart.
        self.execute([write_chart], text="Here it is:\n\n![Totals](charts/totals.png)\n\nAlso `../secret.png`.")
        turns = self.conversation(agent["agent_id"], chat)["agent_turns"]
        self.assertEqual([i["path"] for i in turns[0]["images"]], ["charts/totals.png"])
        self.assertEqual([i["path"] for i in turns[1]["images"]], ["charts/totals.png"])

    def test_policy_runs_scripts_but_not_inline_python_or_mutating_git(self):
        from jarvis.policy import validate_process

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self.assertTrue(validate_process(workspace, "python", ["analyze.py"])[0])
            self.assertFalse(validate_process(workspace, "python", ["-c", "print(1)"])[0])
            self.assertTrue(validate_process(workspace, "git", ["diff"])[0])
            self.assertFalse(validate_process(workspace, "git", ["commit", "-m", "x"])[0])

    def test_pandas_and_matplotlib_scripts_make_a_chart_in_the_agent_environment(self):
        """Run with the same minimal environment run_process gives agents: it redirects APPDATA,
        so libraries installed only in the per-user site-packages are invisible there (found live:
        pandas lacked dateutil and matplotlib lacked packaging until they were installed globally)."""
        from jarvis.tools import _minimal_environment

        try:
            import matplotlib  # noqa: F401
            import pandas  # noqa: F401
        except ImportError:
            self.skipTest("pandas/matplotlib are not installed in this Python")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "project"
            folder.mkdir()
            (folder / "data.csv").write_bytes(b"month,total\nJan,10\nFeb,12\nMar,9\n")
            (folder / "analyze.py").write_bytes(
                b"import matplotlib\nmatplotlib.use('Agg')\nimport matplotlib.pyplot as plt\nimport pandas as pd\n"
                b"frame = pd.read_csv('data.csv')\nprint(int(frame['total'].sum()))\n"
                b"frame.plot(x='month', y='total', kind='bar')\nplt.savefig('chart.png')\n")
            done = subprocess.run([sys.executable, "analyze.py"], cwd=folder, capture_output=True, text=True,
                                  timeout=120, check=False, env=_minimal_environment(Path(tmp) / "data"))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.strip(), "31")
            self.assertEqual((folder / "chart.png").read_bytes()[:4], b"\x89PNG")


# =============================================================================== G4
@unittest.skipUnless(HAS_GIT, "git is not installed")
class GitStepTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "project"
        self.root.mkdir()

    def repo(self):
        hub_github.git_init(self.root, "app")
        (self.root / "app" / "main.py").write_bytes(b"print('hi')\n")
        hub_github.git_commit(self.root, "app", "Start")
        return self.root / "app"

    def test_init_branch_commit_diff(self):
        repo = self.repo()
        self.assertEqual(hub_github.git_branch(self.root, "app", "feature/greeting")["branch"], "feature/greeting")
        (repo / "main.py").write_bytes(b"print('hello')\n")
        (repo / "notes.md").write_bytes(b"notes\n")
        done = hub_github.git_commit(self.root, "app", "Greet properly\n\nLonger body", ["main.py"])
        self.assertEqual((done["branch"], done["files"]), ("feature/greeting", ["main.py"]))
        self.assertRegex(done["commit"], r"^[0-9a-f]{40}$")
        log = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%an <%ae>|%s"], capture_output=True,
                             text=True, check=True).stdout.strip()
        self.assertEqual(log, "JARVIS agent <jarvis-agent@jarvis.example>|Greet properly")
        self.assertEqual(hub_github.git_branch(self.root, "app", "main", create=False)["branch"], "main")

    def test_refusals(self):
        repo = self.repo()
        attempts = [
            lambda: hub_github.git_init(self.root, "app"),
            lambda: hub_github.git_init(self.root, "../elsewhere"),
            lambda: hub_github.git_branch(self.root, "app", "bad..name"),
            lambda: hub_github.git_branch(self.root, "app", "-D"),
            lambda: hub_github.git_branch(self.root, "../..", "x"),
            lambda: hub_github.git_commit(self.root, "app", "nothing changed"),
            lambda: hub_github.git_commit(self.root, "app", "x", ["../../secret.txt"]),
            lambda: hub_github.git_commit(self.root, "app", "x", [":(exclude)main.py"]),
            lambda: hub_github.git_commit(self.root, "app", "bell\x07"),
        ]
        for attempt in attempts:
            with self.assertRaises(hub_github.GitStepError):
                attempt()
        config = repo / ".git" / "config"
        config.write_bytes(config.read_bytes() + b"[core]\n\tfsmonitor = run-me.exe\n")
        (repo / "main.py").write_bytes(b"changed\n")
        with self.assertRaises(hub_github.GitStepError):  # executable config is refused, not run
            hub_github.git_commit(self.root, "app", "Should not run")

    def test_pull_request_plan_and_exact_command(self):
        repo = self.repo()
        config = repo / ".git" / "config"
        config.write_bytes(config.read_bytes() + b'[remote "origin"]\n\turl = https://github.com/octo/demo.git\n'
                           b"\tfetch = +refs/heads/*:refs/remotes/origin/*\n")
        plan = hub_github.pull_request_plan(self.root, {"repository_path": "app", "base": "main",
                                                        "head": "feature/x", "title": "  Add   greeting ",
                                                        "body": "Line one\nLine two"})
        self.assertEqual((plan["repository"], plan["title"]), ("octo/demo", "Add greeting"))
        calls = []
        provider = SimpleNamespace(workspace_root=self.root, _run=lambda exe, args, cwd: calls.append((exe, args)) or
                                   SimpleNamespace(ok=True, stdout="https://github.com/octo/demo/pull/7\n",
                                                   error=None, stderr=""))
        result = hub_github.create_pull_request(self.root, plan, provider=provider)
        self.assertEqual(result["url"], "https://github.com/octo/demo/pull/7")
        self.assertEqual(calls, [("gh", ["pr", "create", "--repo=octo/demo", "--base=main", "--head=feature/x",
                                         "--title=Add greeting", "--body=Line one\nLine two"])])
        failing = SimpleNamespace(workspace_root=self.root, _run=lambda *a, **k: SimpleNamespace(
            ok=False, stdout="", error="no commits between main and feature/x", stderr=""))
        with self.assertRaises(hub_github.GitStepError):
            hub_github.create_pull_request(self.root, plan, provider=failing)
        bad = [{"repository_path": "app", "base": "main", "head": "main", "title": "t"},
               {"repository_path": "app", "base": "main", "head": "x", "title": ""},
               {"repository_path": "app", "base": "main", "head": "x", "title": "t", "body": "b" * 8001},
               {"repository_path": "app", "base": "main", "head": "x", "title": "t", "draft": "yes"},
               {"repository_path": "app", "base": "main", "head": "x", "title": "t", "remote": "upstream"},
               {"repository_path": "app", "base": "main", "head": "x", "title": "t", "shell": "rm -rf"}]
        for arguments in bad:
            with self.assertRaises(hub_github.GitStepError, msg=str(arguments)):
                hub_github.pull_request_plan(self.root, arguments)


class GitHubToolTests(ParityBase):
    PLAN = {"repository": "octo/demo", "repository_path": ".", "base": "main", "head": "feature/x",
            "title": "Add greeting", "body": "Body", "draft": False, "body_sha256": "0" * 64}

    def test_git_and_pull_request_tools_follow_permissions(self):
        cases = {("run_commands",): {"git_init", "git_branch", "git_commit"},
                 ("accounts",): {"github_create_pull_request"}, ("files_read",): set()}
        for groups, expected in cases.items():
            agent = self.agent(list(groups))
            chat = self.new_chat(agent["agent_id"])
            self.send(agent["agent_id"], chat, "hello")
            self.execute()
            names = set(self.runs[-1]["tools"]) & {"git_init", "git_branch", "git_commit", "github_create_pull_request"}
            self.assertEqual(names, expected, groups)

    def test_pull_request_waits_for_an_exact_operator_approval(self):
        agent = self.agent(["accounts", "run_commands"])
        chat = self.new_chat(agent["agent_id"])
        self.send(agent["agent_id"], chat, "Open the PR")
        self.memory.decisions = [(False, 41), (True, 41)]
        opened = []
        arguments = {"repository_path": ".", "base": "main", "head": "feature/x", "title": "Add greeting"}
        with patch.object(hub_github, "pull_request_plan", return_value=dict(self.PLAN)), \
                patch.object(hub_github, "create_pull_request",
                             side_effect=lambda root, plan: opened.append(plan) or {"opened": True, "url": "u"}):
            self.execute([lambda tb: tb.execute("github_create_pull_request", arguments)] * 2)
        waiting, done = (json.loads(o) for o in self.runs[0]["outputs"])
        self.assertEqual((waiting["approval_required"], waiting["approval_id"]), (True, 41))
        self.assertEqual(done, {"ok": True, "result": {"opened": True, "url": "u"}})
        self.assertEqual(len(opened), 1)
        approval = self.memory.approvals[0]
        self.assertEqual(approval["action"], "publish_external")
        self.assertEqual((approval["resource"]["tool"], approval["resource"]["repository"],
                          approval["resource"]["title"]), ("github_create_pull_request", "octo/demo", "Add greeting"))
        self.assertIn("octo/demo", approval["reason"])
        with patch.object(hub_github, "pull_request_plan", side_effect=hub_github.GitStepError("bad head")):
            self.send(agent["agent_id"], chat, "Again")
            self.execute([lambda tb: tb.execute("github_create_pull_request", arguments)])
        refused = json.loads(self.runs[-1]["outputs"][0])
        self.assertFalse(refused["ok"])
        self.assertIn("bad head", refused["error"])


# =============================================================================== G8
class PackageValidationTests(unittest.TestCase):
    PY = staticmethod(lambda args: ["PYTHON", *args])
    NPM = staticmethod(lambda args: ["NODE", "NPM-CLI", *args])

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "project"
        (self.root / "web").mkdir(parents=True)

    def plan(self, **arguments):
        return hub_packages.plan(self.root, arguments, python_command=self.PY, npm_command=self.NPM)

    def test_plain_registry_packages_are_accepted(self):
        self.assertEqual(hub_packages.validate("pip", ["requests", "requests-oauthlib==2.0.0", "pandas>=2,<3",
                                                       "uvicorn[standard]", "Django~=5.1", "numpy!=2.0.0",
                                                       "black[d,jupyter]==24.1.1"]),
                         ["requests", "requests-oauthlib==2.0.0", "pandas>=2,<3", "uvicorn[standard]",
                          "Django~=5.1", "numpy!=2.0.0", "black[d,jupyter]==24.1.1"])
        self.assertEqual(hub_packages.validate("npm", ["lodash", "lodash.merge@4.6.2", "@types/node@22",
                                                       "@scope/tool", "react@^18.3.1", "typescript@next"]),
                         ["lodash", "lodash.merge@4.6.2", "@types/node@22", "@scope/tool", "react@^18.3.1",
                          "typescript@next"])

    def test_refusals(self):
        pip_refused = [
            "https://evil.example/pkg.tar.gz", "git+https://github.com/a/b.git", "-e", "-e .", "--index-url=x",
            "--extra-index-url", "--trusted-host", "pkg @ https://x", "pkg@1.0", "./local", "../up", "C:\\wheel.whl",
            "dir/pkg", "pkg; python_version<'3'", "pkg ==1.0", "", "x" * 101, "~/pkg", "file:///tmp/p", 5,
        ]
        for spec in pip_refused:
            with self.assertRaises(hub_packages.PackageError, msg=repr(spec)):
                hub_packages.validate("pip", [spec])
        npm_refused = [
            "git:github.com/a/b", "git+ssh://git@example.com/a/b.git", "github:user/repo", "user/repo",
            "file:../pkg", "link:../pkg", "npm:lodash@4", "https://registry.example/x.tgz", "./pkg", "pkg.tgz",
            "pkg-1.0.tar.gz", "--registry=http://x", "-g", "@scope", "lodash@", "a b", "x" * 101,
        ]
        for spec in npm_refused:
            with self.assertRaises(hub_packages.PackageError, msg=repr(spec)):
                hub_packages.validate("npm", [spec])
        for manager, packages in (("pip", ["requests", "Requests==2.0"]), ("pip", ["a_b", "a-b"]),
                                  ("npm", ["lodash", "lodash@4"]), ("npm", ["@types/node", "@types/node@22"]),
                                  ("pip", []), ("pip", ["a"] * 21),
                                  ("pip", "requests"), ("conda", ["numpy"]), ("pip", [f"p{i}" + "x" * 95
                                                                                     for i in range(11)])):
            with self.assertRaises(hub_packages.PackageError, msg=f"{manager} {packages!r}"[:80]):
                hub_packages.validate(manager, packages)

    def test_exact_argv(self):
        pip = self.plan(manager="pip", packages=["requests==2.32.3", "pandas>=2,<3"])
        self.assertEqual(pip["argv"], ["PYTHON", "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                                       "--only-binary=:all:", "requests==2.32.3", "pandas>=2,<3"])
        built = self.plan(manager="pip", packages=["somepkg"], allow_source_builds=True)
        self.assertEqual(built["argv"], ["PYTHON", "-m", "pip", "install", "--disable-pip-version-check",
                                         "--no-input", "somepkg"])
        npm = self.plan(manager="npm", packages=["lodash@4.17.21", "@types/node"], directory="web")
        folder = str((self.root / "web").resolve())
        self.assertEqual(npm["argv"], ["NODE", "NPM-CLI", "install", "--ignore-scripts", "--no-audit", "--no-fund",
                                       "--prefix", folder, "lodash@4.17.21", "@types/node"])
        self.assertEqual((npm["cwd"], npm["folder"]), (folder, "web"))
        self.assertEqual(self.plan(manager="npm", packages=["lodash"])["folder"], ".")
        resource = hub_packages.approval_resource(built)
        self.assertEqual((resource["manager"], resource["packages"]), ("pip", ["somepkg"]))
        self.assertIn("ALLOWED", resource["source_builds"])
        self.assertIn("SOURCE BUILDS ALLOWED", hub_packages.approval_reason(built))
        self.assertIn("Prebuilt wheels only", hub_packages.approval_reason(pip))
        self.assertIn("project folder web", hub_packages.approval_resource(npm)["installs_into"])

    def test_plan_refusals(self):
        (self.root / "configured").mkdir()
        (self.root / "configured" / ".npmrc").write_text("registry=http://evil.example\n", encoding="utf-8")
        (self.root / "web" / "node_modules").mkdir()
        cases = [dict(manager="npm", packages=["lodash"], directory="../outside"),
                 dict(manager="npm", packages=["lodash"], directory="C:/Windows"),
                 dict(manager="npm", packages=["lodash"], directory="missing"),
                 dict(manager="npm", packages=["lodash"], directory="web/node_modules"),
                 dict(manager="npm", packages=["lodash"], directory="configured"),
                 dict(manager="npm", packages=["lodash"], allow_source_builds=True),
                 dict(manager="pip", packages=["requests"], directory="web"),
                 dict(manager="pip", packages=["requests"], allow_source_builds="yes"),
                 dict(manager="pip", packages=["requests"], index_url="https://x")]
        for arguments in cases:
            with self.assertRaises(hub_packages.PackageError, msg=str(arguments)):
                self.plan(**arguments)

    def test_run_summarises_and_bounds_output(self):
        entry = self.plan(manager="pip", packages=["requests"])
        calls = []

        def runner(program, arguments, **kwargs):
            calls.append((program, arguments, kwargs))
            return SimpleNamespace(stdout="Collecting requests\n" + "x" * 5000 + "\nSuccessfully installed requests-2.32.3",
                                   stderr="token=" + "abcdef1234567890", exit_code=0, timed_out=False)

        result = hub_packages.run(entry, env={"PATH": "p"}, cwd=self.root, runner=runner)
        program, arguments, kwargs = calls[0]
        self.assertEqual((program, kwargs["host_command"], kwargs["timeout"]), ("python", entry["argv"], 600))
        self.assertEqual(arguments, entry["argv"][1:])
        self.assertEqual(result["installed"], "Successfully installed requests-2.32.3")
        self.assertLessEqual(len(result["output"]), hub_packages.MAX_OUTPUT_CHARS + 1)
        self.assertNotIn("abcdef1234567890", result["output"])
        self.assertNotIn("error", result)
        failed = hub_packages.run(entry, env={}, cwd=self.root, runner=lambda *a, **k: SimpleNamespace(
            stdout="", stderr="ERROR: No matching distribution", exit_code=1, timed_out=False))
        self.assertIn("exited with code 1", failed["error"])
        slow = hub_packages.run(entry, env={}, cwd=self.root, runner=lambda *a, **k: SimpleNamespace(
            stdout="", stderr="", exit_code=None, timed_out=True))
        self.assertIn("10 minutes", slow["error"])


class PackageToolTests(ParityBase):
    def test_install_waits_for_an_exact_approval_then_runs_the_approved_command(self):
        agent = self.agent(["run_commands"])
        chat = self.new_chat(agent["agent_id"])
        self.send(agent["agent_id"], chat, "Install requests")
        calls = []

        def runner(program, arguments, **kwargs):
            calls.append(dict(kwargs, program=program))
            return SimpleNamespace(stdout="Successfully installed requests-2.32.3", stderr="", exit_code=0,
                                   timed_out=False)

        self.runtime._package_runner = runner
        self.memory.decisions = [(False, 12), (True, 12)]
        arguments = {"manager": "pip", "packages": ["requests==2.32.3"]}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": KEY, "GITHUB_TOKEN": "ghp_" + "s" * 36}):
            self.execute([lambda tb: tb.execute("install_packages", arguments)] * 2)
        run = self.runs[0]
        self.assertIn("install_packages", run["tools"])
        self.assertIn("install_packages", run["brief"])
        waiting, done = (json.loads(o) for o in run["outputs"])
        self.assertEqual((waiting["approval_required"], waiting["approval_id"]), (True, 12))
        self.assertEqual(done["ok"], True, done)
        self.assertEqual(done["result"]["installed"], "Successfully installed requests-2.32.3")
        self.assertEqual(len(calls), 1)  # nothing ran before the approval
        call = calls[0]
        self.assertEqual(call["host_command"], [str(Path(sys.executable).resolve()), "-m", "pip", "install",
                                                "--disable-pip-version-check", "--no-input", "--only-binary=:all:",
                                                "requests==2.32.3"])
        self.assertEqual(call["timeout"], 600)
        self.assertNotIn("OPENROUTER_API_KEY", call["env"])
        self.assertNotIn("GITHUB_TOKEN", call["env"])
        self.assertEqual(call["env"]["PIP_CONFIG_FILE"], os.devnull)
        approval = self.memory.approvals[0]
        self.assertEqual(approval["action"], "install_dependencies")
        self.assertEqual((approval["resource"]["tool"], approval["resource"]["manager"],
                          approval["resource"]["packages"]), ("install_packages", "pip", ["requests==2.32.3"]))
        self.assertIn(str(Path(sys.executable).resolve()), approval["resource"]["installs_into"])
        self.assertIn("--only-binary=:all:", approval["resource"]["command"])
        self.assertIn("Prebuilt wheels only", approval["reason"])
        tool_rows = [e["detail"] for e in self.runtime.events(agent_id=agent["agent_id"]) if e["kind"] == "tool"]
        self.assertEqual(tool_rows[-1]["args"], "pip install requests==2.32.3")

    def test_refused_requests_never_ask_or_run_and_failures_are_reported(self):
        agent = self.agent(["run_commands"])
        chat = self.new_chat(agent["agent_id"])
        self.send(agent["agent_id"], chat, "Install things")
        self.runtime._package_runner = lambda *a, **k: SimpleNamespace(
            stdout="", stderr="ERROR: No matching distribution found for nosuchpkg", exit_code=1, timed_out=False)
        self.execute([lambda tb: tb.execute("install_packages", {"manager": "pip", "packages": ["-e", "."]}),
                      lambda tb: tb.execute("install_packages", {"manager": "pip", "packages": ["nosuchpkg"],
                                                                 "allow_source_builds": True})])
        refused, failed = (json.loads(o) for o in self.runs[0]["outputs"])
        self.assertFalse(refused["ok"])
        self.assertIn("options are not accepted", refused["error"])
        self.assertEqual(len(self.memory.approvals), 1)  # only the valid request asked
        self.assertIn("SOURCE BUILDS ALLOWED", self.memory.approvals[0]["reason"])
        self.assertFalse(failed["ok"])
        self.assertIn("No matching distribution", failed["result"]["output"])

    def test_only_agents_that_run_programs_get_the_tool(self):
        agent = self.agent(["files_read", "files_write"])
        chat = self.new_chat(agent["agent_id"])
        self.send(agent["agent_id"], chat, "hello")
        self.execute()
        self.assertNotIn("install_packages", self.runs[0]["tools"])
        self.assertNotIn("install_packages", self.runs[0]["brief"])


# =============================================================================== G5
class ConversationControlTests(ParityBase):
    def setUp(self):
        super().setUp()
        self.agent_view = self.agent(["files_read"])
        self.agent_id = self.agent_view["agent_id"]
        self.chat_id = self.new_chat(self.agent_id)
        self.base = f"/api/agents/{self.agent_id}/chats/{self.chat_id}"

    def two_turns(self):
        self.send(self.agent_id, self.chat_id, "First question")
        self.execute(text="First answer")
        self.send(self.agent_id, self.chat_id, "Second question", files=[self.file_payload("n.csv", b"1,2")])
        self.execute(text="Second answer")
        return self.conversation(self.agent_id, self.chat_id)

    def test_regenerate_resends_the_last_message_and_hides_the_old_reply(self):
        before = self.two_turns()
        old_task = before["messages"][2]["task_id"]
        result = self.ok(self.base + "/regenerate", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["superseded"], [old_task])
        new = self.runtime.task(result["task_id"])
        self.assertEqual(new["request"], self.runtime.task(old_task)["request"])
        self.assertEqual([f["path"] for f in result["files"]], [f["path"] for f in before["messages"][2]["files"]])
        code, busy = self.request(self.base + "/regenerate", {})
        self.assertEqual(code, 400)
        self.assertIn("in progress", busy["error"])
        self.execute(text="Better second answer")
        after = self.conversation(self.agent_id, self.chat_id)
        self.assertEqual([m["body"] for m in after["messages"] if m["role"] == "assistant"],
                         ["First answer", "Better second answer"])
        self.assertEqual(len(after["agent_turns"]), 2)
        # The model's history no longer has the replaced turn: a fresh conversation was built.
        conversation = self.runs[-1]["conversation_id"]
        self.assertNotEqual(conversation, self.runs[0]["conversation_id"])
        history = " ".join(content for _, content in self.memory.conversations[conversation])
        self.assertIn("First answer", history)
        self.assertNotIn("Second answer", history)
        # Kept in storage, and never re-run.
        self.assertTrue(self.runtime.turn_meta([old_task])[old_task]["superseded_at"])
        with self.assertRaises(TaskError):
            with self.runtime.db() as db:
                db.execute("UPDATE hub_tasks SET state='FAILED' WHERE task_id=?", (old_task,))
            self.runtime.retry(old_task)

    def test_edit_replaces_a_message_and_everything_after_it(self):
        before = self.two_turns()
        self.assertEqual(before["messages"][0]["role"], "operator")
        result = self.ok(self.base + "/edit", {"index": 0, "body": "A better first question"})
        self.assertEqual(len(result["superseded"]), 2)
        self.execute(text="New answer")
        after = self.conversation(self.agent_id, self.chat_id)
        self.assertEqual([(m["role"], m.get("text", m["body"])) for m in after["messages"]],
                         [("operator", "A better first question"), ("assistant", "New answer")])
        self.assertEqual(self.runs[-1]["prompt"], "A better first question")
        history = self.runtime.chat_context(result["task_id"])
        self.assertEqual(history, [])
        refusals = [{"index": 1, "body": "x"}, {"index": 9, "body": "x"}, {"index": True, "body": "x"},
                    {"index": 0}, {"index": 0, "body": 5}, {"message_id": "nope", "body": "x"},
                    {"index": 0, "message_id": after["messages"][1]["message_id"], "body": "x"},
                    {"index": 0, "body": "x", "role": "system"}, {"index": 0, "body": ""}]
        for payload in refusals:
            code, refused = self.request(self.base + "/edit", payload)
            self.assertEqual(code, 400, (payload, refused))
        by_id = self.ok(self.base + "/edit", {"message_id": after["messages"][0]["message_id"], "body": "Third try",
                                             "request_id": "edit-1"})
        again = self.ok(self.base + "/edit", {"message_id": after["messages"][0]["message_id"], "body": "Third try",
                                             "request_id": "edit-1"})
        self.assertEqual(by_id["task_id"], again["task_id"])

    def test_an_edit_keeps_the_original_attachments(self):
        self.two_turns()
        result = self.ok(self.base + "/edit", {"index": 2, "body": "Changed question"})
        self.assertEqual(self.runtime.task(result["task_id"])["request"], "Changed question\n\n📎 Attached: n.csv")
        self.assertEqual([f["name"] for f in result["files"]], ["n.csv"])
        self.assertEqual(len(result["superseded"]), 1)
        self.execute()
        self.assertIn("n.csv", self.runs[-1]["brief"])

    def test_a_failed_resend_restores_the_thread(self):
        self.two_turns()
        with patch.object(self.runtime, "create_task", side_effect=TaskError("Enable this agent first.")):
            code, _refused = self.request(self.base + "/edit", {"index": 2, "body": "Changed"})
        self.assertEqual(code, 400)
        after = self.conversation(self.agent_id, self.chat_id)
        self.assertEqual(len(after["messages"]), 4)
        with self.runtime.db() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM hub_chat_conversations WHERE agent_id=? AND chat_id=?",
                                            (self.agent_id, self.chat_id)).fetchone())

    def test_feedback_is_stored_on_replies_only(self):
        self.two_turns()
        self.ok(self.base + "/feedback", {"index": 1, "rating": "up", "note": "Exactly right"})
        messages = self.conversation(self.agent_id, self.chat_id)["messages"]
        self.assertEqual(messages[1]["feedback"]["rating"], "up")
        self.assertEqual(messages[1]["feedback"]["note"], "Exactly right")
        self.assertIsNone(messages[3]["feedback"])
        self.ok(self.base + "/feedback", {"message_id": messages[3]["message_id"], "rating": "down"})
        self.ok(self.base + "/feedback", {"index": 1, "rating": None})
        messages = self.conversation(self.agent_id, self.chat_id)["messages"]
        self.assertEqual((messages[1]["feedback"], messages[3]["feedback"]["rating"]), (None, "down"))
        for payload in ({"index": 0, "rating": "up"}, {"index": 1, "rating": "meh"}, {"index": 1},
                        {"index": 1, "rating": None, "note": "orphan"}, {"index": 1, "rating": "up", "note": "n" * 1001},
                        {"index": 1, "rating": "up", "extra": 1}):
            code, refused = self.request(self.base + "/feedback", payload)
            self.assertEqual(code, 400, (payload, refused))

    def test_export_markdown_and_json(self):
        self.two_turns()
        self.ok(self.base + "/regenerate", {})
        self.execute(text="Regenerated answer")
        code, data, headers = self.raw(self.base + "/export?format=md")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "text/markdown; charset=utf-8")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="Numbers.md"')
        text = data.decode("utf-8")
        self.assertTrue(text.startswith("# Numbers"))
        for words in ("### You", "First question", "First answer", "Regenerated answer", "Attached: n.csv"):
            self.assertIn(words, text)
        self.assertNotIn("Second answer", text)
        code, data, headers = self.raw(self.base + "/export?format=json")
        document = json.loads(data)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="Numbers.json"')
        self.assertEqual([m["author"] for m in document["messages"]], ["You", "Atlas", "You", "Atlas"])
        self.assertEqual(document["messages"][2]["files"], ["n.csv"])
        self.assertEqual(self.raw(self.base + "/export?format=html")[0], 400)
        self.assertEqual(self.raw(self.base + "/export?format=md", authenticated=False)[0], 401)
        other = self.agent(["files_read"], name="Other")["agent_id"]
        self.assertEqual(self.raw(f"/api/agents/{other}/chats/{self.chat_id}/export")[0], 400)

    def test_rename_a_chat(self):
        renamed = self.ok(self.base + "/rename", {"title": "  Quarterly\n  numbers\t review  "})
        self.assertEqual((renamed["chat_id"], renamed["title"], renamed["archived"]),
                         (self.chat_id, "Quarterly numbers review", False))
        detail = self.ok(f"/api/agents/{self.agent_id}")
        self.assertEqual(next(c["title"] for c in detail["chats"] if c["chat_id"] == self.chat_id),
                         "Quarterly numbers review")
        headers = self.raw(self.base + "/export?format=md")[2]
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="Quarterly numbers review.md"')
        self.ok(self.base + "/archive", {"archived": True})
        self.assertTrue(self.ok(self.base + "/rename", {"title": "Archived but renamed"})["archived"])
        for body in ({"title": ""}, {"title": "   "}, {"title": "x" * 121}, {"title": "bell\x07"},
                     {"title": "rtl\u202etxt"}, {"title": 5}, {"title": "ok", "chat_id": "x"}, {}):
            code, refused = self.request(self.base + "/rename", body)
            self.assertEqual(code, 400, body)
        other = self.agent(["files_read"], name="Other")["agent_id"]
        self.assertEqual(self.request(f"/api/agents/{other}/chats/{self.chat_id}/rename", {"title": "Mine"})[0], 400)
        self.assertEqual(self.request(self.base + "/rename", {"title": "x" * 120})[0], 200)

    def test_new_routes_require_sign_in(self):
        for path, body in ((self.base + "/regenerate", {}), (self.base + "/edit", {"index": 0, "body": "x"}),
                           (self.base + "/feedback", {"index": 1, "rating": "up"}),
                           (self.base + "/rename", {"title": "x"}),
                           ("/api/personalization", {"about_you": "x"})):
            self.assertEqual(self.request(path, body, authenticated=False)[0], 401, path)
        self.assertEqual(self.request("/api/personalization", authenticated=False)[0], 401)


# =============================================================================== G7
class PersonalizationTests(ParityBase):
    def test_saved_bounded_screened_and_given_to_every_agent_as_data(self):
        self.assertEqual(self.ok("/api/personalization"), {"about_you": "", "response_style": ""})
        saved = self.ok("/api/personalization", {"about_you": "I run a bakery in Leeds.",
                                                 "response_style": "Short answers, metric units."})
        self.assertEqual(saved["response_style"], "Short answers, metric units.")
        self.assertEqual(self.ok("/api/personalization", {"about_you": "I run two bakeries."})["response_style"],
                         "Short answers, metric units.")
        for payload in ({"about_you": "x" * 1501}, {"tone": "x"}, {}, {"about_you": 5},
                        {"about_you": "my key is sk-or-" + "z" * 40}):
            code, _refused = self.request("/api/personalization", payload)
            self.assertEqual(code, 400, payload)
        for groups in (["files_read"], ["web_research"]):
            agent = self.agent(groups)
            chat = self.new_chat(agent["agent_id"])
            self.send(agent["agent_id"], chat, "hello")
            self.execute()
            brief = self.runs[-1]["brief"]
            self.assertIn("Personalization settings", brief)
            self.assertIn("never change your permissions", brief)
            self.assertIn("I run two bakeries.", brief)
            self.assertIn("metric units", brief)
        self.ok("/api/personalization", {"about_you": "", "response_style": ""})
        agent = self.agent(["files_read"])
        chat = self.new_chat(agent["agent_id"])
        self.send(agent["agent_id"], chat, "hello")
        self.execute()
        self.assertNotIn("Personalization settings", self.runs[-1]["brief"])


# ========================================================================= items 8, 9
class WorkspaceAndToolRowTests(ParityBase):
    def test_agent_payload_carries_the_workspace(self):
        agent = self.agent(["files_read"])
        workspace = self.ok(f"/api/agents/{agent['agent_id']}")["workspace"]
        self.assertEqual(workspace, {"folder": agent["project_id"], "git_branch": None, "git_dirty": None})
        if not HAS_GIT:
            return
        root = self.project(agent)
        hub_github.git_init(root, ".")
        (root / "a.txt").write_bytes(b"x")
        with patch.dict(hub_github._workspace_cache, clear=True):
            self.assertEqual(self.ok(f"/api/agents/{agent['agent_id']}")["workspace"],
                             {"folder": agent["project_id"], "git_branch": "main", "git_dirty": True})

    def test_workspace_state_is_cached_and_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "proj"
            (folder / ".git").mkdir(parents=True)
            (folder / ".git" / "HEAD").write_text("ref: refs/heads/dev\n", encoding="utf-8")
            with patch.object(hub_github, "_provider", side_effect=RuntimeError("git exploded")):
                self.assertEqual(hub_github.workspace_state(folder, background=False),
                                 {"folder": "proj", "git_branch": None, "git_dirty": None})
            calls = []
            fake = SimpleNamespace(workspace_root=folder, _run=lambda *a, **k: calls.append(1) or SimpleNamespace(
                ok=True, stdout=" M a.txt\n"))
            with patch.dict(hub_github._workspace_cache, clear=True), \
                    patch.object(hub_github, "_provider", return_value=fake):
                first = hub_github.workspace_state(folder, background=False)
                again = hub_github.workspace_state(folder, background=False)
            self.assertEqual(first, {"folder": "proj", "git_branch": "dev", "git_dirty": True})
            self.assertEqual((again, len(calls)), (first, 1))

    def test_tool_events_carry_args_path_and_the_artifact_for_diffs(self):
        agent = self.agent(["files_read", "files_write"])
        root = self.project(agent)
        chat = self.new_chat(agent["agent_id"])
        self.send(agent["agent_id"], chat, "Write the notes")

        def write(path, content):
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(content.encode("utf-8"))
            return {"path": path}

        schema = {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                  "required": ["path", "content"]}
        self.execute([lambda tb: tb.execute("write_file", {"path": "./docs/notes.md", "content": "hello\n"})],
                     tools={"write_file": Tool("write_file", "Write a file", schema, write)})
        turn = self.conversation(agent["agent_id"], chat)["agent_turns"][0]
        events = self.ok(f"/api/events?task={turn['task_id']}")["events"]
        tool = next(e for e in events if e["kind"] == "tool")
        self.assertEqual({k: tool["detail"][k] for k in ("tool", "args", "path", "change", "has_diff")},
                         {"tool": "write_file", "args": "./docs/notes.md", "path": "docs/notes.md",
                          "change": "created", "has_diff": True})
        artifact = next(a for a in turn["artifacts"] if a["path"] == "docs/notes.md")
        self.assertEqual(tool["detail"]["artifact_id"], artifact["artifact_id"])
        self.assertIn("+hello", self.ok(f"/api/artifacts/{artifact['artifact_id']}/diff")["diff"])
        change = next(e for e in events if e["kind"] == "artifact")
        self.assertEqual(change["detail"]["artifacts"][0]["artifact_id"], artifact["artifact_id"])



class GitLongPathTests(unittest.TestCase):
    def test_every_hub_git_step_enables_long_paths(self):
        from jarvis import hub_github
        seen = []

        class Provider:
            def _run(self, executable, arguments, *, cwd):
                seen.append((executable, list(arguments)))
                return None
        hub_github._run(Provider(), Path("."), ["add", "--all", "--", "."])
        self.assertEqual(seen, [("git", ["-c", "core.longpaths=true", "add", "--all", "--", "."])])


if __name__ == "__main__":
    unittest.main()
