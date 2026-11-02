# PYNQ集成核验技能包 v1

## 能解决什么

给其他队伍一套可复用的检查方法和只读工具，判断故障位于元数据/寄存器接口、DMA传输、缓存/打包布局还是算法与后处理。输入为待核验的bit/hwh、实际输出NPZ和独立参考NPZ；输出是JSON核查报告。

本包不加载FPGA，不写MMIO、不改时钟，不实现一套适配所有IP的DMA读写驱动。真实加载和板上运行继续使用目标工程自己的已验证入口。

## 使用方法

Python 3.6及以上；Overlay脚本仅标准库，数组对照另需NumPy。

```text
python scripts/overlay_audit.py --bit path/design.bit --hwh path/design.hwh --expect-ip recon_0 --out results/overlay.json
python scripts/compare_arrays.py actual.npz reference.npz --key packed --out results/compare.json
```

IP名称和字段名称由自己的工程提供，例子中的`recon_0`、`packed`不是固定要求。退出码0表示所请求的检查通过，2表示未通过。输出文件不能覆盖输入文件。

`overlay_audit.py`检查文件存在、成对名称、可识别的Xilinx bit头部与HWH器件/封装、所要求IP和地址信息，并记录SHA256。**它不能证明bit/hwh来自同一次构建，也不能证明这份bit已经加载上板。** 时钟字段是HWH声明，不是板上读回。

`compare_arrays.py`必须显式指定至少一个字段，可重复`--key`。严格检查dtype、shape、每个元素；报告差异数及首个位置。适合整数、布尔和打包位。浮点不设容差，NaN按差异处理，不适合替代带误差预算的浮点算法验证。

## 复用流程

1. 记录当前输入、软件和硬件指纹。
2. 离线检查bit/hwh和接口约定。
3. 按目标工程入口核对实际加载、内存保留和时钟读回。
4. 使用独立递增图案核对数据布局，再对真实数据执行实际PL/黄金模型比较。
5. 记录失败位置、执行环境及证据等级；改变一个环节后只重跑受影响的检查。

详细操作判断见`references/workflow.md`，提炼来源与真实失败见`references/cases.md`。

## 已验证效果与边界

工具的公开检查摘要保存在`../../sim/results/public_checks.json`。本版运行13项合成元数据与数组行为检查，包括单点破坏、dtype/shape差异及字段缺失；不附真实v99 bit/hwh。历史完整工程的288次PL比较另列在data中，工具本身只读本地文件，不驱动板上DMA。

重跑`tests/validate_tools.py --workdir 新输出目录`时，新的`validation.json`写到指定workdir，不覆盖输入bit/hwh。具备合法取得的实际bit/hwh时，可用可选参数追加元数据检查。

不能确定第三方许可、未提供原始数据、HWH缺少器件字段、bit格式不支持或参考不独立时，保留相应未核实状态。未提供的硬件信息不猜测，元数据通过不替代实测。

## 脱离本题的离线复用验证

`tests/validate_reuse.py --workdir 新外部目录`使用7z010、7z045、zu3eg三种设备元数据夹具，IP名分别为向量加法、传感器过滤和直方图；另用两类非扫描数组字段比较，并确认单点破坏会失败。工具从其他工作目录以命令行执行，不导入本题33份运行模块。结果见`../../sim/results/public_checks.json`。

这些是合成格式/跨任务验证，证明脚本没有绑定本题设备名和字段名，不是上述三块设备的真实上机记录。通用PYNQ能力的跨板实测加分仍按实际完成情况申报。

## 提炼来源、效果及失效条件对照

| 提炼项 | 来源 | 验证方式 | 失效条件 |
|---|---|---|---|
| Overlay元数据 | Hierarchy与寄存器IP误用、bit/hwh版本对应 | 器件/IP、头部格式和明确输入文件检查 | 无构建清单或实际加载证据时，只能确认文件元数据 |
| 数组逐项比较 | PL判定位与独立参考核对 | dtype/shape/字段缺失和单点破坏均应被检出 | 浮点容差另定；参考与实测共用错误布局时不能证明算法正确 |
| 布局与耗时工作流 | 像素次序错位、进度标题误导 | 独立图案分层核对，拆分有包含关系的计时 | 不能直接沿用本案例地址、频率或像素反转规则 |
| 提问与结果模板 | 反复补输入约定、阶段证据和版本信息 | 用非扫描IP/字段执行既有离线夹具 | 输入原件不足时保留未核实状态，不猜测硬件信息 |

[复用工作表](references/reuse_worksheet.md)提供可填写模板。其文字是复用说明，不是历史提示词原文。
