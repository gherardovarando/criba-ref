"""A blinded labelling sheet: the references and empty answer columns, for an expert to fill in.
No model answers are in it, so the expert's labels are independent of the models'."""
import csv
import json
import random
from datetime import datetime
from pathlib import Path

from . import __version__


def header(q):
    """The column name: the question id plus the allowed answers, e.g. "decision [include|exclude|maybe]".
    compare reads the id back and ignores the rest."""
    if q["type"] == "text":
        return f"{q['id']} (free text)"
    hint = f"{q['id']} [{'|'.join(q['labels'])}]"
    return hint + " (several, separated by ;, or none)" if q["multiple"] else hint


def choose(items, sample=None, seed=1, ids=None):
    if ids is not None:
        want = list(dict.fromkeys(ids))
        by_id = {it.id: it for it in items}
        missing = [i for i in want if i not in by_id]
        return [by_id[i] for i in want if i in by_id], missing
    if sample is not None and sample < len(items):
        return random.Random(seed).sample(items, sample), []
    return list(items), []


def write(path, items, questions, info):
    """Write the sheet and a sidecar PATH.json with how it was made. Never overwrites."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} exists already: not overwritten (it may hold an expert's work)")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "title", "year", "doi", "url", "abstract"] + [header(q) for q in questions] + ["comment"])
        for it in items:
            w.writerow([it.id, it.title, it.year, it.doi, it.url, it.abstract] + [""] * len(questions) + [""])
    side = path.with_name(path.name + ".json")
    side.write_text(json.dumps({**info, "date": datetime.now().isoformat(timespec="seconds"), "version": __version__,
                                "questions": [q["id"] for q in questions], "ids": [it.id for it in items]},
                               indent=2) + "\n")
    return side
