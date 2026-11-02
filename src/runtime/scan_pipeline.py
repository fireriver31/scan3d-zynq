"""OV5640 RTL 掩膜 -> 多角度采集 -> PL 三维重建的 PS 侧准备工作。

板上的转盘没有角度接口；角度来自实测转动周期和每次 DDR 读取的时间戳。
这只能近似角度。单 DDR 帧缓冲没有帧序号/双缓冲，软件无法证明所读
800x480 像素来自同一摄像头帧；正式精度测试需要加帧同步接口。
"""

import os
import hashlib
import json
import time
import threading
from contextlib import redirect_stderr, redirect_stdout
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


HEIGHT = 480
WIDTH = 800
COLOR_PHOTO_REPAIR_VERSION = 7
GEOMETRY_ALIGNMENT_VERSION = 5
COLOR_SEGMENTATION_VERSION = 7
COLOR_SEGMENTATION_ALGORITHM = "validated_noise_hysteresis_reference_check_v7"
CALIBRATION_BOUNDARY_VERSION = 97
SIMPLE_SCAN_GUARD_VERSION = 49
FRAME_PIXEL_ORDER = 'camera_raster_fifo16x64_restored_v29'
FRAME_ORDER_FIX_VERSION = 29
RECONSTRUCTION_PERFORMANCE_VERSION = 64
UNCERTAINTY_SCOPE_VERSION = 69
UNCERTAINTY_DISPLAY_VERSION = 72
VOXEL_COLOR_DISPLAY_VERSION = 95


def _uncertainty_scope_module():
    import importlib, uncertainty_scope
    if Path(uncertainty_scope.__file__).resolve().parent != Path(__file__).resolve().parent:
        raise RuntimeError('uncertainty_scope.py必须与scan_pipeline.py同目录')
    return importlib.reload(uncertainty_scope)


def rgb565_to_rgb(frame):
    """将原版彩色 bit 的 DDR RGB565 帧解成 RGB888；不改动帧缓存。"""
    a = np.asarray(frame)
    if a.shape != (HEIGHT, WIDTH) or a.dtype != np.uint16:
        raise ValueError("彩色标定需要 480x800 uint16 RGB565 原始帧")
    out = np.empty((HEIGHT, WIDTH, 3), dtype=np.uint8)
    r, g, b = (a >> 11) & 31, (a >> 5) & 63, a & 31
    out[:, :, 0] = ((r << 3) | (r >> 2)).astype(np.uint8)
    out[:, :, 1] = ((g << 2) | (g >> 4)).astype(np.uint8)
    out[:, :, 2] = ((b << 3) | (b >> 2)).astype(np.uint8)
    return out


def save_color_frame(frame, path):
    """另存标定原图，方便人眼检查相机、光照和转台位置。"""
    Image.fromarray(rgb565_to_rgb(frame)).save(str(path))


