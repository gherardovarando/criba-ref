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

- **`[input]`**: the format comes from the file extension unless `format` is set. For CSV and JSON the usual column names are recognised (`title`, `abstract`, `doi`, `year`, `authors`, `keywords`, `url`, and the Web of Science and Scopus names); `fields` maps the others. RIS uses TI/T1, AB/N2, DO, PY, AU, KW, ID, UR. Without a url, a reference with a DOI gets `https://doi.org/<doi>`. A JSON file is a list of objects, or an object holding one.
- **Ids.** Each reference is known by its DOI (lowercase, without `https://doi.org/`), else its own id (the `id` column, RIS `ID`, the BibTeX key), else a hash of its title and year. A second reference with the same id is skipped with a warning, which also drops duplicates across databases that share a DOI.
- **`[prompt]`**: `context` is your research question and criteria, in plain words. The built-in instructions put it before the questions. `file = "prompt.md"` replaces the built-in instructions with yours: `{context}` and `{questions}` are filled in, and `<!-- comments -->` are dropped. `abstract_chars` cuts very long abstracts.
- **`[[question]]`**: `id` (letters, digits, `-`, `_`), `ask`, and `labels` (with `multiple = true` for tags) or `type = "text"`. `fallback` must be one of the labels. `notes` and `examples` go into the instructions: small models learn much more from examples than from descriptions. `pass` (single-label questions only) lists the labels that send a reference on to full-text screening, for the screening view of `compare`. Without it, every label but `exclude` passes, when there is an `exclude`. The model never sees `pass`, so changing it doesn't make old results stale.

## Output

`results/<id>.json`:

```json
{
  "id": "10.1214/09-aos685",
  "item": {"title": "...", "year": "2009", "doi": "10.1214/09-aos685", "url": "https://doi.org/10.1214/09-aos685", "authors": ["...", "...", "..."], "source": "refs.ris"},
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

`criba-ref export` writes the CSV with the columns `id, title, year, doi, url, status`, then `<question>` and `<question>_reason` for each question (tags joined with `; `), then `screened_by` (the model that answered), `date` and `problems`. So a question can't be called `id`, `title`, `year`, `doi`, `url`, `status`, `screened_by`, `date` or `problems`, nor end in `_reason`.

## Comparing screenings

`criba-ref compare` puts two or more screenings side by side, question by question: models against each other, and models against an expert.

```sh
criba-ref sheet -f expert.csv --sample 50 --seed 1     # a blinded sheet for the expert
# ... the expert fills in the answer columns ...
criba-ref compare results/haiku results/sonnet qwen=results/qwen3-4b expert=expert.csv --reference expert
criba-ref compare ... --reference expert -o compare/  # also write HTML, Markdown, CSV and LaTeX
```

**Sources.** Each source is a results folder or a CSV file, given as `PATH` or `NAME=PATH`. The name defaults to the folder name or the file stem.
- A **results folder**: results with an `error` are left out and counted. If the folder holds results made with different prompts, questions or models (different fingerprints), compare warns.
- A **CSV**: it needs an `id` column and a column per question, named by the question id. Anything after the id in the header, like `decision [include|exclude|maybe]`, is ignored, and so are other columns.
  - Labels are matched without case.
  - Tags are separated by `;` or `,`. `none` means "no tag".
  - An empty cell means "not labelled": that reference is left out of that question, not counted as wrong.
  - An unknown label is left out, with a warning.

  A filled-in sheet or a `criba-ref export` both work.

**The sheet.** `criba-ref sheet` writes one row per reference: `id, title, year, doi, url, abstract`, then one empty column per question (its header lists the allowed answers), then `comment`.
- It has no model answers, so the expert isn't anchored by them.
- `--sample N --seed S` takes a random sample, `--ids FILE` given references, `--questions` some questions only.
- It never overwrites a file, and it writes `<sheet>.json` next to it with how it was made.

A random sample holds few includes when they are rare (e.g. 10% of 50 is 5), so recall measured on it is uncertain: look at the intervals.

**What it reports.**
- For each **single-label question**, for every pair of sources, on the references both labelled:
  - the confusion matrix;
  - the agreement, with a 95% Wilson interval;
  - Cohen's kappa, with a 95% bootstrap interval over references (`--bootstrap 1000`, `--seed 0`: the same inputs always give the same numbers).

  With three or more sources it also gives Fleiss' kappa on the references all of them labelled. The reference, when given, is always the rows.
- **Screening view** (with `--reference`): the labels in `pass` count as "goes on to full text", the others as "out". For each source against the reference:
  - TP, FN, FP, TN;
  - recall (the reference's passes that the source also passes), precision, specificity, F1;
  - the share sent to full text;
  - WSS, work saved over sampling (Cohen et al. 2006): (TN + FN) / N − (1 − recall);
  - the list of **missed references**, which matters most in screening, since a reference missed here is lost.
- For each **tag question**:
  - how many references each source tagged with each tag;
  - Cohen's kappa per tag (present or absent);
  - the mean Jaccard overlap per reference;
  - against the reference, per-tag recall and precision.
- Text questions have no metric.

**Output.** By default, a short summary in the terminal. With `-o DIR`, also (`--formats` picks some):
- `report.html`: one self-contained page, with the tables, the confusion matrices as SVG, the missed references and every disagreement with each source's reason. It reads on a phone and prints to PDF.
- `report.md` and `figures/*.svg`: for git and GitHub.
- `csv/metrics.csv` (long format: question, source_a, source_b, metric, value, ci_low, ci_high, n), `csv/confusion.csv` and `csv/disagreements.csv` (one row per reference and question where sources differ, with each source's answer and reason, to review by hand).
- `latex/`: tables for a paper.
  - The files are `agreement-<q>.tex`, `confusion-<q>-<a>-vs-<b>.tex` (a shaded matrix), `screening-<q>.tex` and `tags-<q>.tex`.
  - Each is a `table` float with a caption and a label (`tab:agreement-decision`, …), to `\input` in a paper that loads `booktabs` and `xcolor` with the `table` option.
  - `report.tex` inputs them all, to compile and look.

Every report states the date, the criba-ref version, the command line and the config, so a number in a paper can be traced back to how it was made.

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
- Stratified samples for the expert sheet (more likely includes, with weights to keep recall unbiased).
- Back-ends for OpenAI-compatible servers and the Anthropic API.
- Fetching missing abstracts.
- Running several references at once.
