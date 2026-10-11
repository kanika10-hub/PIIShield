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

TOP_KEYS = ["document_id", "file_name", "source_type", "extraction_method", "pages", "metadata"]
BLOCK_KEYS = ["block_id", "block_type", "text", "order", "metadata", "bounding_box", "children"]
BLOCK_TYPES = {
    "heading", "paragraph", "list_item", "table", "table_cell", "image_ocr",
    "image", "chart", "diagram", "embedded_object", "word"
}
LOCATIONS = {"body", "header", "footer", "text_box", "notes", "footnote", "endnote", "comment"}
METHODS = {"native", "ocr", "mixed"}
CONTENT_TYPES = {"PDF": "pdf_page", "PPTX": "slide", "DOCX": "docx_body"}
IMAGE_KEYS = {"ocr_engine", "image_width", "image_height", "ocr_confidence", "word_count", "words"}


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


# --------------------------------------------------
# FORMAT CHECK
# --------------------------------------------------

def all_blocks(blocks):
    for block in blocks:
        yield block
        yield from all_blocks(block.get("children", []))


def check_box(box):
    return box is None or all(
        isinstance(box.get(key), (int, float)) and box[key] >= 0
        for key in ("x", "y", "width", "height")
    )


def check_document(document):
    """
    Every way the document breaks the common format.
    """

    problems = []

    def problem(message):
        if message not in problems:
            problems.append(message)

    if list(document) != TOP_KEYS:
        problem(f"top-level keys {list(document)}")

    source_type = document.get("source_type")

    if source_type not in CONTENT_TYPES:
        problem(f"source_type {source_type!r}")

    if document.get("extraction_method") not in METHODS:
        problem(f"extraction_method {document.get('extraction_method')!r}")

    metadata = document.get("metadata", {})

    if metadata.get("page_count") != len(document.get("pages", [])):
        problem("metadata.page_count does not match the pages")

    if not isinstance(metadata.get("properties"), dict) or not isinstance(metadata.get("limitations"), list):
        problem("metadata needs properties (dict) and limitations (list)")

    ids = []

    for page in document.get("pages", []):
        page_metadata = page.get("metadata", {})
        page_number = page.get("page_number")

        if page_metadata.get("content_type") != CONTENT_TYPES.get(source_type):
            problem(f"page {page_number}: content_type {page_metadata.get('content_type')!r}")

        if page_metadata.get("extraction_method") not in METHODS:
            problem(f"page {page_number}: extraction_method {page_metadata.get('extraction_method')!r}")

        if not isinstance(page_metadata.get("char_count"), int) or not isinstance(page_metadata.get("low_text"), bool):
            problem(f"page {page_number}: needs char_count and low_text")

        if source_type in ("PDF", "PPTX") and not (page_metadata.get("width") and page_metadata.get("height")):
            problem(f"page {page_number}: needs width and height")

        if [block["order"] for block in page["blocks"]] != list(range(1, len(page["blocks"]) + 1)):
            problem(f"page {page_number}: block order is not 1..n")

        for block in all_blocks(page["blocks"]):
            block_id = block.get("block_id")
            block_type = block.get("block_type")
            meta = block.get("metadata", {})
            ids.append(block_id)

            if list(block) != BLOCK_KEYS:
                problem(f"{block_id}: keys {list(block)}")

            if block_type not in BLOCK_TYPES:
                problem(f"{block_id}: block_type {block_type!r}")

            if not check_box(block.get("bounding_box")):
                problem(f"{block_id}: bad bounding_box")

            if block_type != "word" and block.get("text", "").strip():
                if meta.get("source") not in ("native", "ocr"):
                    problem(f"{block_type} blocks need source native/ocr, e.g. {block_id}")
                if meta.get("location") not in LOCATIONS:
                    problem(f"{block_type} blocks need a location, e.g. {block_id}")

            if block_type == "table" and not (meta.get("rows") and meta.get("columns")):
                problem(f"{block_id}: table needs rows and columns")

            if block_type == "table_cell" and not all(
                isinstance(meta.get(key), int) and meta[key] >= 1
                for key in ("row", "column", "row_span", "column_span")
            ):
                problem(f"{block_id}: cell needs row, column, row_span, column_span")

            if block_type == "list_item" and not isinstance(meta.get("level"), int):
                problem(f"{block_id}: list_item needs level")

            if block_type == "image_ocr":
                if not IMAGE_KEYS <= set(meta):
                    problem(f"{block_id}: image_ocr needs {sorted(IMAGE_KEYS - set(meta))}")
                elif block["text"].split() != [word["text"] for word in meta["words"]]:
                    problem(f"{block_id}: OCR words do not match the text")

            if block_type == "image" and not meta.get("requires_ocr"):
                problem(f"{block_id}: image needs requires_ocr")

    duplicates = {block_id for block_id in ids if ids.count(block_id) > 1}

    if duplicates:
        problem(f"duplicate block_ids: {sorted(duplicates)[:5]}")

    return problems


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
            problems = check_document(document)
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
