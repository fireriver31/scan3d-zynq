"""Archive external test reports with their logs and explicit path redactions.

Raw outputs stay in the supplied external directories. Run seal_public.py after
archiving, then audit_public.py. This command does not run tests or access PL.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from log_sanitizer import sanitize_log

ROOT = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    for name in ('host', 'touch', 'core', 'project'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    sources = {name: Path(getattr(args, name)).resolve()
               for name in ('host', 'touch', 'core', 'project')}
    for directory in sources.values():
        if directory == ROOT or ROOT in directory.parents or not directory.is_dir():
            parser.error('Use existing test output directories outside this repository')
    result_dir = ROOT / 'sim/results'
    result_dir.mkdir(parents=True, exist_ok=True)
    prefixes = [(ROOT, '$REPO'), (Path.home(), '$USER')]
    prefixes += [(path, '$OUTPUT_' + name.upper()) for name, path in sources.items()]
    provenance = []

    def load(group, name):
        return json.loads((sources[group] / name).read_text(encoding='utf8'))

    def archive_file(group, source_name, destination):
        raw = (sources[group] / source_name).read_bytes()
        public, transforms = sanitize_log(raw, prefixes)
        target = result_dir / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(public)
        provenance.append(dict(path=destination, source_group=group, source_file=source_name,
                               raw_sha256=digest(raw), public_sha256=digest(public),
                               path_redacted=bool(transforms['path_replacements']), **transforms))

    def save(name, value):
        public, _ = sanitize_log(json.dumps(value, ensure_ascii=False).encode('utf8'), prefixes)
        value = json.loads(public.decode('utf8'))
        (result_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')

    host = load('host', 'public_checks.json')
    touch = load('touch', 'rtl_summary.json')
    core = load('core', 'core_rtl_checks.json')
    assert host['passed'] and touch['passed'] and core['passed'], 'Archive only successful complete suites'
    host['check_suite_version'] = host.get('check_suite_version', 3)
    assert len(host['runs']) == {3: 6, 4: 7, 5: 8}[host['check_suite_version']]
    assert len(touch['tests']) == 9 and len(core['cases']) == 5
    for item in host['runs']:
        source_name = item['log']
        assert Path(source_name).name == source_name
        item['log'] = 'logs/host/' + source_name
        archive_file('host', source_name, item['log'])
    for item in touch['tests']:
        item['run_log'] = 'logs/touch/' + Path(item['run_log']).name
    for item in core['cases']:
        item['run_log'] = 'logs/core/' + item['name'] + '_run.log'
    # Include compilation and elaboration too, so PASS text is not the only evidence.
    touch_logs = ['compile.log'] + [item['top'] + ending for item in touch['tests']
                                   for ending in ('_elaborate.log', '_run.log')]
    core_logs = ['recon_compile.log', 'recon_elaborate.log', 'photo_compile.log', 'photo_elaborate.log']
    core_logs += [item['name'] + '_run.log' for item in core['cases']]
    for group, names in [('touch', touch_logs), ('core', core_logs)]:
        for name in names:
            archive_file(group, name, 'logs/' + group + '/' + name)
    for name in ('project_check.txt', 'ip_status.rpt'):
        archive_file('project', name, 'logs/project/' + name)
    status = (sources['project'] / 'project_check.txt').read_text(encoding='utf8')
    assert 'PROJECT_RELOCATION=PASS' in status and 'BD_VALIDATION=PASS' in status
    for report in (host, touch, core):
        report.update(log_scope='repository', log_base='sim/results',
                      archive_note='Public log copies; raw hashes and path replacements are in log_provenance.json.')
    host['package_audit_scope'] = 'sealed repository before archiving this test run'
    touch['source_base'] = 'sim/rtl'
    save('public_checks.json', host)
    save('public_rtl_checks.json', touch)
    save('public_core_rtl_checks.json', core)
    save('public_skill_checks.json', load('host', 'skill/validation.json'))
    save('public_reuse_checks.json', load('host', 'skill_reuse/reuse_checks.json'))
    save('public_project_checks.json', dict(passed=True, tool='Vivado 2026.1',
        part='xc7z020clg400-1', project_relocation=True, bd_validation=True,
        synthesis_run=False, new_bit_generated=False, board_tested=False,
        source_inventory_sha256=digest((ROOT / 'third_party/source_inventory.json').read_bytes()),
        log_scope='repository', log_base='sim/results',
        log='logs/project/project_check.txt', ip_status='logs/project/ip_status.rpt'))
    save('log_provenance.json', dict(
        note='Original captured bytes remain in external output directories. Known path prefixes are replaced at byte level for UTF-8 and GB18030/GBK, then strictly decoded. Unknown encodings, replacement characters and unresolved user path tails are rejected.',
        path_tokens_supported=['$REPO', '$OUTPUT_HOST', '$OUTPUT_TOUCH', '$OUTPUT_CORE', '$OUTPUT_PROJECT', '$USER'],
        token_replacement_counts={token: sum(item['path_replacements'].get(token, 0) for item in provenance)
            for token in ['$REPO', '$OUTPUT_HOST', '$OUTPUT_TOUCH', '$OUTPUT_CORE', '$OUTPUT_PROJECT', '$USER']},
        log_base='sim/results', files=provenance))
    print('Archived %d log/report files with provenance' % len(provenance))


if __name__ == '__main__':
    main()
