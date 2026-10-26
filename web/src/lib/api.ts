export type LineageMode =
  | "standalone"
  | "full_rerun_from_profile"
  | "branch_from_selected_papers"
  | "rerun_with_profile_overrides";
export type RerunMode = Exclude<LineageMode, "standalone">;
export type SelectionSource = "manual" | "compare_bucket" | "preset" | "suggested";
export type CompareView = "papers" | "themes" | "report" | "exports" | "lineage" | "rerun" | "preset";

export type RunLineageRecord = {
  parent_run_id?: string | null;
  root_run_id: string;
  lineage_mode: LineageMode;
  source_paper_ids: string[];
  created_from_profile_snapshot: boolean;
  compare_baseline_run_id?: string | null;
  branch_reason?: string | null;
  created_at: string;
  notes: string;
};

export type ExperimentStatus = {
  schema_version: string;
  artifact_kind: string;
  lifecycle_state: string;
  generation_status: string;
  execution_status: "not_executed" | "unknown_legacy" | "unknown";
  executable: false;
  runner_available: false;
  truth_classification?: string[];
  required_inputs?: string[];
  unresolved_items?: Array<{
    code: string;
    label: string;
  }>;
  evidence_refs?: string[];
  artifact_ref?: string;
};

export type RunRecord = {
  run_id: string;
  topic: string;
  status: string;
  source?: string;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  request?: Record<string, unknown>;
  steps?: Array<Record<string, unknown>>;
  counts?: Record<string, number>;
  artifacts?: Record<string, unknown>;
  experiment_status?: ExperimentStatus;
  compare_ready?: Record<string, unknown>;
  topic_profile_id?: string | null;
  schedule_context?: Record<string, unknown>;
  lineage?: RunLineageRecord;
  rerun_context?: {
    compare_baseline_run_id?: string | null;
    preset_id?: string | null;
    selection_source?: SelectionSource | null;
    selection_bucket_id?: string | null;
    selection_bucket_label?: string | null;
    selected_paper_ids?: string[];
  };
  prototype?: {
    schema_version: string;
    label: string;
    corpus_kind: string;
    corpus_count: number;
    pipeline_status: string;
    report_status: string;
    reviewed_paper_count?: number;
    evidence_span_count?: number;
    parent_run_id?: string | null;
    report_quality?: ReportQualitySummary | null;
    provider: PrototypeProviderSummary;
    real_corpus?: {
      source: string;
      query_id: string;
      discovered_candidate_count?: number;
      license_eligible_count?: number;
      selected_article_count?: number;
      network_request_count: number;
      total_downloaded_bytes?: number;
      provider_call_count: number;
      certification_status: string;
      failure_category?: string | null;
      record_digest?: string;
      replay_status?: string;
      replay_network_request_count?: number;
    };
  };
  report_quality?: ReportQualitySummary | null;
  error?: string | null;
};

export type PrototypeProviderSummary = {
  provider_id?: string | null;
  surface_id?: string | null;
  stage: string;
  output_class: string;
  route_status: string;
  fallback_used: boolean;
  fallback_reason?: string | null;
  live_provider_invoked: boolean;
  accepted_control: string;
  rejection_controls: string;
  control_total?: number | null;
  control_passed?: number | null;
  record_digest?: string | null;
};

export type PrototypeRunSummary = {
  run_id: string;
  topic_profile_id?: string | null;
  topic: string;
  status: string;
  created_at?: string | null;
  finished_at?: string | null;
  lineage: RunLineageRecord;
  counts: { papers?: number; reviewed?: number; evidence_spans?: number };
  report_status: string;
  provider: PrototypeProviderSummary;
  report_quality: ReportQualitySummary | Record<string, unknown>;
};

export type PrototypeStatusResponse = {
  enabled: boolean;
  label: string;
  isolation_status: string;
  reasons: string[];
  data_root_configured: boolean;
  data_root_under_system_temp: boolean;
  profile_id?: string | null;
  topic: string;
  latest_run?: PrototypeRunSummary | null;
  can_start: boolean;
  live_provider_proof: PrototypeLiveAvailability;
};

export type PrototypeRealCorpusStatus = {
  schema_version: string;
  available: boolean;
  network_enabled: boolean;
  external_network_default: "disabled";
  topic: string;
  query_id: string;
  query_cutoff: string;
  source: "PMC Open Access";
  allowed_hosts: string[];
  maximum_articles: number;
  maximum_candidates: number;
  maximum_http_requests: number;
  license_policy: string[];
  live_provider: "disabled";
  provider_calls: 0;
  deterministic_fallback_available: boolean;
  latest_run?: PrototypeRunSummary | null;
};

export type PrototypeWorkspaceTopicKind = "synthetic_demo" | "real_open_access" | "local_empty";
export type PrototypeWorkspaceSourcePolicy =
  | "synthetic_fixture"
  | "pmc_open_access"
  | "local_only_no_external_source";
export type PrototypeWorkspaceProviderPosture =
  | "deterministic_only"
  | "prototype_provider_capability_available_but_disabled";
export type PrototypeWorkspaceCurationState = "included" | "excluded" | "hold";
export type PrototypeWorkspaceCurationReason =
  | "relevant_core"
  | "relevant_context"
  | "duplicate_work"
  | "out_of_scope"
  | "insufficient_full_text"
  | "license_not_accepted"
  | "article_type_not_eligible"
  | "quality_or_relevance_hold"
  | "manual_owner_decision";

export type PrototypeWorkspaceCorpusItem = {
  corpus_item_id: string;
  topic_id: string;
  snapshot_id: string;
  source_type: string;
  source_identity: string;
  pmcid?: string | null;
  pmid?: string | null;
  doi?: string | null;
  work_identity_digest: string;
  title: string;
  publication_date?: string | null;
  license_status: string;
  license_type?: string | null;
  full_text_status: string;
  review_status: string;
  evidence_count: number;
  curation_state: PrototypeWorkspaceCurationState;
  curation_reason: PrototypeWorkspaceCurationReason;
  source_content_digest?: string | null;
  stale_status: string;
};

export type PrototypeWorkspaceSnapshot = {
  snapshot_id: string;
  topic_id: string;
  parent_snapshot_id?: string | null;
  snapshot_version: number;
  source_policy: PrototypeWorkspaceSourcePolicy;
  query_id: string;
  query_digest: string;
  settings_version: number;
  settings_digest: string;
  created_at: string;
  created_by_mode: string;
  refresh_id?: string | null;
  item_count: number;
  included_count: number;
  excluded_count: number;
  hold_count: number;
  reviewed_count: number;
  evidence_count: number;
  manifest_digest: string;
  active: boolean;
  superseded_by_snapshot_id?: string | null;
  change_summary: Record<string, number>;
  source_run_id?: string | null;
  items: PrototypeWorkspaceCorpusItem[];
};

export type PrototypeWorkspaceSettingsVersion = {
  topic_id: string;
  settings_version: number;
  settings_digest: string;
  changed_fields: string[];
  created_at: string;
  new_snapshot_or_run_recommended?: boolean;
  settings: Record<string, unknown>;
};

export type PrototypeWorkspaceChangeset = {
  changeset_id: string;
  kind: "curation" | "refresh";
  topic_id: string;
  source_snapshot_id: string;
  proposed_parent_snapshot_id: string;
  proposed_snapshot_version: number;
  settings_version: number;
  added_item_ids: string[];
  removed_item_ids: string[];
  included_item_ids: string[];
  excluded_item_ids: string[];
  held_item_ids: string[];
  restored_item_ids: string[];
  license_blocked_item_ids: string[];
  stale_item_ids: string[];
  unchanged_item_count: number;
  projected_review_count: number;
  projected_evidence_count: number;
  projected_report_impact: string;
  new_report_run_recommended: boolean;
  changeset_digest: string;
  created_timestamp: string;
  applied: boolean;
  failed: boolean;
  discarded: boolean;
  resulting_snapshot_id?: string | null;
  no_accepted_change: boolean;
};

export type PrototypeWorkspaceRefresh = {
  refresh_id: string;
  topic_id: string;
  source_snapshot_id: string;
  query_id: string;
  status: string;
  started_at: string;
  completed_at?: string | null;
  request_count: number;
  candidate_count: number;
  license_eligible_count: number;
  added_work_count: number;
  removed_unavailable_count: number;
  metadata_changed_count: number;
  content_changed_count: number;
  unchanged_count: number;
  failure_category?: string | null;
  changeset_id?: string | null;
  applied_snapshot_id?: string | null;
  retries: number;
  non_allowlisted_requests: number;
  schedule_id?: string | null;
  schedule_version?: number | null;
  due_at?: string | null;
  idempotency_key?: string | null;
  execution_mode?: "manual" | "scheduled" | "catch_up" | string;
  network_policy?: string;
  briefing_id?: string | null;
  no_material_change?: boolean;
};

export type PrototypeLivingCadence = "disabled" | "manual" | "daily" | "weekly" | "monthly";

export type PrototypeLivingSchedule = {
  schedule_id?: string | null;
  topic_id: string;
  schedule_version: number;
  enabled: boolean;
  cadence: PrototypeLivingCadence;
  time_zone: string;
  local_time: string;
  next_due_at?: string | null;
  last_due_at?: string | null;
  last_attempt_at?: string | null;
  last_success_at?: string | null;
  last_status: string;
  last_refresh_id?: string | null;
  last_briefing_id?: string | null;
  consecutive_failures: number;
  catch_up_policy: "coalesce_one";
  network_policy: "disabled" | "manual_official_source_only" | "scheduled_official_source_only";
  briefing_policy: "no_change_and_material_changes" | "material_changes_only";
  created_at?: string | null;
  updated_at?: string | null;
  schedule_digest?: string | null;
  scheduler_health: string;
};

export type PrototypeLivingChangeEvent = {
  event_id: string;
  topic_id: string;
  source_ref: string;
  before_snapshot_id?: string | null;
  after_snapshot_id?: string | null;
  work_identity?: string | null;
  event_type: string;
  event_subtype?: string | null;
  materiality: string;
  severity: "info" | "attention" | "critical" | string;
  reason_code: string;
  detected_at?: string | null;
  affected_review_ids: string[];
  affected_evidence_ids: string[];
  affected_claim_ids: string[];
  affected_report_version_ids: string[];
};

export type PrototypeLivingImpact = {
  impact_id: string;
  topic_id: string;
  change_event_id: string;
  impact_state: string;
  severity: string;
  recommended_action: string;
  review_ids: string[];
  evidence_ids: string[];
  claim_ids: string[];
  report_version_ids: string[];
  source_paragraph_ids: string[];
};

export type PrototypeLivingBriefing = {
  briefing_id: string;
  topic_id: string;
  briefing_version: number;
  parent_briefing_id?: string | null;
  schedule_id?: string | null;
  schedule_version?: number | null;
  refresh_id?: string | null;
  source_snapshot_id: string;
  observed_snapshot_id?: string | null;
  briefing_type: string;
  status: string;
  period_start: string;
  period_end: string;
  generated_at: string;
  headline: string;
  executive_summary: string;
  what_changed: string[];
  why_it_matters: string[];
  change_event_ids: string[];
  impact_record_ids: string[];
  paper_ids: string[];
  review_ids: string[];
  evidence_ids: string[];
  claim_ids: string[];
  report_version_ids: string[];
  source_paragraph_ids: string[];
  coverage_before: number;
  coverage_after: number;
  recommended_actions: string[];
  attention_count: number;
  critical_count: number;
  acknowledged: boolean;
  dismissed: boolean;
  briefing_digest: string;
  attention_items?: Array<{
    change_event_id: string;
    event_type?: string | null;
    severity?: string | null;
    alert_class?: "informational" | "review_required" | "critical";
    state: "unread" | "acknowledged" | "dismissed" | "suppressed_for_repetition";
  }>;
};

export type PrototypeLivingBriefingComparison = {
  topic_id: string;
  before_briefing_id: string;
  after_briefing_id: string;
  added_change_event_ids: string[];
  resolved_change_event_ids: string[];
  retained_change_event_ids: string[];
  coverage_delta: number;
  critical_delta: number;
  comparison_digest: string;
};

export type PrototypeWorkspaceRun = {
  run_id: string;
  topic_id: string;
  snapshot_id: string;
  snapshot_version: number;
  settings_version: number;
  source_manifest_digest: string;
  evidence_set_digest?: string | null;
  parent_run_id?: string | null;
  status: string;
  created_at: string;
  finished_at?: string | null;
  generation_mode: string;
  provider_calls: number;
  counts: Record<string, number>;
  report_quality: Record<string, unknown>;
  report_version_id?: string | null;
  report_status?: string | null;
};

export type PrototypeWorkspaceReport = {
  report_version_id: string;
  run_id: string;
  topic_id: string;
  snapshot_id: string;
  snapshot_version: number;
  evidence_set_digest?: string | null;
  generation_mode: string;
  quality: Record<string, unknown>;
  status: string;
};

export type PrototypeStudyProfile = {
  study_profile_id: string;
  topic_id: string;
  snapshot_id: string;
  run_id: string;
  paper_id: string;
  paper_version_id: string;
  source_content_digest: string;
  review_id: string;
  profile_version: string;
  extraction_method: string;
  design_type: string;
  population_tags: string[];
  context_tags: string[];
  intervention_tags: string[];
  comparator_tags: string[];
  outcome_tags: string[];
  method_tags: string[];
  follow_up_bucket: string;
  evidence_direction: string;
  evidence_strength: string;
  limitation_codes: string[];
  data_availability_codes: string[];
  sample_size_value?: number | null;
  sample_size_known: boolean;
  coverage_section_count: number;
  coverage_paragraph_count: number;
  evidence_ids: string[];
  claim_ids: string[];
  unknown_fields: string[];
  created_at: string;
  profile_digest: string;
  stale: boolean;
};

export type PrototypeEvidenceMatrix = {
  matrix_id: string;
  topic_id: string;
  snapshot_id: string;
  run_id: string;
  matrix_version: string;
  columns: string[];
  rows: Array<{
    study_profile_id: string;
    paper_id: string;
    cells: Record<string, { value: string | number | string[]; evidence_ids: string[] }>;
  }>;
  source_paper_count: number;
  created_at: string;
  matrix_digest: string;
};

export type PrototypeResearchGap = {
  gap_id: string;
  gap_key: string;
  topic_id: string;
  snapshot_id: string;
  run_id: string;
  report_version_id: string;
  analysis_id: string;
  gap_type: string;
  title: string;
  bounded_statement: string;
  scope_qualifier: "WITHIN_CURRENT_ACCEPTED_CORPUS";
  detection_rule: string;
  materiality: "exploratory" | "actionable" | "high_priority";
  confidence: string;
  independent_paper_ids: string[];
  review_ids: string[];
  evidence_ids: string[];
  claim_ids: string[];
  briefing_ids: string[];
  change_event_ids: string[];
  supporting_profile_ids: string[];
  unknown_or_missing_dimensions: string[];
  stale: boolean;
  status: string;
};

export type PrototypeRecommendationStatus = "generated" | "accepted" | "held" | "rejected" | "promoted" | "stale";

export type PrototypeRecommendationDecision = {
  decision_event_id: string;
  recommendation_id: string;
  topic_id: string;
  analysis_id: string;
  previous_status: string;
  requested_status: "accepted" | "held" | "rejected";
  reason_code: string;
  bounded_note?: string | null;
  actor_mode: "LOCAL_PROTOTYPE_USER";
  created_at: string;
  decision_digest: string;
};

export type PrototypeRecommendationOrigin = {
  origin_id: string;
  new_topic_id: string;
  source_topic_id: string;
  source_snapshot_id: string;
  source_run_id: string;
  source_report_version_id: string;
  analysis_id: string;
  gap_ids: string[];
  recommendation_id: string;
  decision_event_id: string;
  created_at: string;
  origin_digest: string;
};

export type PrototypeResearchRecommendation = {
  recommendation_id: string;
  recommendation_key: string;
  topic_id: string;
  source_snapshot_id: string;
  source_run_id: string;
  source_report_version_id: string;
  analysis_id: string;
  recommendation_type: string;
  title: string;
  research_question: string;
  candidate_hypothesis: string;
  null_hypothesis: string;
  hypothesis_status: string;
  target_population: string[];
  target_context: string[];
  intervention_or_exposure: string[];
  comparator: string[];
  primary_outcomes: string[];
  secondary_outcomes: string[];
  proposed_study_design: string;
  minimum_data_requirements: string[];
  measurement_requirements: string[];
  time_horizon: string;
  supporting_gap_ids: string[];
  supporting_paper_ids: string[];
  supporting_evidence_ids: string[];
  constraint_paper_ids: string[];
  constraint_evidence_ids: string[];
  contradictory_evidence_ids: string[];
  assumptions: string[];
  limitations: string[];
  ethics_or_practical_considerations: string[];
  corpus_relative_novelty_score: number;
  global_novelty_status: "NOT_ASSESSED_OUTSIDE_CURRENT_CORPUS";
  feasibility_score: number;
  significance_score: number;
  falsifiability_score: number;
  evidence_readiness_score: number;
  timeliness_score: number;
  overall_score: number;
  score_label: string;
  score_explanation: string;
  scoring_policy_version: "research_recommendation_scoring_v1";
  confidence: string;
  status: PrototypeRecommendationStatus;
  stored_status: "generated";
  created_at: string;
  stale: boolean;
  collision: {
    result: string;
    maximum_similarity: number;
    comparison_target_ids: string[];
    overlapping_dimensions: string[];
    policy_version: string;
    disclosure: string;
  };
  latest_decision?: PrototypeRecommendationDecision | null;
  decision_history: PrototypeRecommendationDecision[];
  promotion_origin?: PrototypeRecommendationOrigin | null;
  current_for_active_snapshot?: boolean;
  historical?: boolean;
};

export type PrototypeGapAnalysis = {
  analysis_id: string;
  topic_id: string;
  snapshot_id: string;
  run_id: string;
  report_version_id: string;
  scope_id: string;
  scope_version: number;
  profile_version: string;
  gap_rule_version: string;
  scoring_policy_version: string;
  input_paper_count: number;
  input_review_count: number;
  input_evidence_count: number;
  input_claim_count: number;
  input_briefing_count: number;
  input_change_event_count: number;
  matrix_id: string;
  matrix_digest: string;
  input_manifest_digest: string;
  started_at: string;
  completed_at?: string | null;
  status: "PENDING" | "RUNNING" | "COMPLETED" | "INSUFFICIENT_EVIDENCE" | "FAILED_VALIDATION";
  gap_count: number;
  recommendation_count: number;
  analysis_digest: string;
  scope?: Record<string, unknown> | null;
  study_profiles: PrototypeStudyProfile[];
  evidence_matrix?: PrototypeEvidenceMatrix | null;
  gaps: PrototypeResearchGap[];
  recommendations: PrototypeResearchRecommendation[];
  warnings: string[];
  provider_calls: 0;
  external_network_requests: 0;
};

export type PrototypeGapComparison = {
  comparison_id: string;
  topic_id: string;
  before_snapshot_id: string;
  after_snapshot_id: string;
  before_analysis_id: string;
  after_analysis_id: string;
  gaps_added: string[];
  gaps_removed: string[];
  gaps_retained: Array<Record<string, string>>;
  gaps_changed: string[];
  recommendation_ids_added: string[];
  recommendation_ids_removed: string[];
  recommendation_score_deltas: Array<{ recommendation_key: string; delta: number }>;
  support_paper_deltas: Array<{ recommendation_key: string; delta: number }>;
  support_evidence_deltas: Array<{ recommendation_key: string; delta: number }>;
  confidence_deltas: Array<{ recommendation_key: string; before: string; after: string }>;
  stale_transitions: Array<{ recommendation_id: string; transition: string }>;
  change_attribution: string;
  interpretation: string;
  comparison_digest: string;
};

export type PrototypeResearchGapsWorkspace = {
  analyses: PrototypeGapAnalysis[];
  current_analysis?: PrototypeGapAnalysis | null;
  comparisons: PrototypeGapComparison[];
  promoted_origin?: PrototypeRecommendationOrigin | null;
  analysis_count: number;
  gap_count: number;
  recommendation_count: number;
  provider_calls: 0;
  network_requests: 0;
};

export type PrototypeWorkspaceTopic = {
  topic_id: string;
  slug: string;
  display_name: string;
  description: string;
  research_question: string;
  topic_kind: PrototypeWorkspaceTopicKind;
  source_policy: PrototypeWorkspaceSourcePolicy;
  language: string;
  publication_date_cutoff?: string | null;
  license_policy: string;
  refresh_mode: string;
  provider_posture: PrototypeWorkspaceProviderPosture;
  active_snapshot_id?: string | null;
  latest_run_id?: string | null;
  latest_report_version_id?: string | null;
  archived: boolean;
  created_at: string;
  updated_at: string;
  settings_version: number;
  settings_digest: string;
  label: string;
  active_snapshot?: PrototypeWorkspaceSnapshot | null;
  snapshots: PrototypeWorkspaceSnapshot[];
  settings_history: PrototypeWorkspaceSettingsVersion[];
  curation_events: Array<Record<string, unknown>>;
  changesets: PrototypeWorkspaceChangeset[];
  refreshes: PrototypeWorkspaceRefresh[];
  runs: PrototypeWorkspaceRun[];
  reports: PrototypeWorkspaceReport[];
  coverage: {
    included_papers: number;
    reviewed_papers: number;
    evidence_spans: number;
    grounded_claims: number;
    report_count: number;
  };
  last_refresh?: PrototypeWorkspaceRefresh | null;
  living_schedule: PrototypeLivingSchedule;
  schedule_history: PrototypeLivingSchedule[];
  schedule_jobs: Array<Record<string, unknown>>;
  change_events: PrototypeLivingChangeEvent[];
  impact_records: PrototypeLivingImpact[];
  briefings: PrototypeLivingBriefing[];
  latest_briefing?: PrototypeLivingBriefing | null;
  attention_summary: {
    unread: number;
    review_required: number;
    critical: number;
  };
  research_gaps_workspace: PrototypeResearchGapsWorkspace;
  research_origin_id?: string | null;
  origin_recommendation_id?: string | null;
  bounded_rationale?: string | null;
  candidate_hypothesis?: string | null;
  suggested_study_design?: string | null;
  suggested_population?: string[];
  suggested_context?: string[];
  suggested_outcomes?: string[];
  needs_attention: boolean;
  bundle_import?: {
    project_id: string;
    project_semantic_digest?: string | null;
    activation_posture: "DISABLED_PENDING_LOCAL_RECONFIRMATION";
    automatic_execution_enabled: false;
    full_text_embedded: false;
  };
};

export type PrototypeBundleProject = {
  project_id: string;
  project_created_at: string;
  project_semantic_digest: string;
  bundle_id: string;
  bundle_content_digest: string;
  bundle_schema_version: "xrml_research_project_bundle_v1";
  source_prototype_version?: "v0.7";
  source_exported_at?: string;
  imported_at?: string;
  selected_topic_ids: string[];
  source_topic_count?: number;
  imported_topic_count?: number;
  record_counts: Record<string, number>;
  rights_summary?: { records: number; accepted: number; requires_review: number };
  exclusion_policy?: string[];
  activation_posture: "DISABLED_PENDING_LOCAL_RECONFIRMATION";
  automatic_execution_enabled: false;
  full_text_embedded: false;
  status: "IMPORTED";
};

export type PrototypeBundleList = {
  bundle_schema_version: "xrml_research_project_bundle_v1";
  projects: PrototypeBundleProject[];
  current_workspace: { topic_count: number; imported: boolean; export_preview_required: true };
  limits: {
    compressed_bytes: number;
    uncompressed_bytes: number;
    entries: number;
    individual_entry_bytes: number;
    compression_ratio: number;
  };
  activation_posture: "DISABLED_PENDING_LOCAL_RECONFIRMATION";
  provider_calls: 0;
  network_requests: 0;
};

export type PrototypeBundlePreview = {
  status: "READY_FOR_CONFIRMATION" | "EXPORTED" | "VALIDATED_READY_FOR_CONFIRMATION" | "IMPORTED";
  preview_digest: string;
  bundle_schema_version: "xrml_research_project_bundle_v1";
  source_prototype_version?: "v0.7";
  project_id: string;
  project_semantic_digest: string;
  bundle_id: string;
  bundle_content_digest: string;
  selected_topic_ids: string[];
  record_counts: Record<string, number>;
  rights_summary?: { records: number; accepted: number; requires_review: number };
  archive_size_bytes?: number;
  entry_count?: number;
  excluded_content?: string[];
  validation_status?: {
    archive_safety: "VERIFIED";
    manifest: "VERIFIED";
    schema: "VERIFIED";
    integrity: "VERIFIED";
    lineage: "VERIFIED";
    rights: "PRESERVED";
  };
  automatic_execution_enabled: false;
  provider_calls: 0;
  network_requests: 0;
  writes?: number;
  target_writes?: number;
  idempotent?: boolean;
  collision_status?: string;
  migration_outcome?: string;
  rollback_status?: string;
  activation_posture?: "DISABLED_PENDING_LOCAL_RECONFIRMATION";
  import_id?: string;
  state_history?: string[];
  bundle_ref?: string;
  filename?: string;
  download_url?: string;
};

export type PrototypePortableReportVersion = {
  run_id: string;
  report_id: string;
  version: Record<string, unknown>;
  content?: string | null;
  portable: true;
  activation_posture: "DISABLED_PENDING_LOCAL_RECONFIRMATION";
  warnings: string[];
};

export type PrototypePortableEvidence = {
  run_id: string;
  evidence: Record<string, unknown>;
  rights: Array<Record<string, unknown>>;
  portable: true;
  full_text_embedded: false;
  activation_posture: "DISABLED_PENDING_LOCAL_RECONFIRMATION";
};

export type PrototypeBundleProjectDetail = PrototypeBundleProject & {
  topics: PrototypeWorkspaceTopic[];
  portable_records: {
    runs: Array<Record<string, unknown>>;
    rights: Array<Record<string, unknown>>;
    full_text_artifact_descriptors: Array<Record<string, unknown>>;
    reviews: Array<Record<string, unknown>>;
    evidence: Array<Record<string, unknown>>;
    claims: Array<Record<string, unknown>>;
    reports: Array<Record<string, unknown>>;
    report_versions: Array<Record<string, unknown>>;
    provider_execution_summaries: Array<Record<string, unknown>>;
  };
};

export type PrototypeWorkspace = {
  schema_version: string;
  enabled: true;
  external_network_default: "disabled";
  provider_calls: 0;
  topics: PrototypeWorkspaceTopic[];
  totals: {
    active_topics: number;
    archived_topics: number;
    total_included_papers: number;
    real_full_text_papers: number;
    synthetic_papers: number;
    total_reviews: number;
    total_evidence_spans: number;
    total_reports: number;
    topics_needing_attention: number;
    living_topics: number;
    schedules_enabled: number;
    topics_due: number;
    unread_briefings: number;
    review_required: number;
    critical_alerts: number;
    last_successful_refresh?: string | null;
    last_briefing?: string | null;
    research_gap_analyses: number;
    research_gaps: number;
    research_recommendations: number;
    accepted_recommendations: number;
    promoted_recommendations: number;
  };
  recent_lineage: PrototypeWorkspaceRun[];
  bundle_projects?: PrototypeBundleProject[];
  bundle_posture?: {
    schema_version: "xrml_research_project_bundle_v1";
    automatic_execution_default: "disabled";
    provider_default: "disabled";
    network_default: "disabled";
    full_text_embedded: false;
  };
};

