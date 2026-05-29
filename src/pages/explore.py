import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import altair as alt
import pandas as pd
import streamlit as st
from streamlit_tree_select import tree_select

from src.client import (
    MtConnectClientConfiguration,
    MtConnectDevice,
    MtConnectExplorer,
    MtConnectObservation,
)

_log = logging.getLogger(__name__)

_CONTAINER_TAGS = frozenset({"Components", "DataItems", "Composition", "References"})
_SKIP_TAGS = frozenset({
    "Description", "Configuration", "Specifications", "Definition",
    "InitializeUnit", "Filters", "Constraints",
})

# Injected into the tree-select iframe on every remount.
# 1. Overrides the component's default gray text with the parent page's body color.
# 2. After React finishes rendering, scrolls the outer st.container to show the
#    checked node (walks offsetParent to get absolute position, then finds the
#    scrollable ancestor in the parent page and sets scrollTop).
_TREE_FIX_JS = """<script>
(function() {
    var STYLE_ID = 'st-tree-color-fix';

    function scrollToChecked(d, parentDoc) {
        var attempts = 0;
        function tryScroll() {
            attempts++;
            try {
                var checked = d.querySelector('input[type="checkbox"]:checked');
                if (!checked) {
                    if (attempts < 15) setTimeout(tryScroll, 100);
                    return;
                }
                var treeFrame = null;
                parentDoc.querySelectorAll('iframe').forEach(function(f) {
                    try { if (f.contentDocument === d) treeFrame = f; } catch(e) {}
                });
                if (!treeFrame) { if (attempts < 15) setTimeout(tryScroll, 100); return; }

                var container = treeFrame.parentElement;
                while (container && container !== parentDoc.body) {
                    var cs = parentDoc.defaultView.getComputedStyle(container);
                    if (container.scrollHeight > container.clientHeight &&
                        (cs.overflowY === 'auto' || cs.overflowY === 'scroll')) break;
                    container = container.parentElement;
                }
                if (!container || container === parentDoc.body) {
                    if (attempts < 15) setTimeout(tryScroll, 100);
                    return;
                }

                // getBoundingClientRect gives viewport-relative coords.
                // itemRect is relative to the iframe's viewport;
                // frameRect is relative to the parent document's viewport.
                // Adding them gives the item's position in the parent viewport,
                // then subtracting containerRect.top gives offset from container top.
                var itemRect = checked.getBoundingClientRect();
                var frameRect = treeFrame.getBoundingClientRect();
                var containerRect = container.getBoundingClientRect();
                var relativeToContainer = (frameRect.top + itemRect.top) - containerRect.top;
                container.scrollTop += relativeToContainer - container.clientHeight / 3;
            } catch(e) {
                if (attempts < 15) setTimeout(tryScroll, 100);
            }
        }
        setTimeout(tryScroll, 150);
    }

    setInterval(function() {
        try {
            var parentDoc = window.parent.document;
            parentDoc.querySelectorAll('iframe').forEach(function(f) {
                try {
                    var d = f.contentDocument;
                    if (!d || d.getElementById(STYLE_ID)) return;
                    if (!d.querySelector('.react-checkbox-tree')) return;
                    var color = window.parent.getComputedStyle(
                        window.parent.document.body).color;
                    var s = d.createElement('style');
                    s.id = STYLE_ID;
                    s.textContent =
                        '.react-checkbox-tree, .react-checkbox-tree label, ' +
                        '.rct-title { color: ' + color + ' !important; }';
                    d.head.appendChild(s);
                    scrollToChecked(d, parentDoc);
                } catch(e) {}
            });
        } catch(e) {}
    }, 200);
})();
</script>"""

_HIST_UNITS = ["seconds", "minutes", "hours", "days"]


def _fmt_local_ts(ts_str: str) -> str:
    """Parse a UTC ISO timestamp string (from MTConnect) and return local time."""
    if not ts_str or ts_str == "—":
        return ts_str
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ts_str


@dataclass
class TreeNode:
    id: str
    label: str
    tag: str
    attributes: Dict[str, str]
    children: List["TreeNode"] = field(default_factory=list)
    is_data_item: bool = False


def _parse_element(el: ET.Element, path: str = "") -> Optional[TreeNode]:
    tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
    if tag in _SKIP_TAGS:
        return None

    attrs = el.attrib
    label = attrs.get("name") or attrs.get("id") or attrs.get("uuid") or tag
    elem_id = attrs.get("id", "") or f"{path}/{label}".lstrip("/")
    node_path = f"{path}/{label}".lstrip("/")

    children: List[TreeNode] = []
    for child_el in el:
        child_tag = child_el.tag.split("}")[-1] if "}" in child_el.tag else child_el.tag
        if child_tag in _CONTAINER_TAGS:
            for sub in child_el:
                child_node = _parse_element(sub, node_path)
                if child_node:
                    children.append(child_node)
        elif child_tag not in _SKIP_TAGS:
            child_node = _parse_element(child_el, node_path)
            if child_node:
                children.append(child_node)

    return TreeNode(
        id=elem_id,
        label=label,
        tag=tag,
        attributes=dict(attrs),
        children=children,
        is_data_item=(tag == "DataItem"),
    )


