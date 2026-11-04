"""Abstract lookup, with canned answers instead of the network."""
import csv
import json

from criba_ref import abstracts as A
from criba_ref import cli, readers

OPTS = {"sources": ["openalex", "crossref", "arxiv"], "min_similarity": 0.95, "mailto": "", "delay": 0, "timeout": 5}

ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><id>http://arxiv.org/abs/2401.00001v1</id><published>2024-01-02T00:00:00Z</published>
    <title>A Made-up Paper on
      Graphs</title><summary>  We study made-up graphs.  </summary></entry>
</feed>"""


class FakeHttp:
    """Answers by the first URL fragment that matches; records the URLs asked."""

    def __init__(self, routes):
        self.routes, self.asked = routes, []

    def get(self, url, headers=None):
        self.asked.append(url)
        for frag, answer in self.routes.items():
            if frag in url:
                if isinstance(answer, Exception):
                    raise answer
                return 200, answer if isinstance(answer, str) else json.dumps(answer)
        return 404, ""


def oa_work(title, year, words, doi=None):
    return {"id": "https://openalex.org/W1", "doi": f"https://doi.org/{doi}" if doi else None, "title": title,
            "publication_year": year, "abstract_inverted_index": {w: [i] for i, w in enumerate(words)}}


def test_text_helpers():
    assert A.norm_title("Caus\\'al {G}raphs: a Review!") == "causal graphs a review"
    assert A.similarity("Causal graphs.", "causal Graphs") == 1.0
    assert A.from_inverted({"b": [1], "a": [0, 2]}) == "a b a"
    assert A.strip_markup("<jats:title>Abstract</jats:title><jats:p>We &amp; they.</jats:p>") == "We & they."
    assert A.year_ok("2025", "2022") and not A.year_ok("2025", "2021") and not A.year_ok("2020", "2022")
    assert A.year_ok("", "1999")


def test_title_match_is_strict():
    it = readers.Item(id="x", title="A made-up paper on graphs", year="2024")
    near = {"title": "A made-up paper on graph", "year": "2024", "abstract": "ok", "url": "", "doi": ""}
    other = {"title": "Review of a made-up paper on graphs and trees", "year": "2024", "abstract": "no", "url": "",
             "doi": ""}
    old = {**near, "title": "A made-up paper on graphs", "year": "2019"}
    best, _ = A.pick(it, [other, near], False, 0.95)
    assert best[0] is near
    assert A.pick(it, [other], False, 0.95) == (None, "no close title")
    assert A.pick(it, [old], False, 0.95) == (None, "same title but year 2019")
    assert A.pick(it, [{**near, "abstract": ""}], False, 0.95) == (None, "record has no abstract")


def test_doi_lookup_checks_the_title():
    it = readers.Item(id="10.1/a", doi="10.1/a", title="A made-up paper on graphs")
    rec = {"title": "Something else entirely", "year": "", "abstract": "x", "url": "", "doi": "10.1/a"}
    assert A.pick(it, [rec], True, 0.95)[1].startswith("title differs")
    assert A.pick(it, [{**rec, "doi": "10.1/b", "title": it.title}], True, 0.95) == (None, "other DOI")


def test_sources_parse():
    it = readers.Item(id="x", title="A Made-up Paper on Graphs", year="2024")
    http = FakeHttp({"api.openalex.org": {"results": [oa_work("A made-up paper on graphs", 2024, ["We", "study"])]},
                     "api.crossref.org": {"message": {"items": [
                         {"DOI": "10.1/A", "title": ["A made-up paper on graphs"], "issued": {"date-parts": [[2024]]},
                          "abstract": "<jats:p>Crossref text.</jats:p>"}]}},
                     "export.arxiv.org": ARXIV_FEED})
    oa = A.openalex(it, http, OPTS)[0]
    assert (oa["abstract"], oa["year"], oa["url"]) == ("We study", "2024", "https://openalex.org/W1")
    cr = A.crossref(it, http, OPTS)[0]
    assert (cr["abstract"], cr["doi"], cr["year"]) == ("Crossref text.", "10.1/a", "2024")
    ax = A.arxiv(it, http, OPTS)[0]
    assert (ax["title"], ax["abstract"], ax["year"]) == ("A Made-up Paper on Graphs", "We study made-up graphs.", "2024")
    assert "ti%3Amade" in http.asked[-1] and "ti%3Aon" not in http.asked[-1]


def test_look_up_order_and_errors():
    it = readers.Item(id="x", title="A made-up paper on graphs", year="2024")
    http = FakeHttp({"api.openalex.org": A.FetchError("HTTP 429"), "api.crossref.org": {"message": {"items": []}},
                     "export.arxiv.org": ARXIV_FEED})
    rec = A.look_up(it, http, OPTS)
    assert rec["found"] and rec["source"] == "arxiv" and rec["by"] == "title"
    assert rec["errors"] == ["openalex: HTTP 429"] and rec["tried"] == ["crossref: no record", "arxiv"]
    assert not A.needs_lookup(rec, retry=True)  # found: never asked again


def test_run_cache_and_fill_csv(tmp_path):
    src = tmp_path / "refs.csv"
    with open(src, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Title", "venue", "Abstract", "year"])
        w.writerow(["A made-up paper on graphs", "UAI", "", "2024"])
        w.writerow(["Has one already", "UAI", "Kept as it is.", "2024"])
        w.writerow(["Nowhere to be found", "CLeaR", "", "2024"])
    items, _ = readers.read_items(src)
    cache = tmp_path / "abstracts.jsonl"
    http = FakeHttp({"api.openalex.org": {"results": [oa_work("A made-up paper on graphs", 2024, ["Found", "it."])]},
                     "api.crossref.org": A.FetchError("timed out")})
    opts = {**OPTS, "sources": ["openalex", "crossref"]}
    assert A.run(items, cache, opts, http=http, log=open("/dev/null", "w")) == (2, 1)
    # again: the found one is not asked, the one with an error is
    http2 = FakeHttp({"api.crossref.org": {"message": {"items": []}}})
    assert A.run(items, cache, opts, http=http2, log=open("/dev/null", "w")) == (1, 0)
    assert all("Nowhere" in u or "nowhere" in u for u in http2.asked)
    # now tried everywhere without errors: asked again only with retry
    assert A.run(items, cache, opts, http=http2, log=open("/dev/null", "w"))[0] == 0
    assert A.run(items, cache, opts, http=http2, retry=True, log=open("/dev/null", "w"))[0] == 1

    out = tmp_path / "filled.csv"
    assert A.fill(src, "auto", {}, cache, out) == (3, 1)
    rows = list(csv.DictReader(open(out)))
    assert list(rows[0]) == ["Title", "venue", "Abstract", "year", "abstract_source", "abstract_url"]
    assert rows[0]["Abstract"] == "Found it." and rows[0]["abstract_source"] == "openalex"
    assert rows[1]["Abstract"] == "Kept as it is." and rows[1]["abstract_source"] == ""
    assert rows[2]["Abstract"] == ""
    again, _ = readers.read_items(out)
    assert [it.id for it in again] == [it.id for it in items]  # same ids: results still match


def test_fill_other_formats(tmp_path):
    src = tmp_path / "refs.ris"
    src.write_text("TY  - JOUR\nTI  - A made-up paper on graphs\nPY  - 2024\nDO  - 10.1/abc\nAU  - Doe, J\nER  - \n")
    items, _ = readers.read_items(src)
    cache = tmp_path / "c.jsonl"
    A.append_cache(cache, {"id": "10.1/abc", "found": True, "abstract": "From the cache.", "source": "crossref",
                           "url": "https://doi.org/10.1/abc", "errors": [], "tried": ["crossref"]})
    out = tmp_path / "filled.csv"
    assert A.fill(src, "auto", {}, cache, out) == (1, 1)
    again, _ = readers.read_items(out)
    assert again[0].id == items[0].id and again[0].abstract == "From the cache." and again[0].authors == ["Doe, J"]


def test_cli_abstracts(tmp_path, capsys, monkeypatch):
    (tmp_path / "refs.csv").write_text("title,abstract,year\nA made-up paper on graphs,,2024\n")
    (tmp_path / "criba-ref.toml").write_text('[input]\nfile = "refs.csv"\n[abstracts]\nsources = ["openalex"]\n'
                                             '[[question]]\nid = "d"\nask = "?"\nlabels = ["a", "b"]\n')
    monkeypatch.setattr(A, "Http", lambda *a, **k: FakeHttp(
        {"api.openalex.org": {"results": [oa_work("A made-up paper on graphs", 2024, ["Hi."])]}}))
    cfg = str(tmp_path)
    cli.main(["abstracts", "-c", cfg, "--dry-run"])
    assert not (tmp_path / "abstracts.jsonl").exists()
    cli.main(["abstracts", "-c", cfg, "-f", str(tmp_path / "filled.csv")])
    assert "Hi." in (tmp_path / "filled.csv").read_text()
    for bad in (["-f", str(tmp_path / "filled.csv")], ["-f", str(tmp_path / "refs.csv")], ["--sources", "google"]):
        try:
            cli.main(["abstracts", "-c", cfg, *bad])
            raise AssertionError(bad)
        except SystemExit as e:
            assert "error" in str(e.code)
