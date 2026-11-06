"""Report measurements stay identical to the run's persisted measurements."""

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skills.audit.engine import c_report

from .fixtures import init_repo
from .test_c_engine import (
    InjectedStop, _assert_success, _complete_adapter, _deps, _engine,
    _json, _one_failure, _run_dir, _run_id,
)


def _measured_adapter(context):
    context["append_record"]("model-call", {"model": "fixture-model", "durationMs": 5, "exit": 0})
    return _complete_adapter(context)


class ReportMetricsTests(unittest.TestCase):
    def _assert_report_metrics(self, report, metrics):
        section = report.split("## 計測\n", 1)[1].split("\n## 証跡", 1)[0]
        values = dict(line[2:].split(": ", 1) for line in section.splitlines() if line.startswith("- "))
        self.assertEqual(values, {
            "duration": f"{metrics['duration']['wallMs']} ms",
            "modelCalls": str(metrics["modelCalls"]["total"]),
        })

    def test_render_without_measurements(self):
        base = {"runId": "20260101T000000Z-x", "profileName": "p"}
        for metrics in (None, {}, {"duration": {}, "modelCalls": {}},
                        {"duration": {"wallMs": None}, "modelCalls": {"total": None}}):
            with self.subTest(metrics=metrics):
                facts = dict(base)
                if metrics is not None:
                    facts["metrics"] = metrics
                text = c_report.render(facts)
                self.assertIn("- duration: 未計測\n- modelCalls: 未計測\n", text)

    def test_render_measurements_as_scalars(self):
        for wall_ms, total in ((0, 0), (1234, 7)):
            with self.subTest(wall_ms=wall_ms, total=total):
                metrics = {
                    "duration": {"wallMs": wall_ms, "componentMs": {"C-REPORT": 10}},
                    "modelCalls": {"total": total, "byLayer": {"L-DOC": total}},
                    "retrieval": {"method": "scan"}, "postPublishMs": 20,
                }
                text = c_report.render({
                    "runId": "20260101T000000Z-x", "profileName": "p", "metrics": metrics,
                })
                self._assert_report_metrics(text, metrics)

    def _assert_engine_metrics(self, root, result):
        _assert_success(self, result)
        run_dir = _run_dir(root, result["runId"])
        report = (root / result["reportPath"]).read_text(encoding="utf-8")
        metrics = _json(run_dir / "metrics.json")
        self._assert_report_metrics(report, metrics)
        self.assertGreater(metrics["duration"]["wallMs"], 0)
        self.assertGreater(metrics["modelCalls"]["total"], 0)
        journal = [json.loads(line) for line in (run_dir / "journal.jsonl").read_text().splitlines()]
        rendered = next(row["data"] for row in journal if row["kind"] == "rendered")
        self.assertEqual(rendered["metrics"], {key: value for key, value in metrics.items() if key != "postPublishMs"})
        self.assertNotIn("postPublishMs", rendered["metrics"])
        receipt = next(row["data"]["receipt"] for row in journal if row["kind"] == "reported")
        self.assertLess(metrics["duration"]["reportPublishedAt"], receipt["publishedAt"])
        return metrics

    def test_new_run_report_matches_metrics_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)
            deps = _deps(adapters={"L-DOC": _measured_adapter})
            result = _engine().run(root, full=True, deps=deps)
            self._assert_engine_metrics(root, result)

    def test_resumed_report_reuses_render_time_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_repo(root)

            def stop_after_render():
                raise InjectedStop("render complete")

            deps = _deps(adapters={"L-DOC": _measured_adapter}, hooks={"rendered": stop_after_render})
            with self.assertRaises(InjectedStop):
                _engine().run(root, full=True, deps=deps)
            run_id = _run_id(root)
            rendered = (_run_dir(root, run_id) / "report.rendered.md").read_bytes()
            calls = 0

            def resumed_clock():
                nonlocal calls
                calls += 1
                return datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc) + datetime.timedelta(seconds=calls)

            with mock.patch.object(c_report, "render", side_effect=AssertionError("report rendered twice")):
                result = _engine().resume(root, run_id, deps=_deps(clock=resumed_clock))
            self._assert_engine_metrics(root, result)
            self.assertEqual((root / result["reportPath"]).read_bytes(), rendered)

    def test_failed_publication_records_undecided_metrics(self):
        engine = _engine()
        failures = (
            ("report-publish-failed", c_report.ReportPublishFailed("ENOSPC")),
            ("report-conflict", engine.EngineRejected("report-conflict")),
        )
        for reason, error in failures:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                init_repo(root)
                with mock.patch.object(engine, "_publish", side_effect=error):
                    result = engine.run(root, full=True, deps=_deps(adapters={"L-DOC": _one_failure}))
                self.assertEqual((result["outcome"], result["reason"]), ("undecided", reason))
                run_dir = _run_dir(root, result["runId"])
                journal = [json.loads(line) for line in (run_dir / "journal.jsonl").read_text().splitlines()]
                rendered = next(row["data"] for row in journal if row["kind"] == "rendered")
                self.assertEqual(rendered["metrics"]["duration"]["outcome"], "NEEDS_FIX")
                self.assertEqual(rendered["metrics"]["modelCalls"]["outcome"], "NEEDS_FIX")
                metrics = _json(run_dir / "metrics.json")
                for key in ("duration", "modelCalls"):
                    with self.subTest(metric=key):
                        self.assertEqual(metrics[key]["outcome"], "undecided")
