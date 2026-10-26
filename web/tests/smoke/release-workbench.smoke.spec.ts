import { expect, type Page, type Route, test } from "@playwright/test";
import type {
  PrototypeRealCorpusStatus,
  PrototypeStatusResponse,
  PrototypeWorkspace,
} from "../../src/lib/api";

import {
  authFixture,
  dashboardFixture,
  papersFixture,
  profilesFixture,
  reportsFixture,
  topicsFixture,
} from "../fixtures/large-language-models-dashboard";

const appOrigin = "http://127.0.0.1:5174";
const topicId = "large-language-models";
const runId = "run-report-20260507";
const paperId = "paper-constitutional-ai";
const versionId = "version-20260507";
const smokeTimestamp = "2026-05-07T00:00:00Z";

const prototypeMockPaths = new Set([
  "/prototype/status",
  "/prototype/workspace",
  "/prototype/real-corpus/status",
]);

const prototypeStatusFixture = {
  enabled: false,
  label: "Deterministic local smoke fixture",
  isolation_status: "local_only",
  reasons: ["deterministic_local_smoke"],
  data_root_configured: false,
  data_root_under_system_temp: false,
  profile_id: null,
  topic: "Deterministic local smoke",
  latest_run: null,
  can_start: false,
  live_provider_proof: {},
} satisfies PrototypeStatusResponse;

const prototypeRealCorpusStatusFixture = {
  schema_version: "prototype_real_open_access_corpus_v1",
  available: false,
  network_enabled: false,
  external_network_default: "disabled",
  topic: "Deterministic local smoke",
  query_id: "local-smoke-fixture",
  query_cutoff: "2026-05-07",
  source: "PMC Open Access",
  allowed_hosts: [],
  maximum_articles: 4,
  maximum_candidates: 4,
  maximum_http_requests: 0,
  license_policy: ["CC0", "CC BY"],
  live_provider: "disabled",
  provider_calls: 0,
  deterministic_fallback_available: true,
  latest_run: null,
} satisfies PrototypeRealCorpusStatus;

const prototypeWorkspaceFixture = {
  schema_version: "prototype_multi_topic_workspace_v0_7",
  enabled: true,
  external_network_default: "disabled",
  provider_calls: 0,
  topics: [
    {
      topic_id: topicId,
      slug: topicId,
      display_name: "Deterministic Workspace Topic",
      description: "A local-only workspace fixture for the primary smoke route.",
      research_question: "How does the current workspace remain deterministic?",
      topic_kind: "local_empty",
      source_policy: "local_only_no_external_source",
      language: "en",
      publication_date_cutoff: null,
      license_policy: "local_only",
      refresh_mode: "disabled",
      provider_posture: "deterministic_only",
      active_snapshot_id: null,
      latest_run_id: null,
      latest_report_version_id: null,
      archived: false,
      created_at: smokeTimestamp,
      updated_at: smokeTimestamp,
      settings_version: 1,
      settings_digest: "smoke-settings-v1",
      label: "Local empty topic",
      active_snapshot: null,
      snapshots: [],
      settings_history: [
        {
          topic_id: topicId,
          settings_version: 1,
          settings_digest: "smoke-settings-v1",
          changed_fields: [],
          created_at: smokeTimestamp,
          settings: {},
        },
      ],
      curation_events: [],
      changesets: [],
      refreshes: [],
      runs: [],
      reports: [],
      coverage: {
        included_papers: 0,
        reviewed_papers: 0,
        evidence_spans: 0,
        grounded_claims: 0,
        report_count: 0,
      },
      last_refresh: null,
      living_schedule: {
        schedule_id: null,
        topic_id: topicId,
        schedule_version: 0,
        enabled: false,
        cadence: "disabled",
        time_zone: "UTC",
        local_time: "09:00",
        next_due_at: null,
        last_due_at: null,
        last_attempt_at: null,
        last_success_at: null,
        last_status: "disabled",
        last_refresh_id: null,
        last_briefing_id: null,
        consecutive_failures: 0,
        catch_up_policy: "coalesce_one",
        network_policy: "disabled",
        briefing_policy: "no_change_and_material_changes",
        created_at: null,
        updated_at: null,
        schedule_digest: null,
        scheduler_health: "disabled",
      },
      schedule_history: [],
      schedule_jobs: [],
      change_events: [],
      impact_records: [],
      briefings: [],
      latest_briefing: null,
      attention_summary: { unread: 0, review_required: 0, critical: 0 },
      research_gaps_workspace: {
        analyses: [],
        current_analysis: null,
        comparisons: [],
        promoted_origin: null,
        analysis_count: 0,
        gap_count: 0,
        recommendation_count: 0,
        provider_calls: 0,
        network_requests: 0,
      },
      needs_attention: false,
    },
  ],
  totals: {
    active_topics: 1,
    archived_topics: 0,
    total_included_papers: 0,
    real_full_text_papers: 0,
    synthetic_papers: 0,
    total_reviews: 0,
    total_evidence_spans: 0,
    total_reports: 0,
    topics_needing_attention: 0,
    living_topics: 0,
    schedules_enabled: 0,
    topics_due: 0,
    unread_briefings: 0,
    review_required: 0,
    critical_alerts: 0,
    last_successful_refresh: null,
    last_briefing: null,
    research_gap_analyses: 0,
    research_gaps: 0,
    research_recommendations: 0,
    accepted_recommendations: 0,
    promoted_recommendations: 0,
  },
  recent_lineage: [],
  bundle_projects: [],
  bundle_posture: {
    schema_version: "xrml_research_project_bundle_v1",
    automatic_execution_default: "disabled",
    provider_default: "disabled",
    network_default: "disabled",
    full_text_embedded: false,
  },
} satisfies PrototypeWorkspace;

