import csv
import json
import shutil
import subprocess
from html.parser import HTMLParser

import pytest

from criba_ref import cli, compare, config, readers, report, screen, sheet
from test_criba_ref import FIX, project


# ---------- metrics against known values ----------

def test_cohen_kappa():
    assert compare.cohen_kappa([[20, 5], [10, 15]]) == pytest.approx(0.4)  # textbook yes/no example
    assert compare.cohen_kappa([[5, 0], [0, 0]]) is None                   # one label only: undefined
    assert compare.cohen_kappa([[0, 0], [0, 0]]) is None


def test_fleiss_kappa():
    # Fleiss (1971): 10 items, 14 raters, 5 categories -> 0.210
    t = [[0, 0, 0, 0, 14], [0, 2, 6, 4, 2], [0, 0, 3, 5, 6], [0, 3, 9, 2, 0], [2, 2, 8, 1, 1], [7, 7, 0, 0, 0],
         [3, 2, 6, 3, 0], [2, 5, 3, 2, 2], [6, 5, 2, 1, 0], [0, 2, 2, 3, 7]]
    assert compare.fleiss_kappa(t) == pytest.approx(0.210, abs=5e-4)


def test_wilson():
    lo, hi = compare.wilson(8, 10)
    assert (round(lo, 3), round(hi, 3)) == (0.49, 0.943)
    assert compare.wilson(0, 0) is None


def test_screening_and_wss():
    pairs = [("i", "i")] * 9 + [("i", "e")] + [("e", "i")] * 10 + [("e", "e")] * 80
    s = compare.screening(pairs, {"i"})
    assert (s["tp"], s["fn"], s["fp"], s["tn"]) == (9, 1, 10, 80)
    assert s["recall"]["value"] == 0.9 and s["precision"]["value"] == pytest.approx(9 / 19)
    assert s["wss"] == pytest.approx(0.81 - 0.1)  # (TN + FN) / N - (1 - recall)
    assert s["share_pass"] == 0.19


def test_bootstrap_is_seeded():
    pairs = [("a", "a")] * 10 + [("a", "b"), ("b", "b"), ("b", "a")] * 3
    one = compare.bootstrap_kappa(pairs, ["a", "b"], 200, seed=3)
    assert one == compare.bootstrap_kappa(pairs, ["a", "b"], 200, seed=3)
    assert one[0] <= compare.cohen_kappa(compare.confusion(pairs, ["a", "b"])) <= one[1]


def test_rounding_half_up():
    assert report.f2(0.975) == "0.98" and report.f2(0.925) == "0.93" and report.pct(0.975) == "98%"


# ---------- sources ----------

def test_parse_spec():
    assert compare.parse_spec("results/haiku") == ("haiku", "results/haiku")
    assert compare.parse_spec("results/haiku/") == ("haiku", "results/haiku/")
    assert compare.parse_spec("expert=sheets/a.csv") == ("expert", "sheets/a.csv")
    assert compare.parse_spec("sheets/expert.csv") == ("expert", "sheets/expert.csv")


def test_csv_labels(tmp_path):
    qs = project(tmp_path)["question"]
    f = tmp_path / "expert.csv"
    f.write_text("ID,Decision [include|exclude|maybe],topics [a|b] (several),data (free text)\n"
                 "10.5555/EXAMPLE.1, Include ,A; b,survey\n"     # DOI in upper case, label with spaces
                 "rec2,,none,\n"                                    # not labelled; explicit no tags
                 "x3,perhaps,a;z,\n")                               # unknown label, unknown tag
    s = compare.load_csv("expert", f, qs)
    assert s.answers["10.5555/example.1"] == {"decision": "include", "topics": ("a", "b"), "data": "survey"}
    assert s.answers["rec2"] == {"topics": ()}
    assert s.answers["x3"] == {"topics": ("a",)}
    assert len(s.warnings) == 2


def test_mixed_configs_warn(tmp_path):
    cfg = project(tmp_path)
    items, _ = readers.read_items(cfg["input"]["file"])
    screen.run(cfg, items)
    p = tmp_path / "results" / "rec2.json"
    r = json.loads(p.read_text())
    r["fingerprint"] = "other"
    p.write_text(json.dumps(r))
    s = compare.load_results("m", tmp_path / "results", cfg["question"])
    assert any("different configs" in w for w in s.warnings)


def test_pass_labels_and_fingerprint(tmp_path):
    cfg = project(tmp_path)
    q = cfg["question"][0]
    assert config.pass_labels(q) == ["include", "maybe"]
    assert config.pass_labels(cfg["question"][1]) is None  # tags
    fp = config.fingerprint(cfg, "x")
    q["pass"] = ["include"]
    assert config.pass_labels(q) == ["include"]
    assert config.fingerprint(cfg, "x") == fp  # pass never reaches the model
    with pytest.raises(config.ConfigError, match="pass"):
        project(tmp_path, extra='\n[[question]]\nid = "x"\nask = "?"\nlabels = ["a"]\npass = ["b"]\n')


# ---------- end to end ----------

def fake_project(tmp_path):
    """Two "models" (the fake back-end, and a copy with one answer changed) and an expert CSV."""
    cfg = project(tmp_path)
    items, _ = readers.read_items(cfg["input"]["file"])
    screen.run(cfg, items)
    shutil.copytree(tmp_path / "results", tmp_path / "other_model")
    p = tmp_path / "other_model" / readers.file_name("10.5555/example.1")
    r = json.loads(p.read_text())
    r["answers"]["decision"] = {"label": "exclude", "reason": "changed"}
    r["answers"]["topics"] = {"labels": [], "reason": ""}
    p.write_text(json.dumps(r))
    (tmp_path / "expert.csv").write_text("id,decision,topics\n10.5555/example.1,include,a\nrec2,exclude,none\n")
    return cfg


