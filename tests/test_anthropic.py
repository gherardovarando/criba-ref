"""The anthropic back-end and batches, with a fake client instead of the API."""
import json
from types import SimpleNamespace as NS

import pytest

from criba_ref import backends, batch, cli, config, prompt, readers, screen

CONFIG = '''[input]
file = "refs.csv"
[model]
backend = "anthropic"
name = "claude-opus-5-5"
effort = "low"
[[question]]
id = "decision"
ask = "Include?"
labels = ["include", "exclude", "maybe"]
[[question]]
id = "tags"
ask = "Topics?"
labels = ["a", "b"]
multiple = true
'''


def message(answer, stop="end_turn", model="claude-opus-5-5"):
    return NS(content=[NS(type="thinking", thinking=""), NS(type="text", text=json.dumps(answer))],
              stop_reason=stop, model=model, stop_details=None,
              usage=NS(input_tokens=2000, output_tokens=500, cache_read_input_tokens=0,
                       cache_creation_input_tokens=0))


GOOD = {"decision": {"basis": "fits", "label": "include"}, "tags": {"basis": "about a", "labels": ["a"]}}


class FakeBatches:
    def __init__(self):
        self.made, self.status, self.cancelled = {}, {}, []

    def create(self, requests):
        bid = f"msgbatch_{len(self.made) + 1}"
        self.made[bid] = requests
        self.status[bid] = "in_progress"
        return NS(id=bid, processing_status="in_progress")

    def retrieve(self, bid):
        n = len(self.made[bid])
        done = self.status[bid] == "ended"
        return NS(id=bid, processing_status=self.status[bid],
                  request_counts=NS(processing=0 if done else n, succeeded=n if done else 0, errored=0, canceled=0,
                                    expired=0))

    def results(self, bid):
        for i, r in enumerate(self.made[bid]):
            if i == 1:
                yield NS(custom_id=r["custom_id"], result=NS(type="errored", error=NS(error=NS(type="api_error"))))
            else:
                yield NS(custom_id=r["custom_id"], result=NS(type="succeeded", message=message(GOOD)))

    def cancel(self, bid):
        self.cancelled.append(bid)


class FakeClient:
    def __init__(self, answer=None):
        self.sent = []
        self.answer = answer or message(GOOD)
        self.messages = NS(create=self.create, batches=FakeBatches())

    def with_options(self, **kw):
        return self

    def create(self, **params):
        self.sent.append(params)
        return self.answer


@pytest.fixture
def project(tmp_path):
    (tmp_path / "refs.csv").write_text("id,title,abstract\n10.1/a,Paper A,Text A.\n10.1/b,Paper B,Text B.\n"
                                       "x.3,Paper C,\nd,Paper D,Text D.\n")
    (tmp_path / "criba-ref.toml").write_text(CONFIG)
    cfg = config.load(tmp_path)
    items, _ = readers.read_items(cfg["input"]["file"])
    return cfg, items


def test_params_and_answer(project):
    cfg, _ = project
    m = cfg["model"]
    sch = prompt.schema(cfg["question"])
    p = backends.anthropic_params(m, "SYSTEM", "ITEM", sch)
    assert p["model"] == "claude-opus-5-5" and p["max_tokens"] == 16000
    assert p["output_config"] == {"format": {"type": "json_schema", "schema": sch}, "effort": "low"}
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"} and "thinking" not in p
    assert p["messages"] == [{"role": "user", "content": "ITEM"}]
    out, used, usage = backends.anthropic_answer(message(GOOD))
    assert out == GOOD and used == "claude-opus-5-5" and usage == {"input_tokens": 2000, "output_tokens": 500}
    for stop, word in (("refusal", "declined"), ("max_tokens", "max_tokens")):
        with pytest.raises(backends.BackendError, match=word):
            backends.anthropic_answer(message(GOOD, stop=stop))


def test_effort_changes_fingerprint_and_is_checked(project):
    cfg, _ = project
    fp = config.fingerprint(cfg, "S")
    cfg["model"]["effort"] = "high"
    assert config.fingerprint(cfg, "S") != fp
    cfg["model"]["effort"] = "huge"
    with pytest.raises(config.ConfigError, match="effort"):
        config.validate(cfg)


