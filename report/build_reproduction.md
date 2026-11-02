# 构建复现记录

当前命令见[构建说明](../build/README.md)。check_project检查源引用、器件及BD，build_hardware执行IP生成、综合、实现和bit/hwh导出；二者是不同检查。

原完整工程在2026-10-31完成两次全量构建。早期综合子进程退出后，脚本改为在cd前固定目录，将general.maxThreads=1注册到综合前置阶段。

| 归档目录 | 构建方式 | bit SHA256前12位 |
|---|---|---|
| 20261031_completed | 独立全量构建 | 3875f5576822 |
| 20261031_packaged | 从新输入副本运行包内入口 | c563373a98e5 |

两次均为LUT16651、寄存器22729、BRAM118 tile、DSP6，内部WNS+0.520ns、WHS+0.020ns。不同SHA分别保存，不据此声称位流等价。完整日志和产物摘要的原件指纹见[归档索引](../data/evidence_sources.json)。早期摆件扫描使用SHA以c43f921aed62开头的v99文件；本次纸盒实验已部署c563开头的构建，40份运行文件SHA见[配套实验](board_volume_experiment.md)。

源码版清除了XPR中12处旧ImportPath提示，40处实际File Path引用保持不变，见[元数据变更](../build/project_metadata_changes.json)。本轮进行了工程迁移、BD及模块检查，结果和日志见[公开验证](../sim/README.md)，本次未重新全量综合，配套构建已取得新的板上运行记录。新构建输出使用scan3d.bit/scan3d.hwh，不套用历史v99文件名。相机复位3条Critical CDC提示、15输入及28输出缺外部delay的状态仍待专项核对。
