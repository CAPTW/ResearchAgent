#!/usr/bin/env python3
"""
run_all_to_pdf.py — 파이프라인 실행 후 v4(Korean Lab Style) PDF까지 원샷 생성

Windows 예시:
  python tools/run_all_to_pdf.py --top 3 --weekly-top 15 --experiment --rank 1 --run-date 2026-02-15 ^
    --font "C:\Windows\Fonts\malgun.ttf" --font-bold "C:\Windows\Fonts\malgunbd.ttf"
"""

import argparse
import subprocess
import sys
from datetime import datetime

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-date", default=None, help="YYYY-MM-DD (default: today local)")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--weekly-top", type=int, default=15)
    ap.add_argument(
        "--experiment",
        action="store_true",
        help="Generate a non-executable experiment design proposal scaffold.",
    )
    ap.add_argument("--rank", type=int, default=1)
    ap.add_argument("--font", default=None)
    ap.add_argument("--font-bold", default=None)
    ap.add_argument("--font-index", type=int, default=0)
    ap.add_argument("--triage-top", type=int, default=25)
    ap.add_argument("--out", default=None)
    ap.add_argument("--lab-name", default="XRML 연구실")
    ap.add_argument("--project-name", default="XR/AR × ML (Image/Video)")
    args = ap.parse_args()

    run_date = args.run_date or datetime.now().date().isoformat()

    # 1) Run pipeline
    pipeline_cmd = [
        sys.executable, "tools/pipeline.py",
        "--top", str(args.top),
        "--weekly-top", str(args.weekly_top),
        "--rank", str(args.rank),
    ]
    if args.experiment:
        pipeline_cmd.append("--experiment")

    print("[INFO] Running pipeline:", " ".join(pipeline_cmd))
    r = subprocess.run(pipeline_cmd)
    if r.returncode != 0:
        print(f"[ERROR] pipeline failed (exit={r.returncode}). PDF will not be generated.")
        sys.exit(r.returncode)

    # 2) Make PDF (v4)
    pdf_cmd = [
        sys.executable, "tools/make_report_pdf.py",
        "--run-date", run_date,
        "--top-papers", str(args.top),
        "--triage-top", str(args.triage_top),
        "--weekly-top", str(args.weekly_top),
        "--include-experiments",
        "--font-index", str(args.font_index),
        "--lab-name", args.lab_name,
        "--project-name", args.project_name,
    ]
    if args.font:
        pdf_cmd += ["--font", args.font]
    if args.font_bold:
        pdf_cmd += ["--font-bold", args.font_bold]
    if args.out:
        pdf_cmd += ["--out", args.out]

    print("[INFO] Generating PDF:", " ".join(pdf_cmd))
    r2 = subprocess.run(pdf_cmd)
    sys.exit(r2.returncode)

if __name__ == "__main__":
    main()
