# 第三方材料与许可范围

根目录[LICENSE](LICENSE)为混合许可范围索引；标准[MIT文本](licenses/mit.txt)仅适用于[逐文件清单](third_party/file_licenses.json)中标为MIT的项目自研代码和文档。下列材料分别采用原许可或本项目取得的专项授权，原有署名、版权头和链接保留。

## 1. ACZ702相机与显示例程

| 项目 | 记录 |
|---|---|
| 例程 | ACZ702的ov5640_ddr3_tft_hdmi及本项目修改版源文件 |
| 原文件署名 | 保留文件头中的武汉芯路恒科技有限公司等原始署名 |
| 原始链接 | http://www.corecourse.cn ；http://xiaomeige.taobao.com |
| 授权答复店铺 | 小梅哥FPGA企业店 |
| 回复方账号 | zgmxs88:小梅妹 |
| 请求与回复时间 | 2026-11-01 14:16:24 / 14:16:53 |
| 请求的分发方式 | 在本项目公开GitHub仓库保留并分发所述例程的修改版源文件 |
| 请求中的使用承诺 | 仅用于比赛、学术交流，绝不用于任何商业盈利；保留厂家署名和原始链接 |
| 对方原文回复 | 可以 |

[脱敏截图](third_party/authorization/20261101_acz702_permission_v1.png)、[文字转录](third_party/authorization/20261101_acz702_permission_v1.md)和[文件指纹](third_party/authorization/20261101_acz702_permission_v1.json)随库保存。截图仅遮挡提问方购物账号，授权问句、回复、店铺和时间保留，遮挡区域外像素一致。原图私有留存，原SHA与交付副本SHA分别登记。

本项目按上述请求范围使用和分发对应源码，著作权仍归原权利人。`LicenseRef-ACZ702-Project-Permission`指向这份专项记录，不是新拟定的通用许可证。该记录没有将厂家代码改授MIT，也没有明确授予任意下游商业使用或转授权；超出记录范围的用途应向原权利人另行确认。

对应文件及SHA列在[source_inventory.json](third_party/source_inventory.json)。独立自研模块按MIT列示，含厂家原代码的修改文件仍按专项记录处理。预构建整机bit/hwh未随本源码版提供，构建步骤见[build/README.md](build/README.md)。

## 2. AMD生成配置与工具依赖

Vivado工程中的BD、XCI、XPR和system_wrapper属于工具生成的配置或封装。保留已有声明，清单记为`LicenseRef-AMD-Generated-Configuration`；自研MIT及店铺回复均不替代AMD对IP和工具的适用条款。仓库提供配置及构建入口，不附Vivado安装文件或生成的专有IP实现源码。AMD Core License第3.2条涉及其定义的Licensed Materials的位流分发，并不自动授予整机中其他权利人的设计内容许可；本源码版未仅凭该条款附入整机bit/hwh。可参阅[AMD许可入口](https://www.amd.com/en/products/adaptive-socs-and-fpgas/intellectual-property/license.html)，实际构建和分发按所用IP的适用条款核对。

## 3. PYNQ参考源

`sim/reference/pynq_v24_pl.py`与`pynq_v24_overlay.py`来自Xilinx/PYNQ v2.4，保留原始字节、版权头和BSD-3-Clause条件，原文另存[third_party/pynq_bsd.txt](third_party/pynq_bsd.txt)。

## 4. 新字模

`src/runtime/scan_font.bin`从Noto Sans CJK SC生成，含766个32×32字形，发行名为Scan3D Raster CJK，采用[OFL-1.1](third_party/fonts/OFL.txt)。来源提交、字体SHA及生成参数见[fonts/source.json](third_party/fonts/source.json)。加载和生成代码为项目自研MIT，字模数据保持OFL。原来源未明的旧字模未随库提供。

## 5. 授权证明附件

`third_party/authorization/`保存授权证明的账号脱敏副本及其转录，清单记为`LicenseRef-Authorization-Evidence`。保存证明不等于将其中的第三方聊天文字、头像或商标另行授予MIT。

## 6. 仓库整体表述

自研部分采用MIT，第三方文件分别按原权利人许可或专项答复提供。厂家例程沿用本项目比赛、学术交流、非商业和署名的授权范围；PYNQ参考代码保留BSD，新字模采用OFL，AMD配置保留其适用条款。文件适用范围以[逐文件许可](third_party/file_licenses.json)为准。

## 7. 验证记录

工具日志与报告按LicenseRef-Validation-Record提供。交付副本移除了已识别的本机路径；早期乱码若完全位于该路径段，随路径一并遮去，未猜测恢复字符。路径外的测试名称、数值、警告和判定保留。原件私有留存，原SHA、交付SHA及逐段替换记录见[隐私处理说明](data/privacy/README.md)；后续主机检查另有各自日志来源表。

## 8. 板上验证记录

data/board_acceptance中的原始结果子集、来源索引与体积记录按LicenseRef-Validation-Record列示，数据关联到该次样品及配套程序。原result和自动快照保持原字节；不以记录中的文件名指代可公开分发的整机bit/hwh。

## 9. AI协作说明

data/ai_collaboration提供任务摘要与选择性真实交互片段，分别标注原文、整理说明、团队取舍和验证入口，记为LicenseRef-Project-Evidence。项目自研代码的许可范围与工具输出或第三方材料分开列示。
