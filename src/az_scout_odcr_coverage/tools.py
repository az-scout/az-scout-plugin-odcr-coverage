"""MCP tools for ODCR coverage analysis."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any

from pydantic import Field


def get_odcr_coverage(
    region: Annotated[str, Field(description="Azure region name (e.g. francecentral).")],
    subscription_id: Annotated[str, Field(description="Azure subscription ID (UUID).")],
    lookback_days: Annotated[
        int, Field(description="Number of days to look back for allocation events.")
    ] = 7,
    uptime_threshold: Annotated[
        float,
        Field(description="Uptime percentage threshold to recommend ODCR (0-100)."),
    ] = 90.0,
    tenant_id: Annotated[str | None, Field(description="Optional tenant ID.")] = None,
) -> str:
    """Analyse On-Demand Capacity Reservation (ODCR) coverage for VMs in a region.

    Returns per-VM analysis with allocation event history, uptime percentage,
    ODCR coverage status, and risk level. Also returns a fleet summary with
    existing ODCR utilization and unused capacity.

    Risk levels:
    - critical: VM has past allocation failures and no ODCR
    - high: VM runs ≥ threshold uptime, no ODCR, deployment confidence < 80
    - medium: VM runs ≥ threshold uptime, no ODCR, confidence ≥ 80
    - low: VM runs < threshold uptime, no ODCR, no failures
    - covered: VM has ODCR protection
    """
    from az_scout_odcr_coverage.azure_api import (
        get_allocation_events,
        list_capacity_reservations,
        list_vms,
    )

    vms: list[dict[str, Any]]
    reservations: list[dict[str, Any]]
    events_by_vm: dict[str, list[dict[str, Any]]]

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_vms = pool.submit(list_vms, subscription_id, region=region, tenant_id=tenant_id)
        fut_res = pool.submit(list_capacity_reservations, subscription_id, tenant_id=tenant_id)
        fut_evt = pool.submit(
            get_allocation_events,
            subscription_id,
            lookback_days=lookback_days,
            tenant_id=tenant_id,
        )
        vms = fut_vms.result()
        reservations = fut_res.result()
        events_by_vm = fut_evt.result()

    # Filter reservations to the target region
    region_reservations = [r for r in reservations if r["location"] == region.lower()]

    result = _build_coverage_report(
        vms, region_reservations, events_by_vm, lookback_days, uptime_threshold
    )
    return json.dumps(result, indent=2)


def _build_coverage_report(
    vms: list[dict[str, Any]],
    reservations: list[dict[str, Any]],
    events_by_vm: dict[str, list[dict[str, Any]]],
    lookback_days: int,
    uptime_threshold: float,
) -> dict[str, Any]:
    """Build the full coverage report from raw data."""
    from az_scout_odcr_coverage.azure_api import compute_uptime_pct

    # Build ODCR group ID → name lookup
    odcr_group_names: dict[str, str] = {}
    for r in reservations:
        odcr_group_names[r["group_id"].lower()] = r["group_name"]

    vm_reports: list[dict[str, Any]] = []
    summary = {
        "total_vms": len(vms),
        "covered": 0,
        "uncovered_critical": 0,
        "uncovered_high": 0,
        "uncovered_medium": 0,
        "uncovered_low": 0,
        "total_allocation_attempts": 0,
        "total_allocation_failures": 0,
    }

    for vm in vms:
        vm_id_lower = vm["id"].lower()
        events = events_by_vm.get(vm_id_lower, [])

        # Infer power state from events if not available from ARG/instanceView
        power_state = vm["power_state"]
        if power_state == "unknown" and events:
            last_event = events[-1]
            last_op = last_event.get("operation", "")
            last_status = last_event.get("status", "").lower()
            if last_op == "start" and last_status == "succeeded":
                power_state = "running"
            elif last_op in ("deallocate", "powerOff") and last_status == "succeeded":
                power_state = "deallocated"

        # Compute uptime
        uptime_pct = compute_uptime_pct(events, lookback_days, power_state=power_state)

        # Allocation stats
        alloc_attempts = sum(1 for e in events if e["operation"] in ("start", "write"))
        alloc_failures = sum(
            1
            for e in events
            if e["operation"] in ("start", "write")
            and e.get("status", "").lower() in ("failed", "failure")
        )
        last_failure = next(
            (
                e["timestamp"]
                for e in reversed(events)
                if e.get("status", "").lower() in ("failed", "failure")
            ),
            None,
        )

        summary["total_allocation_attempts"] += alloc_attempts
        summary["total_allocation_failures"] += alloc_failures

        # Determine risk
        if vm["has_odcr"]:
            risk = "covered"
            risk_reason = "Protected by Capacity Reservation"
            summary["covered"] += 1
        elif alloc_failures > 0:
            risk = "critical"
            risk_reason = (
                f"{alloc_failures} allocation failure(s) in last {lookback_days}d, no ODCR"
            )
            summary["uncovered_critical"] += 1
        elif uptime_pct >= uptime_threshold:
            risk = "high"
            risk_reason = f"Uptime {uptime_pct}% (≥{uptime_threshold}%), always-on without ODCR"
            summary["uncovered_high"] += 1
        elif uptime_pct > 0:
            risk = "medium"
            risk_reason = f"Uptime {uptime_pct}%, no ODCR"
            summary["uncovered_medium"] += 1
        else:
            risk = "low"
            risk_reason = "Stopped or minimal usage, no ODCR"
            summary["uncovered_low"] += 1

        vm_reports.append(
            {
                "name": vm["name"],
                "resource_id": vm["id"],
                "resource_group": vm["resource_group"],
                "vm_size": vm["vm_size"],
                "zone": vm["zone"],
                "power_state": power_state,
                "has_odcr": vm["has_odcr"],
                "odcr_group_name": odcr_group_names.get(
                    (vm.get("odcr_group_id") or "").lower(), ""
                ),
                "uptime_pct": uptime_pct,
                "allocation_events": events,
                "allocation_summary": {
                    "total_attempts": alloc_attempts,
                    "succeeded": alloc_attempts - alloc_failures,
                    "failed": alloc_failures,
                    "failure_rate_pct": (
                        round(alloc_failures / alloc_attempts * 100, 1) if alloc_attempts else 0.0
                    ),
                    "last_failure": last_failure,
                },
                "risk": risk,
                "risk_reason": risk_reason,
            }
        )

    # Sort: critical first, then high, medium, low, covered
    risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "covered": 4}
    vm_reports.sort(key=lambda v: risk_order.get(v["risk"], 99))

    # ODCR utilization — compute 'used' by counting VMs per group per zone
    vms_per_group_zone: dict[str, int] = {}
    for vm in vms:
        gid = vm.get("odcr_group_id")
        if gid:
            zone = vm.get("zone") or ""
            key = f"{gid.lower()}:{zone}"
            vms_per_group_zone[key] = vms_per_group_zone.get(key, 0) + 1

    reservation_details = []
    total_reserved = 0
    total_used = 0
    for r in reservations:
        zone = r["zone"] or ""
        key = f"{r['group_id'].lower()}:{zone}"
        used = vms_per_group_zone.get(key, 0)
        total_reserved += r["capacity"]
        total_used += used
        reservation_details.append(
            {
                "group": r["group_name"],
                "sku": r["sku_name"],
                "zone": r["zone"],
                "capacity": r["capacity"],
                "used": used,
                "unused": r["capacity"] - used,
            }
        )

    total_unused = total_reserved - total_used
    odcr_summary = {
        "total_reserved": total_reserved,
        "total_used": total_used,
        "total_unused": total_unused,
        "reservations": reservation_details,
    }

    # Add ODCR stats to the top-level summary
    summary["odcr_total_reserved"] = total_reserved
    summary["odcr_total_used"] = total_used
    summary["odcr_total_unused"] = total_unused

    return {
        "summary": summary,
        "odcr_utilization": odcr_summary,
        "vms": vm_reports,
    }
