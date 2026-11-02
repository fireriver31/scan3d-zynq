"""Evidence-based photo segmentation. NumPy/Pillow only; Python 3.6 compatible.

No object classes, target dimensions, hole filling, white-balance or model-specific
parameters. Unknown pixels are retained only when connected to measured foreground.
The thresholds control background false alarms, not physical accuracy guarantees.
"""
import time

import numpy as np


ALGORITHM_VERSION = 7
ALGORITHM_NAME = "validated_noise_hysteresis_reference_check_v7"
IMPLEMENTATION_VERSION = 18
FALSE_ALARM_RATE = 0.005


def _pack(rgb):
    a = np.clip(np.rint(rgb), 0, 255).astype(np.uint16)
    return ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)


def _rgb(a):
    a = np.asarray(a, dtype=np.uint16)
    r, g, b = (a >> 11) & 31, (a >> 5) & 63, a & 31
    return np.stack(((r << 3) | (r >> 2), (g << 2) | (g >> 4),
                     (b << 3) | (b >> 2)), axis=-1).astype(np.float32)


def capture_background_reference(grab_fn, n_frames=8, sleeper=time.sleep):
    """Automatically collect an empty-table noise reference in the existing step 2.

    Different buffers do not prove exposure/frame synchronization. Record duplicate
    samples and fall back to a single-frame reference when no update is observable.
    """
    if not isinstance(n_frames, int) or not 1 <= n_frames <= 16:
        raise ValueError("背景参考采样数必须是1～16之间的整数")
    frames = []
    for _ in range(n_frames):
        frame = np.asarray(grab_fn()).copy()
        if frame.dtype != np.uint16 or frame.shape != (480, 800):
            raise ValueError("背景参考需要480x800 uint16彩色原始帧")
        if not frames or not np.array_equal(frame, frames[-1]):
            frames.append(frame)
        sleeper(0.08)
    rgb = np.stack([_rgb(f) for f in frames])
    reference = np.median(rgb, axis=0)
    sigma = 1.4826 * np.median(np.abs(rgb - reference), axis=0)
    return _pack(reference), sigma.astype(np.float32), {
        "requested_samples": n_frames, "distinct_adjacent_samples": len(frames),
        "temporal_noise_available": len(frames) >= 3,
        "frame_synchronization_verified": False}


