from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Error, sync_playwright


def _launch_browser(playwright, requested_channel: str | None):
    channels = (
        [requested_channel]
        if requested_channel
        else (["msedge", "chrome", None] if __import__("platform").system() == "Windows" else [None])
    )
    launch_errors: list[str] = []
    for channel in channels:
        launch_kwargs = {"headless": True}
        if channel:
            launch_kwargs["channel"] = channel
        try:
            return playwright.chromium.launch(**launch_kwargs)
        except Error as exc:
            label = channel or "chromium"
            launch_errors.append(f"{label}: {exc}")
    joined = " | ".join(launch_errors) or "no browser launch attempts were made"
    raise RuntimeError(f"Unable to launch a browser channel. {joined}")


def _wait_one_of(page, selectors: list[str], label: str):
    last_error: Exception | None = None
    for selector in selectors:
        try:
            locator = page.locator(selector)
            locator.first.wait_for()
            return locator
        except Error as exc:
            last_error = exc
    raise RuntimeError(f"Missing {label}: {last_error}")


def _parse_viewports(values: list[str] | None):
    default_viewports = ["1920x1200", "1440x1000", "390x900", "360x800"]
    raw_values = values or default_viewports
    parsed = []
    for value in raw_values:
        token = value.lower().strip()
        if "x" not in token:
            raise ValueError(f"Invalid viewport '{value}'. Use WIDTHxHEIGHT format.")
        width, height = token.split("x", 1)
        parsed.append((int(width), int(height)))
    return parsed


def _screenshot_name_for_viewport(width: int, height: int) -> str:
    if width >= 1800:
        return "ui-runtable-regression-wide-1920.png"
    if width <= 380:
        return "ui-runtable-regression-360.png" if width <= 365 else "ui-runtable-regression-390.png"
    return "ui-runtable-regression-390.png"


def _save_screenshot(page, screenshot_dir: Path, filename: str) -> str:
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    path = screenshot_dir / filename
    page.screenshot(path=str(path), full_page=True)
    return str(path)


def _capture_screenshot_if_needed(page, screenshot_dir: Path, name: str, captures: list[dict[str, str]], viewport: tuple[int, int]):
    path = _save_screenshot(page, screenshot_dir, name)
    captures.append({"viewport": f"{viewport[0]}x{viewport[1]}", "path": path})


def _apply_long_text_stress(page):
    page.evaluate(
        """
        () => {
          const longData = {
            runId: 'run-with-a-very-long-id-value-that-should-wrap-or-clamp-2026-04-21-local-run-dataset-001',
            runTopic: 'Topic name that is intentionally long for responsive overflow and scanability checks with lots of repeated context',
            profileTitle: 'This is a very long topic profile title used to validate no major truncation regressions in UI cards',
            profileTopic: 'Extremely long topic label that mirrors real dataset names and should remain readable',
            profileDescription:
              'A longer description intended to validate clipping and wrapping behavior in tight dashboard cards and avoid uncontrolled growth of portfolio layout.',
          };
          const rows = document.querySelectorAll('[role="row"]');
          if (rows.length > 0) {
            const row = rows[0];
            const runId = row.querySelector('.run-id');
            const runTopic = row.querySelector('.run-topic');
            if (runId) {
              runId.textContent = longData.runId;
            }
            if (runTopic) {
              runTopic.textContent = longData.runTopic;
            }
          }
          const firstCard = document.querySelector('.profile-card');
          if (firstCard) {
            const title = firstCard.querySelector('.profile-title');
            const topic = firstCard.querySelector('.profile-topic');
            const description = firstCard.querySelector('.profile-description');
            if (title) title.textContent = longData.profileTitle;
            if (topic) topic.textContent = longData.profileTopic;
            if (description) description.textContent = longData.profileDescription;
          }
          const inspector = document.querySelector('[aria-label="selected run inspector"]');
          if (inspector) {
            const title = inspector.querySelector('.run-topic-title');
            if (title) title.textContent = longData.runTopic;
          }
        }
        """
    )


