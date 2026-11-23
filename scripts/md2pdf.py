#!/usr/bin/env python
"""Markdown -> PDF（中文、横版 A4、表格友好）。

用法：
    python tools/md2pdf.py 需求与产品化规划.md
    python tools/md2pdf.py 需求与产品化规划.md -o 汇报版.pdf
    python tools/md2pdf.py 某文档.md --portrait          # 竖版
    python tools/md2pdf.py 某文档.md --no-open           # 不自动打开

依赖：markdown、pymupdf（都在项目 .venv 里）；渲染用本机 Edge 的无头打印，
不需要 pandoc / wkhtmltopdf / LaTeX。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

CSS_TEMPLATE = """
@page {{ size: A4 {orientation}; margin: 14mm 12mm; }}
body {{ font-family: "Microsoft YaHei", "Segoe UI", sans-serif; font-size: 10pt;
       color: #1a1a1a; line-height: 1.55; margin: 0; }}
h1 {{ font-size: 20pt; margin: 0 0 4pt 0; color: #111; }}
h2 {{ font-size: 13.5pt; margin: 16pt 0 6pt 0; padding-bottom: 3pt;
     border-bottom: 1.2px solid #d0d0d0; page-break-after: avoid; }}
h3 {{ font-size: 11.5pt; margin: 12pt 0 4pt 0; page-break-after: avoid; }}
p {{ margin: 4pt 0; }}
ul, ol {{ margin: 4pt 0 4pt 18pt; }}
li {{ margin: 2pt 0; }}
table {{ border-collapse: collapse; width: 100%; margin: 6pt 0 10pt 0;
        font-size: 8.6pt; page-break-inside: auto; }}
th {{ background: #f2f2f2; border: 0.6px solid #bfbfbf; padding: 4pt 5pt;
     text-align: left; font-weight: 600; white-space: nowrap; }}
td {{ border: 0.6px solid #d5d5d5; padding: 4pt 5pt; vertical-align: top; }}
tr {{ page-break-inside: avoid; }}
code {{ font-family: Consolas, monospace; font-size: 8.8pt; background: #f5f5f5;
       padding: 0 2pt; border-radius: 2px; }}
pre {{ background: #f7f7f7; border: 0.6px solid #ddd; padding: 6pt 8pt;
      white-space: pre-wrap; font-size: 8.6pt; }}
blockquote {{ margin: 6pt 0; padding: 4pt 8pt; border-left: 3px solid #ccc;
             color: #444; background: #fafafa; }}
hr {{ border: none; border-top: 0.8px solid #e0e0e0; margin: 12pt 0; }}
"""


def find_edge() -> str:
    for path in EDGE_CANDIDATES:
        if os.path.exists(path):
            return path
    raise SystemExit('找不到 Edge，无法渲染 PDF')


def read_version(md_text: str) -> str:
    """从正文里抓「版本：…」那一行，作为页眉的一部分。"""
    m = re.search(r'版本[:：]\s*(.+)', md_text)
    return m.group(1).strip() if m else ''


def build_html(md_text: str, title: str, orientation: str) -> str:
    import markdown

    body = markdown.markdown(md_text, extensions=['tables', 'fenced_code', 'sane_lists'])
    version = read_version(md_text)
    today = time.strftime('%Y-%m-%d')

    parts = ['内部文档', title]
    if version:
        parts.append(version)
        # 版本行里若已带日期，就不再重复写今天，避免页眉出现两个日期
        if not re.search(r'\d{4}-\d{2}-\d{2}', version):
            parts.append(today)
    else:
        parts.append(today)
    header = "<div style='font-size:9pt;color:#666;margin-bottom:10pt'>{}</div>".format(
        ' · '.join(parts))

    css = CSS_TEMPLATE.format(orientation=orientation)
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<style>{}</style></head><body>{}{}</body></html>".format(css, header, body))


def print_pdf(edge: str, html_path: str, pdf_path: str) -> None:
    if os.path.exists(pdf_path):
        os.remove(pdf_path)
    cmd = [edge, '--headless=new', '--disable-gpu', '--no-pdf-header-footer',
           '--print-to-pdf=' + pdf_path, 'file:///' + html_path.replace('\\', '/').lstrip('/')]
    # Edge 的 file:// URL 需要盘符保留，单独拼一次更稳
    cmd[-1] = 'file:///' + os.path.abspath(html_path).replace('\\', '/')
    proc = subprocess.run(cmd, capture_output=True, timeout=180)
    if not os.path.exists(pdf_path):
        raise SystemExit('渲染失败：{}\n{}'.format(
            proc.returncode, proc.stderr.decode('utf-8', 'replace')[:500]))


def stamp_page_numbers(pdf_path: str) -> int:
    """在右下角盖「第 X / Y 页」。用内嵌 CJK 字体，不依赖系统字体文件。"""
    import pymupdf

    doc = pymupdf.open(pdf_path)
    total = doc.page_count
    for index, page in enumerate(doc, 1):
        rect = page.rect
        text = '第 {} / {} 页'.format(index, total)
        box = pymupdf.Rect(rect.width - 110, rect.height - 22,
                           rect.width - 12, rect.height - 8)
        page.insert_textbox(box, text, fontname='china-s', fontsize=8,
                            color=(0.45, 0.45, 0.45), align=2)
    doc.saveIncr()
    doc.close()
    return total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Markdown 转 PDF（中文、横版 A4）')
    ap.add_argument('source', help='Markdown 文件路径')
    ap.add_argument('-o', '--output', default='', help='输出 PDF 路径（默认同名 .pdf）')
    ap.add_argument('--portrait', action='store_true', help='竖版 A4（默认横版，适合宽表）')
    ap.add_argument('--keep-html', action='store_true', help='保留中间 HTML 到 _pdfsrc/')
    ap.add_argument('--no-open', action='store_true', help='生成后不自动打开')
    ap.add_argument('--no-page-numbers', action='store_true', help='不盖页码')
    args = ap.parse_args(argv)

    src = os.path.abspath(args.source)
    if not os.path.exists(src):
        raise SystemExit('文件不存在：{}'.format(src))

    md_text = open(src, encoding='utf-8').read()
    title = os.path.splitext(os.path.basename(src))[0]
    orientation = 'portrait' if args.portrait else 'landscape'

    html = build_html(md_text, title, orientation)

    if args.keep_html:
        out_dir = os.path.join(ROOT, '_pdfsrc')
        os.makedirs(out_dir, exist_ok=True)
        html_path = os.path.join(out_dir, title + '.html')
    else:
        fd, html_path = tempfile.mkstemp(suffix='.html', prefix='md2pdf_')
        os.close(fd)
    with open(html_path, 'w', encoding='utf-8') as fp:
        fp.write(html)

    pdf_path = os.path.abspath(args.output or os.path.splitext(src)[0] + '.pdf')
    print('渲染中…（{}，{}）'.format(orientation, os.path.basename(pdf_path)))
    print_pdf(find_edge(), html_path, pdf_path)

    if not args.keep_html:
        try:
            os.remove(html_path)
        except OSError:
            pass

    pages = '?'
    try:
        if args.no_page_numbers:
            import pymupdf
            pages = pymupdf.open(pdf_path).page_count
        else:
            pages = stamp_page_numbers(pdf_path)
    except Exception as exc:                             # noqa: BLE001
        print('  （页码未加盖：{}）'.format(exc))

    size_kb = os.path.getsize(pdf_path) / 1024
    print('完成：{}（{} 页，{:.0f} KB）'.format(pdf_path, pages, size_kb))

    if not args.no_open:
        try:
            os.startfile(pdf_path)                        # noqa: S606 - Windows 专用
        except Exception:                                 # noqa: BLE001
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
