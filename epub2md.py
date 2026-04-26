#!/usr/bin/env python3
"""
epub2md.py — ePub to Markdown Converter for Obsidian
用法: python3 epub2md.py <input.epub> [选项]

选项:
  -o, --output DIR    输出目录（默认: 与 epub 同目录）
  --single            合并为单一 Markdown 文件（默认: 按章节拆分）
  --images            提取图片到 assets/ 子目录
  --no-frontmatter    不生成 YAML frontmatter
"""

import sys
import os
import re
import argparse
import zipfile
import shutil
import posixpath
from pathlib import Path
from html.parser import HTMLParser

try:
    import ebooklib
    from ebooklib import epub
except ImportError:
    print("请先安装: pip install ebooklib")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError:
    print("请先安装: pip install beautifulsoup4")
    sys.exit(1)


# ─────────────────────────────────────────────
# HTML → Markdown 转换核心
# ─────────────────────────────────────────────

def _sanitize_block_id(raw: str) -> str:
    """Obsidian 块 ID 只允许字母、数字、连字符。其他字符替换为连字符。"""
    bid = re.sub(r'[^A-Za-z0-9\-]', '-', raw)
    bid = re.sub(r'-+', '-', bid).strip('-')
    return bid or "anchor"


def _dedupe_preserve_order(values) -> list:
    """按原始出现顺序去重，避免 set 打乱目录链接目标。"""
    result = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _preprocess_anchors(soup):
    """
    预处理 HTML：把所有 id 属性收集并"上浮"到其所属的最近块级元素上。
    同时移除原始 id 属性（避免重复渲染）。

    特殊处理：若目标是标题（h1-h6），不追加块引用——Markdown 标题本身就是跳转目标。
    但 id 信息仍保留在 data-heading-ids 属性上，用于建立章内链接。
    """
    BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6",
                  "table", "ul", "ol", "pre", "blockquote",
                  "div", "section", "article", "figure", "hr"}
    HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    block_ids = {}  # id(tag) -> list of ids
    heading_ids = {}  # id(tag) -> list of ids for headings

    for el in list(soup.find_all(id=True)):
        el_id = el.get("id", "").strip()
        if not el_id:
            continue

        # 找到最近的块级祖先（包括自己）
        target = el
        while target and target.name not in BLOCK_TAGS:
            if target.parent is None or target.parent.name in (None, "[document]", "html", "body"):
                break
            target = target.parent

        # 若找不到块级元素（直接在 body 下的 span），用元素自身
        if target is None or target.name in (None, "[document]", "html", "body"):
            target = el

        key = id(target)
        if target.name in HEADING_TAGS:
            # 标题上的 id：记录但不作为块引用输出
            heading_ids.setdefault(key, []).append(el_id)
        else:
            block_ids.setdefault(key, []).append(el_id)

        # 删除原始 id（避免重复注入）
        if target is not el:
            del el["id"]

    # 把 id 列表写入目标元素属性，后续渲染时读取
    seen = set()
    for el in soup.find_all(True):
        key = id(el)
        if key in block_ids and key not in seen:
            seen.add(key)
            ids = block_ids[key]
            el["data-block-ids"] = ",".join(ids)
        if key in heading_ids:
            el["data-heading-ids"] = ",".join(heading_ids[key])


def _get_block_refs(node) -> str:
    """返回元素所属的所有 Obsidian 块引用字符串：' ^id1 ^id2'"""
    raw = node.get("data-block-ids", "")
    if not raw:
        return ""
    ids = _dedupe_preserve_order(_sanitize_block_id(x) for x in raw.split(",") if x.strip())
    if not ids:
        return ""
    return " " + " ".join(f"^{x}" for x in ids)


def _get_heading_refs(node) -> list:
    """返回标题上原始 id 对应的块引用 id，保持原始顺序。"""
    raw = node.get("data-heading-ids", "")
    return _dedupe_preserve_order(_sanitize_block_id(x) for x in raw.split(",") if x.strip())


def _compact_text(text: str) -> str:
    """把 HTML 中的换行、制表符、多空格折叠成适合作为链接别名的一行文本。"""
    return re.sub(r'\s+', ' ', text or '').strip()


