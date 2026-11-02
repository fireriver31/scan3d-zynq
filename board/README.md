# 构建与上板前置条件

本仓库提供ACZ702例程源工程和构建脚本。先按[构建说明](../build/README.md)在新目录生成`scan3d.bit`与`scan3d.hwh`，将二者原名成对放入Notebook的DATA_DIR，与src/runtime中的Python、C、Notebook及scan_font.bin同级。源码版Notebook的COLOR_BIT默认指向`scan3d.bit`。

新构建文件沿用自己的名称和SHA。原比赛实机使用的`1028_display_buffer_v99.bit`及配套hwh属于历史产物；若使用自行持有的那一对，应明确修改COLOR_BIT指向原名并核对SHA，不把新文件改成历史文件名。c563开头的构建已与本次软件配套完成板上八步；其他重新构建的产物仍按自身SHA完成部署核验。

运行前核对：

1. XC7Z020CLG400-1、OV5640、显示屏和GT911接线，见[接口说明](interfaces.md)。
2. Linux本次启动为相机专门保留0x01800000–0x018FFFFF；实际帧占0x01800000–0x018BB7FF。未保留时停止初始化。
3. FCLK0实际读回50MHz；相机和显示时钟按设计核对。
4. 原实机环境为PYNQ2.4、Python3.6.5及对应NumPy/Pillow/SciPy/gcc，先核对目标板版本。

scan_workflow.ipynb提供九格/八步，touch_workflow.ipynb由Jupyter启动后在实体屏继续操作，须保持内核运行，未配置开机自启动。新字模由Noto生成，本次配套板上运行已使用，中文显示有人工确认。

触摸屏端INT/SCL/RST/SDA孔序为3/6/7/8，对应FPGA G14/L15/J14/K14；断电核对板型及接口，共地后再上电。本次配套程序与c563构建完成八步，时钟和DDR已记录；退出方式及形状限制见[配套实验](../report/board_volume_experiment.md)。历史实测与主机测试分别列在[验证说明](../sim/README.md)。


## 源码版与构建时间

本库不含预构建bit/hwh，首次使用需安装适用的Vivado并运行构建命令，然后部署产物。当前提供的是需要构建的源码版，未配置开机自启动。厂家申请与答复的原文完整保留。

原完整工程2026-10-31一次全量构建日志从19:34:43到19:51:42，约17分钟，原件指纹见[归档索引](../data/evidence_sources.json)的build/reproduction_reports/20261031_packaged/full_build.log。这是历史主机的一次记录；当前源码版未新增全量构建，实际时间受电脑性能、IP生成与缓存状态影响。

## 被测版本定位

本次配套程序对应的源码清单及38份软件映射见[被测快照](../data/board_acceptance/cuboid_606/tested_snapshot.md)。当前包的文档与核验工具有后续修订，部署软件和记录中的bit/hwh指纹保持一致。

## 验收记录的生成与复核

[验收归档工具](tools/README.md)保存该轮已执行的Notebook和提取的导出定义。它说明七项人工输入如何写入验收摘要，Notebook保留13段输出与KeyboardInterrupt记录。主扫描入口仍是src/runtime中的Notebook；历史验收安装格依赖当时的载荷ZIP，使用条件在工具说明中列出。
