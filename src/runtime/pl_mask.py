"""Saved RGB565 photos: PL pixel evidence, PS fixed-window subject ownership.

Pixel subtraction, LUT correction, noise weighting and source-gradient evidence
execute in photo_mask_axis. PS estimates sparse parameters/references and makes
the final binary component or bounded region decision. There is no dense PS
colour-mask fallback, uniform hole fill, contour expansion or realtime dual stream.
Python 3.6 / NumPy; the verified hardware driver and register format are retained.
"""
import hashlib
import json
import time
from pathlib import Path

import numpy as np

EQUIVALENT_PERFORMANCE_VERSION = 89
_EXACT_ACCEL = None
IMPLEMENTATION_VERSION = 20
REGISTER_ACCESS_FIX_VERSION = 21
FAILURE_EVIDENCE_VERSION = 44
BOUNDARY_REASON_SCOPE_VERSION = 25
BACKGROUND_MODEL_VERSION = 26
ADAPTIVE_ROUTE_VERSION = 44
FRAME_ORDER_METADATA_VERSION = 29
LOW_CONTRAST_RECOVERY_VERSION = 76
MAGIC = 0x504d1901
CORE_BASE = 0x43c00000
CORE_RANGE = 0x1000
SHAPE = (480, 800)
PIXELS = 384000
WORDS = PIXELS // 16
ALGORITHM = 'pl_full_reference_noise_score_v19'
PIPELINE_ALGORITHM = 'PL_fixed_window_region_foreground_v76'
if '_UNSAFE_BUFFER_OWNERS' not in globals():
    _UNSAFE_BUFFER_OWNERS = []  # Retain even when Notebook reloads this module.


def _frame(value):
    a = np.asarray(value)
    if a.dtype != np.uint16 or a.shape != SHAPE:
        raise ValueError('需要480x800 uint16 RGB565原始帧')
    return a


def _rgb(value):
    r, g, b = (value >> 11) & 31, (value >> 5) & 63, value & 31
    return np.stack(((r << 3) | (r >> 2), (g << 2) | (g >> 4),
                     (b << 3) | (b >> 2)), axis=-1).astype(np.int32)