def _visible_text_with_inline_spacing(node) -> str:
    """提取目录可见文本，只在块级/换行边界补空格，不拆散中文词。"""
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    if node.name and node.name.lower() == "br":
        return " "
    BLOCK_TAGS = {"p", "div", "section", "article", "li", "ul", "ol", "nav",
                  "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
    parts = [_visible_text_with_inline_spacing(child) for child in node.children]
    text = "".join(parts)
    if node.name and node.name.lower() in BLOCK_TAGS:
        return f" {text} "
    return text


def _nav_label_text(node) -> str:
    """目录链接文本：保留原有 inline 串联，只折叠空白。"""
    return _compact_text(_visible_text_with_inline_spacing(node))


def _wiki_alias(text: str) -> str:
    """清理 Obsidian 双链别名，避免中英文目录里的换行/方括号/竖线打断链接。"""
    text = _compact_text(text)
    return text.replace('[', '(').replace(']', ')').replace('|', '｜')


def _markdown_link_label(text: str) -> str:
    """清理普通 Markdown 链接文本。"""
    text = _compact_text(text)
    return text.replace('[', '\\[').replace(']', '\\]')


def _is_placeholder_title(title: str) -> bool:
    """判断是否为无法从正文推断出的占位章节名。"""
    return bool(re.fullmatch(r'Chapter \d+', _compact_text(title or "")))


def _stem_from_link_map(link_map: dict, key: str) -> str:
    """从 link_map 取某个 EPUB 文件对应的 Markdown 文件 stem。"""
    if not key:
        return ""
    return link_map.get(key + ":stem", "")


def _first_entry_id_from_link_map(link_map: dict, key: str) -> str:
    """从 link_map 取章节入口 id，供目录链接精确跳转章节开头。"""
    ids = link_map.get(key + ":heading_ids", []) if key else []
    return ids[0] if ids else ""


def _normalize_epub_path(path: str) -> str:
    """规范化 EPUB 内部 href/file_name，兼容 URL 编码、相对路径和 query。"""
    from urllib.parse import unquote

    if not path:
        return ""
    path = path.split("?", 1)[0]
    path = unquote(path).replace("\\", "/").lstrip("/")
    normalized = posixpath.normpath(path)
    if normalized == ".":
        return ""
    return normalized.lstrip("./")


def _href_candidates(file_part: str, base_href: str = "") -> list:
    """生成 EPUB 内部链接可能对应的 key，包含相对当前文件解析后的路径。"""
    from urllib.parse import unquote

    candidates = []

    def add(value):
        if value and value not in candidates:
            candidates.append(value)

    raw = (file_part or "").split("?", 1)[0]
    decoded = unquote(raw)
    for value in (raw, decoded, _normalize_epub_path(raw), _normalize_epub_path(decoded)):
        add(value)
        add(os.path.basename(value))

    if raw and base_href:
        base_dir = posixpath.dirname(_normalize_epub_path(base_href))
        resolved = _normalize_epub_path(posixpath.join(base_dir, decoded))
        add(resolved)
        add(os.path.basename(resolved))

    return candidates


def html_to_markdown(html_content: str, image_dir: str = None, base_href: str = "",
                     link_map: dict = None, single_file: bool = False) -> str:
    """将 HTML 内容转换为 Markdown，尽量保留所有格式。"""
    soup = BeautifulSoup(html_content, "html.parser")

    # 移除 script / style / meta / link 标签
    for tag in soup(["script", "style", "meta", "link", "head"]):
        tag.decompose()

    # 预处理：把 id 上浮到块级元素
    _preprocess_anchors(soup)

    body = soup.find("body") or soup
    return _node_to_md(body, image_dir=image_dir, base_href=base_href,
                       link_map=link_map or {}, single_file=single_file).strip()


def _node_to_md(node, image_dir=None, base_href="", list_depth=0, ordered=False,
                list_counter=None, link_map=None, single_file=False) -> str:
    if link_map is None:
        link_map = {}
    if isinstance(node, NavigableString):
        text = str(node)
        text = re.sub(r'\n+', ' ', text)
        return text

    if not isinstance(node, Tag):
        return ""

    tag = node.name.lower() if node.name else ""

    # 读取块引用（若此元素被预处理标记为块）
    block_refs = _get_block_refs(node)

    children_md = lambda **kw: "".join(
        _node_to_md(c, image_dir=image_dir, base_href=base_href,
                    list_depth=list_depth, ordered=ordered,
                    list_counter=list_counter, link_map=link_map,
                    single_file=single_file, **kw)
        for c in node.children
    )

    # ── 标题 ──
    # Markdown 标题本身是跳转目标，但我们额外输出 ^id 块引用：
    # 这样跨文件链接可用 [[文件#^id|文字]]，避免 [[文件#标题]] 中 # 被误识别为 tag
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(tag[1])
        inner = children_md().strip()
        if not inner:
            return ""
        # 从预处理时保留的 data-heading-ids 拿 id
        ids = _get_heading_refs(node)
        # 块引用必须独占一行
        ref_lines = "\n\n" + " ".join(f"^{i}" for i in ids) if ids else ""
        return f"\n\n{'#' * level} {inner}{ref_lines}\n\n"

    # ── 段落 ──
    if tag == "p":
        inner = children_md().strip()
        if not inner and not block_refs:
            return ""
        return f"\n\n{inner}{block_refs}\n\n"

    # ── 换行 ──
    if tag == "br":
        return "  \n"

    # ── 水平线 ──
    if tag == "hr":
        if block_refs:
            return f"\n\n---{block_refs}\n\n"
        return "\n\n---\n\n"

    # ── 加粗 / 斜体 / 删除线 / 下划线 / 上下标 / 行内代码（行内元素，不含块引用）──
    if tag in ("strong", "b"):
        inner = children_md().strip()
        return f"**{inner}**" if inner else ""

    if tag in ("em", "i"):
        inner = children_md().strip()
        return f"*{inner}*" if inner else ""

    if tag in ("del", "s", "strike"):
        inner = children_md().strip()
        return f"~~{inner}~~" if inner else ""

    if tag == "u":
        inner = children_md().strip()
        return f"<u>{inner}</u>" if inner else ""

    if tag == "sup":
        inner = children_md().strip()
        if not inner:
            return ""
        # 纯数字 1-9 用 Unicode 上标字符（Obsidian 所有视图都原生显示）
        unicode_sup = {'0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
                       '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹',
                       '+': '⁺', '-': '⁻', '=': '⁼', '(': '⁽', ')': '⁾'}
        if all(c in unicode_sup for c in inner):
            return ''.join(unicode_sup[c] for c in inner)
        # 其他情况用 Pandoc/MultiMarkdown 的 ^...^ 语法
        return f"^{inner}^"

    if tag == "sub":
        inner = children_md().strip()
        if not inner:
            return ""
        unicode_sub = {'0': '₀', '1': '₁', '2': '₂', '3': '₃', '4': '₄',
                       '5': '₅', '6': '₆', '7': '₇', '8': '₈', '9': '₉',
                       '+': '₊', '-': '₋', '=': '₌', '(': '₍', ')': '₎'}
        if all(c in unicode_sub for c in inner):
            return ''.join(unicode_sub[c] for c in inner)
        return f"~{inner}~"

    if tag == "code":
        if node.parent and node.parent.name == "pre":
            return node.get_text()
        inner = node.get_text()
        return f"`{inner}`" if inner else ""

    # ── 代码块 ──
    if tag == "pre":
        code_tag = node.find("code")
        lang = ""
        if code_tag:
            cls = code_tag.get("class", [])
            for c in (cls if isinstance(cls, list) else [cls]):
                if c and c.startswith("language-"):
                    lang = c[9:]
                    break
            text = code_tag.get_text()
        else:
            text = node.get_text()
        # 块引用放在代码块结束后的独立行
        suffix = f"\n{block_refs.strip()}" if block_refs else ""
        return f"\n\n```{lang}\n{text}\n```{suffix}\n\n"

    # ── 引用块 ──
    # epub 里 blockquote 常被滥用做缩进（目录层级等），需要区分：
    # 判断规则：内部直接子节点全是 <a>、<blockquote>、<span>（无实质段落文字）→ 目录缩进
    #           内部含 <p> 且有实质文字 → 真正引用
    if tag == "blockquote":
        meaningful_children = [
            c for c in node.children
            if isinstance(c, Tag) and c.name not in ("br",)
        ]
        # 目录型：子节点只有链接、嵌套blockquote、span
        is_toc_indent = bool(meaningful_children) and all(
            c.name in ("a", "blockquote", "span") for c in meaningful_children
        )
        inner = children_md().strip()
        if not inner:
            return ""
        if is_toc_indent:
            lines = inner.splitlines()
            indented = "\n".join(f"  {l}" for l in lines)
            return f"\n{indented}\n"
        else:
            # 真正的引用块
            lines = inner.splitlines()
            quoted = "\n".join(f"> {l}" for l in lines)
            return f"\n\n{quoted}\n\n"

    # ── 链接 ──
    if tag == "a":
        href = node.get("href", "")
        inner = children_md().strip()
        return _convert_internal_href(href, inner, link_map, base_href, single_file) if inner else ""

    # ── 图片 ──
    if tag == "img":
        src = node.get("src", "")
        alt = node.get("alt", "").strip()
        title = node.get("title", "").strip()

        if not src:
            return ""

        src_clean = src.split("?")[0]
        filename = os.path.basename(src_clean)

        if filename and image_dir:
            caption = alt or title
            if caption:
                return f"![[{filename}|{caption}]]"
            return f"![[{filename}]]"

        caption = alt or title
        if title:
            return f"![{caption}]({src} \"{title}\")"
        return f"![{caption}]({src})"

    # ── 无序列表 ──
    if tag == "ul":
        items = []
        for li in node.find_all("li", recursive=False):
            inner = _node_to_md(li, image_dir=image_dir, base_href=base_href,
                                 list_depth=list_depth + 1, ordered=False,
                                 link_map=link_map, single_file=single_file).strip()
            indent = "  " * list_depth
            items.append(f"{indent}- {inner}")
        if not items:
            return ""
        # 块引用追加到最后一项
        if block_refs:
            items[-1] = items[-1] + block_refs
        return "\n\n" + "\n".join(items) + "\n\n"

    # ── 有序列表 ──
    if tag == "ol":
        items = []
        start = int(node.get("start", 1))
        for i, li in enumerate(node.find_all("li", recursive=False), start=start):
            inner = _node_to_md(li, image_dir=image_dir, base_href=base_href,
                                 list_depth=list_depth + 1, ordered=True,
                                 link_map=link_map, single_file=single_file).strip()
            indent = "  " * list_depth
            items.append(f"{indent}{i}. {inner}")
        if not items:
            return ""
        if block_refs:
            items[-1] = items[-1] + block_refs
        return "\n\n" + "\n".join(items) + "\n\n"

    # ── 列表项 ──
    if tag == "li":
        return children_md().strip()

    # ── 定义列表 ──
    if tag == "dl":
        result = "\n\n"
        for child in node.children:
            if isinstance(child, Tag):
                if child.name == "dt":
                    result += f"**{child.get_text().strip()}**  \n"
                elif child.name == "dd":
                    result += f":   {child.get_text().strip()}\n"
        return result + block_refs + "\n"

    # ── 表格 ──
    if tag == "table":
        table_md = _table_to_md(node)
        # 块引用放在表格后独立行
        suffix = f"\n{block_refs.strip()}\n\n" if block_refs else "\n"
        return table_md + suffix

    # ── div / section / article / main / aside / figure ──
    if tag in ("div", "section", "article", "main", "aside", "figure", "figcaption",
               "header", "footer", "nav", "span", "body"):
        inner = children_md()
        if tag in ("div", "section", "article", "main", "aside", "header", "footer", "figure"):
            inner = inner.strip()
            if inner:
                return f"\n\n{inner}{block_refs}\n\n"
            elif block_refs:
                # 空 div 但有锚点 → 生成占位段落以保留锚点
                return f"\n\n<span></span>{block_refs}\n\n"
            return ""
        return inner

    # ── 其他：直接递归子节点 ──
    return children_md()


def _table_to_md(table_tag) -> str:
    """将 <table> 转为 GFM Markdown 表格。"""
    rows = []
    for tr in table_tag.find_all("tr"):
        cells = []
        for cell in tr.find_all(["th", "td"]):
            text = cell.get_text(" ", strip=True).replace("|", "\\|")
            cells.append(text)
        if cells:
            rows.append(cells)

    if not rows:
        return ""

    # 对齐列数
    max_cols = max(len(r) for r in rows)
    for r in rows:
        while len(r) < max_cols:
            r.append("")

    lines = []
    header = rows[0]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * max_cols) + " |")
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")

    return "\n\n" + "\n".join(lines) + "\n\n"


