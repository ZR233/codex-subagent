"""Deterministic routing and fallback safety tests without live model calls."""

import datetime
import importlib.util
import io
import json
import tempfile
import unittest
import uuid
from argparse import Namespace
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_subagent.py"
SPEC = importlib.util.spec_from_file_location("run_subagent", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class RoutingTests(unittest.TestCase):
    def test_peak_boundaries_and_override(self):
        utc = datetime.timezone.utc
        before = datetime.datetime(2026, 9, 21, 5, 59, tzinfo=utc)
        start = datetime.datetime(2026, 9, 21, 6, 0, tzinfo=utc)
        end = datetime.datetime(2026, 9, 21, 10, 0, tzinfo=utc)
        self.assertEqual(runner.route_provider(None, "auto", before)[0], "zai")
        self.assertEqual(runner.route_provider(None, "auto", start)[0], "deepseek")
        self.assertEqual(runner.route_provider(None, "auto", end)[0], "zai")
        self.assertEqual(runner.route_provider(None, "off", start)[0], "zai")
        self.assertEqual(runner.route_provider("zai", "on", start)[0], "zai")

    def test_only_error_events_trigger_provider_detection(self):
        state = runner.new_state()
        runner.audit_event({"type": "item.completed", "item": {
            "type": "agent_message", "text": "HTTP 429 quota exceeded"}}, state)
        self.assertEqual(state["error_text"], "")
        runner.audit_event({"type": "turn.failed", "error": {
            "message": "HTTP 429 Too Many Requests"}}, state)
        self.assertEqual(runner.match_provider_failure(state["error_text"]), "rate_limit")
        self.assertEqual(runner.match_provider_failure("智谱用量已用尽"),
                         "quota_exhausted")

    def test_tool_activity_blocks_fallback(self):
        state = runner.new_state()
        runner.audit_event({"type": "item.started", "item": {
            "type": "command_execution", "command": "echo started"}}, state)
        runner.audit_event({"type": "turn.failed", "error": {
            "message": "rate limit"}}, state)
        self.assertTrue(state["tool_started"])
        record = {"provider": "zai", "status_written": True, "raised": None,
                  "status": {"result": "failed", "reason": "provider_error",
                             "outcome": "exited", "tool_started": state["tool_started"],
                             "provider_failure": "rate_limit",
                             "saw_turn_completed": False}}
        allowed, _ = runner.fallback_allowed(record, None, {"reason": None})
        self.assertFalse(allowed)
        record["status"]["tool_started"] = False
        allowed, _ = runner.fallback_allowed(record, None, {"reason": None})
        self.assertTrue(allowed)
        allowed, _ = runner.fallback_allowed(record, "previous-evidence", {"reason": None})
        self.assertFalse(allowed)

    def test_timeout_remains_timeout_even_with_stderr_provider_words(self):
        state = runner.new_state()
        result, reason, _ = runner.classify(
            state, "timeout", 1, 0, provider_failure="rate_limit")
        self.assertEqual((result, reason), ("failed", "timeout"))

    def test_resume_rejects_unknown_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            evidence = directory / "evidence"
            evidence.mkdir()
            status = {"session_persisted": True, "thread_id": str(uuid.uuid4()),
                      "mode": "explore", "cwd": str(directory),
                      "provider": "unrelated-custom-profile"}
            (evidence / "status.json").write_text(json.dumps(status), encoding="utf-8")
            context, error = runner.load_resume_context(evidence, "explore", directory)
            self.assertIsNone(context)
            self.assertIsNotNone(error)
            status.pop("provider")
            (evidence / "status.json").write_text(json.dumps(status), encoding="utf-8")
            context, error = runner.load_resume_context(evidence, "explore", directory)
            self.assertIsNone(error)
            self.assertEqual(context["provider"], "deepseek")

    def test_zai_resume_at_peak_fails_before_creating_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            evidence = root / "previous"
            evidence.mkdir()
            prompt = root / "task.txt"
            prompt.write_text("continue", encoding="utf-8")
            (evidence / "status.json").write_text(json.dumps({
                "session_persisted": True, "thread_id": str(uuid.uuid4()),
                "mode": "explore", "cwd": str(root), "provider": "zai"
            }), encoding="utf-8")
            output = root / "next"
            args = ["--mode", "explore", "--cwd", str(root),
                    "--prompt-file", str(prompt), "--output-dir", str(output),
                    "--resume-from", str(evidence), "--peak-window", "off"]
            peak = datetime.datetime(2026, 9, 21, 6, tzinfo=datetime.timezone.utc)
            stderr = io.StringIO()
            with mock.patch.object(runner, "current_utc_now", return_value=peak), \
                    mock.patch.object(runner, "run_session") as start, \
                    redirect_stderr(stderr):
                self.assertEqual(runner.run(args), 2)
            start.assert_not_called()
            self.assertFalse(output.exists())
            self.assertIn("高峰", stderr.getvalue())
            self.assertIn("新开会话", stderr.getvalue())

            before = datetime.datetime(2026, 9, 21, 5, 59,
                                       tzinfo=datetime.timezone.utc)
            with mock.patch.object(runner, "current_utc_now", return_value=before), \
                    mock.patch.object(runner, "run_session", return_value=0) as start:
                self.assertEqual(runner.run(args), 0)
            self.assertEqual(start.call_args.args[6], "zai")

    def test_deepseek_resume_at_peak_is_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            evidence = root / "previous"
            evidence.mkdir()
            prompt = root / "task.txt"
            prompt.write_text("continue", encoding="utf-8")
            (evidence / "status.json").write_text(json.dumps({
                "session_persisted": True, "thread_id": str(uuid.uuid4()),
                "mode": "explore", "cwd": str(root), "provider": "deepseek"
            }), encoding="utf-8")
            peak = datetime.datetime(2026, 9, 21, 6, tzinfo=datetime.timezone.utc)
            args = ["--mode", "explore", "--cwd", str(root),
                    "--prompt-file", str(prompt), "--output-dir", str(root / "next"),
                    "--resume-from", str(evidence)]
            with mock.patch.object(runner, "current_utc_now", return_value=peak), \
                    mock.patch.object(runner, "run_session", return_value=0) as start:
                self.assertEqual(runner.run(args), 0)
            self.assertEqual(start.call_args.args[6], "deepseek")

    def test_exhausted_zai_resume_reports_new_session_required(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            output = root / "evidence"
            output.mkdir()
            attempt_dir = output / "attempt-1"
            attempt_dir.mkdir()
            prompt = root / "task.txt"
            prompt.write_text("continue", encoding="utf-8")
            status = {"result": "failed", "reason": "provider_error",
                      "error": "quota exhausted", "provider_failure": "quota_exhausted",
                      "outcome": "exited", "session_persisted": True,
                      "thread_id": str(uuid.uuid4())}
            record = runner.attempt_record(1, "zai", attempt_dir, status, True, None)
            args = Namespace(mode="explore", timeout=10.0, provider=None,
                             peak_window="auto")
            with redirect_stderr(io.StringIO()):
                code = runner.finish_attempts(
                    args, root, prompt, output, [record], "zai", None,
                    "resume zai", status["thread_id"], str(root / "previous"),
                    0.0, None, "no fallback", None)
            result = json.loads((output / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(code, 1)
            self.assertEqual(result["provider"], "zai")
            self.assertEqual(result["attempt_count"], 1)
            self.assertEqual(result["next_action"], "start_new_session")
            self.assertEqual(result["recommended_provider"], "deepseek")
            self.assertIn("新开会话", result["error"])

    def test_attempt_evidence_selects_fallback_without_overwriting_first(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            output = root / "evidence"
            output.mkdir()
            prompt = root / "task.txt"
            prompt.write_text("task", encoding="utf-8")
            args = Namespace(mode="explore", timeout=10.0, provider=None,
                             peak_window="off")
            providers = []

            def fake_attempt(index, provider, output_dir, *_args):
                providers.append(provider)
                attempt_dir = output_dir / f"attempt-{index}"
                attempt_dir.mkdir()
                (attempt_dir / "final.txt").write_text(provider, encoding="utf-8")
                status = {"result": "failed" if index == 1 else "succeeded",
                          "reason": "provider_error" if index == 1 else "ok",
                          "outcome": "exited", "tool_started": False,
                          "saw_turn_completed": index == 2,
                          "provider_failure": "quota_exhausted" if index == 1 else None,
                          "thread_id": str(uuid.uuid4()), "session_persisted": True,
                          "child_exit_code": 1 if index == 1 else 0,
                          "final_message_present": True}
                return runner.attempt_record(index, provider, attempt_dir, status, True, None)

            with mock.patch.object(runner, "run_attempt", side_effect=fake_attempt):
                code = runner.run_attempts(args, root, "task", prompt, output, None,
                                           None, "zai", False, "test route")
            status = json.loads((output / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertEqual(providers, ["zai", "deepseek"])
            self.assertEqual(status["provider"], "deepseek")
            self.assertEqual(status["attempt_count"], 2)
            self.assertTrue(status["fallback"]["attempted"])
            self.assertEqual((output / "final.txt").read_text(encoding="utf-8"), "deepseek")
            self.assertEqual((output / "attempt-1" / "final.txt").read_text(
                encoding="utf-8"), "zai")


if __name__ == "__main__":
    unittest.main()
