"""Bounded OV5640 SCCB register access and scan-wide camera parameter lock.

GPIO handover is permitted only after the existing hardware initialisation has
finished. Lock the actual sensor; never simulate fixed exposure by editing PNGs.
Python 3.6 compatible. Register definitions: OV5640 datasheet v2.03, 7.5 / 7.22.
"""
import json
import time
import hashlib
from pathlib import Path

import numpy as np

IMPLEMENTATION_VERSION = 20
EXPOSURE_PAIR_VERSION = 24


class SCCB:
    def __init__(self, overlay, timeout_s=2.):
        self.overlay = overlay
        if not overlay.is_loaded():
            raise RuntimeError('摄像头bit已被替换，请重新运行初始化格')
        try:
            self.io = overlay.camera_control.mmio
        except AttributeError as exc:
            raise RuntimeError('当前bit没有摄像头参数控制接口；需要本包v20 bit/hwh') from exc
        deadline = time.monotonic()+float(timeout_s)
        while not (self.io.read(8) & 2):
            if time.monotonic() > deadline:
                raise RuntimeError('摄像头硬件初始化未完成，不能接管SCCB')
            time.sleep(.01)
        self.control = 1  # ownership, SCL high, SDA released
        self.io.write(0, self.control)
        self._pause()
        if not self._sample():
            raise RuntimeError('摄像头SCCB数据线未释放；停止操作')

    @staticmethod
    def _pause():
        time.sleep(.00005)  # Below the sensor maximum bus speed; no speed promise.

    def _set(self, scl_low=None, sda_low=None):
        value = self.control
        if scl_low is not None:
            value = (value & ~2) | (2 if scl_low else 0)
        if sda_low is not None:
            value = (value & ~4) | (4 if sda_low else 0)
        self.control = value
        self.io.write(0, value)
        self._pause()

    def _sample(self):
        return bool(self.io.read(8) & 1)

    def _start(self):
        self._set(scl_low=False, sda_low=False)
        self._set(sda_low=True)
        self._set(scl_low=True)

    def _stop(self):
        self._set(scl_low=True, sda_low=True)
        self._set(scl_low=False)
        self._set(sda_low=False)

    def _write_byte(self, value):
        for shift in range(7, -1, -1):
            self._set(scl_low=True, sda_low=not bool(value & (1 << shift)))
            self._set(scl_low=False)
            self._set(scl_low=True)
        self._set(sda_low=False)
        self._set(scl_low=False)
        acknowledged = not self._sample()
        self._set(scl_low=True)
        if not acknowledged:
            raise RuntimeError('OV5640未应答SCCB字节0x%02x，不能确认相机参数已固定' % value)

    def _read_byte(self):
        value = 0
        self._set(sda_low=False)
        for _ in range(8):
            self._set(scl_low=False)
            value = (value << 1) | int(self._sample())
            self._set(scl_low=True)
        # NACK terminates the one-byte read.
        self._set(sda_low=False)
        self._set(scl_low=False)
        self._set(scl_low=True)
        return value

    def write(self, register, value):
        try:
            self._start()
            for byte in (0x78, register >> 8, register & 255, value):
                self._write_byte(byte)
        finally:
            self._stop()

    def read(self, register):
        try:
            self._start()
            for byte in (0x78, register >> 8, register & 255):
                self._write_byte(byte)
            # SCCB supports the two separate address/read phases with STOP.
            self._stop()
            self._start()
            self._write_byte(0x79)
            return self._read_byte()
        finally:
            self._stop()


def _snapshot(bus):
    addresses = [0x3500, 0x3501, 0x3502, 0x350a, 0x350b,
                 0x519f, 0x51a0, 0x51a1, 0x51a2, 0x51a3, 0x51a4]
    values = {a: bus.read(a) for a in addresses}
    return dict(exposure_lines_x16=((values[0x3500] & 15) << 16) |
                (values[0x3501] << 8) | values[0x3502],
                gain_x16=((values[0x350a] & 3) << 8) | values[0x350b],
                awb_gains=[((values[a] & 15) << 8) | values[a+1] for a in (0x519f, 0x51a1, 0x51a3)],
                registers_hex={'%04x' % a: value for a, value in values.items()})


