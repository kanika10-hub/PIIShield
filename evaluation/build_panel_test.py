"""
Build the six held-out test documents for the panel report, with a
labels file next to each one listing every PII value planted in it.

    python evaluation/build_panel_test.py
    python evaluation/run_evaluation.py --corpus evaluation/panel_test

writes evaluation/panel_test/:

    contractor_onboarding.docx   details table (label | value) with a
                                 three-line address, IBAN and IDs in
                                 sentences, bulleted list, approval stamp
                                 image (OCR), comment, header, footer,
                                 properties, a surname mention
    assignment_record.docx       assignee table (dates of birth beside
                                 start dates), PESEL, Aadhaar, PAN, SSN, NI
                                 number, Polish and US addresses, card
                                 numbers in a list, work permit image (OCR)
    tprm_training.pptx           title slide, contact bullets, a table with
                                 no PII, vendor-record screenshot (OCR),
                                 grouped text boxes, speaker notes
    incident_briefing.pptx       IP addresses in bullets, a table and a
                                 screenshot (OCR), a card and a bank
                                 account number, timeline text boxes, notes
    due_diligence_report.pdf     text layer, two pages: running header,
                                 numbered headings, ruled table, IDs and
                                 addresses in paragraphs, badge image (OCR)
    access_request_scan.pdf      a scanned form with no text layer: grey
                                 paper, specks, tilted half a degree, JPEG

These documents are a test set: they were written once, before the
pipeline was run on them, and no detection rule may be tuned on them.
Every value is invented: example.com/.org/.net addresses, 555-01xx and
Ofcom drama-range phone numbers, documentation IP ranges, card-scheme
test numbers, the published example IBANs, the voided SSN 219-09-9999,
QQ-prefix NI numbers, and made-up names and addresses. Check digits
(Luhn, IBAN, PESEL, Verhoeff) are computed here, independently of the
detector.
"""

import io
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from build_corpus import SCAN_DPI, Labels, font, png


HERE = Path(__file__).resolve().parent
PANEL_DIR = HERE / "panel_test"

# The scanned form is tilted this many degrees, as a sheet fed slightly
# askew into a scanner
SCAN_SKEW = 0.5


# --------------------------------------------------
# CHECK DIGITS
# --------------------------------------------------

