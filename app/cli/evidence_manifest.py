from __future__ import annotations

import argparse
import json

from app.services import (
    evidence_manifest_admission,
    evidence_manifest_batch_dry_run,
    evidence_manifest_registry,
    evidence_manifest_registry_apply_preflight,
    evidence_manifest_registry_apply_harness,
    evidence_manifest_registry_commit_guard,
    evidence_manifest_registry_generate,
    evidence_manifest_registry_proposal,
    evidence_manifest_registry_write,
    evidence_manifest_replay,
    evidence_manifest_seed,
    evidence_manifest_verifier,
)


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_verification(payload: dict) -> None:
    print(f"status: {payload.get('status')}")
    print(f"paper_id: {payload.get('paper_id')}")
    print(f"topic_id: {payload.get('topic_id')}")
    print(f"safe_to_claim_local_verified: {str(payload.get('safe_to_claim_local_verified')).lower()}")
    print("safe_to_claim_clean_clone_reproducible: false")
    if payload.get("reasons"):
        print("reasons:")
        for reason in payload["reasons"]:
            print(f"- {reason}")
    print("note: local verification is not clean-clone offline reproducibility.")


def _print_plan(payload: dict) -> None:
    if payload.get("status") != "plan_only":
        _print_json(payload)
        return
    print("status: plan_only")
    print("bounded replay command:")
    print(payload["command"])
    print("note: this command was printed only; the CLI does not run network, set ALLOW_BOUNDED_PDF_FETCH, or write DB/storage.")
    print("note: future replay requires ALLOW_BOUNDED_PDF_FETCH=1 and explicit operator approval.")
    print("note: local verification is not clean-clone offline reproducibility.")


def _print_replay(payload: dict) -> None:
    print(f"status: {payload.get('status')}")
    print(f"paper_id: {payload.get('paper_id')}")
    print(f"topic_id: {payload.get('topic_id')}")
    print(f"replay_required: {str(payload.get('replay_required')).lower()}")
    print(f"write_attempted: {str(payload.get('write_attempted')).lower()}")
    print(f"network_attempted: {str(payload.get('network_attempted')).lower()}")
    print(f"safe_to_claim_reviewed: {str(payload.get('safe_to_claim_reviewed')).lower()}")
    print("safe_to_claim_clean_clone_reproducible: false")
    if payload.get("planned_command"):
        print("bounded replay command:")
        print(payload["planned_command"])
    if payload.get("reasons"):
        print("reasons:")
        for reason in payload["reasons"]:
            print(f"- {reason}")
    print("note: replay is operator-gated; local verification is not clean-clone offline reproducibility.")


def _print_seed(payload: dict) -> None:
    print(f"status: {payload.get('status')}")
    print(f"paper_id: {payload.get('paper_id')}")
    print(f"topic_id: {payload.get('topic_id')}")
    print(f"data_dir: {payload.get('data_dir')}")
    print(f"dry_run: {str(payload.get('dry_run')).lower()}")
    print(f"wrote_files: {str(payload.get('wrote_files')).lower()}")
    print(f"wrote_db: {str(payload.get('wrote_db')).lower()}")
    print(f"network_attempted: {str(payload.get('network_attempted')).lower()}")
    print(f"review_written: {str(payload.get('review_written')).lower()}")
    print(f"replay_executed: {str(payload.get('replay_executed')).lower()}")
    print(f"safe_to_claim_reviewed: {str(payload.get('safe_to_claim_reviewed')).lower()}")
    print(f"safe_to_claim_replay_planned: {str(payload.get('safe_to_claim_replay_planned')).lower()}")
    if payload.get("reasons"):
        print("reasons:")
        for reason in payload["reasons"]:
            print(f"- {reason}")
    print("note: seed metadata enables replay planning only; it is not reviewed evidence or replay success.")


