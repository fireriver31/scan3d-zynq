"""Board-side smoke test for the OV5640 -> RTL mask -> DDR path.

This module only reads the existing framebuffer. It never writes to the
camera framebuffer or modifies teammates' Python files.
"""

import mmap
import os
import re
import time
from pathlib import Path

import numpy as np
from PIL import Image


FRAME_BASE = 0x01800000
HEIGHT = 480
WIDTH = 800
FRAME_READER_VERSION = 29
FRAME_PIXEL_ORDER = 'camera_raster_fifo16x64_restored_v29'


def restore_ddr_pixel_order(array):
    """Undo this design's MSB-first 16->64 FIFO packing, not a byte swap.

    Camera A,B,C,D become the integer 0xAAAABBBBCCCCDDDD. Little-endian
    uint16 memory reads see D,C,B,A. The display's 64->16 FIFO already
    unpacks MSB first; only the PS memory reader needs this inverse mapping.
    Works for matching saved binary masks as well. Apply exactly once.
    """
    a=np.asarray(array)
    if a.ndim<2 or a.shape[-2:]!=(HEIGHT,WIDTH):
        raise ValueError('像素顺序还原需要末两维为480x800的数组')
    return a.reshape(a.shape[:-1]+(WIDTH//4,4))[...,::-1].reshape(a.shape).copy()


def require_framebuffer_reservation():
    """在加载会持续写 DDR 的 bit 前确认固定帧缓存已从 Linux 保留。

    现有板子的 /proc/iomem 把 0x01800000 列在 System RAM 内，且没有
    reserved-memory 节点；在修好系统镜像前必须停止加载，而非只警告。
    """
    end = FRAME_BASE + WIDTH * HEIGHT * 2  # 半开区间
    root = Path('/proc/device-tree/reserved-memory')
    if root.is_dir():
        def cells(prop, fallback):
            path = root / prop
            if not path.is_file():
                path = root.parent / prop
            return int.from_bytes(path.read_bytes(), 'big') if path.is_file() else fallback

        ac = cells('#address-cells', 1)
        sc = cells('#size-cells', 1)
        if 0 < ac <= 2 and 0 < sc <= 2:
            unit = 4 * (ac + sc)
            for child in root.iterdir():
                reg = child / 'reg'
                if not reg.is_file():
                    continue
                raw = reg.read_bytes()
                for pos in range(0, len(raw) - unit + 1, unit):
                    base = int.from_bytes(raw[pos:pos + 4 * ac], 'big')
                    length = int.from_bytes(raw[pos + 4 * ac:pos + unit], 'big')
                    if base <= FRAME_BASE and end <= base + length:
                        return 'device-tree: ' + child.name

    # 某些镜像用启动固件预留地址，未生成 /reserved-memory 子节点。
    iomem = Path('/proc/iomem')
    if iomem.is_file():
        for line in iomem.read_text(errors='replace').splitlines():
            match = re.match(r'\s*([0-9a-fA-F]+)-([0-9a-fA-F]+)\s*:\s*(.*)', line)
            if match and 'reserved' in match.group(3).lower():
                lo, hi = int(match.group(1), 16), int(match.group(2), 16)
                if lo <= FRAME_BASE and end - 1 <= hi:
                    return 'iomem: ' + line.strip()
    raise RuntimeError(
        '停止加载 Overlay：固定帧缓存 0x%08X–0x%08X 没有查到 Linux 内存保留。'
        'PL 持续写这里可能覆盖内核；先修设备树或帧缓存地址。' %
        (FRAME_BASE, end - 1))


def load_mask_overlay(directory=None):
    """Program the matching bit/hwh pair; do this with an empty turntable."""
    from pynq import Overlay

    require_framebuffer_reservation()

    root = Path(directory) if directory else Path(__file__).resolve().parent
    bit = root / "1010_rtl_mask_v1.bit"
    hwh = bit.with_suffix(".hwh")
    if not bit.is_file() or not hwh.is_file():
        raise FileNotFoundError("Missing matching 1010_rtl_mask_v1.bit/.hwh")
    overlay = Overlay(str(bit), download=True)
    print("Loaded:", bit)
    print("IP:", sorted(overlay.ip_dict))
    # Camera initialization / first valid background frame need some time.
    time.sleep(2.0)
    return overlay


def grab_raw():
    """Copy a camera-raster RGB565 snapshot, restoring 4-pixel FIFO lanes.

    This fixes spatial order only; a single live DDR buffer still cannot
    guarantee sensor-frame ownership or prevent tearing during the copy.
    """
    flags = os.O_RDONLY | getattr(os, "O_SYNC", 0)
    fd = os.open("/dev/mem", flags)
    try:
        with mmap.mmap(fd, WIDTH * HEIGHT * 2,
                       flags=mmap.MAP_SHARED, prot=mmap.PROT_READ,
                       offset=FRAME_BASE) as mm:
            raw=np.frombuffer(mm,dtype='<u2').reshape(HEIGHT,WIDTH).copy()
        return restore_ddr_pixel_order(raw)
    finally:
        os.close(fd)


grab_raw.frame_pixel_order = FRAME_PIXEL_ORDER
grab_raw.frame_reader_version = FRAME_READER_VERSION


def diagnose(frame, save_as=None, strict=True):
    """Report actual binary pixels, then save a viewable black/white PNG."""
    a = np.asarray(frame)
    if a.shape != (HEIGHT, WIDTH) or a.dtype != np.uint16:
        raise ValueError("Expected raw 480x800 uint16 RGB565 data")
    black = int(np.count_nonzero(a == 0))
    white = int(np.count_nonzero(a == 0xffff))
    other = a.size - black - white
    result = {"black": black, "white": white, "other": other,
              "black_fraction": black / float(a.size)}
    print("Black (object): {black}, white (background): {white}, "
          "other: {other}, black fraction: {black_fraction:.4f}".format(**result))
    if save_as:
        # Nonbinary pixels are shown gray so corruption is visible.
        view = np.full(a.shape, 127, dtype=np.uint8)
        view[a == 0] = 0
        view[a == 0xffff] = 255
        Image.fromarray(view, mode="L").save(str(save_as))
        print("Saved:", save_as)
    if strict and other:
        sample = np.unique(a[(a != 0) & (a != 0xffff)])[:12]
        raise ValueError("Frame contains nonbinary pixels: %s. Check the new "
                         "bitstream, framebuffer address, or a torn frame."
                         % sample.tolist())
    return result


def capture_mask128(crop_x=160, crop_y=0):
    """Return one 128x128 mask: 1=object, 0=background."""
    from mask_input import frame_to_recon_mask

    return frame_to_recon_mask(grab_raw(), crop_x=crop_x, crop_y=crop_y)
