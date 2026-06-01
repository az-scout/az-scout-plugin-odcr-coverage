"""API routes for ODCR coverage plugin."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from az_scout.plugin_api import PluginUpstreamError, PluginValidationError
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

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

    from az_scout_odcr_coverage.tools import get_odcr_coverage

    try:
        result = await asyncio.to_thread(
            get_odcr_coverage,
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


@router.get("/coverage/stream")
async def stream_coverage(
    region: str = "",
    subscription_id: str = "",
    tenant_id: str = "",
    lookback_days: int = 7,
    uptime_threshold: float = 90.0,
) -> StreamingResponse:
    """Stream ODCR coverage analysis via Server-Sent Events.

    Phase 1 ("vms"): fast — returns VMs + ODCR utilization with preliminary risk.
    Phase 2 ("enriched"): slower — enriches VMs with Activity Log events + accurate risk.
    Final ("done"): signals completion.
    """
    if not region:
        raise PluginValidationError("Region is required")
    if not subscription_id:
        raise PluginValidationError("Subscription ID is required")

    async def event_stream() -> AsyncIterator[str]:
        from az_scout_odcr_coverage.azure_api import (
            iter_allocation_events,
            list_capacity_reservations,
            list_vms,
        )
        from az_scout_odcr_coverage.tools import build_coverage_report

        tid = tenant_id or None

        # Phase 1: fetch VMs + reservations (fast, skip Activity Log)
        try:
            vms, reservations = await asyncio.gather(
                asyncio.to_thread(list_vms, subscription_id, region=region, tenant_id=tid),
                asyncio.to_thread(list_capacity_reservations, subscription_id, tenant_id=tid),
            )
            region_reservations = [r for r in reservations if r["location"] == region.lower()]

            # Build preliminary report with empty events
            phase1 = build_coverage_report(
                vms, region_reservations, {}, lookback_days, uptime_threshold
            )
            yield _sse("vms", phase1)
        except Exception as exc:
            yield _sse("error", {"message": f"VM listing failed: {exc}"})
            yield _sse("done", {})
            return

        # Phase 2: page-by-page Activity Log enrichment
        yield _sse("progress", {"days_covered": 0, "lookback_days": lookback_days})

        queue: asyncio.Queue[Any] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        thread_error: list[str] = []

        def _fetch_pages() -> None:
            try:
                for events_by_vm, days_covered, stats in iter_allocation_events(
                    subscription_id, lookback_days=lookback_days, tenant_id=tid
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, (events_by_vm, days_covered, stats))
            except Exception as exc:
                thread_error.append(str(exc))
            loop.call_soon_threadsafe(queue.put_nowait, None)

        fut = loop.run_in_executor(None, _fetch_pages)
        fut.add_done_callback(
            lambda f: (
                logger.error("_fetch_pages raised: %s", f.exception())
                if not f.cancelled() and f.exception()
                else None
            )
        )

        while True:
            item = await queue.get()
            if item is None:
                break
            events_by_vm, days_covered, stats = item
            enriched = build_coverage_report(
                vms, region_reservations, events_by_vm, lookback_days, uptime_threshold
            )
            yield _sse(
                "progress",
                {
                    "days_covered": days_covered,
                    "lookback_days": lookback_days,
                    **stats,
                },
            )
            yield _sse("enriched", enriched)

        if thread_error:
            yield _sse("error", {"message": f"Activity Log fetch failed: {thread_error[0]}"})

        yield _sse("done", {})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: Any) -> str:
    """Format a Server-Sent Event."""
    payload = json.dumps(data, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"
