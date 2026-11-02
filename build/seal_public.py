"""Seal the public subset and record per-file license coverage."""
from pathlib import Path
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[1]


def files():
    return sorted(p for p in ROOT.rglob('*') if p.is_file()
        and '.git' not in p.relative_to(ROOT).parts and '__pycache__' not in p.parts)


def refresh_origin():
    path = ROOT / 'third_party/public_origin.json'
    origin = json.loads(path.read_text(encoding='utf8'))
    for row in origin['selected']:
        digest = hashlib.sha256((ROOT / row['path']).read_bytes()).hexdigest()
        row['public_sha256'] = digest
        row['modified_for_public_edition'] = digest != row['source_sha256']
    path.write_text(json.dumps(origin, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def refresh_snapshot_comparison():
    directory = ROOT / 'data/board_acceptance/cuboid_606'
    source = directory / 'tested_source_manifest.sha256'
    if not source.is_file():
        return
    original = {name: digest for digest, name in
                (line.split('  ', 1) for line in source.read_text(encoding='utf8').splitlines())}
    path = directory / 'snapshot_comparison.json'
    record = json.loads(path.read_text(encoding='utf8'))
    groups = {key: [] for key in ('identical', 'changed', 'not_at_original_path')}
    for name, digest in original.items():
        current = ROOT / name
        actual = hashlib.sha256(current.read_bytes()).hexdigest() if current.is_file() else None
        group = 'not_at_original_path' if actual is None else 'identical' if actual == digest else 'changed'
        groups[group].append(dict(path=name, tested_sha256=digest, current_sha256=actual))
    counts = {key: len(rows) for key, rows in groups.items()}
    outside = len({p.relative_to(ROOT).as_posix() for p in files()} - set(original))
    description = directory / 'tested_snapshot.md'
    text = description.read_text(encoding='utf8')
    text, replacements = re.subn(
        r'原204条清单与当前交付目录逐路径比较：.*?38份受测运行软件仍逐项相同。',
        '原204条清单与当前交付目录逐路径比较：%d条逐字节相同、%d条SHA不同、%d条未按原路径提供。'
        '当前目录另有%d份文件不在原清单路径中，包含说明、工具和新记录。38份受测运行软件仍逐项相同。'
        % (counts['identical'], counts['changed'], counts['not_at_original_path'], outside), text, flags=re.S)
    if replacements != 1:
        raise ValueError('Snapshot comparison paragraph is missing or ambiguous')
    description.write_text(text, encoding='utf8')
    for rows in groups.values():
        for row in rows:
            if row['path'] == description.relative_to(ROOT).as_posix():
                row['current_sha256'] = hashlib.sha256(description.read_bytes()).hexdigest()
    record.update(groups=groups, counts=counts, current_files_outside_original_paths=outside)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def main():
    refresh_origin()
    hardware_inventory = json.loads((ROOT / 'third_party/source_inventory.json').read_text(encoding='utf8'))
    hardware_licenses = {item['path']: item['license'] for item in hardware_inventory['files']}
    ledger = ROOT / 'third_party/file_licenses.json'
    names = {p.relative_to(ROOT).as_posix() for p in files()}
    names.update(['third_party/file_licenses.json', 'manifest.sha256'])
    mapping = {}
    for name in sorted(names):
        if name == 'LICENSE':
            license_id = 'MIT'
        elif name.startswith('hardware/') and not name.endswith('.md'):
            if name not in hardware_licenses:
                raise ValueError('Record hardware origin/license before sealing: ' + name)
            license_id = hardware_licenses[name]
        elif name in {'board/tools/acceptance_capture.ipynb', 'board/tools/source.json'} or name.startswith(('sim/results/', 'data/board_acceptance/', 'data/privacy/', 'data/verification/')):
            license_id = 'LicenseRef-Validation-Record'
        elif name.startswith('third_party/source_history/'):
            license_id = 'LicenseRef-Validation-Record'
        elif name.startswith('data/ai_collaboration/'):
            license_id = 'LicenseRef-Project-Evidence'
        elif name.startswith('third_party/authorization/'):
            license_id = 'LicenseRef-Authorization-Evidence'
        else:
            license_id = ('BSD-3-Clause' if name in {
            'sim/reference/pynq_v24_pl.py', 'sim/reference/pynq_v24_overlay.py', 'third_party/pynq_bsd.txt'}
            else 'OFL-1.1' if name == 'src/runtime/scan_font.bin' or name.startswith('third_party/fonts/')
            else 'MIT')
        mapping[name] = license_id
    ledger.write_text(json.dumps(mapping, indent=2) + '\n', encoding='utf8')
    refresh_snapshot_comparison()
    rows = [hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.relative_to(ROOT).as_posix()
        for p in files() if p.name != 'manifest.sha256']
    (ROOT / 'manifest.sha256').write_text('\n'.join(rows) + '\n', encoding='utf8')
    print('Sealed %d files plus manifest' % len(rows))


if __name__ == '__main__':
    main()
