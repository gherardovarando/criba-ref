"""Compare screenings: model result folders and CSV labels (e.g. an expert's), question by question.

Everything here is plain data and pure functions; report.py turns the report dict into text, HTML,
Markdown, CSV and LaTeX."""
import csv
import glob
import itertools
import math
import os
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import __version__, config, prompt, readers, screen

EMPTY_TAGS = {"none", "-", "no", "[]"}  # an explicit "no tag" in a CSV cell; an empty cell is "not labelled"


# ---------- metrics ----------

def confusion(pairs, labels):
    """pairs: [(a, b)] -> matrix m[i][j] = count of a == labels[i] and b == labels[j]."""
    idx = {x: i for i, x in enumerate(labels)}
    m = [[0] * len(labels) for _ in labels]
    for a, b in pairs:
        m[idx[a]][idx[b]] += 1
    return m


def agreement(m):
    """-> (number of agreements, n)."""
    return sum(m[i][i] for i in range(len(m))), sum(map(sum, m))


def cohen_kappa(m):
    """Cohen's kappa of a square confusion matrix; None when it is undefined (no items, or chance
    agreement is 1, e.g. both sources always give the same single label)."""
    n = sum(map(sum, m))
    if n == 0:
        return None
    po = sum(m[i][i] for i in range(len(m))) / n
    rows = [sum(r) / n for r in m]
    cols = [sum(m[i][j] for i in range(len(m))) / n for j in range(len(m))]
    pe = sum(r * c for r, c in zip(rows, cols))
    if pe >= 1 - 1e-12:
        return None
    return (po - pe) / (1 - pe)


def fleiss_kappa(table):
    """table[i][j] = number of raters who gave item i category j (same number of raters per item).
    None when undefined."""
    if not table:
        return None
    r = sum(table[0])
    n = len(table)
    if r < 2 or any(sum(row) != r for row in table):
        return None
    p_j = [sum(row[j] for row in table) / (n * r) for j in range(len(table[0]))]
    p_i = [(sum(c * c for c in row) - r) / (r * (r - 1)) for row in table]
    p_bar, pe = sum(p_i) / n, sum(p * p for p in p_j)
    if pe >= 1 - 1e-12:
        return None
    return (p_bar - pe) / (1 - pe)


