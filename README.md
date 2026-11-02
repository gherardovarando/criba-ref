# criba-ref

A first screening of a list of references with a language model. You give it a list of titles and abstracts (CSV, BibTeX, RIS or JSON), your research question with its criteria, and a few questions with the answers allowed. It asks the model about each reference and writes one JSON file per reference with the answers, each with a short reason. Then it exports everything as one CSV, one row per reference.

The model can be a local one, through [Ollama](https://ollama.com), or Claude, through the `claude` command of [Claude Code](https://claude.com/claude-code) and your own Claude login.

It is meant for a first pass. The answers are a model's reading of the abstract: check them, at least the `include` and `maybe` ones, before they go into a study.

## What it does

- **Questions are the core.** Each `[[question]]` in the config is one thing to decide about a reference:
  - one label from a list (e.g. `include`, `exclude`, `maybe`);
  - zero or more labels from a list (tags, e.g. the topics or the methods);
  - a short free answer (e.g. "what data does the study use?").

  Every answer comes with a reason based on the title and abstract. "Include or not" is just one question, so you can also use it only to tag a list, or to ask several decisions at once.
- **One JSON file per reference**, in `output.dir`, with the answers, the status, the model that answered, the date and a fingerprint of the instructions.
- **Runs resume.** A reference that already has a result made with the same instructions and model is skipped, so you can stop a run (Ctrl-C) and start it again. A result with an error is tried again. If you change the prompt, the questions or the model, the old results are kept and reported as made "with another config": `run --rerun` redoes them. To compare two models, give each its own folder with `-o`.
- **No abstract, no model call.** A reference without an abstract gets the answer in `[missing_abstract]` (by default `maybe` for the decision) and the status `no_abstract`.
- **The model only answers.** It gets the instructions and the text of one reference, and must reply with JSON that fits a schema built from the questions. It has no tools. An answer outside the allowed labels becomes the question's `fallback` (or empty) and the status `invalid_answer`.

## Install

```sh
git clone <this repository> criba-ref
cd criba-ref
python3 -m venv .venv
.venv/bin/pip install -e .
ln -s "$PWD/.venv/bin/criba-ref" ~/.local/bin/criba-ref   # optional: put it on your PATH
```

Python 3.11 or later. No other Python packages are needed.

For a local model: install Ollama and pull a model, e.g. `ollama pull qwen3:4b-instruct` (2.5 GB; on a 4-core CPU without a GPU, about a minute per reference).
For Claude: install Claude Code and log in once (`claude`).

## Use

```sh
mkdir my-review && cd my-review
criba-ref init                      # writes criba-ref.toml to edit
# put your list in, e.g. references.ris, and edit criba-ref.toml
criba-ref check                     # reads the list, tests the model with a made-up reference
criba-ref prompt --item first       # the exact instructions and text the model gets
criba-ref run -n 10                 # screen 10 references to try the questions
criba-ref stats                     # counts per status and per label
criba-ref run                       # the rest
criba-ref export -f decisions.csv   # one row per reference
```

Other options:

```sh
criba-ref run --backend claude-cli --model sonnet -o results-sonnet   # another model, its own folder
criba-ref run --ids 10.1214/09-aos685 --rerun   # redo some references
criba-ref run --dry-run                         # what would be screened
criba-ref check -i other-list.csv               # another input file
criba-ref prompt --schema                       # the JSON schema of the answer
criba-ref prompt copy                           # copy the built-in instructions to prompt.md to edit
```

`-c DIR` (or `-c FILE`) points to the project config when you are not in its folder. Command-line options override the config file.

## The config

`criba-ref init` writes a commented example. The parts:

```toml
[input]
file = "references.ris"      # .csv .tsv .bib .ris .json .jsonl
# CSV/JSON: say which column is what, when the names aren't the usual ones
# fields = { title = "Article Title", abstract = "Abstract", doi = "DOI" }

[output]
dir = "results"

[model]
backend = "ollama"           # or "claude-cli"
name = "qwen3:4b-instruct"   # claude-cli: "haiku", "sonnet", "opus"
timeout = 300

[prompt]
context = """
Research question: ...
Include: ...
Exclude: ...
"""

[[question]]
id = "decision"
ask = "Should the reference go on to full-text screening, given the criteria?"
labels = ["include", "exclude", "maybe"]
fallback = "maybe"
notes = "Use maybe when the abstract doesn't say enough to apply a criterion."
examples = ["An abstract that ... -> exclude"]

[[question]]
id = "methods"
ask = "Which kinds of methods does the study use?"
labels = ["causal-discovery", "regression", "other"]
multiple = true

[[question]]
id = "data"
ask = "What data does the study use? At most one sentence."
type = "text"

[missing_abstract]
skip_model = true
answer = { decision = "maybe" }
```

- **`[input]`**: the format comes from the file extension unless `format` is set. For CSV and JSON the usual column names are recognised (`title`, `abstract`, `doi`, `year`, `authors`, `keywords`, and the Web of Science and Scopus names); `fields` maps the others. RIS uses TI/T1, AB/N2, DO, PY, AU, KW, ID. A JSON file is a list of objects, or an object holding one.
- **Ids.** Each reference is known by its DOI (lowercase, without `https://doi.org/`), else its own id (the `id` column, RIS `ID`, the BibTeX key), else a hash of its title and year. A second reference with the same id is skipped with a warning, which also drops duplicates across databases that share a DOI.
- **`[prompt]`**: `context` is your research question and criteria, in plain words. The built-in instructions put it before the questions. `file = "prompt.md"` replaces the built-in instructions with yours: `{context}` and `{questions}` are filled in, and `<!-- comments -->` are dropped. `abstract_chars` cuts very long abstracts.
- **`[[question]]`**: `id` (letters, digits, `-`, `_`), `ask`, and `labels` (with `multiple = true` for tags) or `type = "text"`. `fallback` must be one of the labels. `notes` and `examples` go into the instructions: small models learn much more from examples than from descriptions.

## Output

`results/<id>.json`:

```json
{
  "id": "10.1214/09-aos685",
  "item": {"title": "...", "year": "2009", "doi": "10.1214/09-aos685", "authors": ["...", "...", "..."], "source": "refs.ris"},
  "answers": {
    "decision": {"label": "include", "reason": "..."},
    "topics": {"labels": ["causal-discovery", "causal-effects"], "reason": "..."},
    "data": {"text": "Simulated data, plus a riboflavin production data set.", "reason": "..."}
  },
  "status": "ok",
  "problems": [],
  "model": {"backend": "claude-cli", "name": "haiku", "used": "<full model name reported by the back-end>"},
  "fingerprint": "ddfd2fd85ae6f306",
  "date": "2026-11-02T14:43:26",
  "seconds": 9.9
}
```

- `status`: `ok`, `no_abstract`, `invalid_answer` (some answer was outside the labels or empty; see `problems`) or `error` (the model could not be reached or gave no JSON; tried again on the next run).
- `fingerprint`: a hash of the instructions, the questions and the model settings. Two results with the same fingerprint were made in the same way.
- The abstract is not copied into the result: it stays in the input file.

`criba-ref export` writes the CSV with the columns `id, title, year, doi, status`, then `<question>` and `<question>_reason` for each question (tags joined with `; `), then `model, date, problems`.

## Privacy

- With `ollama`, nothing leaves the machine unless `model.url` points elsewhere.
- With `claude-cli`, the instructions and each reference's text go to Anthropic under your Claude account. That is fine for published titles and abstracts, not for confidential material such as manuscripts under review. The command runs in an empty temporary folder with no settings, no tools and no MCP servers, so none of your CLAUDE.md files, memory or connected services are involved.

## Tests

```sh
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

## Not done yet

- Several models per reference, with an agreement rule and a third model or a person for the disagreements.
- Back-ends for OpenAI-compatible servers and the Anthropic API.
- Fetching missing abstracts.
- Running several references at once.
