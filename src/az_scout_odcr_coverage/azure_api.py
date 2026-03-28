"""Azure ARM helpers for ODCR coverage analysis.

Fetches VMs (with ODCR association), Capacity Reservation Groups,
and Activity Log allocation events.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

from az_scout.azure_api import ArmRequestError, arm_get, arm_paginate, arm_post

logger = logging.getLogger(__name__)

AZURE_MGMT_URL = "https://management.azure.com"
COMPUTE_API = "2024-03-01"
ACTIVITY_LOG_API = "2015-04-01"
ARG_API = "2021-03-01"

# ---------------------------------------------------------------------------
# Simple in-memory cache with TTL
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL_VM = 300  # 5 min for VM list
_CACHE_TTL_CR = 300  # 5 min for capacity reservations
_CACHE_TTL_EVENTS = 600  # 10 min for activity log events


def _cached(key: str, ttl: int) -> Any | None:
    """Return cached value if still valid, else None."""
    entry = _cache.get(key)
    if entry and time.monotonic() - entry[0] < ttl:
        return entry[1]
    return None


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)


# Activity Log operation names for VM lifecycle events
_VM_OPERATIONS = (
    "Microsoft.Compute/virtualMachines/start/action",
    "Microsoft.Compute/virtualMachines/deallocate/action",
    "Microsoft.Compute/virtualMachines/powerOff/action",
    "Microsoft.Compute/virtualMachines/write",
)

# Only count 'write' events as allocation attempts if they have these sub-status
# codes or the status message contains an allocation error. Other 'write' events
# (tag updates, property changes) are excluded from allocation stats.
_WRITE_ALLOCATION_SUBSTATUS = {
    "created",
    "accepted",
}

_ARG_VM_QUERY = (
    "resources"
    " | where type =~ 'microsoft.compute/virtualmachines'"
    " | extend powerState = tostring(properties.extended.instanceView.powerState.code)"
    " | extend vmSize = tostring(properties.hardwareProfile.vmSize)"
    " | extend crGroupId = tostring(properties.capacityReservation.capacityReservationGroup.id)"
    " | extend zone = tostring(zones[0])"
    " | project id, name, location, resourceGroup, vmSize, zone, powerState, crGroupId"
)


def _list_vms_arg(
    subscription_id: str,
    *,
    region: str | None = None,
    tenant_id: str | None = None,
) -> list[dict[str, Any]] | None:
    """List VMs with power state via a single Azure Resource Graph query.

    Returns None if ARG call fails (caller should fall back to ARM).
    """
    query = _ARG_VM_QUERY
    if region:
        query += f" | where location =~ '{region}'"

    url = f"{AZURE_MGMT_URL}/providers/Microsoft.ResourceGraph/resources?api-version={ARG_API}"
    body: dict[str, Any] = {
        "subscriptions": [subscription_id],
        "query": query,
    }

    try:
        all_rows: list[dict[str, Any]] = []
        skip_token: str | None = None
        while True:
            if skip_token:
                body["options"] = {"$skipToken": skip_token}
            result = arm_post(url, json=body, tenant_id=tenant_id)
            rows = result.get("data", [])
            all_rows.extend(rows)
            skip_token = result.get("$skipToken")
            if not skip_token:
                break

        vms: list[dict[str, Any]] = []
        for row in all_rows:
            raw_state = row.get("powerState", "")
            if raw_state.startswith("PowerState/"):
                power_state = raw_state.split("/", 1)[1]
            elif raw_state:
                power_state = raw_state
            else:
                power_state = "unknown"

            cr_group_id = row.get("crGroupId") or None
            if cr_group_id == "":
                cr_group_id = None

            vms.append(
                {
                    "name": row.get("name", ""),
                    "id": row.get("id", ""),
                    "resource_group": row.get("resourceGroup", ""),
                    "vm_size": row.get("vmSize", ""),
                    "location": row.get("location", "").lower().replace(" ", ""),
                    "zone": row.get("zone") or None,
                    "power_state": power_state,
                    "odcr_group_id": cr_group_id,
                    "has_odcr": cr_group_id is not None,
                }
            )

        logger.info(
            "list_vms (ARG): %d VMs in %s (region=%s)",
            len(vms),
            subscription_id[:8],
            region or "all",
        )
        return vms
    except Exception as exc:
        logger.warning("ARG VM query failed (%s), falling back to ARM list", exc)
        return None


def list_vms(
    subscription_id: str,
    *,
    region: str | None = None,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """List VMs with capacity reservation info and power state.

    Uses Azure Resource Graph (single query) for accurate power state and
    region filtering. Falls back to ARM list endpoint if ARG is unavailable.
    """
    cache_key = f"vms:{subscription_id}:{region or ''}"
    hit = _cached(cache_key, _CACHE_TTL_VM)
    if hit is not None:
        return hit  # type: ignore[no-any-return]

    # Primary path: single ARG query
    vms = _list_vms_arg(subscription_id, region=region, tenant_id=tenant_id)
    if vms is not None:
        _cache_set(cache_key, vms)
        return vms

    # Fallback: ARM list endpoint (no power state)
    url = (
        f"{AZURE_MGMT_URL}/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Compute/virtualMachines"
        f"?api-version={COMPUTE_API}&$statusOnly=true"
    )
    raw_vms = arm_paginate(url, tenant_id=tenant_id)

    vms_fallback: list[dict[str, Any]] = []
    for vm in raw_vms:
        location = vm.get("location", "").lower().replace(" ", "")
        if region and location != region.lower():
            continue

        props = vm.get("properties", {})
        zones = vm.get("zones", [])

        # Power state from instanceView (often absent on list endpoints)
        power_state = "unknown"
        for status in props.get("instanceView", {}).get("statuses", []):
            code = status.get("code", "")
            if code.startswith("PowerState/"):
                power_state = code.split("/", 1)[1]

        # Check ODCR association
        cr_group = props.get("capacityReservation", {}).get("capacityReservationGroup", {})
        odcr_group_id = cr_group.get("id") if cr_group else None

        vms_fallback.append(
            {
                "name": vm.get("name", ""),
                "id": vm.get("id", ""),
                "resource_group": _extract_rg(vm.get("id", "")),
                "vm_size": props.get("hardwareProfile", {}).get("vmSize", ""),
                "location": location,
                "zone": zones[0] if zones else None,
                "power_state": power_state,
                "odcr_group_id": odcr_group_id,
                "has_odcr": odcr_group_id is not None,
            }
        )

    logger.info(
        "list_vms (ARM fallback): %d VMs in %s (region=%s)",
        len(vms_fallback),
        subscription_id[:8],
        region or "all",
    )
    _cache_set(cache_key, vms_fallback)
    return vms_fallback


def list_capacity_reservations(
    subscription_id: str,
    *,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """List all Capacity Reservation Groups with utilization details."""
    cache_key = f"cr:{subscription_id}"
    hit = _cached(cache_key, _CACHE_TTL_CR)
    if hit is not None:
        return hit  # type: ignore[no-any-return]

    url = (
        f"{AZURE_MGMT_URL}/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Compute/capacityReservationGroups"
        f"?api-version={COMPUTE_API}"
    )
    try:
        groups = arm_paginate(url, tenant_id=tenant_id)
    except ArmRequestError:
        logger.warning("Failed to list capacity reservation groups for %s", subscription_id[:8])
        _cache_set(cache_key, [])
        return []

    reservations: list[dict[str, Any]] = []
    for group in groups:
        group_id = group.get("id", "")
        group_name = group.get("name", "")
        location = group.get("location", "").lower().replace(" ", "")
        zones = group.get("zones", [])

        # Get individual reservations within the group
        res_url = f"{AZURE_MGMT_URL}{group_id}/capacityReservations?api-version={COMPUTE_API}"
        try:
            cr_list = arm_paginate(res_url, tenant_id=tenant_id)
        except ArmRequestError:
            logger.warning("Failed to list reservations for group %s", group_name)
            cr_list = []

        for cr in cr_list:
            sku = cr.get("sku", {})
            capacity = sku.get("capacity", 0)

            reservations.append(
                {
                    "group_name": group_name,
                    "group_id": group_id,
                    "reservation_name": cr.get("name", ""),
                    "location": location,
                    "zone": (cr.get("zones") or zones or [None])[0],
                    "sku_name": sku.get("name", ""),
                    "capacity": capacity,
                }
            )

    logger.info(
        "list_capacity_reservations: %d reservations in %s",
        len(reservations),
        subscription_id[:8],
    )
    _cache_set(cache_key, reservations)
    return reservations


# ---------------------------------------------------------------------------
# Activity Log event processing helpers
# ---------------------------------------------------------------------------
_OP_NAMES = {op.lower() for op in _VM_OPERATIONS}
_STATUS_PRIORITY = {"failed": 3, "failure": 3, "succeeded": 2, "accepted": 1, "started": 0}


def _process_raw_events(
    raw_events: list[dict[str, Any]],
    best_events: dict[tuple[str, str, str], tuple[int, dict[str, Any]]],
) -> None:
    """Filter and deduplicate raw Activity Log events into *best_events* (in-place)."""
    for event in raw_events:
        op_name = event.get("operationName", {})
        if isinstance(op_name, dict):
            op_name = op_name.get("value", "")
        if op_name.lower() not in _OP_NAMES:
            continue

        resource_id = (event.get("resourceId") or "").lower()
        if not resource_id:
            continue

        status_obj = event.get("status", {})
        status = status_obj.get("value", "") if isinstance(status_obj, dict) else str(status_obj)

        if op_name.lower().endswith("/write"):
            sub_status = event.get("subStatus", {})
            sub_val = (
                sub_status.get("value", "").lower()
                if isinstance(sub_status, dict)
                else str(sub_status).lower()
            )
            is_creation = sub_val in _WRITE_ALLOCATION_SUBSTATUS
            is_failure = status.lower() in ("failed", "failure")
            if not is_creation and not is_failure:
                continue

        error_code = None
        if status.lower() in ("failed", "failure"):
            try:
                status_msg = json.loads(event.get("properties", {}).get("statusMessage", "{}"))
                error_info = status_msg.get("error", status_msg)
                error_code = error_info.get("code", "")
            except (json.JSONDecodeError, AttributeError):
                error_code = "Unknown"

        short_op = op_name.rsplit("/", 1)[-1] if "/" in op_name else op_name
        if short_op == "action":
            parts = op_name.rsplit("/", 2)
            short_op = parts[-2] if len(parts) >= 2 else short_op

        entry: dict[str, Any] = {
            "timestamp": event.get("eventTimestamp", ""),
            "operation": short_op,
            "status": status,
        }
        if error_code:
            entry["error_code"] = error_code

        correlation_id = event.get("correlationId", "")
        dedup_key = (resource_id, correlation_id, short_op)
        priority = _STATUS_PRIORITY.get(status.lower(), 0)
        existing = best_events.get(dedup_key)
        if existing is None or priority > existing[0]:
            best_events[dedup_key] = (priority, entry)


def _build_events_by_vm(
    best_events: dict[tuple[str, str, str], tuple[int, dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Build sorted per-VM event lists from deduplicated *best_events*."""
    events_by_vm: dict[str, list[dict[str, Any]]] = {}
    for (resource_id, _, _), (_, entry) in best_events.items():
        events_by_vm.setdefault(resource_id, []).append(entry)
    for vm_events in events_by_vm.values():
        vm_events.sort(key=lambda e: e["timestamp"])
    return events_by_vm


