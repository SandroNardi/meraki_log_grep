import streamlit as st
import pandas as pd
import os
import html
import time
import re

from logic import EventLogLogic, CACHE_CONFIG, PRODUCT_TYPES, TIME_WINDOW_OPTIONS
from core.logger import logger, ENABLE_FILE_LOGGING, LOG_FILENAME

# ─── Columns excluded from search regardless of user selection ────────────────
ALWAYS_EXCLUDED_FROM_SEARCH = {"occurredAt", "networkName", "networkId", "productType"}


# ─── Helper: read file content ────────────────────────────────────────────────
def get_file_content(filepath, last_n_lines=None):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            if last_n_lines:
                lines = f.readlines()
                return lines[-last_n_lines:]
            return f.read()
    except FileNotFoundError:
        return f"File not found: {filepath}"
    except Exception as e:
        return f"Error reading file: {e}"


# ─── Helper: ensure all object columns are strings for Arrow ──────────────────
def ensure_arrow_compatible(df):
    result = df.copy()
    for col in result.columns:
        if result[col].dtype == "object":
            result[col] = result[col].apply(
                lambda v: str(v) if isinstance(v, (dict, list)) else v
            )
    return result


# ─── Helper: apply cell-level highlight via Pandas Styler ────────────────────
def style_grep_matches(df, search_term, search_cols, exclude_cols=None):
    if exclude_cols is None:
        exclude_cols = set()

    pattern = re.compile(re.escape(search_term), re.IGNORECASE)
    active_cols = set(search_cols) - set(exclude_cols)

    def _highlight(val, col_name):
        if col_name in active_cols and pattern.search(str(val)):
            return "background-color: #fff3cd; color: #664d03; font-weight: 600;"
        return ""

    return df.style.apply(
        lambda col: [_highlight(v, col.name) for v in col],
        axis=0,
    )


# ─── Modal Dialogs ────────────────────────────────────────────────────────────
@st.dialog("System Configuration", width="large")
def show_config_modal():
    st.markdown("### 🛠️ Environment & Logging")
    api_key_status = "✅ Set" if os.getenv("MK_CSM_KEY") else "❌ Missing"
    st.write(f"**API Key (MK_CSM_KEY):** {api_key_status}")
    st.write(f"**Log Level:** `INFO`")
    st.write(f"**File Logging:** `{'Enabled' if ENABLE_FILE_LOGGING else 'Disabled'}`")
    if ENABLE_FILE_LOGGING:
        st.write(f"**Log Filename:** `{LOG_FILENAME}`")
    st.divider()
    st.markdown("### ⏱️ Caching Timers (Seconds)")
    st.json(CACHE_CONFIG)
    st.divider()
    st.markdown("### 📦 Product Types Queried")
    st.write(", ".join(PRODUCT_TYPES))
    st.divider()
    st.markdown("### 🕐 Time Window Options")
    st.json(TIME_WINDOW_OPTIONS)


@st.dialog("Application Logs", width="large")
def show_log_modal():
    st.markdown(f"**Reading from:** `{LOG_FILENAME}`")
    lines = get_file_content(LOG_FILENAME, last_n_lines=2000)

    if isinstance(lines, list):
        full_content = "".join(lines)
        st.download_button(
            label="📥 Download Log File",
            data=full_content,
            file_name="application_log.txt",
            mime="text/plain",
        )
        log_html = ["""
        <style>
            .terminal-window {
                background-color:#0e1117; color:#c9d1d9;
                font-family:'Courier New',Courier,monospace;
                font-size:12px; padding:15px; border-radius:8px;
                border:1px solid #30363d; height:500px;
                overflow-y:auto; white-space:pre-wrap; line-height:1.4;
            }
            .log-line  { margin-bottom:2px; }
            .log-info  { color:#3fb950; }
            .log-warn  { color:#d29922; }
            .log-error { color:#f85149; }
            .log-meta  { color:#8b949e; }
        </style>
        <div class="terminal-window">
        """]
        for line in lines:
            safe_line = html.escape(line.strip())
            css_class = "log-line"
            if "INFO" in safe_line:
                css_class += " log-info"
            elif "WARNING" in safe_line:
                css_class += " log-warn"
            elif "ERROR" in safe_line or "CRITICAL" in safe_line:
                css_class += " log-error"
            parts = safe_line.split(" - ", 1)
            if len(parts) > 1:
                log_html.append(
                    f'<div class="{css_class}">'
                    f'<span class="log-meta">{parts[0]} - </span>{parts[1]}</div>'
                )
            else:
                log_html.append(f'<div class="{css_class}">{safe_line}</div>')
        log_html.append("</div>")
        st.markdown("".join(log_html), unsafe_allow_html=True)
    else:
        st.error(lines)


