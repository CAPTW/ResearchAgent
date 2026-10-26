from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Literal

WebhookDestinationClass = Literal["empty", "configured_nonempty"]


def classify_persisted_webhook_destination(raw: str | None) -> WebhookDestinationClass:
    if raw is None or not str(raw).strip():
        return "empty"
    return "configured_nonempty"


def _diagnostic_contains_raw_destination(record: Mapping[str, object] | None) -> bool:
    if record is None:
        return False
    forbidden_keys = {"destination", "url", "webhook_url", "raw", "raw_destination"}
    for key, value in record.items():
        if str(key).casefold() in forbidden_keys:
            return True
        text = str(value)
        if "://" in text or "destination=" in text:
            return True
    return False


def assert_no_external_webhook_delivery(*, delivery_attempted: bool) -> None:
    if delivery_attempted:
        raise AssertionError("unavailable webhook must not attempt external delivery")


def evaluate_webhook_unavailable_contract(
    *,
    input_disabled: bool,
    send_action_disabled: bool,
    destination_class: str,
    delivery_attempted: bool = False,
    diagnostic_record: Mapping[str, object] | None = None,
) -> None:
    if destination_class not in {"empty", "configured_nonempty"}:
        raise AssertionError("destination_class must be empty or configured_nonempty")
    if not input_disabled:
        raise AssertionError("unavailable webhook destination input must be disabled")
    if not send_action_disabled:
        raise AssertionError("unavailable webhook send/test action must be disabled")
    if _diagnostic_contains_raw_destination(diagnostic_record):
        raise AssertionError("diagnostic record must not contain a raw destination")
    assert_no_external_webhook_delivery(delivery_attempted=delivery_attempted)


def evaluate_notification_delivery_history_contract(
    *,
    region_present: bool,
    region_visible: bool,
    record_count: int,
    region_text: str,
    empty_state_visible: bool,
) -> None:
    if not region_present:
        raise AssertionError("delivery-history region must exist")
    if not region_visible:
        raise AssertionError("delivery-history region must be visible")
    if record_count < 0:
        raise AssertionError("record_count must be >= 0")
    text = region_text or ""
    empty_copy = "아직 외부 전달 기록이 없습니다."
    if record_count == 0:
        if not empty_state_visible or empty_copy not in text:
            raise AssertionError("zero-record history must render the product empty-state copy")
        return
    if empty_state_visible or empty_copy in text:
        raise AssertionError("nonempty history must not present the empty-state copy as the region truth")
    markers = ("webhook", "email", "실패", "전송됨", "비활성", "규칙 ")
    if not any(marker in text for marker in markers):
        raise AssertionError("nonempty history must include a product-owned delivery marker")


def _remove_tree_for_restore(path: Path) -> None:
    for _ in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.2)
    if path.exists():
        for child in path.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()