def _print_admission(payload: dict) -> None:
    print(f"status: {payload.get('status')}")
    print(f"topic_id: {payload.get('topic_id')}")
    print(f"batch_size: {payload.get('batch_size')}")
    print(f"admitted_count: {payload.get('admitted_count')}")
    print(f"rejected_count: {payload.get('rejected_count')}")
    print(f"warning_count: {payload.get('warning_count')}")
    print(f"safe_to_run_dry_run_batch: {str(payload.get('safe_to_run_dry_run_batch')).lower()}")
    print("safe_to_run_live_batch: false")
    print("safe_to_claim_arbitrary_manifest_automation: false")
    if payload.get("rejections"):
        print("rejections:")
        for rejection in payload["rejections"]:
            print(f"- {rejection.get('manifest_path')}: {rejection.get('code')} - {rejection.get('message')}")
    print("note: admission check is read-only; it does not seed, replay, fetch PDFs, or write DB/storage.")


def _print_batch_dry_run(payload: dict) -> None:
    print(f"status: {payload.get('status')}")
    print(f"topic_id: {payload.get('topic_id')}")
    print(f"batch_size: {payload.get('batch_size')}")
    print(f"attempted_count: {payload.get('attempted_count')}")
    print(f"dry_run_planned_count: {payload.get('dry_run_planned_count')}")
    print(f"failed_count: {payload.get('failed_count')}")
    print(f"skipped_count: {payload.get('skipped_count')}")
    print(f"stopped_early: {str(payload.get('stopped_early')).lower()}")
    print(f"network_attempted_count: {payload.get('network_attempted_count')}")
    print(f"write_attempted_count: {payload.get('write_attempted_count')}")
    print(f"replay_verified_count: {payload.get('replay_verified_count')}")
    print(f"reviewed_claim_count: {payload.get('reviewed_claim_count')}")
    print(f"safe_to_claim_batch_dry_run_readiness: {str(payload.get('safe_to_claim_batch_dry_run_readiness')).lower()}")
    print("safe_to_claim_batch_live_replay_success: false")
    print("safe_to_claim_arbitrary_manifest_automation: false")
    if payload.get("first_failure_status"):
        print(f"first_failure_status: {payload.get('first_failure_status')}")
    print("note: batch dry-run seeds temp metadata and plans replay only; it does not run live fetch or write real DB/storage.")


def _print_registry(payload: dict) -> None:
    print(f"status: {payload.get('status')}")
    print(f"registry_path: {payload.get('registry_path')}")
    print(f"topic_id: {payload.get('topic_id')}")
    print(f"entry_count: {payload.get('entry_count')}")
    print(f"verified_count: {payload.get('verified_count')}")
    print(f"rejected_count: {payload.get('rejected_count')}")
    print(f"warning_count: {payload.get('warning_count')}")
    print(f"drift_count: {payload.get('drift_count')}")
    print(f"duplicate_count: {payload.get('duplicate_count')}")
    print(f"missing_manifest_count: {payload.get('missing_manifest_count')}")
    print(f"safe_to_use_registry_for_admission: {str(payload.get('safe_to_use_registry_for_admission')).lower()}")
    print(f"safe_to_claim_registry_integrity: {str(payload.get('safe_to_claim_registry_integrity')).lower()}")
    print("safe_to_claim_clean_clone_reproducible: false")
    print("safe_to_claim_arbitrary_manifest_automation: false")
    if payload.get("reasons"):
        print("reasons:")
        for reason in payload["reasons"]:
            print(f"- {reason.get('code')}: {reason.get('message')}")
    print("note: registry check is read-only; it does not create registry files, seed, fetch, or write DB/storage.")


def _print_registry_generate(payload: dict) -> None:
    print(f"status: {payload.get('status')}")
    print(f"topic_id: {payload.get('topic_id')}")
    print(f"manifest_count: {payload.get('manifest_count')}")
    print(f"generated_entry_count: {payload.get('generated_entry_count')}")
    print(f"registry_written: {str(payload.get('registry_written')).lower()}")
    print(f"write_attempted: {str(payload.get('write_attempted')).lower()}")
    print(f"network_attempted: {str(payload.get('network_attempted')).lower()}")
    print(f"safe_to_write_tracked_registry: {str(payload.get('safe_to_write_tracked_registry')).lower()}")
    print(f"safe_to_claim_registry_lifecycle_complete: {str(payload.get('safe_to_claim_registry_lifecycle_complete')).lower()}")
    print("safe_to_claim_clean_clone_reproducible: false")
    print("safe_to_claim_arbitrary_manifest_automation: false")
    if payload.get("reasons"):
        print("reasons:")
        for reason in payload["reasons"]:
            print(f"- {reason.get('code')}: {reason.get('message')}")
    print("note: registry generation is dry-run only; it writes no registry file and does not seed, replay, fetch, or write DB/storage.")


