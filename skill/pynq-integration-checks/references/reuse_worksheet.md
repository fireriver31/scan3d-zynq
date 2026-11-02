# PYNQ集成问题复用工作表

## 何时使用

另一项目出现Overlay不匹配、寄存器访问异常、DMA结果与参考不一致，或进度显示与实际耗时不一致时，按对应问题填写。这里的文字是复用模板，不是历史对话原文，也不要求使用扫描工程的IP名或地址。

## 提问模板

```text
目标功能：[这次需要验证的接口或计算]
当前现象：[错误栈、首个差异、读回值或阶段耗时]
已有材料：[bit/hwh、PYNQ版本、IP名和地址依据、数组字段、源码SHA]
输入约定：[dtype、shape、像素/字节/位平面顺序、开始与结束条件]
执行约束：[仅离线核对，或允许使用本工程已有运行入口；需保留的输入和结果]
期望输出：先区分已证实的事实和待核实的原因；列出所需最小检查、判断依据和失败条件。
```

## 按问题选操作

| 问题 | 最小操作 | 保留什么 | 失效或停止条件 |
|---|---|---|---|
| bit/hwh疑似混配 | overlay_audit.py核对器件、模块及SHA，再关联构建清单 | 两文件SHA、工具JSON和构建产物对应表 | 格式不支持、HWH字段缺失，或没有实际加载记录时，不把元数据通过写成部署完成 |
| PL与参考不同 | 明确至少一个字段，用compare_arrays.py逐项比较 | 原数组、dtype/shape、差异数和首个位置 | 参考使用了同一未经核实的错误布局、浮点需要容差、字段或形状不对应时先修验证设计 |
| 位或像素次序不清 | 用目标工程已有入口输入独立递增图案，分别检查打包、传输与展开 | 小尺寸图案、每阶段预期/实际值、位宽顺序 | 不把本扫描工程的每4像素反转结论硬套到其他FIFO |
| 等待时间过长 | 单独记录PS准备、传输等待、后处理、保存的计时边界 | 开始/结束时刻及包含关系 | 输入、时钟或功能不一致时，不能直接给优化倍率 |

## 命令模板

从技能目录运行，输出使用新目录，字段/IP名按目标工程填写：

```text
python scripts/overlay_audit.py --bit input/design.bit --hwh input/design.hwh --expect-ip target_ip --out output/overlay.json
python scripts/compare_arrays.py input/actual.npz input/reference.npz --key output_field --out output/compare.json
python tests/validate_tools.py --workdir output/tool_checks
python tests/validate_reuse.py --workdir output/reuse_checks
```

前两条核对提供的材料；后两条检查工具自身的异常检测与离线复用能力。技能不会自动下载bit或操作DMA。真实工程的加载、内存保留、缓存所有权和完成条件按其接口执行。

## 结果登记模板

```text
检查问题：
输入文件与SHA：
执行环境/工具版本：
证据类型：[主机检查 / RTL仿真 / 板上实测]
实际结果与原始记录位置：
已确定原因：
尚不能区分的原因：
采用的修改及受影响范围：
未采用建议及原因：
复验输入、输出和剩余限制：
```

无法取得必要原件时，输出缺失清单和已能确定的事实。记录“未核实”，不补写测量值、加载状态或现场操作。
