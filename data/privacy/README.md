# 交付副本的隐私处理

交付文件不提供本机个人路径或提问方购物账号。工具报告和日志使用路径脱敏副本，测试名称、数值和判定保持；授权截图只遮挡提问方账号。

[redaction_manifest.json](redaction_manifest.json)记录技术报告与日志的原文件SHA、交付SHA及路径替换范围。已有乱码仅在完整路径标记内遮去，不猜测字符。build/evidence_privacy.py核对历史证明与交付副本的对应关系，整包manifest另覆盖当前文件。

授权图的遮挡范围与文件SHA在third_party/authorization登记，问句、回复、店铺及时间保留。