def test_build(tmp_path):
    cfg = fake_project(tmp_path)
    rep = compare.build(cfg, [str(tmp_path / "results"), str(tmp_path / "other_model"),
                              f"expert={tmp_path / 'expert.csv'}"], reference="expert", reps=50)
    dec = next(q for q in rep["questions"] if q["id"] == "decision")
    first = dec["pairs"][0]
    assert (first["a"], first["b"], first["n"], first["agree"]) == ("results", "other_model", 2, 1)
    assert [p["a"] for p in dec["pairs"][1:]] == ["expert", "expert"]  # the reference is always the rows
    assert dec["fleiss"]["n"] == 2
    by = {s["source"]: s for s in dec["screening"]}
    # expert: include, exclude. results: include, maybe (no abstract -> maybe). other_model: exclude, maybe.
    assert (by["results"]["tp"], by["results"]["fp"], by["results"]["fn"]) == (1, 1, 0)
    assert by["other_model"]["missed"] == ["10.5555/example.1"]
    tags = next(q for q in rep["questions"] if q["id"] == "topics")
    assert tags["counts"]["expert"] == {"a": 1, "b": 0} and tags["used"] == ["a"]
    assert {d["question"] for d in rep["disagreements"]} == {"decision", "topics"}
    assert rep["titles"]["10.5555/example.1"][1] == "https://doi.org/10.5555/example.1"
    txt = report.text(rep)
    assert "screening vs expert (pass = include, maybe)" in txt and "other_model missed 1" in txt


class Checker(HTMLParser):
    VOID = {"meta", "br", "img", "input", "link"}

    def __init__(self):
        super().__init__()
        self.stack, self.bad = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.bad.append(tag)


def test_write_all_formats(tmp_path):
    cfg = fake_project(tmp_path)
    rep = compare.build(cfg, [str(tmp_path / "results"), f"other_model={tmp_path / 'other_model'}",
                              f"expert={tmp_path / 'expert.csv'}"], reference="expert", reps=20)
    files = report.write(rep, tmp_path / "out")
    names = {str(p.relative_to(tmp_path / "out")) for p in (tmp_path / "out").rglob("*") if p.is_file()}
    assert {"report.html", "report.md", "csv/metrics.csv", "csv/confusion.csv", "csv/disagreements.csv",
            "latex/report.tex", "latex/agreement-decision.tex", "latex/screening-decision.tex",
            "latex/tags-topics.tex", "figures/confusion-decision-expert-vs-results.svg"} <= names
    assert len(files) == len(names)
    c = Checker()
    c.feed((tmp_path / "out" / "report.html").read_text())
    assert not c.stack and not c.bad
    tex = (tmp_path / "out" / "latex" / "agreement-decision.tex").read_text()
    assert r"other\_model" in tex and "other_model" not in tex.replace(r"other\_model", "")
    rows = list(csv.DictReader(open(tmp_path / "out" / "csv" / "metrics.csv")))
    assert any(r["metric"] == "recall" and r["source_b"] == "results" and r["value"] == "1.0" for r in rows)
    if shutil.which("pdflatex"):
        r = subprocess.run(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "report.tex"],
                           cwd=tmp_path / "out" / "latex", capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout[-2000:]


def test_latex_escape():
    assert report.tex("a_b & 50% #1 $x$ {y}") == r"a\_b \& 50\% \#1 \$x\$ \{y\}"


# ---------- sheet ----------

def test_sheet(tmp_path):
    cfg = project(tmp_path, "refs.bib")
    items, _ = readers.read_items(cfg["input"]["file"])
    chosen, _ = sheet.choose(items, sample=1, seed=5)
    assert chosen == sheet.choose(items, sample=1, seed=5)[0]
    out = tmp_path / "expert.csv"
    side = sheet.write(out, chosen, cfg["question"], {"selection": "test"})
    rows = list(csv.reader(open(out)))
    assert rows[0] == ["id", "title", "year", "doi", "url", "abstract", "decision [include|exclude|maybe]",
                       "topics [a|b] (several, separated by ;, or none)", "data (free text)", "comment"]
    assert rows[1][6:] == ["", "", "", ""]  # blinded: no answers
    assert json.loads(side.read_text())["ids"] == [chosen[0].id]
    with pytest.raises(FileExistsError):
        sheet.write(out, chosen, cfg["question"], {})
    # filled in, it reads back with compare
    rows[1][6], rows[1][7] = "exclude", "b"
    with open(out, "w", newline="") as fh:
        csv.writer(fh).writerows(rows)
    s = compare.load_csv("expert", out, cfg["question"])
    assert s.answers[chosen[0].id] == {"decision": "exclude", "topics": ("b",)}


def test_cli_sheet_and_compare(tmp_path, capsys):
    fake_project(tmp_path)
    cli.main(["sheet", "-c", str(tmp_path), "-f", str(tmp_path / "s.csv"), "--sample", "1", "--questions", "decision"])
    assert open(tmp_path / "s.csv").readline().strip() == "id,title,year,doi,url,abstract,decision [include|exclude|maybe],comment"
    cli.main(["compare", "-c", str(tmp_path), str(tmp_path / "results"), str(tmp_path / "other_model"),
              "--bootstrap", "0", "--questions", "decision"])
    out = capsys.readouterr().out
    assert "results vs other_model" in out and "topics" not in out


def test_urls():
    items, _ = readers.read_items(FIX / "refs.ris")
    assert items[0].url == "https://doi.org/10.5555/example.1" and items[1].url == ""