def _assert_run_table_health(page, width: int, height: int):
    table = _wait_one_of(
        page,
        [
            "div.run-table-surface[role='table'][aria-label='recent runs table']",
            "[aria-label='recent runs table'] .run-table-surface",
            ".run-table-surface",
        ],
        "recent runs table",
    )
    rows = table.locator("[role='row']")
    if rows.count() == 0:
        raise RuntimeError("Run table has zero rows")

    inspect_buttons = page.locator("button[aria-label^='Inspect run '], a[aria-label^='Inspect run ']")
    if inspect_buttons.count() == 0:
        raise RuntimeError("Run table has no Inspect buttons")

    row_checkboxes = rows.locator("input[type='checkbox']")
    if row_checkboxes.count() == 0:
        raise RuntimeError("Run table rows are missing selection checkboxes")
    unlabeled = 0
    for i in range(min(row_checkboxes.count(), 5)):
        cb = row_checkboxes.nth(i)
        if not cb.get_attribute("aria-label"):
            unlabeled += 1
    if unlabeled:
        raise RuntimeError("Some run row checkboxes are missing aria-label")

    # Ensure row identity and action remain visible, especially at narrow widths.
    first_row = rows.nth(0)
    run_id = first_row.locator(".run-id")
    run_topic = first_row.locator(".run-topic")
    if not run_id.count() or not run_topic.count():
        raise RuntimeError("Run row missing run id or topic element")
    if not run_id.first.is_visible() or not run_topic.first.is_visible():
        raise RuntimeError("Run row identity not visible")

    if width <= 760:
        mobile_summary = first_row.locator(".run-table-mobile-summary")
        if not mobile_summary.count():
            raise RuntimeError("Narrow viewport missing run-table-mobile-summary")
        if not mobile_summary.first.is_visible():
            raise RuntimeError("Narrow viewport run-table-mobile-summary is not visible")
        header = page.locator(".run-table-header")
        if header.count() > 0 and header.first.is_visible():
            raise RuntimeError("Run table header should be hidden at narrow viewport")

    # Check for accidental overflow; severe overflow at narrow widths is a UX regression.
    metrics = page.evaluate(
        """
        () => {
          const wrapper = document.querySelector('.run-table-wrapper');
          const surface = document.querySelector('.run-table-surface');
          if (!wrapper || !surface) {
            return null;
          }
          const sideBar = document.querySelector('.dashboard-sidebar');
          const header = document.querySelector('header.dashboard-command-bar');
          const app = document.querySelector('main, [data-dashboard-shell], .dashboard-shell');
          const widths = {
            wrapperClient: wrapper.clientWidth,
            wrapperScroll: wrapper.scrollWidth,
            surfaceClient: surface.clientWidth,
            surfaceScroll: surface.scrollWidth,
            docScroll: document.documentElement.scrollWidth,
            bodyScroll: document.body.scrollWidth,
            viewportWidth: window.innerWidth,
            viewportHeight: window.innerHeight,
            sidebarClient: sideBar ? sideBar.clientWidth : 0,
            headerClient: header ? header.clientWidth : 0,
            appShellClient: app ? app.clientWidth : 0,
          };
          const row = surface.querySelector('[role=\"row\"]');
          if (row) {
            widths.firstRowScroll = row.scrollWidth;
            widths.firstRowClient = row.clientWidth;
          }
          return widths;
        }
        """
    )
    if not metrics:
        raise RuntimeError("Unable to read run table layout metrics")
    if width <= 760:
        if metrics["wrapperScroll"] > metrics["wrapperClient"] + 24:
            raise RuntimeError(
                f"Horizontal overflow at narrow width: wrapperScroll={metrics['wrapperScroll']} wrapperClient={metrics['wrapperClient']}"
            )
    else:
        if metrics["surfaceScroll"] > metrics["surfaceClient"] + 24:
            raise RuntimeError(
                f"Horizontal overflow on table at viewport {width}x{height}: surfaceScroll={metrics['surfaceScroll']} surfaceClient={metrics['surfaceClient']}"
            )
    if metrics["docScroll"] > metrics["viewportWidth"] + 20 or metrics["bodyScroll"] > metrics["viewportWidth"] + 20:
        raise RuntimeError(
            f"Global horizontal overflow at viewport {width}x{height}: docScroll={metrics['docScroll']} bodyScroll={metrics['bodyScroll']} viewportWidth={metrics['viewportWidth']}"
        )

    # ultra-small regression hardening
    if width <= 430:
        if metrics["appShellClient"] and metrics["appShellClient"] > metrics["viewportWidth"]:
            raise RuntimeError(
                f"Dashboard shell overflow on ultra-small viewport: appShellClient={metrics['appShellClient']} viewportWidth={metrics['viewportWidth']}"
            )
        if metrics["headerClient"] and metrics["headerClient"] > metrics["viewportWidth"]:
            raise RuntimeError(
                f"Command bar overflow on ultra-small viewport: headerClient={metrics['headerClient']} viewportWidth={metrics['viewportWidth']}"
            )
        if metrics["sidebarClient"] and metrics["sidebarClient"] > metrics["viewportWidth"] - 36:
            raise RuntimeError(
                f"Sidebar width too large for ultra-small viewport: sidebarClient={metrics['sidebarClient']} viewportWidth={metrics['viewportWidth']}"
            )

    return {
        "rows": rows.count(),
        "inspectButtons": inspect_buttons.count(),
        "checkboxes": row_checkboxes.count(),
        "overflow": metrics,
    }


