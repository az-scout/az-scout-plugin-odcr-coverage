"""Azure ARM helpers for ODCR coverage analysis.

Fetches VMs (with ODCR association), Capacity Reservation Groups,
and Activity Log allocation events.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from az_scout.azure_api import ArmRequestError, arm_paginate

logger = logging.getLogger(__name__)

AZURE_MGMT_URL = "https://management.azure.com"
COMPUTE_API = "2024-03-01"
ACTIVITY_LOG_API = "2015-04-01"

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

# Allocation failure error codes
_ALLOCATION_ERROR_CODES = {
    "AllocationFailed",
    "OverconstrainedAllocationRequest",
    "OverconstrainedZonalAllocationRequest",
    "AllocationTimedOut",
}


def list_vms(
    subscription_id: str,
    *,
    region: str | None = None,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """List VMs with capacity reservation info.

    Returns a list of VM records with normalized fields.
    Power state is derived from allocation events rather than instanceView
    to avoid the $expand=instanceView limitation on list endpoints.
    """
    cache_key = f"vms:{subscription_id}:{region or ''}"
    hit = _cached(cache_key, _CACHE_TTL_VM)
    if hit is not None:
        return hit  # type: ignore[no-any-return]

    url = (
        f"{AZURE_MGMT_URL}/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Compute/virtualMachines"
        f"?api-version={COMPUTE_API}&$statusOnly=true"
    )
    raw_vms = arm_paginate(url, tenant_id=tenant_id)

    vms: list[dict[str, Any]] = []
    for vm in raw_vms:
        location = vm.get("location", "").lower().replace(" ", "")
        if region and location != region.lower():
            continue

        props = vm.get("properties", {})
        zones = vm.get("zones", [])

        # Power state — may not be available without instanceView expand.
        # We'll infer it from allocation events later if missing.
        power_state = "unknown"
        for status in props.get("instanceView", {}).get("statuses", []):
            code = status.get("code", "")
            if code.startswith("PowerState/"):
                power_state = code.split("/", 1)[1]

        # Check ODCR association
        cr_group = props.get("capacityReservation", {}).get("capacityReservationGroup", {})
        odcr_group_id = cr_group.get("id") if cr_group else None

        vms.append(
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
        "list_vms: %d VMs in %s (region=%s)",
        len(vms),
        subscription_id[:8],
        region or "all",
    )
    _cache_set(cache_key, vms)
    return vms


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
    cache_key = f"events:{subscription_id}:{lookback_days}"
    hit = _cached(cache_key, _CACHE_TTL_EVENTS)
    if hit is not None:
        return hit  # type: ignore[no-any-return]

    start_time = (datetime.now(UTC) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # The Activity Log $filter only supports a limited set of fields
    # (eventTimestamp, resourceType, status, etc.). operationName is NOT
    # supported as a filter — we fetch all VM events and filter in code.
    odata_filter = (
        f"eventTimestamp ge '{start_time}' and resourceType eq 'Microsoft.Compute/virtualMachines'"
    )

    url = (
        f"{AZURE_MGMT_URL}/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Insights/eventtypes/management/values"
        f"?api-version={ACTIVITY_LOG_API}&$filter={odata_filter}"
    )

    raw_events = arm_paginate(url, tenant_id=tenant_id)

    # Filter to relevant operations in code
    _op_names = {op.lower() for op in _VM_OPERATIONS}

    events_by_vm: dict[str, list[dict[str, Any]]] = {}
    for event in raw_events:
        # Check operation name
        op_name = event.get("operationName", {})
        if isinstance(op_name, dict):
            op_name = op_name.get("value", "")
        if op_name.lower() not in _op_names:
            continue

        resource_id = (event.get("resourceId") or "").lower()
        if not resource_id:
            continue

        status_obj = event.get("status", {})
        status = status_obj.get("value", "") if isinstance(status_obj, dict) else str(status_obj)

        # For 'write' events, only include actual VM creation or allocation
        # failures — skip tag updates, property changes, etc.
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

        # Extract error code from failed events
        error_code = None
        if status.lower() in ("failed", "failure"):
            try:
                status_msg = json.loads(event.get("properties", {}).get("statusMessage", "{}"))
                error_info = status_msg.get("error", status_msg)
                error_code = error_info.get("code", "")
            except (json.JSONDecodeError, AttributeError):
                error_code = "Unknown"

        # Normalize operation name to short form
        short_op = op_name.rsplit("/", 1)[-1] if "/" in op_name else op_name
        if short_op == "action":
            # e.g. "start/action" → "start"
            parts = op_name.rsplit("/", 2)
            short_op = parts[-2] if len(parts) >= 2 else short_op

        entry: dict[str, Any] = {
            "timestamp": event.get("eventTimestamp", ""),
            "operation": short_op,
            "status": status,
        }
        if error_code:
            entry["error_code"] = error_code

        events_by_vm.setdefault(resource_id, []).append(entry)

    # Sort events by timestamp for each VM
    for vm_events in events_by_vm.values():
        vm_events.sort(key=lambda e: e["timestamp"])

    logger.info(
        "get_allocation_events: %d events across %d VMs (lookback=%dd)",
        sum(len(v) for v in events_by_vm.values()),
        len(events_by_vm),
        lookback_days,
    )
    _cache_set(cache_key, events_by_vm)
    return events_by_vm


def compute_uptime_pct(
    events: list[dict[str, Any]],
    lookback_days: int,
) -> float:
    """Estimate uptime percentage from allocation events.

    Assumes the VM was running at the start of the lookback window
    unless the first event is a 'start'.
    """
    if not events:
        return 0.0

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
