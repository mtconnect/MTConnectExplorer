"""The privacy and recording-knowledge patterns, shared by the tests and the content check.

One source for the rules in AGENTS.md ("Privacy and secrets", "Recording knowledge"):
- tests/test_privacy.py and tests/test_agent_docs.py scan the repository with them (CI runs both);
- .github/scripts/check-content.py uses them as the Claude Code PreToolUse hook (blocks a write that would put a match
  into a tracked file) and as the opt-in pre-commit hook (.githooks/pre-commit, scans the staged files).

Standard library only, so the hook runs on a bare python3 without pytest. A legitimate hit goes in ALLOWED (privacy)
or ALLOWED_TRANSCRIPT (recording knowledge) with a reason. Never weaken a pattern to silence one.
"""

from __future__ import annotations

import getpass
import os
import re
import socket
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files that spell the patterns themselves (this module) or their test samples: the privacy scan skips them.
# Keep this list minimal: a skipped file is unscanned. check-content.py is not here because it scans clean.
SPELLS_PATTERNS = {"tests/privacy_rules.py", "tests/test_privacy.py"}

# Files allowed to name people and link personal public repos (deliberate public attribution).
ATTRIBUTION_FILES = {"README.md", "LICENSE.TXT", "NOTICE"}

# (repo-relative path, check name, exact matched text) -> reason it is acceptable.
ALLOWED: dict[tuple[str, str, str], str] = {}

ALLOWED_EMAIL_DOMAINS = re.compile(r"@(?:[\w-]+\.)*example\.(?:com|org|net)$", re.IGNORECASE)
ALLOWED_EMAILS = {
    "git@github.com",  # SSH remote form (git@github.com:org/repo.git), not a person
}

# A literal assigned to a key/secret/password-like name is fine only if it is an obvious
# placeholder, or a fake value inside tests/.
PLACEHOLDER = re.compile(r"^<[^<>]+>$")
FAKE_TEST_VALUE = re.compile(r"(?i)\b(?:test|fake|dummy|example)\b|^test-")

CHECKS: dict[str, re.Pattern[str]] = {
    "windows-abs-path": re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:(?:\\|/)(?=[A-Za-z0-9_.~$%-])"),
    "unc-path": re.compile(r"(?<![A-Za-z0-9_\\])\\\\[A-Za-z0-9_.-]+\\[A-Za-z0-9_$.-]+"),
    "unix-home-path": re.compile(
        r"(?<![A-Za-z0-9_.~-])/(?:home|Users|root)/[A-Za-z0-9_.-]+"
        r"|(?<![A-Za-z0-9_.~-])/mnt/[a-z]/"
    ),
    # An email that starts inside a url-credentials match (scheme://user:password@host) is reported by that check
    # alone; see _violations. mailto: and "Key:addr" forms are still emails.
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    "token": re.compile(
        r"\bgh[pousr]_[A-Za-z0-9]{36,}"  # GitHub tokens
        r"|\bgithub_pat_[A-Za-z0-9_]{22,}"
        r"|\bglpat-[A-Za-z0-9_-]{20,}"  # GitLab
        r"|\bAKIA[0-9A-Z]{16}\b"  # AWS access key id
        r"|\bsk-[A-Za-z0-9_-]{20,}"  # OpenAI / Anthropic style
        r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"  # Slack
        r"|\bAIza[0-9A-Za-z_-]{35}"  # Google API key
        r"|\b[rs]k_live_[0-9A-Za-z]{20,}"  # Stripe live key
        r"|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"  # JSON web token
    ),
    # A password inside a URL (scheme://user:password@host), the agent-URL rule in AGENTS.md.
    "url-credentials": re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@'\"<>]+:([^\s/@'\"<>]+)@"),
    "secret-literal": re.compile(
        r"""(?i)\b(?:api[_-]?key|secret|password|passwd|pwd|token|auth[_-]?token|access[_-]?key)"""
        r"""["']?\s*[:=]\s*["']([^"'\s]{4,})["']"""
    ),
}


def _redact(s: str) -> str:
    return s if len(s) <= 6 else f"{s[:4]}…({len(s)} chars)"