const runFixture = {
  run_id: runId,
  topic: "Large Language Models",
  status: "completed",
  source: "local_smoke_fixture",
  created_at: "2026-05-07T08:45:00+09:00",
  updated_at: "2026-05-07T08:50:00+09:00",
  topic_profile_id: topicId,
  request: {
    source_settings: {
      defaultProvider: "arxiv",
      enabledProviders: ["arxiv"],
    },
  },
  counts: {
    triage_items: 1,
    reviewed_items: 1,
  },
  artifacts: {
    report_markdown: "storage/runs/run-report-20260507/reports/preview.md",
  },
  steps: [
    { name: "collect", status: "completed", note: "fixture-only collection state" },
    { name: "review", status: "completed", note: "fixture-only review state" },
    { name: "report", status: "completed", note: "deterministic report preview ready" },
  ],
};

const paper = {
  paper_id: paperId,
  title: "Constitutional AI: Harmlessness from AI Feedback",
  source_provider: "arxiv",
  score: 92,
  score_band: "high",
  theme: "Alignment",
  published: "2026-05-06",
  entry_url: "https://arxiv.org/abs/constitutional-ai",
  pdf_url: "https://arxiv.org/pdf/constitutional-ai",
  status: "reviewed",
  abstract_excerpt: "Local fixture abstract for the smoke test.",
  score_breakdown: {
    primary_hits: ["alignment"],
    secondary_hits: ["safety"],
    negative_hits: [],
    primary_points: 50,
    secondary_points: 10,
    negative_points: 0,
    xr_bonus: 0,
    realtime_bonus: 0,
    pdf_bonus: 10,
    final_score: 92,
  },
  dedup: {
    cluster_id: "cluster-alignment",
    cluster_size: 1,
    is_primary: true,
    primary_paper_id: paperId,
    members: [paperId],
  },
  review_action: { decision: "unreviewed", pinned: false, note: "", updated_at: null },
  artifacts: { review_json: "storage/runs/run-report-20260507/reviews/paper.json" },
};

const runPapersFixture = {
  run_id: runId,
  items: [paper],
  page: 1,
  page_size: 25,
  total: 1,
  total_pages: 1,
  q: "",
  provider: null,
  dedup: "all",
  facets: {
    themes: [{ value: "Alignment", count: 1 }],
    statuses: [{ value: "reviewed", count: 1 }],
    score_bands: [{ value: "high", count: 1 }],
    providers: [{ value: "arxiv", count: 1 }],
    duplicate_clusters: 1,
  },
};

const paperDetailFixture = {
  run_id: runId,
  paper,
  abstract: "A local fixture abstract for browser smoke coverage.",
  ranking_explanation: ["score includes primary keyword hits and local fixture PDF availability"],
  related_papers: [],
};

