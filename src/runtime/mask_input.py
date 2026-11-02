"""Convert the new PL black/white DDR frame into a recon mask.

The input must be the raw 800x480 RGB565 frame BEFORE ui_mixer adds its UI.
Output convention: 1 = object, 0 = background, matching ps_demo.demo's default.
"""

import numpy as np


def frame_to_recon_mask(frame_rgb565, crop_x=160, crop_y=0,
                        crop_size=480, out_size=128):
    frame = np.asarray(frame_rgb565)
    if frame.shape != (480, 800):
        raise ValueError("expected one raw 480x800 RGB565 frame")
    if frame.dtype != np.uint16:
        raise ValueError("expected uint16 RGB565 pixels, not RGB888 or a PNG")
    if crop_size <= 0 or out_size <= 0 or crop_x < 0 or crop_y < 0:
        raise ValueError("invalid crop parameters")
    if crop_x + crop_size > 800 or crop_y + crop_size > 480:
        raise ValueError("square crop is outside the camera frame")
    if np.any((frame != 0) & (frame != 0xffff)):
        raise ValueError("frame is not a PL binary mask; load the NEW bitstream")

    roi = frame[crop_y:crop_y + crop_size, crop_x:crop_x + crop_size]
    take = np.arange(out_size, dtype=np.int32) * crop_size // out_size
    return (roi[np.ix_(take, take)] == 0).astype(np.uint8)


def scaled_geometry(axis_col_full, v_ground_full, mm_per_px_full,
                    crop_x=160, crop_y=0, crop_size=480, out_size=128):
    """Keep axis, ground, and pixel scale consistent with the square crop."""
    if crop_size <= 0 or out_size <= 0 or mm_per_px_full <= 0:
        raise ValueError("invalid scale")
    scale = float(out_size) / float(crop_size)
    return ((axis_col_full - crop_x) * scale,
            (v_ground_full - crop_y) * scale,
            mm_per_px_full / scale)
