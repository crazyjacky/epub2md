#!/usr/bin/env python3
"""
remove_epub_indent.py
---------------------
消除 epub 书籍中段落开头的空格缩进。

支持的缩进形式：
  - 普通空格（ASCII space）
  - 全角空格（\u3000）
  - 不换行空格（&nbsp; / \u00a0）
  - HTML 实体：&nbsp; &#160; &#x3000; 等
  - CSS text-indent 样式（行内或外部 CSS）

用法：
  python remove_epub_indent.py input.epub [output.epub]

若不指定输出文件，则在原文件名后加 _no_indent 保存。
"""

import sys
import os
import re
import shutil
import zipfile
import tempfile
from pathlib import Path


# ── 正则：匹配段落标签内开头的各种空白字符 / HTML 实体 ──────────────────────
# 捕获 <p ...> 之后、实际文字之前的空白序列
LEADING_WHITESPACE_IN_P = re.compile(
    r'(<p[^>]*>)'           # 段落开始标签
    r'(\s|&nbsp;|&#160;|&#xA0;|&#x3000;|&#12288;|\u3000|\u00a0)+',
    re.IGNORECASE
)

# CSS text-indent（行内 style）
INLINE_TEXT_INDENT = re.compile(
    r'(text-indent\s*:\s*)[^;"\']+(;?)',
    re.IGNORECASE
)

# CSS 文件中的 text-indent 规则
CSS_TEXT_INDENT = re.compile(
    r'text-indent\s*:\s*[^;{}]+;?',
    re.IGNORECASE
)


def strip_leading_whitespace(html: str) -> str:
    """去除 <p> 标签内开头的空白/实体缩进。"""
    return LEADING_WHITESPACE_IN_P.sub(r'\1', html)


def strip_inline_text_indent(html: str) -> str:
    """把 style="... text-indent: 2em ..." 中的 text-indent 置为 0。"""
    return INLINE_TEXT_INDENT.sub(r'\g<1>0\2', html)


def strip_css_text_indent(css: str) -> str:
    """把 CSS 文件中所有 text-indent 值置为 0。"""
    return CSS_TEXT_INDENT.sub('text-indent: 0;', css)


XHTML_EXTENSIONS = {'.xhtml', '.html', '.htm', '.xml'}
CSS_EXTENSIONS   = {'.css'}


def process_epub(input_path: str, output_path: str) -> None:
    src = Path(input_path)
    dst = Path(output_path)

    if not src.exists():
        print(f"[错误] 找不到文件：{src}")
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)

        # 解压 epub（epub 本质是 zip）
        with zipfile.ZipFile(src, 'r') as zf:
            zf.extractall(tmp)

        modified_files = []

        for fpath in tmp.rglob('*'):
            if not fpath.is_file():
                continue

            suffix = fpath.suffix.lower()

            if suffix in XHTML_EXTENSIONS:
                original = fpath.read_text(encoding='utf-8', errors='replace')
                processed = strip_leading_whitespace(original)
                processed = strip_inline_text_indent(processed)
                if processed != original:
                    fpath.write_text(processed, encoding='utf-8')
                    modified_files.append(str(fpath.relative_to(tmp)))

            elif suffix in CSS_EXTENSIONS:
                original = fpath.read_text(encoding='utf-8', errors='replace')
                processed = strip_css_text_indent(original)
                if processed != original:
                    fpath.write_text(processed, encoding='utf-8')
                    modified_files.append(str(fpath.relative_to(tmp)))

        # 重新打包为 epub
        # epub 要求 mimetype 文件必须是第一个且不压缩
        with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as zout:
            mimetype_path = tmp / 'mimetype'
            if mimetype_path.exists():
                zout.write(mimetype_path, 'mimetype', compress_type=zipfile.ZIP_STORED)

            for fpath in sorted(tmp.rglob('*')):
                if not fpath.is_file():
                    continue
                arcname = str(fpath.relative_to(tmp))
                if arcname == 'mimetype':
                    continue  # 已经写过了
                zout.write(fpath, arcname)

    print(f"✅ 处理完成：{dst}")
    if modified_files:
        print(f"   修改了 {len(modified_files)} 个文件：")
        for f in modified_files:
            print(f"     · {f}")
    else:
        print("   （未发现需要修改的缩进）")


def main():
    if len(sys.argv) < 2:
        print("用法：python remove_epub_indent.py input.epub [output.epub]")
        sys.exit(1)

    input_path = sys.argv[1]
    if len(sys.argv) >= 3:
        output_path = sys.argv[2]
    else:
        stem = Path(input_path).stem
        suffix = Path(input_path).suffix
        output_path = str(Path(input_path).parent / f"{stem}_no_indent{suffix}")

    process_epub(input_path, output_path)


if __name__ == '__main__':
    main()
