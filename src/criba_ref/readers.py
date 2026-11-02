"""Read a list of references (CSV, TSV, BibTeX, RIS, JSON, JSONL) into Items."""
import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

FORMATS = {".csv": "csv", ".tsv": "tsv", ".bib": "bib", ".ris": "ris", ".json": "json",
           ".jsonl": "jsonl", ".ndjson": "jsonl"}

# Field names tried, case-insensitively, when [input.fields] doesn't map a field (CSV and JSON only).
CANDIDATES = {
    "title": ["title", "article title", "ti", "t1", "primary title"],
    "abstract": ["abstract", "ab", "n2", "summary"],
    "doi": ["doi", "di", "do"],
    "year": ["year", "publication year", "py", "pubyear", "date"],
    "id": ["id", "key", "pmid", "eid", "ut (unique wos id)", "accession number"],
    "authors": ["authors", "author", "au", "author full names"],
    "keywords": ["keywords", "author keywords", "kw", "de"],
    "url": ["url", "link", "ur"],
}


@dataclass
class Item:
    id: str
    title: str = ""
    abstract: str = ""
    year: str = ""
    doi: str = ""
    authors: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    source: str = ""
    url: str = ""

    def meta(self):
        """What goes into the result file about the item (no abstract: the input file has it)."""
        return {"title": self.title, "year": self.year, "doi": self.doi, "url": self.url,
                "authors": self.authors[:3] + (["et al."] if len(self.authors) > 3 else []),
                "source": self.source}


class InputError(Exception):
    pass


def detect_format(path, fmt="auto"):
    if fmt and fmt != "auto":
        if fmt not in set(FORMATS.values()):
            raise InputError(f"unknown input format {fmt!r} (use one of {', '.join(sorted(set(FORMATS.values())))})")
        return fmt
    ext = Path(path).suffix.lower()
    if ext not in FORMATS:
        raise InputError(f"can't tell the format of {path} from its extension: set input.format")
    return FORMATS[ext]


def clean_text(s):
    return " ".join(str(s or "").split())


def norm_doi(s):
    s = clean_text(s).lower()
    s = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", s)
    return s if s.startswith("10.") else ""


def year_of(s):
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(s or ""))
    return m.group(1) if m else ""


def split_list(v, sep=r"\s*;\s*|\s+and\s+"):
    if isinstance(v, list):
        return [clean_text(x) for x in v if clean_text(x)]
    return [x for x in (clean_text(p) for p in re.split(sep, str(v or ""))) if x]


def hashed_id(title, year):
    key = re.sub(r"\W+", " ", title.lower()).strip() + "|" + year
    return "h-" + hashlib.sha1(key.encode()).hexdigest()[:10]


def make_item(rec, source):
    """rec: dict with canonical keys (title, abstract, doi, year, id, authors, keywords)."""
    title = clean_text(rec.get("title"))
    year = year_of(rec.get("year"))
    doi = norm_doi(rec.get("doi"))
    own = clean_text(rec.get("id"))
    return Item(id=doi or own or hashed_id(title, year), title=title, abstract=clean_text(rec.get("abstract")),
                year=year, doi=doi, authors=split_list(rec.get("authors")),
                keywords=split_list(rec.get("keywords"), r"\s*[;,]\s*"), source=source,
                url=clean_text(rec.get("url")) or (f"https://doi.org/{doi}" if doi else ""))


def map_fields(row, mapping):
    """A CSV/JSON row -> canonical keys, using [input.fields] first, then CANDIDATES."""
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    out = {}
    for canon, names in CANDIDATES.items():
        if canon in mapping:
            out[canon] = row.get(mapping[canon], lower.get(str(mapping[canon]).lower()))
            continue
        for n in names:
            if lower.get(n) not in (None, ""):
                out[canon] = lower[n]
                break
    return out


# ---------- RIS ----------

RIS_LINE = re.compile(r"^([A-Z][A-Z0-9])  -(?: (.*))?$")
RIS_MAP = {"TI": "title", "T1": "title", "AB": "abstract", "N2": "abstract", "DO": "doi", "PY": "year",
           "Y1": "year", "DA": "year", "ID": "id", "AN": "id", "AU": "authors", "A1": "authors", "KW": "keywords", "UR": "url"}
RIS_LISTS = {"authors", "keywords"}


def parse_ris(text):
    recs, cur, last = [], None, None
    for line in text.lstrip("\ufeff").splitlines():
        m = RIS_LINE.match(line.rstrip())
        if m:
            tag, val = m.group(1), (m.group(2) or "").strip()
            if tag == "TY":
                cur, last = {"authors": [], "keywords": []}, None
            elif tag == "ER":
                if cur is not None:
                    recs.append(cur)
                cur, last = None, None
            elif cur is not None and tag in RIS_MAP:
                key = RIS_MAP[tag]
                if key in RIS_LISTS:
                    cur[key].append(val)
                elif not cur.get(key):  # first wins (TI before T1, AB before N2)
                    cur[key] = val
                last = key
            else:
                last = None
        elif cur is not None and last and line.strip() and last not in RIS_LISTS:
            cur[last] = cur[last] + " " + line.strip()  # continuation line
    if cur is not None:  # file without a final ER
        recs.append(cur)
    return recs


