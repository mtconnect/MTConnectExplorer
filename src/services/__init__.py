"""Streamlit-free domain services.

Nothing under this package may import streamlit: these modules own all domain
state (sessions, ports, datasets, events) as plain module-level singletons
guarded by locks, so the same managers serve the Streamlit frontend and the
FastAPI routes alike.
"""