def _drawer_smoke(page, screenshot_dir: Path, captures: list[dict[str, str]], viewport: tuple[int, int]):
    add_profile_button = page.locator("button:has-text('Add topic profile')")
    edit_profile_button = page.locator("article.profile-card button:has-text('Edit')")
    if add_profile_button.count() > 0 and add_profile_button.first.is_visible():
        add_profile_button.first.scroll_into_view_if_needed()
        add_profile_button.first.click()
    elif edit_profile_button.count() > 0:
        edit_profile_button.first.scroll_into_view_if_needed()
        edit_profile_button.first.click()
    else:
        raise RuntimeError("No profile editor trigger found for drawer smoke check.")

    drawer = page.locator("[aria-label='profile drawer'][role='dialog']")
    drawer.wait_for()
    drawer_bounds = page.evaluate(
        """
        () => {
          const drawer = document.querySelector("[aria-label='profile drawer'][role='dialog']");
          if (!drawer) {
            return null;
          }
          const rect = drawer.getBoundingClientRect();
          return {
            x: rect.x,
            width: rect.width,
          };
        }
        """
    )
    if not drawer_bounds:
        raise RuntimeError("Profile drawer failed to expose measurable layout")
    if viewport[0] <= 430 and (
        drawer_bounds["width"] > viewport[0] - 6
        or drawer_bounds["x"] < -6
        or (drawer_bounds["x"] + drawer_bounds["width"]) > viewport[0] + 6
    ):
        raise RuntimeError(
            f"Profile drawer overflow at ultra-small viewport {viewport[0]}x{viewport[1]}: x={drawer_bounds['x']}, width={drawer_bounds['width']}"
        )

    if viewport[0] >= 500:
        _capture_screenshot_if_needed(page, screenshot_dir, "ui-rescue-profile-drawer.png", captures, viewport)
    _capture_screenshot_if_needed(page, screenshot_dir, "ui-runtable-regression-profile-drawer.png", captures, viewport)
    page.get_by_role("button", name="Cancel").click()
    page.wait_for_timeout(200)
    drawer_closed = page.locator("[aria-label='profile drawer'][role='dialog']")
    if drawer_closed.count() > 0 and drawer_closed.first.is_visible():
        raise RuntimeError("Profile drawer did not close")


