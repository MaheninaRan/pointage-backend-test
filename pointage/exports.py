"""Tabular exports in CSV, Excel and PDF."""
import csv
import io

from django.http import HttpResponse


def export_table(filename, title, headers, rows, fmt="csv"):
    fmt = (fmt or "csv").lower()
    if fmt == "xlsx":
        return _xlsx(filename, title, headers, rows)
    if fmt == "pdf":
        return _pdf(filename, title, headers, rows)
    return _csv(filename, headers, rows)


def _csv(filename, headers, rows):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
    response.write("﻿")  # BOM so Excel opens accents correctly
    writer = csv.writer(response, delimiter=";")
    writer.writerow(headers)
    writer.writerows(rows)
    return response


def _xlsx(filename, title, headers, rows):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "".join(c for c in title if c not in '[]:*?/\\')[:31]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E8C")
    for row in rows:
        ws.append(list(row))
    for i, header in enumerate(headers, start=1):
        width = max([len(str(header))] + [len(str(r[i - 1])) for r in rows[:500] if i - 1 < len(r)])
        ws.column_dimensions[get_column_letter(i)].width = min(40, width + 2)
    ws.freeze_panes = "A2"
    buffer = io.BytesIO()
    wb.save(buffer)
    response = HttpResponse(buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{filename}.xlsx"'
    return response


def _pdf(filename, title, headers, rows):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    wide = len(headers) > 7
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4) if wide else A4, leftMargin=24, rightMargin=24, topMargin=28, bottomMargin=28, title=title)
    styles = getSampleStyleSheet()
    font_size = 6 if len(headers) > 20 else 8
    data = [headers] + [[str(c) for c in r] for r in rows]
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E8C")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#C7CED9")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F5FA")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    doc.build([Paragraph(title, styles["Title"]), Spacer(1, 8), table])
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}.pdf"'
    return response


def report_pdf(report):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, title=report.title)
    styles = getSampleStyleSheet()
    author = f"{report.user.first_name} {report.user.last_name}".strip() or report.user.username

    def paragraphs(text):
        return [Paragraph(escape(line) or "&nbsp;", styles["BodyText"]) for line in (text or "").splitlines()]

    story = [
        Paragraph(escape(report.title), styles["Title"]),
        Paragraph(f"{escape(author)} — {report.date:%d/%m/%Y} — {report.get_status_display()}", styles["Normal"]),
        Spacer(1, 12), Paragraph("Contenu", styles["Heading2"]), *paragraphs(report.content),
    ]
    if report.tasks:
        story += [Spacer(1, 8), Paragraph("Tâches réalisées", styles["Heading2"]), *paragraphs(report.tasks)]
    if report.manager_comment:
        story += [Spacer(1, 8), Paragraph("Commentaire du manager", styles["Heading2"]), *paragraphs(report.manager_comment)]
    attachments = list(report.attachments.all())
    if attachments:
        story += [Spacer(1, 8), Paragraph("Pièces jointes", styles["Heading2"])]
        story += [Paragraph(escape(a.filename or a.file.name), styles["BodyText"]) for a in attachments]
    doc.build(story)
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="rapport-{report.date}-{report.id}.pdf"'
    return response