def test_run_with_anthropic_records_usage(project, monkeypatch):
    cfg, items = project
    fake = FakeClient()
    monkeypatch.setattr(backends, "client", lambda: fake)
    st = screen.run(cfg, items, log=open("/dev/null", "w"))
    assert st.counts == {"ok": 3, "no_abstract": 1} and len(fake.sent) == 3
    r = screen.load_result(screen.result_path(cfg, items[0]))
    assert r["usage"]["output_tokens"] == 500 and r["model"]["used"] == "claude-opus-5-5"
    assert r["answers"]["decision"] == {"label": "include", "reason": "fits"}
    from criba_ref import export
    assert "tokens over 3 model answers: input 6,000 (mean 2,000), output 1,500 (mean 500)" in export.stats(
        cfg, screen.results(cfg, items))


def test_batch_submit_status_collect(project):
    cfg, items = project
    fake = FakeClient()
    log = open("/dev/null", "w")
    counts, ids = batch.submit(cfg, items, dry_run=True, client=fake, log=log)
    assert counts["to_send"] == 3 and counts["no_abstract"] == 1 and ids == [] and not fake.messages.batches.made
    counts, ids = batch.submit(cfg, items, client=fake, log=log)
    assert ids == ["msgbatch_1"] and counts["to_send"] == 3
    reqs = fake.messages.batches.made["msgbatch_1"]
    assert [r["custom_id"] for r in reqs] == ["r000001", "r000002", "r000003"]
    assert reqs[0]["params"]["output_config"]["effort"] == "low"
    # sent again: nothing new, they are in a batch
    counts, ids = batch.submit(cfg, items, client=fake, log=log)
    assert counts["in_a_batch"] == 3 and ids == []
    assert batch.status(cfg, client=fake)[0]["status"] == "in_progress"
    assert batch.collect(cfg, items, client=fake, log=log) == {}
    fake.messages.batches.status["msgbatch_1"] = "ended"
    assert batch.collect(cfg, items, client=fake, log=log) == {"ok": 2, "error": 1}
    r = screen.load_result(screen.result_path(cfg, items[0]))
    assert r["batch"] == "msgbatch_1" and r["status"] == "ok" and r["usage"]["input_tokens"] == 2000
    bad = screen.load_result(screen.result_path(cfg, items[1]))
    assert bad["status"] == "error" and "errored" in bad["problems"][0]
    assert batch.status(cfg, client=fake)[0]["collected"]
    # the error is sent again, the rest is done
    counts, ids = batch.submit(cfg, items, client=fake, log=log)
    assert counts["to_send"] == 1 and counts["skipped"] == 3
    assert fake.messages.batches.made["msgbatch_2"][0]["params"]["messages"][0]["content"].count("Paper B")


def test_batch_needs_anthropic_backend(project):
    cfg, items = project
    cfg["model"]["backend"] = "fake"
    with pytest.raises(batch.BatchError):
        batch.submit(cfg, items)


def test_cli_batch(project, monkeypatch, capsys):
    cfg, _ = project
    fake = FakeClient()
    monkeypatch.setattr(backends, "client", lambda: fake)
    d = str(cfg["_path"].parent) if hasattr(cfg["_path"], "parent") else cfg["_path"]
    cli.main(["batch", "submit", "-c", str(d), "--dry-run"])
    assert "to send 3" in capsys.readouterr().out
    cli.main(["batch", "submit", "-c", str(d), "--dry-run", "--ids", "d", "10.1/a"])
    assert "to send 2" in capsys.readouterr().out
    cli.main(["batch", "submit", "-c", str(d), "-n", "2"])
    assert "sent 1 batch" in capsys.readouterr().out
    cli.main(["batch", "status", "-c", str(d)])
    assert "2 requests  in_progress" in capsys.readouterr().out
    cli.main(["batch", "cancel", "-c", str(d)])
    assert fake.messages.batches.cancelled == ["msgbatch_1"]
