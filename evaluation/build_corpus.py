"""
Build the synthetic documents the evaluation runs the pipeline on, with
a labels file next to each one listing the PII planted in it.

    python evaluation/build_corpus.py

writes evaluation/corpus/synthetic/:

    hr_onboarding.docx      body, table (header context), header, footer,
                            comment author, image text (OCR), file
                            properties, surname mention
    vendor_review.pptx      title slide, bullets, text box, table,
                            speaker notes, image text (OCR), properties
    access_policy.pdf       text layer: paragraphs, a phone number broken
                            over two lines, ruled table, image text (OCR),
                            file properties
    incident_scan.pdf       scanned page (no text layer, read by OCR):
                            paragraphs and a table with a dark header band
    clean_memo.docx         no PII at all, only values that look like it
    invalid/                files main.py must refuse: empty, renamed, or
                            not what their extension says

Every value is invented: example.com/.org/.net addresses, 555-01xx and
Ofcom drama-range phone numbers, documentation IP ranges (192.0.2.0/24,
198.51.100.0/24), the voided SSN 078-05-1120, and made-up names.

<file>.labels.json (hr_onboarding.docx.labels.json):

    file                the document, in the same folder
    synthetic           true: every value is invented
    expected_outcome    "processed", or "rejected" by the input check
    description
    pii                 [{"text", "type", "where"}]: each PII value as it
                        appears in the document, once per place. The
                        labelling rules of archive/tests/evaluate_detector.py:
                        the value without its label, a name without titles,
                        a name inside an email address is part of the EMAIL
    not_pii             [{"text", "why"}]: values that look like PII but
                        are not. Redaction must leave them alone

Labels for real documents use the same format (see README.md).
"""

import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


HERE = Path(__file__).resolve().parent
SYNTHETIC_DIR = HERE / "corpus" / "synthetic"

# Scanned pages are drawn at this resolution, a common scanner setting
SCAN_DPI = 200


class Labels:
    """
    The labels of one document, collected while it is written. pii() and
    keep() return the value, so a document's text and its labels are
    written once and cannot drift apart.
    """

    def __init__(self, file_name, description, expected_outcome="processed"):
        self.data = {
            "file": file_name,
            "synthetic": True,
            "expected_outcome": expected_outcome,
            "description": description,
            "pii": [],
            "not_pii": [],
        }

    def pii(self, text, pii_type, where):
        self.data["pii"].append({"text": text, "type": pii_type, "where": where})
        return text

    def keep(self, text, why):
        self.data["not_pii"].append({"text": text, "why": why})
        return text

    def save(self, folder):
        path = Path(folder) / f"{self.data['file']}.labels.json"
        path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path


# --------------------------------------------------
# IMAGES
# --------------------------------------------------

