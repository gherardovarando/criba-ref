"""Render a compare report (compare.build) as terminal text, CSV, HTML, Markdown + SVG, or LaTeX."""
import csv
import html
from decimal import ROUND_HALF_UP, Decimal
import io
import re
from pathlib import Path

# One-hue sequential ramp, light -> dark, for confusion matrices
RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
FORMATS = ("html", "md", "latex", "csv")


# ---------- helpers ----------

def r2(x, places="0.01"):
    """Round half up (0.975 -> 0.98), the same for every number, unlike float formatting."""
    return Decimal(repr(x)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def f2(x):
    return "n/a" if x is None else f"{r2(x)}"


def pct(x):
    return "n/a" if x is None else f"{r2(100 * x, '1')}%"


def ci(c, f=None):
    f = f or f2
    return "" if not c else f"[{f(c[0])}, {f(c[1])}]"


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()


def shade(v, vmax):
    """-> (fill, ink) for a cell with count v."""
    if v == 0 or vmax == 0:
        return None, "#0b0b0b"
    i = round((v - 1) / max(vmax - 1, 1) * (len(RAMP) - 1)) if vmax > 1 else len(RAMP) - 1
    return RAMP[i], ("#ffffff" if i >= 3 else "#0b0b0b")


def pair_name(p):
    return f"{p['a']} vs {p['b']}"


def label_qs(rep):
    return [q for q in rep["questions"] if q["type"] == "label"]


def tag_qs(rep):
    return [q for q in rep["questions"] if q["type"] == "tags"]


def tag_table(rep, q, fmt=f2):
    """-> (header, rows): one row per tag used by some source: how many items each source tagged, then
    the kappa of each pair; a last row with the mean Jaccard overlap."""
    names = [s["name"] for s in rep["sources"]]
    head = ["tag"] + [f"{n} (of {q['n_by_source'][n]})" for n in names] + [f"κ {pair_name(p)}" for p in q["pairs"]]
    idx = {t: k for k, t in enumerate(q["labels"])}
    rows = [[t] + [q["counts"][n][t] for n in names] + [fmt(p["per_tag"][idx[t]]["kappa"]) for p in q["pairs"]]
            for t in q["used"]]
    rows.append(["mean Jaccard"] + [""] * len(names) + [fmt(p["jaccard"]) for p in q["pairs"]])
    return head, rows


def table_text(rows, header):
    w = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
    line = lambda r: "  ".join(str(c).ljust(w[i]) if i == 0 else str(c).rjust(w[i]) for i, c in enumerate(r))
    return [line(header)] + [line(r) for r in rows]


def screening_row(s, f=f2):
    return [s["source"], s["n"], f"{f(s['recall']['value'])} {ci(s['recall']['ci'], f)}",
            f"{f(s['precision']['value'])} {ci(s['precision']['ci'], f)}", f(s["specificity"]["value"]),
            f(s["f1"]), pct(s["share_pass"]), f(s["wss"])]


SCREEN_HEAD = ["source", "n", "recall [95% CI]", "precision [95% CI]", "specificity", "F1", "to full text", "WSS"]
PAIR_HEAD = ["pair", "n", "agreement", "95% CI", "kappa", "95% CI"]


def pair_row(p):
    return [pair_name(p), p["n"], f"{p['agree']} ({pct(p['agreement'])})", ci(p["agreement_ci"], pct),
            f2(p["kappa"]), ci(p["kappa_ci"])]


# ---------- terminal ----------

def text(rep):
    out = []
    srcs = ", ".join(f"{s['name']} ({s['items']} items)" for s in rep["sources"])
    out.append(f"sources: {srcs}" + (f"; reference: {rep['reference']}" if rep["reference"] else ""))
    for s in rep["sources"]:
        for w in s["warnings"][:5]:
            out.append(f"warning: {s['name']}: {w}")
        if len(s["warnings"]) > 5:
            out.append(f"warning: {s['name']}: ... and {len(s['warnings']) - 5} more")
        if s["statuses"].get("error"):
            out.append(f"note: {s['name']}: {s['statuses']['error']} results with an error left out")
    for q in label_qs(rep):
        out += ["", f"{q['id']} ({' | '.join(q['labels'])})"]
        out += ["  " + l for l in table_text([pair_row(p) for p in q["pairs"]], PAIR_HEAD)]
        if q["fleiss"]:
            out.append(f"  Fleiss' kappa, {len(q['fleiss']['sources'])} sources, {q['fleiss']['n']} items: "
                       f"{f2(q['fleiss']['kappa'])}")
        for p in q["pairs"]:
            out.append(f"  {p['a']} (rows) vs {p['b']} (columns):")
            rows = [[lab] + p["matrix"][i] for i, lab in enumerate(q["labels"])]
            out += ["    " + l for l in table_text(rows, [""] + q["labels"])]
        if q["screening"]:
            out.append(f"  screening vs {rep['reference']} (pass = {', '.join(q['pass'])}):")
            out += ["    " + l for l in table_text([screening_row(s) for s in q["screening"]], SCREEN_HEAD)]
            for s in q["screening"]:
                if s["missed"]:
                    out.append(f"    {s['source']} missed {len(s['missed'])}: " +
                               "; ".join(title_of(rep, i) for i in s["missed"][:10]))
    for q in tag_qs(rep):
        out += ["", f"{q['id']} (tags)"]
        head, rows = tag_table(rep, q)
        out.append("  items tagged by each source, and kappa per tag (present / absent):")
        out += ["    " + l for l in table_text(rows, head)]
        for s in q["screening"]:
            rows = [[t["tag"], t["n_ref"], f2(t["recall"]["value"]), f2(t["precision"]["value"])]
                    for t in s["per_tag"] if t["n_ref"] or t["precision"]["n"]]
            out.append(f"  {s['source']} vs {rep['reference']}, per tag:")
            out += ["    " + l for l in table_text(rows, ["tag", f"n ({rep['reference']})", "recall", "precision"])]
    out += ["", f"{len(rep['disagreements'])} disagreements (item and question); "
                f"with -o DIR they go to csv/disagreements.csv"]
    return "\n".join(out)


def title_of(rep, iid, n=60):
    t = (rep["titles"].get(iid) or ["", ""])[0]
    return (t[:n] + "…" if len(t) > n else t) or iid


# ---------- CSV ----------

def csvs(rep):
    """-> {file name: text}."""
    m = [["question", "source_a", "source_b", "metric", "value", "ci_low", "ci_high", "n"]]
    conf = [["question", "source_a", "source_b", "label_a", "label_b", "count"]]
    for q in label_qs(rep):
        for p in q["pairs"]:
            for name, v, c in (("agreement", p["agreement"], p["agreement_ci"]), ("kappa", p["kappa"], p["kappa_ci"])):
                m.append([q["id"], p["a"], p["b"], name, v, *(c or ("", "")), p["n"]])
            for i, la in enumerate(q["labels"]):
                for j, lb in enumerate(q["labels"]):
                    conf.append([q["id"], p["a"], p["b"], la, lb, p["matrix"][i][j]])
        if q["fleiss"]:
            m.append([q["id"], "+".join(q["fleiss"]["sources"]), "", "fleiss_kappa", q["fleiss"]["kappa"], "", "",
                      q["fleiss"]["n"]])
        for s in q["screening"]:
            for name in ("recall", "precision", "specificity"):
                m.append([q["id"], rep["reference"], s["source"], name, s[name]["value"], *(s[name]["ci"] or ("", "")),
                          s[name]["n"]])
            for name in ("f1", "share_pass", "wss", "tp", "fn", "fp", "tn"):
                m.append([q["id"], rep["reference"], s["source"], name, s[name], "", "", s["n"]])
    for q in tag_qs(rep):
        for p in q["pairs"]:
            m.append([q["id"], p["a"], p["b"], "mean_jaccard", p["jaccard"], "", "", p["n"]])
            for t in p["per_tag"]:
                m.append([f"{q['id']}:{t['tag']}", p["a"], p["b"], "kappa", t["kappa"], "", "", p["n"]])
        for s in q["screening"]:
            for t in s["per_tag"]:
                for name in ("recall", "precision"):
                    m.append([f"{q['id']}:{t['tag']}", rep["reference"], s["source"], name, t[name]["value"],
                              *(t[name]["ci"] or ("", "")), t[name]["n"]])
    names = [s["name"] for s in rep["sources"]]
    d = [["id", "title", "url", "question"] + [x for n in names for x in (n, f"{n}_reason")]]
    for r in rep["disagreements"]:
        d.append([r["id"], r["title"], r["url"], r["question"]] + [x for n in names for x in r["answers"][n]])

    def dump(rows):
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(
            [["" if v is None else (round(v, 4) if isinstance(v, float) else v) for v in r] for r in rows])
        return buf.getvalue()
    return {"metrics.csv": dump(m), "confusion.csv": dump(conf), "disagreements.csv": dump(d)}


# ---------- SVG ----------

def svg_confusion(labels, matrix, a, b):
    """A confusion matrix as a standalone SVG (own background, readable in light and dark pages)."""
    k, cell, left, top = len(labels), 64, 110, 78
    w, h = left + k * cell + 16, top + k * cell + 16
    vmax = max(max(r) for r in matrix) if matrix else 0
    e = html.escape
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
         f'font-family="system-ui, -apple-system, Segoe UI, sans-serif" role="img" '
         f'aria-label="Confusion matrix, {e(a)} in rows, {e(b)} in columns">',
         f'<rect width="{w}" height="{h}" fill="#fcfcfb" rx="8"/>',
         f'<text x="{left + k * cell / 2}" y="20" text-anchor="middle" font-size="13" fill="#52514e">{e(b)}</text>',
         f'<text x="18" y="{top + k * cell / 2}" text-anchor="middle" font-size="13" fill="#52514e" '
         f'transform="rotate(-90 18 {top + k * cell / 2})">{e(a)}</text>']
    for j, lab in enumerate(labels):
        p.append(f'<text x="{left + j * cell + cell / 2}" y="{top - 12}" text-anchor="middle" font-size="12" '
                 f'fill="#52514e">{e(lab)}</text>')
    for i, lab in enumerate(labels):
        y = top + i * cell
        p.append(f'<text x="{left - 8}" y="{y + cell / 2 + 4}" text-anchor="end" font-size="12" '
                 f'fill="#52514e">{e(lab)}</text>')
        for j in range(k):
            v = matrix[i][j]
            fill, ink = shade(v, vmax)
            x = left + j * cell
            if fill:
                p.append(f'<rect x="{x + 1}" y="{y + 1}" width="{cell - 2}" height="{cell - 2}" rx="4" fill="{fill}">'
                         f'<title>{e(a)}: {e(lab)}, {e(b)}: {e(labels[j])}: {v}</title></rect>')
            else:
                p.append(f'<rect x="{x + 1.5}" y="{y + 1.5}" width="{cell - 3}" height="{cell - 3}" rx="4" '
                         f'fill="none" stroke="#e4e3df"/>')
            weight = "700" if i == j else "400"
            p.append(f'<text x="{x + cell / 2}" y="{y + cell / 2 + 6}" text-anchor="middle" font-size="17" '
                     f'font-weight="{weight}" fill="{ink}">{v}</text>')
    p.append("</svg>")
    return "\n".join(p)