def _activity_log_url(subscription_id: str, lookback_days: int) -> str:
    """Build the Activity Log API URL with date filter and field selection.

    Uses ``$select`` to request only the fields we need, reducing payload
    size on large subscriptions. The ``$filter`` only supports eventTimestamp,
    resourceType, resourceGroupName, and resourceId — status and
    operationName must be filtered in code.
    """
    start_time = (datetime.now(UTC) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_time = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    odata_filter = (
        f"eventTimestamp ge '{start_time}' and eventTimestamp le '{end_time}'"
        " and resourceType eq 'Microsoft.Compute/virtualMachines'"
    )
    select = "eventTimestamp,operationName,resourceId,status,subStatus,properties,correlationId"
    return (
        f"{AZURE_MGMT_URL}/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Insights/eventtypes/management/values"
        f"?api-version={ACTIVITY_LOG_API}&$filter={odata_filter}&$select={select}"
    )


def iter_allocation_events(
    subscription_id: str,
    *,
    lookback_days: int = 7,
    tenant_id: str | None = None,
) -> Iterator[tuple[dict[str, list[dict[str, Any]]], int, dict[str, int]]]:
    """Yield ``(events_by_vm, days_covered, stats)`` after each Activity Log page.

    *days_covered* is the number of lookback days covered so far, computed
    from the oldest event timestamp seen. On a cache hit the generator
    yields once with ``days_covered=lookback_days``.
    *stats* contains ``pages``, ``raw_events``, and ``filtered_events``.
    Each yield contains the cumulative, deduplicated events collected so far.
    """
    cache_key = f"events:{subscription_id}:{lookback_days}"
    hit = _cached(cache_key, _CACHE_TTL_EVENTS)
    if hit is not None:
        total = sum(len(v) for v in hit.values())
        yield hit, lookback_days, {"pages": 0, "raw_events": 0, "filtered_events": total}
        return

    url: str | None = _activity_log_url(subscription_id, lookback_days)
    best_events: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}
    page_num = 0
    raw_total = 0
    now = datetime.now(UTC)
    oldest_ts = now
    t0 = time.monotonic()

    while url:
        page_num += 1
        response = arm_get(url, tenant_id=tenant_id)
        raw_page = response.get("value", [])
        url = response.get("nextLink")
        raw_total += len(raw_page)

        # Track oldest event timestamp to compute days covered.
        # Activity Log returns events newest-first, so the last event
        # on each page is the oldest so far.
        for evt in raw_page:
            ts_str = evt.get("eventTimestamp", "")
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts < oldest_ts:
                        oldest_ts = ts
                except ValueError:
                    pass

        days_covered = min(int((now - oldest_ts).total_seconds() / 86400) + 1, lookback_days)

        _process_raw_events(raw_page, best_events)
        events_by_vm = _build_events_by_vm(best_events)
        filtered = sum(len(v) for v in events_by_vm.values())
        yield (
            events_by_vm,
            days_covered,
            {
                "pages": page_num,
                "raw_events": raw_total,
                "filtered_events": filtered,
            },
        )

    final = _build_events_by_vm(best_events)
    logger.info(
        "iter_allocation_events: %d events across %d VMs (lookback=%dd, %d pages, %.1fs)",
        sum(len(v) for v in final.values()),
        len(final),
        lookback_days,
        page_num,
        time.monotonic() - t0,
    )
    _cache_set(cache_key, final)