def font(size, bold=False):
    names = ("arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else ("arial.ttf", "DejaVuSans.ttf")

    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass

    return ImageFont.load_default(size=size)


def png(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def text_image(lines, width=1000, size=36):
    """
    A picture of some lines of text: a screenshot or a scanned form
    pasted into a document. The extractors read it by OCR.
    """

    image = Image.new("RGB", (width, 40 + len(lines) * 60), "white")
    draw = ImageDraw.Draw(image)

    for number, line in enumerate(lines):
        draw.text((20, 20 + number * 60), line, fill="black", font=font(size))

    return png(image)


# --------------------------------------------------
# DOCX
# --------------------------------------------------

def hr_onboarding(folder):
    from docx import Document
    from docx.shared import Inches

    labels = Labels(
        "hr_onboarding.docx",
        "New-starter record: PII in body text, a table whose headers give the "
        "context, the header and footer, a comment's author, an image and the "
        "file properties, and a person later named by surname alone."
    )
    pii, keep = labels.pii, labels.keep

    doc = Document()

    properties = doc.core_properties
    properties.title = keep("New Starter Record", "document title")
    properties.subject = "Onboarding"
    properties.keywords = "onboarding; payroll"
    properties.comments = "Synthetic evaluation document"
    properties.category = "HR"
    properties.author = pii("Marta Kowalczyk", "PERSON_NAME", "properties")
    properties.last_modified_by = pii("Kenji Arakawa", "PERSON_NAME", "properties")

    section = doc.sections[0]
    section.header.paragraphs[0].text = (
        f"Confidential - prepared by {pii('Laura Okonjo', 'PERSON_NAME', 'header')}"
    )
    section.footer.paragraphs[0].text = (
        f"Questions: {pii('samuel.brandt@example.org', 'EMAIL', 'footer')}"
    )

    doc.add_heading("New Starter Record", level=1)

    intro = doc.add_paragraph(
        f"This record confirms that {pii('Grace Holloway', 'PERSON_NAME', 'body')} joined the "
        f"{keep('Risk Analytics', 'team name')} team in {keep('London', 'city')} on "
        f"{keep('2026-03-02', 'date')}. Her line manager is "
        f"{pii('Omar Siddiqui', 'PERSON_NAME', 'body')} ({pii('EMP-48213', 'EMPLOYEE_ID', 'body')})."
    )
    doc.add_comment(
        intro.runs, text="Start date confirmed with payroll.",
        author=pii("Ingrid Solberg", "PERSON_NAME", "comment"), initials="IS"
    )

    doc.add_paragraph(
        f"She can be reached at {pii('grace.holloway@example.com', 'EMAIL', 'body')} or on "
        f"{pii('+44 7700 900481', 'PHONE', 'body')}."
    )
    doc.add_paragraph(
        f"For payroll, Social Security Number: {pii('078-05-1120', 'US_SSN', 'body')} and passport "
        f"number {pii('K8203917', 'PASSPORT_NUMBER', 'body')} were checked under ticket "
        f"{keep('ISS-2026-0417', 'ticket reference')}, cost centre {keep('4500018832', 'cost centre')}."
    )
    doc.add_paragraph(
        f"{pii('Holloway', 'PERSON_NAME', 'surname mention')} completes the security induction in "
        f"week one, as required by policy {keep('HR-POL-014', 'policy reference')}."
    )

    doc.add_heading("Team Contacts", level=2)

    rows = [
        ["Name", "Email", "Phone", "Employee ID"],
        [
            pii("Nadia Rahman", "PERSON_NAME", "table"),
            pii("nadia.rahman@example.com", "EMAIL", "table"),
            pii("020 7946 0123", "PHONE", "table"),
            pii("EMP-55102", "EMPLOYEE_ID", "table"),
        ],
        [
            pii("Felix Moreau", "PERSON_NAME", "table"),
            pii("f.moreau@example.com", "EMAIL", "table"),
            pii("+33 1 99 00 12 34", "PHONE", "table"),
            # No EMP prefix: only the column header says what it is
            pii("CFG-7731", "EMPLOYEE_ID", "table"),
        ],
    ]
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    table.style = "Table Grid"

    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = value

    doc.add_paragraph("Sign-off sheet:")
    doc.add_paragraph().add_run().add_picture(io.BytesIO(text_image([
        f"Approved by: {pii('Daniel Osei', 'PERSON_NAME', 'image (OCR)')}",
        f"Mobile {pii('+44 7700 900733', 'PHONE', 'image (OCR)')}",
    ])), width=Inches(4))

    doc.save(folder / labels.data["file"])
    return labels.save(folder)


def clean_memo(folder):
    from docx import Document

    labels = Labels(
        "clean_memo.docx",
        "No PII at all: dates, versions, references, counts, places and team "
        "names that look like PII. Every finding is a false positive, and the "
        "verdict should be PASS."
    )
    keep = labels.keep

    doc = Document()

    properties = doc.core_properties
    properties.title = keep("Quarterly Update", "document title")
    properties.author = keep("Risk Office", "team name")
    properties.last_modified_by = keep("Risk Office", "team name")
    properties.comments = "Synthetic evaluation document"

    doc.add_heading("Quarterly Update", level=1)
    doc.add_paragraph(
        f"The steering committee approved budget line {keep('4500018832', 'budget line')} on "
        f"{keep('2026-07-01', 'date')} for the years {keep('2026-2028', 'year range')}."
    )
    doc.add_paragraph(
        f"Release {keep('2.4.1', 'version number')} of the Risk Portal ships to the "
        f"{keep('Rotterdam', 'city')} and {keep('Singapore', 'city')} offices."
    )
    doc.add_paragraph(
        f"Policy {keep('GRP-POL-007', 'policy reference')} remains in force until "
        f"{keep('2027-01-31', 'date')} (see page 14 of 35)."
    )
    doc.add_paragraph(
        f"Contact the {keep('Service Desk', 'team name')} through the intranet portal for access requests."
    )

    rows = [
        ["Metric", "Value"],
        ["Employee count", keep("1500", "a count, not an ID")],
        ["Open findings", keep("37", "a count")],
        ["Build", keep("10.0.19041", "version number")],
    ]
    table = doc.add_table(rows=len(rows), cols=2)
    table.style = "Table Grid"

    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = value

    doc.save(folder / labels.data["file"])
    return labels.save(folder)


# --------------------------------------------------
# PPTX
# --------------------------------------------------

def vendor_review(folder):
    from pptx import Presentation
    from pptx.util import Inches

    labels = Labels(
        "vendor_review.pptx",
        "Vendor review deck: PII in the title slide, bullets, a table whose "
        "header says 'Passport No.', speaker notes, an image and the file "
        "properties."
    )
    pii, keep = labels.pii, labels.keep

    prs = Presentation()

    properties = prs.core_properties
    properties.title = keep("Vendor Risk Review", "document title")
    properties.author = pii("Aisha Bello", "PERSON_NAME", "properties")
    properties.last_modified_by = keep("Vendor Management Office", "team name")
    properties.comments = "Synthetic evaluation document"

    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "Vendor Risk Review Q3 2026"
    slide.placeholders[1].text = f"Presented by {pii('Lucas Ferreira', 'PERSON_NAME', 'title slide')}"

    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Escalation Contacts"
    body = slide.placeholders[1].text_frame
    body.text = (
        f"Account manager: {pii('Hannah Becker', 'PERSON_NAME', 'bullet')} "
        f"({pii('hannah.becker@example.net', 'EMAIL', 'bullet')})"
    )

    for line in (
        f"Escalation line: {pii('+1 (212) 555-0186', 'PHONE', 'bullet')}",
        f"VPN gateway {pii('192.0.2.44', 'IP_ADDRESS', 'bullet')} was exposed for 3 days",
        f"Contract {keep('VND-2026-0042', 'contract reference')}, availability "
        f"{keep('99.9%', 'service level')}",
    ):
        body.add_paragraph().text = line

    slide.shapes.add_textbox(Inches(0.5), Inches(6.5), Inches(6), Inches(0.5)).text_frame.text = (
        f"Prepared for {keep('Northwind Traders', 'company')}, {keep('Rotterdam', 'city')}"
    )
    slide.notes_slide.notes_text_frame.text = (
        f"Remind {pii('Arun Pillai', 'PERSON_NAME', 'speaker notes')} to send the PAN "
        f"{pii('ABCPK1234F', 'INDIAN_PAN', 'speaker notes')} for the vendor file."
    )

    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Site Contacts"
    rows = [
        ["Contact", "Role", "Passport No."],
        [
            pii("Victor Nakamura", "PERSON_NAME", "table"), keep("Site lead", "job title"),
            pii("TN4471902", "PASSPORT_NUMBER", "table"),
        ],
        [
            pii("Sofia Ricci", "PERSON_NAME", "table"), keep("Auditor", "job title"),
            pii("YB2208815", "PASSPORT_NUMBER", "table"),
        ],
    ]
    table = slide.shapes.add_table(len(rows), 3, Inches(0.5), Inches(1.6), Inches(9), Inches(1.5)).table

    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = value

    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_picture(io.BytesIO(text_image([
        "Site visit signed off by",
        f"{pii('Chloe Dubois', 'PERSON_NAME', 'image (OCR)')} on {keep('2026-08-19', 'date')}",
    ])), Inches(1), Inches(1.5), Inches(7))
    slide.shapes.add_textbox(Inches(1), Inches(4.5), Inches(6), Inches(0.5)).text_frame.text = (
        "Photo: site visit record"
    )

    prs.save(folder / labels.data["file"])
    return labels.save(folder)


# --------------------------------------------------
# PDF
# --------------------------------------------------

def access_policy(folder):
    import pymupdf

    labels = Labels(
        "access_policy.pdf",
        "PDF with a text layer: paragraphs, a phone number the layout breaks "
        "over two lines, a ruled table, an image with text and the file "
        "properties."
    )
    pii, keep = labels.pii, labels.keep

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)

    page.insert_text((60, 80), "Access Control Policy", fontsize=20)

    paragraphs = [
        [
            f"Policy {keep('GRP-POL-007', 'policy reference')} supersedes "
            f"{keep('ISS-2026-0233', 'ticket reference')} (see page 14 of 35).",
        ],
        [
            f"The policy owner is {pii('Elena Varga', 'PERSON_NAME', 'body')}, "
            f"{pii('elena.varga@example.com', 'EMAIL', 'body')},",
            f"telephone {pii('+1 (646) 555-0119', 'PHONE', 'body')}.",
        ],
        [
            f"Server {pii('198.51.100.23', 'IP_ADDRESS', 'body')} hosts the identity provider; "
            f"build {keep('10.0.19041', 'version number')} is approved.",
        ],
    ]

    # Broken over two lines, as page layout breaks long lines
    wrapped_phone = pii("+44 7700\n900218", "PHONE", "body (line break)")
    first, second = wrapped_phone.split("\n")
    paragraphs.append([
        f"Out of hours, call {pii('Yusuf Demir', 'PERSON_NAME', 'body')} on {first}",
        f"{second} or the duty manager.",
    ])

    y = 120

    for lines in paragraphs:
        for line in lines:
            page.insert_text((60, y), line, fontsize=11)
            y += 15

        y += 12

    rows = [
        ["Name", "Employee ID", "Mobile"],
        [
            pii("Tomasz Nowak", "PERSON_NAME", "table"), pii("EMP-61234", "EMPLOYEE_ID", "table"),
            pii("+1 (415) 555-0172", "PHONE", "table"),
        ],
        [
            pii("Ines Duarte", "PERSON_NAME", "table"), pii("EMP-61290", "EMPLOYEE_ID", "table"),
            pii("+1 (415) 555-0193", "PHONE", "table"),
        ],
    ]
    left, top, widths, height = 60, y + 10, [170, 140, 170], 26

    for r, row in enumerate(rows):
        x = left

        for c, value in enumerate(row):
            page.draw_rect(pymupdf.Rect(x, top + r * height, x + widths[c], top + (r + 1) * height), width=0.8)
            page.insert_text((x + 6, top + r * height + 17), value, fontsize=11)
            x += widths[c]

    top += len(rows) * height + 30
    page.insert_text((60, top), "Badge record:", fontsize=11)
    page.insert_image(pymupdf.Rect(60, top + 10, 400, top + 110), stream=text_image([
        f"Badge issued to {pii('Rafael Costa', 'PERSON_NAME', 'image (OCR)')}",
        f"Staff ID: {pii('EMP-61377', 'EMPLOYEE_ID', 'image (OCR)')}",
    ]))

    doc.set_metadata({
        "title": "Access Control Policy",
        "author": pii("Mateo Alvarez", "PERSON_NAME", "properties"),
        "subject": "Identity and access",
        "keywords": "IAM; policy",
        "creator": "Evaluation corpus builder",
        "producer": "PyMuPDF",
    })
    doc.save(folder / labels.data["file"])
    return labels.save(folder)


def incident_scan(folder):
    import pymupdf

    labels = Labels(
        "incident_scan.pdf",
        "A scanned page with no text layer, read by OCR: paragraphs, and a "
        "table in the case-study style (dark header band, thin row rules) "
        "whose headers give the context."
    )
    pii, keep = labels.pii, labels.keep

    scale = SCAN_DPI / 72
    width, height = int(612 * scale), int(792 * scale)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    def pt(value):
        return int(value * scale)

    draw.text((pt(60), pt(60)), f"Incident Report {keep('IR-2026-118', 'incident reference')}",
              fill="black", font=font(pt(18), bold=True))

    lines = [
        f"Reported by {pii('Kofi Mensah', 'PERSON_NAME', 'scanned page (OCR)')} on "
        f"{keep('2026-05-14', 'date')}.",
        f"Contact: {pii('kofi.mensah@example.com', 'EMAIL', 'scanned page (OCR)')}, "
        f"{pii('+1 (312) 555-0147', 'PHONE', 'scanned page (OCR)')}.",
        f"Affected host {pii('192.0.2.81', 'IP_ADDRESS', 'scanned page (OCR)')}; ticket "
        f"{keep('ISS-2026-0571', 'ticket reference')}.",
    ]

    for number, line in enumerate(lines):
        draw.text((pt(60), pt(110 + number * 22)), line, fill="black", font=font(pt(12)))

    # The table style pdf_tables.py rebuilds: a dark header band across
    # the table, then rows separated by thin grey rules
    left, right, top = pt(60), pt(552), pt(200)
    columns = [pt(60), pt(230), pt(380)]
    band, row_height = pt(24), pt(28)

    draw.rectangle((left, top, right, top + band), fill=(31, 56, 100))

    for x, label in zip(columns, ["Name", "Employee ID", "Passport Number"]):
        draw.text((x + pt(6), top + pt(5)), label, fill="white", font=font(pt(12), bold=True))

    rows = [
        [
            pii("Priyanka Rao", "PERSON_NAME", "scanned table (OCR)"),
            pii("EMP-70412", "EMPLOYEE_ID", "scanned table (OCR)"),
            pii("P7719024", "PASSPORT_NUMBER", "scanned table (OCR)"),
        ],
        [
            pii("Jonas Weber", "PERSON_NAME", "scanned table (OCR)"),
            pii("EMP-70455", "EMPLOYEE_ID", "scanned table (OCR)"),
            pii("C3390561", "PASSPORT_NUMBER", "scanned table (OCR)"),
        ],
    ]

    for r, row in enumerate(rows):
        y = top + band + r * row_height

        for x, value in zip(columns, row):
            draw.text((x + pt(6), y + pt(7)), value, fill="black", font=font(pt(12)))

        draw.line((left, y + row_height, right, y + row_height), fill=(215, 215, 215), width=2)

    draw.text((pt(60), pt(740)), "Page 1 of 1", fill="black", font=font(pt(9)))

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=png(image))
    doc.set_metadata({"title": "Incident Report", "producer": "Scanner"})
    doc.save(folder / labels.data["file"])
    return labels.save(folder)


