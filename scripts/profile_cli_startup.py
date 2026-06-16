"""Profile yoke CLI startup imports, file access, and wall-clock timings."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from statistics import mean
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"

STARTUP_CASES: dict[str, list[str] | None] = {
    "import-main": None,
    "version": ["version"],
    "help": ["--help"],
    "tools-help": ["tools", "--help"],
    "models-help": ["models", "--help"],
    "providers-help": ["providers", "--help"],
    "skills-help": ["skills", "--help"],
    "headless-empty-stdin": ["--headless"],
    "interactive-empty-stdin": [],
}


AUDIT_PROBE = r"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path


case_name = sys.argv[1]
case_args = json.loads(sys.argv[2])
counts = Counter()
open_paths = Counter()
directory_paths = Counter()


def normalize_path(value: object) -> str:
    try:
        if isinstance(value, int):
            return f"fd:{value}"
        return str(Path(os.fsdecode(value)))
    except Exception:
        return repr(value)


def audit_hook(event: str, args: tuple[object, ...]) -> None:
    if event.startswith("import"):
        counts["import"] += 1
        return
    if event == "open":
        counts["open"] += 1
        if args:
            open_paths[normalize_path(args[0])] += 1
        return
    if event in {"os.listdir", "os.scandir"}:
        counts[event] += 1
        if args:
            directory_paths[f"{event} {normalize_path(args[0])}"] += 1


sys.addaudithook(audit_hook)
start_modules = set(sys.modules)
start_time = time.perf_counter()
exit_code = 0
raised = None

try:
    from yoke.cli.main import main

    imported_time = time.perf_counter()
    if case_args is not None:
        try:
            exit_code = int(main(case_args) or 0)
        except SystemExit as exc:
            exit_code = int(exc.code or 0)
except BaseException as exc:
    imported_time = time.perf_counter()
    raised = f"{type(exc).__name__}: {exc}"
    exit_code = 999

end_time = time.perf_counter()
new_modules = sorted(set(sys.modules) - start_modules)
prefix_counts = Counter(module.split(".")[0] for module in new_modules)

print(
    json.dumps(
        {
            "case": case_name,
            "exit_code": exit_code,
            "raised": raised,
            "import_ms": (imported_time - start_time) * 1000,
            "total_ms": (end_time - start_time) * 1000,
            "modules_imported": len(new_modules),
            "event_counts": dict(counts),
            "top_open_paths": open_paths.most_common(20),
            "top_directory_paths": directory_paths.most_common(20),
            "top_module_prefixes": prefix_counts.most_common(30),
        },
        sort_keys=True,
    )
)
"""


def python_environment() -> dict[str, str]:
    environment = dict(os.environ)
    python_path = environment.get("PYTHONPATH")
    source_root_text = str(SOURCE_ROOT)
    environment["PYTHONPATH"] = (
        source_root_text
        if not python_path
        else os.pathsep.join([source_root_text, python_path])
    )
    return environment


