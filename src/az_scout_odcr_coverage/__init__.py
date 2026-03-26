"""az-scout ODCR Coverage plugin.

Analyses On-Demand Capacity Reservation coverage for VMs,
highlighting allocation failure risks and ODCR gaps.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Any

from az_scout.plugin_api import ChatMode, TabDefinition, get_plugin_logger

if TYPE_CHECKING:
    from fastapi import APIRouter

logger = get_plugin_logger("odcr-coverage")

_STATIC_DIR = Path(__file__).parent / "static"

try:
    __version__ = _pkg_version("az-scout-plugin-odcr-coverage")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"


class OdcrCoveragePlugin:
    """ODCR Coverage analysis plugin for az-scout."""

    name = "odcr-coverage"
    version = __version__

    def get_router(self) -> APIRouter | None:
        from az_scout_odcr_coverage.routes import router

        return router

    def get_mcp_tools(self) -> list[Callable[..., Any]] | None:
        from az_scout_odcr_coverage.tools import (
            get_odcr_coverage,
            get_odcr_coverage_summary,
            get_odcr_vm_allocation_history,
        )

        return [get_odcr_coverage, get_odcr_coverage_summary, get_odcr_vm_allocation_history]

    def get_static_dir(self) -> Path | None:
        return _STATIC_DIR

    def get_tabs(self) -> list[TabDefinition] | None:
        return [
            TabDefinition(
                id="odcr-coverage",
                label="ODCR Coverage",
                icon="bi bi-shield-check",
                js_entry="js/odcr-coverage-tab.js",
                css_entry="css/odcr-coverage.css",
            )
        ]

    def get_chat_modes(self) -> list[ChatMode] | None:
        return [
            ChatMode(
                id="odcr-advisor",
                label="ODCR Advisor",
                system_prompt=(
                    "You are an **ODCR Coverage Advisor** for Azure. You help users "
                    "identify VMs at risk of allocation failure and recommend On-Demand "
                    "Capacity Reservations.\n\n"
                    "**Tool selection:**\n"
                    "- Use `get_odcr_coverage_summary` for overview questions (fast, "
                    "no Activity Log).\n"
                    "- Use `get_odcr_vm_allocation_history` to drill into specific "
                    "VMs the user asks about.\n"
                    "- Use `get_odcr_coverage` only when the user explicitly requests "
                    "the full analysis with allocation events for all VMs.\n\n"
                    "Focus on:\n"
                    "- VMs with past allocation failures (critical risk)\n"
                    "- Always-on VMs without ODCR protection\n"
                    "- Unused ODCR capacity (waste)\n"
                    "- Recommending which VMs should be added to Capacity Reservations\n\n"
                    "Present results clearly with risk levels. "
                    "For critical VMs, emphasise urgency."
                ),
                welcome_message=(
                    "**ODCR Coverage Advisor**\n\n"
                    "I can help you identify VMs at risk of allocation failure and "
                    "recommend Capacity Reservations.\n\n"
                    "Try asking:\n"
                    "- [[What is the ODCR coverage for my VMs in this region?]]\n"
                    "- [[Which VMs have had allocation failures in the last 30 days?]]\n"
                    "- [[Are there any always-on VMs without ODCR protection?]]\n"
                    "- [[Show unused capacity reservations across my subscriptions]]\n"
                    "- [[What is the allocation failure rate for Standard_D16s_v5?]]"
                ),
            )
        ]

    def get_system_prompt_addendum(self) -> str | None:
        return (
            "For ODCR / Capacity Reservation questions, prefer "
            "`get_odcr_coverage_summary` for quick overview (fast, no Activity Log). "
            "Use `get_odcr_vm_allocation_history` to drill into specific VMs. "
            "Only use `get_odcr_coverage` when the user explicitly requests the "
            "full analysis with allocation events for all VMs."
        )


# Module-level instance — referenced by the entry point
plugin = OdcrCoveragePlugin()