def _nav_points_to_lines(node, link_map: dict, base_href: str = "", single_file: bool = False,
                         depth: int = 0) -> list:
    """把 EPUB2 ncx/navPoint 目录转成 Markdown 列表。"""
    lines = []
    for nav_point in node.find_all("navpoint", recursive=False):
        label = nav_point.find("text")
        content = nav_point.find("content")
        title = _nav_label_text(label) if label else ""
        href = content.get("src", "") if content else ""
        if title:
            link = _convert_internal_href(href, title, link_map, base_href, single_file) if href else title
            lines.append(f"{'  ' * depth}- {link}")
        lines.extend(_nav_points_to_lines(nav_point, link_map, base_href, single_file, depth + 1))
    return lines


def nav_to_markdown(html_content: str, link_map: dict = None, base_href: str = "",
                    single_file: bool = False) -> str:
    """专门转换 EPUB nav / ncx 目录，避免目录被当成普通正文段落造成中英文错位。"""
    soup = BeautifulSoup(html_content, "html.parser")
    link_map = link_map or {}

    nav_map = soup.find("navmap")
    if nav_map:
        lines = _nav_points_to_lines(nav_map, link_map, base_href, single_file)
        return "\n".join(lines).strip()

    nav = soup.find("nav", attrs={"epub:type": "toc"}) or soup.find("nav", attrs={"type": "toc"}) or soup.find("nav")
    if nav:
        nav_copy = BeautifulSoup(str(nav), "html.parser")
        for a in nav_copy.find_all("a"):
            label = _nav_label_text(a)
            converted = _convert_internal_href(a.get("href", ""), label, link_map, base_href, single_file)
            a.clear()
            a.append(converted)
        return _node_to_md(nav_copy, base_href=base_href, link_map={},
                           single_file=single_file).strip()

    return html_to_markdown(html_content, base_href=base_href, link_map=link_map,
                            single_file=single_file)


