"""Look up missing abstracts in public sources (OpenAlex, Crossref, Semantic Scholar, arXiv).

A reference with a DOI is looked up by DOI; one without, by title. A title match is accepted only
when the titles are nearly the same (after lower-casing and dropping accents and punctuation) and the
years are close, so a wrong paper's abstract is not taken. Every lookup is kept in a cache (JSONL, one
line per reference) with the source, the link and the matched title, so the filled list can be
rebuilt and checked without asking again.

API keys are read from the environment only (OPENALEX_API_KEY, S2_API_KEY), never from the config.
"""
import csv
import difflib
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from . import __version__, readers

SOURCES = ("openalex", "crossref", "semanticscholar", "arxiv")
USER_AGENT = f"criba-ref/{__version__} (https://github.com/gherardovarando/criba-ref)"
DOI_MIN_SIMILARITY = 0.8  # a DOI lookup whose title is this different is probably a wrong DOI
ARXIV_DELAY = 3.0         # arXiv asks for one request every 3 seconds


class FetchError(Exception):
    """A source could not be asked (network, rate limit, server error): try again later."""


# ---------- titles ----------

def norm_title(s):
    s = unicodedata.normalize("NFKD", readers.latex_to_text(str(s or "")))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def similarity(a, b):
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else difflib.SequenceMatcher(None, a, b).ratio()