def save_color_sheet(frames, path, cols=6):
    """保存同一圈物体彩色原图的缩略图，不把黑白掩膜误称为照片。"""
    a = np.asarray(frames)
    tile_w, tile_h = 200, 120
    sheet = Image.new("RGB", (tile_w * cols, tile_h * ((len(a) + cols - 1) // cols)))
    for i, frame in enumerate(a):
        tile = Image.fromarray(rgb565_to_rgb(frame))
        tile = tile.resize((tile_w, tile_h), resample=Image.NEAREST)
        sheet.paste(tile, ((i % cols) * tile_w, (i // cols) * tile_h))
    sheet.save(str(path))





def color_difference_mask(frame565, empty565):
    """用同一像素的空台彩色图作背景，返回初始掩膜和照片中的亮斑候选。

    曝光偏移只从画面顶部估计。亮斑候选不直接计作物体，须再经
    轮廓局部性和其他视角核验；饱和到与背景同色时仍无法绝对判定。
    """
    frame = rgb565_to_rgb(frame565).astype(np.int16)
    empty = rgb565_to_rgb(empty565).astype(np.int16)
    shift = np.median((frame[:40] - empty[:40]).reshape(-1, 3), axis=0)
    corrected = np.clip(frame.astype(np.float32) - shift, 0, 255)
    delta = np.sqrt(np.sum((corrected - empty) ** 2, axis=2))
    threshold = max(24.0, float(np.percentile(delta[:40], 99.5)) * 1.8)
    foreground = delta > threshold
    bright = (frame.min(axis=2) >= 220) & (frame.max(axis=2) >= 240)
    return foreground.astype(np.uint8), bright.astype(np.uint8), float(threshold)


def _row_span(mask_row, min_run=20):
    """找一行里最长的连续变化段；小缺口已在调用处水平闭合。"""
    padded = np.concatenate(([False], mask_row, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    if len(edges) < 2:
        return None
    lens = edges[1::2] - edges[::2]
    idx = int(np.argmax(lens))
    if lens[idx] < min_run:
        return None
    return int(edges[2 * idx]), int(edges[2 * idx + 1] - 1)


def _quantile_finite(values, fraction):
    """用顺序统计量取分位数，兼容板上旧 NumPy 并拒绝无效值。"""
    a = np.asarray(values, dtype=np.float32).ravel()
    if not len(a) or not np.isfinite(a).all():
        raise ValueError("图像差分里有 NaN/Inf 或空区域；请检查 DDR 帧")
    k = int(np.ceil(fraction * (len(a) - 1)))
    return float(np.partition(a, k)[k])


def _turntable_upper_band(rows, lefts, rights):
    """Choose the first persistent upper plateau, not a global width percentile.

    A few lower reflection rows must not set the target width of the rim.
    Contact below a selected stable window cannot invalidate its diameter.
    Never search past sustained contact to find a convenient lower window.
    """
    widths = rights - lefts + 1
    clipped = (lefts < 5) | (rights >= WIDTH - 5)
    # The old v36 check rejected on any five clipped rows in all 80 search
    # rows, even when an earlier complete stable rim had already been found.
    contacts=[]
    begin=None
    for k in range(len(rows)+1):
        hit=k<len(rows) and bool(clipped[k])
        if hit and begin is None:begin=k
        if not hit and begin is not None:
            if k-begin>=5:contacts.append((begin,k))
            begin=None
    first_contact=contacts[0][0] if contacts else len(rows)
    if len(rows) < 8:
        raise ValueError("上部候选不足连续8行，无法确认稳定测量带；查看标定诊断")
    # Recorded for diagnosis only: lower rows cannot set the rim's width floor.
    supported_peak = max(float(np.min(widths[i:i + 5]))
                         for i in range(len(rows) - 4))
    window = min(20, len(rows))
    for i in range(len(rows) - window + 1):
        # A full candidate window must precede sustained edge contact, and
        # even isolated contact is not allowed inside the measured window.
        if i+window>first_contact:
            break
        if clipped[i:i+window].any():
            continue
        ls, rs = lefts[i:i + window], rights[i:i + window]
        ml, mr = float(np.median(ls)), float(np.median(rs))
        width = mr - ml + 1
        if width < 100.:
            continue
        spread = max(_quantile_finite(np.abs(ls - ml), .90),
                     _quantile_finite(np.abs(rs - mr), .90))
        if spread > max(8., .025 * width):
            continue
        local_peak = _quantile_finite(widths[i:i + window], .95)
        local_wide = widths[i:i + window] >= .97 * local_peak
        if np.count_nonzero(local_wide) < 5:
            continue
        broad_start = int(rows[i + np.flatnonzero(local_wide)[0]])
        band = rows[i:i + window][widths[i:i + window] >= .95 * local_peak]
        if len(band) >= 5:
            return broad_start, band, dict(
                method='stable_upper_window_before_boundary_contact_v45',
                boundary_check_scope='selected_window_not_entire_search',
                search_boundary_contact_rows=rows[clipped].astype(int).tolist(),
                persistent_boundary_runs_y=[[int(rows[a]),int(rows[b-1])+1] for a,b in contacts],
                search_rows=[int(rows[0]), int(rows[-1])],
                local_window_rows=[int(rows[i]), int(rows[i + window - 1])],
                supported_peak_px=supported_peak, local_peak_px=float(local_peak))
    if contacts:
        raise ValueError("上沿尚未确认稳定测量带就连续触及左右边缘；可能裁切或差分污染，查看标定诊断")
    raise ValueError("上部候选的左右边界没有稳定测量带；查看两张原图及标定诊断，不能据此认定转台出画")


def calibrate_turntable_photos(background565, turntable565,
                               diameter_mm=200.0, preview_path=None,
                               ground_row_override=None):
    """无转台/有空转台两张彩色原图，量上部直径、转轴和台面行。

    只用转台顶缘附近的宽带，不把下方反光/桌面当成直径。不能可靠
    定位时直接抛异常；`ground_row_override` 供目视核对后手动修正。
    返回值不含图像数组，可直接写入 JSON。
    """
    if not np.isfinite(diameter_mm) or diameter_mm <= 0:
        raise ValueError("转台实测直径必须是正数，单位 mm")
    bg = rgb565_to_rgb(background565).astype(np.int16)
    fg = rgb565_to_rgb(turntable565).astype(np.int16)
    if np.array_equal(bg, fg):
        raise ValueError("两张图完全相同：DDR 可能没更新，或没有放入转台")
    # 标定图的底边有转台反光，右下可能有手，不能把整圈边框当
    # 固定背景。只用远离转台的顶部 40 行估计自动曝光偏移。
    anchor = np.zeros((HEIGHT, WIDTH), dtype=bool)
    anchor[:40, :] = True
    shift = np.median((fg - bg)[anchor], axis=0)
    corrected = np.clip(fg.astype(np.float32) - shift, 0, 255)
    dist = np.max(np.abs(corrected - bg), axis=2)
    anchor_noise = _quantile_finite(dist[anchor], 0.95)
    if anchor_noise > 32:
        raise ValueError("顶部背景变化太大（95%%=%.1f），相机/灯光/曝光可能改变；请重拍" % anchor_noise)
    threshold = max(20.0, anchor_noise * 2.0)
    # A white turntable may be darker than the wall it occludes. Accept
    # either contrast polarity at the same RGB/noise threshold. The
    # existing upper-band and boundary checks still exclude lower
    # reflections/shadows from the diameter measurement.
    raw = dist > threshold
    if preview_path is not None:
        diff_path = Path(preview_path).with_name(
            Path(preview_path).stem + "_difference.png")
        Image.fromarray(np.clip(dist * 3.0, 0, 255).astype(np.uint8)).save(str(diff_path))
    anchor_changed = float(raw[anchor].mean())
    if anchor_changed > 0.08:
        raise ValueError("顶部固定背景变化过多（%.1f%%），请检查镜头/灯光；差分图已保存" %
                         (100.0 * anchor_changed))
    # 只填同一行里至多 8 px 的小黑洞；不改变上/下边界。
    mask = raw.copy()
    for y in range(HEIGHT):
        xs = np.flatnonzero(raw[y])
        if len(xs) < 2:
            continue
        gaps = np.flatnonzero((np.diff(xs) > 1) & (np.diff(xs) <= 9))
        for i in gaps:
            mask[y, xs[i] + 1:xs[i + 1]] = True
    spans = [_row_span(mask[y], min_run=80) for y in range(HEIGHT)]
    good = np.asarray([s is not None for s in spans])
    # 至少连续 8 行有宽前景，避免手/高亮杂点被当作转台顶缘。
    runs = []
    begin = None
    for y in range(HEIGHT + 1):
        hit = y < HEIGHT and bool(good[y])
        if hit and begin is None:
            begin = y
        if not hit and begin is not None:
            if y - begin >= 8:
                runs.append((begin, y))
            begin = None
    if not runs:
        raise ValueError("没有找到连续的转台变化带；查看两张彩色原图，排除手、阴影和背景运动")
    # 印字/黑线可能把白盘分成上下两段；下段通常面积更大，但不能
    # 因此丢掉真正的上沿。先排除细小杂物，再选最靠上的主要宽区域。
    def run_stats(ab):
        ww = [spans[y][1] - spans[y][0] + 1 for y in range(ab[0], ab[1])]
        return max(ww), sum(ww)
    stats = [run_stats(ab) for ab in runs]
    max_peak = max(s[0] for s in stats)
    max_area = max(s[1] for s in stats)
    candidates = [ab for ab, st in zip(runs, stats)
                  if st[0] >= 0.70 * max_peak and st[1] >= 0.08 * max_area]
    start, stop = min(candidates, key=lambda ab: ab[0])
    # The change region includes bright tabletop reflections below the plate.
    # Its lower end is not the turntable boundary and is never used to measure
    # the diameter or contact plane. Only the actual measurement band must fit.
    lower_difference_reaches_bottom = bool(stop > HEIGHT - 5)
    if start < 5:
        raise ValueError("上沿候选变化触及画面顶部，不能确认完整上沿；请查看原图和差分图")
    # 下半部常有投影和反光，只搜上方最多 80 行。
    upper_stop = min(stop, start + 80)
    rows = np.arange(start, upper_stop)
    lefts = np.asarray([spans[y][0] for y in rows], dtype=float)
    rights = np.asarray([spans[y][1] for y in rows], dtype=float)
    widths = rights - lefts + 1
    diagnostic = dict(version=CALIBRATION_BOUNDARY_VERSION,
                      contrast_method='absolute_rgb_difference',
                      positive_brightness_required=False,
                      selected_difference_run_y=[int(start), int(stop)],
                      rows=rows.tolist(), lefts=lefts.tolist(),
                      rights=rights.tolist(), widths=widths.tolist(),
                      threshold_rgb=float(threshold),
                      background_noise_p95=float(anchor_noise),
                      status='selecting_upper_band')
    diagnostic_path = (Path(preview_path).with_name(
        Path(preview_path).stem + '_diagnostic.json')
        if preview_path is not None else None)
    try:
        broad_start, band_rows, selection = _turntable_upper_band(rows, lefts, rights)
    except ValueError as exc:
        if diagnostic_path is not None:
            diagnostic.update(status='rejected', error=str(exc))
            diagnostic_path.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2),
                                       encoding='utf-8')
        raise
    if int(band_rows[0]) < 5 or int(band_rows[-1]) > HEIGHT - 5:
        raise ValueError("实际用于标定的上部宽带触及图像边缘，无法确认完整标定带；请查看原图和差分图")
    ls = lefts[band_rows - start]
    rs = rights[band_rows - start]
    left_px = float(np.median(ls))
    right_px = float(np.median(rs))
    width_px = right_px - left_px + 1.0
    edge_spread = float(max(_quantile_finite(np.abs(ls - left_px), 0.90),
                            _quantile_finite(np.abs(rs - right_px), 0.90)))
    if edge_spread > max(8.0, 0.025 * width_px):
        raise ValueError("上沿左右边界不稳定（%.1f px），可能混入阴影或其它物体" % edge_spread)
    if left_px < 5 or right_px >= WIDTH - 5:
        raise ValueError("转台左右边缘出画，不能量直径")
    # 背景/相机变化不能自动求出转轴；仅当盘是圆形且相机基本正对时，
    # 图像上沿的中点才是转轴投影。
    axis_col = 0.5 * (left_px + right_px)
    ground_row = float(broad_start)
    if ground_row_override is not None:
        if not np.isfinite(ground_row_override) or not 0 <= ground_row_override < HEIGHT:
            raise ValueError("手动台面行必须在 0..479 之间")
        ground_row = float(ground_row_override)
    result = dict(mm_per_px=float(diameter_mm) / width_px,
                  frame_pixel_order=FRAME_PIXEL_ORDER,
                  frame_order_fix_version=FRAME_ORDER_FIX_VERSION,
                  axis_col=axis_col, v_ground=ground_row,
                  width_px=width_px, left_px=left_px, right_px=right_px,
                  upper_row_start=int(band_rows[0]),
                  upper_row_end=int(band_rows[-1]),
                  edge_spread_px=edge_spread,
                  background_noise_p95=anchor_noise,
                  anchor_changed_fraction=anchor_changed,
                  threshold_rgb=float(threshold),
                  calibration_boundary_version=CALIBRATION_BOUNDARY_VERSION,
                  calibration_contrast_method='absolute_rgb_difference',
                  calibration_requires_brighter_foreground=False,
                  upper_band_selection=selection,
                  selected_difference_run_y=[int(start), int(stop)],
                  selected_difference_run_reaches_bottom=lower_difference_reaches_bottom,
                  lower_difference_edge_used_for_calibration=False,
                  ground_source=("manual" if ground_row_override is not None
                                 else "first_full_width_row_check_preview"),
                  diameter_mm=float(diameter_mm))
    if preview_path is not None:
        diagnostic.update(status='accepted', calibration=result)
        diagnostic_path.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2),
                                   encoding='utf-8')
        # 青色为检测到的变化，红线为轴，蓝线为物体底面，黄线为直径。
        view = fg.astype(np.uint8).copy()
        view[mask] = (0.65 * view[mask] + 0.35 * np.array([0, 255, 255])).astype(np.uint8)
        image = Image.fromarray(view)
        d = ImageDraw.Draw(image)
        d.line((axis_col, 0, axis_col, HEIGHT - 1), fill=(255, 0, 0), width=2)
        d.line((0, ground_row, WIDTH - 1, ground_row), fill=(0, 70, 255), width=2)
        d.line((left_px, band_rows[0], right_px, band_rows[0]), fill=(255, 255, 0), width=3)
        d.ellipse((left_px - 5, band_rows[0] - 5, left_px + 5, band_rows[0] + 5),
                  fill=(255, 255, 0))
        d.ellipse((right_px - 5, band_rows[0] - 5, right_px + 5, band_rows[0] + 5),
                  fill=(255, 255, 0))
        image.save(str(preview_path))
    return result


def object_mask(frame):
    """把 RTL 的 RGB565 黑白帧变为 1=物体、0=背景的二维数组。"""
    a = np.asarray(frame)
    if a.shape != (HEIGHT, WIDTH) or a.dtype != np.uint16:
        raise ValueError("需要 480x800 的 uint16 原始 DDR 帧")
    bad = (a != 0) & (a != 0xffff)
    if np.any(bad):
        raise ValueError("掩膜中有 %d 个非黑白像素；检查 bit、地址或帧撕裂" %
                         int(np.count_nonzero(bad)))
    return (a == 0).astype(np.uint8)


def _largest_component(mask):
    """按逐行连续段找最大的八连通区域，避免逐像素 Python 洪泛。"""
    parent, area, runs = [], [], []
    previous = []

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def join(a, b):
        a, b = root(a), root(b)
        if a == b:
            return
        if area[a] < area[b]:
            a, b = b, a
        parent[b] = a
        area[a] += area[b]

    for y, row in enumerate(mask):
        edge = np.flatnonzero(np.diff(np.pad(row.astype(np.int8), (1, 1), mode="constant")))
        current = []
        previous_index = 0
        for left, right in zip(edge[::2], edge[1::2]):
            left, right = int(left), int(right)  # right 为不含端点
            index = len(parent)
            parent.append(index)
            area.append(right - left)
            current.append((left, right, index))
            runs.append((y, left, right, index))
            while previous_index < len(previous) and previous[previous_index][1] < left:
                previous_index += 1
            scan_index = previous_index
            while scan_index < len(previous) and previous[scan_index][0] <= right:
                join(index, previous[scan_index][2])
                scan_index += 1
        previous = current

    if not runs:
        return np.zeros_like(mask, dtype=bool), 0, 0
    roots = sorted((area[i] for i in range(len(parent)) if root(i) == i), reverse=True)
    winner = max((i for i in range(len(parent)) if root(i) == i), key=lambda i: area[i])
    result = np.zeros_like(mask, dtype=bool)
    for y, left, right, index in runs:
        if root(index) == winner:
            result[y, left:right] = True
    return result, roots[0], roots[1] if len(roots) > 1 else 0


def clean_object_mask(raw_mask, empty_mask, ground_row,
                      roi=(160, 0, 640, 480), return_stats=False):
    """离线清理 RTL 掩膜：空台差分、台面裁剪、细线去除、主体连通域。

    这是有损的轮廓处理；如主体贴到裁剪边界或出现两个相近的大块，
    必须报错并人工查看原图，不能悄悄生成看似正常的体积。
    """
    raw = np.asarray(raw_mask, dtype=bool)
    empty = np.asarray(empty_mask, dtype=bool)
    if raw.shape != (HEIGHT, WIDTH) or empty.shape != raw.shape:
        raise ValueError("原始掩膜与空转台掩膜都必须是 480x800")
    if not np.isfinite(ground_row):
        raise ValueError("台面行必须是有限数")
    x0, y0, x1, y1 = map(int, roi)
    if not (0 <= x0 < x1 <= WIDTH and 0 <= y0 < y1 <= HEIGHT):
        raise ValueError("掩膜裁剪区域超出画面")
    bottom = min(y1, int(np.floor(ground_row)))  # 台面自身不属于物体
    if bottom <= y0 + 10:
        raise ValueError("台面行太靠上，无法保留物体")

    candidate = raw[y0:bottom, x0:x1] & ~empty[y0:bottom, x0:x1]
    tile = Image.fromarray(candidate.astype(np.uint8) * 255, mode="L")
    # 3x3 开运算消掉细线和小点；闭运算补回小缺口。
    tile = tile.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    tile = tile.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    filtered = np.asarray(tile, dtype=np.uint8) != 0
    body, body_area, second_area = _largest_component(filtered)
    if body_area < 150:
        raise ValueError("清理后没有足够大的物体轮廓；查看原始掩膜和空转台掩膜")
    if second_area >= max(150, 0.35 * body_area):
        raise ValueError("清理后仍有两个相近的大黑块；无法确定哪个是物体")
    ys, xs = np.nonzero(body)
    left, right = int(xs.min() + x0), int(xs.max() + x0)
    top, low = int(ys.min() + y0), int(ys.max() + y0)
    if left <= x0 + 2 or right >= x1 - 3 or top <= y0 + 2:
        raise ValueError("清理后主体仍碰到测量区上方或左右边缘；物体可能被裁掉")
    if right - left + 1 >= 0.90 * (x1 - x0):
        raise ValueError("主体宽度接近整个测量区，可能把转台横线连进来了")

    result = np.zeros(raw.shape, dtype=np.uint8)
    result[y0:bottom, x0:x1] = body.astype(np.uint8)
    stats = {"raw_pixels": int(raw.sum()), "candidate_pixels": int(candidate.sum()),
             "filtered_pixels": int(filtered.sum()), "kept_pixels": int(body_area),
             "second_pixels": int(second_area), "bbox": (left, top, right, low)}
    return (result, stats) if return_stats else result


def read_valid_mask(grab_fn, tries=3):
    """遇到非二值的瞬时帧时重读；全部失败则明确报错。"""
    last_error = None
    for _ in range(tries):
        frame = grab_fn()
        try:
            return object_mask(frame)
        except ValueError as exc:
            last_error = exc
            time.sleep(0.01)
    raise RuntimeError("连续 %d 次读不到二值帧：%s" % (tries, last_error))


def calibrate_reference(frame, known_diameter_mm, preview_path=None):
    """用放在转轴上的已知直径哑光圆柱，标定比例/转轴/台面行。

    拍摄前须已用空转台采过 RTL 背景。圆柱应竖直、居中、轮廓完整。
    返回的台面行来自圆柱掩膜最下端，仍要看预览图核对阴影。
    """
    if not np.isfinite(known_diameter_mm) or known_diameter_mm <= 0:
        raise ValueError("known_diameter_mm 必须是实测的正数")
    mask = object_mask(frame).astype(bool)
    ys, xs = np.nonzero(mask)
    if len(xs) < 200:
        raise ValueError("圆柱前景太少；检查背景和阈值")
    if xs.min() == 0 or xs.max() == WIDTH - 1 or ys.min() == 0 or ys.max() == HEIGHT - 1:
        raise ValueError("圆柱贴到画面边缘，不能可靠标定")

    low = int(ys.min() + 0.2 * (ys.max() - ys.min()))
    high = int(ys.min() + 0.8 * (ys.max() - ys.min()))
    widths, centers = [], []
    for row in range(low, high + 1):
        cols = np.flatnonzero(mask[row])
        if len(cols) >= 10:
            widths.append(float(cols[-1] - cols[0] + 1))
            centers.append(0.5 * float(cols[0] + cols[-1]))
    if len(widths) < 10:
        raise ValueError("圆柱有效行太少，不能量直径")
    width_px = float(np.median(widths))
    spread = float(np.median(np.abs(np.asarray(widths) - width_px)))
    if width_px < 10 or spread > max(3.0, 0.15 * width_px):
        raise ValueError("圆柱各行宽度变化太大；可能有阴影、反光或背景污染")
    axis_col = float(np.median(centers))
    ground_row = float(ys.max())
    result = dict(mm_per_px=float(known_diameter_mm) / width_px,
                  axis_col=axis_col, v_ground=ground_row,
                  width_px=width_px, width_mad_px=spread)
    if preview_path is not None:
        rgb = np.repeat((255 - mask.astype(np.uint8) * 255)[:, :, None], 3, axis=2)
        im = Image.fromarray(rgb)
        draw = ImageDraw.Draw(im)
        draw.line((axis_col, 0, axis_col, HEIGHT - 1), fill=(255, 0, 0), width=2)
        draw.line((0, ground_row, WIDTH - 1, ground_row), fill=(0, 0, 255), width=2)
        im.save(str(preview_path))
    return result


def calibrate_known_span(left_px, right_px, ground_row, known_diameter_mm):
    """用已测量的转台上沿左右边界和 200 mm 等已知直径标定。

    左右边界须取自未移动相机时保存的原始彩色图，不能从物体掩膜猜。
    透视会使不同高度的毫米/像素变化，此比例是台面附近的近似值。
    """
    values = [left_px, right_px, ground_row, known_diameter_mm]
    if not all(np.isfinite(v) for v in values):
        raise ValueError("标定值必须都是有限数")
    if not (0 <= left_px < right_px < WIDTH and 0 <= ground_row < HEIGHT):
        raise ValueError("转台左右边界或台面行超出 800x480 图像")
    if known_diameter_mm <= 0:
        raise ValueError("已知转台直径必须大于 0")
    width_px = float(right_px - left_px + 1)
    return dict(mm_per_px=float(known_diameter_mm) / width_px,
                axis_col=0.5 * float(left_px + right_px),
                v_ground=float(ground_row), width_px=width_px,
                width_mad_px=None)


def measure_period(turns=3, clock=time.monotonic, ask=None):
    """对着转台边缘的固定记号计时；在同一格内可重测或取消。"""
    if turns < 1:
        raise ValueError("turns 至少为 1")
    if ask is None:
        ask = input
    while True:
        start = ask("记号到指定位置时按回车开始；输入 q 回车取消：").strip().lower()
        if start == "q":
            return None
        if start:
            print("请直接按回车开始，或输入 q 取消。")
            continue
        t0 = clock()
        end = ask("记号再经过同一位置 %d 次后按回车；没测好输入 r 回车重测，q 取消：" %
                  turns).strip().lower()
        if end == "r":
            print("已放弃本次计时，重新开始。")
            continue
        if end == "q":
            return None
        if end:
            print("输入无效，本次计时作废；请重新测。")
            continue
        dt = clock() - t0
        if dt <= 0:
            print("计时无效，请重新测。")
            continue
        return dt / float(turns)


def save_mask_sheet(masks, path, cols=6):
    """保存一圈掩膜缩略图：黑=物体，白=背景。"""
    a = np.asarray(masks)
    n = len(a)
    tile_w, tile_h = 200, 120
    sheet = Image.new("L", (tile_w * cols, tile_h * ((n + cols - 1) // cols)), 255)
    for i, m in enumerate(a):
        tile = Image.fromarray(255 - np.asarray(m, dtype=np.uint8) * 255)
        tile = tile.resize((tile_w, tile_h), resample=Image.NEAREST)
        sheet.paste(tile, ((i % cols) * tile_w, (i // cols) * tile_h))
    sheet.save(str(path))


def _atomic_npz(path, **arrays):
    path = Path(path)
    temp = path.with_name(path.name + ".part")
    with open(str(temp), "wb") as stream:
        np.savez_compressed(stream, **arrays)
    os.replace(str(temp), str(path))


def capture_views(grab_fn, period_s, n_views=24, out_path=None,
                  preview_path=None, closure_check=True,
                  empty_mask=None, ground_row=None, raw_preview_path=None,
                  clock=time.monotonic, sleeper=time.sleep):
    """按周期抓取一圈 RTL 掩膜；采集结束后再清理，避免错过角度。

    grab_fn 应返回 DDR 中一份 480x800 uint16 拷贝。时间戳是读取时刻，
    不是传感器曝光时刻；若转速变化或帧缓存撕裂，角度/轮廓会出错。
    """
    if not np.isfinite(period_s) or period_s <= 0:
        raise ValueError("period_s 必须是实测的正数")
    if not isinstance(n_views, int) or n_views < 3:
        raise ValueError("n_views 至少为 3；现有 PL bit 通常要求 24")
    if empty_mask is None or ground_row is None:
        raise ValueError("清理掩膜必须提供空转台掩膜和已核对的台面行")
    interval = float(period_s) / n_views
    t0 = clock()
    masks, times, latencies = [], [], []
    for i in range(n_views):
        remaining = t0 + i * interval - clock()
        if remaining > 0:
            sleeper(remaining)
        begin = clock()
        late = begin - (t0 + i * interval)
        if late > 0.25 * interval:
            raise RuntimeError("第 %d 张采集迟到 %.3f 秒；这一圈角度不可靠" %
                               (i + 1, late))
        mask = read_valid_mask(grab_fn)
        end = clock()
        masks.append(mask)
        times.append(0.5 * (begin + end))
        latencies.append(end - begin)
        print("%02d/%02d  t=%.3fs  原始黑像素=%d" %
              (i + 1, n_views, times[-1] - t0, int(mask.sum())))

    angles = (np.asarray(times, dtype=np.float64) - times[0]) * 360.0 / period_s
    if angles[-1] >= 360.0:
        raise RuntimeError("最后一个视角超过一圈，采集时间或周期有误")
    closing_raw = None
    if closure_check:
        remaining = t0 + period_s - clock()
        if remaining > 0:
            sleeper(remaining)
        closing_raw = read_valid_mask(grab_fn)
    raw_path = None
    if out_path is not None:
        destination = Path(out_path)
        raw_path = destination.with_name(destination.stem + "_raw" + destination.suffix)
        # 先抓完首尾校验帧，再保存；离线清理即使失败，原始 24 张仍在。
        _atomic_npz(raw_path, raw_masks=np.stack(masks).astype(np.uint8),
                    angles_deg=angles, times_s=np.asarray(times) - times[0],
                    period_s=float(period_s), grab_s=np.asarray(latencies))
        print("已保存完整分辨率原始掩膜：", raw_path)
    if raw_preview_path is not None:
        save_mask_sheet(masks, raw_preview_path)
        print("已保存原始掩膜对照图：", raw_preview_path)
    cleaned, clean_stats = [], []
    for i, raw in enumerate(masks):
        try:
            body, stats = clean_object_mask(raw, empty_mask, ground_row,
                                            return_stats=True)
        except ValueError as exc:
            raise RuntimeError("第 %d 张清理失败：%s；请先看原始掩膜图" %
                               (i + 1, exc)) from exc
        cleaned.append(body)
        clean_stats.append(stats)
        print("%02d/%02d  清理后保留=%d，范围=%s" %
              (i + 1, n_views, stats["kept_pixels"], stats["bbox"]))
    masks = np.stack(cleaned).astype(np.uint8)
    closure_iou = np.nan
    if closure_check:
        try:
            closing = clean_object_mask(closing_raw, empty_mask,
                                        ground_row).astype(bool)
        except ValueError as exc:
            raise RuntimeError("一圈结束时的掩膜清理失败：%s" % exc) from exc
        first = masks[0].astype(bool)
        last = closing.astype(bool)
        union = np.count_nonzero(first | last)
        closure_iou = (np.count_nonzero(first & last) / float(union)) if union else 0.0
        print("转满一圈与第一张的轮廓重合度 IoU=%.3f" % closure_iou)
        if closure_iou < 0.8:
            print("警告：首尾轮廓不一致，先检查转速、角度、物体松动和掩膜")

    result = dict(masks=masks, angles_deg=angles,
                  times_s=np.asarray(times) - times[0],
                  period_s=float(period_s),
                  grab_s=np.asarray(latencies),
                  raw_scan_path=str(raw_path) if raw_path is not None else "",
                  clean_candidate_pixels=np.asarray(
                      [s["candidate_pixels"] for s in clean_stats], dtype=np.int32),
                  clean_filtered_pixels=np.asarray(
                      [s["filtered_pixels"] for s in clean_stats], dtype=np.int32),
                  clean_kept_pixels=np.asarray(
                      [s["kept_pixels"] for s in clean_stats], dtype=np.int32),
                  clean_second_pixels=np.asarray(
                      [s["second_pixels"] for s in clean_stats], dtype=np.int32),
                  clean_bbox_xyxy=np.asarray(
                      [s["bbox"] for s in clean_stats], dtype=np.int16),
                  closure_iou=float(closure_iou))
    if out_path is not None:
        _atomic_npz(out_path, **result)
        print("已保存扫描数据：", out_path)
    if preview_path is not None:
        save_mask_sheet(masks, preview_path)
        print("已保存 24 视角预览：", preview_path)
    return result


def capture_color_views(grab_fn, period_s, n_views=24, out_path=None,
                        preview_path=None, clock=time.monotonic,
                        sleeper=time.sleep):
    """用原彩色 bit 连续保存一圈 RGB565 原图，掩膜留到采集结束后处理。"""
    if not np.isfinite(period_s) or period_s <= 0 or n_views < 3:
        raise ValueError("转动周期必须为正、视角数至少为 3")
    interval = float(period_s) / n_views
    t0 = clock()
    frames, times = [], []
    for i in range(n_views):
        remaining = t0 + i * interval - clock()
        if remaining > 0:
            sleeper(remaining)
        begin = clock()
        if begin - (t0 + i * interval) > 0.25 * interval:
            raise RuntimeError("第 %d 张彩色图采集太迟，角度不可靠" % (i + 1))
        frame = np.asarray(grab_fn())
        end = clock()
        if frame.shape != (HEIGHT, WIDTH) or frame.dtype != np.uint16:
            raise ValueError("彩色采集需要 480x800 uint16 RGB565 帧")
        if np.count_nonzero((frame != 0) & (frame != 0xffff)) < 1000:
            raise RuntimeError("彩色帧几乎只有黑白两值；仍加载着掩膜 bit，不能当原图")
        frames.append(frame.copy())
        times.append(0.5 * (begin + end))
        print("%02d/%02d  彩色原图 t=%.3fs" % (i + 1, n_views, times[-1] - t0))
    remaining = t0 + period_s - clock()
    if remaining > 0:
        sleeper(remaining)
    closing = np.asarray(grab_fn(), dtype=np.uint16).copy()
    if closing.shape != (HEIGHT, WIDTH):
        raise ValueError("整圈结束时的彩色帧尺寸错误")
    angles = (np.asarray(times, dtype=np.float64) - times[0]) * 360.0 / period_s
    if angles[-1] >= 360.0:
        raise RuntimeError("最后一张已超过一圈，请重新测量周期")
    result = dict(frames565=np.stack(frames), angles_deg=angles,
                  frame_pixel_order=getattr(grab_fn,'frame_pixel_order','unspecified'),
                  frame_reader_version=getattr(grab_fn,'frame_reader_version',0),
                  closing_frame565=closing, period_s=float(period_s),
                  times_s=np.asarray(times) - times[0])
    if out_path is not None:
        _atomic_npz(out_path, **result)
        print("彩色原图及角度已保存：", out_path)
    if preview_path is not None:
        save_color_sheet(result["frames565"], preview_path)
    return result


class _SegmentationProgress:
    """One notebook status area, with timing evidence independent of completion."""
    def __init__(self, directory=None, backend='PS', version=18):
        self.backend, self.version = backend, version
        self.started = time.monotonic()
        self.cpu_started = time.process_time()
        self.state = {'status':'running','stage':'准备分割','frame':'',
                      'stage_started_s':0.,'implementation_version':version,
                      'segmentation_backend':backend}
        self.events = []
        self.path = Path(directory)/'segmentation_progress.json' if directory is not None else None
        self.handle = None
        self.stop = threading.Event()
        self.render_lock = threading.Lock()
        self.worker = None
        self._last_progress_save_s = float("-inf")
        self._last_progress_render_s = float("-inf")

    def __enter__(self):
        print('开始%s掩膜处理（v%d），下面显示当前阶段。' % (self.backend,self.version),flush=True)
        try:
            from IPython import get_ipython
            from IPython.display import display
            if get_ipython() is not None:
                self.handle = display('',display_id=True)
        except Exception:
            self.handle = None
        self.update('准备分割')
        if self.handle is not None and hasattr(self.handle,'update'):
            self.worker = threading.Thread(target=self._heartbeat)
            self.worker.daemon = True
            self.worker.start()
        return self

    def _snapshot(self):
        snapshot = dict(self.state)
        snapshot['elapsed_s'] = time.monotonic()-self.started
        snapshot['stage_elapsed_s'] = snapshot['elapsed_s']-snapshot['stage_started_s']
        snapshot['cpu_elapsed_s'] = time.process_time()-self.cpu_started
        snapshot['progress_performance_version'] = 82
        import sys
        edge=sys.modules.get('edge_partition')
        snapshot['region_label_backend'] = getattr(edge,'REGION_ACCELERATION_BACKEND','not_loaded')
        return snapshot

    def _render(self):
        if self.handle is None:return
        snapshot = self._snapshot()
        line = '%s %s｜%s｜本阶段 %.0f 秒，总计 %.0f 秒' % (
            snapshot['status'],snapshot['frame'],snapshot['stage'],
            snapshot['stage_elapsed_s'],snapshot['elapsed_s'])
        try:
            from IPython.display import HTML
            from html import escape
            with self.render_lock:self.handle.update(HTML('<div>'+escape(line)+'</div>'))
        except Exception:
            pass

    def _heartbeat(self):
        while not self.stop.wait(2.):self._render()

    def _screen_progress(self):
        # Optional observer: no new import or dependency when UI is absent.
        import sys
        module=sys.modules.get('scan_screen')
        if module is not None:
            module.publish_progress(self._snapshot(),self.path.parent if self.path is not None else None)

    def update(self, stage, frame=None):
        elapsed = time.monotonic()-self.started
        self.events.append(self._snapshot())
        self.state = dict(self.state,stage=stage,stage_started_s=elapsed,
                          frame=self.state['frame'] if frame is None else frame)
        self._save()
        # Keep every event in memory; coalesce only expensive frontend I/O.
        if elapsed-self._last_progress_render_s>=.5:
            self._render()
            self._last_progress_render_s=elapsed
        self._screen_progress()

    def _save(self):
        if self.path is None:return
        now=time.monotonic()
        if self.state.get('status')=='running' and now-self._last_progress_save_s<2.:return
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.path.with_name(self.path.name+'.part')
            temporary.write_text(json.dumps(dict(self._snapshot(),events=self.events),
                                            ensure_ascii=False,separators=(',',':')),encoding='utf-8')
            temporary.replace(self.path)
            self._last_progress_save_s=now
        except OSError:
            pass

    def __exit__(self, kind, value, tb):
        self.stop.set()
        if self.worker is not None:self.worker.join(timeout=1.)
        status = 'completed' if kind is None else 'interrupted' if issubclass(kind,KeyboardInterrupt) else 'failed'
        self.events.append(self._snapshot())
        self.state = dict(self.state,status=status,error='' if value is None else str(value))
        if kind is None:self.state['stage']='分割及保存完成'
        self._save();self._render()
        print('%s掩膜处理%s，总计 %.1f 秒。' % (self.backend,
            '完成' if kind is None else '已中断' if status=='interrupted' else '失败',
            time.monotonic()-self.started),flush=True)


def _segment_color_frames(frames, empty565, ground_row, reference_sigma=None,
                          diagnostic_dir=None, progress=None, label='照片'):
    """同一自适应入口用于预览、整圈分割和旧数据重算。"""
    # Notebook step 7 reloads this module. Reload the companion too, so replacing
    # these two files and rerunning step 7 does not keep the old boundary check.
    import importlib, adaptive_scan
    adaptive_scan = importlib.reload(adaptive_scan)
    segment_frame = adaptive_scan.segment_frame
    started = time.monotonic()
    reference = rgb565_to_rgb(empty565)
    masks, highlights, thresholds, strongs, unknowns, diagnostics = [], [], [], [], [], []
    for i, frame in enumerate(frames):
        frame_started = time.monotonic()
        if progress is not None:progress.update('RGB解码','%s %d/%d' % (label,i+1,len(frames)))
        try:
            rgb = rgb565_to_rgb(frame)
            body, strong, unknown, info = segment_frame(
                rgb, reference, ground_row, roi=(160, 0, 640, 480),
                reference_sigma=reference_sigma,
                progress=progress.update if progress is not None else None)
        except ValueError as exc:
            details = ''
            if diagnostic_dir is not None:
                try:
                    target = Path(diagnostic_dir)
                    target.mkdir(parents=True,exist_ok=True)
                    name = 'segmentation_failed_%02d' % (i+1)
                    view = Image.fromarray(rgb565_to_rgb(frame))
                    draw = ImageDraw.Draw(view)
                    draw.rectangle((160,0,639,479),outline=(255,255,0))
                    draw.line((0,int(ground_row),799,int(ground_row)),fill=(0,255,255),width=2)
                    info = dict(getattr(exc,'diagnostics',{}))
                    info.update(frame_number=i+1,error=str(exc))
                    for region in info.get('boundary_blocking_components',[]):
                        box = region['bbox_xyxy']
                        draw.rectangle((box[0],box[1],box[2]-1,box[3]-1),outline=(255,0,0))
                    png = target/(name+'.png')
                    view.save(str(png))
                    with (target/(name+'.json')).open('w',encoding='utf-8') as stream:
                        json.dump(info,stream,ensure_ascii=False,indent=2)
                    evidence = dict(frame565=np.asarray(frame),empty565=np.asarray(empty565),
                                    ground_row=float(ground_row),roi=np.asarray([160,0,640,480]),
                                    segmentation_version=COLOR_SEGMENTATION_VERSION,
                                    diagnostic_schema_version=2)
                    if hasattr(exc,'foreground'):
                        evidence.update(foreground=exc.foreground,strong=exc.strong)
                    if reference_sigma is not None:evidence['reference_sigma']=reference_sigma
                    _atomic_npz(target/(name+'.npz'),**evidence)
                    details = '；定位图：%s' % png
                except Exception as diagnostic_error:
                    details = '；定位图保存失败：%s' % diagnostic_error
            raise RuntimeError("第 %d 张彩色原图分割失败：%s%s" % (i + 1, exc, details)) from exc
        info['segmentation_elapsed_s'] = time.monotonic()-frame_started
        masks.append(body)
        strongs.append(strong)
        unknowns.append(unknown)
        diagnostics.append(info)
        highlights.append((rgb.min(axis=2) >= 220) & (rgb.max(axis=2) >= 240))
        thresholds.append(info['high_threshold'])
    return {"masks": np.stack(masks).astype(np.uint8),
            "photo_strong_masks": np.stack(strongs).astype(np.uint8),
            "photo_unknown_masks": np.stack(unknowns).astype(np.uint8),
            "photo_highlight_masks": np.stack(highlights).astype(np.uint8),
            "photo_thresholds": np.asarray(thresholds, dtype=np.float32),
            "photo_threshold_units": "standardized_background_residual",
            "segmentation_diagnostics_json": json.dumps(diagnostics, ensure_ascii=False),
            "segmentation_version": COLOR_SEGMENTATION_VERSION,
            "segmentation_implementation_version": adaptive_scan.IMPLEMENTATION_VERSION,
            "segmentation_elapsed_s": time.monotonic()-started,
            "segmentation_algorithm": COLOR_SEGMENTATION_ALGORITHM}


def segment_color_scan(color_scan, empty565, ground_row, out_path=None,
                       preview_path=None, reference_sigma=None):
    """Segment saved photos, displaying a single status area in notebooks."""
    directory = (Path(out_path).parent if out_path is not None else
                 Path(preview_path).parent if preview_path is not None else None)
    with _SegmentationProgress(directory) as progress:
        return _segment_color_scan_impl(color_scan,empty565,ground_row,out_path,
                                        preview_path,reference_sigma,progress)


def _closure_diagnostic(frames, closing_frame, masks, closing, angles, directory):
    first = masks[0].astype(bool)
    final = closing.astype(bool)
    overlaps = []
    for mask in masks.astype(bool):
        union = np.count_nonzero(mask|final)
        overlaps.append(float(np.count_nonzero(mask&final)/union) if union else 0.)
    best = int(np.argmax(overlaps))
    result = {'closure_iou':overlaps[0],
              'mask_iou_with_closing_per_frame':overlaps,
              'closest_mask_frame_number':best+1,
              'closest_mask_nominal_angle_deg':float(angles[best]),
              'first_only_pixels':int(np.count_nonzero(first&~final)),
              'closing_only_pixels':int(np.count_nonzero(final&~first)),
              'union_pixels':int(np.count_nonzero(first|final)),
              'cause':'undetermined_requires_same_run_photo_review',
              'note':'轮廓相似不等于真实转角；不自动调角度或修改首尾一致性阈值'}
    if directory is None:return result
    directory = Path(directory);directory.mkdir(parents=True,exist_ok=True)
    view = Image.new('RGB',(960,344),'white')
    difference = np.full((HEIGHT,WIDTH,3),240,dtype=np.uint8)
    difference[first&final]=(60,60,60)
    difference[first&~final]=(230,65,65)
    difference[final&~first]=(45,100,230)
    panels = [rgb565_to_rgb(frames[0]),rgb565_to_rgb(closing_frame),difference]
    labels = ['First photo','Closing photo','Masks: red=first only, blue=closing only']
    draw = ImageDraw.Draw(view)
    for i,(panel,label) in enumerate(zip(panels,labels)):
        crop = Image.fromarray(panel).crop((160,0,640,480)).resize((320,320),Image.NEAREST)
        view.paste(crop,(i*320,24));draw.text((i*320+3,5),label,fill='black')
    view.save(str(directory/'closure_comparison.png'))
    (directory/'closure_diagnostic.json').write_text(
        json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


def _segment_color_scan_impl(color_scan, empty565, ground_row, out_path=None,
                             preview_path=None, reference_sigma=None, progress=None):
    """从同圈彩照生成掩膜，同时保存空台原图供第 8 步局部核验。"""
    frames = np.asarray(color_scan["frames565"])
    angles = np.asarray(color_scan["angles_deg"], dtype=np.float64)
    if frames.ndim != 3 or frames.shape[1:] != (HEIGHT, WIDTH) or \
            frames.dtype != np.uint16 or len(frames) != len(angles):
        raise ValueError("彩色原图或角度数量不正确")
    diagnostics = (Path(out_path).parent if out_path is not None else
                   Path(preview_path).parent if preview_path is not None else None)
    segmented = _segment_color_frames(frames, empty565, ground_row, reference_sigma,
                                      diagnostic_dir=diagnostics,progress=progress)
    closing = _segment_color_frames([color_scan["closing_frame565"]], empty565,
                                    ground_row, reference_sigma,
                                    diagnostic_dir=(diagnostics/'closing' if diagnostics is not None else None),
                                    progress=progress,label='首尾核验帧')['masks'][0].astype(bool)
    first = segmented["masks"][0].astype(bool)
    union = np.count_nonzero(first | closing)
    closure_iou = float(np.count_nonzero(first & closing) / union) if union else 0.0
    if progress is not None:progress.update('首尾一致性检查',frame='')
    closure = _closure_diagnostic(frames,color_scan['closing_frame565'],
                                  segmented['masks'],closing,angles,diagnostics)
    result = dict(segmented, angles_deg=angles,
                  empty_color_frame565=np.asarray(empty565, dtype=np.uint16).copy(),
                  closing_color_frame565=np.asarray(
                      color_scan["closing_frame565"], dtype=np.uint16).copy(),
                  closure_iou=closure_iou, source="color_photos")
    result['closure_diagnostic_json'] = json.dumps(closure,ensure_ascii=False)
    result['color_frames_sha256'] = hashlib.sha256(
        frames.astype('<u2',copy=False).tobytes()).hexdigest()
    result['empty_frame_sha256'] = hashlib.sha256(
        np.asarray(empty565).astype('<u2',copy=False).tobytes()).hexdigest()
    if reference_sigma is not None:
        result['background_noise_sigma'] = np.asarray(reference_sigma, dtype=np.float32)
    if out_path is not None:
        if progress is not None:progress.update('保存掩膜及诊断数据')
        _atomic_npz(out_path, **result)
    if preview_path is not None:
        if progress is not None:progress.update('生成掩膜预览')
        save_mask_sheet(result["masks"], preview_path)
    print("彩色原图分割后的首尾 IoU=%.3f" % closure_iou,flush=True)
    if closure_iou < .8 and diagnostics is not None:
        print('首尾差异需核对；对照图：%s' % (diagnostics/'closure_comparison.png'),flush=True)
        try:
            from IPython import get_ipython
            from IPython.display import display
            if get_ipython() is not None:display(Image.open(str(diagnostics/'closure_comparison.png')))
        except Exception:
            pass
    return result


def suggest_voxel_mm(masks, axis_col, v_ground, mm_per_px, grid_n=128):
    """根据物体轮廓自动选能装下全部形状的体素边长（留 5% 余量）。"""
    if not np.isfinite(mm_per_px) or mm_per_px <= 0 or grid_n < 8:
        raise ValueError("标定比例或体素网格无效")
    arr = np.asarray(masks, dtype=bool)
    if arr.ndim != 3 or not arr.any():
        raise ValueError("需要非空的多视角掩膜")
    _, ys, xs = np.nonzero(arr)
    radius_mm = max(abs(xs.min() - axis_col), abs(xs.max() - axis_col)) * mm_per_px
    height_mm = (v_ground - ys.min()) * mm_per_px
    if height_mm <= 0:
        raise ValueError("台面行必须在物体顶部下方")
    return 1.05 * max(2.0 * radius_mm / (grid_n - 2),
                      height_mm / (grid_n - 2))


def occupancy_dimensions_mm(occ, voxel_mm):
    """给出占据体素沿坐标轴的外接包围范围与 PLY 点中心跨度。"""
    a = np.asarray(occ, dtype=bool)
    if a.ndim != 3 or not a.any() or not np.isfinite(voxel_mm) or voxel_mm <= 0:
        raise ValueError("需要非空三维占据数组和正的体素边长")
    coords = np.nonzero(a)
    spans = [int(axis.max() - axis.min()) for axis in coords]
    bottom_z = int(coords[0].min())
    top_z = int(coords[0].max())
    return {
        "voxel_extent_mm": {key: float((span + 1) * voxel_mm)
                            for key, span in zip(("z", "y", "x"), spans)},
        "point_center_span_mm": {key: float(span * voxel_mm)
                                 for key, span in zip(("z", "y", "x"), spans)},
        "bottom_gap_mm": float(bottom_z * voxel_mm),
        "table_to_top_mm": float((top_z + 1) * voxel_mm),
    }


def _turntable_camera_module():
    import importlib,turntable_camera
    if Path(turntable_camera.__file__).resolve().parent!=Path(__file__).resolve().parent:
        raise RuntimeError('turntable_camera.py 与 scan_pipeline.py 必须在同一目录')
    return importlib.reload(turntable_camera)


def reconstruct_full_resolution_silhouette(masks, angles_deg, calibration,
                                           coarse_voxel_mm, grid_n=256,
                                           coarse_grid_n=128,
                                           outer_margin_full_px=0,
                                           coarse_repair_additions=None):
    """在 PS 上用原始 480 像素掩膜细化外轮廓，不冒充 PL 结果。

    与 PL 使用同一立方体尺寸、转轴和角度，并同步其已确认的掩膜修补。
    PL 若对降采样掩膜外轮廓留余量，PS 只在原分辨率每行的最外侧
    留对应余量，不扩大内部孔洞。
    不从彩色纹理猜测凹陷，也不把未见的面补成真实细节。
    """
    from ps_demo import plan_geometry

    src = np.asarray(masks, dtype=bool)
    angles = np.asarray(angles_deg, dtype=np.float64)
    if src.ndim != 3 or src.shape[1:] != (HEIGHT, WIDTH) or \
            len(src) != len(angles) or not src.any(axis=(1, 2)).all():
        raise ValueError("细化需要与角度对应的非空 480x800 掩膜")
    if not isinstance(grid_n, int) or not 8 <= grid_n <= 256 or \
            not isinstance(coarse_grid_n, int) or coarse_grid_n <= 0:
        raise ValueError("细化网格须为 8～256，PL 网格边长必须为正整数")
    if not np.isfinite(coarse_voxel_mm) or coarse_voxel_mm <= 0:
        raise ValueError("PL 体素边长无效")
    if not isinstance(outer_margin_full_px, (int, np.integer)) or \
            not 0 <= outer_margin_full_px <= 8:
        raise ValueError("原图外轮廓余量必须是 0～8 个整数像素")
    if coarse_repair_additions is not None:
        additions = np.asarray(coarse_repair_additions, dtype=bool)
        if additions.shape != (len(src), coarse_grid_n, coarse_grid_n):
            raise ValueError("PL 掩膜修补与原图视角或降采样尺寸不一致")
    else:
        additions = None
    x0,y0,x1,y1=map(int,calibration.get('reconstruction_roi_xyxy',(160,0,640,480)))
    if (src[:, :, :x0].any() or src[:, :, x1:].any() or
            src[:, 0, x0:x1].any() or src[:, -1, x0:x1].any()):
        raise ValueError("原始掩膜超出细化区域，不能裁切物体")
    cropped = src[:, y0:y1, x0:x1].copy()
    if additions is not None and additions.any():
        side=max(x1-x0,y1-y0)
        sx=np.floor((np.arange(x1-x0)+.5)*coarse_grid_n/side).astype(np.intp)
        sy=np.floor((np.arange(y1-y0)+.5)*coarse_grid_n/side).astype(np.intp)
        cropped |= additions[:,sy[:,None],sx[None,:]]
    if outer_margin_full_px:
        # 只扩每行最外边界；逐行处理可以保留中间真实空隙。
        for view in cropped:
            for row in view:
                cols = np.flatnonzero(row)
                if len(cols):
                    row[max(0, int(cols[0]) - outer_margin_full_px):int(cols[0])] = True
                    row[int(cols[-1]) + 1:min(len(row), int(cols[-1]) + 1 +
                                                outer_margin_full_px)] = True
    if (cropped[:, 0, :].any() or cropped[:, -1, :].any() or
            cropped[:, :, 0].any() or cropped[:, :, -1].any()):
        raise ValueError("细化掩膜贴着 480 像素 ROI 边缘，不能静默裁切模型")
    voxel_mm = float(coarse_voxel_mm) * coarse_grid_n / grid_n
    if 'turntable_camera' in calibration:
        if outer_margin_full_px or (additions is not None and additions.any()):
            raise ValueError('透视对照不兼容扩边或修改 PL 掩膜')
        camera=_turntable_camera_module()
        return camera.carve(src,angles,calibration['turntable_camera'],grid_n,voxel_mm),voxel_mm
    kv, ofv, kux, kuy, ofu = plan_geometry(
        angles, grid_n, x1 - x0, y1 - y0, voxel_mm,
        float(calibration["mm_per_px"]),
        float(calibration["axis_col"]) - x0,
        float(calibration["v_ground"]) - y0)
    x = np.arange(grid_n, dtype=np.int64)[None, :]
    y = np.arange(grid_n, dtype=np.int64)[:, None]
    occ = np.ones((grid_n, grid_n, grid_n), dtype=bool)
    # Same Q16 samples as the scalar-z loop. Planes mapping to the same camera
    # row share its projected mask. Empty contiguous groups need no more work.
    vv=(int(ofv)-np.arange(grid_n,dtype=np.int64)*int(kv))>>16
    valid_z=(vv>=0)&(vv<y1-y0)
    occ[~valid_z]=False
    changes=np.flatnonzero(np.r_[True,np.diff(vv)!=0,True])
    row_groups=[(int(a),int(b),int(vv[a])) for a,b in zip(changes[:-1],changes[1:]) if valid_z[a]]
    for i, view in enumerate(cropped):
        u = (int(ofu[i]) + x * int(kux[i]) + y * int(kuy[i])) >> 16
        valid_u = (u >= 0) & (u < x1 - x0)
        safe_u = np.clip(u, 0, x1 - x0 - 1)
        for start,stop,v in row_groups:
            block=occ[start:stop]
            if block.any():
                block &= valid_u&view[v,safe_u]
    return occ, voxel_mm


def repair_full_resolution_from_photos(masks, pl_occ, voxel_mm, angles_deg,
                                       calibration, frames565, empty565,
                                       photo_thresholds):
    """用同圈照片修复原分辨率的局部漏判，保留照片显示为背景的孔。

    PL 占据投影只限定待检查像素；是否补回仍由彩照与空台、周边
    已识别物体颜色共同决定。不会凭 PL 外壳把整行内部填实。
    """
    from ps_demo import plan_geometry

    src = np.asarray(masks, dtype=bool)
    occ = np.asarray(pl_occ, dtype=bool)
    frames = np.asarray(frames565)
    empty = np.asarray(empty565)
    angles = np.asarray(angles_deg, dtype=np.float64)
    thresholds = np.asarray(photo_thresholds, dtype=np.float32)
    if (src.ndim != 3 or src.shape[1:] != (HEIGHT, WIDTH) or
            frames.shape != src.shape or frames.dtype != np.uint16 or
            empty.shape != (HEIGHT, WIDTH) or empty.dtype != np.uint16 or
            occ.ndim != 3 or not occ.any() or len(angles) != len(src) or
            thresholds.shape != (len(src),)):
        raise ValueError("全分辨率彩照、掩膜、PL 占据或角度不匹配")
    if not np.isfinite(thresholds).all() or (thresholds <= 0).any():
        raise ValueError("彩照差分阈值无效")
    mm_per_px = float(calibration["mm_per_px"])
    axis_col = float(calibration["axis_col"])
    v_ground = float(calibration["v_ground"])
    if not np.isfinite([voxel_mm, mm_per_px, axis_col, v_ground]).all() or \
            voxel_mm <= 0 or mm_per_px <= 0:
        raise ValueError("PL 占据投影的几何参数无效")
    kv, ofv, kux, kuy, ofu = plan_geometry(
        angles, occ.shape[0], WIDTH, HEIGHT, voxel_mm,
        mm_per_px, axis_col, v_ground)
    zz, yy, xx = np.nonzero(occ)
    zz, yy, xx = zz.astype(np.int64), yy.astype(np.int64), xx.astype(np.int64)
    v = (int(ofv) - zz * int(kv)) >> 16
    empty_rgb = rgb565_to_rgb(empty).astype(np.int16)
    repaired = src.copy()
    accepted = np.zeros(src.shape, dtype=bool)
    counts = []
    columns = np.arange(WIDTH)[None, :]
    for i, view in enumerate(src):
        u = (int(ofu[i]) + xx * int(kux[i]) +
             yy * int(kuy[i])) >> 16
        valid = (u >= 0) & (u < WIDTH) & (v >= 0) & (v < HEIGHT)
        projected = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
        projected[v[valid], u[valid]] = 255
        projected = np.asarray(Image.fromarray(projected).filter(
            ImageFilter.MaxFilter(5)), dtype=np.uint8) != 0
        occupied_rows = view.any(axis=1)
        left = np.where(occupied_rows, np.argmax(view, axis=1), WIDTH)
        right = np.where(occupied_rows,
                         WIDTH - 1 - np.argmax(view[:, ::-1], axis=1), -1)
        candidates = (projected & ~view &
                      (columns >= left[:, None]) &
                      (columns <= right[:, None]))
        ys, xs = np.nonzero(candidates)
        frame_rgb = rgb565_to_rgb(frames[i]).astype(np.int16)
        shift = np.median((frame_rgb[:40] - empty_rgb[:40]).reshape(-1, 3),
                          axis=0)
        count = 0
        for y, x in zip(ys, xs):
            corrected = np.clip(frame_rgb[y, x].astype(np.float32) - shift,
                                0, 255)
            background_distance = np.sqrt(np.sum(
                (corrected - empty_rgb[y, x]) ** 2))
            if background_distance <= max(12.0, float(thresholds[i])):
                continue
            y0, y1 = max(0, y - 3), min(HEIGHT, y + 4)
            x0, x1 = max(0, x - 3), min(WIDTH, x + 4)
            nearby = frame_rgb[y0:y1, x0:x1][
                view[y0:y1, x0:x1]].astype(np.float32)
            if len(nearby) == 0:
                continue
            neighbor_distance = np.sqrt(np.sum(
                (nearby - frame_rgb[y, x].astype(np.float32)) ** 2,
                axis=1)).min()
            if background_distance > 2.0 * neighbor_distance:
                accepted[i, y, x] = True
                repaired[i, y, x] = True
                count += 1
        counts.append(count)
    return repaired, accepted, counts


def _surface_mask(occ):
    """仅保留六邻域中至少有一面朝向空气的体素。"""
    src = np.asarray(occ, dtype=bool)
    if src.ndim != 3:
        raise ValueError("占据数组必须为 (z,y,x)")
    inside = src.copy()
    for axis in range(3):
        lo = [slice(None)] * 3
        hi = [slice(None)] * 3
        lo[axis] = slice(0, -1)
        hi[axis] = slice(1, None)
        inside[tuple(lo)] &= src[tuple(hi)]
        inside[tuple(hi)] &= src[tuple(lo)]
        # Outside the finite grid is air. Boundary cells cannot be interior
        # merely because their in-grid neighbors are occupied.
        first=[slice(None)]*3;last=[slice(None)]*3
        first[axis]=0;last[axis]=-1
        inside[tuple(first)]=False;inside[tuple(last)]=False
    return src & ~inside





SURFACE_COLOR_FIX_VERSION = 32
MODEL_EXPORT_COORDINATE_VERSION = 33
PHOTO_GEOMETRY_AUDIT_VERSION = 43


def opposite_silhouette_consistency(masks, angles_deg, axis_col):
    """Diagnostic of the orthographic assumption; never edits input views.

    Opposite orthographic silhouettes should mirror about the calibrated axis.
    Differences alone cannot identify segmentation, angle or perspective error.
    The review threshold is empirical, not a physical accuracy certificate.
    """
    src=np.asarray(masks,dtype=bool)
    angles=np.asarray(angles_deg,dtype=float)
    if src.ndim!=3 or angles.ndim!=1 or len(src)!=len(angles) or \
            not np.isfinite(angles).all() or not np.isfinite(axis_col):
        raise ValueError('相反视角诊断输入无效')
    result={'assumption':'orthographic_fixed_axis', 'pairs':[],
            'empirical_review_iou':.9,'median_iou':None,'requires_review':False,
            'inputs_modified':False,'cause_identified':False}
    if len(angles)<2:
        return result
    phase=np.mod(angles,360.)
    intervals=np.diff(np.r_[np.sort(phase),np.sort(phase)[0]+360.])
    positive=intervals[intervals>1e-6]
    tolerance=max(.05,min(2.,float(np.median(positive))*.1)) if len(positive) else .05
    cols=np.floor(2.*float(axis_col)-np.arange(src.shape[2])+.5).astype(np.intp)
    valid=(cols>=0)&(cols<src.shape[2])
    pairs=[]
    for i,angle in enumerate(phase):
        errors=np.abs((phase-(angle+180.)+180.)%360.-180.)
        j=int(np.argmin(errors))
        if j<=i or errors[j]>tolerance:
            continue
        reflected=np.zeros_like(src[j])
        reflected[:,valid]=src[j][:,cols[valid]]
        union=int(np.count_nonzero(src[i]|reflected))
        if not union:
            continue
        iou=float(np.count_nonzero(src[i]&reflected))/union
        pairs.append({'views':[i,j],'angular_mismatch_deg':float(errors[j]),'iou':iou})
    result['pairs']=pairs
    if pairs:
        result['median_iou']=float(np.median([p['iou'] for p in pairs]))
        result['requires_review']=result['median_iou']<result['empirical_review_iou']
    return result


def _white_turntable_color_reference(reference565, calibration):
    """Bounded common color gain from this project's known white table.

    The object does not determine neutrality; captured frames remain unchanged.
    This is approximate reference balancing, not full physical color calibration.
    """
    info={'applied':False,'source':'known_white_turntable_reference',
          'implementation_version':SURFACE_COLOR_FIX_VERSION,
          'raw_frames_modified':False,'physical_color_accuracy_verified':False}
    if reference565 is None:
        info['reason']='no_white_turntable_reference'
        return info
    required=('left_px','right_px','width_px','v_ground')
    if any(k not in calibration for k in required):
        info['reason']='missing_calibrated_turntable_extent'
        return info
    values=np.asarray([calibration[k] for k in required],dtype=float)
    if not np.isfinite(values).all() or values[2]<=0:
        info['reason']='invalid_reference_extent'
        return info
    left,right,width,ground=values
    x0=max(0,int(left+.15*width));x1=min(WIDTH,int(right-.15*width))
    upper=float(calibration.get('upper_row_end',ground+.04*width))
    if not np.isfinite(upper):
        info['reason']='invalid_reference_row'
        return info
    y0=max(0,int(upper)+2)
    y1=min(HEIGHT,y0+max(8,min(32,int(width*.08))))
    info['reference_roi_xyxy']=[x0,y0,x1,y1]
    if x0>=x1 or y0>=y1:
        info['reason']='reference_roi_outside_image'
        return info
    ref=rgb565_to_rgb(reference565)
    samples=ref[y0:y1,x0:x1].reshape(-1,3)
    accepted=(samples.min(axis=1)>128)&(samples.max(axis=1)<250)&(np.ptp(samples,axis=1)<64)
    info['accepted_reference_samples']=int(accepted.sum())
    if accepted.sum()<256:
        info['reason']='insufficient_unclipped_white_reference'
        return info
    neutral=np.median(samples[accepted],axis=0).astype(float)
    gains=float(neutral.mean())/neutral
    info.update(reference_median_RGB=neutral.tolist(),channel_gains=gains.tolist(),
                approximate_neutral_target=float(neutral.mean()))
    if not np.all((gains>=.8)&(gains<=1.25)):
        info['reason']='reference_cast_outside_bounded_correction_range'
        return info
    info.update(applied=True,reason='bounded_common_gain_from_known_white_reference')
    return info


def colorize_surface_from_views(occ, voxel_mm, frames565, masks,
                                angles_deg, calibration, return_valid=False,
                                reference565=None, return_diagnostics=False,
                                return_faces=False, depth_direction_selection=None):
    """逐外露面核验照片方向与遮挡；点云颜色只是逐面结果的代表值。"""
    frames=np.asarray(frames565);silhouettes=np.asarray(masks,dtype=bool)
    angles=np.asarray(angles_deg,dtype=np.float64)
    if (frames.ndim!=3 or frames.shape[1:]!=(HEIGHT,WIDTH) or
            frames.dtype!=np.uint16 or frames.shape!=silhouettes.shape or len(angles)!=len(frames)):
        raise ValueError("彩色原图、同角度掩膜或角度数量不匹配")
    if return_faces and not return_diagnostics:
        raise ValueError("逐面数据必须同时请求取色诊断")
    import importlib, surface_photo
    if Path(surface_photo.__file__).resolve().parent!=Path(__file__).resolve().parent:
        raise RuntimeError("surface_photo.py 与 scan_pipeline.py 必须在同一目录")
    surface_photo=importlib.reload(surface_photo)
    balance=_white_turntable_color_reference(reference565,calibration)
    gains=np.asarray(balance['channel_gains']) if balance['applied'] else None
    if 'turntable_camera' in calibration:
        camera=_turntable_camera_module()
        data=camera.colorize_faces(occ,voxel_mm,frames,silhouettes,angles,calibration['turntable_camera'],gains)
    elif depth_direction_selection is None:
        data=surface_photo.colorize_faces_auto(occ,voxel_mm,frames,silhouettes,angles,calibration,gains)
    else:
        if not depth_direction_selection.get('confirmed') or depth_direction_selection.get('selected_sign') not in (-1,1):
            raise ValueError('重用观察方向时必须提供已完成照片复核的选择')
        data=surface_photo.colorize_faces(occ,voxel_mm,frames,silhouettes,angles,calibration,gains,
            camera_depth_sign=int(depth_direction_selection['selected_sign']))
        data['diagnostics']['depth_direction_selection']=dict(depth_direction_selection)
        data['diagnostics']['depth_direction_reused_for_same_capture']=True
    rgb=data['point_colors'];observed=data['point_valid'];count=int(observed.sum())
    diagnostics=data['diagnostics']
    diagnostics.update(surface_points=len(observed),observed_points=count,white_balance=balance,
                       raw_frames_modified=False,occlusion_test_retained=True,
                       unobserved_points_stay_gray=True)
    if return_diagnostics:
        result=(rgb,count,observed,diagnostics)
        return result+(data,) if return_faces else result
    if return_valid:return rgb,count,observed
    return rgb,count


def model_export_y_sign(color_diagnostics):
    """Keep image-right, image-up and the selected camera side right-handed.

    A negative depth-side choice with unchanged projection coordinates is a
    reflected camera. Reflect model Y as well, and reverse triangle winding in
    the mesh exporter. Volume and all per-vertex color associations are kept.
    """
    sign=int((color_diagnostics or {}).get('camera_depth_sign',1))
    if sign not in (-1,1):
        raise ValueError('取色观察方向必须为 -1 或 1')
    return sign


def save_surface_ply(path, occ, voxel_mm, colors=None, export_y_sign=1):
    """Atomic binary point cloud; bounded buffers, identical grid/point/RGB order."""
    surface = _surface_mask(occ)
    zz, yy, xx = np.nonzero(surface)
    if len(zz) == 0:
        raise ValueError("细化结果为空；请检查掩膜、转轴和角度")
    if export_y_sign not in (-1,1):
        raise ValueError('模型导出Y方向必须为 -1 或 1')
    if colors is not None:
        colors = np.asarray(colors)
        if colors.shape != (len(zz), 3) or colors.dtype != np.uint8:
            raise ValueError("表面颜色必须为每个点对应的 uint8 RGB")
    if not np.isfinite(voxel_mm) or voxel_mm <= 0:
        raise ValueError('体素边长必须为正的有限数')
    path = Path(path)
    temporary = path.with_name(path.name + ".partial")
    half = (occ.shape[2] - 1) / 2.0
    dtype=[('x','<f4'),('y','<f4'),('z','<f4')]
    if colors is not None:dtype += [('red','u1'),('green','u1'),('blue','u1')]
    header=['ply','format binary_little_endian 1.0',
        'comment point_export_version 56','element vertex %d' % len(zz),
        'property float x','property float y','property float z']
    if colors is not None:header += ['property uchar red','property uchar green','property uchar blue']
    header += ['end_header']
    # A record buffer is <=983040 bytes, including color. Avoid one Python
    # iteration/string formatting call per point on the board's ARM CPU.
    with open(str(temporary), 'wb') as stream:
        stream.write(('\n'.join(header)+'\n').encode('ascii'))
        for start in range(0,len(zz),65536):
            end=min(start+65536,len(zz));v=np.empty(end-start,dtype=dtype)
            v['x']=(xx[start:end]-half)*voxel_mm
            v['y']=(yy[start:end]-half)*voxel_mm*export_y_sign
            v['z']=zz[start:end]*voxel_mm
            if colors is not None:
                v['red']=colors[start:end,0];v['green']=colors[start:end,1];v['blue']=colors[start:end,2]
            stream.write(v.tobytes())
    os.replace(str(temporary), str(path))
    return len(zz)


def save_surface_mesh_ply(path, occ, voxel_mm, colors=None,
                          color_valid=None, preserve_color_boundaries=False,
                          face_colors=None, face_valid=None, metadata=None,
                          export_y_sign=1, uncertain_voxels=None,
                          highlight_uncertainty=False, uniform_voxel_color=False):
    """把体素外表面写成带三角面的 PLY，可在 MeshLab 旋转查看。

    顶点按格点去重。传入逐面颜色时，每个外露面单独使用已确认的照片颜色，
    默认逐面保存照片属性；uniform_voxel_color启用时，同体素各面使用
    该体素已有的代表照片颜色。该显示颜色复用不代表测得不可见面的颜色，
    没有任何有效照片颜色的体素仍保持灰色，不向相邻体素借色。
    preserve_color_boundaries 可保留颜色接缝。体素仅沿边/角接触时可能出现非流形边；
    此网格只用于外观显示，不能当作已恢复的真实凹陷或可打印模型。
    待定标记默认不改照片颜色；仅独立诊断显式指定highlight_uncertainty时涂橙色。
    """
    src = np.asarray(occ, dtype=bool)
    if src.ndim != 3 or not src.any() or not np.isfinite(voxel_mm) or voxel_mm <= 0:
        raise ValueError("网格需要非空三维占据和正的采样间距")
    if export_y_sign not in (-1,1):
        raise ValueError('模型导出Y方向必须为 -1 或 1')
    surface = _surface_mask(src)
    if uniform_voxel_color:
        if colors is None:
            raise ValueError('统一体素颜色需要已有表面颜色')
        # Keep adjacent materials separate; never average their colors.
        preserve_color_boundaries = True
    if uncertain_voxels is not None:
        uncertain_voxels = np.asarray(uncertain_voxels, dtype=bool)
        if uncertain_voxels.shape != src.shape or (uncertain_voxels & ~src).any():
            raise ValueError('待定显示标记与模型网格不一致')
        if highlight_uncertainty and not preserve_color_boundaries:
            raise ValueError('待定显示需要保留诊断颜色边界')
        if highlight_uncertainty and colors is None:
            colors = np.full((int(surface.sum()), 3), 180, np.uint8)
            color_valid = np.zeros(len(colors), dtype=bool)
    if colors is not None:
        colors = np.asarray(colors)
        if colors.shape != (int(surface.sum()), 3) or colors.dtype != np.uint8:
            raise ValueError("网格颜色与表面体素数量不匹配")
        if color_valid is None:
            color_valid = np.ones(len(colors), dtype=bool)
        else:
            color_valid = np.asarray(color_valid, dtype=bool)
            if color_valid.shape != (len(colors),):
                raise ValueError("已确认颜色标记与表面体素数量不匹配")
        surface_flat = np.flatnonzero(surface)
    elif color_valid is not None:
        raise ValueError("无颜色时不能单独传入已确认颜色标记")
    if face_colors is not None:
        if colors is None or face_valid is None or len(face_colors)!=6 or len(face_valid)!=6:
            raise ValueError('逐面颜色须包含六个方向的颜色及有效标记')
    elif face_valid is not None:
        raise ValueError('逐面有效标记缺少对应颜色')
    directions = (
        (2, 1, (1, 0, 0), (0, 1, 0), (0, 0, 1)),
        (2, -1, (-1, 0, 0), (0, 0, 1), (0, 1, 0)),
        (1, 1, (0, 1, 0), (0, 0, 1), (1, 0, 0)),
        (1, -1, (0, -1, 0), (1, 0, 0), (0, 0, 1)),
        (0, 1, (0, 0, 1), (1, 0, 0), (0, 1, 0)),
        (0, -1, (0, 0, -1), (0, 1, 0), (1, 0, 0)),
    )
    corners = []
    corner_colors = []
    corner_valid = []
    for direction, (axis, sign, normal, tangent_u, tangent_v) in enumerate(directions):
        exposed = src.copy()
        current = [slice(None)] * 3
        neighbor = [slice(None)] * 3
        if sign > 0:
            current[axis], neighbor[axis] = slice(0, -1), slice(1, None)
        else:
            current[axis], neighbor[axis] = slice(1, None), slice(0, -1)
        exposed[tuple(current)] &= ~src[tuple(neighbor)]
        z, y, x = np.nonzero(exposed)
        if not len(z):
            continue
        base = np.stack((x, y, z), axis=1).astype(np.int16)
        normal = np.asarray(normal, dtype=np.int16)
        tangent_u = np.asarray(tangent_u, dtype=np.int16)
        tangent_v = np.asarray(tangent_v, dtype=np.int16)
        combinations = ((-1, -1), (1, -1), (1, 1), (-1, 1))
        four = np.empty((len(z), 4, 3), dtype=np.int16)
        for j, (cu, cv) in enumerate(combinations):
            four[:, j, :] = base + (1 + normal + cu * tangent_u +
                                    cv * tangent_v) // 2
        corners.append(four.reshape(-1, 3))
        if colors is not None:
            if face_colors is not None:
                fc=np.asarray(face_colors[direction]);fv=np.asarray(face_valid[direction],dtype=bool)
                if fc.dtype!=np.uint8 or fc.shape!=(len(z),3) or fv.shape!=(len(z),):
                    raise ValueError('逐面颜色与实际外露面数量不匹配')
            if face_colors is not None and not uniform_voxel_color:
                display_colors, display_valid = fc, fv
            else:
                flat = np.ravel_multi_index((z, y, x), src.shape)
                indices = np.searchsorted(surface_flat, flat)
                display_colors, display_valid = colors[indices], color_valid[indices]
            if uncertain_voxels is not None and highlight_uncertainty:
                # This is an explicit diagnostic colour. Never change the
                # caller's measured/photo-valid arrays or observation counts.
                ambiguous = uncertain_voxels[z, y, x]
                display_colors = np.array(display_colors, copy=True)
                display_valid = np.array(display_valid, copy=True)
                display_colors[ambiguous] = (245, 160, 20)
                display_valid[ambiguous] = True
            corner_colors.append(np.repeat(display_colors, 4, axis=0))
            corner_valid.append(np.repeat(display_valid, 4))
    all_corners = np.concatenate(corners)
    seam_colors = None
    if colors is not None and preserve_color_boundaries:
        sampled = np.concatenate(corner_colors).copy()
        sampled[~np.concatenate(corner_valid).astype(bool)] = 180
        # PLY vertex color interpolates over triangles. Share a vertex only when
        # its geometry AND observed color agree; no cross-material RGB average.
        # Coordinates/faces remain identical; only attribute seams gain indices.
        attributes = np.concatenate((all_corners, sampled.astype(np.int16)), axis=1)
        unique, inverse = np.unique(attributes, axis=0, return_inverse=True)
        lattice, seam_colors = unique[:, :3], unique[:, 3:].astype(np.uint8)
    else:
        lattice, inverse = np.unique(all_corners, axis=0, return_inverse=True)
    quads = inverse.reshape(-1, 4).astype(np.int32)
    triangles = np.empty((len(quads) * 2, 3), dtype=np.int32)
    triangles[::2] = quads[:, (0, 1, 2)]
    triangles[1::2] = quads[:, (0, 2, 3)]
    vertex_dtype = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")]
    if colors is not None:
        vertex_dtype += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
    vertices = np.empty(len(lattice), dtype=vertex_dtype)
    # X/Y 围绕转轴居中；Z=0 是台面，不可再减去半个立方体高度。
    for axis, (name, origin) in enumerate(zip(
            ("x", "y", "z"),
            (src.shape[2] / 2.0, src.shape[1] / 2.0, 0.5))):
        vertices[name] = (lattice[:, axis].astype(np.float32) - origin) * voxel_mm
    if export_y_sign == -1:
        vertices['y'] *= -1
        # A reflection reverses orientation. Reverse winding so back-face
        # culling and generated normals continue to point out of the object.
        triangles=triangles[:,(0,2,1)]
    if seam_colors is not None:
        for channel, name in enumerate(("red", "green", "blue")):
            vertices[name] = seam_colors[:, channel]
    elif colors is not None:
        samples = np.concatenate(corner_colors)
        valid = np.concatenate(corner_valid).astype(np.float32)
        counts = np.bincount(inverse, weights=valid, minlength=len(lattice))
        for channel, name in enumerate(("red", "green", "blue")):
            totals = np.bincount(inverse,
                                 weights=samples[:, channel].astype(np.float32) * valid,
                                 minlength=len(lattice))
            averaged = np.rint(totals / np.maximum(counts, 1))
            vertices[name] = np.where(counts > 0, averaged, 180).astype(np.uint8)
    face_dtype = [("count", "u1"), ("a", "<i4"), ("b", "<i4"), ("c", "<i4")]
    faces = np.empty(len(triangles), dtype=face_dtype)
    faces["count"] = 3
    for column, name in enumerate(("a", "b", "c")):
        faces[name] = triangles[:, column]
    path = Path(path)
    temporary = path.with_name(path.name + ".partial")
    with open(str(temporary), "wb") as stream:
        header = ["ply", "format binary_little_endian 1.0",
                  "comment reconstructed from silhouettes"]
        for key,value in sorted((metadata or {}).items()):
            item='%s %s' % (key,value)
            if any(ord(ch)>127 for ch in item) or '\n' in item or '\r' in item:
                raise ValueError('PLY 元数据必须为单行 ASCII')
            header.append('comment '+item)
        header += ["element vertex %d" % len(vertices),
                  "property float x", "property float y", "property float z"]
        if colors is not None:
            header += ["property uchar red", "property uchar green",
                       "property uchar blue"]
        header += ["element face %d" % len(faces),
                   "property list uchar int vertex_indices", "end_header"]
        stream.write(("\n".join(header) + "\n").encode("ascii"))
        stream.write(vertices.tobytes())
        stream.write(faces.tobytes())
    os.replace(str(temporary), str(path))
    return len(vertices), len(faces)


def mask_motion_diagnostic(masks):
    """量相邻轮廓变化；轮廓不变不能区分对称物体和相机卡帧。"""
    a = np.asarray(masks, dtype=bool)
    if a.ndim != 3 or len(a) < 2:
        raise ValueError("至少需要两张掩膜")
    changed = np.count_nonzero(a[1:] != a[:-1], axis=(1, 2))
    union = np.count_nonzero(a[1:] | a[:-1], axis=(1, 2))
    fraction = changed / np.maximum(union, 1)
    return dict(identical_adjacent_pairs=int(np.count_nonzero(changed == 0)),
                adjacent_pairs=int(len(changed)),
                median_adjacent_change_fraction=float(np.median(fraction)))


def repair_enclosed_holes(masks):
    """补多视角掩膜内部的白洞；与外部相连的背景保持不变。

    白洞用八连通判定，避免把仅经对角线连到外部的真实开口误补。
    只适用于已确认没有真实通孔的物体；返回新掩膜及每张新增像素数。
    """
    src = np.asarray(masks, dtype=bool)
    if src.ndim != 3:
        raise ValueError("masks 必须是 (视角, 高, 宽) 的三维数组")
    repaired = src.copy()
    counts = []
    for view in repaired:
        ys, xs = np.nonzero(view)
        if len(xs) == 0:
            counts.append(0)
            continue
        tile = view[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        background = ~tile
        seen = np.zeros(tile.shape, dtype=bool)
        height, width = tile.shape
        filled = 0
        for y, x in zip(*np.nonzero(background)):
            if seen[y, x]:
                continue
            queue = deque([(int(y), int(x))])
            seen[y, x] = True
            region = []
            touches_edge = False
            while queue:
                cy, cx = queue.popleft()
                region.append((cy, cx))
                if cy == 0 or cy == height - 1 or cx == 0 or cx == width - 1:
                    touches_edge = True
                for ny in range(max(0, cy - 1), min(height, cy + 2)):
                    for nx in range(max(0, cx - 1), min(width, cx + 2)):
                        if background[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            queue.append((ny, nx))
            if not touches_edge:
                for cy, cx in region:
                    tile[cy, cx] = True
                filled += len(region)
        counts.append(filled)
    return repaired, counts


def multiview_support_counts(masks, angles_deg, grid_n, voxel_mm,
                             mm_per_px, axis_col, v_ground,
                             return_missing_view=False):
    """按 PL 使用的 Q16 投影统计每个体素被多少张前景掩膜支持。

    越界投影视为背景；返回 [z,y,x] 的 uint8 数组。这个 PS 模拟
    只有与本次 PL 占据结果逐体素相符时，才可用于解释扫描结果。
    """
    from ps_demo import plan_geometry

    masks = np.asarray(masks, dtype=bool)
    if masks.ndim != 3 or masks.shape[0] < 2 or masks.shape[0] > 255:
        raise ValueError("多视角掩膜必须是 2～255 张的三维数组")
    n_views, height, width = masks.shape
    if np.asarray(angles_deg).shape != (n_views,):
        raise ValueError("多视角角度数量与掩膜数量不一致")
    kv, ofv, kux, kuy, ofu = plan_geometry(
        angles_deg, grid_n, width, height, voxel_mm,
        mm_per_px, axis_col, v_ground)
    x = np.arange(grid_n, dtype=np.int64)[None, :]
    y = np.arange(grid_n, dtype=np.int64)[:, None]
    votes = np.zeros((grid_n, grid_n, grid_n), dtype=np.uint8)
    missing = np.zeros_like(votes) if return_missing_view else None
    for i in range(n_views):
        u = (int(ofu[i]) + x * int(kux[i]) + y * int(kuy[i])) >> 16
        valid_u = (u >= 0) & (u < width)
        safe_u = np.clip(u, 0, width - 1)
        for z in range(grid_n):
            v = (int(ofv) - z * int(kv)) >> 16
            if 0 <= v < height:
                supported = valid_u & masks[i, v, safe_u]
                votes[z] += supported
                if missing is not None:
                    missing[z][~supported] = i + 1
            elif missing is not None:
                missing[z].fill(i + 1)
    return (votes, missing) if return_missing_view else votes


def adaptive_repair_enclosed_holes(masks, angles_deg, grid_n, voxel_mm,
                                   mm_per_px, axis_col, v_ground,
                                   votes, missing_view, photo_evidence=None):
    """结合其他视角和同张原图亮斑，补封闭白洞及局部边缘缺口。

    无彩色原图时维持旧版只补封闭白洞的行为；有彩色原图时
    边缘缺口还必须位于照片亮斑内。饱和到和背景同色时仍有歧义。
    """
    from ps_demo import plan_geometry

    src = np.asarray(masks, dtype=bool)
    n_views, height, width = src.shape
    if votes.shape != (grid_n, grid_n, grid_n) or missing_view.shape != votes.shape:
        raise ValueError("计票数组与体素网格尺寸不一致")
    all_filled, _ = repair_enclosed_holes(src)
    holes = all_filled & ~src
    edge_gaps = np.zeros_like(src)
    if photo_evidence is not None:
        photo_evidence = np.asarray(photo_evidence, dtype=bool)
        if photo_evidence.shape != src.shape:
            raise ValueError("彩色原图亮斑图必须与降采样掩膜同尺寸")
        for i, view in enumerate(src):
            image = Image.fromarray(view.astype(np.uint8) * 255)
            closed = np.asarray(image.filter(ImageFilter.MaxFilter(5)).filter(
                ImageFilter.MinFilter(5)), dtype=np.uint8) != 0
            edge_gaps[i] = closed & ~view & ~holes[i]
    candidates = holes | edge_gaps
    if photo_evidence is not None:
        candidates &= photo_evidence
    repaired = src.copy()
    evidence = np.zeros_like(src)
    kv, ofv, kux, kuy, ofu = plan_geometry(
        angles_deg, grid_n, width, height, voxel_mm,
        mm_per_px, axis_col, v_ground)
    one_miss = votes == n_views - 1
    for i in range(n_views):
        if not candidates[i].any():
            continue
        for z in range(grid_n):
            v = (int(ofv) - z * int(kv)) >> 16
            if not (0 <= v < height):
                continue
            ys, xs = np.nonzero(one_miss[z] & (missing_view[z] == i + 1))
            if len(xs) == 0:
                continue
            u = (int(ofu[i]) + xs.astype(np.int64) * int(kux[i]) +
                 ys.astype(np.int64) * int(kuy[i])) >> 16
            valid = (u >= 0) & (u < width)
            evidence[i, v, u[valid]] = True
        repaired[i] |= candidates[i] & evidence[i]
    hole_counts = np.count_nonzero(repaired & holes, axis=(1, 2)).astype(int).tolist()
    edge_counts = np.count_nonzero(repaired & edge_gaps, axis=(1, 2)).astype(int).tolist()
    return repaired, hole_counts, edge_counts, holes, edge_gaps, evidence


def _horizontal_margin(masks, margin_px):
    """只扩每行最外侧轮廓，保留腿缝、孔洞和其他内部空隙。"""
    src = np.asarray(masks, dtype=bool)
    if src.ndim != 3 or margin_px not in (0, 1):
        raise ValueError("masks 必须是三维数组，margin_px 只能是 0 或 1")
    result = src.copy()
    if margin_px == 1:
        occupied_rows = src.any(axis=2)
        left = np.argmax(src, axis=2)
        right = src.shape[2] - 1 - np.argmax(src[:, :, ::-1], axis=2)
        views, rows = np.nonzero(occupied_rows & (left > 0))
        result[views, rows, left[views, rows] - 1] = True
        views, rows = np.nonzero(occupied_rows & (right < src.shape[2] - 1))
        result[views, rows, right[views, rows] + 1] = True
    return result


def photo_object_evidence_from_color(masks128, frames565, empty565,
                                     thresholds, roi=(160, 0, 640, 480)):
    """只在小白洞/边缘缺口处核对真实彩照，供多视角修补二次确认。

    像素须明显区别于空台同位置，且颜色更接近邻近已识别物体；
    即使满足这两点，也必须再经过其他视角的单票缺失校验。
    这只是有依据的掩膜修正，不把照片颜色推断成真实三维凹凸。
    """
    src = np.asarray(masks128, dtype=bool)
    frames = np.asarray(frames565)
    empty = np.asarray(empty565)
    thresholds = np.asarray(thresholds, dtype=np.float32)
    if src.ndim != 3 or src.shape[1:] != (128, 128) or \
            frames.shape != (len(src), HEIGHT, WIDTH) or frames.dtype != np.uint16 or \
            empty.shape != (HEIGHT, WIDTH) or empty.dtype != np.uint16 or \
            thresholds.shape != (len(src),) or \
            not np.isfinite(thresholds).all() or np.any(thresholds <= 0):
        raise ValueError("彩照局部核验需要逐角度 RGB565 原图、空台原图和差分阈值")
    x0, y0, x1, y1 = map(int, roi)
    if not (0 <= x0 < x1 <= WIDTH and 0 <= y0 < y1 <= HEIGHT and
            x1 - x0 == y1 - y0):
        raise ValueError("彩照局部核验需要图内的正方形 ROI")
    sample = np.floor((np.arange(128) + 0.5) * (x1 - x0) / 128).astype(np.intp)
    ys, xs = y0 + sample, x0 + sample
    empty_rgb = rgb565_to_rgb(empty).astype(np.float32)
    empty_sample = empty_rgb[ys[:, None], xs[None, :]]
    evidence = np.zeros_like(src)
    for i, view in enumerate(src):
        holes = repair_enclosed_holes(view[None])[0][0] & ~view
        tile = Image.fromarray(view.astype(np.uint8) * 255, mode="L")
        closed = np.asarray(tile.filter(ImageFilter.MaxFilter(5)).filter(
            ImageFilter.MinFilter(5)), dtype=np.uint8) != 0
        candidates = holes | (closed & ~view)
        if not candidates.any():
            continue
        frame_rgb = rgb565_to_rgb(frames[i]).astype(np.float32)
        shift = np.median((frame_rgb[:40] - empty_rgb[:40]).reshape(-1, 3), axis=0)
        corrected = np.clip(frame_rgb - shift, 0, 255)
        colors = corrected[ys[:, None], xs[None, :]]
        difference = np.sqrt(np.sum((colors - empty_sample) ** 2, axis=2))
        for y, x in zip(*np.nonzero(candidates)):
            if difference[y, x] <= max(12.0, float(thresholds[i])):
                continue
            ya, yb = max(0, y - 2), min(128, y + 3)
            xa, xb = max(0, x - 2), min(128, x + 3)
            nearby = colors[ya:yb, xa:xb][view[ya:yb, xa:xb]]
            if len(nearby) == 0:
                continue
            neighbor_distance = np.sqrt(np.sum((nearby - colors[y, x]) ** 2,
                                               axis=1)).min()
            if difference[y, x] > 2.0 * neighbor_distance:
                evidence[i, y, x] = True
    return evidence


def _refresh_photo_segmentation(scan, calibration, output_dir):
    """第8步从同圈原图重算旧分割，避免只换代码却继续沿用旧掩膜。"""
    if scan.get('source') in ('pl_preview_unverified', 'pl_review_only') or \
            not bool(scan.get('reconstruction_ready', True)):
        raise RuntimeError('预览或待核验数据不能用于重建；请先完成第7步整圈核验')
    result = {key: (value.item() if isinstance(value,np.ndarray) and value.ndim==0 else value)
              for key,value in scan.items()}
    if scan.get("source") not in ("color_photos", "pl_color_photos"):
        return result
    for key, field in (('color_frames_sha256','color_frames565'),
                       ('empty_frame_sha256','empty_color_frame565')):
        if key in scan and field in scan:
            actual = hashlib.sha256(np.asarray(scan[field]).astype('<u2',copy=False).tobytes()).hexdigest()
            if str(scan[key]) != actual:
                raise ValueError("原图与分割数据的采集指纹不匹配：%s；不能混用不同数据" % field)
    if scan.get('source') == 'pl_color_photos':
        if scan.get('segmentation_backend') != 'PL' or int(scan.get('segmentation_implementation_version',0)) not in (19,20):
            raise RuntimeError('PL掩膜格式不匹配；请重新运行第7步，不回退到PS分割')
        if int(scan.get('interior_evidence_version',0))>=53 and 'measurement_calibration_json' in scan:
            saved=json.loads(str(scan['measurement_calibration_json']))
            if any(k not in calibration or float(calibration[k])!=v for k,v in saved.items()):
                raise ValueError('扫描测量框与当前标定不匹配；不能混用新旧标定')
        return result
    if int(scan.get("segmentation_version", 0)) >= COLOR_SEGMENTATION_VERSION:
        return result
    if "color_frames565" not in scan or "empty_color_frame565" not in scan:
        raise RuntimeError("旧版彩色分割缺少原图或空台参考；请重新运行第7步，不混用旧掩膜")
    old_masks = np.asarray(scan["masks"], dtype=np.uint8)
    np.save(str(output_dir / "masks_full_before_resegmentation.npy"), old_masks)
    refreshed = _segment_color_frames(scan["color_frames565"],
                                     scan["empty_color_frame565"],
                                     calibration["v_ground"],
                                     scan.get("background_noise_sigma"), diagnostic_dir=output_dir)
    for key in ("segmentation_gains", "segmentation_offsets",
                "segmentation_anchor_exceed_fraction"):
        result.pop(key, None)
    result.update(refreshed)
    if "closing_color_frame565" not in scan and not np.array_equal(old_masks, result["masks"]):
        raise RuntimeError("分割版本改变且缺少首尾复拍原图；请重新运行第7步生成并核对掩膜")
    if "closing_color_frame565" in scan:
        closing = _segment_color_frames(
            [scan["closing_color_frame565"]], scan["empty_color_frame565"],
            calibration["v_ground"], scan.get("background_noise_sigma"),
            diagnostic_dir=output_dir/'closing')['masks'][0].astype(bool)
        first = result["masks"][0].astype(bool)
        union = np.count_nonzero(first | closing)
        result["closure_iou"] = (float(np.count_nonzero(first & closing)) / union
                                 if union else 0.0)
        if result["closure_iou"] < 0.8:
            raise RuntimeError("重新分割后的首尾掩膜不一致，不能沿用旧掩膜的通过结果")
    np.save(str(output_dir / "masks_full_after_resegmentation.npy"), result["masks"])
    save_mask_sheet(result["masks"], output_dir / "masks_full_after_resegmentation.png")
    result["segmentation_migrated_from_version"] = int(scan.get("segmentation_version", 0))
    return result


def _silhouette_grid_bound(masks, angles, initial_voxel_mm, mm_per_px, axis_col,
                           v_ground, grid_n):
    """Conservative projection envelope for grid allocation, never occupancy.

    Raster pixel cells have half-pixel boundaries. Intersect 2D silhouette bounds
    in XY; holes and individual foreground pixels remain authoritative later.
    """
    half=(grid_n-1)*initial_voxel_mm*.5
    polygon=[(-half,-half),(half,-half),(half,half),(-half,half)]
    upper_z=(grid_n-1)*initial_voxel_mm
    for mask,angle in zip(np.asarray(masks,dtype=bool),angles):
        yy,xx=np.nonzero(mask)
        if not len(yy):return None
        c,s=float(np.cos(np.deg2rad(angle))),float(np.sin(np.deg2rad(angle)))
        guard=(grid_n+1)*mm_per_px/65536.
        low=(float(xx.min())-axis_col-.5)*mm_per_px-guard
        high=(float(xx.max())-axis_col+.5)*mm_per_px+guard
        upper_z=min(upper_z,(v_ground-float(yy.min())+.5)*mm_per_px+guard)
        for nx,ny,bound in ((c,-s,high),(-c,s,-low)):
            clipped=[]
            if not polygon:return None
            previous=polygon[-1];old=nx*previous[0]+ny*previous[1]-bound
            for point in polygon:
                value=nx*point[0]+ny*point[1]-bound
                if (value<=0)!=(old<=0):
                    fraction=old/(old-value)
                    clipped.append((previous[0]+fraction*(point[0]-previous[0]),
                                    previous[1]+fraction*(point[1]-previous[1])))
                if value<=0:clipped.append(point)
                previous,old=point,value
            polygon=clipped
    if not polygon or upper_z<=0:return None
    radius=max(max(abs(x),abs(y)) for x,y in polygon)
    padding=min(2.,(grid_n-1)*.125)
    pitch=max(radius/((grid_n-1)*.5-padding),upper_z/(grid_n-1-padding))
    return {'candidate_voxel_mm':float(min(initial_voxel_mm,pitch)),
            'xy_envelope_mm':[list(point) for point in polygon],
            'upper_z_bound_mm':float(upper_z),'padding_voxels':padding,
            'used_only_for_grid_allocation':True}


def consistent_voxel_mm(masks, angles, initial_voxel_mm, mm_per_px, axis_col,
                        v_ground, grid_n=128, diagnostic_dir=None):
    """Fit the grid to geometrically consistent space, not isolated 2D clutter.

    Pad by two pilot voxels for sampling uncertainty. This is a numerical grid
    allocation, not hole repair. Bad silhouettes can still remove true space.
    """
    votes = multiview_support_counts(masks, angles, grid_n, initial_voxel_mm,
                                    mm_per_px, axis_col, v_ground)
    z, y, x = np.nonzero(votes == len(angles))
    pilot_voxel_mm=float(initial_voxel_mm)
    retry=None
    if not len(z):
        # A coarse empty pilot is not proof of an empty continuous intersection.
        # Retry only with a conservatively allocated tighter grid; no mask edits,
        # no dropped views, and still require support from every recorded view.
        retry=_silhouette_grid_bound(masks,angles,initial_voxel_mm,mm_per_px,
                                     axis_col,v_ground,grid_n)
        if retry is not None and retry['candidate_voxel_mm']<.95*initial_voxel_mm:
            pilot_voxel_mm=retry['candidate_voxel_mm']
            votes=multiview_support_counts(masks,angles,grid_n,pilot_voxel_mm,
                                           mm_per_px,axis_col,v_ground)
            z,y,x=np.nonzero(votes==len(angles))
    if not len(z):
        src=np.asarray(masks,dtype=bool)
        shared_rows=np.flatnonzero(src.any(axis=2).all(axis=0))
        boxes=[]
        for mask in src:
            yy,xx=np.nonzero(mask)
            boxes.append([int(xx.min()),int(yy.min()),int(xx.max()+1),int(yy.max()+1)] if len(yy) else None)
        diagnostic={'status':'no_common_sampled_voxels','grid_n':int(grid_n),
                    'initial_voxel_mm':float(initial_voxel_mm),'mm_per_px':float(mm_per_px),
                    'retried_grid_bound':retry,'last_pilot_voxel_mm':pilot_voxel_mm,
                    'axis_col':float(axis_col),'v_ground':float(v_ground),
                    'angles_deg':np.asarray(angles).tolist(),'mask_bboxes_xyxy':boxes,
                    'shared_foreground_rows':shared_rows.tolist(),
                    'maximum_supporting_views':int(votes.max()),'total_views':len(angles),
                    'support_histogram':np.bincount(votes.ravel(),minlength=len(angles)+1).tolist(),
                    'continuous_intersection_emptiness_proven':False,
                    'cause':'undetermined_requires_same_run_photos_masks_and_calibration'}
        hint=('各视角连共同前景高度都没有；先核对掩膜与投影模型，不能只归因于转速' if not len(shared_rows)
              else '有共同前景高度，但当前网格未找到共同横向投影；需核对掩膜、转轴、角度及采样')
        if diagnostic_dir is not None:
            target=Path(diagnostic_dir);target.mkdir(parents=True,exist_ok=True)
            np.save(str(target/'geometry_failed_masks128.npy'),src)
            np.save(str(target/'geometry_failed_support_counts.npy'),votes)
            (target/'geometry_failure.json').write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding='utf-8')
            hint+='；诊断：%s' % (target/'geometry_failure.json')
        raise ValueError('%d³网格未找到各视角共同占据体素。%s' % (grid_n,hint))
    half = (grid_n - 1) / 2.
    radius = max(float(np.max(np.abs(x-half))), float(np.max(np.abs(y-half)))) + 2.
    height = float(z.max()) + 2.
    required = max(2*radius/(grid_n-1), height/(grid_n-1))*pilot_voxel_mm
    pitch = min(pilot_voxel_mm, required)
    return pitch, {'initial_voxel_mm':float(initial_voxel_mm),
                   'selected_voxel_mm':pitch,'pilot_consistent_voxels':int(len(z)),
                   'empty_coarse_pilot_retry':retry,'final_pilot_voxel_mm':pilot_voxel_mm,
                   'pilot_padding_voxels':2,'selection':'intersection_bounds_not_2d_outliers'}


def silhouette_reprojection_check(occ, voxel_mm, masks, angles, calibration):
    """Measure whether one 3D shape explains the recorded silhouettes.

    Dilation here approximates voxel footprints in the diagnostic renderer only;
    it never changes input silhouettes or occupancy. Good coverage is necessary,
    not sufficient, for a physically accurate shape.
    """
    from ps_demo import plan_geometry
    a = np.asarray(occ, dtype=bool)
    z,y,x = np.nonzero(_surface_mask(a))
    n = a.shape[0]
    if 'turntable_camera' in calibration:
        camera=_turntable_camera_module();model=calibration['turntable_camera']
        points=np.stack([x,y,z],1);footprint=max(1,int(np.ceil(voxel_mm/calibration['mm_per_px']*.5*np.sqrt(2))))
        coverage=[];unexplained=[]
        for mask,angle in zip(np.asarray(masks,bool),angles):
            uf,vf,depth=camera.project_voxels(points,model,angle,n,voxel_mm)
            u=np.floor(uf+.5).astype(np.intp);v=np.floor(vf+.5).astype(np.intp)
            valid=(depth>0)&(u>=0)&(u<WIDTH)&(v>=0)&(v<HEIGHT)
            rendered=np.zeros((HEIGHT,WIDTH),np.uint8);rendered[v[valid],u[valid]]=255
            rendered=np.asarray(Image.fromarray(rendered).filter(ImageFilter.MaxFilter(2*footprint+1)))!=0
            missing=int(np.count_nonzero(mask&~rendered));unexplained.append(missing);coverage.append(1.-missing/max(1,mask.sum()))
        return dict(foreground_coverage_per_view=coverage,unexplained_foreground_pixels_per_view=unexplained,
            minimum_coverage=float(min(coverage)),median_coverage=float(np.median(coverage)),
            diagnostic_footprint_radius_px=footprint,camera_model='photo_estimated_pinhole',physical_shape_accuracy_verified=False)
    kv,ofv,kux,kuy,ofu = plan_geometry(angles,n,WIDTH,HEIGHT,voxel_mm,
        calibration['mm_per_px'],calibration['axis_col'],calibration['v_ground'])
    v = ((int(ofv)-z.astype(np.int64)*int(kv))>>16).astype(np.intp)
    footprint = max(1,int(np.ceil(voxel_mm/calibration['mm_per_px']*.5*np.sqrt(2))))
    coverage, unexplained = [], []
    for i,mask in enumerate(np.asarray(masks,dtype=bool)):
        u = ((int(ofu[i])+x.astype(np.int64)*int(kux[i])+y.astype(np.int64)*int(kuy[i]))>>16).astype(np.intp)
        valid = (u>=0)&(u<WIDTH)&(v>=0)&(v<HEIGHT)
        rendered = np.zeros((HEIGHT,WIDTH),dtype=np.uint8)
        rendered[v[valid],u[valid]]=255
        rendered = np.asarray(Image.fromarray(rendered).filter(
            ImageFilter.MaxFilter(2*footprint+1)))!=0
        missing = int(np.count_nonzero(mask&~rendered))
        unexplained.append(missing)
        coverage.append(float(1.-missing/max(1,mask.sum())))
    return {'foreground_coverage_per_view':coverage,
            'unexplained_foreground_pixels_per_view':unexplained,
            'minimum_coverage':float(min(coverage)),
            'median_coverage':float(np.median(coverage)),
            'diagnostic_footprint_radius_px':footprint,
            'physical_shape_accuracy_verified':False}


def reconstruct_scan(scan, calibration, overlay, output_dir,
                     horizontal_margin_px=0, fill_enclosed_holes=False,
                     multiview_diagnostics=False, make_preview=True,
                     detail_surface=True, progress=None, detail_attributes=True):
    """复用当前 Overlay，把整圈掩膜送到 recon_0 并保存体积/形状。

    输出的 .ply 是体素中心点云，不是带面的网格，也不带物体颜色。
    horizontal_margin_px=1 只扩每行最外侧的横向边界，
    不改变体素边长；必须另存目录并用已知尺寸实物验证。
    fill_enclosed_holes="auto" 只有同圈彩照的亮斑或局部颜色证据，
    再加多视角一致性支持时才补洞；没有对应原图就保留白洞。
    True/False 留作旧调用兼容。单角度可见的真实孔仍可能误判。
    multiview_diagnostics 还测量横向扩边前的 PL 体积，并保存逐体素计票。
    """
    from ps_demo import Recon, demo, prepare_scan_masks, save_ply, show_voxels

    if horizontal_margin_px not in (0, 1):
        raise ValueError("horizontal_margin_px 只能是 0 或 1")
    if fill_enclosed_holes not in ("auto", True, False):
        raise ValueError("fill_enclosed_holes 只能为 'auto'、True 或 False")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if progress:progress.update("核对同圈输入与掩膜")
    scan = _refresh_photo_segmentation(scan, calibration, output_dir)
    adaptive = (scan.get('source') in ('color_photos','pl_color_photos') and
                int(scan.get('segmentation_version',0)) >= 5)
    if adaptive and fill_enclosed_holes is not False:
        raise ValueError("自适应证据分割不兼容旧版RGB阈值补洞；应保持fill_enclosed_holes=False")
    masks_full = np.asarray(scan["masks"])
    angles = np.asarray(scan["angles_deg"], dtype=np.float64)
    if masks_full.shape[0] != len(angles):
        raise ValueError("掩膜数量与角度数量不一致")

    if int(scan.get('interior_evidence_version',0))>=53:
        strict=np.asarray(masks_full,dtype=bool)
        unknown=np.asarray(scan['interior_unknown_masks'],dtype=bool)
        inferred=np.asarray(scan['interior_inferred_masks'],dtype=bool)
        confirmed=np.asarray(scan['interior_confirmed_background_masks'],dtype=bool)
        upper=np.asarray(scan['conservative_surface_masks'],dtype=bool)
        if any(v.shape!=strict.shape for v in (unknown,inferred,confirmed,upper)):
            raise ValueError('内部证据形状与原始掩膜不匹配')
        if ((unknown&inferred)|(unknown&confirmed)|(inferred&confirmed)|((unknown|inferred|confirmed)&strict)).any():
            raise ValueError('内部证据状态重叠')
        if not np.array_equal(upper,strict|unknown|inferred):
            raise ValueError('保守外壳与内部证据不一致')
    if progress:progress.update("读取PL配置与降采样")
    recon = Recon(overlay=overlay)
    if len(angles) != recon.NV:
        raise ValueError("当前 bit 要求 %d 个视角，实际采集 %d 个" %
                         (recon.NV, len(angles)))
    roi = tuple(map(int,scan.get('reconstruction_roi_xyxy',(160,0,640,480))))
    masks128, mm_per_px128, axis128, ground128 = prepare_scan_masks(
        masks_full, calibration["mm_per_px"], calibration["axis_col"],
        calibration["v_ground"], roi=roi, out_size=recon.MW)
    if masks128.shape[1:] != (recon.MH, recon.MW):
        raise ValueError("当前 bit 掩膜不是正方形 128x128；请核对编译参数")
    photo128 = None
    photo_evidence_mode = "none"
    photo_color_error = None
    if fill_enclosed_holes is not False and "photo_highlight_masks" in scan:
        highlights = np.asarray(scan["photo_highlight_masks"])
        if highlights.shape != masks_full.shape:
            raise ValueError("彩色原图亮斑图与扫描掩膜的数量或尺寸不一致")
        photo128 = np.stack([
            np.asarray(Image.fromarray((view[roi[1]:roi[3], roi[0]:roi[2]] != 0)
                                       .astype(np.uint8) * 255).resize(
                (recon.MW, recon.MH), resample=Image.NEAREST), dtype=np.uint8) != 0
            for view in highlights])
        np.save(str(output_dir / "photo_highlight128.npy"), photo128)
        save_mask_sheet(photo128, output_dir / "photo_highlight128.png")
        photo_evidence_mode = "bright_only"
    voxel_mm = suggest_voxel_mm(masks128, axis128, ground128,
                                mm_per_px128, recon.N)
    grid_selection = None
    if adaptive:
        if progress:progress.update("PS核对共同体素并分配网格")
        voxel_mm, grid_selection = consistent_voxel_mm(
            masks128,angles,voxel_mm,mm_per_px128,axis128,ground128,recon.N,
            diagnostic_dir=output_dir)
    if progress:progress.update("PL雕刻与结果读回")
    auto_mode = fill_enclosed_holes == "auto"
    raw_votes = None
    raw_mismatch = None
    final_votes = None
    final_mismatch = None
    auto_error = None
    possible_hole_pixels = 0
    possible_edge_pixels = 0
    photo_candidate_hole_pixels = 0
    photo_candidate_edge_pixels = 0
    hole_counts = [0] * len(masks128)
    edge_counts = [0] * len(masks128)
    # 水平方向各留一个 128 像素掩膜像素的余量；自动判断使用同一份 PL 输入。
    raw_masks = _horizontal_margin(masks128, horizontal_margin_px)
    if fill_enclosed_holes is not False and "color_frames565" in scan and "empty_color_frame565" in scan and \
            "photo_thresholds" in scan:
        try:
            color_evidence = photo_object_evidence_from_color(
                raw_masks, scan["color_frames565"], scan["empty_color_frame565"],
                scan["photo_thresholds"], roi=roi)
            photo128 = color_evidence if photo128 is None else photo128 | color_evidence
            photo_evidence_mode = "bright_and_local_color" if \
                "photo_highlight_masks" in scan else "local_color"
            np.save(str(output_dir / "photo_object_evidence128.npy"), photo128)
            save_mask_sheet(photo128, output_dir / "photo_object_evidence128.png")
        except Exception as exc:
            photo_color_error = str(exc)
            print("彩照局部核验跳过：%s；继续用已有掩膜重建" % exc)
    motion = mask_motion_diagnostic(masks128)
    print("输入 PL：%d 张 %dx%d，体素边长 %.3f mm" %
          (len(angles), recon.MW, recon.MH, voxel_mm))
    print("降采样掩膜最外侧的水平边界余量：%d 像素；内部空隙不扩张" %
          horizontal_margin_px)
    print("相邻掩膜完全相同 %d/%d 对；轮廓相同可能是圆柱对称，也可能是相机卡帧，首尾 IoU 不能单独验证转速" %
          (motion["identical_adjacent_pairs"], motion["adjacent_pairs"]))
    save_mask_sheet(masks128, output_dir / "masks128_before_hole_fill.png")
    no_margin_volume = None
    no_margin_pl_mismatch = None
    no_margin_error = None
    if multiview_diagnostics and horizontal_margin_px == 1:
        # 横向扩张来自单个 94 mm 圆柱的经验验证；保留未扩张的 PL 对照。
        try:
            print("横向 1 像素余量对照：先运行未扩张的原始 128 掩膜")
            no_margin_occ = demo(None, masks128, angles, voxel_mm, mm_per_px128,
                                 axis_col=axis128, v_ground=ground128, overlay=overlay)
            no_margin_votes = multiview_support_counts(
                masks128, angles, recon.N, voxel_mm,
                mm_per_px128, axis128, ground128)
            no_margin_pl_mismatch = int(np.count_nonzero(
                (no_margin_votes == len(angles)) != no_margin_occ))
            if no_margin_pl_mismatch:
                no_margin_error = "未扩张掩膜的软件投影与 PL 相差 %d 个体素" % no_margin_pl_mismatch
                print("横向余量对照未通过：%s" % no_margin_error)
            else:
                np.save(str(output_dir / "occupancy_no_horizontal_margin.npy"),
                        no_margin_occ)
                no_margin_volume = float(np.count_nonzero(no_margin_occ) *
                                         voxel_mm ** 3 / 1000.0)
                print("未扩张的 PL 体积 %.2f cm³；与本次主结果分开保存" %
                      no_margin_volume)
        except Exception as exc:
            no_margin_error = str(exc)
            print("横向余量对照未完成：%s；仍继续主结果" % exc)
    if auto_mode:
        print("自动修补：先用未补洞的掩膜做 PL 基线重建")
        np.save(str(output_dir / "masks128_before_auto.npy"), raw_masks)
        save_mask_sheet(raw_masks, output_dir / "masks128_before_auto.png")
        raw_occ = demo(None, raw_masks, angles, voxel_mm, mm_per_px128,
                       axis_col=axis128, v_ground=ground128, overlay=overlay)
        np.save(str(output_dir / "occupancy_before_auto.npy"), raw_occ)
        masks_for_pl = raw_masks
        occ = raw_occ
        try:
            raw_votes, missing_view = multiview_support_counts(
                raw_masks, angles, recon.N, voxel_mm,
                mm_per_px128, axis128, ground128, return_missing_view=True)
            raw_mismatch = int(np.count_nonzero((raw_votes == len(angles)) != raw_occ))
            if raw_mismatch:
                print("软件投影与 PL 相差 %d 个体素，自动修补跳过" % raw_mismatch)
            elif photo128 is None:
                filled_candidate, _ = repair_enclosed_holes(raw_masks)
                possible_hole_pixels = int(np.count_nonzero(
                    filled_candidate & ~raw_masks))
                print("没有同角度彩色原图，保留 %d 个可疑白洞像素；不自动填实" %
                      possible_hole_pixels)
            else:
                repaired, hole_counts, edge_counts, holes, edge_gaps, evidence = adaptive_repair_enclosed_holes(
                    raw_masks, angles, recon.N, voxel_mm,
                    mm_per_px128, axis128, ground128, raw_votes, missing_view,
                    photo_evidence=photo128)
                possible_hole_pixels = int(np.count_nonzero(holes))
                possible_edge_pixels = int(np.count_nonzero(edge_gaps))
                if photo128 is not None:
                    photo_candidate_hole_pixels = int(np.count_nonzero(holes & photo128))
                    photo_candidate_edge_pixels = int(np.count_nonzero(edge_gaps & photo128))
                if sum(hole_counts) + sum(edge_counts):
                    masks_for_pl = repaired
                    if photo128 is None:
                        print("多视角确认：内部白洞 %d 个像素；再次送入 PL 重建" %
                              sum(hole_counts))
                    else:
                        print("彩色原图与多视角确认：内部 %d、边缘 %d 个像素；再次送入 PL 重建" %
                              (sum(hole_counts), sum(edge_counts)))
                    occ = demo(None, masks_for_pl, angles, voxel_mm, mm_per_px128,
                               axis_col=axis128, v_ground=ground128, overlay=overlay)
                    final_votes = multiview_support_counts(
                        masks_for_pl, angles, recon.N, voxel_mm,
                        mm_per_px128, axis128, ground128)
                    final_mismatch = int(np.count_nonzero(
                        (final_votes == len(angles)) != occ))
                    if final_mismatch:
                        raise RuntimeError("修补后软件投影与 PL 相差 %d 个体素" % final_mismatch)
                else:
                    print("没有足够的跨视角证据，保留未修补的 PL 结果")
        except Exception as exc:
            # 自动判断失败时不沿用半完成的修补模型，重用已保存的基线结果。
            auto_error = str(exc)
            masks_for_pl = raw_masks
            occ = raw_occ
            hole_counts = [0] * len(masks128)
            edge_counts = [0] * len(masks128)
            print("自动修补跳过：%s；保留未修补的 PL 结果" % exc)
    else:
        repaired = repair_enclosed_holes(masks128)[0] if fill_enclosed_holes else masks128
        masks_for_pl = _horizontal_margin(repaired, horizontal_margin_px)
        hole_counts = np.count_nonzero(masks_for_pl & ~raw_masks,
                                      axis=(1, 2)).astype(int).tolist()
        occ = demo(None, masks_for_pl, angles, voxel_mm, mm_per_px128,
                   axis_col=axis128, v_ground=ground128, overlay=overlay)
    print("内部白洞补回像素：总计 %d，各视角 %s" %
          (sum(hole_counts), hole_counts))
    if photo128 is not None:
        print("彩色原图辅助的边缘补回像素：总计 %d，各视角 %s" %
              (sum(edge_counts), edge_counts))
    save_mask_sheet(masks_for_pl, output_dir / "masks128_sent_to_pl.png")
    np.save(str(output_dir / "masks128_sent_to_pl.npy"), masks_for_pl)
    print("已导出点云：%s (%d 点)" %
          (output_dir / "recon.ply", save_ply(str(output_dir / "recon.ply"), occ, voxel_mm)))
    if progress:progress.update("保存PL结果与投影诊断")
    np.save(str(output_dir / "occupancy.npy"), occ)
    volume_cm3 = float(np.count_nonzero(occ) * voxel_mm ** 3 / 1000.0)
    dimensions = occupancy_dimensions_mm(occ, voxel_mm)
    closure = float(scan.get("closure_iou", np.nan))
    report = dict(reconstruction_performance_version=RECONSTRUCTION_PERFORMANCE_VERSION,
                  reconstruction_roi_xyxy=list(roi),volume_cm3=volume_cm3, occupied_voxels=int(np.count_nonzero(occ)),
                  voxel_mm=float(voxel_mm), mm_per_px128=float(mm_per_px128),
                  axis_col128=float(axis128), v_ground128=float(ground128),
                  n_views=len(angles),
                  horizontal_margin_px=int(horizontal_margin_px),
                  horizontal_margin_mode=("outer_edges_only" if horizontal_margin_px
                                          else "none"),
                  fill_enclosed_holes=bool(sum(hole_counts) + sum(edge_counts)),
                  hole_fill_mode="auto" if auto_mode else bool(fill_enclosed_holes),
                  photo_guided_repair=bool(photo128 is not None and auto_mode),
                  photo_evidence_mode=photo_evidence_mode,
                  photo_color_evidence_error=photo_color_error,
                  filled_hole_pixels=int(sum(hole_counts)),
                  filled_hole_pixels_per_view=[int(n) for n in hole_counts],
                  filled_edge_pixels=int(sum(edge_counts)),
                  filled_edge_pixels_per_view=[int(n) for n in edge_counts],
                  voxel_extent_mm=dimensions["voxel_extent_mm"],
                  point_center_span_mm=dimensions["point_center_span_mm"],
                  bottom_gap_mm=dimensions["bottom_gap_mm"],
                  table_to_top_mm=dimensions["table_to_top_mm"],
                  mask_motion=motion,
                  closure_iou=closure if np.isfinite(closure) else None)
    report["pipeline_version"] = 20 if scan.get('source') == 'pl_color_photos' else 18
    if 'camera_settings' in calibration:
        report['camera_settings'] = calibration['camera_settings']
    report['segmentation_backend'] = scan.get('segmentation_backend',
                                             'PS' if scan.get('source') == 'color_photos' else 'legacy_PL')
    if scan.get('source') == 'pl_color_photos':
        report['pl_mask_board_self_test_passed'] = bool(scan.get('pl_board_self_test_passed',False))
        if 'pl_packed_decisions' in scan:
            np.save(str(output_dir/'pl_packed_decisions.npy'), scan['pl_packed_decisions'])
    report['projection_pixel_convention'] = 'integer_pixel_centers_nearest_sampling_q16'
    report["segmentation_version"] = int(scan.get("segmentation_version", 0))
    report["segmentation_migrated_from_version"] = scan.get(
        "segmentation_migrated_from_version")
    report["mask_geometry_operations"] = (
        "noise_calibrated_hysteresis_no_morphological_filling" if adaptive else
        "legacy_v9_photo_difference_morphology_and_component_filter" if
        scan.get("source") in ("color_photos","pl_color_photos") else "legacy_rtl_mask_cleaning")
    report['grid_selection'] = grid_selection
    report["segmentation_algorithm"] = scan.get("segmentation_algorithm")
    report['acquisition_fingerprints'] = {key:str(scan[key]) for key in
        ('color_frames_sha256','empty_frame_sha256') if key in scan}
    if "photo_thresholds" in scan:
        report["segmentation_thresholds"] = np.asarray(scan["photo_thresholds"]).tolist()
        report['segmentation_threshold_units'] = str(scan.get('photo_threshold_units','RGB_distance'))
    if 'segmentation_diagnostics_json' in scan:
        report['segmentation_diagnostics'] = json.loads(str(scan['segmentation_diagnostics_json']))
    report['roi_boundary_filtered_pixels'] = sum(
        int(d.get('boundary_removed_pixels',0)) for d in report.get('segmentation_diagnostics',[]))
    report['roi_boundary_requires_review'] = report['roi_boundary_filtered_pixels'] > 0
    if multiview_diagnostics:
        report["no_horizontal_margin_volume_cm3"] = no_margin_volume
        report["no_horizontal_margin_pl_mismatch_voxels"] = no_margin_pl_mismatch
        report["no_horizontal_margin_error"] = no_margin_error
        if no_margin_volume is not None and not auto_mode:
            report["horizontal_margin_baseline_effect_cm3"] = volume_cm3 - no_margin_volume
    if auto_mode:
        report["auto_baseline_volume_cm3"] = float(
            np.count_nonzero(raw_occ) * voxel_mm ** 3 / 1000.0)
        if no_margin_volume is not None:
            report["horizontal_margin_baseline_effect_cm3"] = float(
                report["auto_baseline_volume_cm3"] - no_margin_volume)
        report["auto_candidate_hole_pixels"] = possible_hole_pixels
        report["auto_candidate_edge_pixels"] = possible_edge_pixels
        report["auto_photo_candidate_hole_pixels"] = photo_candidate_hole_pixels
        report["auto_photo_candidate_edge_pixels"] = photo_candidate_edge_pixels
        report["auto_filled_hole_pixels"] = int(sum(hole_counts))
        report["auto_filled_edge_pixels"] = int(sum(edge_counts))
        report["auto_preserved_hole_pixels"] = possible_hole_pixels - int(sum(hole_counts))
        report["auto_preserved_edge_pixels"] = possible_edge_pixels - int(sum(edge_counts))
        report["auto_raw_pl_mismatch_voxels"] = raw_mismatch
        report["auto_final_pl_mismatch_voxels"] = final_mismatch
        report["auto_error"] = auto_error
    if multiview_diagnostics:
        report["multiview_verified"] = False
        try:
            print("正在用送入 PL 的掩膜核对 24 视角软件投影……")
            if auto_mode and raw_votes is not None and masks_for_pl is raw_masks:
                votes_pl = raw_votes
            elif auto_mode and final_votes is not None and masks_for_pl is not raw_masks:
                votes_pl = final_votes
            else:
                votes_pl = multiview_support_counts(
                    masks_for_pl, angles, recon.N, voxel_mm,
                    mm_per_px128, axis128, ground128)
            mismatch = int(np.count_nonzero((votes_pl == len(angles)) != occ))
            report["multiview_pl_mismatch_voxels"] = mismatch
            if mismatch:
                print("多视角诊断暂停：软件投影与 PL 相差 %d 个体素；PL 结果仍已保存" % mismatch)
            else:
                print("投影核对通过，正在统计原始掩膜的可疑区域……")
                if auto_mode and raw_votes is not None:
                    votes=raw_votes
                elif masks_for_pl is raw_masks or np.array_equal(masks_for_pl,raw_masks):
                    votes=votes_pl
                else:
                    votes=multiview_support_counts(raw_masks,angles,recon.N,voxel_mm,
                                                   mm_per_px128,axis128,ground128)
                report['multiview_baseline_reused_identical_input']=votes is votes_pl
                strict = votes == len(angles)
                one_miss = votes == len(angles) - 1
                strict_count = int(np.count_nonzero(strict))
                candidate_count = int(np.count_nonzero(one_miss))
                voxel_cm3 = float(voxel_mm ** 3 / 1000.0)
                report["multiview_raw_strict_voxels"] = strict_count
                report["multiview_one_miss_voxels"] = candidate_count
                report["multiview_raw_strict_volume_cm3"] = strict_count * voxel_cm3
                report["multiview_one_miss_upper_volume_cm3"] = (
                    strict_count + candidate_count) * voxel_cm3
                np.save(str(output_dir / "multiview_support_counts.npy"), votes)
                Image.fromarray((one_miss.any(axis=0).astype(np.uint8) * 255)).save(
                    str(output_dir / "multiview_one_miss_xy.png"))
                report["multiview_verified"] = True
                print("原始掩膜 %d 视角全票体积 %.2f cm³；仅差 1 票的可疑体素 %d 个" %
                      (len(angles), report["multiview_raw_strict_volume_cm3"], candidate_count))
                print("若把全部可疑体素都算入，宽松上界 %.2f cm³；不是自动修补结果" %
                      report["multiview_one_miss_upper_volume_cm3"])
        except Exception as exc:
            report["multiview_verified"] = False
            report["multiview_error"] = str(exc)
            print("多视角诊断失败：%s；PL 结果仍已保存" % exc)
    # PL 的 128³ 体积始终是主测量值。完整相机掩膜只生成另一份
    # PS 表面预览；不覆盖 PL 结果，也不把更密的采样称为内部几何恢复。
    report["detail_surface_source"] = "full_resolution_silhouettes_pl_aligned_ps"
    report["detail_surface_file"] = None
    report["detail_surface_points"] = None
    report["detail_surface_voxel_mm"] = None
    report["detail_surface_error"] = None
    report["detail_mesh_file"] = None
    report["detail_mesh_vertices"] = None
    report["detail_mesh_faces"] = None
    report["detail_mesh_error"] = None
    report["detail_surface_colored"] = False
    report["detail_color_assigned_points"] = 0
    report["detail_color_error"] = None
    report["detail_outer_margin_full_px"] = None
    report["detail_pl_repair_pixels128"] = None
    report["detail_surface_volume_cm3"] = None
    report["detail_surface_volume_minus_pl_cm3"] = None
    report["detail_occupancy_file"] = None
    report["detail_photo_repair_pixels"] = 0 if fill_enclosed_holes is False else None
    report["detail_photo_repair_pixels_per_view"] = None
    report["detail_photo_repair_error"] = None
    report["texture_white_balance"] = {"applied": False, "source": "disabled"}
    report["texture_projection"] = "same_q16_as_reconstruction_front_facing_only"
    report["mesh_vertex_color_mode"] = "observed_color_seams_no_cross_color_average"
    calibration=dict(calibration,reconstruction_roi_xyxy=list(roi))
    detail_calibration=dict(calibration)
    detail_calibration.pop('turntable_camera',None)
    report['camera_projection_estimate']=dict(version=43,accepted=False,reason='not_evaluated')
    report['PL_projection_model']='legacy_parallel_projection'
    if detail_surface and recon.N == 128:
        try:
            started = time.monotonic()
            uncertainty_voxels = None
            # 128 掩膜的一像素相当于 3.75 个原图像素；向下取整，
            # 避免把圆柱上的经验余量放大到 4 像素以上。
            detail_margin = int(np.floor(horizontal_margin_px *
                                         (roi[2] - roi[0]) / recon.MW))
            repair_additions = masks_for_pl & ~raw_masks
            detail_masks = masks_full
            if int(scan.get('interior_evidence_version',0))>=53:
                upper=np.asarray(scan['conservative_surface_masks'],dtype=bool)
                unknown=np.asarray(scan['interior_unknown_masks'],dtype=bool)
                inferred=np.asarray(scan['interior_inferred_masks'],dtype=bool)
                if upper.shape!=masks_full.shape or unknown.shape!=masks_full.shape or inferred.shape!=masks_full.shape:
                    raise ValueError('内部三态证据尺寸不匹配')
                if not np.array_equal(upper,masks_full.astype(bool)|unknown|inferred):
                    raise ValueError('保守外壳必须等于原始掩膜与已记录不确定证据的并集')
                detail_masks=upper
                np.save(str(output_dir/'strict_measurement_masks.npy'),masks_full)
                np.save(str(output_dir/'interior_unknown_masks.npy'),unknown)
                np.save(str(output_dir/'interior_inferred_masks.npy'),inferred)
                report['interior_evidence']=dict(version=53,
                    unknown_pixels=int(unknown.sum()),inferred_pixels=int(inferred.sum()),
                    PL_masks_unchanged=True,PS_surface='conservative_visual_hull_not_measured_material',
                    unresolved_true_holes_may_be_closed=True,physical_depth_recovered=False)
                report['detail_surface_source']='uncertainty_preserving_conservative_visual_hull_PS' 
                scope_source=scan
                source_extra=np.zeros_like(upper)
                source_info=dict(version=75,performed=False,added_uncertainty_pixels=0,
                    reason='PL_evidence_unavailable')
                if int(scan.get('low_contrast_recovery_version',0)) >= 66:
                    import importlib,subject_evidence
                    if Path(subject_evidence.__file__).resolve().parent!=Path(__file__).resolve().parent:
                        raise RuntimeError('subject_evidence.py必须与scan_pipeline.py同目录')
                    subject_evidence=importlib.reload(subject_evidence)
                    source_extra,source_info=subject_evidence.review_scan(scan,calibration,output_dir,progress)
                    # Keep the previous supplementary audit independent of
                    # the new review, in stage 7 and in saved-scan replays.
                    if int(scan.get('source_evidence_version',0))>=75:
                        scope_source=dict(scan,conservative_surface_masks=upper&~source_extra,
                            interior_unknown_masks=unknown&~source_extra)
                    scope = _uncertainty_scope_module()
                    try:
                        additional_unknown, scope_info = scope.review(scope_source, calibration, output_dir, progress)
                    except (OSError,ValueError,RuntimeError) as scope_error:
                        additional_unknown = np.zeros_like(upper)
                        scope_info = dict(version=69,performed=False,
                            reason='uncertainty_audit_unavailable_original_surface_retained',
                            error=str(scope_error),strict_foreground_changed=False)
                    report['uncertainty_scope'] = scope_info
                    if scope_info.get('performed') and scope_info.get('added_unknown_pixels',0):
                        detail_masks = upper | additional_unknown
                        report['interior_evidence'].update(
                            source_unknown_pixels=int(unknown.sum()),
                            unknown_pixels=int(unknown.sum()+additional_unknown.sum()),
                            additional_PL_overlap_unknown_pixels=int(additional_unknown.sum()),
                            uncertain_surface_display='photo_colors_with_separate_uncertainty_diagnostics')
                    del additional_unknown
                report['source_evidence_review']=source_info
                if source_info.get('added_uncertainty_pixels',0):
                    detail_masks=detail_masks|source_extra
                    prior=np.asarray(scope_source['conservative_surface_masks'],bool)
                    combined=detail_masks&~prior
                    scope_info=report.setdefault('uncertainty_scope',{})
                    scope_info.update(version=75,performed=True,prior_scope_version=69,
                        prior_scope_added_unknown_pixels=scope_info.get('added_unknown_pixels',0),
                        source_region_unknown_pixels=int(source_extra.sum()),
                        added_unknown_pixels=int(combined.sum()),
                        source_upper_sha256=hashlib.sha256(prior.astype(np.uint8).tobytes()).hexdigest(),
                        added_unknown_sha256=hashlib.sha256(combined.astype(np.uint8).tobytes()).hexdigest(),
                        audited_upper_sha256=hashlib.sha256(detail_masks.astype(np.uint8).tobytes()).hexdigest(),
                        surface_masks_file='1024_surface_evidence_v75.npz',
                        reason='measured_PL_parts_and_verified_source_regions_retained_as_unknown')
                    _atomic_npz(output_dir/'1024_surface_evidence_v75.npz',
                        surface_masks=detail_masks,added_unknown_masks=combined)
                    report['interior_evidence'].update(
                        unknown_pixels=int((detail_masks&~masks_full.astype(bool)&~inferred).sum()),
                        source_region_unknown_pixels=int(source_extra.sum()),
                        source_evidence_version=75)
                    del combined
                del source_extra
            if (fill_enclosed_holes is not False and "color_frames565" in scan and
                    "empty_color_frame565" in scan and
                    "photo_thresholds" in scan):
                try:
                    detail_masks, photo_additions, photo_counts = \
                        repair_full_resolution_from_photos(
                            masks_full, occ, voxel_mm, angles, calibration,
                            scan["color_frames565"],
                            scan["empty_color_frame565"],
                            scan["photo_thresholds"])
                    report["detail_photo_repair_pixels"] = int(sum(photo_counts))
                    report["detail_photo_repair_pixels_per_view"] = [
                        int(n) for n in photo_counts]
                    np.save(str(output_dir / "detail_photo_repair_mask.npy"),
                            photo_additions)
                    save_mask_sheet(photo_additions,
                                    output_dir / "detail_photo_repair_mask.png")
                except Exception as exc:
                    report["detail_photo_repair_error"] = str(exc)
                    print("全分辨率彩照核验跳过：%s；使用原始掩膜细化" % exc)
            if progress:progress.update("PS细化256立方体网格")
            detail_occ, detail_voxel_mm = reconstruct_full_resolution_silhouette(
                detail_masks, angles, detail_calibration, voxel_mm,
                grid_n=256, coarse_grid_n=recon.N,
                outer_margin_full_px=detail_margin,
                coarse_repair_additions=repair_additions)
            if (detail_margin==0 and fill_enclosed_holes is False and
                    'color_frames565' in scan and not repair_additions.any()):
                try:
                    camera=_turntable_camera_module()
                    if progress:progress.update("照片匹配与透视参数估计")
                    model,camera_info=camera.estimate_with_budget(scan['color_frames565'],detail_masks,angles,calibration,budget_s=20.,progress=progress)
                    report['camera_projection_estimate']=camera_info
                    if camera_info.get('reason','').startswith('camera_time_budget_exceeded'):
                        print('透视估计达到20秒预算；保留已有轮廓模型，继续取色和导出。',flush=True)
                    if model is not None:
                        candidate_cal=dict(detail_calibration,turntable_camera=model)
                        if progress:progress.update("透视网格雕刻与投影验证")
                        candidate=camera.carve(detail_masks,angles,model,256,detail_voxel_mm)
                        baseline_check=silhouette_reprojection_check(detail_occ,detail_voxel_mm,detail_masks,angles,calibration)
                        candidate_check=silhouette_reprojection_check(candidate,detail_voxel_mm,detail_masks,angles,candidate_cal) if candidate.any() else None
                        clipped=bool(candidate[0].any() or candidate[-1].any() or candidate[:,0].any() or
                            candidate[:,-1].any() or candidate[:,:,0].any() or candidate[:,:,-1].any())
                        adopt=bool(candidate_check and not clipped and
                            candidate_check['minimum_coverage']>=baseline_check['minimum_coverage'] and
                            candidate_check['median_coverage']>=baseline_check['median_coverage']+.03)
                        camera_info['mask_reprojection_comparison']=dict(baseline=baseline_check,candidate=candidate_check,
                            candidate_grid_clipped=clipped,geometry_adopted=adopt,
                            interpretation='same_pixel_footprint_consistency_not_physical_accuracy')
                        if adopt:
                            np.save(str(output_dir/'detail_occupancy_parallel_baseline.npy'),detail_occ)
                        else:
                            camera_info.update(accepted=False,reason='estimated_camera_failed_independent_mask_consistency_or_grid_bounds')
                    with open(str(output_dir/'camera_projection_estimate.json'),'w',encoding='utf8') as stream:
                        json.dump(camera_info,stream,ensure_ascii=False,indent=2)
                    if model is not None and adopt:
                        # Commit only after every candidate check and evidence
                        # save succeeded; an I/O failure keeps the old model.
                        detail_occ=candidate;detail_calibration=candidate_cal
                        report['detail_surface_source']='photo_estimated_perspective_silhouette_intersection_PS'
                        report['texture_projection']='same_estimated_perspective_camera_and_ray_occlusion'
                        report['parallel_baseline_occupancy_file']='detail_occupancy_parallel_baseline.npy'
                except Exception as exc:
                    report['camera_projection_estimate']=dict(version=43,accepted=False,
                        reason='camera_estimation_unavailable_parallel_baseline_retained',error=str(exc))
            report["detail_outer_margin_full_px"] = detail_margin
            report["detail_pl_repair_pixels128"] = int(repair_additions.sum())
            if report.get('uncertainty_scope',{}).get('added_unknown_pixels',0):
                if progress:progress.update('区分严格外壳与待定外壳')
                uncertainty_voxels,dependency = scope.dependency_mask(detail_occ,detail_voxel_mm,
                    masks_full,scope_source['conservative_surface_masks'],angles,detail_calibration)
                scope_info = report['uncertainty_scope']
                scope_info.update(dependency)
                scope_info.update(
                    display_semantics='photo_colors_preserved_uncertainty_saved_separately',
                    uncertainty_display_version=UNCERTAINTY_DISPLAY_VERSION,
                    PL_volume_unchanged=True, inferred_material_voxels=0,
                    possible_envelope_grid_boundary_contact=bool(detail_occ[-1].any() or
                        detail_occ[:,0].any() or detail_occ[:,-1].any() or
                        detail_occ[:,:,0].any() or detail_occ[:,:,-1].any()),
                    uncertain_voxels_file='1024_uncertain_voxels_v69.npz',
                    voxel_mask_coordinate_frame='native_carving_grid')
                scope.save_voxels(output_dir/scope_info['uncertain_voxels_file'], uncertainty_voxels)
            report["detail_surface_volume_cm3"] = float(
                np.count_nonzero(detail_occ) * detail_voxel_mm ** 3 / 1000.0)
            report["detail_surface_volume_minus_pl_cm3"] = float(
                report["detail_surface_volume_cm3"] - volume_cm3)
            np.save(str(output_dir / "detail_occupancy.npy"), detail_occ)
            report["detail_occupancy_file"] = "detail_occupancy.npy"
            colors = None
            observed = None
            face_data = None
            report['detail_attributes_requested']=bool(detail_attributes)
            if detail_attributes and "color_frames565" in scan:
                try:
                    if progress:progress.update("表面可见性与照片取色")
                    colors, assigned, observed, color_diagnostics, face_data = colorize_surface_from_views(
                        detail_occ, detail_voxel_mm, scan["color_frames565"],
                        detail_masks, angles, detail_calibration, return_valid=True,
                        reference565=scan.get('empty_color_frame565'),return_diagnostics=True,return_faces=True)
                    report['surface_color_fix_version']=int(color_diagnostics.get('implementation_version',SURFACE_COLOR_FIX_VERSION))
                    report['surface_color_diagnostics']=color_diagnostics
                    report['texture_white_balance']=color_diagnostics['white_balance']
                    if assigned == 0:
                        raise ValueError("没有表面点匹配到彩色原图")
                    report["detail_color_assigned_points"] = int(assigned)
                    report["detail_surface_colored"] = True
                except Exception as exc:
                    colors = None
                    observed = None
                    face_data = None
                    report["detail_color_error"] = str(exc)
                    print("彩色点云未生成：%s；保存无色轮廓点云" % exc)
            # An optional sparse photo-depth check is independent of PL's
            # pixel classifier. It never rewrites the raw masks or PL volume.
            # A silhouette hull is not treated as measured surface depth.
            report['photo_geometry_audit']=dict(version=PHOTO_GEOMETRY_AUDIT_VERSION,
                performed=False,reason='missing_confirmed_photo_direction_or_evidence',added_surface_voxels=0)
            if (face_data is not None and
                    color_diagnostics.get('depth_direction_selection',{}).get('confirmed') and
                    all(k in scan for k in ('empty_color_frame565','background_noise_sigma','photo_strong_masks')) and
                    'turntable_camera' not in detail_calibration):
                try:
                    import importlib, photo_geometry
                    if Path(photo_geometry.__file__).resolve().parent!=Path(__file__).resolve().parent:
                        raise RuntimeError('photo_geometry.py 与 scan_pipeline.py 必须在同一目录')
                    photo_geometry=importlib.reload(photo_geometry)
                    seg_info=json.loads(str(scan['segmentation_diagnostics_json'])) if 'segmentation_diagnostics_json' in scan else None
                    if progress:progress.update("稀疏三视角表面核验")
                    additions,audit=photo_geometry.audit_surface(detail_occ,detail_voxel_mm,
                        scan['color_frames565'],detail_masks,scan['photo_strong_masks'],angles,calibration,
                        scan['empty_color_frame565'],scan['background_noise_sigma'],
                        color_diagnostics['camera_depth_sign'],seg_info)
                    if additions.shape!=detail_occ.shape or additions.dtype!=bool or (additions&detail_occ).any():
                        raise RuntimeError('照片几何核验返回了无效的新增点')
                    audit['performed']=True
                    if additions.any():
                        proposal=detail_occ|additions
                        # A verified photo-depth observation can support color
                        # at that exact camera pixel, without rewriting the PL
                        # silhouettes used for carving or their saved evidence.
                        color_support=np.zeros_like(detail_masks,dtype=bool)
                        for item in audit['accepted_evidence']:
                            cu,cv=item['pixel_uv'];color_support[item['view'],cv,cu]=True
                        color_masks=detail_masks.astype(bool)|color_support
                        new_colors,new_assigned,new_observed,new_diag,new_faces=colorize_surface_from_views(
                            proposal,detail_voxel_mm,scan['color_frames565'],color_masks,angles,calibration,
                            reference565=scan['empty_color_frame565'],return_diagnostics=True,return_faces=True,
                            depth_direction_selection=color_diagnostics['depth_direction_selection'])
                        if new_diag['camera_depth_sign']!=color_diagnostics['camera_depth_sign']:
                            raise RuntimeError('照片几何候选改变了观察方向，保留原几何')
                        # Keep the baseline immutable. Candidate files are not
                        # referenced until every check and evidence save succeeds.
                        np.save(str(output_dir/'detail_occupancy_photo_refined.npy'),proposal)
                        np.save(str(output_dir/'photo_geometry_color_support.npy'),color_support)
                        audit['photo_color_support_pixels']=int(color_support.sum())
                    np.save(str(output_dir/'detail_photo_geometry_additions.npy'),additions)
                    audit['additions_sha256']=hashlib.sha256(additions.tobytes()).hexdigest()
                    with open(str(output_dir/'photo_geometry_evidence.json'),'w',encoding='utf8') as f:
                        json.dump(audit,f,ensure_ascii=False,indent=2)
                    if additions.any():
                        detail_occ=proposal
                        colors,assigned,observed,color_diagnostics,face_data=new_colors,new_assigned,new_observed,new_diag,new_faces
                        report['detail_color_assigned_points']=int(assigned)
                        report['surface_color_diagnostics']=color_diagnostics
                        report['detail_surface_volume_cm3']=float(detail_occ.sum()*detail_voxel_mm**3/1000.)
                        report['detail_surface_volume_minus_pl_cm3']=report['detail_surface_volume_cm3']-volume_cm3
                        report['detail_occupancy_before_photo_geometry_file']='detail_occupancy.npy'
                        report['detail_occupancy_file']='detail_occupancy_photo_refined.npy'
                    report['photo_geometry_audit']=audit
                    print('稀疏三视角几何核验：%d 个表面体素候选通过软件判据；原PL掩膜和体积保持不变' % audit['added_surface_voxels'])
                except Exception as exc:
                    report['photo_geometry_audit_error']=str(exc)
                    print('照片几何核验未采用：%s' % exc)
            if 'turntable_camera' in detail_calibration:
                report['photo_geometry_audit'].update(reason='legacy_sparse_depth_matcher_not_applied_to_new_perspective_camera')
            name = ("1015_细化轮廓彩色_v1.ply" if colors is not None
                    else "1015_细化轮廓表面_v1.ply")
            export_y_sign=model_export_y_sign(None if face_data is None else color_diagnostics)
            report['model_export_coordinate_version']=MODEL_EXPORT_COORDINATE_VERSION
            report['model_export_y_sign']=export_y_sign
            report['model_export_transform']=('reflect_y_reverse_triangle_winding' if export_y_sign==-1 else 'identity')
            report['occupancy_arrays_coordinate_frame']='native_carving_grid_before_export_transform'
            pl_export_y_sign=(int(detail_calibration['turntable_camera']['rotation_sign'])
                if 'turntable_camera' in detail_calibration else export_y_sign)
            report['pl_export_y_sign']=pl_export_y_sign
            if 'turntable_camera' in detail_calibration:
                report['occupancy_arrays_coordinate_frame']='PL_legacy_positive_angle_grid; PS_detail_estimated_right_handed_camera_world'
            if pl_export_y_sign==-1:
                # Keep the PL comparison point cloud in the same exported
                # frame, while saved occupancy arrays retain native indexing.
                save_ply(str(output_dir/'recon.ply'),occ[:,::-1,:],voxel_mm)
            detail_path = output_dir / name
            if progress:progress.update("批量导出表面点云")
            count = save_surface_ply(detail_path, detail_occ, detail_voxel_mm,
                                     colors=colors,export_y_sign=export_y_sign)
            try:
                mesh_name = ("1015_细化彩色三角网格_v1.ply" if colors is not None
                             else "1015_细化轮廓三角网格_v1.ply")
                mesh_path = output_dir / mesh_name
                mesh_metadata=dict(surface_color_version=SURFACE_COLOR_FIX_VERSION,
                    model_export_coordinate_version=MODEL_EXPORT_COORDINATE_VERSION,
                    export_y_sign=export_y_sign,
                    photo_geometry_audit_version=PHOTO_GEOMETRY_AUDIT_VERSION,
                    photo_geometry_added_voxels=report['photo_geometry_audit']['added_surface_voxels'],
                    masks_sha256=hashlib.sha256(np.asarray(detail_masks,dtype=np.uint8).tobytes()).hexdigest(),
                    angles_sha256=hashlib.sha256(np.asarray(angles,dtype='<f8').tobytes()).hexdigest())
                if int(scan.get('interior_evidence_version',0))>=53:
                    mesh_metadata.update(interior_evidence_version=53,
                        surface_kind='conservative_visual_hull_not_measured_material',
                        unresolved_true_holes_may_be_closed=True,
                        strict_masks_sha256=hashlib.sha256(np.asarray(masks_full,dtype=np.uint8).tobytes()).hexdigest(),
                        interior_unknown_masks_sha256=hashlib.sha256(np.asarray(scan['interior_unknown_masks'],dtype=np.uint8).tobytes()).hexdigest())
                if uncertainty_voxels is not None:
                    mesh_name = ('1015_照片与待定外壳三角网格_v72.ply' if colors is not None
                                 else '1015_待定外壳轮廓三角网格_v72.ply')
                    if report.get('source_evidence_review',{}).get('added_uncertainty_pixels',0):
                        mesh_name=mesh_name.replace('_v72.ply','_v75.ply')
                    if int(scan.get('region_foreground_version',0))>=76:
                        mesh_name=mesh_name.replace('_v75.ply','_v76.ply')
                    mesh_path = output_dir/mesh_name
                    mesh_metadata.update(mask_foreground_review_version=int(scan.get('region_foreground_version',0)),
                        uncertainty_scope_version=report['uncertainty_scope']['version'],
                        source_evidence_version=report.get('source_evidence_review',{}).get('version'),
                        source_evidence_strict_masks_changed=False,
                        source_evidence_surface_masks_file=report['uncertainty_scope'].get('surface_masks_file'),
                        uncertainty_display_version=UNCERTAINTY_DISPLAY_VERSION,
                        uncertainty_color_overlay_enabled=False,
                        observed_photo_colors_preserved=True,
                        unobserved_faces_remain_uncolored_or_gray=True,
                        uncertainty_is_not_measured_material=True,
                        uncertainty_voxels_file=report['uncertainty_scope']['uncertain_voxels_file'],
                        newly_retained_unknown_voxels=int(uncertainty_voxels.sum()),
                        total_uncertain_voxels=int(report['uncertainty_scope']['uncertain_voxels']),
                        source_upper_masks_sha256=report['uncertainty_scope']['source_upper_sha256'])
                if 'turntable_camera' in detail_calibration:
                    mesh_metadata.update(camera_projection_version=43,capture_rotation_sign=detail_calibration['turntable_camera']['rotation_sign'],
                        camera_model_source='same_circle_photo_estimate_not_physical_calibration')
                if "color_frames565" in scan:
                    mesh_metadata['photos_sha256']=hashlib.sha256(np.asarray(scan['color_frames565'],dtype='<u2').tobytes()).hexdigest()
                if face_data is not None:
                    mesh_metadata['camera_depth_sign']=face_data['diagnostics']['camera_depth_sign']
                    mesh_metadata['surface_color_version']=int(color_diagnostics.get('implementation_version',SURFACE_COLOR_FIX_VERSION))
                if report['photo_geometry_audit'].get('performed'):
                    mesh_metadata['photo_geometry_additions_sha256']=report['photo_geometry_audit']['additions_sha256']
                if colors is not None:
                    color_display = dict(version=VOXEL_COLOR_DISPLAY_VERSION,
                        policy='same_voxel_representative_photo_color',
                        color_source='existing_selected_photo_color_of_same_voxel',
                        same_color_on_all_exposed_faces=True,
                        unseen_face_color_is_measured=False,
                        neighbor_color_propagation=False,geometry_modified=False,
                        photo_colored_surface_voxels=int(np.count_nonzero(observed)),
                        gray_surface_voxels=int(len(observed)-np.count_nonzero(observed)))
                    report['detail_mesh_color_display']=color_display
                    mesh_metadata.update(voxel_color_display_version=VOXEL_COLOR_DISPLAY_VERSION,
                        mesh_color_policy=color_display['policy'],
                        same_color_on_all_exposed_faces=True,
                        unseen_face_color_is_measured=False,
                        voxels_without_photo_color_remain_gray=True,
                        point_photo_colors_preserved=True,direct_face_photo_colors_preserved=False)
                    if 'observed_photo_colors_preserved' in mesh_metadata:
                        mesh_metadata['observed_photo_colors_preserved']=False
                    mesh_metadata.pop('unobserved_faces_remain_uncolored_or_gray',None)
                report['detail_mesh_metadata']=mesh_metadata
                if progress:progress.update("导出彩色三角网格")
                mesh_vertices, mesh_faces = save_surface_mesh_ply(
                    mesh_path, detail_occ, detail_voxel_mm,
                    colors=colors, color_valid=observed, preserve_color_boundaries=True,
                    face_colors=None if face_data is None else face_data['face_colors'],
                    face_valid=None if face_data is None else face_data['face_valid'],metadata=mesh_metadata,
                    export_y_sign=export_y_sign, uncertain_voxels=uncertainty_voxels,
                    uniform_voxel_color=colors is not None)
                report["detail_mesh_file"] = mesh_name
                report["detail_mesh_vertices"] = int(mesh_vertices)
                report["detail_mesh_faces"] = int(mesh_faces)
            except Exception as exc:
                report["detail_mesh_error"] = str(exc)
                print("细化三角网格未生成：%s；表面点云保留" % exc)
            report["detail_surface_file"] = detail_path.name
            report["detail_surface_points"] = int(count)
            report["detail_surface_voxel_mm"] = float(detail_voxel_mm)
            report["point_cloud_export_version"] = 56
            report["point_cloud_format"] = "binary_little_endian_float32"
            report["detail_surface_runtime_s"] = float(time.monotonic() - started)
            print("PS 全分辨率轮廓表面：%d 点，采样 %.3f mm，耗时 %.1f s" %
                  (count, detail_voxel_mm, report["detail_surface_runtime_s"]))
            if colors is not None:
                print("照片颜色对应 %d/%d 个表面点；只是近似投影外观" %
                      (report["detail_color_assigned_points"], count))
        except Exception as exc:
            report["detail_surface_error"] = str(exc)
            print("PS 细化表面未生成：%s；PL 体积及原点云保留" % exc)
    report['shape_consistency'] = None
    report['opposite_view_consistency'] = opposite_silhouette_consistency(
        masks_full, angles, calibration['axis_col'])
    report['opposite_view_consistency']['applicable_to_selected_camera']=('turntable_camera' not in detail_calibration)
    if 'turntable_camera' in detail_calibration:
        report['opposite_view_consistency']['interpretation']='legacy_parallel_diagnostic; mirror_equality_not_required_by_perspective_camera'
    report['segmentation_sensitivity'] = None
    report['physical_accuracy_verified'] = False
    report['frame_pixel_order'] = str(scan.get('frame_pixel_order','legacy_unspecified'))
    report['calibration_frame_pixel_order'] = str(calibration.get('frame_pixel_order','legacy_unspecified'))
    report['frame_order_fix_version'] = FRAME_ORDER_FIX_VERSION
    if progress:progress.update("最终形状一致性与不确定性核验")
    if adaptive:
        try:
            check_occ = detail_occ if report.get('detail_surface_file') else occ
            check_pitch = detail_voxel_mm if report.get('detail_surface_file') else voxel_mm
            report['shape_consistency'] = silhouette_reprojection_check(
                check_occ, check_pitch, masks_full, angles, detail_calibration)
            strong128 = prepare_scan_masks(scan['photo_strong_masks'],
                calibration['mm_per_px'],calibration['axis_col'],calibration['v_ground'],
                roi=roi,out_size=recon.MW)[0]
            lower = multiview_support_counts(strong128,angles,recon.N,voxel_mm,
                                             mm_per_px128,axis128,ground128)==len(angles)
            lower &= occ
            uncertain = occ & ~lower
            np.save(str(output_dir/'uncertain_occupancy128.npy'),uncertain)
            np.save(str(output_dir/'photo_strong_masks.npy'),scan['photo_strong_masks'])
            np.save(str(output_dir/'photo_unknown_masks.npy'),scan['photo_unknown_masks'])
            save_mask_sheet(scan['photo_unknown_masks'],output_dir/'photo_unknown_masks.png')
            report['segmentation_sensitivity'] = {
                'strong_only_volume_cm3':float(lower.sum()*voxel_mm**3/1000.),
                'connected_uncertainty_volume_cm3':float(occ.sum()*voxel_mm**3/1000.),
                'uncertain_voxels':int(uncertain.sum()),
                'uncertain_fraction':float(uncertain.sum()/max(1,occ.sum())),
                'interpretation':'segmentation_sensitivity_not_true_volume_confidence_interval',
                'hole_filling_used':False}
        except Exception as exc:
            report['evidence_validation_error'] = str(exc)
    report['shape_consistency_passed'] = (
        report['shape_consistency']['minimum_coverage'] >= .9
        if report.get('shape_consistency') else None)
    report['reconstruction_quality_status'] = (
        'needs_review' if report.get('interior_evidence',{}).get('unknown_pixels',0) or report.get('roi_boundary_requires_review') or report.get('evidence_validation_error') or
        report.get('shape_consistency_passed') is False or
        (report['opposite_view_consistency']['requires_review'] and report['opposite_view_consistency'].get('applicable_to_selected_camera',True)) or
        (report.get('segmentation_sensitivity') and
         report['segmentation_sensitivity']['uncertain_fraction'] > .2)
        else 'consistency_checks_passed_not_physical_accuracy_verified' if adaptive
        else 'no_photo_evidence_validation')
    with open(str(output_dir / "result.json"), "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    if make_preview:
        try:
            show_voxels(occ, voxel_mm=voxel_mm,
                        save_png=str(output_dir / "recon_preview.png"))
        except RuntimeError as exc:
            print("三维 PNG 未生成：%s；可在电脑打开 recon.ply" % exc)
    print("体积 %.2f cm³；结果保存在 %s" % (volume_cm3, output_dir))
    print("占据体素外接包围范围 X=%.2f、Y=%.2f、Z=%.2f mm；PLY 点中心跨度各少 1 格" %
          (dimensions["voxel_extent_mm"]["x"],
           dimensions["voxel_extent_mm"]["y"],
           dimensions["voxel_extent_mm"]["z"]))
    if dimensions["bottom_gap_mm"] > 0:
        print("注意：占据体素底部离标定台面 %.2f mm；从台面到最高体素为 %.2f mm，请核对台面行" %
              (dimensions["bottom_gap_mm"], dimensions["table_to_top_mm"]))
    return occ, report


SINGLE_CIRCLE_FLOW_VERSION = 71


def capture_color_views_demo(grab_fn, period_s, directory, n_views=24):
    """Capture one real circle, with the same UI callback as PL processing."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with _SegmentationProgress(directory, backend='capture', version=71) as progress:
        counter = [0]

        def reader():
            value = grab_fn()
            counter[0] += 1
            frame = '%d/%d' % (counter[0], n_views) if counter[0] <= n_views else '首尾帧'
            progress.update('彩色原图采集', frame)
            return value

        for key, default in (('frame_pixel_order', 'unspecified'), ('frame_reader_version', 0)):
            setattr(reader, key, getattr(grab_fn, key, default))
        with (directory/'capture_log.txt').open('w', encoding='utf8', buffering=1) as log:
            with redirect_stdout(log), redirect_stderr(log):
                return capture_color_views(reader, period_s, n_views=n_views,
                    out_path=directory/'1014_color24_v1.npz',
                    preview_path=directory/'1014_color_sheet_v1.png')


def capture_views_demo(grab_fn, period_s, log_path, **kwargs):
    """演示模式：采集和校验照常运行，逐帧记录写进文件。"""
    with open(str(log_path), "w", encoding="utf-8") as log:
        with redirect_stdout(log), redirect_stderr(log):
            return capture_views(grab_fn, period_s, **kwargs)



class _ReconstructionProgress(_SegmentationProgress):
    """One status area and a heartbeat file, including before the first PL call."""
    def __init__(self,directory):
        super().__init__(directory,backend='reconstruction',version=55)
        self.path=Path(directory)/'reconstruction_progress.json'
        self.save_lock=threading.Lock()
    def _save(self):
        try:
            with self.save_lock:
                self.path.parent.mkdir(parents=True,exist_ok=True)
                temporary=self.path.with_suffix('.tmp')
                temporary.write_text(json.dumps(dict(self._snapshot(),events=self.events),ensure_ascii=False,indent=2),encoding='utf8')
                temporary.replace(self.path)
        except OSError:pass
    def __enter__(self):
        try:
            from IPython import get_ipython
            from IPython.display import display
            if get_ipython() is not None:self.handle=display('',display_id=True)
        except Exception:pass
        self.update('入口与输入验证')
        self.worker=threading.Thread(target=self._heartbeat)
        self.worker.daemon=True;self.worker.start()
        return self
    def _heartbeat(self):
        while not self.stop.wait(2.):
            self._save();self._render()
    def _render(self):
        if self.handle is None:return
        v=self._snapshot()
        try:
            with self.render_lock:
                self.handle.update('%s｜%s｜本阶段 %.0f 秒，总计 %.0f 秒' %
                    (v['status'],v['stage'],v['stage_elapsed_s'],v['elapsed_s']))
        except Exception:pass
    def update(self,stage,frame=None):
        super().update(stage,frame)
        print('[第8步阶段] %s' % stage,flush=True)
    def __exit__(self,kind,value,tb):
        self.stop.set()
        if self.worker is not None:self.worker.join(timeout=1.)
        status='completed' if kind is None else 'interrupted' if issubclass(kind,KeyboardInterrupt) else 'failed'
        self.events.append(self._snapshot())
        self.state=dict(self.state,status=status,error='' if value is None else str(value))
        if kind is None:self.state['stage']='结果保存完成'
        self._save();self._render()
        print('[第8步结束] %s' % status,flush=True)


def reconstruct_demo(scan, calibration, overlay, output_dir, geometry_only=False):
    """演示模式：完整诊断仍运行，只在 Notebook 呈现简短结论。"""
    if float(scan.get("closure_iou", 0.0)) < 0.8:
        raise RuntimeError("首尾轮廓差别较大，请先检查转速、物体松动和掩膜")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(str(output_dir / "run_log.txt"), "w", encoding="utf-8", buffering=1) as log:
        with redirect_stdout(log), redirect_stderr(log):
            print('第8步已进入；阶段记录实时刷新。',flush=True)
            with _ReconstructionProgress(output_dir) as progress:
                _, report = reconstruct_scan(
                    scan, calibration, overlay, output_dir,
                    horizontal_margin_px=0, fill_enclosed_holes=False,
                    multiview_diagnostics=True,
                    make_preview=False,progress=progress,detail_attributes=not geometry_only)
    notes = []
    if report.get('uncertainty_scope',{}).get('added_unknown_pixels',0):
        notes.append('待定范围另存诊断，主模型保留照片外观；不代表测得了材料或缺口深度')
    if report.get('interior_evidence',{}).get('unknown_pixels',0):
        notes.append('模型保守保留内部不确定区域，真实通孔可能被封闭；PL体积仍使用原始掩膜')
    if report.get('roi_boundary_requires_review'):
        notes.append('已排除与主体分离的边界噪声规模小块，入画完整性仍需核对')
    if report.get('evidence_validation_error'):
        notes.append('分割证据核对未完成')
    if report.get('shape_consistency') and report['shape_consistency']['minimum_coverage'] < .9:
        notes.append('部分实拍轮廓无法由模型解释，形状一致性未通过')
    if report.get('segmentation_sensitivity') and report['segmentation_sensitivity']['uncertain_fraction'] > .2:
        notes.append('体积对分割不确定区域较敏感')
    if report.get("auto_error") or report.get("auto_raw_pl_mismatch_voxels"):
        notes.append("自动修补未采用")
    if report.get("multiview_verified") is False:
        notes.append("PL 投影核对未完成")
    if report.get("no_horizontal_margin_error"):
        notes.append("扩边对照未完成")
    if report.get("photo_color_evidence_error"):
        notes.append("彩照局部核验未完成")
    if (not report.get("photo_guided_repair") and
            report.get("auto_candidate_hole_pixels", 0)):
        notes.append("无同角度彩色原图，可疑白洞未自动填实")
    if report.get("detail_surface_error"):
        notes.append("细化轮廓点云未生成")
    if report.get("detail_mesh_error"):
        notes.append("细化三角网格未生成")
    if report.get("detail_photo_repair_error"):
        notes.append("全分辨率彩照修补未完成")
    if report.get('photo_geometry_audit_error'):
        notes.append('照片几何核验未采用，详见结果日志')
    elif report.get('photo_geometry_audit',{}).get('performed') and report['photo_geometry_audit'].get('internal_zero_pixels',0):
        if report['photo_geometry_audit'].get('added_surface_voxels',0)==0:
            notes.append('内部缺口未获得可靠多视角深度，未补成实体')
        else:
            notes.append('部分表面来自三视角照片估计，未验证物理精度')
    if (report.get("detail_surface_volume_minus_pl_cm3") is not None and
            abs(report["detail_surface_volume_minus_pl_cm3"]) >
            0.05 * report["volume_cm3"]):
        notes.append("可旋转模型与 PL 体积差超过 5%，应核对 result.json")
    if "color_frames565" in scan:
        if report.get("detail_color_error"):
            notes.append("彩色表面未生成")
        elif not report.get('surface_color_diagnostics',{}).get('depth_direction_selection',{}).get('confirmed',False):
            notes.append('照片不足以确定观察方向，颜色方向仍需核对')
        elif report.get("detail_surface_points") and \
                report.get("detail_color_assigned_points", 0) < \
                0.6 * report["detail_surface_points"]:
            notes.append("彩色表面有较多未匹配点")
    report['diagnostic_notes'] = notes
    with open(str(output_dir / 'result.json'), 'w', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return report, notes
