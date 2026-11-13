"""Screen with the Anthropic Message Batches API: half the price of single calls, results within 24 hours.

`submit` sends the items that `run` would screen, in batches of at most CHUNK requests, and keeps a note of
each batch in <output.dir>/_batches/<batch id>.json (the batch id, the fingerprint, the model and which
request is which item). `collect` writes the answers of the batches that have ended as ordinary result
files, the same as `run` writes. Items with an error are sent again by the next `submit`."""
import json
import sys
from datetime import datetime
from pathlib import Path

from . import backends, config, prompt, screen

CHUNK = 10000  # requests per batch (the API takes up to 100,000 or 256 MB)


class BatchError(Exception):
    pass


def notes_dir(cfg):
    return Path(cfg["output"]["dir"]) / "_batches"


def notes(cfg):
    d = notes_dir(cfg)
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


def save_note(cfg, note):
    screen.write_json(notes_dir(cfg) / f"{note['batch']}.json", note)


def check_backend(cfg):
    if cfg["model"]["backend"] != "anthropic":
        raise BatchError(f"batches need model.backend = \"anthropic\" (it is {cfg['model']['backend']!r})")


def submit(cfg, items, rerun=False, limit=None, dry_run=False, client=None, log=sys.stderr):
    """Send the items that have no current result and are not in a batch yet. -> (counts, batch ids)."""
    check_backend(cfg)
    m = cfg["model"]
    system, sch = prompt.system_prompt(cfg), prompt.schema(cfg["question"])
    fp = config.fingerprint(cfg, system)
    waiting = {i for n in notes(cfg) if not n["collected"] and n["fingerprint"] == fp for i in n["ids"].values()}
    counts = {"skipped": 0, "stale": 0, "in_a_batch": 0, "no_abstract": 0, "to_send": 0}
    todo = []
    for item in items:
        path = screen.result_path(cfg, item)
        what = screen.state(path, item, fp, rerun)
        if what == "skip":
            counts["skipped"] += 1
        elif what == "stale":
            counts["stale"] += 1
        elif item.id in waiting:
            counts["in_a_batch"] += 1
        elif not item.abstract and cfg["missing_abstract"]["skip_model"]:
            counts["no_abstract"] += 1
            if not dry_run:  # no model call needed: written now, as `run` does
                screen.write_json(path, screen.screen_item(cfg, system, sch, fp, item))
        elif limit is None or len(todo) < limit:
            todo.append(item)
    counts["to_send"] = len(todo)
    if dry_run or not todo:
        return counts, []
    client = client or backends.client()
    ids = []
    for start in range(0, len(todo), CHUNK):
        part = todo[start:start + CHUNK]
        reqs, which = [], {}
        for n, item in enumerate(part, start + 1):
            cid = f"r{n:06d}"  # custom ids allow only [A-Za-z0-9_-]; item ids may hold "/" or "."
            which[cid] = item.id
            text = prompt.item_text(item, cfg["prompt"]["abstract_chars"])
            reqs.append({"custom_id": cid, "params": backends.anthropic_params(m, system, text, sch)})
        b = client.messages.batches.create(requests=reqs)
        save_note(cfg, {"batch": b.id, "created": datetime.now().isoformat(timespec="seconds"), "fingerprint": fp,
                        "model": m["name"], "effort": m.get("effort") or "", "requests": len(reqs),
                        "collected": False, "ids": which})
        print(f"sent batch {b.id}: {len(reqs)} requests", file=log, flush=True)
        ids.append(b.id)
    return counts, ids


def status(cfg, client=None):
    """-> one dict per batch: id, model, requests, the API's status and counts (unless collected)."""
    out = []
    for n in notes(cfg):
        row = {"batch": n["batch"], "created": n["created"], "model": n["model"], "requests": n["requests"],
               "collected": n["collected"]}
        if not n["collected"]:
            client = client or backends.client()
            b = client.messages.batches.retrieve(n["batch"])
            rc = b.request_counts
            row["status"] = b.processing_status
            row["counts"] = {k: getattr(rc, k) for k in ("processing", "succeeded", "errored", "canceled", "expired")}
        out.append(row)
    return out


def result_of(cfg, note, item, r):
    """One batch result -> a result dict, as `run` writes it."""
    res = screen.new_result(cfg, item, note["fingerprint"])
    res["model"] = {"backend": "anthropic", "name": note["model"]}
    res["batch"] = note["batch"]
    if r.result.type != "succeeded":
        err = getattr(r.result, "error", None)
        detail = getattr(getattr(err, "error", err), "type", "") if err else ""
        res["status"], res["problems"] = "error", [f"batch request {r.result.type}" + (f": {detail}" if detail else "")]
        return res
    try:
        raw, used, usage = backends.anthropic_answer(r.result.message)
    except backends.BackendError as e:
        res["status"], res["problems"] = "error", [str(e)]
        return res
    return screen.fill(res, cfg, raw, used, usage)


def collect(cfg, items, client=None, log=sys.stderr):
    """Write the results of the batches that have ended. A result already there is kept when it is fine
    and made with the batch's or the current fingerprint. -> counts per status."""
    check_backend(cfg)
    fp_now = config.fingerprint(cfg, prompt.system_prompt(cfg))
    by_id = {it.id: it for it in items}
    counts = {}
    for note in notes(cfg):
        if note["collected"]:
            continue
        client = client or backends.client()
        b = client.messages.batches.retrieve(note["batch"])
        if b.processing_status != "ended":
            print(f"batch {note['batch']}: {b.processing_status}, not ended yet", file=log)
            continue
        if note["fingerprint"] != fp_now:
            print(f"warning: batch {note['batch']} was sent with another config: its results are stale", file=log)
        mine = {}
        for r in client.messages.batches.results(note["batch"]):
            item = by_id.get(note["ids"].get(r.custom_id))
            if item is None:
                mine["not_in_input"] = mine.get("not_in_input", 0) + 1
                continue
            path = screen.result_path(cfg, item)
            old = screen.load_result(path)
            if (old and old.get("id") == item.id and old.get("status") != "error"
                    and old.get("fingerprint") in (note["fingerprint"], fp_now)):
                mine["kept"] = mine.get("kept", 0) + 1
                continue
            res = result_of(cfg, note, item, r)
            screen.write_json(path, res)
            mine[res["status"]] = mine.get(res["status"], 0) + 1
        note["collected"] = datetime.now().isoformat(timespec="seconds")
        note["result_counts"] = mine
        save_note(cfg, note)
        print(f"batch {note['batch']}: " + ", ".join(f"{k} {v}" for k, v in sorted(mine.items())), file=log)
        for k, v in mine.items():
            counts[k] = counts.get(k, 0) + v
    return counts


def cancel(cfg, client=None, log=sys.stderr):
    """Cancel the batches still running. Requests already answered are kept: `collect` writes them."""
    n = 0
    for note in notes(cfg):
        if note["collected"]:
            continue
        client = client or backends.client()
        if client.messages.batches.retrieve(note["batch"]).processing_status == "in_progress":
            client.messages.batches.cancel(note["batch"])
            print(f"cancelling batch {note['batch']}", file=log)
            n += 1
    return n