export type PrototypeWorkspaceSnapshotComparison = {
  topic_id: string;
  from_snapshot: PrototypeWorkspaceSnapshot;
  to_snapshot: PrototypeWorkspaceSnapshot;
  added_papers: string[];
  removed_papers: string[];
  held_papers: string[];
  unchanged_papers: string[];
  changed_source_content_digests: string[];
  review_count_delta: number;
  evidence_count_delta: number;
  report_availability: { from: boolean; to: boolean };
};

export type PrototypeRealCorpusArticle = {
  pmcid: string;
  pmid?: string | null;
  doi?: string | null;
  title: string;
  authors: string[];
  journal: string;
  publication_date: string;
  license_type: "CC0" | "CC BY";
  license_url: string;
  rights_accepted: boolean;
  full_text_retrieved: boolean;
  content_digest: string;
  article_identity_digest: string;
  usable_section_count: number;
  usable_paragraph_count: number;
  review_method: string;
  review_id: string;
  evidence_ids: string[];
  retrieval_result: string;
};

export type PrototypeRealCorpusClaim = {
  claim_id: string;
  claim_type: "grounded" | "inference" | "proposal" | "uncertainty";
  claim_category: string;
  claim_text: string;
  evidence_ids: string[];
  paper_ids: string[];
  source_coverage: string;
  confidence_strength: string;
};

export type PrototypeRealCorpusEvidence = {
  evidence_id: string;
  run_id: string;
  paper_id: string;
  full_text_artifact_id: string;
  source_content_digest: string;
  section_id: string;
  paragraph_id: string;
  section_path: string[];
  paragraph_ordinal: number;
  normalized_quote: string;
  claim_category: string;
  stale: boolean;
};

export type PrototypeRealCorpusRecord = {
  schema_version: string;
  run_id: string;
  topic: string;
  source: string;
  status: "complete" | "incomplete";
  failure_category?: string | null;
  query: {
    query_id: string;
    query_sha256: string;
    date_cutoff: string;
    result_count?: number;
    selected_article_ids?: string[];
    selection_algorithm_version: string;
  };
  discovery_result_count: number;
  discovered_candidate_count: number;
  license_eligible_count: number;
  selected_article_count: number;
  articles: PrototypeRealCorpusArticle[];
  rejected_candidates: Array<{ pmcid?: string | null; reason: string }>;
  evidence: PrototypeRealCorpusEvidence[];
  evidence_category_counts?: Record<string, number>;
  limitation_uncertainty_evidence_present?: boolean;
  invalid_or_stale_evidence_count?: number;
  review_count?: number;
  usable_section_count?: number;
  usable_paragraph_count?: number;
  claims: PrototypeRealCorpusClaim[];
  report?: {
    report_id: string;
    report_version_id: string;
    generation_mode: string;
    report_digest: string;
    grounded_claim_count: number;
    inference_count: number;
    proposal_count: number;
    uncertainty_count: number;
    unsupported_grounded_claims: number;
    unknown_evidence_ids: number;
    cross_run_evidence_ids: number;
    abstract_only_body_claims: number;
    license_rejected_article_references: number;
    stale_evidence_references: number;
    claim_to_evidence_coverage: number;
    license_appendix: boolean;
  } | null;
  network: {
    request_count: number;
    total_downloaded_bytes: number;
    retries: number;
    non_allowlisted_requests: number;
  };
  provider_calls: number;
  replay?: PrototypeRealCorpusReplay | null;
  record_digest: string;
};

export type PrototypeRealCorpusEvidenceDetail = {
  run_id: string;
  evidence_id: string;
  paper_id: string;
  full_text_artifact_id: string;
  source_content_digest: string;
  section_id: string;
  section_path: string[];
  paragraph_id: string;
  paragraph_ordinal: number;
  bounded_source_excerpt: string;
  claim_category: string;
  stale: boolean;
  source_kind: "real_pmc_open_access_full_text";
  paper: {
    title: string;
    pmcid: string;
    pmid?: string | null;
    doi?: string | null;
    license_type: string;
    license_url: string;
    content_digest: string;
    review_method: string;
  };
};

export type PrototypeRealCorpusReplay = {
  schema_version: string;
  run_id: string;
  status: "passed";
  network_enabled: false;
  network_request_count: 0;
  selected_article_ids: string[];
  evidence_identity_count: number;
  report_digest: string;
  allowed_metadata_difference: string;
  replayed_at: string;
  record_digest: string;
};

export type PrototypeProviderRoute = {
  provider_id: "codex";
  execution_surface: "codex_sdk_python";
  stage: "report_draft_assist";
  internal_output_class: "report_draft";
  product_output_class: "weekly_report";
};

export type PrototypeLiveAvailability = PrototypeProviderRoute & {
  schema_version: string;
  default_state: "disabled";
  availability_flag: boolean;
  endpoint_available: boolean;
  adapter_available: boolean;
  authenticated_execution_readiness: "ready" | "unavailable" | "unknown";
  deterministic_fallback_available: boolean;
  selected_provider_id?: "deterministic_local" | "codex" | string;
  provider_available?: boolean;
  provider_authenticated?: boolean;
  provider_activated?: boolean;
  official_cli_installed?: boolean;
  official_cli_version?: string | null;
  session_reason_code?: string | null;
  last_probe_at?: string | null;
  reasons: string[];
};

export type ReportDraftAssistProviderCard = {
  provider_id: "deterministic_local" | "codex" | string;
  display_name: string;
  surface: "local" | "cli" | "sdk" | string;
  installed: boolean;
  available: boolean;
  authenticated: boolean;
  activation_proof_state: string;
  supported_stages: string[];
  supported_output_classes: string[];
  version?: string | null;
  last_probe_at?: string | null;
  reason_code: string;
  official_login_command?: string | null;
  official_status_command?: string | null;
  official_auth_surface_available?: boolean;
  deterministic_fallback_available: boolean;
};

export type ReportDraftAssistStatus = {
  schema_version: string;
  stage: "report_draft_assist";
  selected_provider_id: "deterministic_local" | "codex" | string;
  activation_state: "inactive" | "eligible" | "activated" | string;
  activation_proof_state: string;
  last_probe_at?: string | null;
  deterministic_fallback_available: boolean;
  providers: ReportDraftAssistProviderCard[];
};

export type PrototypeLiveExecution = PrototypeProviderRoute & {
  schema_version: string;
  execution_id: string;
  run_id: string;
  state: "PENDING" | "VALIDATED_PENDING_USER_ACCEPTANCE" | "ACCEPTED" | "REJECTED" | "FAILED" | string;
  invocation_attempted: boolean;
  logical_invocation_count: number;
  provider_call_count: number;
  start_timestamp?: string | null;
  completion_timestamp?: string | null;
  timeout_seconds?: number | null;
  timed_out: boolean;
  duration_ms?: number | null;
  model_backend_identifier?: string | null;
  result_category?: string | null;
  schema_valid: boolean;
  route_valid: boolean;
  evidence_valid: boolean;
  content_safety_valid: boolean;
  unknown_evidence_count: number;
  grounded_claim_count: number;
  unsupported_content_label_count: number;
  referenced_evidence_count: number;
  candidate_available: boolean;
  accepted: boolean;
  rejected: boolean;
  acceptance_state?: "not_attempted" | "pending" | "accepted" | "rejected";
  rejection_reasons: string[];
  fallback_used: boolean;
  fallback_reason?: string | null;
  reason_category?: string | null;
  accepted_output_digest?: string | null;
  resulting_report_version_id?: string | null;
  baseline_version_id?: string | null;
  baseline_report_unchanged?: boolean | null;
  trusted_persistence_count: number;
  exit_code?: number | null;
  safe_diagnostics?: {
    source?: string | null;
    exit_code?: number | null;
    timed_out?: boolean | null;
    launcher_kind?: string | null;
    diagnostic_summary?: string | null;
    schema_profile_version?: string | null;
    usage_limit?: {
      source?: string | null;
      exit_code?: number | null;
      timed_out?: boolean | null;
      reset_time_text?: string | null;
      reset_timezone_claimed?: boolean | null;
    } | null;
  } | null;
};

export type PrototypeReportVersionSummary = {
  version_id: string;
  version_number: number;
  generation_mode: string;
  display_label: string;
  content_sha256?: string | null;
  provider_id?: string | null;
  execution_record_id?: string | null;
};

export type PrototypeLiveProofStatus = PrototypeLiveAvailability & {
  run_id: string;
  one_live_call_budget: "unused" | "consumed";
  history: PrototypeLiveExecution[];
  latest_execution?: PrototypeLiveExecution | null;
  report_versions: PrototypeReportVersionSummary[];
};

export type PrototypeActivationResponse = {
  schema_version: string;
  run_id: string;
  activation_token: string;
  token_hint: string;
  expires_at: string;
  request_digest: string;
  route: PrototypeProviderRoute;
  baseline_version_id: string;
  one_live_call_budget: "unused";
  explicit_confirmation_required: true;
};

export type PrototypeCandidatePreview = {
  run_id: string;
  execution_id: string;
  state: string;
  markdown: string;
  grounded_claim_count: number;
  referenced_evidence_count: number;
  accepted: boolean;
};

export type RunEvent = {
  run_id: string;
  type: string;
  timestamp: string;
  payload: Record<string, unknown>;
};

export type AuthUser = {
  user_id: string;
  display_name: string;
  email?: string | null;
  auth_source: "disabled" | "oidc";
};

export type AuthSessionResponse = {
  auth_mode: "disabled" | "oidc";
  authenticated: boolean;
  is_admin: boolean;
  user?: AuthUser | null;
  login_url?: string | null;
  logout_url?: string | null;
  mcp_url?: string | null;
  mcp_resource_metadata_url?: string | null;
  product_auth_server_side_only: boolean;
  provider_auth_server_side_only: boolean;
  product_session_backend?: "local_internal" | "server_side_file";
  session_configuration_status?: "not_required" | "valid" | "invalid";
  session_configuration_reason_code?:
    | "session_secret_missing"
    | "session_secret_too_short"
    | "session_secret_disallowed"
    | null;
  warnings: string[];
};

export type AuthOperatorRole = "viewer" | "operator" | "local_operator" | "admin";

export type AuthOperatorCapability =
  | "view_dashboard"
  | "edit_topic_config"
  | "preview_run_plan"
  | "prepare_provider_manual_run"
  | "run_fixture_provider"
  | "run_real_provider_cli"
  | "accept_provider_risk"
  | "save_provider_config"
  | "view_provider_readiness";

export type OperatorAuditSummary = {
  totalEvents: number;
  allowedCount: number;
  deniedCount: number;
  secretPolicyRejectionCount: number;
  providerRiskAcceptanceCount: number;
  configWriteCount: number;
  latestEventSummaries: Array<{
    auditEventId: string;
    actionType: string;
    decision: string;
    targetType?: string | null;
    targetId?: string | null;
    riskLevel: string;
    reasonCodes: string[];
  }>;
};

export type OperatorAuditHealth = {
  auditLogStatus: "unavailable" | "empty" | "valid" | "warnings" | "errors";
  validEventCount: number;
  invalidEventCount: number;
  warningCount: number;
  secretLikeViolationCount: number;
  missingRequiredFieldCount: number;
  inconsistentCapabilityCount: number;
  latestCheckedEventId?: string | null;
  reasonCounts: Record<string, number>;
};

export type OAuthLiveExchangeReadiness = {
  readinessId: string;
  providerId: string;
  readinessStatus: "ready_for_future_live_exchange" | "blocked" | "warnings_only" | "disabled";
  liveExchangeEnabled: boolean;
  tokenStorageEnabled: boolean;
  externalAuthCallPerformed: boolean;
  checks: Array<{
    checkId: string;
    label: string;
    status: "passed" | "failed" | "warning" | "disabled";
    reasonCodes: string[];
    warnings: string[];
  }>;
  failedChecks: string[];
  warningChecks: string[];
  reasonCodes: string[];
  safeNextSteps: string[];
  forbiddenActions: string[];
};

export type OAuthReadinessRemediation = {
  providerId: string;
  readinessStatus: "ready_for_future_live_exchange" | "blocked" | "warnings_only" | "disabled";
  remediationStatus: "clear" | "warnings" | "blocking";
  blockingRemediationCount: number;
  warningRemediationCount: number;
  infoRemediationCount: number;
  liveExchangeEnabled: boolean;
  tokenStorageEnabled: boolean;
  items: Array<{
    remediationId: string;
    providerId: string;
    readinessCheck: string;
    reasonCode: string;
    severity: "info" | "warning" | "blocking";
    category:
      | "metadata"
      | "env_var"
      | "redirect_uri"
      | "state_store"
      | "audit_health"
      | "account_link_health"
      | "session_policy"
      | "dashboard_safety"
      | "future_live_exchange";
    operatorAction: string;
    safeCommandOrConfigHint?: string | null;
    envVarNames: string[];
    docsAnchor: string;
    forbiddenActions: string[];
    canBeResolvedWithoutCodeChange: boolean;
    requiresRestart: boolean;
    secretHandlingNote: string;
  }>;
  safeNextSteps: string[];
  forbiddenActions: string[];
};

export type OAuthPreflightPackage = {
  preflightId: string;
  providerId: string;
  generatedAt: string;
  readinessStatus: "ready_for_future_live_exchange" | "blocked" | "warnings_only" | "disabled";
  remediationSummary: OAuthReadinessRemediation;
  auditHealthStatus: "unavailable" | "empty" | "valid" | "warnings" | "errors";
  accountLinkHealthStatus: "empty" | "valid" | "warnings" | "errors";
  tokenStorageEnabled: boolean;
  liveExchangeEnabled: boolean;
  envVarNames: string[];
  envVarValuesExposed: boolean;
  checks?: Array<{
    checkId: string;
    label: string;
    status: "passed" | "warning" | "blocking" | "info";
    reasonCodes: string[];
    warnings: string[];
  }>;
  summary?: {
    providerId: string;
    preflightStatus: "ready" | "blocked" | "warnings" | "disabled";
    blockingCheckCount: number;
    warningCheckCount: number;
    requiredConfirmationCount: number;
    acceptanceReady: boolean;
    tokenStorageEnabled: boolean;
    liveExchangeEnabled: boolean;
  };
  forbiddenActions: string[];
  safeNextSteps: string[];
  requiredOperatorConfirmations: string[];
  acceptanceReady: boolean;
  reasonCodes: string[];
  warnings: string[];
};

export type OAuthLiveExchangePilotStatus = {
  pilotStatus: "disabled_by_default" | "available_explicit_api_only";
  liveExchangeEnabledByDefault: boolean;
  tokenStorageEnabled: boolean;
  dashboardExecutionEnabled: boolean;
  tokenStorageStillDisabled?: boolean;
  dashboardExecutionStillDisabled?: boolean;
  explicitOperatorAcceptanceRequired: boolean;
  tokenValuesReturned: boolean;
  tokenValuesAudited: boolean;
  livePilotLeakCheckStatus?: "not_run" | "clean" | "redacted" | "blocked";
  latestRedactionSummary?: {
    redactedFields: string[];
    redactedTokenFieldCount: number;
    leakDetected?: boolean;
    leakCount?: number;
    reasonCodes?: string[];
    warningCount?: number;
    authorizationCodeRedacted: boolean;
    rawTokenStored: boolean;
    tokenValuesReturned: boolean;
    tokenValuesAudited: boolean;
  };
  lastPilotStatus?: string | null;
  reasonCodes: string[];
  warnings: string[];
};

export type OAuthLivePilotReplayVerification = {
  verificationId: string;
  providerId: string;
  verificationStatus: "valid" | "warnings" | "failed";
  failedChecks: string[];
  warningChecks: string[];
  leakCount: number;
  secretLikeViolationCount: number;
  missingRequiredEvidenceCount: number;
  reasonCodes: string[];
  redactedPaths: string[];
  warnings: string[];
};

export type OAuthLivePilotEvidencePackage = {
  packageId: string;
  providerId: string;
  generatedAt: string;
  readinessStatus: "ready_for_future_live_exchange" | "blocked" | "warnings_only" | "disabled";
  remediationSummary: OAuthReadinessRemediation;
  preflightSummary: {
    providerId: string;
    preflightStatus: "ready" | "blocked" | "warnings" | "disabled";
    blockingCheckCount: number;
    warningCheckCount: number;
    requiredConfirmationCount: number;
    acceptanceReady: boolean;
    tokenStorageEnabled: boolean;
    liveExchangeEnabled: boolean;
  };
  checklistSummary: OAuthOperatorAcceptanceChecklist;
  auditHealthStatus: "unavailable" | "empty" | "valid" | "warnings" | "errors";
  accountLinkHealthStatus: "empty" | "valid" | "warnings" | "errors";
  leakCheckStatus: "clean" | "warnings" | "failed";
  gateStatus: "passed" | "blocked" | "dry_run_only" | "failed";
  livePilotEnabledDefault: boolean;
  dashboardExecutionEnabled: boolean;
  tokenStorageEnabled: boolean;
  tokenValuesPresent: boolean;
  secretValuesPresent: boolean;
  externalCallPerformed: boolean;
  envVarNames: string[];
  envVarValuesExposed: boolean;
  evidenceItems: Array<{
    itemId: string;
    category: string;
    label: string;
    status: "present" | "warning" | "missing" | "failed";
    summary: string;
    reasonCodes: string[];
    warnings: string[];
  }>;
  replayVerification: OAuthLivePilotReplayVerification;
  summary?: {
    providerId: string;
    readinessStatus: "ready_for_future_live_exchange" | "blocked" | "warnings_only" | "disabled";
    preflightStatus: "ready" | "blocked" | "warnings" | "disabled";
    checklistValid: boolean;
    auditHealthStatus: "unavailable" | "empty" | "valid" | "warnings" | "errors";
    accountLinkHealthStatus: "empty" | "valid" | "warnings" | "errors";
    leakCheckStatus: "clean" | "warnings" | "failed";
    gateStatus: "passed" | "blocked" | "dry_run_only" | "failed";
    verificationStatus: "valid" | "warnings" | "failed";
    tokenStorageEnabled: boolean;
    dashboardExecutionEnabled: boolean;
    livePilotEnabledDefault: boolean;
  };
  forbiddenActions: string[];
  safeNextSteps: string[];
  reasonCodes: string[];
  warnings: string[];
};

export type OAuthRealProviderSmokeStatus = {
  smokeStatus: "not_configured" | "skipped" | "ready_for_operator_api" | "completed_redacted" | "blocked" | "failed" | "dry_run_only";
  liveSmokeEnabledByDefault: boolean;
  tokenStorageEnabled: boolean;
  dashboardExecutionEnabled: boolean;
  externalAuthCallPerformed: boolean;
  latestSmokeEvidence: "none" | "redacted";
  reasonCodes: string[];
  warnings: string[];
};

export type OAuthSmokeResultSummary = {
  providerId: string;
  latestSmokeStatus: "no_result" | "skipped_not_configured" | "blocked" | "completed_mocked" | "completed_real_redacted" | "failed_redacted";
  latestResultId?: string | null;
  replayVerificationStatus: "valid" | "warnings" | "failed";
  accountLinkCreated: boolean;
  metadataOnlyLink: boolean;
  tokenStorageEnabled: boolean;
  dashboardExecutionEnabled: boolean;
  reasonCodes: string[];
  warnings: string[];
};

export type ProviderOrchestrationAvailability = "available" | "preview_only" | "cli_only" | "blocked" | "future_disabled";
export type ProviderOrchestrationWorkflow =
  | "review_with_stub_provider"
  | "review_with_fixture_command"
  | "manual_provider_package_import"
  | "command_provider_dry_run"
  | "command_provider_live_cli_preview"
  | "real_provider_review_future_disabled";

export type ProviderOrchestrationPlannedMode =
  | "read_only"
  | "dry_run"
  | "fixture_only"
  | "manual_import"
  | "cli_only"
  | "blocked"
  | "future_disabled";

export type ProviderOrchestrationAction = {
  actionId: string;
  actionLabel: string;
  providerType: string;
  availability: ProviderOrchestrationAvailability;
  requiredCapabilities: string[];
  requiredOperatorAcceptance: boolean;
  requiresM4Validation: boolean;
  requiresPreflight: boolean;
  requiresEvidenceReplay: boolean;
  dashboardAllowed: boolean;
  externalCallPossible: boolean;
  tokenStorageAllowed: boolean;
  defaultEnabled: boolean;
  reasonCodes: string[];
  safeNextSteps: string[];
  warnings: string[];
};

export type ProviderOrchestrationReplayResult = {
  replayId: string;
  replayStatus: "valid" | "warnings" | "failed";
  failedChecks: string[];
  warningChecks: string[];
  forbiddenActionCount: number;
  missingGateCount: number;
  reasonCodes: string[];
  redactedPaths: string[];
  warnings: string[];
};

export type ProviderOrchestrationExecutionReplay = {
  replayId: string;
  replayStatus: "valid" | "warnings" | "failed";
  failedChecks: string[];
  warningChecks: string[];
  unsafeExecutionCount: number;
  missingValidationCount: number;
  secretLikeViolationCount: number;
  reasonCodes: string[];
  redactedPaths: string[];
  warnings: string[];
};

export type ProviderOrchestrationPlanStep = {
  stepId: string;
  actionId: string;
  actionLabel: string;
  providerType: string;
  plannedMode: ProviderOrchestrationPlannedMode;
  requiredCapabilities: string[];
  requiredAcceptance: boolean;
  requiresM4Validation: boolean;
  requiresOAuthPreflight: boolean;
  requiresEvidenceReplay: boolean;
  dashboardExecutionAllowed: boolean;
  externalCallPlanned: boolean;
  tokenStorageAllowed: boolean;
  expectedArtifacts: string[];
  reasonCodes: string[];
  safeNextSteps: string[];
  warnings: string[];
};

export type ProviderOrchestrationPlanSummary = {
  planId: string;
  workflowType: ProviderOrchestrationWorkflow;
  topicId?: string | null;
  providerConfigId?: string | null;
  stepCount: number;
  blockedStepCount: number;
  cliOnlyStepCount: number;
  previewOnlyStepCount: number;
  externalCallPlanned: boolean;
  dashboardExecutionPlanned: boolean;
  tokenStoragePlanned: boolean;
  replayStatus: "valid" | "warnings" | "failed";
  forbiddenActionCount: number;
  missingGateCount: number;
  safeNextSteps: string[];
  reasonCodes: string[];
  warnings: string[];
};

export type ProviderOrchestrationDryRunPlan = ProviderOrchestrationPlanSummary & {
  generatedAt: string;
  steps: ProviderOrchestrationPlanStep[];
  replaySummary: ProviderOrchestrationReplayResult;
  summary: ProviderOrchestrationPlanSummary;
};

export type ProviderOrchestrationExecutionSummary = {
  executionId: string;
  planId: string;
  workflowType: ProviderOrchestrationWorkflow;
  fixtureOnly: boolean;
  dryRun: boolean;
  externalCallPerformed: boolean;
  dashboardTriggered: boolean;
  tokenStorageUsed: boolean;
  stepsExecuted: number;
  stepsSkipped: number;
  acceptedDraftCount: number;
  needsReviewCount: number;
  rejectedCount: number;
  skippedExistingAcceptedCount: number;
  m4ValidationApplied: boolean;
  resultReplayStatus: "valid" | "warnings" | "failed";
  unsafeExecutionCount: number;
  missingValidationCount: number;
  safeNextSteps: string[];
  reasonCodes: string[];
  warnings: string[];
};

export type ProviderOrchestrationExecutionResult = ProviderOrchestrationExecutionSummary & {
  generatedAt: string;
  executedSteps: Array<{
    stepId: string;
    actionId: string;
    actionLabel: string;
    providerType: string;
    executionStatus: "executed_fixture" | "preview_only" | "skipped" | "blocked" | "rejected";
    fixtureOnly: boolean;
    externalCallPerformed: boolean;
    dashboardTriggered: boolean;
    tokenStorageUsed: boolean;
    m4ValidationApplied: boolean;
    acceptedDraftCount: number;
    needsReviewCount: number;
    rejectedCount: number;
    skippedExistingAcceptedCount: number;
    reasonCodes: string[];
    warnings: string[];
  }>;
  replaySummary: ProviderOrchestrationExecutionReplay;
};

export type ProviderOrchestrationSafetyMatrix = {
  matrixId: string;
  generatedAt: string;
  matrixStatus: "valid" | "warnings" | "blocked";
  actionCount: number;
  availableCount: number;
  previewOnlyCount: number;
  cliOnlyCount: number;
  blockedCount: number;
  futureDisabledCount: number;
  dashboardExecutionAllowed: boolean;
  externalCallPossibleCount: number;
  tokenStorageAllowed: boolean;
  accountLinkInfluence: "display_only" | "none" | "future_policy_placeholder";
  m4ValidationRequired: boolean;
  providerAcceptanceRequired: boolean;
  effectiveCapabilities: string[];
  actions: ProviderOrchestrationAction[];
  actionGateSummaries?: Array<{
    actionId: string;
    availability: ProviderOrchestrationAvailability;
    gateStatus: ProviderOrchestrationAvailability;
    requiredCapabilities: string[];
    missingCapabilities: string[];
    requiredOperatorAcceptance: boolean;
    requiresM4Validation: boolean;
    dashboardAllowed: boolean;
    reasonCodes: string[];
  }>;
  summary: {
    matrixStatus: "valid" | "warnings" | "blocked";
    availableCount: number;
    previewOnlyCount: number;
    cliOnlyCount: number;
    blockedCount: number;
    futureDisabledCount: number;
    dashboardExecutionAllowed: boolean;
    tokenStorageAllowed: boolean;
    safeNextSteps: string[];
    reasonCodes: string[];
    warnings?: string[];
  };
  reasonCodes: string[];
  warnings: string[];
};

export type OAuthOperatorAcceptanceChecklistRequest = {
  providerId?: string;
  confirmedNoSecretCommit?: boolean;
  confirmedEnvVarsConfigured?: boolean;
  confirmedRedirectUriRegistered?: boolean;
  confirmedAuditHealthReviewed?: boolean;
  confirmedTokenStoragePolicy?: boolean;
  confirmedDashboardExecutionDisabled?: boolean;
  confirmedProviderAcceptanceGateIndependent?: boolean;
  acceptedByOperatorId?: string | null;
  acceptedAt?: string | null;
};

export type OAuthOperatorAcceptanceChecklist = Required<
  Pick<
    OAuthOperatorAcceptanceChecklistRequest,
    | "providerId"
    | "confirmedNoSecretCommit"
    | "confirmedEnvVarsConfigured"
    | "confirmedRedirectUriRegistered"
    | "confirmedAuditHealthReviewed"
    | "confirmedTokenStoragePolicy"
    | "confirmedDashboardExecutionDisabled"
    | "confirmedProviderAcceptanceGateIndependent"
  >
> & {
  acceptedByOperatorId?: string | null;
  acceptedAt?: string | null;
  acceptanceValid: boolean;
  missingConfirmations: string[];
  liveExchangeStillDisabled: boolean;
  liveExchangeEnabled: boolean;
  tokenStorageEnabled: boolean;
  reasonCodes: string[];
  warnings: string[];
};

