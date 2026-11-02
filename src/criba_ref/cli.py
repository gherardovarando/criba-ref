"""The criba-ref command."""
import argparse
import sys
from importlib import resources
from pathlib import Path

from . import __version__, backends, compare, config, export, prompt, readers, report, screen, sheet


def common():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("-c", "--config", help=f"the project config, or its folder (default: ./{config.CONFIG_NAME})")
    p.add_argument("--backend", choices=config.BACKENDS, help="model back-end (overrides model.backend)")
    p.add_argument("--model", help="model name (overrides model.name)")
    p.add_argument("--model-url", help="Ollama server URL (overrides model.url)")
    p.add_argument("-o", "--output-dir", help="results folder (overrides output.dir), e.g. one per model")
    p.add_argument("-i", "--input", help="input file (overrides input.file)")
    return p


def parser():
    p = argparse.ArgumentParser(prog="criba-ref",
                                description="Screen lists of references (titles and abstracts) with a language model.")
    p.add_argument("--version", action="version", version=f"criba-ref {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = common()
    i = sub.add_parser("init", help=f"write an example {config.CONFIG_NAME} to edit")
    i.add_argument("dir", nargs="?", default=".")
    ck = sub.add_parser("check", parents=[c], help="check the config, read the input and test the model")
    ck.add_argument("--no-model", action="store_true", help="don't call the model")
    pr = sub.add_parser("prompt", parents=[c], help="show what the model gets, or copy the built-in prompt to edit")
    pr.add_argument("action", nargs="?", choices=["show", "copy"], default="show")
    pr.add_argument("--item", help="also show the text sent for this item id (or 'first')")
    pr.add_argument("--schema", action="store_true", help="also show the JSON schema of the answer")
    r = sub.add_parser("run", parents=[c], help="screen the items that have no result yet")
    r.add_argument("-n", "--limit", type=int, help="screen at most N items in this run")
    r.add_argument("--ids", nargs="+", help="only these item ids")
    r.add_argument("--rerun", action="store_true",
                   help="also redo items screened with another prompt, questions or model")
    r.add_argument("--dry-run", action="store_true", help="list what would be screened, call no model")
    e = sub.add_parser("export", parents=[c], help="write the results as CSV, one row per item")
    e.add_argument("-f", "--file", default="-", help="CSV file to write (default: print)")
    sub.add_parser("stats", parents=[c], help="counts per status and per label")
    s = sub.add_parser("sheet", parents=[c], help="write a blinded labelling sheet (CSV) for an expert",
                       description="A CSV with the references and one empty column per question, without any "
                                   "model answer. Fill it in and give it to `compare`.")
    s.add_argument("-f", "--file", required=True, help="the CSV to write (never overwritten)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--sample", type=int, metavar="N", help="a random sample of N references")
    g.add_argument("--ids", metavar="FILE", help="the references listed in FILE, one id per line")
    s.add_argument("--seed", type=int, default=1, help="seed of the random sample (default 1)")
    s.add_argument("--questions", help="comma-separated question ids (default: all)")
    s.add_argument("--html", action="store_true",
                   help="also write a labelling page (same name, .html) that the expert opens in a browser")
    k = sub.add_parser("compare", help="compare screenings: result folders and CSV labels",
                       description="Compare two or more screenings question by question. A source is a results "
                                   "folder or a CSV with an id column and question columns (e.g. a filled-in "
                                   "sheet), as PATH or NAME=PATH.")
    k.add_argument("sources", nargs="+", metavar="SOURCE")
    k.add_argument("-c", "--config", help=f"the project config, or its folder (default: ./{config.CONFIG_NAME})")
    k.add_argument("-r", "--reference", metavar="NAME", help="score the others against this source (e.g. the expert)")
    k.add_argument("--questions", help="comma-separated question ids (default: all label questions)")
    k.add_argument("--ids", metavar="FILE", help="only the references listed in FILE")
    k.add_argument("-o", "--out", metavar="DIR", help="also write the report files into DIR")
    k.add_argument("--formats", default=",".join(report.FORMATS),
                   help=f"with -o: comma-separated, any of {', '.join(report.FORMATS)} (default: all)")
    k.add_argument("--bootstrap", type=int, default=1000, help="bootstrap resamples for kappa intervals (0: none)")
    k.add_argument("--seed", type=int, default=0, help="bootstrap seed (default 0)")
    return p


def load(args):
    over = {"model": {"backend": args.backend, "name": args.model, "url": args.model_url},
            "output": {"dir": args.output_dir}, "input": {"file": args.input}}
    cfg = config.load(args.config, over)
    for key, val in (("output", args.output_dir), ("input", args.input)):  # relative to where you are
        if val:
            cfg[key]["dir" if key == "output" else "file"] = str(Path(val).expanduser().resolve())
    return cfg


def items_of(cfg, warn=True):
    items, problems = readers.read_items(cfg["input"]["file"], cfg["input"]["format"], cfg["input"]["fields"])
    if warn:
        for msg in problems[:20]:
            print(f"warning: {msg}", file=sys.stderr)
        if len(problems) > 20:
            print(f"warning: ... and {len(problems) - 20} more", file=sys.stderr)
    return items, problems


def cmd_init(args):
    d = Path(args.dir)
    d.mkdir(parents=True, exist_ok=True)
    target = d / config.CONFIG_NAME
    if target.exists():
        sys.exit(f"{target} exists already: not overwritten")
    target.write_text(resources.files("criba_ref").joinpath(f"data/{config.CONFIG_NAME}").read_text())
    print(f"wrote {target}: set input.file, prompt.context and the questions, then run `criba-ref check`")


def cmd_check(args):
    cfg = load(args)
    print(f"config: {cfg['_path']}")
    items, problems = items_of(cfg, warn=False)
    no_abs = sum(1 for it in items if not it.abstract)
    no_doi = sum(1 for it in items if not it.doi)
    skipped = f", {len(problems)} records skipped" if problems else ""
    print(f"input: {cfg['input']['file']}: {len(items)} items, {no_abs} without abstract, "
          f"{no_doi} without DOI{skipped}")
    for msg in problems[:10]:
        print(f"  {msg}")
    qs = cfg["question"]
    print(f"questions: {', '.join(q['id'] for q in qs)}")
    old = screen.results(cfg, items)
    print(f"results: {cfg['output']['dir']}: {len(old)} of {len(items)} items have one")
    m = cfg["model"]
    if args.no_model:
        return
    probe = readers.Item(id="check", title="A test reference about nothing in particular",
                         abstract="This is a made-up abstract used to check that the model answers.")
    try:
        raw, used = backends.complete(m, prompt.system_prompt(cfg), prompt.item_text(probe, 1000),
                                      prompt.schema(qs))
    except backends.BackendError as e:
        sys.exit(f"model: {m['backend']} {m['name']}: FAILED: {e}")
    _, bad = screen.clean(raw, qs)
    print(f"model: {m['backend']} {used}: answers" + (f", but: {'; '.join(bad)}" if bad else " in the right format"))


def cmd_prompt(args):
    cfg = load(args)
    if args.action == "copy":
        target = Path(cfg["_base"]) / "prompt.md"
        if target.exists():
            sys.exit(f"{target} exists already: not overwritten")
        target.write_text("<!-- Instructions for the model. {context} and {questions} are filled in from "
                          f"{config.CONFIG_NAME}. Set prompt.file = \"prompt.md\" to use this file. -->\n"
                          + prompt.PROMPT)
        print(f"wrote {target}: set prompt.file = \"prompt.md\" in {config.CONFIG_NAME} to use it")
        return
    print(prompt.system_prompt(cfg))
    if args.schema:
        import json
        print("--- answer schema ---")
        print(json.dumps(prompt.schema(cfg["question"]), indent=2))
    if args.item:
        items, _ = items_of(cfg, warn=False)
        it = items[0] if args.item == "first" and items else next((x for x in items if x.id == args.item), None)
        if not it:
            sys.exit(f"no item with id {args.item!r}")
        print(f"--- item {it.id} ---")
        print(prompt.item_text(it, cfg["prompt"]["abstract_chars"]))


def cmd_run(args):
    cfg = load(args)
    items, _ = items_of(cfg)
    if args.ids:
        want = set(args.ids)
        items = [it for it in items if it.id in want]
        missing = want - {it.id for it in items}
        if missing:
            print(f"warning: no item with id {', '.join(sorted(missing))}", file=sys.stderr)
    m = cfg["model"]
    print(f"screening {len(items)} items with {m['backend']} {m['name']} -> {cfg['output']['dir']}", file=sys.stderr)
    st = screen.run(cfg, items, rerun=args.rerun, dry_run=args.dry_run, limit=args.limit)
    counts = ", ".join(f"{k} {v}" for k, v in sorted(st.counts.items())) or "nothing new"
    print(f"done: {counts}; {st.skipped} already screened"
          + (f"; {st.stale} screened with another config, left as they are (use --rerun)" if st.stale else ""),
          file=sys.stderr)
    if st.counts.get("error"):
        sys.exit(1)


def cmd_export(args):
    cfg = load(args)
    items, _ = items_of(cfg, warn=False)
    text = export.to_csv(cfg, screen.results(cfg, items))
    if args.file == "-":
        sys.stdout.write(text)
    else:
        Path(args.file).write_text(text)
        print(f"wrote {args.file}", file=sys.stderr)


def cmd_stats(args):
    cfg = load(args)
    items, _ = items_of(cfg, warn=False)
    res = screen.results(cfg, items)
    print(f"{len(items)} items in the input")
    print(export.stats(cfg, res))


def read_ids(path):
    return [l.strip() for l in Path(path).read_text().splitlines() if l.strip() and not l.startswith("#")]


def pick_questions(cfg, spec):
    if not spec:
        return cfg["question"]
    want = [x.strip() for x in spec.split(",") if x.strip()]
    qmap = {q["id"]: q for q in cfg["question"]}
    unknown = [x for x in want if x not in qmap]
    if unknown:
        raise config.ConfigError(f"no question {', '.join(unknown)}")
    return [qmap[x] for x in want]


def cmd_sheet(args):
    cfg = load(args)
    items, _ = items_of(cfg, warn=False)
    chosen, missing = sheet.choose(items, args.sample, args.seed, read_ids(args.ids) if args.ids else None)
    if missing:
        print(f"warning: {len(missing)} ids not in the input: {', '.join(missing[:5])}", file=sys.stderr)
    qs = pick_questions(cfg, args.questions)
    info = {"config": cfg["_path"], "input": cfg["input"]["file"],
            "selection": (f"random sample of {args.sample}, seed {args.seed}" if args.sample else
                          f"ids from {args.ids}" if args.ids else "all")}
    page = Path(args.file).with_suffix(".html") if args.html else None
    if page:
        info["html"] = str(page)
    try:
        side = sheet.write(args.file, chosen, qs, info)
        if page:
            sheet.write_html(page, Path(args.file).stem, chosen, qs, cfg["prompt"]["context"], info)
    except FileExistsError as e:
        sys.exit(f"error: {e}")
    print(f"wrote {args.file}: {len(chosen)} references, questions {', '.join(q['id'] for q in qs)} "
          f"(how it was made: {side})", file=sys.stderr)
    if page:
        print(f"wrote {page}: open it in a browser; its Download CSV gives a sheet that compare reads", file=sys.stderr)


def cmd_compare(args):
    cfg = config.load(args.config)
    qids = [q["id"] for q in pick_questions(cfg, args.questions)] if args.questions else None
    rep = compare.build(cfg, args.sources, args.reference, qids, read_ids(args.ids) if args.ids else None,
                        args.bootstrap, args.seed, command="criba-ref " + " ".join(sys.argv[1:]))
    print(report.text(rep))
    if args.out:
        formats = [f.strip() for f in args.formats.split(",") if f.strip()]
        bad = set(formats) - set(report.FORMATS)
        if bad:
            sys.exit(f"error: unknown format(s) {', '.join(sorted(bad))}")
        files = report.write(rep, args.out, formats)
        print(f"wrote {len(files)} files to {args.out}", file=sys.stderr)


COMMANDS = {"init": cmd_init, "check": cmd_check, "prompt": cmd_prompt, "run": cmd_run, "export": cmd_export,
            "stats": cmd_stats, "sheet": cmd_sheet, "compare": cmd_compare}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        COMMANDS[args.cmd](args)
    except (config.ConfigError, readers.InputError) as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        sys.exit("interrupted: the items screened so far are saved; run again to go on")


if __name__ == "__main__":
    main()
