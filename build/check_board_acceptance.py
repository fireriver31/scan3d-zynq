"""Verify archived board evidence and the ruler-based volume comparison."""
from pathlib import Path
import argparse
import csv
import hashlib
import json
import math
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / 'data/board_acceptance/cuboid_606'

def read(name):
    return json.loads((CASE / name).read_text(encoding='utf8'))

def verify():
    index = read('source_index.json')
    for row in index['files']:
        path = CASE / row['file']
        assert path.stat().st_size == row['bytes'], row['file']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256'], row['file']
    summary, result = read('acceptance_summary.json'), read('result.json')
    snapshot = read('tested_snapshot.json')
    manifest_path = CASE / snapshot['manifest_file']
    assert manifest_path.parent.resolve() == CASE.resolve()
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == snapshot['manifest_sha256'] == summary['public_source_manifest_sha256']
    tested_files = {name: sha for sha, name in (line.split('  ', 1) for line in manifest_path.read_text(encoding='utf8').splitlines())}
    assert len(tested_files) == snapshot['manifest_entries']
    assert len(snapshot['runtime_files']) == 38
    assert {row['path'] for row in snapshot['runtime_files']} == {'src/runtime/' + item['name'] for item in summary['expected_payload']['files'] if not item['name'].endswith(('.bit', '.hwh'))}
    assert snapshot['paired_artifacts'] == [item for item in summary['expected_payload']['files'] if item['name'].endswith(('.bit', '.hwh'))]
    assert snapshot['original_source_edition'] == summary['expected_payload']['source_edition']
    for row in snapshot['runtime_files']:
        assert row['path'].startswith('src/runtime/') and '..' not in Path(row['path']).parts
        actual = hashlib.sha256((ROOT / row['path']).read_bytes()).hexdigest()
        assert actual == row['sha256'] == tested_files[row['path']], row['path']

    assert not summary['test_fixture'] and summary['missing_expected_files'] == []
    assert summary['boot_id_at_start'] == summary['boot_id_at_export']
    for key in ('step8_completed_event','all_operator_checks_confirmed', 'expected_files_unchanged',
                'initialization_clock_passed','export_clock_matches_expected','ddr_reservation_confirmed'):
        assert summary[key], key
    assert all(value is True for value in summary['operator_checks'].values())
    expected = {row['name']: row for row in summary['expected_payload']['files']}
    assert len(expected) == 40 and len(summary['runtime_files']) == 40
    runtime_count = 0
    for name, item in expected.items():
        path = ROOT / 'src/runtime' / name
        if path.exists():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256'], name
            runtime_count += 1
        else:
            assert name in ('scan3d.bit', 'scan3d.hwh'), name
    assert runtime_count in (38, 40)
    assert all(row['matches'] and row['expected_sha256'] == row['actual_sha256'] == expected[row['file']]['sha256']
               for row in summary['runtime_files'])
    clock, ddr = read('clock_status.json'), read('ddr_reservation.json')
    assert summary['export_fclk0_mhz'] == clock['fclk0_register_mhz'] == 50.0
    assert clock['boot_id'] == ddr['boot_id'] == summary['boot_id_at_export']
    assert ddr['reservation_confirmed'] and any(row['no_map'] and row['covers_framebuffer'] for row in ddr['ranges'])
    events = read('touch_flow.json')['events']
    assert all(any(row['step'] == step and row['event'] == 'completed' for row in events) for step in range(1,9))
    elapsed = {}
    for step in (7,8):
        start = next(row['at_monotonic_s'] for row in events if row['step'] == step and row['event'] == 'started')
        end = next(row['at_monotonic_s'] for row in events if row['step'] == step and row['event'] == 'completed')
        elapsed[str(step)] = end-start
    exit_record = read('flow_exit.json')
    touch = read('touch_viewer.json')
    assert touch['mode'] == 'result' and touch['error'] is None
    assert touch['touch']['online'] and touch['touch']['errors'] == 0
    occupancy = np.load(str(CASE / 'occupancy.npy'), allow_pickle=False)
    assert occupancy.shape == (128,128,128) and occupancy.dtype == np.bool_
    count = int(np.count_nonzero(occupancy))
    volume = count*result['voxel_mm']**3/1000
    assert count == result['occupied_voxels']
    assert math.isclose(volume, result['volume_cm3'], abs_tol=1e-9)
    reference = read('volume_reference.json')
    assert reference['measurement_method'] == 'ruler_dimensions_cuboid_approximation'
    dimensions = reference['dimensions_cm']
    assert len(dimensions) == 3 and all(v > 0 for v in dimensions)
    reference_volume = dimensions[0]*dimensions[1]*dimensions[2]
    assert math.isclose(reference_volume, reference['reference_volume_cm3'], abs_tol=1e-9)
    with (CASE/'volume_comparison.csv').open(encoding='utf8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2
    for row, field in zip(rows, ('volume_cm3','detail_surface_volume_cm3')):
        estimate = float(row['estimated_volume_cm3'])
        assert row['result_field'] == field and math.isclose(estimate, result[field], abs_tol=1e-9)
        assert math.isclose(float(row['reference_volume_cm3']), reference_volume, abs_tol=1e-9)
        assert math.isclose(float(row['signed_error_cm3']), estimate-reference_volume, abs_tol=1e-9)
        assert math.isclose(float(row['relative_error_percent']), abs(estimate-reference_volume)/reference_volume*100, abs_tol=1e-9)
        assert row['predefined_pass_threshold_percent'] == '' and row['passed'] == ''
    assert rows[0]['role'] == 'primary_measurement' and rows[1]['role'] == 'display_shell'
    return dict(passed=True, evidence_files=len(index['files']), runtime_files_matched=runtime_count,
        board_record_present=True, hardware_access_performed=False,
        source_edition_tested=summary['expected_payload']['source_edition'],
        tested_snapshot_description=snapshot['description'],
        tested_snapshot_manifest_file='data/board_acceptance/cuboid_606/' + snapshot['manifest_file'],
        tested_runtime_snapshot_matches=len(snapshot['runtime_files']),
        tested_source_manifest_sha256=summary['public_source_manifest_sha256'],
        occupied_voxels=count, volume_cm3=volume, reference_volume_cm3=reference_volume,
        relative_error_percent=abs(volume-reference_volume)/reference_volume*100,
        reference_scope='one_sample_ruler_based_cuboid_estimate', repeats=1,
        operator_checks_source='manual_confirmation_in_returned_archive',
        step_elapsed_s=elapsed, flow_exit_error=exit_record['error'],
        clean_flow_exit_verified=not bool(exit_record['error']),
        shape_consistency_passed=result['shape_consistency_passed'],
        physical_accuracy_verified=result['physical_accuracy_verified'],
        shape_minimum_coverage=result['shape_consistency']['minimum_coverage'],
        raw_zip_in_public_repo=False)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists() or output == ROOT or ROOT in output.parents:
        parser.error('Choose a fresh external output file')
    report = verify()
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print(json.dumps(report,indent=2))

if __name__ == '__main__':
    main()
