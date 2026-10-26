# Browser Dashboard Regression Checklist

- Target: dashboard release baseline `Stitch handoff` implementation
- Baseline label: `Stitch handoff applied, not runtime integrated`

Checklist (must pass after any dashboard UI change):

1. `/app/` route loads successfully.
2. Left sidebar is visible.
3. Top command bar is visible.
4. Status strip is visible.
5. Recent runs table/list is visible.
6. Profile portfolio renders; no severe overflow in narrow width.
7. Profile drawer opens and closes.
8. `fallback_only` is visible when provider is fallback mode.
9. `Seam prepared` is visible in status.
10. No forbidden text contains `Integrated`/`Runtime Stitch sync`/`Full Stitch MCP integration` in dashboard status UI.
11. High-priority actions are present and named:
    - Add/Edit profile
    - Start selected profiles
    - Compare selected runs
    - Open rerun entry
    - Open latest report
12. Long-text stress validation is executed and remains readable (single line truncation/tooltip path for long values).

Execution command baseline:

- `uv run --with playwright python tools/browser_dashboard_settings_smoke.py --base-url <local-base-url> --capture-screenshots --long-text-stress`

Captured regression screenshots:

- `design/output/ui-polish-v2-dashboard-wide-1920.png`
- `design/output/ui-polish-v2-dashboard-1440.png`
- `design/output/ui-polish-v2-dashboard-narrow.png`
- `design/output/ui-polish-v2-profile-drawer.png`
- `design/output/ui-polish-v2-long-text-stress.png`

Latest captured baseline:

- [design/output/ui-polish-v2-dashboard-wide-1920.png](/xrml-arxr-research-agent-starter-allinone/design/output/ui-polish-v2-dashboard-wide-1920.png)
- [design/output/ui-polish-v2-dashboard-1440.png](/xrml-arxr-research-agent-starter-allinone/design/output/ui-polish-v2-dashboard-1440.png)
- [design/output/ui-polish-v2-dashboard-narrow.png](/xrml-arxr-research-agent-starter-allinone/design/output/ui-polish-v2-dashboard-narrow.png)
- [design/output/ui-polish-v2-profile-drawer.png](/xrml-arxr-research-agent-starter-allinone/design/output/ui-polish-v2-profile-drawer.png)
- [design/output/ui-polish-v2-long-text-stress.png](/xrml-arxr-research-agent-starter-allinone/design/output/ui-polish-v2-long-text-stress.png)
