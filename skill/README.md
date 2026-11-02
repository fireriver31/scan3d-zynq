# PYNQ检查技能

子目录pynq-integration-checks提供bit/hwh元数据、DMA布局及数组比较方法。两只读工具输入由调用工程指定，MIT许可；测试夹具不需要真实硬件。

公开检查运行13项工具行为测试和5个跨任务/设备元数据夹具案例，不附旧v99 bit/hwh。工具不会下载bit或写MMIO。结果见sim/results/public_checks.json，操作说明见子目录README和SKILL.md。

## 提交内容与阅读入口

| 内容 | 位置 | 用途与适用范围 |
|---|---|---|
| 模型工作流 | [SKILL.md](pynq-integration-checks/SKILL.md) | 选择元数据、寄存器、DMA或数组核对路径 |
| 使用说明 | [README](pynq-integration-checks/README.md) | 输入、命令、退出码、验证效果与失效条件 |
| 实际案例 | [cases.md](pynq-integration-checks/references/cases.md) | Hierarchy误用、像素布局、时钟与耗时等失败的提炼依据 |
| 可复用模板 | [reuse_worksheet.md](pynq-integration-checks/references/reuse_worksheet.md) | 按其他项目的IP/字段填写提问和结果记录，避免照搬扫描参数 |
| 只读工具 | scripts/overlay_audit.py、scripts/compare_arrays.py | 本地文件核对；不加载硬件、不写MMIO |
| 工具与复用测试 | tests/validate_tools.py、tests/validate_reuse.py | 格式/字段错误及非扫描合成夹具；不等于真实跨板验收 |

技能的提炼过程、采用与未采用的建议及工程心得见[设计报告第9章](../report/design_report.md)。
