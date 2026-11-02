# 开放字模生成

公开副本使用Noto Sans CJK SC，固定上游提交与字体SHA见third_party/fonts/source.json。字体源文件不随库重复打包，可从该记录中的官方URL取得。

```text
python tools/generate_font.py --font NotoSansCJKsc-Regular.otf --characters tools/font_characters.txt --output new_scan_font.bin
```

生成需要Pillow/FreeType。每个字形为32×32、逐行MSB优先的128字节，采用30px字形、居中和128阈值。运行端仅加载已生成bin，不需要TTF/OTF或Pillow取模。

766个字符均有非空字形，加载接口保持packed(char)。新字模发行名为Scan3D Raster CJK，数据按OFL-1.1分发；加载和生成代码为MIT。字形外观与旧字体不同，本次板上记录已确认中文可读；各屏幕的外观仍应结合实际字体和布局检查。
