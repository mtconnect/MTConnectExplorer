"""Privacy guard: fail if a committable file leaks local identity or secrets.

Enforces the "Privacy and secrets" rules in AGENTS.md. Scans every
tracked file plus every untracked file that is not gitignored (i.e. everything `git add -A`
would stage) for:

- absolute paths (Windows drive, UNC, /home, /Users, /root, /mnt/<drive>, file:///<drive>:),
- email addresses (except the RFC 2606 example domains),
- credential shapes (private keys, well-known token prefixes, key/secret/password literals),
- passwords inside URLs (scheme://user:password@host),
- this machine's home directory, login name, hostname, and git user.name / user.email
  (computed at runtime, so no identity is ever written into this file),
- metadata chunks in images (PNG text/EXIF, JPEG EXIF),

and checks that the sensitive local paths stay gitignored.

The patterns live in tests/privacy_rules.py. A legitimate hit goes in its ALLOWED with a reason.
Never weaken a pattern to silence one.
Matched text is redacted in failure output so a CI log never republishes a leaked secret.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.privacy_rules import (  # the shared patterns (also used by .github/scripts/check-content.py)
    CHECKS,
    SPELLS_PATTERNS,
    _local_identity,
    _violations,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git_files() -> list[str]:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    proc = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if proc.returncode != 0:
        pytest.skip("not a git checkout")
    paths = [p for p in proc.stdout.decode("utf-8").split("\0") if p]
    return [p for p in paths if (REPO_ROOT / p).is_file()]


def _text(path: str) -> str | None:
    data = (REPO_ROOT / path).read_bytes()
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def test_no_private_data_in_committable_files():
    identity = _local_identity()
    violations: list[str] = []
    scanned = 0
    for path in _git_files():
        if path in SPELLS_PATTERNS:
            continue
        text = _text(path)
        if text is None:
            continue
        scanned += 1
        violations += _violations(path, text, identity)
    assert scanned > 0, "privacy scan found no text files to check"
    assert not violations, (
        "Private data in committable files (see 'Privacy and secrets' in AGENTS.md):\n  "
        + "\n  ".join(violations)
    )


def _png_metadata_chunks(data: bytes) -> list[str]:
    chunks, i = [], 8
    while i + 8 <= len(data):
        length = int.from_bytes(data[i : i + 4], "big")
        ctype = data[i + 4 : i + 8].decode("latin-1")
        if ctype in {"tEXt", "iTXt", "zTXt", "eXIf"}:
            chunks.append(ctype)
        if ctype == "IEND":
            break
        i += 12 + length
    return chunks


def test_images_carry_no_metadata():
    problems = []
    for path in _git_files():
        lower = path.lower()
        data = (REPO_ROOT / path).read_bytes()
        if lower.endswith(".png") and data.startswith(b"\x89PNG\r\n\x1a\n"):
            chunks = _png_metadata_chunks(data)
            if chunks:
                problems.append(f"{path}: PNG metadata chunks {sorted(set(chunks))}")
        elif lower.endswith((".jpg", ".jpeg")) and b"Exif\0\0" in data[:65536]:
            problems.append(f"{path}: JPEG EXIF block")
    assert not problems, "Strip image metadata before committing:\n  " + "\n  ".join(problems)


# Representative paths for every kind of local file the rules say must never be committed.
# They need not exist: `git check-ignore --no-index` evaluates the ignore rules alone.
MUST_STAY_IGNORED = [
    "config.json",  # api_key
    ".claude/settings.local.json",  # home path + login in permission entries
    ".env",
    ".env.local",
    "certs/server.pem",
    "certs/server.key",
    "adapter.log",
    "datasets/http_192.0.2.10_5000-2026-01-01T00-00-00Z.jsonl",  # real hostnames, IPs, serials
    "datasets/obsolete/old.jsonl",
    "test/xml-output.jsonl",  # Header sender = this machine's hostname
    "dist/MTConnectExplorer.exe",  # bundled .pyc embed the build machine's home path
    "build/MTConnectExplorer/warn.txt",
    "src/__pycache__/main.cpython-313.pyc",
    "venv/pyvenv.cfg",
    "working/issue-draft.md",  # local drafts may quote logs, paths, or captured data
]


def test_sensitive_paths_stay_gitignored():
    if shutil.which("git") is None:
        pytest.skip("git not available")
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z"],
        cwd=REPO_ROOT,
        input="\0".join(MUST_STAY_IGNORED).encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode not in (0, 1):
        pytest.skip("not a git checkout")
    ignored = {p for p in proc.stdout.decode("utf-8").split("\0") if p}
    missing = [p for p in MUST_STAY_IGNORED if p not in ignored]
    assert not missing, (
        "These must stay gitignored (see 'Privacy and secrets' in AGENTS.md):\n  "
        + "\n  ".join(missing)
    )


LEAK_SAMPLES = [
    ("windows-abs-path", r"C:\Users\someone\proj"),
    ("windows-abs-path", "file:///D:/work/x"),
    ("unc-path", r"\\fileserver\share"),
    ("unix-home-path", "/home/someone/proj"),
    ("unix-home-path", "/Users/someone/proj"),
    ("unix-home-path", "/mnt/c/proj"),
    ("email", "first.last@company.com"),
    ("token", "ghp_" + "a1B2" * 9),
    ("token", "AKIA" + "ABCDEFGHIJKLMNOP"),
    ("secret-literal", 'api_key = "s3cr3t-value"'),
    ("private-key", "-----BEGIN " + "RSA PRIVATE KEY-----"),
    ("token", "sk_" + "live_" + "a1B2" * 6),
    ("token", "eyJ" + "hbGciOiJIUzI1" + ".eyJ" + "zdWIiOiIxMjM0" + ".SflKxwRJSMeKKF2QT4"),
    ("url-credentials", "http://admin:" + "Hunter2Hunter2" + "@192.0.2.10:5000/"),
    ("email", "[mail](mailto:someone" + "@company.com)"),
    ("email", "Contact:someone" + "@company.com"),
    ("email", "contact someone" + "@company.com"),
    ("email", "(someone" + "@company.com)"),
    ('email', '"email": "someone' + '@company.com"'),
    ("email", "https://someone" + "@company.com/"),
]

# The characters an email can follow, each of which must have a sample above ("^" = start of line).
# A narrowed regex (for example a lookbehind) silently stops matching at one of these.
EMAIL_BOUNDARIES = {"^", " ", ":", "/", "(", '"'}
_LOCAL_PART = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-")


@pytest.mark.parametrize(("check", "sample"), LEAK_SAMPLES)
def test_patterns_catch_known_leaks(check, sample):
    hits = _violations("src/x.py", sample, {})
    assert any(f"[{check}]" in hit for hit in hits), f"{check} did not catch {sample!r}: {hits}"


def test_every_check_has_leak_samples():
    sampled = {check for check, _ in LEAK_SAMPLES}
    assert not set(CHECKS) - sampled, f"add a LEAK_SAMPLES entry for {sorted(set(CHECKS) - sampled)}"
    covered = set()
    for check, sample in LEAK_SAMPLES:
        if check != "email":
            continue
        i = sample.index("@")
        while i > 0 and sample[i - 1] in _LOCAL_PART:
            i -= 1
        covered.add(sample[i - 1] if i else "^")
    assert not EMAIL_BOUNDARIES - covered, (
        f"add email samples that follow {sorted(EMAIL_BOUNDARIES - covered)}"
    )


@pytest.mark.parametrize(
    "sample",
    [
        "http://127.0.0.1:5000/ and https://demo.mtconnect.org/",
        r"venv\\Scripts\\python.exe and venv/bin/python",
        "see src/services/auth.py:20",
        "contact user@example.com",
        "git@github.com:org/repo.git",
        'api_key="<your-api-key>"',
        "token: str = secrets.token_urlsafe(32)",
        "${workspaceFolder}/venv/bin/python",
        "http://<user>:<password>@<host>:<port>/",
        "mqtt://app:${BROKER_PASSWORD}@127.0.0.1:1883",
        "mqtt://app:%BROKER_PW%" + "@broker.company.com:1883",  # placeholder password; not an email either
    ],
)
def test_patterns_allow_safe_text(sample):
    assert not _violations("src/x.py", sample, {}), f"false positive on {sample!r}"


# --- The content check's write hook (.github/scripts/check-content.py --hook) ----------------------------------------

def _run_hook(rel: str, content: str) -> subprocess.CompletedProcess:
    import json
    import sys

    event = {"cwd": str(REPO_ROOT), "tool_input": {"file_path": str(REPO_ROOT / rel), "content": content}}
    return subprocess.run([sys.executable, str(REPO_ROOT / ".github" / "scripts" / "check-content.py"), "--hook"],
                          input=json.dumps(event).encode(), capture_output=True, cwd=REPO_ROOT)


def test_hook_blocks_a_secret_in_a_tracked_file():
    result = _run_hook("src/new_module.py", "TOKEN = '" + "ghp_" + "a1B2" * 9 + "'")
    assert result.returncode == 2, result.stderr.decode("utf-8", "replace")
    assert b"Blocked by the content check" in result.stderr
    assert b"a1B2a1B2a1B2" not in result.stderr  # redacted


def test_hook_blocks_a_transcript_in_agent_docs():
    result = _run_hook("AGENTS.md", "The user said to always rebase.")
    assert result.returncode == 2, result.stderr.decode("utf-8", "replace")


def test_hook_fails_closed_on_an_internal_error():
    import sys

    result = subprocess.run([sys.executable, str(REPO_ROOT / ".github" / "scripts" / "check-content.py"), "--hook"],
                            input=b"not json", capture_output=True, cwd=REPO_ROOT)
    assert result.returncode == 2, result.stderr.decode("utf-8", "replace")
    assert b"so this write is blocked" in result.stderr


def test_hook_allows_clean_and_gitignored_writes():
    assert _run_hook("src/new_module.py", "HOST = '127.0.0.1'").returncode == 0
    assert _run_hook("working/notes.md", "first.last" + "@company.com").returncode == 0  # working/ is gitignored

