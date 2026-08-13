#!/usr/bin/env python3
"""Render README.md to a printable PDF.

    ~/miniconda3/bin/python scripts/build_pdf.py [--out linux-ml-setup.pdf]

Needs `markdown` and `weasyprint` (both present in the miniconda env on this
box). Code blocks wrap rather than overflow the page, which is the whole reason
this is a script and not a one-liner.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import markdown
from weasyprint import HTML

REPO = Path(__file__).resolve().parents[1]

CSS = """
@page {
  size: Letter;
  margin: 18mm 16mm 20mm 16mm;
  @bottom-center { content: counter(page); font: 9pt "DejaVu Sans"; color: #6b7280; }
}
body { font: 10.5pt/1.5 "DejaVu Sans", sans-serif; color: #1f2937; }
h1 { font-size: 21pt; border-bottom: 2px solid #1f2937; padding-bottom: 6pt; margin-bottom: 14pt; }
h2 { font-size: 15pt; margin-top: 20pt; border-bottom: 1px solid #d1d5db;
     padding-bottom: 3pt; break-after: avoid; }
h3 { font-size: 12pt; margin-top: 14pt; color: #111827; break-after: avoid; }
p, li { orphans: 2; widows: 2; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 8.8pt;
       background: #f3f4f6; padding: 0.5pt 2pt; border-radius: 2pt; }
pre { background: #f8fafc; border: 1px solid #e5e7eb; border-left: 3px solid #6b7280;
      padding: 7pt 9pt; border-radius: 3pt; break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 8.4pt; line-height: 1.42;
           white-space: pre-wrap; word-break: break-word; }
table { border-collapse: collapse; width: 100%; font-size: 9.2pt; margin: 8pt 0;
        break-inside: avoid; }
th, td { border: 1px solid #d1d5db; padding: 4pt 6pt; text-align: left; vertical-align: top; }
th { background: #f3f4f6; }
blockquote { border-left: 3px solid #d1d5db; margin-left: 0; padding-left: 10pt; color: #4b5563; }
a { color: #1d4ed8; text-decoration: none; }
hr { border: none; border-top: 1px solid #e5e7eb; margin: 16pt 0; }
li { margin: 2pt 0; }
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, default=REPO / "README.md")
    ap.add_argument("--out", type=Path, default=REPO / "linux-ml-setup.pdf")
    args = ap.parse_args()

    body = markdown.markdown(
        args.src.read_text(),
        extensions=["fenced_code", "tables", "toc", "sane_lists"],
    )
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )
    HTML(string=html, base_url=str(REPO)).write_pdf(args.out)
    size_kb = args.out.stat().st_size / 1024
    print(f"wrote {args.out} ({size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
