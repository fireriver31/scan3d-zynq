# 数字比较与历史结果

reference_summary.json记录原完整比赛工程的一次运行数值和输入指纹。reference_comparisons.csv汇总288次PL判定、128³占据及体积换算的数字参考比较；pl_reference_comparison.csv逐项列出PL判定结果。两张资源与显示比较表记录完整版本资源和指定压力条件下的显示表现。

**历史原始输入和输出未随公开库发布。** CSV的actual_source是原完整提交工程内的相对路径，actual_source_scope标为competition_submission_v11，evidence_in_public_repo明确为False。[归档索引](evidence_sources.json)登记原文件大小和SHA，不把指纹当作可重放原件。masks.npz、occupancy.npy和result.json均已在该归档中核对存在；复跑这份历史比较需要对应原始数组。

公开可重跑的验证使用sim/generate_core_vectors.py生成的确定性合成输入，命令及实际日志见[验证说明](../sim/README.md)。两类验证分别记录。

历史体积86.958081cm³没有独立物理参考，原结果形状检查为needs_review。性能数据按主机组件、RTL压力仿真和单次板上运行分别归档，比较范围见[优化说明](../report/optimization_comparison.md)。

## 近似长方体实测

[cuboid_606](board_acceptance/cuboid_606/README.md)提供新组合的一次板上结果、严格占据和验收记录子集。直尺尺寸得到参考606.096cm³，主测量637.328cm³，偏差5.153%。此目录可独立复算体积；上述历史摆件的原始数据范围保持不变。

## AI辅助记录

[ai_collaboration](ai_collaboration/README.md)包含任务整理、三组真实交互选段、一则模型分析记录中的自我纠错案例及四个技术主题的引用索引。原文与整理说明分开；标为比赛归档的附件不作为仓库内可打开的文件。

## 人工检查的写入来源

[board/tools](../board/tools/README.md)保存返回归档中的已执行验收Notebook及导出函数。七项人工输入、自动状态占位字段和设备读回的区别见[字段说明](board_acceptance/cuboid_606/field_meanings.md)。