def figures(rep):
    """-> {file name: svg} for every pair of every label question."""
    out = {}
    for q in label_qs(rep):
        for p in q["pairs"]:
            out[f"confusion-{slug(q['id'])}-{slug(p['a'])}-vs-{slug(p['b'])}.svg"] = \
                svg_confusion(q["labels"], p["matrix"], p["a"], p["b"])
    return out


# ---------- Markdown ----------

def md_table(header, rows):
    esc = lambda c: str(c).replace("|", "\\|")
    return "\n".join(["| " + " | ".join(map(esc, header)) + " |", "|" + "---|" * len(header)] +
                     ["| " + " | ".join(map(esc, r)) + " |" for r in rows])


def markdown(rep, figdir="figures"):
    o = [f"# Comparison of screenings", "", meta_line(rep), "", "## Sources", "",
         md_table(["name", "kind", "items", "statuses", "models", "configs"],
                  [[s["name"], s["kind"], s["items"], status_str(s), ", ".join(s["models"]) or "-",
                    len(s["fingerprints"]) or "-"] for s in rep["sources"]])]
    for s in rep["sources"]:
        o += [f"- **{s['name']}**: {w}" for w in s["warnings"]]
    for q in label_qs(rep):
        o += ["", f"## {q['id']}", "", f"_{q['ask']}_", "", md_table(PAIR_HEAD, [pair_row(p) for p in q["pairs"]])]
        if q["fleiss"]:
            o += ["", f"Fleiss' kappa ({', '.join(q['fleiss']['sources'])}; {q['fleiss']['n']} items): "
                      f"**{f2(q['fleiss']['kappa'])}**"]
        o.append("")
        for p in q["pairs"]:
            o.append(f"![{q['id']}: {pair_name(p)}]({figdir}/confusion-{slug(q['id'])}-{slug(p['a'])}-vs-"
                     f"{slug(p['b'])}.svg)")
        if q["screening"]:
            o += ["", f"### Screening vs {rep['reference']} (pass = {', '.join(q['pass'])})", "",
                  md_table(SCREEN_HEAD, [screening_row(s) for s in q["screening"]])]
            for s in q["screening"]:
                if s["missed"]:
                    o += ["", f"Missed by {s['source']}:"] + [f"- {title_of(rep, i, 120)} (`{i}`)" for i in s["missed"]]
    for q in tag_qs(rep):
        o += ["", f"## {q['id']} (tags)", "", f"_{q['ask']}_", "",
              "Items tagged by each source, and kappa per tag (present / absent):", "", md_table(*tag_table(rep, q))]
        for s in q["screening"]:
            o += ["", f"{s['source']} vs {rep['reference']}:", "",
                  md_table(["tag", f"n ({rep['reference']})", "recall", "precision"],
                           [[t["tag"], t["n_ref"], f2(t["recall"]["value"]), f2(t["precision"]["value"])]
                            for t in s["per_tag"]])]
    o += ["", f"{len(rep['disagreements'])} disagreements: see `csv/disagreements.csv`.", "", DEFS]
    return "\n".join(o) + "\n"


