"""Run public-edition host checks with outputs outside the repository."""
from pathlib import Path
import argparse
import json
import os
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists() or output == ROOT or ROOT in output.parents:
        parser.error('Choose a fresh output directory outside the repository')
    output.mkdir(parents=True)
    env = os.environ.copy()
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', SCAN3D_TEST_OUTPUT=str(output))
    jobs = [
        ('public_package', ['build/audit_public.py', '--output', str(output / 'public_audit.json')]),
        ('log_sanitizer', ['sim/test_log_sanitizer.py']),
        ('board_evidence', ['build/check_board_acceptance.py', '--output', str(output / 'board_acceptance_check.json')]),
        ('touch', ['sim/test_touch_workflow.py']),
        ('rpc', ['sim/test_pl_rpc.py']),
        ('skill', ['skill/pynq-integration-checks/tests/validate_tools.py', '--workdir', str(output / 'skill')]),
        ('skill_reuse', ['skill/pynq-integration-checks/tests/validate_reuse.py', '--workdir', str(output / 'skill_reuse')]),
        ('vectors', ['sim/generate_core_vectors.py', '--output', str(output / 'vectors')]),
    ]
    records = []
    for name, arguments in jobs:
        result = subprocess.run([sys.executable, '-B', '-W', 'ignore::SyntaxWarning'] + arguments,
            cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
        (output / (name + '.log')).write_bytes(result.stdout)
        records.append(dict(name=name, returncode=result.returncode, passed=result.returncode == 0, log=name + '.log'))
        print(name, 'PASS' if result.returncode == 0 else 'FAIL', flush=True)
    report = dict(passed=all(r['passed'] for r in records), runs=records,
        python=platform.python_version(), platform=platform.system(),
        board_tested=False, hardware_operations=False, public_edition=True,
        log_scope='external_output_directory', log_base='--output', check_suite_version=5)
    (output / 'public_checks.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
