#!/usr/bin/env bash
# Print MTConnectExplorer's single source-of-truth version, validated as semver.
#
#     version=$(bash .github/scripts/read-version.sh)   # -> 0.1.0-alpha.5
#
# WHY THIS EXISTS
#     ci.yml and release.yml each parsed src/__version__.py with
#
#         version=$(grep -oP '"\K[^"]+' src/__version__.py)
#
#     which is UNANCHORED. It works only as long as src/__version__.py contains
#     exactly one quoted string. Add a module docstring — the obvious next edit
#     to that file — and the third quote of the triple-quote opener starts a
#     match, so the docstring's first line comes out AHEAD of the version and
#     the guard fails with a two-line $version. That is not hypothetical: the
#     sibling CellSmith repo shipped the identical snippet, grew a docstring,
#     and its first release PR died on it — in both inlined copies, wrong in
#     the same way. Which is the argument for one shared reader.
#
# Diagnostics go to stderr; ONLY the version goes to stdout, so the caller can
# use $(...) safely.
set -uo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
FILE="$ROOT/src/__version__.py"

die() { echo "::error::$1" >&2; exit 1; }

[ -f "$FILE" ] || die "version file not found: $FILE"

# Anchored to the start of a line, so docstring quotes cannot match.
#
# POSIX `sed`, deliberately NOT `grep -oP`: PCRE grep is not portable. Git Bash
# on Windows refuses it outright — "grep: -P supports only unibyte and UTF-8
# locales" — which would make this reader untestable on a dev machine even
# though it works on the runner. BRE costs nothing here.
mapfile -t matches < <(
    sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$FILE")

case "${#matches[@]}" in
    0) die "no '__version__ = \"...\"' assignment found in $FILE" ;;
    1) ;;
    # Ambiguity is a hard error, not first-match-wins: anything that execs the
    # file keeps the LAST assignment and a text parser keeps the FIRST, so a
    # duplicate would silently decouple the git tag from the shipped version.
    *) die "expected exactly one __version__ assignment in $FILE, found ${#matches[@]}: ${matches[*]}" ;;
esac

version="${matches[0]}"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
    die "__version__ '$version' is not valid semver (expected MAJOR.MINOR.PATCH[-PRERELEASE])"
fi

printf '%s\n' "$version"
