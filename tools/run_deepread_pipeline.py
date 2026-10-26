#!/usr/bin/env python3
"""
run_deepread_pipeline.py — (권장) Deep-dive 3편만 "본문(PDF) 읽기" 단계까지 준비하는 파이프라인

하는 일:
1) triage 상위 N편 PDF 다운로드(fetch_pdfs.py)
2) PDF 텍스트 추출(extract_pdf_text.py)
3) 리뷰 템플릿(notes/reviews/*.json) 생성(review_template.py)
4) (여기서) Codex가 텍스트를 읽고 리뷰를 채워 넣어야 함
5) 주간 journal.json 생성(compile_journal.py)
6) 저널 PDF 생성(make_journal_pdf.py)

사용:
  python tools/run_deepread_pipeline.py --run-date 2026-02-15 --deep 3 --shortlist 12 --font ... --font-bold ...

주의:
- 4번은 LLM(Codex)이 채워야 하므로 자동 스크립트만으로는 완성되지 않습니다.
"""

from __future__ import annotations
import argparse
import subprocess
import sys
from datetime import datetime

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-date", default=None)
    ap.add_argument("--deep", type=int, default=3)
    ap.add_argument("--shortlist", type=int, default=12)
    ap.add_argument("--font", default=r"C:\Windows\Fonts\malgun.ttf")
    ap.add_argument("--font-bold", default=r"C:\Windows\Fonts\malgunbd.ttf")
    ap.add_argument("--require-fulltext", action="store_true", help="Fail if deep reviews missing")
    args = ap.parse_args()

    run_date = args.run_date or datetime.now().date().isoformat()

    cmds = [
        [sys.executable, "tools/fetch_pdfs.py", "--top", str(args.deep)],
        [sys.executable, "tools/extract_pdf_text.py", "--top", str(args.deep)],
        [sys.executable, "tools/review_template.py", "--top", str(args.deep)],
        [sys.executable, "tools/compile_journal.py", "--run-date", run_date, "--shortlist", str(args.shortlist), "--deep-dive", str(args.deep)] + (["--require-fulltext"] if args.require_fulltext else []),
        [sys.executable, "tools/make_journal_pdf.py", "--run-date", run_date, "--font", args.font, "--font-bold", args.font_bold],
    ]
    for c in cmds:
        print("[RUN]", " ".join(c))
        r = subprocess.run(c)
        if r.returncode != 0:
            print(f"[STOP] failed: {' '.join(c)} (exit={r.returncode})")
            sys.exit(r.returncode)

    print("[DONE] Deepread journal PDF generated.")

if __name__ == "__main__":
    main()