def _print_registry_proposal(payload: dict) -> None:
    print(f"status: {payload.get('status')}")
    print(f"topic_id: {payload.get('topic_id')}")
    print(f"proposal_path: {payload.get('proposal_path')}")
    print(f"proposal_status: {payload.get('proposal_status')}")
    print(f"output_path_type: {payload.get('output_path_type')}")
    print(f"proposal_written: {str(payload.get('proposal_written')).lower()}")
    print(f"tracked_registry_written: {str(payload.get('tracked_registry_written')).lower()}")
    print(f"target_registry_write_allowed: {str(payload.get('target_registry_write_allowed')).lower()}")
    print(f"registry_check_status: {payload.get('registry_check_status')}")
    print(f"network_attempted: {str(payload.get('network_attempted')).lower()}")
    print(f"write_attempted: {str(payload.get('write_attempted')).lower()}")
    print(f"db_storage_write_attempted: {str(payload.get('db_storage_write_attempted')).lower()}")
    print(f"live_replay_executed: {str(payload.get('live_replay_executed')).lower()}")
    print(f"safe_to_claim_registry_lifecycle_complete: {str(payload.get('safe_to_claim_registry_lifecycle_complete')).lower()}")
    print(f"safe_to_claim_tracked_registry_exists: {str(payload.get('safe_to_claim_tracked_registry_exists')).lower()}")
    print("safe_to_claim_clean_clone_reproducible: false")
    print("safe_to_claim_arbitrary_manifest_automation: false")
    if payload.get("reasons"):
        print("reasons:")
        for reason in payload["reasons"]:
            print(f"- {reason.get('code')}: {reason.get('message')}")
    print("note: proposal writer creates review-only proposal artifacts; it is not a tracked registry writer.")


def _print_registry_write(payload: dict) -> None:
    print(f"status: {payload.get('status')}")
    print(f"proposal_path: {payload.get('proposal_path')}")
    print(f"registry_path: {payload.get('registry_path')}")
    print(f"dry_run: {str(payload.get('dry_run')).lower()}")
    print(f"would_write: {str(payload.get('would_write')).lower()}")
    print(f"registry_written: {str(payload.get('registry_written')).lower()}")
    print(f"write_attempted: {str(payload.get('write_attempted')).lower()}")
    print(f"db_storage_write_attempted: {str(payload.get('db_storage_write_attempted')).lower()}")
    print(f"network_attempted: {str(payload.get('network_attempted')).lower()}")
    print(f"live_replay_executed: {str(payload.get('live_replay_executed')).lower()}")
    print(f"provider_oauth_external_ai_used: {str(payload.get('provider_oauth_external_ai_used')).lower()}")
    print(f"dashboard_trigger_used: {str(payload.get('dashboard_trigger_used')).lower()}")
    print(f"operator_flag_present: {str(payload.get('operator_flag_present')).lower()}")
    print(f"env_gate_present: {str(payload.get('env_gate_present')).lower()}")
    print(f"confirm_path_matched: {str(payload.get('confirm_path_matched')).lower()}")
    print(f"registry_check_status: {payload.get('registry_check_status')}")
    print(f"safe_to_write_tracked_registry: {str(payload.get('safe_to_write_tracked_registry')).lower()}")
    print(f"safe_to_claim_tracked_registry_exists: {str(payload.get('safe_to_claim_tracked_registry_exists')).lower()}")
    print(f"safe_to_claim_registry_lifecycle_complete: {str(payload.get('safe_to_claim_registry_lifecycle_complete')).lower()}")
    print("safe_to_claim_clean_clone_reproducible: false")
    if payload.get("reasons"):
        print("reasons:")
        for reason in payload["reasons"]:
            print(f"- {reason.get('code')}: {reason.get('message')}")
    print("note: registry write validation is dry-run only; it writes no tracked registry file.")


