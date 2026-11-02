"""Seal the public subset and record per-file license coverage."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]


def files():
    return sorted(p for p in ROOT.rglob('*') if p.is_file()
        and '.git' not in p.relative_to(ROOT).parts and '__pycache__' not in p.parts)


def main():
    hardware_inventory = json.loads((ROOT / 'third_party/source_inventory.json').read_text(encoding='utf8'))
    hardware_licenses = {item['path']: item['license'] for item in hardware_inventory['files']}
    ledger = ROOT / 'third_party/file_licenses.json'
    names = {p.relative_to(ROOT).as_posix() for p in files()}
    names.update(['third_party/file_licenses.json', 'manifest.sha256'])
    mapping = {}
    for name in sorted(names):
        if name == 'LICENSE':
            license_id = 'LicenseRef-Mixed-License-Index'
        elif name.startswith('hardware/') and not name.endswith('.md'):
            if name not in hardware_licenses:
                raise ValueError('Record hardware origin/license before sealing: ' + name)
            license_id = hardware_licenses[name]
        elif name in {'board/tools/acceptance_capture.ipynb', 'board/tools/source.json'} or name.startswith(('sim/results/', 'data/board_acceptance/', 'data/privacy/')):
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
    rows = [hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.relative_to(ROOT).as_posix()
        for p in files() if p.name != 'manifest.sha256']
    (ROOT / 'manifest.sha256').write_text('\n'.join(rows) + '\n', encoding='utf8')
    print('Sealed %d files plus manifest' % len(rows))


if __name__ == '__main__':
    main()
