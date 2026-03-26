import streamlit as st
import pandas as pd
from datetime import datetime, timedelta, timezone
from meraki.exceptions import APIError
from core.api import session
from core.logger import logger

API_CALL_COUNTER = 0


def _increment_counter(endpoint):
    global API_CALL_COUNTER
    API_CALL_COUNTER += 1
    logger.info(f"[bold cyan][API CALL #{API_CALL_COUNTER}][/] Fetching: [green]{endpoint}[/]")


CACHE_CONFIG = {
    'short': 300,
    'medium': 3600,
    'long': 86400
}

PRODUCT_TYPES = [
    "wireless",
    "appliance",
    "switch",
    "systemsManager",
    "camera",
    "cellularGateway",
    "wirelessController",
    "campusGateway",
    "secureConnect",
]

TIME_WINDOW_OPTIONS = {
    "Last 24 Hours": 1,
    "Last 7 Days": 7,
    "Last 30 Days": 30,
}

# Columns that should be rendered as integers when present.
INTEGER_COLUMNS = ["ssidNumber"]


def _compute_start_time(window_label):
    days = TIME_WINDOW_OPTIONS.get(window_label)
    if days is None:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


def _flatten_event_data(event):
    """
    Flatten the 'eventData' field into a single string.
    Handles dict, list, None, and already-stringified values.
    """
    event_data = event.get("eventData")

    if event_data is None:
        event["eventData"] = ""
    elif isinstance(event_data, dict):
        parts = []
        for k, v in event_data.items():
            parts.append(f"{k}:{v}")
        event["eventData"] = ", ".join(parts)
    elif isinstance(event_data, list):
        event["eventData"] = str(event_data)
    elif not isinstance(event_data, str):
        event["eventData"] = str(event_data)

    return event


def _sanitize_dataframe(df):
    """
    Ensure all object columns contain only string-compatible types
    so PyArrow serialization does not fail.
    """
    for col in df.columns:
        if df[col].dtype == "object":
            df[col] = df[col].apply(
                lambda v: str(v) if isinstance(v, (dict, list)) else v
            )
    return df


def _clean_column_types(df):
    """
    Convert known numeric columns to proper integer types.
    Missing values are replaced with empty string so the column
    renders cleanly in the table (no NaN / .0 floats).
    """
    for col in INTEGER_COLUMNS:
        if col in df.columns:
            numeric_col = pd.to_numeric(df[col], errors="coerce")
            df[col] = numeric_col.apply(
                lambda v: str(int(v)) if pd.notna(v) else ""
            )
    return df


def _extract_network_product_types(network, product_type_filter=None):
    raw_types = network.get("productTypes", [])
    network_name = network.get("name", "Unknown")

    if raw_types:
        valid_types = [pt for pt in raw_types if pt in PRODUCT_TYPES]
    else:
        logger.info(
            f"[yellow]Network [cyan]{network_name}[/] "
            f"has no productTypes field — will attempt all types[/]"
        )
        valid_types = list(PRODUCT_TYPES)

    if product_type_filter:
        filtered_types = [pt for pt in valid_types if pt in product_type_filter]
        skipped_by_filter = [pt for pt in valid_types if pt not in product_type_filter]

        if skipped_by_filter:
            logger.info(
                f"[yellow]Network [cyan]{network_name}[/] — "
                f"user filter excludes: [cyan]{', '.join(skipped_by_filter)}[/][/]"
            )

        valid_types = filtered_types

    logger.info(
        f"[bold green]Network [cyan]{network_name}[/] "
        f"product types to query: [cyan]{', '.join(valid_types) if valid_types else 'none'}[/] "
        f"(raw: {', '.join(raw_types) if raw_types else 'empty'})[/]"
    )

    return valid_types


