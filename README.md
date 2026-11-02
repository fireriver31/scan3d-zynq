# 单相机转台三维重建与体积测算

本仓库包含项目源码、技能包、设计报告及选定验证记录。比赛完整包另含完整实测附件和配套bit/hwh，两者文件范围分别说明。

本项目使用OV5640、转台和Zynq-7000/PYNQ实现多视角轮廓重建、体积估算、照片着色及GT911触摸操作。本仓库提供项目软件、RTL、Vivado源工程、测试及技能工具。

**仓库整体采用混合许可，[根LICENSE](LICENSE)是范围索引；[MIT文本](licenses/mit.txt)仅覆盖逐文件清单中的自研部分；ACZ702例程源码依据2026-11-01的本项目非商业分发授权，PYNQ参考与新字模分别保留BSD和OFL。** 范围与证据见[第三方通知](THIRD_PARTY_NOTICES.md)。仓库已恢复所述例程的Vivado源工程及构建入口，保留原文件中的“武汉芯路恒科技有限公司”等署名，以及http://www.corecourse.cn、http://xiaomeige.taobao.com原始链接。

本源码版不附预构建整机bit/hwh、旧字模或原始实拍附件，首次部署需要先构建。原比赛提交包另行保留，构建与部署条件见[硬件说明](hardware/README.md)。