def _print_registry_commit_guard(payload: dict) -> None:
    print(f"status: {payload.get('status')}")
    print(f"mode: {payload.get('mode')}")
    print(f"repo_root: {payload.get('repo_root')}")
    print(f"checked_files_count: {payload.get('checked_files_count')}")
    print(f"staged_files: {payload.get('staged_files')}")
    print(f"tracked_modified_files: {payload.get('tracked_modified_files')}")
    print(f"untracked_files: {payload.get('untracked_files')}")
    print(f"forbidden_files: {payload.get('forbidden_files')}")
    print(f"warning_files: {payload.get('warning_files')}")
    print(f"allow_registry_file: {str(payload.get('allow_registry_file')).lower()}")
    print(f"allow_docs_changes: {str(payload.get('allow_docs_changes')).lower()}")
    print(f"safe_to_apply_registry_write: {str(payload.get('safe_to_apply_registry_write')).lower()}")
    print(f"safe_to_commit_registry: {str(payload.get('safe_to_commit_registry')).lower()}")
    print(f"registry_apply_performed: {str(payload.get('registry_apply_performed')).lower()}")
    print(f"registry_written: {str(payload.get('registry_written')).lower()}")
    print(f"write_attempted: {str(payload.get('write_attempted')).lower()}")
    print(f"db_storage_write_attempted: {str(payload.get('db_storage_write_attempted')).lower()}")
    print(f"network_attempted: {str(payload.get('network_attempted')).lower()}")
    if payload.get("reasons"):
        print("reasons:")
        for reason in payload["reasons"]:
            print(f"- {reason.get('code')}: {reason.get('message')}")
    print("note: registry commit guard is read-only; it does not stage, unstage, or write registry files.")


def _print_registry_apply_harness(payload: dict) -> None:
    print(f"status: {payload.get('status')}")
    print(f"temp_project_root: {payload.get('temp_project_root')}")
    print(f"proposal_path: {payload.get('proposal_path')}")
    print(f"registry_path: {payload.get('registry_path')}")
    print(f"registry_written: {str(payload.get('registry_written')).lower()}")
    print(f"write_attempted: {str(payload.get('write_attempted')).lower()}")
    print(f"candidate_written: {str(payload.get('candidate_written')).lower()}")
    print(f"atomic_replace_attempted: {str(payload.get('atomic_replace_attempted')).lower()}")
    print(f"atomic_replace_succeeded: {str(payload.get('atomic_replace_succeeded')).lower()}")
    print(f"backup_created: {str(payload.get('backup_created')).lower()}")
    print(f"rollback_attempted: {str(payload.get('rollback_attempted')).lower()}")
    print(f"rollback_succeeded: {str(payload.get('rollback_succeeded')).lower()}")
    print(f"post_write_check_status: {payload.get('post_write_check_status')}")
    print(f"network_attempted: {str(payload.get('network_attempted')).lower()}")
    print(f"db_storage_write_attempted: {str(payload.get('db_storage_write_attempted')).lower()}")
    print(f"live_replay_executed: {str(payload.get('live_replay_executed')).lower()}")
    print(f"provider_oauth_external_ai_used: {str(payload.get('provider_oauth_external_ai_used')).lower()}")
    print(f"dashboard_trigger_used: {str(payload.get('dashboard_trigger_used')).lower()}")
    print(f"safe_to_claim_real_project_registry_exists: {str(payload.get('safe_to_claim_real_project_registry_exists')).lower()}")
    print(f"safe_to_claim_harness_apply_verified: {str(payload.get('safe_to_claim_harness_apply_verified')).lower()}")
    print(f"safe_to_claim_registry_lifecycle_complete: {str(payload.get('safe_to_claim_registry_lifecycle_complete')).lower()}")
    if payload.get("reasons"):
        print("reasons:")
        for reason in payload["reasons"]:
            print(f"- {reason.get('code')}: {reason.get('message')}")
    print("note: registry apply harness writes only inside an explicit temp git repo; production registry apply remains not implemented.")