# --------------------------------------------------
# INVALID INPUTS
# --------------------------------------------------

def invalid_inputs(folder):
    """
    Files the input check (main.input_problem) must refuse before
    anything is uploaded or extracted.
    """

    folder = Path(folder) / "invalid"
    folder.mkdir(parents=True, exist_ok=True)

    files = {
        "empty.pdf": (b"", "An empty file."),
        "notes_renamed.docx": (b"Plain text notes, renamed to .docx\n", "Text renamed to .docx."),
        "scan_renamed.pptx": (b"%PDF-1.4\n%%EOF\n", "A PDF renamed to .pptx."),
    }

    paths = []

    for name, (content, description) in files.items():
        (folder / name).write_bytes(content)
        paths.append(Labels(name, description, expected_outcome="rejected").save(folder))

    return paths


BUILDERS = (hr_onboarding, clean_memo, vendor_review, access_policy, incident_scan)


def build(folder=SYNTHETIC_DIR):
    """
    Write every synthetic document and its labels into folder. Returns
    the label files written.
    """

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    return [builder(folder) for builder in BUILDERS] + invalid_inputs(folder)


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else SYNTHETIC_DIR

    for path in build(folder):
        labels = json.loads(path.read_text(encoding="utf-8"))
        print(f"  {path.parent.name}/{labels['file']:24} {len(labels['pii']):3} PII values, "
              f"{len(labels['not_pii']):3} look-alikes")

    print(f"Corpus: {folder}")


if __name__ == "__main__":
    main()