class EventLogLogic:
    def __init__(self):
        self.dashboard = session.get_dashboard()

    @st.cache_data(ttl=CACHE_CONFIG['long'])
    def get_organizations(_self):
        _increment_counter("organizations.getOrganizations")
        try:
            orgs = _self.dashboard.organizations.getOrganizations()
            logger.info(f"[bold green]Fetched {len(orgs)} organizations[/]")
            return orgs
        except APIError as e:
            logger.error(f"[bold red]Failed to fetch organizations: {e}[/]")
            return []

    @st.cache_data(ttl=CACHE_CONFIG['long'])
    def get_networks(_self, org_id):
        _increment_counter(f"organizations.getOrganizationNetworks (org: {org_id})")
        try:
            networks = _self.dashboard.organizations.getOrganizationNetworks(
                organizationId=org_id,
                total_pages='all'
            )
            logger.info(
                f"[bold green]Fetched {len(networks)} networks for org [cyan]{org_id}[/][/]"
            )
            return networks
        except APIError as e:
            logger.error(f"[bold red]Failed to fetch networks for org {org_id}: {e}[/]")
            return []

    @st.cache_data(ttl=CACHE_CONFIG['short'])
    def get_network_events_by_product(
        _self, network_id, network_name, product_type, starting_after=None
    ):
        endpoint = (
            f"networks.getNetworkEvents "
            f"(net: {network_name}, product: {product_type}, after: {starting_after})"
        )
        _increment_counter(endpoint)
        try:
            kwargs = {
                "networkId": network_id,
                "productType": product_type,
                "perPage": 1000,
            }
            if starting_after:
                kwargs["startingAfter"] = starting_after

            response = _self.dashboard.networks.getNetworkEvents(**kwargs)
            return response

        except APIError as e:
            status = getattr(e, 'status', None)
            message = str(e)
            if status == 400:
                logger.info(
                    f"[yellow]Product type [cyan]{product_type}[/] not valid for "
                    f"network [cyan]{network_name}[/]: {message}[/]"
                )
            elif status == 404:
                logger.info(
                    f"[yellow]Network [cyan]{network_name}[/] not found or no events "
                    f"endpoint for product [cyan]{product_type}[/][/]"
                )
            else:
                logger.error(
                    f"[bold red]API error for [cyan]{network_name}[/] / "
                    f"[cyan]{product_type}[/] (status {status}): {message}[/]"
                )
            return None
        except Exception as e:
            logger.error(
                f"[bold red]Unexpected error for [cyan]{network_name}[/] / "
                f"[cyan]{product_type}[/]: {e}[/]"
            )
            return None

    def fetch_events_with_time_window(
        self, network_id, network_name, product_type, cutoff_dt
    ):
        all_events = []
        starting_after = None
        stop_pagination = False

        while not stop_pagination:
            response = self.get_network_events_by_product(
                network_id, network_name, product_type, starting_after
            )

            if response is None:
                break

            events = response.get("events", [])
            if not events:
                break

            for event in events:
                occurred_str = event.get("occurredAt", "")
                if occurred_str:
                    try:
                        event_dt = datetime.fromisoformat(
                            occurred_str.replace("Z", "+00:00")
                        )
                    except (ValueError, TypeError):
                        event_dt = None

                    if event_dt and cutoff_dt and event_dt < cutoff_dt:
                        stop_pagination = True
                        break

                event = _flatten_event_data(event)
                all_events.append(event)

            if stop_pagination:
                break

            page_end_at = response.get("pageEndAt", None)
            if page_end_at and page_end_at != starting_after and len(events) == 1000:
                starting_after = page_end_at
            else:
                break

        if all_events:
            logger.info(
                f"[bold green]Collected {len(all_events)} events for "
                f"[cyan]{network_name}[/] / [cyan]{product_type}[/] within time window[/]"
            )
        else:
            logger.info(
                f"[yellow]No events within time window for "
                f"[cyan]{network_name}[/] / [cyan]{product_type}[/][/]"
            )

        return all_events

    def collect_events_for_network(
        self, network, time_window_label,
        product_type_filter=None, progress_callback=None
    ):
        network_id = network["id"]
        network_name = network["name"]
        cutoff_dt = _compute_start_time(time_window_label)

        net_product_types = _extract_network_product_types(
            network, product_type_filter
        )

        all_skipped = [pt for pt in PRODUCT_TYPES if pt not in net_product_types]

        if not net_product_types:
            logger.info(
                f"[yellow]No matching product types for network "
                f"[cyan]{network_name}[/] — skipping[/]"
            )
            if progress_callback:
                for pt in PRODUCT_TYPES:
                    progress_callback(
                        f"{network_name} — skipped (no matching product types)"
                    )
            return pd.DataFrame()

        if progress_callback:
            for pt in all_skipped:
                progress_callback(
                    f"{network_name} — skipping {pt} (not applicable)"
                )

        all_frames = []

        for product_type in net_product_types:
            if progress_callback:
                progress_callback(f"{network_name} — product: {product_type}")

            events = self.fetch_events_with_time_window(
                network_id, network_name, product_type, cutoff_dt
            )

            if events:
                df = pd.DataFrame(events)
                df["productType"] = product_type
                df["networkName"] = network_name
                df["networkId"] = network_id
                all_frames.append(df)

        if all_frames:
            combined = pd.concat(all_frames, ignore_index=True)
            combined = _sanitize_dataframe(combined)
            combined = _clean_column_types(combined)
            return combined
        return pd.DataFrame()

    @staticmethod
    def sort_and_order_events(df):
        if df.empty:
            return df

        if "occurredAt" in df.columns:
            df["occurredAt"] = pd.to_datetime(df["occurredAt"], errors="coerce", utc=True)
            df = df.sort_values(by="occurredAt", ascending=False).reset_index(drop=True)

        priority_cols = ["networkName", "productType", "occurredAt"]
        existing_priority = [c for c in priority_cols if c in df.columns]
        remaining = [c for c in df.columns if c not in existing_priority]
        df = df[existing_priority + remaining]

        df = _sanitize_dataframe(df)
        df = _clean_column_types(df)

        return df

    @staticmethod
    def grep_events(df, search_term, columns=None):
        """
        Filter rows where any searchable column contains search_term.

        Parameters
        ----------
        df          : source DataFrame
        search_term : case-insensitive plain-text search string
        columns     : optional list of column names to restrict the search to.
                      When None (default) all columns except the hard-coded
                      exclusion set are searched — preserving original behaviour.
        """
        if df.empty or not search_term:
            return df

        # Columns that are never meaningful to search
        always_exclude = {"networkName", "occurredAt"}

        if columns is not None:
            # User-supplied list — still honour the hard exclusions
            search_cols = [
                c for c in columns
                if c in df.columns and c not in always_exclude
            ]
        else:
            # Original behaviour: search everything except excluded cols
            search_cols = [c for c in df.columns if c not in always_exclude]

        if not search_cols:
            return df

        search_lower = search_term.lower()
        mask = pd.Series(False, index=df.index)

        for col in search_cols:
            mask = mask | df[col].astype(str).str.lower().str.contains(
                search_lower, na=False, regex=False
            )

        filtered = df[mask].reset_index(drop=True)
        logger.info(
            f"[bold green]Grep filter '{search_term}' (case-insensitive, "
            f"cols: {search_cols}): {len(filtered)}/{len(df)} rows matched[/]"
        )
        return filtered