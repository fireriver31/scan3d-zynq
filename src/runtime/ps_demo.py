#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ps_demo.py —— 在 PYNQ（PS 侧）上使用 PL 里的三维重建核

它做四件事：
   1. 加载 Overlay，拿到 0x4002_0000 的寄存器窗口
   2. 把每视角的几何系数（Q16 定点）算好写进去
   3. 把 24 张掩膜（降采样到 128x128）写进去
   4. 按 start → 等 done → 把 128^3 的体素占据位图读回来
      统计体积、可选导出一份 .ply 点云

★ 掩膜约定：PL 内部要求 1=背景、0=物体。demo() 默认接收
   scan.npz 的 1=物体 布尔掩膜，并在写入 PL 前自动取反。

★ 与 ref_model.py 的逐体素整数投影规则相同；真实物体的 z=0
   改为标定出的台面行，仿真向量仍使用 ref_model.py 自己的居中坐标。
      u = (OFU + x*KUX + y*KUY) >> 16        （>> 是向下取整）
      v = (OFV - z*KV)          >> 16
"""

import os
import time
import warnings
import numpy as np
PIXEL_CENTER_PROJECTION_VERSION = 1

RECON_BASE = 0x40020000          # BD Address Editor 里分配给 recon_0 的基地址

# ---- 寄存器偏移（和 recon_axil.v 里一一对应）----
R_CTRL      = 0x000
R_STATUS    = 0x004
R_CFG_N     = 0x008
R_CFG_MW    = 0x00C
R_CFG_MH    = 0x010
R_CFG_NV    = 0x014
R_COEF_KV   = 0x018
R_COEF_OFV  = 0x01C
R_MASK_DATA = 0x020
R_MASK_PTR  = 0x024
R_MASK_CNT  = 0x028
R_OCC_PTR   = 0x02C
R_OCC_DATA  = 0x030
R_OCC_CNT   = 0x034
R_COEF_BASE = 0x100              # 每视角 16 字节：+0 KUX, +4 KUY, +8 OFU


def q16(x):
    """浮点 -> Q16 定点（四舍五入，半值向上）。"""
    value = float(x)
    if not np.isfinite(value):
        raise ValueError("Q16 系数必须是有限数")
    result = int(np.floor(value * 65536.0 + 0.5))
    if not -(1 << 31) <= result < (1 << 31):
        raise ValueError("Q16 系数超出 32 位有符号范围")
    return result


# ======================================================================
# 一、几何：把标定参数变成 PL 要的 Q16 系数
# ======================================================================
def plan_geometry(angles_deg, N, MW, MH, voxel_mm, mm_per_px,
                  axis_col=None, v_ground=None):
    """
    angles_deg : 每张掩膜对应的转台角度（度），长度 = 视角数
    N          : 体素网格边长（这里默认 128）
    MW, MH     : 掩膜宽 / 高（像素）
    voxel_mm   : 一个体素多少毫米
    mm_per_px  : 掩膜上 1 像素多少毫米
    axis_col   : 转轴在掩膜上的列坐标（像素），必须实测
    v_ground   : 物体底面在掩膜上的行坐标（像素），必须实测；z=0 对应此行

    两个数须从标定图像测得；本项目使用实测200mm转台。
    图像坐标以整数表示像素中心，与 prepare_scan_masks 的中心映射一致。
    PL右移执行floor，所以在OFU/OFV加入0.5以实现最近像素中心采样。

    返回 (kv, ofv, kux, kuy, ofu)，全部是 Q16 整数（numpy int64 数组）
    """
    if N <= 0 or MW <= 0 or MH <= 0 or MW % 32:
        raise ValueError("N/MW/MH 必须为正，掩膜宽 MW 必须是 32 的倍数")
    if not np.isfinite(voxel_mm) or not np.isfinite(mm_per_px) or voxel_mm <= 0 or mm_per_px <= 0:
        raise ValueError("voxel_mm 和 mm_per_px 必须是正的有限数")
    if axis_col is None or v_ground is None:
        raise ValueError("axis_col 和 v_ground 必须从实物图像标定，不能使用画面中心代替")
    if not np.isfinite(axis_col) or not np.isfinite(v_ground):
        raise ValueError("axis_col 和 v_ground 必须是有限数")
    if not (0 <= axis_col < MW and 0 <= v_ground < MH):
        raise ValueError("axis_col 或 v_ground 超出降采样后掩膜的范围")
    angles = np.asarray(angles_deg, dtype=np.float64)
    if angles.ndim != 1 or angles.size == 0 or not np.all(np.isfinite(angles)):
        raise ValueError("angles_deg 必须是一维、非空的有限角度序列")

    # v = (OFV - z*KV) >> 16 ，只跟 z 有关
    kv_f = voxel_mm / mm_per_px
    kv   = q16(kv_f)
    ofv  = q16(v_ground + 0.5)  # floor(v+0.5)：与像素中心坐标一致

    n = len(angles)
    kux = np.zeros(n, np.int64)
    kuy = np.zeros(n, np.int64)
    ofu = np.zeros(n, np.int64)
    half = (N - 1) / 2.0
    for i, a in enumerate(angles):
        th = np.deg2rad(a)
        c, s = np.cos(th), np.sin(th)
        # u = axis_col + ((x-half)*voxel_mm*c - (y-half)*voxel_mm*s) / mm_per_px
        kux[i] = q16(voxel_mm / mm_per_px * c)
        kuy[i] = q16(-voxel_mm / mm_per_px * s)
        ofu[i] = q16(axis_col + 0.5 - half * voxel_mm / mm_per_px * (c - s))
    return kv, ofv, kux, kuy, ofu


def prepare_scan_masks(foreground_masks, mm_per_px, axis_col, v_ground,
                       roi, out_size=128):
    """将原相机的 1=物体 掩膜裁成正方形，再等比例采样。

    roi=(x0,y0,x1,y1)，右下边界不含，必须是完整包含物体的正方形。
    返回 (前景布尔掩膜, 新 mm_per_px, 新 axis_col, 新 v_ground)。
    新掩膜仍是 1=物体；demo() 会在写入 PL 前转为 1=背景。
    """
    masks = np.asarray(foreground_masks, dtype=bool)
    if masks.ndim != 3 or not masks.any(axis=(1, 2)).all():
        raise ValueError("输入必须是 (视角, 高, 宽) 的非空前景掩膜")
    if not isinstance(roi, (tuple, list)) or len(roi) != 4:
        raise ValueError("roi 必须是 (x0,y0,x1,y1)")
    x0, y0, x1, y1 = map(int, roi)
    _, height, width = masks.shape
    side = max(x1 - x0, y1 - y0)
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("roi 必须是图像范围内的矩形")
    if not isinstance(out_size, (int, np.integer)) or out_size <= 0 or out_size > side:
        raise ValueError("out_size 必须是 1..roi 边长内的整数")
    if not np.isfinite(mm_per_px) or mm_per_px <= 0:
        raise ValueError("mm_per_px 必须为正的有限数")
    if not np.isfinite(axis_col) or not np.isfinite(v_ground):
        raise ValueError("轴列和底面行必须是有限数")
    if not (x0 <= axis_col < x1 and y0 <= v_ground < y1):
        raise ValueError("轴列或底面行落在正方形 ROI 外")
    if (masks[:, :y0, :].any() or masks[:, y1:, :].any() or
            masks[:, y0:y1, :x0].any() or masks[:, y0:y1, x1:].any()):
        raise ValueError("roi 裁掉了前景；放大 ROI 或重新拍摄")
    cropped = masks[:, y0:y1, x0:x1]
    if (cropped[:, 0, :].any() or cropped[:, -1, :].any() or
            cropped[:, :, 0].any() or cropped[:, :, -1].any()):
        raise ValueError("前景贴着 ROI 边界；不能确认物体已完整入画")
    if cropped.shape[1:] != (side,side):
        square=np.zeros((len(masks),side,side),bool)
        square[:,:y1-y0,:x1-x0]=cropped
        cropped=square
    sample = np.floor((np.arange(out_size) + 0.5) * side / out_size).astype(np.intp)
    sampled = cropped[:, sample[:, None], sample[None, :]]
    if not sampled.any(axis=(1, 2)).all():
        raise ValueError("降采样后出现空掩膜；物体太小或需要更高分辨率")
    factor = float(out_size) / side
    return (sampled, float(mm_per_px) / factor,
            (float(axis_col) - x0 + 0.5) * factor - 0.5,
            (float(v_ground) - y0 + 0.5) * factor - 0.5)


def expand_horizontal_masks(masks, margin_px=1):
    """在降采样掩膜上给左右边界留 1 像素余量，不改变任何有效行。

    只用于对比 128 像素投影/取整造成的系统性缩径；输入保持不变。
    """
    src = np.asarray(masks, dtype=bool)
    if src.ndim != 3 or margin_px not in (0, 1):
        raise ValueError("masks 必须是三维数组，margin_px 只能是 0 或 1")
    if margin_px == 0:
        return src.copy()
    widened = src.copy()
    widened[:, :, 1:] |= src[:, :, :-1]
    widened[:, :, :-1] |= src[:, :, 1:]
    return widened


# ======================================================================
# 二、掩膜打包：128x128 的 0/1 图 -> 32bit 字流（每行 4 个字）
# ======================================================================
def pack_masks(masks):
    """
    masks : (NV, MH, MW) 的 0/1 数组，1=背景 0=物体
    返回  : (NV*MH*MW/32,) 的 uint32 数组，顺序 [view][row][word]
            字内 bit j 就是第 j 列（bit0 = 最左边那一列）
    """
    NV, MH, MW = masks.shape
    assert MW % 32 == 0
    m = masks.reshape(NV, MH, MW // 32, 32).astype(np.uint32)
    out = np.zeros((NV, MH, MW // 32), np.uint32)
    for j in range(32):
        out |= (m[:, :, :, j] << j)
    return out.reshape(-1)


def unpack_occ(words, N):
    """把读回来的占据字还原成 (N,N,N) 的 bool 数组，索引顺序 [z,y,x]。"""
    nvox = N * N * N
    w = np.asarray(words, np.uint32)
    bits = np.zeros(len(w) * 32, np.uint8)
    for j in range(32):
        bits[j::32] = ((w >> j) & 1).astype(np.uint8)
    return bits[:nvox].astype(bool).reshape(N, N, N)


# ======================================================================
# 三、PL 驱动
# ======================================================================
class Recon(object):
    def __init__(self, overlay_path=None, base=RECON_BASE, overlay=None):
        from pynq import Overlay, MMIO
        if overlay is None and overlay_path is None:
            raise ValueError("必须提供已加载的 overlay 或 bit 文件路径")
        self.ol = overlay if overlay is not None else Overlay(overlay_path)
        self.mm = MMIO(base, 0x4000)
        self.N  = self.mm.read(R_CFG_N)
        self.MW = self.mm.read(R_CFG_MW)
        self.MH = self.mm.read(R_CFG_MH)
        self.NV = self.mm.read(R_CFG_NV)
        self.mask_words = self.mm.read(R_MASK_CNT)
        self.occ_words  = self.mm.read(R_OCC_CNT)
        print("PL 里编译好的配置: N=%d  MW=%d  MH=%d  NV=%d" %
              (self.N, self.MW, self.MH, self.NV))
        print("掩膜字数=%d  占据字数=%d" % (self.mask_words, self.occ_words))

    # ---------- 系数 ----------
    def set_geometry(self, kv, ofv, kux, kuy, ofu):
        if not (len(kux) == len(kuy) == len(ofu) == self.NV):
            raise ValueError("每组角度系数的数量必须等于 PL 的 NV")
        w = self.mm.write
        w(R_COEF_KV,  int(kv) & 0xFFFFFFFF)
        w(R_COEF_OFV, int(ofv) & 0xFFFFFFFF)
        for i in range(len(kux)):
            w(R_COEF_BASE + i * 16 + 0, int(kux[i]) & 0xFFFFFFFF)
            w(R_COEF_BASE + i * 16 + 4, int(kuy[i]) & 0xFFFFFFFF)
            w(R_COEF_BASE + i * 16 + 8, int(ofu[i]) & 0xFFFFFFFF)

    # ---------- 掩膜 ----------
    def write_masks(self, words):
        if len(words) != self.mask_words:
            raise ValueError("掩膜字数与 PL 配置不一致：%d != %d" %
                             (len(words), self.mask_words))
        w = self.mm.write
        w(R_MASK_PTR, 0)
        for v in words:
            w(R_MASK_DATA, int(v))
        return self.mm.read(R_MASK_PTR)

    # ---------- 跑 ----------
    def run(self, timeout=10.0):
        if timeout <= 0:
            raise ValueError("timeout 必须为正")
        w = self.mm.write
        t0 = time.monotonic()
        w(R_CTRL, 0x1)                    # bit0 = start
        while (self.mm.read(R_STATUS) & 0x2) == 0:
            if time.monotonic() - t0 > timeout:
                raise RuntimeError("等 done 超时；STATUS=%08x" % self.mm.read(R_STATUS))
        return time.monotonic() - t0

    # ---------- 读结果 ----------
    def read_occ(self):
        w = self.mm.write
        r = self.mm.read
        w(R_OCC_PTR, 0)
        n = self.occ_words
        out = np.empty(n, np.uint32)
        for i in range(n):
            out[i] = r(R_OCC_DATA)
        return out


# ======================================================================
# 四、导出 .ply（体素点云，MeshLab / CloudCompare 直接打开）
# ======================================================================
def save_ply(path, occ, voxel_mm=1.0):
    zz, yy, xx = np.nonzero(occ)
    n = len(zz)
    temporary = str(path) + ".partial"
    with open(temporary, "w") as f:
        f.write("ply\nformat ascii 1.0\n")
        f.write("element vertex %d\n" % n)
        f.write("property float x\nproperty float y\nproperty float z\n")
        f.write("end_header\n")
        for i in range(n):
            f.write("%.3f %.3f %.3f\n" %
                    ((xx[i] - (occ.shape[2] - 1) / 2.0) * voxel_mm,
                     (yy[i] - (occ.shape[1] - 1) / 2.0) * voxel_mm,
                     zz[i] * voxel_mm))
    os.replace(temporary, str(path))
    return n


def show_voxels(occ, voxel_mm=1.0, max_points=10000, save_png=None):
    """在 Notebook 中显示体素点云；坐标与 save_ply() 使用相同的毫米单位。"""
    occ = np.asarray(occ)
    if occ.ndim != 3:
        raise ValueError("occ 必须是 (z, y, x) 三维体素数组")
    if not np.isfinite(voxel_mm) or voxel_mm <= 0:
        raise ValueError("voxel_mm 必须是正数")
    if not isinstance(max_points, (int, np.integer)) or max_points <= 0:
        raise ValueError("max_points 必须是正整数")

    z, y, x = np.nonzero(occ)
    if len(x) == 0:
        raise ValueError("重建结果为空，请检查掩膜和标定")
    pick = np.linspace(0, len(x) - 1, min(len(x), max_points), dtype=np.intp)
    px = (x[pick] - (occ.shape[2] - 1) / 2.0) * voxel_mm
    py = (y[pick] - (occ.shape[1] - 1) / 2.0) * voxel_mm
    pz = z[pick] * voxel_mm

    try:
        # Matplotlib 2.x only registers the 3D projection after this import.
        from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("Matplotlib 未安装；可先用 recon.ply 在电脑上查看三维点云") from exc

    fig = plt.figure(figsize=(7, 6))
    ax = fig.add_subplot(111, projection="3d")
    ax.scatter(px, py, pz, s=1, c=pz, cmap="viridis", linewidths=0)
    ax.set(xlabel="X (mm)", ylabel="Y (mm)", zlabel="Z (mm)",
           title="PL 重建的三维体素")
    if save_png is not None:
        fig.savefig(save_png, dpi=160, bbox_inches="tight")
    plt.show()
    return fig, ax


# ======================================================================
# 五、端到端示例
# ======================================================================
def demo(overlay_path, masks, angles_deg, voxel_mm, mm_per_px,
         axis_col=None, v_ground=None, ply_path=None, foreground_value=1,
         overlay=None):
    """
    masks : (NV, MH, MW) 的 0/1 掩膜；默认 1=物体，写 PL 前自动取反。
            若调用者已给出 1=背景、0=物体，传 foreground_value=0。
    """
    masks = np.asarray(masks)
    if masks.ndim != 3 or foreground_value not in (0, 1):
        raise ValueError("masks 必须是三维 0/1 数组，foreground_value 只能是 0 或 1")
    if not np.all((masks == 0) | (masks == 1)):
        raise ValueError("掩膜只能含 0/1；请先二值化，不能输入灰度插值结果")
    NV, MH, MW = masks.shape
    angles = np.asarray(angles_deg, dtype=np.float64)
    if angles.shape != (NV,) or not np.all(np.isfinite(angles)):
        raise ValueError("掩膜数和角度数必须一致")
    object_masks = (masks == foreground_value)
    if not object_masks.any(axis=(1, 2)).all():
        raise ValueError("至少一张掩膜没有物体")
    if axis_col is None or v_ground is None:
        raise ValueError("请先标定轴列 axis_col 和底面行 v_ground，再加载 Overlay")
    if NV > 1:
        span = float(angles.max() - angles.min())
        if span > 430:
            warnings.warn("角度跨越 %.1f 度，超过一圈；请核对转盘角速度与拍照时间" % span)
    r = Recon(overlay_path, overlay=overlay)
    if (r.MW, r.MH, r.NV) != (MW, MH, NV):
        raise ValueError("掩膜尺寸/视角数和 PL 里编译的不一致: "
                         "PL=(%d,%d,%d), 输入=(%d,%d,%d)" %
                         (r.MW, r.MH, r.NV, MW, MH, NV))

    kv, ofv, kux, kuy, ofu = plan_geometry(
        angles, r.N, MW, MH, voxel_mm, mm_per_px, axis_col, v_ground)
    r.set_geometry(kv, ofv, kux, kuy, ofu)

    t_w = time.monotonic()
    ptr = r.write_masks(pack_masks((~object_masks).astype(np.uint8)))
    if ptr != r.mask_words:
        raise RuntimeError("掩膜写指针为 %d，期望 %d" % (ptr, r.mask_words))
    print("掩膜写入: %d 字 (指针回到 %d)  耗时 %.3f s" %
          (r.mask_words, ptr, time.monotonic() - t_w))

    dt = r.run()
    print("PL 雕刻耗时: %.4f s" % dt)

    t_r = time.monotonic()
    words = r.read_occ()
    occ = unpack_occ(words, r.N)
    print("结果读回: %d 字  耗时 %.3f s" % (len(words), time.monotonic() - t_r))

    nvox = int(occ.sum())
    vol = nvox * (voxel_mm ** 3) / 1000.0          # cm^3
    print("占据体素 = %d   体积 = %.2f cm^3   (体素边长 %.3f mm)" %
          (nvox, vol, voxel_mm))
    if ply_path:
        print("已导出点云: %s (%d 点)" % (ply_path, save_ply(ply_path, occ, voxel_mm)))
    return occ


if __name__ == "__main__":
    print(__doc__)
    print("把上面的 demo() 接到你的 scan.npz 上就能跑。")
