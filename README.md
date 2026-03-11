# az-scout-plugin-odcr-coverage

An [az-scout](https://github.com/az-scout/az-scout) plugin that analyses **On-Demand Capacity Reservation (ODCR) coverage** for Azure VMs.

Identifies VMs at risk of allocation failure by combining VM inventory, Activity Log allocation events, and existing ODCR utilization — then classifies each VM by risk level.

## Features

- **Risk classification** — critical (past allocation failures), high (always-on without ODCR), medium, low, covered
- **Allocation event timeline** — start/deallocate/failed events from Activity Log with configurable lookback (3–30 days)
- **ODCR utilization** — reserved capacity, used/unused counts per reservation group
- **Multi-subscription** — analyse across multiple subscriptions with incremental rendering and progress bar
- **VM detail modal** — click any VM row to see risk profile, allocation summary, and event timeline
- **Cross-highlighting** — hover an ODCR card to highlight associated VMs, and vice versa
- **Azure Portal links** — direct links to each VM in the Azure Portal
- **MCP tool** — `get_odcr_coverage` available to AI agents (Claude, VS Code Copilot)
- **Chat mode** — "ODCR Advisor" for guided capacity reservation analysis
- **Caching** — VM list (5 min), capacity reservations (5 min), activity log (10 min)

## Installation

```bash
pip install az-scout-plugin-odcr-coverage
# or
uv pip install az-scout-plugin-odcr-coverage
```

Restart az-scout — the plugin is discovered automatically.

## MCP Tool

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_odcr_coverage` | `region`, `subscription_id`, `lookback_days?` (default: 7), `uptime_threshold?` (default: 90), `tenant_id?` | Analyse ODCR coverage with per-VM risk classification and allocation history |

## RBAC Requirements

| Role | Why |
|------|-----|
| **Reader** | List VMs, Capacity Reservation Groups |
| **Reader** or **Monitoring Reader** | Activity Log queries |
cp -r /tmp/az-scout/docs/plugin-scaffold ./az-scout-myplugin
cd ./az-scout-myplugin

# Update pyproject.toml: name, entry point, package name
# Rename src/az_scout_odcr_coverage/ to match your package

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
3. When both are set, it fetches subscriptions from `/api/subscriptions`.
4. The user picks a subscription and clicks the button.
5. The plugin calls `GET /plugins/odcr-coverage/hello?subscription_name=…&tenant=…&region=…`.

## Quality checks

The scaffold includes GitHub Actions workflows in `.github/workflows/`:

- **`ci.yml`** — Runs lint (ruff + mypy) and tests (pytest) on Python 3.11–3.13, triggered on push/PR to `main`.
- **`publish.yml`** — Builds, creates a GitHub Release, and publishes to PyPI via trusted publishing (OIDC). Triggered on version tags (`v*`). Requires a `pypi` environment configured in your repo settings with OIDC trusted publishing.

Run the same checks locally:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
uv run pytest
```

To publish a release:

```bash
git tag v2026.2.0
git push origin v2026.2.0
```

## Copilot support

The `.github/copilot-instructions.md` file provides context to GitHub Copilot about
the plugin structure, conventions, and az-scout plugin API. It helps Copilot generate
code that follows the project patterns.


## License

[MIT](LICENSE.txt)

## Disclaimer

> **This tool is not affiliated with Microsoft.** All capacity, pricing, and latency information are indicative and not a guarantee of deployment success. Spot placement scores are probabilistic. Quota values and pricing are dynamic and may change between planning and actual deployment. Latency values are based on [Microsoft published statistics](https://learn.microsoft.com/en-us/azure/networking/azure-network-latency) and must be validated with in-tenant measurements.