def _same_values(a, b):
    return all(a[k] == b[k] for k in ('exposure_lines_x16', 'gain_x16', 'awb_gains'))


def lock_for_scan(overlay, out_path=None):
    """Run in existing step 1, before saving ANY calibration photo."""
    bus = SCCB(overlay)
    sensor = (bus.read(0x300a) << 8) | bus.read(0x300b)
    if sensor != 0x5640:
        raise RuntimeError('摄像头ID不是OV5640（读到0x%04x），停止写相机设置' % sensor)
    original_aec, original_awb = bus.read(0x3503), bus.read(0x5196)
    already_locked = (original_aec & 3) == 3 and bool(original_awb & 32)
    converged = False
    if not already_locked:
        time.sleep(1.)
        deadline = time.monotonic()+3.
        previous = _snapshot(bus)
        while time.monotonic() < deadline:
            time.sleep(.15)
            current = _snapshot(bus)
            # Observe convergence; do not invent fixed exposure/gain constants.
            old = [previous['exposure_lines_x16'], previous['gain_x16']]+previous['awb_gains']
            new = [current['exposure_lines_x16'], current['gain_x16']]+current['awb_gains']
            if all(abs(x-y) <= max(1., .02*max(x, y)) for x, y in zip(old, new)):
                converged = True
                break
            previous = current
        try:
            bus.write(0x3503, original_aec | 3)  # manual AEC and AGC; preserve other bits
            bus.write(0x5196, original_awb | 32)  # AWB FREEZE, preserve converged gains
            if (bus.read(0x3503) & 3) != 3 or not (bus.read(0x5196) & 32):
                raise RuntimeError('摄像头锁定控制位读回不符')
        except BaseException:
            # Do not leave an unreported half-applied configuration.
            try:
                bus.write(0x3503, original_aec)
                bus.write(0x5196, original_awb)
            except Exception:
                pass
            raise
    time.sleep(.4)
    first = _snapshot(bus)
    time.sleep(.15)
    final = _snapshot(bus)
    if not _same_values(first, final):
        raise RuntimeError('相机已写锁定位，但曝光/增益/白平衡读数仍变化；停止拍标定照片')
    if final['exposure_lines_x16'] == 0 or final['gain_x16'] == 0:
        raise RuntimeError('相机曝光或增益读数为零，不能确认有效锁定')
    info = dict(final, sensor_id='0x5640', implementation_version=20,
                aec_agc_control=bus.read(0x3503), awb_control=bus.read(0x5196),
                already_locked=already_locked, observed_auto_convergence=converged,
                readback_verified=True, sampling_before_calibration=True,
                geometry_accuracy_verified=False, brightness_of_scene_guaranteed=False)
    if out_path is not None:
        Path(out_path).write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding='utf-8')
    print('曝光、增益和白平衡已固定并核对；本次标定和扫描沿用这些参数。', flush=True)
    if not converged and not already_locked:
        print('自动参数此前仍有波动；已冻结当前值，请核对原图是否过亮或过暗。', flush=True)
    return info


def verify_for_scan(overlay, settings):
    bus = SCCB(overlay)
    if (bus.read(0x3503) & 3) != 3 or not (bus.read(0x5196) & 32):
        raise RuntimeError('摄像头自动参数被重新启用，不能混用先前背景；请从初始化重新运行')
    current = _snapshot(bus)
    if not _same_values(current, settings):
        raise RuntimeError('摄像头参数与标定照片时不一致；请从初始化重新运行')
    return current