const paperReviewFixture = {
  run_id: runId,
  paper_id: paperId,
  status: "complete",
  completeness: 0.9,
  missing_fields: [],
  title: paper.title,
  theme: "Alignment",
  one_liner: "Links safety evidence into a report draft.",
  why_it_matters: ["Demonstrates the workbench can render review evidence locally."],
  key_contributions: ["A bounded fixture contribution."],
  xr_takeaways: [],
  limitations: ["Fixture data only."],
  lab_actions: [],
  confidence: 0.8,
  method: {},
  experiments: {},
  evidence: [
    {
      evidence_id: "ev-1",
      source_id: "src-1",
      paper_id: paperId,
      title: paper.title,
      source_type: "paper",
      snippet: "Evidence snippet rendered by the local smoke.",
      location: { page: 1, reference: "abstract" },
      artifact_path: "storage/runs/run-report-20260507/evidence.json",
      quality_hint: "deterministic_rubric",
    },
  ],
  artifacts: { review_json: "storage/runs/run-report-20260507/reviews/paper.json" },
};

const outlineFixture = {
  run_id: runId,
  path: "storage/runs/run-report-20260507/reports/outline.json",
  updated_at: "2026-05-07T08:50:00+09:00",
  sections: [
    {
      section_id: "executive-summary",
      title: "Executive Summary",
      kind: "markdown",
      body_markdown: "Local smoke report draft.",
      paper_ids: [paperId],
      compare_notes: [],
      provenance: [],
    },
  ],
  applied_actions: [{ action_id: "action-1" }],
};

const previewFixture = {
  run_id: runId,
  path: "storage/runs/run-report-20260507/reports/preview.md",
  content: "# LLM Weekly Report\n\nQuality summary: deterministic_rubric / weekly_journal_h005_v1.",
  generated_at: "2026-05-07T08:50:00+09:00",
};

const exportFixture = {
  run_id: runId,
  capabilities: {
    report_markdown: { available: true, renderer: "markdown_copy" },
    report_pdf: { available: false, reason: "renderer unavailable in local smoke" },
    journal_pdf: { available: false, reason: "renderer unavailable in local smoke" },
  },
  items: [
    {
      export_id: "export-markdown",
      format: "report_markdown",
      output: "storage/runs/run-report-20260507/reports/exported.md",
      created_at: "2026-05-07T08:51:00+09:00",
      status: "ready",
      renderer: "markdown_copy",
      mime_type: "text/markdown; charset=utf-8",
      size_bytes: 256,
      sha256: "abc123fixture",
    },
  ],
};

const citationsFixture = {
  run_id: runId,
  generated_at: "2026-05-07T08:52:00+09:00",
  source_outline_path: outlineFixture.path,
  source_preview_path: previewFixture.path,
  items: [
    {
      citation_id: "executive-summary:paper-constitutional-ai",
      section_id: "executive-summary",
      section_title: "Executive Summary",
      paper_id: paperId,
      paper_title: paper.title,
      citation_text: paper.title,
      artifact_path: "storage/runs/run-report-20260507/evidence.json",
      snippet: "Evidence snippet rendered by the local smoke.",
      provenance: {},
    },
  ],
  warnings: [],
};

const versionFixture = {
  version_id: versionId,
  version_number: 1,
  run_id: runId,
  status: "completed",
  source: "exported_report_markdown",
  created_at: "2026-05-07T08:51:00+09:00",
  content_hash: "abc123fixture",
  snapshot_path: "storage/runs/run-report-20260507/reports/versions/v1.md",
  artifacts: exportFixture.items,
  artifact_availability: { markdown_snapshot: true },
  download_available: true,
  source_export_id: "export-markdown",
  provider_source_coverage: { providers: ["arxiv"], provider_counts: { arxiv: 1 } },
  citation_provenance: [paperId],
};

