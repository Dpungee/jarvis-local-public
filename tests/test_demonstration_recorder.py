import io
import threading
import time
import unittest
from unittest.mock import Mock

from PIL import Image

from jarvis.attachments import ImageAttachment
from jarvis.demonstration_recorder import (
    DemonstrationCapture,
    DemonstrationRecorder,
    DemonstrationSkillDraft,
    DemonstrationTransition,
    demonstration_contact_sheet,
    demonstration_timeline_json,
    normalize_skill_name,
    structured_response_object,
    validate_extraction,
    validate_skill_draft,
)
from jarvis.presence import PresenceRuntime
from jarvis.screen_companion import ScreenObservation


def _image(color: str) -> ImageAttachment:
    output = io.BytesIO()
    Image.new("RGB", (80, 50), color).save(output, format="PNG")
    return ImageAttachment("image/png", output.getvalue(), "active-window.png")


def _observation(
    digest: str,
    *,
    title: str = "Example document",
    color: str = "blue",
    excluded: bool = False,
) -> ScreenObservation:
    return ScreenObservation(
        application="demo.exe",
        title=title,
        observed_at=time.time(),
        context_sha256=digest * 64,
        image=None if excluded else _image(color),
        excluded=excluded,
        exclusion_reason="window appears sensitive" if excluded else None,
    )


class _Provider:
    available = True

    def __init__(self, observations):
        self.observations = list(observations)
        self.calls = []

    def observe(self, *, capture_pixels, excluded_apps):
        self.calls.append((capture_pixels, set(excluded_apps)))
        if not self.observations:
            return None
        return self.observations.pop(0)


class DemonstrationRecorderTests(unittest.TestCase):
    def test_explicit_recording_stays_in_memory_and_stops_with_capture(self):
        provider = _Provider([
            _observation("a", color="red"),
            _observation("b", color="green"),
        ])
        recorder = DemonstrationRecorder(provider, sample_seconds=10)
        started = recorder.start(excluded_apps={"private.exe"})
        self.assertTrue(started["recording"])
        self.assertEqual(started["frame_count"], 1)
        self.assertFalse(started["raw_screens_persisted"])
        recorder._sample_once()
        capture = recorder.stop()
        self.assertEqual(len(capture.frames), 2)
        self.assertEqual(len(capture.transitions), 2)
        self.assertFalse(recorder.status()["recording"])
        self.assertEqual(recorder.status()["frame_count"], 0)
        self.assertTrue(provider.calls[0][0])
        self.assertIn("private.exe", provider.calls[0][1])

    def test_sensitive_observation_records_only_a_hidden_gap(self):
        recorder = DemonstrationRecorder(
            _Provider([_observation("c", title="Bank login", excluded=True)]),
            sample_seconds=10,
        )
        recorder.start(excluded_apps=set())
        capture = recorder.stop()
        self.assertEqual(capture.frames, ())
        self.assertEqual(capture.excluded_observations, 1)
        self.assertTrue(capture.transitions[0].excluded)
        self.assertEqual(capture.transitions[0].title, "Sensitive window hidden")

    def test_cancel_discards_all_recorded_material(self):
        recorder = DemonstrationRecorder(
            _Provider([_observation("d")]), sample_seconds=10
        )
        recorder.start(excluded_apps=set())
        recorder.cancel()
        status = recorder.status()
        self.assertFalse(status["recording"])
        self.assertEqual(status["frame_count"], 0)
        self.assertEqual(status["transition_count"], 0)
        with self.assertRaisesRegex(RuntimeError, "No demonstration"):
            recorder.stop()

    def test_contact_sheet_is_one_bounded_in_memory_image(self):
        started = time.time()
        frames = (
            _observation("e", color="red"),
            _observation("f", color="green"),
            _observation("1", color="blue"),
        )
        capture = DemonstrationCapture(
            started_at=started,
            stopped_at=started + 8,
            frames=frames,
            transitions=(),
            excluded_observations=0,
        )
        sheet = demonstration_contact_sheet(capture)
        self.assertIsNotNone(sheet)
        self.assertEqual(sheet.mime, "image/jpeg")
        self.assertEqual(sheet.name, "demonstration-keyframes.jpg")

    def test_timeline_json_contains_sequence_not_pixel_data(self):
        started = time.time()
        capture = DemonstrationCapture(
            started_at=started,
            stopped_at=started + 4,
            frames=(_observation("2"),),
            transitions=(DemonstrationTransition(
                application="demo.exe",
                title="Example document",
                observed_at=started + 1,
                context_sha256="a" * 64,
                excluded=False,
            ),),
            excluded_observations=0,
        )
        timeline = demonstration_timeline_json(capture)
        self.assertIn('"application":"demo.exe"', timeline)
        self.assertNotIn("image/png", timeline)
        self.assertNotIn(capture.frames[0].image.data.hex(), timeline)


