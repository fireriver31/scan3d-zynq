# 近似长方体单次扫描

2026-11-01收到本次板上归档。板上时钟仍为2019年，记录按运行文件SHA、同一boot_id和步骤事件关联。完整ZIP有119项、118条导出SHA，CRC和逐项指纹已核对。

这里提供原始result、严格占据和验收记录子集。source_index.json列出它们在原始ZIP中的位置和SHA；各原始文件逐字节保留。完整照片、细化数组、模型和运行备份位于比赛包中的returned_evidence.zip，不纳入源码版文件。

volume_reference.json记录直尺尺寸12.2×9.2×5.4cm及近似长方体计算方法；volume_comparison.csv分别列主测量和显示外壳。量具分辨率和尺寸复测未记录，尚无预设通过阈值。

在仓库根目录执行：

```text
python build/check_board_acceptance.py --output ../cuboid_acceptance_check.json
```

脚本复算严格占据与体积，检查参考乘积、误差、源码指纹、时钟、DDR和八步记录；不访问开发板。结果见[实验说明](../../../report/board_volume_experiment.md)。

## 被测源码对应

[快照说明](tested_snapshot.md)区分验收时的原始源码清单和当前交付清单；[映射索引](tested_snapshot.json)记录38份运行软件及bit/hwh配对关系。原始JSON的来源标签与SHA保留，核验从保存的原始快照进行。

人工操作记录与自动状态占位字段的区别见[字段说明](field_meanings.md)，原始值均保留。
