# 硬件源工程与构建

本仓库包含ACZ702的ov5640_ddr3_tft_hdmi源工程，包括RTL、BD、XCI、XDC、XPR与测试资产。厂家例程及其修改版按[专项记录](../third_party/authorization/20261101_acz702_permission_v1.md)保留和分发，署名及原始链接不变；独立自研模块按MIT列示。

hardware/rtl/1016_photo_mask_axis_v19.sv为前景核。其余集成RTL位于vivado/ov5640_ddr3_tft_hdmi.srcs/sources_1/new，独立新增模块包括体素雕刻、GT911、UI混合和显示读取。厂家及生成配置的许可类别和每份源文件SHA见[源清单](../third_party/source_inventory.json)。

## Vivado入口

Vivado 2026.1，器件xc7z020clg400-1。仓库根目录执行，输出选新的短英文外部路径：

```text
vivado -mode batch -source build/check_project.tcl -tclargs ../scan3d_project_check
vivado -mode batch -source build/build_hardware.tcl -tclargs ../scan3d_build
```

前者核对XPR引用、器件、BD和IP状态；后者生成IP、综合、实现和scan3d.bit/scan3d.hwh，并导出时序、资源、DRC和CDC。构建须使用合法安装的Vivado及适用IP许可，仓库不打包专有IP实现源码。详细命令和范围见[build/README](../build/README.md)。

## 验证与部署范围

模块仿真按[sim/README](../sim/README.md)执行。源工程随库提供，BD/IP状态可用`build/check_project.tcl`核对；已有的工程迁移与BD检查输出见[历史检查日志](../sim/results/logs/project/project_check.txt)及[结果摘要](../sim/results/public_project_checks.json)。历史完整工程的全量构建记录见[构建说明](../report/build_reproduction.md)；此次资料补充未重跑Vivado或全量综合；c563构建与本次配套运行文件的新板测见[配套实验](../report/board_volume_experiment.md)。

预构建整机bit/hwh未随本仓库分发；可按构建入口在本地生成。部署仍须检查外部IO、相机复位CDC、实际时钟和DDR保留，见[板上说明](../board/README.md)。
