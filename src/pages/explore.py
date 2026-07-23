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
from src.pages.core import (
    GlobalState,
    get_explore_state,
    get_global_state,
    get_state_lock,
    running_agent_urls,
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

# Cap on points drawn per chart. A high-rate DataItem over a long window can hold
# far more samples than are worth rendering (and Altair errors past ~5000 rows),
# so history is decimated to this many points for display; the X domain still
# spans the full window, so the plot looks filled.
_MAX_PLOT_POINTS = 2000


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


def _filter_tree(node: TreeNode, query: str) -> Optional[TreeNode]:
    """Return a pruned copy of node keeping only branches that match query on id
    or name (case-insensitive). Ancestors of matches are retained so the path is
    visible; if a node itself matches, its full subtree is kept. Returns None when
    nothing in the subtree matches."""
    self_match = query in node.label.lower() or query in node.id.lower()
    filtered_children = [
        fc for fc in (_filter_tree(c, query) for c in node.children) if fc is not None
    ]
    if not self_match and not filtered_children:
        return None
    return TreeNode(
        id=node.id,
        label=node.label,
        tag=node.tag,
        attributes=node.attributes,
        children=node.children if self_match else filtered_children,
        is_data_item=node.is_data_item,
    )


def _to_select_node(node: TreeNode) -> dict:
    entry: dict = {"label": node.label, "value": node.id}
    if node.children:
        entry["children"] = [_to_select_node(c) for c in node.children]
    return entry


def _render_history(
    history: List[MtConnectObservation],
    window: timedelta,
    x_max: Optional[datetime] = None,
) -> None:
    """Render observation history as a line chart (numeric) or table (non-numeric).
    The X-axis spans [x_max - window, x_max]. When ``x_max`` is given (e.g. the
    current time, shared across plots), the right edge tracks it even while a
    DataItem receives no new data; otherwise it falls back to the most recent
    observation's timestamp so replayed/historical data is still visible."""
    x_max_utc = x_max if x_max is not None else max(
        (h.timestamp for h in history),
        default=datetime.now(tz=timezone.utc),
    )
    x_min_utc = x_max_utc - window
    try:
        pairs = [(h.timestamp, float(h.text)) for h in history if h.text and h.timestamp >= x_min_utc]
        if not pairs:
            st.caption("No numeric data in history.")
            return
        if len(pairs) > _MAX_PLOT_POINTS:
            # Uniform decimation, always keeping the most recent sample.
            stride = -(-len(pairs) // _MAX_PLOT_POINTS)  # ceil division
            sampled = pairs[::stride]
            if sampled[-1] is not pairs[-1]:
                sampled.append(pairs[-1])
            pairs = sampled
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
                    scale=alt.Scale(domain=[x_min_utc.isoformat(), x_max_utc.isoformat()]),
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

    global_state = get_global_state()
    state = get_explore_state()

    disabled = state.running

    # --- Configuration (kept narrow; the rest of the page stays wide) ---
    config_col = st.columns([1, 2, 1])[1]
    with config_col:
        st.subheader("Configuration")
        # Base URL is a plain text field so a manually typed value is kept exactly
        # as entered — tab, enter, or clicking Start all commit it. (An editable
        # selectbox only commits typed text on enter/click and discarded it on
        # tab, snapping back to the first option.) Currently-playing local agents
        # are offered as one-click quick-fills below.
        # Quick-fill suggestions: all currently-running local agents (Play +
        # Simulator sessions), matching the Capture page.
        playing_urls = running_agent_urls()
        default_url = GlobalState.model_fields["base_url"].default
        if "exp_base_url" not in st.session_state:
            st.session_state["exp_base_url"] = global_state.base_url or default_url

        def _use_base_url(url: str) -> None:
            st.session_state["exp_base_url"] = url

        base_url = st.text_input(
            "Base URL",
            key="exp_base_url",
            disabled=disabled,
            placeholder=default_url,
        )
        # Quick-fill buttons for the demo server and any currently-playing local
        # agents, one per column in a single row right under the field; clicking
        # one populates the Base URL field above.
        quick_urls = [u for u in dict.fromkeys([default_url, *playing_urls]) if u]
        if quick_urls and not disabled:
            qcols = st.columns(len(quick_urls))
            for i, u in enumerate(quick_urls):
                qcols[i].button(u, key=f"exp_url_quick_{i}", width="stretch",
                                on_click=_use_base_url, args=(u,))
        # Interval default depends on the target and mode and updates as either
        # changes (a manual edit is remembered per target+mode). The shared public
        # demo server is floored at 1000 ms (raise it, never lower) to avoid
        # hammering it; a local agent can be polled far faster, so its default
        # follows the mode.
        _MODE_DEFAULT_INTERVAL_MS = {"stream": 10, "sample": 50, "current": 200}
        _DEMO_MIN_INTERVAL_MS = 1000
        demo_url = default_url.strip().rstrip("/")
        is_demo = str(base_url).strip().rstrip("/") == demo_url

        col1, col2 = st.columns(2)
        # Mode (left) is created first so the interval default can read it.
        with col1:
            _modes = ["stream", "sample", "current"]
            mode = st.selectbox(
                "Mode", _modes,
                index=_modes.index(state.cfg_mode) if state.cfg_mode in _modes else 0,
                key="exp_input_mode", disabled=disabled,
                filter_mode=None,
            )
        with col2:
            # The key encodes target+mode so switching either re-seeds the default;
            # Streamlit ignores `value` once a keyed widget has state.
            if is_demo:
                interval_value = interval_min = _DEMO_MIN_INTERVAL_MS
                interval_key = "exp_interval_demo"
            else:
                interval_value = _MODE_DEFAULT_INTERVAL_MS.get(mode, 10)
                interval_min = 1
                interval_key = f"exp_interval_{mode}"
            interval_ms = st.number_input(
                "Interval (ms)", value=interval_value, min_value=interval_min, step=1,
                key=interval_key, disabled=disabled,
                help=("The public demo server is shared: 1000 ms is the minimum "
                      "(you can set a higher interval) to avoid overloading it."
                      if is_demo else None),
            )

        col1, col2 = st.columns(2)
        with col1:
            start_clicked = st.button(
                "Start", disabled=disabled, width="stretch", type="primary",
            )
        with col2:
            stop_clicked = st.button(
                "Stop", disabled=not disabled, width="stretch",
            )

    if start_clicked:
        # Re-check under the shared lock so two users can't both launch.
        with get_state_lock():
            if not state.running:
                global_state.base_url = str(base_url)
                state.cfg_interval_ms = int(interval_ms)
                state.cfg_mode = str(mode)
                state.selected_id = None
                state.expanded = None
                state.device_filter = None
                state.node_filter = ""
                state.last_error = None
                explorer = MtConnectExplorer(
                    MtConnectClientConfiguration(base_url=str(base_url)),
                    interval_ms=int(interval_ms),
                    mode=str(mode),
                ).start()
                state.explorer = explorer
                state.running = True
                st.rerun()

    if stop_clicked:
        with get_state_lock():
            if state.running:
                explorer = state.explorer
                if explorer:
                    explorer.stop()
                state.explorer = None
                state.running = False
                st.rerun()

    # Auto-stop if explorer thread died
    if state.running and state.explorer:
        _explorer: MtConnectExplorer = state.explorer
        if not _explorer.is_alive:
            state.last_error = _explorer.error or "Connection lost."
            state.running = False
            state.explorer = None
            st.rerun()

    explorer: Optional[MtConnectExplorer] = state.explorer
    if explorer is None:
        if not state.running:
            if state.last_error:
                st.error(f"Connection lost: {state.last_error}")
            else:
                st.info("Configure the connection above and click **Start** to explore the MTConnect agent.")
        return

    # Only the probe (static device model) is needed here; the live fragments
    # below fetch fresh observations themselves each tick.
    probe_devices, _ = explorer.get_state()

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
        if state.device_filter not in device_labels:
            state.device_filter = device_labels[0] if device_labels else None

        selected_device = st.selectbox(
            "Device",
            device_labels,
            index=device_labels.index(state.device_filter)
            if state.device_filter in device_labels else 0,
            key="exp_device_select",
            filter_mode=None,
        )
        if selected_device != state.device_filter:
            state.device_filter = selected_device
            state.selected_id = None
            state.expanded = None

        node_filter = st.text_input(
            "Filter",
            value=state.node_filter,
            key="exp_node_filter_input",
            placeholder="Filter nodes by name or id…",
        )
        if node_filter != state.node_filter:
            state.node_filter = node_filter
            state.expanded = None

        device_trees = [t for t in trees if t.label == selected_device]
        flat: List[Tuple[TreeNode, int]] = []
        for t in device_trees:
            flat.extend(_flatten(t, 0))

        id_to_node: Dict[str, TreeNode] = {n.id: n for n, _ in flat}

        # Apply the text filter to produce the trees actually shown in the widget.
        query = state.node_filter.strip().lower()
        if query:
            display_trees = [
                ft for ft in (_filter_tree(t, query) for t in device_trees) if ft
            ]
        else:
            display_trees = device_trees

        display_flat: List[Tuple[TreeNode, int]] = []
        for t in display_trees:
            display_flat.extend(_flatten(t, 0))

        select_nodes = [_to_select_node(t) for t in display_trees]

        if state.expanded is None:
            state.expanded = [
                n.id for n, _ in display_flat if not n.is_data_item
            ]

        if state.selected_id not in id_to_node:
            state.selected_id = flat[0][0].id if flat else None

        prev_selected = state.selected_id
        prev_checked = [prev_selected] if prev_selected else []
        tree_key = f"exp_tree_{prev_selected or 'init'}"

        with st.container(height=600, border=False):
            if query and not select_nodes:
                st.caption("No nodes match the filter.")
                result = {"checked": prev_checked, "expanded": state.expanded}
            else:
                result = tree_select(
                    nodes=select_nodes,
                    checked=prev_checked,
                    no_cascade=True,
                    expanded=state.expanded,
                    key=tree_key,
                )

        new_checked: List[str] = result.get("checked", [])
        new_expanded: List[str] = result.get("expanded", [])

        if new_checked != prev_checked:
            added = [x for x in new_checked if x not in prev_checked]
            if added:
                state.selected_id = added[-1]
            elif new_checked:
                state.selected_id = new_checked[-1]

        state.expanded = new_expanded

    # Live regions (current value, history, pinned plots) refresh on their own
    # timers via st.fragment rather than a full-page rerun. This is the key
    # performance change: the tree column above has no live data, so it renders
    # only on interaction and its heavy tree_select component is NOT re-rendered
    # each tick — only the value/plot elements below re-render every 0.5s, so
    # reload cost no longer scales with re-rendering the whole page.
    refresh = 0.5 if state.running else None
    # Pinned plots additionally honor an Auto-Refresh toggle so the user can
    # freeze them for inspection; run_every is fixed when the fragment is
    # defined, so the toggle triggers a full rerun (below) to redefine it.
    pin_refresh = refresh if state.pin_auto_refresh else None

    # ---- Detail column (live fragment) ----
    @st.fragment(run_every=refresh)
    def _detail_fragment() -> None:
        # If the explorer thread died, bounce a full rerun so the page-top
        # auto-stop/error handling runs.
        if state.running and not explorer.is_alive:
            st.rerun(scope="app")
            return
        # Retain enough history to fill the largest configured plot duration
        # (history or pinned-plot window); the explorer caps it at 100,000
        # samples per DataItem. (hasattr guards an explorer instance from before
        # this method existed — a session started across a code reload; it takes
        # full effect once Explore is stopped and restarted.)
        if hasattr(explorer, "set_history_window"):
            explorer.set_history_window(max(
                timedelta(**{state.hist_window_unit: state.hist_window_val}),
                timedelta(**{state.pin_window_unit: state.pin_window_val}),
            ))
        _, live_obs = explorer.get_state()
        node = id_to_node.get(state.selected_id)
        if node is None:
            st.info("Select a node in the tree.")
            return
        st.markdown(f"**{node.tag}** — `{node.label}`")

        st.markdown("##### Attributes")
        if node.attributes:
            st.table([{"Attribute": k, "Value": v} for k, v in node.attributes.items()])
        else:
            st.caption("No attributes.")

        if node.is_data_item:
            obs: Optional[MtConnectObservation] = live_obs.get(node.id)
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
                # Per-node key gives the chart a stable identity that Streamlit
                # unmounts when the selection changes, so a plot from a previous
                # node never lingers when the new node has no history to show.
                with st.container(key=f"exp_hist_{node.id}"):
                    if len(history) > 1:
                        st.markdown("##### History")
                        hc1, hc2 = st.columns([1, 2])
                        with hc1:
                            w_val = st.number_input(
                                "Duration", min_value=1, step=1,
                                value=state.hist_window_val,
                                key="exp_hist_val",
                            )
                        with hc2:
                            w_unit = st.selectbox(
                                "Unit", _HIST_UNITS,
                                index=_HIST_UNITS.index(state.hist_window_unit),
                                key="exp_hist_unit",
                                filter_mode=None,
                            )
                        state.hist_window_val = int(w_val)
                        state.hist_window_unit = w_unit
                        _render_history(history, timedelta(**{w_unit: int(w_val)}))

                is_pinned = node.id in state.pinned_items
                if st.button(
                    "✓ In Pinned Plots" if is_pinned else "+ Add to Pinned Plots",
                    key="exp_add_pin",
                    disabled=is_pinned,
                ):
                    state.pinned_items.append(node.id)
                    # Fragment-scope rerun updates this button now; the pinned
                    # fragment shows the new plot on its next tick.
                    st.rerun()
            else:
                st.caption("No observation received yet.")

        else:
            data_items = _subtree_data_items(node)
            if data_items:
                st.markdown("##### DataItems")
                rows = []
                for di in data_items:
                    di_obs: Optional[MtConnectObservation] = live_obs.get(di.id)
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

    with detail_col:
        _detail_fragment()

    # ---- Pinned plots (live fragment) ----
    @st.fragment(run_every=pin_refresh)
    def _pinned_fragment() -> None:
        pinned = state.pinned_items
        if not pinned:
            return
        st.markdown("---")
        st.subheader("Pinned Plots")
        pa, ph1, ph2 = st.columns([1, 1, 2], vertical_alignment="bottom")
        with pa:
            auto = st.toggle(
                "Auto-Refresh",
                value=state.pin_auto_refresh,
                key="exp_pin_auto",
                help="When on, pinned plots keep updating with live data. Turn it off "
                "to freeze them for inspection, then back on to resume.",
            )
            if auto != state.pin_auto_refresh:
                # run_every is bound when the fragment is defined, so a full rerun
                # is needed to re-arm (or disarm) the auto-refresh cadence.
                state.pin_auto_refresh = auto
                st.rerun(scope="app")
        with ph1:
            pin_w_val = st.number_input(
                "Duration", min_value=1, step=1,
                value=state.pin_window_val,
                key="exp_pin_hist_val",
            )
        with ph2:
            pin_w_unit = st.selectbox(
                "Unit", _HIST_UNITS,
                index=_HIST_UNITS.index(state.pin_window_unit),
                key="exp_pin_hist_unit",
                filter_mode=None,
            )
        state.pin_window_val = int(pin_w_val)
        state.pin_window_unit = pin_w_unit
        window = timedelta(**{pin_w_unit: int(pin_w_val)})
        # Anchor every pinned plot to the same wall-clock "now" so they share an
        # identical X range ([now - window, now]) and the right edge keeps moving
        # even for DataItems that aren't currently receiving new data.
        pin_x_max = datetime.now(tz=timezone.utc)
        for item_id in list(pinned):
            pin_node = all_id_to_node.get(item_id)
            label = pin_node.label if pin_node else item_id
            pc1, pc2 = st.columns([5, 1])
            with pc1:
                st.markdown(f"**{label}**" + (f" — {pin_node.tag}" if pin_node else ""))
            with pc2:
                if st.button("Remove", key=f"exp_rm_{item_id}"):
                    state.pinned_items.remove(item_id)
                    st.rerun()
            pin_history = explorer.get_history(item_id)
            if len(pin_history) > 1:
                _render_history(pin_history, window, x_max=pin_x_max)
            else:
                st.caption("No history data yet.")

    _pinned_fragment()

    # Liveness is driven by the fragments' run_every above — no full-page rerun.