def luhn_valid(number):
    digits = [int(d) for d in number if d.isdigit()][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
    return total % 10 == 0


def iban_valid(iban):
    compact = iban.replace(" ", "")
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


def pesel(first_ten):
    weights = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
    check = (10 - sum(int(d) * w for d, w in zip(first_ten, weights)) % 10) % 10
    return f"{first_ten}{check}"


VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6), (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4), (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2), (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
VERHOEFF_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def verhoeff_valid(number):
    check = 0
    for position, digit in enumerate(reversed(number)):
        check = VERHOEFF_D[check][VERHOEFF_P[position % 8][int(digit)]]
    return check == 0


def aadhaar(first_eleven):
    check = next(str(d) for d in range(10) if verhoeff_valid(f"{first_eleven}{d}"))
    number = f"{first_eleven}{check}"
    return f"{number[:4]} {number[4:8]} {number[8:]}"


# The algorithms are checked on published examples before they are used
assert verhoeff_valid("2363") and not verhoeff_valid("2364")
assert pesel("4405140135") == "44051401359"
assert luhn_valid("4111 1111 1111 1111") and not luhn_valid("4111 1111 1111 1112")
assert iban_valid("GB82 WEST 1234 5698 7654 32")


# --------------------------------------------------
# IMAGES
# --------------------------------------------------

def screenshot(title, rows, width=1100, size=30):
    """
    A screenshot of an application record pasted into a document: a dark
    title bar with white text, then label/value rows on white with faint
    separators. Returns the PNG and the text it shows, line by line.
    """

    bar, row_height = 70, 56
    image = Image.new("RGB", (width, bar + 24 + len(rows) * row_height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, bar), fill=(31, 56, 100))
    draw.text((24, 18), title, fill="white", font=font(size, bold=True))

    for number, line in enumerate(rows):
        y = bar + 20 + number * row_height
        draw.text((24, y), line, fill="black", font=font(size))
        draw.line((24, y + row_height - 10, width - 24, y + row_height - 10), fill=(225, 225, 225), width=1)

    return png(image), "\n".join([title, *rows])


# --------------------------------------------------
# DOCX
# --------------------------------------------------

def contractor_onboarding(folder):
    from docx import Document
    from docx.shared import Inches

    labels = Labels(
        "contractor_onboarding.docx",
        "Contractor onboarding form: a details table (label | value) with a three-line home "
        "address, an IBAN and an employee ID in sentences, a bulleted screening list with a "
        "referee and a surname mention, an approval stamp image, a reviewer's comment, the "
        "header, footer and file properties."
    )
    pii, keep, element = labels.pii, labels.keep, labels.element

    doc = Document()

    properties = doc.core_properties
    properties.title = keep("Contractor Onboarding Form", "document title")
    properties.subject = "Contractor onboarding"
    properties.keywords = "onboarding; contractor; procurement"
    properties.category = "Procurement"
    properties.comments = "Synthetic test document"
    properties.author = pii("Claire Whitaker", "PERSON_NAME", "properties")
    properties.last_modified_by = pii("Henrik Lund", "PERSON_NAME", "properties")

    section = doc.sections[0]
    section.header.paragraphs[0].text = element("Cadence Group | Procurement | Confidential", "paragraph", "header")
    section.footer.paragraphs[0].text = element(
        f"Form {keep('PRC-F-11', 'form reference')} v2.3 | Form owner: "
        f"{pii('Claire Whitaker', 'PERSON_NAME', 'footer')}",
        "paragraph", "footer"
    )

    doc.add_heading(element("Contractor Onboarding Form", "heading"), level=1)
    doc.add_paragraph(element(
        f"This form records the onboarding of an independent contractor to the Vendor Risk "
        f"Remediation project ({keep('PRJ-2026-044', 'project code')}). The engagement starts on "
        f"{keep('2 March 2026', 'start date')} and runs for six months at a day rate of "
        f"{keep('£650', 'amount')}.",
        "paragraph"
    ))

    doc.add_heading(element("1. Contractor details", "heading"), level=2)

    rows = [
        ["Field", "Details"],
        ["Full name", pii("Siobhan Gallagher", "PERSON_NAME", "table")],
        ["Trading name", keep("Northfield Data Services", "company name")],
        ["Company number", keep("08451277", "company registration number")],
        ["Date of birth", pii("14/09/1986", "DATE_OF_BIRTH", "table")],
        ["Home address", pii("27 Kingsley Road\nBristol\nBS7 8LQ", "POSTAL_ADDRESS", "table")],
        ["Email", pii("siobhan.gallagher@example.com", "EMAIL", "table")],
        ["Mobile", pii("07700 900417", "PHONE", "table")],
        ["National Insurance number", pii("QQ 88 77 66 D", "NATIONAL_ID", "table")],
        ["Passport number", pii("548120773", "PASSPORT_NUMBER", "table")],
    ]
    table = doc.add_table(rows=len(rows), cols=2)
    table.style = "Table Grid"

    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = element(value, "table_cell", table=1, row=r + 1, column=c + 1)

    doc.add_heading(element("2. Payment details", "heading"), level=2)

    iban = pii("GB29 NWBK 6016 1331 9268 19", "BANK_ACCOUNT", "body")
    assert iban_valid(iban)
    payment_text = element(
        f"Monthly invoices are paid into her personal account, IBAN {iban}. Expenses above "
        f"{keep('£500', 'amount')} need prior approval from the engagement manager, "
        f"{pii('Marcus Oyelaran', 'PERSON_NAME', 'body')} (employee ID "
        f"{pii('EMP-30418', 'EMPLOYEE_ID', 'body')}).",
        "paragraph"
    )
    payment = doc.add_paragraph(payment_text)
    doc.add_comment(
        payment.runs,
        text=element(
            "Check the IBAN against the bank letter before the first payment.",
            "paragraph", "comment", anchor=payment_text
        ),
        author=pii("Henrik Lund", "PERSON_NAME", "comment"), initials="HL"
    )

    doc.add_heading(element("3. Background screening", "heading"), level=2)

    for line in (
        f"Right-to-work check completed on {keep('18 February 2026', 'date')} "
        f"(case {keep('RTW-77120', 'case reference')}).",
        "Credit reference check returned no adverse findings.",
        f"Employment reference given by her previous manager, "
        f"{pii('Tomás Herrera', 'PERSON_NAME', 'list item')}, on {pii('0161 496 0732', 'PHONE', 'list item')}.",
        f"Ms {pii('Gallagher', 'PERSON_NAME', 'surname mention')} must return the signed NDA before "
        f"system access is granted.",
    ):
        doc.add_paragraph(element(line, "list_item"), style="List Bullet")

    doc.add_heading(element("4. Approval", "heading"), level=2)
    doc.add_paragraph(element("Approval stamp (scanned):", "paragraph"))

    image, text = screenshot("APPROVED - PROCUREMENT", [
        f"Approved by: {pii('Marcus Oyelaran', 'PERSON_NAME', 'image (OCR)')}",
        f"Date: {keep('24/02/2026', 'approval date')}",
    ])
    element(text, "image_text")
    doc.add_paragraph().add_run().add_picture(io.BytesIO(image), width=Inches(4.5))

    doc.save(folder / labels.data["file"])
    return labels.save(folder)


def assignment_record(folder):
    from docx import Document
    from docx.shared import Inches

    labels = Labels(
        "assignment_record.docx",
        "International assignment record: an assignee table with dates of birth beside start "
        "dates, national IDs of three countries (UK NI, PESEL, Aadhaar) with an Indian PAN and a "
        "US SSN, a Polish and a US home address, card numbers in a bulleted list, a work permit "
        "screenshot, contact details, the header, footer and file properties."
    )
    pii, keep, element = labels.pii, labels.keep, labels.element

    okafor = "Daniel Okafor"
    wojcik = "Agnieszka Wójcik"
    raman = "Priya Raman"

    doc = Document()

    properties = doc.core_properties
    properties.title = keep("International Assignment Record", "document title")
    properties.subject = "Global mobility"
    properties.keywords = "assignment; payroll; tax"
    properties.category = "HR"
    properties.comments = "Synthetic test document"
    properties.author = pii("Fiona MacLeod", "PERSON_NAME", "properties")
    properties.last_modified_by = keep("Global Mobility", "team name")

    section = doc.sections[0]
    section.header.paragraphs[0].text = element("HR Confidential - Global Mobility", "paragraph", "header")
    section.footer.paragraphs[0].text = element(
        f"Policy {keep('HR-MOB-004', 'policy reference')} v2.0", "paragraph", "footer"
    )

    doc.add_heading(element("International Assignment Record", "heading"), level=1)
    doc.add_paragraph(element(
        f"Three employees start long-term assignments in {keep('July 2026', 'month')}. Tax "
        f"equalisation is handled under case {keep('GM-2026-0193', 'case reference')} and allowances "
        f"follow policy HR-MOB-004.",
        "paragraph"
    ))

    doc.add_heading(element("Assignees", "heading"), level=2)

    rows = [
        ["Name", "Employee ID", "Host location", "Start date", "Date of birth"],
        [
            pii(okafor, "PERSON_NAME", "table"), pii("EMP-41207", "EMPLOYEE_ID", "table"),
            keep("Warsaw", "city"), keep("01/07/2026", "start date"),
            pii("04/03/1979", "DATE_OF_BIRTH", "table"),
        ],
        [
            pii(wojcik, "PERSON_NAME", "table"), pii("EMP-41388", "EMPLOYEE_ID", "table"),
            keep("Pune", "city"), keep("15/07/2026", "start date"),
            pii("22/11/1990", "DATE_OF_BIRTH", "table"),
        ],
        [
            pii(raman, "PERSON_NAME", "table"), pii("EMP-41412", "EMPLOYEE_ID", "table"),
            keep("Chicago", "city"), keep("03/08/2026", "start date"),
            pii("09/06/1985", "DATE_OF_BIRTH", "table"),
        ],
    ]
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    table.style = "Table Grid"

    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = element(value, "table_cell", table=1, row=r + 1, column=c + 1)

    doc.add_heading(element("Identity and tax details", "heading"), level=2)

    pesel_number = pesel("9011220756")
    aadhaar_number = aadhaar("48317290512")

    for line in (
        f"{pii(okafor, 'PERSON_NAME', 'body')} stays on UK payroll under National Insurance number "
        f"{pii('QQ 12 34 56 C', 'NATIONAL_ID', 'body')}.",
        f"{pii(wojcik, 'PERSON_NAME', 'body')} (PESEL {pii(pesel_number, 'NATIONAL_ID', 'body')}) keeps her "
        f"home address at {pii('ul. Lipowa 14/3, 00-950 Warszawa', 'POSTAL_ADDRESS', 'body')} for tax purposes.",
        f"{pii(raman, 'PERSON_NAME', 'body')} holds Aadhaar {pii(aadhaar_number, 'NATIONAL_ID', 'body')} and "
        f"PAN {pii('AFKPR7741M', 'INDIAN_PAN', 'body')}. For US payroll she was issued Social Security "
        f"number {pii('219-09-9999', 'US_SSN', 'body')}; her address in the US is "
        f"{pii('2250 North Clark Street, Apt 5B, Chicago, IL 60614', 'POSTAL_ADDRESS', 'body')}.",
    ):
        doc.add_paragraph(element(line, "paragraph"))

    doc.add_heading(element("Corporate cards", "heading"), level=2)

    visa = pii("4111 1111 1111 1111", "PAYMENT_CARD", "list item")
    amex = pii("3782 822463 10005", "PAYMENT_CARD", "list item")
    assert luhn_valid(visa) and luhn_valid(amex)

    for line in (
        f"{pii(okafor, 'PERSON_NAME', 'list item')}: Visa {visa}, monthly limit {keep('£5,000', 'amount')}",
        f"{pii(raman, 'PERSON_NAME', 'list item')}: American Express {amex}, monthly limit "
        f"{keep('$8,000', 'amount')}",
        f"{pii(wojcik, 'PERSON_NAME', 'list item')}: card to be issued in {keep('August 2026', 'month')}",
    ):
        doc.add_paragraph(element(line, "list_item"), style="List Bullet")

    doc.add_heading(element("Work permit", "heading"), level=2)
    doc.add_paragraph(element("Work permit confirmation (screenshot):", "paragraph"))

    image, text = screenshot("WORK PERMIT CONFIRMATION", [
        f"Holder: {pii(okafor, 'PERSON_NAME', 'image (OCR)')}",
        f"Passport: {pii('120394857', 'PASSPORT_NUMBER', 'image (OCR)')}",
        f"Permit reference: {keep('WP/2026/55812', 'permit reference')}",
        f"Valid until: {keep('30/06/2028', 'expiry date')}",
    ])
    element(text, "image_text")
    doc.add_paragraph().add_run().add_picture(io.BytesIO(image), width=Inches(4.5))

    doc.add_heading(element("Contacts", "heading"), level=2)
    doc.add_paragraph(element(
        f"Questions go to the mobility adviser, {pii('Fiona MacLeod', 'PERSON_NAME', 'body')}, at "
        f"{pii('fiona.macleod@example.org', 'EMAIL', 'body')} or {pii('+1 (312) 555-0164', 'PHONE', 'body')}.",
        "paragraph"
    ))

    doc.save(folder / labels.data["file"])
    return labels.save(folder)


# --------------------------------------------------
# PPTX
# --------------------------------------------------

def tprm_training(folder):
    from pptx import Presentation
    from pptx.util import Inches

    labels = Labels(
        "tprm_training.pptx",
        "Third-party risk training deck: title slide, contact bullets, a risk-tier table with no "
        "PII, a vendor-record screenshot, grouped text boxes, speaker notes and file properties."
    )
    pii, keep, element = labels.pii, labels.keep, labels.element

    lead = "Nkechi Adeyemi"

    prs = Presentation()
    properties = prs.core_properties
    properties.title = keep("Third-Party Risk Management: Analyst Training", "document title")
    properties.subject = "TPRM training"
    properties.comments = "Synthetic test document"
    properties.author = pii(lead, "PERSON_NAME", "properties")
    # python-pptx's default names a real person here
    properties.last_modified_by = keep("Learning and Development", "team name")

    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = element("Third-Party Risk Management: Analyst Training", "heading", page=1)
    slide.placeholders[1].text = element(
        f"Presented by {pii(lead, 'PERSON_NAME', 'title slide')}, TPRM Lead | "
        f"{keep('September 2026', 'month')}",
        "paragraph", page=1
    )

    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = element("Your TPRM Contacts", "heading", page=2)
    body = slide.placeholders[1].text_frame
    lines = [
        f"Programme lead: {pii(lead, 'PERSON_NAME', 'bullet')} "
        f"({pii('nkechi.adeyemi@example.com', 'EMAIL', 'bullet')})",
        f"Assessment queries: {pii('Oliver Brandt', 'PERSON_NAME', 'bullet')}, "
        f"{pii('+1 (646) 555-0127', 'PHONE', 'bullet')}",
        f"OneTrust administrator: {pii('Mei Lin Zhou', 'PERSON_NAME', 'bullet')}, "
        f"{pii('mei.zhou@example.com', 'EMAIL', 'bullet')}",
        f"Escalations go to the {keep('Third-Party Risk Committee', 'committee name')}, which meets monthly",
    ]
    body.text = element(lines[0], "list_item", page=2)

    for line in lines[1:]:
        body.add_paragraph().text = element(line, "list_item", page=2)

    slide.notes_slide.notes_text_frame.text = element(
        f"If {pii('Oliver Brandt', 'PERSON_NAME', 'speaker notes')} is away, assessment queries go to his "
        f"deputy {pii('Ama Owusu', 'PERSON_NAME', 'speaker notes')} on "
        f"{pii('+44 7700 900311', 'PHONE', 'speaker notes')}.",
        "paragraph", "notes", page=2
    )

    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = element("Risk Tiers and Review Cadence", "heading", page=3)
    rows = [
        ["Tier", "Criteria", "Review cadence", "Example vendor"],
        ["Tier 1", "Critical service or access to customer data", "Every 12 months",
         keep("Fabrikam Payroll Services", "company")],
        ["Tier 2", "Important service, limited data access", "Every 24 months",
         keep("Contoso Facilities", "company")],
        ["Tier 3", "Low risk, no data access", "At renewal", keep("Northwind Stationery", "company")],
    ]
    table = slide.shapes.add_table(len(rows), 4, Inches(0.4), Inches(1.6), Inches(9.2), Inches(2.4)).table

    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = element(value, "table_cell", page=3, table=1, row=r + 1, column=c + 1)

    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = element("Worked Example: Vendor Record", "heading", page=4)
    image, text = screenshot("VENDOR RECORD VR-10442", [
        "Vendor: Fabrikam Payroll Services",
        f"Relationship owner: {pii('Gareth Pryce', 'PERSON_NAME', 'image (OCR)')}",
        f"Owner email: {pii('gareth.pryce@example.com', 'EMAIL', 'image (OCR)')}",
        f"Last sign-in from: {pii('203.0.113.57', 'IP_ADDRESS', 'image (OCR)')}",
        "Risk tier: 1 (Critical)",
    ])
    element(text, "image_text", page=4)
    slide.shapes.add_picture(io.BytesIO(image), Inches(1), Inches(1.6), Inches(8))
    slide.shapes.add_textbox(Inches(1), Inches(6.4), Inches(8), Inches(0.5)).text_frame.text = element(
        "Screenshot from the OneTrust training tenant", "paragraph", "text_box", page=4
    )
    slide.notes_slide.notes_text_frame.text = element(
        "Point out that the relationship owner's details are visible in the screenshot, which is why "
        "exports must be redacted.",
        "paragraph", "notes", page=4
    )

    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = element("Handling Evidence", "heading", page=5)
    group = slide.shapes.add_group_shape()

    for index, line in enumerate((
        "Do: request evidence through the OneTrust portal and check it against the questionnaire.",
        "Don't: paste passport scans or bank letters into tickets or chat.",
    )):
        group.shapes.add_textbox(Inches(0.5), Inches(1.8 + 1.2 * index), Inches(9), Inches(1)).text_frame.text = (
            element(line, "paragraph", "text_box", page=5)
        )

    slide.shapes.add_textbox(Inches(0.5), Inches(4.6), Inches(9), Inches(0.6)).text_frame.text = element(
        f"Retention: evidence is kept for 7 years after the contract ends "
        f"({keep('POL-TPRM-002', 'policy reference')}).",
        "paragraph", "text_box", page=5
    )

    prs.save(folder / labels.data["file"])
    return labels.save(folder)


def incident_briefing(folder):
    from pptx import Presentation
    from pptx.util import Inches

    labels = Labels(
        "incident_briefing.pptx",
        "Security incident briefing: IP addresses in bullets, a table and a SIEM screenshot, "
        "affected staff with employee IDs, a card number and a bank account number, timeline "
        "text boxes, speaker notes and file properties."
    )
    pii, keep, element = labels.pii, labels.keep, labels.element

    fischer, kowal = "Lena Fischer", "Tomasz Kowal"
    first_ip, second_ip = "198.51.100.77", "203.0.113.140"
    kowal_email = "tomasz.kowal@example.com"

    prs = Presentation()
    properties = prs.core_properties
    properties.title = keep("Incident Briefing INC-2026-0712", "document title")
    properties.subject = "Security incident"
    properties.comments = "Synthetic test document"
    properties.author = pii("Rohan Kapoor", "PERSON_NAME", "properties")
    properties.last_modified_by = keep("Security Operations", "team name")

    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = element("Incident Briefing: Credential Phishing", "heading", page=1)
    slide.placeholders[1].text = element(
        f"{keep('INC-2026-0712', 'incident reference')} | Risk Committee | {keep('9 October 2026', 'date')}",
        "paragraph", page=1
    )

    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = element("What Happened", "heading", page=2)
    body = slide.placeholders[1].text_frame
    lines = [
        f"On {keep('6 October 2026', 'date')} a phishing email reached 214 mailboxes.",
        f"Two staff entered their passwords: {pii(fischer, 'PERSON_NAME', 'bullet')} and "
        f"{pii(kowal, 'PERSON_NAME', 'bullet')}.",
        f"The attacker signed in from {pii(first_ip, 'IP_ADDRESS', 'bullet')} and "
        f"{pii(second_ip, 'IP_ADDRESS', 'bullet')}.",
        "No customer data was exported; three forwarding rules were removed.",
    ]
    body.text = element(lines[0], "list_item", page=2)

    for line in lines[1:]:
        body.add_paragraph().text = element(line, "list_item", page=2)

    slide.notes_slide.notes_text_frame.text = element(
        f"{pii('Rohan Kapoor', 'PERSON_NAME', 'speaker notes')} (SOC analyst, "
        f"{pii('+44 20 7946 0321', 'PHONE', 'speaker notes')}) can answer technical questions.",
        "paragraph", "notes", page=2
    )

    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = element("Affected Accounts", "heading", page=3)
    rows = [
        ["Name", "Employee ID", "Email", "Last sign-in IP", "Action"],
        [
            pii(fischer, "PERSON_NAME", "table"), pii("EMP-55871", "EMPLOYEE_ID", "table"),
            pii("lena.fischer@example.com", "EMAIL", "table"), pii(first_ip, "IP_ADDRESS", "table"),
            "Password reset",
        ],
        [
            pii(kowal, "PERSON_NAME", "table"), pii("EMP-56002", "EMPLOYEE_ID", "table"),
            pii(kowal_email, "EMAIL", "table"), pii(second_ip, "IP_ADDRESS", "table"),
            "Password reset, MFA re-enrolled",
        ],
    ]
    table = slide.shapes.add_table(len(rows), 5, Inches(0.3), Inches(1.6), Inches(9.4), Inches(1.8)).table

    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = element(value, "table_cell", page=3, table=1, row=r + 1, column=c + 1)

    slide.notes_slide.notes_text_frame.text = element(
        "Do not share this slide outside the committee: it lists staff by name.", "paragraph", "notes", page=3
    )

    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = element("Financial Exposure", "heading", page=4)
    body = slide.placeholders[1].text_frame
    card = pii("4000 0566 5566 5556", "PAYMENT_CARD", "bullet")
    assert luhn_valid(card)
    lines = [
        f"A finance mailbox held one unencrypted card number, {card}; the card was cancelled.",
        f"The cardholder, {pii('Hélène Lefèvre', 'PERSON_NAME', 'bullet')}, was told on "
        f"{keep('7 October 2026', 'date')}.",
        f"A remittance advice in the same mailbox showed the bank account number "
        f"{pii('41926837', 'BANK_ACCOUNT', 'bullet')} of a contractor, "
        f"{pii('Femi Adebayo', 'PERSON_NAME', 'bullet')}.",
        f"Payment run {keep('PR-2026-41', 'payment run reference')} was checked: no bank details were changed.",
    ]
    body.text = element(lines[0], "list_item", page=4)

    for line in lines[1:]:
        body.add_paragraph().text = element(line, "list_item", page=4)

    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = element("Timeline", "heading", page=5)

    for index, line in enumerate((
        f"{keep('06 Oct 09:14', 'date and time')} - first phishing email delivered",
        "06 Oct 09:52 - first password entered",
        "06 Oct 11:30 - SOC blocked the sender domain",
        "07 Oct 08:00 - all affected passwords reset",
    )):
        slide.shapes.add_textbox(Inches(0.8), Inches(1.7 + 0.9 * index), Inches(8.4), Inches(0.6)).text_frame.text = (
            element(line, "paragraph", "text_box", page=5)
        )

    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = element("Evidence: SIEM Alert", "heading", page=6)
    image, text = screenshot(f"ALERT {keep('88213', 'alert number')} - IMPOSSIBLE TRAVEL", [
        f"User: {pii(kowal_email, 'EMAIL', 'image (OCR)')}",
        f"Source IP: {pii(second_ip, 'IP_ADDRESS', 'image (OCR)')}",
        "Location: unknown (anonymising proxy)",
        f"Rule: {keep('AAD-IT-004', 'detection rule')}",
    ])
    element(text, "image_text", page=6)
    slide.shapes.add_picture(io.BytesIO(image), Inches(1), Inches(1.6), Inches(8))

    prs.save(folder / labels.data["file"])
    return labels.save(folder)


# --------------------------------------------------
# PDF
# --------------------------------------------------

def due_diligence_report(folder):
    import pymupdf

    labels = Labels(
        "due_diligence_report.pdf",
        "Two-page vendor due diligence report with a text layer: running header and page "
        "numbers, numbered headings, a ruled key-personnel table, IP addresses, directors' "
        "dates of birth, passport, NI number, home address and IBAN in paragraphs, a surname "
        "mention, a visitor badge image and the file properties."
    )
    pii, keep, element = labels.pii, labels.keep, labels.element

    malhotra, arslan, moreno = "Arjun Malhotra", "Elif Arslan", "Beatriz Moreno"

    # The built-in Helvetica: PyMuPDF writes an embedded Arial's "-" and
    # " " into the text layer as a soft hyphen and a no-break space, which
    # a PDF from Word would not. Helvetica covers the Latin-1 letters used
    body_font = {"fontname": "helv"}
    bold_font = {"fontname": "hebo"}

    doc = pymupdf.open()
    state = {}

    def new_page(number):
        page = doc.new_page(width=612, height=792)
        page.insert_text(
            (60, 40), element("Cadence Group | Vendor Due Diligence | Confidential", "paragraph", "header", page=number),
            fontsize=8, **body_font
        )
        state.update(page=page, number=number, y=90)
        return page

    def heading(text, size=13):
        state["y"] += 6
        state["page"].insert_text((60, state["y"]), element(text, "heading", page=state["number"]), fontsize=size, **bold_font)
        state["y"] += 24

    def paragraph(*lines):
        element("\n".join(lines), "paragraph", page=state["number"])

        for line in lines:
            state["page"].insert_text((60, state["y"]), line, fontsize=11, **body_font)
            state["y"] += 15

        state["y"] += 12

    def footer(total):
        state["page"].insert_text(
            (280, 765), element(f"Page {state['number']} of {total}", "paragraph", "footer", page=state["number"]),
            fontsize=8, **body_font
        )

    new_page(1)
    heading("Vendor Due Diligence Report", size=18)
    paragraph(
        f"Vendor: {keep('Halcyon Cloud Hosting Ltd', 'company')} (vendor reference "
        f"{keep('VND-2026-0577', 'vendor reference')}).",
        f"Assessment completed on {keep('30 September 2026', 'date')} against questionnaire v4.2.",
    )
    paragraph(
        f"Assessor: {pii(moreno, 'PERSON_NAME', 'body')}, {pii('beatriz.moreno@example.com', 'EMAIL', 'body')},",
        f"telephone {pii('+1 (415) 555-0138', 'PHONE', 'body')}.",
    )
    heading("1. Summary")
    paragraph(
        "Halcyon holds a current ISO 27001 certificate and a SOC 2 Type II report",
        f"covering {keep('1 April 2025', 'date')} to 31 March 2026. Three medium findings remain open.",
    )
    heading("2. Key Personnel")

    rows = [
        ["Name", "Role", "Email", "Phone"],
        [
            pii(malhotra, "PERSON_NAME", "table"), keep("Security lead", "job title"),
            pii("arjun.malhotra@example.net", "EMAIL", "table"), pii("+44 20 7946 0774", "PHONE", "table"),
        ],
        [
            pii("Hannah Sørensen", "PERSON_NAME", "table"), keep("Data protection officer", "job title"),
            pii("h.sorensen@example.net", "EMAIL", "table"), pii("+44 20 7946 0819", "PHONE", "table"),
        ],
    ]
    page, left, top, widths, height = state["page"], 60, state["y"], [105, 125, 150, 112], 24

    for r, row in enumerate(rows):
        x = left

        for c, value in enumerate(row):
            page.draw_rect(pymupdf.Rect(x, top + r * height, x + widths[c], top + (r + 1) * height), width=0.8)
            page.insert_text(
                (x + 5, top + r * height + 16),
                element(value, "table_cell", page=1, table=1, row=r + 1, column=c + 1),
                fontsize=9.5, **(bold_font if r == 0 else body_font)
            )
            x += widths[c]

    state["y"] = top + len(rows) * height + 30
    heading("3. Technical Testing")
    paragraph(
        f"Production servers sit in the range {pii('192.0.2.10', 'IP_ADDRESS', 'body')} to "
        f"{pii('192.0.2.40', 'IP_ADDRESS', 'body')}. The management",
        f"interface on {pii('192.0.2.15', 'IP_ADDRESS', 'body')} was reachable from the internet during testing.",
    )
    footer(2)

    new_page(2)
    heading("4. Identity Verification of Directors")
    paragraph(
        f"The director, {pii(malhotra, 'PERSON_NAME', 'body')}, born {pii('1974-08-19', 'DATE_OF_BIRTH', 'body')}, "
        f"provided passport",
        f"{pii('X4829173', 'PASSPORT_NUMBER', 'body')} and a utility bill for "
        f"{pii('9 Ferncliff Gardens, Leeds LS6 4QT', 'POSTAL_ADDRESS', 'body')}.",
    )
    paragraph(
        f"The company secretary, {pii(arslan, 'PERSON_NAME', 'body')}, born "
        f"{pii('3 May 1983', 'DATE_OF_BIRTH', 'body')}, provided her",
        f"National Insurance number {pii('QQ 45 67 89 B', 'NATIONAL_ID', 'body')}.",
    )
    paragraph(
        f"Mr {pii('Malhotra', 'PERSON_NAME', 'surname mention')} confirmed that no director holds another directorship",
        "with a Cadence supplier.",
    )
    heading("5. Banking")
    iban = pii("DE89 3704 0044 0532 0130 00", "BANK_ACCOUNT", "body")
    assert iban_valid(iban)
    paragraph(
        "Deposits are refunded to the director's personal account,",
        f"IBAN {iban}.",
    )
    paragraph("Visitor badge issued for the on-site review:")

    image, text = screenshot(f"VISITOR BADGE {keep('VB-20931', 'badge number')}", [
        f"Name: {pii(arslan, 'PERSON_NAME', 'image (OCR)')}",
        f"Host: {pii(moreno, 'PERSON_NAME', 'image (OCR)')}",
        f"Valid on: {keep('14/10/2026', 'date')}",
    ], width=900)
    element(text, "image_text", page=2)
    page = state["page"]
    page.insert_image(pymupdf.Rect(60, state["y"], 360, state["y"] + 110), stream=image)
    state["y"] += 135

    heading("6. Conclusion")
    paragraph(
        "The vendor is approved as Tier 2, on condition that the exposed",
        f"management interface is closed by {keep('31 October 2026', 'date')}.",
    )
    footer(2)

    doc.set_metadata({
        "title": "Vendor Due Diligence Report",
        "author": pii(moreno, "PERSON_NAME", "properties"),
        "subject": "Third-party risk",
        "keywords": "TPRM; due diligence",
        "creator": "Report generator",
        "producer": "PyMuPDF",
    })
    doc.save(folder / labels.data["file"])
    return labels.save(folder)


def access_request_scan(folder):
    import pymupdf

    labels = Labels(
        "access_request_scan.pdf",
        "A scanned IT access request form with no text layer: grey paper with specks, tilted "
        f"{SCAN_SKEW} degrees and saved as JPEG. Requester details as label: value lines, a "
        "table in the case-study style (dark header band) with no PII, approvers and a footer."
    )
    pii, keep, element = labels.pii, labels.keep, labels.element

    scale = SCAN_DPI / 72
    width, height = int(612 * scale), int(792 * scale)
    image = Image.new("RGB", (width, height), (247, 246, 242))
    draw = ImageDraw.Draw(image)

    def pt(value):
        return int(value * scale)

    def write(y, text, size=12, bold=False, kind="paragraph", where="body"):
        draw.text((pt(60), pt(y)), element(text, kind, where), fill=(20, 20, 20), font=font(pt(size), bold=bold))

    write(56, "IT Access Request Form", size=18, bold=True, kind="heading")
    write(84, f"Form {keep('ITS-F-07', 'form reference')} rev 2", size=10)

    write(118, "A. Requester", size=14, bold=True, kind="heading")
    lines = [
        f"Full name: {pii('Chloe Abernathy', 'PERSON_NAME', 'scanned page (OCR)')}",
        f"Employee ID: {pii('E204877', 'EMPLOYEE_ID', 'scanned page (OCR)')}",
        f"Department: {keep('Treasury Operations', 'department')}",
        f"Work email: {pii('chloe.abernathy@example.com', 'EMAIL', 'scanned page (OCR)')}",
        f"Mobile: {pii('(512) 555-0182', 'PHONE', 'scanned page (OCR)')}",
        f"Date of birth: {pii('17 June 1992', 'DATE_OF_BIRTH', 'scanned page (OCR)')}",
        f"Home address: {pii('1187 Larkspur Drive, Austin, TX 78745', 'POSTAL_ADDRESS', 'scanned page (OCR)')}",
        f"Laptop: {keep('LT-0042187', 'asset tag')}, static IP {pii('10.20.31.45', 'IP_ADDRESS', 'scanned page (OCR)')}",
    ]

    for number, line in enumerate(lines):
        write(146 + number * 22, line)

    write(330, "B. Access Requested", size=14, bold=True, kind="heading")

    # The case-study table style: a dark header band, then thin row rules
    left, right, top = pt(60), pt(552), pt(352)
    columns = [pt(60), pt(200), pt(330)]
    band, row_height = pt(24), pt(28)
    draw.rectangle((left, top, right, top + band), fill=(31, 56, 100))

    for column, (x, label) in enumerate(zip(columns, ["System", "Access level", "Justification"]), start=1):
        element(label, "table_cell", table=1, row=1, column=column)
        draw.text((x + pt(6), top + pt(5)), label, fill="white", font=font(pt(12), bold=True))

    rows = [
        ["SAP Treasury", "Read and write", "Daily cash positioning"],
        ["Kyriba", "Approver", f"Release payments up to {keep('$250,000', 'amount')}"],
    ]

    for r, row in enumerate(rows):
        y = top + band + r * row_height

        for column, (x, value) in enumerate(zip(columns, row), start=1):
            element(value, "table_cell", table=1, row=r + 2, column=column)
            draw.text((x + pt(6), y + pt(7)), value, fill=(20, 20, 20), font=font(pt(12)))

        draw.line((left, y + row_height, right, y + row_height), fill=(215, 215, 215), width=2)

    write(450, "C. Approvals", size=14, bold=True, kind="heading")
    write(478, f"Line manager: {pii('Desmond Achebe', 'PERSON_NAME', 'scanned page (OCR)')}, "
               f"approved {keep('08/10/2026', 'approval date')}")
    write(500, f"Security reviewer: {pii('Natalia Petrova', 'PERSON_NAME', 'scanned page (OCR)')}, "
               f"ticket {keep('REQ-118204', 'ticket reference')}")
    write(740, "ITS-F-07 rev 2 | Page 1 of 1", size=9, where="footer")

    # What a sheet fed through an office scanner looks like: tilted,
    # specks of dust, greyscale, JPEG-compressed
    image = image.rotate(SCAN_SKEW, resample=Image.BICUBIC, expand=False, fillcolor=(247, 246, 242))
    draw = ImageDraw.Draw(image)
    specks = random.Random(7)

    for _ in range(1500):
        x, y = specks.randrange(width), specks.randrange(height)
        draw.point((x, y), fill=(specks.randrange(90, 180),) * 3)

    buffer = io.BytesIO()
    image.convert("L").save(buffer, "JPEG", quality=80)

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=buffer.getvalue())
    doc.set_metadata({"title": "IT Access Request Form", "producer": "Scanner"})
    doc.save(folder / labels.data["file"])
    return labels.save(folder)


BUILDERS = (
    contractor_onboarding, assignment_record, tprm_training, incident_briefing,
    due_diligence_report, access_request_scan,
)


def build(folder=PANEL_DIR):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    return [builder(folder) for builder in BUILDERS]


def main():
    import json

    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else PANEL_DIR

    for path in build(folder):
        labels = json.loads(path.read_text(encoding="utf-8"))
        print(f"  {labels['file']:28} {len(labels['pii']):3} PII values, "
              f"{len(labels['not_pii']):3} look-alikes, {len(labels['structure']):3} elements")

    print(f"Test set: {folder}")


if __name__ == "__main__":
    main()
