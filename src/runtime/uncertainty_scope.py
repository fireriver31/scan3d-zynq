"""PL-supported ambiguity, explicitly a possible envelope, never material.

No colour classifier runs densely on PS. Existing actual PL prototype decisions
provide the two compatible explanations. Sparse original-photo texture can veto
background. Native strong foreground brackets bound the search, not occupancy.
"""
from pathlib import Path
import hashlib
import json
import numpy as np

VERSION = 69
ORANGE = (245, 160, 20)


def digest(a):
    return hashlib.sha256(np.asarray(a, dtype=np.uint8).tobytes()).hexdigest()


def overlap_from_pl(scan):
    """Do not refit or approximate any PL colour likelihood on the CPU."""
    if '_PL_overlap_support_v69' in scan:
        return np.asarray(scan['_PL_overlap_support_v69'], dtype=bool)
    needed = ('pl_actual_pass_packed', 'pl_actual_pass_kind', 'pl_actual_pass_frame_indices')
    if not all(k in scan for k in needed):
        return None
    import pl_mask
    n = len(scan['masks'])
    result = np.zeros(np.shape(scan['masks']), dtype=bool)
    kinds = np.asarray(scan['pl_actual_pass_kind'])
    frames = np.asarray(scan['pl_actual_pass_frame_indices'])
    packed = np.asarray(scan['pl_actual_pass_packed'])
    if len(kinds) != len(frames) or len(packed) != len(frames):
        raise ValueError('实际PL判定记录长度不一致')
    for i in range(n):
        foreground = np.zeros(result.shape[1:], dtype=bool)
        background = foreground.copy()
        have_fg = have_bg = False
        for j in np.flatnonzero(frames == i):
            kind = str(kinds[j])
            if not kind.startswith(('prototype_foreground_', 'prototype_background_')):
                continue
            strong, _ = pl_mask.unpack_decisions(packed[j])
            if kind.startswith('prototype_foreground_'):
                foreground |= ~strong
                have_fg = True
            else:
                background |= ~strong
                have_bg = True
        if have_fg and have_bg:
            result[i] = foreground & background
    return result


def brackets(core):
    """Search only between measured foreground in BOTH native directions."""
    horizontal = np.zeros_like(core, dtype=bool)
    vertical = horizontal.copy()
    for y, row in enumerate(core):
        x = np.flatnonzero(row)
        if len(x) > 1:
            horizontal[y, x[0]:x[-1]+1] = True
    for x, column in enumerate(core.T):
        y = np.flatnonzero(column)
        if len(y) > 1:
            vertical[y[0]:y[-1]+1, x] = True
    # A vertical bracket alone can include the visible background of a neck,
    # arch or other concavity. Requiring BOTH prevents turning that exterior
    # background into a connecting column merely because its colour overlaps.
    return horizontal & vertical