class DemonstrationDraftValidationTests(unittest.TestCase):
    def test_structured_responses_and_drafts_are_strictly_bounded(self):
        extracted = structured_response_object({
            "content": '{"summary":"Export a report","steps":["Open export"],"uncertainties":[]}'
        }, label="extract")
        self.assertEqual(validate_extraction(extracted)["steps"], ["Open export"])
        draft = validate_skill_draft({
            "name": "Export Report",
            "description": "Export a reviewed report.",
            "what_it_does": "Creates a local export.",
            "how_to_use": "Ask for an export after reviewing the destination.",
            "content": "# Workflow\n\n1. Review the destination.\n2. Export.\n3. Verify the file.",
        })
        self.assertIsInstance(draft, DemonstrationSkillDraft)
        self.assertEqual(draft.name, "export-report")

    def test_invalid_or_secret_shaped_drafts_fail(self):
        self.assertEqual(normalize_skill_name("  My Useful Skill  "), "my-useful-skill")
        with self.assertRaises(ValueError):
            normalize_skill_name("---")
        with self.assertRaises(ValueError):
            validate_extraction({"summary": "x", "steps": [], "uncertainties": []})
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            validate_skill_draft({
                "name": "demo",
                "description": "",
                "what_it_does": "x",
                "how_to_use": "x",
                "content": "x",
            })

    def test_exact_window_titles_are_removed_from_generated_skill_text(self):
        started = time.time()
        capture = DemonstrationCapture(
            started_at=started,
            stopped_at=started + 1,
            frames=(),
            transitions=(DemonstrationTransition(
                application="editor.exe",
                title="Private Client Roadmap.docx",
                observed_at=started,
                context_sha256="b" * 64,
                excluded=False,
            ),),
            excluded_observations=0,
        )
        generalized = PresenceRuntime._generalize_demonstration_draft({
            "name": "review-roadmap",
            "description": "Review Private Client Roadmap.docx.",
            "what_it_does": "Checks Private Client Roadmap.docx.",
            "how_to_use": "Open Private Client Roadmap.docx.",
            "content": "# Steps\n\nOpen Private Client Roadmap.docx and review it.",
        }, capture)
        for value in generalized.values():
            self.assertNotIn("Private Client Roadmap.docx", value)

    def test_analysis_uses_two_passes_and_only_then_exposes_a_draft(self):
        started = time.time()
        capture = DemonstrationCapture(
            started_at=started,
            stopped_at=started + 3,
            frames=(),
            transitions=(DemonstrationTransition(
                application="editor.exe",
                title="Private Client Roadmap.docx",
                observed_at=started + 1,
                context_sha256="c" * 64,
                excluded=False,
            ),),
            excluded_observations=0,
        )
        runtime = object.__new__(PresenceRuntime)
        runtime._demonstration_lock = threading.Lock()
        runtime._demonstration_analysis = {
            "state": "analyzing", "draft": None, "error": None,
            "analysis_pass": 0, "analysis_id": "analysis-1",
        }
        runtime.emit = Mock()
        runtime._demonstration_model_call = Mock(side_effect=[
            {
                "summary": "Review a roadmap",
                "steps": ["Open the roadmap", "Review each section"],
                "uncertainties": [],
            },
            {
                "name": "review-roadmap",
                "description": "Review Private Client Roadmap.docx safely.",
                "what_it_does": "Checks the roadmap structure.",
                "how_to_use": "Open the roadmap and request a review.",
                "content": "# Workflow\n\n1. Open Private Client Roadmap.docx.\n2. Review it.\n3. Verify changes.",
            },
        ])
        runtime._analyze_demonstration(capture, "analysis-1")
        self.assertEqual(runtime._demonstration_model_call.call_count, 2)
        self.assertEqual(runtime._demonstration_analysis["state"], "draft_ready")
        draft = runtime._demonstration_analysis["draft"]
        self.assertIsInstance(draft, DemonstrationSkillDraft)
        self.assertNotIn("Private Client Roadmap.docx", draft.content)

    def test_deleted_analysis_generation_cannot_repopulate_the_draft(self):
        started = time.time()
        capture = DemonstrationCapture(
            started_at=started,
            stopped_at=started + 1,
            frames=(),
            transitions=(DemonstrationTransition(
                application="editor.exe",
                title="Document",
                observed_at=started,
                context_sha256="d" * 64,
                excluded=False,
            ),),
            excluded_observations=0,
        )
        runtime = object.__new__(PresenceRuntime)
        runtime._demonstration_lock = threading.Lock()
        runtime._demonstration_analysis = {
            "state": "idle", "draft": None, "error": None, "analysis_pass": 0
        }
        runtime.emit = Mock()
        runtime._demonstration_model_call = Mock()
        runtime._analyze_demonstration(capture, "stale-analysis")
        runtime._demonstration_model_call.assert_not_called()
        self.assertEqual(runtime._demonstration_analysis["state"], "idle")


if __name__ == "__main__":
    unittest.main()
