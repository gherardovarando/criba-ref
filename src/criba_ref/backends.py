"""Model back-ends. Each one takes the instructions, the item text and a JSON schema, and returns
(answer dict, name of the model that answered, token use or {}). The model never calls tools."""
import json
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request


class BackendError(Exception):
    pass


def _post(url, payload, timeout):
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise BackendError(f"HTTP {e.code} from {url}: {e.read()[:300].decode(errors='replace')}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BackendError(f"can't reach {url}: {e}") from None


def _json(text):
    try:
        out = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise BackendError(f"the model's answer is not JSON: {str(text)[:200]!r}") from None
    if not isinstance(out, dict):
        raise BackendError("the model's answer is not a JSON object")
    return out


def ollama(m, system, user, schema):
    payload = {"model": m["name"], "stream": False, "format": schema, "keep_alive": "5m",
               "options": {"temperature": m["temperature"], "num_ctx": m["num_ctx"]},
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if m.get("think") is not None:
        payload["think"] = m["think"]
    out = _post(m["url"].rstrip("/") + "/api/chat", payload, m["timeout"])
    usage = {"input_tokens": out.get("prompt_eval_count"), "output_tokens": out.get("eval_count")}
    return _json(out.get("message", {}).get("content")), m["name"], {k: v for k, v in usage.items() if v}


def claude_cli(m, system, user, schema):
    """`claude -p` with structured output. It runs in an empty temporary folder with no settings
    sources, no tools and no MCP servers, so no CLAUDE.md, memory or tool reaches the model: only
    the instructions and the item text. It uses whatever login the claude command has."""
    exe = shutil.which(m["command"])
    if not exe:
        raise BackendError(f"command not found: {m['command']}")
    cmd = [exe, "-p", "--output-format", "json", "--json-schema", json.dumps(schema), "--system-prompt", system,
           "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
           "--model", m["name"]]
    with tempfile.TemporaryDirectory(prefix="criba-ref-") as tmp:
        try:
            r = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=m["timeout"], cwd=tmp)
        except subprocess.TimeoutExpired:
            raise BackendError(f"claude took longer than {m['timeout']} s") from None
    try:
        env = json.loads(r.stdout)
    except json.JSONDecodeError:
        raise BackendError(f"claude exited with {r.returncode}: {(r.stderr or r.stdout)[:300].strip()}") from None
    if env.get("is_error") or env.get("subtype") != "success":
        raise BackendError(f"claude: {str(env.get('result') or env.get('subtype'))[:300]}")
    out = env.get("structured_output")
    if out is None:
        out = _json(env.get("result"))
    used = list(env.get("modelUsage") or {}) or [m["name"]]
    u = env.get("usage") or {}
    usage = {k: u[k] for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                               "cache_creation_input_tokens") if u.get(k)}
    if env.get("total_cost_usd") is not None:
        usage["cost_usd"] = env["total_cost_usd"]
    return out, used[0], usage


def anthropic_params(m, system, user, schema):
    """The Messages API request: the instructions as a cached system block, the item as the user message,
    the answer constrained to the JSON schema (structured outputs). Thinking is left to the model."""
    out_cfg = {"format": {"type": "json_schema", "schema": schema}}
    if m.get("effort"):
        out_cfg["effort"] = m["effort"]
    return {"model": m["name"], "max_tokens": m["max_tokens"],
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}], "output_config": out_cfg}


def anthropic_answer(msg):
    """A Messages API response (or a batch result's message) -> (answer, model, token use)."""
    u = msg.usage
    usage = {k: getattr(u, k, None) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                               "cache_creation_input_tokens")}
    usage = {k: v for k, v in usage.items() if v}
    if msg.stop_reason == "refusal":
        cat = getattr(getattr(msg, "stop_details", None), "category", None)
        raise BackendError(f"the model declined to answer (refusal{', ' + cat if cat else ''})")
    if msg.stop_reason == "max_tokens":
        raise BackendError("the answer was cut at max_tokens (raise model.max_tokens)")
    text = next((b.text for b in msg.content if b.type == "text"), None)
    return _json(text), msg.model, usage


def client():
    """The Anthropic client. The key comes from ANTHROPIC_API_KEY (or another credential the SDK finds),
    never from the config file."""
    try:
        import anthropic
    except ImportError:
        raise BackendError("the anthropic back-end needs the SDK: pip install 'criba-ref[anthropic]'") from None
    return anthropic.Anthropic()


def anthropic_api(m, system, user, schema):
    import anthropic
    try:
        msg = client().with_options(timeout=m["timeout"]).messages.create(**anthropic_params(m, system, user, schema))
    except anthropic.APIStatusError as e:
        raise BackendError(f"Anthropic API: HTTP {e.status_code}: {str(e.message)[:300]}") from None
    except anthropic.APIConnectionError as e:
        raise BackendError(f"Anthropic API: can't connect: {e}") from None
    return anthropic_answer(msg)


def fake(m, system, user, schema):
    """A deterministic answer: the first label of each question. For tests and trying a config."""
    out = {}
    for qid, prop in schema["properties"].items():
        key = next(k for k in prop["properties"] if k != "basis")
        ans = prop["properties"][key]
        val = ans["enum"][0] if "enum" in ans else [ans["items"]["enum"][0]] if ans["type"] == "array" else "-"
        out[qid] = {"basis": "fake back-end", key: val}
    return out, "fake", {}


BACKENDS = {"ollama": ollama, "claude-cli": claude_cli, "anthropic": anthropic_api, "fake": fake}


def complete(m, system, user, schema):
    """-> (answer, model that answered, token use)."""
    return BACKENDS[m["backend"]](m, system, user, schema)