DEFS = """**Definitions.** Agreement: share of items with the same answer. Kappa: Cohen's kappa, agreement
corrected for chance (1 = perfect, 0 = chance level); n/a when undefined. Proportion intervals: Wilson 95%;
kappa intervals: bootstrap 95% over items. Screening view: the reference's and the source's labels are
collapsed to pass (go on to full text) or fail; recall = share of the reference's passes that the source also
passes; precision = share of the source's passes that the reference passes; WSS = work saved over sampling
at that recall (Cohen et al. 2006)."""


def status_str(s):
    return ", ".join(f"{k} {v}" for k, v in sorted(s["statuses"].items())) or "-"


def meta_line(rep):
    m = rep["meta"]
    return (f"Made by criba-ref {m['version']} on {m['date']}. Config: `{m['config']}`. Bootstrap: {m['bootstrap']} "
            f"resamples, seed {m['seed']}." + (f" Command: `{m['command']}`" if m["command"] else ""))


# ---------- HTML ----------

CSS = """
:root{--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--line:#e4e3df;--card:#ffffff}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--line:#383835;--card:#242422}}
:root[data-theme="dark"]{--bg:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--line:#383835;--card:#242422}
body{background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;padding:24px 16px}
main{max-width:980px;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 4px} h2{font-size:1.2rem;margin:32px 0 4px} h3{font-size:1rem;margin:20px 0 6px}
.meta,.ask{color:var(--ink2);font-size:.9rem} .warn{color:#b3541e}
.scroll{overflow-x:auto} table{border-collapse:collapse;margin:8px 0;font-variant-numeric:tabular-nums}
th,td{padding:4px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left} th{color:var(--ink2);font-weight:600}
.figs{display:flex;flex-wrap:wrap;gap:12px} .figs svg{max-width:100%;height:auto}
details{margin:6px 0} summary{cursor:pointer;color:var(--ink2)}
"""