def _quality(frame):
    """Measure RGB565 endpoint clipping; this does not assess object geometry."""
    frame = np.asarray(frame)
    if frame.dtype != np.uint16 or frame.shape != (480, 800):
        raise ValueError('曝光检查需要480x800 uint16原图')
    a = frame[:, 160:640]
    rgb = np.stack(((a >> 11) & 31, (a >> 5) & 63, a & 31), axis=-1)
    saturated = (rgb == np.array([31, 63, 31])).any(axis=-1)
    return dict(any_channel_saturated_fraction=float(saturated.mean()),
                all_channels_saturated_fraction=float((a == 65535).mean()),
                green_p99=float(np.percentile(rgb[..., 1], 99)),
                acceptable_clipping=bool(saturated.mean() <= .005),
                roi_xyxy=[160, 0, 640, 480], max_saturated_fraction=.005,
                physical_accuracy_verified=False)


def _set_exposure(bus, settings):
    """Only shorten manual exposure; retain actual gain and frozen AWB."""
    if (bus.read(0x3503) & 3) != 3 or not (bus.read(0x5196) & 32):
        raise RuntimeError('曝光配对需要已锁定的摄像头，停止改变参数')
    exposure = int(settings['exposure_lines_x16'])
    if not 16 <= exposure <= 0xfffff or exposure & 15:
        raise ValueError('曝光必须为有效的整行值')
    # Preserve the documented reserved high nibble of 3500.
    bus.write(0x3500, (bus.read(0x3500) & 0xf0) | ((exposure >> 16) & 15))
    bus.write(0x3501, (exposure >> 8) & 255)
    bus.write(0x3502, exposure & 255)
    current = _snapshot(bus)
    if not _same_values(current, settings):
        raise RuntimeError('曝光写入后读回不符；不能继续使用旧背景')
    return dict(settings, **current)


def _settled_frame(grab_fn):
    # Bounded settling/discard, not a sensor frame-completion guarantee.
    time.sleep(.35)
    grab_fn()
    time.sleep(.08)
    frame = np.asarray(grab_fn()).copy()
    _quality(frame)
    return frame


def _fingerprint(frame):
    return hashlib.sha256(np.asarray(frame).astype('<u2', copy=False).tobytes()).hexdigest()


