"""Exercise the unchanged tools on unrelated metadata and array fixtures.

Fixtures establish offline portability only, never physical cross-board use.
"""
from pathlib import Path
import argparse
import importlib.util
import json
import struct
import subprocess
import sys
import numpy as np

HERE = Path(__file__).resolve().parents[1]


def bit_bytes(part):
    magic = b'\x0f\xf0\x0f\xf0\x0f\xf0\x0f\xf0\x00'
    data = struct.pack('>H', len(magic)) + magic + struct.pack('>H', 1)
    for tag, value in [(b'a', b'unrelated_design\0'), (b'b', part.encode('ascii') + b'\0'),
                       (b'c', b'2026/10/31\0'), (b'd', b'12:00:00\0')]:
        data += tag + struct.pack('>H', len(value)) + value
    return data + b'e' + struct.pack('>I', 4) + b'\0' * 4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workdir', required=True)
    args = parser.parse_args()
    work = Path(args.workdir).resolve()
    if work.exists() or HERE == work or HERE in work.parents:
        parser.error('Choose a fresh external work directory')
    work.mkdir(parents=True)
    spec = importlib.util.spec_from_file_location('reuse_compare', HERE / 'scripts/compare_arrays.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checks = []
    for device, package, ip in [('7z010', 'clg400', 'vector_add_0'),
                                ('7z045', 'ffg900', 'sensor_filter_0'),
                                ('zu3eg', 'sbva484', 'histogram_0')]:
        folder = work / (device + '_fixture')
        folder.mkdir()
        bit, hwh, output = folder / 'design.bit', folder / 'design.hwh', folder / 'report.json'
        bit.write_bytes(bit_bytes(device + package))
        hwh.write_text('<EDKSYSTEM VIVADOVERSION="fixture"><SYSTEMINFO DEVICE="' + device +
                       '" PACKAGE="' + package + '"/><MODULE INSTANCE="' + ip + '"/></EDKSYSTEM>', encoding='utf8')
        command = [sys.executable, str(HERE / 'scripts/overlay_audit.py'), '--bit', str(bit),
                   '--hwh', str(hwh), '--expect-ip', ip, '--out', str(output)]
        completed = subprocess.run(command, cwd=str(work), stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=30)
        result = json.loads(output.read_text(encoding='utf8'))
        assert completed.returncode == 0 and result['passed']
        assert result['modules'] == [ip] and not result['proves_loaded_hardware']
        checks.append(dict(case=device + '_metadata_fixture', passed=True, synthetic=True,
                           independent_working_directory=True, real_board_test=False))
    for task, values in [('vector_add', (np.arange(32, dtype=np.int32) * 3).reshape(4, 8)),
                         ('sensor_frames', np.arange(15, dtype=np.uint16).reshape(5, 3))]:
        folder = work / task
        folder.mkdir()
        np.savez(str(folder / 'actual.npz'), **{task: values})
        np.savez(str(folder / 'reference.npz'), **{task: values.copy()})
        assert module.compare(folder / 'actual.npz', folder / 'reference.npz', [task])['passed']
        altered = values.copy()
        altered.flat[-1] += 1
        np.savez(str(folder / 'wrong.npz'), **{task: altered})
        result = module.compare(folder / 'wrong.npz', folder / 'reference.npz', [task])
        assert not result['passed'] and result['fields'][0]['mismatched_elements'] == 1
        checks.append(dict(case=task + '_arrays', passed=True, synthetic=True,
                           no_scan_specific_fields=True, corrupted_element_detected=True))
    report = dict(passed=True, cases=checks, offline_fixture_cases=len(checks),
                  no_scan_runtime_imported=True, physical_cross_board_verified=False,
                  real_hardware_writes=False,
                  scope='Different metadata devices and unrelated tasks; synthetic fixtures, not board measurements')
    (work / 'reuse_checks.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
