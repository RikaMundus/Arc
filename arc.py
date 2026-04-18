#!/usr/bin/env python3
"""Arc - minimal self-evolving AGI harness."""
import os, sys, re, json, shlex, shutil, subprocess, ast, tempfile
from datetime import datetime, timezone
from pathlib import Path
import urllib.request, urllib.error

SELF = Path(__file__).resolve()
MEMORY = SELF.parent / "memory.md"
BAK = SELF.parent / "arc.py.bak"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

MEMORY_TEMPLATE = '''## Core
<!-- Never compacted. System prompt, config, tool definitions. -->

### Config
API_KEY: {api_key}
MODEL: anthropic/claude-opus-4.7

### System Prompt
You are a self-evolving AI agent running inside Arc, a minimal Python harness.

Your state lives in memory.md with two sections:
- `## Core` - your system prompt, config (API_KEY, MODEL), and tool list. Never compacted.
- `## Scratch` - the running transcript. Each turn begins with `### [role] <ISO-timestamp>` on its own line. `[assistant]` marks your prior output; `[tool:NAME]` marks results of tools you invoked (suffixed with status `ok`/`err`/`cancelled`); `[user]` is the human; `[system]` is from the harness itself.

The entire Scratch is handed to you as a single user-role message; continue from the end as a new assistant turn.

To invoke a tool, write it on its own line, e.g. `/cmd("ls")`. The harness will confirm with the user, execute, and append the result to Scratch. Multiple calls in one turn run sequentially. You may discuss tools in prose by placing them inside triple-backtick code fences - fenced content is ignored by the parser.

To extend yourself: edit `arc.py` via `/cmd` with a Python one-liner, then `/restart()`. The harness syntax-checks arc.py and backs it up before reloading; if the check fails, the restart is aborted and you get another turn to fix it.

Args are shell-style (shlex), not Python-style: `/cmd("echo hi")` passes one string. Commas are literal characters, not separators.

### Tools
- `/cmd(command)` - run a shell command (cmd.exe on Windows, /bin/sh elsewhere). Confirmation required.
- `/set_key=KEY` - update API_KEY in Core.
- `/set_model=MODEL` - update MODEL in Core.
- `/restart()` - reload arc.py. ast-checks and backs up first.
- `/view()` - print memory.md to the terminal. Output not re-logged to Scratch.
- `/edit()` - open memory.md in $EDITOR (or notepad). Human-only.

## Scratch
'''

def load_memory():
    text = MEMORY.read_text(encoding="utf-8")
    m = re.search(r"^## Scratch\s*$", text, re.MULTILINE)
    if not m: return text, ""
    return text[:m.start()].rstrip() + "\n", text[m.end():].lstrip("\n")

def save_memory(core, scratch):
    text = core.rstrip() + "\n\n## Scratch\n" + scratch.lstrip("\n")
    tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(MEMORY.parent), delete=False)
    try: tmp.write(text); tmp.close(); os.replace(tmp.name, str(MEMORY))
    except Exception:
        try: os.unlink(tmp.name)
        except OSError: pass
        raise

def append_scratch(role, content, status=None):
    core, scratch = load_memory()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hdr = f"### [{role}] {ts}" + (f" {status}" if status else "")
    sep = "\n" if scratch.strip() else ""
    save_memory(core, scratch.rstrip() + sep + f"\n{hdr}\n{content}\n")

def get_config(core, key):
    m = re.search(rf"^\s*{re.escape(key)}:\s*(.*?)\s*$", core, re.MULTILINE)
    return m.group(1) if m else None

def set_config(core, key, value):
    pat = re.compile(rf"^(\s*){re.escape(key)}:\s*.*?\s*$", re.MULTILINE)
    if pat.search(core):
        return pat.sub(rf"\g<1>{key}: {value}", core, count=1)
    cfg = re.search(r"^### Config\s*$", core, re.MULTILINE)
    if cfg:
        i = cfg.end()
        return core[:i] + f"\n{key}: {value}" + core[i:]
    return core + f"\n### Config\n{key}: {value}\n"

FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
CALL_RE = re.compile(r"^\s*/([a-zA-Z_]\w*)\s*(?:=(.*)|\((.*)\)|)\s*$", re.MULTILINE)

def parse_calls(text):
    stripped = FENCE_RE.sub("", text)
    calls = []
    for m in CALL_RE.finditer(stripped):
        name, kv, paren = m.group(1), m.group(2), m.group(3)
        if kv is not None:
            calls.append((name, "kv", kv.strip()))
        elif paren is not None:
            try: args = shlex.split(paren, posix=True)
            except ValueError: args = [paren]
            calls.append((name, "call", args))
        else:
            calls.append((name, "bare", []))
    return calls

SENSITIVE = {"set_key"}
REDACT_OUTPUT = {"view"}

def call_repr(name, form, payload, redact=True):
    if redact and name in SENSITIVE:
        return f"/{name}=<redacted>"
    if form == "kv":
        return f"/{name}={payload}"
    if form == "call":
        return f"/{name}(" + " ".join(shlex.quote(a) for a in payload) + ")"
    return f"/{name}()"

def _payload_str(payload):
    return (payload if isinstance(payload, str) else " ".join(payload)).strip()

def _cmd(payload, from_model):
    cmd_str = _payload_str(payload)
    if not cmd_str: return "no command", "err"
    try:
        p = subprocess.run(cmd_str, shell=True, capture_output=True, text=True, timeout=300)
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
        return (out.rstrip() or "(no output)"), ("ok" if p.returncode == 0 else "err")
    except subprocess.TimeoutExpired: return "timeout after 300s", "err"
    except Exception as e: return f"exception: {e}", "err"