export type AuthFoundationStatus = {
  authMode: "disabled" | "local_operator" | "oauth_ready";
  legacyAuthMode: "disabled" | "oidc";
  oauthReady: boolean;
  tokenStorageEnabled: boolean;
  providerLiveExecution: "cli_only_acceptance_gated";
  operator: {
    operatorId: string;
    displayName: string;
    role: AuthOperatorRole;
    capabilities: AuthOperatorCapability[];
  };
  oauthProviders: Array<{
    providerId: string;
    displayName: string;
    issuer?: string | null;
    authorizationUrl?: string | null;
    tokenUrl?: string | null;
    scopes: string[];
    clientIdEnvVar?: string | null;
    clientSecretEnvVar?: string | null;
    enabled: boolean;
    notes: string[];
    tokenStorageEnabled: boolean;
    liveTokenExchangeEnabled: boolean;
  }>;
  oauthDryRun: {
    oauthMode: "metadata_only" | "dry_run_ready";
    tokenExchangeEnabled: boolean;
    tokenPersistenceEnabled: boolean;
    externalAuthCallsEnabled: boolean;
    stateStoreAvailable: boolean;
    pendingStateCount: number;
    expiredStateCount: number;
    usedStateCount: number;
    malformedStateCount: number;
    stateStorage: "local_ignored";
    warnings: string[];
  };
  oauthTokenExchange: {
    exchangeMode: "mock_only" | "disabled" | "live_not_supported";
    mockExchangeSupported: boolean;
    liveExchangeSupported: boolean;
    tokenStorageEnabled: boolean;
    accountLinkPreviewEnabled: boolean;
    externalAuthCallsEnabled: boolean;
    reasonCodes: string[];
    warnings: string[];
  };
  accountLinkStatus: OAuthAccountLinkHealth;
  sessionBinding: OAuthSessionBinding;
  sessionCapabilityPolicy: OAuthSessionCapabilityPolicy;
  oauthFlowTimeline: OAuthFlowTimeline;
  oauthLiveExchangeReadiness: OAuthLiveExchangeReadiness;
  oauthReadinessRemediation?: OAuthReadinessRemediation;
  oauthPreflightPackage?: OAuthPreflightPackage;
  oauthLiveExchangePilot?: OAuthLiveExchangePilotStatus;
  oauthLivePilotEvidencePackage?: OAuthLivePilotEvidencePackage;
  oauthRealProviderSmoke?: OAuthRealProviderSmokeStatus;
  oauthSmokeResultSummary?: OAuthSmokeResultSummary;
  providerOrchestrationSafetyMatrix?: ProviderOrchestrationSafetyMatrix;
  providerOrchestrationPlanSummary?: ProviderOrchestrationPlanSummary;
  providerOrchestrationExecutionSummary?: ProviderOrchestrationExecutionSummary;
  operatorAuditSummary: OperatorAuditSummary;
  operatorAuditHealth: OperatorAuditHealth;
  reasonCodes: string[];
  warnings: string[];
};

export type OAuthAccountLinkHealth = {
  status: "none" | "mock_linked" | "live_pilot_linked" | "disabled" | "invalid";
  linkedAccountCount: number;
  activeLinkCount: number;
  mockLinkCount: number;
  liveLinkCount: number;
  invalidLinkCount: number;
  revokedPlaceholderCount: number;
  tokenLeakViolationCount: number;
  liveExchangeViolationCount: number;
  duplicateLinkCount: number;
  healthStatus: "empty" | "valid" | "warnings" | "errors";
  tokenStorageEnabled: boolean;
  latestLinkedAt?: string | null;
  reasonCodes: string[];
  warnings: string[];
};

export type OAuthSessionBinding = {
  bindingStatus: "unbound" | "local_mock_bound" | "disabled";
  operatorId: string;
  role: AuthOperatorRole;
  providerIds: string[];
  accountLinkCount: number;
  capabilities: AuthOperatorCapability[];
  accountLinkGrantsProviderExecution: boolean;
  providerExecution: "cli_only_acceptance_gated";
  tokenStorageEnabled: boolean;
  liveTokenExchangePerformed: boolean;
  reasonCodes: string[];
  warnings: string[];
};

export type OAuthSessionCapabilityPolicy = {
  effectiveCapabilities: AuthOperatorCapability[];
  capabilitySources: Record<string, string[]>;
  deniedByPolicy: string[];
  accountLinkInfluence: "display_only" | "none" | "future_policy_placeholder";
  accountLinkCount: number;
  accountLinkGrantsProviderExecution: boolean;
  providerExecution: "cli_only_acceptance_gated";
  tokenStorageEnabled: boolean;
  liveTokenExchangePerformed: boolean;
  reasonCodes: string[];
  warnings: string[];
};

export type OAuthFlowTimeline = {
  status: "empty" | "metadata_only" | "mock_linked" | "disabled";
  tokenStorageEnabled: boolean;
  realTokenExchangeEnabled: boolean;
  externalAuthCallsEnabled: boolean;
  steps: Array<{
    stepId: string;
    label: string;
    status: "available" | "disabled" | "metadata_only" | "mock_only";
    tokenStorageEnabled?: boolean;
    liveTokenExchangeEnabled?: boolean;
    externalAuthCallPerformed?: boolean;
    reasonCodes: string[];
    warnings?: string[];
  }>;
  reasonCodes: string[];
  warnings: string[];
};

export type OAuthAccountLinkMetadata = {
  accountLinkId: string;
  providerId: string;
  operatorId: string;
  subjectHash?: string | null;
  displaySubjectHint?: string | null;
  scopes: string[];
  linkedAt?: string | null;
  lastValidatedAt?: string | null;
  tokenStorageEnabled: boolean;
  liveTokenExchangePerformed: boolean;
  source: "mock_exchange_validation" | "dry_run_preview" | "future_live_exchange";
  status: "preview" | "linked_mock" | "disabled" | "invalid" | "revoked_placeholder";
  reasonCodes: string[];
  warnings: string[];
};

export type OAuthAccountLinkOperationResponse = {
  accepted: boolean;
  validationStatus: "preview" | "saved" | "invalid";
  accountLink?: OAuthAccountLinkMetadata | null;
  health: OAuthAccountLinkHealth;
  sessionBinding: OAuthSessionBinding;
  linkStored: boolean;
  tokenStorageEnabled: boolean;
  liveTokenExchangePerformed: boolean;
  externalAuthCallPerformed: boolean;
  tokenRevocationPerformed: boolean;
  reasonCodes: string[];
  warnings: string[];
};

export type OAuthAccountLinkListResponse = {
  items: OAuthAccountLinkMetadata[];
  health: OAuthAccountLinkHealth;
};

export type OAuthAuthorizationDryRunRequest = {
  oauthProviderMetadata: {
    providerId?: string | null;
    displayName?: string | null;
    issuer?: string | null;
    authorizationUrl?: string | null;
    tokenUrl?: string | null;
    scopes?: string[] | string | null;
    clientIdEnvVar?: string | null;
    clientSecretEnvVar?: string | null;
    enabled?: boolean | null;
    notes?: string[] | string | null;
  };
  redirectUri: string;
  ttlSeconds?: number;
};

export type OAuthLiveExchangeReadinessRequest = {
  oauthProviderMetadata: OAuthAuthorizationDryRunRequest["oauthProviderMetadata"];
  redirectUri?: string | null;
};

export type OAuthAuthorizationDryRunResponse = {
  providerId: string;
  validationStatus: "preview" | "prepared" | "invalid";
  authorizationUrl?: string | null;
  redirectUri: string;
  scopes: string[];
  state: string;
  nonce: string;
  pkce: {
    codeChallenge: string;
    codeChallengeMethod: "S256";
  };
  createdAt: string;
  expiresAt: string;
  correlationId: string;
  stateStored: boolean;
  clientIdStatus: "resolved" | "missing_env";
  tokenExchangeEnabled: boolean;
  tokenPersistenceEnabled: boolean;
  externalAuthCallPerformed: boolean;
  reasonCodes: string[];
  warnings: string[];
  errors: string[];
};

export type OAuthCallbackDryRunRequest = {
  providerId: string;
  state?: string | null;
  code?: string | null;
  error?: string | null;
  redirectUri?: string | null;
};

export type OAuthCallbackDryRunResponse = {
  providerId: string;
  acceptedForTokenExchange: boolean;
  tokenExchangePerformed: boolean;
  stateStatus: "valid" | "missing" | "expired" | "replayed" | "unknown";
  correlationId?: string | null;
  secretPresent: boolean;
  externalAuthCallPerformed: boolean;
  reasonCodes: string[];
  warnings: string[];
};

export type OAuthTokenExchangeRequest = {
  providerId: string;
  correlationId?: string | null;
  authorizationCodePresent: boolean;
  stateStatus: "valid" | "missing" | "expired" | "replayed" | "unknown";
  redirectUri?: string | null;
  codeVerifierPresent: boolean;
  exchangeMode: "mock_only" | "disabled" | "live_not_supported";
  mockTokenResponse?: Record<string, unknown> | null;
};

export type OAuthTokenExchangeResponse = {
  exchangePerformed: boolean;
  tokenExchangePerformed: boolean;
  exchangeMode: "mock_only" | "disabled" | "live_not_supported";
  providerId: string;
  correlationId?: string | null;
  accepted: boolean;
  tokenResponsePresent: boolean;
  accessTokenPresent: boolean;
  refreshTokenPresent: boolean;
  idTokenPresent: boolean;
  expiresIn?: number | null;
  scopes: string[];
  subjectHash?: string | null;
  rawTokenStored: boolean;
  validationSummary: {
    valid: boolean;
    tokenResponsePresent: boolean;
    accessTokenPresent: boolean;
    refreshTokenPresent: boolean;
    idTokenPresent: boolean;
    expiresIn?: number | null;
    scopes: string[];
    subjectHash?: string | null;
    reasonCodes: string[];
  };
  accountLinkPreview: {
    providerId: string;
    subjectHash?: string | null;
    scopes: string[];
    linkedAt?: string | null;
    tokenStorageEnabled: boolean;
    linkStored: boolean;
  };
  redactionSummary: {
    redactedFields: string[];
    redactedTokenFieldCount: number;
    authorizationCodeRedacted: boolean;
    rawTokenStored: boolean;
  };
  reasonCodes: string[];
  warnings: string[];
};

export type SourceProviderId = "arxiv" | "openalex" | "crossref";
export type RegistryProviderId = SourceProviderId | "crossref" | string;
export type SourceProviderState = "available" | "unavailable_missing_env" | "unavailable_not_implemented" | "degraded";

export type TopicSourceProviderConfig = {
  provider: RegistryProviderId;
  enabled: boolean;
  priority: number;
  queries: string[];
  categories?: string[];
  maxResults: number;
  sortBy: "submittedDate" | "lastUpdatedDate" | "relevance" | "publication_date" | "cited_by_count" | "published" | "deposited" | "is-referenced-by-count";
  sortOrder: "ascending" | "descending";
};

export type TopicSourceSettings = {
  defaultProvider: SourceProviderId;
  enabledProviders: SourceProviderId[];
  providers: {
    arxiv: TopicSourceProviderConfig;
    openalex?: TopicSourceProviderConfig;
    crossref?: TopicSourceProviderConfig;
    [provider: string]: TopicSourceProviderConfig | undefined;
  };
};

export type TopicSourceStatus = {
  provider: RegistryProviderId;
  label: string;
  displayName?: string | null;
  description?: string | null;
  category?: string | null;
  capabilities?: Record<string, boolean>;
  supported: boolean;
  available: boolean;
  configured?: boolean;
  state?: SourceProviderState;
  enabled: boolean;
  default: boolean;
  priority: number;
  requiresCredentials: boolean;
  statusLabel: string;
  unavailableReason?: string | null;
  supportedQueryFields?: string[];
  lastCheckedAt?: string | null;
  lastSuccessfulCollectionAt?: string | null;
};

export type ApiSourceContribution = {
  provider: RegistryProviderId;
  label: string;
  paperCount: number;
  lastSuccessfulCollectionAt?: string | null;
};

export type ApiSourceSummary = {
  defaultProvider: SourceProviderId;
  enabledProviders: SourceProviderId[];
  statuses: TopicSourceStatus[];
  recentContributions: ApiSourceContribution[];
};

export type SourceProviderRegistryItem = {
  provider: RegistryProviderId;
  label: string;
  displayName: string;
  description: string;
  category: string;
  capabilities: Record<string, boolean>;
  requiresCredentials: boolean;
  configured: boolean;
  supported: boolean;
  available: boolean;
  state: SourceProviderState;
  statusLabel: string;
  unavailableReason?: string | null;
  supportedQueryFields: string[];
  lastCheckedAt?: string | null;
};

export type SourceProviderRegistryResponse = {
  checkedAt: string;
  items: SourceProviderRegistryItem[];
};

export type TopicProfile = {
  profile_id: string;
  title: string;
  description?: string | null;
  topic: string;
  queries: string[];
  categories: string[];
  max_results: number;
  source_provider: SourceProviderId;
  arxiv_sort_by: "submittedDate" | "lastUpdatedDate" | "relevance";
  arxiv_sort_order: "ascending" | "descending";
  source_settings: TopicSourceSettings;
  source_statuses: TopicSourceStatus[];
  top_papers: number;
  weekly_top: number;
  is_favorite: boolean;
  status: "active" | "archived";
  archived_at?: string | null;
  default_collection_cadence: string;
  default_report_cadence: string;
  acquisition_language: string;
  review_report_language: string;
  acquisition_start_date?: string | null;
  acquisition_end_date?: string | null;
  created_at: string;
  updated_at: string;
};

export type TopicProfilesResponse = {
  items: TopicProfile[];
};

export type DashboardCount = {
  runs: number;
  topics: number;
  papers: number;
  evidence: number;
  report_sections: number;
};

export type DashboardRunSummary = {
  run_id: string;
  topic: string;
  status: string;
  created_at: string;
  updated_at: string;
  triage_items: number;
  reviewed_items: number;
  evidence_count: number;
  claim_count: number;
};

export type DashboardPaperSummary = {
  run_id: string;
  paper_id: string;
  title: string;
  theme: string;
  score: number;
  status: string;
  published?: string | null;
};

export type DashboardOverviewResponse = {
  totals: DashboardCount;
  status_counts: Array<{ status: string; count: number }>;
  top_topics: Array<{ topic: string; count: number }>;
  top_themes: Array<{ theme: string; count: number }>;
  recent_runs: DashboardRunSummary[];
  index_state: Record<string, string>;
};

export type DashboardMetricPoint = {
  label: string;
  count: number;
};

export type DashboardTrendPoint = DashboardMetricPoint;

export type DashboardKeyword = {
  keyword: string;
  count: number;
};

export type DashboardInstitution = {
  name: string;
  count: number;
};

export type DashboardReportSummary = {
  run_id: string;
  title: string;
  path?: string | null;
  created_at: string;
  status: string;
};

export type TopicDashboardResponse = {
  selected_profile_id?: string | null;
  topic?: TopicProfile | null;
  totals: DashboardCount;
  recent_runs: DashboardRunSummary[];
  latest_papers: DashboardPaperSummary[];
  hot_keywords: DashboardKeyword[];
  trend: DashboardTrendPoint[];
  distributions: {
    themes: DashboardMetricPoint[];
    statuses: DashboardMetricPoint[];
  };
  institution_rankings: DashboardInstitution[];
  recent_reports: DashboardReportSummary[];
  warnings: string[];
};

export type ApiDashboardTopic = {
  id: string;
  name: string;
  description?: string | null;
  updatedAt: string;
  isFavorite: boolean;
  status: "active" | "archived";
};

export type ApiTopicsResponse = {
  items: ApiDashboardTopic[];
};

export type ApiTopicsOverviewSummary = {
  topicCount: number;
  collectedPapers: number;
  fullTextReviewedCount: number;
};

export type ApiTopicOverviewItem = {
  id: string;
  name: string;
  description?: string | null;
  updatedAt: string;
  isFavorite: boolean;
  collectedPapers: number;
  collectedDelta: number;
  collectedSparkline: number[];
  corePapers: number;
  coveragePercentage: number;
  fullTextReviewedCount: number;
  totalPapers: number;
  trendLabel: string;
  trendDeltaPercent: number;
  degraded: boolean;
};

export type ApiTopicsOverviewResponse = {
  summary: ApiTopicsOverviewSummary;
  items: ApiTopicOverviewItem[];
};

export type ApiTopicRecommendation = {
  id: string;
  versionId?: string|null;
  type: "new_topic" | "gap";
  title: string;
  question: string;
  whyNow: string;
  methodDirection: string;
  expectedContribution: string;
  gapStatement: string;
  noveltyRationale: string;
  confidence: string;
  supportingPaperCount: number;
  suggestedQueries: string[];
  actionable: boolean;
  supportingPaperIds?: string[];
  evidenceRefs?: Array<{paper_id:string;source_text_sha256?:string|null;evidence_span_id?:string|null;review_level:string;provider_origin?:string|null;quoted_or_summarized_basis:string}>;
  coverageWarning?: string|null;
  riskNotes?: string[];
  requiredFollowUpEvidence?: string[];
  problemStatement?: string;
  differentiationClaim?: string;
  nearestExistingWork?: string[];
  limitationRefs?: ApiTopicRecommendation["evidenceRefs"];
  sourceBasis?: string;
};

export type ApiTopicRecommendationsResponse = {
  topicId: string;
  items: ApiTopicRecommendation[];
  newTopicCount: number;
  gapCount: number;
  coverageWarning: string | null;
};

export type ApiMetricSeries = {
  value: number;
  delta: number;
  sparkline: number[];
};

export type ApiTrendIndexMetric = {
  label: string;
  deltaPercent: number;
  sparkline: number[];
};

export type ApiDashboardMetrics = {
  collectedPapers: ApiMetricSeries;
  corePapers: ApiMetricSeries;
  averageImpact: ApiMetricSeries;
  totalCitations: ApiMetricSeries;
  trendIndex: ApiTrendIndexMetric;
};

export type ApiDashboardTrends = {
  labels: string[];
  totalPapers: number[];
  corePapers: number[];
  citations: number[];
};

export type ApiHotKeyword = {
  rank: number;
  keyword: string;
  score: number;
  paperCount?: number;
  sourceRunIds?: string[];
  explanation?: string | null;
};

export type ApiScheduledTaskType =
  | "collect_papers"
  | "collect_analyze"
  | "weekly_report"
  | "monthly_report"
  | "citation_alert"
  | string;

export type ApiScheduledTask = {
  id: string;
  title: string;
  scheduleLabel: string;
  enabled: boolean;
  taskType: ApiScheduledTaskType;
  lastRunAt?: string | null;
  nextRunAt?: string | null;
  schedulerState: string;
  currentRunStatus?: string | null;
  lastStatus?: string | null;
  lastError?: string | null;
};

export type ApiLatestCorePaper = {
  id: string;
  title: string;
  authors: string[];
  publishedAt?: string | null;
  citations: number;
  impactScore: number;
  sourceUrl?: string | null;
  sourceProvider?: string | null;
  runId?: string | null;
  rationale?: string | null;
  saved: boolean;
};

export type ApiTopicPaper = {
  id: string;
  runId: string;
  title: string;
  authors: string[];
  publishedAt?: string | null;
  citations: number;
  impactScore: number;
  sourceUrl?: string | null;
  sourceProvider?: string | null;
  saved: boolean;
  theme?: string | null;
  status?: string | null;
};

export type ApiRuntimeProvenance = {
  surfaceId: string;
  label: string;
  mode: "persisted_artifact" | "runtime_adapter";
  definition: string;
  computedAt: string;
  sourceRunIds: string[];
  warnings?: string[];
};

export type ApiTopicPapersResponse = {
  topic?: ApiDashboardTopic | null;
  items: ApiTopicPaper[];
  total: number;
  q: string;
  providerFilter?: string | null;
  providerCounts: ApiSourceContribution[];
  provenance?: ApiRuntimeProvenance | null;
  warnings: string[];
};

export type ApiFieldDistribution = {
  label: string;
  count: number;
  percentage: number;
  sampleTitles?: string[];
  samplePaperIds?: string[];
  sourceRunIds?: string[];
  emptyReason?: string | null;
};

export type ApiInstitutionDistribution = {
  name: string;
  count: number;
  canonicalName?: string | null;
  rawVariants?: string[];
  sampleTitles?: string[];
  samplePaperIds?: string[];
  sourceRunIds?: string[];
  emptyReason?: string | null;
};

export type ApiRecentReport = {
  id: string;
  runId?: string | null;
  title: string;
  createdAt: string;
  updatedAt?: string | null;
  status: "queued" | "running" | "completed" | "failed" | "artifact_unavailable";
  downloadUrl?: string | null;
  error?: string | null;
};

export type ApiReviewCoverage = {
  totalPapers: number;
  reviewedCount: number;
  abstractOnlyCount: number;
  fullTextReviewedCount: number;
  coveragePercentage: number;
  rawIndexedPaperCount: number;
  uniqueCandidateCount: number;
  topicRelevantUniqueCount: number;
  reviewedUniqueCount: number;
  abstractOnlyUniqueCount: number;
  fullTextAvailableUniqueCount: number;
  fullTextReviewedUniqueCount: number;
};

export type ApiTopicConfiguration = {
  topicId: string;
  displayName: string;
  queryTerms: string[];
  includeKeywords: string[];
  excludeKeywords: string[];
  minRelevance: number;
  defaultUniqueLimit: number;
  defaultAllowDomains: string[];
  defaultDownloadLimit: number;
  defaultMaxPdfBytes: number;
  recommendationLimit: number;
  followupLimit: number;
  providerModeDefault: "none" | "stub" | "command_dry_run";
  liveActionsEnabled: boolean;
  notes: string[];
  acquisitionLanguage: string;
  reviewReportLanguage: string;
  acquisitionStartDate: string | null;
  acquisitionEndDate: string | null;
  validationWarnings: string[];
};

export type ApiOperationsRunPlan = {
  planId: string;
  topicId: string;
  description: string;
  riskLevel: "read_only" | "dry_run" | "write_requires_cli";
  copyableCliCommand: string;
  requiredSafetyFlags: string[];
  writesFiles: boolean;
  callsNetwork: boolean;
  callsProvider: boolean;
  requiresExplicitOperatorAction: boolean;
  previewOnly: boolean;
  serverExecutesCommand: boolean;
  warnings: string[];
};

export type ApiProviderConfiguration = {
  providerId: string;
  displayName: string;
  providerType: "stub" | "command";
  profileRole: "stub" | "generic_command" | "fixture" | "real_example";
  executionSurface: "sdk" | "cli" | "api" | "mcp";
  authMode:
    | "api_key"
    | "google_adc"
    | "cli_oauth"
    | "chatgpt_signin"
    | "claude_code_login"
    | "external_cli_auth"
    | "oidc"
    | "none"
    | "unsupported"
    | "unknown";
  credentialPolicy: Array<
    "external_only" | "env_presence_only" | "operator_profile" | "no_token_storage" | "no_cache_inspection"
  >;
  status:
    | "configured_not_verified"
    | "provider_smoke_only"
    | "non_live_available"
    | "unsupported"
    | "policy_sensitive"
    | "live_verified_tiny"
    | "live_verified";
  proofArtifactRequired: boolean;
  allowedCommands: string[][];
  forbiddenCredentialPaths: string[];
  statusAfterProofCandidate: "live_verified_tiny" | "live_verified";
  researchOutputAllowed: boolean;
  credentialBoundaryDescription: string;
  operatorProfileSecretFree: boolean;
  enabled: boolean;
  defaultMode: "stub" | "command_dry_run";
  commandArgvTemplate: string[];
  workingDirectory?: string | null;
  timeoutSeconds: number;
  maxOutputChars: number;
  maxPacketChars: number;
  allowedTopicIds: string[];
  allowedReviewLevels: Array<"abstract_only" | "full_text_grounded">;
  allowLiveCommand: boolean;
  requiresOperatorConfirmation: boolean;
  requiresOperatorAcceptance: boolean;
  notes: string[];
  validationWarnings: string[];
};

export type ApiProviderCommandPreview = {
  argvRedacted: string[];
  executableLabel: string;
  argsRedacted: boolean;
  profilePathRedacted: boolean;
  credentialValuesPrinted: boolean;
  liveExecutionAllowed: boolean;
};

export type ApiProviderAuthBridgeIssue = {
  code: string;
  message: string;
  field: string;
};

export type ApiProviderAuthBridge = {
  providerId: string;
  executionSurface: string;
  authMode: string;
  credentialPolicy: string[];
  currentStatus: string;
  readinessLabel: string;
  authBoundary: string;
  commandPreview: ApiProviderCommandPreview;
  redactionApplied: boolean;
  proofArtifactRequired: boolean;
  proofArtifactPresent: boolean;
  researchOutputAllowed: boolean;
  outputAllowed: boolean;
  blockedReason: string;
  nextRequiredGate: string;
  issues: ApiProviderAuthBridgeIssue[];
};

export type ApiProviderReadiness = {
  providerId: string;
  displayName: string;
  providerType: "stub" | "command";
  profileRole: "stub" | "generic_command" | "fixture" | "real_example";
  enabled: boolean;
  readinessStatus:
    | "ready"
    | "ready_stub"
    | "ready_fixture"
    | "not_configured"
    | "disabled"
    | "command_missing"
    | "unsafe_config"
    | "dry_run_only"
    | "example_profile_configured"
    | "operator_acceptance_required"
    | "live_disabled";
  profileId?: string;
  providerAlias?: string;
  executionSurface?: ApiProviderConfiguration["executionSurface"];
  authMode?: ApiProviderConfiguration["authMode"] | string;
  credentialPolicy?: string[];
  status?: ApiProviderConfiguration["status"] | string;
  proofArtifactRequired?: boolean;
  allowedCommands?: string[][];
  forbiddenCredentialPaths?: string[];
  statusAfterProofCandidate?: "live_verified_tiny" | "live_verified" | string;
  credentialBoundaryDescription?: string;
  canonicalStatus?:
    | "live_verified_tiny"
    | "live_verified"
    | "configured_not_verified"
    | "non_live_available"
    | "unsupported"
    | "policy_sensitive"
    | "dry_run_verified"
    | "configured_but_not_available"
    | "environment_blocked_binary_missing"
    | "environment_blocked_live_gate_missing"
    | "environment_blocked_command_unknown"
    | "unsafe_to_probe"
    | "mock_only"
    | "docs_only"
    | "not_supported";
  proofLevel?: string;
  binaryStatus?: "found" | "missing" | "not_applicable" | "unknown";
  versionStatus?: string;
  dryRunStatus?: string;
  liveProofStatus?: string;
  liveGateStatus?: string;
  routingLabel?:
    | "provider_smoke_only"
    | "not_allowed_for_research_output"
    | "setup_required"
    | "operator_approval_required"
    | "live_proof_required"
    | "research_output_blocked";
  researchOutputAllowed?: boolean;
  reportGenerationAllowed?: boolean;
  orchestrationAllowed?: boolean;
  setupRequired?: boolean;
  operatorActionRequired?: boolean;
  statusMessage?: string;
  evidence?: string[];
  reasonCodes: string[];
  safeModesAvailable: string[];
  liveAllowed: boolean;
  operatorAcceptanceRequired: boolean;
  commandPreviewRedacted: string[];
  authBridge?: ApiProviderAuthBridge | null;
  warnings: string[];
  configuration: ApiProviderConfiguration;
};

