"""Check scoped permission, source inventory and files without FPGA access."""
from pathlib import Path
import argparse
import ast
import csv
import hashlib
import importlib.util
import json
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from log_sanitizer import public_log_issues
from evidence_privacy import audit as audit_privacy
from check_board_acceptance import verify as verify_board_acceptance

ROOT = Path(__file__).resolve().parents[1]



def check_analysis_records():
    folder = ROOT / 'data/ai_collaboration'
    selected = json.loads((folder / 'selected_interactions.json').read_text(encoding='utf8'))
    document = (folder / 'selected_interactions.md').read_text(encoding='utf8')
    messages = [m for case in selected['cases'] for fragment in case['fragments'] for m in fragment['messages']]
    assert len(selected['cases']) == 3 and len(messages) == 7
    assert all(m['role'] in {'user', 'assistant'} and m['content'] in document for m in messages)
    references = [selected['design_note'], *selected['analysis_notes'], selected['self_correction']]
    for note in references:
        path = (ROOT / note['path']).resolve()
        assert ROOT.resolve() in path.parents and path.is_file(), note['path']
        assert ('## ' + note['heading']) in path.read_text(encoding='utf8')
    assert selected['self_correction']['authorship'] == 'assistant'
    assert selected['self_correction']['record_kind'] == 'assistant_technical_note_reference'
    for item in [*selected['cases'], selected['self_correction']]:
        for name in item.get('evidence', []):
            path = (ROOT / name).resolve()
            assert ROOT.resolve() in path.parents and path.is_file(), name
        for evidence in item.get('archived_evidence', []):
            assert evidence['scope'] == 'competition_submission'
            assert evidence['included_in_repository'] is False
            assert not (ROOT / evidence['path']).exists()
    source = json.loads((ROOT / 'board/tools/source.json').read_text(encoding='utf8'))
    notebook_path = ROOT / 'board/tools' / source['notebook_file']
    module_path = ROOT / 'board/tools' / source['module_file']
    assert hashlib.sha256(notebook_path.read_bytes()).hexdigest() == source['notebook_sha256']
    assert hashlib.sha256(module_path.read_bytes()).hexdigest() == source['module_sha256']
    notebook = json.loads(notebook_path.read_text(encoding='utf8'))
    cells = [''.join(c['source']) for c in notebook['cells'] if c['cell_type'] == 'code']
    original, extracted = ast.parse(cells[0]), ast.parse(module_path.read_text(encoding='utf8'), feature_version=(3, 6))
    export = lambda tree: next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_b_export')
    assert ast.dump(export(original)) == ast.dump(export(extracted))
    outputs = [o for c in notebook['cells'] for o in c.get('outputs', [])]
    assert len(outputs) == 13 and any('KeyboardInterrupt' in json.dumps(o) for o in outputs)
    assert not any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_board_state' for t in n.targets) for n in extracted.body)
    return dict(interaction_groups=3, original_messages=7, self_correction_cases=1,
        technical_topics=len(references)-1, local_references_resolve=True,
        external_evidence_scope_explicit=True, acceptance_writer_matches_notebook=True,
        executed_notebook_outputs=13, interrupt_output_preserved=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists() or output == ROOT or ROOT in output.parents:
        parser.error('Choose a fresh output file outside the repository')
    privacy = audit_privacy(ROOT)
    assert (ROOT / '.gitattributes').read_bytes() == b'* -text\n'
    origin = json.loads((ROOT / 'third_party/public_origin.json').read_text(encoding='utf8'))
    def source_manifest(record):
        path = ROOT / record['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record['sha256']
        return {name: digest for digest, name in (line.split('  ', 1) for line in path.read_text(encoding='utf8').splitlines())}
    current_sources = source_manifest(origin['source_manifest'])
    historical_sources = source_manifest(origin['historical_base']['source_manifest'])
    assert origin['source_snapshot'] == 'competition-v27'
    for row in origin['selected']:
        assert current_sources[row['source']] == row['source_sha256'], row['path']
        actual = hashlib.sha256((ROOT / row['path']).read_bytes()).hexdigest()
        assert actual == row['public_sha256']
        assert row['modified_for_public_edition'] == (actual != row['source_sha256'])
    for row in origin['historical_base']['selected']:
        assert historical_sources[row['source']] == row['source_sha256'], row['path']

    paths = [p for p in ROOT.rglob('*') if p.is_file() and '.git' not in p.relative_to(ROOT).parts
        and '__pycache__' not in p.parts]
    names = {p.relative_to(ROOT).as_posix() for p in paths}
    forbidden = [n for n in names if n.endswith(('.bit', '.hwh'))]
    assert not forbidden, forbidden
    assert (ROOT / 'LICENSE').read_text(encoding='utf8').startswith('Mixed licensing')
    assert (ROOT / 'licenses/mit.txt').read_text(encoding='utf8').startswith('MIT License')
    assert 'SIL OPEN FONT LICENSE' in (ROOT / 'third_party/fonts/OFL.txt').read_text(encoding='utf8')
    assert 'All rights reserved.' in (ROOT / 'sim/reference/pynq_v24_pl.py').read_text(encoding='utf8')
    auth_dir = ROOT / 'third_party/authorization'
    auth = json.loads((auth_dir / '20261101_acz702_permission_v1.json').read_text(encoding='utf8'))
    assert auth['reply_verbatim'] == '可以' and auth['source_distribution_permission']
    assert auth['stated_noncommercial_commitment'] and not auth['blanket_mit_granted']
    assert auth['requester_account_omitted_from_public_text']
    assert auth['responder_account_as_displayed'] == 'zgmxs88:小梅妹'
    assert hashlib.sha256((auth_dir / auth['screenshot_file']).read_bytes()).hexdigest() == auth['screenshot_sha256']
    assert hashlib.sha256((auth_dir / auth['transcript_file']).read_bytes()).hexdigest() == auth['transcript_sha256']
    source_inventory = json.loads((ROOT / 'third_party/source_inventory.json').read_text(encoding='utf8'))
    source_entries = {item['path']: item for item in source_inventory['files']}
    assert len(source_entries) == 41
    for name, item in source_entries.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == item['current_sha256'], name
    assert (ROOT / 'hardware/vivado/ov5640_ddr3_tft_hdmi.xpr').is_file()
    assert all((ROOT / 'build' / name).is_file() for name in ['build_hardware.tcl', 'check_project.tcl', 'synthesis_resources.tcl'])
    python36 = []
    for path in (ROOT / 'src/runtime').glob('*.py'):
        ast.parse(path.read_text(encoding='utf8'), feature_version=(3, 6))
        python36.append(path.name)
    cells = 0
    for path in (ROOT / 'src/runtime').glob('*.ipynb'):
        notebook = json.loads(path.read_text(encoding='utf8'))
        for cell in notebook['cells']:
            if cell['cell_type'] == 'code':
                ast.parse(''.join(cell['source']), feature_version=(3, 6))
                assert not cell.get('outputs'), path
                cells += 1
    links = []
    for path in ROOT.rglob('*.md'):
        for target in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding='utf8')):
            if target.startswith(('http:', 'https:', '#', 'mailto:')):
                continue
            if not (path.parent / target.split('#')[0]).exists():
                links.append([path.relative_to(ROOT).as_posix(), target])
    assert not links, links
    personal = re.compile(
        rb'[A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+'
        rb'|[A-Za-z]:[\\/]+[^\r\n\"\']*[\\/]\d{2,4}\.\d{1,2}\.\d{1,2}[\\/]'
        rb'|\$(?:USER|HOME)[\\/]+\S'
        rb'|(?:Documents|Desktop)[\\/]+[^\r\n\"\']*[\\/]\d{2}\.\d{1,2}\.\d{1,2}[\\/]', re.I)
    path_findings = [p.relative_to(ROOT).as_posix() for p in paths
        if p.suffix in {'.py', '.md', '.json', '.txt', '.log', '.rpt', '.jsonl', '.csv', '.v', '.sv', '.tcl', '.xdc', '.xpr', '.bd', '.xci'} and personal.search(p.read_bytes())]
    assert not path_findings, path_findings
    secret_patterns = [rb'\bgh[pousr]_[A-Za-z0-9_]{25,}\b', rb'\bgithub_pat_[A-Za-z0-9_]{30,}\b',
        rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----']
    secret_findings = [p.relative_to(ROOT).as_posix() for p in paths
        if p.suffix in {'.py', '.md', '.json', '.txt', '.log', '.csv'}
        and any(re.search(pattern, p.read_bytes()) for pattern in secret_patterns)]
    assert not secret_findings, secret_findings
    font = ROOT / 'src/runtime/scan_screen_font.py'
    spec = importlib.util.spec_from_file_location('public_font', font)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    chars = (ROOT / 'tools/font_characters.txt').read_text(encoding='utf8').rstrip('\r\n')
    assert module.CHARS == chars
    assert len(chars) == len(set(chars)) == 766
    assert all(len(module.packed(c)) == 128 and any(module.packed(c)) for c in chars)
    assert module.packed('\U0010ffff') is None
    licenses = json.loads((ROOT / 'third_party/file_licenses.json').read_text(encoding='utf8'))
    assert set(licenses) == names
    assert licenses['LICENSE'] == 'LicenseRef-Mixed-License-Index'
    assert licenses['licenses/mit.txt'] == 'MIT'
    for name, item in source_entries.items():
        assert licenses[name] == item['license'], name
    for name in names:
        if name.startswith('third_party/authorization/'):
            assert licenses[name] == 'LicenseRef-Authorization-Evidence'
    notice = (ROOT / 'THIRD_PARTY_NOTICES.md').read_text(encoding='utf8')
    assert 'LicenseRef-ACZ702-Project-Permission' in notice and '非商业' in notice
    assert 'THIRD_PARTY_NOTICES.md' in (ROOT / 'README.md').read_text(encoding='utf8')
    # Archived summaries must resolve to shipped files, not the runner's external cwd.
    result_dir = ROOT / 'sim/results'
    provenance = json.loads((result_dir / 'log_provenance.json').read_text(encoding='utf8'))
    log_entries = {item['path']: item for item in provenance['files']}
    assert len(log_entries) == len(provenance['files'])
    assert set(log_entries) == {path.relative_to(result_dir).as_posix()
                                for path in (result_dir / 'logs').rglob('*') if path.is_file()}
    for name, item in log_entries.items():
        path = (result_dir / name).resolve()
        assert result_dir.resolve() in path.parents and path.is_file(), name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['public_sha256'], name
        assert not public_log_issues(path.read_text(encoding='utf8', errors='strict')), name
        assert item['lossy_decode_used'] is False
        assert len(item['raw_sha256']) == 64 and all(c in '0123456789abcdef' for c in item['raw_sha256'])
        assert licenses[path.relative_to(ROOT).as_posix()] == 'LicenseRef-Validation-Record'
    linked_logs = 0
    counts = {token: sum(item['path_replacements'].get(token, 0) for item in log_entries.values())
              for token in provenance['path_tokens_supported']}
    assert counts == provenance['token_replacement_counts']
    for file_name, collection, field, expected in [
            ('public_checks.json', 'runs', 'log', None),
            ('public_rtl_checks.json', 'tests', 'run_log', 9),
            ('public_core_rtl_checks.json', 'cases', 'run_log', 5)]:
        summary = json.loads((result_dir / file_name).read_text(encoding='utf8'))
        if collection == 'runs':
            expected = {3: 6, 4: 7, 5: 8}[summary['check_suite_version']]
        assert summary['passed'] and summary['log_scope'] == 'repository'
        assert summary['log_base'] == 'sim/results' and len(summary[collection]) == expected
        for case in summary[collection]:
            assert case['passed'] and case[field] in log_entries, (file_name, case)
            linked_logs += 1
    project = json.loads((result_dir / 'public_project_checks.json').read_text(encoding='utf8'))
    assert project['passed'] and project['log_scope'] == 'repository'
    assert project['log'] in log_entries and project['ip_status'] in log_entries
    assert project['source_inventory_sha256'] == hashlib.sha256((ROOT / 'third_party/source_inventory.json').read_bytes()).hexdigest()
    # Historical CSV sources use their own archive namespace; hashes are indexes, not raw evidence.
    evidence = json.loads((ROOT / 'data/evidence_sources.json').read_text(encoding='utf8'))
    indexed = {item['path']: item for item in evidence['files']}
    with (ROOT / 'data/reference_comparisons.csv').open(encoding='utf8', newline='') as stream:
        comparisons = list(csv.DictReader(stream))
    assert len(comparisons) == 3
    for row in comparisons:
        assert row['actual_source_scope'] == evidence['scope'] == 'competition_submission_v11'
        assert row['evidence_in_public_repo'] == 'False'
        assert not (ROOT / row['actual_source']).exists()
        assert row['actual_source_sha256'] == indexed[row['actual_source']]['sha256']
        assert not indexed[row['actual_source']]['evidence_in_public_repo']
        if row['reference_in_public_repo'] == 'True':
            assert (ROOT / row['reference'].split(':')[0]).is_file()
    xpr = (ROOT / 'hardware/vivado/ov5640_ddr3_tft_hdmi.xpr').read_text(encoding='utf8')
    assert 'uart_hp_ddr3_tft800x480_hdmi' not in xpr
    assert len(re.findall(r'<File Path="', xpr)) == 40
    main_notebook = json.loads((ROOT / 'src/runtime/scan_workflow.ipynb').read_text(encoding='utf8'))
    initial = next(cell for cell in main_notebook['cells'] if cell['cell_type'] == 'code')
    assert "COLOR_BIT = DATA_DIR / 'scan3d.bit'" in ''.join(initial['source'])
    report_text = (ROOT / 'report/design_report.md').read_text(encoding='utf8')
    assert len(re.findall(r'^## \d+\.', report_text, re.M)) == 12
    manifest = {}
    for line in (ROOT / 'manifest.sha256').read_text(encoding='utf8').splitlines():
        digest, name = line.split('  ', 1)
        manifest[name] = digest
    assert manifest == {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths if p.name != 'manifest.sha256'}
    board_evidence = verify_board_acceptance()
    analysis_records = check_analysis_records()
    report = dict(passed=True, privacy=privacy, analysis_records=analysis_records, files=len(paths), runtime_python36=len(python36),
        notebook_cells=cells, glyphs=len(chars), missing_markdown_links=[],
        local_personal_path_findings=[], authorization_evidence_hashes_verified=True,
        hardware_source_files=len(source_entries), vendor_material_not_mit=True,
        full_system_bit_absent=True, per_file_license_coverage=True, board_record_present=board_evidence['board_record_present'],
        hardware_access_performed=False,
        linked_test_logs=linked_logs, archived_log_files=len(log_entries),
        historical_data_scope_explicit=True, design_report_chapters=12,
        obsolete_import_paths_absent=True, deployment_bit_name='scan3d.bit')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
