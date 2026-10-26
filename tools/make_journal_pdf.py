#!/usr/bin/env python3
"""
make_journal_pdf.py — (v6.2) Weekly Journal PDF 생성기 (strict)

v6.2 변화:
- 기본적으로 "비어있는 리뷰"면 PDF 생성을 중단합니다.
  (원치 않으면 --allow-empty 로 완화 가능)
"""

from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Tuple, List

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.enums import TA_CENTER
from xml.sax.saxutils import escape as xml_escape

def safe_filename(s: str) -> str:
    s=(s or "").strip()
    s=re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    return s[:140] if len(s) > 140 else s

def date_to_week(date_str: str) -> str:
    dt = datetime.fromisoformat(date_str)
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"

def register_korean_fonts(font: str, font_bold: str) -> Tuple[str,str]:
    if not (font and Path(font).exists()):
        raise FileNotFoundError(f"font not found: {font}")
    if not (font_bold and Path(font_bold).exists()):
        raise FileNotFoundError(f"font_bold not found: {font_bold}")
    base="XRMLFont"
    bold="XRMLFont-Bold"
    pdfmetrics.registerFont(TTFont(base, font))
    pdfmetrics.registerFont(TTFont(bold, font_bold))
    registerFontFamily(base, normal=base, bold=bold, italic=base, boldItalic=bold)
    return base, bold

def styles(base_font: str, bold_font: str):
    ss=getSampleStyleSheet()
    title=ParagraphStyle("Title", parent=ss["Title"], fontName=bold_font, fontSize=22, leading=28, alignment=TA_CENTER, wordWrap="CJK")
    h1=ParagraphStyle("H1", parent=ss["Heading1"], fontName=bold_font, fontSize=16, leading=20, spaceAfter=6, wordWrap="CJK")
    h2=ParagraphStyle("H2", parent=ss["Heading2"], fontName=bold_font, fontSize=12.5, leading=16, spaceAfter=4, wordWrap="CJK")
    body=ParagraphStyle("Body", parent=ss["BodyText"], fontName=base_font, fontSize=10.5, leading=14, wordWrap="CJK")
    small=ParagraphStyle("Small", parent=ss["BodyText"], fontName=base_font, fontSize=9, leading=12, wordWrap="CJK")
    return {"Title":title,"H1":h1,"H2":h2,"Body":body,"Small":small}

def on_page(canvas, doc, base_font: str, header: str):
    canvas.saveState()
    canvas.setFont(base_font, 9)
    canvas.drawString(doc.leftMargin, A4[1]-12*mm, header)
    canvas.drawRightString(A4[0]-doc.rightMargin, 12*mm, str(doc.page))
    canvas.restoreState()

def load_json(path: Path) -> Dict[str,Any]:
    return json.loads(path.read_text(encoding="utf-8", errors="ignore"))

def is_empty_review_obj(obj: Dict[str,Any]) -> bool:
    r = (obj or {}).get("review_ko", {})
    if not isinstance(r, dict):
        return True
    one = (r.get("one_liner","") or "").strip()
    contrib = r.get("key_contributions", [])
    return (len(one) < 10) or (not isinstance(contrib, list)) or (len([x for x in contrib if isinstance(x,str) and x.strip()]) < 2)