export type ApiProviderReadinessList = {
  generatedAt: string;
  items: ApiProviderReadiness[];
  warnings: string[];
};

export type ApiOperationsPlanner = {
  topicConfiguration: ApiTopicConfiguration;
  runPlans: ApiOperationsRunPlan[];
  providerReadiness: ApiProviderReadiness[];
  warnings: string[];
};

export type TopicConfigurationDraft = Partial<Omit<ApiTopicConfiguration, "validationWarnings">>;

export type ApiTopicConfigurationValidation = {
  topicId: string;
  validationStatus: "valid" | "invalid" | "saved";
  topicConfiguration: ApiTopicConfiguration;
  changedFields: string[];
  diff: Array<{ field: string; currentValue?: unknown; draftValue?: unknown }>;
  warnings: string[];
  errors: string[];
  configHash: string;
  savedPath?: string | null;
  operationsPlanner: ApiOperationsPlanner;
};

export type ApiResearchOperations = {
  coverage: {
    fullTextReviewedUniqueCount: number;
    topicRelevantUniqueCount: number;
    fullTextCoverageRatio: number;
    aiGroundedAcceptedCount: number;
    aiGroundedCoverageRatio: number;
    providerGeneratedAcceptedCount: number;
    providerReviewCoverage?: {
      acceptedAiGroundedDraftCount: number;
      acceptedByOrigin: Record<string, number>;
      needsReviewByOrigin: Record<string, number>;
      rejectedByOrigin: Record<string, number>;
      fixtureSyntheticCount: number;
      realProviderAcceptedCount: number;
      providerOrchestratedAcceptedCount: number;
      providerOrchestratedSyntheticCount: number;
      fullTextReviewedDenominator: number;
      coverageWarning: string;
      reasonCodes: string[];
      warnings: string[];
      provenanceAudit?: {
        auditStatus: string;
        checkedCount: number;
        mismatchCount: number;
        syntheticCount: number;
        realProviderCount: number;
        unknownOriginCount: number;
        rejectedOrNeedsReviewLeakCount: number;
        missingM4ValidationCount: number;
        reasonCodes: string[];
        warnings: string[];
      };
    };
  };
  recommendations: {
    recommendationGapCandidateCount: number;
    recommendationTopicCandidateCount: number;
    recommendationWeakCount: number;
    recommendationNeedsFollowupCount: number;
    recommendationCoverageWarningCount: number;
    preliminary: boolean;
  };
  followup: {
    followupPlannedCount: number;
    followupExecutedCount: number;
    followupReviewedCount: number;
    skippedExistingReviewedCount: number;
    skippedNoPdfUrlCount: number;
    skippedBlockedCount: number;
  };
  provider: {
    generatedCount: number;
    acceptedCount: number;
    needsReviewCount: number;
    rejectedCount: number;
    failedCount: number;
    latestProviderOrigin?: string | null;
  };
  rcAudit?: {
    auditStatus: string;
    checkCount: number;
    failedCheckCount: number;
    warningCheckCount: number;
    reasonCodes: string[];
    surfaceSummaries: Array<{
      surfaceId: string;
      status: string;
      summary: string;
      reasonCodes: string[];
    }>;
    artifactBoundary: {
      boundaryStatus: string;
      knownRuntimeUntrackedCount: number;
      unexpectedRuntimeTrackedCount: number;
      runtimeArtifactWarningCount: number;
      knownRuntimeArtifacts: string[];
      unexpectedTrackedPaths: string[];
      reasonCodes: string[];
      warnings: string[];
    };
    checks: Array<{
      checkId: string;
      category: string;
      status: string;
      label: string;
      summary: string;
      reasonCodes: string[];
    }>;
    safeNextSteps: string[];
  };
  releaseFreeze?: {
    releaseStatus: string;
    rcAuditStatus: string;
    validationMatrixSummary: Record<string, unknown>;
    coverageSummary: {
      fullTextReviewedUniqueCount?: number;
      aiGroundedAcceptedCount?: number;
      providerOrchestratedAcceptedCount?: number;
    };
    providerProvenanceSummary: {
      auditStatus?: string;
      syntheticCount?: number;
      realProviderCount?: number;
      mismatchCount?: number;
    };
    recommendationSummary: {
      preliminary?: boolean;
      weakCount?: number;
      needsFollowupCount?: number;
    };
    oauthProviderSafetySummary: {
      tokenStorageEnabled?: boolean;
      liveOAuthEnabled?: boolean;
      dashboardExecutionEnabled?: boolean;
      providerExecutionEnabled?: boolean;
    };
    rcAuditSummary: {
      checkCount?: number;
      failedCheckCount?: number;
      warningCheckCount?: number;
    };
    artifactBoundarySummary: {
      boundaryStatus?: string;
      acceptedRuntimeWarningCount?: number;
      blockingReleaseWarningCount?: number;
      knownRuntimeUntrackedCount?: number;
    };
    knownWarnings: Array<{
      path: string;
      classification: string;
      artifactPolicy: string;
      severity: string;
      shouldRemainUntracked: boolean;
      reasonCodes: string[];
    }>;
    knownWarningCount: number;
    blockingIssueCount: number;
    reasonCodes: string[];
    safeNextSteps: string[];
  };
  auth: AuthFoundationStatus;
  latestRecommendationAuditSummary?: Record<string, unknown> | null;
  latestRecommendationFollowupPlanSummary?: Record<string, unknown> | null;
  latestFollowupExecutionSummary?: Record<string, unknown> | null;
  latestProviderRunSummary?: Record<string, unknown> | null;
  latestReportPath?: string | null;
  guidance: {
    recommendationDryRunCommand: string;
    recommendationAuditCommand: string;
    followupPlanCommand: string;
    followupExecutionDryRunCommand: string;
  };
  operationsPlanner: ApiOperationsPlanner;
  warnings: string[];
};

export type ApiReportHubItem = {
  id: string;
  runId: string;
  topicId?: string | null;
  topicName: string;
  title: string;
  createdAt: string;
  updatedAt?: string | null;
  status: "queued" | "running" | "completed" | "failed" | "artifact_unavailable";
  downloadUrl?: string | null;
  error?: string | null;
  sourceProviders: string[];
  completedVersionCount: number;
  latestCompletedVersionId?: string | null;
  latestCompletedVersionAt?: string | null;
  latestCompletedVersionHash?: string | null;
  exportArtifacts?: ExportArtifact[];
  exportArtifactSummary?: {
    total: number;
    downloadable: number;
    unavailable: number;
    formats: string[];
  } | null;
};

export type ApiReportsResponse = {
  items: ApiReportHubItem[];
  total: number;
  provenance?: ApiRuntimeProvenance | null;
  warnings: string[];
};

export type ApiAnalyticsSourceRun = {
  runId: string;
  status: string;
  createdAt: string;
  finishedAt?: string | null;
};

export type ApiAnalyticsCoverage = {
  coverageId: string;
  label: string;
  covered: number;
  total: number;
  percentage: number;
  emptyReason?: string | null;
};

export type ApiAnalyticsMetricDefinition = {
  metricId: string;
  label: string;
  definition: string;
};

export type ApiAnalyticsFreshness = {
  status: string;
  label: string;
  computedAt: string;
  lastAnalysisAt?: string | null;
  newestSourceRunAt?: string | null;
  sourceRunCount: number;
  sourcePaperCount: number;
  staleReason?: string | null;
};

export type ApiAnalyticsDerivation = {
  surfaceId: string;
  label: string;
  mode: "persisted_artifact" | "runtime_adapter";
  definition: string;
  computedAt: string;
  sourceRunIds: string[];
};

export type ApiAnalyticsTrendPoint = {
  label: string;
  totalPapers: number;
  corePapers: number;
  citations: number;
  averageImpact: number;
  sourceRunIds?: string[];
  bucketMode?: string;
  emptyReason?: string | null;
};

export type ApiAnalyticsTrendDetail = {
  bucketMode: string;
  summary: string;
  points: ApiAnalyticsTrendPoint[];
};

export type ApiAnalyticsKeywordDetail = {
  keyword: string;
  score: number;
  paperCount: number;
  sampleTitles: string[];
  samplePaperIds?: string[];
  sourceRunIds?: string[];
  rawVariants?: string[];
  explanation: string;
};

export type ApiAnalyticsFieldDetail = {
  label: string;
  count: number;
  percentage: number;
  sampleTitles: string[];
  samplePaperIds?: string[];
  sourceRunIds?: string[];
  emptyReason?: string | null;
};

export type ApiAnalyticsInstitutionDetail = {
  name: string;
  count: number;
  canonicalName?: string | null;
  rawVariants: string[];
  sampleTitles: string[];
  samplePaperIds?: string[];
  sourceRunIds?: string[];
  emptyReason?: string | null;
};

export type ApiAnalyticsCorePaperDetail = {
  paperId: string;
  title: string;
  runId?: string | null;
  publishedAt?: string | null;
  citations: number;
  impactScore: number;
  rationale: string;
  sourceUrl?: string | null;
};

export type ApiTopicAnalytics = {
  profileId: string;
  artifactVersion?: string;
  computedAt?: string;
  generatedAt: string;
  lastAnalysisAt?: string | null;
  freshness?: ApiAnalyticsFreshness | null;
  sourceRuns: ApiAnalyticsSourceRun[];
  coverage: ApiAnalyticsCoverage[];
  metricDefinitions: ApiAnalyticsMetricDefinition[];
  derivations?: ApiAnalyticsDerivation[];
  trendDetail: ApiAnalyticsTrendDetail;
  keywordDetails: ApiAnalyticsKeywordDetail[];
  fieldDetails: ApiAnalyticsFieldDetail[];
  institutionDetails: ApiAnalyticsInstitutionDetail[];
  corePaperDetails: ApiAnalyticsCorePaperDetail[];
};

export type TopicNotificationChannel = {
  channel_id: string;
  label: string;
  description: string;
  available: boolean;
  enabled: boolean;
  state:
    | "available"
    | "unavailable_missing_env"
    | "misconfigured"
    | "failed_delivery"
    | "connection_failed"
    | "delivery_failed"
    | "disabled";
  readiness_state?: "available" | "unavailable_missing_env" | "invalid_config" | "connection_failed" | "delivery_failed" | string;
  status_label: string;
  destination: string;
  destination_label: string;
  unavailable_reason?: string | null;
  supports_test: boolean;
};

export type TopicNotificationRule = {
  rule_id: string;
  label: string;
  description: string;
  enabled: boolean;
};

export type TopicNotificationSettings = {
  profile_id: string;
  topic_id: string;
  topic_name: string;
  in_app_enabled: boolean;
  channels: TopicNotificationChannel[];
  rules: TopicNotificationRule[];
  created_at: string;
  updated_at: string;
};

export type TopicNotificationSettingsUpdateRequest = {
  in_app_enabled?: boolean;
  channels?: Array<{
    channel_id: "email" | "webhook";
    enabled: boolean;
    destination: string;
  }>;
  rules?: Array<{
    rule_id: string;
    enabled: boolean;
  }>;
};

export type TopicNotificationItem = {
  notification_id: string;
  profile_id: string;
  topic_name: string;
  rule_id: string;
  event_type: string;
  severity: "info" | "success" | "warning" | "error";
  title: string;
  message: string;
  created_at: string;
  run_id?: string | null;
  schedule_id?: string | null;
  download_url?: string | null;
};

export type TopicNotificationsResponse = {
  profile_id: string;
  topic_name: string;
  items: TopicNotificationItem[];
  total: number;
};

export type TopicNotificationDelivery = {
  delivery_id: string;
  notification_id?: string | null;
  profile_id: string;
  topic_name: string;
  channel_id: string;
  rule_id: string;
  event_type: string;
  state:
    | "available"
    | "unavailable_missing_env"
    | "misconfigured"
    | "failed_delivery"
    | "connection_failed"
    | "delivery_failed"
    | "disabled";
  status: "sent" | "failed" | "disabled";
  destination_label: string;
  status_code?: number | null;
  error_code?: string | null;
  message: string;
  attempts: number;
  attempted?: boolean;
  created_at: string;
  updated_at: string;
};

export type TopicNotificationDeliveriesResponse = {
  profile_id: string;
  topic_name: string;
  items: TopicNotificationDelivery[];
  total: number;
};

export type TopicNotificationTestDeliveryResponse = {
  profile_id: string;
  channel_id: "email" | "webhook";
  deliveries: TopicNotificationDelivery[];
};

export type ApiTopicDashboardResponse = {
  topic?: ApiDashboardTopic | null;
  sourceSummary?: ApiSourceSummary | null;
  metrics: ApiDashboardMetrics;
  trends: ApiDashboardTrends;
  hotKeywords: ApiHotKeyword[];
  scheduledTasks: ApiScheduledTask[];
  scheduleStatus?: {
    state: "ready" | "defaults" | "unavailable";
    warnings: string[];
  } | null;
  latestCorePapers: ApiLatestCorePaper[];
  fieldDistribution: ApiFieldDistribution[];
  institutionDistribution: ApiInstitutionDistribution[];
  recentReports: ApiRecentReport[];
  reviewCoverage: ApiReviewCoverage;
  researchOperations: ApiResearchOperations;
  analytics?: ApiTopicAnalytics | null;
  warnings: string[];
};

export type TopicSchedulePipelineStep = {
  step_id: string;
  label: string;
  description: string;
};

export type TopicSchedule = {
  schedule_id: string;
  profile_id: string;
  title: string;
  topic: string;
  task_type: ApiScheduledTaskType;
  kind: string;
  enabled: boolean;
  cadence: string;
  cadence_label: string;
  timezone: string;
  pipeline: TopicSchedulePipelineStep[];
  last_run_id?: string | null;
  last_status?: string | null;
  last_run_at?: string | null;
  last_success_at?: string | null;
  last_failure_at?: string | null;
  last_failure_message?: string | null;
  last_scheduled_at?: string | null;
  next_run_at?: string | null;
  next_run_hint?: string | null;
  current_run_id?: string | null;
  current_run_status?: string | null;
  scheduler_state: string;
  last_error?: string | null;
  last_trigger_source?: string | null;
  last_worker_id?: string | null;
  dispatch_attempts: number;
  lock_owner?: string | null;
  lock_acquired_at?: string | null;
  lock_expires_at?: string | null;
  last_recovery_at?: string | null;
  last_recovery_message?: string | null;
  created_at: string;
  updated_at: string;
};

export type TopicSchedulerStatus = {
  mode: string;
  configured_mode?: string | null;
  configured_mode_raw?: string | null;
  configured_mode_valid?: boolean;
  enabled: boolean;
  running: boolean;
  status: string;
  runtime_mode?: "single_node_file_backed" | "db_backed" | "queue_backed" | "unavailable" | string;
  runtime_mode_status?: "healthy" | "degraded" | "unavailable" | string;
  runtime_mode_label?: string | null;
  storage_backend?: string | null;
  configured_runtime_mode?: string | null;
  configured_runtime_mode_raw?: string | null;
  configured_runtime_mode_source?: string | null;
  configured_runtime_mode_valid?: boolean;
  backend?: {
    mode?: string;
    configured_backend?: string;
    configured_runtime_mode?: string | null;
    configured_runtime_mode_raw?: string | null;
    configured_runtime_mode_source?: string | null;
    configured_runtime_mode_valid?: boolean;
    available?: boolean;
    read_only?: boolean;
    unavailable_reason?: string | null;
    [key: string]: unknown;
  };
  distributed_ready?: boolean;
  distributed_requested?: boolean;
  distributed_support_status?: "gated" | "unsupported" | "supported" | string;
  queue_backend_status?: "available" | "unavailable" | "not_selected" | string;
  queue_item_counts?: {
    queued?: number;
    leased?: number;
    expired?: number;
    completed?: number;
    failed?: number;
    total?: number;
  };
  multi_node_dispatch_safe?: boolean;
  architecture_note?: string | null;
  dispatch_safety?: {
    dispatch_leases?: boolean;
    duplicate_execution_protection?: boolean;
    orphan_recovery?: boolean;
    durable_queue?: boolean;
    multi_node_dispatch_safe?: boolean;
    multi_process_dispatch_safe?: boolean;
  };
  requires_external_worker?: boolean;
  operator_message?: string | null;
  worker_id?: string | null;
  pid?: number | null;
  thread?: string | null;
  tick_seconds?: number | null;
  started_at?: string | null;
  heartbeat_at?: string | null;
  heartbeat_age_seconds?: number | null;
  stale: boolean;
  last_cycle_summary: {
    executed_schedule_ids?: string[];
    errors?: string[];
  };
  currently_leased_task_count?: number;
  due_task_count?: number;
  lease_health?: {
    active?: number;
    expired?: number;
    healthy?: boolean;
  };
  last_dispatch_at?: string | null;
  last_dispatch_schedule_id?: string | null;
  last_dispatch_worker_id?: string | null;
  last_success_at?: string | null;
  last_success_schedule_id?: string | null;
  last_failure_at?: string | null;
  last_failure_schedule_id?: string | null;
  last_failure_message?: string | null;
  orphan_recovery_count?: number;
  last_orphan_recovery_at?: string | null;
  last_orphan_recovery_message?: string | null;
  lock_timeout_seconds?: number | null;
  dispatch_lease_seconds?: number | null;
  store_lock?: {
    locked?: boolean;
    path?: string;
    pid?: number | null;
    process_id?: number | null;
    hostname?: string | null;
    same_host?: boolean | null;
    owner_running?: boolean | null;
    thread?: string | null;
    owner?: string | null;
    purpose?: string | null;
    context?: string | null;
    acquired_at?: string | null;
    created_at?: string | null;
    updated_at?: string | null;
    heartbeat_at?: string | null;
    age_seconds?: number | null;
    stale?: boolean;
    malformed?: boolean;
  };
  schedule_summary?: {
    total?: number;
    enabled?: number;
    running?: number;
    failed?: number;
    due?: number;
    active_dispatch_leases?: number;
    expired_dispatch_leases?: number;
  };
};

export type TopicSchedulesResponse = {
  items: TopicSchedule[];
  scheduler?: TopicSchedulerStatus | null;
  degraded?: boolean;
  read_only?: boolean;
  warnings?: string[];
};

export type TopicScheduleCreateRequest = {
  profile_id: string;
  title?: string | null;
  task_type: ApiScheduledTaskType;
  cadence: string;
  enabled?: boolean;
};

export type TopicAction = "collect" | "analyze" | "report" | "full_pipeline";

export type DashboardTopicActionResponse = {
  profile_id: string;
  action: TopicAction;
  run_id: string;
  topic: string;
  status: string;
  message: string;
  execution_plan: string[];
};

export type TopicScheduleRunNowResponse = {
  schedule: TopicSchedule;
  run: DashboardStartedRun;
};

export type DashboardRunSearchResponse = {
  items: DashboardRunSummary[];
};

export type DashboardPaperSearchResponse = {
  items: DashboardPaperSummary[];
};

export type DashboardStartedRun = {
  profile_id: string;
  run_id: string;
  topic: string;
  status: string;
};

export type DashboardStartRunsResponse = {
  items: DashboardStartedRun[];
};

export type SettingsProviderStatus = {
  provider?: string | null;
  provider_mode: string;
  configured: boolean;
  live_call_enabled: boolean;
  canonicalStatus?: ApiProviderReadiness["canonicalStatus"];
  routingLabel?: ApiProviderReadiness["routingLabel"];
  researchOutputAllowed?: boolean;
  reportGenerationAllowed?: boolean;
  orchestrationAllowed?: boolean;
  statusMessage?: string;
  model?: string | null;
  api_base?: string | null;
  warnings: string[];
};

export type ProviderProofGateStatus = {
  productOutputClass: "paper_review" | "weekly_report" | string;
  outputClass: "research_output" | "report_draft" | string;
  allowed: boolean;
  requiredProof: "output_class_live" | string;
  requiredProofLevel?: string | null;
  currentProof: "missing" | "present" | string;
  currentProofLevel?: string | null;
  proofMode?: string | null;
  status: "blocked" | "allowed" | string;
  blockedReason?: string | null;
  reasonCodes?: string[];
};

export type ProviderReportIntegrationStatus = {
  status?: string;
  normalizedProviderReportDraftAvailable?: boolean;
  providerReportDraftId?: string;
  topicId?: string;
  sourceProviderReviewIds?: string[];
  sourcePaperIds?: string[];
  proofArtifactRef?: string;
  reportSource?: string;
  dryRunApplyPlan?: {
    status?: string;
    applied?: boolean;
    realApplyStatus?: string;
    wouldCreateReportDraftRow?: boolean;
    wouldPersistReportArtifact?: boolean;
    wouldWriteRealDb?: boolean;
    reasonCodes?: string[];
  };
  previewContext?: {
    status?: string;
    providerReportDraftCount?: number;
    sourceProviderReviewCount?: number;
    evidenceSpanCount?: number;
    reportPersistenceStatus?: string;
    reportDraftAllowed?: boolean;
    messages?: string[];
    reasonCodes?: string[];
  };
  qualityContext?: {
    status?: string;
    evaluationMethod?: string;
    rubricVersion?: string;
    overallStatus?: string;
    h005TotalScore?: number;
    h005MaxScore?: number;
    criticalFailFlags?: string[];
    deterministicLocalRubricOnly?: boolean;
    providerCallPerformed?: boolean;
    realReportPersistencePerformed?: boolean;
  };
  realApplyStatus?: string;
  reportDraftAllowed?: boolean;
  researchOutputAllowed?: boolean;
  reportPersistenceInScope?: boolean;
  dbWriteInScope?: boolean;
  providerAuthIsAppOauthOidc?: boolean;
  localMcpIsPublicConnector?: boolean;
  productionReady?: boolean;
  externalBetaReady?: boolean;
  reasonCodes?: string[];
};

export type ProviderRoutingPromotionPolicy = {
  policyName?: string;
  policyMode?: "planning_only" | string;
  preferredMainAiProviderFamilyId?: string | null;
  preferredMainAiSurfaceId?: string | null;
  preferredForOutputClasses?: string[];
  mainAiCandidateStatus?: string;
  executionWeightsAllZero?: boolean;
  liveExecutionAllowed?: boolean;
  wouldExecuteProvider?: boolean;
  providerCallCountPlanned?: number;
  mayRunNow?: boolean;
  requiresOwnerLiveApproval?: boolean;
  providerAuthIsAppOauthOidc?: boolean;
  localMcpIsPublicConnector?: boolean;
  productionReady?: boolean;
  externalBetaReady?: boolean;
  outputClassRoutes?: Record<
    string,
    {
      outputClass?: string;
      productOutputClass?: string;
      planningAllowed?: boolean;
      selectedProviderFamilyId?: string | null;
      selectedSurfaceId?: string | null;
      planningWeight?: number;
      executionWeight?: number;
      liveExecutionAllowed?: boolean;
      wouldExecuteProvider?: boolean;
      providerCallCountPlanned?: number;
      reasonCodes?: string[];
    }
  >;
  weightedRoutingCandidates?: Array<{
    providerFamilyId?: string;
    surfaceId?: string;
    displayName?: string;
    executionSurfaceType?: string;
    adapterKind?: string;
    backendRole?: string;
    mainAiCandidate?: boolean;
    weightedRoutingCandidate?: boolean;
    preferredForPlanning?: boolean;
    preferredForOutputClasses?: string[];
    defaultBlocked?: boolean;
    mayRunNow?: boolean;
    liveExecutionAllowed?: boolean;
    wouldExecuteProvider?: boolean;
    executionWeight?: number;
    planningWeight?: number;
    reasonCodes?: string[];
  }>;
  reasonCodes?: string[];
};

export type ProviderProofStateStatus = {
  providerBackendFamilies?: Array<{
    providerFamilyId: string;
    displayName?: string;
    backendRole?: string;
    defaultBlocked?: boolean;
    outputClassCandidates?: string[];
    provenOutputClasses?: string[];
    supportsMainAiSelectionCandidate?: boolean;
    supportsWeightedRoutingCandidate?: boolean;
    reasonCodes?: string[];
  }>;
  providerExecutionSurfaces?: Array<{
    providerFamilyId: string;
    surfaceId: string;
    displayName?: string;
    executionSurfaceType?: string;
    adapterKind?: string;
    backendRole?: string;
    status?: string;
    defaultBlocked?: boolean;
    wouldExecute?: boolean;
    mayImportSdk?: boolean;
    mayRunProviderCommand?: boolean;
    mayCallNetwork?: boolean;
    reasonCodes?: string[];
  }>;
  providerTinyProofStatus?: {
    providerFamilyId?: string;
    surfaceId?: string;
    tinyProofStatus: "passed" | "missing" | "unknown" | "not_available" | string;
    artifactKind?: "provider_tiny_proof" | string;
    tinyProofKind?: string;
    proofMode?: string;
    proofStatusCandidate?: string;
    providerSdkRunCount?: number;
    providerCommandCount?: number;
    providerCallCount?: number;
    artifactRecorded?: boolean;
    artifactRef?: string | null;
    outputClassProof?: boolean;
    researchOutputAllowed?: boolean;
    reportDraftAllowed?: boolean;
    cannotUnlockOutputClass?: boolean;
    reasonCodes?: string[];
  };
  outputClassGateStatus?: {
    paper_review?: ProviderProofGateStatus;
    weekly_report?: ProviderProofGateStatus;
  };
  providerReviewIntegrationStatus?: Record<string, unknown>;
  providerReportIntegrationStatus?: ProviderReportIntegrationStatus;
  routingPromotionPolicy?: ProviderRoutingPromotionPolicy;
  appOauthOidcStatusSummary?: {
    providerAuthIsAppOauthOidc?: boolean;
    appOauthOidcInScope?: boolean;
    status?: string;
    reasonCodes?: string[];
  };
  mcpConnectorStatusSummary?: {
    localMcpIsPublicConnector?: boolean;
    publicMcpConnectorProofSatisfied?: boolean;
    status?: string;
    reasonCodes?: string[];
  };
  productionReadinessSummary?: {
    productionReady?: boolean;
    externalBetaReady?: boolean;
    status?: string;
    reasonCodes?: string[];
  };
  noSecretInspection?: boolean;
  noTokenCacheInspection?: boolean;
  noCredentialPathDisclosure?: boolean;
  reasonCodes?: string[];
};

export type SettingsOpsStatus = {
  generated_at: string;
  run_counts: Record<string, number>;
  latest_run_id?: string | null;
  latest_run_status?: string | null;
  latest_run_updated_at?: string | null;
  last_successful_run_id?: string | null;
  last_successful_run_at?: string | null;
  last_failed_run_id?: string | null;
  last_failed_run_at?: string | null;
  last_failure_message?: string | null;
  scheduler: TopicSchedulerStatus;
  notification_delivery: {
    total_recent_deliveries?: number;
    status_counts?: Record<string, number>;
    channel_counts?: Record<string, number>;
    latest_delivery_at?: string | null;
    latest_failure_at?: string | null;
    latest_failure_message?: string | null;
    smtp_state?: "available" | "unavailable_missing_env" | "misconfigured" | "failed_delivery" | "connection_failed" | "delivery_failed";
    smtp_readiness_state?:
      | "available"
      | "unavailable_missing_env"
      | "invalid_config"
      | "connection_failed"
      | "delivery_failed"
      | string;
    smtp_available?: boolean;
    smtp_configured?: boolean;
    smtp_missing_env?: string[];
    smtp_invalid_config?: string[];
    smtp_tls_mode?: "starttls" | "ssl" | "plain" | "invalid" | string;
    smtp_unavailable_reason?: string | null;
  };
};

