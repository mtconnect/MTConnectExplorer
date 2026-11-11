# CLAUDE.md

Project guidance for Claude Code lives in AGENTS.md, which other coding agents read too, and is
loaded by the import below. Make project changes in AGENTS.md; this file holds only the import and
any Claude Code-specific instructions, added below it.

@AGENTS.md

Claude Code: `.claude/settings.json` runs the content check before every Write, Edit, MultiEdit and
NotebookEdit, and blocks a write that would put private data or a transcript into a tracked file (AGENTS.md,
"Privacy and secrets", Enforcement).
