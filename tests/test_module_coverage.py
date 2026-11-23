"""The per-module coverage gate: it fails on one module left behind, not on the average."""

import importlib.util
import json
import pathlib

SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1]
    / ".github"
    / "scripts"
    / "check_module_coverage.py"
)
_SPEC = importlib.util.spec_from_file_location("check_module_coverage", SCRIPT)
check_module_coverage = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_module_coverage)


def _report(**percents: float) -> dict:
    return {
        "files": {
            name: {"summary": {"percent_covered": percent}}
            for name, percent in percents.items()
        }
    }


def test_one_module_at_95_percent_fails_although_the_average_is_high(tmp_path):
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(_report(a=100.0, b=100.0, c=95.0)))

    assert check_module_coverage.main(["check", str(report)]) == 1


def test_every_module_above_95_percent_passes(tmp_path):
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(_report(a=100.0, b=95.1)))

    assert check_module_coverage.main(["check", str(report)]) == 0


def test_a_report_without_modules_is_no_pass(tmp_path):
    """An empty report would otherwise pass every module it does not list."""
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"files": {}}))

    assert check_module_coverage.main(["check", str(report)]) == 1
