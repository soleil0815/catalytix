"""Build the German user manual PDF from its HTML source.

Usage (from the repository root or anywhere else):

    python3 tools/build_manual.py

Reads docs/manual/handbuch.de.html (+ docs/manual/manual.css, screenshots in docs/manual/img/) and writes
docs/Handbuch-Catalytix.pdf (A4, backgrounds printed) with Playwright's headless Chromium.

Requirements: the Python package `playwright` with its Chromium (`python3 -m playwright install chromium`) and
`pypdf` (or, as a fallback, `pdftotext` from poppler-utils) to read the page numbers back. Everything is loaded
from local files – fonts come from catalytix/static/fonts, monospace text uses a system font – so the build works
offline.

How the table of contents gets its page numbers (two passes):
  1. Every heading with data-mark="key" gets an invisible marker text "QQkeyQQ" (absolutely positioned, so the
     layout does not change). The page is printed once and the PDF text is searched for the markers.
  2. The numbers are written into the <span data-toc="key"> elements of the table of contents and the page is
     printed again without markers. A last check confirms that the numbers still match.

Screenshots: docs/manual/img/ holds screenshots of a running instance with neutral demo data. When the interface
changes, take new ones at the same size (1280×800 viewport at 1.5× for full pages, single cards at 2×) and keep
the file names.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "manual" / "handbuch.de.html"
OUT = ROOT / "docs" / "Handbuch-Catalytix.pdf"

MARK = re.compile(r'(<(h[1-6])\b[^>]*\bdata-mark="([a-z][a-z0-9-]*)"[^>]*>)')
TOC = re.compile(r'(<span\b[^>]*\bdata-toc="([a-z][a-z0-9-]*)"[^>]*>)[^<]*(</span>)')
FOUND = re.compile(r"QQ([a-z][a-z0-9-]*)QQ")


def add_markers(html: str) -> str:
    """Invisible, layout-neutral marker text right at the start of every marked heading."""
    marker = ('<span style="position:absolute;font-size:2px;line-height:1px;color:#fff;left:0">'
              'QQ{key}QQ</span>')
    return MARK.sub(lambda m: m.group(1) + marker.format(key=m.group(3)), html)


def fill_toc(html: str, pages: dict[str, int]) -> str:
    return TOC.sub(lambda m: f"{m.group(1)}{pages.get(m.group(2), '')}{m.group(3)}", html)


def render(html: str, pdf: Path) -> None:
    from playwright.sync_api import sync_playwright
    # Written next to the source so that manual.css, img/ and the fonts resolve via relative paths.
    with tempfile.NamedTemporaryFile("w", suffix=".html", dir=SRC.parent, prefix=".render-",
                                     encoding="utf-8", delete=False) as tmp:
        tmp.write(html)
        tmp_path = Path(tmp.name)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(tmp_path.as_uri())
            page.wait_for_load_state("networkidle")
            page.evaluate("document.fonts.ready")
            page.pdf(path=str(pdf), format="A4", prefer_css_page_size=True, print_background=True)
            browser.close()
    finally:
        tmp_path.unlink(missing_ok=True)


def page_texts(pdf: Path) -> list[str]:
    try:
        from pypdf import PdfReader
        return [(pg.extract_text() or "") for pg in PdfReader(str(pdf)).pages]
    except ImportError:
        if not shutil.which("pdftotext"):
            sys.exit("Neither pypdf nor pdftotext is available to read the page numbers.")
        text = subprocess.run(["pdftotext", "-raw", str(pdf), "-"], check=True,
                              capture_output=True, text=True).stdout
        return text.split("\f")


def find_pages(pdf: Path) -> tuple[dict[str, int], int]:
    texts = page_texts(pdf)
    pages: dict[str, int] = {}
    for number, text in enumerate(texts, start=1):
        for key in FOUND.findall(re.sub(r"\s+", "", text)):
            pages.setdefault(key, number)
    return pages, len([t for t in texts if t.strip()]) or len(texts)


def main() -> None:
    src = SRC.read_text(encoding="utf-8")
    wanted = TOC.findall(src)
    keys = [k for _, k, _ in wanted]
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.pdf"
        # Pass 1: find the pages. TOC numbers are filled with a placeholder of the final width first,
        # so that the table of contents is laid out exactly as in the final document.
        render(add_markers(fill_toc(src, {k: 88 for k in keys})), probe)
        pages, _ = find_pages(probe)
        missing = [k for k in keys if k not in pages]
        if missing:
            sys.exit(f"Headings not found in the PDF: {', '.join(missing)}")
        # Check: with the real numbers filled in, nothing moves.
        render(add_markers(fill_toc(src, pages)), probe)
        again, _ = find_pages(probe)
        if any(again.get(k) != pages[k] for k in keys):
            sys.exit("Page numbers changed between passes – please check the layout of the table of contents.")
    # Final pass without markers.
    render(fill_toc(src, pages), OUT)
    count = len(page_texts(OUT))
    print(f"{OUT.relative_to(ROOT)}: {count} pages")
    for key in keys:
        print(f"  {pages[key]:>3}  {key}")


if __name__ == "__main__":
    main()