def get_allocation_events(
    subscription_id: str,
    *,
    lookback_days: int = 7,
    tenant_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Get VM allocation events from the Activity Log.

    Returns a dict keyed by VM resource ID (lowercased) → list of events.
    Each event has: timestamp, operation, status, error_code (if failed).
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for events_by_vm, _, _ in iter_allocation_events(
        subscription_id, lookback_days=lookback_days, tenant_id=tenant_id
    ):
        result = events_by_vm
    return result


def compute_uptime_pct(
    events: list[dict[str, Any]],
    lookback_days: int,
    power_state: str = "unknown",
) -> float:
    """Estimate uptime percentage from allocation events.

    Assumes the VM was running at the start of the lookback window
    unless the first event is a 'start'. If no events exist, uses
    the current power state: running → 100%, otherwise 0%.
    """
    if not events:
        return 100.0 if power_state == "running" else 0.0

    now = datetime.now(UTC)
    window_start = now - timedelta(days=lookback_days)

    running = True  # assume running at window start
    last_ts = window_start
    running_seconds = 0.0

    for event in events:
        try:
            ts = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue

        if ts < window_start:
            ts = window_start

        if running:
            running_seconds += (ts - last_ts).total_seconds()

        op = event.get("operation", "")
        status = event.get("status", "").lower()

        if op == "start" and status == "succeeded":
            running = True
        elif op in ("deallocate", "powerOff") and status == "succeeded":
            running = False
        elif op == "start" and status in ("failed", "failure"):
            running = False  # failed to start → stays off

        last_ts = ts

    # Account for time from last event to now
    if running:
        running_seconds += (now - last_ts).total_seconds()

    total_seconds = lookback_days * 86400
    return round(min(running_seconds / total_seconds * 100, 100.0), 1)


def _extract_rg(resource_id: str) -> str:
    """Extract resource group name from an ARM resource ID."""
    parts = resource_id.split("/")
    for i, part in enumerate(parts):
        if part.lower() == "resourcegroups" and i + 1 < len(parts):
            return parts[i + 1]
    return ""