# ---------- BibTeX ----------

ACCENT = re.compile(r"\\[`'^\"~=.uvHcdbtrk]\s*\{?\\?([A-Za-z])\}?")


def latex_to_text(s):
    s = ACCENT.sub(r"\1", s)
    s = re.sub(r"\\(textit|textbf|emph|mathrm|mathit|textrm|texttt)\s*", "", s)
    s = s.replace("\\&", "&").replace("\\%", "%").replace("\\_", "_").replace("~", " ").replace("--", "–")
    return clean_text(s.replace("{", "").replace("}", ""))


def _braced(text, i):
    """text[i] is '{': return (content, index after the matching '}')."""
    depth, j = 0, i
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    raise InputError("unbalanced braces in BibTeX entry")


def _quoted(text, i):
    """text[i] is '"': quotes inside braces don't end the value."""
    depth, j = 0, i + 1
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == '"' and depth == 0:
            return text[i + 1:j], j + 1
        j += 1
    raise InputError("unterminated quoted value in BibTeX entry")


def _entry_fields(body):
    """body: what is between the entry's outer braces, after 'key,'."""
    fields, i, n = {}, 0, len(body)
    name_re = re.compile(r"\s*([A-Za-z][\w:.-]*)\s*=\s*")
    while i < n:
        m = name_re.match(body, i)
        if not m:
            nxt = body.find(",", i)
            if nxt < 0:
                break
            i = nxt + 1
            continue
        name, i = m.group(1).lower(), m.end()
        parts = []
        while i < n:  # value, maybe concatenated with #
            c = body[i]
            if c == "{":
                v, i = _braced(body, i)
            elif c == '"':
                v, i = _quoted(body, i)
            else:
                mm = re.match(r"[^,#}\s]+", body[i:])
                v = mm.group(0) if mm else ""
                i += len(v)
            parts.append(v)
            mm = re.match(r"\s*#\s*", body[i:])
            if not mm:
                break
            i += mm.end()
        fields[name] = "".join(parts)
        mm = re.match(r"\s*,?", body[i:])
        i += mm.end()
    return fields


def parse_bib(text):
    recs = []
    for m in re.finditer(r"@\s*(\w+)\s*([{(])", text):
        kind = m.group(1).lower()
        if kind in ("comment", "string", "preamble"):
            continue
        start = m.end() - 1
        if m.group(2) == "{":
            inner, _ = _braced(text, start)
        else:
            end = text.find(")\n", start)
            inner = text[start + 1:end if end > 0 else len(text)]
        key, _, body = inner.partition(",")
        f = _entry_fields(body)
        recs.append({"id": key.strip(), "title": latex_to_text(f.get("title", "")),
                     "abstract": latex_to_text(f.get("abstract", "")), "doi": f.get("doi", ""),
                     "year": f.get("year") or f.get("date", ""),
                     "authors": [latex_to_text(a) for a in re.split(r"\s+and\s+", f.get("author", "")) if a.strip()],
                     "keywords": latex_to_text(f.get("keywords", "")), "url": f.get("url", "")})
    return recs


# ---------- entry point ----------

def read_items(path, fmt="auto", mapping=None):
    """-> (items, problems). Duplicate ids keep the first item; problems lists what was dropped."""
    path = Path(path)
    if not path.exists():
        raise InputError(f"input file not found: {path}")
    fmt = detect_format(path, fmt)
    mapping = mapping or {}
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if fmt in ("csv", "tsv"):
        rows = csv.DictReader(text.splitlines(), delimiter="\t" if fmt == "tsv" else ",")
        recs = [map_fields(r, mapping) for r in rows]
    elif fmt == "json":
        data = json.loads(text)
        if isinstance(data, dict):  # {"items": [...]} or {"references": [...]}
            data = next((v for v in data.values() if isinstance(v, list)), [])
        recs = [map_fields(r, mapping) for r in data if isinstance(r, dict)]
    elif fmt == "jsonl":
        recs = [map_fields(json.loads(l), mapping) for l in text.splitlines() if l.strip()]
    elif fmt == "ris":
        recs = parse_ris(text)
    else:
        recs = parse_bib(text)
    items, seen, problems = [], set(), []
    for n, r in enumerate(recs, 1):
        it = make_item(r, path.name)
        if not it.title and not it.abstract:
            problems.append(f"record {n}: no title and no abstract, skipped")
            continue
        if it.id in seen:
            problems.append(f"record {n}: duplicate id {it.id}, skipped")
            continue
        seen.add(it.id)
        items.append(it)
    return items, problems


def file_name(item_id):
    """A safe file name for an item id. DOIs contain slashes: an id that had to be changed gets a
    short hash, so two ids never share a file."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", item_id).strip("._")[:120]
    if safe != item_id:
        safe += "-" + hashlib.sha1(item_id.encode()).hexdigest()[:6]
    return safe + ".json"
