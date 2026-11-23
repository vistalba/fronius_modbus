"""Fail when any module of the integration is covered 95 % or less.

The quality scale's test-coverage rule asks for more than 95 % in EVERY
module, not on average: a well-tested package can hide a module that nothing
runs. The total floor in pytest's --cov-fail-under cannot see that.

Reads the JSON report pytest-cov writes (--cov-report=json:<path>):

    python .github/scripts/check_module_coverage.py coverage.json
"""

import json
import pathlib
import sys

MINIMUM_PERCENT = 95.0


def below(report: dict) -> dict[str, float]:
    """The modules at or under the minimum, with their coverage."""
    return {
        path: summary["percent_covered"]
        for path, file in report["files"].items()
        if (summary := file["summary"])["percent_covered"] <= MINIMUM_PERCENT
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    report = json.loads(pathlib.Path(argv[1]).read_text(encoding="utf-8"))
    if not report["files"]:
        print("the coverage report lists no module", file=sys.stderr)
        return 1
    failing = below(report)
    for path, percent in sorted(failing.items()):
        print(f"{path}: {percent:.2f} % (more than {MINIMUM_PERCENT:.0f} % needed)")
    if failing:
        return 1
    print(f"every one of {len(report['files'])} modules covered above 95 %")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