export type SettingsMcpStatus = {
  endpoint?: string | null;
  resource_metadata_url?: string | null;
  transport: "streamable-http";
  protocol_version: string;
  auth_required: boolean;
  public_https: boolean;
  cli_ready: boolean;
  chatgpt_ready: boolean;
  readiness_label: "not_configured" | "local_cli_only" | "oidc_http_only" | "oidc_missing_audience" | "oauth_ready";
  authorization_servers: string[];
  public_tools: string[];
  protected_read_tools: string[];
  protected_write_tools: string[];
  warnings: string[];
};

export type AppOauthOidcDecisionStatus = {
  appOauthOidcDecision?: "app_oauth_oidc_deferred" | "app_oauth_oidc_disabled_local_internal" | "app_oauth_oidc_current_scope_proof_required" | string;
  appOauthOidcDecisionLabel?: "deferred" | "disabled_local_internal" | "current_scope_proof_required" | string;
  appOauthOidcProofStatus?: "not_in_scope" | "missing" | "configured_not_verified" | "proof_required" | "proven" | string;
  authMode?: "disabled" | "oidc" | string;
  currentAction?: "defer" | "plan_only" | "proof_later" | string;
  localInternalWorkbench?: boolean;
  appLoginSessionProofSatisfied?: boolean;
  authCodeExchangeProofSatisfied?: boolean;
  sessionCookieProofSatisfied?: boolean;
  providerAuthSatisfiesAppOauthOidc?: boolean;
  codexSdkProviderAuthSatisfiesAppLoginSession?: boolean;
  publicMcpConnectorProofSatisfied?: boolean;
  publicHttpsRequiredForOidc?: boolean;
  requiredFutureProof?: string[];
  requiredEnvNames?: string[];
  futureProofPrompt?: string;
  productionReady?: boolean;
  externalBetaReady?: boolean;
  noSecretInspection?: boolean;
  noTokenCacheInspection?: boolean;
  noCredentialPathDisclosure?: boolean;
  reasonCodes?: string[];
};

export type SettingsStatusResponse = {
  health_status: string;
  auth_mode: "disabled" | "oidc";
  authenticated: boolean;
  product_session_backend?: "local_internal" | "server_side_file";
  auth_warnings?: string[];
  authFoundation: AuthFoundationStatus;
  appOauthOidcDecision?: AppOauthOidcDecisionStatus;
  mcp_url?: string | null;
  provider: SettingsProviderStatus;
  providerProofState?: ProviderProofStateStatus;
  reportDraftAssist?: ReportDraftAssistStatus;
  mcp: SettingsMcpStatus;
  deployment?: {
    readiness?: "ready" | "degraded" | "not_ready";
    environment?: string;
    deployment_profile?: string;
    app_base_url?: string;
    shared_deployment?: boolean;
    auth?: {
      mode: "disabled" | "oidc";
      auth_enabled: boolean;
      shared_deployment: boolean;
      deployment_safe: boolean;
      deployment_safety_status: "ready" | "degraded" | "unsafe" | "local_internal_only" | string;
      oidc_readiness_status?:
        | "configured_not_verified"
        | "configured"
        | "missing_env"
        | "invalid_config"
        | "disabled_dev_mode"
        | "unsafe_production_disabled"
        | string;
      oidc_configured: boolean;
      oidc_missing_env?: string[];
      oidc_invalid_config?: string[];
      oidc_audience_configured?: boolean;
      oidc_scopes?: string[];
      issuer_configured: boolean;
      issuer_valid: boolean;
      client_id_configured: boolean;
      client_secret_configured: boolean;
      session_secret_configured: boolean;
      session_secret_strong: boolean;
      session_secret_validation_status?: "not_required" | "valid" | "invalid";
      session_secret_reason_code?:
        | "session_secret_missing"
        | "session_secret_too_short"
        | "session_secret_disallowed"
        | null;
      app_base_url_valid: boolean;
      app_base_url: string;
      callback_url: string;
      secure_cookies: boolean;
      cookie_samesite: string;
      mcp_enabled: boolean;
      mcp_auth_required: boolean;
      mcp_chatgpt_ready: boolean;
      mcp_scope_status?: "local_disabled" | "audience_configured" | "missing_audience" | string;
      warnings: string[];
      errors: string[];
    };
    scheduler_architecture?: string;
    scheduler_runtime_mode?: string;
    scheduler_runtime_mode_status?: string;
    scheduler_storage_backend?: string;
    scheduler_runtime_mode_valid?: boolean;
    scheduler_distributed_requested?: boolean;
    scheduler_distributed_support_status?: string;
    scheduler_architecture_note?: string;
    single_service_deployable?: boolean;
    distributed_scheduler_supported?: boolean;
    warnings?: string[];
    errors?: string[];
    storage?: {
      data_root?: string;
      runs_root?: string;
      artifact_storage_root?: string;
      storage_mode?: string;
      writable?: boolean;
      atomic_replace_supported?: boolean;
      windows_path_safe?: boolean;
      layout?: Record<string, string>;
      checks?: Record<string, unknown>;
      error?: string | null;
    };
    release_validation?: {
      available?: boolean;
      status?: string;
      created_at?: string | null;
      log_dir?: string | null;
      with_playwright?: boolean;
      playwright_screenshot_dir?: string | null;
      step_count?: number;
      failed_step_count?: number;
      latest_summary_path?: string | null;
      expected_warnings?: string[];
      error?: string | null;
    };
  };
  metadata_index_state: Record<string, string>;
  ops: SettingsOpsStatus;
  stitch_status: "not_integrated" | "seam_prepared" | "integrated";
};

export type OwnershipAuditBucketType =
  | "unowned_runs"
  | "mixed_ownership_lineage_families"
  | "owned_runs_missing_ownership_source"
  | "owned_runs_missing_owner_assigned_at"
  | "owned_runs_missing_owner_assigned_by"
  | "lineage_incomplete_families";

export type OwnershipAuditSeverity = "info" | "warn" | "critical";

export type OwnershipAuditProblemBucket = {
  bucket_id: string;
  bucket_type: OwnershipAuditBucketType;
  count: number;
  sample_run_ids: string[];
  sample_root_run_ids: string[];
  explanation: string;
  severity: OwnershipAuditSeverity;
  recommended_action: string;
};

export type OwnershipAuditLineageStateCounts = {
  fully_unowned_family_count: number;
  fully_owned_single_owner_family_count: number;
  mixed_owned_and_unowned_family_count: number;
  mixed_multiple_owner_family_count: number;
  lineage_incomplete_family_count: number;
};

export type OwnershipAuditResponse = {
  generated_at: string;
  auth_mode: "disabled" | "oidc";
  ownership_audit_version: string;
  total_runs: number;
  unowned_run_count: number;
  mixed_ownership_lineage_family_count: number;
  owned_runs_missing_ownership_source_count: number;
  owned_runs_missing_owner_assigned_at_count: number;
  owned_runs_missing_owner_assigned_by_count: number;
  lineage_incomplete_family_count: number;
  lineage_family_state_counts: OwnershipAuditLineageStateCounts;
  problem_buckets: OwnershipAuditProblemBucket[];
  warnings: string[];
  recommended_next_actions: string[];
};

export type OwnershipSource = "run_creator" | "admin_claim" | "admin_assign" | "lineage_claim";
export type LegacyRunStatusFilter = "unowned" | "all";
export type LegacyRunsMigrationMode = "claim_to_self" | "assign_to_user";
export type LegacyRunPreviewAction = "claim" | "assign" | "skip_already_owned" | "already_owned" | "unchanged" | "missing";

export type LegacyRunLineageSummary = {
  parent_run_id?: string | null;
  root_run_id: string;
  child_run_ids: string[];
  same_root_run_ids: string[];
  family_size: number;
};

export type LegacyRunSummary = {
  run_id: string;
  topic: string;
  status: string;
  created_at: string;
  updated_at: string;
  owner_user_id?: string | null;
  owner_assigned_at?: string | null;
  owner_assigned_by?: string | null;
  ownership_source?: OwnershipSource | null;
  is_unowned: boolean;
  lineage: LegacyRunLineageSummary;
};

export type LegacyRunsListResponse = {
  status: LegacyRunStatusFilter;
  q?: string | null;
  items: LegacyRunSummary[];
  total_count: number;
  unowned_count: number;
  warnings: string[];
};

export type LegacyRunsMigrationRequest = {
  run_ids: string[];
  mode: LegacyRunsMigrationMode;
  target_user_id?: string | null;
  include_lineage_family: boolean;
  skip_already_owned: boolean;
};

export type LegacyOwnershipPreview = {
  run_id: string;
  topic: string;
  status: string;
  action: LegacyRunPreviewAction;
  explicitly_requested: boolean;
  included_via_lineage: boolean;
  current_owner_user_id?: string | null;
  projected_owner_user_id?: string | null;
  current_ownership_source?: OwnershipSource | null;
  projected_ownership_source?: OwnershipSource | null;
  parent_run_id?: string | null;
  root_run_id?: string | null;
  warnings: string[];
};

export type LegacyRunsMigrationResponse = {
  requested_run_ids: string[];
  target_user_id?: string | null;
  effective_run_ids: string[];
  included_lineage_run_ids: string[];
  already_owned_run_ids: string[];
  missing_run_ids: string[];
  warnings: string[];
  conflicts: string[];
  would_change_count: number;
  changed_count: number;
  ownership_preview: LegacyOwnershipPreview[];
  lineage_warnings: string[];
};

export type ScoreBreakdown = {
  primary_hits: string[];
  secondary_hits: string[];
  negative_hits: string[];
  primary_points: number;
  secondary_points: number;
  negative_points: number;
  xr_bonus: number;
  realtime_bonus: number;
  pdf_bonus: number;
  final_score: number;
};

export type DedupCluster = {
  cluster_id: string;
  cluster_size: number;
  is_primary: boolean;
  primary_paper_id?: string | null;
  members: string[];
};

export type PaperArtifacts = {
  note_markdown?: string | null;
  review_json?: string | null;
  pdf?: string | null;
  text_txt?: string | null;
  pages_json?: string | null;
};

export type PaperReviewAction = {
  decision: "unreviewed" | "keep" | "dismiss" | "save";
  pinned: boolean;
  note: string;
  updated_at?: string | null;
};

export type PaperReviewActionPatch = Partial<Pick<PaperReviewAction, "decision" | "pinned" | "note">>;

export type PaperRecord = {
  paper_id: string;
  title: string;
  source_provider?: string | null;
  score: number;
  score_band: "high" | "medium" | "low";
  theme: string;
  published?: string | null;
  entry_url?: string | null;
  pdf_url?: string | null;
  status: string;
  abstract_excerpt: string;
  score_breakdown: ScoreBreakdown;
  dedup: DedupCluster;
  review_action: PaperReviewAction;
  triage?: Record<string, string[] | string>;
  artifacts: PaperArtifacts;
};

export type CountOption = {
  value: string;
  count: number;
};