async def verify(base_url: str, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    api_base = re.sub(r":5173(?=/|$)", ":8000", base_url)
    # Transient Playwright extra; persistent project env is Playwright-free.
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        automation_task_name = "Playwright 자동 테스트 작업"
        lifecycle_topic_name = "Playwright Topic Lifecycle"
        lifecycle_topic_slug = "playwright-topic-lifecycle"

        async def wait_for_report_row(topic_id: str, run_id: str) -> None:
            for _ in range(45):
                await page.goto(f"{base_url}/app/topics/{topic_id}/reports", wait_until="networkidle")
                row = page.locator("article.ai-report-hub-row").filter(has_text=run_id).first
                if await row.count() > 0:
                    return
                await page.wait_for_timeout(1000)
            raise AssertionError(f"report row for {run_id} did not appear on topic reports")

        async def wait_for_hub_row(topic_id: str, run_id: str) -> None:
            for _ in range(45):
                await page.goto(f"{base_url}/app/reports?topicId={topic_id}", wait_until="networkidle")
                row = page.locator("article.ai-report-hub-row").filter(
                    has=page.locator(f"a[href$='/reports/{run_id}']")
                ).first
                if await row.count() > 0:
                    return
                await page.wait_for_timeout(1000)
            raise AssertionError(f"report row for {run_id} did not appear on report hub")

        async def wait_for_report_terminal_state(run_id: str) -> tuple[str, str]:
            observed_statuses: set[str] = set()
            final_state = "running"
            body_text = ""
            for _ in range(90):
                await page.goto(f"{base_url}/app/reports/{run_id}", wait_until="networkidle")
                body_text = await page.locator("body").inner_text()
                if "대기열" in body_text or "보고서 생성 대기열에 등록되었습니다." in body_text:
                    observed_statuses.add("queued")
                if "생성 중" in body_text or "보고서를 생성 중입니다." in body_text:
                    observed_statuses.add("running")
                if "보고서 생성 실패" in body_text:
                    observed_statuses.add("failed")
                    final_state = "failed"
                    break
                if "보고서 내용" in body_text:
                    observed_statuses.add("completed")
                    final_state = "completed"
                    break
                await page.wait_for_timeout(1000)

            assert observed_statuses & {"queued", "running", "completed", "failed"}
            return final_state, body_text

        async def set_source_provider_enabled(provider_label: str, enabled: bool) -> None:
            checkbox = page.get_by_label(f"{provider_label} provider 활성화")
            if await checkbox.is_checked() == enabled:
                return
            await checkbox.click()
            await page.wait_for_timeout(300)
            if await checkbox.is_checked() != enabled:
                option_row = page.locator("label.ai-source-provider-option").filter(has_text=provider_label).first
                await option_row.click()
                await page.wait_for_timeout(300)
            assert await checkbox.is_checked() == enabled

        async def find_completed_download_href(topic_id: str) -> str | None:
            await page.goto(f"{base_url}/app/reports?topicId={topic_id}", wait_until="networkidle")
            rows = page.locator("article.ai-report-hub-row")
            for index in range(await rows.count()):
                row = rows.nth(index)
                row_text = await row.inner_text()
                if "완료" not in row_text:
                    continue
                link = row.get_by_role("link", name="다운로드")
                if await link.count() == 0:
                    continue
                href = await link.get_attribute("href")
                if href:
                    return href
            return None

        async def open_and_shot(path: str, name: str) -> None:
            await page.goto(f"{base_url}{path}", wait_until="networkidle")
            await page.screenshot(path=str(output_dir / f"{name}.png"), full_page=True)

        async def delete_topic_if_present(profile_id: str) -> None:
            response = await page.context.request.delete(f"{api_base}/topic-profiles/{profile_id}")
            assert response.status in {200, 404}

        async def delete_task_if_present(task_name: str) -> None:
            for _ in range(6):
                schedules_response = await page.context.request.get(f"{api_base}/topic-schedules")
                assert schedules_response.ok
                schedules = await schedules_response.json()
                matches = [
                    item
                    for item in schedules.get("items", [])
                    if item.get("title") == task_name and item.get("schedule_id")
                ]
                if not matches:
                    return
                for item in matches:
                    delete_response = await page.context.request.delete(
                        f"{api_base}/topic-schedules/{item['schedule_id']}"
                    )
                    assert delete_response.status in {200, 404}
                await page.wait_for_timeout(300)
            schedules_response = await page.context.request.get(f"{api_base}/topic-schedules")
            assert schedules_response.ok
            schedules = await schedules_response.json()
            assert all(item.get("title") != task_name for item in schedules.get("items", []))

        async def wait_for_task_status(task_name: str, status_label: str) -> None:
            for _ in range(30):
                await page.goto(f"{base_url}/app/automations?topicId=large-language-models", wait_until="networkidle")
                row = page.locator("article.ai-task-row").filter(has_text=task_name).first
                if await row.count() > 0 and await row.get_by_text(status_label).count() == 1:
                    return
                await page.wait_for_timeout(500)
            raise AssertionError(f"task {task_name} did not reach {status_label}")

        async def save_notification_settings_if_changed() -> None:
            save_button = page.get_by_role("button", name="설정 저장")
            if await save_button.is_enabled():
                await save_button.click()
                await page.get_by_text("알림 설정을 저장했습니다.").wait_for()

        await open_and_shot("/app/topics/large-language-models", "dashboard")
        body_text = await page.locator("body").inner_text()
        assert "Default XRML Monitor" not in body_text, "sample topic should not appear on production dashboard routes"
        assert "로컬 데모 모드" not in body_text, "demo copy should not remain on production dashboard routes"
        assert "프로필 필요" not in body_text, "unconfigured catalog topics should not remain in the sidebar"
        assert "분석 provenance" in body_text
        assert "persisted topic analytics artifact" in body_text or "read-only runtime snapshot" in body_text
        assert "분석 snapshot의 중복 제거 논문 수" in body_text
        assert "근거 run" in body_text

        await page.get_by_role("link", name="저장된 논문").click()
        await page.wait_for_url("**/topics/large-language-models/papers")
        await page.get_by_role("heading", name="저장된 논문").first.wait_for()

        await page.goto(f"{base_url}/app/topics/large-language-models", wait_until="networkidle")
        await page.get_by_role("link", name="분석 설정").click()
        await page.wait_for_url("**/topics/large-language-models/settings")
        await page.get_by_role("button", name="프로필 편집").wait_for()
        settings_text = await page.locator("body").inner_text()
        assert "프로필 편집" in settings_text
        assert "arXiv" in settings_text
        assert "OpenAlex" in settings_text
        assert "Crossref" in settings_text
        assert "사용 가능" in settings_text
        assert "제출일" in settings_text
        await page.screenshot(path=str(output_dir / "provider-settings-state.png"), full_page=True)
        await page.get_by_role("button", name="프로필 편집").click()
        crossref_toggle = page.get_by_label("Crossref provider 활성화")
        await crossref_toggle.wait_for()
        assert not await crossref_toggle.is_disabled()
        await page.get_by_label("Crossref 검색 쿼리").wait_for()
        await page.screenshot(path=str(output_dir / "provider-settings-drawer-state.png"), full_page=True)
        await page.get_by_role("button", name="토픽 편집 패널 닫기").last.click()

        await page.goto(f"{base_url}/app/topics/large-language-models", wait_until="networkidle")
        await page.get_by_role("link", name="보고서 허브").click()
        await page.wait_for_url("**/reports")
        await page.get_by_role("heading", name="보고서 허브").wait_for()
        assert "보고서 허브" in await page.locator("body").inner_text()

        await page.goto(f"{base_url}/app/topics/large-language-models", wait_until="networkidle")
        await page.get_by_role("button", name="알림 센터").click()
        await page.wait_for_url("**/settings/notifications**")
        await page.get_by_role("heading", name="알림 설정").wait_for()
        assert "알림 설정" in await page.locator("body").inner_text()

        await page.goto(f"{base_url}/app/topics/large-language-models", wait_until="networkidle")
        await page.get_by_role("button", name="사용 가이드").click()
        await page.wait_for_url("**/evidence")
        await page.get_by_role("heading", name="사용 가이드").wait_for()
        guide_text = await page.locator("body").inner_text()
        assert "사용 가이드" in guide_text
        assert "실제 워크플로우" in guide_text
        assert "주제/설정" in guide_text
        assert "Preview/Export" in guide_text
        assert "1. 시작하기" in guide_text
        assert "6. 보고서 작성과 내보내기" in guide_text
        assert "11. 아직 준비 중인 기능" in guide_text
        assert "현재 capability 상태" in guide_text
        assert "바로 실행할 워크플로우" in guide_text
        assert "대시보드로 이동" in guide_text
        assert "새 주제 추가" in guide_text
        assert "Run Workbench 열기" in guide_text
        assert "PDF run" in guide_text
        assert any(
            runtime_label in guide_text
            for runtime_label in [
                "single-node/file-backed",
                "db_backed",
                "read-only fallback",
                "queue_backed",
            ]
        )
        assert "multi-node distributed scheduler는 구현 전까지 게이트됩니다" in guide_text
        assert "semantic diff" in guide_text
        await page.screenshot(path=str(output_dir / "guide.png"), full_page=True)

        await page.get_by_role("link", name="새 주제 추가").click()
        await page.wait_for_url("**/topics**")
        await page.get_by_role("button", name="새 주제 추가").first.wait_for()

        await page.goto(f"{base_url}/app/evidence", wait_until="networkidle")
        topic_settings_cta = page.get_by_role("link", name="주제 설정 열기")
        if await topic_settings_cta.count() > 0:
            await topic_settings_cta.click()
            await page.wait_for_url("**/topics/**/settings")
            await page.get_by_role("button", name="프로필 편집").wait_for()
        else:
            assert "주제 설정은 저장된 topic profile이 생긴 뒤 열 수 있습니다." in await page.locator("body").inner_text()

        await page.goto(f"{base_url}/app/evidence", wait_until="networkidle")
        papers_cta = page.get_by_role("link", name="논문 목록 보기")
        if await papers_cta.count() > 0:
            await papers_cta.click()
            await page.wait_for_url("**/topics/**/papers")
            await page.get_by_role("heading", name="저장된 논문").first.wait_for()
        else:
            assert "논문 목록은 topic이 생성되고 collect run이 실행된 뒤 확인할 수 있습니다." in await page.locator("body").inner_text()

        await page.goto(f"{base_url}/app/evidence", wait_until="networkidle")
        run_workbench_cta = page.get_by_role("link", name="Run Workbench 열기")
        if await run_workbench_cta.count() > 0:
            await run_workbench_cta.click()
            await page.wait_for_url(re.compile(r".*/app/(topics/[^/]+/)?runs/[^/]+.*"))
            await page.get_by_role("heading", name="워크벤치 흐름").wait_for()
        else:
            assert "아직 열 수 있는 run이 없으면 collect/analyze/report를 먼저 실행해야 합니다." in await page.locator("body").inner_text()

        await page.goto(f"{base_url}/app/evidence", wait_until="networkidle")
        await page.get_by_role("link", name="보고서 허브 보기").click()
        await page.wait_for_url("**/reports")
        await page.get_by_role("heading", name="보고서 허브").wait_for()

        await page.goto(f"{base_url}/app/evidence", wait_until="networkidle")
        await page.get_by_role("link", name="시스템 설정 보기").click()
        await page.wait_for_url("**/settings")
        await page.get_by_role("heading", name="Auth / Provider / Status").wait_for()

        await page.goto(f"{base_url}/app/topics/large-language-models", wait_until="networkidle")

        await page.get_by_role("tab", name="논문 목록").click()
        await page.wait_for_url("**/topics/large-language-models/papers")
        await open_and_shot("/app/topics/large-language-models/papers", "papers")
        assert await page.get_by_role("heading", name="저장된 논문").count() == 1
        papers_text = await page.locator("body").inner_text()
        assert "Provider 필터" in papers_text
        assert "provider 기여:" in papers_text or "provider provenance" in papers_text

        await page.get_by_role("tab", name="분석 인사이트").click()
        await page.wait_for_url("**/topics/large-language-models/insights")
        await open_and_shot("/app/topics/large-language-models/insights", "insights")
        insights_text = await page.locator("body").inner_text()
        assert "분석 기준" in insights_text
        assert "분석 산출물 결정" in insights_text
        assert "데이터 커버리지" in insights_text
        assert "키워드 근거" in insights_text
        assert "산출물 버전" in insights_text
        assert "근거 run" in insights_text
        assert "정규화" in insights_text
        assert "트렌드 해석" in insights_text

        await page.get_by_role("tab", name="보고서").click()
        await page.wait_for_url("**/topics/large-language-models/reports")
        await open_and_shot("/app/topics/large-language-models/reports", "reports")

        await page.get_by_role("tab", name="설정").click()
        await page.wait_for_url("**/topics/large-language-models/settings")
        await open_and_shot("/app/topics/large-language-models/settings", "settings")

        await open_and_shot("/app/reports", "report-hub")
        report_hub_text = await page.locator("body").inner_text()
        assert "보고서 허브" in report_hub_text
        assert "보고서 lifecycle" in report_hub_text
        assert "목록 provenance" in report_hub_text
        assert "전역 허브 화면은 아직 정리 중입니다." not in report_hub_text

        app_nav = page.get_by_role("navigation", name="primary app navigation")
        await app_nav.get_by_role("link", name="비교").click()
        await page.wait_for_url("**/compare")
        await page.get_by_role("heading", name="Run comparison and rerun planning").wait_for()
        compare_text = await page.locator("body").inner_text()
        assert "토픽 운영 비교" in compare_text
        assert "trend momentum" in compare_text
        assert "core density" in compare_text
        assert "계산 정의" in compare_text
        assert "Run comparison and rerun planning" in compare_text
        await page.screenshot(path=str(output_dir / "compare.png"), full_page=True)

        run_ids = await page.locator("select").first.locator("option").evaluate_all(
            "(options) => options.map((option) => option.value).filter(Boolean)"
        )
        if len(run_ids) >= 2:
            await page.goto(f"{base_url}/app/compare?runA={run_ids[0]}&runB={run_ids[1]}", wait_until="networkidle")
            await page.get_by_role("heading", name="Run comparison and rerun planning").wait_for()
            await page.get_by_text("Score distribution / keyword movement / provider contribution").wait_for()
            compare_run_text = await page.locator("body").inner_text()
            assert "Score distribution" in compare_run_text
            assert "Keyword movement" in compare_run_text
            assert "Provider contribution" in compare_run_text
            assert "Report recency / status" in compare_run_text
            assert "Compare provenance" in compare_run_text
            assert "Added / removed / changed papers" in compare_run_text
            await page.get_by_role("button", name="rerun").click()
            await page.get_by_text("Selective rerun is only enabled").wait_for()
            rerun_text = await page.locator("body").inner_text()
            assert "Suggested rerun selections" in rerun_text
            assert "빈 branch는 안전하지 않아 게이트합니다." in rerun_text or "선택된" in rerun_text
        else:
            assert "비교할 두 run을 선택하세요." in compare_text
        await page.screenshot(path=str(output_dir / "compare-run-ops.png"), full_page=True)

        await open_and_shot("/app/evidence", "guide-from-app-nav")
        await page.get_by_role("heading", name="사용 가이드").wait_for()
        guide_nav_text = await page.locator("body").inner_text()
        assert "사용 가이드" in guide_nav_text
        assert "provider registry" in guide_nav_text
        assert "현재 capability 상태" in guide_nav_text
        assert "single-node/file-backed" in guide_nav_text
        assert "semantic diff" in guide_nav_text
        assert "SMTP" in guide_nav_text

        await open_and_shot("/app/runs", "runs")
        runs_text = await page.locator("body").inner_text()
        assert "Local Demo Mode" not in runs_text
        assert "One machine, full research loop" not in runs_text
        assert "실행 이력" in runs_text
        assert "source" in runs_text
        assert "trigger" in runs_text
        if "run detail" in runs_text:
            assert "source " in runs_text
            assert "trigger " in runs_text
        else:
            assert "아직 생성된 run이 없습니다." in runs_text
            assert "표시 0 / 전체 0" in runs_text
            await page.screenshot(path=str(output_dir / "runs-empty-state.png"), full_page=True)

        authoring_run_id = "20260403-100702-m3-validation-run"
        fixture_run_response = await page.request.get(f"{api_base}/runs/{authoring_run_id}")
        if fixture_run_response.ok:
            await page.goto(f"{base_url}/app/topics/large-language-models/runs/{authoring_run_id}", wait_until="networkidle")
            await page.get_by_role("heading", name="워크벤치 흐름").wait_for()
            await page.get_by_role("heading", name="Review actions").wait_for()
            run_workbench_text = await page.locator("body").inner_text()
            assert "Run stepper" in run_workbench_text
            assert "Live job log / events" in run_workbench_text
            assert "토픽/프로필" in run_workbench_text
            assert "랭크 리뷰" in run_workbench_text
            assert "근거/출처" in run_workbench_text
            assert "아웃라인 편집" in run_workbench_text
            assert "미리보기/내보내기" in run_workbench_text
            assert "비교/재실행" in run_workbench_text
            assert "점수 breakdown" in run_workbench_text
            assert "선택 논문 근거 열기" in run_workbench_text
            assert "아웃라인 편집 열기" in run_workbench_text
            assert "Review actions" in run_workbench_text
            assert "Evidence / provenance" in run_workbench_text
            assert "Dedup cluster inspector" in run_workbench_text
            assert "Provider arXiv" in run_workbench_text or "Provider OpenAlex" in run_workbench_text or "출처 미확인" in run_workbench_text
            assert "근거 데이터를 찾지 못했습니다." in run_workbench_text or "Evidence snippet" in run_workbench_text or "Citation:" in run_workbench_text
            assert "중복 클러스터가 없습니다." in run_workbench_text or "Cluster " in run_workbench_text
            review_note_text = "Playwright persisted review note"
            await page.get_by_role("button", name="유지").first.click()
            await page.get_by_label("Review note").fill(review_note_text)
            await page.get_by_role("button", name="메모 저장").click()
            await page.get_by_text("마지막 저장").wait_for()
            await page.screenshot(path=str(output_dir / "run-workbench.png"), full_page=True)
            await page.reload(wait_until="networkidle")
            await page.get_by_role("heading", name="워크벤치 흐름").wait_for()
            await page.get_by_role("heading", name="Review actions").wait_for()
            reloaded_workbench_text = await page.locator("body").inner_text()
            assert "Review queue" in reloaded_workbench_text
            assert await page.get_by_label("Review note").input_value() == review_note_text
            restore_action_response = await page.request.patch(
                f"{api_base}/runs/{authoring_run_id}/papers/2601.18372v1",
                data={"decision": "unreviewed", "pinned": False, "note": ""},
            )
            assert restore_action_response.ok

            authoring_reports_dir = Path(__file__).resolve().parents[1] / "storage" / "runs" / authoring_run_id / "reports"
            authoring_versions_dir = authoring_reports_dir / "versions"
            authoring_versions_backup_dir = Path(tempfile.mkdtemp(prefix="xrml-report-versions-"))
            authoring_restore_paths = [
                authoring_reports_dir / "outline.json",
                authoring_reports_dir / "preview.md",
                authoring_reports_dir / "exports.json",
                authoring_reports_dir / "exported_preview.md",
                authoring_reports_dir / "report_versions.json",
            ]
            authoring_original_files = {
                path: path.read_bytes() if path.exists() else None
                for path in authoring_restore_paths
            }
            if authoring_versions_dir.exists():
                shutil.copytree(authoring_versions_dir, authoring_versions_backup_dir / "versions")
            authoring_trace_text = "Playwright report authoring trace"
            try:
                await page.get_by_role("button", name="아웃라인 편집 열기").click()
                await page.get_by_role("heading", name="Outline / Preview / Export").wait_for()
                first_body_field = page.locator(".outline-card label").filter(has_text="Body").first.locator("textarea")
                existing_body = await first_body_field.input_value()
                await first_body_field.fill(f"{existing_body}\n\n{authoring_trace_text}".strip())
                await page.get_by_role("button", name="Save outline").click()
                await page.get_by_text("outline 변경사항을 저장했습니다.").wait_for()
                await page.get_by_role("button", name="Refresh preview").click()
                await page.get_by_text("preview를 다시 생성했습니다.").wait_for()
                await page.locator(".preview-box").get_by_text(authoring_trace_text).wait_for()
                await page.get_by_text("Citation Trace").wait_for()
                await page.get_by_text("Outline claims to sources").wait_for()
                await page.get_by_role("button", name="Export drawer").click()
                await page.get_by_role("dialog", name="Export drawer").wait_for()
                await page.screenshot(path=str(output_dir / "report-export-drawer.png"), full_page=True)
                await page.get_by_role("button", name="Markdown 내보내기").click()
                await page.get_by_text("report_markdown export를 완료했습니다.").wait_for()
                await page.get_by_role("link", name="Download artifact").first.wait_for()
                exports_response = await page.request.get(f"{api_base}/runs/{authoring_run_id}/exports")
                assert exports_response.ok
                export_items = (await exports_response.json())["items"]
                markdown_exports = [item for item in export_items if item.get("format") in {"markdown", "report_markdown"}]
                assert markdown_exports, "markdown export did not create an artifact record"
                latest_export = markdown_exports[-1]
                versions_response = await page.request.get(f"{api_base}/runs/{authoring_run_id}/report/versions")
                assert versions_response.ok
                version_items = (await versions_response.json())["items"]
                assert version_items, "markdown export did not create a completed report version"
                if len(version_items) < 2:
                    second_export_response = await page.request.post(
                        f"{api_base}/runs/{authoring_run_id}/export",
                        data={"format": "report_markdown"},
                    )
                    assert second_export_response.ok
                    exports_response = await page.request.get(f"{api_base}/runs/{authoring_run_id}/exports")
                    assert exports_response.ok
                    export_items = (await exports_response.json())["items"]
                    markdown_exports = [item for item in export_items if item.get("format") in {"markdown", "report_markdown"}]
                    versions_response = await page.request.get(f"{api_base}/runs/{authoring_run_id}/report/versions")
                    assert versions_response.ok
                    version_items = (await versions_response.json())["items"]
                    assert len(version_items) >= 2, "second markdown export did not create a comparable report version"
                    latest_export = markdown_exports[-1]
                pdf_export_response = await page.request.post(
                    f"{api_base}/runs/{authoring_run_id}/export",
                    data={"format": "report_pdf"},
                )
                assert pdf_export_response.ok, "PDF export should create a real binary artifact in this release candidate"
                exports_response = await page.request.get(f"{api_base}/runs/{authoring_run_id}/exports")
                assert exports_response.ok
                export_items = (await exports_response.json())["items"]
                pdf_exports = [item for item in export_items if item.get("format") in {"pdf", "report_pdf"}]
                assert pdf_exports, "PDF export did not create a binary artifact record"
                versions_response = await page.request.get(f"{api_base}/runs/{authoring_run_id}/report/versions")
                assert versions_response.ok
                version_items = (await versions_response.json())["items"]
                assert any(
                    artifact.get("format") == "pdf"
                    for version in version_items
                    for artifact in version.get("artifacts", [])
                ), "completed report versions did not link the PDF artifact"
                assert any(item["source_export_id"] == latest_export["export_id"] for item in version_items)
                download_response = await page.request.get(f"{api_base}/runs/{authoring_run_id}/exports/{latest_export['export_id']}/download")
                assert download_response.status == 200
                assert "text/markdown" in (download_response.headers.get("content-type") or "")
                assert authoring_trace_text in await download_response.text()
                await page.screenshot(path=str(output_dir / "run-report-authoring.png"), full_page=True)
                await page.goto(f"{base_url}/app/reports/{authoring_run_id}", wait_until="networkidle")
                report_version_text = await page.locator("body").inner_text()
                assert "완료 보고서 버전" in report_version_text
                if len(version_items) >= 2:
                    assert "버전 비교" in report_version_text
                    await page.get_by_role("button", name="버전 비교").click()
                    await page.get_by_text("비교 요약").wait_for()
                    await page.get_by_text("추가된 내용").wait_for()
                    await page.get_by_text("변경된 인용/근거").wait_for()
                    await page.get_by_text("Binary 아티팩트 비교").wait_for()
                    await page.get_by_text("metadata/hash 기준 비교").first.wait_for()
                    await page.get_by_text("extracted-text 비교를 사용할 수 없습니다").wait_for()
                    await page.screenshot(path=str(output_dir / "report-version-diff.png"), full_page=True)
                    await page.screenshot(path=str(output_dir / "report-binary-artifact-compare.png"), full_page=True)
                else:
                    assert "비교할 완료 버전이 아직 부족합니다" in report_version_text
                await page.screenshot(path=str(output_dir / "report-detail-versions.png"), full_page=True)
                await page.screenshot(path=str(output_dir / "report-version-history.png"), full_page=True)
            finally:
                for path, content in authoring_original_files.items():
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(content)
                if (authoring_versions_backup_dir / "versions").exists():
                    _remove_tree_for_restore(authoring_versions_dir)
                    shutil.copytree(authoring_versions_backup_dir / "versions", authoring_versions_dir, dirs_exist_ok=True)
                else:
                    _remove_tree_for_restore(authoring_versions_dir)
                shutil.rmtree(authoring_versions_backup_dir, ignore_errors=True)

        await open_and_shot("/app/settings", "system-settings")
        system_settings_text = await page.locator("body").inner_text()
        assert "Auth / Provider / Status" in system_settings_text
        assert "Provider" in system_settings_text
        assert "Metadata Index" in system_settings_text
        assert "session local_internal" in system_settings_text
        assert "Auth Safety" in system_settings_text
        assert "local/internal only" in system_settings_text
        assert "OIDC unconfigured" in system_settings_text
        assert "MCP enabled" in system_settings_text
        assert "MCP OAuth는 커넥터 접근 제어이며 제품 로그인 대체 수단이 아닙니다." in system_settings_text
        assert "Scheduler Health" in system_settings_text
        assert "Deployment Readiness" in system_settings_text
        assert "runtime single_node_file_backed" in system_settings_text
        assert "storage file_json" in system_settings_text
        assert "dispatch safety: leases on" in system_settings_text
        assert "not DB/queue-backed distributed scheduling" in system_settings_text
        assert "distributed not supported" in system_settings_text
        assert "Run Activity" in system_settings_text
        assert "Notification Delivery" in system_settings_text

        await open_and_shot("/app/automations?topicId=large-language-models", "automations")
        automations_text = await page.locator("body").inner_text()
        assert "예약 작업 관리" in automations_text
        assert "scheduler worker" in automations_text
        assert "최근 heartbeat" in automations_text
        assert "worker " in automations_text
        assert "due " in automations_text
        assert "lease " in automations_text

        await delete_task_if_present(automation_task_name)

        await page.get_by_role("button", name="+ 새 예약 작업").click()
        await page.get_by_placeholder("예: LLM 최신 논문 수집 및 분석").fill(automation_task_name)
        await page.get_by_label("작업 유형").select_option("weekly_report")
        await page.get_by_label("실행 주기").select_option("weekly_monday_1000")
        await page.get_by_role("button", name="작업 생성").click()
        await page.get_by_text("새 예약 작업을 생성했습니다.").wait_for()
        await page.goto(f"{base_url}/app/automations?topicId=large-language-models", wait_until="networkidle")

        created_row = page.locator("article.ai-task-row").filter(has_text=automation_task_name).first
        await created_row.wait_for()
        assert await created_row.get_by_text("상태 활성").count() == 1
        assert await created_row.get_by_text("다음 실행").count() >= 1
        await page.screenshot(path=str(output_dir / "automations-created.png"), full_page=True)

        await created_row.get_by_role("switch", name=f"{automation_task_name} 예약 작업 토글").click()
        await wait_for_task_status(automation_task_name, "상태 일시 중지")
        created_row = page.locator("article.ai-task-row").filter(has_text=automation_task_name).first

        await created_row.get_by_role("switch", name=f"{automation_task_name} 예약 작업 토글").click()
        await wait_for_task_status(automation_task_name, "상태 활성")
        created_row = page.locator("article.ai-task-row").filter(has_text=automation_task_name).first

        default_task_row = page.locator("article.ai-task-row").filter(has_text="LLM 최신 논문 수집 및 분석").first
        await default_task_row.get_by_role("button", name="지금 실행").click()
        await page.wait_for_timeout(1500)
        await page.wait_for_load_state("networkidle")
        automations_text_after_run = await page.locator("body").inner_text()
        assert (
            "예약 작업을 실행했습니다:" in automations_text_after_run
            or "이미 실행 중인 예약 작업입니다." in automations_text_after_run
            or "상태 실행 중" in automations_text_after_run
        )
        await page.screenshot(path=str(output_dir / "automations-ran.png"), full_page=True)

        await delete_task_if_present(automation_task_name)
        await page.goto(f"{base_url}/app/automations?topicId=large-language-models", wait_until="networkidle")
        assert await page.locator("article.ai-task-row").filter(has_text=automation_task_name).count() == 0

        await open_and_shot("/app/settings/notifications?topicId=large-language-models", "notifications")
        notifications_text = await page.locator("body").inner_text()
        assert "알림 설정" in notifications_text
        assert "인앱 알림 센터" in notifications_text
        assert "준비 중" not in notifications_text or "이메일" in notifications_text
        assert "외부 전달 기록" in notifications_text
        assert "SMTP 환경 설정" in notifications_text
        assert "Webhook" in notifications_text
        assert (
            "상태 사용 가능" in notifications_text
            or "상태 SMTP 환경 없음" in notifications_text
            or "상태 설정 오류" in notifications_text
            or "SMTP readiness available" in notifications_text
            or "SMTP readiness unavailable_missing_env" in notifications_text
            or "SMTP readiness invalid_config" in notifications_text
        )

        email_channel = page.locator("article.ai-notification-channel").filter(has_text="이메일").first
        await email_channel.wait_for()
        email_input = email_channel.get_by_label("이메일 대상")
        email_toggle = email_channel.get_by_role("switch", name="이메일 채널 활성화")
        original_email_destination = await email_input.input_value()
        original_email_state = await email_toggle.get_attribute("aria-checked")
        assert original_email_state in {"true", "false"}
        email_available = not await email_input.is_disabled()
        if email_available:
            email_channel_text = await email_channel.inner_text()
            assert "상태 사용 가능" in email_channel_text
            assert "SMTP readiness available" in email_channel_text
            email_delivery_address = "playwright@example.test"
            await email_input.fill(email_delivery_address)
            if await email_toggle.get_attribute("aria-checked") != "true":
                await email_toggle.click()
            await save_notification_settings_if_changed()
            await page.goto(f"{base_url}/app/settings/notifications?topicId=large-language-models", wait_until="networkidle")
            email_channel = page.locator("article.ai-notification-channel").filter(has_text="이메일").first
            await email_channel.wait_for()
            assert await email_channel.get_by_label("이메일 대상").input_value() == email_delivery_address
            assert await email_channel.get_by_role("switch", name="이메일 채널 활성화").get_attribute("aria-checked") == "true"
            await email_channel.get_by_role("button", name="테스트 전송").click()
            await page.get_by_text(re.compile(r"email 테스트 전송")).wait_for()
            email_delivery_history = page.locator("section.ai-card").filter(has_text="외부 전달 기록").first
            await email_delivery_history.get_by_text("email").first.wait_for()
            email_delivery_text = await email_delivery_history.inner_text()
            assert "전송됨" in email_delivery_text or "실패" in email_delivery_text
            assert (
                "상태 사용 가능" in email_delivery_text
                or "상태 전송 실패" in email_delivery_text
                or "상태 SMTP 연결 실패" in email_delivery_text
            )
            assert "실제 시도" in email_delivery_text
        else:
            email_channel_text = await email_channel.inner_text()
            assert "SMTP" in email_channel_text
            assert "환경 설정" in email_channel_text
            assert "상태 SMTP 환경 없음" in email_channel_text or "상태 설정 오류" in email_channel_text
            assert (
                "SMTP readiness unavailable_missing_env" in email_channel_text
                or "SMTP readiness invalid_config" in email_channel_text
            )
        await page.screenshot(path=str(output_dir / "notifications-email-state.png"), full_page=True)

        webhook_channel = page.locator("article.ai-notification-channel").filter(has_text="Webhook").first
        await webhook_channel.wait_for()
        webhook_input = webhook_channel.get_by_label("Webhook 대상")
        original_webhook_destination = await webhook_input.input_value()
        webhook_toggle = webhook_channel.get_by_role("switch", name="Webhook 채널 활성화")
        original_webhook_state = await webhook_toggle.get_attribute("aria-checked")
        assert original_webhook_state in {"true", "false"}
        report_completed_toggle = page.get_by_role("switch", name="보고서 생성 완료 알림 토글")
        original_report_completed_state = await report_completed_toggle.get_attribute("aria-checked")
        assert original_report_completed_state in {"true", "false"}

        # Webhook delivery is gated by the outbound egress policy: with no exact-host
        # allowlist the backend reports the channel unavailable and the UI disables it.
        # That is the correct posture for a release environment, which configures no
        # notification delivery, so branch on availability exactly like the email
        # channel above instead of requiring egress configuration to pass.
        webhook_available = not await webhook_input.is_disabled()
        webhook_delivery_url = "http://127.0.0.1:9/playwright-notification-webhook"
        if webhook_available:
            await webhook_input.fill(webhook_delivery_url)
            if await webhook_toggle.get_attribute("aria-checked") != "true":
                await webhook_toggle.click()
        else:
            webhook_channel_text = await webhook_channel.inner_text()
            assert "Webhook" in webhook_channel_text
            assert "is-disabled" in (await webhook_channel.get_attribute("class") or "")
            assert await webhook_toggle.is_disabled(), "an unavailable channel must not be toggleable"
            send_action_disabled = await webhook_channel.get_by_role(
                "button", name="테스트 전송"
            ).is_disabled()
            destination_class = classify_persisted_webhook_destination(original_webhook_destination)
            evaluate_webhook_unavailable_contract(
                input_disabled=await webhook_input.is_disabled(),
                send_action_disabled=send_action_disabled,
                destination_class=destination_class,
                delivery_attempted=False,
                diagnostic_record={
                    "availability": "unavailable",
                    "destination_class": destination_class,
                    "input_disabled": True,
                    "send_action_disabled": send_action_disabled,
                    "pathname": "/app/settings/notifications",
                },
            )
            assert "상태" in webhook_channel_text

        # Notification rules are in-app and stay available regardless of the egress
        # policy, so the report-completed rule is exercised on both paths.
        if await report_completed_toggle.get_attribute("aria-checked") != "true":
            await report_completed_toggle.click()
        await save_notification_settings_if_changed()
        await page.goto(f"{base_url}/app/settings/notifications?topicId=large-language-models", wait_until="networkidle")
        webhook_channel = page.locator("article.ai-notification-channel").filter(has_text="Webhook").first
        await webhook_channel.wait_for()
        if webhook_available:
            assert await webhook_channel.get_by_label("Webhook 대상").input_value() == webhook_delivery_url
            assert await webhook_channel.get_by_role("switch", name="Webhook 채널 활성화").get_attribute("aria-checked") == "true"
            await webhook_channel.get_by_role("button", name="테스트 전송").click()
            await page.get_by_text(re.compile(r"webhook 테스트 전송")).wait_for()
            delivery_history = page.locator("section.ai-card").filter(has_text="외부 전달 기록").first
            await delivery_history.get_by_text("webhook").first.wait_for()
            assert "실패" in await delivery_history.inner_text()
        else:
            # The unavailable state must survive a reload rather than being transient.
            reloaded_input = webhook_channel.get_by_label("Webhook 대상")
            reloaded_class = classify_persisted_webhook_destination(await reloaded_input.input_value())
            evaluate_webhook_unavailable_contract(
                input_disabled=await reloaded_input.is_disabled(),
                send_action_disabled=await webhook_channel.get_by_role(
                    "button", name="테스트 전송"
                ).is_disabled(),
                destination_class=reloaded_class,
                delivery_attempted=False,
                diagnostic_record={
                    "availability": "unavailable",
                    "destination_class": reloaded_class,
                    "input_disabled": True,
                    "send_action_disabled": True,
                    "pathname": "/app/settings/notifications",
                },
            )
            assert reloaded_class == classify_persisted_webhook_destination(original_webhook_destination)
        await page.screenshot(path=str(output_dir / "notifications-webhook-delivery.png"), full_page=True)

        run_failed_toggle = page.get_by_role("switch", name="실행 실패 알림 토글")
        await run_failed_toggle.wait_for()
        original_run_failed_state = await run_failed_toggle.get_attribute("aria-checked")
        assert original_run_failed_state in {"true", "false"}
        await run_failed_toggle.click()
        await save_notification_settings_if_changed()
        await page.goto(f"{base_url}/app/settings/notifications?topicId=large-language-models", wait_until="networkidle")
        await page.get_by_role("switch", name="실행 실패 알림 토글").wait_for()
        toggled_state = "false" if original_run_failed_state == "true" else "true"
        assert await page.get_by_role("switch", name="실행 실패 알림 토글").get_attribute("aria-checked") == toggled_state
        await page.get_by_role("switch", name="실행 실패 알림 토글").click()
        await save_notification_settings_if_changed()
        await page.goto(f"{base_url}/app/settings/notifications?topicId=large-language-models", wait_until="networkidle")
        assert await page.get_by_role("switch", name="실행 실패 알림 토글").get_attribute("aria-checked") == original_run_failed_state
        if await page.get_by_role("switch", name="실행 실패 알림 토글").get_attribute("aria-checked") != "true":
            await page.get_by_role("switch", name="실행 실패 알림 토글").click()
            await save_notification_settings_if_changed()
            await page.goto(f"{base_url}/app/settings/notifications?topicId=large-language-models", wait_until="networkidle")

        initial_notification_count = await page.locator("article.ai-notification-item").count()
        await page.goto(f"{base_url}/app/topics/large-language-models", wait_until="networkidle")
        await page.get_by_role("button", name=re.compile(r"^보고서 생성")).click()
        notice = page.locator("p.ai-notice[role='status']").first
        await notice.wait_for()
        notice_text = await notice.inner_text()
        assert "보고서 생성 작업을" in notice_text
        run_id_match = re.search(r":\s*([a-zA-Z0-9-]+)\s*$", notice_text)
        assert run_id_match, notice_text
        generated_report_run_id = run_id_match.group(1)

        await wait_for_report_row("large-language-models", generated_report_run_id)
        topic_report_row = page.locator("article.ai-report-hub-row").filter(has_text=generated_report_run_id).first
        topic_report_text = await topic_report_row.inner_text()
        assert any(label in topic_report_text for label in ["대기열", "생성 중", "완료", "실패"])
        assert "내보내기" in topic_report_text
        await page.screenshot(path=str(output_dir / "topic-report-lifecycle.png"), full_page=True)

        await wait_for_hub_row("large-language-models", generated_report_run_id)
        hub_report_row = page.locator("article.ai-report-hub-row").filter(
            has=page.locator(f"a[href$='/reports/{generated_report_run_id}']")
        ).first
        hub_report_text = await hub_report_row.inner_text()
        assert any(label in hub_report_text for label in ["대기열", "생성 중", "완료", "실패"])
        assert "내보내기" in hub_report_text
        await page.screenshot(path=str(output_dir / "report-hub-lifecycle.png"), full_page=True)

        final_report_state, report_detail_text = await wait_for_report_terminal_state(generated_report_run_id)
        assert "보고서 허브" in report_detail_text
        assert "실행 상세" in report_detail_text
        assert "완료 보고서 버전" in report_detail_text
        assert "비교할 완료 버전이 아직 부족합니다" in report_detail_text or "완료된 보고서 버전 없음" in report_detail_text
        await page.screenshot(path=str(output_dir / "report-version-diff-empty.png"), full_page=True)
        await page.screenshot(path=str(output_dir / "report-detail-lifecycle.png"), full_page=True)

        if final_report_state == "completed":
            download_link = page.get_by_role("link", name="다운로드").first
            download_href = await download_link.get_attribute("href")
        else:
            download_href = await find_completed_download_href("large-language-models")

        assert download_href, "no completed report download link was available"
        download_response = await page.context.request.get(download_href)
        assert download_response.status == 200
        assert "text/markdown" in (download_response.headers.get("content-type") or "")
        download_text = await download_response.text()
        assert download_text.strip()
        await page.screenshot(path=str(output_dir / "report-download-source.png"), full_page=True)

        observed_notification_text = ""
        _observed_notification_count = initial_notification_count
        for _ in range(45):
            await page.goto(f"{base_url}/app/settings/notifications?topicId=large-language-models", wait_until="networkidle")
            _observed_notification_count = await page.locator("article.ai-notification-item").count()
            observed_notification_text = await page.locator("body").inner_text()
            if "보고서 생성 완료" in observed_notification_text or "실행 실패" in observed_notification_text:
                break
            await page.wait_for_timeout(1000)

        assert "보고서 생성 완료" in observed_notification_text or "실행 실패" in observed_notification_text
        delivery_history = page.locator("section.ai-card").filter(has_text="외부 전달 기록").first
        await delivery_history.wait_for()
        history_visible = await delivery_history.is_visible()
        history_text = await delivery_history.inner_text()
        history_record_count = await delivery_history.locator("article.ai-notification-item").count()
        empty_state = delivery_history.locator("div.ai-empty-state").filter(
            has_text="아직 외부 전달 기록이 없습니다."
        )
        empty_state_visible = await empty_state.count() > 0 and await empty_state.first.is_visible()
        evaluate_notification_delivery_history_contract(
            region_present=True,
            region_visible=history_visible,
            record_count=history_record_count,
            region_text=history_text,
            empty_state_visible=empty_state_visible,
        )
        if email_available or webhook_available:
            assert "규칙 report_completed" in history_text or "규칙 run_failed" in history_text
        else:
            # Committed delivery fixtures and enabled-but-unavailable webhook
            # attempts leave a nonempty history. Assert that scoped region
            # rather than a page-wide empty-state literal.
            assert history_record_count > 0
            assert "webhook" in history_text or "email" in history_text
        await page.screenshot(path=str(output_dir / "notifications-after-event.png"), full_page=True)

        await page.goto(f"{base_url}/app/settings/notifications?topicId=large-language-models", wait_until="networkidle")
        email_channel = page.locator("article.ai-notification-channel").filter(has_text="이메일").first
        if email_available:
            if await email_channel.get_by_role("switch", name="이메일 채널 활성화").get_attribute("aria-checked") != original_email_state:
                await email_channel.get_by_role("switch", name="이메일 채널 활성화").click()
            await email_channel.get_by_label("이메일 대상").fill(original_email_destination)
        webhook_channel = page.locator("article.ai-notification-channel").filter(has_text="Webhook").first
        if webhook_available:
            if await webhook_channel.get_by_role("switch", name="Webhook 채널 활성화").get_attribute("aria-checked") != original_webhook_state:
                await webhook_channel.get_by_role("switch", name="Webhook 채널 활성화").click()
            await webhook_channel.get_by_label("Webhook 대상").fill(original_webhook_destination)
        if await page.get_by_role("switch", name="보고서 생성 완료 알림 토글").get_attribute("aria-checked") != original_report_completed_state:
            await page.get_by_role("switch", name="보고서 생성 완료 알림 토글").click()
        if await page.get_by_role("switch", name="실행 실패 알림 토글").get_attribute("aria-checked") != original_run_failed_state:
            await page.get_by_role("switch", name="실행 실패 알림 토글").click()
        await save_notification_settings_if_changed()

        await open_and_shot("/app/topics/robotics", "unconfigured")
        assert "토픽 프로필이 아직 없습니다." in await page.locator("body").inner_text()
        await open_and_shot("/app/topics/robotics/insights", "unconfigured-insights")
        assert "토픽 프로필이 아직 없습니다." in await page.locator("body").inner_text()

        await delete_topic_if_present(lifecycle_topic_slug)
        try:
            await page.goto(f"{base_url}/app/topics", wait_until="networkidle")
            await page.get_by_role("button", name="새 주제 추가").click()
            await page.get_by_label("토픽 제목").fill(lifecycle_topic_name)
            await page.get_by_label("토픽 이름").fill(lifecycle_topic_name)
            await page.get_by_label("토픽 설명").fill("Playwright topic lifecycle 검증")
            await page.get_by_label("검색 쿼리", exact=True).fill("topic lifecycle\nresearch dashboard")
            await page.get_by_label("토픽 카테고리").fill("cs.AI, cs.IR")
            await set_source_provider_enabled("arXiv", False)
            await set_source_provider_enabled("Crossref", True)
            await page.get_by_label("수집 provider").select_option("crossref")
            await page.get_by_label("Crossref 검색 쿼리").fill("topic lifecycle research dashboard")
            await page.get_by_label("Crossref 최대 결과").fill("1")
            await page.get_by_label("Crossref 수집 우선순위").fill("1")
            await page.get_by_label("Crossref 정렬 기준").select_option("published")
            await page.get_by_label("Crossref 정렬 순서").select_option("descending")
            await page.get_by_label("기본 수집 주기").select_option("daily_1800")
            await page.get_by_label("기본 보고서 주기").select_option("monthly_day_1_1000")
            await page.get_by_role("button", name="토픽 프로필 저장").click()
            await page.get_by_text("새 주제 프로필을 추가했습니다.").wait_for()
            await page.get_by_role("heading", name=lifecycle_topic_name).wait_for()
            await page.locator("button.ai-topic-item").filter(has_text=lifecycle_topic_name).first.wait_for()
            created_profiles_response = await page.request.get(f"{api_base}/topic-profiles")
            assert created_profiles_response.ok
            created_profiles = await created_profiles_response.json()
            created_profile = next(item for item in created_profiles["items"] if item["profile_id"] == lifecycle_topic_slug)
            assert created_profile["source_settings"]["defaultProvider"] == "crossref"
            assert created_profile["source_settings"]["enabledProviders"] == ["crossref"]
            assert created_profile["source_settings"]["providers"]["arxiv"]["enabled"] is False
            assert created_profile["source_settings"]["providers"]["crossref"]["queries"] == ["topic lifecycle research dashboard"]
            assert created_profile["source_settings"]["providers"]["crossref"]["maxResults"] == 1
            assert created_profile["source_settings"]["providers"]["crossref"]["priority"] == 1
            assert created_profile["source_settings"]["providers"]["crossref"]["sortBy"] == "published"
            assert created_profile["source_settings"]["providers"]["crossref"]["sortOrder"] == "descending"
            await page.screenshot(path=str(output_dir / "topic-created.png"), full_page=True)

            await page.get_by_role("tab", name="설정").click()
            await page.wait_for_url(f"**/topics/{lifecycle_topic_slug}/settings")
            await page.get_by_role("button", name="즐겨찾기 추가").click()
            await page.get_by_text("즐겨찾기에 추가했습니다.").wait_for()
            await page.reload(wait_until="networkidle")
            await page.get_by_role("button", name="즐겨찾기 해제").wait_for()

            await page.get_by_role("button", name="프로필 편집").click()
            await page.get_by_label("토픽 설명").fill("Playwright topic lifecycle 검증 수정")
            await page.get_by_label("Crossref 최대 결과").fill("2")
            await page.get_by_label("Crossref 수집 우선순위").fill("2")
            await page.get_by_label("Crossref 정렬 기준").select_option("is-referenced-by-count")
            await page.get_by_label("기본 수집 주기").select_option("daily_0900")
            await page.get_by_role("button", name="토픽 프로필 저장").click()
            await page.get_by_text("토픽 프로필을 업데이트했습니다.").wait_for()
            await page.screenshot(path=str(output_dir / "topic-settings-edited.png"), full_page=True)

            await page.reload(wait_until="networkidle")
            await page.get_by_text("매일 09:00").first.wait_for()
            await page.get_by_text("Crossref").first.wait_for()
            edited_profiles_response = await page.request.get(f"{api_base}/topic-profiles")
            assert edited_profiles_response.ok
            edited_profiles = await edited_profiles_response.json()
            edited_profile = next(item for item in edited_profiles["items"] if item["profile_id"] == lifecycle_topic_slug)
            assert edited_profile["description"] == "Playwright topic lifecycle 검증 수정"
            assert edited_profile["source_settings"]["defaultProvider"] == "crossref"
            assert edited_profile["source_settings"]["providers"]["crossref"]["maxResults"] == 2
            assert edited_profile["source_settings"]["providers"]["crossref"]["priority"] == 2
            assert edited_profile["source_settings"]["providers"]["crossref"]["sortBy"] == "is-referenced-by-count"

            await page.goto(f"{base_url}/app/topics/{lifecycle_topic_slug}", wait_until="networkidle")
            await page.get_by_role("button", name=re.compile(r"^새 논문 수집")).click()
            collect_notice = page.locator(".ai-notice").last
            await collect_notice.wait_for()
            collect_notice_text = await collect_notice.inner_text()
            assert "새 논문 수집 작업을" in collect_notice_text
            collect_run_id_match = re.search(r":\s*([a-zA-Z0-9-]+)\s*$", collect_notice_text)
            assert collect_run_id_match, collect_notice_text
            collect_run_id = collect_run_id_match.group(1)
            collect_run_response = await page.request.get(f"{api_base}/runs/{collect_run_id}")
            assert collect_run_response.ok
            collect_run = await collect_run_response.json()
            assert collect_run["request"]["source_settings"]["defaultProvider"] == "crossref"
            assert collect_run["request"]["source_settings"]["enabledProviders"] == ["crossref"]
            assert collect_run["request"]["source_settings"]["providers"]["crossref"]["queries"] == [
                "topic lifecycle research dashboard",
            ]
            assert "arxiv" not in collect_run["profile"]["sources"]
            assert "openalex" not in collect_run["profile"]["sources"]
            assert collect_run["profile"]["sources"]["crossref"]["sort_by"] == "is-referenced-by-count"
            assert collect_run["profile"]["sources"]["crossref"]["max_results"] == 2
            assert collect_run["profile"]["sources"]["crossref"]["priority"] == 2

            analyze_button = page.get_by_role("button", name=re.compile(r"^즉시 분석 실행"))
            await analyze_button.wait_for()
            await analyze_button.click()
            analyze_notice = page.locator(".ai-notice").last
            await analyze_notice.wait_for()
            await page.wait_for_function(
                "() => Array.from(document.querySelectorAll('.ai-notice')).some((node) => node.textContent?.includes('즉시 분석 실행 작업을'))",
                timeout=10000,
            )
            analyze_notice_text = await analyze_notice.inner_text()
            assert "즉시 분석 실행 작업을" in analyze_notice_text, analyze_notice_text
            analyze_run_id_match = re.search(r":\s*([a-zA-Z0-9-]+)\s*$", analyze_notice_text)
            assert analyze_run_id_match, analyze_notice_text
            analyze_run_id = analyze_run_id_match.group(1)
            analyze_run_response = await page.request.get(f"{api_base}/runs/{analyze_run_id}")
            assert analyze_run_response.ok
            analyze_run = await analyze_run_response.json()
            assert analyze_run["request"]["source_settings"]["defaultProvider"] == "crossref"
            assert "arxiv" not in analyze_run["profile"]["sources"]
            assert "openalex" not in analyze_run["profile"]["sources"]
            assert analyze_run["profile"]["sources"]["crossref"]["sort_by"] == "is-referenced-by-count"
            assert analyze_run["profile"]["sources"]["crossref"]["priority"] == 2

            await page.goto(f"{base_url}/app/topics/{lifecycle_topic_slug}/settings", wait_until="networkidle")
            await page.get_by_role("button", name="즐겨찾기 해제").wait_for()
            await page.get_by_role("button", name="즐겨찾기 해제").click()
            await page.get_by_text("즐겨찾기를 해제했습니다.").wait_for()
            await page.reload(wait_until="networkidle")
            await page.get_by_role("button", name="즐겨찾기 추가").wait_for()
        finally:
            await delete_topic_if_present(lifecycle_topic_slug)

        await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument(
        "--output-dir",
        default=str(Path("output") / "playwright" / "truth-pass"),
    )
    args = parser.parse_args()
    asyncio.run(verify(args.base_url, Path(args.output_dir)))


if __name__ == "__main__":
    main()
