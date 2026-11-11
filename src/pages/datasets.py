import streamlit as st

from mtconnect_explorer_client import ApiError

from src.pages.api import get_client
from src.pages.core import get_datasets_state

# Maximum size (MB) of a single uploaded dataset. Must not exceed the server's
# server.maxUploadSize (set to 5 GB in .streamlit/config.toml and src/main.py).
_MAX_UPLOAD_MB = 5120

_EXT = ".jsonl"


def _status_labels(in_use: list) -> str:
    """Format a dataset's in-use scopes for the Status column ('Playing',
    'Capturing', or 'Capturing & Playing')."""
    labels = {"play": "Playing", "capture": "Capturing"}
    return " & ".join(sorted(labels[u] for u in in_use))


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
    cancel = c1.button("Cancel", width="stretch", key=f"ds_rename_cancel_{filename}")
    rename = c2.button(
        "Rename", type="primary", width="stretch", key=f"ds_rename_confirm_{filename}",
    )

    if cancel:
        st.rerun()
    if rename:
        target_name = f"{new_stem}{_EXT}"
        if not new_stem:
            st.error("Name cannot be empty.")
        elif target_name == filename:
            st.rerun()  # No change.
        else:
            try:
                get_client().datasets_rename(filename, target_name)
            except ApiError as ex:
                if ex.code == "invalid_name":
                    st.error('Name cannot contain any of: < > : " / \\ | ? *')
                elif ex.code == "dataset_exists":
                    st.error("A dataset with that name already exists.")
                elif ex.code == "dataset_in_use":
                    st.error("This dataset is now in use and cannot be renamed.")
                else:
                    st.error(ex.message)
            else:
                st.rerun()


def datasets_page() -> None:
    st.set_page_config(page_title="MTConnectExplorer", layout="wide")
    st.title("Datasets")
    get_datasets_state()
    client = get_client()

    try:
        infos = client.datasets_list()
    except ApiError as ex:
        st.error(f"API error: {ex.message}")
        return
    status_map = {d.name: _status_labels(d.in_use) for d in infos if d.in_use}

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
        saved, failed = [], []
        for uf in uploaded:
            # Stream the Streamlit UploadedFile (a file-like object) through the
            # multipart endpoint — no second full copy in frontend memory.
            uf.seek(0)
            try:
                client.datasets_upload(uf.name, uf)
            except ApiError as ex:
                failed.append((uf.name, ex))
            else:
                saved.append(uf.name)
        for name, ex in failed:
            if ex.code == "dataset_in_use":
                st.warning(f"Skipped '{name}': a dataset with that name is currently in use.")
            elif ex.code == "dataset_exists":
                st.warning(f"Skipped '{name}': a dataset with that name already exists "
                           "(overwriting is not allowed — delete it first).")
            else:
                st.warning(f"Skipped '{name}': {ex.message}")
        if saved:
            st.success(f"Uploaded: {', '.join(saved)}")
            # Bump the key so the uploader resets and files aren't re-saved on rerun.
            st.session_state.ds_upload_gen += 1
            st.rerun()

    st.divider()

    if not infos:
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

    for info in infos:
        status = status_map.get(info.name)
        busy = status is not None
        c1, c2, c3, c4, c5, c6 = st.columns([4, 1, 1, 1, 1, 2])
        c1.write(info.name)
        c2.write(round(info.size_bytes / (1024 * 1024), 1))
        with c3:
            st.download_button(
                "Download",
                data=b"".join(client.datasets_download(info.name)),
                file_name=info.name,
                mime="application/octet-stream",
                key=f"dl_{info.name}",
            )
        with c4:
            if st.button(
                "Rename",
                key=f"rn_{info.name}",
                disabled=busy,
                help="Cannot rename while the dataset is being played or captured." if busy else None,
            ):
                _rename_dialog(info.name)
        with c5:
            if st.button(
                "Delete",
                key=f"del_{info.name}",
                type="secondary",
                disabled=busy,
                help="Cannot delete while the dataset is being played or captured." if busy else None,
            ):
                try:
                    client.datasets_delete(info.name)
                except ApiError as ex:
                    st.error(ex.message)
                else:
                    st.rerun()
        c6.write(status or "")