def _local_identity() -> dict[str, re.Pattern[str]]:
    """Patterns for this machine's home dir, login, and hostname, built at runtime."""
    found: dict[str, re.Pattern[str]] = {}
    home = str(Path.home())
    if len(home) > 3:
        variants = {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}
        found["local-home-dir"] = re.compile(
            "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True)), re.IGNORECASE
        )
    if os.environ.get("CI"):  # CI runner identities ("runner", random hosts) are not personal
        return found
    try:
        login = getpass.getuser()
    except Exception:
        login = ""
    if len(login) >= 4:
        found["local-login"] = re.compile(rf"(?<![A-Za-z0-9]){re.escape(login)}(?![A-Za-z0-9])", re.I)
    for key in ("user.name", "user.email"):  # git's identity, as commits record it
        proc = subprocess.run(["git", "config", "--get", key], cwd=REPO_ROOT, capture_output=True, text=True)
        value = proc.stdout.strip()
        if len(value) >= 4 and "noreply" not in value.lower():
            found[f"local-git-{key.split('.')[1]}"] = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", re.I
            )
    host = socket.gethostname().split(".")[0]
    if len(host) >= 4 and host.lower() != "localhost":
        found["local-hostname"] = re.compile(rf"(?<![A-Za-z0-9]){re.escape(host)}(?![A-Za-z0-9])", re.I)
    return found


def _violations(path: str, text: str, identity: dict[str, re.Pattern[str]]) -> list[str]:
    hits: list[str] = []
    checks = dict(CHECKS)
    for name, pat in identity.items():
        if name in ("local-login", "local-git-name") and path in ATTRIBUTION_FILES:
            continue  # a login may legitimately equal a cited author's surname
        checks[name] = pat
    for lineno, line in enumerate(text.splitlines(), 1):
        url_creds = [m.span() for m in CHECKS["url-credentials"].finditer(line)]
        for name, pat in checks.items():
            for m in pat.finditer(line):
                matched = m.group(0)
                if name == "email" and (
                    ALLOWED_EMAIL_DOMAINS.search(matched)
                    or matched.lower() in ALLOWED_EMAILS
                    or any(start <= m.start() < end for start, end in url_creds)
                ):
                    continue
                if name == "url-credentials" and m.group(1).startswith(("<", "$", "{", "%")):
                    continue
                if name == "secret-literal":
                    value = m.group(1)
                    if PLACEHOLDER.match(value):
                        continue
                    if path.startswith("tests/") and FAKE_TEST_VALUE.search(value):
                        continue
                if (path, name, matched) in ALLOWED:
                    continue
                hits.append(f"{path}:{lineno}: [{name}] {_redact(matched)}")
    return hits


# --- Recording knowledge: instructions, not transcripts -------------------------------------------------------------

_S = r"\s+"  # any whitespace, including line breaks
ATTRIBUTION = [
    re.compile(
        rf"\b(?:the{_S})?(?:user|maintainer|owner|customer|client)(?:'s)?{_S}"
        r"(?:said|says|asked|asks|reported|reports|wants|wanted|requested|complained|felt|feels|"
        r"prefers|preferred|insisted|told)\b",
        re.IGNORECASE,
    ),
    re.compile(rf"\b(?:user|customer){_S}feedback\b|\blive{_S}report\b", re.IGNORECASE),
    re.compile(rf"\b(?:per|as){_S}(?:the{_S})?user(?:{_S}(?:asked|requested))?\b", re.IGNORECASE),
    # First-person quotes. Case-sensitive "I" so code and words like "UI" don't match.
    re.compile(rf"(?<![\w`'\"-])I(?:{_S}|'(?:m|ve|d){_S})"
               r"(?:want|wanted|don't|do\s+not|need|prefer|asked|said|think|feel|like|expect)\b"),
    re.compile(rf"\b[Mm]y{_S}(?:preference|feedback|machine|laptop|files?|account|workload)\b"),
]

# (path, matched text) -> reason it is acceptable.
ALLOWED_TRANSCRIPT: dict[tuple[str, str], str] = {}


def is_agent_doc(rel: str) -> bool:
    """The agent-facing docs the transcript rule covers: AGENTS.md, CLAUDE.md, contributing.md and docs/**.md."""
    return rel in ("AGENTS.md", "CLAUDE.md", "contributing.md") or (rel.startswith("docs/") and rel.endswith(".md"))


def transcript_hits(rel: str, text: str) -> list[str]:
    """Guidance attributed to a person or quoted in the first person (patterns allow any whitespace, so a phrase that
    wraps across lines is still caught)."""
    hits = []
    for pat in ATTRIBUTION:
        for m in pat.finditer(text):
            matched = " ".join(m.group(0).split())
            if (rel, matched) in ALLOWED_TRANSCRIPT:
                continue
            hits.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: {matched!r}")
    return hits
