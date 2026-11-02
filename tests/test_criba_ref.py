import json
from pathlib import Path

import pytest

from criba_ref import cli, config, export, prompt, readers, screen

FIX = Path(__file__).parent / "fixtures"

CONFIG = """
[input]
file = "{input}"

[output]
dir = "results"

[model]
backend = "fake"
name = "fake"

[prompt]
context = "Research question: made up."

[[question]]
id = "decision"
ask = "Include it?"
labels = ["include", "exclude", "maybe"]
fallback = "maybe"
examples = ["Something -> include"]

[[question]]
id = "topics"
ask = "Topics?"
labels = ["a", "b"]
multiple = true

[[question]]
id = "data"
ask = "Data?"
type = "text"

[missing_abstract]
answer = {{ decision = "maybe" }}
"""


def project(tmp_path, input_file="refs.ris", extra=""):
    (tmp_path / "criba-ref.toml").write_text(CONFIG.format(input=FIX / input_file) + extra)
    return config.load(tmp_path)


# ---------- readers ----------

def test_ris():
    items, problems = readers.read_items(FIX / "refs.ris")
    assert len(items) == 2 and len(problems) == 1 and "duplicate" in problems[0]
    a, b = items
    assert a.id == a.doi == "10.5555/example.1"
    assert a.abstract == "We propose a made-up method. The abstract goes on over a second line."
    assert a.authors == ["Doe, Jane", "Roe, Richard"] and a.keywords == ["causal discovery", "graphs"]
    assert a.year == "2021"
    assert b.id == "rec2" and b.year == "2019" and b.abstract == ""


def test_bib():
    items, _ = readers.read_items(FIX / "refs.bib")
    assert [i.id for i in items] == ["10.5555/example.2", "roe2020"]
    a = items[0]
    assert a.title == "A Made-up Study of Etudes & Graphs"
    assert a.abstract == 'We test nested braces, a "quote" and Muller.'
    assert a.authors == ["Doe, Jane", "Roe, Richard"] and a.year == "2021"
    assert a.keywords == ["causal discovery", "graphs"]


def test_csv_json_jsonl():
    items, _ = readers.read_items(FIX / "refs.csv")
    assert items[0].title == "A made-up CSV entry" and items[0].abstract == "An abstract, with a comma."
    assert items[0].authors == ["Doe J", "Roe R"] and items[0].year == "2022"
    assert items[1].id.startswith("h-") and items[1].abstract == ""
    items, _ = readers.read_items(FIX / "refs.jsonl")
    assert [i.id for i in items] == ["j1", "10.5555/example.4"] and items[1].abstract == "Other text."
    items, _ = readers.read_items(FIX / "refs.json")
    assert items[0].id == "123" and items[0].title == "A made-up JSON entry"


