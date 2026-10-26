from __future__ import annotations

import argparse
import json

from app.repositories.jsonl import JsonlIntegrityError
from app.services import ai_provider, ai_review, recommendations
from app.services.paper_review import refresh_reviews_for_topic, validate_evidence_store_for_topic

_STORE_PROVIDER_DRAFTS = "__store_provider_drafts__"


def _print_json(payload: dict) -> None:
    try:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    except UnicodeEncodeError:
        print(json.dumps(payload, ensure_ascii=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh deterministic paper reviews for a local topic profile.")
    parser.add_argument("--topic", required=True, help="Topic profile id, for example large-language-models.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum candidate papers to refresh.")
    parser.add_argument("--unique-limit", type=int, default=None, help="Maximum unique paper candidates to refresh.")
    parser.add_argument("--window-days", type=int, default=None, help="Only consider local rows from this recent window.")
    parser.add_argument("--min-relevance", type=float, default=None, help="Minimum topic relevance score for unique candidates.")
    parser.add_argument("--dry-run", action="store_true", help="Preview refresh counts without writing paper_reviews.")
    parser.add_argument("--force", action="store_true", help="Regenerate reviews that already exist.")
    parser.add_argument("--use-full-text", action="store_true", help="Read existing local text/pages artifacts for review evidence.")
    parser.add_argument("--max-text-chars", type=int, default=24000, help="Maximum local full-text characters to use per paper.")
    parser.add_argument("--prefer-existing-text", action="store_true", default=True, help="Prefer existing .txt artifacts before pages JSON.")
    parser.add_argument("--extract-existing-pdf", action="store_true", help="Optionally extract text from existing local PDFs only.")
    parser.add_argument("--fetch-missing-pdf", action="store_true", help="Explicitly download trusted missing PDFs from existing pdf_url metadata.")
    parser.add_argument("--download-limit", type=int, default=0, help="Maximum PDFs to download or include in a dry-run acquisition plan.")
    parser.add_argument("--provider", default="arxiv", help="Trusted source provider for PDF acquisition.")
    parser.add_argument("--allow-domain", action="append", default=None, help="Allowed PDF URL domain. Can be repeated.")
    parser.add_argument("--max-pdf-bytes", type=int, default=25_000_000, help="Maximum bytes allowed per PDF download.")
    parser.add_argument("--download-timeout", type=int, default=30, help="PDF download timeout in seconds.")
    parser.add_argument("--extract-after-fetch", action="store_true", help="Extract text locally after a successful PDF download.")
    parser.add_argument("--exclude-paper-id", action="append", default=None, help="Paper id to exclude from PDF acquisition planning. Can be repeated.")
    parser.add_argument("--target-paper-id", default=None, help="Restrict PDF acquisition planning to this paper id.")
    parser.add_argument(
        "--text-source-run-id",
        default=None,
        help="Explicit source run id to read local text/pages artifacts for scoped full-text review updates.",
    )
    parser.add_argument(
        "--allow-bounded-pdf-fetch",
        action="store_true",
        help="Allow live bounded PDF fetch only when ALLOW_BOUNDED_PDF_FETCH=1 is also present.",
    )
    parser.add_argument("--validate-evidence-store", action="store_true", help="Validate stored full-text review evidence without writing.")
    parser.add_argument("--repair-plan", action="store_true", help="Include a plan-only repair summary for evidence-store issues.")
    parser.add_argument("--export-ai-review-packets", default=None, help="Write provider-neutral AI review packets to this JSON path.")
    parser.add_argument("--ai-packet-limit", type=int, default=10, help="Maximum AI review packets to export.")
    parser.add_argument("--ai-packet-max-chars", type=int, default=12000, help="Maximum selected text characters per AI review packet.")
    parser.add_argument("--import-ai-review-drafts", default=None, help="Import and validate AI review draft JSON from this path.")
    parser.add_argument("--validate-ai-review-drafts", action="store_true", help="Validate AI review drafts before storing accepted drafts.")
    parser.add_argument("--run-ai-review-provider", action="store_true", help="Run a local AI review provider bridge for review packets.")
    parser.add_argument("--provider-acceptance-smoke", action="store_true", help="Run an optional CLI-only provider acceptance smoke summary.")
    parser.add_argument("--ai-provider", choices=["stub", "command"], default="stub", help="AI review provider adapter.")
    parser.add_argument("--ai-provider-command", default=None, help="Command adapter executable and args. Requires --allow-live-ai-provider.")
    parser.add_argument("--allow-live-ai-provider", action="store_true", help="Explicitly allow live local command provider execution.")
    parser.add_argument("--accept-provider-risk", action="store_true", help="Acknowledge operator responsibility for real command-provider execution.")
    parser.add_argument("--provider-operator-note", default=None, help="Optional operator note; stored only as a sha256 digest in provider run metadata.")
    parser.add_argument("--ai-provider-limit", type=int, default=5, help="Maximum packets to send to provider.")
    parser.add_argument("--ai-provider-timeout", type=int, default=30, help="Provider command timeout in seconds.")
    parser.add_argument("--ai-provider-max-output-chars", type=int, default=20000, help="Maximum provider stdout characters to parse.")
    parser.add_argument("--ai-provider-mode", choices=["dry-run", "stub", "live-command"], default=None, help="Provider invocation mode.")
    parser.add_argument(
        "--import-provider-drafts",
        nargs="?",
        const=_STORE_PROVIDER_DRAFTS,
        default=None,
        help="Store accepted provider drafts after validation, or import manual provider output from this path.",
    )
    parser.add_argument("--ai-provider-output-dir", default=None, help="Local ignored output directory for raw provider outputs.")
    parser.add_argument("--ai-provider-run-dir", default=None, help="Isolated ignored run directory for command-provider packet/output/stdout/stderr artifacts.")
    parser.add_argument("--ai-provider-force-refresh", action="store_true", help="Replace an existing accepted provider draft for the same paper/hash.")
    parser.add_argument("--ai-provider-skip-existing", dest="ai_provider_skip_existing", action="store_true", default=True, help="Skip papers that already have accepted AI drafts. Enabled by default.")
    parser.add_argument("--ai-provider-no-skip-existing", dest="ai_provider_skip_existing", action="store_false", help="Allow provider generation even when accepted AI drafts already exist.")
    parser.add_argument("--ai-provider-run-id", default=None, help="Explicit provider run id for audit metadata.")
    parser.add_argument("--ai-provider-origin", default=None, help="Explicit provider origin label, for example stub or live_command.")
    parser.add_argument("--prepare-provider-manual-run", action="store_true", help="Prepare bounded manual command-provider packets and previews without executing a provider.")
    parser.add_argument("--provider-manual-run-dir", default=None, help="Explicit ignored output directory for manual provider run artifacts.")
    parser.add_argument("--ai-provider-config", default=None, help="Provider configuration id for manual-run audit metadata.")
    parser.add_argument("--ai-provider-command-arg", action="append", default=None, help="One argument-list template item for manual command previews. Can be repeated.")
    parser.add_argument("--generate-recommendations", action="store_true", help="Generate deterministic research gap and topic recommendations.")
    parser.add_argument("--recommendation-limit", type=int, default=5, help="Maximum research gap/topic recommendations to return.")
    parser.add_argument("--recommendation-min-confidence", choices=["low", "medium", "high"], default="low", help="Minimum recommendation confidence to include.")
    parser.add_argument("--export-recommendations", default=None, help="Write recommendation JSON only when explicitly provided and not dry-run.")
    parser.add_argument("--recommendation-dry-run", action="store_true", help="Preview recommendations without writing export artifacts.")
    parser.add_argument("--include-abstract-only-recommendation-evidence", action="store_true", help="Allow abstract-only papers as weak recommendation evidence.")
    parser.add_argument("--recommendation-topic-scope", default=None, help="Optional recommendation topic scope label for local operator notes.")
    parser.add_argument("--audit-recommendations", action="store_true", help="Attach deterministic recommendation quality audit output.")
    parser.add_argument("--export-recommendation-audit", default=None, help="Write recommendation audit JSON only when explicitly provided and not dry-run.")
    parser.add_argument("--plan-recommendation-followup", action="store_true", help="Attach deterministic follow-up acquisition planning output.")
    parser.add_argument("--export-recommendation-followup", default=None, help="Write recommendation follow-up JSON only when explicitly provided and not dry-run.")
    parser.add_argument("--recommendation-followup-limit", type=int, default=10, help="Maximum follow-up acquisition candidates per recommendation.")
    parser.add_argument("--execute-recommendation-followup", action="store_true", help="Run an explicit bounded recommendation-driven PDF acquisition/review loop.")
    parser.add_argument("--recommendation-followup-download-limit", type=int, default=None, help="Maximum follow-up downloads. Defaults to --download-limit.")
    parser.add_argument("--recommendation-followup-review-limit", type=int, default=None, help="Maximum follow-up candidates to review after acquisition or artifact reuse.")
    parser.add_argument("--recommendation-followup-min-impact", choices=["low", "medium", "high"], default="low", help="Minimum expected confidence impact for follow-up execution.")
    parser.add_argument("--export-recommendation-followup-execution", default=None, help="Write recommendation follow-up execution JSON only when explicitly provided and not dry-run.")
    parser.add_argument("--compare-recommendation-quality-before-after", action="store_true", help="Attach before/after recommendation quality comparison after follow-up execution.")
    args = parser.parse_args(argv)
    if args.fetch_missing_pdf and args.download_limit <= 0:
        parser.error("--fetch-missing-pdf requires --download-limit greater than 0")
    if args.text_source_run_id and not args.target_paper_id:
        parser.error("--text-source-run-id requires --target-paper-id")
    if args.execute_recommendation_followup:
        if not args.generate_recommendations or not args.plan_recommendation_followup:
            parser.error("--execute-recommendation-followup requires --generate-recommendations and --plan-recommendation-followup")
        if not args.fetch_missing_pdf:
            parser.error("--execute-recommendation-followup requires --fetch-missing-pdf")
        if not args.allow_domain:
            parser.error("--execute-recommendation-followup requires at least one --allow-domain")
        if not args.extract_after_fetch:
            parser.error("--execute-recommendation-followup requires --extract-after-fetch")
    if args.import_ai_review_drafts and not args.validate_ai_review_drafts:
        parser.error("--import-ai-review-drafts requires --validate-ai-review-drafts")
    manual_import_path = (
        args.import_provider_drafts
        if isinstance(args.import_provider_drafts, str) and args.import_provider_drafts != _STORE_PROVIDER_DRAFTS
        else None
    )
    if manual_import_path and not args.validate_ai_review_drafts:
        parser.error("--import-provider-drafts <path> requires --validate-ai-review-drafts")
    if args.prepare_provider_manual_run and not args.provider_manual_run_dir:
        parser.error("--prepare-provider-manual-run requires --provider-manual-run-dir")
    if args.prepare_provider_manual_run:
        output = ai_provider.prepare_manual_provider_run(
            args.topic,
            output_dir=args.provider_manual_run_dir,
            limit=args.ai_provider_limit,
            provider_run_id=args.ai_provider_run_id,
            command_template=args.ai_provider_command_arg,
            dry_run=args.dry_run,
            max_packet_chars=args.ai_packet_max_chars,
        )
        if args.ai_provider_config:
            output["provider_config_id"] = args.ai_provider_config
        _print_json(output)
        return 0
    if manual_import_path:
        try:
            output = ai_provider.import_manual_provider_outputs(
                args.topic,
                manual_import_path,
                provider_run_id=args.ai_provider_run_id,
                dry_run=args.dry_run,
                force_refresh=args.ai_provider_force_refresh,
                skip_existing=args.ai_provider_skip_existing,
                provider_origin=args.ai_provider_origin or "manual_command_import",
            )
        except JsonlIntegrityError as exc:
            _print_json({"status": "failed", "error": exc.code})
            return 2
        _print_json(output)
        return 0
    if args.provider_acceptance_smoke:
        if not args.ai_provider_config:
            parser.error("--provider-acceptance-smoke requires --ai-provider-config")
        output = ai_provider.run_provider_acceptance_smoke(
            args.topic,
            provider_config=args.ai_provider_config,
            dry_run=args.dry_run,
            limit=args.ai_provider_limit,
            output_root=args.ai_provider_output_dir or ai_provider.DEFAULT_PROVIDER_OUTPUT_ROOT,
            run_dir=args.ai_provider_run_dir,
            allow_live_provider=args.allow_live_ai_provider,
            accept_provider_risk=args.accept_provider_risk,
            provider_operator_note=args.provider_operator_note,
            import_provider_drafts=args.import_provider_drafts == _STORE_PROVIDER_DRAFTS,
            force_refresh=args.ai_provider_force_refresh,
            skip_existing=args.ai_provider_skip_existing,
            provider_run_id=args.ai_provider_run_id,
            timeout_seconds=args.ai_provider_timeout,
            max_output_chars=args.ai_provider_max_output_chars,
        )
        _print_json(output)
        return 0
    if args.run_ai_review_provider:
        provider_mode = "dry_run" if args.dry_run else (args.ai_provider_mode or ("stub" if args.ai_provider == "stub" else "live-command"))
        provider_mode = provider_mode.replace("-", "_")
        output = ai_provider.run_ai_review_provider(
            args.topic,
            provider=args.ai_provider,
            mode=provider_mode,
            limit=args.ai_provider_limit,
            output_root=args.ai_provider_output_dir or ai_provider.DEFAULT_PROVIDER_OUTPUT_ROOT,
            run_dir=args.ai_provider_run_dir,
            import_provider_drafts=args.import_provider_drafts == _STORE_PROVIDER_DRAFTS,
            command=args.ai_provider_command_arg or args.ai_provider_command,
            provider_config=args.ai_provider_config,
            allow_live_provider=args.allow_live_ai_provider,
            timeout_seconds=args.ai_provider_timeout,
            max_output_chars=args.ai_provider_max_output_chars,
            force_refresh=args.ai_provider_force_refresh,
            skip_existing=args.ai_provider_skip_existing,
            provider_run_id=args.ai_provider_run_id,
            provider_origin=args.ai_provider_origin,
            accept_provider_risk=args.accept_provider_risk,
            provider_operator_note=args.provider_operator_note,
        )
        _print_json(output)
        return 0
    recommendation_requested = any(
        [
            args.generate_recommendations,
            args.recommendation_dry_run,
            args.audit_recommendations,
            args.export_recommendations,
            args.export_recommendation_audit,
            args.plan_recommendation_followup,
            args.export_recommendation_followup,
            args.execute_recommendation_followup,
            args.export_recommendation_followup_execution,
            args.compare_recommendation_quality_before_after,
        ]
    )
    if recommendation_requested:
        try:
            output = recommendations.write_recommendation_outputs(
                args.topic,
                args.export_recommendations,
                limit=args.recommendation_limit,
                min_confidence=args.recommendation_min_confidence,
                include_abstract_only=args.include_abstract_only_recommendation_evidence,
                dry_run=bool(args.dry_run or args.recommendation_dry_run),
                include_audit=args.audit_recommendations,
                audit_output_path=args.export_recommendation_audit,
                include_followup=args.plan_recommendation_followup,
                followup_output_path=args.export_recommendation_followup,
                followup_limit=args.recommendation_followup_limit,
                include_followup_execution=args.execute_recommendation_followup,
                followup_execution_output_path=args.export_recommendation_followup_execution,
                followup_download_limit=args.recommendation_followup_download_limit
                if args.recommendation_followup_download_limit is not None
                else args.download_limit,
                followup_review_limit=args.recommendation_followup_review_limit,
                followup_min_impact=args.recommendation_followup_min_impact,
                fetch_missing_pdf=args.fetch_missing_pdf,
                provider=args.provider,
                allow_domains=args.allow_domain,
                extract_after_fetch=args.extract_after_fetch,
                max_pdf_bytes=args.max_pdf_bytes,
                download_timeout=args.download_timeout,
                max_text_chars=args.max_text_chars,
                allow_bounded_pdf_fetch=args.allow_bounded_pdf_fetch,
                compare_recommendation_quality=args.compare_recommendation_quality_before_after,
            )
        except recommendations.RecommendationReadOnlySafetyError as exc:
            _print_json(
                {
                    "status": "recommendation_read_only_blocked",
                    "mode": "research_recommendation_generation",
                    "topic_id": args.topic,
                    "dry_run": bool(args.dry_run or args.recommendation_dry_run),
                    "writes": False,
                    "reason": str(exc),
                    "sidecar_guard": {
                        "checked": True,
                        "blocked": True,
                        "sidecars": exc.sidecars,
                    },
                }
            )
            return 2
        if args.recommendation_topic_scope:
            output["recommendation_topic_scope"] = args.recommendation_topic_scope
        _print_json(output)
        return 0
    if args.export_ai_review_packets:
        output = ai_review.write_ai_review_packets(
            args.topic,
            args.export_ai_review_packets,
            limit=args.ai_packet_limit,
            max_chars=args.ai_packet_max_chars,
            dry_run=args.dry_run,
        )
        _print_json(output)
        return 0
    if args.import_ai_review_drafts:
        output = ai_review.validate_and_import_ai_review_drafts(
            args.topic,
            args.import_ai_review_drafts,
            dry_run=args.dry_run,
        )
        _print_json(output)
        return 0
    if args.validate_evidence_store:
        validation = validate_evidence_store_for_topic(args.topic, include_repair_plan=args.repair_plan)
        _print_json(
            {
                "mode": "evidence_store_validation",
                "topic_id": validation["topic_id"],
                "dry_run": True,
                "use_full_text": bool(args.use_full_text),
                "evidence_store_validation": validation,
            }
        )
        return 0

    summary = refresh_reviews_for_topic(
        args.topic,
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        window_days=args.window_days,
        min_relevance=args.min_relevance,
        unique_limit=args.unique_limit,
        use_full_text=args.use_full_text,
        max_text_chars=args.max_text_chars,
        prefer_existing_text=args.prefer_existing_text,
        extract_existing_pdf=args.extract_existing_pdf,
        fetch_missing_pdf=args.fetch_missing_pdf,
        download_limit=args.download_limit,
        provider=args.provider,
        allow_domains=args.allow_domain,
        max_pdf_bytes=args.max_pdf_bytes,
        download_timeout=args.download_timeout,
        extract_after_fetch=args.extract_after_fetch,
        allow_bounded_pdf_fetch=args.allow_bounded_pdf_fetch,
        exclude_paper_ids=args.exclude_paper_id,
        target_paper_id=args.target_paper_id,
        text_source_run_id=args.text_source_run_id,
    )
    _print_json(summary)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
