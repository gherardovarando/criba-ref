# criba-ref

[![DOI](https://zenodo.org/badge/1443034509.svg)](https://doi.org/10.5281/zenodo.24190598)

A first screening of a list of references with a language model. You give it a list of titles and abstracts (CSV, BibTeX, RIS or JSON), your research question with its criteria, and a few questions with the answers allowed. It asks the model about each reference and writes one JSON file per reference with the answers, each with a short reason. Then it exports everything as one CSV, one row per reference.

The model can be a local one, through [Ollama](https://ollama.com), or Claude: through the `claude` command of [Claude Code](https://claude.com/claude-code) and your own Claude login, or through the Anthropic API with an API key, one reference at a time or as a batch at half the price.

It is meant for a first pass. The answers are a model's reading of the abstract: check them, at least the `include` and `maybe` ones, before they go into a study.

## What it does

- **Questions are the core.** Each `[[question]]` in the config is one thing to decide about a reference:
  - one label from a list (e.g. `include`, `exclude`, `maybe`);
  - zero or more labels from a list (tags, e.g. the topics or the methods);
  - a short free answer (e.g. "what data does the study use?").

  Every answer comes with a reason based on the title and abstract. "Include or not" is just one question, so you can also use it only to tag a list, or to ask several decisions at once.
- **One JSON file per reference**, in `output.dir`, with the answers, the status, the model that answered, the tokens used, the date and a fingerprint of the instructions.
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

Python 3.11 or later. No other Python packages are needed, except `anthropic` for the Anthropic API back-end.

For a local model: install Ollama and pull a model, e.g. `ollama pull qwen3:4b-instruct` (2.5 GB; on a 4-core CPU without a GPU, about a minute per reference).
For Claude: install Claude Code and log in once (`claude`).
For the Anthropic API: `.venv/bin/pip install -e '.[anthropic]'` (the `anthropic` package) and an API key; see [Anthropic API and batches](#anthropic-api-and-batches).

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

## Anthropic API and batches

**The key.** Create one in the [Anthropic Console](https://platform.claude.com) (API keys → Create key), inside a workspace. The API is billed to the Console organisation (prepaid credits or billing), not to a Claude subscription. criba-ref reads the key only from the environment, never from the config file. A simple way to keep it: a file only you can read, filled without the key appearing on screen or in the shell history.

```sh
mkdir -p ~/.config/criba-ref
read -rs K && printf 'export ANTHROPIC_API_KEY=%s\n' "$K" > ~/.config/criba-ref/env && unset K   # paste the key, Enter
chmod 600 ~/.config/criba-ref/env
. ~/.config/criba-ref/env    # in the shell where you run criba-ref
```

Each line of that file must start with `export `. A key that is not scoped to a workspace also needs the workspace id: add `export ANTHROPIC_WORKSPACE_ID=wrkspc_...` (Console → Settings → Workspaces). Don't paste the key into chats, scripts or the config file; if it ends up somewhere it shouldn't, delete it in the Console and make a new one.

**The config.**

```toml
[model]
backend = "anthropic"
name = "claude-opus-5-5"     # a full model id
# effort = "low"             # optional
# max_tokens = 16000         # optional
```

Or keep your config as it is and choose on the command line: `--backend anthropic --model claude-opus-5-5 -o results-opus`.

With `backend = "anthropic"` and a full model id (e.g. `name = "claude-opus-5-5"`), `run` asks the API one reference at a time. The answer is constrained to the JSON schema of the questions (structured outputs), and the instructions are sent as a cached block. `effort` (`low`, `medium`, `high`, `xhigh`, `max`; empty = the model's default) sets how much the model reasons before answering. It is part of the fingerprint, as the model is. `max_tokens` (default 16000) caps each answer, reasoning included.

For many references, send them as [Message Batches](https://platform.claude.com/docs/en/build-with-claude/batch-processing): half the price, results within 24 hours (often within an hour).

```sh
criba-ref batch submit --dry-run   # how many references would be sent
criba-ref batch submit -n 10       # try 10 first (or --ids ID ID ... for chosen ones)
criba-ref batch status             # the batches and their counts
criba-ref batch collect            # write the results of the batches that have ended
criba-ref stats                    # counts, and the tokens used
criba-ref batch submit             # the rest
criba-ref batch cancel             # stop the batches still running
```

- `submit` sends what `run` would screen: references without a result for the current config, and those with an error. References already in a batch that is not collected are not sent again. References without an abstract are written at once, without the model.
- Each batch is noted in `results/_batches/<batch id>.json`: the batch id, the fingerprint, the model and which request is which reference. Up to 10,000 references per batch.
- `collect` writes ordinary result files, with `"batch": "<batch id>"`. Requests that failed or expired get `status: error` and go in the next `submit`.
- Each result has `usage`, the tokens of that answer, and `stats` adds them up. Multiply by the [prices](https://platform.claude.com/docs/en/about-claude/pricing) (halved for batches) for the cost.

An example: 10 abstracts of a causal-inference test list (about 330 tokens each), with three questions and Claude Opus 5.5 at its default effort, used about 490 input tokens, 2,600 cached instruction tokens and 210 output tokens per abstract, about $0.004 per abstract as a batch (November 2026 prices) and 6½ minutes for the batch. Run a small batch on your own list first: the cost depends on the length of the abstracts, the instructions and the answers.

## Missing abstracts

`criba-ref abstracts` looks up the references without an abstract in public sources and writes a copy of the list with the abstracts it found:

```sh
criba-ref abstracts --dry-run                   # which references would be looked up
criba-ref abstracts -f references-filled.csv    # look them up, write the filled-in list
# then set input.file = "references-filled.csv" and screen as usual
criba-ref abstracts -f references-filled.csv --force --no-lookup   # rewrite the list from the cache only
criba-ref abstracts --retry                     # ask again for those not found before
```

- **Sources**, tried in order until one has the abstract: OpenAlex, Crossref, arXiv, Semantic Scholar (`[abstracts] sources`, or `--sources openalex,arxiv`).
- **Matching.** A reference with a DOI is looked up by its DOI, and the record is rejected when its title is very different (a wrong DOI). A reference without a DOI is looked up by title. A record is accepted only when the titles are the same or nearly the same (similarity ≥ `min_similarity`, 0.95, after lower-casing and dropping accents and punctuation) and its year is at most 3 years before or 1 after the reference's (a preprint is often older than the paper). The abstract of a matched preprint can be an earlier version of the published one.
- **Cache.** Every lookup is kept in `abstracts.jsonl` (`[abstracts] cache`), one line per reference: found or not, the source, the link to the record, the matched title and year, the similarity, what each source said, and the date. A reference is not asked again once found or tried everywhere without errors. Network errors and rate limits are tried again on the next run. Keep the cache with the list (e.g. in git): it shows where every abstract came from.
- **The filled-in list.** A CSV or TSV input keeps all its columns and rows, with the abstract filled in and two new columns, `abstract_source` and `abstract_url`. Other formats (BibTeX, RIS, JSON) become a CSV with the fields criba-ref reads. The ids stay the same, so results made before still match. The input file is never changed.
- **Keys and rate limits.** None is needed. Without a key OpenAlex shares a small daily budget per IP address, and Semantic Scholar often answers "too many requests". Keys are read from the environment only, never from the config: `OPENALEX_API_KEY`, `S2_API_KEY`. Crossref asks for a contact address (`[abstracts] mailto`, empty by default) for its polite pool; it is sent only if you set it. Requests wait `delay` seconds per host (3 s for arXiv).

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
backend = "ollama"           # or "claude-cli", "anthropic"
name = "qwen3:4b-instruct"   # claude-cli: "haiku", "sonnet", "opus"; anthropic: e.g. "claude-opus-5-5"
timeout = 300
# effort = "low"             # anthropic only
# max_tokens = 16000         # anthropic only

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
  "seconds": 9.9,
  "usage": {"input_tokens": 2012, "output_tokens": 486}
}
```

- `status`: `ok`, `no_abstract`, `invalid_answer` (some answer was outside the labels or empty; see `problems`) or `error` (the model could not be reached or gave no JSON; tried again on the next run).
- `fingerprint`: a hash of the instructions, the questions and the model settings. Two results with the same fingerprint were made in the same way.
- `usage`: the tokens of that answer, when the back-end reports them. With `claude-cli`, also `cost_usd`, the cost the `claude` command reports.
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

**The labelling page.** `criba-ref sheet -f expert.csv --html` also writes `expert.html`, one self-contained page to send to the expert. It needs no server, no install and no internet, and nothing leaves the browser.
- The expert writes their name, reads the instructions (the research question, and each question with its notes and examples, as the model gets them, without any model answer), and labels the papers one at a time.
- Answers are radio buttons for one label, check boxes plus "none" for tags, and text fields for free answers and a comment. A paper is done when every label and tag question has an answer.
- Layout: on a wide screen the paper is on the left and the instructions (collapsible) and answers on the right, which stay in view while you read; on a phone, one column.
- An answered question collapses to one line with its answer (click it to change it); tag and text questions stay open until you answer another one or press done.
- Moving around: previous, next and "next not done" in a bar always visible at the bottom, numbered buttons for every paper, and keys (`1`–`9` for the first question, `←` `→`, `n`, `?`).
- The page keeps the answers in the browser as the expert goes, and counts the seconds spent on each paper.
- **Download CSV** gives `expert-<name>.csv`: the sheet's columns filled in, plus `labeller`, `seconds` and `last_change`. That is the file to give to `compare`. **Load CSV** reads one back, to go on in another browser.
- The page warns when there are answers not yet downloaded. If the browser keeps nothing between visits (e.g. a private window), it says so.

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
- `report.html`: one self-contained page, starting with the questions and their labels, then the tables, the confusion matrices as SVG, the missed references and every disagreement with each source's reason. It reads on a phone and prints to PDF.
- `report.md` and `figures/*.svg`: for git and GitHub.
- `csv/metrics.csv` (long format: question, source_a, source_b, metric, value, ci_low, ci_high, n), `csv/confusion.csv` and `csv/disagreements.csv` (one row per reference and question where sources differ, with each source's answer and reason, to review by hand).
- `latex/`: tables for a paper.
  - The files are `questions.tex` (the questions and their labels), `agreement-<q>.tex`, `confusion-<q>-<a>-vs-<b>.tex` (a shaded matrix), `screening-<q>.tex` and `tags-<q>.tex`.
  - Each is a `table` float with a caption and a label (`tab:agreement-decision`, …), to `\input` in a paper that loads `booktabs`, `array` and `xcolor` with the `table` option.
  - `report.tex` inputs them all, to compile and look.

Every report states the date, the criba-ref version, the command line and the config, so a number in a paper can be traced back to how it was made.

## Privacy

- With `ollama`, nothing leaves the machine unless `model.url` points elsewhere.
- With `claude-cli`, the instructions and each reference's text go to Anthropic under your Claude account. That is fine for published titles and abstracts, not for confidential material such as manuscripts under review. The command runs in an empty temporary folder with no settings, no tools and no MCP servers, so none of your CLAUDE.md files, memory or connected services are involved.
- With `anthropic`, the same text goes to the Anthropic API under the organisation of your API key, with its data retention settings.

With `criba-ref abstracts`, the titles and DOIs of the references without an abstract are sent to the sources listed above.

## Tests

```sh
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

## Not done yet

- Several models per reference, with an agreement rule and a third model or a person for the disagreements.
- Stratified samples for the expert sheet (more likely includes, with weights to keep recall unbiased).
- A back-end for OpenAI-compatible servers.
- Running several references at once with `run` (it asks one at a time; `batch` sends them all at once, but only to the Anthropic API).
- Several experts on one computer: the labelling page keeps its answers in the browser per sheet, not per person, so two people in the same browser overwrite each other. Use separate browsers or computers.
- A consensus of several experts as the reference in `compare` (now it compares each expert with each model and with the others).

## How this was made

This tool was written by an AI coding agent: **Claude Opus 5.5** (model ID `claude-opus-5-5`) by Anthropic, running in **Claude Code**, from 2 to 16 November 2026. The idea came from [amanuense](https://github.com/gherardovarando/amanuense), the owner's tool that sorts email with a local model: criba-ref does the same kind of work on a list of references, one decision per item from its text. The design, features and constraints came from the repository owner's requests during the sessions, and they reviewed the results along the way:
- configurable questions, with the include/exclude decision as just one of them
- a local model through Ollama as an option, so references can stay on your computer
- a blinded sheet and a labelling page for an expert, and a comparison of the model with the expert
- the Anthropic API with batches, and the tokens used in every result

The agent wrote the code and the tests. It tested the tool on made-up references and on a small test list of papers chosen by the owner, who labelled some of them as the expert.

At the owner's request, the agent also searched GitHub for code like this tool's, to see if any of it came from other projects. It searched for about 20 distinctive lines from every module, from the labelling page and from the tests (336 searches), looked for notes like "adapted from", and checked the dependencies (none, apart from the optional `anthropic` package). It found no copied code. The lines found elsewhere are common one-line patterns (opening a URL, creating a folder, splitting a list into chunks, a dataclass field, a few CSS rules) and the textbook formulas for Cohen's kappa and the Jaccard index, with the rest of the code different. One of them, rounding half up with `Decimal`, is the same line as in another public project also written with Claude. This is most likely the same model writing the same short pattern, not copying. AI-written code can look like other AI-written code.

This search has limits. GitHub's code search only finds text that is almost the same, and only on GitHub. It can't find code that was rewritten or that lives elsewhere, and it can't rule out that the model repeats something it saw in training. If you find code here that comes from your work, we are sorry. Please open an issue in this repository: we will credit you, add your licence, or rewrite or remove the code.
