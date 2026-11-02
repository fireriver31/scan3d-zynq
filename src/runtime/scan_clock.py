"""Plan FCLK0 dividers from the running PS clock, before Overlay.download.

No clock/MMIO writes here: the original Overlay.download owns programming.
Works with legacy PYNQ metaclass registers and newer clock backends.
"""
import copy
import json
import math
from pathlib import Path

VERSION = 98
TARGET_MHZ = 50.0


def _source_mhz(clocks):
    # Legacy PYNQ 2.x exposes these on the Clocks metaclass.
    getter = getattr(clocks, '_get_src_clk_mhz', None)
    registers = getattr(clocks, 'PL_CLK_CTRLS', None)
    field = getattr(clocks, 'PL_CLK_SRC_FIELD', None)
    if callable(getter) and registers is not None and field is not None:
        selected = int(registers[0][field])
        return float(getter(selected)), selected, 'legacy_clocks_metaclass'
    # PYNQ 3.x delegates to a backend instance with RegisterMap fields.
    backend = getattr(clocks, '_instance', None)
    if backend is not None and callable(getattr(backend, '_get_src_clk_mhz', None)):
        selected = int(backend.PL_CLK_CTRLS[0].SRCSEL)
        return float(backend._get_src_clk_mhz(selected)), selected, 'clocks_backend'
    raise RuntimeError('不能读取当前PS时钟源，未猜测分频或修改时钟；请保留此报错')


def prepare(overlay, clocks, target_mhz=TARGET_MHZ):
    """Only update the not-yet-downloaded overlay's clock metadata."""
    source, selected, backend = _source_mhz(clocks)
    before = float(clocks.fclk0_mhz)
    target_mhz = float(target_mhz)
    if not all(math.isfinite(v) and v > 0 for v in (source, before, target_mhz)):
        raise ValueError('PS时钟读数无效；未下载Overlay或修改时钟')
    mapping = getattr(overlay, 'clock_dict', None)
    if not isinstance(mapping, dict) or 0 not in mapping or not isinstance(mapping[0], dict):
        raise RuntimeError('Overlay没有FCLK0时钟描述，未修改时钟或下载硬件')
    original = copy.deepcopy(mapping[0])
    old0, old1 = int(original.get('divisor0', 0)), int(original.get('divisor1', 0))
    if not 1 <= old0 <= 63 or not 1 <= old1 <= 63:
        raise ValueError('HWH中的FCLK0分频无效，未下载Overlay')
    # Prefer the original pair when already exact, then the smallest change.
    choices = [(abs(source/(d0*d1)-target_mhz), abs(d0-old0)+abs(d1-old1),
                abs(d0-d1), d0, d1) for d0 in range(1, 64) for d1 in range(1, 64)]
    _, _, _, div0, div1 = min(choices)
    expected = source/(div0*div1)
    if abs(expected-target_mhz) > target_mhz*.001:
        raise RuntimeError('当前PS时钟源不能分出要求的50 MHz；未修改时钟或下载硬件')
    corrected = copy.deepcopy(original)
    corrected.update(enable=1, divisor0=div0, divisor1=div1)
    mapping[0] = corrected
    return dict(version=VERSION, target_fclk0_mhz=target_mhz,
        before_fclk0_register_mhz=before, source_register_mhz=source,
        source_select=selected, clock_api=backend, original_clock_dict=original,
        corrected_clock_dict=copy.deepcopy(corrected), planned_fclk0_mhz=expected,
        original_dividers_on_running_source_mhz=source/(old0*old1),
        preparation_wrote_clock_registers=False, bit_or_hwh_file_modified=False,
        physical_frequency_measured=False)


def verify(clocks, plan, out_path=None):
    actual = float(clocks.fclk0_mhz)
    valid = math.isfinite(actual) and abs(actual-plan['target_fclk0_mhz']) <= .05
    record = dict(plan, actual_fclk0_register_mhz=actual, passed=bool(valid),
        computed_display_clock_mhz=actual*24/36,
        computed_camera_xclk_mhz=actual*24/50,
        computed_axi_clock_mhz=actual*24/6,
        computed_clock_ratios_source='current_bit_MMCM_mul24_div1_outputs_div36_div50_div6',
        screen_image_confirmed=False)
    if out_path is not None:
        Path(out_path).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf8')
    if not valid:
        raise RuntimeError('FCLK0核验失败：要求50 MHz，寄存器读数%.6f MHz；停止后续初始化' % actual)
    return record
