# 验收归档写入工具

acceptance_capture.ipynb是本次returned_evidence.zip内保存的原Notebook快照；文件路径、大小和指纹登记在source.json。acceptance_support.py从其第一个代码格提取初始化调用之前的定义，_b_export函数原样保留，模块导入不执行安装。

## 七项人工检查如何写入

_b_export(state, operator_checks)先复制人工传入的字典，检查七个键及True/False/None类型，再计算：

```python
checks = dict(operator_checks)
all_operator_checks_confirmed = all(checks.get(key) is True for key in needed_checks)
```

七项True来自人工输入的汇总，视觉快照中的默认False不参与该表达式。原Notebook快照保留当次归档入口，当前模块可供直接检查写入过程。

## 使用范围

这是历史验收归档工具，不替代src/runtime中的主扫描入口。原Notebook的安装格依赖当时的1101_scan3d_board_payload_v1.zip，执行时会备份并安装载荷；公开源码版不附该整机载荷。查看代码或导入acceptance_support.py不需要载荷，也不触发硬件访问。

归档已有会话时，_b_export需要该次初始化产生的state和人工填写的operator_checks。输出包括本轮扫描、配套源码、环境及人工项。它不自动评判屏幕观感，不能代替操作视频。

board/tools下的写入工具与build/archive_validation.py不同：后者是历史主机日志整理入口，不生成这七项板上记录。
