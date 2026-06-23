import shutil

import streamlit as st

from src.pages.core import (
    DATASETS_DIR,
    get_capture_state,
    get_datasets_state,
    get_play_state,
)

# Maximum size (MB) of a single uploaded dataset. Must not exceed the server's
# server.maxUploadSize (set to 5 GB in .streamlit/config.toml and src/main.py).
_MAX_UPLOAD_MB = 5120


def _dataset_status() -> dict:
    """Map dataset filename -> status label ('Playing', 'Capturing', or both)
    for datasets currently in use by a play or capture session."""
    play_state = get_play_state()
    capture_state = get_capture_state()
    kinds: dict = {}
    for s in play_state.sessions.values():
        kinds.setdefault(s.dataset_file, set()).add("Playing")
    for s in capture_state.sessions.values():
        kinds.setdefault(s.filename, set()).add("Capturing")
    return {name: " & ".join(sorted(k)) for name, k in kinds.items()}


_EXT = ".jsonl"
_INVALID_NAME_CHARS = set('<>:"/\\|?*')


@st.dialog("Rename dataset")
def _rename_dialog(filename: str) -> None:
    """Modal to rename a dataset's stem; the .jsonl extension is preserved."""
    stem = filename[: -len(_EXT)] if filename.lower().endswith(_EXT) else filename
    st.write(f"Renaming **{filename}**")
    new_stem = st.text_input(
        "New name", value=stem, key=f"ds_rename_input_{filename}",
    ).strip()
    st.caption(f"New filename: {new_stem}{_EXT}" if new_stem else "Enter a name.")

    c1, c2 = st.columns(2)
    cancel = c1.button("Cancel", use_container_width=True, key=f"ds_rename_cancel_{filename}")
    rename = c2.button(
        "Rename", type="primary", use_container_width=True, key=f"ds_rename_confirm_{filename}",
    )

    if cancel:
        st.rerun()
    if rename:
        target_name = f"{new_stem}{_EXT}"
        if not new_stem:
            st.error("Name cannot be empty.")
        elif any(ch in _INVALID_NAME_CHARS for ch in new_stem):
            st.error('Name cannot contain any of: < > : " / \\ | ? *')
        elif target_name == filename:
            st.rerun()  # No change.
        elif (DATASETS_DIR / target_name).exists():
            st.error("A dataset with that name already exists.")
        elif filename in _dataset_status():
            st.error("This dataset is now in use and cannot be renamed.")
        else:
            (DATASETS_DIR / filename).rename(DATASETS_DIR / target_name)
            st.rerun()


def datasets_page() -> None:
    st.set_page_config(page_title="MTConnectExplorer", layout="wide")
    st.title("Datasets")
    get_datasets_state()
    DATASETS_DIR.mkdir(exist_ok=True)

    status_map = _dataset_status()

    # --- Upload (drag-and-drop onto the area, or browse) ---
    if "ds_upload_gen" not in st.session_state:
        st.session_state.ds_upload_gen = 0
    uploaded = st.file_uploader(
        "Upload datasets (drag & drop or browse)",
        type=["jsonl"],
        accept_multiple_files=True,
        max_upload_size=_MAX_UPLOAD_MB,
        key=f"ds_upload_{st.session_state.ds_upload_gen}",
    )
    if uploaded:
        saved, skipped = [], []
        for uf in uploaded:
            if uf.name in status_map:
                skipped.append(uf.name)
                continue
            # Stream to disk to avoid holding a second full copy in memory.
            uf.seek(0)
            with open(DATASETS_DIR / uf.name, "wb") as out:
                shutil.copyfileobj(uf, out)
            saved.append(uf.name)
        for name in skipped:
            st.warning(f"Skipped '{name}': a dataset with that name is currently in use.")
        if saved:
            st.success(f"Uploaded: {', '.join(saved)}")
            # Bump the key so the uploader resets and files aren't re-saved on rerun.
            st.session_state.ds_upload_gen += 1
            st.rerun()

    st.divider()

    files = sorted(DATASETS_DIR.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        st.info("No datasets found in datasets/.")
        return

    h1, h2, h3, h4, h5, h6 = st.columns([4, 1, 1, 1, 1, 2])
    h1.markdown("**Filename**")
    h2.markdown("**Size (MB)**")
    h3.markdown("**Download**")
    h4.markdown("**Rename**")
    h5.markdown("**Delete**")
    h6.markdown("**Status**")
    st.divider()

    for f in files:
        stat = f.stat()
        status = status_map.get(f.name)
        busy = status is not None
        c1, c2, c3, c4, c5, c6 = st.columns([4, 1, 1, 1, 1, 2])
        c1.write(f.name)
        c2.write(round(stat.st_size / (1024 * 1024), 1))
        with c3:
            st.download_button(
                "Download",
                data=f.read_bytes(),
                file_name=f.name,
                mime="application/octet-stream",
                key=f"dl_{f.name}",
            )
        with c4:
            if st.button(
                "Rename",
                key=f"rn_{f.name}",
                disabled=busy,
                help="Cannot rename while the dataset is being played or captured." if busy else None,
            ):
                _rename_dialog(f.name)
        with c5:
            if st.button(
                "Delete",
                key=f"del_{f.name}",
                type="secondary",
                disabled=busy,
                help="Cannot delete while the dataset is being played or captured." if busy else None,
            ):
                f.unlink()
                st.rerun()
        c6.write(status or "")