def h_table(header, rows):
    e = html.escape
    return ('<div class="scroll"><table><thead><tr>' + "".join(f"<th>{e(str(c))}</th>" for c in header) +
            "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>"
                                             for r in rows) + "</tbody></table></div>")


def html_page(rep):
    e = html.escape
    o = [f"<h1>Comparison of screenings</h1>", f'<p class="meta">{e(meta_line(rep))}</p>', "<h2>Sources</h2>",
         h_table(["name", "kind", "items", "statuses", "models", "configs"],
                 [[s["name"], s["kind"], s["items"], status_str(s), ", ".join(s["models"]) or "-",
                   len(s["fingerprints"]) or "-"] for s in rep["sources"]])]
    for s in rep["sources"]:
        o += [f'<p class="warn">{e(s["name"])}: {e(w)}</p>' for w in s["warnings"]]
    for q in label_qs(rep):
        o += [f"<h2>{e(q['id'])}</h2>", f'<p class="ask">{e(q["ask"])}</p>',
              h_table(PAIR_HEAD, [pair_row(p) for p in q["pairs"]])]
        if q["fleiss"]:
            o.append(f"<p>Fleiss' kappa ({e(', '.join(q['fleiss']['sources']))}; {q['fleiss']['n']} items): "
                     f"<b>{f2(q['fleiss']['kappa'])}</b></p>")
        o.append('<div class="figs">' + "".join(svg_confusion(q["labels"], p["matrix"], p["a"], p["b"])
                                               for p in q["pairs"]) + "</div>")
        if q["screening"]:
            o += [f"<h3>Screening vs {e(rep['reference'])} (pass = {e(', '.join(q['pass']))})</h3>",
                  h_table(SCREEN_HEAD, [screening_row(s) for s in q["screening"]])]
            for s in q["screening"]:
                if s["missed"]:
                    items = "".join(f"<li>{link(rep, i)}</li>" for i in s["missed"])
                    o.append(f"<details open><summary>Missed by {e(s['source'])} ({len(s['missed'])})</summary>"
                             f"<ul>{items}</ul></details>")
    for q in tag_qs(rep):
        o += [f"<h2>{e(q['id'])} (tags)</h2>", f'<p class="ask">{e(q["ask"])}</p>',
              "<p>Items tagged by each source, and kappa per tag (present / absent)</p>", h_table(*tag_table(rep, q))]
        for s in q["screening"]:
            o += [f"<h3>{e(s['source'])} vs {e(rep['reference'])}</h3>",
                  h_table(["tag", f"n ({rep['reference']})", "recall", "precision"],
                          [[t["tag"], t["n_ref"], f2(t["recall"]["value"]), f2(t["precision"]["value"])]
                           for t in s["per_tag"]])]
    o.append(f"<h2>Disagreements ({len(rep['disagreements'])})</h2>")
    names = [s["name"] for s in rep["sources"]]
    rows = "".join(
        f"<details><summary>{e(r['question'])}: {link(rep, r['id'])} — " +
        e(" · ".join(f"{n}: {r['answers'][n][0] or '–'}" for n in names)) + "</summary><ul>" +
        "".join(f"<li><b>{e(n)}</b>: {e(r['answers'][n][1] or '')}</li>" for n in names if r["answers"][n][0]) +
        "</ul></details>" for r in rep["disagreements"])
    o.append(rows or "<p>None.</p>")
    o.append(f'<p class="meta">{e(DEFS)}</p>'.replace("**", ""))
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>Screening comparison</title><style>{CSS}</style></head><body><main>" + "\n".join(o) +
            "</main></body></html>\n")


