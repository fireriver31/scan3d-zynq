# 检查与全量构建入口

`python build/run_checks.py --output 新外部目录`运行包清单、触控、RPC、技能工具及合成向量检查，不加载Overlay。`python build/audit_public.py --output 新外部JSON`核对源文件、Python3.6语法、授权证据、字模、链接、逐文件许可和指纹，并检查AI选段的引用范围与验收归档来源。编辑后用`python build/seal_public.py`更新清单；未知硬件文件的许可类别须先登记。

## Vivado

工具Vivado 2026.1，器件xc7z020clg400-1，无HLS依赖。在仓库根目录执行：

```text
vivado -mode batch -source build/check_project.tcl -tclargs ../scan3d_project_check
vivado -mode batch -source build/build_hardware.tcl -tclargs ../scan3d_build
```

输出目录须尚不存在且位于仓库外，推荐短英文路径。脚本复制hardware树后操作副本。check_project检查引用、器件、BD与IP状态；build_hardware完成IP生成、综合、布局布线和bit/hwh导出，内部setup/hold负裕量或严重DRC会退出失败。

synthesis_resources.tcl和build_hardware.tcl保持同级，综合前置设置general.maxThreads=1。C辅助模块由目标板gcc生成，外部工具及依赖按原许可使用。

本源码版不附预构建bit/hwh。原v11的完整构建属于历史证据，说明见[构建记录](../report/build_reproduction.md)。新构建输出保留scan3d.bit/scan3d.hwh原名，与旧实机v99产物分别记录。内部时序通过不代替外部IO与相机复位检查。

检查输出放在外部目录。需要随库保存时，用archive_validation.py同时归档主机、两类RTL及工程检查的JSON和日志；命令和路径替换规则见[验证说明](../sim/README.md)。归档后依次seal_public、audit_public，核对最终清单。


## 源码发布检查

发布内容为scan3d-zynq目录内的源文件、文档和验证记录，不包含上级工作目录。使用独立Git仓库，并在其根目录检查：

```text
git rev-parse --show-toplevel
git rev-parse --absolute-git-dir
git rev-parse --show-prefix
git status --short
```

顶层应为scan3d-zynq自身，Git目录应为其内部.git，show-prefix应为空。再按manifest逐项检查待提交文件，确认没有带入其他项目的历史或私人材料。需要另建副本时，只复制发布文件，并在副本中git init，不复制父目录的.git。配置远程、创建提交和推送属于后续发布操作。

制作源码压缩包时排除本地`.git`目录；它不属于manifest列出的发布文件。

## 报告与日志的隐私副本

本规则同时覆盖reference_reports、reproduction_reports、sim/reference_logs、sim/results及data/optimization_evidence。工具安装目录、个人目录和历史工作目录使用占位符替代，测试数值和判定不变。带路径脱敏的文件不再称为原字节，历史构建证明中的原SHA通过[脱敏清单](../data/privacy/redaction_manifest.json)绑定到交付副本，校验入口为build/evidence_privacy.py。原件仅私有留存。

## 文件字节与行尾

.gitattributes使用`* -text`关闭Git自动行尾转换，保留清单对应的文件字节。主动编辑（包括GitHub网页编辑）后，重新执行seal_public.py及audit_public.py，再提交更新。

来源版本与历史标签见[来源说明](../third_party/source_history/README.md)。