def _robust_affine(frame, reference, anchor):
    """Select offset/affine using held-out spatial blocks, not object pixels."""
    x = reference[anchor]
    y = frame[anchor]
    width = frame.shape[1]
    blocks = np.broadcast_to((np.arange(width) // 32) % 2,
                             frame.shape[:2])[anchor]
    shift = np.median(y - x, axis=0)
    gains, offsets = np.ones(3, dtype=np.float32), shift.astype(np.float32)
    selected, validation = [], []
    for c in range(3):
        xx, yy = x[:, c], y[:, c]
        usable = (xx > 8) & (xx < 247) & (yy > 8) & (yy < 247)
        train, test = usable & (blocks == 0), usable & (blocks == 1)
        good = train.copy()
        accepted = False
        diagnostics = {"reason": "insufficient_background_contrast"}
        if good.sum() >= 128 and test.sum() >= 128 and np.std(xx[good]) >= 8:
            a, b = 1., float(shift[c])
            for _ in range(5):
                if good.sum() < 128:
                    break
                gx, gy = xx[good], yy[good]
                mx, my = gx.mean(), gy.mean()
                variance = np.mean((gx - mx) ** 2)
                if variance < 1:
                    break
                a = float(np.mean((gx - mx) * (gy - my)) / variance)
                b = float(my - a * mx)
                residual = yy - (a * xx + b)
                center = np.median(residual[good])
                scale = max(2., 1.4826 * np.median(np.abs(residual[good] - center)))
                good = train & (np.abs(residual - center) <= 3.5 * scale)
            if 0.25 <= a <= 4. and good.sum() >= 128:
                base = np.abs(yy[test] - shift[c] - xx[test])
                candidate = np.abs((yy[test] - b) / a - xx[test])
                old = np.percentile(base, [50, 95])
                new = np.percentile(candidate, [50, 95])
                # Both error measures must improve on unseen blocks. The model
                # is only a local exposure approximation, not a camera response.
                accepted = bool(np.all(new < old))
                diagnostics = {"reason": "heldout_improvement" if accepted else "no_heldout_improvement",
                               "offset_error_p50_p95": old.tolist(),
                               "affine_error_p50_p95": new.tolist()}
                if accepted:
                    gains[c], offsets[c] = a, b
        selected.append("affine" if accepted else "offset")
        validation.append(diagnostics)
    corrected = np.clip((frame - offsets) / gains, 0, 255)
    # Automatic camera response may be nonlinear. Fit an intensity-stratified
    # monotone response on known background only, then validate on other blocks.
    # Equal weighting per brightness interval prevents a large dark background
    # from hiding a poor fit on bright materials.
    for c in range(3):
        xx, yy = x[:, c], y[:, c]
        bins = np.floor(xx / 16).astype(int)
        xs, ys = [], []
        for k in range(1, 15):
            hit = (bins == k) & (blocks == 0) & (yy > 8) & (yy < 247)
            if hit.sum() >= 32:
                xs.append(float(np.median(xx[hit])))
                ys.append(float(np.median(yy[hit])))
        if len(xs) < 6 or np.any(np.diff(ys) <= 0):
            continue
        xs, ys = np.asarray(xs), np.asarray(ys)
        test = (blocks == 1) & (xx >= xs[0]) & (xx <= xs[-1]) & (yy > 8) & (yy < 247)
        proposed = np.interp(yy, ys, xs)
        current = np.clip((yy-offsets[c])/gains[c],0,255)
        old_bins, new_bins = [], []
        for k in range(1, 15):
            hit = test & (bins == k)
            if hit.sum() >= 32:
                old_bins.append(float(np.percentile(np.abs(current[hit]-xx[hit]),75)))
                new_bins.append(float(np.percentile(np.abs(proposed[hit]-xx[hit]),75)))
        if len(old_bins) >= 6 and np.mean(new_bins) < np.mean(old_bins) and \
                np.percentile(new_bins,90) <= np.percentile(old_bins,90):
            values = frame[...,c]
            channel = np.interp(values,ys,xs)
            # Linear extrapolation, recorded as unvalidated outside support.
            lo, hi = values < ys[0], values > ys[-1]
            channel[lo] = xs[0]+(values[lo]-ys[0])*(xs[1]-xs[0])/(ys[1]-ys[0])
            channel[hi] = xs[-1]+(values[hi]-ys[-1])*(xs[-1]-xs[-2])/(ys[-1]-ys[-2])
            corrected[...,c] = np.clip(channel,0,255)
            selected[c] = "monotone_response"
            validation[c] = {"reason":"brightness_balanced_heldout_improvement",
                             "reference_knots":xs.tolist(),"frame_knots":ys.tolist(),
                             "old_binned_error":old_bins,"new_binned_error":new_bins,
                             "extrapolated_fraction":float((lo|hi).mean())}
    return corrected, {
        "gain": gains.tolist(), "offset": offsets.tolist(),
        "photometric_model_per_channel": selected,
        "photometric_validation": validation}


def adaptive_difference(frame_rgb, reference_rgb, reference_sigma=None, anchor=None,
                         return_context=False, progress=None):
    """Return measured strong/possible foreground and background uncertainty.

    Per-channel noise has a RGB565 quantization floor. Estimated subpixel
    background displacement increases uncertainty at image edges, rather than
    matching arbitrary neighboring colors or changing the camera calibration.
    """
    frame, reference = (np.asarray(a, dtype=np.float32)
                        for a in (frame_rgb, reference_rgb))
    if frame.shape != reference.shape or frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("原图和背景必须为同尺寸RGB图像")
    if not np.isfinite(frame).all() or not np.isfinite(reference).all():
        raise ValueError("图像包含无效数值")
    h, w, _ = frame.shape
    rows = min(40, max(4, h // 6))
    if anchor is None:
        anchor = np.zeros((h, w), dtype=bool)
        anchor[:rows] = True
    anchor = np.asarray(anchor, dtype=bool)
    if anchor.shape != (h, w) or anchor.sum() < 256:
        raise ValueError("没有足够大的已知静止背景区域")
    if progress is not None:progress('光照响应估计')
    corrected, info = _robust_affine(frame, reference, anchor)
    if progress is not None:progress('背景位移估计')
    residual = corrected - reference
    gx, gy = np.zeros_like(reference), np.zeros_like(reference)
    gx[:, 1:-1] = (reference[:, 2:] - reference[:, :-2]) * .5
    gy[1:-1] = (reference[2:] - reference[:-2]) * .5
    # Robust linearized background flow, independently checked on held-out
    # spatial blocks. Large motion is flagged, never silently corrected.
    block = np.broadcast_to((np.arange(w) // 32) % 2, (h, w))[anchor]
    design = np.stack((gx[anchor], gy[anchor]), axis=-1).reshape(-1, 2)
    target = residual[anchor].reshape(-1)
    training = np.repeat(block == 0, 3)
    testing = ~training
    motion = np.zeros(2, dtype=np.float32)
    a, b = design[training], target[training]
    flow_reason = "insufficient_background_texture"
    if len(a) >= 128 and np.linalg.cond(a.T.dot(a) + np.eye(2) * 1e-6) < 1e5:
        weights = np.ones(len(a), dtype=np.float32)
        candidate = np.zeros(2)
        for _ in range(4):
            normal = a.T.dot(a * weights[:, None]) + np.eye(2) * 1e-6
            candidate = np.linalg.solve(normal, a.T.dot(b * weights))
            errors = b - a.dot(candidate)
            scale = max(2., 1.4826 * np.median(np.abs(errors - np.median(errors))))
            weights = np.minimum(1., 2.5 * scale / np.maximum(np.abs(errors), 1e-6))
        original_error = np.percentile(np.abs(target[testing]), 95)
        fit_error = np.percentile(np.abs(target[testing] - design[testing].dot(candidate)), 95)
        if np.max(np.abs(candidate)) <= 1. and fit_error < original_error:
            motion[:] = candidate
            flow_reason = "heldout_improvement"
        elif np.max(np.abs(candidate)) > 1.:
            flow_reason = "motion_exceeds_linear_model"
        else:
            flow_reason = "no_heldout_improvement"
    if progress is not None:progress('背景噪声与阈值估计')
    jitter_signal = gx * motion[0] + gy * motion[1]
    anchor_error = (residual - jitter_signal)[anchor]
    centered = anchor_error - np.median(anchor_error, axis=0)
    # Includes frame/reference noise; do not estimate it from object pixels.
    noise = np.maximum(1.4826 * np.median(np.abs(centered), axis=0),
                       np.array([8., 4., 8.]) / np.sqrt(12.))
    # Estimate conditional noise versus reference intensity using the known
    # background. Bright edges/regions need not have the same noise as dark ones.
    conditional = np.broadcast_to(noise, reference.shape).copy()
    noise_knots = []
    for c in range(3):
        xx = reference[...,c][anchor]
        rr = anchor_error[:,c]
        bins = (xx//16).astype(int)
        centers, scales = [], []
        for k in range(16):
            hit = bins == k
            if hit.sum() >= 32:
                values = rr[hit]
                # Include bias: a failed response fit must not look confident.
                sigma = np.percentile(np.abs(values),68.27)
                centers.append(float(np.median(xx[hit])))
                scales.append(float(max(sigma, np.array([8.,4.,8.])[c]/np.sqrt(12.))))
        if len(centers) >= 2:
            conditional[...,c] = np.interp(reference[...,c],centers,scales)
        noise_knots.append({"intensity":centers,"sigma":scales})
    variance = conditional ** 2 + jitter_signal ** 2
    if reference_sigma is not None:
        sigma = np.asarray(reference_sigma, dtype=np.float32)
        if sigma.shape != frame.shape or not np.isfinite(sigma).all() or (sigma < 0).any():
            raise ValueError("背景噪声参考尺寸或数值无效")
        variance = variance + sigma ** 2
    score = np.sqrt(np.sum(residual ** 2 / np.maximum(variance, 1e-6), axis=2))
    # Chi-square floors (3 channels) plus empirical held-out background tails.
    # These are false-alarm thresholds, not calibrated posterior probabilities.
    anchor_scores = score[anchor]
    low = max(np.sqrt(7.815), float(np.percentile(anchor_scores, 95)))
    high = max(np.sqrt(12.838), float(np.percentile(anchor_scores, 99.5)), low)
    strong, possible = score > high, score > low
    info.update(noise_rgb=noise.tolist(), conditional_noise=noise_knots,
                background_flow_xy=motion.tolist(),
                background_flow_reason=flow_reason, low_threshold=low,
                high_threshold=high, background_false_alarm_target=FALSE_ALARM_RATE,
                measured_anchor_strong_fraction=float(strong[anchor].mean()),
                anchor_pixels=int(anchor.sum()),
                frame_raw_color_modified=False)
    if return_context:
        return strong, possible, info, {'corrected':corrected,'variance':variance}
    return strong, possible, info


def _connected_regions(mask):
    """Yield exact 8-connected regions using row runs instead of pixel queues.

    Neighboring runs join when their intervals overlap or touch diagonally.
    NumPy expands coordinates only after unions; no extra board dependency.
    """
    a = np.asarray(mask, dtype=bool)
    h, w = a.shape
    parents, runs = [], []

    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    previous = []
    for y in range(h):
        edges = np.flatnonzero(np.diff(np.r_[False, a[y], False]))
        current, cursor = [], 0
        for start, stop in edges.reshape(-1, 2):
            start, stop = int(start), int(stop)
            idx = len(parents)
            parents.append(idx)
            runs.append((y,start,stop))
            # Half-open intervals may meet at an endpoint for diagonal contact.
            while cursor < len(previous) and previous[cursor][1] < start:
                cursor += 1
            j = cursor
            while j < len(previous) and previous[j][0] <= stop:
                root, other = find(idx), find(previous[j][2])
                if root != other:
                    parents[max(root,other)] = min(root,other)
                j += 1
            current.append((start,stop,idx))
        previous = current
    groups = {}
    for idx, run in enumerate(runs):
        groups.setdefault(find(idx), []).append(run)
    # Earliest raster pixel preserves the original component ordering for ties.
    for root in sorted(groups):
        region = groups[root]
        lengths = np.asarray([stop-start for _,start,stop in region],dtype=np.intp)
        rows = np.repeat(np.asarray([y for y,_,_ in region],dtype=np.intp),lengths)
        starts = np.repeat(np.asarray([start for _,start,_ in region],dtype=np.intp),lengths)
        offsets = np.repeat(np.cumsum(lengths)-lengths,lengths)
        cols = starts + np.arange(int(lengths.sum()),dtype=np.intp)-offsets
        yield rows, cols


def _seed_connected(possible, seeds):
    """Exact closure of 8-neighbor growth without repeated full-image filters."""
    active = np.zeros_like(possible,dtype=bool)
    for yy,xx in _connected_regions(np.asarray(possible,dtype=bool)|seeds):
        if seeds[yy,xx].any():
            active[yy,xx] = True
    return active


def _significant_components(mask, forbidden=None, minimum_size=None):
    """Reject small isolated noise clusters, retaining multiple object parts.

    Cluster cutoff follows a union-bound heuristic for independent noise. Actual
    image noise can be correlated; this is not a significance guarantee.
    """
    a = np.asarray(mask, dtype=bool)
    h, w = a.shape
    keep = np.zeros_like(a)
    if forbidden is None:
        forbidden = np.zeros_like(a)
    cutoff = max(2, int(np.ceil(np.log(max(1, h * w) / FALSE_ALARM_RATE) /
                                -np.log(8 * FALSE_ALARM_RATE))))
    if minimum_size is not None:
        cutoff = max(cutoff, int(minimum_size))
    sizes = []
    for yy, xx in _connected_regions(a):
        if len(yy) >= cutoff and not forbidden[yy, xx].any():
            keep[yy, xx] = True
            sizes.append(len(yy))
    return keep, cutoff, sorted(sizes, reverse=True)


class BoundaryEvidenceError(ValueError):
    def __init__(self, message, foreground, strong, info):
        super().__init__(message)
        self.foreground = foreground
        self.strong = strong
        self.diagnostics = info


def _background_reference_check(corrected, reference, variance, yy, xx):
    """Test the actual reference image, not component size or object class.

    Fit a local lighting offset on alternate 8x8 spatial blocks. Check unused
    blocks against noise and against mismatched reference locations. Flat or
    ambiguous reference patches cannot supply a distinctive match. This is
    provisional appearance evidence, not a proof of physical object absence.
    """
    h,w = reference.shape[:2]
    block_ids = (yy//8)*((w+7)//8)+xx//8
    train = ((yy//8+xx//8)%2)==0
    test = ~train
    result = {'accepted':False,'method':'heldout_reference_texture_with_local_offset'}
    if train.sum()<128 or test.sum()<128:
        result['reason']='insufficient_independent_reference_samples'
        return result
    observed = corrected[yy,xx].astype(np.float64)
    expected = reference[yy,xx].astype(np.float64)
    noise = np.maximum(variance[yy,xx].astype(np.float64),1e-6)
    offset = np.median((observed-expected)[train],axis=0)
    loss = np.sum((observed-expected-offset)**2/noise,axis=1)
    test_loss = float(np.mean(loss[test]))
    # Avoid accepting constant surfaces by allowing a freely fitted color shift.
    # Signal power must exceed measured per-pixel noise power.
    gray = expected.mean(axis=1)
    signal_power = float(np.var(gray[test]))
    noise_power = float(np.mean(noise[test].sum(axis=1)/9.))
    result.update(local_offset_rgb=offset.tolist(),heldout_normalized_loss=test_loss,
                  reference_signal_power=signal_power,expected_gray_noise_power=noise_power,
                  training_pixels=int(train.sum()),validation_pixels=int(test.sum()))
    if signal_power<=noise_power:
        result['reason']='reference_texture_not_distinctive_above_noise'
        return result
    if test_loss>3.:
        result['reason']='heldout_residual_exceeds_background_noise_model'
        return result
    width,height=int(xx.max()-xx.min()+1),int(yy.max()-yy.min()+1)
    offsets=((max(4,width),0),(-max(4,width),0),
             (max(2,width//2),0),(-max(2,width//2),0),
             (0,max(4,height)),(0,-max(4,height)),
             (0,max(2,height//2)),(0,-max(2,height//2)))
    comparisons=[]
    for dx,dy in offsets:
        valid=(xx+dx>=0)&(xx+dx<w)&(yy+dy>=0)&(yy+dy<h)
        tr,te=train&valid,test&valid
        if tr.sum()<128 or te.sum()<128:
            continue
        control=reference[np.clip(yy+dy,0,h-1),np.clip(xx+dx,0,w-1)].astype(np.float64)
        control_offset=np.median((observed-control)[tr],axis=0)
        control_loss=np.sum((observed-control-control_offset)**2/noise,axis=1)
        block_differences=[]
        for block in np.unique(block_ids[te]):
            hit=te&(block_ids==block)
            block_differences.append(float(np.mean(control_loss[hit]-loss[hit])))
        blocks=len(block_differences)
        if blocks<8:
            continue
        win_fraction=float(np.mean(np.asarray(block_differences)>0))
        # Conservative sign-test bound under independent spatial blocks. Real
        # image blocks may remain correlated; preserve review status downstream.
        bound=float(np.exp(-2*blocks*max(0.,win_fraction-.5)**2))
        comparisons.append({'offset_xy':[dx,dy],'blocks':blocks,
                            'matched_location_win_fraction':win_fraction,
                            'win_probability_upper_bound':bound,
                            'control_normalized_loss':float(np.mean(control_loss[te]))})
    result['mismatched_reference_controls']=comparisons
    if len(comparisons)<4 or any(c['win_probability_upper_bound']>.01 for c in comparisons):
        result['reason']='same_location_match_not_distinctive_from_controls'
        return result
    result.update(accepted=True,reason='unused_pixels_match_reference_texture_within_noise')
    return result


def _screen_boundary_components(active, strong, roi, bottom, noise_area_limit,
                                 background_linked=None, reference_matches=None):
    """Keep the main component intact; screen disconnected noise-sized groups.

    Filtering is provisional, not proof that the object is complete. Record every
    removed region. A primary or larger ambiguous boundary region still fails.
    """
    x0,y0,x1,_ = roi
    regions = list(_connected_regions(active))
    if not regions:
        return active.copy(), {'boundary_removed_components':[], 'boundary_removed_runs':[],
                               'boundary_removed_pixels':0, 'boundary_requires_review':False}
    primary = max(range(len(regions)), key=lambda i:(
        int(strong[regions[i]].sum()), len(regions[i][0])))
    primary_area = len(regions[primary][0])
    if background_linked is None:background_linked = np.zeros_like(active)
    if reference_matches is None:reference_matches = {}
    records, rejected, runs = [], [], []
    cleaned = active.copy()
    for i,(yy,xx) in enumerate(regions):
        sides = []
        if (yy==y0).any():sides.append('top')
        if (xx==x0).any():sides.append('left')
        if (xx==x1-1).any():sides.append('right')
        if not sides:
            continue
        record = {'pixels':int(len(yy)), 'strong_pixels':int(strong[yy,xx].sum()),
                  'bbox_xyxy':[int(xx.min()),int(yy.min()),int(xx.max()+1),int(yy.max()+1)],
                  'sides':sides, 'is_primary':i==primary}
        linked = bool(background_linked[yy,xx].any())
        record['continues_into_assumed_static_background'] = linked
        match = reference_matches.get(tuple(record['bbox_xyxy']))
        if match is not None:record['background_reference_check']=match
        reference_match = bool(match and match.get('accepted'))
        noise_sized = len(yy)<=noise_area_limit
        provisional_noise = len(yy)<primary_area and (noise_sized or linked or reference_match)
        if i==primary or not provisional_noise:
            rejected.append(record)
            continue
        cleaned[yy,xx] = False
        record['exclusion_reason'] = ('validated_reference_appearance' if reference_match else
                                     'background_continuation' if linked else 'background_noise_size')
        records.append(record)
        # Lossless run-length coordinates allow the saved diagnostic to show
        # exactly what was excluded without a large JSON pixel list.
        for row in np.unique(yy):
            cols = np.sort(xx[yy==row])
            edges = np.flatnonzero(np.diff(cols)>1)
            starts = np.r_[0,edges+1];stops = np.r_[edges,len(cols)-1]
            runs.extend([[int(row),int(cols[a]),int(cols[b])+1] for a,b in zip(starts,stops)])
    info = {'boundary_removed_components':records,'boundary_removed_runs':runs,
            'boundary_removed_pixels':int(sum(c['pixels'] for c in records)),
            'boundary_noise_area_limit':int(noise_area_limit),
            'boundary_requires_review':bool(records),
            'boundary_blocking_components':rejected,
            'roi_xyxy':list(map(int,roi)), 'ground_row':int(bottom)}
    if rejected:
        first = rejected[0]
        reason = '主体连通区域' if first['is_primary'] else '不能确认为背景噪声的独立区域'
        raise BoundaryEvidenceError(
            '%s触及ROI%s边界（区域%d像素，框%s）；需查看定位图，不能据此认定操作错误' %
            (reason,'/'.join(first['sides']),first['pixels'],first['bbox_xyxy']),
            active.copy(),strong.copy(),info)
    return cleaned,info


def segment_frame(frame_rgb, reference_rgb, ground_row, roi=None,
                  reference_sigma=None, progress=None):
    """Hysteresis admits uncertain pixels connected to observed foreground.

    Background pixels are never admitted, including enclosed holes. No opening,
    closing, convex hull or geometric expansion is applied.
    """
    h, w = np.asarray(frame_rgb).shape[:2]
    if roi is None:
        roi = (0, 0, w, h)
    x0, y0, x1, y1 = map(int, roi)
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
        raise ValueError("分割ROI无效")
    bottom = min(y1, int(np.floor(ground_row)))
    # Existing capture ROI guarantees the object is inside x0..x1. Only use
    # outside-ROI pixels above the table; neither the table/shadow below it nor
    # a tall object at the image top is silently assumed to be static background.
    anchor = np.zeros((h, w), dtype=bool)
    anchor[:bottom, :x0] = True
    anchor[:bottom, x1:] = True
    if anchor.sum() < 256:
        anchor[:min(40, max(4, h // 6))] = True
    strong, possible, info, context = adaptive_difference(
        frame_rgb, reference_rgb, reference_sigma, anchor, return_context=True,
        progress=progress)
    if progress is not None:progress('前景连通区域')
    full_possible = possible.copy()
    background_possible = possible & anchor
    allowed = np.zeros_like(strong)
    allowed[y0:bottom, x0:x1] = True
    # Evaluate connectivity before clipping. Do not discard crossing components
    # simply because they overlap the assumed background: that could hide a
    # genuinely cropped main object. The boundary screen handles them explicitly.
    candidate = strong.copy()
    candidate[bottom:] = False
    _, _, background_clusters = _significant_components(candidate & anchor)
    # Observed spatially correlated noise supersedes the independent-pixel
    # cluster approximation. Small disconnected parts below this size remain
    # unresolved; connected fine branches need no such area cutoff.
    cluster_limit = 1 + max(background_clusters or [0])
    seeds, cutoff, sizes = _significant_components(
        candidate, minimum_size=cluster_limit)
    seeds &= allowed
    strong &= allowed
    possible &= allowed
    # The converged flood is precisely the possible components containing seeds.
    # Confirmed background, including enclosed holes, remains absent.
    if progress is not None:progress('不确定区域连接')
    active = _seed_connected(possible, seeds)
    if not seeds.any():
        raise ValueError("照片未提供足够的前景证据：物体可能与背景同色、过曝或未入画")
    # Size reference uses the unmodified evidence in known static background.
    if progress is not None:progress('ROI边界检查')
    _, _, possible_background_clusters = _significant_components(background_possible)
    noise_area_limit = max(cutoff, max(possible_background_clusters or [0]))
    background_linked = np.zeros_like(active)
    if active[y0,x0:x1].any() or active[y0:bottom,x0].any() or active[y0:bottom,x1-1].any():
        # A disconnected boundary response may simply continue into an area
        # already used as static background. Preserve this direct evidence.
        # Exclusion remains provisional: the ROI assumption is not a proof of
        # object completeness, and the main component is never removed this way.
        for yy,xx in _connected_regions(full_possible):
            if anchor[yy,xx].any():background_linked[yy,xx] = True
    reference_matches = {}
    regions = list(_connected_regions(active))
    if regions:
        primary = max(range(len(regions)),key=lambda i:(int(seeds[regions[i]].sum()),len(regions[i][0])))
        for i,(yy,xx) in enumerate(regions):
            touches = (yy==y0).any() or (xx==x0).any() or (xx==x1-1).any()
            if i!=primary and touches and len(yy)>noise_area_limit and not background_linked[yy,xx].any():
                key=(int(xx.min()),int(yy.min()),int(xx.max()+1),int(yy.max()+1))
                if progress is not None:progress('独立区域背景纹理核验')
                reference_matches[key]=_background_reference_check(
                    context['corrected'],np.asarray(reference_rgb),context['variance'],yy,xx)
    active,boundary_info = _screen_boundary_components(
        active,seeds,roi,bottom,noise_area_limit,background_linked,reference_matches)
    strong = seeds & active
    unknown = active & ~strong
    info.update(strong_pixels=int(strong.sum()), unknown_pixels=int(unknown.sum()),
                foreground_pixels=int(active.sum()), significant_component_sizes=sizes,
                noise_cluster_cutoff=cutoff, background_cluster_sizes=background_clusters,
                hole_fill_pixels=0,
                uncertainty_fraction=float(unknown.sum() / max(1, active.sum())))
    info.update(boundary_info)
    if not strong.any():
        raise ValueError("照片未提供足够的前景证据：物体可能与背景同色、过曝或未入画")
    return active.astype(np.uint8), strong.astype(np.uint8), unknown.astype(np.uint8), info
