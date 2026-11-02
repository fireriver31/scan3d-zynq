"""Release saved-only diagnostics before reconstruction on a 512MB board.

An archive must match each removed array, streamed without a second large
allocation. Raw photos, strict masks and source archives are never modified.
"""
from pathlib import Path
import gc
import hashlib
import sys
import zipfile
import numpy as np

VERSION = 69
DROP = ('low_contrast_baseline_masks', 'low_contrast_recovered_masks',
        'low_contrast_unknown_masks', 'main_core_protection_masks',
        'pl_actual_pass_packed', 'pl_actual_pass_luts',
        'pl_actual_pass_inverse_variances', 'pl_actual_pass_low_q16',
        'pl_actual_pass_high_q16', 'pl_baseline_packed_decisions',
        'background_model_grids', 'roi_modeled_references565')
BINARY = ('masks', 'conservative_surface_masks', 'interior_unknown_masks',
          'interior_inferred_masks', 'interior_confirmed_background_masks',
          'photo_strong_masks', 'photo_unknown_masks')


def storage_bytes(scan):
    owners = {}
    for value in scan.values():
        if isinstance(value, np.ndarray):
            while isinstance(value.base, np.ndarray):
                value = value.base
            owners[id(value)] = value.nbytes
        elif isinstance(value, str):
            owners[id(value)] = sys.getsizeof(value)
    return int(sum(owners.values()))


def _matches(archive, key, value):
    if not isinstance(value, np.ndarray) or value.dtype.hasobject or not value.flags.c_contiguous:
        return False
    try:
        with archive.open(key+'.npy') as stream:
            version = np.lib.format.read_magic(stream)
            if version == (1, 0):
                shape, order, dtype = np.lib.format.read_array_header_1_0(stream)
            elif version == (2, 0):
                shape, order, dtype = np.lib.format.read_array_header_2_0(stream)
            else:
                return False
            if shape != value.shape or order or dtype != value.dtype:
                return False
            digest = hashlib.sha256()
            total = 0
            while True:
                data = stream.read(1024*1024)
                if not data:
                    break
                digest.update(data)
                total += len(data)
            return total == value.nbytes and digest.digest() == hashlib.sha256(memoryview(value)).digest()
    except KeyError:
        return False


def compact(scan, archive_path):
    """Mutate the shared scan dictionary only after on-disk equality checks."""
    if scan.get('runtime_memory_version') == VERSION:
        return dict(scan['runtime_memory_diagnostics'])
    before = storage_bytes(scan)
    # np.load returns 0-D arrays even for strings/bools. The cached route must
    # recover Python metadata before report JSON serialization, as fresh scans
    # already do. This also releases large UCS-4 diagnostic string wrappers.
    for key, value in list(scan.items()):
        if isinstance(value, np.ndarray) and value.ndim == 0:
            scan[key] = value.item()
    info = dict(version=VERSION, before_storage_bytes=before, removed_keys=[],
                source_archive=str(archive_path or ''), source_arrays_changed=False)
    path = Path(archive_path) if archive_path else None
    if path is None or not path.is_file():
        info.update(reason='matching_saved_archive_unavailable_no_arrays_removed', after_storage_bytes=before)
        return info
    with zipfile.ZipFile(str(path)) as archive:
        # Validate source masks before reusing its PL prototype records.
        for key in ('masks', 'conservative_surface_masks', 'interior_unknown_masks'):
            if key not in scan or not _matches(archive, key, scan[key]):
                info.update(reason='saved_mask_mismatch_no_arrays_removed', after_storage_bytes=before)
                return info
        matched = [key for key in DROP if key in scan and _matches(archive, key, scan[key])]
        if 'pl_actual_pass_packed' in scan and 'pl_actual_pass_packed' not in matched:
            scan['_uncertainty_PL_archive_mismatch_v69'] = True
        if 'pl_actual_pass_packed' in matched:
            import uncertainty_scope
            overlap = uncertainty_scope.overlap_from_pl(scan)
            if overlap is not None:
                scan['_PL_overlap_support_v69'] = overlap
        # A failed match leaves that particular diagnostic array in memory.
        for key in matched:
            scan.pop(key)
        info['removed_keys'] = matched
    for key in BINARY:
        value = scan.get(key)
        if not isinstance(value, np.ndarray) or value.dtype not in (np.dtype('uint8'), np.dtype('bool')):
            continue
        if value.size and (value.max() > 1):
            continue
        if not value.any():
            scan[key] = np.broadcast_to(np.zeros((), dtype=bool), value.shape)
        else:
            scan[key] = value.view(np.bool_) if value.dtype == np.uint8 else value
    gc.collect()
    info.update(reason='saved_diagnostics_released_exactly', after_storage_bytes=storage_bytes(scan))
    scan['diagnostic_archive_path'] = str(path.resolve())
    scan['runtime_memory_version'] = VERSION
    scan['runtime_memory_diagnostics'] = dict(info)
    return info


