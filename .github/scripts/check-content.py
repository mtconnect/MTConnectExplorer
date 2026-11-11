#!/usr/bin/env python3
"""Keep secrets, personal data and transcripts out of the repository (AGENTS.md, "Privacy and secrets" and
"Recording knowledge").

The patterns live in tests/privacy_rules.py, shared with tests/test_privacy.py and tests/test_agent_docs.py (which CI
runs). This script applies them in three places:
  --hook    Claude Code PreToolUse hook (.claude/settings.json, on Write/Edit/MultiEdit/NotebookEdit): reads the hook's
            JSON on stdin and BLOCKS (exit 2) a write that would put a match into a tracked file of this repository.
            It fails closed: an error inside the check also exits 2, and the settings.json command turns any other
            failure (no python3, an import error) into exit 2;
  --staged  the opt-in pre-commit hook (.githooks/pre-commit; enable per clone with
            `git config core.hooksPath .githooks`): scans the staged versions;
  (none)    scans every file git would commit, as the tests do.
Matched text is redacted in the report. Standard library only. Image metadata and the must-stay-ignored paths are
checked by tests/test_privacy.py only.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.privacy_rules import SPELLS_PATTERNS, _local_identity, _violations, is_agent_doc, transcript_hits  # noqa: E402

GUIDANCE = ("See AGENTS.md, 'Privacy and secrets': write placeholders (<user>, <host>, <your-api-key>), repo-relative "
            "paths and example.com addresses; a real secret that reached a tracked file must be revoked or rotated, so "
            "tell the user. For transcripts, see 'Recording knowledge': restate the guidance as an instruction. A "
            "reviewed false positive goes into ALLOWED / ALLOWED_TRANSCRIPT in tests/privacy_rules.py with a reason.")


def git(*args, cwd=ROOT):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)


def findings(rel, text, identity):
    if rel in SPELLS_PATTERNS:
        return []
    found = _violations(rel, text, identity)
    if is_agent_doc(rel):
        found += [f"{hit} [transcript]" for hit in transcript_hits(rel, text)]
    return found


def decode(data):
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def check_repo(staged):
    identity = _local_identity()
    if staged:
        names = git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR").stdout.decode().split("\0")
    else:
        names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout.decode().split("\0")
    hits = []
    for rel in filter(None, names):
        if staged:
            data = git("show", f":{rel}").stdout
        else:
            path = ROOT / rel
            if not path.is_file():
                continue
            data = path.read_bytes()
        text = decode(data)
        if text is not None:
            hits += findings(rel, text, identity)
    if hits:
        print("Content check FAILED: private data or transcripts in files git would commit:", file=sys.stderr)
        print("  " + "\n  ".join(hits), file=sys.stderr)
        print(GUIDANCE, file=sys.stderr)
        return 1
    print("Content check passed.")
    return 0


def check_hook():
    event = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    tool_input = event.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not target:
        return 0
    path = Path(event.get("cwd") or os.getcwd(), target).resolve()
    parent = next((p for p in path.parents if p.is_dir()), None)
    top = git("rev-parse", "--show-toplevel", cwd=parent).stdout.decode().strip() if parent else ""
    if not top or Path(top).resolve() != ROOT.resolve():
        return 0  # outside this repository: its own rules apply
    rel = path.relative_to(ROOT.resolve()).as_posix()
    if git("check-ignore", "-q", "--no-index", rel).returncode == 0:
        return 0  # gitignored (working/, config.json, datasets/): local material may live there
    texts = [tool_input.get(key) for key in ("content", "new_string", "new_source")]
    texts += [e.get("new_string") for e in tool_input.get("edits") or [] if isinstance(e, dict)]
    text = "\n".join(t for t in texts if isinstance(t, str))
    hits = findings(rel, text, _local_identity())
    if not hits:
        return 0
    print(f"Blocked by the content check: this write would put private data or a transcript into the tracked file "
          f"{rel}:", file=sys.stderr)
    print("  " + "\n  ".join(hits), file=sys.stderr)
    print(GUIDANCE + " If you believe it's a false positive, stop and ask the user.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    # Windows' console (cp1252) can't encode every excerpt; a crash would exit 1, which does NOT block the write.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    if "--hook" in sys.argv[1:]:
        # Fail closed: Claude Code blocks a write only on exit 2, so an unexpected error must not let it through.
        try:
            sys.exit(check_hook())
        except Exception as exc:  # noqa: BLE001
            print(f"The content check failed ({type(exc).__name__}: {exc}), so this write is blocked. Fix the "
                  "check (.github/scripts/check-content.py) rather than bypassing it.", file=sys.stderr)
            sys.exit(2)
    sys.exit(check_repo(staged="--staged" in sys.argv[1:]))
