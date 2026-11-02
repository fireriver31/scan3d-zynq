# 屏幕状态字段的含义与范围

这些记录来自不同采集方式，布尔值不能直接混成同一项自动验收。

| 文件和字段 | 生成方式 | 可以说明什么 |
|---|---|---|
| touch_viewer.json / board_display_confirmed=false | src/runtime/tft_viewer.py的状态方法固定写False | 查看器未自动替代人工完成实体屏观感验收，不是检测到屏幕失败 |
| display_status.json / screen_image_confirmed=false | src/runtime/screen_probe.py固定写False | 硬件FIFO和像素探针读回不等于人眼观感判断 |
| display_clock.json / screen_image_confirmed=false | src/runtime/scan_clock.py固定写False | 时钟寄存器和计算频率检查不自动判定屏幕观感 |
| acceptance_summary.json / operator_checks和all_operator_checks_confirmed=true | 归档工具接收人工输入的七个布尔值，再按全部为True汇总 | 本次操作者报告这七项通过，不是自动图像识别或独立视频复核 |

上述三处False的赋值可直接从配套运行源码核对。归档工具把operator_checks复制后，按七项值全部为True计算all_operator_checks_confirmed；它没有读取这三个占位字段作为视觉检测结果。七项人工记录与设备状态保留各自身份，原JSON不改值。

本次纸盒记录包含步骤和触点状态，七项操作结论来自人工填写；尚无该轮实拍视频可供独立核查，正常结束按钮退出也未在本轮记录中完成。因此只登记其人工报告范围，不据此宣布显示观感已获独立复验。

data/verification/board_run_review.md说明的是另一圈早期摆件。它的display_confirmed=false和待实拍说明不用于覆盖本次纸盒，也不因纸盒人工报告而回写旧结果。

## 归档写入代码

实际归档入口及导出函数随库提供，见[归档工具](../../../board/tools/README.md)。Notebook为返回ZIP内的原字节快照，模块保留相同的_b_export函数；其operator_checks输入、七项校验和all汇总可直接检查。历史build/archive_validation.py是主机日志整理脚本，与这七项板上记录的写入无关。