def _build_trees(devices: List[MtConnectDevice]) -> List[TreeNode]:
    trees = []
    for device in devices:
        try:
            node = _parse_element(ET.fromstring(device.model))
            if node:
                trees.append(node)
        except Exception as ex:
            _log.warning("Failed to parse device tree: %s", ex)
    return trees


def _flatten(node: TreeNode, depth: int = 0) -> List[Tuple[TreeNode, int]]:
    result: List[Tuple[TreeNode, int]] = [(node, depth)]
    for child in node.children:
        result.extend(_flatten(child, depth + 1))
    return result


def _subtree_data_items(node: TreeNode) -> List[TreeNode]:
    result: List[TreeNode] = []
    for child in node.children:
        if child.is_data_item:
            result.append(child)
        else:
            result.extend(_subtree_data_items(child))
    return result


def _to_select_node(node: TreeNode) -> dict:
    entry: dict = {"label": node.label, "value": node.id}
    if node.children:
        entry["children"] = [_to_select_node(c) for c in node.children]
    return entry


def _render_history(history: List[MtConnectObservation], window: timedelta) -> None:
    """Render observation history as a line chart (numeric) or table (non-numeric).
    X-axis spans [now - window, now] so gaps in data are visible."""
    now_utc = datetime.now(tz=timezone.utc)
    x_min_utc = now_utc - window
    try:
        pairs = [(h.timestamp, float(h.text)) for h in history if h.text and h.timestamp >= x_min_utc]
        if not pairs:
            st.caption("No numeric data in history.")
            return
        ts_list, val_list = zip(*pairs)
        df = pd.DataFrame({"timestamp": list(ts_list), "value": list(val_list)})
        # Vega-Lite converts UTC timestamps to browser local time for display,
        # so passing UTC datetimes + UTC domain gives correct local-time labels.
        chart = (
            alt.Chart(df)
            .mark_line(point=True)
            .encode(
                x=alt.X(
                    "timestamp:T",
                    axis=alt.Axis(format="%H:%M:%S", labelAngle=-30, title=None),
                    scale=alt.Scale(domain=[x_min_utc.isoformat(), now_utc.isoformat()]),
                ),
                y=alt.Y("value:Q", axis=alt.Axis(title=None)),
            )
            .properties(height=220)
        )
        st.altair_chart(chart, width="stretch")
    except (ValueError, TypeError):
        recent = [h for h in history if h.timestamp >= x_min_utc][-20:]
        if not recent:
            st.caption("No data in the selected time window.")
            return
        st.table([
            {
                "Timestamp": _fmt_local_ts(h.attributes.get("timestamp", "")),
                "Value": h.text or "",
            }
            for h in reversed(recent)
        ])


