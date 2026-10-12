"""Render docs/DESIGN.md -> docs/DESIGN.pdf (tables, code, Mermaid diagrams) via headless Chrome."""
import re
import sys
from pathlib import Path

import markdown
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
md = (ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")

mermaids: list[str] = []


def _stash(m):
    mermaids.append(m.group(1))
    return f"\n\nMERMAID_{len(mermaids) - 1}\n\n"


md = re.sub(r"```mermaid\n(.*?)```", _stash, md, flags=re.S)
html = markdown.markdown(md, extensions=["tables", "fenced_code", "sane_lists", "toc"])
for i, src in enumerate(mermaids):
    html = html.replace(f"<p>MERMAID_{i}</p>", f'<pre class="mermaid">{src}</pre>')

page = f"""<!doctype html><html><head><meta charset="utf-8">
<style>
 @page {{ size: A4; margin: 16mm 14mm; }}
 body {{ font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; font-size: 10.2pt; line-height: 1.45; color: #1a1a1a; }}
 h1 {{ font-size: 20pt; border-bottom: 2px solid #333; padding-bottom: 4px; }}
 h2 {{ font-size: 15pt; border-bottom: 1px solid #ccc; padding-bottom: 3px; margin-top: 26px; page-break-after: avoid; }}
 h3 {{ font-size: 12pt; margin-top: 18px; page-break-after: avoid; }}
 table {{ border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 8.8pt; page-break-inside: auto; }}
 tr {{ page-break-inside: avoid; }}
 th, td {{ border: 1px solid #c8c8c8; padding: 4px 6px; vertical-align: top; text-align: left; }}
 th {{ background: #f0f2f5; }}
 code {{ font-family: Consolas, "Courier New", monospace; font-size: 8.6pt; background: #f4f4f4; padding: 0 2px; }}
 pre {{ font-family: "Cascadia Mono", "Cascadia Code", "DejaVu Sans Mono", Consolas, monospace;
       background: #f7f7f7; border: 1px solid #e1e1e1; padding: 8px; font-size: 7.6pt; line-height: 1.25;
        overflow: hidden; white-space: pre; page-break-inside: avoid; }}
 pre code {{ background: none; padding: 0; font-size: inherit; font-family: inherit; }}
 pre.mermaid {{ background: white; border: none; text-align: center; }}
 a {{ color: #0b5cad; word-break: break-all; }}
 hr {{ border: none; border-top: 1px solid #ddd; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
</head><body>{html}
<script>mermaid.initialize({{startOnLoad: false, theme: "neutral"}});
mermaid.run().then(() => document.body.setAttribute("data-done", "1"));</script>
</body></html>"""

out_html = ROOT / "docs" / "DESIGN.html"
out_html.write_text(page, encoding="utf-8")
with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    pg = browser.new_page()
    pg.goto(out_html.as_uri())
    pg.wait_for_selector("body[data-done='1']", timeout=60_000)
    n_svg = pg.locator("pre.mermaid svg").count()
    pg.pdf(path=str(ROOT / "docs" / "DESIGN.pdf"), format="A4", print_background=True,
           display_header_footer=True, header_template="<span></span>",
           footer_template='<div style="font-size:8px;width:100%;text-align:center;color:#888">'
                           'AI Project Evaluation Platform — Backend Architecture · '
                           '<span class="pageNumber"></span>/<span class="totalPages"></span></div>',
           margin={"top": "16mm", "bottom": "16mm", "left": "14mm", "right": "14mm"})
    browser.close()
out_html.unlink()
print(f"mermaid diagrams rendered: {n_svg}/{len(mermaids)}")
sys.exit(0 if n_svg == len(mermaids) else 1)
