"""Executable Lab06 demo checks; all provider traffic is controlled and offline."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from importlib import import_module, util
import io
import json
import sys
import unittest
from unittest.mock import patch

import httpx

from taip.ai_integration.config import ProviderConfig, ProviderKind
from taip.ai_integration.errors import AIConfigurationError, AIProviderError
from taip.ai_integration.http_transport import HTTPJSONTransport
from taip.common.timetable_contracts import SchoolClass, TimeSlot, Weekday
from tests.ai_integration.test_mock_provider import (
    EXTRACTION_RESPONSE,
    INVALID_SOLUTION_RESPONSE,
    PROMPT,
    SOLUTION_RESPONSE,
    timetable_data,
)
from tests.ai_integration.test_providers import RecordingTransport, completion


def complete_extraction() -> str:
    """A hand-checked known instance with no unresolved extraction annotations."""
    response = json.loads(EXTRACTION_RESPONSE)
    response["missing_information"] = []
    response["unsupported_requirements"] = []
    return json.dumps(response, ensure_ascii=False)


class DemoTests(unittest.TestCase):
    def setUp(self):
        name = "tests.ai_integration.demo"
        self.assertIsNotNone(util.find_spec(name), "The runnable Lab06 demo must exist")
        self.demo = import_module(name)

    def config(self, kind):
        return ProviderConfig(kind.value, kind, "requested-exact-model", "fake-private-key")

    def assert_safe_report(self, report):
        text = json.dumps(report, ensure_ascii=False, allow_nan=False)
        for private in (PROMPT, "fake-private-key", "private-transport-details", "raw_response", "assignments", "missing_information"):
            self.assertNotIn(private, text)
        allowed = {
            "call_id", "operation", "provider", "model_id", "requested_model_id",
            "returned_model_id", "duration_ms", "outcome", "error_code",
            "http_status_code", "retry_after_seconds",
            "generation_parameters", "usage",
        }
        for record in report["invocations"]:
            self.assertEqual(set(record), allowed)
            self.assertGreaterEqual(record["duration_ms"], 0)
            self.assertTrue(record["call_id"])

    def test_mock_runs_extract_solve_and_independent_verification(self):
        report = self.demo.run_mock_demo()

        self.assertEqual(report["mode"], "mock")
        self.assertEqual(len(report["models"]), 1)
        model = report["models"][0]
        self.assertEqual(model["status"], "completed")
        self.assertEqual(model["workflow_status"], "COMPLETED")
        self.assertEqual(model["verification_status"], "VERIFIED")
        self.assertEqual(model["violation_codes"], [])
        self.assertEqual([item["operation"] for item in report["invocations"]], ["extract", "solve"])
        self.assertTrue(all(item["provider"] == "mock" for item in report["invocations"]))
        self.assert_safe_report(report)

    def test_mock_invalid_candidate_is_independently_rejected(self):
        report = self.demo.run_mock_demo(invalid_solution=True)

        model = report["models"][0]
        self.assertEqual(model["workflow_status"], "COMPLETED")
        self.assertEqual(model["status"], "completed")
        self.assertEqual(model["verification_status"], "FAILED")
        self.assertIn("schedule.teacher_overlap", model["violation_codes"])
        self.assertEqual([item["outcome"] for item in report["invocations"]], ["success", "success"])
        self.assert_safe_report(report)

    def test_mock_error_is_measured_and_stops_before_solving(self):
        report = self.demo.run_mock_demo(malformed_extraction=True)

        model = report["models"][0]
        self.assertEqual(model["status"], "failed")
        self.assertEqual(model["workflow_status"], "FAILED")
        self.assertEqual(model["error_code"], "invalid_json")
        self.assertIsNone(model["verification_status"])
        self.assertEqual(len(report["invocations"]), 1)
        self.assertEqual(report["invocations"][0]["outcome"], "error")
        self.assert_safe_report(report)

    def test_semantic_gate_accepts_known_facts_with_independent_entity_ids(self):
        data = timetable_data()
        arbitrary_ids = replace(
            data,
            classes=(replace(data.classes[0], class_id="class-new"),),
            teachers=(replace(data.teachers[0], teacher_id="teacher-new", subject_ids=("subject-new",)),),
            subjects=(replace(data.subjects[0], subject_id="subject-new"),),
            rooms=(replace(data.rooms[0], room_id="room-new"),),
            time_slots=(replace(data.time_slots[1], slot_id="slot-tue"), replace(data.time_slots[0], slot_id="slot-mon")),
            requirements=(replace(data.requirements[0], requirement_id="req-new", class_id="class-new", teacher_id="teacher-new", subject_id="subject-new"),),
        )

        self.assertTrue(self.demo.is_expected_demo_data(data))
        self.assertTrue(self.demo.is_expected_demo_data(arbitrary_ids))

    def test_semantic_gate_rejects_changed_missing_or_invented_facts(self):
        data = timetable_data()
        changes = (
            replace(data, classes=()),
            replace(data, teachers=()),
            replace(data, subjects=()),
            replace(data, rooms=()),
            replace(data, requirements=()),
            replace(data, time_slots=data.time_slots[:1]),
            replace(data, classes=data.classes + (SchoolClass("extra", "B", 20),)),
            replace(data, classes=(replace(data.classes[0], name="B"),)),
            replace(data, classes=(replace(data.classes[0], student_count=None),)),
            replace(data, teachers=(replace(data.teachers[0], name="Maria"),)),
            replace(data, teachers=(replace(data.teachers[0], subject_ids=()),)),
            replace(data, subjects=(replace(data.subjects[0], name="Fizică"),)),
            replace(data, rooms=(replace(data.rooms[0], name="Altă sală"),)),
            replace(data, rooms=(replace(data.rooms[0], capacity=None),)),
            replace(data, rooms=(replace(data.rooms[0], features=("projector",)),)),
            replace(data, time_slots=(TimeSlot("other", Weekday.FRIDAY, 1, "08:00", "08:50"), data.time_slots[1])),
            replace(data, time_slots=(replace(data.time_slots[0], period=2), data.time_slots[1])),
            replace(data, time_slots=(replace(data.time_slots[0], start_time=None), data.time_slots[1])),
            replace(data, time_slots=(replace(data.time_slots[0], end_time="09:00"), data.time_slots[1])),
            replace(data, requirements=(replace(data.requirements[0], sessions_per_week=1),)),
            replace(data, requirements=(replace(data.requirements[0], teacher_id="absent"),)),
            replace(data, requirements=(replace(data.requirements[0], subject_id="absent"),)),
            replace(data, requirements=(replace(data.requirements[0], class_id="absent"),)),
            replace(data, classes=(replace(data.classes[0], class_id="  "),)),
            replace(data, time_slots=(replace(data.time_slots[0], slot_id="tue-1"), data.time_slots[1])),
        )
        for changed in changes:
            with self.subTest(data=changed):
                self.assertFalse(self.demo.is_expected_demo_data(changed))

    def test_semantic_gate_accepts_only_known_equivalent_class_labels(self):
        data = timetable_data()
        for name in ("A", "clasa A", "Clasa A"):
            with self.subTest(name=name):
                extracted = replace(data, classes=(replace(data.classes[0], name=name),))
                self.assertTrue(self.demo.is_expected_demo_data(extracted))
                self.assertEqual(extracted.classes[0].name, name)
        for name in ("B", "clasa B", "Clasa B", "a", "A ", "Clasa A 1"):
            with self.subTest(name=name):
                extracted = replace(data, classes=(replace(data.classes[0], name=name),))
                self.assertFalse(self.demo.is_expected_demo_data(extracted))

    def test_live_controlled_subset_uses_real_factory_workflow_and_aop(self):
        for kind in ProviderKind:
            with self.subTest(provider=kind):
                transport = RecordingTransport(completion(kind, complete_extraction()), completion(kind, SOLUTION_RESPONSE))

                report = self.demo.run_live_demo((self.config(kind) for _ in range(1)), transport=transport)

                self.assertEqual(report["mode"], "live")
                self.assertEqual([model["model_id"] for model in report["models"]], [kind.value])
                self.assertEqual(report["models"][0]["verification_status"], "VERIFIED")
                self.assertEqual([item["operation"] for item in report["invocations"]], ["extract", "solve"])
                self.assertEqual([item["returned_model_id"] for item in report["invocations"]], ["returned-model-version"] * 2)
                self.assertEqual(len(transport.requests), 2)
                self.assert_safe_report(report)

    def test_live_http_transport_roundtrip_stays_offline(self):
        replies = [completion(ProviderKind.GROQ, complete_extraction()), completion(ProviderKind.GROQ, SOLUTION_RESPONSE)]
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=replies.pop(0))

        with HTTPJSONTransport(transport=httpx.MockTransport(handler)) as transport:
            report = self.demo.run_live_demo((self.config(ProviderKind.GROQ),), transport=transport)

        self.assertEqual(report["models"][0]["verification_status"], "VERIFIED")
        self.assertEqual(len(requests), 2)
        self.assertEqual(replies, [])
        extraction_prompt = json.loads(requests[0].content)["messages"][1]["content"]
        self.assertEqual(json.loads(extraction_prompt)["prompt"], self.demo.DEMO_PROMPT)
        self.assertNotIn("prânz", self.demo.DEMO_PROMPT)

    def test_live_missing_unsupported_or_unfaithful_extraction_needs_review(self):
        baseline = json.loads(complete_extraction())
        missing = dict(baseline, missing_information=[{"code": "unknown", "message": "private review message"}])
        unsupported = dict(baseline, unsupported_requirements=["private unsupported request"])
        changed = json.loads(complete_extraction())
        changed["data"]["classes"][0]["student_count"] = 19
        invalid = json.loads(complete_extraction())
        invalid["data"]["requirements"][0]["teacher_id"] = "missing-teacher"
        empty = {"data": {}}
        for extraction in (missing, unsupported, changed, invalid, empty):
            with self.subTest(extraction=extraction):
                transport = RecordingTransport(completion(ProviderKind.GROQ, json.dumps(extraction)))

                report = self.demo.run_live_demo((self.config(ProviderKind.GROQ),), transport=transport)

                model = report["models"][0]
                self.assertEqual(model["status"], "extraction_requires_review")
                self.assertEqual(model["workflow_status"], "DRAFT")
                self.assertIsNone(model["verification_status"])
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual([item["operation"] for item in report["invocations"]], ["extract"])
                self.assert_safe_report(report)

    def test_live_invalid_candidate_is_retained_for_verification(self):
        transport = RecordingTransport(completion(ProviderKind.GROQ, complete_extraction()), completion(ProviderKind.GROQ, INVALID_SOLUTION_RESPONSE))

        report = self.demo.run_live_demo((self.config(ProviderKind.GROQ),), transport=transport)

        self.assertEqual(report["models"][0]["verification_status"], "FAILED")
        self.assertIn("schedule.teacher_overlap", report["models"][0]["violation_codes"])
        self.assertEqual(len(transport.requests), 2)

    def test_live_extraction_failure_has_safe_metadata_without_retry(self):
        transport = RecordingTransport(AIProviderError("timeout"))

        report = self.demo.run_live_demo((self.config(ProviderKind.GROQ),), transport=transport)

        self.assertEqual(report["models"][0]["status"], "failed")
        self.assertEqual(report["models"][0]["error_code"], "timeout")
        self.assertEqual(len(transport.requests), 1)
        self.assert_safe_report(report)

    def test_live_solve_failure_is_summarized_without_retry_or_exception_text(self):
        transport = RecordingTransport(completion(ProviderKind.GROQ, complete_extraction()), AIProviderError("rate_limit", status_code=429, retry_after_seconds=2))

        report = self.demo.run_live_demo((self.config(ProviderKind.GROQ),), transport=transport)

        model = report["models"][0]
        self.assertEqual(model["status"], "failed")
        self.assertEqual(model["workflow_status"], "FAILED")
        self.assertEqual(model["error_code"], "rate_limit")
        self.assertEqual(report["invocations"][1]["http_status_code"], 429)
        self.assertEqual(report["invocations"][1]["retry_after_seconds"], 2)
        self.assertEqual(len(transport.requests), 2)
        self.assert_safe_report(report)

    def test_unexpected_transport_failure_does_not_print_exception_text(self):
        transport = RecordingTransport(ValueError("private-transport-details"))
        output, errors = io.StringIO(), io.StringIO()

        with redirect_stdout(output), redirect_stderr(errors):
            report = self.demo.run_live_demo((self.config(ProviderKind.GROQ),), transport=transport)

        self.assertEqual(report["models"][0]["error_code"], "unexpected_error")
        self.assertNotIn("private-transport-details", output.getvalue() + errors.getvalue())
        self.assert_safe_report(report)

    def test_cli_defaults_to_mock_without_loading_configuration_or_http(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["demo"]), redirect_stdout(output), patch.object(self.demo, "load_provider_configs", side_effect=AssertionError("Must not read environment")), patch.object(self.demo, "HTTPJSONTransport", side_effect=AssertionError("Must not open HTTP")):
            exit_code = self.demo.main()

        self.assertEqual(exit_code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["mode"], "mock")
        self.assertEqual(report["models"][0]["verification_status"], "VERIFIED")
        self.assert_safe_report(report)

    def test_cli_expected_invalid_solution_exits_zero_but_call_failure_exits_one(self):
        for option, expected_code, expected_status in (("--invalid-solution", 0, "FAILED"), ("--malformed-extraction", 1, None)):
            with self.subTest(option=option):
                output = io.StringIO()
                with patch.object(sys, "argv", ["demo", option]), redirect_stdout(output):
                    exit_code = self.demo.main()
                self.assertEqual(exit_code, expected_code)
                self.assertEqual(json.loads(output.getvalue())["models"][0]["verification_status"], expected_status)

    def test_cli_live_is_explicit_and_uses_runtime_loader_and_transport_context(self):
        transport = HTTPJSONTransport(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=completion(ProviderKind.GROQ, '{"data":{}}'))))
        output = io.StringIO()
        with patch.object(sys, "argv", ["demo", "--live"]), redirect_stdout(output), patch.object(self.demo, "load_provider_configs", return_value=(self.config(ProviderKind.GROQ),)), patch.object(self.demo, "HTTPJSONTransport", return_value=transport):
            exit_code = self.demo.main()

        self.assertEqual(exit_code, 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["mode"], "live")
        self.assertEqual(report["models"][0]["status"], "extraction_requires_review")
        self.assertTrue(transport._client.is_closed)

    def test_cli_configuration_failure_reports_safe_category(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["demo", "--live"]), redirect_stdout(output), patch.object(self.demo, "load_provider_configs", side_effect=AIConfigurationError("api_key")):
            exit_code = self.demo.main()

        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(output.getvalue()), {"mode": "live", "error_code": "configuration_error", "models": [], "invocations": []})


if __name__ == "__main__":
    unittest.main()
