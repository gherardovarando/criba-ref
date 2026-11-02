"""Screen items one by one: ask the model, check its answer, write one JSON file per item."""
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import backends, config, prompt, readers


def clean(raw, questions):
    """The model's answer -> {qid: {"label"|"labels"|"text": ..., "reason": ...}}, and the problems found.
    A label outside the list becomes the question's fallback (or null), unknown tags are dropped."""
    out, problems = {}, []
    for q in questions:
        qid, key = q["id"], prompt.answer_key(q)
        a = raw.get(qid)
        if not isinstance(a, dict):
            a = {key: a} if a is not None else {}
        reason = " ".join(str(a.get("basis") or a.get("reason") or "").split())
        val = a.get(key)
        if key == "text":
            val = " ".join(str(val or "").split())
            if not val:
                problems.append(f"{qid}: empty answer")
        elif key == "labels":
            vals = val if isinstance(val, list) else [val] if val else []
            bad = [v for v in vals if v not in q["labels"]]
            val = [x for x in q["labels"] if x in vals]  # in the config's order, no repeats
            if bad:
                problems.append(f"{qid}: unknown label(s) {bad} dropped")
        elif val not in q["labels"]:
            problems.append(f"{qid}: {val!r} is not a label" + (f", used {q['fallback']!r}" if q.get("fallback") else ""))
            val = q.get("fallback")
        out[qid] = {key: val, "reason": reason}
    return out, problems


def missing_answer(cfg):
    """The answer written without a model call for an item with no abstract. Questions with no
    configured answer get null (or [] for tags)."""
    given = cfg["missing_abstract"]["answer"]
    out = {}
    for q in cfg["question"]:
        key = prompt.answer_key(q)
        default = [] if key == "labels" else None
        out[q["id"]] = {key: given.get(q["id"], default), "reason": "no abstract"}
    return out


def result_path(cfg, item):
    return Path(cfg["output"]["dir"]) / readers.file_name(item.id)


def load_result(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, path)


def screen_item(cfg, system, sch, fp, item):
    """-> the result dict for one item (it doesn't write it)."""
    m = cfg["model"]
    res = {"id": item.id, "item": item.meta(), "answers": None, "status": "ok", "problems": [],
           "model": {"backend": m["backend"], "name": m["name"]}, "fingerprint": fp,
           "date": datetime.now().isoformat(timespec="seconds")}
    if not item.abstract and cfg["missing_abstract"]["skip_model"]:
        res["answers"], res["status"] = missing_answer(cfg), "no_abstract"
        res["model"] = None
        return res
    t0 = time.monotonic()
    try:
        raw, used = backends.complete(m, system, prompt.item_text(item, cfg["prompt"]["abstract_chars"]), sch)
    except backends.BackendError as e:
        res["status"], res["problems"] = "error", [str(e)]
        return res
    res["model"]["used"] = used
    res["seconds"] = round(time.monotonic() - t0, 1)
    res["answers"], res["problems"] = clean(raw, cfg["question"])
    if res["problems"]:
        res["status"] = "invalid_answer"
    return res


@dataclass
class Stats:
    total: int = 0
    done: int = 0
    skipped: int = 0   # already screened with the current fingerprint
    stale: int = 0     # screened with another prompt, questions or model; left as they are
    counts: dict = field(default_factory=dict)  # status -> n

    def add(self, status):
        self.done += 1
        self.counts[status] = self.counts.get(status, 0) + 1


def run(cfg, items, rerun=False, retry_errors=True, dry_run=False, limit=None, log=sys.stderr):
    """Screen the items that have no result with the current fingerprint. Results with an error are
    tried again; results made with another fingerprint only with rerun=True. limit: screen at most
    that many items (the ones already done don't count)."""
    system = prompt.system_prompt(cfg)
    sch = prompt.schema(cfg["question"])
    fp = config.fingerprint(cfg, system)
    st = Stats(total=len(items))
    for n, item in enumerate(items, 1):
        path = result_path(cfg, item)
        old = load_result(path)
        if old and old.get("id") == item.id:
            same = old.get("fingerprint") == fp
            if same and not (retry_errors and old.get("status") == "error"):
                st.skipped += 1
                continue
            if not same and not rerun:
                st.stale += 1
                continue
        if limit is not None and st.done >= limit:
            break
        if dry_run:
            print(f"[{n}/{len(items)}] would screen {item.id}", file=log)
            st.add("dry_run")
            continue
        res = screen_item(cfg, system, sch, fp, item)
        write_json(path, res)
        st.add(res["status"])
        extra = f" ({res['problems'][0]})" if res["status"] == "error" else ""
        print(f"[{n}/{len(items)}] {item.id}: {res['status']}{extra}", file=log, flush=True)
    return st


def results(cfg, items=None):
    """The result files in the output folder, in input order when items are given."""
    out_dir = Path(cfg["output"]["dir"])
    if items is not None:
        found = (load_result(result_path(cfg, it)) for it in items)
        return [r for r in found if r]
    return [r for r in (load_result(p) for p in sorted(out_dir.glob("*.json"))) if r]
