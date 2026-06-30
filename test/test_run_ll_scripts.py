import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

try:
    REPO_ROOT = Path(__file__).resolve().parents[1]
except Exception:
    REPO_ROOT = Path.cwd()

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _find_ll_scripts(root: Path):
    return sorted(root.glob("modules/*_LL.py"))


def _extract_last_json_object(text: str):
    """
    Extract the first valid top-level JSON object found in noisy text.

    This is safer than searching from the last '{', because LL reports
    contain many nested dicts and the last '{' usually belongs to details.
    """
    if not text:
        return None

    for start in [m.start() for m in re.finditer(r"\{", text)]:
        depth = 0
        in_string = False
        escape = False

        for idx in range(start, len(text)):
            ch = text[idx]

            if escape:
                escape = False
                continue

            if ch == "\\":
                escape = True
                continue

            if ch == '"':
                in_string = not in_string
                continue

            if in_string:
                continue

            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1

                if depth == 0:
                    candidate = text[start : idx + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        break

    return None


def _summarize_error(parsed, returncode, stderr, reason):
    if reason:
        return reason

    if parsed:
        errors = parsed.get("errors") or []
        if errors:
            return "; ".join(str(e) for e in errors[:3])

        if parsed.get("device_present") is False:
            return "device_present=false"

    lines = []
    if stderr:
        lines.extend([line.strip() for line in stderr.splitlines() if line.strip()])

    for line in reversed(lines):
        if line == "}":
            continue
        if (
            "Error" in line
            or "Exception" in line
            or "Failed" in line
            or "FAILED" in line
            or "Traceback" in line
        ):
            return line[:240]

    if returncode not in (0, None):
        return f"returncode={returncode}"

    for line in reversed(lines):
        if line and line != "}":
            return line[:240]

    return ""


def _is_missing_hardware_result(result):
    if result.get("success"):
        return False

    error = (result.get("error") or "").lower()
    parsed = result.get("parsed")
    parsed_text = ""
    if parsed is not None:
        parsed_text = json.dumps(parsed).lower()

    missing_patterns = [
        "no such file or directory",
        "no such device or address",
        "remote i/o error",
        "could not open any i2c bus",
        "basic probe failed after scanning all candidate",
        "device_present=false",
        "no ais/gps traffic detected",
        "no nmea device detected",
        "no serial ports available",
        "bus .*: open failed",
        "could not probe",
        "connection timed out",
        "errno 110",
    ]

    if "device_present=false" in error:
        return True

    if any(pattern in error for pattern in missing_patterns):
        return True

    if any(pattern in parsed_text for pattern in missing_patterns):
        return True

    return False


def _run_script(script: Path, timeout: int):
    module_name = f"modules.{script.stem}"
    cmd = [os.environ.get("PYTHON", sys.executable), str(script)]

    entry = {
        "script": str(script.relative_to(REPO_ROOT)),
        "module": module_name,
        "name": script.stem,
        "returncode": None,
        "success": False,
        "error": "",
        "stdout": "",
        "stderr": "",
        "parsed": None,
    }

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
            cwd=str(REPO_ROOT),
            env=os.environ.copy(),
        )

        entry["returncode"] = proc.returncode
        entry["stdout"] = proc.stdout.strip()
        entry["stderr"] = proc.stderr.strip()

        combined = "\n".join([entry["stdout"], entry["stderr"]])
        parsed = _extract_last_json_object(combined)
        entry["parsed"] = parsed

        if parsed is not None:
            if parsed.get("success") is not None:
                entry["success"] = bool(proc.returncode == 0 and parsed.get("success"))
            else:
                entry["success"] = bool(
                    proc.returncode == 0
                    and parsed.get("initialized") is True
                    and parsed.get("opened") is True
                    and parsed.get("device_present") is True
                    and not parsed.get("errors")
                )

            if entry["success"]:
                entry["error"] = ""
            else:
                entry["error"] = _summarize_error(
                    parsed=parsed,
                    returncode=proc.returncode,
                    stderr="\n".join([entry["stdout"], entry["stderr"]]),
                    reason="",
                )
        elif (
            script.stem == "audioProc_LL"
            and proc.returncode == 0
            and "Full test: OK" in combined
        ):
            entry["success"] = True
            entry["error"] = ""
        else:
            entry["error"] = _summarize_error(
                parsed=None,
                returncode=proc.returncode,
                stderr=combined,
                reason="",
            )

    except subprocess.TimeoutExpired as exc:
        entry["returncode"] = -1
        entry["stdout"] = (
            (exc.stdout or "").strip()
            if isinstance(exc.stdout, str)
            else str(exc.stdout or "")
        )
        entry["stderr"] = (
            (exc.stderr or "").strip()
            if isinstance(exc.stderr, str)
            else str(exc.stderr or "")
        )
        entry["success"] = False
        entry["error"] = f"timeout after {timeout}s"

    except Exception as exc:
        entry["success"] = False
        entry["error"] = f"runner_exception: {exc}"

    return entry