def _convert_internal_href(href: str, label: str, link_map: dict, base_href: str = "",
                           single_file: bool = False) -> str:
    """把 EPUB 内部 href 转换为 Obsidian 双链，供 HTML <a> 和 ncx 目录共用。"""
    if not href:
        return _compact_text(label)

    if href.startswith(("http://", "https://", "mailto:")):
        return f"[{_markdown_link_label(label)}]({href})"

    if "#" in href:
        file_part, anchor_part = href.rsplit("#", 1)
    else:
        file_part, anchor_part = href, ""

    anchor_part = anchor_part.strip()
    anchor_bid = _sanitize_block_id(anchor_part) if anchor_part else ""
    target_title = ""
    target_stem = ""
    target_entry_ids = []
    target_sub_headings = {}
    if file_part:
        for cand in _href_candidates(file_part, base_href):
            if cand in link_map:
                target_title = link_map[cand]
                target_stem = link_map.get(cand + ":stem", "")
                target_entry_ids = link_map.get(cand + ":heading_ids", set())
                target_sub_headings = link_map.get(cand + ":sub_headings", {})
                break

    target_entry_bids = _dedupe_preserve_order(_sanitize_block_id(x) for x in target_entry_ids)
    anchor_is_chapter_entry = bool(anchor_part) and (
        anchor_part in target_entry_ids or anchor_bid in target_entry_bids
    )
    wiki_label = _wiki_alias(label)

    if single_file:
        if target_title and (not anchor_part or anchor_is_chapter_entry):
            if anchor_part:
                return f"[[#^{anchor_bid}|{wiki_label}]]"
            if target_entry_bids:
                return f"[[#^{target_entry_bids[0]}|{wiki_label}]]"
            return f"[[#{_compact_text(target_title)}|{wiki_label}]]"
        if anchor_part:
            return f"[[#^{anchor_bid}|{wiki_label}]]"
        return _compact_text(label)

    if target_stem:
        if not anchor_part or anchor_is_chapter_entry:
            # 目录里的章目链接应该精确跳到 EPUB 原锚点，而不是只打开文件。
            if anchor_part:
                return f"[[{target_stem}#^{anchor_bid}|{wiki_label}]]"
            if target_entry_bids:
                return f"[[{target_stem}#^{target_entry_bids[0]}|{wiki_label}]]"
            if _compact_text(label) == _compact_text(target_title):
                return f"[[{target_stem}]]"
            return f"[[{target_stem}|{wiki_label}]]"
        return f"[[{target_stem}#^{anchor_bid}|{wiki_label}]]"

    if anchor_part and not file_part:
        return f"[[#^{anchor_bid}|{wiki_label}]]"

    return _compact_text(label)


