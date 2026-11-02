# 公开验证入口

| 入口 | 输入与范围 |
|---|---|
| build/run_checks.py | 主机检查；日志编码与脱敏回归、触控控制器、模拟PYNQ事务、13项技能工具测试、5个离线复用案例和确定性向量 |
| run_rtl.py | 9组触摸/显示读取/寄存器RTL测试，自研模块和行为夹具 |
| run_core_rtl.py | recon_top的两种投影，photo_mask_axis的协议、合成图案与随机图案，共5种情况 |

在仓库根目录执行：

```text
python build/run_checks.py --output ../public_host_checks
python sim/run_rtl.py --vivado-bin /path/to/Vivado/2026.1/bin --output ../public_touch_rtl
python sim/run_core_rtl.py --vivado-bin /path/to/Vivado/2026.1/bin --output ../public_core_rtl
```

输出目录须在仓库外且尚不存在。公开输入为确定性合成图案，photo_pattern不使用私有实拍；雕刻测试使用128×128掩模、24视角及32³测试占据，不能推作128³组合实现的资源或板级精度结论。

## 随库保存的结果与日志

[主机8组](results/public_checks.json)、[触摸与显示9组](results/public_rtl_checks.json)、[运算核5组](results/public_core_rtl_checks.json)和[工程迁移检查](results/public_project_checks.json)均附可解析的日志路径。摘要的log_base为sim/results，log_scope为repository；日志实际位于results/logs。编译与展开日志也一同保存。

[日志来源表](results/log_provenance.json)记录原始输出SHA、公开副本SHA和路径替换标记。测试脚本原样保存标准输出字节；归档先在字节层匹配UTF-8及GB18030/GBK路径，再严格解码成UTF-8，不用替换字符吞掉坏字节。路径按实际出现替换成$REPO、$OUTPUT_*或$USER，每个令牌的生效次数单独登记，零次表示未遇到。工具版本、测试名、警告和结果保留；未识别编码、已有乱码替换字符及残留个人路径会使归档或审计失败。原始输出仍在执行时指定的外部目录。主机public_package.log检查的是该次结果入库前的密封仓库，入库后应再运行seal_public和audit_public核对最终清单。

## 新一轮结果入库

三个测试入口生成的原始JSON明确标记log_scope=external_output_directory，裸日志名只在对应--output内解析。完成工程检查后，将同一次执行的四个外部目录一起入库：

```text
python build/archive_validation.py --host ../public_host_checks --touch ../public_touch_rtl --core ../public_core_rtl --project ../scan3d_project_check
python build/seal_public.py
python build/audit_public.py --output ../public_final_audit.json
```

入库脚本保留原始输出，生成公开副本和原始/公开SHA；audit_public检查22个测试日志引用、工程检查日志和逐个公开日志SHA。不能只复制摘要JSON而遗漏日志。

整机相机、显示FIFO网表和真实DDR压力测试不在上述模块测试范围；Vivado工程迁移不包含全量综合。历史实机原始数组未随库发布，范围见[data说明](../data/README.md)。

## 配套板测证据复核

`python build/check_board_acceptance.py --output ../board_acceptance_check.json`核对本次运行指纹、同次启动的时钟和DDR、八步事件、占据体积及直尺参考误差。原始记录为本次配套程序的实机运行；此脚本本身是离线核验，不加载硬件。现场人工确认、自动快照和中断退出分别列出，详见[实验说明](../report/board_volume_experiment.md)。