def _print_registry_apply_preflight(payload: dict) -> None:
    print(f"status: {payload.get('status')}")
    print(f"proposal_path: {payload.get('proposal_path')}")
    print(f"registry_path: {payload.get('registry_path')}")
    print(f"proposal_generated_at: {payload.get('proposal_generated_at')}")
    print(f"proposal_age_minutes: {payload.get('proposal_age_minutes')}")
    print(f"max_proposal_age_minutes: {payload.get('max_proposal_age_minutes')}")
    print(f"proposal_fresh: {str(payload.get('proposal_fresh')).lower()}")
    print(f"proposal_in_repo: {str(payload.get('proposal_in_repo')).lower()}")
    print(f"proposal_under_registry_dir: {str(payload.get('proposal_under_registry_dir')).lower()}")
    print(f"proposal_path_is_symlink: {str(payload.get('proposal_path_is_symlink')).lower()}")
    print(f"registry_absent: {str(payload.get('registry_absent')).lower()}")
    print(f"registry_check_status: {payload.get('registry_check_status')}")
    print(f"manifest_hashes_fresh: {str(payload.get('manifest_hashes_fresh')).lower()}")
    print(f"fresh_manifest_entries: {payload.get('fresh_manifest_entries')}")
    print(f"stale_manifest_entries: {len(payload.get('stale_manifest_entries') or [])}")
    print(f"writer_dry_run_status: {payload.get('writer_dry_run_status')}")
    print(f"commit_guard_status: {payload.get('commit_guard_status')}")
    print(f"known_untracked_files: {len(payload.get('known_untracked_files') or [])}")
    if payload.get("acknowledgement_status"):
        print(f"acknowledgement_status: {json.dumps(payload.get('acknowledgement_status'), sort_keys=True)}")
    print(f"preflight_ready: {str(payload.get('preflight_ready')).lower()}")
    print(f"eligible_for_operator_review: {str(payload.get('eligible_for_operator_review')).lower()}")
    print(f"would_apply: {str(payload.get('would_apply')).lower()}")
    print(f"registry_written: {str(payload.get('registry_written')).lower()}")
    print(f"write_attempted: {str(payload.get('write_attempted')).lower()}")
    print(f"safe_to_claim_real_registry_exists: {str(payload.get('safe_to_claim_real_registry_exists')).lower()}")
    print(f"safe_to_claim_registry_lifecycle_complete: {str(payload.get('safe_to_claim_registry_lifecycle_complete')).lower()}")
    if payload.get("reasons"):
        print("reasons:")
        for reason in payload["reasons"]:
            print(f"- {reason.get('code')}: {reason.get('message')}")
    print("note: production apply preflight is no-write; production registry apply remains not implemented.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify tracked paper evidence replay manifests without writing state.")
    parser.add_argument("--manifest", action="append", help="Path to a tracked paper evidence replay manifest JSON file. Repeat for batch checks.")
    parser.add_argument("--topic", default=None, help="Optional topic id assertion.")
    parser.add_argument("--paper-id", default=None, help="Optional paper id assertion.")
    parser.add_argument("--strict", action="store_true", help="Treat any verifier ambiguity as not locally verified.")
    parser.add_argument("--batch-policy-check", action="store_true", help="Read-only admission check for one or more manifests.")
    parser.add_argument("--batch-dry-run", action="store_true", help="Run admitted manifests through temp seed plus replay dry-run only.")
    parser.add_argument("--registry-check", action="store_true", help="Read-only check of a supplied manifest registry JSON file.")
    parser.add_argument("--registry-generate-dry-run", action="store_true", help="Generate proposed registry JSON in memory/stdout only.")
    parser.add_argument("--registry-write-proposal", action="store_true", help="Write a review-only registry proposal artifact to temp or approved non-registry path.")
    parser.add_argument("--registry-write", action="store_true", help="Dry-run validate a registry proposal for future tracked registry write readiness.")
    parser.add_argument("--registry-apply-preflight", action="store_true", help="Run no-write production registry apply preflight.")
    parser.add_argument("--registry-apply-harness", action="store_true", help="Apply a registry proposal only inside an isolated temp git repo test harness.")
    parser.add_argument("--registry-commit-guard", action="store_true", help="Read-only git status guard for future registry write/apply commit safety.")
    parser.add_argument("--registry", default=None, help="Path to a supplied registry JSON file for --registry-check.")
    parser.add_argument("--repo-root", default=None, help="Optional repository root for --registry-commit-guard.")
    parser.add_argument("--temp-project-root", default=None, help="Explicit temp git repo root for --registry-apply-harness.")
    parser.add_argument("--mode", default="pre-apply", help="Guard mode for --registry-commit-guard: pre-apply, post-apply, or pre-commit.")
    parser.add_argument("--proposal", default=None, help="Path to a registry proposal JSON file for --registry-write.")
    parser.add_argument("--output", default=None, help="Explicit non-registry output path for --registry-write-proposal.")
    parser.add_argument("--temp-output", action="store_true", help="Write --registry-write-proposal to a temp directory outside the repo.")
    parser.add_argument("--allow-output-overwrite", action="store_true", help="Allow --registry-write-proposal to overwrite an existing explicit output file.")
    parser.add_argument("--allow-existing-registry", action="store_true", help="Allow --registry-apply-harness to replace an existing temp registry.")
    parser.add_argument("--no-backup-existing-registry", action="store_true", help="Disable temp backup creation for --registry-apply-harness existing-registry replacement.")
    parser.add_argument("--allow-registry-file", action="store_true", help="Allow registry.json as an expected file for --registry-commit-guard future modes.")
    parser.add_argument("--allow-docs-changes", action="store_true", help="Allow docs changes for --registry-commit-guard future modes.")
    parser.add_argument("--block-known-untracked", action="store_true", help="Block known duplicate untracked files in --registry-commit-guard.")
    parser.add_argument("--operator-approve-tracked-registry-write", action="store_true", help="Operator approval gate for --registry-write dry-run readiness.")
    parser.add_argument("--confirm-registry-path", default=None, help="Exact target registry path confirmation for --registry-write.")
    parser.add_argument("--max-proposal-age-minutes", type=int, default=60, help="Maximum proposal age for --registry-apply-preflight.")
    parser.add_argument("--acknowledge-known-untracked", action="store_true", help="Acknowledge known duplicate untracked files for --registry-apply-preflight.")
    parser.add_argument("--acknowledge-commit-guard-warnings", action="store_true", help="Acknowledge commit guard warnings for --registry-apply-preflight.")
    parser.add_argument("--acknowledge-backup-policy", action="store_true", help="Acknowledge backup policy for --registry-apply-preflight.")
    parser.add_argument("--acknowledge-rollback-plan", action="store_true", help="Acknowledge rollback plan for --registry-apply-preflight.")
    parser.add_argument("--acknowledge-post-write-diff-review", action="store_true", help="Acknowledge post-write diff review for --registry-apply-preflight.")
    parser.add_argument("--acknowledge-commit-plan", action="store_true", help="Acknowledge commit plan for --registry-apply-preflight.")
    parser.add_argument("--acknowledge-push-deferred", action="store_true", help="Acknowledge push deferral for --registry-apply-preflight.")
    parser.add_argument("--generated-by-phase", default=None, help="Phase label for --registry-generate-dry-run metadata.")
    parser.add_argument("--proposal-id", default=None, help="Proposal id for --registry-write-proposal metadata.")
    parser.add_argument("--registry-id", default=None, help="Registry id for --registry-generate-dry-run metadata.")
    parser.add_argument("--allow-untracked-manifest", action="store_true", help="Allow explicitly selected untracked manifests in batch admission checks.")
    parser.add_argument("--skip-local-verified", action="store_true", help="Skip local verifier readiness during batch admission checks.")
    parser.add_argument("--temp-parent-dir", default=None, help="Optional temp parent directory for --batch-dry-run.")
    parser.add_argument("--keep-temp", action="store_true", help="Keep --batch-dry-run temp directories for inspection.")
    parser.add_argument("--verify-local", action="store_true", help="Verify the current local runtime state against the manifest.")
    parser.add_argument("--plan-replay", action="store_true", help="Print a future bounded replay command without executing it.")
    parser.add_argument("--replay-bounded", action="store_true", help="Run the operator-gated bounded replay workflow.")
    parser.add_argument("--seed-temp-metadata", action="store_true", help="Seed temp run/triage candidate metadata for replay planning only.")
    parser.add_argument("--data-dir", default=None, help="Temp XRML data directory for --seed-temp-metadata.")
    parser.add_argument("--allow-real-data-dir", action="store_true", help="Allow seeding into the real data directory; blocked by default.")
    parser.add_argument("--dry-run", action="store_true", help="For --replay-bounded or --seed-temp-metadata, plan only without network or writes.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args(argv)
    manifests = list(args.manifest or [])

    if args.registry_apply_preflight:
        result = evidence_manifest_registry_apply_preflight.run_production_apply_preflight(
            proposal_path=args.proposal or "",
            registry_path=args.registry or evidence_manifest_registry_apply_preflight.TARGET_REGISTRY_PATH,
            topic_id=args.topic,
            operator_approved=args.operator_approve_tracked_registry_write,
            confirm_registry_path=args.confirm_registry_path,
            max_proposal_age_minutes=args.max_proposal_age_minutes,
            acknowledge_known_untracked=args.acknowledge_known_untracked,
            acknowledge_commit_guard_warnings=args.acknowledge_commit_guard_warnings,
            backup_policy_acknowledged=args.acknowledge_backup_policy,
            rollback_plan_acknowledged=args.acknowledge_rollback_plan,
            post_write_diff_review_acknowledged=args.acknowledge_post_write_diff_review,
            commit_plan_acknowledged=args.acknowledge_commit_plan,
            push_deferred_acknowledged=args.acknowledge_push_deferred,
            project_root=args.repo_root,
            strict=args.strict,
        )
        if args.json:
            _print_json(result)
        else:
            _print_registry_apply_preflight(result)
        return 0 if result.get("status") == "production_apply_preflight_ready" else 1

    if args.registry_apply_harness:
        result = evidence_manifest_registry_apply_harness.apply_registry_in_temp_harness(
            proposal_path=args.proposal or "",
            temp_project_root=args.temp_project_root,
            registry_path=args.registry or evidence_manifest_registry_apply_harness.TARGET_REGISTRY_PATH,
            topic_id=args.topic,
            allow_existing_registry=args.allow_existing_registry,
            backup_existing_registry=not args.no_backup_existing_registry,
            operator_approved=args.operator_approve_tracked_registry_write,
            confirm_registry_path=args.confirm_registry_path,
            strict=args.strict,
        )
        if args.json:
            _print_json(result)
        else:
            _print_registry_apply_harness(result)
        return 0 if result.get("status") == "registry_apply_harness_applied" else 1

    if args.registry_commit_guard:
        result = evidence_manifest_registry_commit_guard.check_registry_commit_guard(
            repo_root=args.repo_root,
            expected_registry_path=args.registry or evidence_manifest_registry_commit_guard.TARGET_REGISTRY_PATH,
            mode=args.mode,
            allow_docs_changes=args.allow_docs_changes,
            allow_registry_file=args.allow_registry_file,
            allow_known_untracked=not args.block_known_untracked,
            strict=args.strict,
        )
        if args.json:
            _print_json(result)
        else:
            _print_registry_commit_guard(result)
        return 0 if result.get("status") in {"registry_commit_guard_passed", "registry_commit_guard_warning"} else 1

    if args.registry_write:
        result = evidence_manifest_registry_write.validate_registry_write_dry_run(
            proposal_path=args.proposal or "",
            registry_path=args.registry or "",
            topic_id=args.topic,
            dry_run=args.dry_run,
            operator_approved=args.operator_approve_tracked_registry_write,
            confirm_registry_path=args.confirm_registry_path,
            strict=args.strict,
        )
        if args.json:
            _print_json(result)
        else:
            _print_registry_write(result)
        return 0 if result.get("status") == "registry_write_dry_run_ready" else 1

    if args.registry_write_proposal:
        if not args.topic:
            parser.error("--registry-write-proposal requires --topic")
        result = evidence_manifest_registry_proposal.write_registry_proposal(
            manifests,
            topic_id=args.topic,
            output_path=args.output,
            temp_output=args.temp_output,
            allow_output_overwrite=args.allow_output_overwrite,
            generated_by_phase=args.generated_by_phase or evidence_manifest_registry_proposal.DEFAULT_GENERATED_BY_PHASE,
            proposal_id=args.proposal_id,
            allow_untracked_manifest=args.allow_untracked_manifest,
            require_local_verified=not args.skip_local_verified,
            strict=args.strict,
        )
        if args.json:
            _print_json(result)
        else:
            _print_registry_proposal(result)
        return 0 if result.get("status") == "proposal_written" else 1

    if args.registry_check:
        if not args.registry:
            parser.error("--registry-check requires --registry")
        if not args.topic:
            parser.error("--registry-check requires --topic")
        result = evidence_manifest_registry.check_manifest_registry(
            args.registry,
            topic_id=args.topic,
            strict=args.strict,
        )
        if args.json:
            _print_json(result)
        else:
            _print_registry(result)
        return 0 if result.get("status") in {"registry_verified", "registry_warning"} else 1

    if args.registry_generate_dry_run:
        if not args.topic:
            parser.error("--registry-generate-dry-run requires --topic")
        result = evidence_manifest_registry_generate.generate_registry_dry_run(
            manifests,
            topic_id=args.topic,
            generated_by_phase=args.generated_by_phase or evidence_manifest_registry_generate.DEFAULT_GENERATED_BY_PHASE,
            registry_id=args.registry_id,
            allow_untracked_manifest=args.allow_untracked_manifest,
            require_local_verified=not args.skip_local_verified,
            strict=args.strict,
        )
        if args.json:
            _print_json(result)
        else:
            _print_registry_generate(result)
        return 0 if result.get("status") == "registry_generate_dry_run_planned" else 1

    if args.batch_policy_check:
        if not args.topic:
            parser.error("--batch-policy-check requires --topic")
        result = evidence_manifest_admission.check_batch_admission(
            manifests,
            topic_id=args.topic,
            require_tracked=not args.allow_untracked_manifest,
            require_local_verified=not args.skip_local_verified,
            strict=args.strict,
        )
        if args.json:
            _print_json(result)
        else:
            _print_admission(result)
        return 0 if result.get("status") == "admitted" else 1

    if args.batch_dry_run:
        if not args.topic:
            parser.error("--batch-dry-run requires --topic")
        result = evidence_manifest_batch_dry_run.run_batch_dry_run(
            manifests,
            topic_id=args.topic,
            temp_parent_dir=args.temp_parent_dir,
            keep_temp=args.keep_temp,
            require_local_verified=not args.skip_local_verified,
            allow_untracked_manifest=args.allow_untracked_manifest,
            strict=args.strict,
        )
        if args.json:
            _print_json(result)
        else:
            _print_batch_dry_run(result)
        return 0 if result.get("status") == "batch_dry_run_planned" else 1

    if len(manifests) != 1:
        parser.error("exactly one --manifest is required unless a batch or registry command is used")
    manifest_path = manifests[0]

    if args.seed_temp_metadata:
        result = evidence_manifest_seed.seed_manifest_metadata(
            manifest_path,
            data_dir=args.data_dir,
            dry_run=args.dry_run,
            allow_real_data_dir=args.allow_real_data_dir,
        )
        if args.json:
            _print_json(result)
        else:
            _print_seed(result)
        return 0 if result.get("status") in {"seed_plan_created", "seed_written", "seed_already_present", "replay_plan_ready"} else 1

    if args.replay_bounded:
        result = evidence_manifest_replay.replay_manifest(manifest_path, dry_run=args.dry_run)
        if args.json:
            _print_json(result)
        else:
            _print_replay(result)
        return 0 if result.get("status") in {"local_already_verified", "replay_dry_run_planned", "replay_verified"} else 1

    if args.plan_replay:
        plan = evidence_manifest_verifier.build_replay_plan(manifest_path)
        if args.json:
            _print_json(plan)
        else:
            _print_plan(plan)
        return 0 if plan.get("status") == "plan_only" else 1

    result = evidence_manifest_verifier.verify_manifest(
        manifest_path,
        topic_id=args.topic,
        paper_id=args.paper_id,
        strict=args.strict,
    )
    if args.json:
        _print_json(result)
    else:
        _print_verification(result)
    return 0 if result.get("status") == "verified" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
