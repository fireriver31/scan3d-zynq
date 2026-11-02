# 数据布局与软硬件接口

以下为原完整工程与PS驱动约定的地址和数据布局。源码版不附整机HWH；外部底座集成后，须依据实际HWH核对IP名称、地址、时钟及寄存器，不能直接沿用未核实的映射。

| 实例 | 基址—末址 | 接口/驱动 |
|---|---|---|
| `ui_bram` | 0x40000000—0x4001FFFF | S_AXI；`scan_screen.py` 的位图OSD写入 |
| `recon_0` | 0x40020000—0x4003FFFF | s_axi；`ps_demo.py`，RTL `hardware/rtl/recon_axil.v`/`recon_top.v` |
| `photo_dma` | 0x40400000—0x4040FFFF | S_AXI_LITE；PYNQ DMA、`pl_mask.py` |
| `camera_control` | 0x41200000—0x4120FFFF | S_AXI；`camera_control.py` SCCB访问 |
| `photo_mask_0` | 0x43C00000—0x43C00FFF | s_axi；`pl_mask.py`，`hardware/rtl/1016_photo_mask_axis_v19.sv` |
| `touch_viewer_0` | 0x43C10000—0x43C10FFF | s_axi；`tft_touch.py`，RTL `touch_viewer_ctrl.v` |

PS DDR地址范围在HWH中为0x00000000—0x1FFFFFFF，两条HP接口不表示两份独立512 MB内存。固定相机帧缓冲是其中 `0x01800000–0x018BB7FF`，必须从Linux保留；UI BRAM不是这块DDR。

## 原图与DMA判定位

图像数组为 `(480,800) uint16` RGB565。相机16→64 FIFO高位像素先行，PS小端读取时每4像素顺序需要由 `rtl_mask_demo.restore_ddr_pixel_order` 还原一次。显示端64→16 FIFO已经按高位先行展开。不能对已还原数组重复反转，也不能当作RGB/BGR通道交换处理。

`pl_mask.reference_decisions` 是离线整数参考，不是实际扫描时替代PL的密集颜色判定。PL使用配置LUT、按背景颜色分箱的逆方差与Q16阈值。原图及参考通过DMA对应同尺寸数组；返回每像素2位，LSB优先，每32位字16像素，总24000个uint32。强前景必须是候选前景子集，`unpack_decisions` 保留此检查。

完整实际PL记录应同时包含原图、参考指纹/常量、LUT、逆方差、阈值和packed位。诊断顶层参数可能在后续阶段更新，不能任意跨阶段拼参数重放。最终PS掩模与某一次PL强位不是同一字段；区域处理仍会参与前景归属。

## 重建投影与占据

`ps_demo.py` 写入Q16系数和掩模数据，启动 `recon_top`，再从PL读回打包的占据位图。PS解包后统计占据体素数：示例驱动使用 `int(occ.sum())`，扫描流程使用 `np.count_nonzero(occ)`；体积由PS按体素边长换算。整数图像坐标表示像素中心；PL右移执行floor，因此偏置中加入0.5，使PL/PS投影和照片取样约定一致。

`OCC_WCNT`（偏移0x034，Python驱动中为 `R_OCC_CNT`）表示占据位图的32位数据字数，用于确定读回长度。它不统计值为1的体素；N=128时，128³个占据位对应65536个32位字。

| 字段或结果 | 执行位置与含义 | 随附实测 |
|---|---|---:|
| OCC_WCNT / R_OCC_CNT | PL配置给出的占据数据字数 | 65536个32位字 |
| occupied_voxels | PS对解包后的占据数组统计 | 123392个体素 |
| volume_cm3 | PS按N·d³/1000换算，d单位为mm | 86.958081cm³ |

体积公式中的N指占据体素数，不是网格边长或数据字数。严格PL占据、PS细化结果和照片显示分别保存；照片着色不作为空间几何证据，主模型的不可见面复用代表色也不是该面真实观测。

## 屏幕与触摸所有权

触摸ABI87，诊断版本90，显示诊断99。`touch_viewer_ctrl.v` 负责触摸读取/显示请求寄存器；`viewer_frame_reader.v` 在安全帧边界处理显示所有权；`tft_touch.py`、`tft_model.py`、`tft_viewer.py` 负责主机请求和手势。

`screen_lease.py` 用进程锁约束实体屏占用，查看器退出/异常时要确认读取结束再释放DMA缓存。不能仅因Python引用消失就认为PL已经不读该内存。`scan_pl_guard.py` 保护旧PYNQ共享连接的完整查询/更新事务，不修改系统包，不自动重启PL服务。

运行字库只含子集字符；新增进度文案需核对 `scan_screen_font.py` 覆盖，不能只确认Python字符串正确。主流程右栏与顶部预留范围由同一套绘制/触摸坐标定义，避免按钮显示位置与触点范围不一致。
