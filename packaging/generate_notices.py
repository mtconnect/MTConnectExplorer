#!/usr/bin/env python3
"""Generate a THIRD_PARTY_NOTICES.txt for the release bundle.

Run with the *build* virtualenv's interpreter after dependencies are installed
(the release workflow does this), e.g.::

    venv/bin/python generate_notices.py THIRD_PARTY_NOTICES.txt        # Linux
    venv\\Scripts\\python.exe generate_notices.py THIRD_PARTY_NOTICES.txt  # Windows

It reads every installed distribution's metadata + bundled license text via
``importlib.metadata`` (no extra dependency), and appends manual entries for the
non-Python components shipped in the release (the MTConnect C++ agent binary and
the vendored three.js). It is intentionally over-inclusive — it lists every
installed distribution — so nothing that PyInstaller may bundle is missed.

Attribution obligations covered:
- Apache-2.0 / MIT / BSD: preserve each component's copyright + license text.
- certifi (MPL-2.0): weak, file-level copyleft — MPL §3.2 only requires telling
  recipients where the source is, so we include the license text plus a source
  URL (no need to ship certifi's source).
"""

import importlib.metadata as im
import sys
from pathlib import Path

_LICENSE_FILENAMES = (
    "LICENSE", "LICENSE.txt", "LICENSE.md", "LICENSE.rst",
    "LICENCE", "LICENCE.txt", "COPYING", "COPYING.txt", "NOTICE", "NOTICE.txt",
)
_SEP = "=" * 80

# Non-Python components bundled into the release artifact.
_BUNDLED = [
    (
        "MTConnect C++ Agent (bundled 'agent' / 'agent.exe' binary)", "",
        "Apache-2.0",
        "Source: https://github.com/mtconnect/cppagent (Apache License 2.0).",
    ),
    (
        "three.js (src/static/three.min.js, src/static/OrbitControls.js)", "r137",
        "MIT",
        "Copyright 2010-2022 three.js Authors. "
        "Source: https://github.com/mrdoob/three.js (MIT License).",
    ),
]


def _license_id(meta) -> str:
    expr = meta.get("License-Expression")  # PEP 639 SPDX expression (preferred)
    if expr:
        return expr.strip()
    classifiers = [
        c.split("::")[-1].strip()
        for c in (meta.get_all("Classifier") or [])
        if c.startswith("License ")
    ]
    if classifiers:
        return "; ".join(classifiers)
    lic = (meta.get("License") or "").strip()
    return lic.splitlines()[0][:60] if lic else "(see text below / upstream)"


def _license_text(dist) -> str:
    # Wheels usually ship the license under the dist-info (sometimes licenses/).
    candidates = list(_LICENSE_FILENAMES) + [f"licenses/{n}" for n in _LICENSE_FILENAMES]
    for name in candidates:
        try:
            text = dist.read_text(name)
        except Exception:
            text = None
        if text and text.strip():
            return text.strip()
    # Fall back: scan declared files for a license-named file.
    for f in (dist.files or []):
        if f.name.lower().startswith(("license", "licence", "copying")):
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                try:
                    text = Path(f.locate()).read_text(encoding="utf-8", errors="replace")
                except Exception:
                    text = None
            if text and text.strip():
                return text.strip()
    # Last resort: some packages inline the whole license in the License field.
    lic = (dist.metadata.get("License") or "").strip()
    return lic if len(lic) > 80 else ""


def _notes_for(name: str, version: str) -> str:
    if name.lower() == "certifi":
        return (
            "certifi is licensed under MPL-2.0 (weak, file-level copyleft). Its "
            "source is available unmodified at "
            f"https://github.com/certifi/python-certifi (release {version})."
        )
    return ""


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("THIRD_PARTY_NOTICES.txt")

    parts = [
        "THIRD-PARTY NOTICES",
        "",
        "MTConnectExplorer is distributed under the Apache License 2.0. It bundles",
        "the third-party components listed below, each under its own license,",
        "reproduced here to satisfy their attribution requirements. Generated from",
        "the pinned build environment.",
        "",
        _SEP,
    ]

    dists = sorted(im.distributions(), key=lambda d: (d.metadata["Name"] or "").lower())
    for dist in dists:
        meta = dist.metadata
        name, version = meta["Name"], meta["Version"]
        block = [f"{name} {version}", f"License: {_license_id(meta)}"]
        note = _notes_for(name, version)
        if note:
            block.append(note)
        text = _license_text(dist)
        if text:
            block.append("")
            block.append(text)
        parts.append("\n".join(block))
        parts.append(_SEP)

    for name, version, license_id, note in _BUNDLED:
        header = f"{name} {version}".rstrip()
        parts.append("\n".join([header, f"License: {license_id}", note]))
        parts.append(_SEP)

    out.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(
        f"Wrote {out} - {len(dists)} Python distributions "
        f"+ {len(_BUNDLED)} bundled components."
    )


if __name__ == "__main__":
    main()