const settingsFixture = {
  health_status: "ok",
  auth_mode: "disabled",
  authenticated: true,
  product_session_backend: "local_internal",
  auth_warnings: ["AUTH_MODE=disabled is an explicit internal single-user mode."],
  mcp_url: `${appOrigin}/mcp`,
  provider: {
    provider: "openai",
    provider_mode: "configured_no_project_live_proof",
    configured: true,
    live_call_enabled: false,
    canonicalStatus: "configured_not_verified",
    routingLabel: "live_proof_required",
    researchOutputAllowed: false,
    reportGenerationAllowed: false,
    orchestrationAllowed: false,
    statusMessage: "Provider credential configured; project live proof and research-output gate missing.",
    model: "gpt-4o-mini",
    api_base: "redacted",
    warnings: ["Provider credential configured; project live proof and research-output gate missing."],
  },
  mcp: {
    endpoint: `${appOrigin}/mcp`,
    resource_metadata_url: `${appOrigin}/.well-known/oauth-protected-resource/mcp`,
    transport: "streamable-http",
    protocol_version: "2025-03-26",
    auth_required: true,
    public_https: false,
    cli_ready: true,
    chatgpt_ready: true,
    readiness_label: "oauth_ready",
    authorization_servers: [],
    public_tools: [],
    protected_read_tools: ["get_run_status", "get_report_preview"],
    protected_write_tools: ["start_research_run"],
    warnings: [],
  },
  deployment: {
    readiness: "degraded",
    environment: "local",
    deployment_profile: "single-service",
    shared_deployment: false,
    auth: {
      mode: "disabled",
      auth_enabled: false,
      shared_deployment: false,
      deployment_safe: true,
      deployment_safety_status: "local_internal_only",
      oidc_readiness_status: "disabled_dev_mode",
      oidc_configured: false,
      oidc_missing_env: [],
      oidc_invalid_config: [],
      oidc_audience_configured: false,
      oidc_scopes: ["openid", "profile", "email"],
      issuer_configured: false,
      issuer_valid: true,
      client_id_configured: false,
      client_secret_configured: false,
      session_secret_configured: false,
      session_secret_strong: false,
      app_base_url_valid: true,
      app_base_url: appOrigin,
      callback_url: `${appOrigin}/auth/callback`,
      secure_cookies: false,
      cookie_samesite: "lax",
      mcp_enabled: true,
      mcp_auth_required: false,
      mcp_chatgpt_ready: false,
      warnings: ["AUTH_MODE=disabled is local/internal single-user mode, not a shared deployment mode."],
      errors: [],
    },
    scheduler_architecture: "single_node_file_backed",
    scheduler_runtime_mode: "single_node_file_backed",
    scheduler_runtime_mode_status: "healthy",
    scheduler_storage_backend: "file_json",
    scheduler_architecture_note:
      "This runtime is single-node/file-backed and is not DB/queue-backed distributed scheduling.",
    single_service_deployable: true,
    distributed_scheduler_supported: false,
    warnings: [],
    errors: [],
    storage: {
      storage_mode: "repo_local_default",
      data_root: "storage",
      runs_root: "storage/runs",
      artifact_storage_root: "storage/runs",
      writable: true,
      atomic_replace_supported: true,
      windows_path_safe: true,
      layout: {
        report_versions: "storage/runs/example-run/reports/versions",
        export_artifacts: "storage/runs/example-run/reports/exports.json",
      },
      error: null,
    },
    release_validation: {
      available: true,
      status: "passed",
      created_at: "2026-07-04T12:42:19Z",
      log_dir: "output/release-validation/local-smoke-fixture",
      with_playwright: false,
      playwright_screenshot_dir: "output/playwright/truth-pass",
      step_count: 8,
      failed_step_count: 0,
      latest_summary_path: "output/release-validation/local-smoke-fixture/validation-summary.json",
      expected_warnings: [
        "Vite may emit the existing chunk-size warning during production build.",
        "OIDC is expected to report unavailable/not-ready when production IdP env is not configured.",
      ],
    },
  },
  metadata_index_state: {
    last_backfill_at: "2026-05-07T08:50:00+09:00",
    last_incremental_reason: "run_finalize",
  },
  ops: {
    generated_at: "2026-05-07T08:55:00+09:00",
    run_counts: { completed: 8, failed: 0, running: 0 },
    latest_run_id: runId,
    latest_run_status: "completed",
    latest_run_updated_at: "2026-05-07T08:50:00+09:00",
    last_successful_run_id: runId,
    last_successful_run_at: "2026-05-07T08:50:00+09:00",
    last_failed_run_id: null,
    last_failed_run_at: null,
    last_failure_message: null,
    scheduler: {
      mode: "embedded",
      configured_mode: "embedded",
      configured_mode_raw: "embedded",
      configured_mode_valid: true,
      enabled: false,
      running: false,
      status: "healthy",
      runtime_mode: "single_node_file_backed",
      runtime_mode_status: "healthy",
      runtime_mode_label: "single-node file-backed scheduler",
      storage_backend: "file_json",
      configured_runtime_mode: "single_node_file_backed",
      configured_runtime_mode_raw: "single_node_file_backed",
      configured_runtime_mode_source: "default",
      configured_runtime_mode_valid: true,
      distributed_ready: false,
      distributed_requested: false,
      distributed_support_status: "unsupported",
      multi_node_dispatch_safe: false,
      queue_backend_status: "unavailable",
      queue_item_counts: { queued: 0, leased: 0, expired: 0, failed: 0, completed: 0, total: 0 },
      architecture_note: "Single-node/file-backed scheduler only; cross-node dispatch is gated.",
      dispatch_safety: {
        dispatch_leases: true,
        duplicate_execution_protection: true,
        orphan_recovery: true,
        durable_queue: false,
      },
      operator_message: "scheduler disabled for local smoke fixture.",
      stale: false,
      last_cycle_summary: { executed_schedule_ids: [], errors: [] },
      worker_id: null,
      heartbeat_at: null,
      currently_leased_task_count: 0,
      due_task_count: 0,
      lease_health: { active: 0, expired: 0, healthy: true },
      last_dispatch_at: null,
      last_dispatch_worker_id: null,
      last_success_at: "2026-05-07T08:50:00+09:00",
      last_failure_at: null,
      last_failure_message: null,
      orphan_recovery_count: 0,
      last_orphan_recovery_at: null,
      store_lock: { locked: false, path: "storage/topic_schedules/schedules.lock" },
      schedule_summary: { due: 0, active_dispatch_leases: 0, expired_dispatch_leases: 0 },
    },
    notification_delivery: {
      total_recent_deliveries: 0,
      status_counts: { sent: 0, failed: 0 },
      channel_counts: {},
      latest_delivery_at: null,
      latest_failure_at: null,
      latest_failure_message: null,
      smtp_state: "unconfigured",
      smtp_readiness_state: "missing_env",
      smtp_available: false,
      smtp_configured: false,
      smtp_missing_env: ["SMTP_HOST"],
      smtp_invalid_config: [],
      smtp_tls_mode: "starttls",
      smtp_unavailable_reason: "SMTP env is not configured for local smoke.",
    },
  },
  stitch_status: "seam_prepared",
};