def bullets(items: List[str]) -> str:
    items=[x.strip() for x in (items or []) if isinstance(x,str) and x.strip()]
    if not items:
        return "• (없음)"
    return "<br/>".join([xml_escape("• "+x) for x in items])

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--run-date", default=None)
    ap.add_argument("--journal", default=None)
    ap.add_argument("--font", default=r"C:\Windows\Fonts\malgun.ttf")
    ap.add_argument("--font-bold", default=r"C:\Windows\Fonts\malgunbd.ttf")
    ap.add_argument("--out", default=None)
    ap.add_argument("--allow-empty", action="store_true", help="Allow building PDF even if reviews are empty (not recommended).")
    args=ap.parse_args()

    project_root=Path(__file__).resolve().parents[1]
    run_date=args.run_date or datetime.now().date().isoformat()
    week=date_to_week(run_date)

    base_font, bold_font=register_korean_fonts(args.font, args.font_bold)
    st=styles(base_font, bold_font)

    journal_path=Path(args.journal) if args.journal else (project_root/"notes"/"journal"/f"{week}.json")
    if not journal_path.exists():
        raise FileNotFoundError(f"journal json not found: {journal_path} (run tools/compile_journal.py)")

    journal=load_json(journal_path)
    domain_context = journal.get("domain_context") if isinstance(journal.get("domain_context"), dict) else {}
    domain_label = str(domain_context.get("display_name") or "Research Topic").strip() or "Research Topic"

    pow_ = journal.get("paper_of_the_week")
    dd = journal.get("deep_dive", []) or []
    if not args.allow_empty:
        problems=[]
        if not pow_ or not isinstance(pow_, dict) or is_empty_review_obj(pow_):
            problems.append("paper_of_the_week review is empty")
        for it in dd:
            if isinstance(it, dict) and is_empty_review_obj(it):
                problems.append(f"deep_dive review empty: {it.get('paper_id','')}")
        if problems:
            raise SystemExit("Refusing to build empty journal PDF. Fix reviews first:\n- " + "\n- ".join(problems))

    out_path=Path(args.out) if args.out else (project_root/"reports"/f"XRML_WeeklyJournal_{week}.pdf")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    header=f"XRML Weekly Journal · {week} · run {run_date}"
    doc=SimpleDocTemplate(str(out_path), pagesize=A4, leftMargin=16*mm, rightMargin=16*mm, topMargin=18*mm, bottomMargin=16*mm, title=header)

    story=[]
    story.append(Paragraph(f"{xml_escape(domain_label)} 논문 리뷰 위클리 저널", st["Title"]))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(f"Week: {xml_escape(journal.get('week',week))} · Run date: {xml_escape(journal.get('run_date',run_date))}", st["Body"]))
    story.append(Spacer(1, 4*mm))

    # editorial
    ed = journal.get("editorial_ko", {}) or {}
    story.append(Paragraph(ed.get("this_week_takeaway","이번 주 요약"), st["H1"]))
    story.append(Paragraph(bullets(ed.get("bullets", [])), st["Body"]))
    story.append(Spacer(1, 4*mm))

    # themes
    themes = journal.get("themes", []) or []
    if themes:
        rows=[["Theme","Count"]]
        for th in themes[:8]:
            rows.append([th.get("theme",""), str(th.get("count",0))])
        tbl=Table(rows, colWidths=[90*mm, 20*mm], repeatRows=1)
        tbl.setStyle(TableStyle([
            ("FONT",(0,0),(-1,0), bold_font, 10),
            ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#E9EEF6")),
            ("GRID",(0,0),(-1,-1), 0.25, colors.lightgrey),
            ("FONT",(0,1),(-1,-1), base_font, 10),
            ("LEFTPADDING",(0,0),(-1,-1), 5),
            ("RIGHTPADDING",(0,0),(-1,-1), 5),
            ("TOPPADDING",(0,0),(-1,-1), 3),
            ("BOTTOMPADDING",(0,0),(-1,-1), 3),
        ]))
        story.append(tbl)

    story.append(PageBreak())

    # shortlist
    story.append(Paragraph("Shortlist", st["H1"]))
    sl = journal.get("shortlist", []) or []
    if sl:
        header_row=["R","Score","ID","Theme","Date","Title"]
        data=[header_row]
        for it in sl[:20]:
            data.append([
                str(it.get("rank","")),
                str(it.get("score","")),
                xml_escape(it.get("paper_id","")),
                xml_escape(it.get("theme","")),
                xml_escape(it.get("date","")),
                Paragraph(xml_escape(it.get("title","")), st["Small"]),
            ])
        colw=[8*mm, 12*mm, 28*mm, 26*mm, 16*mm, 88*mm]
        tbl=Table(data, colWidths=colw, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("FONT",(0,0),(-1,0), bold_font, 9.5),
            ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#F0F3F7")),
            ("GRID",(0,0),(-1,-1), 0.25, colors.lightgrey),
            ("VALIGN",(0,0),(-1,-1), "TOP"),
            ("LEFTPADDING",(0,0),(-1,-1), 4),
            ("RIGHTPADDING",(0,0),(-1,-1), 4),
            ("TOPPADDING",(0,0),(-1,-1), 3),
            ("BOTTOMPADDING",(0,0),(-1,-1), 3),
        ]))
        story.append(tbl)
    story.append(PageBreak())

    # Paper of the week
    story.append(Paragraph("Paper of the Week", st["H1"]))
    if pow_ and isinstance(pow_, dict):
        r = pow_.get("review_ko", {}) or {}
        story.append(Paragraph(xml_escape(pow_.get("title","")), st["H2"]))
        story.append(Paragraph(f"{xml_escape(pow_.get('paper_id',''))} · {xml_escape(pow_.get('theme',''))}", st["Small"]))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph("<b>한 줄 요약</b><br/>"+bullets([r.get("one_liner","")]), st["Body"]))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph("<b>Why it matters</b><br/>"+bullets(r.get("why_it_matters",[])), st["Body"]))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph("<b>핵심 기여</b><br/>"+bullets(r.get("key_contributions",[])), st["Body"]))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph("<b>실험/수치</b><br/>"+bullets((r.get("experiments",{}) or {}).get("key_results",[])), st["Body"]))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph(f"<b>{xml_escape(domain_label)} 관점 포인트</b><br/>"+bullets(r.get("xr_takeaways",[])), st["Body"]))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph("<b>한계/리스크</b><br/>"+bullets(r.get("limitations",[])), st["Body"]))
        story.append(Spacer(1, 2*mm))
        story.append(Paragraph("<b>랩 액션</b><br/>"+bullets(r.get("lab_actions",[])), st["Body"]))
    story.append(PageBreak())

    # Deep dive
    story.append(Paragraph("Deep Dive", st["H1"]))
    for i, it in enumerate(dd[:5], start=1):
        if not isinstance(it, dict):
            continue
        r = it.get("review_ko", {}) or {}
        story.append(Paragraph(f"{i}. {xml_escape(it.get('title',''))}", st["H2"]))
        story.append(Paragraph(f"{xml_escape(it.get('paper_id',''))} · {xml_escape(it.get('theme',''))}", st["Small"]))
        story.append(Spacer(1, 1.5*mm))
        story.append(Paragraph("<b>요약</b><br/>"+bullets([r.get("one_liner","")]), st["Body"]))
        story.append(Spacer(1, 1.5*mm))
        story.append(Paragraph("<b>기여</b><br/>"+bullets(r.get("key_contributions",[])), st["Body"]))
        story.append(Spacer(1, 1.5*mm))
        story.append(Paragraph("<b>방법(핵심)</b><br/>"+bullets([
            (r.get("method",{}) or {}).get("problem",""),
            (r.get("method",{}) or {}).get("model",""),
        ]), st["Body"]))
        story.append(Spacer(1, 1.5*mm))
        story.append(Paragraph("<b>실험/수치</b><br/>"+bullets((r.get("experiments",{}) or {}).get("key_results",[])), st["Body"]))
        story.append(Spacer(1, 1.5*mm))
        story.append(Paragraph("<b>랩 액션</b><br/>"+bullets(r.get("lab_actions",[])), st["Body"]))
        story.append(Spacer(1, 3*mm))

    story.append(PageBreak())

    # Notable mentions
    story.append(Paragraph("Notable mentions", st["H1"]))
    nm = journal.get("notable_mentions", []) or []
    if nm:
        rows=[["ID","Title","1-liner(한글)"]]
        for it in nm[:20]:
            rows.append([
                xml_escape(it.get("paper_id","")),
                Paragraph(xml_escape(it.get("title","")), st["Small"]),
                Paragraph(xml_escape(it.get("one_liner_ko","") or ""), st["Small"])
            ])
        tbl=Table(rows, colWidths=[28*mm, 82*mm, 62*mm], repeatRows=1)
        tbl.setStyle(TableStyle([
            ("FONT",(0,0),(-1,0), bold_font, 9.5),
            ("BACKGROUND",(0,0),(-1,0), colors.HexColor("#F0F3F7")),
            ("GRID",(0,0),(-1,-1), 0.25, colors.lightgrey),
            ("VALIGN",(0,0),(-1,-1), "TOP"),
            ("LEFTPADDING",(0,0),(-1,-1), 4),
            ("RIGHTPADDING",(0,0),(-1,-1), 4),
            ("TOPPADDING",(0,0),(-1,-1), 3),
            ("BOTTOMPADDING",(0,0),(-1,-1), 3),
        ]))
        story.append(tbl)

    story.append(PageBreak())
    story.append(Paragraph("Appendix", st["H1"]))
    story.append(Paragraph(f"journal.json: {xml_escape(str(journal_path))}", st["Body"]))
    story.append(Paragraph(f"generated_utc: {xml_escape(datetime.now(timezone.utc).isoformat())}", st["Body"]))

    def _on_page(c,d):
        on_page(c,d, base_font=base_font, header=header)

    doc.build(story, onFirstPage=_on_page, onLaterPages=_on_page)
    print(json.dumps({"status":"ok","output":str(out_path)}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