def link(rep, iid):
    t, url = rep["titles"].get(iid) or ["", ""]
    t = html.escape(t or iid)
    return f'<a href="{html.escape(url)}">{t}</a>' if url else t


# ---------- LaTeX ----------

TEX_ESC = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
           "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def tex(s):
    return "".join(TEX_ESC.get(c, c) for c in str(s))


def t2(x):
    return "--" if x is None else f"{r2(x)}"


def tci(c):
    return "" if not c else f"[{r2(c[0])}, {r2(c[1])}]"


def tex_table(caption, label, colspec, header, rows, note=""):
    lines = [r"\begin{table}", r"\centering", r"\small", rf"\caption{{{caption}}}", rf"\label{{{label}}}",
             rf"\begin{{tabular}}{{{colspec}}}", r"\toprule", " & ".join(header) + r" \\", r"\midrule"]
    lines += [" & ".join(map(str, r)) + r" \\" for r in rows]
    lines += [r"\bottomrule", r"\end{tabular}"]
    if note:
        lines += [r"\par\smallskip", rf"\parbox{{\linewidth}}{{\footnotesize {note}}}"]
    lines.append(r"\end{table}")
    return "\n".join(lines) + "\n"


def latex(rep):
    """-> {file name: text}: table fragments to \\input, and report.tex that inputs them all."""
    files = {}
    for q in label_qs(rep):
        qs = slug(q["id"])
        rows = [[tex(p["a"]) + " vs " + tex(p["b"]), p["n"], t2(p["agreement"]), tci(p["agreement_ci"]),
                 t2(p["kappa"]), tci(p["kappa_ci"])] for p in q["pairs"]]
        note = ("Agreement: share of papers with the same label; 95\\% Wilson interval. $\\kappa$: Cohen's kappa; "
                f"95\\% bootstrap interval ({rep['meta']['bootstrap']} resamples).")
        if q["fleiss"]:
            note += f" Fleiss' $\\kappa$ over all {len(q['fleiss']['sources'])} sources ({q['fleiss']['n']} papers): " \
                    f"{t2(q['fleiss']['kappa'])}."
        files[f"agreement-{qs}.tex"] = tex_table(
            f"Agreement on \\texttt{{{tex(q['id'])}}} between screenings.", f"tab:agreement-{qs}", "lrrcrc",
            ["Pair", "$n$", "Agreement", "95\\% CI", "$\\kappa$", "95\\% CI"], rows, note)
        for p in q["pairs"]:
            files[f"confusion-{qs}-{slug(p['a'])}-vs-{slug(p['b'])}.tex"] = tex_confusion(q, p)
        if q["screening"]:
            rows = [[tex(s["source"]), s["n"], f"{t2(s['recall']['value'])} {tci(s['recall']['ci'])}",
                     f"{t2(s['precision']['value'])} {tci(s['precision']['ci'])}", t2(s["specificity"]["value"]),
                     t2(s["f1"]), f"{100 * s['share_pass']:.0f}\\%" if s["share_pass"] is not None else "--",
                     t2(s["wss"])] for s in q["screening"]]
            files[f"screening-{qs}.tex"] = tex_table(
                f"Screening on \\texttt{{{tex(q['id'])}}} against {tex(rep['reference'])}: "
                f"{tex(', '.join(q['pass']))} count as passing to full text.", f"tab:screening-{qs}", "lrccrrrr",
                ["Screening", "$n$", "Recall", "Precision", "Specificity", "F1", "To full text", "WSS"], rows,
                "Recall and precision with 95\\% Wilson intervals. WSS: work saved over sampling (Cohen et al., 2006).")
    for q in tag_qs(rep):
        qs = slug(q["id"])
        head, rows = tag_table(rep, q, t2)
        names = [s["name"] for s in rep["sources"]]
        head = ["Tag"] + [tex(n) for n in names] + [r"$\kappa$ " + tex(p["a"]) + " vs " + tex(p["b"]) for p in q["pairs"]]
        rows = [[tex(c) for c in r] for r in rows]
        rows[-1][0] = r"\midrule Mean Jaccard"
        ns = ", ".join(f"{tex(n)} {q['n_by_source'][n]}" for n in names)
        files[f"tags-{qs}.tex"] = tex_table(
            f"Agreement on the \\texttt{{{tex(q['id'])}}} tags: papers tagged by each screening, and Cohen's "
            f"$\\kappa$ per tag (present or absent).", f"tab:tags-{qs}", "l" + "r" * (len(names) + len(q["pairs"])),
            head, rows, f"Papers labelled: {ns}. Tags no screening used are left out.")
    body = "\n".join(rf"\input{{{name}}}" for name in files)
    m = rep["meta"]
    url = lambda s: r"\url{" + re.sub(r"[{}%#\\]", "", s) + "}"
    meta = (f"Made by criba-ref {tex(m['version'])} on {tex(m['date'])}, bootstrap {m['bootstrap']} resamples "
            f"with seed {m['seed']}.\\\\\nConfig: {url(m['config'])}" +
            (f"\\\\\nCommand: {url(m['command'])}" if m["command"] else ""))
    files["report.tex"] = (
        "% Preview of the tables. In a paper, \\input the fragments; they need booktabs and xcolor[table].\n"
        "\\documentclass{article}\n\\usepackage[margin=2cm]{geometry}\n\\usepackage{booktabs}\n"
        "\\usepackage[table]{xcolor}\n\\usepackage{float}\n\\usepackage[obeyspaces,spaces]{url}\n"
        "\\floatplacement{table}{H}\n\\begin{document}\n"
        f"\\section*{{Comparison of screenings}}\n{{\\small\\raggedright {meta}\\par}}\n\n{body}\n\\end{{document}}\n")
    return files


