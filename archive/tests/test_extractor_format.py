"""
Check that the PDF, PPTX and DOCX extractors all produce the common
format described in src/models/document.py.

    python -m tests.test_extractor_format            # samples + input/
    python -m tests.test_extractor_format --samples  # samples only (fast)

The sample files are built in a temporary folder and cover what the
case-study files do not: a PDF with a text layer, a table with merged
cells and an image with text, a scanned page and a blank page; a PPTX
with bullets, a text box, a group, merged cells, a chart, an embedded
object, speaker notes and a hidden slide; a DOCX with lists, sections,
a header image, a comment, merged and nested table cells.
"""

import io
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from main import extract_document
from src.models.schema import all_blocks, format_problems


# --------------------------------------------------
# SAMPLE FILES
# --------------------------------------------------

def text_image(lines):
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except OSError:
        font = ImageFont.load_default(size=36)

    image = Image.new("RGB", (900, 200), "white")
    draw = ImageDraw.Draw(image)

    for number, line in enumerate(lines):
        draw.text((20, 20 + number * 60), line, fill="black", font=font)

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


IMAGE = text_image(["Approved by: Priya Sharma", "Mobile +44 7700 900123"])


def make_pdf(path):
    import pymupdf

    doc = pymupdf.open()

    # Page with a text layer: heading, paragraph, table, image
    page = doc.new_page()
    page.insert_text((60, 80), "1. Vendor Contacts", fontsize=20)
    page.insert_text((60, 115), "Escalations go to Dana Whitfield, dana.whitfield@example.com.", fontsize=11)
    page.insert_text((60, 130), "Call +1 (212) 555-0147 during business hours.", fontsize=11)

    left, top, widths, height = 60, 160, [200, 200], 28
    rows = [["Name", "Passport Number"], ["Ravi Menon", "TK4820913"], ["Merged note for both columns", None]]

    for r, row in enumerate(rows):
        y = top + r * height
        x = left
        page.draw_line((left, y), (left + sum(widths), y))
        for c, value in enumerate(row):
            if value:
                page.insert_text((x + 5, y + 18), value, fontsize=11)
            x += widths[c]
        page.draw_line((left, y), (left, y + height))
        page.draw_line((left + sum(widths), y), (left + sum(widths), y + height))
        if r < 2:
            page.draw_line((left + widths[0], y), (left + widths[0], y + height))

    page.draw_line((left, top + 3 * height), (left + sum(widths), top + 3 * height))
    page.insert_image(pymupdf.Rect(60, 280, 420, 360), stream=IMAGE)

    # Scanned page: an image of text, no text layer
    scanned = doc.new_page()
    scanned.insert_image(pymupdf.Rect(40, 40, 555, 154), stream=text_image([
        "Policy owner: Miriam Achebe", "Contact: m.achebe@example.com"
    ]))

    # Blank page
    doc.new_page()

    doc.set_metadata({"author": "Morgan Ellery", "title": "Sample policy"})
    doc.save(path)


def make_pptx(path):
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.enum.shapes import PROG_ID
    from pptx.util import Inches

    prs = Presentation()

    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Escalation Contacts"
    body = slide.placeholders[1].text_frame
    body.text = "Primary: Dana Whitfield"
    item = body.add_paragraph()
    item.text = "Backup: Ravi Menon, +1 (212) 555-0147"
    item.level = 1
    slide.shapes.add_textbox(Inches(0.5), Inches(5.6), Inches(4), Inches(0.5)).text_frame.text = "Prepared by Lena Okafor"
    group = slide.shapes.add_group_shape()
    group.shapes.add_textbox(Inches(6), Inches(5.6), Inches(3), Inches(0.5)).text_frame.text = "lena.okafor@example.com"
    slide.shapes.add_picture(io.BytesIO(IMAGE), Inches(5), Inches(1.5), Inches(4.5), Inches(1))
    slide.notes_slide.notes_text_frame.text = "Speaker note: call Dana on 555-0199"

    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Vendor Register"
    table = slide.shapes.add_table(3, 3, Inches(0.5), Inches(1.5), Inches(6), Inches(1.5)).table
    for r, row in enumerate([["Vendor", "Owner", "Passport Number"], ["Acme", "Ravi Menon", "TK4820913"], ["Merged", "", ""]]):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    table.cell(2, 0).merge(table.cell(2, 2))

    data = CategoryChartData()
    data.categories = ["Dana Whitfield", "Ravi Menon"]
    data.add_series("Open issues", (3, 5))
    chart = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.5), Inches(3.3), Inches(4), Inches(3), data).chart
    chart.has_title = True
    chart.chart_title.text_frame.text = "Issues by owner"
    slide.shapes.add_ole_object(io.BytesIO(b"workbook"), PROG_ID.XLSX, Inches(6), Inches(4))

    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide._element.set("show", "0")

    prs.core_properties.author = "Morgan Ellery"
    prs.save(path)


def make_docx(path):
    from docx import Document
    from docx.enum.section import WD_SECTION
    from docx.shared import Inches

    doc = Document()
    doc.core_properties.author = "Morgan Ellery"

    header = doc.sections[0].header
    header.paragraphs[0].text = "Confidential - prepared by Lena Okafor"
    header.add_paragraph().add_run().add_picture(io.BytesIO(IMAGE), width=Inches(3))

    doc.add_heading("Vendor Contacts", level=1)
    intro = doc.add_paragraph("Escalations go to Dana Whitfield on +1 (212) 555-0147.")
    doc.add_paragraph("Primary: Ravi Menon", style="List Bullet")
    doc.add_paragraph("Backup: dana.whitfield@example.com", style="List Bullet 2")
    doc.add_comment(intro.runs, text="Check with Priya before sending", author="Sam Patel", initials="SP")
    doc.add_paragraph().add_run().add_picture(io.BytesIO(IMAGE), width=Inches(4))

    table = doc.add_table(rows=3, cols=3)
    for r, row in enumerate([["Name", "Passport Number", "Notes"], ["Ravi Menon", "TK4820913", ""], ["Dana Whitfield", "", ""]]):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    table.cell(1, 2).merge(table.cell(2, 2)).text = "Shared note"
    table.cell(2, 1).add_table(rows=1, cols=1).cell(0, 0).text = "Nested: SSN 123-45-6789"

    doc.add_section(WD_SECTION.NEW_PAGE)
    doc.add_paragraph("Second section text for Ravi Menon.")
    doc.save(path)


def main():

    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder)
        samples = []

        for name, make in (("sample.pdf", make_pdf), ("sample.pptx", make_pptx), ("sample.docx", make_docx)):
            make(folder / name)
            samples.append(folder / name)

        files = samples

        if "--samples" not in sys.argv:
            files += sorted(
                path for path in Path("input").iterdir()
                if path.suffix.lower() in (".pdf", ".pptx", ".docx")
            )

        failures = 0

        for path in files:
            document = extract_document(path).to_dict()
            problems = format_problems(document)
            failures += bool(problems)

            blocks = [block for page in document["pages"] for block in all_blocks(page["blocks"])]
            types = sorted({block["block_type"] for block in blocks} - {"word"})

            print(f"{'FAIL' if problems else 'PASS'}  {path.name:28} {document['extraction_method']:6} "
                  f"pages={len(document['pages'])} blocks={len(blocks)} types={types}")

            for message in problems:
                print(f"        {message}")

    print(f"\n{len(files) - failures} of {len(files)} files follow the common format")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
