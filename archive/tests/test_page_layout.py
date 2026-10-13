import sys
import tempfile
from pathlib import Path

import pymupdf

from src.extractors.pdf_extractor import extract_pdf
from src.models.document import BoundingBox, DocumentBlock
from src.structure.page_layout import reading_order

# Page layout of PDFs with a text layer (src/structure/page_layout.py):
# running headers, footers and page numbers are labelled and moved to the
# start and end of their page, and a page in two columns is read left
# column first. Each case writes its own small PDF; nothing is OCR'd.


def write_pdf(folder, name, pages):
    """
    pages: for each page, (x, y, text, font size) lines.
    """

    path = Path(folder) / name
    doc = pymupdf.open()

    for lines in pages:
        page = doc.new_page(width=612, height=792)

        for x, y, text, size in lines:
            page.insert_text((x, y), text, fontsize=size)

    doc.save(path)
    return path


def blocks(path):
    return [
        (page.page_number, block.text.replace("\n", " "), block.metadata.get("location"))
        for page in extract_pdf(str(path)).pages
        for block in page.blocks
    ]


def paragraph(x, y, lines, size=10):
    return [(x, y + 13 * number, line, size) for number, line in enumerate(lines)]


def case_two_columns(folder):
    left = paragraph(60, 110, ["Left one, first line of text", "and its second line here."]) \
        + paragraph(60, 160, ["Left two, first line of text", "and its second line here."])
    right = paragraph(330, 110, ["Right one, first line of text", "and its second line here."]) \
        + paragraph(330, 160, ["Right two, first line of text", "and its second line here."])
    path = write_pdf(folder, "columns.pdf", [[(60, 70, "Bulletin Title Across Both", 20), *left, *right]])
    texts = [text.split(",")[0] for _, text, _ in blocks(path)]

    return texts == ["Bulletin Title Across Both", "Left one", "Left two", "Right one", "Right two"], texts


def case_labels_beside_values_are_not_columns(folder):
    # Single-line blocks either side of a gap, as a form's labels and values
    def block(number, x, y, text):
        return DocumentBlock(
            block_id=f"b{number}", block_type="paragraph", text=text,
            bounding_box=BoundingBox(x=x, y=y, width=120, height=12), metadata={"line_count": 1}
        )

    form = [block(1, 60, 110, "Owner"), block(2, 330, 110, "Risk Office"),
            block(3, 60, 130, "Status"), block(4, 330, 130, "Approved")]
    texts = [item.text for item in reading_order(form, 612)]

    return texts == ["Owner", "Risk Office", "Status", "Approved"], texts


def case_running_header_and_page_numbers(folder):
    pages = [
        [(60, 40, "Cadence Group | Internal", 8), *paragraph(60, 100, [f"Body text of page {number}", "goes on here."], 11),
         (280, 765, f"Page {number} of 2", 8)]
        for number in (1, 2)
    ]
    path = write_pdf(folder, "report.pdf", pages)
    found = blocks(path)
    expected = [
        (1, "Cadence Group | Internal", "header"), (1, "Body text of page 1 goes on here.", "body"),
        (1, "Page 1 of 2", "footer"),
        (2, "Cadence Group | Internal", "header"), (2, "Body text of page 2 goes on here.", "body"),
        (2, "Page 2 of 2", "footer"),
    ]

    return found == expected, found


def case_single_page_number_is_a_footer(folder):
    lines = [*paragraph(60, 60, ["A short note near the top of a one-page document,",
                                 "long enough to be read from its text layer."], 11),
             (300, 770, "3", 9)]
    path = write_pdf(folder, "note.pdf", [lines])
    found = blocks(path)

    return [location for _, _, location in found] == ["body", "footer"], found


def case_text_near_the_top_once_stays_in_the_body(folder):
    # In the header band, but on one page only and not a page number
    lines = [(60, 40, "Board briefing for the March meeting", 9),
             *paragraph(60, 100, ["The briefing covers three topics", "in the order below."], 11)]
    path = write_pdf(folder, "briefing.pdf", [lines])
    found = blocks(path)

    return [location for _, _, location in found] == ["body", "body"], found


CASES = [
    ("Two columns are read left column first, after a title across both", case_two_columns),
    ("Labels beside their values are not two columns", case_labels_beside_values_are_not_columns),
    ("A running header and page numbers are labelled and put first and last", case_running_header_and_page_numbers),
    ("A page number alone on one page is a footer", case_single_page_number_is_a_footer),
    ("Text near the top of one page only stays in the body", case_text_near_the_top_once_stays_in_the_body),
]

failures = 0

print("========== PAGE LAYOUT TEST ==========\n")

with tempfile.TemporaryDirectory() as folder:
    for description, case in CASES:
        passed, actual = case(folder)
        failures += not passed
        print(f"{'PASS' if passed else 'FAIL'}  {description}")

        if not passed:
            print(f"        actual: {actual}")

print(f"\n{len(CASES) - failures} of {len(CASES)} cases passed")
print("======================================")

sys.exit(1 if failures else 0)