def review(scan, calibration, directory=None, progress=None, maximum_centers=512):
    """Return extra UNKNOWN pixels. Strict foreground/background stay immutable.

    Equal empty-scene colour is not proof of an empty ray when PL also admits
    measured object colours. Such a ray is unresolved. An envelope using these
    rays MAY include real background/holes; it must be displayed as uncertain.
    """
    import types
    import pl_mask
    import interior_evidence as interior
    strict = np.asarray(scan['masks'], dtype=bool)
    upper = np.asarray(scan.get('conservative_surface_masks', strict), dtype=bool)
    extra = np.zeros_like(strict)
    info = dict(version=VERSION, performed=False, added_unknown_pixels=0,
                strict_foreground_changed=False, measured_material_added=False,
                uniform_hole_fill=False, outline_dilation=False,
                unknown_may_be_real_background=True, unresolved_true_holes_may_close=True,
                source_upper_sha256=digest(upper), maximum_patch_centers_per_view=maximum_centers,
                search_domain='native_foreground_brackets_in_both_rows_and_columns',
                photo_color_is_not_material_proof=True, per_view=[])
    if int(scan.get('single_component_filter_version',0))>=81 and str(scan.get('single_component_filter_mode',''))=='strict_largest_8_connected_component':
        info.update(reason='user_strict_main_component_scope_no_supplementary_expansion',
                    single_component_policy_enforced=True)
        return extra,info
    required = ('color_frames565', 'empty_color_frame565', 'pl_luts',
                'photo_strong_masks', 'interior_confirmed_background_masks',
                'interior_unknown_masks')
    if scan.get('_uncertainty_PL_archive_mismatch_v69'):
        info['reason'] = 'actual_PL_records_do_not_match_saved_source'
        return extra, info
    if (scan.get('source') != 'pl_color_photos' or
            int(scan.get('low_contrast_recovery_version', 0)) < 66 or
            not all(k in scan for k in required)):
        info['reason'] = 'original_PL_prototype_evidence_unavailable'
        return extra, info
    overlap = overlap_from_pl(scan)
    if overlap is None or overlap.shape != strict.shape:
        info['reason'] = 'actual_PL_prototype_decisions_unavailable'
        return extra, info
    if not np.isfinite(maximum_centers) or not 1 <= maximum_centers <= 1024:
        raise ValueError('背景纹理核验预算无效')
    confirmed = np.asarray(scan['interior_confirmed_background_masks'], dtype=bool)
    if confirmed.shape != strict.shape or upper.shape != strict.shape:
        raise ValueError('三态证据尺寸不一致')
    window = interior.measurement_window(calibration, calibration['v_ground'])
    left, _, right, bottom = window
    allowed = np.zeros(strict.shape[1:], bool)
    allowed[:bottom, left:right] = True
    driver = types.SimpleNamespace(**vars(pl_mask))
    frames = np.asarray(scan['color_frames565'])
    if frames.shape != strict.shape or frames.dtype != np.uint16:
        raise ValueError('原图与正式掩膜不对应')
    for key, value in (('color_frames_sha256', frames),
                       ('empty_frame_sha256', scan['empty_color_frame565'])):
        if key in scan and str(scan[key]) != hashlib.sha256(
                np.asarray(value, dtype='<u2').tobytes()).hexdigest():
            raise ValueError('待定核验输入与采集指纹不对应：'+key)
    for i in range(len(strict)):
        if progress is not None:
            progress.update('保留PL双重解释的待定射线', '%d/%d' % (i+1, len(strict)))
        core = interior._erode(np.asarray(scan['photo_strong_masks'][i], bool) & strict[i], 1)
        candidate = brackets(core) & overlap[i] & allowed & ~upper[i] & ~confirmed[i]
        yy, xx = np.nonzero(candidate)
        legal = (yy >= 3) & (yy < strict.shape[1]-3) & (xx >= 3) & (xx < strict.shape[2]-3)
        yy, xx = yy[legal], xx[legal]
        # Unsampled pixels remain UNKNOWN, never certified foreground.
        take = np.linspace(0, len(yy)-1, min(int(maximum_centers), len(yy))).astype(int) if len(yy) else np.empty(0, int)
        coords = np.stack((yy[take], xx[take]), axis=1)
        same, _, _ = interior._patch_evidence(frames[i], scan['empty_color_frame565'],
            scan['pl_luts'][i], scan.get('background_noise_sigma'), coords, driver)
        veto = coords[same]
        if len(veto):
            candidate[veto[:, 0], veto[:, 1]] = False
        extra[i] = candidate
        info['per_view'].append(dict(view=i+1, original_unknown_pixels=int(
            np.asarray(scan['interior_unknown_masks'][i], bool).sum()),
            added_unknown_pixels=int(candidate.sum()), patch_centers=len(coords),
            matching_background_centers=int(same.sum())))
    info.update(performed=True, reason='overlapping_PL_explanations_retained_as_unknown_not_material',
                added_unknown_pixels=int(extra.sum()), added_unknown_sha256=digest(extra),
                audited_upper_sha256=digest(upper | extra))
    if directory is not None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory/'1024_uncertainty_scope_v69.npz'
        np.savez_compressed(str(path), added_unknown_masks=extra, surface_masks=upper | extra)
        info['surface_masks_file'] = path.name
        (directory/'1024_uncertainty_scope_v69.json').write_text(
            json.dumps(info, ensure_ascii=False, indent=2), encoding='utf8')
    return extra, info


def save_voxels(path, mask):
    a = np.asarray(mask, dtype=bool)
    np.savez_compressed(str(path), bits=np.packbits(a.ravel()), shape=np.asarray(a.shape, np.int16))


