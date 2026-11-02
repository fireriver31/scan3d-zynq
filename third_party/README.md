# 来源、专项授权与许可

2026-11-01，小梅哥FPGA企业店客服针对本项目“公开仓库保留并分发ACZ702例程修改版源文件”的请求回复“可以”。请求同时承诺比赛及学术交流、非商业使用，并保留厂家署名和链接。账号脱敏截图、文字转录及SHA保存在[authorization/](authorization/20261101_acz702_permission_v1.md)。

根LICENSE是混合许可范围索引；licenses/mit.txt仅覆盖逐文件标为MIT的自研部分。各来源的适用范围集中列在[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)，当前状态见[license_status.json](license_status.json)。

| 内容 | 来源 | 许可与处理 |
|---|---|---|
| 新增Python/C、独立RTL、测试、工具和文档 | Scan3D项目 | MIT，逐文件列在file_licenses.json |
| ACZ702例程及其修改版源码、原厂家管脚文件 | 所述相机/显示例程 | 已取得本项目范围的源码分发答复；按非商业请求及署名条件登记，保留原头部与链接 |
| XPR、BD、XCI、system_wrapper | Vivado工程配置/生成封装 | 保留原声明和AMD适用条款，不改标MIT |
| PYNQ v2.4两份参考源 | Xilinx/PYNQ | 原BSD-3-Clause及原字节保留，另附pynq_bsd.txt |
| 新766字符字模scan_font.bin | Noto Sans CJK SC | OFL-1.1；生成及加载代码MIT |
| 授权截图与转录 | 本项目收到的店铺答复 | 保存为授权证据，不改授MIT |
| 验证日志和报告 | 项目测试及Vivado/XSim输出 | 验证记录；保留第三方声明，日志路径替换及SHA单独登记 |

原厂家署名“武汉芯路恒科技有限公司”等及http://www.corecourse.cn、http://xiaomeige.taobao.com在文件头和本说明保留。41份硬件源及指纹见[source_inventory.json](source_inventory.json)。独立自研模块适用MIT，厂家文件及包含厂家原代码的修改文件适用专项记录。

旧字模、预构建bit/hwh、实拍和私人附件未随库提供，详见[排除清单](excluded_files.json)。本次答复不是厂家材料的通用MIT/Apache授权，也未记载任意下游商业使用或转授权。

PYNQ原文来源：

- https://github.com/Xilinx/PYNQ/blob/v2.4/pynq/pl.py
- https://github.com/Xilinx/PYNQ/blob/v2.4/pynq/overlay.py
