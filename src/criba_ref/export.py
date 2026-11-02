"""Results -> one CSV row per item, and counts per label."""
import csv
import io
from collections import Counter

from . import prompt

# Fixed CSV columns: a question can't have one of these ids (nor end in _reason, see config.validate)
COLUMNS = ("id", "title", "year", "doi", "url", "status", "screened_by", "date", "problems")


def value(answer, q):
    if not answer:
        return ""
    v = answer.get(prompt.answer_key(q))
    if isinstance(v, list):
        return "; ".join(v)
    return "" if v is None else str(v)


def rows(cfg, results):
    qs = cfg["question"]
    header = ["id", "title", "year", "doi", "url", "status"]
    for q in qs:
        header += [q["id"], f"{q['id']}_reason"]
    header += ["screened_by", "date", "problems"]
    out = [header]
    for r in results:
        it, ans = r.get("item") or {}, r.get("answers") or {}
        model = r.get("model") or {}
        row = [r.get("id", ""), it.get("title", ""), it.get("year", ""), it.get("doi", ""), it.get("url", ""),
               r.get("status", "")]
        for q in qs:
            a = ans.get(q["id"])
            row += [value(a, q), (a or {}).get("reason", "")]
        row += [model.get("used") or model.get("name") or "", r.get("date", ""), " | ".join(r.get("problems") or [])]
        out.append(row)
    return out


def to_csv(cfg, results):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows(cfg, results))
    return buf.getvalue()


def stats(cfg, results):
    """-> text: counts per status and, for each label question, per label."""
    lines = [f"{len(results)} results"]
    st = Counter(r.get("status", "?") for r in results)
    lines.append("status: " + ", ".join(f"{k} {v}" for k, v in st.most_common()))
    for q in cfg["question"]:
        if q["type"] != "label":
            continue
        c = Counter()
        for r in results:
            a = (r.get("answers") or {}).get(q["id"])
            v = (a or {}).get(prompt.answer_key(q))
            for x in (v if isinstance(v, list) else [v]):
                c[x if x is not None else "(none)"] += 1
        order = [x for x in q["labels"] if c[x]] + [x for x in c if x not in q["labels"]]
        lines.append(f"{q['id']}: " + ", ".join(f"{x} {c[x]}" for x in order))
    return "\n".join(lines)
