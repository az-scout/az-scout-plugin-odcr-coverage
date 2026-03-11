"""API routes for ODCR coverage plugin."""

from __future__ import annotations

from typing import Any

from az_scout.plugin_api import PluginUpstreamError, PluginValidationError
from fastapi import APIRouter

router = APIRouter()


@router.get("/coverage")
async def get_coverage(
    region: str = "",
    subscription_id: str = "",
    tenant_id: str = "",
    lookback_days: int = 7,
    uptime_threshold: float = 90.0,
) -> dict[str, Any]:
    """Get ODCR coverage analysis for VMs in a region.

    Available at /plugins/odcr-coverage/coverage
    """
    if not region:
        raise PluginValidationError("Region is required")
    if not subscription_id:
        raise PluginValidationError("Subscription ID is required")

    import json

    from az_scout_odcr_coverage.tools import get_odcr_coverage

    try:
        result = get_odcr_coverage(
            region=region,
            subscription_id=subscription_id,
            lookback_days=lookback_days,
            uptime_threshold=uptime_threshold,
            tenant_id=tenant_id or None,
        )
        result_dict: dict[str, Any] = json.loads(result)
        return result_dict
    except Exception as exc:
        raise PluginUpstreamError(f"ODCR coverage analysis failed: {exc}") from exc