def prepare_parameters(frame565, reference565, ground_row, reference_sigma=None,
                       background_samples=None, photometric_parameters=None,
                       exterior_validity=None, measurement_roi=(160,0,640,480)):
    """Only 1/32 of pixels reach the PS response/noise estimator, never masks."""
    from adaptive_scan import _robust_affine
    frame, reference = _frame(frame565), _frame(reference565)
    bottom = min(480, int(np.floor(ground_row)))
    if not 8 < bottom <= 480:
        raise ValueError('台面像素行无效')
    current = _rgb(frame[::8, ::4]).astype(np.float32)
    background = _rgb(reference[::8, ::4]).astype(np.float32)
    yy, xx = np.mgrid[:480:8, :800:4]
    left,_,right,_=measurement_roi
    anchor = (yy < bottom) & ((xx < left) | (xx >= right))
    if exterior_validity is not None:
        validity=np.asarray(exterior_validity,dtype=bool)
        if validity.shape!=anchor.shape:raise ValueError('稀疏背景参数采样区格式错误')
        anchor &= validity
    if background_samples is not None:
        samples=np.asarray(background_samples,dtype=bool)
        if samples.shape!=(120,200):
            raise ValueError('已核验背景样本的稀疏尺寸错误')
        anchor |= samples[::2] & (yy<bottom)
    if int(anchor.sum()) < 256:
        raise ValueError('ROI外静止背景样本不足，不能可靠估计阈值')
    if photometric_parameters is None:
        _, info = _robust_affine(current, background, anchor)
        lut = np.empty((3, 256), dtype=np.uint8)
        domain = np.arange(256, dtype=np.float64)
        for c, kind in enumerate(info['photometric_model_per_channel']):
            values = (domain-info['offset'][c])/info['gain'][c]
            if kind == 'monotone_response':
                fit = info['photometric_validation'][c]
                x, y = np.asarray(fit['frame_knots']), np.asarray(fit['reference_knots'])
                values = np.interp(domain, x, y)
                lo, hi = domain < x[0], domain > x[-1]
                values[lo] = y[0]+(domain[lo]-x[0])*(y[1]-y[0])/(x[1]-x[0])
                values[hi] = y[-1]+(domain[hi]-x[-1])*(y[-1]-y[-2])/(x[-1]-x[-2])
            lut[c] = np.clip(np.rint(values), 0, 255).astype(np.uint8)
    else:
        # The local model predicts values in the BASELINE LUT's color space.
        # Refitting a second global response here changes that space and can
        # reintroduce the illumination difference the model just explained.
        lut=np.asarray(photometric_parameters['lut']).copy()
        if lut.dtype!=np.uint8 or lut.shape!=(3,256):
            raise ValueError('背景预测模型的颜色响应表格式错误')
        info=dict(photometric_parameters['info'])
        info['photometric_response_frozen_for_modeled_reference']=True
    cur, ref = current[anchor].astype(int), background[anchor].astype(int)
    corrected = np.stack([lut[c, cur[:, c]] for c in range(3)], axis=-1).astype(int)
    residual = corrected-ref
    temporal = None
    if reference_sigma is not None:
        sigma = np.asarray(reference_sigma)
        if sigma.shape != (480, 800, 3):
            raise ValueError('背景噪声参考尺寸无效')
        temporal = sigma[::8, ::4][anchor]
        if not np.isfinite(temporal).all() or (temporal < 0).any():
            raise ValueError('背景噪声参考数值无效')
    variance = np.empty((3, 16), dtype=np.float64)
    for c in range(3):
        floor = [8., 4., 8.][c]/np.sqrt(12.)
        global_noise = max(floor, float(np.percentile(np.abs(residual[:, c]), 68.27)))
        for k in range(16):
            hit = (ref[:, c] >> 4) == k
            local = (max(floor, float(np.percentile(np.abs(residual[hit, c]), 68.27)))
                     if hit.sum() >= 16 else global_noise)
            noise2 = float(np.percentile(temporal[hit, c]**2, 90)) if temporal is not None and hit.any() else 0.
            variance[c, k] = local**2+noise2
    if background_samples is not None:
        # A pooled median hides local model error: a correctly matched curtain
        # patch may have a different illumination/noise level from the sidebars.
        # Use measured local RMS (including bias) from VERIFIED sparse background,
        # and include the newly rendered reference's quantization uncertainty.
        tile=(yy[anchor]//64)*13+(xx[anchor]//64)
        spatial_bins=78
        for c in range(3):
            group=(ref[:,c]>>4)*spatial_bins+tile
            counts=np.bincount(group,minlength=16*spatial_bins).reshape(16,spatial_bins)
            energy=np.bincount(group,weights=residual[:,c].astype(float)**2,
                               minlength=16*spatial_bins).reshape(16,spatial_bins)
            local_mse=energy/np.maximum(counts,1)
            local_mse[counts<16]=0
            # Two independently quantized colors can differ by one native code
            # while their underlying intervals still overlap at a boundary.
            # A rendered model has another rounding operation. Use half a native
            # code as a conservative score uncertainty, not uniform-noise sigma.
            variance[c]=np.maximum(np.maximum(variance[c],local_mse.max(axis=1)),
                                   [64.,16.,64.][c]/4.)
    inv = np.clip(np.rint(65536./variance), 1, 65535).astype(np.uint16)
    scores = sum(residual[:, c].astype(np.int64)**2 *
                 inv[c, ref[:, c] >> 4].astype(np.int64) for c in range(3))
    low = int(np.ceil(max(7.815*65536, float(np.percentile(scores, 95)))))
    high = int(np.ceil(max(12.838*65536, float(np.percentile(scores, 99.5)), low)))
    if not 0 <= low <= high <= 0xffffffff:
        raise ValueError('背景变化超过PL评分范围，请核对原图与背景')
    info.update(backend='PL', algorithm=ALGORITHM, parameter_sample_pixels=int(anchor.sum()),
                parameter_sample_stride_yx=[8, 4], variance_by_reference_bin=variance.tolist(),
                interior_reference_verified_samples_used=background_samples is not None,
                spatial_background_uncertainty_used=background_samples is not None,
                low_score_q16=low, high_score_q16=high,
                high_threshold=float(np.sqrt(high/65536.)),
                low_threshold=float(np.sqrt(low/65536.)),
                measured_anchor_strong_fraction=float(np.mean(scores > high)),
                frame_raw_color_modified=False, geometric_expansion=False,
                hole_filling=False, background_motion_corrected=False)
    return {'lut': lut, 'inverse_variance': inv, 'low': low, 'high': high, 'info': info}


def reference_decisions(frame565, reference565, parameters):
    """Independent integer oracle for offline HDL verification, NOT a fallback."""
    cur, ref = _rgb(_frame(frame565)), _rgb(_frame(reference565))
    score = np.zeros(SHAPE, dtype=np.int64)
    for c in range(3):
        d = parameters['lut'][c, cur[..., c]].astype(np.int64)-ref[..., c]
        score += d*d*parameters['inverse_variance'][c, ref[..., c] >> 4]
    return (score > parameters['high']), (score > parameters['low'])


def unpack_decisions(words):
    words = np.asarray(words, dtype=np.uint32)
    if words.shape != (WORDS,):
        raise ValueError('PL返回字数错误')
    codes = ((words[:, None] >> (2*np.arange(16, dtype=np.uint32))) & 3).astype(np.uint8).reshape(SHAPE)
    strong, possible = (codes & 1) != 0, (codes & 2) != 0
    if (strong & ~possible).any():
        raise RuntimeError('PL返回的强前景不是候选前景的子集')
    return strong, possible


class PLMaskSegmenter:
    """Own one bounded DMA transaction; stop DMA before releasing CMA buffers."""
    REFERENCE_GROUP_DRIVER_VERSION = 25
    def __init__(self, overlay, timeout_s=8.):
        # Register the PYNQ DMA driver before lazily resolving Overlay IPs.
        from pynq import MMIO
        from pynq.lib import DMA  # noqa: F401
        self.overlay, self.timeout_s = overlay, float(timeout_s)
        if not 0 < self.timeout_s <= 60:
            raise ValueError('DMA超时必须在0～60秒之间')
        self._active = False
        self._safe_to_free = True
        self._closed = False
        self.self_test_passed = False
        self._allocator = None
        self.input_buffer = self.output_buffer = None
        self._check_overlay()
        try:
            self.dma = overlay.photo_dma
        except AttributeError as exc:
            raise RuntimeError('当前bit没有PL掩膜加速器；请加载本包匹配的bit和hwh') from exc
        # Older HWH parsers expose module_ref photo_mask_0 as a hierarchy,
        # which has no read/write methods. Use the v20 hardware's verified
        # register window directly, just as the existing Recon driver does.
        # The DMA must exist before probing this bit-specific register window.
        description = getattr(overlay, 'ip_dict', {}).get('photo_mask_0', {})
        if 'phys_addr' in description and int(description['phys_addr']) != CORE_BASE:
            raise RuntimeError('硬件描述中的PL掩膜地址不匹配；请核对v20 bit/hwh')
        self.core = MMIO(CORE_BASE, CORE_RANGE)
        if self.core.read(0) != MAGIC:
            raise RuntimeError('PL掩膜版本不匹配；不能回退到PS分割')
        try:
            try:
                from pynq import allocate
                allocation = lambda n: allocate(shape=(n,), dtype=np.uint32)
            except ImportError:
                from pynq import Xlnk
                self._allocator = Xlnk()
                allocation = lambda n: self._allocator.cma_array(shape=(n,), dtype=np.uint32, cacheable=0)
            self.input_buffer, self.output_buffer = allocation(PIXELS), allocation(WORDS)
            for buffer in (self.input_buffer, self.output_buffer):
                address = int(buffer.physical_address)
                if address % 8 or address < 0 or address+buffer.nbytes > 0x20000000:
                    raise RuntimeError('DMA连续内存地址或对齐不符合512MB板配置')
                if address < 0x018bb800 and address+buffer.nbytes > 0x01800000:
                    raise RuntimeError('CMA分配与相机固定帧缓存重叠；停止使用此系统内存配置')
        except BaseException:
            self.close()
            raise

    def _check_overlay(self):
        if not self.overlay.is_loaded():
            raise RuntimeError('FPGA已被其他Notebook重新加载；请从本Notebook初始化格重新运行')

    def _reset_transfer(self):
        """Return only after a verified AXI DMA reset, or retain owned buffers."""
        self._safe_to_free = False
        self._table_group_key = None
        self._check_overlay()
        self.core.write(4, 2)
        self.dma.mmio.write(0x00, 4)
        deadline = time.monotonic()+1.
        while self.dma.mmio.read(0x00) & 4:
            if time.monotonic() >= deadline:
                raise RuntimeError('DMA复位未完成：保留连续内存，请重新初始化硬件')
            time.sleep(.001)
        # After reset no DMA master can write the buffers. Now restart channels.
        self._safe_to_free = True
        self._active = False
        for channel in (self.dma.sendchannel, self.dma.recvchannel):
            channel._active_buffer = None
            channel.start()

    def classify(self, frame565, reference565, parameters):
        if self._closed:
            raise RuntimeError('PL掩膜设备已关闭，请重新初始化')
        self._check_overlay()
        frame, reference = _frame(frame565), _frame(reference565)
        lut = np.asarray(parameters['lut'])
        noise = np.asarray(parameters['inverse_variance'])
        low, high = int(parameters['low']), int(parameters['high'])
        if lut.shape != (3, 256) or lut.dtype != np.uint8 or noise.shape != (3, 16) or \
                noise.dtype != np.uint16 or (noise == 0).any() or not 0 <= low <= high <= 0xffffffff:
            raise ValueError('PL参数格式或范围无效')
        if self._active:
            raise RuntimeError('上一笔DMA尚未结束')
        started = time.monotonic()
        if self.core.read(8) & 1:
            self._reset_transfer()
        sequence = self.core.read(0x30)
        np.copyto(self.input_buffer, reference.reshape(-1), casting='unsafe')
        np.left_shift(self.input_buffer, 16, out=self.input_buffer)
        np.bitwise_or(self.input_buffer, frame.reshape(-1), out=self.input_buffer)
        table_key = lut.tobytes()+noise.tobytes()
        reuse = (parameters.get('_within_reference_group', False) and
                 getattr(self, '_table_group_key', None) == table_key and
                 getattr(self, '_table_group_sequence', None) == sequence and
                 self.core.read(0x40) == ((48 << 10) | 768))
        if not reuse:
            self.core.write(0x20, 0)
            for value in lut.reshape(-1):
                self.core.write(0x24, int(value))
            self.core.write(0x28, 0)
            for value in noise.reshape(-1):
                self.core.write(0x2c, int(value))
        if self.core.read(0x40) != ((48 << 10) | 768):
            raise RuntimeError('PL查找表未完整写入')
        self.core.write(0x0c, PIXELS)
        self.core.write(0x10, high)
        self.core.write(0x14, low)
        transfer_started = time.monotonic()
        self._active, self._safe_to_free = True, False
        try:
            # Arm receiver before the producer, then start the input DMA.
            self.dma.recvchannel.transfer(self.output_buffer)
            self.core.write(4, 1)
            self.dma.sendchannel.transfer(self.input_buffer)
            deadline = time.monotonic()+self.timeout_s
            while True:
                tx, rx = self.dma.mmio.read(0x04), self.dma.mmio.read(0x34)
                status = self.core.read(8)
                if (tx | rx) & 0x770 or status & 4:
                    raise RuntimeError('PL/DMA错误：TX=0x%x RX=0x%x CORE=0x%x' %
                                       (tx, rx, self.core.read(0x3c)))
                if tx & 2 and rx & 2 and status & 2 and not status & 1:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('PL掩膜DMA超时（%.1f秒）；已中止，不会切回PS分割' % self.timeout_s)
                time.sleep(.001)
            if self.core.read(0x1c) != PIXELS or self.core.read(0x18) != WORDS or \
                    self.core.read(0x30) != ((sequence+1) & 0xffffffff):
                raise RuntimeError('PL掩膜帧序号或像素/字数不符')
            # Wait is bounded in practice here: both channels were proven idle.
            self.dma.sendchannel.wait()
            self.dma.recvchannel.wait()
            dma_wait_s = time.monotonic()-transfer_started
            self._active, self._safe_to_free = False, True
            packed = np.asarray(self.output_buffer).copy()
            strong, possible = unpack_decisions(packed)
            if int(strong.sum()) != self.core.read(0x34) or int(possible.sum()) != self.core.read(0x38):
                raise RuntimeError('PL计数器和DMA返回位数据不一致')
            self._table_group_key = table_key
            self._table_group_sequence = self.core.read(0x30)
            return strong, possible, {
                'pl_frame_sequence': int(self.core.read(0x30)),
                'pl_strong_pixels': int(strong.sum()), 'pl_possible_pixels': int(possible.sum()),
                'pl_dma_transfer_and_wait_s': dma_wait_s,
                'packing_configuration_dma_decode_s': time.monotonic()-started,
                'pl_input_pixels': PIXELS, 'pl_output_words': WORDS}, packed
        except BaseException as exc:
            self._table_group_key = None
            try:
                self._reset_transfer()
            except BaseException as recovery:
                # Keep buffers alive: freeing while an unproven DMA may write is unsafe.
                self._safe_to_free = False
                _UNSAFE_BUFFER_OWNERS.append(self)
                raise RuntimeError('%s；DMA恢复失败：%s；请重新初始化硬件' % (exc, recovery)) from exc
            raise

    def self_test(self):
        """Executed automatically on BOARD, never replaced by a host-only PASS."""
        background = np.full(SHAPE, 0x8410, dtype=np.uint16)
        frame = background.copy()
        positions = np.asarray([0, 15, 16, 799, 800, PIXELS-1])
        frame.reshape(-1)[positions] = 0xffff
        params = {'lut': np.tile(np.arange(256, dtype=np.uint8), (3, 1)),
                  'inverse_variance': np.full((3, 16), 1024, dtype=np.uint16),
                  'low': 5*65536, 'high': 10*65536}
        strong, possible, _, _ = self.classify(frame, background, params)
        expected = np.zeros(PIXELS, dtype=bool)
        expected[positions] = True
        if not np.array_equal(strong.reshape(-1), expected) or not np.array_equal(possible, strong):
            raise RuntimeError('板上PL掩膜自检失败，停止采集')
        self.self_test_passed = True

    def close(self):
        if self._closed:
            return
        if self._active:
            self._reset_transfer()
        if not self._safe_to_free:
            raise RuntimeError('尚不能确认DMA停止，禁止释放其连续内存')
        for buffer in (self.input_buffer, self.output_buffer):
            if buffer is not None:
                if hasattr(buffer, 'freebuffer'):
                    buffer.freebuffer()
                else:
                    buffer.close()
        self.input_buffer = self.output_buffer = None
        self._closed = True


def _regions_reference(mask, strong):
    """4-connected run-length union, no per-pixel Python arrays for noise blobs."""
    parent, area, strength, runs = [], [], [], []
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    previous = []
    for y in range(mask.shape[0]):
        edge = np.flatnonzero(np.diff(np.r_[False, mask[y], False].astype(np.int8)))
        current, cursor = [], 0
        for start, stop in zip(edge[::2], edge[1::2]):
            node = len(parent)
            parent.append(node); area.append(int(stop-start))
            strength.append(int(strong[y, start:stop].sum()))
            while cursor < len(previous) and previous[cursor][1] <= start:
                cursor += 1
            j = cursor
            while j < len(previous) and previous[j][0] < stop:
                r = root(previous[j][2]); n = root(node)
                if r != n:
                    parent[r] = n; area[n] += area[r]; strength[n] += strength[r]
                j += 1
            current.append((int(start), int(stop), node))
            runs.append((y, int(start), int(stop), node))
        previous = current
    labels = np.asarray([root(i) for i in range(len(parent))], dtype=int)
    return runs, labels, np.asarray(area, dtype=int), np.asarray(strength, dtype=int)


def _regions(mask, strong):
    # Same integer run unions, labels and intermediate counts. No RGB decisions.
    global _EXACT_ACCEL
    try:
        if _EXACT_ACCEL is None:
            import exact_scan_accel, importlib
            if Path(exact_scan_accel.__file__).resolve().parent != Path(__file__).resolve().parent:
                return _regions_reference(mask, strong)
            _EXACT_ACCEL = importlib.reload(exact_scan_accel)
    except ImportError:
        return _regions_reference(mask, strong)
    result = _EXACT_ACCEL.regions(mask, strong)
    return _regions_reference(mask, strong) if result is None else result


def clean_binary(strong, possible, ground_row, roi=(160, 0, 640, 480)):
    """Binary-only seeded connectivity; never guess holes or move outlines."""
    from adaptive_scan import BoundaryEvidenceError
    strong, possible = np.asarray(strong, dtype=bool), np.asarray(possible, dtype=bool)
    if strong.shape != SHAPE or possible.shape != SHAPE or (strong & ~possible).any():
        raise ValueError('PL二值证据格式错误')
    x0, y0, x1, y1 = map(int, roi)
    bottom = min(y1, int(np.floor(ground_row)))
    if not (0 <= x0 < x1 <= 800 and 0 <= y0 < bottom <= 480):
        raise ValueError('ROI或台面行无效')
    candidate = possible[:bottom]
    runs, labels, areas, seeds = _regions(candidate, strong[:bottom])
    active = np.zeros(SHAPE, dtype=bool)
    candidates = {}
    # Determine a noise seed cutoff using wholly outside-ROI strong components.
    # Never use the size of the object to invent an absolute morphology radius.
    anchor = np.zeros((bottom, 800), dtype=bool)
    anchor[:, :x0] = True; anchor[:, x1:] = True
    _, noise_labels, noise_areas, _ = _regions(strong[:bottom] & anchor, strong[:bottom] & anchor)
    cutoff = max(6, 1+max(noise_areas[np.unique(noise_labels)].tolist() or [0]))
    for y, a, b, node in runs:
        r = int(labels[node])
        if seeds[r] < cutoff or y < y0:
            continue
        if r not in candidates:
            candidates[r] = {'pixels': 0, 'strong_pixels': 0,
                             'bbox_xyxy': [x1, bottom, x0, y0],
                             'background_linked': False, 'runs': []}
        item = candidates[r]
        item['background_linked'] |= a < x0 or b > x1
        a, b = max(a, x0), min(b, x1)
        if a >= b:
            continue
        item['pixels'] += b-a
        item['strong_pixels'] += int(strong[y, a:b].sum())
        box = item['bbox_xyxy']
        item['bbox_xyxy'] = [min(a, box[0]), min(y, box[1]), max(b, box[2]), max(y+1, box[3])]
        item['runs'].append((y, a, b))
    candidates = {k: v for k, v in candidates.items() if v['strong_pixels'] >= cutoff}
    if not candidates:
        raise ValueError('PL没有提供足够的前景证据；请查看原图与空台参考')
    primary = max(candidates, key=lambda r: (candidates[r]['strong_pixels'], candidates[r]['pixels']))
    removed, blocked = [], []
    for r, item in candidates.items():
        box = item['bbox_xyxy']
        sides = [name for name, hit in [('top', box[1] == y0), ('left', box[0] == x0),
                                       ('right', box[2] == x1)] if hit]
        info = {k: v for k, v in item.items() if k != 'runs'}
        info.update(sides=sides, is_primary=r == primary)
        for y, a, b in item['runs']:
            active[y, a:b] = True
        if sides:
            blocked.append(info)
    info = {'binary_cleanup': 'all_seeded_components_no_automatic_secondary_removal_v39',
            'strong_noise_cluster_cutoff': cutoff, 'roi_xyxy': list(roi),
            'ground_row': bottom, 'boundary_removed_components': removed,
            'boundary_requires_review': bool(blocked), 'boundary_blocking_components': blocked,
            'boundary_removed_pixels': sum(v['pixels'] for v in removed)}
    if blocked:
        # A weak connected path is not evidence that the strong object is cropped.
        # Keep the refusal: low-contrast real objects can also produce such paths.
        sruns, slabels, sareas, _ = _regions(strong[:bottom], strong[:bottom])
        boundary_active = np.zeros(SHAPE, dtype=bool)
        boundary_active[y0, x0:x1] = active[y0, x0:x1]
        boundary_active[y0:bottom, x0] = active[y0:bottom, x0]
        boundary_active[y0:bottom, x1-1] = active[y0:bottom, x1-1]
        primary_boundary=np.zeros(SHAPE,dtype=bool)
        for y,a,b in candidates[primary]['runs']:
            if y==y0:primary_boundary[y,a:b]=True
            if a==x0:primary_boundary[y,x0]=True
            if b==x1:primary_boundary[y,x1-1]=True
        strong_boundary = []
        primary_strong_boundary=[]
        for y, a, b, node in sruns:
            # Only the retained boundary pixels actually responsible for this
            # refusal count. A screened secondary region must not relabel an
            # unrelated weak connection as a strong foreground crop.
            if sareas[slabels[node]] >= cutoff and boundary_active[y, a:b].any():
                strong_boundary.append(int(slabels[node]))
                if primary_boundary[y,a:b].any():
                    primary_strong_boundary.append(int(slabels[node]))
        info['significant_strong_boundary_components'] = sorted(set(strong_boundary))
        info['boundary_evidence_kind'] = ('strong_foreground' if strong_boundary
                                          else 'weak_candidate_connection')
        info['boundary_reason_scope_version'] = BOUNDARY_REASON_SCOPE_VERSION
        info['primary_boundary_evidence_kind'] = (
            'strong_foreground' if primary_strong_boundary else
            'weak_candidate_connection' if primary_boundary.any() else 'no_primary_boundary')
        message = ('PL强前景触及ROI边界，完整轮廓无法确认' if strong_boundary else
                   'PL弱候选区域连到ROI边界，强前景未贴边；可能是背景误连或物体弱对比部分，不能直接裁掉')
        raise BoundaryEvidenceError(message+'；查看定位图及原始诊断',
                                    active.copy(), strong.copy(), info)
    return active.astype(np.uint8), (strong & active).astype(np.uint8), \
        (active & ~strong).astype(np.uint8), info


def _save_failure(directory, index, frame, reference, ground, error,
                  parameters=None, possible=None, strong=None, status=None,
                  packed=None, reference_sigma=None, modeled_reference=None,
                  reference_offsets=None, hypothesis_packed=None, model_grid=None,
                  baseline_evidence=None):
    if directory is None:
        return ''
    from PIL import Image, ImageDraw
    from scan_pipeline import rgb565_to_rgb, _atomic_npz
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    stem = directory/('segmentation_failed_%02d' % index)
    info = dict(getattr(error, 'diagnostics', {}), backend='PL', error=str(error),
                failure_evidence_version=FAILURE_EVIDENCE_VERSION, algorithm=PIPELINE_ALGORITHM,
                implementation_version=IMPLEMENTATION_VERSION,
                frame_sha256=hashlib.sha256(_frame(frame).astype('<u2',copy=False).tobytes()).hexdigest(),
                reference_sha256=hashlib.sha256(_frame(reference).astype('<u2',copy=False).tobytes()).hexdigest(),
                ground_row=float(ground))
    if parameters is not None:
        if 'info' in parameters:
            info['parameters'] = parameters['info']
        else:
            # Edge-feature configurations carry actual core registers but no
            # photometric-fit metadata. Preserve the real configuration; do
            # not let a missing optional label erase the failure evidence.
            info['parameters'] = dict(configuration_kind='PL_edge_feature_registers',
                                      photometric_fit_metadata_supplied=False,
                                      low_score_q16=int(parameters['low']),
                                      high_score_q16=int(parameters['high']))
    if status is not None:
        info['pl_status'] = status
    image = Image.fromarray(rgb565_to_rgb(frame)); draw = ImageDraw.Draw(image)
    draw.rectangle((160, 0, 639, 479), outline=(255, 255, 0))
    draw.line((0, int(ground), 799, int(ground)), fill=(0, 255, 255), width=2)
    for item in info.get('boundary_blocking_components', []):
        a, b, c, d = item['bbox_xyxy']
        draw.rectangle((a, b, c-1, d-1), outline=(255, 0, 0))
    image.save(str(stem)+'.png')
    arrays = dict(frame565=frame, empty565=reference, ground_row=ground)
    if hasattr(error, 'foreground'):
        arrays.update(foreground=error.foreground, strong=error.strong)
    if strong is not None:
        arrays['strong'] = strong
    if possible is not None:
        arrays['possible'] = possible
    if packed is not None:
        arrays['pl_packed_decisions'] = packed
    if reference_sigma is not None:
        arrays['reference_sigma'] = reference_sigma
    if parameters is not None:
        arrays.update(lut=parameters['lut'], inverse_variance=parameters['inverse_variance'],
                      low_q16=parameters['low'], high_q16=parameters['high'])
    if modeled_reference is not None:
        arrays['pl_reference565'] = modeled_reference
        info['pl_reference_sha256'] = hashlib.sha256(
            _frame(modeled_reference).astype('<u2',copy=False).tobytes()).hexdigest()
    if reference_offsets is not None:
        arrays['pl_reference_offsets_yx'] = np.asarray(reference_offsets,dtype=np.int16)
    if hypothesis_packed:
        arrays['pl_hypotheses_packed'] = np.stack(hypothesis_packed)
    if model_grid is not None:
        arrays['background_model_grid'] = model_grid
    if baseline_evidence is not None:
        for key, value in baseline_evidence.items():
            if key == 'diagnostics':
                info['baseline_diagnostics'] = value
            else:
                arrays['baseline_'+key] = value
    arrays['diagnostics_json'] = json.dumps(info,ensure_ascii=False)
    Path(str(stem)+'.json').write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    _atomic_npz(str(stem)+'.npz', **arrays)
    return '；定位图：%s.png；原始诊断：%s.npz' % (stem,stem)


def _pack_decisions(strong, possible):
    codes = strong.astype(np.uint32)+2*possible.astype(np.uint32)
    return np.bitwise_or.reduce(codes.reshape(-1,16) <<
                               (2*np.arange(16,dtype=np.uint32)),axis=1)


def classify_reference_group(device, frame, reference, params, offsets, progress=None):
    """Every RGB decision runs in PL. PS only ANDs returned binary evidence."""
    padded = np.pad(reference,1,mode='edge')
    raw, statuses = [], []
    strong = possible = None
    for index,(dy,dx) in enumerate(offsets):
        if progress: progress.update('PL背景参考核验','%d/%d' % (index+1,len(offsets)))
        shifted = padded[1+dy:481+dy,1+dx:801+dx]
        configured = dict(params,_within_reference_group=index>0)
        s,p,status,packed = device.classify(frame,shifted,configured)
        raw.append(packed);statuses.append(status)
        if strong is None:
            strong,possible = s,p
        else:
            strong &= s;possible &= p
    info = dict(pl_reference_passes=len(offsets),pl_reference_pass_status=statuses,
                pl_dma_transfer_and_wait_s=sum(v['pl_dma_transfer_and_wait_s'] for v in statuses),
                packing_configuration_dma_decode_s=sum(v['packing_configuration_dma_decode_s'] for v in statuses),
                pl_input_pixels_per_pass=PIXELS,pl_binary_fusion='AND_across_reference_samples',
                pl_packed_decisions_kind='binary_fused_PL_passes')
    return strong,possible,info,_pack_decisions(strong,possible),raw


def prepare_background(frame, reference, ground, reference_sigma=None,
                       baseline_parameters=None):
    """One shared preparation path for board execution and operating-range audit."""
    import background_model
    if background_model.IMPLEMENTATION_VERSION < BACKGROUND_MODEL_VERSION:
        raise RuntimeError('请同时覆盖background_model.py和pl_mask.py')
    original_baseline=(prepare_parameters(frame,reference,ground,reference_sigma)
                       if baseline_parameters is None else baseline_parameters)
    baseline=original_baseline
    effective_sigma=reference_sigma
    aligned,registration=background_model.align_reference(frame,reference,baseline,ground)
    if registration['accepted']:
        if reference_sigma is not None:
            dy,dx=registration['offset_yx']
            padded_sigma=np.pad(reference_sigma,((1,1),(1,1),(0,0)),mode='edge')
            effective_sigma=padded_sigma[1+dy:481+dy,1+dx:801+dx]
        baseline=prepare_parameters(frame,aligned,ground,effective_sigma)
    modeled,grid,model_info=background_model.fit_reference(frame,aligned,baseline,ground)
    if registration['accepted'] and not model_info['applied']:
        # A motion estimate that helps edge anchors can still break the actual
        # reference texture. Validate the composed model, not each stage alone.
        alternative,alternative_grid,alternative_info=background_model.fit_reference(
            frame,reference,original_baseline,ground)
        if alternative_info['applied']:
            registration=dict(accepted=False,offset_yx=[0,0],search_radius_px=1,
                              rejection_reason='shift_reduces_validated_reference_support',
                              rejected_candidate=registration)
            baseline=original_baseline
            effective_sigma=reference_sigma
            modeled,grid,model_info=alternative,alternative_grid,alternative_info
    model_info['background_registration']=registration
    params=(prepare_parameters(frame,modeled,ground,effective_sigma,
                               background_model.verified_samples(model_info),baseline)
            if model_info['applied'] else baseline)
    offsets,offset_info=background_model.choose_reference_offsets(
        frame,modeled,params,ground,selection_parameters=baseline)
    params['low']=offset_info['recommended_low_q16'];params['high']=offset_info['recommended_high_q16']
    params['info'].update(low_score_q16=params['low'],high_score_q16=params['high'],
                          low_threshold=float(np.sqrt(params['low']/65536.)),
                          high_threshold=float(np.sqrt(params['high']/65536.)),
                          background_appearance_model=model_info,
                          background_spatial_reference=offset_info,
                          background_reference_offsets_yx=offsets,
                          background_model_version=BACKGROUND_MODEL_VERSION,algorithm=PIPELINE_ALGORITHM,
                          pixel_core_algorithm=ALGORITHM)
    return params,modeled,grid,offsets


def appearance_review_required(frame,reference,parameters,ground_row):
    """Conservatively require a model if any held-out background witness disagrees.

    Detecting a mismatch needs one witness; fitting the existing smooth field
    still requires its own six validated patches. No dense output mask is made.
    """
    import background_model as bm
    started=time.monotonic()
    ref=bm.rgb565(reference[::4,::4]);current=bm.rgb565(frame[::4,::4]).astype(int)
    observed=np.stack([parameters['lut'][c,current[...,c]] for c in range(3)],axis=-1).astype(float)
    variance=np.stack([65536./parameters['inverse_variance'][c,ref[...,c].astype(int)>>4] for c in range(3)],axis=-1)
    variance=np.maximum(variance,[16.,4.,16.])
    yy,xx=np.mgrid[:120,:200];bottom=int(ground_row)//4
    # Sparse scoring is parameter/model selection only, never an output mask.
    y=yy*4;x=xx*4;minimum=np.full((120,200),np.inf)
    for dy in (-1,0,1):
        for dx in (-1,0,1):
            sample=bm.rgb565(reference[np.clip(y+dy,0,479),np.clip(x+dx,0,799)]).astype(int)
            score=sum((observed[...,c]-sample[...,c])**2*parameters['inverse_variance'][c,sample[...,c]>>4] for c in range(3))
            minimum=np.minimum(minimum,score)
    records=[];bad=[]
    for cy in range(8,bottom,8):
        for cx in range(8,200,8):
            ya,yb=max(0,cy-8),min(bottom,cy+8);xa,xb=max(0,cx-8),min(200,cx+8)
            y0,x0=yy[ya:yb,xa:xb],xx[ya:yb,xa:xb]
            design=np.stack((np.ones_like(x0),(x0-cx)/8.,(y0-cy)/8.),axis=-1).reshape(-1,3)
            obs=observed[ya:yb,xa:xb].reshape(-1,3);expected=ref[ya:yb,xa:xb].reshape(-1,3)
            var=variance[ya:yb,xa:xb].reshape(-1,3)
            train=((x0//2+y0//2)%2==0).ravel();test=~train
            if min(train.sum(),test.sum())<64:continue
            offset=np.median(obs[train]-expected[train],axis=0)
            gain=np.sum(expected[train]*obs[train],axis=0)/np.maximum(np.sum(expected[train]**2,axis=0),1e-9)
            pred=np.stack((expected+offset,expected*gain))
            losses=np.mean(np.minimum((obs[train]-pred[:,train])**2/var[train],30.),axis=1)
            losses[1,(gain<=.25)|(gain>=4.)]=np.inf
            selected=np.argmin(losses,axis=0)
            prediction=np.stack([pred[selected[c],:,c] for c in range(3)],axis=-1)
            coefs=np.linalg.lstsq(design[train],np.concatenate((obs[train],expected[train]),axis=1),rcond=-1)[0]
            null=design.dot(coefs);error=obs-prediction
            loss=float(np.mean(np.sum(error[test]**2/var[test],axis=1)))
            power=float(np.mean(np.sum((expected[test]-null[test,3:])**2/var[test],axis=1)))
            improvement=np.sum(((obs[test]-null[test,:3])**2-error[test]**2)/var[test],axis=1)
            verified=loss<4.5 and power>2 and improvement.mean()>.75 and np.mean(improvement>0)>.6
            if not verified:continue
            alarms=minimum[ya:yb,xa:xb].ravel()[test]>parameters['high'];n=len(alarms);k=int(alarms.sum())
            # One-sided conservative Wilson lower bound. It is a screening
            # statistic, not confidence in physical shape or object absence.
            phat=k/n;z=3.;lower=(phat+z*z/(2*n)-z*np.sqrt(phat*(1-phat)/n+z*z/(4*n*n)))/(1+z*z/n)
            rec=dict(center_xy=[cx*4,cy*4],loss=loss,power=power,alarms=k,samples=n,alarm_lower_bound=float(lower))
            records.append(rec)
            if lower>.005:bad.append(rec)
    return bool(bad),dict(verified_patches=len(records),unexplained_patches=len(bad),bad=bad,
        minimum_patches=1,selection_reason="any_verified_background_mismatch_requires_review",elapsed_host_or_board_s=time.monotonic()-started,
        sparse_parameter_selection_not_pixel_mask=True)



def review_spatial_reference(device, frame, reference, parameters, baseline_evidence,
                             baseline_status, ground, progress=None):
    """Try measured reference-position uncertainty before an appearance model.

    Uses the same fixed radius and background-edge validation as the existing
    review. Never infers a radius from the object or clips boundary pixels.
    Reuse the baseline PL output only when its exact thresholds are unchanged.
    """
    import background_model
    started=time.monotonic()
    offsets,info=background_model.choose_reference_offsets(frame,reference,parameters,ground)
    info=dict(info,algorithm='background_validated_original_reference_spatial_review',
              appearance_model_applied=False)
    if len(offsets)==1 or not info['target_met']:
        info['performed']=False
        return None,info,time.monotonic()-started
    if len(offsets)>9 or offsets[0]!=(0,0):
        raise RuntimeError('原始背景位置核验超出固定九参考上限')
    requires_model,appearance_info=appearance_review_required(frame,reference,parameters,ground)
    info['appearance_selection']=appearance_info
    if requires_model:
        info.update(performed=False,reason='heldout_background_texture_requires_appearance_model')
        return None,info,time.monotonic()-started
    cfg=dict(parameters,info=dict(parameters['info']))
    cfg['low']=info['recommended_low_q16'];cfg['high']=info['recommended_high_q16']
    cfg['info'].update(low_score_q16=cfg['low'],high_score_q16=cfg['high'],
        low_threshold=float(np.sqrt(cfg['low']/65536.)),high_threshold=float(np.sqrt(cfg['high']/65536.)))
    reuse=cfg['low']==parameters['low'] and cfg['high']==parameters['high']
    info.update(performed=True,cached_baseline_reused=bool(reuse),reference_hypotheses=len(offsets))
    preparation_s=time.monotonic()-started
    selected=offsets[1:] if reuse else offsets
    strong,possible,status,packed,raw=classify_reference_group(device,frame,reference,cfg,selected,progress)
    if reuse:
        strong &= baseline_evidence['strong'];possible &= baseline_evidence['possible']
        raw=[baseline_evidence['pl_packed_decisions']]+raw
        packed=_pack_decisions(strong,possible)
        status['pl_reference_pass_status']=baseline_status['pl_reference_pass_status']+status['pl_reference_pass_status']
        status['pl_reference_passes']=len(offsets)
    actual_new=len(selected)
    for key in ('pl_dma_transfer_and_wait_s','packing_configuration_dma_decode_s'):
        status[key]+=baseline_status.get(key,0.)
    status.update(pl_total_passes=1+actual_new,spatial_review_new_PL_passes=actual_new,
                  spatial_baseline_reused=bool(reuse),baseline_pl_status=baseline_status)
    cfg['info'].update(background_spatial_reference=info,
        background_appearance_model=dict(applied=False,reason='spatial_review_before_appearance_fit',accepted_patches=0))
    return (strong,possible,status,packed,raw,cfg,offsets),info,preparation_s


def classify_motion_group(device,frame,reference,params,proposal,baseline_status,
                          evidence_sink,progress=None):
    """PL classifies every rendered reference; PS only combines returned bits."""
    import reference_motion
    render_started=time.monotonic()
    poses=proposal['poses']
    models=np.empty((len(poses),)+SHAPE,dtype=np.uint16)
    for i,dy in enumerate(poses):
        models[i]=reference_motion.render_reference(reference,proposal,float(dy))
    evidence_sink['subpixel_reference_hypotheses565']=models
    evidence_sink['subpixel_dy_hypotheses']=np.asarray(poses,dtype=np.float64)
    evidence_sink['subpixel_lut']=params['lut'].copy()
    evidence_sink['subpixel_inverse_variance']=params['inverse_variance'].copy()
    evidence_sink['subpixel_low_q16']=params['low']
    evidence_sink['subpixel_high_q16']=params['high']
    raw=[];statuses=[];strong=possible=None
    render_s=time.monotonic()-render_started
    evidence_sink['subpixel_completed_pl_hypotheses_packed']=np.empty((0,WORDS),np.uint32)
    for i,model in enumerate(models):
        if progress:progress.update('PL亚像素背景参考判定','%d/%d'%(i+1,len(models)))
        s,p,status,packed=device.classify(frame,model,dict(params,_within_reference_group=i>0))
        raw.append(packed);statuses.append(status)
        evidence_sink['subpixel_completed_pl_hypotheses_packed']=np.stack(raw)
        if strong is None:strong,possible=s,p
        else:strong&=s;possible&=p
    # Parameter and reference changes apply only on their measured support.
    # Preserve the original PL bits exactly on every other image row.
    start,stop=proposal['start'],proposal['stop']
    strong[:start]=evidence_sink['strong'][:start];strong[stop:]=evidence_sink['strong'][stop:]
    possible[:start]=evidence_sink['possible'][:start];possible[stop:]=evidence_sink['possible'][stop:]
    status=dict(pl_reference_passes=len(models),
        pl_reference_pass_status=statuses,
        pl_dma_transfer_and_wait_s=sum(v['pl_dma_transfer_and_wait_s'] for v in statuses),
        packing_configuration_dma_decode_s=sum(v['packing_configuration_dma_decode_s'] for v in statuses),
        pl_input_pixels_per_pass=PIXELS,
        pl_binary_fusion='baseline_outside_support_AND_fractional_references_inside_support',
        pl_packed_decisions_kind='binary_fused_PL_passes',
        pl_reference_input_sha256=[hashlib.sha256(m.astype('<u2',copy=False).tobytes()).hexdigest() for m in models],
        pl_total_passes=baseline_status.get('pl_total_passes',1)+len(models),
        baseline_pl_status=baseline_status,subpixel_review_new_PL_passes=len(models),
        subpixel_reference_render_s=render_s)
    for key in ('pl_dma_transfer_and_wait_s','packing_configuration_dma_decode_s'):
        status[key]+=baseline_status.get(key,0.)
    return strong,possible,status,_pack_decisions(strong,possible),raw


def verify_removed_boundary_seeds(frame, reference, body, evidence, ground):
    """A nuisance model must not erase a crossing strong part without texture evidence.

    This verifies sparse raw-photo texture, not a second pixel classifier.
    Never average a missing object back into the mask or silently discard it.
    """
    if evidence is None:return []
    bottom=int(np.floor(ground));strong=evidence['strong'][:bottom]
    cutoff=evidence['diagnostics']['strong_noise_cluster_cutoff']
    runs,labels,areas,_=_regions(strong,strong)
    groups={}
    for y,a,b,node in runs:
        r=int(labels[node])
        if areas[r]<cutoff:continue
        item=groups.setdefault(r,dict(box=[800,bottom,0,0],retained=False,crosses=False))
        box=item['box'];item['box']=[min(box[0],a),min(box[1],y),max(box[2],b),max(box[3],y+1)]
        item['retained'] |= bool(body[y,max(a,160):min(b,640)].any()) if min(b,640)>max(a,160) else False
        item['crosses'] |= (a<=160<b or a<=639<b or (y==0 and b>160 and a<640))
    records=[]
    for item in groups.values():
        if item['retained'] or not item['crosses']:continue
        a,b,c,d=item['box']
        # Two sparse-sample intervals of context reveal the boundary edge;
        # the component itself alone can be a constant-colour edge fragment.
        a,b,c,d=max(0,a-4),max(0,b-4),min(800,c+4),min(480,d+4)
        yy,xx=np.mgrid[b:d:2,a:c:2]
        ref=_rgb(reference[yy,xx]).reshape(-1,3).astype(float)
        cur=_rgb(frame[yy,xx]).reshape(-1,3).astype(float)
        phase=((xx//2+yy//2)%2).ravel();folds=[];accepted=True
        if len(ref)<32:accepted=False
        else:
            for k in (0,1):
                train=phase==k;test=~train
                if min(train.sum(),test.sum())<16:accepted=False;break
                rc=ref[train]-ref[train].mean(0);fc=cur[train]-cur[train].mean(0)
                power=np.sum(rc*rc,axis=0)
                slope=np.sum(rc*fc,axis=0)/np.maximum(power,1e-9)
                predicted=(ref[test]-ref[train].mean(0))*slope+cur[train].mean(0)
                null=cur[train].mean(0)
                delta=np.sum((cur[test]-null)**2-(cur[test]-predicted)**2,axis=1)
                improvement=float(delta.mean());sem=float(delta.std()/np.sqrt(len(delta)))
                informative=power/float(train.sum())>np.array([16.,4.,16.])
                valid=bool(informative.any() and np.all((slope[informative]>.25)&(slope[informative]<4.))
                           and improvement>3*sem and improvement>0)
                folds.append(dict(improvement=improvement,standard_error=sem,slope=slope.tolist(),accepted=valid))
                accepted &= valid
        records.append(dict(bbox_xyxy=[a,b,c,d],accepted=bool(accepted),folds=folds))
    evidence['diagnostics']['removed_boundary_seed_texture_review']=records
    if any(not v['accepted'] for v in records):
        raise ValueError('背景模型会消除缺乏原图纹理支持的跨界强前景；无法排除真实裁切，已停止')
    return records


def propose_motion_reference(*args,**kwargs):
    import reference_motion
    return reference_motion.propose(*args,**kwargs)


def _segment_frames_v40(device, frames, reference, ground, reference_sigma=None,
                   directory=None, progress=None, label='照片',
                   context_frames=None,context_indices=None):
    import importlib
    try:
        import reference_motion
    except ImportError as exc:
        raise RuntimeError('请将配套reference_motion.py上传到pl_mask.py所在目录') from exc
    reference_motion=importlib.reload(reference_motion)
    if reference_motion.IMPLEMENTATION_VERSION<39:
        raise RuntimeError('请上传配套reference_motion.py')
    from adaptive_scan import BoundaryEvidenceError
    started = time.monotonic()
    masks, strongs, unknowns, diagnostics, packed_frames, model_grids, baseline_frames = [], [], [], [], [], [], []
    saved_luts,saved_variances,saved_lows,saved_highs=[],[],[],[]
    for i, frame in enumerate(frames):
        frame_started = time.monotonic()
        params = strong = possible = status = packed = None
        modeled = reference
        offsets = [(0,0)];hypothesis_packed = []
        model_grid = np.zeros((30,50,3),dtype=np.float32)
        baseline_evidence = None
        parameter_s = cleanup_s = 0.
        def timed_cleanup(s,p):
            nonlocal cleanup_s
            cleanup_started=time.monotonic()
            try:
                return clean_binary(s,p,ground)
            finally:
                cleanup_s+=time.monotonic()-cleanup_started
        route = 'baseline_single_PL_pass'
        review_s = 0.
        if progress: progress.update('估计背景参数', '%s %d/%d' % (label, i+1, len(frames)))
        try:
            # Preserve the established single-pass result whenever its original
            # checks succeed. Do not perturb a usable reference unconditionally.
            params=prepare_parameters(frame,reference,ground,reference_sigma)
            parameter_s = time.monotonic()-frame_started
            params['info'].update(algorithm=PIPELINE_ALGORITHM,
                                  background_model_version=BACKGROUND_MODEL_VERSION,
                                  processing_route=route,
                                  background_appearance_model=dict(applied=False,
                                      reason='baseline_not_yet_reviewed',accepted_patches=0),
                                  background_spatial_reference=dict(radius_px=0))
            if progress: progress.update('单次PL逐像素判定')
            strong, possible, status, packed, hypothesis_packed = classify_reference_group(
                device,frame,modeled,params,offsets,progress)
            baseline_packed=packed.copy()
            baseline_status=dict(status)
            if progress: progress.update('PS二值连通区域检查')
            try:
                body, seeds, unknown, cleanup = timed_cleanup(strong, possible)
                params['info']['background_appearance_model']['reason']='baseline_checks_passed_model_skipped'
            except BoundaryEvidenceError as baseline_error:
                original_parameters=params
                baseline_evidence=dict(strong=strong.copy(),possible=possible.copy(),
                    pl_packed_decisions=baseline_packed,lut=params['lut'].copy(),
                    inverse_variance=params['inverse_variance'].copy(),low_q16=params['low'],
                    high_q16=params['high'],diagnostics=dict(baseline_error.diagnostics,
                        parameters=dict(params['info']),pl_status=baseline_status))
                original_parameters=params
                motion_started=time.monotonic()
                proposal,motion_info=propose_motion_reference(
                    frame,reference,params,ground,baseline_error.foreground,
                    context_frames=frames if context_frames is None else context_frames,
                    frame_index=i if context_indices is None else context_indices[i])
                motion_prepare_s=time.monotonic()-motion_started
                parameter_s+=motion_prepare_s;review_s+=motion_prepare_s
                baseline_evidence['diagnostics']['subpixel_review']=motion_info
                motion_completed=False;motion_attempted=proposal is not None
                if proposal is not None:
                    params=reference_motion.rendered_parameters(params)
                    strong,possible,status,packed,hypothesis_packed=classify_motion_group(
                        device,frame,reference,params,proposal,baseline_status,baseline_evidence,progress)
                    parameter_s+=status.get('subpixel_reference_render_s',0.)
                    review_s+=status.get('subpixel_reference_render_s',0.)
                    params=dict(params,info=dict(params['info']))
                    params['info'].update(
                        background_reference_motion=motion_info,
                        background_review_count=0,max_total_PL_passes=19,
                        background_appearance_model=dict(applied=True,
                            model='local_vertical_subpixel_reference_v39',
                            reason='background_reference_position_modeled',accepted_patches=0),
                        background_spatial_reference=dict(radius_px=1,target_met=True,
                            kind='continuous_local_reference',reference_hypotheses=len(proposal['poses'])))
                    offsets=None
                    try:
                        body,seeds,unknown,cleanup=timed_cleanup(strong,possible)
                        motion_completed=True
                        route='baseline_then_validated_subpixel_reference_v39'
                    except BoundaryEvidenceError as motion_error:
                        baseline_evidence['diagnostics']['subpixel_cleanup_failure']=motion_error.diagnostics
                if not motion_completed:
                    # First test the measured spatial uncertainty on the ORIGINAL
                    # reference. A global warp/illumination model can help curtain
                    # anchors while making a separate support edge worse.
                    if progress: progress.update('边界歧义：原始背景位置核验')
                    spatial_started=time.monotonic()
                    if motion_attempted:
                        spatial=None
                        spatial_info=dict(motion_info,reason='integer_review_skipped_after_fractional_attempt')
                        spatial_prepare_s=0.
                    else:
                        spatial,spatial_info,spatial_prepare_s=review_spatial_reference(
                            device,frame,reference,params,baseline_evidence,baseline_status,ground,progress)
                    parameter_s+=spatial_prepare_s;review_s+=spatial_prepare_s
                    spatial_passed=False
                    if spatial is not None:
                        strong,possible,status,packed,hypothesis_packed,params,offsets=spatial
                        route='baseline_then_verified_original_spatial_review'
                        params['info'].update(processing_route=route,background_review_count=0,
                            baseline_boundary_diagnostics=baseline_error.diagnostics,max_total_PL_passes=19)
                        try:
                            body,seeds,unknown,cleanup=timed_cleanup(strong,possible)
                            spatial_passed=True
                            params['info']['background_appearance_model']['reason']='original_spatial_review_passed_model_not_needed'
                        except BoundaryEvidenceError as spatial_error:
                            baseline_evidence.update(spatial_strong=strong.copy(),spatial_possible=possible.copy(),
                                spatial_pl_packed_decisions=packed.copy(),spatial_offsets_yx=np.asarray(offsets),
                                spatial_low_q16=params['low'],spatial_high_q16=params['high'])
                            baseline_evidence['diagnostics']['spatial_review_failure']=spatial_error.diagnostics
                    spatial_elapsed=time.monotonic()-spatial_started
                    if not spatial_passed:
                        # At most one appearance review after one bounded spatial
                        # review. No empty-object, DMA or configuration errors retry.
                        prior_status=dict(status)
                        if progress: progress.update('边界仍有歧义：一次局部背景核验')
                        review_started=time.monotonic()
                        try:
                            candidate,review_reference,review_grid,review_offsets=prepare_background(
                                frame,reference,ground,reference_sigma,
                                baseline_parameters=original_parameters)
                        finally:
                            model_prepare_s=time.monotonic()-review_started
                            review_s+=model_prepare_s;parameter_s+=model_prepare_s
                        if len(review_offsets)>9:
                            raise RuntimeError('局部背景核验超过9次PL判定上限')
                        model_info=candidate['info']['background_appearance_model']
                        registration=model_info['background_registration']
                        if (not model_info['applied'] and not registration['accepted'] and len(review_offsets)==1):
                            status.update(processing_route='baseline_refused_without_verified_review',background_review_s=review_s)
                            raise baseline_error
                        params,modeled,model_grid,offsets=candidate,review_reference,review_grid,review_offsets
                        route=('baseline_spatial_then_verified_background_review' if spatial is not None
                               else 'baseline_then_verified_background_review')
                        params['info'].update(processing_route=route,baseline_boundary_diagnostics=baseline_error.diagnostics,
                            background_review_count=1,max_total_PL_passes=19,original_spatial_review=spatial_info)
                        strong=possible=packed=None;hypothesis_packed=[]
                        status=dict(prior_status,processing_route=route,completed_stage='before_appearance_PL',background_review_s=review_s)
                        if progress: progress.update('经核验的参考：PL判定')
                        strong,possible,review_status,packed,hypothesis_packed=classify_reference_group(
                            device,frame,modeled,params,offsets,progress)
                        status=dict(review_status)
                        for key in ('pl_dma_transfer_and_wait_s','packing_configuration_dma_decode_s'):
                            status[key]=status.get(key,0.)+prior_status.get(key,0.)
                        status['pl_total_passes']=prior_status.get('pl_total_passes',1)+review_status['pl_reference_passes']
                        status['baseline_pl_status']=baseline_status
                        if spatial is not None:status['spatial_pl_status']=prior_status
                        try:
                            body,seeds,unknown,cleanup=timed_cleanup(strong,possible)
                        except BoundaryEvidenceError as composed_error:
                            # Appearance and position changes can coexist. Assess
                            # residual position on the VERIFIED appearance reference,
                            # never on a substitute object image or altered silhouette.
                            compose_started=time.monotonic()
                            proposal,compose_info=propose_motion_reference(
                                frame,modeled,params,ground,composed_error.foreground,
                                context_frames=frames if context_frames is None else context_frames,
                                frame_index=i if context_indices is None else context_indices[i])
                            elapsed=time.monotonic()-compose_started
                            parameter_s+=elapsed;review_s+=elapsed
                            baseline_evidence['diagnostics']['composed_reference_review']=compose_info
                            if proposal is None:raise
                            # At most one bounded composition, never loop until pass.
                            if status['pl_total_passes']+len(proposal['poses'])>28:
                                raise RuntimeError('组合参考核验超过PL判定上限')
                            composition=dict(strong=strong.copy(),possible=possible.copy(),
                                pl_packed_decisions=packed.copy(),reference565=modeled.copy(),
                                lut=params['lut'].copy(),inverse_variance=params['inverse_variance'].copy(),
                                low_q16=params['low'],high_q16=params['high'])
                            params=reference_motion.rendered_parameters(params)
                            route='baseline_then_verified_appearance_and_position_v40'
                            try:
                                strong,possible,status,packed,hypothesis_packed=classify_motion_group(
                                    device,frame,modeled,params,proposal,status,composition,progress)
                            finally:
                                for evidence_key,evidence_value in composition.items():
                                    baseline_evidence['composed_'+evidence_key]=evidence_value
                            parameter_s+=status.get('subpixel_reference_render_s',0.)
                            review_s+=status.get('subpixel_reference_render_s',0.)
                            params=dict(params,info=dict(params['info']))
                            params['info'].update(background_reference_motion=compose_info,
                                reference_composition='validated_appearance_then_local_position',
                                background_appearance_model=dict(params['info']['background_appearance_model'],
                                    applied=True,composed_local_position=True),
                                max_total_PL_passes=28)
                            offsets=None
                            body,seeds,unknown,cleanup=timed_cleanup(strong,possible)
                            route='baseline_then_verified_appearance_and_position_v40'
                    status['spatial_review_elapsed_s']=spatial_elapsed
                    if status['pl_total_passes']>28:raise RuntimeError('PL边界核验超过总判定上限')
            audit_started=time.monotonic()
            cleanup['removed_boundary_seed_texture_review']=verify_removed_boundary_seeds(
                frame,reference,body,baseline_evidence,ground)
            cleanup_s+=time.monotonic()-audit_started
        except (ValueError, RuntimeError, np.linalg.LinAlgError) as exc:
            if status is not None:
                status.update(frame_elapsed_s=time.monotonic()-frame_started,
                              parameter_estimation_s=parameter_s,
                              binary_cleanup_s=cleanup_s,
                              background_review_s=review_s)
            try:
                details = _save_failure(directory, i+1, frame, reference, ground, exc,
                                        params, possible, strong, status, packed, reference_sigma,
                                        modeled,offsets,hypothesis_packed,model_grid,baseline_evidence)
            except Exception as diagnostic_error:
                details = '；诊断保存失败：%s' % diagnostic_error
            wrapped=RuntimeError('第%d张PL掩膜生成失败：%s%s' % (i+1, exc, details))
            wrapped.segmentation_frame_evidence=dict(parameters=params,possible=possible,strong=strong,status=status,
                packed=packed,baseline_evidence=baseline_evidence,modeled_reference=modeled,
                reference_offsets=offsets,hypothesis_packed=hypothesis_packed,model_grid=model_grid)
            raise wrapped from exc
        info = dict(params['info'], **status)
        info.update(cleanup)
        info.update(segmentation_elapsed_s=time.monotonic()-frame_started,
                    parameter_estimation_s=parameter_s, binary_cleanup_s=cleanup_s,
                    background_review_s=review_s,processing_route=route,
                    pl_total_passes=status.get('pl_total_passes',status['pl_reference_passes']),
                    frame_sha256=hashlib.sha256(np.asarray(frame).astype('<u2', copy=False).tobytes()).hexdigest())
        diagnostics.append(info)
        masks.append(body); strongs.append(seeds); unknowns.append(unknown); packed_frames.append(packed)
        model_grids.append(model_grid)
        baseline_frames.append(baseline_packed)
        saved_luts.append(params['lut'].copy());saved_variances.append(params['inverse_variance'].copy())
        saved_lows.append(params['low']);saved_highs.append(params['high'])
    return dict(masks=np.stack(masks), photo_strong_masks=np.stack(strongs),
                photo_unknown_masks=np.stack(unknowns), pl_packed_decisions=np.stack(packed_frames),
                photo_thresholds=np.asarray([v['high_threshold'] for v in diagnostics]),
                photo_threshold_units='standardized_background_residual',
                segmentation_diagnostics_json=json.dumps(diagnostics, ensure_ascii=False),
                segmentation_version=8, segmentation_implementation_version=20,
                segmentation_algorithm=PIPELINE_ALGORITHM, segmentation_backend='PL',
                background_model_version=BACKGROUND_MODEL_VERSION,background_model_grids=np.stack(model_grids),
                pl_baseline_packed_decisions=np.stack(baseline_frames),
                pl_luts=np.stack(saved_luts),pl_inverse_variances=np.stack(saved_variances),
                pl_low_q16=np.asarray(saved_lows,dtype=np.uint32),
                pl_high_q16=np.asarray(saved_highs,dtype=np.uint32),
                pl_packed_decisions_kind='binary_fused_PL_reference_passes',
                segmentation_elapsed_s=time.monotonic()-started)


def _suppressed_foreground_review(frame, reference, ground, noise, result, witness):
    """Optional sparse audit of independently discarded, very strong cores."""
    old=result['masks'][0].astype(bool)
    s,_=unpack_decisions(result['pl_baseline_packed_decisions'][0])
    if not (s&~old).any():return dict(requested=False,components=[],sample_pixels=0)
    parameters=None
    runs,labels,areas,_=_regions(s,s);groups={}
    for y,a,b,node in runs:
        r=int(labels[node]);item=groups.setdefault(r,dict(runs=[],overlap=False,box=[800,480,0,0]))
        item['runs'].append((y,a,b));item['overlap'] |= bool(old[y,a:b].any())
        box=item['box'];item['box']=[min(box[0],a),min(box[1],y),max(box[2],b),max(box[3],y+1)]
    cutoff=int(json.loads(result['segmentation_diagnostics_json'])[0].get('strong_noise_cluster_cutoff',6))
    found=[];sampled=0
    for root,item in groups.items():
        a,b,c,d=item['box']
        if item['overlap'] or not 6<=areas[root]<cutoff or not (160<a<c<640 and 0<b<d<=ground):continue
        if sampled+4>512:break
        if parameters is None:parameters=prepare_parameters(frame,reference,ground,noise)
        coords=np.asarray([(y,x) for y,a,b in item['runs'] for x in range(a,b)],int)
        coords=coords[np.linspace(0,len(coords)-1,min(4,len(coords))).astype(int)]
        y,x=coords.T;raw=witness.rgb565(frame[y,x]).astype(int);ref=witness.rgb565(reference[y,x]).astype(int)
        cur=np.stack([parameters['lut'][ch,raw[:,ch]] for ch in range(3)],1)
        score=sum((cur[:,ch]-ref[:,ch]).astype(np.int64)**2*
                  parameters['inverse_variance'][ch,ref[:,ch]>>4].astype(np.int64) for ch in range(3))
        sampled+=len(coords)
        if not np.all(score>max(25.902*65536,2*parameters['high'])):continue
        compatible=witness.background_compatible(frame,reference,parameters['lut'],coords,noise)
        if compatible.all():continue
        found.append(dict(pixels=int(areas[root]),bbox_xyxy=item['box'],pixels_yx=coords.tolist(),
            minimum_score_q16=int(score.min()),size_cutoff=cutoff))
    return dict(requested=bool(found),components=found,sample_pixels=sampled)


def _edge_frame_result(frame,reference,ground,noise,previous,state,mask,detail,features,baseline):
    cfg=features['parameters'];qstrong=features['strong'];packed=features['packed'][0]
    previous_info=json.loads(previous['segmentation_diagnostics_json'])[0] if previous is not None else {}
    status=state.get('status') or {}
    old_passes=previous_info.get('pl_total_passes',status.get('pl_total_passes',status.get('pl_reference_passes',1)))
    diag=dict(previous_info)
    diag.update(backend='PL',algorithm='PL_colour_and_edges_PS_region_graph_v44',
        processing_route='edge_graph_after_legacy_refusal_v44' if previous is None else 'edge_graph_confirmed_independent_part_v44',
        high_threshold=float(np.sqrt(cfg['high']/65536.)),low_threshold=float(np.sqrt(cfg['low']/65536.)),
        low_score_q16=cfg['low'],high_score_q16=cfg['high'],
        frame_raw_color_modified=False,hole_filling=False,geometric_expansion=False,
        strong_noise_cluster_cutoff=6,ground_row=float(ground),roi_xyxy=[160,0,640,480],
        boundary_requires_review=False,boundary_removed_components=[],boundary_removed_pixels=0,
        foreground_pixels=int(mask.sum()),
        pl_total_passes=int(old_passes+3),max_total_PL_passes=31,pl_reference_passes=3,
        pl_reference_pass_status=features['status'],edge_partition=detail,
        full_resolution_colour_and_gradient_in_PL=True,final_region_inference_in_PS=True,
        pl_packed_decisions_kind='actual_PL_colour_feature_not_final_region_labels',
        frame_sha256=hashlib.sha256(frame.astype('<u2',copy=False).tobytes()).hexdigest())
    base={} if previous is None else dict(previous)
    base.update(masks=mask[None].astype(np.uint8),photo_strong_masks=(mask&qstrong)[None].astype(np.uint8),
        photo_unknown_masks=(mask&~qstrong)[None].astype(np.uint8),
        pl_packed_decisions=np.asarray([packed],np.uint32),pl_baseline_packed_decisions=np.asarray([baseline],np.uint32),
        pl_luts=np.asarray([cfg['lut']],np.uint8),pl_inverse_variances=np.asarray([cfg['inverse_variance']],np.uint16),
        pl_low_q16=np.asarray([cfg['low']],np.uint32),pl_high_q16=np.asarray([cfg['high']],np.uint32),
        photo_thresholds=np.asarray([diag['high_threshold']]),photo_threshold_units='standardized_background_residual',
        segmentation_diagnostics_json=json.dumps([diag],ensure_ascii=False),segmentation_version=8,
        segmentation_implementation_version=20,segmentation_algorithm='PL_evidence_automatic_edge_graph_v44',
        segmentation_backend='PL',background_model_version=BACKGROUND_MODEL_VERSION,
        background_model_grids=np.zeros((1,30,50,3),np.float32),
        pl_packed_decisions_kind='actual_PL_colour_feature_not_final_region_labels',
        segmentation_elapsed_s=float(detail['elapsed_host_or_board_s']))
    return base


def _segment_frames_v48(device, frames, reference, ground, reference_sigma=None,
                   directory=None, progress=None, label='照片',
                   context_frames=None,context_indices=None):
    """Keep established successes; switch to PL-edge graph on an actual refusal."""
    if progress is None:
        from scan_pipeline import _SegmentationProgress
        class PreviewProgress(_SegmentationProgress):
            def _render(self):
                if self.handle is None:return
                st=self._snapshot()
                line='%s %s｜%s｜本阶段 %.0f 秒，总计 %.0f 秒'%(st['status'],st['frame'],st['stage'],st['stage_elapsed_s'],st['elapsed_s'])
                try:
                    with self.render_lock:self.handle.update(line)
                except Exception:pass
        with PreviewProgress(directory,backend='PL',version=46) as live:
            return _segment_frames_v48(device,frames,reference,ground,reference_sigma,directory,live,label,context_frames,context_indices)
    import importlib
    try:
        import edge_partition,background_witness
    except ImportError as missing:
        raise RuntimeError('请配套上传 pl_mask.py、edge_partition.py、background_witness.py 三个文件') from missing
    from adaptive_scan import BoundaryEvidenceError
    for module in (edge_partition,background_witness):
        if Path(module.__file__).resolve().parent!=Path(__file__).resolve().parent:
            raise RuntimeError('请将 edge_partition.py、background_witness.py 与 pl_mask.py 放在同一目录')
    edge_partition=importlib.reload(edge_partition);background_witness=importlib.reload(background_witness)
    started=time.monotonic();results=[];edge_records=[];edge_indices=[];edge_adopted=[]
    all_frames=frames if context_frames is None else context_frames
    for i,frame in enumerate(frames):
        previous=None;legacy_error=None;state={};frame_started=time.monotonic()
        index=i if context_indices is None else context_indices[i]
        attempt=Path(directory)/('legacy_attempt_%02d'%(i+1)) if directory is not None else None
        try:
            previous=_segment_frames_v40(device,[frame],reference,ground,reference_sigma,
                attempt,progress,label,context_frames=all_frames,context_indices=[index])
        except RuntimeError as exc:
            state=getattr(exc,'segmentation_frame_evidence',{})
            if not isinstance(exc.__cause__,BoundaryEvidenceError):
                cause=exc.__cause__ if exc.__cause__ is not None else exc
                try:
                    details=_save_failure(directory,i+1,frame,reference,ground,cause,
                        state.get('parameters'),state.get('possible'),state.get('strong'),state.get('status'),
                        state.get('packed'),reference_sigma,state.get('modeled_reference'),
                        state.get('reference_offsets'),state.get('hypothesis_packed'),state.get('model_grid'),state.get('baseline_evidence'))
                except Exception as saving_error:details='；诊断保存失败：%s'%saving_error
                raise RuntimeError('第%d张PL掩膜生成失败：%s%s'%(i+1,cause,details)) from cause
            legacy_error=exc
        raw_boundary=background_witness.boundary_texture_loss(frame,reference,ground)
        parts=(dict(requested=False,components=[],sample_pixels=0) if previous is None else
               _suppressed_foreground_review(frame,reference,ground,reference_sigma,previous,background_witness))
        needed=legacy_error is not None or raw_boundary['requires_review'] or parts['requested']
        if not needed:
            diag=json.loads(previous['segmentation_diagnostics_json']);diag[0]['raw_boundary_texture_check']=raw_boundary
            diag[0]['independent_part_review']=parts
            previous['segmentation_diagnostics_json']=json.dumps(diag,ensure_ascii=False)
            results.append(previous);continue
        reason=('原分割边界歧义' if legacy_error is not None else '原图边界证据变化' if raw_boundary['requires_review'] else '独立小部件复核')
        if progress:progress.update('自动切换边缘图分割：'+reason,'%s %d/%d'%(label,i+1,len(frames)))
        baseline=state.get('baseline_evidence')
        if baseline is not None:
            # The initial unmodified-reference estimate is already preserved.
            # Do not refit its response/noise when switching methods.
            parameters=dict(lut=baseline['lut'],inverse_variance=baseline['inverse_variance'],
                            low=int(baseline['low_q16']),high=int(baseline['high_q16']),
                            info=baseline['diagnostics']['parameters'])
        else:
            parameters=prepare_parameters(frame,reference,ground,reference_sigma)
        features=None
        try:
            proposal,detail,features=edge_partition.partition(device,frame,reference,parameters['lut'],ground,reference_sigma,progress)
            edge_records.append(features);edge_indices.append(i);edge_adopted.append(False)
            detail.update(raw_boundary_texture_check=raw_boundary,independent_part_review=parts)
            if detail['boundary_requires_review']:
                raise BoundaryEvidenceError('边缘图分割仍有前景候选触及测量边界；无法确认完整入画',
                    features['full_foreground'],features['strong'],dict(roi_xyxy=[160,0,640,480],ground_row=ground,edge_partition=detail))
            if int(proposal.sum())<150:raise ValueError('边缘图算法没有提供足够的物体证据')
            if previous is not None and not raw_boundary['requires_review']:
                # Restore a genuinely discarded independent component only.
                # Never union entire alternate masks or fill old internal zeros.
                original=previous['masks'][0].astype(bool);new=original.copy()
                labels=features['labels'];selected=set()
                for component in parts['components']:
                    for y,x in component['pixels_yx']:selected.add(int(labels[y,x]))
                accepted_ids=[]
                for region in sorted(selected):
                    region_mask=np.zeros(SHAPE,bool);region_mask[:labels.shape[0]]=labels==region
                    if not proposal[region_mask].all() or not features['possible'][region_mask].all():continue
                    if region_mask[0,160:640].any() or region_mask[:,160].any() or region_mask[:,639].any():continue
                    new |= region_mask;accepted_ids.append(region)
                detail.update(restored_source_regions=accepted_ids,restored_pixels=int((new&~original).sum()),
                    existing_mask_pixels_removed=0,restoration_requires_fresh_PL_possible=True)
                if np.array_equal(new,original):
                    diag=json.loads(previous['segmentation_diagnostics_json']);diag[0]['edge_partition_not_adopted']=detail
                    diag[0]['pl_total_passes']=diag[0].get('pl_total_passes',1)+3
                    previous['segmentation_diagnostics_json']=json.dumps(diag,ensure_ascii=False)
                    results.append(previous);continue
                proposal=new
            baseline=(previous['pl_baseline_packed_decisions'][0] if previous is not None else
                      state['baseline_evidence']['pl_packed_decisions'])
            result=_edge_frame_result(frame,reference,ground,reference_sigma,previous,state,proposal,detail,features,baseline)
            result['segmentation_elapsed_s']=time.monotonic()-frame_started
            results.append(result);edge_adopted[-1]=True
        except (ValueError,RuntimeError) as exc:
            # Optional independent-part review must not turn a previous success
            # into a new rejection. A raw boundary conflict or original refusal
            # remains blocking; input/DMA errors always stop.
            physical_evidence=getattr(exc,'edge_partition_evidence',None)
            hardware_failure=getattr(exc,'edge_partition_hardware_failure',False) or getattr(exc,'edge_partition_input_failure',False)
            if previous is not None and not raw_boundary['requires_review'] and not hardware_failure:
                diag=json.loads(previous['segmentation_diagnostics_json'])
                diag[0]['independent_part_review']=dict(parts,alternate_not_adopted=str(exc),requires_review=True)
                extra=features if features is not None else physical_evidence
                if extra:
                    diag[0]['pl_total_passes']=diag[0].get('pl_total_passes',1)+len(extra['packed'])
                    if not edge_indices or edge_indices[-1]!=i:
                        edge_records.append(extra);edge_indices.append(i);edge_adopted.append(False)
                previous['segmentation_diagnostics_json']=json.dumps(diag,ensure_ascii=False)
                results.append(previous);continue
            evidence=dict(state.get('baseline_evidence') or {})
            actual=features if features is not None else physical_evidence
            if actual:
                evidence['edge_input_references565']=np.asarray(actual['references'],np.uint16)
                evidence['edge_completed_packed']=np.asarray(actual['packed'],np.uint32).reshape(-1,WORDS)
                evidence['edge_luts']=np.asarray([c['lut'] for c in actual['configurations']],np.uint8)
                evidence['edge_inverse_variances']=np.asarray([c['inverse_variance'] for c in actual['configurations']],np.uint16)
                evidence['edge_low_q16']=np.asarray([c['low'] for c in actual['configurations']],np.uint32)
                evidence['edge_high_q16']=np.asarray([c['high'] for c in actual['configurations']],np.uint32)
            cfg=features['parameters'] if features is not None else state.get('parameters',parameters)
            try:
                details=_save_failure(directory,i+1,frame,reference,ground,exc,cfg,
                    None if features is None else features['possible'],None if features is None else features['strong'],
                    state.get('status'),None if features is None else features['packed'][0],reference_sigma,
                    reference,[(0,0)],None,None,evidence)
            except Exception as saving_error:details='；诊断保存失败：%s'%saving_error
            origin=legacy_error.__cause__ if legacy_error is not None and legacy_error.__cause__ is not None else legacy_error
            if isinstance(exc,BoundaryEvidenceError):
                # Evidence rejection is one failure, not several hardware
                # failures. Keep full traces in the run directory.
                if directory is not None:
                    try:
                        import traceback
                        old_trace=(''.join(traceback.format_exception(type(legacy_error),legacy_error,legacy_error.__traceback__))
                                   if legacy_error is not None else str(raw_boundary))
                        trace=old_trace+'\n替代阶段：\n'+traceback.format_exc()
                        Path(directory,'segmentation_traceback_%02d.txt'%(i+1)).write_text(trace,encoding='utf-8')
                    except OSError:pass
                raise RuntimeError('第%d张掩膜未通过核验：前景候选触及测量边界，尚不能区分背景变化与物体裁切。原阶段：%s%s'%(i+1,origin or raw_boundary,details)) from None
            raise RuntimeError('第%d张PL掩膜生成失败，原始原因：%s；已尝试替代分割：%s%s'%(i+1,origin or raw_boundary,exc,details)) from exc
    first=results[0];combined={}
    for key,value in first.items():
        if key=='segmentation_diagnostics_json':
            combined[key]=json.dumps([json.loads(r[key])[0] for r in results],ensure_ascii=False)
        elif isinstance(value,np.ndarray) and value.ndim and value.shape[0]==1:
            combined[key]=np.concatenate([r[key] for r in results],axis=0)
        else:combined[key]=value
    combined.update(segmentation_elapsed_s=time.monotonic()-started,
        segmentation_algorithm='PL_evidence_automatic_edge_graph_v44',
        edge_frame_indices=np.asarray(edge_indices,np.int16),
        edge_mask_adopted_indices=np.asarray([i for i,used in zip(edge_indices,edge_adopted) if used],np.int16))
    if edge_records:
        combined.update(edge_colour_packed_decisions=np.asarray([v['packed'][0] for v in edge_records],np.uint32),
            edge_horizontal_packed_decisions=np.asarray([v['packed'][1] for v in edge_records],np.uint32),
            edge_vertical_packed_decisions=np.asarray([v['packed'][2] for v in edge_records],np.uint32),
            edge_luts=np.asarray([[c['lut'] for c in v['configurations']] for v in edge_records],np.uint8),
            edge_inverse_variances=np.asarray([[c['inverse_variance'] for c in v['configurations']] for v in edge_records],np.uint16),
            edge_low_q16=np.asarray([[c['low'] for c in v['configurations']] for v in edge_records],np.uint32),
            edge_high_q16=np.asarray([[c['high'] for c in v['configurations']] for v in edge_records],np.uint32))
    return combined


def segment_frames(device, frames, reference, ground, reference_sigma=None,
                   directory=None, progress=None, label='照片',
                   context_frames=None, context_indices=None, axis_col=None, calibration=None):
    """Fixed measurement scope, no automatic retries of the archived routes."""
    import importlib, sys
    import subject_region, roi_reference, interior_evidence
    parent=Path(__file__).resolve().parent
    for module in (subject_region,roi_reference,interior_evidence):
        if Path(module.__file__).resolve().parent!=parent:
            raise RuntimeError('subject_region.py、roi_reference.py 必须与 pl_mask.py 同目录')
    subject_region=importlib.reload(subject_region)
    roi_reference=importlib.reload(roi_reference)
    for name in ('edge_partition','background_witness','competitive_colour'):
        module=importlib.import_module(name)
        if Path(module.__file__).resolve().parent!=parent:
            raise RuntimeError('%s.py 必须与 pl_mask.py 同目录'%name)
        importlib.reload(module)
    if subject_region.VERSION<76 or module.VERSION<76:
        raise RuntimeError('v76配套文件不完整；请一起覆盖subject_region.py、subject_evidence.py和competitive_colour.py')
    # A directly loaded module is not necessarily registered in sys.modules
    # during independent tests; use a namespace of this module's real globals.
    import types
    driver=types.SimpleNamespace(**globals())
    window=interior_evidence.measurement_window(calibration,ground)
    driver.measurement_roi=window
    original_prepare=driver.prepare_parameters
    def scoped_prepare(*a,**k):
        k['measurement_roi']=window
        try:return original_prepare(*a,**k)
        except ValueError as exc:
            if '静止背景样本不足' not in str(exc):raise
            # A near-full-width declared domain has too little exterior.
            # Use measured EMPTY-scene noise, never treat current object pixels
            # as background to create enough samples. Rim validation remains.
            a=list(a);a[0]=a[1]
            k.pop('exterior_validity',None)
            k['measurement_roi']=(160,0,640,480)
            cfg=original_prepare(*a,**k)
            cfg['info']['parameter_source']='empty_reference_due_to_insufficient_exterior'
            return cfg
    driver.prepare_parameters=scoped_prepare
    if calibration is not None:axis_col=float(calibration['axis_col'])
    if progress is not None:
        return subject_region.segment_frames(driver,device,frames,reference,ground,
            reference_sigma,directory,progress,label,axis_col)
    from scan_pipeline import _SegmentationProgress
    class SubjectProgress(_SegmentationProgress):
        def _render(self):
            if self.handle is None:return
            st=self._snapshot()
            line='%s %s｜%s｜本阶段 %.0f 秒，总计 %.0f 秒'%(st['status'],st['frame'],st['stage'],st['stage_elapsed_s'],st['elapsed_s'])
            try:
                with self.render_lock:self.handle.update(line)
            except Exception:pass
    with SubjectProgress(directory,backend='PL',version=82) as live:
        return subject_region.segment_frames(driver,device,frames,reference,ground,
            reference_sigma,directory,live,label,axis_col)



def _segment_scan_reference(device, color_scan, empty565, ground_row, out_path=None,
                 preview_path=None, reference_sigma=None, axis_col=None, calibration=None):
    from scan_pipeline import _SegmentationProgress, _closure_diagnostic, _atomic_npz, save_mask_sheet
    frames = np.asarray(color_scan['frames565'])
    angles = np.asarray(color_scan['angles_deg'], dtype=np.float64)
    if frames.ndim != 3 or frames.shape[1:] != SHAPE or frames.dtype != np.uint16 or \
            len(frames) != len(angles) or not np.isfinite(angles).all():
        raise ValueError('彩色原图和角度数量/格式不匹配')
    _frame(empty565); _frame(color_scan['closing_frame565'])
    directory = Path(out_path).parent if out_path is not None else Path(preview_path).parent if preview_path is not None else None
    class PlainProgress(_SegmentationProgress):
        def _render(self):
            if self.handle is None:return
            s=self._snapshot()
            line='%s %s｜%s｜本阶段 %.0f 秒，总计 %.0f 秒' % (
                s['status'],s['frame'],s['stage'],s['stage_elapsed_s'],s['elapsed_s'])
            try:
                # Older board display frontends can show HTML's object repr.
                # A plain string remains readable with the same single area.
                with self.render_lock:self.handle.update(line)
            except Exception:
                pass
    import subject_evidence,importlib
    if Path(subject_evidence.__file__).resolve().parent!=Path(__file__).resolve().parent:
        raise RuntimeError('subject_evidence.py必须与pl_mask.py同目录')
    subject_evidence=importlib.reload(subject_evidence)
    if getattr(subject_evidence,'PERFORMANCE_VERSION',0)<81:
        raise RuntimeError('请一起覆盖v81包里的五个Python文件')
    resumed=subject_evidence.recover_completed_save(color_scan,empty565,ground_row,reference_sigma,calibration)
    with PlainProgress(directory, backend='PL', version=89) as progress:
        if resumed is not None:
            progress.update('复用已完成的掩膜，仅重新筛选和保存','')
            result=subject_evidence.normalize_completed_save(resumed[0])
            closing=subject_evidence._largest_component8(resumed[1])[0]
            resumed=True  # Release the unfiltered completed arrays after conversion.
        else:
            result = segment_frames(device, frames, empty565, ground_row, reference_sigma, directory, progress,axis_col=axis_col,calibration=calibration)
            closing = segment_frames(device, [color_scan['closing_frame565']], empty565, ground_row,
                                     reference_sigma, directory/'closing' if directory is not None else None,
                                     progress, '首尾核验帧',context_frames=frames,
                                     context_indices=[0],axis_col=axis_col,calibration=calibration)['masks'][0]
        import interior_evidence, types
        if calibration is not None:axis_col=float(calibration['axis_col'])
        progress.update('有限三视角内部复核', '')
        multiview=interior_evidence.audit_multiview(frames,result,angles,axis_col,types.SimpleNamespace(**globals()))
        result['interior_multiview_diagnostics_json']=json.dumps(multiview,ensure_ascii=False)
        if calibration is not None:
            result['measurement_calibration_json']=json.dumps({k:float(calibration[k]) for k in
                ('left_px','right_px','axis_col','v_ground','mm_per_px')},sort_keys=True)
        progress.update('首尾一致性检查', '')
        closure = _closure_diagnostic(frames, color_scan['closing_frame565'], result['masks'], closing, angles, directory)
        result.update(angles_deg=angles, source='pl_color_photos',
                      frame_pixel_order=str(color_scan.get('frame_pixel_order','legacy_unspecified')),
                      frame_reader_version=int(color_scan.get('frame_reader_version',0)),
                      empty_color_frame565=np.asarray(empty565).copy(),
                      closing_color_frame565=np.asarray(color_scan['closing_frame565']).copy(),
                      closure_iou=closure['closure_iou'],
                      closure_diagnostic_json=json.dumps(closure, ensure_ascii=False),
                      color_frames_sha256=hashlib.sha256(frames.astype('<u2', copy=False).tobytes()).hexdigest(),
                      empty_frame_sha256=hashlib.sha256(np.asarray(empty565).astype('<u2', copy=False).tobytes()).hexdigest(),
                      pl_board_self_test_passed=(bool(result.get('pl_board_self_test_passed',False)) if resumed is not None else bool(device.self_test_passed)))
        if reference_sigma is not None:
            result['background_noise_sigma'] = np.asarray(reference_sigma, dtype=np.float32)
        if out_path is not None:
            progress.update('保存掩膜及诊断数据')
            import subject_evidence
            if hasattr(subject_evidence,'save_scan_archive'):
                subject_evidence.save_scan_archive(out_path,result,progress)
            else:
                _atomic_npz(out_path, **result)
        if preview_path is not None:
            progress.update('生成掩膜预览')
            save_mask_sheet(result['masks'], preview_path)
    print('PL掩膜首尾 IoU=%.3f' % result['closure_iou'], flush=True)
    if result['closure_iou'] < .8 and directory is not None:
        print('首尾差异需核对：%s' % (directory/'closure_comparison.png'), flush=True)
    return result


def segment_scan(device, color_scan, empty565, ground_row, out_path=None,
                 preview_path=None, reference_sigma=None, axis_col=None, calibration=None):
    # Sidecar only: original image evidence and the saved NPZ field set stay exact.
    wall=time.monotonic();cpu=time.process_time();state='failed_or_interrupted'
    directory=Path(out_path).parent if out_path is not None else Path(preview_path).parent if preview_path is not None else None
    print('第7步等效实现 v89；分割规则不变，结束后自动保存本机版本与耗时。',flush=True)
    try:
        result=_segment_scan_reference(device,color_scan,empty565,ground_row,out_path,
            preview_path,reference_sigma,axis_col,calibration)
        state='completed'
        return result
    finally:
        try:
            import exact_scan_accel
            exact_scan_accel.save_performance(directory,state,time.monotonic()-wall,
                time.process_time()-cpu,bool(getattr(device,'self_test_passed',False)))
        except Exception as error:
            print('本机提速记录未保存：%s'%error,flush=True)