async function fulfillJson(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body),
  });
}

async function fulfillEventStream(route: Route) {
  await route.fulfill({
    status: 200,
    contentType: "text/event-stream; charset=utf-8",
    body: `data: ${JSON.stringify({
      run_id: runId,
      type: "preview_ready",
      timestamp: "2026-05-07T08:52:00+09:00",
      payload: {},
    })}\n\n`,
  });
}

function isApiLikePath(pathname: string) {
  if (pathname === "/prototype" || pathname.startsWith("/prototype/")) return true;
  return [
    "/auth/",
    "/api/",
    "/runs",
    "/topic-profiles",
    "/dashboard/",
    "/settings/",
    "/compare",
  ].some((prefix) => pathname === prefix.replace(/\/$/, "") || pathname.startsWith(prefix));
}

async function installLocalApiMocks(page: Page) {
  const externalRequests: string[] = [];
  const unexpectedApiRequests: string[] = [];
  const fulfilledPrototypePaths: string[] = [];

  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== appOrigin) {
      externalRequests.push(`${request.method()} ${request.url()}`);
      await route.abort("blockedbyclient");
      return;
    }

    const pathname = url.pathname;
    if (request.method() === "OPTIONS" && isApiLikePath(pathname)) {
      await route.fulfill({ status: 204 });
      return;
    }
    if (request.method() === "GET" && pathname === "/prototype/status") {
      fulfilledPrototypePaths.push(pathname);
      return fulfillJson(route, prototypeStatusFixture);
    }
    if (request.method() === "GET" && pathname === "/prototype/workspace") {
      fulfilledPrototypePaths.push(pathname);
      return fulfillJson(route, prototypeWorkspaceFixture);
    }
    if (request.method() === "GET" && pathname === "/prototype/real-corpus/status") {
      fulfilledPrototypePaths.push(pathname);
      return fulfillJson(route, prototypeRealCorpusStatusFixture);
    }
    if (pathname === "/auth/me") return fulfillJson(route, authFixture);
    if (pathname === "/api/topics") return fulfillJson(route, topicsFixture);
    if (pathname === "/topic-profiles") return fulfillJson(route, profilesFixture);
    if (pathname === `/api/topics/${topicId}/dashboard`) return fulfillJson(route, dashboardFixture);
    if (pathname === `/api/topics/${topicId}/papers`) return fulfillJson(route, papersFixture);
    if (pathname === "/api/reports") return fulfillJson(route, reportsFixture);
    if (pathname === "/settings/status") return fulfillJson(route, settingsFixture);
    if (pathname === "/runs") return fulfillJson(route, [runFixture]);
    if (pathname === `/runs/${runId}`) return fulfillJson(route, runFixture);
    if (pathname === `/runs/${runId}/rerun-presets`) return fulfillJson(route, { run_id: runId, items: [] });
    if (pathname === `/runs/${runId}/events`) return fulfillEventStream(route);
    if (pathname === `/runs/${runId}/papers`) return fulfillJson(route, runPapersFixture);
    if (pathname === `/runs/${runId}/papers/${paperId}`) return fulfillJson(route, paperDetailFixture);
    if (pathname === `/runs/${runId}/papers/${paperId}/review`) return fulfillJson(route, paperReviewFixture);
    if (pathname === `/runs/${runId}/outline`) return fulfillJson(route, outlineFixture);
    if (pathname === `/runs/${runId}/preview`) return fulfillJson(route, previewFixture);
    if (pathname === `/runs/${runId}/exports`) return fulfillJson(route, exportFixture);
    if (pathname === `/runs/${runId}/citations`) return fulfillJson(route, citationsFixture);
    if (pathname === `/runs/${runId}/workspace-views`) return fulfillJson(route, { items: [] });
    if (pathname === `/runs/${runId}/editing-sessions`) return fulfillJson(route, { items: [] });
    if (pathname === `/runs/${runId}/workspace-actions/staged`) {
      return fulfillJson(route, { run_id: runId, editing_session_id: null, items: [] });
    }
    if (pathname === `/runs/${runId}/workspace-suggestions`) {
      return fulfillJson(route, { run_id: runId, compare_partner_run_id: null, items: [], warnings: [] });
    }
    if (pathname === `/runs/${runId}/outline-action-history`) {
      return fulfillJson(route, { run_id: runId, items: [], warnings: [] });
    }
    if (pathname === `/runs/${runId}/report`) {
      return fulfillJson(route, { run_id: runId, path: previewFixture.path, content: previewFixture.content });
    }
    if (pathname === `/runs/${runId}/report/versions`) {
      return fulfillJson(route, { run_id: runId, total: 1, warnings: [], items: [versionFixture] });
    }
    if (pathname === `/api/reports/${runId}/versions/${versionId}`) {
      return fulfillJson(route, {
        run_id: runId,
        version: versionFixture,
        content: previewFixture.content,
        warnings: [],
        source_refs: { run_id: runId },
      });
    }

    if (isApiLikePath(pathname)) {
      unexpectedApiRequests.push(`${request.method()} ${request.url()}`);
      await route.fulfill({
        status: 500,
        contentType: "application/json; charset=utf-8",
        body: JSON.stringify({ detail: `Unexpected smoke API request: ${pathname}` }),
      });
      return;
    }

    await route.continue();
  });

  return { externalRequests, fulfilledPrototypePaths, unexpectedApiRequests };
}