def wilson(k, n, z=1.959964):
    """95% Wilson score interval for k successes out of n; None when n == 0."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def bootstrap_kappa(pairs, labels, reps=1000, seed=0):
    """Percentile 95% interval of Cohen's kappa over `reps` resamples of the items; None if most
    resamples leave kappa undefined, or reps is 0."""
    if reps <= 0 or not pairs:
        return None
    rng = random.Random(seed)
    vals = []
    for _ in range(reps):
        k = cohen_kappa(confusion([pairs[rng.randrange(len(pairs))] for _ in pairs], labels))
        if k is not None:
            vals.append(k)
    if len(vals) < reps / 2:
        return None
    vals.sort()
    return (vals[int(0.025 * (len(vals) - 1))], vals[int(round(0.975 * (len(vals) - 1)))])


def screening(pairs, passing):
    """pairs: [(reference label, source label)]; passing: the labels that go on to full text.
    -> counts and rates of the source's pass/fail decisions against the reference's."""
    tp = sum(1 for r, s in pairs if r in passing and s in passing)
    fn = sum(1 for r, s in pairs if r in passing and s not in passing)
    fp = sum(1 for r, s in pairs if r not in passing and s in passing)
    tn = len(pairs) - tp - fn - fp
    n = len(pairs)

    def rate(k, d):
        return {"value": k / d if d else None, "ci": wilson(k, d), "k": k, "n": d}
    rec = rate(tp, tp + fn)
    prec = rate(tp, tp + fp)
    f1 = 2 * tp / (2 * tp + fp + fn) if (tp + fp + fn) else None
    # Work saved over sampling (Cohen et al. 2006): the share of items not read, minus what random
    # sampling would save at the same recall.
    wss = (tn + fn) / n - (1 - rec["value"]) if n and rec["value"] is not None else None
    return {"n": n, "tp": tp, "fn": fn, "fp": fp, "tn": tn, "recall": rec, "precision": prec,
            "specificity": rate(tn, tn + fp), "f1": f1, "share_pass": (tp + fp) / n if n else None, "wss": wss}


def jaccard(a, b):
    a, b = set(a), set(b)
    return 1.0 if not a | b else len(a & b) / len(a | b)


# ---------- sources ----------

@dataclass
class Source:
    name: str
    kind: str                       # "results" or "csv"
    path: str
    answers: dict = field(default_factory=dict)  # item id -> {qid: label | tuple of tags | text}
    reasons: dict = field(default_factory=dict)  # item id -> {qid: reason}
    titles: dict = field(default_factory=dict)   # item id -> (title, url), from result files
    statuses: Counter = field(default_factory=Counter)
    models: Counter = field(default_factory=Counter)
    fingerprints: Counter = field(default_factory=Counter)
    warnings: list = field(default_factory=list)

    def info(self):
        return {"name": self.name, "kind": self.kind, "path": self.path, "items": len(self.answers),
                "statuses": dict(self.statuses), "models": dict(self.models),
                "fingerprints": dict(self.fingerprints), "warnings": self.warnings}


def norm_id(x):
    x = str(x or "").strip()
    return readers.norm_doi(x) or x


def parse_spec(spec):
    """"name=path" or "path" -> (name, path). The name of a plain path is its folder or file stem."""
    name, sep, path = spec.partition("=")
    if not sep or not name or os.sep in name:
        path = spec
        name = Path(spec.rstrip("/")).stem if Path(spec).suffix else Path(spec.rstrip("/")).name
    return name, path


def load_results(name, path, questions):
    src = Source(name, "results", str(path))
    for f in sorted(glob.glob(os.path.join(path, "*.json"))):
        r = screen.load_result(f)
        if not r or "id" not in r:
            continue
        st = r.get("status", "?")
        src.statuses[st] += 1
        if st == "error" or not r.get("answers"):
            continue
        m = r.get("model") or {}
        src.models[m.get("used") or m.get("name") or "-"] += 1
        src.fingerprints[r.get("fingerprint", "?")] += 1
        it = r.get("item") or {}
        src.titles[r["id"]] = (it.get("title", ""), it.get("url", ""))
        ans, why = {}, {}
        for q in questions:
            a = r["answers"].get(q["id"]) or {}
            v = a.get(prompt.answer_key(q))
            if v is None:
                continue
            ans[q["id"]] = tuple(v) if isinstance(v, list) else v
            why[q["id"]] = a.get("reason", "")
        src.answers[r["id"]], src.reasons[r["id"]] = ans, why
    if not src.statuses:
        src.warnings.append(f"no result files in {path}")
    if len(src.fingerprints) > 1:
        src.warnings.append(f"results made with {len(src.fingerprints)} different configs (prompt, questions or "
                            "model): rerun with --rerun or keep one config per folder")
    return src


HINT = re.compile(r"\s*[\[(].*$")


def header_map(fieldnames, questions):
    """CSV header -> question id: "decision [include|exclude|maybe]" and "Decision" both map to decision."""
    qids = {q["id"].lower(): q["id"] for q in questions}
    out = {}
    for h in fieldnames or []:
        base = HINT.sub("", h or "").strip().lower()
        if base in qids:
            out[h] = qids[base]
    return out


def load_csv(name, path, questions):
    src = Source(name, "csv", str(path))
    qmap = {q["id"]: q for q in questions}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = csv.DictReader(fh)
        cols = header_map(rows.fieldnames, questions)
        id_col = next((h for h in rows.fieldnames or [] if h.strip().lower() == "id"), None)
        if not id_col:
            raise readers.InputError(f"{path}: no id column")
        if not cols:
            src.warnings.append(f"no column named after a question ({', '.join(qmap)})")
        for n, row in enumerate(rows, 2):
            iid = norm_id(row.get(id_col))
            if not iid:
                continue
            ans = {}
            for col, qid in cols.items():
                q, cell = qmap[qid], (row.get(col) or "").strip()
                if not cell:
                    continue
                if q["type"] == "text":
                    ans[qid] = cell
                    continue
                lower = {x.lower(): x for x in q["labels"]}
                if q["multiple"]:
                    parts = [p.strip().lower() for p in re.split(r"[;,]", cell) if p.strip()]
                    if all(p in EMPTY_TAGS for p in parts):
                        ans[qid] = ()
                        continue
                    bad = [p for p in parts if p not in lower]
                    if bad:
                        src.warnings.append(f"line {n}, {qid}: unknown label(s) {', '.join(bad)} ignored")
                    ans[qid] = tuple(x for x in q["labels"] if x.lower() in parts)
                elif cell.lower() in lower:
                    ans[qid] = lower[cell.lower()]
                else:
                    src.warnings.append(f"line {n}, {qid}: unknown label {cell!r}, left out")
            if ans:
                src.answers[iid] = ans
                src.statuses["labelled"] += 1
    return src


def load_source(spec, questions):
    name, path = parse_spec(spec)
    p = Path(path)
    if p.is_dir():
        return load_results(name, p, questions)
    if p.suffix.lower() in (".csv", ".tsv") and p.exists():
        return load_csv(name, p, questions)
    raise readers.InputError(f"{path}: not a results folder or a CSV file")


# ---------- the report ----------

def label_report(q, sources, ref, reps, seed):
    labels, qid = q["labels"], q["id"]
    out = {"id": qid, "type": "label", "ask": q["ask"], "labels": labels, "pass": config.pass_labels(q),
           "pairs": [], "fleiss": None, "screening": []}
    for a, b in pairs_of(sources, ref):
        ids = sorted(i for i in a.answers if qid in a.answers[i] and qid in b.answers.get(i, {}))
        pr = [(a.answers[i][qid], b.answers[i][qid]) for i in ids]
        m = confusion(pr, labels)
        k, n = agreement(m)
        out["pairs"].append({"a": a.name, "b": b.name, "n": n, "matrix": m, "agree": k,
                             "agreement": k / n if n else None, "agreement_ci": wilson(k, n),
                             "kappa": cohen_kappa(m), "kappa_ci": bootstrap_kappa(pr, labels, reps, seed)})
    if len(sources) >= 3:
        common = sorted(set.intersection(*[{i for i, x in s.answers.items() if qid in x} for s in sources]))
        if common:
            table = [[sum(1 for s in sources if s.answers[i][qid] == x) for x in labels] for i in common]
            out["fleiss"] = {"n": len(common), "kappa": fleiss_kappa(table), "sources": [s.name for s in sources]}
    passing = out["pass"]
    if ref and passing:
        for s in sources:
            if s is ref:
                continue
            ids = sorted(i for i in ref.answers if qid in ref.answers[i] and qid in s.answers.get(i, {}))
            res = screening([(ref.answers[i][qid], s.answers[i][qid]) for i in ids], set(passing))
            res["source"] = s.name
            res["missed"] = [i for i in ids if ref.answers[i][qid] in passing and s.answers[i][qid] not in passing]
            out["screening"].append(res)
    return out


def tags_report(q, sources, ref):
    labels, qid = q["labels"], q["id"]
    labelled = {s.name: [x[qid] for x in s.answers.values() if qid in x] for s in sources}
    counts = {name: {t: sum(t in v for v in vals) for t in labels} for name, vals in labelled.items()}
    used = [t for t in labels if any(counts[n][t] for n in counts)]
    out = {"id": qid, "type": "tags", "ask": q["ask"], "labels": labels, "used": used, "pairs": [], "screening": [],
           "counts": counts, "n_by_source": {n: len(v) for n, v in labelled.items()}}
    for a, b in pairs_of(sources, ref):
        ids = sorted(i for i in a.answers if qid in a.answers[i] and qid in b.answers.get(i, {}))
        per_tag = []
        for t in labels:
            pr = [(t in a.answers[i][qid], t in b.answers[i][qid]) for i in ids]
            m = confusion(pr, [True, False])
            k, n = agreement(m)
            per_tag.append({"tag": t, "a": sum(x for x, _ in pr), "b": sum(y for _, y in pr),
                            "agreement": k / n if n else None, "kappa": cohen_kappa(m)})
        out["pairs"].append({"a": a.name, "b": b.name, "n": len(ids), "per_tag": per_tag,
                             "jaccard": (sum(jaccard(a.answers[i][qid], b.answers[i][qid]) for i in ids) / len(ids)
                                         if ids else None)})
    if ref:
        for s in sources:
            if s is ref:
                continue
            ids = sorted(i for i in ref.answers if qid in ref.answers[i] and qid in s.answers.get(i, {}))
            per_tag = []
            for t in labels:
                r = screening([(t in ref.answers[i][qid], t in s.answers[i][qid]) for i in ids], {True})
                per_tag.append({"tag": t, "n_ref": r["tp"] + r["fn"], "recall": r["recall"],
                                "precision": r["precision"]})
            out["screening"].append({"source": s.name, "n": len(ids), "per_tag": per_tag})
    return out


def pairs_of(sources, ref):
    """Every unordered pair, in the order given; the reference always comes first (it is the rows)."""
    out = []
    for a, b in itertools.combinations(sources, 2):
        out.append((b, a) if b is ref else (a, b))
    return out


def disagreements(questions, sources, titles):
    rows = []
    for q in questions:
        if q["type"] == "text":
            continue
        qid = q["id"]
        ids = sorted({i for s in sources for i, x in s.answers.items() if qid in x})
        for i in ids:
            vals = {s.name: s.answers[i][qid] for s in sources if qid in s.answers.get(i, {})}
            if len(vals) < 2 or len(set(vals.values())) == 1:
                continue
            title, url = titles.get(i, ("", ""))
            rows.append({"id": i, "title": title, "url": url, "question": qid,
                         "answers": {s.name: (fmt_value(vals[s.name]) if s.name in vals else "",
                                              s.reasons.get(i, {}).get(qid, "")) for s in sources}})
    return rows


def fmt_value(v):
    return "; ".join(v) if isinstance(v, tuple) else str(v)


def build(cfg, specs, reference=None, qids=None, ids=None, reps=1000, seed=0, command=None):
    questions = [q for q in cfg["question"] if not qids or q["id"] in qids]
    if qids:
        unknown = set(qids) - {q["id"] for q in questions}
        if unknown:
            raise config.ConfigError(f"no question {', '.join(sorted(unknown))}")
    sources = [load_source(s, cfg["question"]) for s in specs]
    names = [s.name for s in sources]
    if len(sources) < 2:
        raise config.ConfigError("compare needs at least two sources")
    if len(set(names)) != len(names):
        raise config.ConfigError(f"two sources have the same name ({', '.join(names)}): use NAME=PATH")
    ref = None
    if reference:
        ref = next((s for s in sources if s.name == reference), None)
        if not ref:
            raise config.ConfigError(f"--reference {reference}: no source with that name ({', '.join(names)})")
    if ids is not None:
        keep = {norm_id(i) for i in ids}
        for s in sources:
            s.answers = {i: x for i, x in s.answers.items() if i in keep}
    titles = {}
    for s in sources:
        titles.update(s.titles)
    try:  # titles and urls from the input list, when it can be read
        items, _ = readers.read_items(cfg["input"]["file"], cfg["input"]["format"], cfg["input"]["fields"])
        titles.update({it.id: (it.title, it.url) for it in items})
    except (readers.InputError, OSError) as e:
        print(f"warning: can't read the input list for titles: {e}", file=sys.stderr)
    qs = []
    for q in questions:
        if q["type"] == "label" and not q["multiple"]:
            qs.append(label_report(q, sources, ref, reps, seed))
        elif q["type"] == "label":
            qs.append(tags_report(q, sources, ref))
    return {
        "meta": {"date": datetime.now().isoformat(timespec="seconds"), "version": __version__,
                 "command": command or "", "config": cfg["_path"], "bootstrap": reps, "seed": seed,
                 "ids_filter": ids is not None},
        "sources": [s.info() for s in sources],
        "reference": ref.name if ref else None,
        "questions": qs,
        "disagreements": disagreements(questions, sources, titles),
        "titles": {k: list(v) for k, v in titles.items()},
    }