def tex_confusion(q, p):
    """The confusion matrix as a shaded tabular (needs xcolor with the table option)."""
    k = len(q["labels"])
    vmax = max(max(r) for r in p["matrix"])
    lines = [r"\begin{table}", r"\centering", r"\small",
             rf"\caption{{\texttt{{{tex(q['id'])}}}: {tex(p['a'])} (rows) against {tex(p['b'])} (columns); "
             rf"{p['agree']} of {p['n']} papers agree.}}",
             rf"\label{{tab:confusion-{slug(q['id'])}-{slug(p['a'])}-vs-{slug(p['b'])}}}",
             rf"\setlength{{\tabcolsep}}{{8pt}}\renewcommand{{\arraystretch}}{{1.6}}",
             rf"\begin{{tabular}}{{l{'c' * k}}}",
             rf" & \multicolumn{{{k}}}{{c}}{{{tex(p['b'])}}} \\",
             f"{tex(p['a'])} & " + " & ".join(tex(x) for x in q["labels"]) + r" \\", r"\midrule"]
    for i, lab in enumerate(q["labels"]):
        cells = []
        for j in range(k):
            v = p["matrix"][i][j]
            fill, ink = shade(v, vmax)
            txt = rf"\textbf{{{v}}}" if i == j else str(v)
            if fill:
                txt = rf"\cellcolor[HTML]{{{fill[1:].upper()}}}" + (rf"\textcolor{{white}}{{{txt}}}" if ink == "#ffffff"
                                                                    else txt)
            cells.append(txt)
        lines.append(f"{tex(lab)} & " + " & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    return "\n".join(lines) + "\n"


# ---------- writing ----------

def write(rep, out_dir, formats=FORMATS):
    out = Path(out_dir)
    written = []

    def put(rel, txt):
        p = out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, encoding="utf-8")
        written.append(str(p))
    if "html" in formats:
        put("report.html", html_page(rep))
    if "md" in formats:
        put("report.md", markdown(rep))
        for name, svg in figures(rep).items():
            put(f"figures/{name}", svg)
    if "csv" in formats:
        for name, txt in csvs(rep).items():
            put(f"csv/{name}", txt)
    if "latex" in formats:
        for name, txt in latex(rep).items():
            put(f"latex/{name}", txt)
    return written
