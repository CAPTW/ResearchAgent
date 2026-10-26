#!/usr/bin/env python3
"""Deprecated compatibility wrapper for pipeline -> journal JSON -> journal PDF."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-date", default=None)
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--weekly-top", type=int, default=15)
    parser.add_argument(
        "--experiment",
        action="store_true",
        help="Generate a non-executable experiment design proposal scaffold.",
    )
    parser.add_argument("--rank", type=int, default=1)
    parser.add_argument("--shortlist", type=int, default=12)
    parser.add_argument("--reviews", type=int, default=3)
    parser.add_argument("--font", default=None)
    parser.add_argument("--font-bold", default=None)
    parser.add_argument("--lab-name", default="XRML 연구실")
    parser.add_argument("--project-name", default="XR/AR × ML (Image/Video)")
    args = parser.parse_args(argv)

    run_date = args.run_date or datetime.now().date().isoformat()
    print(
        "[DEPRECATED] run_all_to_journal_pdf.py is a compatibility wrapper. "
        "Unsupported PDF args are no longer forwarded to make_journal_pdf.py."
    )
    if args.lab_name or args.project_name:
        print("[INFO] --lab-name/--project-name are currently ignored by the strict journal PDF generator.")

    pipeline_cmd = [
        sys.executable,
        "tools/pipeline.py",
        "--top",
        str(args.top),
        "--weekly-top",
        str(args.weekly_top),
        "--rank",
        str(args.rank),
    ]
    if args.experiment:
        pipeline_cmd.append("--experiment")

    compile_cmd = [
        sys.executable,
        "tools/compile_journal.py",
        "--run-date",
        run_date,
        "--shortlist",
        str(args.shortlist),
        "--deep-dive",
        str(args.reviews),
    ]
    pdf_cmd = [sys.executable, "tools/make_journal_pdf.py", "--run-date", run_date]
    if args.font:
        pdf_cmd += ["--font", args.font]
    if args.font_bold:
        pdf_cmd += ["--font-bold", args.font_bold]

    for command in (pipeline_cmd, compile_cmd, pdf_cmd):
        print("[RUN]", " ".join(command))
        result = subprocess.run(command)
        if result.returncode != 0:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