def explore_page() -> None:
    st.set_page_config(page_title="MTConnectExplorer", layout="wide")
    st.title("Explore")

    _DEFAULTS = {
        "exp_running": False,
        "exp_explorer": None,
        "exp_selected_id": None,
        "exp_expanded": None,
        "exp_device_filter": None,
        "exp_hist_window_val": 5,
        "exp_hist_window_unit": "minutes",
        "exp_pinned_items": [],
        "exp_pin_window_val": 5,
        "exp_pin_window_unit": "minutes",
        "exp_last_error": None,
        "exp_cfg_base_url": "https://demo.mtconnect.org/",
        "exp_cfg_interval_ms": 1000,
        "exp_cfg_mode": "stream",
    }
    for k, v in _DEFAULTS.items():
        if k not in st.session_state:
            st.session_state[k] = v

    disabled = st.session_state.exp_running

    # --- Configuration ---
    st.subheader("Configuration")
    base_url = st.text_input(
        "Base URL", value=st.session_state.exp_cfg_base_url,
        key="exp_input_base_url", disabled=disabled,
    )
    col1, col2 = st.columns(2)
    with col1:
        interval_ms = st.number_input(
            "Interval (ms)", value=st.session_state.exp_cfg_interval_ms,
            min_value=1, step=1, key="exp_input_interval", disabled=disabled,
        )
    with col2:
        _modes = ["stream", "sample", "current"]
        mode = st.selectbox(
            "Mode", _modes,
            index=_modes.index(st.session_state.exp_cfg_mode),
            key="exp_input_mode", disabled=disabled,
            filter_mode=None,
        )

    col1, col2 = st.columns(2)
    with col1:
        start_clicked = st.button(
            "Start", disabled=disabled, use_container_width=True, type="primary",
        )
    with col2:
        stop_clicked = st.button(
            "Stop", disabled=not disabled, use_container_width=True,
        )

    if start_clicked and not st.session_state.exp_running:
        st.session_state.exp_cfg_base_url = str(base_url)
        st.session_state.exp_cfg_interval_ms = int(interval_ms)
        st.session_state.exp_cfg_mode = str(mode)
        st.session_state.exp_selected_id = None
        st.session_state.exp_expanded = None
        st.session_state.exp_device_filter = None
        st.session_state.exp_last_error = None
        explorer = MtConnectExplorer(
            MtConnectClientConfiguration(base_url=str(base_url)),
            interval_ms=int(interval_ms),
            mode=str(mode),
        ).start()
        st.session_state.exp_explorer = explorer
        st.session_state.exp_running = True
        st.rerun()

    if stop_clicked and st.session_state.exp_running:
        explorer = st.session_state.exp_explorer
        if explorer:
            explorer.stop()
        st.session_state.exp_explorer = None
        st.session_state.exp_running = False
        st.rerun()

    # Auto-stop if explorer thread died
    if st.session_state.exp_running and st.session_state.exp_explorer:
        _explorer: MtConnectExplorer = st.session_state.exp_explorer
        if not _explorer.is_alive:
            st.session_state.exp_last_error = _explorer.error or "Connection lost."
            st.session_state.exp_running = False
            st.session_state.exp_explorer = None
            st.rerun()

    explorer: Optional[MtConnectExplorer] = st.session_state.get("exp_explorer")
    if explorer is None:
        if not st.session_state.exp_running:
            if st.session_state.exp_last_error:
                st.error(f"Connection lost: {st.session_state.exp_last_error}")
            else:
                st.info("Configure the connection above and click **Start** to explore the MTConnect agent.")
        return

    probe_devices, observations = explorer.get_state()

    if not probe_devices:
        st.info("Waiting for probe response…")
        time.sleep(0.3)
        st.rerun()
        return

    trees = _build_trees(probe_devices)

    # Build cross-device lookup for pinned plots (all devices, not just filtered)
    all_id_to_node: Dict[str, TreeNode] = {}
    for t in trees:
        for n, _ in _flatten(t):
            all_id_to_node[n.id] = n

    st.markdown("---")
    st.iframe(_TREE_FIX_JS, height=1)
    tree_col, detail_col = st.columns([2, 3], gap="medium")

    # ---- Tree column ----
    with tree_col:
        st.markdown("**Device Tree**")

        # Device filter dropdown — "Agent" sorted last
        device_labels = sorted(
            (t.label for t in trees),
            key=lambda lbl: (lbl == "Agent", lbl),
        )
        if st.session_state.exp_device_filter not in device_labels:
            st.session_state.exp_device_filter = device_labels[0] if device_labels else None

        selected_device = st.selectbox(
            "Device",
            device_labels,
            index=device_labels.index(st.session_state.exp_device_filter)
            if st.session_state.exp_device_filter in device_labels else 0,
            key="exp_device_select",
            filter_mode=None,
        )
        if selected_device != st.session_state.exp_device_filter:
            st.session_state.exp_device_filter = selected_device
            st.session_state.exp_selected_id = None
            st.session_state.exp_expanded = None

        filtered_trees = [t for t in trees if t.label == selected_device]
        flat: List[Tuple[TreeNode, int]] = []
        for t in filtered_trees:
            flat.extend(_flatten(t, 0))

        id_to_node: Dict[str, TreeNode] = {n.id: n for n, _ in flat}
        select_nodes = [_to_select_node(t) for t in filtered_trees]

        if st.session_state.exp_expanded is None:
            st.session_state.exp_expanded = [n.id for n, _ in flat if not n.is_data_item]

        if st.session_state.exp_selected_id not in id_to_node:
            st.session_state.exp_selected_id = flat[0][0].id if flat else None

        prev_selected = st.session_state.exp_selected_id
        prev_checked = [prev_selected] if prev_selected else []
        tree_key = f"exp_tree_{prev_selected or 'init'}"

        with st.container(height=600, border=False):
            result = tree_select(
                nodes=select_nodes,
                checked=prev_checked,
                no_cascade=True,
                expanded=st.session_state.exp_expanded,
                key=tree_key,
            )

        new_checked: List[str] = result.get("checked", [])
        new_expanded: List[str] = result.get("expanded", [])

        if new_checked != prev_checked:
            added = [x for x in new_checked if x not in prev_checked]
            if added:
                st.session_state.exp_selected_id = added[-1]
            elif new_checked:
                st.session_state.exp_selected_id = new_checked[-1]

        st.session_state.exp_expanded = new_expanded

    # ---- Detail column ----
    with detail_col:
        node = id_to_node.get(st.session_state.exp_selected_id)
        if node is None:
            st.info("Select a node in the tree.")
        else:
            st.markdown(f"**{node.tag}** — `{node.label}`")

            st.markdown("##### Attributes")
            if node.attributes:
                st.table([{"Attribute": k, "Value": v} for k, v in node.attributes.items()])
            else:
                st.caption("No attributes.")

            if node.is_data_item:
                obs: Optional[MtConnectObservation] = observations.get(node.id)
                st.markdown("##### Current Value")
                if obs:
                    mc1, mc2 = st.columns(2)
                    with mc1:
                        st.metric("Value", obs.text or "UNAVAILABLE")
                    with mc2:
                        st.metric("Sequence", obs.attributes.get("sequence", "—"))
                    st.caption(
                        f"Timestamp: {_fmt_local_ts(obs.attributes.get('timestamp', '—'))}"
                    )

                    history = explorer.get_history(node.id)
                    if len(history) > 1:
                        st.markdown("##### History")
                        hc1, hc2 = st.columns([1, 2])
                        with hc1:
                            w_val = st.number_input(
                                "Duration", min_value=1, step=1,
                                value=st.session_state.exp_hist_window_val,
                                key="exp_hist_val",
                            )
                        with hc2:
                            w_unit = st.selectbox(
                                "Unit", _HIST_UNITS,
                                index=_HIST_UNITS.index(st.session_state.exp_hist_window_unit),
                                key="exp_hist_unit",
                                filter_mode=None,
                            )
                        st.session_state.exp_hist_window_val = int(w_val)
                        st.session_state.exp_hist_window_unit = w_unit
                        _render_history(history, timedelta(**{w_unit: int(w_val)}))

                    is_pinned = node.id in st.session_state.exp_pinned_items
                    if st.button(
                        "✓ In Pinned Plots" if is_pinned else "+ Add to Pinned Plots",
                        key="exp_add_pin",
                        disabled=is_pinned,
                    ):
                        st.session_state.exp_pinned_items.append(node.id)
                        st.rerun()
                else:
                    st.caption("No observation received yet.")

            else:
                data_items = _subtree_data_items(node)
                if data_items:
                    st.markdown("##### DataItems")
                    rows = []
                    for di in data_items:
                        di_obs: Optional[MtConnectObservation] = observations.get(di.id)
                        rows.append({
                            "ID": di.id,
                            "Name": di.label,
                            "Type": di.attributes.get("type", ""),
                            "Category": di.attributes.get("category", ""),
                            "Value": (di_obs.text or "UNAVAILABLE") if di_obs else "—",
                            "Timestamp": _fmt_local_ts(
                                di_obs.attributes.get("timestamp", "—")
                            ) if di_obs else "—",
                        })
                    st.dataframe(rows, width="stretch", hide_index=True)

    pinned = st.session_state.exp_pinned_items
    if pinned:
        st.markdown("---")
        st.subheader("Pinned Plots")
        ph1, ph2 = st.columns([1, 2])
        with ph1:
            pin_w_val = st.number_input(
                "Duration", min_value=1, step=1,
                value=st.session_state.exp_pin_window_val,
                key="exp_pin_hist_val",
            )
        with ph2:
            pin_w_unit = st.selectbox(
                "Unit", _HIST_UNITS,
                index=_HIST_UNITS.index(st.session_state.exp_pin_window_unit),
                key="exp_pin_hist_unit",
                filter_mode=None,
            )
        st.session_state.exp_pin_window_val = int(pin_w_val)
        st.session_state.exp_pin_window_unit = pin_w_unit
        window = timedelta(**{pin_w_unit: int(pin_w_val)})
        for item_id in list(pinned):
            pin_node = all_id_to_node.get(item_id)
            label = pin_node.label if pin_node else item_id
            pc1, pc2 = st.columns([5, 1])
            with pc1:
                st.markdown(f"**{label}**" + (f" — {pin_node.tag}" if pin_node else ""))
            with pc2:
                if st.button("Remove", key=f"exp_rm_{item_id}"):
                    st.session_state.exp_pinned_items.remove(item_id)
                    st.rerun()
            pin_history = explorer.get_history(item_id)
            if len(pin_history) > 1:
                _render_history(pin_history, window)
            else:
                st.caption("No history data yet.")

    if st.session_state.exp_running:
        time.sleep(0.5)
        st.rerun()