# ─────────────────────────────────────────────
# ePub 读取与处理
# ─────────────────────────────────────────────

def get_metadata(book) -> dict:
    """提取书籍元数据。"""
    def _get(key):
        try:
            val = book.get_metadata("DC", key)
            return val[0][0] if val else ""
        except Exception:
            return ""

    return {
        "title": _get("title"),
        "author": _get("creator"),
        "publisher": _get("publisher"),
        "language": _get("language"),
        "date": _get("date"),
        "description": _get("description"),
        "identifier": _get("identifier"),
    }


def make_frontmatter(meta: dict) -> str:
    """生成 YAML frontmatter。"""
    lines = ["---"]
    if meta.get("title"):
        lines.append(f'title: "{meta["title"]}"')
    if meta.get("author"):
        lines.append(f'author: "{meta["author"]}"')
    if meta.get("publisher"):
        lines.append(f'publisher: "{meta["publisher"]}"')
    if meta.get("language"):
        lines.append(f'language: "{meta["language"]}"')
    if meta.get("date"):
        lines.append(f'date: "{meta["date"]}"')
    lines.append("tags: []")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def clean_filename(name: str) -> str:
    """清理文件名，适合 Obsidian。"""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r'\s+', " ", name).strip()
    return name or "untitled"


def extract_images(book, output_dir: Path):
    """提取所有图片到 assets/ 目录。"""
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    count = 0
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        filename = os.path.basename(item.file_name)
        out_path = assets_dir / filename
        with open(out_path, "wb") as f:
            f.write(item.get_content())
        count += 1
    return count