export type PaperListResponse = {
  run_id: string;
  items: PaperRecord[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
  q: string;
  status?: string | null;
  theme?: string | null;
  score_band?: string | null;
  provider?: string | null;
  dedup: string;
  facets: {
    themes: CountOption[];
    statuses: CountOption[];
    score_bands: CountOption[];
    providers: CountOption[];
    duplicate_clusters: number;
  };
};

export type RelatedPaper = {
  paper_id: string;
  title: string;
  score: number;
  theme: string;
  relation: string;
};

export type PaperDetailResponse = {
  run_id: string;
  paper: PaperRecord;
  abstract: string;
  note_content?: string | null;
  note_path?: string | null;
  ranking_explanation: string[];
  related_papers: RelatedPaper[];
};

export type EvidenceItem = {
  evidence_id: string;
  source_id?: string | null;
  paper_id?: string | null;
  title?: string | null;
  source_type: string;
  snippet: string;
  summary?: string | null;
  location: {
    page?: number | null;
    section?: string | null;
    reference?: string | null;
  };
  quality_hint?: string | null;
  confidence?: number | null;
  artifact_path?: string | null;
  citation_text?: string | null;
};

export type ReviewResponse = {
  run_id: string;
  paper_id: string;
  status: "complete" | "partial" | "missing";
  completeness: number;
  missing_fields: string[];
  title: string;
  theme?: string | null;
  one_liner?: string | null;
  why_it_matters: string[];
  key_contributions: string[];
  xr_takeaways: string[];
  limitations: string[];
  lab_actions: string[];
  confidence?: number | null;
  method: Record<string, string | string[]>;
  experiments: Record<string, string | string[]>;
  evidence: EvidenceItem[];
  artifacts: PaperArtifacts;
  raw_path?: string | null;
};

export type OutlineSection = {
  section_id: string;
  title: string;
  kind: "markdown" | "papers";
  body_markdown: string;
  paper_ids: string[];
  compare_notes: string[];
  provenance: Array<Record<string, string | string[] | null>>;
  claims?: Array<Record<string, unknown>>;
};

export type OutlineResponse = {
  run_id: string;
  path: string;
  updated_at: string;
  sections: OutlineSection[];
  applied_actions: Array<Record<string, string | string[] | null>>;
};

export type ReportQualitySummary = {
  evaluation_status?: string | null;
  evaluation_method?: string | null;
  rubric_version?: string | null;
  report_kind?: string | null;
  evaluated_surface?: string | null;
  overall_status?: string | null;
  overall_score?: number | null;
  max_score?: number | null;
  threshold?: number | null;
  h005_total_score?: number | null;
  h005_max_score?: number | null;
  report_quality_classification?: string | null;
  critical_fail_flags?: string[] | null;
  failure_reasons?: string[] | null;
  warning_reasons?: string[] | null;
  checked_at?: string | null;
};

export type PreviewResponse = {
  run_id: string;
  path: string;
  content: string;
  generated_at: string;
  quality?: ReportQualitySummary | null;
};

export type ExportArtifact = {
  export_id: string;
  artifact_id?: string | null;
  report_id?: string | null;
  version_id?: string | null;
  topic_id?: string | null;
  run_id?: string | null;
  format: string;
  mime_type?: string | null;
  file_name?: string | null;
  output: string;
  storage_path?: string | null;
  created_at: string;
  size_bytes?: number | null;
  sha256?: string | null;
  source_markdown_version_id?: string | null;
  source_markdown_hash?: string | null;
  renderer?: string | null;
  render_options?: Record<string, unknown>;
  status: "ready" | "completed" | "failed" | "unavailable";
  error_message?: string | null;
  immutable?: boolean;
  download_available?: boolean;
  provenance_summary?: Record<string, unknown>;
};

export type ExportFormatCapability = {
  format: string;
  available: boolean;
  status: "available" | "unavailable" | "degraded";
  reason?: string | null;
  renderer?: string | null;
  template_id?: string | null;
  meaning?: string | null;
  source_pdf_handling?: string | null;
};

export type ExportListResponse = {
  run_id: string;
  items: ExportArtifact[];
  capabilities?: Record<string, ExportFormatCapability>;
};

export type ReportVersion = {
  version_id: string;
  version_number: number;
  run_id: string;
  report_id?: string | null;
  topic_id?: string | null;
  title: string;
  created_at: string;
  created_by?: string | null;
  trigger_source?: string | null;
  source: string;
  source_export_id?: string | null;
  source_artifact_path: string;
  snapshot_path: string;
  markdown_content_reference?: string | null;
  content_hash: string;
  content_sha256?: string;
  corpus_snapshot_id?: string|null;
  evidence_set_digest?: string|null;
  source_manifest_digest?: string|null;
  acceptance_record_digest?: string|null;
  byte_size: number;
  status: "ready" | "completed" | "failed" | "unavailable";
  immutable: boolean;
  artifact_references?: Record<string, string | null | undefined>;
  artifacts?: ExportArtifact[];
  artifact_availability?: {
    markdown_snapshot?: boolean;
    source_artifact?: boolean;
  };
  download_available?: boolean;
  source_outline_id?: string | null;
  citation_provenance?: Record<string, unknown> | null;
  provider_source_coverage?: Record<string, unknown> | null;
  provenance: Record<string, unknown>;
};

export type ReportVersionsResponse = {
  run_id: string;
  report_id?: string | null;
  order?: string;
  items: ReportVersion[];
  total: number;
  warnings: string[];
};

export type ReportVersionDetailResponse = {
  run_id: string;
  report_id?: string | null;
  version: ReportVersion | null;
  content_reference?: string | null;
  citations?: {status:"VERIFIED"|"NOT_RECORDED"|"UNBOUND_CONTENT"|"OUTLINE_UNAVAILABLE"|"UNREADABLE"|"DIGEST_MISMATCH";items:CitationTraceItem[];warnings:string[]};
  review_binding?: string;
  review_eligible?: boolean;
  content?: string | null;
  content_integrity?: "SHA256_VERIFIED"|"LEGACY_HASH_VERIFIED"|"DIGEST_MISMATCH"|"DIGEST_UNAVAILABLE"|"SNAPSHOT_MISSING"|"SNAPSHOT_UNREADABLE";
  source_refs?: {
    run_id?: string | null;
    topic_id?: string | null;
    report_id?: string | null;
  };
  citation_provenance?: Record<string, unknown> | null;
  provenance?: Record<string, unknown> | null;
  warnings: string[];
};

export type ReportVersionSectionDiff = {
  section_id: string;
  title: string;
  status: "added" | "removed" | "changed" | "unchanged";
  from_order?: number | null;
  to_order?: number | null;
  from_excerpt?: string | null;
  to_excerpt?: string | null;
  body_similarity?: number | null;
};

export type BinaryArtifactComparisonItem = {
  format: string;
  from?: Partial<ExportArtifact> | null;
  to?: Partial<ExportArtifact> | null;
  availability: string;
  hash_equal?: boolean | null;
  metadata_changed: boolean;
  comparison_basis: "sha256_metadata";
  semantic_claim: "none";
  warning?: string | null;
};

export type BinaryArtifactComparisonResponse = {
  mode: "metadata_hash_only";
  items: BinaryArtifactComparisonItem[];
  text_extraction: {
    available: boolean;
    status: "available" | "unavailable";
    reason?: string | null;
  };
  warnings: string[];
};

export type ReportVersionDiffResponse = {
  run_id: string;
  ready: boolean;
  mode: "completed_versions";
  from_version?: Partial<ReportVersion> | null;
  to_version?: Partial<ReportVersion> | null;
  summary: {
    hash_changed?: boolean | null;
    added_sections: number;
    removed_sections: number;
    changed_sections: number;
    unchanged_sections: number;
  };
  metadata_changes?: Record<string, unknown>;
  citation_provenance_changes?: Record<string, unknown>;
  source_provenance_changes?: Record<string, unknown>;
  semantic_diff_status?: Record<string, unknown>;
  binary_artifact_comparison?: BinaryArtifactComparisonResponse;
  section_diffs: ReportVersionSectionDiff[];
  empty_reason?: string | null;
  warnings: string[];
};

export type CitationTraceItem = {
  citation_id: string;
  section_id: string;
  section_title: string;
  claim_id?: string | null;
  claim_text?: string | null;
  evidence_id?: string | null;
  source_id?: string | null;
  paper_id?: string | null;
  paper_title?: string | null;
  artifact_path?: string | null;
  citation_text?: string | null;
  snippet?: string | null;
  provenance: Record<string, unknown>;
};

export type CitationTraceResponse = {
  run_id: string;
  generated_at: string;
  source_outline_path?: string | null;
  source_preview_path?: string | null;
  items: CitationTraceItem[];
  warnings: string[];
};

export type LineageRunSummary = {
  run_id: string;
  topic: string;
  status: string;
  created_at: string;
  finished_at?: string | null;
  lineage_mode: LineageMode;
};

export type RunLineageResponse = {
  run_id: string;
  lineage: RunLineageRecord;
  parent?: LineageRunSummary | null;
  root: LineageRunSummary;
  children: LineageRunSummary[];
  same_root_runs: LineageRunSummary[];
};

export type CompareCandidate = {
  run_id: string;
  topic: string;
  status: string;
  created_at: string;
  triage_items: number;
  compare_ready_available: boolean;
  relation: string;
  same_root: boolean;
  preview_hash?: string | null;
  parent_run_id?: string | null;
  root_run_id: string;
};

export type CompareCandidatesResponse = {
  run_id: string;
  items: CompareCandidate[];
};

export type CompareSide = {
  run_id: string;
  topic: string;
  status: string;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  triage_items: number;
  reviewed_items: number;
  preview_hash?: string | null;
  preview_hash_source?: string | null;
  outline_sections: number;
  exports: string[];
  lineage: RunLineageRecord;
};

export type PaperDiffItem = {
  paper_id: string;
  title: string;
  theme: string;
  status: "added" | "removed" | "common";
  left_score?: number | null;
  right_score?: number | null;
  score_delta: number;
  left_rank?: number | null;
  right_rank?: number | null;
  rank_delta?: number | null;
  changed: boolean;
  present_in_left: boolean;
  present_in_right: boolean;
};

export type ThemeDelta = {
  theme: string;
  left_count: number;
  right_count: number;
  delta: number;
};

export type ScoreDistributionDelta = {
  band: "high" | "medium" | "low" | "unscored";
  left_count: number;
  right_count: number;
  delta: number;
};

export type KeywordMovement = {
  keyword: string;
  left_count: number;
  right_count: number;
  delta: number;
  left_sample_paper_ids: string[];
  right_sample_paper_ids: string[];
};

export type ProviderContributionDelta = {
  provider: string;
  left_count: number;
  right_count: number;
  delta: number;
  left_sample_paper_ids: string[];
  right_sample_paper_ids: string[];
};

export type CompareReportStatus = {
  left_available: boolean;
  right_available: boolean;
  left_status: string;
  right_status: string;
  left_updated_at?: string | null;
  right_updated_at?: string | null;
  left_artifact_path?: string | null;
  right_artifact_path?: string | null;
};

export type CompareProvenance = {
  source_run_ids: string[];
  computed_at: string;
  definitions: string[];
  warnings: string[];
};

export type OutlineSectionSummary = {
  section_id: string;
  title: string;
  kind: string;
  paper_ids: string[];
  body_hash?: string | null;
};

export type OutlineSectionDelta = {
  section_id: string;
  title: string;
  status: "added" | "removed" | "changed" | "unchanged";
  left_kind?: string | null;
  right_kind?: string | null;
  left_paper_count: number;
  right_paper_count: number;
};

export type ExportFormatDiff = {
  format: string;
  left_available: boolean;
  right_available: boolean;
  left_status?: string | null;
  right_status?: string | null;
};

export type CompareFilterContext = {
  paper_query?: string | null;
  theme?: string | null;
  status?: string | null;
  source_side: "left" | "right" | "both";
  bucket_id?: string | null;
  bucket_label?: string | null;
};

export type CompareRerunBucket = {
  bucket_id: string;
  label: string;
  description: string;
  source_run_id: string;
  compare_partner_run_id: string;
  mode: RerunMode;
  selected_paper_ids: string[];
  selection_count: number;
  available: boolean;
  empty_reason?: string | null;
  suggested_note?: string | null;
  profile_overrides?: RerunProfileOverrides | null;
  compare_filters?: CompareFilterContext | null;
};

export type ArtifactFallback = {
  run_side: "left" | "right";
  artifact_kind: string;
  available: boolean;
  used: boolean;
  path?: string | null;
  reason?: string | null;
};

export type CompareReportSummaryDiff = {
  left_source?: string | null;
  right_source?: string | null;
  left_hash?: string | null;
  right_hash?: string | null;
  hash_changed?: boolean | null;
  completeness: "full" | "partial" | "outline_only" | "metadata_only" | "unavailable";
  confidence: number;
  added_sections: number;
  removed_sections: number;
  changed_sections: number;
  unchanged_sections: number;
  order_changed_sections: number;
  citation_delta_count: number;
  missing_explanations: string[];
};

export type ReportSectionDiff = {
  section_key: string;
  display_title: string;
  status: "added" | "removed" | "changed" | "unchanged";
  left_title?: string | null;
  right_title?: string | null;
  left_order?: number | null;
  right_order?: number | null;
  title_changed: boolean;
  order_changed: boolean;
  body_changed: boolean;
  body_similarity?: number | null;
  left_excerpt?: string | null;
  right_excerpt?: string | null;
  left_citation_count: number;
  right_citation_count: number;
  citation_delta_count: number;
  left_paper_ids: string[];
  right_paper_ids: string[];
  change_summary: string[];
};

export type CitationDiff = {
  identifier: string;
  kind: "paper_id" | "url" | "reference";
  status: "added" | "removed" | "changed" | "unchanged";
  left_count: number;
  right_count: number;
  delta: number;
  left_sections: string[];
  right_sections: string[];
};

export type CompareReportDetailResponse = {
  left_run_id: string;
  right_run_id: string;
  report_summary_diff: CompareReportSummaryDiff;
  section_diffs: ReportSectionDiff[];
  citation_diffs: CitationDiff[];
  export_diffs: ExportFormatDiff[];
  artifact_fallbacks: ArtifactFallback[];
};

export type CompareResponse = {
  left: CompareSide;
  right: CompareSide;
  summary: {
    relation: string;
    same_root: boolean;
    preview_changed?: boolean | null;
    added_papers: number;
    removed_papers: number;
    common_papers: number;
    changed_common_papers: number;
    unchanged_common_papers: number;
    theme_delta_count: number;
    outline_changed: boolean;
  };
  papers: {
    added: PaperDiffItem[];
    removed: PaperDiffItem[];
    common: PaperDiffItem[];
  };
  themes: ThemeDelta[];
  score_distribution: ScoreDistributionDelta[];
  keyword_movement: KeywordMovement[];
  provider_contribution: ProviderContributionDelta[];
  report_status: CompareReportStatus;
  report: {
    left_outline: OutlineSectionSummary[];
    right_outline: OutlineSectionSummary[];
    outline_deltas: OutlineSectionDelta[];
    left_preview_hash?: string | null;
    right_preview_hash?: string | null;
    left_preview_hash_source?: string | null;
    right_preview_hash_source?: string | null;
    report_summary_diff: CompareReportSummaryDiff;
    artifact_fallbacks: ArtifactFallback[];
  };
  exports: ExportFormatDiff[];
  lineage: {
    relation: string;
    same_root: boolean;
    left: RunLineageResponse;
    right: RunLineageResponse;
  };
  rerun_buckets: CompareRerunBucket[];
  provenance: CompareProvenance;
};

export type RerunProfileOverrides = {
  topic?: string;
  queries?: string[];
  categories?: string[];
  max_results?: number | null;
  top_papers?: number | null;
  weekly_top?: number | null;
  skip_summary?: boolean | null;
  skip_synthesis?: boolean | null;
  experiment?: boolean | null;
  rank?: number | null;
};

export type RunRerunRequest = {
  mode: RerunMode;
  selected_paper_ids?: string[];
  profile_overrides?: RerunProfileOverrides;
  note?: string | null;
  compare_baseline_run_id?: string | null;
  skip_retrieval_when_branching?: boolean;
  preset_id?: string | null;
  selection_bucket_id?: string | null;
  selection_bucket_label?: string | null;
  selection_source?: SelectionSource | null;
};

export type RunRerunResponse = {
  child_run_id: string;
  status: string;
  lineage: RunLineageRecord;
  execution_plan: string[];
  message: string;
  rerun_context?: {
    compare_baseline_run_id?: string | null;
    preset_id?: string | null;
    selection_source?: SelectionSource | null;
    selection_bucket_id?: string | null;
    selection_bucket_label?: string | null;
    selected_paper_ids: string[];
  } | null;
};

export type RerunPresetLineageHint = {
  compare_baseline_run_id?: string | null;
  selection_source?: SelectionSource | null;
  selection_bucket_id?: string | null;
  selection_bucket_label?: string | null;
};

export type RerunPreset = {
  preset_id: string;
  source_run_id: string;
  compare_partner_run_id?: string | null;
  preset_name: string;
  preset_type: "compare_bucket" | "manual_selection" | "profile_override" | "full_rerun";
  mode: RerunMode;
  selected_paper_ids: string[];
  profile_overrides?: RerunProfileOverrides | null;
  compare_filters?: CompareFilterContext | null;
  note?: string | null;
  created_at: string;
  created_from_compare: boolean;
  lineage_hint: RerunPresetLineageHint;
};

export type CompareContextBanner = {
  title: string;
  message: string;
  relation?: string | null;
  bucket_label?: string | null;
  preset_name?: string | null;
  focus_theme?: string | null;
  focus_section_title?: string | null;
};

export type CompareContextSummary = {
  source_run_id: string;
  compare_partner_run_id?: string | null;
  relation?: string | null;
  same_root: boolean;
};

export type CompareContextSectionSeed = {
  section_id: string;
  section_title: string;
  source_side: "left" | "right";
  available: boolean;
  matched_outline_section_id?: string | null;
  body_markdown?: string | null;
  excerpt?: string | null;
  paper_ids: string[];
  change_summary: string[];
};

export type RunCompareContextResponse = {
  run_id: string;
  editing_session_id?: string | null;
  editing_session_title?: string | null;
  editing_session_stale: boolean;
  workspace_view_id?: string | null;
  workspace_view_title?: string | null;
  compare_partner_run_id?: string | null;
  compare_mode?: string | null;
  compare_view?: CompareView | null;
  selection_bucket_id?: string | null;
  preset_id?: string | null;
  selection_source?: SelectionSource | null;
  preferred_report_side: "left" | "right" | "both";
  active_pane: "paper" | "report";
  effective_filters: CompareFilterContext;
  selected_paper_ids: string[];
  focus_paper_id?: string | null;
  focus_theme?: string | null;
  focus_section_id?: string | null;
  focus_section_title?: string | null;
  banner?: CompareContextBanner | null;
  summary: CompareContextSummary;
  pending_section_seed?: CompareContextSectionSeed | null;
  warnings: string[];
};

export type WorkspaceActionType =
  | "insert_section_before"
  | "insert_section_after"
  | "append_to_target_section"
  | "replace_section_draft"
  | "create_candidate_section"
  | "attach_compare_note"
  | "apply_theme_outline_suggestion"
  | "apply_paper_seed_suggestion";

export type WorkspaceActionPayload = {
  title?: string | null;
  body_markdown?: string | null;
  paper_ids: string[];
  compare_note?: string | null;
  metadata: Record<string, unknown>;
};

export type WorkspaceActionRequest = {
  editing_session_id?: string | null;
  workspace_view_id?: string | null;
  action_type: WorkspaceActionType;
  source_run_id?: string | null;
  compare_partner_run_id?: string | null;
  source_section_id?: string | null;
  source_section_title?: string | null;
  target_section_id?: string | null;
  target_position?: "before" | "after" | "append" | "replace" | "note" | null;
  preferred_report_side?: "left" | "right" | "both";
  source_bucket_id?: string | null;
  source_theme?: string | null;
  source_paper_ids?: string[];
  preset_id?: string | null;
  selection_source?: string | null;
  payload?: Partial<WorkspaceActionPayload>;
  warnings?: string[];
};

export type WorkspaceActionTarget = {
  target_section_id?: string | null;
  target_section_title?: string | null;
  target_index?: number | null;
  destructive: boolean;
};

export type WorkspaceActionPreviewResponse = {
  run_id: string;
  action_id: string;
  action_type: WorkspaceActionType;
  preview_ready: boolean;
  effective_target: WorkspaceActionTarget;
  proposed_section?: OutlineSection | null;
  proposed_compare_note?: string | null;
  provenance: Record<string, unknown>;
  warnings: string[];
  conflicts: string[];
  missing_artifacts: string[];
  summary_lines: string[];
  preview_blocks: WorkspacePreviewBlock[];
  changed_block_count: number;
  citation_delta: WorkspaceCitationDelta;
  destructive: boolean;
  requires_confirmation: boolean;
  completeness: "full" | "partial" | "coarse" | "metadata_only";
  structured_patch?: StructuredPatch | null;
  conflict_details: WorkspaceConflictDetail[];
  candidate_conflict_summary: WorkspaceCandidateConflictSummary;
};

export type WorkspaceActionApplyResponse = {
  run_id: string;
  action_id: string;
  action_type: WorkspaceActionType;
  applied: boolean;
  message: string;
  warnings: string[];
  outline: OutlineResponse;
  preview: PreviewResponse;
};

export type WorkspacePreviewBlock = {
  block_id: string;
  block_type: "unchanged" | "added" | "removed" | "replaced" | "context";
  label: string;
  kind: "source" | "target" | "proposed" | "note" | "summary" | "patch";
  content: string;
  before_text?: string | null;
  after_text?: string | null;
  before_excerpt?: string | null;
  after_excerpt?: string | null;
  anchor_kind: "section" | "paragraph" | "note" | "summary";
  anchor_value?: string | null;
  related_citation_keys: string[];
  warnings: string[];
};

export type WorkspaceCitationDelta = {
  left_count: number;
  right_count: number;
  delta: number;
  notes: string[];
};

export type PatchAnchor = {
  anchor_kind: "section_boundary" | "paragraph_index" | "sentence_window" | "citation_key" | "note_slot" | "fallback";
  anchor_value?: string | null;
  matched: boolean;
  confidence: number;
  fallback_reason?: string | null;
};

export type WorkspaceTargetResolution = {
  original_target_section_id?: string | null;
  resolved_target_section_id?: string | null;
  resolution_mode: "exact" | "retargeted" | "title_match" | "index_shift" | "fallback" | "missing";
  resolution_confidence: number;
  retarget_warnings: string[];
};

export type WorkspaceConflictKind =
  | "missing_target_section"
  | "missing_source_section"
  | "weak_anchor_match"
  | "ambiguous_target_match"
  | "duplicate_target_mutation"
  | "destructive_replace_requires_confirmation"
  | "stale_outline_hash"
  | "citation_overlap_or_reference_risk"
  | "target_order_instability"
  | "source_artifact_missing"
  | "dropped_dependency"
  | "recoverable_by_rebase";

export type WorkspaceConflictSeverity = "info" | "warn" | "blocker" | "recoverable" | "confirmation_required";
export type WorkspaceRecoveryMode = "refresh_session_metadata" | "rebase_staged_candidates" | "discard_staged_candidates" | "none";
export type WorkspaceCandidateStatus =
  | "safe"
  | "warn"
  | "blocked"
  | "recoverable_by_rebase"
  | "destructive_confirmation_required"
  | "dropped";

export type WorkspaceConflictDetail = {
  conflict_id: string;
  candidate_id?: string | null;
  conflict_kind: WorkspaceConflictKind;
  severity: WorkspaceConflictSeverity;
  title: string;
  message: string;
  related_section_ids: string[];
  related_candidate_ids: string[];
  related_paper_ids: string[];
  anchor_summary?: string | null;
  target_resolution_summary?: string | null;
  suggested_recovery_mode: WorkspaceRecoveryMode;
  suggested_manual_actions: string[];
  can_recover_by_rebase: boolean;
  can_proceed_with_confirmation: boolean;
  warnings: string[];
  provenance: Record<string, unknown>;
};

export type WorkspaceCandidateConflictSummary = {
  candidate_id?: string | null;
  status: WorkspaceCandidateStatus;
  total_conflicts: number;
  highest_severity: WorkspaceConflictSeverity;
  severity_counts: Record<string, number>;
  affected_section_ids: string[];
  related_paper_ids: string[];
  can_apply: boolean;
  can_recover_by_rebase: boolean;
  can_proceed_with_confirmation: boolean;
  suggested_recovery_mode: WorkspaceRecoveryMode;
  suggested_manual_actions: string[];
  warnings: string[];
};

export type WorkspaceRecoveryConflictDelta = {
  candidate_id: string;
  original_status: WorkspaceCandidateStatus;
  projected_status: WorkspaceCandidateStatus;
  original_conflicts: WorkspaceConflictDetail[];
  projected_conflicts: WorkspaceConflictDetail[];
  resolved_conflict_ids: string[];
  introduced_conflict_ids: string[];
  warnings: string[];
};

export type WorkspaceRebaseDiff = {
  candidate_id: string;
  recovery_mode: WorkspaceRecoveryMode;
  comparison_precision: "fine_grained" | "section_level" | "coarse";
  original_target_resolution: WorkspaceTargetResolution;
  projected_target_resolution: WorkspaceTargetResolution;
  original_patch?: StructuredPatch | null;
  projected_patch?: StructuredPatch | null;
  original_conflicts: WorkspaceConflictDetail[];
  projected_conflicts: WorkspaceConflictDetail[];
  original_status: WorkspaceCandidateStatus;
  projected_status: WorkspaceCandidateStatus;
  confidence_delta: number;
  warnings_before: string[];
  warnings_after: string[];
  dropped_blocks: number;
  added_blocks: number;
  changed_anchor_count: number;
  affected_sections_before: string[];
  affected_sections_after: string[];
  projected_can_apply: boolean;
  projected_requires_confirmation: boolean;
  warnings: string[];
};

export type WorkspaceRouteRestoreHints = {
  compare_view?: CompareView | null;
  active_pane: "paper" | "report";
  editing_session_id?: string | null;
  compare_partner_run_id?: string | null;
  focus_section_id?: string | null;
  focus_section_title?: string | null;
  focus_paper_id?: string | null;
  selected_paper_ids: string[];
  selection_source?: SelectionSource | null;
  warnings: string[];
};

export type StructuredPatchBlock = {
  block_id: string;
  operation: "context" | "add" | "remove" | "replace" | "append" | "note";
  anchor_before: PatchAnchor;
  anchor_after: PatchAnchor;
  before_text?: string | null;
  after_text?: string | null;
  before_excerpt?: string | null;
  after_excerpt?: string | null;
  related_citation_keys: string[];
  related_paper_ids: string[];
  warnings: string[];
  confidence: number;
};

export type StructuredPatch = {
  candidate_id?: string | null;
  merge_intent:
    | "replace_target_section"
    | "insert_before_target"
    | "insert_after_target"
    | "append_to_target_section"
    | "create_candidate_section"
    | "attach_compare_note_only";
  source_run_id?: string | null;
  compare_partner_run_id?: string | null;
  source_section_id?: string | null;
  source_section_title?: string | null;
  target_section_id?: string | null;
  target_section_title?: string | null;
  patch_scope: "section" | "body" | "note" | "metadata";
  destructive: boolean;
  requires_confirmation: boolean;
  completeness: "full" | "partial" | "coarse" | "metadata_only";
  provenance: Record<string, unknown>;
  warnings: string[];
  conflict_flags: string[];
  conflict_details: WorkspaceConflictDetail[];
  candidate_conflict_summary: WorkspaceCandidateConflictSummary;
  target_resolution: WorkspaceTargetResolution;
  blocks: StructuredPatchBlock[];
  created_at?: string | null;
};

export type MergeCandidate = {
  candidate_id: string;
  editing_session_id?: string | null;
  source_run_id: string;
  compare_partner_run_id?: string | null;
  workspace_view_id?: string | null;
  source_section_id?: string | null;
  source_section_title?: string | null;
  source_bucket_id?: string | null;
  source_theme?: string | null;
  source_paper_ids: string[];
  target_section_id?: string | null;
  target_section_title?: string | null;
  target_index?: number | null;
  merge_intent:
    | "replace_target_section"
    | "insert_before_target"
    | "insert_after_target"
    | "append_to_target_section"
    | "create_candidate_section"
    | "attach_compare_note_only";
  preview_blocks: WorkspacePreviewBlock[];
  changed_block_count: number;
  citation_delta: WorkspaceCitationDelta;
  warnings: string[];
  conflict_flags: string[];
  missing_artifacts: string[];
  destructive: boolean;
  requires_confirmation: boolean;
  provenance: Record<string, unknown>;
  summary_lines: string[];
  completeness: "full" | "partial" | "coarse" | "metadata_only";
  structured_patch?: StructuredPatch | null;
  conflict_summary: WorkspaceCandidateConflictSummary;
  request: WorkspaceActionRequest;
  created_at: string;
  order: number;
};

export type WorkspaceStagedActionsResponse = {
  run_id: string;
  editing_session_id?: string | null;
  items: MergeCandidate[];
  warnings: string[];
};

export type WorkspaceActionApplyStagedResponse = {
  run_id: string;
  editing_session_id?: string | null;
  applied_count: number;
  applied_action_ids: string[];
  warnings: string[];
  blockers: string[];
  status: "applied" | "blocked" | "partial" | "noop";
  outline: OutlineResponse;
  preview: PreviewResponse;
};

export type WorkspaceSuggestion = {
  suggestion_id: string;
  suggestion_type: "report_delta" | "theme_delta" | "paper_seed" | "rerun_context";
  title: string;
  description: string;
  action_seed: WorkspaceActionRequest;
  related_paper_ids: string[];
  focus_theme?: string | null;
  focus_section_id?: string | null;
  focus_section_title?: string | null;
  warnings: string[];
};

export type WorkspaceSuggestionsResponse = {
  run_id: string;
  compare_partner_run_id?: string | null;
  items: WorkspaceSuggestion[];
  warnings: string[];
};

export type WorkspaceViewActionSeed = {
  action_type: WorkspaceActionType;
  source_section_id?: string | null;
  source_section_title?: string | null;
  target_section_id?: string | null;
  preferred_report_side: "left" | "right" | "both";
  source_bucket_id?: string | null;
  source_theme?: string | null;
  source_paper_ids: string[];
  payload: WorkspaceActionPayload;
};

export type WorkspaceView = {
  view_id: string;
  run_id: string;
  compare_partner_run_id?: string | null;
  title: string;
  description?: string | null;
  active_compare_view?: string | null;
  compare_mode?: string | null;
  selection_bucket_id?: string | null;
  preset_id?: string | null;
  focus_theme?: string | null;
  focus_section_id?: string | null;
  focus_section_title?: string | null;
  preferred_report_side: "left" | "right" | "both";
  selection_source?: string | null;
  selected_paper_ids: string[];
  pending_action_seed?: WorkspaceViewActionSeed | null;
  report_tab_state: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type WorkspaceViewCreateRequest = {
  title: string;
  description?: string | null;
  compare_partner_run_id?: string | null;
  active_compare_view?: string | null;
  compare_mode?: string | null;
  selection_bucket_id?: string | null;
  preset_id?: string | null;
  focus_theme?: string | null;
  focus_section_id?: string | null;
  focus_section_title?: string | null;
  preferred_report_side?: "left" | "right" | "both";
  selection_source?: string | null;
  selected_paper_ids?: string[];
  pending_action_seed?: WorkspaceViewActionSeed | null;
  report_tab_state?: Record<string, unknown>;
};

export type WorkspaceViewsResponse = {
  run_id: string;
  items: WorkspaceView[];
};

export type EditingSession = {
  session_id: string;
  run_id: string;
  title: string;
  description?: string | null;
  workspace_view_id?: string | null;
  compare_partner_run_id?: string | null;
  active_compare_view?: string | null;
  compare_mode?: string | null;
  selection_bucket_id?: string | null;
  preset_id?: string | null;
  focus_theme?: string | null;
  focus_section_id?: string | null;
  focus_section_title?: string | null;
  preferred_report_side: "left" | "right" | "both";
  selection_source?: string | null;
  selected_paper_ids: string[];
  staged_action_ids: string[];
  pending_merge_order: string[];
  report_tab_state: Record<string, unknown>;
  outline_draft_hash?: string | null;
  current_outline_draft_hash?: string | null;
  stale: boolean;
  warnings: string[];
  created_at: string;
  updated_at: string;
};

export type WorkspaceApplyStagedPreflightCandidate = {
  candidate_id: string;
  merge_intent:
    | "replace_target_section"
    | "insert_before_target"
    | "insert_after_target"
    | "append_to_target_section"
    | "create_candidate_section"
    | "attach_compare_note_only";
  target_section_id?: string | null;
  target_section_title?: string | null;
  target_index?: number | null;
  warnings: string[];
  blockers: string[];
  destructive: boolean;
  requires_confirmation: boolean;
  changed_block_count: number;
  status: WorkspaceCandidateStatus;
  structured_patch?: StructuredPatch | null;
  conflict_summary: WorkspaceCandidateConflictSummary;
};

export type WorkspacePreflightGroup = {
  group_key: string;
  label: string;
  severity: "info" | "warning" | "blocker";
  reason: string;
  candidate_ids: string[];
  messages: string[];
  candidate_summaries: WorkspaceCandidateConflictSummary[];
  conflicts: WorkspaceConflictDetail[];
  affected_section_ids: string[];
  related_paper_ids: string[];
  suggested_recovery_mode: WorkspaceRecoveryMode;
  suggested_manual_actions: string[];
};

export type WorkspacePreflightQueueConflictGroup = {
  conflict_type:
    | "duplicate_target_mutation"
    | "stale_outline_hash"
    | "missing_target_reference"
    | "missing_source_reference"
    | "reorder_risk"
    | "destructive_confirmation";
  target_section_id?: string | null;
  candidate_ids: string[];
  message: string;
};

export type WorkspaceRecommendedRecoveryPlan = {
  mode: WorkspaceRecoveryMode;
  summary: string;
  candidate_ids: string[];
  warnings: string[];
};

export type WorkspaceApplyStagedPreflightResponse = {
  run_id: string;
  editing_session_id?: string | null;
  current_outline_hash?: string | null;
  session_outline_hash?: string | null;
  is_stale: boolean;
  status: "ready" | "ready_with_warnings" | "blocked";
  candidate_order: string[];
  affected_section_ids: string[];
  warnings: string[];
  blockers: string[];
  destructive_candidates: string[];
  confirmation_required_ids: string[];
  safe_candidate_ids: string[];
  warn_candidate_ids: string[];
  blocked_candidate_ids: string[];
  rebaseable_candidate_ids: string[];
  grouped_warnings: WorkspacePreflightGroup[];
  grouped_blockers: WorkspacePreflightGroup[];
  queue_conflict_groups: WorkspacePreflightQueueConflictGroup[];
  can_apply_all: boolean;
  recommended_recovery_mode: WorkspaceRecoveryMode;
  recommended_recovery_plan: WorkspaceRecommendedRecoveryPlan;
  items: WorkspaceApplyStagedPreflightCandidate[];
};

export type EditingSessionStaleness = {
  run_id: string;
  session_id: string;
  outline_hash_changed: boolean;
  missing_target_refs: string[];
  missing_source_refs: string[];
  reorder_risk: string[];
  deleted_staged_dependencies: string[];
  stale_reasons: string[];
  recommended_actions: string[];
  can_rebase: boolean;
  can_refresh: boolean;
  can_discard_queue: boolean;
  warnings: string[];
};

export type EditingSessionRecoverResponse = {
  run_id: string;
  session_id: string;
  mode: "refresh_session_metadata" | "rebase_staged_candidates" | "discard_staged_candidates";
  recovered_candidate_ids: string[];
  discarded_candidate_ids: string[];
  warnings: string[];
  session: EditingSession;
  staged: WorkspaceStagedActionsResponse;
  staleness: EditingSessionStaleness;
};

export type EditingSessionRecoveryCandidateResult = {
  candidate_id: string;
  merge_intent:
    | "replace_target_section"
    | "insert_before_target"
    | "insert_after_target"
    | "append_to_target_section"
    | "create_candidate_section"
    | "attach_compare_note_only";
  result: "preserved" | "rebased" | "dropped";
  original_target_section_id?: string | null;
  resolved_target_section_id?: string | null;
  original_status: WorkspaceCandidateStatus;
  projected_status: WorkspaceCandidateStatus;
  target_resolution: WorkspaceTargetResolution;
  warnings: string[];
  blockers: string[];
  original_structured_patch?: StructuredPatch | null;
  structured_patch?: StructuredPatch | null;
  original_conflict_summary: WorkspaceCandidateConflictSummary;
  projected_conflict_summary: WorkspaceCandidateConflictSummary;
  recovery_conflict_delta: WorkspaceRecoveryConflictDelta;
  rebase_diff?: WorkspaceRebaseDiff | null;
};

export type EditingSessionRecoveryPreviewResponse = {
  run_id: string;
  session_id: string;
  recovery_mode: Exclude<WorkspaceRecoveryMode, "none">;
  outline_hash_changed: boolean;
  candidate_results: EditingSessionRecoveryCandidateResult[];
  preserved_candidate_ids: string[];
  rebased_candidate_ids: string[];
  dropped_candidate_ids: string[];
  warnings: string[];
  blockers: string[];
  projected_can_apply_all: boolean;
  projected_preflight_summary: WorkspaceApplyStagedPreflightResponse;
};

export type OutlineActionHistoryAffectedSection = {
  section_id: string;
  title: string;
  before_excerpt?: string | null;
  after_excerpt?: string | null;
  before_paper_ids: string[];
  after_paper_ids: string[];
  changed_block_count: number;
  completeness: "full" | "partial" | "coarse" | "metadata_only";
  preview_blocks: WorkspacePreviewBlock[];
  structured_patch?: StructuredPatch | null;
  related_paper_ids: string[];
  warnings: string[];
};

export type OutlineActionHistoryEntry = {
  action_id: string;
  candidate_id?: string | null;
  editing_session_id?: string | null;
  merge_intent:
    | "replace_target_section"
    | "insert_before_target"
    | "insert_after_target"
    | "append_to_target_section"
    | "create_candidate_section"
    | "attach_compare_note_only";
  source_run_id?: string | null;
  compare_partner_run_id?: string | null;
  source_section_id?: string | null;
  target_section_id?: string | null;
  timestamp: string;
  outcome: "applied" | "partial" | "blocked";
  warnings: string[];
  before_outline_hash?: string | null;
  after_outline_hash?: string | null;
  affected_sections: OutlineActionHistoryAffectedSection[];
  provenance: Record<string, unknown>;
  preview_summary: string[];
};

export type OutlineActionHistoryResponse = {
  run_id: string;
  items: OutlineActionHistoryEntry[];
  warnings: string[];
};

export type OutlineActionHistoryDiffResponse = {
  run_id: string;
  from_action_id?: string | null;
  to_action_id?: string | null;
  ready: boolean;
  mode: "single_action" | "between_actions" | "action_vs_current_draft";
  items: OutlineActionHistoryAffectedSection[];
  warnings: string[];
};

export type OutlineActionHistoryFocusContext = {
  run_id: string;
  action_id: string;
  ready: boolean;
  editing_session_id?: string | null;
  compare_partner_run_id?: string | null;
  source_run_id?: string | null;
  affected_section_ids: string[];
  focus_section_id?: string | null;
  focus_section_title?: string | null;
  related_paper_ids: string[];
  focus_paper_id?: string | null;
  selection_source?: SelectionSource | null;
  provenance_summary: string[];
  source_artifacts_available: boolean;
  route_restore_hints: WorkspaceRouteRestoreHints;
  warnings: string[];
};

export type EditingSessionCreateRequest = {
  title: string;
  description?: string | null;
  workspace_view_id?: string | null;
  compare_partner_run_id?: string | null;
  active_compare_view?: string | null;
  compare_mode?: string | null;
  selection_bucket_id?: string | null;
  preset_id?: string | null;
  focus_theme?: string | null;
  focus_section_id?: string | null;
  focus_section_title?: string | null;
  preferred_report_side?: "left" | "right" | "both";
  selection_source?: string | null;
  selected_paper_ids?: string[];
  staged_action_ids?: string[];
  pending_merge_order?: string[];
  report_tab_state?: Record<string, unknown>;
  outline_draft_hash?: string | null;
};

export type EditingSessionsResponse = {
  run_id: string;
  items: EditingSession[];
};

export type RerunPresetListResponse = {
  run_id: string;
  items: RerunPreset[];
};

export type RerunPresetCreateRequest = {
  preset_name: string;
  preset_type?: "compare_bucket" | "manual_selection" | "profile_override" | "full_rerun";
  mode: RerunMode;
  compare_partner_run_id?: string | null;
  selected_paper_ids?: string[];
  profile_overrides?: RerunProfileOverrides | null;
  compare_filters?: CompareFilterContext | null;
  note?: string | null;
  created_from_compare?: boolean;
  lineage_hint?: RerunPresetLineageHint | null;
};

function resolveApiBase() {
  const configured = import.meta.env.VITE_API_BASE_URL?.trim();
  if (configured) {
    return configured.replace(/\/$/, "");
  }
  if (typeof window !== "undefined") {
    if (window.location.port === "5173") {
      return "http://127.0.0.1:8000";
    }
    return window.location.origin;
  }
  return "http://127.0.0.1:8000";
}

const API_BASE = resolveApiBase();

export type EvidenceCorpusKind = "paper" | "review" | "evidence" | "run" | "report_section";

export type EvidenceSearchResultKind = "paper" | "run" | "report_section";

export type EvidenceReviewLevel = "abstract_only" | "full_text_reviewed";

export type EvidenceReviewSource = "full_text" | "abstract" | "provider_output_class_proof";

export type EvidenceStatus =
  | "full_text_reviewed"
  | "abstract_only"
  | "provider_proof_backed"
  | "deterministic_local"
  | "missing_evidence"
  | "missing_artifact"
  | "stale"
  | "current"
  | "degraded"
  | "orphaned_reference"
  | "unverified_reference";

export type EvidenceWarning =
  | "report_provenance_not_indexed"
  | "paper_id_collision"
  | "orphaned_reference"
  | "missing_evidence"
  | "malformed_provider_report"
  | "provider_ownership_unproven";

export type EvidenceTruthLevel =
  | "exact_persisted"
  | "exact_indexed_projection"
  | "exact_portable_ref"
  | "derived_deterministic";

export type EvidenceProvenanceNodeKind =
  | "topic"
  | "run"
  | "paper"
  | "review"
  | "evidence"
  | "evidence_span"
  | "report"
  | "report_section"
  | "artifact"
  | "provider"
  | "provider_proof";

export type EvidenceProvenanceEdgeKind =
  | "topic_contains_paper"
  | "run_contains_paper"
  | "paper_has_review"
  | "review_cites_evidence_span"
  | "run_has_evidence"
  | "evidence_cites_paper"
  | "review_references_artifact"
  | "provider_generated_review"
  | "evidence_span_references_provider_proof"
  | "run_has_report"
  | "report_has_section"
  | "run_derived_from_run"
  | "provider_report_references_review"
  | "provider_report_references_paper"
  | "provider_report_references_evidence_span"
  | "provider_report_references_proof"
  | "run_topic_matches_profile";

export type EvidenceArtifactVerificationStatus =
  | "current"
  | "stale"
  | "missing_artifact"
  | "orphaned_reference"
  | "unverified_reference";

export type EvidenceSafeLinks = {
  topic: string | null;
  run: string | null;
  report: string | null;
  paper: string | null;
};

export type EvidenceSafeSnippet = {
  text: string;
  sourceField: string;
  truncated: boolean;
};

export type EvidenceRankComponents = {
  tier: number;
  tokenCoverage: number;
  effectiveTimestamp: string;
  kindOrder: number;
};

export type EvidenceOccurrence = {
  runId: string;
  rawPaperId: string;
  title: string;
  topicId: string | null;
  topicName: string;
  runStatus: string;
  paperStatus: string;
  theme: string;
  score: number;
  published: string | null;
  entryUrl: string | null;
  pdfUrl: string | null;
  updatedAt: string;
  links: EvidenceSafeLinks;
};

export type EvidenceReviewSummary = {
  topicId: string;
  source: EvidenceReviewSource;
  providerId: string | null;
  level: EvidenceReviewLevel;
  reviewedAt: string;
  reviewModel: string | null;
  inlineEvidenceSpanCount: number;
};

export type EvidenceProvenanceNode = {
  nodeId: string;
  nodeKind: EvidenceProvenanceNodeKind;
  stableId: string;
  label: string;
  topicId: string | null;
  runId: string | null;
  paperId: string | null;
  portableRef: string | null;
  status: EvidenceStatus | null;
};

export type EvidenceProvenanceEdge = {
  edgeId: string;
  edgeKind: EvidenceProvenanceEdgeKind;
  fromNodeId: string;
  toNodeId: string;
  truthLevel: EvidenceTruthLevel;
};

export type EvidenceSearchResultItem = {
  resultId: string;
  resultKind: EvidenceSearchResultKind;
  canonicalPaperId: string | null;
  paperIdAliases: string[];
  title: string;
  titleAuthority: string;
  safeSnippet: EvidenceSafeSnippet | null;
  matchedFields: string[];
  matchedCorpusKinds: EvidenceCorpusKind[];
  matchReason: string;
  rankTier: number;
  rankComponents: EvidenceRankComponents;
  topicIds: string[];
  reviewSummary: EvidenceReviewSummary | null;
  inlineEvidenceSpanCount: number;
  runEvidenceCount: number;
  occurrenceCount: number;
  occurrences: EvidenceOccurrence[];
  occurrencesTruncated: boolean;
  provenancePreview: EvidenceProvenanceEdge[];
  statuses: EvidenceStatus[];
  warnings: EvidenceWarning[];
  links: EvidenceSafeLinks;
};

export type EvidenceSearchFiltersEcho = {
  topicIds: string[];
  runIds: string[];
  corpusKinds: EvidenceCorpusKind[];
  reviewSources: EvidenceReviewSource[];
  providerIds: string[];
  reviewLevels: EvidenceReviewLevel[];
  hasEvidence: boolean | null;
};

export type EvidenceSearchQueryEcho = {
  raw: string;
  normalized: string;
  tokens: string[];
  filters: EvidenceSearchFiltersEcho;
};

export type EvidenceSearchPage = {
  limit: number;
  returnedCount: number;
  total: number;
  totalRelation: string;
  nextCursor: string | null;
};

export type EvidenceSearchFacets = {
  corpusKinds: Record<string, number>;
  reviewLevels: Record<string, number>;
  reviewSources: Record<string, number>;
  providers: Record<string, number>;
  statuses: Record<string, number>;
};

export type EvidenceSearchResponse = {
  query: EvidenceSearchQueryEcho;
  page: EvidenceSearchPage;
  facets: EvidenceSearchFacets;
  items: EvidenceSearchResultItem[];
};

export type EvidenceArtifactVerification = {
  portableRef: string | null;
  refKind: string;
  status: EvidenceArtifactVerificationStatus;
  exists: boolean;
  expectedHashPresent: boolean;
  hashMatches: boolean | null;
};

export type EvidenceProviderReportSummary = {
  draftId: string;
  providerFamilyId: string;
  providerSurfaceId: string;
  sourceReviewIds: string[];
  sourcePaperIds: string[];
  evidenceSpanRefs: string[];
  proofRefs: string[];
};

type ExcludeNullFields<T> = T extends Array<infer Item>
  ? Array<ExcludeNullFields<Item>>
  : T extends object
    ? {
        [Key in keyof T as null extends T[Key] ? never : Key]: ExcludeNullFields<T[Key]>;
      } & {
        [Key in keyof T as null extends T[Key] ? Key : never]?: ExcludeNullFields<Exclude<T[Key], null>>;
      }
    : T;

export type EvidenceItemDetailResponse = {
  resultId: string;
  resultKind: EvidenceSearchResultKind;
  summary: ExcludeNullFields<EvidenceSearchResultItem>;
  occurrenceCount: number;
  occurrences: Array<ExcludeNullFields<EvidenceOccurrence>>;
  occurrencesTruncated: boolean;
  selectedOccurrenceRunId?: string;
  provenanceNodes: Array<ExcludeNullFields<EvidenceProvenanceNode>>;
  provenanceEdges: EvidenceProvenanceEdge[];
  artifactVerifications: Array<ExcludeNullFields<EvidenceArtifactVerification>>;
  providerReport?: EvidenceProviderReportSummary;
  warnings: EvidenceWarning[];
  links: ExcludeNullFields<EvidenceSafeLinks>;
};

export type EvidenceSearchParams = {
  q?: string;
  topic_ids?: string[];
  run_ids?: string[];
  corpus_kinds?: EvidenceCorpusKind[];
  review_sources?: EvidenceReviewSource[];
  provider_ids?: string[];
  review_levels?: EvidenceReviewLevel[];
  has_evidence?: boolean | null;
  limit?: number;
  cursor?: string | null;
};

export type GlobalSearchKind = "topic" | "paper" | "evidence" | "run";

export type GlobalSearchItem = {
  kind: GlobalSearchKind;
  resultId: string;
  topicId?: string | null;
  paperVersionId?: string | null;
  evidenceId?: string | null;
  runId?: string | null;
  primaryLabel: string;
  secondaryText: string;
  route: string;
  rank: number;
  rankReason: string;
  matchedFields: string[];
  sourceState: "available" | "empty" | "unavailable";
  updatedAt?: string | null;
};

export type GlobalSearchResponse = {
  query: { raw: string; normalized: string; tokens: string[] };
  queryDigest: string;
  requestedKinds: string[];
  limit: number;
  total: number;
  items: GlobalSearchItem[];
  sourceStates: Record<string, "available" | "empty" | "unavailable">;
  unavailableKinds: Record<string, string>;
};

export type SearchHelpResponse = {
  normalization: Record<string, string>;
  limits: Record<string, number>;
  syntax: Record<string, { status: string; interpretation: string }>;
  scopes: Record<string, string>;
  examples: Array<Record<string, string>>;
};

export const SEARCH_SYNTAX_CONTRACT = {
  plain: { status: "supported", interpretation: "normalized_literal_text" },
  quotes: { status: "literal", interpretation: "literal" },
  comma: { status: "literal", interpretation: "literal" },
  boolean: { status: "literal", interpretation: "literal" },
  wildcard: { status: "literal", interpretation: "literal" },
  field: { status: "literal", interpretation: "literal" },
} as const;

export function normalizeSearchQuery(value: string | null | undefined): string {
  return (value ?? "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export type EvidenceItemDetailParams = {
  occurrence_run_id?: string | null;
};

export type EvidenceApiRequestOptions = {
  signal?: AbortSignal;
};

export class ApiError extends Error {
  status: number;
  body: string;

  constructor(message: string, status: number, body: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

async function checkedResponse(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    const body = await response.text();
    let message = body;
    try {
      const parsed = JSON.parse(body) as { detail?: string };
      if (parsed?.detail) {
        message = parsed.detail;
      }
    } catch {
      // Fall back to the raw body text.
    }
    if (response.status === 401) {
      window.dispatchEvent(new CustomEvent("xrml:auth-unauthorized"));
    }
    throw new ApiError(message || `Request failed: ${response.status}`, response.status, body);
  }

  return response;
}

export async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  return (await checkedResponse(path, init)).json() as Promise<T>;
}

/** Preserve server JSON number spelling and Unicode when exporting hashed records. */
export async function fetchText(path: string, init?: RequestInit): Promise<string> {
  return (await checkedResponse(path, { ...init, cache: "no-store" })).text();
}

export function buildQuery(params: Record<string, string | number | boolean | null | undefined>) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") {
      return;
    }
    search.set(key, String(value));
  });
  const query = search.toString();
  return query ? `?${query}` : "";
}

