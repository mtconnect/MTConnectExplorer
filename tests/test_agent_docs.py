"""Checks on the agent-facing docs (see "Recording knowledge" in AGENTS.md).

- AGENTS.md is the single source of project guidance; CLAUDE.md loads it with an `@AGENTS.md`
  import and repeats none of its sections, so the two can't drift apart.
- AGENTS.md, CLAUDE.md, contributing.md, and docs/ contain instructions, not transcripts: no
  attributions of guidance to a person and no first-person quotes. Patterns allow any whitespace,
  so a phrase that wraps across lines is still caught.

A legitimate hit goes in ALLOWED with a reason; otherwise restate the text as an instruction.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

CORE_SECTIONS = [
    "## Privacy and secrets (mandatory)",
    "## Recording knowledge",
    "## Commands",
    "## Architecture",
    "## Gotchas",
]


def _text(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8").replace("\r\n", "\n")


def _outside_code(text: str) -> str:
    """Text with fenced code blocks and inline code spans removed (import parsing skips both)."""
    text = re.sub(r"^```.*?^```", "", text, flags=re.MULTILINE | re.DOTALL)
    return re.sub(r"`[^`\n]*`", "", text)


def _h2(text: str) -> set[str]:
    return {line.strip() for line in _outside_code(text).splitlines() if line.startswith("## ")}


def test_agents_md_has_core_sections():
    missing = [s for s in CORE_SECTIONS if s not in _h2(_text("AGENTS.md"))]
    assert not missing, f"AGENTS.md is missing sections: {missing}"


def test_claude_md_imports_agents_md():
    lines = [line.strip() for line in _outside_code(_text("CLAUDE.md")).splitlines()]
    assert "@AGENTS.md" in lines, "CLAUDE.md must load the project guidance with an `@AGENTS.md` line"


def test_claude_md_repeats_no_agents_md_section():
    shared = sorted(_h2(_text("CLAUDE.md")) & _h2(_text("AGENTS.md")))
    assert not shared, (
        f"CLAUDE.md repeats AGENTS.md sections {shared}; keep project guidance in AGENTS.md and "
        "only Claude Code-specific notes in CLAUDE.md"
    )


SCANNED = ["AGENTS.md", "CLAUDE.md", "contributing.md", *sorted(
    p.relative_to(REPO_ROOT).as_posix() for p in (REPO_ROOT / "docs").rglob("*.md")
)]

from tests.privacy_rules import ALLOWED_TRANSCRIPT as ALLOWED  # noqa: E402  (shared patterns)
from tests.privacy_rules import ATTRIBUTION  # noqa: E402


@pytest.mark.parametrize("rel", SCANNED)
def test_docs_hold_instructions_not_transcripts(rel):
    path = REPO_ROOT / rel
    if not path.is_file():
        pytest.skip(f"{rel} not present")
    text = _text(rel)
    hits = []
    for pat in ATTRIBUTION:
        for m in pat.finditer(text):
            matched = " ".join(m.group(0).split())
            if (rel, matched) in ALLOWED:
                continue
            hits.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: {matched!r}")
    assert not hits, (
        "Guidance attributed to a person or quoted in the first person; restate it as an "
        "instruction (see 'Recording knowledge' in AGENTS.md):\n  " + "\n  ".join(hits)
    )


@pytest.mark.parametrize(
    "sample",
    [
        "the user\nsaid to always rebase",  # wraps across a line break
        "The maintainer wants fewer prompts.",
        "Per the user, use 127.0.0.1.",
        "user feedback: too slow",
        "I want the agent to stop guessing.",
        "Respect my preference for short replies.",
    ],
)
def test_attribution_patterns_catch(sample):
    assert any(p.search(sample) for p in ATTRIBUTION), sample


@pytest.mark.parametrize(
    "sample",
    [
        "user decision, 2026-11-11",
        "The UI shows the user a dialog.",
        "Users can upload datasets.",
        "If I/O fails, retry once.",
        "The API key is passed by the client library.",
    ],
)
def test_attribution_patterns_allow(sample):
    assert not any(p.search(sample) for p in ATTRIBUTION), sample
