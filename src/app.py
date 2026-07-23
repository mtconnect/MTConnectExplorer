import sys
from pathlib import Path

# Allow `from src.*` imports when launched via `streamlit run src/app.py` from project root.
sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st

from src.pages.datasets import datasets_page
from src.pages.capture import capture_page
from src.pages.explore import explore_page
from src.pages.play import play_page
from src.pages.simulator import simulator_page

pg = st.navigation([
    st.Page(explore_page, title="Explore"),
    st.Page(capture_page, title="Capture"),
    st.Page(datasets_page, title="Datasets"),
    st.Page(play_page, title="Play"),
    st.Page(simulator_page, title="Simulate"),
])
pg.run()
