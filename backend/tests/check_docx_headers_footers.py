
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run

INPUT_FILE = "input/tprm_training.docx"

# Text boxes are stored twice: a drawing (mc:Choice) and a VML
# fallback (mc:Fallback). Only the first copy is read.
NOT_FALLBACK = "not(ancestor::*[local-name()='Fallback'])"


def run_text(paragraph):
    """
    Text of every run in the paragraph, including runs inside
    content controls, but not runs inside text boxes.
    """

    runs = paragraph._p.xpath(".//w:r[not(ancestor::w:txbxContent)]")

    return "".join(Run(run, paragraph).text for run in runs).strip()


def text_box_texts(element):
    """
    Text of each text box inside the element.
    """

    boxes = element.xpath(f".//w:txbxContent[{NOT_FALLBACK}]")

    return [
        "".join(text.text or "" for text in box.iter(qn("w:t"))).strip()
        for box in boxes
    ]


def describe_part(part_element, parent):
    """
    Print the paragraphs, tables, text boxes, images and fields
    of a header or footer, in order.
    """

    lines = []

    for element in part_element.iterchildren():

        if element.tag == qn("w:p"):

            text = run_text(Paragraph(element, parent))

            if text:
                lines.append(f"    paragraph: {text}")

        elif element.tag == qn("w:tbl"):

            table = Table(element, parent)

            for row_index, row in enumerate(table.rows, start=1):
                cells = [
                    " / ".join(
                        run_text(paragraph)
                        for paragraph in cell.paragraphs
                        if run_text(paragraph)
                    )
                    for cell in row.cells
                ]
                lines.append(f"    table row {row_index}: {cells}")

    for text in text_box_texts(part_element):
        lines.append(f"    text box: {text or '[empty]'}")

    images = part_element.xpath(f".//w:drawing[{NOT_FALLBACK}]//a:blip")

    for blip in images:
        rel_id = blip.get(qn("r:embed"))
        target = parent.part.rels[rel_id].target_ref if rel_id else "?"
        lines.append(f"    image: {target}")

    fields = [
        text.strip()
        for text in part_element.xpath(".//w:instrText/text()")
    ] + [
        field.get(qn("w:instr")).strip()
        for field in part_element.xpath(".//w:fldSimple")
    ]

    if fields:
        lines.append(f"    fields: {fields}")

    return lines or ["    [no content]"]


doc = Document(INPUT_FILE)

odd_and_even = doc.settings.odd_and_even_pages_header_footer

print("========== HEADERS AND FOOTERS ==========")
print("Sections:", len(doc.sections))
print("Different odd/even pages:", odd_and_even)

for section_index, section in enumerate(doc.sections, start=1):

    first_page = section.different_first_page_header_footer

    print(f"\nSection {section_index}")
    print("Different first page:", first_page)

    parts = [
        ("Default header", section.header, True),
        ("Default footer", section.footer, True),
        ("First-page header", section.first_page_header, first_page),
        ("First-page footer", section.first_page_footer, first_page),
        ("Even-page header", section.even_page_header, odd_and_even),
        ("Even-page footer", section.even_page_footer, odd_and_even),
    ]

    for name, header_footer, shown in parts:

        # Checking is_linked_to_previous first avoids python-docx
        # creating an empty definition when .paragraphs is accessed.
        if header_footer.is_linked_to_previous:
            status = "same as previous section" if section_index > 1 else "not defined"
            print(f"\n  {name}: {status}")
            continue

        part = header_footer.part

        visibility = "shown" if shown else "defined but NOT shown in Word"

        print(f"\n  {name}: {part.partname} ({visibility})")

        for line in describe_part(header_footer._element, header_footer):
            print(line)

print("\n=========================================")
