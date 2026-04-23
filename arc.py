import os
import sys
import re
import json
import subprocess
import urllib.request
import urllib.error

ARC_DIR = os.path.dirname(os.path.abspath(__file__))
MEM_PATH = os.path.join(ARC_DIR, "memory.md")
ARC_PATH = os.path.join(ARC_DIR, "arc.py")
API_URL = "https://openrouter.ai/api/v1/chat/completions"
CMD_RE = re.compile(r"^/(\w+)\((.*)\)\s*$", re.MULTILINE)


def _die(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(1)


def read_memory():
    if not os.path.isfile(MEM_PATH):
        _die(f"memory.md not found in {ARC_DIR}")
    with open(MEM_PATH, "r", encoding="utf-8") as f:
        text = f.read()

    core_marker = "## Core"
    scratch_marker = "## Scratch"
    ci = text.find(core_marker)
    si = text.find(scratch_marker)
    if ci < 0:
        _die("memory.md missing '## Core' heading")
    if si < 0:
        _die("memory.md missing '## Scratch' heading")
    if si < ci:
        _die("memory.md has '## Scratch' before '## Core'")

    core = text[ci + len(core_marker):si].lstrip("\n")
    scratch = text[si + len(scratch_marker):].lstrip("\n")

    cfg = {"api_key": "", "model": "", "auto_approve": []}
    for line in core.splitlines():
        if line.strip() == "":
            break
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if k == "auto_approve":
            try:
                parsed = json.loads(v) if v else []
                if not isinstance(parsed, list):
                    raise ValueError("auto_approve must be a JSON array")
                cfg[k] = parsed
            except (json.JSONDecodeError, ValueError) as e:
                append_scratch(f"### error\nauto_approve not valid JSON array: {e}\n")
                cfg[k] = []
        else:
            cfg[k] = v

    return core, scratch, cfg


def append_scratch(text):
    with open(MEM_PATH, "r", encoding="utf-8") as f:
        current = f.read()
    if not current.endswith("\n"):
        current += "\n"
    new = current + text
    if not new.endswith("\n"):
        new += "\n"
    tmp = MEM_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    os.replace(tmp, MEM_PATH)


def extract_commands(text):
    out = []
    for m in CMD_RE.finditer(text):
        name = m.group(1)
        inner = m.group(2)
        raw = m.group(0)
        try:
            args = json.loads("[" + inner + "]")
        except json.JSONDecodeError as e:
            out.append((name, None, f"{raw} -> {e}"))
            continue
        out.append((name, args, raw))
    return out


def api_call(core, scratch, cfg):
    body = json.dumps({
        "model": cfg.get("model", ""),
        "messages": [
            {"role": "system", "content": core},
            {"role": "user", "content": scratch},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {cfg.get('api_key', '')}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    return data["choices"][0]["message"]["content"]


def confirm_shell(cmd, cfg):
    for pat in cfg.get("auto_approve", []) or []:
        try:
            if re.search(pat, cmd):
                return True
        except re.error:
            continue
    sys.stdout.write(f"/shell: {cmd}\n[y]/n> ")
    sys.stdout.flush()
    try:
        line = input()
    except (EOFError, KeyboardInterrupt):
        return False
    return line.strip() == "" or line.strip().lower() == "y"


def run_shell(cmd, user_typed, cfg):
    if not user_typed and not confirm_shell(cmd, cfg):
        append_scratch(f"### tool:/shell [aborted]\ncmd: {cmd}\n")
        return
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=ARC_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        rc = proc.returncode
        out = proc.stdout or ""
        err = proc.stderr or ""
    except Exception as e:
        append_scratch(
            f"### tool:/shell\ncmd: {cmd}\nexit_code: -1\nstdout:\n\nstderr:\n{type(e).__name__}: {e}\n"
        )
        return
    append_scratch(
        f"### tool:/shell\ncmd: {cmd}\nexit_code: {rc}\nstdout:\n{out}\nstderr:\n{err}\n"
    )


def do_restart():
    try:
        with open(ARC_PATH, "rb") as f:
            src = f.read()
        compile(src, "arc.py", "exec")
    except SyntaxError as e:
        append_scratch(f"### error\nSyntaxError on /restart: {e}\n")
        return
    except OSError as e:
        append_scratch(f"### error\nfailed to read arc.py on /restart: {e}\n")
        return
    os.execv(sys.executable, [sys.executable, ARC_PATH])


REGISTRY = {
    "shell": lambda args, ut, cfg: run_shell(args[0] if args else "", ut, cfg),
    "restart": lambda args, ut, cfg: do_restart(),
}


def dispatch(name, args, raw, user_typed, cfg):
    if args is None:
        append_scratch(f"### error\nbad args for /{name}: {raw}\n")
        return
    fn = REGISTRY.get(name)
    if fn is None:
        append_scratch(f"### error\nunknown command: /{name}\n")
        return
    try:
        fn(args, user_typed, cfg)
    except Exception as e:
        append_scratch(f"### error\nhandler for /{name} raised {type(e).__name__}: {e}\n")


def stdin_subloop(cfg):
    while True:
        line = input()
        append_scratch(f"### user\n{line}\n")
        m = CMD_RE.match(line)
        if not m:
            return
        name = m.group(1)
        inner = m.group(2)
        try:
            args = json.loads("[" + inner + "]")
        except json.JSONDecodeError as e:
            append_scratch(f"### error\nbad args for /{name}: {line} -> {e}\n")
            continue
        dispatch(name, args, line, user_typed=True, cfg=cfg)


def main():
    try:
        while True:
            core, scratch, cfg = read_memory()
            try:
                response = api_call(core, scratch, cfg)
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, json.JSONDecodeError, KeyError, ValueError) as e:
                append_scratch(f"### error\n{type(e).__name__}: {e}\n")
                stdin_subloop(cfg)
                continue
            append_scratch(f"### assistant\n{response}\n")
            cmds = extract_commands(response)
            if not cmds:
                stdin_subloop(cfg)
                continue
            for name, args, raw in cmds:
                dispatch(name, args, raw, user_typed=False, cfg=cfg)
    except (KeyboardInterrupt, EOFError):
        sys.exit(0)


if __name__ == "__main__":
    main()