@st.dialog("License", width="large")
def show_license_modal():
    st.markdown("### Open Source License")
    st.code(get_file_content("LICENSE"), language="text")


@st.dialog("ReadMe", width="large")
def show_readme_modal():
    st.markdown(get_file_content("README.md"))


# ─── Derive searchable columns from a loaded dataframe ───────────────────────
def get_searchable_columns(df: pd.DataFrame) -> list:
    return [c for c in df.columns if c not in ALWAYS_EXCLUDED_FROM_SEARCH]


# ─── Inject CSS to wrap text in the eventData column ─────────────────────────
def inject_eventdata_wrap_css():
    """
    Streamlit dataframes render inside a shadow DOM / canvas element.
    The only reliable hook is the aria-label on the column header cell,
    which we use to find the column index and target the data cells below it.
    This CSS targets the glide-data-grid cell wrapper for that column.
    """
    st.markdown("""
    <style>
    /* Target every cell in every st.dataframe on the page.
       We cannot isolate a single column via pure CSS in the canvas renderer,
       so we allow all cells to wrap — this is generally fine and consistent. */
    [data-testid="stDataFrame"] iframe {
        min-height: 300px;
    }
    /* The actual cell text nodes sit inside a div with this class
       in Streamlit's glide-data-grid renderer */
    [data-testid="stDataFrame"] div[class*="dvn-scroller"] {
        overflow-x: auto !important;
    }
    </style>
    """, unsafe_allow_html=True)


# ─── Render a single network table ───────────────────────────────────────────
def render_network_table(net_name, df, grep_term, search_cols, table_key_suffix):
    display_df = df.copy()

    drop_cols = [c for c in ["networkName", "networkId"] if c in display_df.columns]
    display_df = display_df.drop(columns=drop_cols)

    if "occurredAt" in display_df.columns:
        display_df["occurredAt"] = display_df["occurredAt"].dt.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    display_df = ensure_arrow_compatible(display_df)

    # Build column config — eventData gets extra width
    column_config = {}
    if "eventData" in display_df.columns:
        column_config["eventData"] = st.column_config.TextColumn(
            "eventData",
            width="large",
        )

    st.markdown(f"#### 🔹 {net_name}")
    st.caption(f"{len(display_df):,} events")

    if grep_term and search_cols:
        styled = style_grep_matches(
            display_df,
            search_term=grep_term,
            search_cols=search_cols,
            exclude_cols={"occurredAt"},
        )
        st.dataframe(
            styled,
            width='stretch',
            hide_index=True,
            key=f"table_{table_key_suffix}",
            column_config=column_config,
        )
    else:
        st.dataframe(
            display_df,
            width='stretch',
            hide_index=True,
            key=f"table_{table_key_suffix}",
            column_config=column_config,
        )