def capture_exposure_backgrounds(overlay, grab_fn, settings, out_path):
    """Existing step 1: acquire real no-table references at bounded exposures."""
    verify_for_scan(overlay, settings)
    bus = SCCB(overlay)
    base = int(settings['exposure_lines_x16'])
    # Nine real exposure levels, at most eight stops below the current setting.
    exposures = sorted(set(max(16, ((base >> k) // 16) * 16) for k in range(9)), reverse=True)
    frames, configs = [], []
    original = dict(settings)
    # OV5640 only supports whole-line exposure. The source setting is expected
    # to be whole-line too; reject unsupported values rather than silently round.
    if base & 15 or base < 16:
        raise ValueError('当前曝光不是有效整行值，不能建立可靠的配对背景')
    try:
        for exposure in exposures:
            config = dict(settings, exposure_lines_x16=exposure)
            config = _set_exposure(bus, config)
            frame = _settled_frame(grab_fn)
            # Catch changes during settling, not just before capture.
            if not _same_values(_snapshot(bus), config):
                raise RuntimeError('采背景时摄像头参数改变，停止建立配对')
            frames.append(frame); configs.append(config)
    finally:
        _set_exposure(bus, original)
    if len(frames) > 1 and all(np.array_equal(frames[0], f) for f in frames[1:]):
        raise RuntimeError('不同曝光下原始帧完全相同，无法确认摄像头或DDR更新；停止使用这组背景')
    bank = dict(frames565=np.stack(frames), settings=configs,
                frame_sha256=[_fingerprint(f) for f in frames], version=24)
    from scan_pipeline import _atomic_npz
    _atomic_npz(out_path, frames565=bank['frames565'],
                settings_json=json.dumps(configs, ensure_ascii=False),
                frame_sha256=np.asarray(bank['frame_sha256']), exposure_pair_version=24)
    print('已自动保存%d档真实曝光背景；下一步将为空转台选择合适的一档。' % len(frames), flush=True)
    return bank


def select_exposure_pair(overlay, grab_fn, bank, out_path=None):
    """Existing step 2: choose brightest unclipped pair; never synthesize a ref."""
    frames = np.asarray(bank['frames565'])
    configs, hashes = bank['settings'], bank['frame_sha256']
    if bank.get('version') != 24 or frames.dtype != np.uint16 or frames.ndim != 3 or \
            frames.shape[1:] != (480, 800) or not 1 <= len(frames) <= 9 or \
            len(configs) != len(frames) or len(hashes) != len(frames):
        raise ValueError('曝光背景配对无效，请从第1步重新采集')
    for frame, expected in zip(frames, hashes):
        if _fingerprint(frame) != expected:
            raise ValueError('曝光背景指纹不符，不能混用参考照片')
    exposures = [int(s['exposure_lines_x16']) for s in configs]
    if exposures != sorted(set(exposures), reverse=True):
        raise ValueError('曝光背景顺序或重复参数无效')
    first = configs[0]
    for s in configs:
        if s['gain_x16'] != first['gain_x16'] or s['awb_gains'] != first['awb_gains']:
            raise ValueError('曝光背景的增益或白平衡不一致，不能配对')
    bus = SCCB(overlay)
    original = dict(first, **_snapshot(bus))
    # Only switch exposure; if another parameter changed, do not silently fix it.
    if original['gain_x16'] != first['gain_x16'] or original['awb_gains'] != first['awb_gains']:
        raise RuntimeError('相机增益或白平衡与背景组不同，请从第1步重拍')
    attempts, selected = [], None
    try:
        for index, (background, config) in enumerate(zip(frames, configs)):
            actual = _set_exposure(bus, config)
            table = _settled_frame(grab_fn)
            if not _same_values(_snapshot(bus), actual):
                raise RuntimeError('采空台时摄像头参数改变，停止配对')
            quality = _quality(table); background_quality = _quality(background)
            attempts.append(dict(index=index, exposure_lines_x16=exposures[index],
                                 table=quality, no_table=background_quality))
            # Both calibration photos must retain bright information.
            if quality['acceptable_clipping'] and background_quality['acceptable_clipping']:
                selected = (index, actual, table)
                break
        if selected is None:
            raise RuntimeError('曝光候选组内未找到不过曝的背景配对；诊断已保存，不能继续标定')
        index, actual, table = selected
        if _quality(table)['green_p99'] < 8:
            raise RuntimeError('配对画面过暗，无法确认有效曝光；停止标定')
        actual.update(exposure_pair_version=24, exposure_pair_index=index,
                      matched_no_table_sha256=hashes[index],
                      exposure_selected_on_empty_scene=True,
                      exposure_pair_attempts=attempts,
                      frame_synchronization_verified=False,
                      exposure_selected_does_not_guarantee_object_quality=True)
        if out_path is not None:
            Path(out_path).write_text(json.dumps(actual,ensure_ascii=False,indent=2),encoding='utf-8')
        print('曝光配对已选定：空台饱和像素 %.2f%%，沿用同档真实背景。' %
              (100*_quality(table)['any_channel_saturated_fraction']), flush=True)
        return frames[index].copy(), table, actual
    except BaseException:
        # Restore the setting present on entry; never leave a half-selected bank.
        _set_exposure(bus, original)
        if out_path is not None:
            failure = dict(exposure_pair_version=24, exposure_selection_failed=True, attempts=attempts)
            Path(out_path).with_name('1016_exposure_selection_failed_v24.json').write_text(
                json.dumps(failure,ensure_ascii=False,indent=2),encoding='utf-8')
        raise