def _set(key_name):
    def fn(payload, from_model):
        val = _payload_str(payload)
        if not val: return f"no {key_name} provided", "err"
        core, scratch = load_memory()
        save_memory(set_config(core, key_name, val), scratch)
        return f"{key_name} updated", "ok"
    return fn

def _restart(payload, from_model):
    try: ast.parse(SELF.read_text(encoding="utf-8"))
    except SyntaxError as e: return f"restart aborted: {e}", "err"
    try: shutil.copy(str(SELF), str(BAK))
    except Exception as e: return f"backup failed: {e}", "err"
    append_scratch("system", "restarting...")
    print("\n[arc] restarting...\n", flush=True)
    os.execv(sys.executable, [sys.executable, str(SELF)])

def _view(payload, from_model): return MEMORY.read_text(encoding="utf-8"), "ok"

def _edit(payload, from_model):
    if from_model: return "/edit is human-only (would block stdin)", "cancelled"
    editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
    try: subprocess.run([editor, str(MEMORY)]); return f"edited via {editor}", "ok"
    except Exception as e: return f"editor failed: {e}", "err"

TOOLS = {"cmd": _cmd, "set_key": _set("API_KEY"), "set_model": _set("MODEL"),
         "restart": _restart, "view": _view, "edit": _edit}

def confirm(cs, batch):
    if batch.get("all"):
        return "y"
    while True:
        try:
            ans = input(f"\n[confirm] {cs}\n  [y]es / [n]o / [a]ll (rest of turn) / [q]uit: ").strip().lower() or "n"
        except (EOFError, KeyboardInterrupt):
            return "q"
        if ans in ("y", "n", "a", "q"):
            if ans == "a":
                batch["all"] = True
                return "y"
            return ans

class ContextOverflow(Exception): pass

def call_model(core, scratch):
    api_key = get_config(core, "API_KEY") or ""
    model = get_config(core, "MODEL") or "anthropic/claude-opus-4.7"
    if not api_key or api_key == "sk-or-...":
        raise RuntimeError("API_KEY not set. Use /set_key=sk-or-...")
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": core},
            {"role": "user", "content": scratch or "(scratch empty - begin)"},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(OPENROUTER_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = ""
        try: body_text = e.read().decode("utf-8", errors="replace")
        except Exception: pass
        if e.code == 413 or any(s in body_text.lower() for s in ("context_length", "context length", "too long")):
            raise ContextOverflow(body_text)
        raise RuntimeError(f"HTTP {e.code}: {body_text[:500]}")
    return data["choices"][0]["message"]["content"]

def execute(name, form, payload, from_model):
    if name not in TOOLS:
        return f"unknown tool: {name}", "err"
    return TOOLS[name](payload, from_model)

def run_model_turn():
    batch = {"all": False}
    while True:
        core, scratch = load_memory()
        try:
            resp = call_model(core, scratch)
        except ContextOverflow as e:
            append_scratch("system", f"context overflow - please /compact or trim Scratch.\n{str(e)[:500]}")
            print("[arc] context overflow; returning to prompt.")
            return
        except Exception as e:
            append_scratch("system", f"api error: {e}")
            print(f"[arc] api error: {e}")
            return
        print("\n" + resp + "\n", flush=True)
        append_scratch("assistant", resp)
        calls = parse_calls(resp)
        if not calls:
            return
        for (name, form, payload) in calls:
            cs = call_repr(name, form, payload)
            ans = confirm(cs, batch)
            if ans == "q":
                append_scratch("system", f"user quit during tool confirmation at {cs}")
                return
            if ans == "n":
                append_scratch(f"tool:{name}", f"{cs}\n(cancelled by user)", status="cancelled")
                continue
            out, status = execute(name, form, payload, from_model=True)
            log_out = "(output not logged)" if name in REDACT_OUTPUT else out
            append_scratch(f"tool:{name}", f"{cs}\n{log_out}", status=status)

def repl():
    print("[arc] type a message, or a /command. Ctrl-C to exit.")
    while True:
        try:
            line = input("\n> ").rstrip()
        except (EOFError, KeyboardInterrupt):
            print("\n[arc] bye.")
            return
        if not line:
            continue
        if line.startswith("/"):
            calls = parse_calls(line)
            if not calls:
                print("[arc] didn't parse as a tool call.")
                continue
            for (name, form, payload) in calls:
                cs = call_repr(name, form, payload)
                out, status = execute(name, form, payload, from_model=False)
                log_out = "(output not logged)" if name in REDACT_OUTPUT else out
                append_scratch(f"user-tool:{name}", f"{cs}\n{log_out}", status=status)
                print(out if name in REDACT_OUTPUT else f"[{status}] {out[:4000]}")
        else:
            append_scratch("user", line)
            run_model_turn()

def main():
    if not MEMORY.exists():
        print("[arc] first run. No memory.md found.")
        try:
            key = input("Paste OpenRouter API key (sk-or-...), or blank to set later: ").strip()
        except (EOFError, KeyboardInterrupt):
            key = ""
        if not key:
            key = "sk-or-..."
        MEMORY.write_text(MEMORY_TEMPLATE.format(api_key=key), encoding="utf-8")
        print(f"[arc] wrote {MEMORY}.")
        print("[arc] note: memory.md contains your API key. Do not share or commit it.")
    repl()

if __name__ == "__main__":
    main()