def _print_summary(results):
    print("\nLL functional test summary")
    print("-" * 80)

    for r in results:
        status = "OK" if r["success"] else "ERROR"
        name = r["name"]
        error = "" if r["success"] else (r.get("error") or "")

        if error:
            print(f"{name:<24} {status:<6} | {error}")
        else:
            print(f"{name:<24} {status:<6}")

    print("-" * 80)

    ok_count = sum(1 for r in results if r["success"])
    print(f"Total: {ok_count}/{len(results)} OK")


def _write_reports(results, report_dir: Path):
    report_dir.mkdir(exist_ok=True)

    report_file = report_dir / "ll_scripts_report.json"
    summary_file = report_dir / "ll_scripts_summary.log"

    with report_file.open("w", encoding="utf-8") as f:
        json.dump({"results": results}, f, indent=2, default=str)

    with summary_file.open("w", encoding="utf-8") as f:
        for r in results:
            status = "OK" if r["success"] else "ERROR"
            error = "" if r["success"] else (r.get("error") or "")
            if error:
                f.write(f"{r['name']:<24} {status:<6} | {error}\n")
            else:
                f.write(f"{r['name']:<24} {status:<6}\n")

        ok_count = sum(1 for r in results if r["success"])
        f.write(f"\nTotal: {ok_count}/{len(results)} OK\n")

    return report_file, summary_file


@pytest.mark.hardware
@pytest.mark.timeout(300)
def test_run_all_ll_scripts_and_report(tmp_path):
    if os.getenv("RUN_HARDWARE_TESTS", "0").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        pytest.skip("hardware test disabled; set RUN_HARDWARE_TESTS=1 to run")

    timeout = int(os.getenv("LL_SCRIPT_TIMEOUT", "90"))

    scripts = _find_ll_scripts(REPO_ROOT)
    assert scripts, "No LL scripts found under modules/"

    results = [_run_script(script, timeout=timeout) for script in scripts]

    _print_summary(results)

    report_file, summary_file = _write_reports(results, REPO_ROOT / "test" / "reports")

    failed = [r for r in results if not r["success"]]
    if failed:
        missed = [r for r in failed if _is_missing_hardware_result(r)]
        if len(missed) == len(failed):
            missed_names = ", ".join(r["name"] for r in missed)
            pytest.skip(
                f"Skipping LL scripts due to missing hardware: {missed_names}. "
                f"Summary: {summary_file}. Details: {report_file}"
            )

        failed_names = ", ".join(r["name"] for r in failed)
        pytest.fail(
            f"Some LL scripts failed: {failed_names}. "
            f"Summary: {summary_file}. Details: {report_file}"
        )


if __name__ == "__main__":
    try:
        test_run_all_ll_scripts_and_report(Path.cwd() / ".tmp_ll_runner")
    except BaseException as exc:
        # pytest.fail raises BaseException, not Exception.
        print(f"\nRunner finished with failure: {exc}")
        raise
