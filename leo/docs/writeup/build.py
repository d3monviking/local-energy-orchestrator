"""Build the LEO submission document (HTML, then PDF via print.mjs).

Every number is read from eval/results/econ_year.json (the year-sampled
simulation priced by economics.yaml), so the document cannot drift from
the results. Run from leo/:

    python docs/writeup/build.py && node docs/writeup/print.mjs
"""
import json
import re
from pathlib import Path

from refs import REFS
from content import sections

HERE = Path(__file__).resolve().parent
LEO = HERE.parents[1]
ECON = json.load(open(LEO / "eval" / "results" / "econ_year.json"))
YAML_TEXT = (LEO / "economics.yaml").read_text()

order: list[str] = []


def cite(*keys: str) -> str:
    nums = []
    for k in keys:
        if k not in REFS:
            raise KeyError(f"unknown reference {k}")
        if k not in order:
            order.append(k)
        nums.append(order.index(k) + 1)
    return "<sup class='cite'>[" + ", ".join(str(n) for n in sorted(nums)) + "]</sup>"


def lakh(x, d=2):
    if x is None:
        return "—"
    s = "−" if x < 0 else ""
    a = abs(x)
    if a >= 1e7:
        return f"{s}₹{a/1e7:.{d}f} Cr"
    if a >= 1e5:
        return f"{s}₹{a/1e5:.{d}f} L"
    return f"{s}₹{a:,.0f}"


def n(x, d=0):
    if x is None:
        return "—"
    return f"{x:,.{d}f}"


def pct(a, b):
    return 100 * (b - a) / a if a else 0.0


fig_no = [0]
tab_no = [0]


def fig(name: str, caption: str, width: str = "100%", land: bool = False) -> str:
    fig_no[0] += 1
    f = (f"<figure><img src='../diagrams/{name}' style='width:{width}'/>"
         f"<figcaption><b>Figure {fig_no[0]}.</b> {caption}</figcaption></figure>")
    return f"<div class='land'>{f}</div>" if land else f


def table(head: list[str], rows: list[list], caption: str | None = None, cls: str = "") -> str:
    h = ""
    if caption:
        tab_no[0] += 1
        h = f"<div class='tcap'><b>Table {tab_no[0]}.</b> {caption}</div>"
    th = "".join(f"<th>{c}</th>" for c in head)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"{h}<table class='{cls}'><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table>"


def assumptions_rows() -> list[list[str]]:
    rows, section = [], ""
    for line in YAML_TEXT.split("\n"):
        m = re.match(r"^([a-z_]+):\s*$", line)
        if m:
            section = m.group(1)
            continue
        kv = re.match(r"^\s{2}([a-z_]+):\s*([^#]+?)\s*(?:#\s*(.*))?$", line)
        if kv:
            note = kv.group(3) or ""
            status = ("<span class='verify'>to verify</span>" if note.startswith("verify")
                      else "<span class='ok'>sourced</span>" if note.startswith(("research", "web")) else "design / model")
            rows.append([section, kv.group(1).replace("_", " "), kv.group(2), note, status])
    return rows


CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm; }
@page land { size: A4 landscape; margin: 12mm 14mm 16mm 14mm; }
.land { page: land; }
.land figure img { max-height: 160mm; }
body { font-family: 'Source Serif 4', Georgia, 'DejaVu Serif', serif; font-size: 10.2pt; line-height: 1.45; color: #1f2328; }
h1, h2, h3, h4, .tcap, figcaption, table, .kpi, .cover, .callout { font-family: Inter, 'Segoe UI', 'DejaVu Sans', Arial, sans-serif; }
h1 { font-size: 17pt; margin: 0 0 6pt; color: #0b3d6b; page-break-before: always; border-bottom: 2px solid #0969da; padding-bottom: 4pt; }
h1.first { page-break-before: avoid; }
h2 { font-size: 12.5pt; margin: 14pt 0 4pt; color: #0b3d6b; }
h3 { font-size: 10.8pt; margin: 10pt 0 3pt; color: #24292f; }
p { margin: 0 0 6pt; text-align: justify; }
ul, ol { margin: 0 0 6pt 16pt; padding: 0; }
li { margin-bottom: 2pt; }
sup.cite { color: #0969da; font-size: 7.5pt; font-family: Inter, Arial, sans-serif; }
table { border-collapse: collapse; width: 100%; font-size: 8.4pt; margin: 2pt 0 10pt; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th { background: #eef4fb; text-align: left; padding: 3pt 4pt; border-bottom: 1.2px solid #8c959f; vertical-align: bottom; }
td { padding: 2.5pt 4pt; border-bottom: 0.5px solid #d0d7de; vertical-align: top; }
table.num td:not(:first-child), table.num th:not(:first-child) { text-align: right; }
.tcap { font-size: 8.6pt; color: #424a53; margin-top: 6pt; }
figure { margin: 8pt 0 10pt; text-align: center; page-break-inside: avoid; }
figure img { max-width: 100%; max-height: 205mm; object-fit: contain; }
figcaption { font-size: 8.4pt; color: #424a53; margin-top: 3pt; text-align: left; }
.callout { background: #f3f9ff; border-left: 3px solid #0969da; padding: 6pt 9pt; margin: 6pt 0 10pt; font-size: 9.4pt; }
.callout.warn { background: #fff8e6; border-left-color: #bf8700; }
.kpis { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6pt; margin: 6pt 0 10pt; }
.kpi { border: 1px solid #d0d7de; border-radius: 4pt; padding: 6pt 8pt; }
.kpi .v { font-size: 14pt; font-weight: 700; color: #0b3d6b; }
.kpi .l { font-size: 8pt; color: #57606a; }
.verify { color: #9a6700; font-weight: 600; }
.ok { color: #1a7f37; font-weight: 600; }
.cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; }
.cover h0 { display:block; font-size: 32pt; font-weight: 800; color: #0b3d6b; letter-spacing: -0.5pt; }
.cover .sub { font-size: 14pt; color: #24292f; margin: 6pt 0 18pt; }
.cover .meta { font-size: 10pt; color: #57606a; line-height: 1.7; }
.toc { font-family: Inter, Arial, sans-serif; font-size: 9.6pt; columns: 2; column-gap: 18pt; }
.toc div { margin-bottom: 2pt; }
.refs { font-size: 8.2pt; }
.refs li { margin-bottom: 3pt; word-break: break-word; }
.small { font-size: 8.6pt; color: #57606a; }
code { font-size: 8.6pt; background: #f6f8fa; padding: 0 2pt; }
"""


def main():
    ctx = dict(cite=cite, lakh=lakh, n=n, pct=pct, fig=fig, table=table, E=ECON, assumptions_rows=assumptions_rows)
    secs = sections(ctx)
    toc = "".join(f"<div>{i}. {t}</div>" for i, (t, _) in enumerate(secs[1:], 1))
    body = secs[0][1].replace("{{TOC}}", f"<div class='toc'>{toc}</div>")
    for i, (title, html) in enumerate(secs[1:], 1):
        body += f"<h1 id='s{i}'>{i}. {title}</h1>{html}"
    refs = "".join(f"<li>{REFS[k]}</li>" for k in order)
    body += f"<h1>References</h1><ol class='refs'>{refs}</ol>"
    html = f"<!doctype html><html><head><meta charset='utf-8'><title>LEO — Local Energy Orchestrator</title><style>{CSS}</style></head><body>{body}</body></html>"
    (HERE / "LEO_submission.html").write_text(html)
    print(f"written: {len(secs)} sections, {fig_no[0]} figures, {tab_no[0]} tables, {len(order)} references")


if __name__ == "__main__":
    main()
