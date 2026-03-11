"""Tests for ODCR coverage plugin."""

from __future__ import annotations

from typing import Any

from az_scout_odcr_coverage.azure_api import compute_uptime_pct
from az_scout_odcr_coverage.tools import _build_coverage_report

_SUB = "/subscriptions/s"
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
            {"timestamp": "2026-03-04T00:00:00Z", "operation": "start", "status": "Succeeded"},
        ]
        pct = compute_uptime_pct(events, 7)
        assert pct > 95.0

    def test_start_then_deallocate(self) -> None:
        events = [
            {"timestamp": "2026-03-04T00:00:00Z", "operation": "start", "status": "Succeeded"},
            {"timestamp": "2026-03-07T12:00:00Z", "operation": "deallocate", "status": "Succeeded"},
        ]
        pct = compute_uptime_pct(events, 7)
        assert 30.0 < pct < 60.0

    def test_failed_start_stays_off(self) -> None:
        events = [
            {"timestamp": "2026-03-04T00:00:00Z", "operation": "deallocate", "status": "Succeeded"},
            {"timestamp": "2026-03-05T00:00:00Z", "operation": "start", "status": "Failed"},
        ]
        pct = compute_uptime_pct(events, 7)
        # VM was assumed running at start, then deallocated, then failed to start
        assert pct < 50.0


class TestBuildCoverageReport:
    """Tests for _build_coverage_report()."""

    def test_empty_inputs(self) -> None:
        result = _build_coverage_report([], [], {}, 7, 90.0)
        assert result["summary"]["total_vms"] == 0
        assert result["vms"] == []
        assert result["odcr_utilization"]["total_reserved"] == 0

    def test_vm_with_odcr_is_covered(self) -> None:
        gid = f"{_CR_PFX}/g1"
        vms = [_vm(odcr_group_id=gid)]
        reservations = [_reservation(group_id=gid, group_name="g1")]
        result = _build_coverage_report(vms, reservations, {}, 7, 90.0)
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
        result = _build_coverage_report([vm], [], events, 7, 90.0)
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
        result = _build_coverage_report([vm], [], events, 7, 90.0)
        assert result["summary"]["uncovered_critical"] == 1
        assert result["vms"][0]["risk"] == "critical"
        assert result["vms"][0]["allocation_summary"]["failed"] == 1

    def test_stopped_vm_is_low_risk(self) -> None:
        vm = _vm(power_state="deallocated")
        result = _build_coverage_report([vm], [], {}, 7, 90.0)
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
        result = _build_coverage_report(vms, reservations, {}, 7, 90.0)
        util = result["odcr_utilization"]
        assert util["total_reserved"] == 3
        assert util["total_used"] == 1
        assert util["total_unused"] == 2

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
        result = _build_coverage_report(vms, [_reservation(group_id=gid)], events, 7, 90.0)
        risks = [v["risk"] for v in result["vms"]]
        assert risks.index("critical") < risks.index("low")
        assert risks.index("low") < risks.index("covered")

    def test_summary_odcr_stats(self) -> None:
        reservations = [_reservation(capacity=5)]
        result = _build_coverage_report([], reservations, {}, 7, 90.0)
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
        assert p.get_static_dir() is not None
        assert p.get_tabs() is not None
        assert p.get_chat_modes() is not None
        assert p.get_system_prompt_addendum() is not None
