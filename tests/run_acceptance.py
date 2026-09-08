import argparse
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from tests.acceptance import REGISTRY, design_ids, route_ids
from tests.acceptance_plan import ORDER, PHASES


class TrackingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executed = set()

    def startTest(self, test):
        self.executed.add(test.id())
        super().startTest(test)


class TrackingRunner(unittest.TextTestRunner):
    resultclass = TrackingResult


def required(phase):
    phases = ORDER if phase == "all" else ORDER[: ORDER.index(phase) + 1]
    return ({i for p in phases for i in PHASES[p]["design"]},
            {i for p in phases for i in PHASES[p]["route"]})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect", choices=[*ORDER, "all"], required=True)
    ap.add_argument("--allow-missing", action="store_true")
    ap.add_argument("--suite-dir", default="tests")
    args = ap.parse_args(argv)
    REGISTRY.clear()
    suite_dir = Path(args.suite_dir).resolve()
    loader = unittest.TestLoader()
    default_suite = (suite_dir == (Path.cwd() / "tests").resolve())
    suite = loader.discover(str(suite_dir), top_level_dir=str(Path.cwd()) if default_suite else str(suite_dir))
    stream = io.StringIO()
    result = TrackingRunner(stream=stream, verbosity=0).run(suite)
    def case_id(test):
        return getattr(test, "test_case", test).id()

    bad = {case_id(t) for t, _ in result.failures + result.errors}
    skipped = {case_id(t) for t, _ in result.skipped}
    bad.update(case_id(t) for t, _ in result.expectedFailures)
    bad.update(case_id(t) for t in result.unexpectedSuccesses)
    seen = {ident for ident, _, _ in REGISTRY}
    design_required, route_required = required(args.expect)
    expected = design_required | route_required
    canonical = set(design_ids()) | set(route_ids())
    unknown = seen - canonical
    missing = expected - seen
    records = []
    for ident, name, targets in REGISTRY:
        status = "合格" if name in result.executed and name not in bad and name not in skipped else "失敗" if name in bad else "skip" if name in skipped else "未実行"
        records.append((ident, name, targets, status))
    statuses = {}
    for ident, _, _, status in records:
        statuses.setdefault(ident, []).append(status)
    passed = {ident for ident, values in statuses.items() if all(value == "合格" for value in values)}
    failed = {ident for ident, values in statuses.items() if "失敗" in values}
    skipped_ids = {ident for ident, values in statuses.items() if ident not in failed and "skip" in values}
    unexecuted = {ident for ident, values in statuses.items() if ident not in failed and ident not in skipped_ids and "未実行" in values}
    unimplemented = expected - seen
    for label, ids in (("design", design_required), ("route", route_required)):
        values = (passed & ids, failed & ids, skipped_ids & ids, unimplemented & ids)
        print(f"設計 {len(design_ids())} 件: 合格 {len(values[0])}・失敗 {len(values[1])}・skip {len(values[2])}・未実装 {len(values[3])}" if label == "design" else f"route {len(route_ids())} 件: 合格 {len(values[0])}・失敗 {len(values[1])}・skip {len(values[2])}・未実装 {len(values[3])}")
    for ident, name, targets, status in records:
        print(f"{ident}: targets={targets} {status} {name}")
    if unknown:
        print("未知 ID: " + ", ".join(sorted(unknown)))
    ahead = (seen & canonical) - expected
    if ahead:
        print("先行実装: " + ", ".join(sorted(ahead)))
    for ident in sorted(seen):
        entries = [(name, targets) for item, name, targets, _ in records if item == ident]
        if entries:
            print(f"{ident}: targets合計={sum(targets for _, targets in entries)} tests={','.join(name for name, _ in entries)}")
    if result.testsRun and (result.failures or result.errors):
        print(stream.getvalue(), end="")
    if (result.failures or result.errors or failed or skipped_ids or unexecuted or unknown or ahead or (missing and not args.allow_missing)):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