test("core workbench routes render with local-only mocked APIs", async ({ page }) => {
  const consoleErrors: string[] = [];
  const requestFailures: Array<{
    method: string;
    originClass: "same_origin" | "off_origin" | "invalid_url";
    pathname: string;
    resourceType: string;
    errorText: string;
  }> = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => {
    consoleErrors.push(error.message);
  });
  page.on("requestfailed", (request) => {
    const rawFailureText = request.failure()?.errorText?.trim();
    const errorText = !rawFailureText
      ? "unknown_request_failure"
      : /(?:https?|file):\/\//i.test(rawFailureText) || /[A-Za-z]:[\\/]/.test(rawFailureText)
        ? "sanitized_request_failure"
        : rawFailureText;
    try {
      const url = new URL(request.url());
      requestFailures.push({
        method: request.method(),
        originClass: url.origin === appOrigin ? "same_origin" : "off_origin",
        pathname: url.pathname,
        resourceType: request.resourceType(),
        errorText,
      });
    } catch {
      requestFailures.push({
        method: request.method(),
        originClass: "invalid_url",
        pathname: "invalid_url",
        resourceType: request.resourceType(),
        errorText,
      });
    }
  });
  const { externalRequests, fulfilledPrototypePaths, unexpectedApiRequests } = await installLocalApiMocks(page);

  expect(isApiLikePath("/prototype")).toBe(true);
  expect(isApiLikePath("/prototype/status")).toBe(true);
  expect(isApiLikePath("/prototype/workspace")).toBe(true);
  expect(isApiLikePath("/prototype/real-corpus/status")).toBe(true);
  expect(isApiLikePath("/prototype/future-unmocked-endpoint")).toBe(true);
  expect(isApiLikePath("/app/prototype/status")).toBe(false);
  expect(isApiLikePath("/prototype-assets/icon.svg")).toBe(false);

  await page.goto(`/app/topics/${topicId}/dashboard`);
  await expect(page).toHaveURL(`/app/topics/${topicId}/dashboard`);
  await expect(page.getByRole("heading", { name: "Deterministic Workspace Topic", exact: true })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Topic sections" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Coverage and freshness" })).toBeVisible();

  await page.goto(`/app/runs/${runId}`);
  await expect(page.getByRole("heading", { name: "워크벤치 흐름" })).toBeVisible();
  await expect(page.getByRole("heading", { name: paper.title })).toBeVisible();
  await page.getByRole("button", { name: "아웃라인 편집 열기" }).click();
  await expect(page.getByRole("heading", { name: "Report workspace" })).toBeVisible();
  await expect(page.getByText("deterministic_rubric")).toBeVisible();

  await page.goto("/app/reports");
  await expect(page.getByRole("heading", { name: "보고서 허브" })).toBeVisible();
  await expect(page.getByText("LLM 주간 트렌드 리포트")).toBeVisible();
  await expect(page.getByText(/완료 버전 1개/)).toBeVisible();
  await expect(page.getByRole("link", { name: "다운로드" })).toBeVisible();

  await page.goto("/app/settings");
  await expect(page.getByRole("heading", { name: "Auth / Provider / Status" })).toBeVisible();
  await expect(page.getByText("configured_not_verified")).toBeVisible();
  await expect(page.getByText("live_proof_required")).toBeVisible();
  await expect(page.getByText("research output blocked")).toBeVisible();
  await expect(page.getByText("report generation blocked")).toBeVisible();
  await expect(page.getByText("OAuth config ready (proof pending)", { exact: true })).toBeVisible();
  await expect(page.getByText("ChatGPT OAuth config ready (proof pending)", { exact: true })).toBeVisible();
  await expect(page.getByText("MCP OAuth는 커넥터 접근 제어이며 제품 로그인 대체 수단이 아닙니다.")).toBeVisible();
  await expect(page.getByText("production and external beta", { exact: false })).toHaveCount(0);
  await expect(page.getByText("live_ready")).toHaveCount(0);
  await expect(page.getByText("ChatGPT OAuth ready")).toHaveCount(0);

  expect(unexpectedApiRequests).toEqual([]);
  expect(externalRequests).toEqual([]);
  expect(requestFailures).toEqual([]);
  expect(consoleErrors).toEqual([]);
  expect(fulfilledPrototypePaths).toContain("/prototype/workspace");
  expect(fulfilledPrototypePaths.every((pathname) => prototypeMockPaths.has(pathname))).toBe(true);
});