def get_spine_items(book):
    """按阅读顺序返回正文章节。"""
    spine_ids = [item_id for item_id, _ in book.spine]
    items = []
    for item_id in spine_ids:
        item = book.get_item_with_id(item_id)
        if item and item.get_type() == ebooklib.ITEM_DOCUMENT:
            items.append(item)
    # 若 spine 为空，退回到所有文档
    if not items:
        items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
    return items


def get_nav_items(book):
    """返回 EPUB 自带目录文件（EPUB3 nav 或 EPUB2 ncx）。"""
    nav_items = []
    for item in book.get_items():
        file_name = getattr(item, "file_name", "") or ""
        media_type = getattr(item, "media_type", "") or ""
        if item.get_type() == ebooklib.ITEM_NAVIGATION or file_name.lower().endswith(".ncx"):
            nav_items.append(item)
            continue
        if item.get_type() == ebooklib.ITEM_DOCUMENT and media_type in ("application/xhtml+xml", "text/html"):
            try:
                html = item.get_content().decode("utf-8", errors="replace")
            except Exception:
                continue
            soup = BeautifulSoup(html, "html.parser")
            if soup.find("nav", attrs={"epub:type": "toc"}) or soup.find("nav", attrs={"type": "toc"}):
                nav_items.append(item)
    return nav_items


def build_nav_markdown(book, link_map: dict, single_file: bool = False) -> str:
    """优先使用 EPUB 自带目录生成 Obsidian 可点击目录。"""
    parts = []
    seen = set()
    for item in get_nav_items(book):
        item_path = _normalize_epub_path(item.file_name)
        if item_path in seen:
            continue
        seen.add(item_path)
        html = item.get_content().decode("utf-8", errors="replace")
        md = nav_to_markdown(html, link_map=link_map, base_href=item_path, single_file=single_file)
        md = _clean_md(md)
        if md:
            parts.append(md)
    return "\n".join(parts).strip()


def _collect_nav_entries_from_node(node, base_href: str = "") -> list:
    """收集目录中的 href/title，用于用 EPUB 原目录标题修正占位章节名。"""
    entries = []

    nav_map = node.find("navmap")
    if nav_map:
        for nav_point in nav_map.find_all("navpoint"):
            label = nav_point.find("text")
            content = nav_point.find("content")
            title = _nav_label_text(label) if label else ""
            href = content.get("src", "") if content else ""
            if href and title:
                entries.append((href, title, base_href))

    nav = node.find("nav", attrs={"epub:type": "toc"}) or node.find("nav", attrs={"type": "toc"}) or node.find("nav")
    if nav:
        for a in nav.find_all("a", href=True):
            title = _nav_label_text(a)
            if title:
                entries.append((a.get("href", ""), title, base_href))

    return entries


def update_link_map_titles_from_nav(book, link_map: dict):
    """当正文缺少标题导致文件名是 Chapter N 时，用 EPUB 目录标题修正 link_map。"""
    updates = {}
    for item in get_nav_items(book):
        item_path = _normalize_epub_path(item.file_name)
        html = item.get_content().decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        for href, title, base_href in _collect_nav_entries_from_node(soup, item_path):
            file_part = href.rsplit("#", 1)[0] if "#" in href else href
            if not file_part:
                continue
            for cand in _href_candidates(file_part, base_href):
                old_title = link_map.get(cand, "")
                old_stem = _stem_from_link_map(link_map, cand)
                if old_stem and (_is_placeholder_title(old_title) or _is_placeholder_title(old_stem.split(" - ", 1)[-1])):
                    updates[old_stem] = title
                    break

    if not updates:
        return

    for key in list(link_map.keys()):
        if key.endswith((":stem", ":heading_ids", ":sub_headings")):
            continue
        old_stem = _stem_from_link_map(link_map, key)
        if old_stem in updates:
            new_title = updates[old_stem]
            prefix = old_stem.split(" - ", 1)[0] if " - " in old_stem else ""
            new_stem = clean_filename(f"{prefix} - {new_title}" if prefix else new_title)
            link_map[key] = new_title
            link_map[key + ":stem"] = new_stem


def infer_chapter_title(soup, index: int) -> str:
    """从 HTML 内容中推断章节标题。"""
    for h in ["h1", "h2", "h3", "title"]:
        tag = soup.find(h)
        if tag and tag.get_text(strip=True):
            return _compact_text(tag.get_text(" ", strip=True))
    return f"Chapter {index + 1}"


# ─────────────────────────────────────────────
# 主转换逻辑
# ─────────────────────────────────────────────