项目仓库：[fireriver31/scan3d-zynq](https://github.com/fireriver31/scan3d-zynq)。

## 功能与划分

| 功能 | 实现 |
|---|---|
| 转台标定、标记测速、24视角采集组织 | PS Python |
| 前景强弱像素判定 | PL photo_mask_axis，PS完成区域归属和掩模后处理 |
| 多视角体素雕刻 | PL recon_top、recon_axil、voxel_carver |
| 占据计数及体积 | PS读取位图后计数，按体素边长换算 |
| 外形细化、照片着色和PLY导出 | PS Python/C |
| 触摸流程、模型旋转与查看 | GT911 RTL与PS界面程序 |

## 目录

| 路径 | 内容 |
|---|---|
| src/runtime | PS软件、C辅助模块及Notebook入口；新OFL字模 |
| hardware/rtl、hardware/vivado | 自研前景核、完整RTL/BD/XCI/XDC/XPR源工程 |
| hardware/tests | 重建测试台及确定性向量 |
| sim | 触摸/RPC主机测试、自研RTL功能仿真和合成数据生成 |
| build | 源码版主机检查、清单校验及依赖 |
| board | 构建与部署条件、接口、验收归档工具及已执行Notebook快照 |
| data | 实测结果与体积复算、AI辅助选段及模型分析记录、隐私处理索引 |
| skill | PYNQ元数据与数组比较工具 |
| report | 完整12章设计报告源稿、功能框图、优化比较与工程过程 |
| tools | 开放字体的字模生成工具 |
| third_party | BSD/OFL原文、逐文件许可及排除清单 |

## 主机检查

Python3.12，依赖见build/requirements_host.txt。在仓库根目录执行，输出选择尚不存在的外部目录：

```text
python build/run_checks.py --output ../scan3d_host_checks
python sim/run_rtl.py --vivado-bin /path/to/Vivado/2026.1/bin --output ../scan3d_touch_rtl
python sim/run_core_rtl.py --vivado-bin /path/to/Vivado/2026.1/bin --output ../scan3d_core_rtl
```

前一项为主机软件与公开包检查，后两项需要已安装Vivado/XSim，只仿真自研RTL。整机综合入口及工具依赖见build/README；光学采集和实体屏操作需真实开发板。检查结果和覆盖范围见[验证说明](sim/README.md)。

## 上板条件

原实机环境为XC7Z020CLG400-1、PYNQ2.4、Python3.6.5及Vivado 2026.1。运行源码保持Python3.6语法；Noto字模已随本次配套组合在板上使用，中文显示有现场人工确认。

Notebook需配套Overlay，且初始化会访问固定DDR缓存。**先按[板上说明](board/README.md)构建并核对bit/hwh、保留内存、时钟和IP接口，再运行Notebook。** 工程未配置开机自启动。本次配套程序、新字模与配套构建已取得本次板上运行记录。

## 配套上板与体积对照

2026-11-01归档的近似长方体实验完成步骤1至8，38份运行软件与配套bit/hwh合计40份部署文件，SHA一致，初始化与导出均为50MHz。直尺尺寸12.2×9.2×5.4cm得到参考606.096cm³，严格占据体积637.328cm³，相对偏差5.153%。本仓库src/runtime提供其中38份软件，bit/hwh按构建说明生成。这是一个样品的一次对照，形状检查仍标记needs_review，末尾退出记录为KeyboardInterrupt。

[实验说明](report/board_volume_experiment.md)列明测量方法、主测量和显示外壳差异；[证据及复算](data/board_acceptance/cuboid_606/README.md)提供原始结果、严格占据和验收记录子集。完整照片和ZIP留在比赛包，不随源码版包含。[验收归档工具](board/tools/README.md)提供当时执行的Notebook和七项人工检查的写入代码；原Notebook的13段输出及中断记录保留。

## 历史结果与限制

历史完整工程的一次扫描记录：第7步750.328s、第8步596.768s，严格占据123392体素、体素边长0.8898995384mm，体积估计86.958081cm³。记录中的288次PL判定及128³占据与数字参考零差异。数值摘要、数据指纹和未公开标记见[data](data/README.md)。masks.npz、occupancy.npy和result.json保存在原比赛完整归档中，未随本库发布。

这些历史结果与本次近似长方体实验分别记录。样品没有独立体积真值，部分掩模和形状检查仍有限制；区域超预算问题也保留在原算法中。性能比较按RTL压力仿真、主机组件计时和单次板上记录分别列示，详见[优化比较](report/optimization_comparison.md)。

## 技术报告与其他提交材料

完整[12章技术报告源稿](report/design_report.md)和功能框图保留在report目录；[系统摘要](report/system_design.md)便于快速查看。正式模板PDF、演示视频和签署授权书在赛事网站各自栏目提交，未包含在本源码库中。报告中的历史实测原件按[data归档索引](data/evidence_sources.json)区分，公开测试结果配有[实际日志](sim/README.md)。

## 许可

[根LICENSE](LICENSE)列出整库的许可分类，[MIT文本](licenses/mit.txt)仅适用于逐文件清单中的自研部分。厂家例程按本项目比赛、学术交流和非商业请求范围的答复处理；PYNQ为BSD-3-Clause，新字模为OFL-1.1，AMD配置保留原适用条款。完整范围见[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)及[逐文件许可](third_party/file_licenses.json)。

## AI辅助与分析记录

ChatGPT用于方案比较、代码检查、故障分析，以及测试脚本和文档的备选修改；WorkBuddy用于外部复审。核心RTL的AI辅助范围为方案设计、代码检查与故障分析，采用范围由工程验证决定。

仓库提供[协作说明](report/ai_collaboration_record.md)、[三组真实交互及模型自我纠错案例](data/ai_collaboration/selected_interactions.md)、[模型分析与建议摘录](data/ai_collaboration/technical_excerpts.md)。润色提问为整理稿，引用原文单独标注；模型分析记录与板上实测数据按各自来源保存。

构建、校验与源码发布说明见[build/README.md](build/README.md)。

## 比赛工程包的组成

“上传全部代码”的比赛RAR同时包含工程包、技能包和完整设计报告。公开仓库保留源码、技能及设计报告，原始私有附件和整机预构建文件的范围与比赛RAR不同。[报告第9章](report/design_report.md)说明AI协助的要点、采用与未采用的建议、工程心得和技能提炼。

授权截图与历史报告的隐私处理及原件/副本SHA对应关系见[隐私处理说明](data/privacy/README.md)。