def test_field_mapping(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("Name,Text\nT1,A1\n")
    items, _ = readers.read_items(f, mapping={"title": "Name", "abstract": "Text"})
    assert items[0].title == "T1" and items[0].abstract == "A1"


def test_file_name_unique():
    assert readers.file_name("rec2") == "rec2.json"
    a, b = readers.file_name("10.1/a_b"), readers.file_name("10.1/a/b")
    assert a != b and "/" not in a


def test_hashed_id_stable():
    assert readers.hashed_id("A Title!", "2020") == readers.hashed_id("a  title", "2020")


# ---------- config ----------

def test_config_errors(tmp_path):
    with pytest.raises(config.ConfigError, match="fallback"):
        project(tmp_path, extra='\n[[question]]\nid = "x"\nask = "?"\nlabels = ["a"]\nfallback = "b"\n')
    with pytest.raises(config.ConfigError, match="twice"):
        project(tmp_path, extra='\n[[question]]\nid = "data"\nask = "?"\ntype = "text"\n')


# ---------- prompt ----------

def test_prompt_and_schema(tmp_path):
    cfg = project(tmp_path)
    s = prompt.system_prompt(cfg)
    assert "Research question: made up." in s and "{questions}" not in s
    assert '- decision: Include it? Answer in "label": exactly one of "include", "exclude", "maybe".' in s
    assert '"labels": a list of zero or more of "a", "b"' in s and "  - Something -> include" in s
    sch = prompt.schema(cfg["question"])
    assert sch["required"] == ["decision", "topics", "data"]
    assert list(sch["properties"]["decision"]["properties"]) == ["basis", "label"]
    assert sch["properties"]["topics"]["properties"]["labels"]["items"]["enum"] == ["a", "b"]


def test_prompt_file(tmp_path):
    (tmp_path / "prompt.md").write_text("<!-- note -->\nMine. {context}\n{questions}\n")
    cfg = project(tmp_path, extra='')
    cfg["prompt"]["file"] = str(tmp_path / "prompt.md")
    s = prompt.system_prompt(cfg)
    assert s.startswith("Mine. Research question: made up.") and "note" not in s


# ---------- clean ----------

def test_clean(tmp_path):
    qs = project(tmp_path)["question"]
    out, problems = screen.clean({"decision": {"basis": " why \n", "label": "yes"},
                                  "topics": {"basis": "", "labels": ["b", "z", "a", "b"]},
                                  "data": {"basis": "", "text": "  survey  data "}}, qs)
    assert out["decision"] == {"label": "maybe", "reason": "why"}
    assert out["topics"]["labels"] == ["a", "b"] and out["data"]["text"] == "survey data"
    assert len(problems) == 2


# ---------- run, resume, export ----------

def test_run_resume_export(tmp_path, capsys):
    cfg = project(tmp_path)
    items, _ = readers.read_items(cfg["input"]["file"])
    st = screen.run(cfg, items)
    assert st.counts == {"ok": 1, "no_abstract": 1}
    r = json.loads((tmp_path / "results" / readers.file_name("10.5555/example.1")).read_text())
    assert r["answers"]["decision"] == {"label": "include", "reason": "fake back-end"}
    assert r["model"]["used"] == "fake" and r["item"]["title"].startswith("A made-up")
    nr = json.loads((tmp_path / "results" / "rec2.json").read_text())
    assert nr["status"] == "no_abstract" and nr["answers"]["decision"]["label"] == "maybe"
    assert nr["answers"]["topics"]["labels"] == []
    # again: nothing to do
    st = screen.run(cfg, items)
    assert st.skipped == 2 and st.done == 0
    # another question: results are stale, redone only with rerun
    cfg["question"][0]["ask"] = "Include it now?"
    st = screen.run(cfg, items)
    assert st.stale == 2 and st.done == 0
    st = screen.run(cfg, items, rerun=True, limit=1)
    assert st.done == 1
    rows = export.rows(cfg, screen.results(cfg, items))
    assert rows[0][:7] == ["id", "title", "year", "doi", "status", "decision", "decision_reason"]
    assert rows[1][5] == "include" and rows[1][7] == "a"
    assert "decision: include 1, maybe 1" in export.stats(cfg, screen.results(cfg, items))


def test_errors_are_retried(tmp_path, monkeypatch):
    cfg = project(tmp_path)
    items, _ = readers.read_items(cfg["input"]["file"])

    def broken(*a):
        raise screen.backends.BackendError("down")
    monkeypatch.setitem(screen.backends.BACKENDS, "fake", broken)
    st = screen.run(cfg, items)
    assert st.counts == {"error": 1, "no_abstract": 1}
    monkeypatch.undo()
    st = screen.run(cfg, items)
    assert st.counts == {"ok": 1} and st.skipped == 1


def test_cli(tmp_path, capsys):
    project(tmp_path)
    cli.main(["check", "-c", str(tmp_path)])
    out = capsys.readouterr().out
    assert "2 items, 1 without abstract" in out and "answers in the right format" in out
    cli.main(["run", "-c", str(tmp_path)])
    cli.main(["export", "-c", str(tmp_path)])
    assert capsys.readouterr().out.count("\n") == 3
    cli.main(["init", str(tmp_path / "new")])
    cfg = config.load(tmp_path / "new")  # the example config is valid
    assert cfg["question"][0]["id"] == "decision"
