# az-scout-plugin-odcr-coverage

An [az-scout](https://github.com/az-scout/az-scout) plugin that analyses **On-Demand Capacity Reservation (ODCR) coverage** for Azure VMs.

Identifies VMs at risk of allocation failure by combining VM inventory, Activity Log allocation events, and existing ODCR utilization — then classifies each VM by risk level.

## Features

- **Risk classification** — critical (past allocation failures), high (always-on without ODCR), medium, low, covered
- **Allocation event timeline** — start/deallocate/failed events from Activity Log with configurable lookback (3–30 days)
- **ODCR utilization** — reserved capacity, used/unused counts per reservation group
- **Multi-subscription** — analyse across multiple subscriptions with incremental rendering and progress bar
- **SSE streaming** — two-phase analysis: VMs load instantly, Activity Log events stream page-by-page with day-based progress
- **VM detail modal** — click any VM row to see risk profile, allocation summary, and event timeline
- **Cross-highlighting** — hover an ODCR card to highlight associated VMs, and vice versa
- **Azure Portal links** — direct links to each VM in the Azure Portal
- **MCP tools** — three tools with tiered performance for AI agents:
  - `get_odcr_coverage_summary` — fast overview (no Activity Log)
  - `get_odcr_vm_allocation_history` — drill-down for specific VMs
  - `get_odcr_coverage` — full analysis with allocation events
- **Chat mode** — "ODCR Advisor" with tool selection guidance
- **Caching** — VM list (5 min), capacity reservations (5 min), activity log (10 min)

## Installation

```bash
pip install az-scout-plugin-odcr-coverage
# or
uv pip install az-scout-plugin-odcr-coverage
```

Restart az-scout — the plugin is discovered automatically.

## MCP Tools

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_odcr_coverage_summary` | `region`, `subscription_id`, `tenant_id?` | Fast ODCR overview — VMs + utilization, no Activity Log |
| `get_odcr_vm_allocation_history` | `region`, `subscription_id`, `vm_names`, `lookback_days?` (7), `uptime_threshold?` (90), `tenant_id?` | Allocation events + accurate risk for specific VMs |
| `get_odcr_coverage` | `region`, `subscription_id`, `lookback_days?` (7), `uptime_threshold?` (90), `tenant_id?` | Full analysis with per-VM allocation history |

## RBAC Requirements

| Role | Why |
|------|-----|
| **Reader** | List VMs (via ARG), Capacity Reservation Groups |
| **Reader** or **Monitoring Reader** | Activity Log queries |

## Setup

```bash
uv pip install az-scout-plugin-odcr-coverage
az-scout  # plugin is auto-discovered
```

For development:

```bash
git clone https://github.com/az-scout/az-scout-plugin-odcr-coverage
cd az-scout-plugin-odcr-coverage
uv sync --group dev
uv pip install -e .
az-scout  # plugin is auto-discovered
```

## Structure

```text
az-scout-plugin-odcr-coverage/
├── .github/
│   ├── copilot-instructions.md  # Copilot context for this plugin
│   └── workflows/
│       └── ci.yml               # CI pipeline (lint + test, Python 3.11–3.13)
├── pyproject.toml
├── README.md
└── src/
    └── az_scout_odcr_coverage/
        ├── __init__.py          # Plugin class + module-level `plugin` instance
        ├── routes.py            # FastAPI APIRouter (optional)
        ├── tools.py             # MCP tool functions (optional)
        └── static/
            ├── css/
            │   └── odcr-coverage.css      # Plugin styles (auto-loaded via css_entry)
            ├── html/
            │   └── odcr-coverage-tab.html # HTML fragment (fetched by JS at runtime)
            └── js/
                └── odcr-coverage-tab.js   # Tab UI logic (auto-loaded via js_entry)
```

## How it works

1. The plugin JS loads the HTML fragment into `#plugin-tab-odcr-coverage`.
2. It listens to `azscout:*` context events from the core app.
3. When tenant and region are set, it fetches subscriptions from `/api/subscriptions`.
4. The user picks subscriptions and clicks **Analyse**.
5. For each subscription, an SSE stream (`GET /plugins/odcr-coverage/coverage/stream`) delivers results in two phases:
   - **Phase 1** — VMs and ODCR utilization load instantly via Azure Resource Graph.
   - **Phase 2** — Activity Log events stream page-by-page, progressively enriching VM risk levels and allocation stats. Progress is displayed as days covered (e.g. "events 12/30d").
6. The table, summary cards, and risk bar update incrementally as data arrives.

## Quality checks

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
uv run pytest
```

## Copilot support

The `.github/copilot-instructions.md` file provides context to GitHub Copilot about
the plugin structure, conventions, and az-scout plugin API.

## License

[MIT](LICENSE.txt)

## Disclaimer

> **This tool is not affiliated with Microsoft.** All capacity, pricing, and availability information is indicative and not a guarantee of deployment success. Values are dynamic and may change between planning and actual deployment. Always validate in official Microsoft sources and in your target tenant/subscription.
