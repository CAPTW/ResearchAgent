#!/usr/bin/env python3
"""
XRML PDF Report Generator — v4 (Korean Lab Style)
=================================================

무엇이 달라졌나?
- (v3에서 발생한) 한글 깨짐/검은 네모(□) 문제를 **폰트 패밀리 임베딩**으로 해결합니다.
- 연구실 보고서에 맞게 **한글 섹션 구성 + 표/그래프/인포그래픽(퍼널)**을 기본 포함합니다.
- matplotlib 없이 reportlab만으로 그래프를 그려, 의존성/폰트 문제를 줄였습니다.

요구사항:
  pip install reportlab

권장(Windows 한글 폰트):
  C:\Windows\Fonts\malgun.ttf
  C:\Windows\Fonts\malgunbd.ttf

사용 예 (Windows):
  python tools/make_report_pdf.py --run-date 2026-02-15 --font "C:\Windows\Fonts\malgun.ttf" --font-bold "C:\Windows\Fonts\malgunbd.ttf"

WSL/Linux 예시(Noto CJK):
  sudo apt-get install -y fonts-nanum
  python tools/make_report_pdf.py --run-date 2026-02-15 --font "/usr/share/fonts/truetype/nanum/NanumGothic.ttf" --font-bold "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf" 

출력:
  reports/XRML_LabReport_<YYYY-MM-DD>.pdf
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# reportlab
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak,
    Table, TableStyle, ListFlowable, ListItem, KeepTogether
)
from reportlab.graphics.shapes import Drawing, Rect, Line, String
from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.lib.fonts import addMapping

from xml.sax.saxutils import escape as xml_escape


# ---------------------------
# Utilities
# ---------------------------

def safe_filename(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return s[:120] if len(s) > 120 else s


def date_to_tag(date_str: str) -> str:
    return re.sub(r"[^0-9]", "", date_str)[:8]


def parse_iso_date(s: str) -> Optional[date]:
    if not s:
        return None
    try:
        # accept "2026-02-11T19:53:06Z"
        s2 = s.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(s2).date()
    except Exception:
        try:
            return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not path.exists():
        return items
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:
                continue
    return items


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for _ in f:
            n += 1
    return n


def find_latest_file(dir_path: Path, pattern: str) -> Optional[Path]:
    files = sorted(dir_path.glob(pattern))
    return files[-1] if files else None


def split_tags(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out
    if isinstance(v, str):
        # "ar, pose, real-time"
        parts = [p.strip() for p in v.split(",")]
        return [p for p in parts if p]
    return []


def md_extract_section(md: str, heading: str) -> List[str]:
    """
    Weekly md에서 특정 섹션(예: 'Research gaps / open questions') 다음에 나오는 bullet lines를 추출(느슨한 파싱).
    """
    if not md:
        return []
    lines = md.splitlines()
    out: List[str] = []
    inside = False
    for ln in lines:
        if not inside:
            if heading.lower() in ln.lower():
                inside = True
            continue
        # 섹션 종료 조건: 다음 제목(대충) 또는 빈줄 이후 제목
        if re.match(r"^\s*#{1,6}\s+", ln):
            break
        if re.match(r"^\s*[A-Za-z].*:$", ln) and len(out) > 0:
            break
        m = re.match(r"^\s*[\-\*\u2022]\s+(.*)$", ln)
        if m:
            txt = m.group(1).strip()
            if txt:
                out.append(txt)
            continue
        m2 = re.match(r"^\s*(\d+)\)\s+(.*)$", ln)
        if m2:
            txt = m2.group(2).strip()
            if txt:
                out.append(txt)
            continue
        # stop if blank line and we already collected some
        if not ln.strip() and out:
            break
    return out


# ---------------------------
# Fonts (CJK-safe)
# ---------------------------

@dataclass
class FontPack:
    # style.fontName으로 사용할 face 이름 (addMapping에 사용)
    face: str
    # 실제 등록된 폰트 이름들(ReportLab 내부명)
    normal: str
    bold: str
    # 디버그/부록용
    regular_path: str
    bold_path: str


def _register_ttfont(name: str, path: Path, font_index: int = 0) -> None:
    """
    ReportLab은 TrueType(outlines=TTF) 기반을 가장 잘 지원합니다.
    일부 .otf/.ttc(CFF/PostScript outlines)는 TTFError를 발생시킬 수 있습니다.
    """
    if not path.exists():
        raise FileNotFoundError(str(path))
    if path.suffix.lower() == ".ttc":
        # TTC 지원은 제한적이며, 폰트 내부가 CFF면 실패할 수 있습니다.
        pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=font_index))
    else:
        pdfmetrics.registerFont(TTFont(name, str(path)))


def _try_register_pair(face: str, reg_path: Path, bold_path: Path, font_index: int = 0) -> Optional[FontPack]:
    """
    폰트 pair 등록을 시도하고, 성공 시 addMapping까지 세팅하여 <b>/<i>가 깨지지 않도록 함.
    """
    try:
        normal_name = face  # style.fontName과 동일하게
        bold_name = f"{face}-Bold"
        _register_ttfont(normal_name, reg_path, font_index=font_index)
        _register_ttfont(bold_name, bold_path, font_index=font_index)

        # Mapping: style.fontName(face) + (bold/italic) -> 실제 psname
        addMapping(face, 0, 0, normal_name)
        addMapping(face, 1, 0, bold_name)
        addMapping(face, 0, 1, normal_name)   # italic fallback
        addMapping(face, 1, 1, bold_name)

        return FontPack(
            face=face,
            normal=normal_name,
            bold=bold_name,
            regular_path=str(reg_path),
            bold_path=str(bold_path),
        )
    except Exception:
        return None


def register_cjk_font_family(font_path: Optional[str], font_bold_path: Optional[str], font_index: int = 0) -> FontPack:
    """
    v3 "Readable"에서 한글이 □/■로 깨지는 현상을 막기 위해
    - (regular + bold) 폰트를 강제 등록하고
    - Paragraph 내부 <b> 태그가 Helvetica 등으로 튀지 않도록 addMapping을 설정합니다.

    주의:
    - ReportLab은 일부 .otf/.ttc(CFF) 폰트를 지원하지 못할 수 있습니다.
      Windows의 malgun.ttf/malgunbd.ttf, Linux의 NanumGothic.ttf/NanumGothicBold.ttf 를 권장합니다.
    """
    # User-specified first
    pairs: List[Tuple[Path, Path]] = []
    if font_path and font_bold_path:
        pairs.append((Path(font_path), Path(font_bold_path)))

    # Auto-detect common pairs
    windir = os.environ.get("WINDIR", r"C:\Windows")
    win_fonts = Path(windir) / "Fonts"
    pairs += [
        (win_fonts / "malgun.ttf", win_fonts / "malgunbd.ttf"),
        (win_fonts / "NanumGothic.ttf", win_fonts / "NanumGothicBold.ttf"),
    ]
    pairs += [
        (Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"), Path("/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf")),
        (Path("/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf"), Path("/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf")),
    ]

    face = "XRMLFont"  # stable name used by styles
    last_err = None
    for reg, bold in pairs:
        if not (reg.exists() and bold.exists()):
            continue
        fp = _try_register_pair(face, reg, bold, font_index=font_index)
        if fp:
            return fp
        last_err = f"{reg} + {bold}"

    msg = (
        "CJK(한글) 폰트를 등록하지 못했습니다. 이 경우 PDF에서 한글이 □/■로 깨집니다.\n\n"
        "해결 방법:\n"
        "  - Windows:\n"
        "      --font \"C:\\Windows\\Fonts\\malgun.ttf\" --font-bold \"C:\\Windows\\Fonts\\malgunbd.ttf\"\n"
        "  - WSL/Linux(Ubuntu 등):\n"
        "      sudo apt-get install -y fonts-nanum\n"
        "      --font \"/usr/share/fonts/truetype/nanum/NanumGothic.ttf\" --font-bold \"/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf\"\n\n"
        f"마지막 시도: {last_err}\n"
        "주의: 일부 NotoSansCJK .ttc/.otf(CFF) 폰트는 ReportLab에서 지원되지 않을 수 있습니다.\n"
    )
    raise SystemExit(msg)

# ---------------------------
# Styles
# ---------------------------

def build_styles(fp: FontPack) -> Dict[str, ParagraphStyle]:
    ss = getSampleStyleSheet()

    body = ParagraphStyle(
        "Body",
        parent=ss["BodyText"],
        fontName=fp.face,
        fontSize=10.5,
        leading=14,
        spaceAfter=2,
        wordWrap="CJK",
    )
    small = ParagraphStyle(
        "Small",
        parent=ss["BodyText"],
        fontName=fp.face,
        fontSize=9.2,
        leading=12,
        wordWrap="CJK",
    )
    h1 = ParagraphStyle(
        "H1",
        parent=ss["Heading1"],
        fontName=fp.bold,
        fontSize=18,
        leading=22,
        spaceBefore=6,
        spaceAfter=6,
        wordWrap="CJK",
    )
    h2 = ParagraphStyle(
        "H2",
        parent=ss["Heading2"],
        fontName=fp.bold,
        fontSize=13.5,
        leading=18,
        spaceBefore=8,
        spaceAfter=4,
        wordWrap="CJK",
    )
    h3 = ParagraphStyle(
        "H3",
        parent=ss["Heading3"],
        fontName=fp.bold,
        fontSize=12,
        leading=16,
        spaceBefore=6,
        spaceAfter=3,
        wordWrap="CJK",
    )
    title = ParagraphStyle(
        "Title",
        parent=ss["Title"],
        fontName=fp.bold,
        fontSize=22,
        leading=28,
        alignment=TA_CENTER,
        spaceAfter=8,
        wordWrap="CJK",
    )
    subtitle = ParagraphStyle(
        "Subtitle",
        parent=ss["BodyText"],
        fontName=fp.face,
        fontSize=11.5,
        leading=15,
        alignment=TA_CENTER,
        spaceAfter=6,
        wordWrap="CJK",
    )
    return {"Body": body, "Small": small, "H1": h1, "H2": h2, "H3": h3, "Title": title, "Subtitle": subtitle}


def P(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(xml_escape(text), style)


def Pm(text: str, style: ParagraphStyle) -> Paragraph:
    # allow simple markup like <b>, <link>
    return Paragraph(text, style)


# ---------------------------
# Drawing helpers (Infographic + Charts)
# ---------------------------

def make_kpi_cards(fp: FontPack, kpis: List[Tuple[str, str]], width: float) -> Table:
    """
    KPI 카드 4개를 테이블로 구성 (보고서 상단).
    """
    cells = []
    for label, val in kpis:
        cells.append(Pm(f"<b>{xml_escape(val)}</b><br/><font size='9'>{xml_escape(label)}</font>", ParagraphStyle(
            "KPI",
            fontName=fp.face,
            fontSize=16,
            leading=18,
            alignment=TA_CENTER,
            wordWrap="CJK"
        )))
    # 4 columns
    col_w = width / max(1, len(cells))
    t = Table([cells], colWidths=[col_w]*len(cells), hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("BOX", (0,0), (-1,-1), 0.5, colors.lightgrey),
        ("INNERGRID", (0,0), (-1,-1), 0.5, colors.lightgrey),
        ("TOPPADDING", (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
    ]))
    return t


def make_pipeline_funnel(fp: FontPack, counts: List[Tuple[str, int]], width: float, height: float = 34*mm) -> Drawing:
    """
    Scout→Triage→Summary→Synthesis→Experiment 퍼널(박스+화살표).
    """
    d = Drawing(width, height)
    n = len(counts)
    if n == 0:
        return d

    pad = 4*mm
    box_w = (width - pad*(n-1)) / n
    box_h = height - 8*mm
    y = 4*mm
    for i, (label, c) in enumerate(counts):
        x = i*(box_w + pad)
        d.add(Rect(x, y, box_w, box_h, strokeColor=colors.lightgrey, fillColor=colors.whitesmoke, strokeWidth=0.8))
        d.add(String(x + box_w/2, y + box_h*0.62, f"{c}", textAnchor="middle", fontName=fp.bold, fontSize=16))
        d.add(String(x + box_w/2, y + box_h*0.28, label, textAnchor="middle", fontName=fp.normal, fontSize=9.2))
        # arrows
        if i < n-1:
            ax1 = x + box_w
            ax2 = x + box_w + pad
            ay = y + box_h/2
            d.add(Line(ax1, ay, ax2, ay, strokeColor=colors.grey, strokeWidth=1))
            d.add(Line(ax2-2, ay+2, ax2, ay, strokeColor=colors.grey, strokeWidth=1))
            d.add(Line(ax2-2, ay-2, ax2, ay, strokeColor=colors.grey, strokeWidth=1))
    return d


def make_hbar_chart(fp: FontPack, title: str, labels: List[str], values: List[int], width: float, height: float) -> Drawing:
    d = Drawing(width, height)
    # Title
    d.add(String(0, height-12, title, fontName=fp.bold, fontSize=11))

    # Chart area
    chart = HorizontalBarChart()
    chart.x = 0
    chart.y = 0
    chart.width = width
    chart.height = height - 18
    chart.data = [values]
    chart.valueAxis.valueMin = 0
    chart.valueAxis.visibleGrid = 1
    chart.valueAxis.gridStrokeColor = colors.lightgrey
    chart.categoryAxis.categoryNames = labels
    chart.categoryAxis.labels.fontName = fp.normal
    chart.categoryAxis.labels.fontSize = 8.8
    chart.valueAxis.labels.fontName = fp.normal
    chart.valueAxis.labels.fontSize = 8.8
    chart.bars[0].fillColor = colors.HexColor("#4F81BD")
    chart.strokeColor = colors.white

    d.add(chart)
    return d


def make_vbar_chart(fp: FontPack, title: str, labels: List[str], values: List[int], width: float, height: float) -> Drawing:
    d = Drawing(width, height)
    d.add(String(0, height-12, title, fontName=fp.bold, fontSize=11))

    chart = VerticalBarChart()
    chart.x = 10
    chart.y = 0
    chart.width = width - 10
    chart.height = height - 18
    chart.data = [values]
    chart.valueAxis.valueMin = 0
    chart.valueAxis.visibleGrid = 1
    chart.valueAxis.gridStrokeColor = colors.lightgrey
    chart.categoryAxis.categoryNames = labels
    chart.categoryAxis.labels.fontName = fp.normal
    chart.categoryAxis.labels.fontSize = 7.8
    chart.categoryAxis.labels.angle = 45
    chart.categoryAxis.labels.dy = -6
    chart.valueAxis.labels.fontName = fp.normal
    chart.valueAxis.labels.fontSize = 8.8
    chart.bars[0].fillColor = colors.HexColor("#9BBB59")
    d.add(chart)
    return d


# ---------------------------
# Main builder
# ---------------------------

def header_footer(canvas, doc, fp: FontPack, header_text: str):
    canvas.saveState()
    canvas.setFont(fp.normal, 9)
    canvas.setFillColor(colors.grey)
    canvas.drawString(doc.leftMargin, A4[1] - 12*mm, header_text)
    canvas.drawRightString(A4[0] - doc.rightMargin, 12*mm, f"{doc.page}")
    canvas.restoreState()


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return path.read_text(encoding="utf-8", errors="ignore")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-date", default=None, help="YYYY-MM-DD (default: today local)")
    ap.add_argument("--top-papers", type=int, default=3)
    ap.add_argument("--triage-top", type=int, default=25)
    ap.add_argument("--weekly-top", type=int, default=15, help="Weekly view top N (for charts/tables)")
    ap.add_argument(
        "--include-experiments",
        action="store_true",
        help="Include non-executable experiment proposal scaffolds; never treat them as results.",
    )
    ap.add_argument("--lab-name", default="XRML 연구실", help="보고서 상단 표기용 연구실/팀 이름")
    ap.add_argument("--project-name", default="XR/AR × ML (Image/Video)", help="프로젝트/주제명")
    ap.add_argument("--font", default=None, help="CJK regular font path (ttf/otf/ttc)")
    ap.add_argument("--font-bold", default=None, help="CJK bold font path (ttf/otf/ttc)")
    ap.add_argument("--font-index", type=int, default=0, help="For .ttc font collections: subfont index")
    ap.add_argument("--out", default=None, help="Output PDF path")
    args = ap.parse_args()

    run_date = args.run_date or datetime.now().date().isoformat()
    run_dt = parse_iso_date(run_date) or datetime.now().date()
    date_tag = date_to_tag(run_date)

    project_root = Path(__file__).resolve().parents[1]

    # Register fonts (mandatory to avoid broken Korean)
    fp = register_cjk_font_family(args.font, args.font_bold, font_index=args.font_index)
    styles = build_styles(fp)

    # Locate inputs
    raw_dir = project_root / "data" / "papers" / "raw"
    raw_file = raw_dir / f"arxiv_{date_tag}.jsonl"
    if not raw_file.exists():
        raw_file = find_latest_file(raw_dir, "arxiv_*.jsonl") or raw_file

    inbox_file = project_root / "data" / "queues" / "inbox.jsonl"
    triage_jsonl = project_root / "data" / "queues" / "triage.jsonl"
    triage_md = project_root / "data" / "queues" / "triage.md"

    weekly_dir = project_root / "notes" / "weekly"
    weekly_file = find_latest_file(weekly_dir, "*.md")

    daily_file = project_root / "notes" / "daily" / f"{run_date}.md"
    papers_dir = project_root / "notes" / "papers"

    # Stats
    new_papers = count_lines(raw_file)
    inbox_count = count_lines(inbox_file)
    triage_items = load_jsonl(triage_jsonl)
    triage_count = len(triage_items)

    # Fallback if triage.jsonl missing
    if triage_count == 0 and triage_md.exists():
        # minimal parsing from triage.md is possible but not ideal
        pass

    # tag frequency
    tag_counts: Dict[str, int] = {}
    scores: List[int] = []
    pub_dates: List[date] = []
    for it in triage_items:
        sc = it.get("score")
        try:
            sc_i = int(sc)
            scores.append(sc_i)
        except Exception:
            pass

        pd = parse_iso_date(str(it.get("published", "")))
        if pd:
            pub_dates.append(pd)

        for tg in split_tags(it.get("hits") or it.get("tags") or it.get("keywords")):
            tag_counts[tg] = tag_counts.get(tg, 0) + 1

    # Weekly view (last 7 days)
    week_start = run_dt - timedelta(days=7)
    weekly_candidates = []
    for it in triage_items:
        pd = parse_iso_date(str(it.get("published", "")))
        if pd and pd >= week_start and pd <= run_dt:
            weekly_candidates.append(it)
    weekly_candidates.sort(key=lambda x: int(x.get("score", 0)), reverse=True)
    weekly_selected = weekly_candidates[: max(0, args.weekly_top)]

    # Top paper notes paths (optional)
    top_ids = [it.get("paper_id", "") for it in triage_items[: max(0, args.top_papers)] if it.get("paper_id")]
    paper_notes: List[Path] = []
    for pid in top_ids:
        cand = papers_dir / f"{safe_filename(pid)}.md"
        if cand.exists():
            paper_notes.append(cand)

    # Experiments
    exp_root = project_root / "experiments"
    exp_dirs: List[Path] = []
    if args.include_experiments and exp_root.exists():
        exp_dirs = sorted([p for p in exp_root.iterdir() if p.is_dir() and p.name.startswith(date_tag)])

    # Weekly extracts (open questions + next experiments)
    weekly_md_txt = read_text(weekly_file) if (weekly_file and weekly_file.exists()) else ""
    open_q = md_extract_section(weekly_md_txt, "Research gaps")
    if not open_q:
        open_q = md_extract_section(weekly_md_txt, "open questions")
    next_exp = md_extract_section(weekly_md_txt, "Proposed Experiment Scaffolds")
    if not next_exp:
        next_exp = md_extract_section(weekly_md_txt, "Next experiments")
    if not next_exp:
        next_exp = md_extract_section(weekly_md_txt, "experiments")

    # Output path
    out_path = Path(args.out) if args.out else (project_root / "reports" / f"XRML_LabReport_{run_date}.pdf")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    header_text = f"{args.lab_name} | {args.project_name} 주간 리서치 ({run_date})"

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=18*mm,
        rightMargin=18*mm,
        topMargin=18*mm,
        bottomMargin=16*mm,
        title=header_text,
        author=args.lab_name,
    )

    story: List[Any] = []

    # ---------------------------
    # Cover / Executive summary
    # ---------------------------
    story.append(Spacer(1, 8*mm))
    story.append(P(f"{args.project_name}", styles["Title"]))
    story.append(P("주간 리서치 브리프 (자동 생성)", styles["Subtitle"]))
    story.append(P(f"기준일: {run_date}", styles["Subtitle"]))
    story.append(Spacer(1, 6*mm))

    kpis = [
        ("수집 논문 수 (raw)", str(new_papers)),
        ("Inbox", str(inbox_count)),
        ("Triage 항목", str(triage_count)),
        ("주간 Top", str(len(weekly_selected))),
    ]
    story.append(make_kpi_cards(fp, kpis, width=doc.width))
    story.append(Spacer(1, 8*mm))

    funnel_counts = [
        ("Scout(수집)", new_papers or inbox_count),
        ("Weekly(검토)", len(weekly_candidates) if triage_items else 0),
        ("Weekly(선정)", len(weekly_selected)),
        ("요약 노트", len(paper_notes) or max(0, args.top_papers)),
        ("실험 설계 제안 스캐폴드(비실행)", len(exp_dirs)),
    ]
    story.append(P("파이프라인 퍼널", styles["H3"]))
    story.append(make_pipeline_funnel(fp, funnel_counts, width=doc.width))
    story.append(Spacer(1, 6*mm))

    # Top 5 table
    story.append(P("핵심 논문 Top 5 (Triage)", styles["H2"]))
    top5 = triage_items[:5]
    if top5:
        data = [[
            Pm("<b>Rank</b>", styles["Small"]),
            Pm("<b>Score</b>", styles["Small"]),
            Pm("<b>ID</b>", styles["Small"]),
            Pm("<b>제목</b>", styles["Small"]),
            Pm("<b>키워드</b>", styles["Small"]),
        ]]
        for i, it in enumerate(top5, start=1):
            pid = str(it.get("paper_id",""))
            title = xml_escape(str(it.get("title","")))
            url = str(it.get("url",""))
            if url:
                title_cell = Pm(f'<link href="{xml_escape(url)}">{title}</link>', styles["Small"])
            else:
                title_cell = Pm(title, styles["Small"])
            tags = ", ".join(split_tags(it.get("hits") or it.get("tags"))[:6])
            data.append([
                P(str(i), styles["Small"]),
                P(str(it.get("score","")), styles["Small"]),
                P(pid, styles["Small"]),
                title_cell,
                P(tags, styles["Small"]),
            ])
        colw = [10*mm, 14*mm, 28*mm, doc.width-10*mm-14*mm-28*mm-34*mm, 34*mm]
        t = Table(data, colWidths=colw, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1F2A44")),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("INNERGRID", (0,0), (-1,-1), 0.25, colors.lightgrey),
            ("BOX", (0,0), (-1,-1), 0.5, colors.lightgrey),
            ("LEFTPADDING", (0,0), (-1,-1), 6),
            ("RIGHTPADDING", (0,0), (-1,-1), 6),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ]))
        story.append(t)
    else:
        story.append(P("triage.jsonl을 찾지 못해 Top 5 테이블을 생성하지 못했습니다.", styles["Body"]))

    story.append(PageBreak())

    # ---------------------------
    # Trends / Charts
    # ---------------------------
    story.append(P("동향 요약", styles["H1"]))

    # Tag charts
    if tag_counts:
        # sort
        items_sorted = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
        # remove overly generic 'ar' optionally
        items_no_ar = [x for x in items_sorted if x[0].lower() != "ar"]
        top_tags = items_sorted[:12]
        top_tags_no_ar = items_no_ar[:12]

        labels1 = [k if len(k) <= 18 else (k[:17] + "…") for k,_ in reversed(top_tags)]
        vals1 = [v for _,v in reversed(top_tags)]
        d1 = make_hbar_chart(fp, "키워드 빈도 (Top 12, Triage 기반)", labels1, vals1, width=doc.width, height=70*mm)
        story.append(d1)
        story.append(Spacer(1, 6*mm))

        labels2 = [k if len(k) <= 18 else (k[:17] + "…") for k,_ in reversed(top_tags_no_ar)]
        vals2 = [v for _,v in reversed(top_tags_no_ar)]
        d2 = make_hbar_chart(fp, "키워드 빈도 (Top 12, 'ar' 제외)", labels2, vals2, width=doc.width, height=70*mm)
        story.append(d2)
        story.append(Spacer(1, 6*mm))
    else:
        story.append(P("키워드 분포를 계산할 triage.jsonl 데이터가 없습니다.", styles["Body"]))

    # Score distribution
    if scores:
        bins = [(0,19),(20,29),(30,39),(40,49),(50,59),(60,69),(70,79),(80,89),(90,100)]
        labels = [f"{a}-{b}" for a,b in bins]
        counts = [0]*len(bins)
        for s in scores[: max(0, args.triage_top)]:
            for i,(a,b) in enumerate(bins):
                if a <= s <= b:
                    counts[i]+=1
                    break
        d3 = make_vbar_chart(fp, f"점수 분포 (상위 {args.triage_top}개)", labels, counts, width=doc.width, height=70*mm)
        story.append(d3)
        story.append(Spacer(1, 6*mm))

    # Recency (last 14 days)
    if pub_dates:
        start = run_dt - timedelta(days=13)
        day_list = [start + timedelta(days=i) for i in range(14)]
        day_counts = {d:0 for d in day_list}
        for pd in pub_dates:
            if start <= pd <= run_dt:
                day_counts[pd] = day_counts.get(pd, 0) + 1
        labels = [d.strftime("%m-%d") for d in day_list]
        values = [day_counts.get(d,0) for d in day_list]
        d4 = make_vbar_chart(fp, "발행일 추세 (최근 14일)", labels, values, width=doc.width, height=70*mm)
        story.append(d4)

    story.append(PageBreak())

    # ---------------------------
    # Weekly synthesis (structured)
    # ---------------------------
    story.append(P("주간 Synthesis", styles["H1"]))
    if weekly_file and weekly_file.exists():
        story.append(P(f"소스: {weekly_file.name}", styles["Small"]))
    else:
        story.append(P("weekly 파일을 찾지 못했습니다 (notes/weekly/*.md).", styles["Body"]))

    if open_q:
        story.append(Spacer(1, 2*mm))
        story.append(P("오픈 질문 (초안)", styles["H2"]))
        bullets = [ListItem(P(x, styles["Body"]), leftIndent=10) for x in open_q[:8]]
        story.append(ListFlowable(bullets, bulletType="bullet", leftIndent=14))
    else:
        story.append(P("오픈 질문 섹션을 추출하지 못했습니다.", styles["Body"]))

    if next_exp:
        story.append(Spacer(1, 4*mm))
        story.append(P("다음 실험 제안 (가벼운 형태)", styles["H2"]))
        bullets = [ListItem(P(x, styles["Body"]), leftIndent=10) for x in next_exp[:8]]
        story.append(ListFlowable(bullets, bulletType="1", leftIndent=14))
    else:
        story.append(P("다음 실험 제안 섹션을 추출하지 못했습니다.", styles["Body"]))

    # Weekly selected papers table
    story.append(Spacer(1, 6*mm))
    story.append(P(f"주간 후보(최근 7일) Top {len(weekly_selected)}", styles["H2"]))
    if weekly_selected:
        data = [[Pm("<b>Rank</b>", styles["Small"]), Pm("<b>Score</b>", styles["Small"]), Pm("<b>ID</b>", styles["Small"]), Pm("<b>제목</b>", styles["Small"]), Pm("<b>발행</b>", styles["Small"])]]
        for i,it in enumerate(weekly_selected, start=1):
            pid = str(it.get("paper_id",""))
            title = xml_escape(str(it.get("title","")))
            url = str(it.get("url",""))
            title_cell = Pm(f'<link href="{xml_escape(url)}">{title}</link>', styles["Small"]) if url else Pm(title, styles["Small"])
            pub = str(it.get("published",""))[:10]
            data.append([P(str(i), styles["Small"]), P(str(it.get("score","")), styles["Small"]), P(pid, styles["Small"]), title_cell, P(pub, styles["Small"])])
        colw=[10*mm, 14*mm, 28*mm, doc.width-10*mm-14*mm-28*mm-20*mm, 20*mm]
        t=Table(data, colWidths=colw, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#1F2A44")),
            ("TEXTCOLOR",(0,0),(-1,0), colors.white),
            ("VALIGN",(0,0),(-1,-1), "TOP"),
            ("INNERGRID",(0,0),(-1,-1), 0.25, colors.lightgrey),
            ("BOX",(0,0),(-1,-1), 0.5, colors.lightgrey),
            ("LEFTPADDING",(0,0),(-1,-1), 6),
            ("RIGHTPADDING",(0,0),(-1,-1), 6),
            ("TOPPADDING",(0,0),(-1,-1), 4),
            ("BOTTOMPADDING",(0,0),(-1,-1), 4),
        ]))
        story.append(t)
    else:
        story.append(P("주간 후보를 계산할 triage 데이터가 없습니다.", styles["Body"]))

    story.append(PageBreak())

    # ---------------------------
    # Triage table (top N)
    # ---------------------------
    story.append(P("Triage 상위 리스트", styles["H1"]))
    story.append(P(f"상위 {args.triage_top}개 (표)", styles["Small"]))
    topN = triage_items[: max(0, args.triage_top)]
    if topN:
        data = [[
            Pm("<b>Rank</b>", styles["Small"]),
            Pm("<b>Score</b>", styles["Small"]),
            Pm("<b>ID</b>", styles["Small"]),
            Pm("<b>발행</b>", styles["Small"]),
            Pm("<b>제목</b>", styles["Small"]),
            Pm("<b>키워드</b>", styles["Small"]),
        ]]
        for i, it in enumerate(topN, start=1):
            pid = str(it.get("paper_id",""))
            pub = str(it.get("published",""))[:10]
            title = xml_escape(str(it.get("title","")))
            url = str(it.get("url",""))
            title_cell = Pm(f'<link href="{xml_escape(url)}">{title}</link>', styles["Small"]) if url else Pm(title, styles["Small"])
            tags = ", ".join(split_tags(it.get("hits") or it.get("tags"))[:6])
            data.append([P(str(i), styles["Small"]), P(str(it.get("score","")), styles["Small"]), P(pid, styles["Small"]), P(pub, styles["Small"]), title_cell, P(tags, styles["Small"])])
        colw=[10*mm, 14*mm, 26*mm, 18*mm, doc.width-10*mm-14*mm-26*mm-18*mm-36*mm, 36*mm]
        t=Table(data, colWidths=colw, hAlign="LEFT", repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#1F2A44")),
            ("TEXTCOLOR",(0,0),(-1,0), colors.white),
            ("VALIGN",(0,0),(-1,-1), "TOP"),
            ("INNERGRID",(0,0),(-1,-1), 0.25, colors.lightgrey),
            ("BOX",(0,0),(-1,-1), 0.5, colors.lightgrey),
            ("LEFTPADDING",(0,0),(-1,-1), 5),
            ("RIGHTPADDING",(0,0),(-1,-1), 5),
            ("TOPPADDING",(0,0),(-1,-1), 4),
            ("BOTTOMPADDING",(0,0),(-1,-1), 4),
        ]))
        story.append(t)
    else:
        story.append(P("triage.jsonl을 찾지 못했습니다.", styles["Body"]))

    story.append(PageBreak())

    # ---------------------------
    # Selected paper briefs (from abstracts)
    # ---------------------------
    story.append(P("핵심 논문 브리프 (초록 기반)", styles["H1"]))
    if not top_ids:
        story.append(P("Top paper id를 찾지 못했습니다 (triage.jsonl 필요).", styles["Body"]))
    else:
        # Build from triage items directly
        by_id = {str(it.get("paper_id","")): it for it in triage_items}
        for idx, pid in enumerate(top_ids, start=1):
            it = by_id.get(pid)
            if not it:
                continue
            title = str(it.get("title",""))
            authors = str(it.get("authors",""))
            abstract = str(it.get("abstract",""))
            url = str(it.get("url",""))
            signals = ", ".join(split_tags(it.get("hits") or it.get("tags"))[:10])

            block: List[Any] = []
            block.append(P(f"{idx}. {title}", styles["H2"]))
            meta = f"ID: {pid}"
            if authors:
                meta += f" | Authors: {authors}"
            block.append(P(meta, styles["Small"]))
            if url:
                block.append(Pm(f'<link href="{xml_escape(url)}">{xml_escape(url)}</link>', styles["Small"]))
            block.append(Spacer(1, 2*mm))
            block.append(P(f"키워드 시그널: {signals}", styles["Small"]))
            block.append(Spacer(1, 3*mm))

            # Abstract box
            if abstract:
                t = Table([[Pm(f"<b>Abstract</b><br/>{xml_escape(abstract)}", styles["Small"])]], colWidths=[doc.width])
                t.setStyle(TableStyle([
                    ("BACKGROUND",(0,0),(-1,-1), colors.HexColor("#F3F6FB")),
                    ("BOX",(0,0),(-1,-1), 0.5, colors.HexColor("#D5DCE6")),
                    ("LEFTPADDING",(0,0),(-1,-1), 8),
                    ("RIGHTPADDING",(0,0),(-1,-1), 8),
                    ("TOPPADDING",(0,0),(-1,-1), 8),
                    ("BOTTOMPADDING",(0,0),(-1,-1), 8),
                ]))
                block.append(t)

            block.append(Spacer(1, 5*mm))
            block.append(P("후속 확인 체크리스트 (연구실 관점)", styles["H3"]))
            checklist = [
                "실시간성 요구사항(latency)과 목표 FPS/Hz는 무엇인가?",
                "XR 센서 구성(HMD/IMU/카메라/Depth/Eye 등) 가정이 명확한가?",
                "베이스라인/대조군 설정이 타당한가? (기존 방식 vs 제안 방식)",
                "데이터셋/평가지표/재현성(코드/모델)이 공개되어 있는가?",
            ]
            bullets = [ListItem(P(x, styles["Body"]), leftIndent=10) for x in checklist]
            block.append(ListFlowable(bullets, bulletType="bullet", leftIndent=14))

            story.append(KeepTogether(block))
            story.append(PageBreak())

    # ---------------------------
    # Experiments
    # ---------------------------
    story.append(P("제안된 실험 설계 스캐폴드 (비실행)", styles["H1"]))
    story.append(
        P(
            "표시된 항목은 실행 불가능한 연구 설계 proposal입니다. 실행 상태는 not_executed이며 "
            "실험 결과, 관찰 또는 검증 근거로 집계하지 않습니다.",
            styles["Body"],
        )
    )
    if exp_dirs:
        for d in exp_dirs:
            readme = d / "README.md"
            story.append(P(d.name, styles["H2"]))
            if readme.exists():
                # show first ~60 lines as plain text-ish
                txt = read_text(readme).strip().splitlines()
                snippet = "\n".join(txt[:80])
                # Use a single-cell table as a box
                t = Table([[Pm(xml_escape(snippet).replace("\n","<br/>"), styles["Small"])]], colWidths=[doc.width])
                t.setStyle(TableStyle([
                    ("BACKGROUND",(0,0),(-1,-1), colors.HexColor("#F8F8F8")),
                    ("BOX",(0,0),(-1,-1), 0.5, colors.HexColor("#D5DCE6")),
                    ("LEFTPADDING",(0,0),(-1,-1), 8),
                    ("RIGHTPADDING",(0,0),(-1,-1), 8),
                    ("TOPPADDING",(0,0),(-1,-1), 8),
                    ("BOTTOMPADDING",(0,0),(-1,-1), 8),
                ]))
                story.append(t)
            else:
                story.append(P("README.md가 없습니다.", styles["Body"]))
            story.append(Spacer(1, 4*mm))
    else:
        story.append(
            P(
                "이번 실행에서 생성된 비실행 experiment proposal scaffold가 없거나, "
                "--include-experiments 옵션이 꺼져있습니다.",
                styles["Body"],
            )
        )

    story.append(PageBreak())

    # ---------------------------
    # Appendix
    # ---------------------------
    story.append(P("부록: 파일 경로", styles["H1"]))
    appendix = [
        f"Project root: {project_root}",
        f"Raw arXiv file: {raw_file} (lines={new_papers})",
        f"Inbox: {inbox_file} (lines={inbox_count})",
        f"Triage JSONL: {triage_jsonl} (items={triage_count})",
        f"Triage MD: {triage_md}",
        f"Weekly: {weekly_file if weekly_file else '(none)'}",
        f"Daily: {daily_file}",
        f"Papers dir: {papers_dir}",
        f"Reports dir: {out_path.parent}",
        f"Font(normal): {fp.normal}",
        f"Font(bold): {fp.bold}",
        f"Font file(normal): {fp.regular_path}",
        f"Font file(bold): {fp.bold_path}",
    ]
    bullets = [ListItem(P(x, styles["Small"]), leftIndent=10) for x in appendix]
    story.append(ListFlowable(bullets, bulletType="bullet", leftIndent=14))

    # Build document
    def _on_page(canvas, doc_):
        header_footer(canvas, doc_, fp, header_text)

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)

    print(json.dumps({
        "status":"ok",
        "output_pdf": str(out_path),
        "run_date": run_date,
        "font_normal": fp.normal,
        "font_bold": fp.bold,
        "triage_count": triage_count,
        "weekly_selected": len(weekly_selected),
        "experiments": [d.name for d in exp_dirs],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
