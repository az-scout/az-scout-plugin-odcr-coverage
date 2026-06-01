"""Tests for ODCR coverage plugin."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from az_scout_odcr_coverage.azure_api import compute_uptime_pct
from az_scout_odcr_coverage.tools import build_coverage_report

_SUB = "/subscriptions/s"


def _ts(days_ago: float) -> str:
    """Return an ISO timestamp for `days_ago` days before now."""
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


_RG = f"{_SUB}/resourceGroups/rg"
_VM_PFX = f"{_RG}/providers/Microsoft.Compute/virtualMachines"
_CR_PFX = f"{_RG}/providers/Microsoft.Compute/capacityReservationGroups"


def _vm(
    name: str = "vm-1",
    vm_id: str = "",
    vm_size: str = "Standard_D2s_v5",
    zone: str | None = "1",
    power_state: str = "running",
    odcr_group_id: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "id": vm_id or f"{_VM_PFX}/{name}",
        "resource_group": "rg",
        "vm_size": vm_size,
        "location": "eastus",
        "zone": zone,
        "power_state": power_state,
        "odcr_group_id": odcr_group_id,
        "has_odcr": odcr_group_id is not None,
    }


def _reservation(
    group_name: str = "odcr-group-1",
    group_id: str = "",
    sku: str = "Standard_D2s_v5",
    capacity: int = 2,
    zone: str | None = "1",
) -> dict[str, Any]:
    return {
        "group_name": group_name,
        "group_id": group_id or f"{_CR_PFX}/{group_name}",
        "reservation_name": "res-1",
        "location": "eastus",
        "zone": zone,
        "sku_name": sku,
        "capacity": capacity,
    }


class TestComputeUptimePct:
    """Tests for compute_uptime_pct()."""

    def test_no_events(self) -> None:
        assert compute_uptime_pct([], 7) == 0.0

    def test_always_running(self) -> None:
        events = [
            {"timestamp": _ts(5), "operation": "start", "status": "Succeeded"},
        ]
        pct = compute_uptime_pct(events, 7)
        assert pct > 65.0

    def test_start_then_deallocate(self) -> None:
        events = [
            {"timestamp": _ts(5), "operation": "start", "status": "Succeeded"},
            {"timestamp": _ts(2), "operation": "deallocate", "status": "Succeeded"},
        ]
        pct = compute_uptime_pct(events, 7)
        # Running from window start → deallocate at day 2: ~71%
        assert 60.0 < pct < 80.0

    def test_failed_start_stays_off(self) -> None:
        events = [
            {"timestamp": _ts(5), "operation": "deallocate", "status": "Succeeded"},
            {"timestamp": _ts(4), "operation": "start", "status": "Failed"},
        ]
        pct = compute_uptime_pct(events, 7)
        # VM was assumed running at start, then deallocated, then failed to start
        assert pct < 50.0


class TestBuildCoverageReport:
    """Tests for build_coverage_report()."""

    def test_empty_inputs(self) -> None:
        result = build_coverage_report([], [], {}, 7, 90.0)
        assert result["summary"]["total_vms"] == 0
        assert result["vms"] == []
        assert result["odcr_utilization"]["total_reserved"] == 0

    def test_vm_with_odcr_is_covered(self) -> None:
        gid = f"{_CR_PFX}/g1"
        vms = [_vm(odcr_group_id=gid)]
        reservations = [_reservation(group_id=gid, group_name="g1")]
        result = build_coverage_report(vms, reservations, {}, 7, 90.0)
        assert result["summary"]["covered"] == 1
        assert result["vms"][0]["risk"] == "covered"
        assert result["vms"][0]["odcr_group_name"] == "g1"

    def test_vm_with_high_uptime_no_odcr(self) -> None:
        vm = _vm()
        vm_id = vm["id"].lower()
        events = {
            vm_id: [
                {"timestamp": "2026-03-04T00:00:00Z", "operation": "start", "status": "Succeeded"},
            ]
        }
        result = build_coverage_report([vm], [], events, 7, 90.0)
        assert result["summary"]["uncovered_high"] == 1
        assert result["vms"][0]["risk"] == "high"

    def test_vm_with_allocation_failure_is_critical(self) -> None:
        vm = _vm()
        vm_id = vm["id"].lower()
        events = {
            vm_id: [
                {
                    "timestamp": "2026-03-05T00:00:00Z",
                    "operation": "start",
                    "status": "Failed",
                    "error_code": "AllocationFailed",
                },
                {"timestamp": "2026-03-05T01:00:00Z", "operation": "start", "status": "Succeeded"},
            ]
        }
        result = build_coverage_report([vm], [], events, 7, 90.0)
        assert result["summary"]["uncovered_critical"] == 1
        assert result["vms"][0]["risk"] == "critical"
        assert result["vms"][0]["allocation_summary"]["failed"] == 1

    def test_stopped_vm_is_low_risk(self) -> None:
        vm = _vm(power_state="deallocated")
        result = build_coverage_report([vm], [], {}, 7, 90.0)
        assert result["vms"][0]["risk"] == "low"

    def test_odcr_utilization_counts(self) -> None:
        gid = f"{_CR_PFX}/g1"
        vms = [
            _vm(
                name="vm-1",
                vm_id=gid.replace("capacityReservationGroups", "virtualMachines") + "/vm-1",
                odcr_group_id=gid,
            ),
            _vm(
                name="vm-2",
                vm_id=gid.replace("capacityReservationGroups", "virtualMachines") + "/vm-2",
            ),
        ]
        reservations = [_reservation(group_id=gid, capacity=3)]
        result = build_coverage_report(vms, reservations, {}, 7, 90.0)
        util = result["odcr_utilization"]
        assert util["total_reserved"] == 3
        assert util["total_used"] == 1
        assert util["total_unused"] == 2

    def test_odcr_utilization_multi_sku_group(self) -> None:
        """A group with two SKUs should only count VMs against the matching SKU."""
        gid = f"{_CR_PFX}/g1"
        # One VM using Standard_D2ls_v5 in the group
        vms = [
            _vm(
                name="vm-d2ls",
                vm_id=f"{_VM_PFX}/vm-d2ls",
                vm_size="Standard_D2ls_v5",
                odcr_group_id=gid,
            ),
        ]
        # Group has two reservations: D2as_v5 (1 slot) and D2ls_v5 (1 slot)
        reservations = [
            _reservation(group_id=gid, group_name="g1", sku="Standard_D2as_v5", capacity=1),
            _reservation(group_id=gid, group_name="g1", sku="Standard_D2ls_v5", capacity=1),
        ]
        result = build_coverage_report(vms, reservations, {}, 7, 90.0)
        util = result["odcr_utilization"]
        # D2as_v5: 0 used, 1 unused — D2ls_v5: 1 used, 0 unused
        assert util["total_reserved"] == 2
        assert util["total_used"] == 1
        assert util["total_unused"] == 1
        res = {r["sku"]: r for r in util["reservations"]}
        assert res["Standard_D2as_v5"]["used"] == 0
        assert res["Standard_D2as_v5"]["unused"] == 1
        assert res["Standard_D2ls_v5"]["used"] == 1
        assert res["Standard_D2ls_v5"]["unused"] == 0

    def test_risk_sort_order(self) -> None:
        gid = f"{_CR_PFX}/g1"
        critical_id = f"{_VM_PFX}/critical-vm"
        vms = [
            _vm(name="covered-vm", odcr_group_id=gid),
            _vm(
                name="low-vm",
                power_state="deallocated",
                vm_id=f"{_VM_PFX}/low-vm",
            ),
            _vm(name="critical-vm", vm_id=critical_id),
        ]
        events = {
            critical_id.lower(): [
                {
                    "timestamp": "2026-03-05T00:00:00Z",
                    "operation": "start",
                    "status": "Failed",
                    "error_code": "AllocationFailed",
                },
                {"timestamp": "2026-03-05T01:00:00Z", "operation": "start", "status": "Succeeded"},
            ]
        }
        result = build_coverage_report(vms, [_reservation(group_id=gid)], events, 7, 90.0)
        risks = [v["risk"] for v in result["vms"]]
        assert risks.index("critical") < risks.index("low")
        assert risks.index("low") < risks.index("covered")

    def test_summary_odcr_stats(self) -> None:
        reservations = [_reservation(capacity=5)]
        result = build_coverage_report([], reservations, {}, 7, 90.0)
        assert result["summary"]["odcr_total_reserved"] == 5
        assert result["summary"]["odcr_total_unused"] == 5


class TestChatCliCommand:
    """Tests for the plugin Click integration."""

    def test_plugin_has_required_attributes(self) -> None:
        from az_scout_odcr_coverage import OdcrCoveragePlugin

        p = OdcrCoveragePlugin()
        assert p.name == "odcr-coverage"
        assert p.get_router() is not None
        assert p.get_mcp_tools() is not None
        assert len(p.get_mcp_tools()) == 3  # type: ignore[arg-type]
        assert p.get_static_dir() is not None
        assert p.get_tabs() is not None
        assert p.get_chat_modes() is not None
        assert p.get_system_prompt_addendum() is not None


class TestGetOdcrCoverageSummary:
    """Tests for the fast summary tool (no Activity Log)."""

    def test_summary_skips_events(self) -> None:
        """Summary tool should produce a report with no allocation events."""
        # build_coverage_report with empty events → no critical/high risk
        vms = [_vm(name="vm-running"), _vm(name="vm-stopped", power_state="deallocated")]
        result = build_coverage_report(vms, [], {}, 7, 90.0)
        assert result["summary"]["total_vms"] == 2
        # Without events: running VM → medium (uptime 100% but no failures)
        # Stopped VM → low
        risks = {v["name"]: v["risk"] for v in result["vms"]}
        assert risks["vm-running"] == "high"  # 100% uptime ≥ threshold
        assert risks["vm-stopped"] == "low"

    def test_summary_includes_odcr_utilization(self) -> None:
        gid = f"{_CR_PFX}/g1"
        vms = [_vm(name="vm-1", odcr_group_id=gid)]
        reservations = [_reservation(group_id=gid, group_name="g1", capacity=3)]
        result = build_coverage_report(vms, reservations, {}, 7, 90.0)
        assert result["summary"]["covered"] == 1
        assert result["odcr_utilization"]["total_reserved"] == 3
        assert result["odcr_utilization"]["total_used"] == 1


class TestGetOdcrVmAllocationHistory:
    """Tests for the VM drill-down tool."""

    def test_filters_to_named_vms(self) -> None:
        """Drill-down should only include VMs matching the requested names."""
        vms = [_vm(name="vm-prod-01"), _vm(name="vm-prod-02"), _vm(name="vm-dev-01")]
        vm_id_1 = vms[0]["id"].lower()
        events = {
            vm_id_1: [
                {
                    "timestamp": _ts(2),
                    "operation": "start",
                    "status": "Failed",
                    "error_code": "AllocationFailed",
                },
                {"timestamp": _ts(1), "operation": "start", "status": "Succeeded"},
            ]
        }
        # Simulate the filter that get_odcr_vm_allocation_history does
        name_set = {"vm-prod-01"}
        filtered_vms = [vm for vm in vms if vm["name"].lower() in name_set]
        result = build_coverage_report(filtered_vms, [], events, 7, 90.0)
        assert result["summary"]["total_vms"] == 1
        assert result["vms"][0]["name"] == "vm-prod-01"
        assert result["vms"][0]["risk"] == "critical"
        assert result["vms"][0]["allocation_summary"]["failed"] == 1


class TestStreamingRoute:
    """Tests for the SSE streaming endpoint helpers."""

    def test_sse_format(self) -> None:
        from az_scout_odcr_coverage.routes import _sse

        result = _sse("vms", {"count": 42})
        assert result.startswith("event: vms\n")
        assert "data: " in result
        assert result.endswith("\n\n")
        # Data should be valid JSON
        import json

        data_line = result.split("data: ")[1].split("\n")[0]
        parsed = json.loads(data_line)
        assert parsed["count"] == 42

    def testbuild_coverage_report_empty_events_gives_preliminary_risk(self) -> None:
        """Phase-1 report (empty events) should produce preliminary risk levels."""
        vms = [
            _vm(name="running-vm", power_state="running"),
            _vm(name="stopped-vm", power_state="deallocated"),
        ]
        gid = f"{_CR_PFX}/g1"
        covered_vm = _vm(name="covered-vm", odcr_group_id=gid)
        vms.append(covered_vm)
        reservations = [_reservation(group_id=gid, group_name="g1")]

        result = build_coverage_report(vms, reservations, {}, 7, 90.0)
        risk_map = {v["name"]: v["risk"] for v in result["vms"]}
        assert risk_map["covered-vm"] == "covered"
        assert risk_map["stopped-vm"] == "low"
        # Without events, running VM gets "high" (100% uptime ≥ 90% threshold)
        assert risk_map["running-vm"] == "high"


class TestIterAllocationEvents:
    """Tests for the page-by-page Activity Log iterator."""

    def test_process_raw_events_filters_and_deduplicates(self) -> None:
        from az_scout_odcr_coverage.azure_api import _build_events_by_vm, _process_raw_events

        best_events: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}

        raw_events = [
            # Relevant start event (Started → should be overwritten by Succeeded)
            {
                "operationName": {"value": "Microsoft.Compute/virtualMachines/start/action"},
                "resourceId": f"{_VM_PFX}/vm-1",
                "status": {"value": "Started"},
                "eventTimestamp": _ts(2),
                "correlationId": "c1",
            },
            {
                "operationName": {"value": "Microsoft.Compute/virtualMachines/start/action"},
                "resourceId": f"{_VM_PFX}/vm-1",
                "status": {"value": "Succeeded"},
                "eventTimestamp": _ts(2),
                "correlationId": "c1",
            },
            # Irrelevant operation — should be filtered out
            {
                "operationName": {"value": "Microsoft.Compute/virtualMachines/extensions/write"},
                "resourceId": f"{_VM_PFX}/vm-1",
                "status": {"value": "Succeeded"},
                "eventTimestamp": _ts(1),
                "correlationId": "c2",
            },
        ]

        _process_raw_events(raw_events, best_events)
        events_by_vm = _build_events_by_vm(best_events)

        vm_key = f"{_VM_PFX}/vm-1".lower()
        assert vm_key in events_by_vm
        assert len(events_by_vm[vm_key]) == 1  # deduplicated: only Succeeded kept
        assert events_by_vm[vm_key][0]["status"] == "Succeeded"

    def test_process_raw_events_accumulates_across_pages(self) -> None:
        from az_scout_odcr_coverage.azure_api import _build_events_by_vm, _process_raw_events

        best_events: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}

        # Page 1: start event for vm-1
        page1 = [
            {
                "operationName": {"value": "Microsoft.Compute/virtualMachines/start/action"},
                "resourceId": f"{_VM_PFX}/vm-1",
                "status": {"value": "Succeeded"},
                "eventTimestamp": _ts(3),
                "correlationId": "c1",
            },
        ]
        _process_raw_events(page1, best_events)
        r1 = _build_events_by_vm(best_events)
        assert len(r1) == 1  # 1 VM after page 1

        # Page 2: deallocate event for vm-2
        page2 = [
            {
                "operationName": {"value": "Microsoft.Compute/virtualMachines/deallocate/action"},
                "resourceId": f"{_VM_PFX}/vm-2",
                "status": {"value": "Succeeded"},
                "eventTimestamp": _ts(2),
                "correlationId": "c2",
            },
        ]
        _process_raw_events(page2, best_events)
        r2 = _build_events_by_vm(best_events)
        assert len(r2) == 2  # 2 VMs after page 2 (cumulative)


class TestProgressSseEvent:
    """Tests for progress SSE event format."""

    def test_progress_event_contains_day_fields(self) -> None:
        import json

        from az_scout_odcr_coverage.routes import _sse

        progress = _sse(
            "progress",
            {
                "days_covered": 12,
                "lookback_days": 30,
            },
        )
        assert progress.startswith("event: progress\n")
        data_line = progress.split("data: ")[1].split("\n")[0]
        parsed = json.loads(data_line)
        assert parsed["days_covered"] == 12
        assert parsed["lookback_days"] == 30