def load_voxels(path, shape):
    with np.load(str(path), allow_pickle=False) as archive:
        actual = tuple(map(int, archive['shape']))
        if actual != tuple(shape):
            raise ValueError('待定体素与本次网格不对应')
        bits = np.unpackbits(archive['bits'])
        return bits[:int(np.prod(actual))].reshape(actual).astype(bool)


def dependency_mask(occupied, pitch, strict, previous_upper, angles, calibration, batch=65536):
    """Label uncertainty only among existing voxels; never carve full grids twice.

    Since strict <= previous_upper <= audited_upper, their intersections are
    subsets of the new hull. Testing its survivors against those masks is
    exactly the same set operation, with far fewer samples and less memory.
    """
    from ps_demo import plan_geometry
    occ = np.asarray(occupied, bool)
    lower = np.asarray(strict, bool)
    prior = np.asarray(previous_upper, bool)
    a = np.asarray(angles, float)
    if lower.shape != prior.shape or lower.ndim != 3 or len(a) != len(lower):
        raise ValueError('不确定体素核验的视角不对应')
    if occ.ndim != 3:
        raise ValueError('不确定体素网格必须为三维')
    n = occ.shape[0]
    if occ.shape != (n,n,n) or not np.isfinite(pitch) or pitch <= 0 or batch < 1:
        raise ValueError('不确定体素网格无效')
    zz, yy, xx = np.nonzero(occ)
    original = np.ones(len(zz), bool)
    measured = np.ones(len(zz), bool)
    roi = tuple(map(int, calibration.get('reconstruction_roi_xyxy', (160,0,640,480))))
    x0,y0,x1,y1 = roi
    model = calibration.get('turntable_camera')
    if model is None:
        kv,ofv,kux,kuy,ofu = plan_geometry(a,n,x1-x0,y1-y0,pitch,
            calibration['mm_per_px'],calibration['axis_col']-x0,calibration['v_ground']-y0)
    else:
        import turntable_camera as camera
        camera.validate(model)
        t = camera.translation(model)
        focal = model['focal_px']
        cx,cy = model['principal_point']
    for i,angle in enumerate(a):
        if model is not None:
            rotation = camera.rotation(model, angle)
        for start in range(0, len(zz), batch):
            stop = min(start+batch, len(zz))
            z,y,x = zz[start:stop],yy[start:stop],xx[start:stop]
            if model is None:
                u = ((int(ofu[i])+x*int(kux[i])+y*int(kuy[i]))>>16)+x0
                v = ((int(ofv)-z*int(kv))>>16)+y0
                legal = (u>=x0)&(u<x1)&(v>=y0)&(v<y1)
            else:
                X = (x-(n-1)/2.)*pitch
                Y = (y-(n-1)/2.)*pitch
                # Match camera.carve's operation order, including half-pixel ties.
                b = [t[k]+rotation[k,0]*X+rotation[k,1]*Y for k in range(3)]
                depth = b[2]+rotation[2,2]*z*pitch
                safe = np.where(depth>0,depth,1.)
                u = np.floor(cx+focal*(b[0]+rotation[0,2]*z*pitch)/safe+.5).astype(np.intp)
                v = np.floor(cy+focal*(b[1]+rotation[1,2]*z*pitch)/safe+.5).astype(np.intp)
                legal = (depth>0)&(u>=0)&(u<lower.shape[2])&(v>=0)&(v<lower.shape[1])
            u = np.clip(u,0,lower.shape[2]-1)
            v = np.clip(v,0,lower.shape[1]-1)
            original[start:stop] &= legal & prior[i,v,u]
            measured[start:stop] &= legal & lower[i,v,u]
    display = np.zeros_like(occ)
    selected = ~original
    display[zz[selected],yy[selected],xx[selected]] = True
    return display, dict(uncertain_voxels=int(len(zz)-measured.sum()),
        original_possible_voxels=int(original.sum()),
        displayed_new_unknown_voxels=int(selected.sum()),
        PS_strict_voxels=int(measured.sum()), possible_envelope_voxels=int(len(zz)),
        dependency_samples=int(len(zz)*len(a)),
        full_cube_recarving_for_labels=False)