def convert_epub(
    epub_path: str,
    output_dir: str = None,
    single_file: bool = False,
    extract_imgs: bool = True,
    no_frontmatter: bool = False,
):
    epub_path = Path(epub_path).resolve()
    if not epub_path.exists():
        print(f"❌ 找不到文件: {epub_path}")
        sys.exit(1)

    # 确定输出目录
    if output_dir:
        out_dir = Path(output_dir).resolve()
    else:
        out_dir = epub_path.parent / epub_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"📖 读取: {epub_path.name}")
    book = epub.read_epub(str(epub_path))
    meta = get_metadata(book)

    book_title = meta.get("title") or epub_path.stem
    print(f"   标题: {book_title}")
    print(f"   作者: {meta.get('author', '未知')}")

    # 提取图片
    img_count = 0
    if extract_imgs:
        img_count = extract_images(book, out_dir)
        if img_count:
            print(f"   图片: {img_count} 张 → assets/")

    # 获取章节
    items = get_spine_items(book)
    print(f"   章节: {len(items)} 个")

    # 构建内部链接映射表
    link_map = build_link_map(items)
    update_link_map_titles_from_nav(book, link_map)
    nav_md = build_nav_markdown(book, link_map, single_file=single_file)

    if single_file:
        _convert_single(book, items, meta, out_dir, book_title, no_frontmatter, extract_imgs, link_map, nav_md)
    else:
        _convert_split(book, items, meta, out_dir, book_title, no_frontmatter, extract_imgs, link_map, nav_md)

    print(f"\n✅ 完成！输出目录: {out_dir}")
    return out_dir


def _convert_single(book, items, meta, out_dir, book_title, no_frontmatter, extract_imgs, link_map, nav_md=""):
    """合并输出为单一 Markdown 文件。"""
    parts = []

    if not no_frontmatter:
        parts.append(make_frontmatter(meta))

    parts.append(f"# {book_title}\n\n")
    if nav_md:
        parts.append(f"## 目录\n\n{nav_md}\n\n---\n\n")

    first = True
    for i, item in enumerate(items):
        html = item.get_content().decode("utf-8", errors="replace")
        current_file = _normalize_epub_path(item.file_name)
        md = html_to_markdown(html, image_dir=str(out_dir) if extract_imgs else None,
                              link_map=link_map, single_file=True,
                              base_href=current_file)
        md = _clean_md(md)
        if not md or len(md.strip()) < 10:
            continue  # 跳过空章节（导航页等）
        if not first:
            parts.append("\n\n---\n\n")
        parts.append(md)
        first = False

    content = _clean_md("".join(parts))
    out_file = out_dir / f"{clean_filename(book_title)}.md"
    out_file.write_text(content, encoding="utf-8")
    print(f"   → {out_file.name}")


def _convert_split(book, items, meta, out_dir, book_title, no_frontmatter, extract_imgs, link_map, nav_md=""):
    """按章节拆分输出。"""
    # 生成书籍索引文件
    index_lines = []
    if not no_frontmatter:
        index_lines.append(make_frontmatter(meta))
    index_lines.append(f"# {book_title}\n\n")
    index_lines.append("## 目录\n\n")

    chapter_files = []
    used_names = {}

    for i, item in enumerate(items):
        html = item.get_content().decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        chapter_title = infer_chapter_title(soup, i)
        mapped_title = link_map.get(_normalize_epub_path(item.file_name), "")
        if mapped_title and _is_placeholder_title(chapter_title):
            chapter_title = mapped_title

        md = html_to_markdown(html, image_dir=str(out_dir) if extract_imgs else None,
                              link_map=link_map, single_file=False,
                              base_href=_normalize_epub_path(item.file_name))
        md = _clean_md(md)

        if not md or len(md.strip()) < 10:
            continue  # 跳过空章节

        # 生成唯一文件名
        base_name = clean_filename(f"{i+1:03d} - {chapter_title}")
        if base_name in used_names:
            used_names[base_name] += 1
            base_name = f"{base_name} ({used_names[base_name]})"
        else:
            used_names[base_name] = 1

        chapter_file = out_dir / f"{base_name}.md"

        # 章节 frontmatter
        chapter_content = []
        if not no_frontmatter:
            chapter_fm = (
                f"---\n"
                f'title: "{chapter_title}"\n'
                f'book: "[[{clean_filename(book_title)}]]"\n'
                f"tags: []\n"
                f"---\n\n"
            )
            chapter_content.append(chapter_fm)

        chapter_content.append(md)
        chapter_file.write_text("".join(chapter_content), encoding="utf-8")
        chapter_files.append((chapter_title, chapter_file.name))

    # 写入索引：优先使用 EPUB 自带目录；没有目录时退回按章节生成。
    if nav_md:
        index_lines.append(nav_md + "\n")
    else:
        for title, filename in chapter_files:
            stem = filename[:-3]  # 去掉 .md
            index_lines.append(f"- [[{stem}|{_wiki_alias(title)}]]\n")

    index_file = out_dir / f"{clean_filename(book_title)}.md"
    index_file.write_text("".join(index_lines), encoding="utf-8")

    print(f"   索引: {index_file.name}")
    print(f"   章节文件: {len(chapter_files)} 个")


