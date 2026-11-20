"""Builds small synthetic SOP PDFs in the same layout as the pack, for edge-case tests."""
from __future__ import annotations

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table


def make_sop_pdf(path, sop_id="SOP-90", version="1.0", title="Test Procedure",
                 access="All clinic staff", sections=None, effective="2026-10-01"):
    sections = sections or [("Purpose", "To test things."), ("Rules", "Always test the parser twice a day.")]
    ss = getSampleStyleSheet()

    def header_footer(canvas, doc):
        canvas.setFont("Helvetica", 8)
        canvas.drawString(40, A4[1] - 30, "MediCare Clinics")
        canvas.drawRightString(A4[0] - 40, A4[1] - 30, f"{sop_id} | Version {version} | Effective {effective}")
        canvas.drawString(40, 20, "Internal document. Uncontrolled when printed.")
        canvas.drawRightString(A4[0] - 40, 20, f"{sop_id} | Page {doc.page}")

    story = [Paragraph(f"{sop_id}: {title}", ss["Heading2"]),
             Table([["Department", "QA", "Owner", "QA Lead"],
                    ["Version", version, "Effective date", effective],
                    ["Access", access, "Review cycle", "Annual"]], style=[("GRID", (0, 0), (-1, -1), 0.25, "grey")]),
             Spacer(1, 10)]
    for i, (h, body) in enumerate(sections, start=1):
        story.append(Paragraph(f"{i}. {h}", ss["Heading4"]))
        story.append(Paragraph(body, ss["BodyText"]))
    story.append(Paragraph("Revision history", ss["Heading4"]))
    story.append(Table([["Version", "Date", "Change"], [version, effective, "Test change."]],
                       style=[("GRID", (0, 0), (-1, -1), 0.25, "grey")]))
    SimpleDocTemplate(str(path), pagesize=A4, topMargin=50, bottomMargin=40).build(
        story, onFirstPage=header_footer, onLaterPages=header_footer)
    return path