def run_python(
    args: list[str],
    *,
    stdin: int | None = subprocess.DEVNULL,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        env=python_environment(),
        stdin=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def run_audit_probe(case_name: str) -> dict[str, Any]:
    case_args = STARTUP_CASES[case_name]
    completed = run_python(
        ["-c", AUDIT_PROBE, case_name, json.dumps(case_args)],
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    payload = json.loads(lines[-1]) if lines else {}
    payload["process_returncode"] = completed.returncode
    payload["stdout_preview"] = "\n".join(completed.stdout.splitlines()[:8])
    payload["stderr_preview"] = "\n".join(completed.stderr.splitlines()[:8])
    return payload


def parse_importtime(stderr: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in stderr.splitlines():
        if not line.startswith("import time:") or "self [us]" in line:
            continue
        fields = [
            field.strip()
            for field in line.removeprefix("import time:").strip().split("|")
        ]
        if len(fields) != 3:
            continue
        try:
            self_microseconds = int(fields[0])
            cumulative_microseconds = int(fields[1])
        except ValueError:
            continue
        rows.append(
            {
                "module": fields[2].strip(),
                "self_ms": self_microseconds / 1000,
                "cumulative_ms": cumulative_microseconds / 1000,
            }
        )
    return sorted(rows, key=lambda row: row["cumulative_ms"], reverse=True)


def run_importtime(case_name: str) -> list[dict[str, Any]]:
    case_args = STARTUP_CASES[case_name]
    if case_args is None:
        code = "from yoke.cli.main import main"
    else:
        code = f"from yoke.cli.main import main; raise SystemExit(main({case_args!r}))"
    completed = run_python(["-X", "importtime", "-c", code])
    return parse_importtime(completed.stderr)


def run_wall_clock(case_name: str, repeat: int) -> dict[str, Any]:
    case_args = STARTUP_CASES[case_name]
    if case_args is None:
        code = "from yoke.cli.main import main"
    else:
        code = f"from yoke.cli.main import main; raise SystemExit(main({case_args!r}))"
    durations: list[float] = []
    returncodes: list[int] = []
    for _ in range(repeat):
        start_time = time.perf_counter()
        completed = run_python(["-c", code])
        durations.append((time.perf_counter() - start_time) * 1000)
        returncodes.append(completed.returncode)
    return {
        "mean_ms": mean(durations),
        "min_ms": min(durations),
        "max_ms": max(durations),
        "returncodes": returncodes,
    }


def build_report(case_names: list[str], repeat: int) -> dict[str, Any]:
    cases: dict[str, Any] = {}
    for case_name in case_names:
        audit = run_audit_probe(case_name)
        importtime_rows = run_importtime(case_name)
        wall_clock = run_wall_clock(case_name, repeat)
        cases[case_name] = {
            "audit": audit,
            "importtime_top_cumulative": importtime_rows[:40],
            "wall_clock": wall_clock,
        }
    return {
        "python": sys.executable,
        "root": str(ROOT),
        "repeat": repeat,
        "cases": cases,
    }


def print_text_report(report: dict[str, Any]) -> None:
    print(f"python: {report['python']}")
    print(f"root: {report['root']}")
    print(f"repeat: {report['repeat']}")
    for case_name, case_report in report["cases"].items():
        audit = case_report["audit"]
        wall_clock = case_report["wall_clock"]
        print()
        print(f"== {case_name} ==")
        print(
            "audit: "
            f"exit={audit['exit_code']} "
            f"import={audit['import_ms']:.1f}ms "
            f"total={audit['total_ms']:.1f}ms "
            f"modules={audit['modules_imported']} "
            f"events={audit['event_counts']}"
        )
        if audit.get("raised"):
            print(f"raised: {audit['raised']}")
        print(
            "wall: "
            f"mean={wall_clock['mean_ms']:.1f}ms "
            f"min={wall_clock['min_ms']:.1f}ms "
            f"max={wall_clock['max_ms']:.1f}ms "
            f"returncodes={wall_clock['returncodes']}"
        )
        print("top import cumulative:")
        for row in case_report["importtime_top_cumulative"][:12]:
            print(
                f"  {row['cumulative_ms']:7.1f}ms "
                f"{row['self_ms']:6.1f}ms {row['module']}"
            )
        print("top module prefixes:")
        for prefix, count in audit["top_module_prefixes"][:12]:
            print(f"  {count:4} {prefix}")
        print("top file opens:")
        for path, count in audit["top_open_paths"][:8]:
            print(f"  {count:4} {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Profile yoke CLI startup imports and file access."
    )
    parser.add_argument(
        "--case",
        dest="cases",
        choices=sorted(STARTUP_CASES),
        action="append",
        help="Startup case to profile. Repeat to select multiple cases.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=3,
        help="Wall-clock repetitions per case.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full report as JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat must be at least 1")
    case_names = args.cases or ["import-main", "version", "help"]
    report = build_report(case_names, args.repeat)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_text_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