# ─── Main Application ─────────────────────────────────────────────────────────
def run_web():
    st.set_page_config(
        page_title="Meraki Event Log Collector",
        page_icon=":material/receipt_long:",
        layout="wide",
    )

    st.markdown("""
    <style>
        :root {
            --primary-accent: #144a90;
            --top-bar-bg:     #07172B;
            --white:          #FFFFFF;
            --gradient: linear-gradient(to right,#007bff,#6610f2,#e83e8c,#fd7e14);
        }
        [data-testid="stIconMaterial"]      { color: var(--primary-accent) !important; }
        [data-testid="stBaseButton-header"] { color: var(--white) !important; }
        [data-testid="stMainMenu"] svg      { fill:  var(--white) !important; }
        .stAppDeployButton                  { display: none !important; }
        header[data-testid="stHeader"]      { background-color: transparent; }
        .top-gradient-bar {
            position:fixed; top:0; left:0; width:100%; height:4px;
            background-image:var(--gradient); z-index:100001;
        }
        .top-bar {
            position:fixed; top:4px; left:0; width:100%; height:56px;
            background-color:var(--top-bar-bg); z-index:100000;
            display:flex; align-items:center; padding-left:60px;
            box-shadow:0 2px 4px rgba(0,0,0,0.2);
        }
        .top-bar-text { color:var(--white); font-weight:600; font-size:1.1em; }
        .block-container { padding-top:6rem; }
    </style>
    <div class="top-gradient-bar"></div>
    <div class="top-bar">
        <div class="top-bar-text">Meraki Event Log Collector</div>
    </div>
    """, unsafe_allow_html=True)

    try:
        logic = EventLogLogic()

        # ─── Sidebar ──────────────────────────────────────────────────────────
        with st.sidebar:
            st.header("1. Scope")

            orgs = logic.get_organizations()
            if not orgs:
                st.error("No organizations found. Check your API key.")
                st.stop()

            org_names = {org["name"]: org["id"] for org in orgs}
            selected_org_name = st.selectbox(
                "Organization", options=list(org_names.keys()), key="sel_org"
            )
            selected_org_id = org_names[selected_org_name]

            networks = logic.get_networks(selected_org_id)
            if not networks:
                st.warning("No networks found for this organization.")
                st.stop()

            mass_fetch = st.checkbox(
                "Fetch All Networks", value=False, key="mass_fetch"
            )
            if mass_fetch:
                target_nets = networks
                st.info(f"🌐 All **{len(target_nets)}** networks selected.")
            else:
                net_name_map = {n["name"]: n for n in networks}
                selected_net_names = st.multiselect(
                    "Select Network(s)",
                    options=sorted(net_name_map.keys()),
                    key="sel_nets",
                )
                target_nets = [net_name_map[name] for name in selected_net_names]

            st.header("2. Filters")

            time_window = st.radio(
                "⏱️ Time Window",
                options=list(TIME_WINDOW_OPTIONS.keys()),
                index=0,
                key="time_window",
                horizontal=True,
                help=(
                    "Events older than the selected window will not be fetched. "
                    "Pagination stops automatically when events fall outside the window."
                ),
            )

            selected_product_types = st.multiselect(
                "📦 Product Types",
                options=PRODUCT_TYPES,
                default=[],
                key="sel_product_types",
                help=(
                    "Select specific product types to fetch. "
                    "Leave empty to fetch all product types present in each network."
                ),
            )

            grep_term = st.text_input(
                "🔍 Grep / Search",
                placeholder="Filter across selected event fields…",
                key="grep_input",
                help=(
                    "Case-insensitive search. Use **🎛️ Search Column Filter** "
                    "in the main panel to restrict which fields are searched."
                ),
            )

            st.divider()
            run_btn = st.button(
                "🚀 Collect Events",
                type="primary",
                width='stretch',
                disabled=(len(target_nets) == 0),
            )

            st.divider()
            with st.expander("ℹ️ About", expanded=False):
                st.markdown("### Meraki Event Log Collector")
                st.caption(
                    "Collects network events across all product types from the "
                    "Meraki Dashboard and presents them in a unified, searchable table."
                )
                st.markdown("**Author:** SandroN")
                st.markdown("[GitHub Project Repository](#)")
                st.divider()

                if st.button("⚙️ System Configuration", width='stretch'):
                    show_config_modal()
                if ENABLE_FILE_LOGGING:
                    if st.button("📄 Application Logs", width='stretch'):
                        show_log_modal()
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("📜 License", width='stretch'):
                        show_license_modal()
                with c2:
                    if st.button("📖 ReadMe", width='stretch'):
                        show_readme_modal()

        # ─── Collection ───────────────────────────────────────────────────────
        if run_btn:
            if not target_nets:
                st.warning("Please select at least one network.")
                st.stop()

            product_type_filter = selected_product_types if selected_product_types else None
            filter_label = (
                ", ".join(product_type_filter) if product_type_filter else "all detected"
            )

            logger.info(
                f"[bold cyan]Starting collection: {len(target_nets)} networks, "
                f"time window: [cyan]{time_window}[/], "
                f"product types: [cyan]{filter_label}[/][/]"
            )

            progress_bar = st.progress(0)
            status_text = st.empty()
            total_steps = len(target_nets) * len(PRODUCT_TYPES)
            current_step = 0

            def update_progress(message):
                nonlocal current_step
                current_step += 1
                progress_bar.progress(min(current_step / total_steps, 1.0))
                status_text.text(f"Processing: {message}")

            all_frames, skipped_networks = [], []

            for net in target_nets:
                df = logic.collect_events_for_network(
                    network=net,
                    time_window_label=time_window,
                    product_type_filter=product_type_filter,
                    progress_callback=update_progress,
                )
                if not df.empty:
                    all_frames.append(df)
                else:
                    skipped_networks.append(net["name"])

            status_text.text("Collection Complete!")
            progress_bar.progress(1.0)
            time.sleep(0.5)
            status_text.empty()
            progress_bar.empty()

            if all_frames:
                combined_df = EventLogLogic.sort_and_order_events(
                    pd.concat(all_frames, ignore_index=True)
                )
                logger.info(
                    f"[bold green]Collection complete: {len(combined_df)} total events "
                    f"from {len(target_nets)} networks ({time_window}, "
                    f"products: {filter_label})[/]"
                )
            else:
                combined_df = pd.DataFrame()
                logger.info("[yellow]No events collected across any network.[/]")

            st.session_state["event_results"] = combined_df
            st.session_state["skipped_networks"] = skipped_networks
            st.session_state["collection_time_window"] = time_window
            st.session_state["collection_product_filter"] = filter_label
            st.session_state.pop("search_cols_selection", None)

        # ─── Display Results ──────────────────────────────────────────────────
        if "event_results" in st.session_state:
            df_all = st.session_state["event_results"]
            skipped = st.session_state.get("skipped_networks", [])
            collected_window = st.session_state.get("collection_time_window", "")
            collected_products = st.session_state.get(
                "collection_product_filter", "all detected"
            )

            if df_all.empty:
                st.warning(
                    "No events were returned for the selected networks "
                    "and product types within the chosen time window."
                )
            else:
                searchable_cols = get_searchable_columns(df_all)

                if "search_cols_selection" not in st.session_state:
                    st.session_state["search_cols_selection"] = searchable_cols[:]

                active_search_cols = st.session_state["search_cols_selection"]

                # ── Apply grep ────────────────────────────────────────────
                if grep_term and active_search_cols:
                    df_filtered = EventLogLogic.grep_events(
                        df_all, grep_term, columns=active_search_cols
                    )
                else:
                    df_filtered = df_all

                all_network_names = (
                    sorted(df_filtered["networkName"].unique().tolist())
                    if not df_filtered.empty else []
                )

                # ── 1. Metrics ─────────────────────────────────────────────
                col1, col2, col3, col4, col5 = st.columns(5)
                with col1:
                    st.metric("Total Events", f"{len(df_filtered):,}")
                with col2:
                    st.metric("Networks with Events", len(all_network_names))
                with col3:
                    st.metric(
                        "Product Types",
                        df_filtered["productType"].nunique()
                        if not df_filtered.empty else 0,
                    )
                with col4:
                    st.metric("Skipped Networks", len(skipped))
                with col5:
                    st.metric("Time Window", collected_window)

                # ── 2. Search Column Filter ────────────────────────────────
                st.markdown("#### 🎛️ Search Column Filter")
                st.caption(
                    "Restrict the **Grep / Search** to specific columns. "
                    "All columns are active by default. "
                    "Changes apply instantly without re-fetching data."
                )

                btn_c1, btn_c2, _ = st.columns([1, 1, 6])
                with btn_c1:
                    if st.button("✅ Select All", key="cols_select_all"):
                        st.session_state["search_cols_selection"] = searchable_cols[:]
                        st.rerun()
                with btn_c2:
                    if st.button("🗑️ Clear All", key="cols_clear_all"):
                        st.session_state["search_cols_selection"] = []
                        st.rerun()

                st.multiselect(
                    "Active search columns",
                    options=searchable_cols,
                    default=st.session_state["search_cols_selection"],
                    key="search_cols_selection",
                    label_visibility="collapsed",
                )

                # ── 3. Search feedback ─────────────────────────────────────
                if grep_term and not active_search_cols:
                    st.warning(
                        "⚠️ No search columns selected — showing all events. "
                        "Select at least one column above."
                    )
                elif grep_term and not df_filtered.empty:
                    st.success(
                        f"🔍 **{len(df_filtered):,}** events match "
                        f"**\"{grep_term}\"** "
                        f"— searched in: *{', '.join(active_search_cols)}*"
                    )
                elif grep_term and df_filtered.empty:
                    st.warning(
                        f"No events match **\"{grep_term}\"** "
                        f"in the selected columns."
                    )

                # ── 4. Tables ──────────────────────────────────────────────
                if not df_filtered.empty:
                    st.markdown("### 📋 Event Log")
                    st.caption(
                        f"Events sorted by timestamp (newest first) — "
                        f"Time window: **{collected_window}** — "
                        f"Product types: **{collected_products}**."
                    )

                    for idx, net_name in enumerate(all_network_names):
                        net_df = df_filtered[
                            df_filtered["networkName"] == net_name
                        ].copy()
                        if not net_df.empty:
                            render_network_table(
                                net_name=net_name,
                                df=net_df,
                                grep_term=grep_term,
                                search_cols=active_search_cols,
                                table_key_suffix=f"{idx}_{net_name}",
                            )

                # ── 5. CSV download ────────────────────────────────────────
                if not df_filtered.empty:
                    st.divider()
                    csv_df = ensure_arrow_compatible(df_filtered.copy())
                    if "occurredAt" in csv_df.columns:
                        csv_df["occurredAt"] = csv_df["occurredAt"].dt.strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                    st.download_button(
                        label="📥 Download as CSV",
                        data=csv_df.to_csv(index=False).encode("utf-8"),
                        file_name="meraki_events.csv",
                        mime="text/csv",
                        key="download_csv",
                    )

                # ── 6. Skipped networks ────────────────────────────────────
                if skipped:
                    with st.expander(
                        f"⚠️ {len(skipped)} Networks with No Events",
                        expanded=False,
                    ):
                        for name in skipped:
                            st.write(f"- {name}")

    except EnvironmentError as e:
        st.error(f"🔑 Configuration Error: {e}")
        st.info(
            "Please set the `MK_CSM_KEY` environment variable "
            "with your Meraki API key."
        )
    except Exception as e:
        logger.error(f"[bold red]App Error: {e}[/]", exc_info=True)
        st.error(f"An unexpected error occurred: {e}")


if __name__ == "__main__":
    run_web()