def build_link_map(items) -> dict:
    """
    构建 epub 内部文件名 → 章节标题 / 文件 stem 的映射表。
    为兼容各种 href 写法，同一个 item 会注册多个 key：
    - 完整路径：  OEBPS/Text/ch01.xhtml
    - basename： ch01.xhtml
    - URL 编码版本（因为 epub 里有 href="CR%21WDDHDN..." 这种）

    同时记录每章的"标题入口 id 集合"——href 锚点若命中其中之一，说明是
    章节级链接（目录链接），可直接跳到章节文件，不需要 block ref。
    """
    from urllib.parse import unquote

    link_map = {}
    used_names = {}
    for i, item in enumerate(items):
        full_path = _normalize_epub_path(item.file_name)
        base_name_file = os.path.basename(full_path)
        html = item.get_content().decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        title = infer_chapter_title(soup, i)

        stem = clean_filename(f"{i+1:03d} - {title}")
        if stem in used_names:
            used_names[stem] += 1
            stem = f"{stem} ({used_names[stem]})"
        else:
            used_names[stem] = 1

        # 收集两种 id:
        # 1. entry_ids: 章节入口（第一个标题及其子孙的 id）→ 跳这些 id = 跳章节
        # 2. heading_id_to_text: 其他标题上的 id → 文字映射，跳这些 id = 跳小节标题
        entry_ids = []
        heading_id_to_text = {}  # id → heading text

        all_headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        for idx, h in enumerate(all_headings):
            hid = h.get("id", "").strip()
            htext = h.get_text(strip=True)
            if idx == 0:
                # 第一个标题是章节入口
                if hid:
                    entry_ids.append(hid)
                for desc in h.find_all(id=True):
                    did = desc.get("id", "").strip()
                    if did:
                        entry_ids.append(did)
            else:
                # 后续标题是小节
                if hid and htext:
                    heading_id_to_text[hid] = htext
                for desc in h.find_all(id=True):
                    did = desc.get("id", "").strip()
                    if did and htext:
                        heading_id_to_text[did] = htext

        # body 开头附近的 id 也算入口（有些 epub 把 id 挂在章节最外层 div 上）
        body = soup.find("body") or soup
        for child in list(body.descendants)[:8]:
            if isinstance(child, Tag):
                cid = child.get("id", "").strip() if hasattr(child, "get") else ""
                if cid:
                    entry_ids.append(cid)

        entry_ids = _dedupe_preserve_order(entry_ids)

        # 注册多种 key 形式
        keys = {
            full_path,
            base_name_file,
            unquote(full_path),
            unquote(base_name_file),
            _normalize_epub_path(full_path),
            _normalize_epub_path(base_name_file),
        }
        for k in keys:
            if k:
                link_map[k] = title
                link_map[k + ":stem"] = stem
                link_map[k + ":heading_ids"] = entry_ids
                link_map[k + ":sub_headings"] = heading_id_to_text

    return link_map


def _clean_md(md: str) -> str:
    """清理多余的空行：段落间保留一个空行（即两个换行）。"""
    # 去掉行尾空格（保留两个空格换行）
    md = re.sub(r'(?<! ) +$', '', md, flags=re.MULTILINE)
    # 将连续 3 个及以上换行收缩为 2 个（即一个空行）
    md = re.sub(r'\n{3,}', '\n\n', md)
    return md.strip()


# ─────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ePub → Markdown 转换器（Obsidian 友好）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("epub", help="输入的 .epub 文件路径")
    parser.add_argument("-o", "--output", help="输出目录（默认：epub 同目录下同名文件夹）")
    parser.add_argument("--single", action="store_true", help="合并为单一 Markdown 文件")
    parser.add_argument("--images", action="store_true", default=True, help="提取图片（默认开启）")
    parser.add_argument("--no-images", action="store_false", dest="images", help="不提取图片")
    parser.add_argument("--no-frontmatter", action="store_true", help="不生成 YAML frontmatter")

    args = parser.parse_args()
    convert_epub(
        epub_path=args.epub,
        output_dir=args.output,
        single_file=args.single,
        extract_imgs=args.images,
        no_frontmatter=args.no_frontmatter,
    )


if __name__ == "__main__":
    main()