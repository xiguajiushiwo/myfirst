# -*- coding: utf-8 -*-
"""把痛点清单.md 转成自包含 HTML：图片压缩后 base64 内嵌，双击浏览器即可看到图，位置不变。

用法：python tools/md_to_html.py 痛点清单.md [输出.html]
"""
import base64
import io
import os
import re
import sys

import markdown
from PIL import Image

MAX_W = 1600          # 大图最长边压到这个像素（够看清、又不让 HTML 太大）
JPEG_Q = 85


def embed_img(src: str) -> str:
    """把图片文件读出来、必要时压缩，返回 data URI；找不到就原样返回。"""
    if src.startswith("data:") or not os.path.exists(src):
        return src
    try:
        im = Image.open(src)
        w, h = im.size
        big = max(w, h) > MAX_W
        if big:
            f = MAX_W / max(w, h)
            im = im.convert("RGB").resize((int(w * f), int(h * f)))
        buf = io.BytesIO()
        if big or im.mode == "RGB":            # 大图/照片走 JPEG（小很多）
            im.convert("RGB").save(buf, "JPEG", quality=JPEG_Q)
            mime = "image/jpeg"
        else:                                   # 小图（如示意图）保 PNG
            im.save(buf, "PNG")
            mime = "image/png"
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        print("图片处理失败", src, e)
        return src


def main():
    md_path = sys.argv[1] if len(sys.argv) > 1 else "痛点清单.md"
    out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(md_path)[0] + ".html"
    base_dir = os.path.dirname(os.path.abspath(md_path))

    text = open(md_path, encoding="utf-8").read()
    body = markdown.markdown(text, extensions=["tables", "nl2br", "sane_lists"])

    # 把 <img src="xxx.png"> 的 src 换成内嵌 data URI（相对路径按 md 所在目录找）
    def repl(m):
        src = m.group(1)
        return 'src="%s"' % embed_img(src if os.path.isabs(src) else os.path.join(base_dir, src))
    body = re.sub(r'src="([^"]+)"', repl, body)

    title = os.path.splitext(os.path.basename(md_path))[0]
    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
 body{{font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;line-height:1.7;
   color:#222;max-width:960px;margin:24px auto;padding:0 18px}}
 h1{{font-size:26px;border-bottom:3px solid #1F4E79;padding-bottom:8px;color:#1F4E79}}
 h2{{font-size:20px;margin-top:34px;background:#1F4E79;color:#fff;padding:6px 12px;border-radius:6px}}
 table{{border-collapse:collapse;width:100%;margin:12px 0}}
 th,td{{border:1px solid #ccc;padding:7px 10px;text-align:left;vertical-align:top}}
 th{{background:#1F4E79;color:#fff}}
 tr:nth-child(even){{background:#f5f7fa}}
 blockquote{{border-left:4px solid #1F4E79;background:#eef3f9;margin:12px 0;padding:8px 14px;color:#333}}
 img{{max-width:100%;height:auto;border:1px solid #ddd;border-radius:6px;margin:8px 0;display:block}}
 strong{{color:#c0392b}}
 code{{background:#eee;padding:1px 5px;border-radius:4px}}
 hr{{border:none;border-top:1px dashed #bbb;margin:24px 0}}
</style></head><body>
{body}
</body></html>"""
    open(out_path, "w", encoding="utf-8").write(html)
    print("已生成", out_path, f"({os.path.getsize(out_path)/1024/1024:.1f} MB)")


if __name__ == "__main__":
    main()
