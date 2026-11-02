# 被测快照与当前代码包

## 清单的用途

tested_source_manifest.sha256是验收当时保存的原始源码清单，共204条。其文件SHA256为86269aee313d5b5d2812428d54fa0e5a57a139c0ea2760b035f9a874ebdcd475，与原始acceptance_summary.json中的public_source_manifest_sha256一致。

当前仓库根manifest.sha256描述本次交付，新增说明、索引和核验工具后会变化。它与被测历史清单分别保存，不能拿当前整库清单的SHA去替换验收时的SHA。

## 本次测了哪些程序

tested_snapshot.json逐项列出38份受测运行软件：33份Python、2份C、2份Notebook和Noto字模。它们的SHA同时匹配原始源码清单、验收载荷和当前src/runtime。当前版本的说明、引用索引、主机核验工具和文件组织与原快照存在差异，具体范围见下节；后续内容不计入当时的板测。

验收还配套使用scan3d.bit/hwh，故部署文件合计40份。原始源码快照不包含这两个预构建文件，它们由验收载荷单独记录SHA；bit为c563373a开头，hwh为bbfe9e71开头，构建来源为完整工程20261031_packaged。完整比赛包保存同字节产物及构建报告；源码版只保留对应指纹，不附整机预构建文件。

## 原路径逐项差异

原204条清单与当前交付目录逐路径比较：149条逐字节相同、55条SHA不同、0条未按原路径提供。当前目录另有47份文件不在原清单路径中，包含说明、工具和新记录。38份受测运行软件仍逐项相同。

当前目录与被测快照同时存在新增、修改及组织范围差异。后续说明、隐私处理、协作摘要和主机工具不计入当时板测。历史报告与日志已移除本机路径，授权截图已遮挡购物账号，原SHA和交付副本SHA见data/privacy。位流、运行算法、实测占据和原始板测ZIP未改。

逐项原SHA、当前SHA和分类见[snapshot_comparison.json](snapshot_comparison.json)。比赛完整包与源码版的目录范围不同，各自使用本目录的比较结果。

## 版本名称与原始证据

原始JSON里的source_edition和核验输出里的source_edition_tested是验收时的来源标签，保持原值便于追溯；正文称“本次配套程序”。当前交付的文档版次不改写历史被测标签。原始acceptance_summary、result、占据和回传ZIP均不改字节。

在仓库根目录运行build/check_board_acceptance.py，输出到新的外部JSON。脚本核对被测清单SHA及38份软件，再检查配对载荷、步骤、时钟、DDR和体积；不访问板卡。
