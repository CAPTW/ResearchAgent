from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path


def _result(status: str, **payload: object) -> int:
    print(json.dumps({"status": status, **payload}, ensure_ascii=False, indent=2))
    return 0 if status in {"passed", "skipped"} else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Env-gated live smoke for the OpenAI research-plan provider path.")
    parser.add_argument("--source-run-id", default="20260403-100702-m3-validation-run")
    parser.add_argument("--provider", default=os.getenv("XRML_PROVIDER", "openai") or "openai")
    args = parser.parse_args(argv)

    provider = args.provider.strip().lower()
    api_key = (os.getenv("OPENAI_API_KEY", "") or "").strip()
    if provider == "disabled":
        return _result("skipped", reason="XRML_PROVIDER=disabled")
    if provider != "openai":
        return _result("skipped", reason=f"Unsupported provider for live smoke: {provider}")
    if not api_key:
        return _result("skipped", reason="OPENAI_API_KEY is not configured.")

    project_root = Path(__file__).resolve().parents[1]
    source_run_dir = project_root / "storage" / "runs" / args.source_run_id
    if not source_run_dir.exists():
        return _result("failed", reason=f"Source run fixture not found: {source_run_dir}")

    temp_root = Path(tempfile.mkdtemp(prefix="xrml-provider-live-smoke-"))
    storage_base = temp_root / "storage"
    temp_runs_root = storage_base / "runs"
    temp_runs_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_run_dir, temp_runs_root / args.source_run_id)

    os.environ["XRML_DATA_DIR"] = str(storage_base)
    os.environ["XRML_PROVIDER"] = provider

    from app.services import research_plan

    plan = research_plan.generate_run_research_plan(args.source_run_id)
    section_count = len(plan.sections)
    claim_count = sum(len(section.claims) for section in plan.sections)
    if plan.generator.mode != "provider":
        return _result(
            "failed",
            reason="Provider-backed generation did not stay on the provider path.",
            generator_mode=plan.generator.mode,
            warnings=plan.generator.warnings,
            temp_root=str(temp_root),
        )
    return _result(
        "passed",
        provider=plan.generator.provider_name,
        model=plan.generator.model,
        response_id=plan.generator.response_id,
        run_id=args.source_run_id,
        section_count=section_count,
        claim_count=claim_count,
        evidence_count=plan.evidence_count,
        report_path=str((temp_runs_root / args.source_run_id / "reports" / "research_plan.md")),
        temp_root=str(temp_root),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
