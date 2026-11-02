"""Project configuration: built-in defaults < criba-ref.toml < command-line options."""
import copy
import hashlib
import json
import re
import tomllib
from pathlib import Path

CONFIG_NAME = "criba-ref.toml"
BACKENDS = ("ollama", "claude-cli", "fake")

DEFAULTS = {
    "input": {"file": "", "format": "auto", "fields": {}},
    "output": {"dir": "results"},
    "model": {
        "backend": "ollama",           # "ollama", "claude-cli" or "fake" (tests)
        "name": "qwen3:4b-instruct",   # claude-cli: "haiku", "sonnet", "opus" or a full model name
        "url": "http://localhost:11434",
        "temperature": 0.0,            # ollama only; claude-cli uses its own
        "num_ctx": 8192,
        "think": None,
        "timeout": 300,
        "command": "claude",           # claude-cli: the program to run
    },
    "prompt": {"context": "", "file": "", "abstract_chars": 6000},
    "missing_abstract": {"skip_model": True, "answer": {}},
    "question": [],
}


class ConfigError(Exception):
    pass


def merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "fields":
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def find_config(path=None):
    """The config file: the given path (a file or a directory), else ./criba-ref.toml."""
    p = Path(path or ".")
    if p.is_dir():
        p = p / CONFIG_NAME
    if not p.exists():
        raise ConfigError(f"no config file at {p} (create one with `criba-ref init`)")
    return p


def load(path=None, overrides=None):
    p = find_config(path)
    try:
        raw = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p}: {e}") from None
    if "questions" in raw and "question" not in raw:
        raw["question"] = raw.pop("questions")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"{p}: unknown section(s) {', '.join(sorted(unknown))}")
    cfg = merge(DEFAULTS, raw)
    for section, values in (overrides or {}).items():
        cfg[section].update({k: v for k, v in values.items() if v is not None})
    base = p.parent.resolve()
    cfg["_path"] = str(p.resolve())
    cfg["_base"] = str(base)
    for section, key in (("input", "file"), ("output", "dir"), ("prompt", "file")):
        if cfg[section][key]:
            cfg[section][key] = str((base / Path(cfg[section][key]).expanduser()).resolve())
    validate(cfg)
    return cfg


RESERVED = ("id", "title", "year", "doi", "url", "status", "screened_by", "date", "problems")  # export.COLUMNS
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def validate(cfg):
    m = cfg["model"]
    if m["backend"] not in BACKENDS:
        raise ConfigError(f"model.backend must be one of {', '.join(BACKENDS)}, not {m['backend']!r}")
    if not cfg["input"]["file"]:
        raise ConfigError("input.file is not set")
    qs = cfg["question"]
    if not qs:
        raise ConfigError("no [[question]] in the config: add at least one")
    seen = set()
    for n, q in enumerate(qs, 1):
        where = f"question {n}"
        if not isinstance(q, dict) or not q.get("id"):
            raise ConfigError(f"{where}: needs an id")
        qid = q["id"]
        where = f"question {qid!r}"
        if not ID_RE.match(qid):
            raise ConfigError(f"{where}: id must start with a letter and use only letters, digits, - and _")
        if qid in seen:
            raise ConfigError(f"{where}: id used twice")
        if qid in RESERVED or qid.endswith("_reason"):
            raise ConfigError(f"{where}: this id is taken by a column of the CSV export; choose another")
        seen.add(qid)
        if not str(q.get("ask") or "").strip():
            raise ConfigError(f"{where}: needs `ask`, the question for the model")
        q.setdefault("type", "text" if "labels" not in q else "label")
        if q["type"] not in ("label", "text"):
            raise ConfigError(f"{where}: type must be \"label\" or \"text\"")
        q.setdefault("multiple", False)
        q.setdefault("examples", [])
        q.setdefault("notes", "")
        if q["type"] == "label":
            labels = q.get("labels")
            if not isinstance(labels, list) or not labels or not all(isinstance(x, str) and x for x in labels):
                raise ConfigError(f"{where}: labels must be a non-empty list of strings")
            if len(set(labels)) != len(labels):
                raise ConfigError(f"{where}: a label appears twice")
            fb = q.get("fallback")
            if fb is not None and fb not in labels:
                raise ConfigError(f"{where}: fallback {fb!r} is not one of its labels")
            if "pass" in q:
                if q["multiple"] or not isinstance(q["pass"], list) or any(x not in labels for x in q["pass"]):
                    raise ConfigError(f"{where}: pass must be a list of its labels (single-label questions only)")
        elif q.get("multiple") or q.get("labels") or "pass" in q:
            raise ConfigError(f"{where}: a text question has no labels")
    qmap = {q["id"]: q for q in qs}
    for qid, val in cfg["missing_abstract"]["answer"].items():
        q = qmap.get(qid)
        if not q:
            raise ConfigError(f"missing_abstract.answer: no question {qid!r}")
        vals = val if isinstance(val, list) else [val]
        if q["type"] == "label" and any(v not in q["labels"] for v in vals):
            raise ConfigError(f"missing_abstract.answer.{qid}: {val!r} is not one of its labels")
        if q["type"] == "label" and not q["multiple"] and isinstance(val, list):
            raise ConfigError(f"missing_abstract.answer.{qid}: takes one label, not a list")


def questions(cfg):
    return cfg["question"]


def pass_labels(q):
    """The labels that send a reference on (to full-text screening), for the screening view of
    `compare`: the question's `pass`, else every label but "exclude" when it has one, else None."""
    if q["type"] != "label" or q["multiple"]:
        return None
    if "pass" in q:
        return list(q["pass"])
    if "exclude" in q["labels"]:
        return [x for x in q["labels"] if x != "exclude"]
    return None


# Question keys that never reach the model: changing them doesn't make old results stale
NOT_IN_PROMPT = ("pass",)


def fingerprint(cfg, system_prompt):
    """What makes two answers comparable: the questions, the exact instructions and the model.
    A result with another fingerprint is redone by `run --rerun` only."""
    m = cfg["model"]
    qs = [{k: v for k, v in q.items() if k not in NOT_IN_PROMPT} for q in cfg["question"]]
    data = {"prompt": system_prompt, "questions": qs, "backend": m["backend"], "model": m["name"],
            "abstract_chars": cfg["prompt"]["abstract_chars"], "missing_abstract": cfg["missing_abstract"]}
    if m["backend"] == "ollama":
        data.update(temperature=m["temperature"], num_ctx=m["num_ctx"], think=m["think"])
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]
