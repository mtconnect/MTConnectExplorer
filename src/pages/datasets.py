import streamlit as st

from src.pages.core import DATASETS_DIR


def datasets_page() -> None:
    st.set_page_config(page_title="MTConnectExplorer", layout="wide")
    st.title("Datasets")
    DATASETS_DIR.mkdir(exist_ok=True)

    files = sorted(DATASETS_DIR.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        st.info("No datasets found in datasets/.")
        return

    h1, h2, h3, h4 = st.columns([5, 2, 1, 1])
    h1.markdown("**Filename**")
    h2.markdown("**Size (MB)**")
    h3.markdown("**Download**")
    h4.markdown("**Delete**")
    st.divider()

    for f in files:
        stat = f.stat()
        c1, c2, c3, c4 = st.columns([5, 2, 1, 1])
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
            if st.button("Delete", key=f"del_{f.name}", type="secondary"):
                f.unlink()
                st.rerun()