def strip_markup(s):
    """Crossref abstracts are JATS XML: drop the tags and a leading "Abstract"."""
    s = re.sub(r"<[^>]+>", " ", str(s or ""))
    s = " ".join(s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").split())
    return re.sub(r"^(abstract|summary)[\s.:]+", "", s, flags=re.I)


def from_inverted(index):
    """OpenAlex gives the abstract as {word: [positions]}."""
    if not index:
        return ""
    pos = sorted((p, w) for w, ps in index.items() for p in ps)
    return " ".join(w for _, w in pos)


# ---------- HTTP ----------

class Http:
    """GET with a polite delay per host and a few retries on rate limits and server errors."""

    def __init__(self, delay=1.0, timeout=30, tries=3, sleep=time.sleep):
        self.delay, self.timeout, self.tries, self.sleep = delay, timeout, tries, sleep
        self.last = {}

    def get(self, url, headers=None):
        host = urllib.parse.urlsplit(url).netloc
        wait = ARXIV_DELAY if "arxiv.org" in host else self.delay
        for attempt in range(self.tries):
            gap = time.monotonic() - self.last.get(host, -1e9)
            if gap < wait:
                self.sleep(wait - gap)
            self.last[host] = time.monotonic()
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.status, r.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return 404, ""
                if e.code in (429, 500, 502, 503, 504) and attempt + 1 < self.tries:
                    after = e.headers.get("Retry-After", "")
                    self.sleep(float(after) if after.isdigit() else 2 ** attempt * max(wait, 1))
                    continue
                raise FetchError(f"HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt + 1 < self.tries:
                    self.sleep(2 ** attempt * max(wait, 1))
                    continue
                raise FetchError(str(getattr(e, "reason", e))) from None
        raise FetchError("no answer")


def get_json(http, url, headers=None):
    status, body = http.get(url, headers)
    if status == 404 or not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise FetchError("not JSON") from None


def q(s):
    return urllib.parse.quote(s, safe="")


def query_title(title):
    return norm_title(title)  # punctuation breaks some search syntaxes


# ---------- sources: each returns candidates {title, year, abstract, url, doi} ----------

def openalex(item, http, opts):
    key = os.environ.get("OPENALEX_API_KEY", "")
    extra = f"&api_key={q(key)}" if key else ""
    sel = "select=id,doi,title,publication_year,abstract_inverted_index"
    if item.doi:
        works = [get_json(http, f"https://api.openalex.org/works/doi:{q(item.doi)}?{sel}{extra}")]
    else:
        data = get_json(http, f"https://api.openalex.org/works?search={q(query_title(item.title))}"
                              f"&per-page=5&{sel}{extra}") or {}
        works = data.get("results", [])
    return [{"title": w.get("title") or "", "year": str(w.get("publication_year") or ""),
             "abstract": from_inverted(w.get("abstract_inverted_index")), "url": w.get("id") or "",
             "doi": readers.norm_doi(w.get("doi"))} for w in works if w]


def crossref(item, http, opts):
    mail = f"mailto={q(opts['mailto'])}" if opts.get("mailto") else ""
    if item.doi:
        data = get_json(http, f"https://api.crossref.org/works/{q(item.doi)}" + (f"?{mail}" if mail else ""))
        works = [data.get("message", {})] if data else []
    else:
        data = get_json(http, f"https://api.crossref.org/works?query.bibliographic={q(query_title(item.title))}"
                              f"&rows=5&select=DOI,title,abstract,issued" + (f"&{mail}" if mail else "")) or {}
        works = data.get("message", {}).get("items", [])
    out = []
    for w in works:
        parts = (w.get("issued") or {}).get("date-parts") or [[None]]
        doi = readers.norm_doi(w.get("DOI"))
        out.append({"title": " ".join(w.get("title") or []), "year": str(parts[0][0] or ""),
                    "abstract": strip_markup(w.get("abstract")), "doi": doi,
                    "url": f"https://doi.org/{doi}" if doi else ""})
    return out


def semanticscholar(item, http, opts):
    key = os.environ.get("S2_API_KEY", "")
    head = {"x-api-key": key} if key else None
    f = "fields=title,year,abstract,url,externalIds"
    base = "https://api.semanticscholar.org/graph/v1/paper"
    if item.doi:
        works = [get_json(http, f"{base}/DOI:{q(item.doi)}?{f}", head)]
    else:
        data = get_json(http, f"{base}/search/match?query={q(query_title(item.title))}&{f}", head) or {}
        works = data.get("data", [])
    return [{"title": w.get("title") or "", "year": str(w.get("year") or ""), "abstract": w.get("abstract") or "",
             "url": w.get("url") or "", "doi": readers.norm_doi((w.get("externalIds") or {}).get("DOI"))}
            for w in works if w]


ATOM = {"a": "http://www.w3.org/2005/Atom"}
STOP = {"the", "and", "for", "with", "from", "via", "into", "are", "its", "our", "how", "can", "when", "what",
        "using", "towards", "toward", "between", "under", "over", "without", "through"}


def arxiv(item, http, opts):
    m = re.match(r"10\.48550/arxiv\.(.+)$", item.doi or "")
    if m:
        url = f"https://export.arxiv.org/api/query?id_list={q(m.group(1))}"
    elif item.title:
        words = [w for w in query_title(item.title).split() if len(w) > 2 and w not in STOP][:8]
        if not words:
            return []
        url = ("https://export.arxiv.org/api/query?max_results=5&search_query="
               + q(" AND ".join(f"ti:{w}" for w in words)))
    else:
        return []
    status, body = http.get(url)
    if status == 404 or not body:
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise FetchError("not XML") from None
    out = []
    for e in root.findall("a:entry", ATOM):
        out.append({"title": " ".join((e.findtext("a:title", "", ATOM)).split()),
                    "year": (e.findtext("a:published", "", ATOM) or "")[:4],
                    "abstract": " ".join((e.findtext("a:summary", "", ATOM)).split()),
                    "url": e.findtext("a:id", "", ATOM), "doi": ""})
    return out


FETCH = {"openalex": openalex, "crossref": crossref, "semanticscholar": semanticscholar, "arxiv": arxiv}


# ---------- matching ----------

def year_ok(item_year, cand_year):
    """A preprint can be up to three years older than the paper, a later version a year newer."""
    if not item_year or not cand_year:
        return True
    d = int(cand_year) - int(item_year)
    return -3 <= d <= 1


def pick(item, cands, by_doi, min_sim):
    """The best candidate with an abstract, or None and why not (for the closest title)."""
    best, why, why_sim = None, "no record", -1.0
    for c in cands:
        sim = similarity(item.title, c["title"]) if item.title else 1.0
        if by_doi and c.get("doi") and c["doi"] != item.doi:
            bad = "other DOI"
        elif by_doi and item.title and sim < DOI_MIN_SIMILARITY:
            bad = f"title differs from the DOI record ({sim:.2f})"
        elif not by_doi and sim < min_sim:
            bad = "no close title"
        elif not by_doi and not year_ok(item.year, c["year"]):
            bad = f"same title but year {c['year']}"
        elif not c["abstract"]:
            bad = "record has no abstract"
        else:
            bad = None
        if bad:
            if sim > why_sim:
                why, why_sim = bad, sim
        elif best is None or sim > best[1]:
            best = (c, sim)
    return (best, None) if best else (None, why)


def look_up(item, http, opts):
    """-> a cache record: found or not, from where, and what was tried."""
    rec = {"id": item.id, "title": item.title, "found": False, "tried": [], "errors": [],
           "date": datetime.now().isoformat(timespec="seconds")}
    by_doi = bool(item.doi)
    for name in opts["sources"]:
        if not by_doi and not item.title:
            break
        try:
            cands = FETCH[name](item, http, opts)
        except FetchError as e:
            rec["errors"].append(f"{name}: {e}")
            continue
        best, why = pick(item, cands, by_doi, opts["min_similarity"])
        rec["tried"].append(f"{name}: {why}" if why else name)
        if best:
            c, sim = best
            rec.update(found=True, abstract=c["abstract"], source=name, url=c["url"], matched_title=c["title"],
                       matched_year=c["year"], similarity=round(sim, 3), by="doi" if by_doi else "title")
            break
    return rec


# ---------- cache ----------

def read_cache(path):
    out = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                out[r["id"]] = r  # the last line for an id wins
    return out


def append_cache(path, rec):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def needs_lookup(rec, retry):
    """Not asked yet, or asked with errors (network, rate limit); with retry also the not found."""
    if rec is None:
        return True
    return not rec["found"] and (bool(rec["errors"]) or retry)


def run(items, cache_path, opts, http=None, ids=None, limit=None, retry=False, dry_run=False, log=sys.stderr):
    """Look up the items without an abstract that the cache doesn't answer yet. -> (asked, found)."""
    http = http or Http(opts["delay"], opts["timeout"])
    cache = read_cache(cache_path)
    todo = [it for it in items if not it.abstract and (ids is None or it.id in ids)
            and needs_lookup(cache.get(it.id), retry)]
    if limit is not None:
        todo = todo[:limit]
    found = 0
    for n, it in enumerate(todo, 1):
        if dry_run:
            print(f"would look up {it.id} ({'doi' if it.doi else 'title'}): {it.title[:70]}", file=log)
            continue
        rec = look_up(it, http, opts)
        append_cache(cache_path, rec)
        found += rec["found"]
        what = f"found in {rec['source']}" if rec["found"] else "not found: " + "; ".join(rec["tried"] + rec["errors"])
        print(f"[{n}/{len(todo)}] {it.id}: {what}", file=log)
    return len(todo), found


# ---------- the filled list ----------

EXTRA = ["abstract_source", "abstract_url"]
NORMAL = ["id", "title", "year", "doi", "url", "authors", "keywords", "abstract"] + EXTRA


def column(fieldnames, canon, mapping):
    """The input column that read_items takes `canon` from (None when there is none)."""
    if canon in mapping:
        return mapping[canon]
    lower = {f.strip().lower(): f for f in fieldnames}
    return next((lower[n] for n in readers.CANDIDATES[canon] if n in lower), None)


def fill(input_path, fmt, mapping, cache_path, out_path):
    """Write the list with the abstracts found. A CSV/TSV keeps all its columns (plus abstract_source,
    abstract_url); other formats become a CSV with the fields criba-ref reads. -> (rows, filled)."""
    cache = {k: v for k, v in read_cache(cache_path).items() if v["found"]}
    fmt = readers.detect_format(input_path, fmt)
    mapping = mapping or {}
    buf, filled, rows = io.StringIO(), 0, 0
    if fmt in ("csv", "tsv"):
        delim = "\t" if fmt == "tsv" else ","
        text = Path(input_path).read_text(encoding="utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text), delimiter=delim)
        names = list(reader.fieldnames or [])
        col = column(names, "abstract", mapping) or "abstract"
        header = names + [c for c in [col] + EXTRA if c not in names]
        w = csv.DictWriter(buf, header, delimiter=delim, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for row in reader:
            rows += 1
            it = readers.make_item(readers.map_fields(row, mapping), "")
            rec = cache.get(it.id)
            if not it.abstract and rec:
                row[col], row["abstract_source"], row["abstract_url"] = rec["abstract"], rec["source"], rec["url"]
                filled += 1
            w.writerow(row)
    else:
        items, _ = readers.read_items(input_path, fmt, mapping)
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(NORMAL)
        for it in items:
            rows += 1
            rec = cache.get(it.id) if not it.abstract else None
            filled += bool(rec)
            w.writerow([it.id, it.title, it.year, it.doi, it.url, "; ".join(it.authors), "; ".join(it.keywords),
                        rec["abstract"] if rec else it.abstract, rec["source"] if rec else "",
                        rec["url"] if rec else ""])
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(buf.getvalue(), encoding="utf-8")
    return rows, filled
