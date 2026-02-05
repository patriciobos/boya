import os
import json
import logging
import subprocess
from pathlib import Path
import importlib

import pytest
import sys

# Ensure repository root is on sys.path so `import modules.*` works
# regardless of the current working directory or how tests are invoked.
try:
    repo_root = Path(__file__).resolve().parents[2]
except Exception:
    repo_root = Path.cwd()
repo_root_str = str(repo_root)
if repo_root_str not in sys.path:
    sys.path.insert(0, repo_root_str)


def _find_ll_scripts(root: Path):
    return sorted(root.glob('modules/*_LL.py'))


@pytest.mark.timeout(300)
def test_run_all_ll_scripts_and_report(tmp_path):
    repo = Path.cwd()
    scripts = _find_ll_scripts(repo)
    assert scripts, "No LL scripts found under modules/"

    timeout = int(os.getenv('LL_SCRIPT_TIMEOUT', '60'))
    results = []

    for script in scripts:
        module_name = f"modules.{script.stem}"
        result_entry = {
            'script': str(script.relative_to(repo)),
            'module': module_name,
            'main_return': None,
            'returncode': None,
            'success': False,
            'reason': '',
            'stdout': '',
            'stderr': '',
        }

        # Try importing module and calling main() if available
        try:
            mod = importlib.import_module(module_name)
            if hasattr(mod, 'main'):
                try:
                    # call main; some mains accept argv, others ignore it
                    ret = mod.main([])
                    result_entry['main_return'] = ret
                    result_entry['success'] = bool(ret) is True
                except Exception as e:
                    result_entry['reason'] = f'main_exception: {e}'
                    result_entry['success'] = False
            else:
                # fallback: run as subprocess and use returncode
                cmd = [os.environ.get('PYTHON', sys.executable), '-m', module_name]
                env = os.environ.copy()
                try:
                    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=timeout, text=True, cwd=str(repo))
                    result_entry['returncode'] = proc.returncode
                    result_entry['stdout'] = proc.stdout.strip()
                    result_entry['stderr'] = proc.stderr.strip()
                    result_entry['success'] = proc.returncode == 0
                except subprocess.TimeoutExpired as e:
                    result_entry['returncode'] = -1
                    result_entry['stdout'] = (e.stdout or '')
                    result_entry['stderr'] = str(e.stderr or '') + '\n[timeout]'
                    result_entry['success'] = False
        except Exception as imp_e:
            # import failed; fallback to subprocess execution
            result_entry['reason'] = f'import_failed: {imp_e}'
            cmd = [os.environ.get('PYTHON', sys.executable), '-m', module_name]
            env = os.environ.copy()
            try:
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=timeout, text=True, cwd=str(repo))
                result_entry['returncode'] = proc.returncode
                result_entry['stdout'] = proc.stdout.strip()
                result_entry['stderr'] = proc.stderr.strip()
                result_entry['success'] = proc.returncode == 0
            except subprocess.TimeoutExpired as e:
                result_entry['returncode'] = -1
                result_entry['stdout'] = (e.stdout or '')
                result_entry['stderr'] = str(e.stderr or '') + '\n[timeout]'
                result_entry['success'] = False

        results.append(result_entry)

    # write report to logs
    report_dir = repo / 'logs'
    report_dir.mkdir(exist_ok=True)
    report_file = report_dir / 'll_scripts_report.json'
    with report_file.open('w', encoding='utf-8') as f:
        json.dump({'results': results}, f, indent=2, default=str)

    # also write a human-readable log with module name and test result
    logger = logging.getLogger('ll_script_runner')
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fh = logging.FileHandler(report_dir / 'll_scripts_run.log', mode='a', encoding='utf-8')
        fmt = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    for r in results:
        name = r.get('module') or r.get('script')
        if r.get('success'):
            logger.info('%s: OK', name)
        else:
            reason = r.get('reason') or ''
            rc = r.get('returncode')
            extra = []
            if reason:
                extra.append(reason)
            if rc is not None:
                extra.append(f'returncode={rc}')
            logger.error('%s: FAIL%s', name, (': ' + ', '.join(extra)) if extra else '')

    # fail the test if any script failed
    failed = [r for r in results if not r['success']]
    if failed:
        pytest.fail(f"Some LL scripts failed. See {report_file} for details.")


if __name__ == '__main__':
    # Allow running this test file directly to produce the JSON report and human-readable log.
    from pathlib import Path
    try:
        tmp = Path.cwd() / '.tmp_ll_runner'
        tmp.mkdir(exist_ok=True)
        test_run_all_ll_scripts_and_report(tmp)
        print(f"Report written to: {str(Path.cwd() / 'logs' / 'll_scripts_report.json')}")
    except Exception as e:
        print(f"Error running ll scripts runner: {e}")
        raise