export function getApiBase() {
  return API_BASE;
}

export function getAuthMe() {
  return fetchJson<AuthSessionResponse>("/auth/me");
}

export function logoutAuth() {
  return fetchJson<{ auth_mode: "disabled" | "oidc"; logged_out: boolean }>("/auth/logout", {
    method: "POST",
  });
}

export function getLegacyRuns(params: {
  status?: LegacyRunStatusFilter;
  q?: string | null;
  include_lineage_summary?: boolean;
} = {}) {
  return fetchJson<LegacyRunsListResponse>(`/admin/legacy-runs${buildQuery(params)}`);
}

export function getOwnershipAudit(params: { sample_limit?: number } = {}) {
  return fetchJson<OwnershipAuditResponse>(`/admin/ownership-audit${buildQuery(params)}`);
}

export function previewLegacyRunMigration(payload: LegacyRunsMigrationRequest) {
  return fetchJson<LegacyRunsMigrationResponse>("/admin/legacy-runs/preview-migration", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function applyLegacyRunMigration(payload: LegacyRunsMigrationRequest) {
  return fetchJson<LegacyRunsMigrationResponse>("/admin/legacy-runs/apply-migration", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getRuns() {
  return fetchJson<RunRecord[]>("/runs");
}

export function getRun(runId: string) {
  return fetchJson<RunRecord>(`/runs/${runId}`);
}

export function getRunLineage(runId: string) {
  return fetchJson<RunLineageResponse>(`/runs/${runId}/lineage`);
}

export function getRunCompareCandidates(runId: string) {
  return fetchJson<CompareCandidatesResponse>(`/runs/${runId}/compare-candidates`);
}

export function getRunCompareContext(
  runId: string,
  params: {
    editing_session_id?: string | null;
    workspace_view_id?: string | null;
    compare_partner_run_id?: string | null;
    compare_mode?: string | null;
    compare_view?: CompareView | null;
    selection_bucket_id?: string | null;
    preset_id?: string | null;
    focus_paper_id?: string | null;
    focus_theme?: string | null;
    focus_section_id?: string | null;
    focus_section_title?: string | null;
    preferred_report_side?: "left" | "right" | "both" | null;
    selection_source?: SelectionSource | null;
    selected_paper_ids?: string | null;
    seed_section?: boolean;
  },
) {
  return fetchJson<RunCompareContextResponse>(`/runs/${runId}/compare-context${buildQuery(params)}`);
}

export function getWorkspaceSuggestions(
  runId: string,
  params: {
    editing_session_id?: string | null;
    workspace_view_id?: string | null;
    compare_partner_run_id?: string | null;
    compare_mode?: string | null;
    compare_view?: CompareView | null;
    selection_bucket_id?: string | null;
    preset_id?: string | null;
    focus_theme?: string | null;
    focus_section_id?: string | null;
    focus_section_title?: string | null;
    preferred_report_side?: "left" | "right" | "both" | null;
    selection_source?: SelectionSource | null;
    selected_paper_ids?: string | null;
  },
) {
  return fetchJson<WorkspaceSuggestionsResponse>(`/runs/${runId}/workspace-suggestions${buildQuery(params)}`);
}

export function previewWorkspaceAction(runId: string, payload: WorkspaceActionRequest) {
  return fetchJson<WorkspaceActionPreviewResponse>(`/runs/${runId}/workspace-actions/preview`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function applyWorkspaceAction(
  runId: string,
  payload: WorkspaceActionRequest & { confirm_destructive?: boolean },
) {
  return fetchJson<WorkspaceActionApplyResponse>(`/runs/${runId}/workspace-actions/apply`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getStagedWorkspaceActions(runId: string, params: { editing_session_id?: string | null } = {}) {
  return fetchJson<WorkspaceStagedActionsResponse>(`/runs/${runId}/workspace-actions/staged${buildQuery(params)}`);
}

export function getStagedWorkspaceActionPatch(runId: string, actionId: string, params: { editing_session_id?: string | null } = {}) {
  return fetchJson<StructuredPatch>(`/runs/${runId}/workspace-actions/staged/${actionId}/patch${buildQuery(params)}`);
}

export function stageWorkspaceAction(runId: string, payload: WorkspaceActionRequest) {
  return fetchJson<WorkspaceStagedActionsResponse>(`/runs/${runId}/workspace-actions/stage`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function applyStagedWorkspaceActions(
  runId: string,
  payload: {
    editing_session_id?: string | null;
    action_ids?: string[];
    apply_all?: boolean;
    confirm_destructive_action_ids?: string[];
  },
) {
  return fetchJson<WorkspaceActionApplyStagedResponse>(`/runs/${runId}/workspace-actions/apply-staged`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function preflightApplyStagedWorkspaceActions(
  runId: string,
  payload: {
    editing_session_id?: string | null;
    action_ids?: string[];
    apply_all?: boolean;
    confirm_destructive_action_ids?: string[];
  },
) {
  return fetchJson<WorkspaceApplyStagedPreflightResponse>(`/runs/${runId}/workspace-actions/preflight-apply-staged`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function reorderStagedWorkspaceActions(
  runId: string,
  payload: {
    editing_session_id?: string | null;
    ordered_action_ids: string[];
  },
) {
  return fetchJson<WorkspaceStagedActionsResponse>(`/runs/${runId}/workspace-actions/staged/reorder`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteStagedWorkspaceAction(runId: string, actionId: string, params: { editing_session_id?: string | null } = {}) {
  return fetchJson<WorkspaceStagedActionsResponse>(
    `/runs/${runId}/workspace-actions/staged/${actionId}${buildQuery(params)}`,
    {
      method: "DELETE",
    },
  );
}

export function getWorkspaceViews(runId: string) {
  return fetchJson<WorkspaceViewsResponse>(`/runs/${runId}/workspace-views`);
}

export function createWorkspaceView(runId: string, payload: WorkspaceViewCreateRequest) {
  return fetchJson<WorkspaceViewsResponse>(`/runs/${runId}/workspace-views`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deleteWorkspaceView(runId: string, viewId: string) {
  return fetchJson<{ run_id: string; view_id: string; status: "deleted" }>(`/runs/${runId}/workspace-views/${viewId}`, {
    method: "DELETE",
  });
}

export function getEditingSessions(runId: string) {
  return fetchJson<EditingSessionsResponse>(`/runs/${runId}/editing-sessions`);
}

export function createEditingSession(runId: string, payload: EditingSessionCreateRequest) {
  return fetchJson<EditingSession>(`/runs/${runId}/editing-sessions`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getEditingSession(runId: string, sessionId: string) {
  return fetchJson<EditingSession>(`/runs/${runId}/editing-sessions/${sessionId}`);
}

export function getEditingSessionStaleness(runId: string, sessionId: string) {
  return fetchJson<EditingSessionStaleness>(`/runs/${runId}/editing-sessions/${sessionId}/staleness`);
}

export function recoverEditingSession(
  runId: string,
  sessionId: string,
  payload: {
    mode: "refresh_session_metadata" | "rebase_staged_candidates" | "discard_staged_candidates";
  },
) {
  return fetchJson<EditingSessionRecoverResponse>(`/runs/${runId}/editing-sessions/${sessionId}/recover`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function previewEditingSessionRecovery(
  runId: string,
  sessionId: string,
  payload: {
    mode: "refresh_session_metadata" | "rebase_staged_candidates" | "discard_staged_candidates";
  },
) {
  return fetchJson<EditingSessionRecoveryPreviewResponse>(`/runs/${runId}/editing-sessions/${sessionId}/recover-preview`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deleteEditingSession(runId: string, sessionId: string) {
  return fetchJson<{ run_id: string; session_id: string; status: "deleted" }>(`/runs/${runId}/editing-sessions/${sessionId}`, {
    method: "DELETE",
  });
}

export function getOutlineActionHistory(runId: string) {
  return fetchJson<OutlineActionHistoryResponse>(`/runs/${runId}/outline-action-history`);
}

export function getOutlineActionHistoryDiff(
  runId: string,
  params: {
    from_action_id?: string | null;
    to_action_id?: string | null;
    against_current_draft?: boolean;
  } = {},
) {
  return fetchJson<OutlineActionHistoryDiffResponse>(`/runs/${runId}/outline-action-history/diff${buildQuery(params)}`);
}

export function getOutlineActionHistoryFocusContext(runId: string, actionId: string) {
  return fetchJson<OutlineActionHistoryFocusContext>(`/runs/${runId}/outline-action-history/${actionId}/focus-context`);
}

export function compareRuns(leftRunId: string, rightRunId: string) {
  return fetchJson<CompareResponse>(`/compare${buildQuery({ runA: leftRunId, runB: rightRunId })}`);
}

export function getCompareReportDetail(leftRunId: string, rightRunId: string) {
  return fetchJson<CompareReportDetailResponse>(
    `/compare/report${buildQuery({ runA: leftRunId, runB: rightRunId })}`,
  );
}

export function getRunPapers(
  runId: string,
  params: {
    page?: number;
    page_size?: number;
    q?: string;
    status?: string | null;
    theme?: string | null;
    score_band?: string | null;
    provider?: string | null;
    dedup?: string;
  } = {},
) {
  return fetchJson<PaperListResponse>(`/runs/${runId}/papers${buildQuery(params)}`);
}

export function getRunPaperDetail(runId: string, paperId: string) {
  return fetchJson<PaperDetailResponse>(`/runs/${runId}/papers/${paperId}`);
}

export function getRunPaperReview(runId: string, paperId: string) {
  return fetchJson<ReviewResponse>(`/runs/${runId}/papers/${paperId}/review`);
}

export function updateRunPaperReviewAction(runId: string, paperId: string, payload: PaperReviewActionPatch) {
  return fetchJson<PaperReviewAction>(`/runs/${runId}/papers/${paperId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function getRunReport(runId: string) {
  return fetchJson<{ run_id: string; path: string | null; content: string | null }>(`/runs/${runId}/report`);
}

export function getRunOutline(runId: string) {
  return fetchJson<OutlineResponse>(`/runs/${runId}/outline`);
}

export function patchRunOutline(runId: string, sections: OutlineSection[]) {
  return fetchJson<OutlineResponse>(`/runs/${runId}/outline`, {
    method: "PATCH",
    body: JSON.stringify({ sections }),
  });
}

export function getRunPreview(runId: string) {
  return fetchJson<PreviewResponse>(`/runs/${runId}/preview`);
}

export function getRunExports(runId: string) {
  return fetchJson<ExportListResponse>(`/runs/${runId}/exports`);
}

export function getReportVersions(runId: string) {
  return fetchJson<ReportVersionsResponse>(`/runs/${runId}/report/versions`);
}

export function getReportVersionDetail(reportId: string, versionId: string) {
  return fetchJson<ReportVersionDetailResponse>(
    `/api/reports/${encodeURIComponent(reportId)}/versions/${encodeURIComponent(versionId)}`,
  );
}

export function getReportVersionDiff(
  runId: string,
  params: {
    from_version_id?: string | null;
    to_version_id?: string | null;
  } = {},
) {
  return fetchJson<ReportVersionDiffResponse>(`/runs/${runId}/report/versions/diff${buildQuery(params)}`);
}

export function getRunCitations(runId: string) {
  return fetchJson<CitationTraceResponse>(`/runs/${runId}/citations`);
}

export function exportRun(runId: string, format: "report_markdown" | "report_pdf" | "journal_pdf") {
  return fetchJson<{ status: string; format: string; output: string; artifact?: ExportArtifact; version?: ReportVersion }>(`/runs/${runId}/export`, {
    method: "POST",
    body: JSON.stringify({ format }),
  });
}

export function buildExportDownloadUrl(runId: string, exportId: string) {
  return `${API_BASE}/runs/${runId}/exports/${exportId}/download`;
}

export function rerunRun(runId: string, payload: RunRerunRequest) {
  return fetchJson<RunRerunResponse>(`/runs/${runId}/rerun`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getRerunPresets(runId: string) {
  return fetchJson<RerunPresetListResponse>(`/runs/${runId}/rerun-presets`);
}

export function createRerunPreset(runId: string, payload: RerunPresetCreateRequest) {
  return fetchJson<RerunPresetListResponse>(`/runs/${runId}/rerun-presets`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deleteRerunPreset(runId: string, presetId: string) {
  return fetchJson<{ run_id: string; preset_id: string; status: "deleted" }>(`/runs/${runId}/rerun-presets/${presetId}`, {
    method: "DELETE",
  });
}

export function createRun(payload: {
  topic: string;
  queries: string[];
  categories: string[];
  max_results: number | null;
  top_papers: number;
  weekly_top: number;
}) {
  return fetchJson<RunRecord>("/runs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getDashboardOverview(params: { topics?: string[] } = {}) {
  const topics = (params.topics ?? []).filter(Boolean).join(",");
  return fetchJson<DashboardOverviewResponse>(`/dashboard/overview${buildQuery({ topics })}`);
}

export function getPrototypeStatus() {
  return fetchJson<PrototypeStatusResponse>("/prototype/status");
}

export function getPrototypeWorkspace() {
  return fetchJson<PrototypeWorkspace>("/prototype/workspace");
}

export function getPrototypeProjectBundles() {
  return fetchJson<PrototypeBundleList>("/prototype/workspace/bundles");
}

export function previewPrototypeProjectBundle(selectedTopicIds: string[]) {
  return fetchJson<PrototypeBundlePreview>("/prototype/workspace/bundles/export/preview", {
    method: "POST",
    body: JSON.stringify({ selected_topic_ids: selectedTopicIds }),
  });
}

export function exportPrototypeProjectBundle(selectedTopicIds: string[], previewDigest: string) {
  return fetchJson<PrototypeBundlePreview>("/prototype/workspace/bundles/export", {
    method: "POST",
    body: JSON.stringify({
      selected_topic_ids: selectedTopicIds,
      preview_digest: previewDigest,
      confirmed: true,
    }),
  });
}

export function buildPrototypeProjectBundleDownloadUrl(downloadUrl: string) {
  return `${API_BASE}${downloadUrl}`;
}

export async function uploadPrototypeProjectBundle(file: File) {
  const safeFilename = file.name.replace(/[^\x20-\x7e]/g, "_");
  const response = await fetch(`${API_BASE}/prototype/workspace/bundles/imports`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/vnd.xrml.research-project+zip",
      "X-XRML-Bundle-Filename": safeFilename,
    },
    body: file,
  });
  if (!response.ok) {
    const body = await response.text();
    throw new ApiError(body || `Request failed: ${response.status}`, response.status, body);
  }
  return response.json() as Promise<PrototypeBundlePreview>;
}

export function confirmPrototypeProjectBundleImport(importId: string, previewDigest: string) {
  return fetchJson<PrototypeBundlePreview>(
    `/prototype/workspace/bundles/imports/${encodeURIComponent(importId)}/confirm`,
    {
      method: "POST",
      body: JSON.stringify({ preview_digest: previewDigest, confirmed: true }),
    },
  );
}

export function getPrototypeProjectBundleDetail(projectId: string) {
  return fetchJson<PrototypeBundleProjectDetail>(
    `/prototype/workspace/bundles/projects/${encodeURIComponent(projectId)}`,
  );
}

export function getPrototypeProjectBundleReportVersion(runId: string, versionId: string) {
  return fetchJson<PrototypePortableReportVersion>(
    `/prototype/workspace/bundles/runs/${encodeURIComponent(runId)}/report-versions/${encodeURIComponent(versionId)}`,
  );
}

export function getPrototypeProjectBundleEvidence(runId: string, evidenceId: string) {
  return fetchJson<PrototypePortableEvidence>(
    `/prototype/workspace/bundles/runs/${encodeURIComponent(runId)}/evidence/${encodeURIComponent(evidenceId)}`,
  );
}

export function getPrototypeWorkspaceTopic(topicId: string) {
  return fetchJson<PrototypeWorkspaceTopic>(`/prototype/workspace/topics/${encodeURIComponent(topicId)}`);
}

export function createPrototypeWorkspaceTopic(payload: {
  display_name: string;
  description: string;
  research_question: string;
  topic_kind: PrototypeWorkspaceTopicKind;
  source_policy: PrototypeWorkspaceSourcePolicy;
  language: string;
  publication_date_cutoff: string | null;
  license_policy: string;
  refresh_mode: string;
}) {
  return fetchJson<PrototypeWorkspaceTopic>("/prototype/workspace/topics", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updatePrototypeWorkspaceTopic(
  topicId: string,
  payload: Partial<{
    display_name: string;
    description: string;
    research_question: string;
    source_policy: PrototypeWorkspaceSourcePolicy;
    language: string;
    publication_date_cutoff: string | null;
    license_policy: string;
    refresh_mode: string;
    provider_posture: PrototypeWorkspaceProviderPosture;
    archived: boolean;
  }>,
) {
  return fetchJson<PrototypeWorkspaceTopic>(`/prototype/workspace/topics/${encodeURIComponent(topicId)}/settings`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function previewPrototypeWorkspaceCuration(
  topicId: string,
  payload: {
    source_snapshot_id: string;
    corpus_item_id: string;
    requested_state: PrototypeWorkspaceCurationState;
    reason_code: PrototypeWorkspaceCurationReason;
    note?: string | null;
  },
) {
  return fetchJson<PrototypeWorkspaceChangeset>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/curation/preview`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function applyPrototypeWorkspaceChangeset(topicId: string, changesetId: string) {
  return fetchJson<PrototypeWorkspaceSnapshot>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/changesets/${encodeURIComponent(changesetId)}/apply`,
    { method: "POST", body: JSON.stringify({ confirmed: true }) },
  );
}

export function discardPrototypeWorkspaceChangeset(topicId: string, changesetId: string) {
  return fetchJson<PrototypeWorkspaceChangeset>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/changesets/${encodeURIComponent(changesetId)}/discard`,
    { method: "POST" },
  );
}

export function comparePrototypeWorkspaceSnapshots(topicId: string, fromSnapshotId: string, toSnapshotId: string) {
  return fetchJson<PrototypeWorkspaceSnapshotComparison>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/snapshots/compare${buildQuery({
      from_snapshot_id: fromSnapshotId,
      to_snapshot_id: toSnapshotId,
    })}`,
  );
}

export function startPrototypeWorkspaceTopicRun(topicId: string, snapshotId?: string | null) {
  return fetchJson<PrototypeWorkspaceRun>(`/prototype/workspace/topics/${encodeURIComponent(topicId)}/runs`, {
    method: "POST",
    body: JSON.stringify({ snapshot_id: snapshotId ?? null, confirmed: true }),
  });
}

export function acquirePrototypeWorkspaceRealCorpus(topicId: string) {
  return fetchJson<{ run: PrototypeRunSummary; snapshot: PrototypeWorkspaceSnapshot; provider_calls: 0 }>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/initial-acquisition`,
    { method: "POST", body: JSON.stringify({ confirmed: true, network_enabled: true }) },
  );
}

export function refreshPrototypeWorkspaceRealCorpus(topicId: string) {
  return fetchJson<{ refresh: PrototypeWorkspaceRefresh; changeset: PrototypeWorkspaceChangeset }>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/refreshes`,
    { method: "POST", body: JSON.stringify({ confirmed: true }) },
  );
}

export function updatePrototypeLivingSchedule(
  topicId: string,
  payload: {
    enabled: boolean;
    cadence: PrototypeLivingCadence;
    time_zone: string;
    local_time: string;
    network_policy: PrototypeLivingSchedule["network_policy"];
    briefing_policy: PrototypeLivingSchedule["briefing_policy"];
  },
) {
  return fetchJson<PrototypeLivingSchedule>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/schedule`,
    { method: "PUT", body: JSON.stringify(payload) },
  );
}

export function dispatchPrototypeLivingSchedule(topicId: string) {
  return fetchJson<Record<string, unknown>>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/schedule/dispatch`,
    { method: "POST", body: JSON.stringify({ confirmed: true }) },
  );
}

export function createPrototypeCurationDeltaBriefing(
  topicId: string,
  beforeSnapshotId: string,
  afterSnapshotId: string,
) {
  return fetchJson<PrototypeLivingBriefing>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/briefings/curation-delta`,
    {
      method: "POST",
      body: JSON.stringify({ before_snapshot_id: beforeSnapshotId, after_snapshot_id: afterSnapshotId }),
    },
  );
}

export function comparePrototypeLivingBriefings(topicId: string, beforeBriefingId: string, afterBriefingId: string) {
  return fetchJson<PrototypeLivingBriefingComparison>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/briefings/compare${buildQuery({
      before_briefing_id: beforeBriefingId,
      after_briefing_id: afterBriefingId,
    })}`,
  );
}

export function exportPrototypeLivingBriefing(topicId: string, briefingId: string) {
  return fetchJson<{ briefing_id: string; topic_id: string; format: "markdown"; content: string; content_digest: string; provider_calls: 0 }>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/briefings/${encodeURIComponent(briefingId)}/export`,
  );
}

export function updatePrototypeLivingAttention(
  topicId: string,
  briefingId: string,
  changeEventId: string,
  action: "acknowledge" | "dismiss",
) {
  return fetchJson<Record<string, unknown>>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/briefings/${encodeURIComponent(briefingId)}/attention`,
    { method: "POST", body: JSON.stringify({ change_event_id: changeEventId, action }) },
  );
}

export function createPrototypeGapAnalysis(topicId: string, snapshotId: string) {
  return fetchJson<PrototypeGapAnalysis>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/gap-analyses`,
    { method: "POST", body: JSON.stringify({ snapshot_id: snapshotId }) },
  );
}

export function getPrototypeGapAnalysis(topicId: string, analysisId: string) {
  return fetchJson<PrototypeGapAnalysis>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/gap-analyses/${encodeURIComponent(analysisId)}`,
  );
}

export function comparePrototypeGapAnalyses(topicId: string, beforeAnalysisId: string, afterAnalysisId: string) {
  return fetchJson<PrototypeGapComparison>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/gap-analyses/compare`,
    {
      method: "POST",
      body: JSON.stringify({ before_analysis_id: beforeAnalysisId, after_analysis_id: afterAnalysisId }),
    },
  );
}

export function exportPrototypeGapAnalysis(topicId: string, analysisId: string) {
  return fetchJson<{ filename: string; media_type: string; content: string; content_digest: string; provider_calls: 0; network_requests: 0 }>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/gap-analyses/${encodeURIComponent(analysisId)}/export`,
  );
}

export function comparePrototypeRecommendations(topicId: string, recommendationIds: string[]) {
  return fetchJson<{
    topic_id: string;
    recommendation_ids: string[];
    columns: Array<Record<string, unknown>>;
    provider_calls: 0;
    network_requests: 0;
  }>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/recommendations/compare`,
    { method: "POST", body: JSON.stringify({ recommendation_ids: recommendationIds }) },
  );
}

export function decidePrototypeRecommendation(
  topicId: string,
  recommendationId: string,
  requestedStatus: "accepted" | "held" | "rejected",
  reasonCode: string,
  boundedNote?: string | null,
) {
  return fetchJson<{ decision_event: PrototypeRecommendationDecision; recommendation: PrototypeResearchRecommendation; provider_calls: 0; network_requests: 0 }>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/recommendations/${encodeURIComponent(recommendationId)}/decisions`,
    {
      method: "POST",
      body: JSON.stringify({ requested_status: requestedStatus, reason_code: reasonCode, bounded_note: boundedNote ?? null }),
    },
  );
}

export function promotePrototypeRecommendation(topicId: string, recommendationId: string, confirmed: boolean) {
  return fetchJson<{
    duplicate: boolean;
    origin: PrototypeRecommendationOrigin;
    topic: PrototypeWorkspaceTopic;
    automatic_snapshot_count: 0;
    automatic_run_count: 0;
    automatic_report_count: 0;
    automatic_schedule_count: 0;
    provider_calls: 0;
    network_requests: 0;
  }>(
    `/prototype/workspace/topics/${encodeURIComponent(topicId)}/recommendations/${encodeURIComponent(recommendationId)}/promote`,
    { method: "POST", body: JSON.stringify({ confirmed }) },
  );
}

export function startPrototypeRun() {
  return fetchJson<PrototypeRunSummary>("/prototype/runs", { method: "POST" });
}

export function getPrototypeRealCorpusStatus() {
  return fetchJson<PrototypeRealCorpusStatus>("/prototype/real-corpus/status");
}

export function startPrototypeRealCorpusRun() {
  return fetchJson<PrototypeRunSummary & { real_corpus?: Record<string, unknown> }>("/prototype/real-corpus/runs", {
    method: "POST",
    body: JSON.stringify({ confirmed: true, network_enabled: true }),
  });
}

export function getPrototypeRealCorpusRecord(runId: string) {
  return fetchJson<PrototypeRealCorpusRecord>(`/prototype/runs/${runId}/real-corpus`);
}

export function getPrototypeRealCorpusEvidence(runId: string, evidenceId: string) {
  return fetchJson<PrototypeRealCorpusEvidenceDetail>(
    `/prototype/runs/${runId}/real-corpus/evidence/${encodeURIComponent(evidenceId)}`,
  );
}

export function replayPrototypeRealCorpusRun(runId: string) {
  return fetchJson<PrototypeRealCorpusReplay>(`/prototype/runs/${runId}/real-corpus/replay`, { method: "POST" });
}

export const PROTOTYPE_PROVIDER_ROUTE: PrototypeProviderRoute = {
  provider_id: "codex",
  execution_surface: "codex_sdk_python",
  stage: "report_draft_assist",
  internal_output_class: "report_draft",
  product_output_class: "weekly_report",
};

export function getPrototypeProviderProof(runId: string) {
  return fetchJson<PrototypeLiveProofStatus>(`/prototype/runs/${runId}/provider-proof`);
}

export function issuePrototypeProviderActivation(runId: string) {
  return fetchJson<PrototypeActivationResponse>(`/prototype/runs/${runId}/provider-proof/activation`, {
    method: "POST",
    body: JSON.stringify(PROTOTYPE_PROVIDER_ROUTE),
  });
}

export function generatePrototypeProviderDraft(runId: string, activationToken: string) {
  return fetchJson<PrototypeLiveExecution>(`/prototype/runs/${runId}/provider-proof/generate`, {
    method: "POST",
    body: JSON.stringify({ ...PROTOTYPE_PROVIDER_ROUTE, activation_token: activationToken, confirmed: true }),
  });
}

export function getPrototypeProviderCandidate(runId: string, executionId: string) {
  return fetchJson<PrototypeCandidatePreview>(`/prototype/runs/${runId}/provider-proof/${executionId}/candidate`);
}

export function acceptPrototypeProviderCandidate(runId: string, executionId: string) {
  return fetchJson<PrototypeLiveExecution>(`/prototype/runs/${runId}/provider-proof/${executionId}/accept`, {
    method: "POST",
    body: JSON.stringify({ confirmed: true }),
  });
}

export function rejectPrototypeProviderCandidate(runId: string, executionId: string) {
  return fetchJson<PrototypeLiveExecution>(`/prototype/runs/${runId}/provider-proof/${executionId}/reject`, {
    method: "POST",
    body: JSON.stringify({ confirmed: true }),
  });
}

export function comparePrototypeProviderReport(runId: string, executionId: string) {
  return fetchJson<ReportVersionDiffResponse>(`/prototype/runs/${runId}/provider-proof/${executionId}/comparison`);
}

export function previewPrototypeReportVersion(runId: string, versionId: string) {
  return fetchJson<{
    run_id: string;
    version_id: string;
    generation_mode: string;
    display_label: string;
    content: string;
    provider_id?: string | null;
    execution_record_id?: string | null;
    accepted_output_digest?: string | null;
    evidence_references: string[];
  }>(`/prototype/runs/${runId}/report-versions/${versionId}/preview`);
}

export function exportPrototypeReportVersion(runId: string, versionId: string) {
  return fetchJson<Record<string, unknown>>(`/prototype/runs/${runId}/report-versions/${versionId}/export`, {
    method: "POST",
  });
}

export function getTopicDashboard(params: { profile_id?: string | null } = {}) {
  return fetchJson<TopicDashboardResponse>(`/dashboard/topic-dashboard${buildQuery({ profile_id: params.profile_id ?? "" })}`);
}

export function getApiTopics() {
  return fetchJson<ApiTopicsResponse>("/api/topics");
}

function appendEvidenceList(search: URLSearchParams, key: string, values: readonly string[] | undefined) {
  if (values?.length) {
    search.set(key, values.join(","));
  }
}

export function getApiEvidenceSearch(
  params: EvidenceSearchParams = {},
  options: EvidenceApiRequestOptions = {},
) {
  const search = new URLSearchParams();
  if (params.q) {
    search.set("q", params.q);
  }
  appendEvidenceList(search, "topic_ids", params.topic_ids);
  appendEvidenceList(search, "run_ids", params.run_ids);
  appendEvidenceList(search, "corpus_kinds", params.corpus_kinds);
  appendEvidenceList(search, "review_sources", params.review_sources);
  appendEvidenceList(search, "provider_ids", params.provider_ids);
  appendEvidenceList(search, "review_levels", params.review_levels);
  if (params.has_evidence !== undefined && params.has_evidence !== null) {
    search.set("has_evidence", String(params.has_evidence));
  }
  if (params.limit !== undefined) {
    search.set("limit", String(params.limit));
  }
  if (params.cursor) {
    search.set("cursor", params.cursor);
  }
  const query = search.toString();
  return fetchJson<EvidenceSearchResponse>(`/api/evidence/search${query ? `?${query}` : ""}`, {
    signal: options.signal,
  });
}

export function getApiEvidenceItem(
  resultId: string,
  params: EvidenceItemDetailParams = {},
  options: EvidenceApiRequestOptions = {},
) {
  const search = new URLSearchParams();
  if (params.occurrence_run_id) {
    search.set("occurrence_run_id", params.occurrence_run_id);
  }
  const query = search.toString();
  return fetchJson<EvidenceItemDetailResponse>(
    `/api/evidence/items/${encodeURIComponent(resultId)}${query ? `?${query}` : ""}`,
    { signal: options.signal },
  );
}

export function searchGlobal(
  params: { q?: string; kinds?: GlobalSearchKind[]; topicIds?: string[]; limit?: number } = {},
  options: EvidenceApiRequestOptions = {},
) {
  const search = new URLSearchParams();
  if (params.q !== undefined) search.set("q", params.q);
  if (params.kinds?.length) search.set("kinds", params.kinds.join(","));
  if (params.topicIds?.length) search.set("topic_ids", params.topicIds.join(","));
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  const query = search.toString();
  return fetchJson<GlobalSearchResponse>(`/api/search${query ? `?${query}` : ""}`, { signal: options.signal });
}

export function getSearchHelp(options: EvidenceApiRequestOptions = {}) {
  return fetchJson<SearchHelpResponse>("/api/search/help", { signal: options.signal });
}

export function getApiTopicsOverview() {
  return fetchJson<ApiTopicsOverviewResponse>("/api/topics/overview");
}

export function getApiTopicRecommendations(topicId: string) {
  return fetchJson<ApiTopicRecommendationsResponse>(
    `/api/topics/${encodeURIComponent(topicId)}/recommendations`,
  );
}

export function getApiTopicDashboard(topicId: string) {
  return fetchJson<ApiTopicDashboardResponse>(`/api/topics/${encodeURIComponent(topicId)}/dashboard`);
}

export function getApiTopicPapers(topicId: string, params: { q?: string; provider?: string | null; limit?: number } = {}) {
  return fetchJson<ApiTopicPapersResponse>(
    `/api/topics/${encodeURIComponent(topicId)}/papers${buildQuery({
      q: params.q ?? "",
      provider: params.provider ?? "",
      limit: params.limit ?? 50,
    })}`,
  );
}

export function getApiReports(params: { topicId?: string; limit?: number } = {}) {
  return fetchJson<ApiReportsResponse>(`/api/reports${buildQuery({ topicId: params.topicId ?? "", limit: params.limit ?? 50 })}`);
}

export function validateTopicConfiguration(topicId: string, payload: TopicConfigurationDraft) {
  return fetchJson<ApiTopicConfigurationValidation>(`/api/topics/${encodeURIComponent(topicId)}/configuration/validate`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function saveTopicConfiguration(topicId: string, payload: TopicConfigurationDraft) {
  return fetchJson<ApiTopicConfigurationValidation>(`/api/topics/${encodeURIComponent(topicId)}/configuration`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function getProviderConfigurationReadiness() {
  return fetchJson<ApiProviderReadinessList>("/api/provider-configurations/readiness");
}

export function getSourceProviders() {
  return fetchJson<SourceProviderRegistryResponse>("/api/source-providers");
}

export function searchDashboardRuns(params: { q?: string; topics?: string[]; limit?: number } = {}) {
  const topics = (params.topics ?? []).filter(Boolean).join(",");
  return fetchJson<DashboardRunSearchResponse>(`/dashboard/search-runs${buildQuery({ q: params.q ?? "", topics, limit: params.limit ?? 20 })}`);
}

export function searchDashboardPapers(params: { q?: string; themes?: string[]; limit?: number } = {}) {
  const themes = (params.themes ?? []).filter(Boolean).join(",");
  return fetchJson<DashboardPaperSearchResponse>(`/dashboard/search-papers${buildQuery({ q: params.q ?? "", themes, limit: params.limit ?? 20 })}`);
}

export function getTopicProfiles() {
  return fetchJson<TopicProfilesResponse>("/topic-profiles");
}

export function createTopicProfile(payload: {
  title: string;
  description?: string | null;
  topic: string;
  queries: string[];
  categories: string[];
  max_results: number;
  source_provider?: SourceProviderId;
  arxiv_sort_by?: "submittedDate" | "lastUpdatedDate" | "relevance";
  arxiv_sort_order?: "ascending" | "descending";
  source_settings?: TopicSourceSettings;
  top_papers: number;
  weekly_top: number;
  is_favorite?: boolean;
  default_collection_cadence?: string;
  default_report_cadence?: string;
  acquisition_language?: string;
  review_report_language?: string;
  acquisition_start_date?: string | null;
  acquisition_end_date?: string | null;
}) {
  return fetchJson<TopicProfilesResponse>("/topic-profiles", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateTopicProfile(
  profileId: string,
  payload: Partial<{
    title: string;
    description?: string | null;
    topic: string;
    queries: string[];
    categories: string[];
    max_results: number;
    source_provider: SourceProviderId;
    arxiv_sort_by: "submittedDate" | "lastUpdatedDate" | "relevance";
    arxiv_sort_order: "ascending" | "descending";
    source_settings: TopicSourceSettings;
    top_papers: number;
    weekly_top: number;
    is_favorite: boolean;
    status: "active" | "archived";
    default_collection_cadence: string;
    default_report_cadence: string;
    acquisition_language: string;
    review_report_language: string;
    acquisition_start_date: string | null;
    acquisition_end_date: string | null;
  }>,
) {
  return fetchJson<TopicProfilesResponse>(`/topic-profiles/${profileId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function deleteTopicProfile(profileId: string) {
  return fetchJson<{ profile_id: string; status: "deleted" }>(`/topic-profiles/${profileId}`, {
    method: "DELETE",
  });
}

export function startRunsFromProfiles(profileIds: string[]) {
  return fetchJson<DashboardStartRunsResponse>("/dashboard/start-runs", {
    method: "POST",
    body: JSON.stringify({ profile_ids: profileIds }),
  });
}

export function runTopicAction(profileId: string, action: TopicAction) {
  return fetchJson<DashboardTopicActionResponse>("/dashboard/topic-action", {
    method: "POST",
    body: JSON.stringify({ profile_id: profileId, action }),
  });
}

export function runApiTopicAction(topicId: string, action: Exclude<TopicAction, "full_pipeline">) {
  return fetchJson<DashboardTopicActionResponse>(`/api/topics/${encodeURIComponent(topicId)}/actions/${action}`, {
    method: "POST",
  });
}

export function getTopicSchedules() {
  return fetchJson<TopicSchedulesResponse>("/topic-schedules");
}

export function getTopicNotificationSettings(profileId: string) {
  return fetchJson<TopicNotificationSettings>(`/topic-notifications/settings${buildQuery({ profile_id: profileId })}`);
}

export function updateTopicNotificationSettings(profileId: string, payload: TopicNotificationSettingsUpdateRequest) {
  return fetchJson<TopicNotificationSettings>(`/topic-notifications/settings/${encodeURIComponent(profileId)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function getTopicNotifications(profileId: string, params: { limit?: number } = {}) {
  return fetchJson<TopicNotificationsResponse>(
    `/topic-notifications${buildQuery({ profile_id: profileId, limit: params.limit ?? 20 })}`,
  );
}

export function getTopicNotificationDeliveries(profileId: string, params: { limit?: number } = {}) {
  return fetchJson<TopicNotificationDeliveriesResponse>(
    `/topic-notifications/deliveries${buildQuery({ profile_id: profileId, limit: params.limit ?? 20 })}`,
  );
}

export function testTopicNotificationDelivery(profileId: string, channelId: "email" | "webhook") {
  return fetchJson<TopicNotificationTestDeliveryResponse>(
    `/topic-notifications/settings/${encodeURIComponent(profileId)}/test-delivery`,
    {
      method: "POST",
      body: JSON.stringify({ channel_id: channelId }),
    },
  );
}

export function updateTopicSchedule(
  scheduleId: string,
  payload: Partial<{
    title: string;
    task_type: ApiScheduledTaskType;
    enabled: boolean;
    cadence: string;
  }>,
) {
  return fetchJson<TopicSchedulesResponse>(`/topic-schedules/${scheduleId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function createTopicSchedule(payload: TopicScheduleCreateRequest) {
  return fetchJson<TopicSchedule>("/topic-schedules", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function deleteTopicSchedule(scheduleId: string) {
  return fetchJson<{ schedule_id: string; status: "deleted" }>(`/topic-schedules/${scheduleId}`, {
    method: "DELETE",
  });
}

export function updateApiTask(
  taskId: string,
  payload: Partial<{
    title: string;
    task_type: ApiScheduledTaskType;
    enabled: boolean;
    cadence: string;
  }>,
) {
  return fetchJson<ApiScheduledTask>(`/api/tasks/${encodeURIComponent(taskId)}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function runTopicScheduleNow(scheduleId: string) {
  return fetchJson<TopicScheduleRunNowResponse>(`/topic-schedules/${scheduleId}/run-now`, {
    method: "POST",
  });
}

export function runApiTaskNow(taskId: string) {
  return fetchJson<TopicScheduleRunNowResponse>(`/api/tasks/${encodeURIComponent(taskId)}/run`, {
    method: "POST",
  });
}

export function getSettingsStatus() {
  return fetchJson<SettingsStatusResponse>("/settings/status");
}

export function getReportDraftAssistStatus() {
  return fetchJson<ReportDraftAssistStatus>("/settings/report-draft-assist");
}

export function refreshReportDraftAssistStatus() {
  return fetchJson<ReportDraftAssistStatus>("/settings/report-draft-assist/refresh", { method: "POST" });
}

export function selectReportDraftAssistProvider(providerId: "deterministic_local" | "codex") {
  return fetchJson<ReportDraftAssistStatus>("/settings/report-draft-assist/select", {
    method: "POST",
    body: JSON.stringify({ provider_id: providerId }),
  });
}

export function getOAuthDryRunStatus() {
  return fetchJson<AuthFoundationStatus["oauthDryRun"]>("/auth/oauth/dry-run/status");
}

export function previewOAuthAuthorization(payload: OAuthAuthorizationDryRunRequest) {
  return fetchJson<OAuthAuthorizationDryRunResponse>("/auth/oauth/authorization/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function prepareOAuthAuthorization(payload: OAuthAuthorizationDryRunRequest) {
  return fetchJson<OAuthAuthorizationDryRunResponse>("/auth/oauth/authorization/prepare", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function validateOAuthCallbackDryRun(payload: OAuthCallbackDryRunRequest) {
  return fetchJson<OAuthCallbackDryRunResponse>("/auth/oauth/callback/dry-run", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getOAuthTokenExchangeStatus() {
  return fetchJson<AuthFoundationStatus["oauthTokenExchange"]>("/auth/oauth/token-exchange/status");
}

export function getOAuthAccountLinkStatus() {
  return fetchJson<OAuthAccountLinkHealth>("/auth/oauth/account-links/status");
}

export function getOAuthAccountLinkHealth() {
  return fetchJson<OAuthAccountLinkHealth>("/auth/oauth/account-links/health");
}

export function listOAuthAccountLinks() {
  return fetchJson<OAuthAccountLinkListResponse>("/auth/oauth/account-links");
}

export function previewOAuthAccountLink(payload: Partial<OAuthAccountLinkMetadata>) {
  return fetchJson<OAuthAccountLinkOperationResponse>("/auth/oauth/account-links/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function saveMockOAuthAccountLink(payload: Partial<OAuthAccountLinkMetadata>) {
  return fetchJson<OAuthAccountLinkOperationResponse>("/auth/oauth/account-links/mock", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function previewOAuthSessionBinding() {
  return fetchJson<OAuthSessionBinding>("/auth/oauth/session-binding/preview");
}

export function getOAuthSessionCapabilityPolicy() {
  return fetchJson<OAuthSessionCapabilityPolicy>("/auth/oauth/session-capability-policy");
}

export function getOAuthFlowTimeline() {
  return fetchJson<OAuthFlowTimeline>("/auth/oauth/flow-timeline");
}

export function getOAuthLiveExchangeReadiness() {
  return fetchJson<OAuthLiveExchangeReadiness>("/auth/oauth/live-exchange/readiness");
}

export function previewOAuthLiveExchangeReadiness(payload: OAuthLiveExchangeReadinessRequest) {
  return fetchJson<OAuthLiveExchangeReadiness>("/auth/oauth/live-exchange/readiness/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getOAuthReadinessRemediation() {
  return fetchJson<OAuthReadinessRemediation>("/auth/oauth/live-exchange/readiness/remediation");
}

export function previewOAuthReadinessRemediation(payload: OAuthLiveExchangeReadinessRequest) {
  return fetchJson<OAuthReadinessRemediation>("/auth/oauth/live-exchange/readiness/remediation/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getOAuthPreflightPackage() {
  return fetchJson<OAuthPreflightPackage>("/auth/oauth/preflight/package");
}

export function previewOAuthPreflightPackage(payload: OAuthLiveExchangeReadinessRequest) {
  return fetchJson<OAuthPreflightPackage>("/auth/oauth/preflight/package/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function previewOAuthOperatorAcceptanceChecklist(payload: OAuthOperatorAcceptanceChecklistRequest) {
  return fetchJson<OAuthOperatorAcceptanceChecklist>("/auth/oauth/preflight/checklist/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getOAuthLiveExchangePilotStatus() {
  return fetchJson<OAuthLiveExchangePilotStatus>("/auth/oauth/live-exchange/pilot/status");
}

export function getOAuthLivePilotEvidencePackage() {
  return fetchJson<OAuthLivePilotEvidencePackage>("/auth/oauth/live-exchange/evidence/package");
}

export function previewOAuthLivePilotEvidencePackage(payload: OAuthLiveExchangeReadinessRequest) {
  return fetchJson<OAuthLivePilotEvidencePackage>("/auth/oauth/live-exchange/evidence/package/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function previewOAuthLivePilotReplayVerification(payload: OAuthLivePilotEvidencePackage) {
  return fetchJson<OAuthLivePilotReplayVerification>("/auth/oauth/live-exchange/evidence/replay/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getOAuthRealProviderSmokeStatus() {
  return fetchJson<OAuthRealProviderSmokeStatus>("/auth/oauth/live-exchange/smoke/real/status");
}

export function getOAuthSmokeResultSummary() {
  return fetchJson<OAuthSmokeResultSummary>("/auth/oauth/live-exchange/smoke/result/summary");
}

export function getProviderOrchestrationSafetyMatrix() {
  return fetchJson<ProviderOrchestrationSafetyMatrix>("/auth/provider-orchestration/safety-matrix");
}

export function previewProviderOrchestrationDryRunPlan(payload: {
  workflowType?: ProviderOrchestrationWorkflow;
  topicId?: string | null;
  providerConfigId?: string | null;
}) {
  return fetchJson<ProviderOrchestrationDryRunPlan>("/auth/provider-orchestration/plan/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function previewProviderOrchestrationReplay(payload: ProviderOrchestrationDryRunPlan | Record<string, unknown>) {
  return fetchJson<ProviderOrchestrationReplayResult>("/auth/provider-orchestration/plan/replay/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getProviderOrchestrationPlanSummary() {
  return fetchJson<ProviderOrchestrationPlanSummary>("/auth/provider-orchestration/plan/summary");
}

export function previewProviderOrchestrationFixtureExecution(payload: {
  workflowType?: ProviderOrchestrationWorkflow;
  topicId?: string | null;
  providerConfigId?: string | null;
  fixtureOnly?: boolean;
  dryRun?: boolean;
  forceRefresh?: boolean;
  limit?: number;
}) {
  return fetchJson<ProviderOrchestrationExecutionResult>("/auth/provider-orchestration/fixture-execution/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function previewProviderOrchestrationFixtureReplay(payload: ProviderOrchestrationExecutionResult | Record<string, unknown>) {
  return fetchJson<ProviderOrchestrationExecutionReplay>("/auth/provider-orchestration/fixture-execution/replay/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getProviderOrchestrationFixtureExecutionSummary() {
  return fetchJson<ProviderOrchestrationExecutionSummary>("/auth/provider-orchestration/fixture-execution/summary");
}

export function revokeOAuthAccountLinkPlaceholder(accountLinkId: string) {
  return fetchJson<OAuthAccountLinkOperationResponse>(
    `/auth/oauth/account-links/${encodeURIComponent(accountLinkId)}/revoke-placeholder`,
    {
      method: "POST",
    },
  );
}

export function validateMockOAuthTokenExchange(payload: OAuthTokenExchangeRequest) {
  return fetchJson<OAuthTokenExchangeResponse>("/auth/oauth/token-exchange/mock/validate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function validateLiveOAuthTokenExchangeDisabled(payload: OAuthTokenExchangeRequest) {
  return fetchJson<OAuthTokenExchangeResponse>("/auth/oauth/token-exchange/live", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}