def _inspect_nav_health(page):
    return page.evaluate(
        """
        () => {
          const allNavs = Array.from(document.querySelectorAll('nav'));
          const visibleNavs = allNavs.filter((n) => {
            if (!(n instanceof HTMLElement)) return false;
            return n.offsetParent !== null;
          });
          const appNav = document.querySelector('.app-nav');
          const appNavVisible = appNav ? appNav.offsetParent !== null && getComputedStyle(appNav).display !== 'none' : false;
          const appHasPrimarySet = appNav
            ? Array.from(appNav.querySelectorAll('a')).map((a) => (a.textContent || '').trim()).filter((label) =>
                ['Dashboard', 'Topics', 'Runs', 'Evidence', 'Reports', 'Automations', 'Compare', 'Settings'].includes(label),
              ).length >= 5
            : false;
          const dashboardNav = document.querySelector('[aria-label=\"dashboard navigation\"]');
          const dashboardLinks = dashboardNav
            ? Array.from(dashboardNav.querySelectorAll('a')).map((a) => (a.textContent || '').trim())
            : [];
          const sidebarHasPrimarySet = dashboardLinks.filter((label) =>
            ['Dashboard', 'Topics', 'Runs', 'Evidence', 'Reports', 'Automations', 'Compare', 'Settings'].includes(label),
          ).length >= 5;
          const appNavCount = appNav ? 1 : 0;
          const navTextNodes = allNavs.map((n) => n.textContent?.trim().slice(0, 80) || '');
          return {
            allNavCount: allNavs.length,
            visibleNavCount: visibleNavs.length,
            appNavCount,
            appHasPrimarySet,
            sidebarHasPrimarySet,
            appNavVisible,
            appLinks: appNav ? Array.from(appNav.querySelectorAll('a')).map((a) => a.textContent?.trim() || '') : [],
            sidebarLinks: dashboardLinks,
            navTextNodes,
          };
        }
        """
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--browser-channel", default=None)
    parser.add_argument("--viewports", nargs="+", default=None, help="Optional WIDTHxHEIGHT list.")
    parser.add_argument("--screenshot-dir", default="design/output")
    parser.add_argument("--capture-screenshots", action="store_true")
    parser.add_argument("--long-text-stress", action="store_true")
    parser.add_argument(
        "--focus-runtable",
        action="store_true",
        help="Capture explicit RunTable-focused screenshots at each configured viewport.",
    )
    args = parser.parse_args()

    dashboard_url = urljoin(args.base_url.rstrip("/") + "/", "app/")
    settings_url = urljoin(args.base_url.rstrip("/") + "/", "app/settings")
    screenshot_dir = Path(args.screenshot_dir)

    try:
        viewports = _parse_viewports(args.viewports)
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright, args.browser_channel)
            page = browser.new_page()
            captures: list[dict[str, str]] = []
            viewport_checks = []
            nav_checks = []
            run_table_checks = []
            try:
                for width, height in viewports:
                    page.set_viewport_size({"width": width, "height": height})
                    page.goto(dashboard_url, wait_until="networkidle")
                    page.locator("h1", has_text="Research Cockpit").first.wait_for()

                    _wait_one_of(page, ["aside.dashboard-sidebar", "[aria-label='dashboard navigation']"], "sidebar")
                    _wait_one_of(page, ["header.dashboard-command-bar", "[aria-label='dashboard command bar']"], "command bar")
                    _wait_one_of(page, ["section.dashboard-status-strip", "[aria-label='dashboard status strip']"], "status strip")
                    _wait_one_of(
                        page,
                        ["div.run-table-surface[role='table'][aria-label='recent runs table']", "[aria-label='recent runs table']"],
                        "recent runs table",
                    )
                    _wait_one_of(page, ["text=Profile library", "text=No profiles yet"], "topic portfolio section")

                    # status chips / truthfulness checks
                    page.locator("text=/Seam prepared/").first.wait_for()
                    page.locator("text=/fallback_only/").first.wait_for()

                    sidebar_locator = page.locator("aside.dashboard-sidebar, [aria-label='dashboard navigation']")
                    if sidebar_locator.count() == 0:
                        raise RuntimeError("Missing dashboard sidebar on dashboard route")

                    nav_probe = _inspect_nav_health(page)
                    nav_checks.append({"viewport": f"{width}x{height}", **nav_probe})

                    # dashboard shell should not have duplicate primary top navigation.
                    if nav_probe["appHasPrimarySet"] and nav_probe["appNavVisible"] and nav_probe["sidebarHasPrimarySet"]:
                        raise RuntimeError("Duplicate primary navigation set detected on dashboard route.")
                    if nav_probe["allNavCount"] > 2:
                        raise RuntimeError(f"Too many nav landmarks detected: {nav_probe['allNavCount']}")

                    run_table_health = _assert_run_table_health(page, width, height)
                    run_table_checks.append({"viewport": f"{width}x{height}", **run_table_health})

                    if args.capture_screenshots or args.focus_runtable:
                        screenshot_name = _screenshot_name_for_viewport(width, height)
                        _capture_screenshot_if_needed(page, screenshot_dir, screenshot_name, captures, (width, height))

                    if args.long_text_stress:
                        _apply_long_text_stress(page)
                        if width <= 500:
                            _capture_screenshot_if_needed(
                                page,
                                screenshot_dir,
                                "ui-runtable-regression-long-text-stress.png",
                                captures,
                                (width, height),
                            )
                            _assert_run_table_health(page, width, height)

                    if width >= 1320:
                        _drawer_smoke(page, screenshot_dir, captures, (width, height))

                    try:
                        page.locator("[aria-label='primary'] a", has_text="Settings").first.click()
                    except Error:
                        page.get_by_role("link", name="Settings / status").click()
                    page.wait_for_url(settings_url)
                    page.get_by_role("heading").first.wait_for()
                    page.locator("text=/Settings/").first.wait_for(timeout=5000)
                    # Settings UI text may vary across runs depending on provider and scheduler state.
                    # Keep smoke tolerant by probing optional strings only.
                    page.locator("h1").first.wait_for()

                    if page.locator("text='Integrated'").count():
                        raise RuntimeError("Unexpected 'Integrated' label found in settings/dashboard smoke path.")

                    # return to dashboard for next viewport
                    page.goto(dashboard_url, wait_until="networkidle")
                    viewport_checks.append({"viewport": f"{width}x{height}", "status": "passed"})

                provider_mode_text = page.locator("text=/fallback_only|live_ready/").first.text_content()
                connector_locator = page.locator("text=/Local CLI only|OAuth ready|OIDC missing audience|OIDC over HTTP only|ChatGPT OAuth|OAuth/")
                connector_text = connector_locator.first.text_content() if connector_locator.count() > 0 else None
                stitch_locator = page.locator("text=/Seam prepared|Stitch handoff|Stitch direct|Stitch/")
                stitch_text = stitch_locator.first.text_content() if stitch_locator.count() > 0 else None
                print(
                    json.dumps(
                        {
                            "status": "passed",
                            "base_url": args.base_url,
                            "dashboard_url": dashboard_url,
                            "settings_url": settings_url,
                            "provider_mode_text": provider_mode_text,
                            "connector_text": connector_text,
                            "stitch_text": stitch_text,
                            "viewports": viewport_checks,
                            "nav_checks": nav_checks,
                            "run_table_checks": run_table_checks,
                            "screenshot_dir": str(screenshot_dir),
                            "captures": captures,
                        },
                        indent=2,
                    )
                )
                return 0
            finally:
                browser.close()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
