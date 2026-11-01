import io
import json
import sys
import tempfile
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

from src.extractors.dispatch import extract_document
from src.ocr.image_ocr import ocr_image
from src.original_format.inspect import pdf_contents
from src.original_format.redactor import record_path, redact_original, redacted_path, sha256
from src.original_format.validator import MAX_PASSES, another_pass, approved_file, validate_original
from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import findings_path, scan_file
from src.pii.redactor import redact_file, save_json
from src.pii.validator import NotApproved, validate_file

# The redacted PDF file (src/original_format, steps 6 and 7) of a PDF
# built here: a page with a text layer (a name, an email glued to its
# label, a phone, tight lines around them, a picture with text, a mailto
# link, a comment), a scanned page, a scanned page shown rotated, and
# PII where the extractor never looks (document information, XMP, a
# bookmark, an attachment). Every step runs as the pipeline runs it, in
# a temporary folder. Needs the spaCy model and Tesseract.

NAME = "Grace Holloway"
EMAIL = "grace.holloway@example.com"
PHONE = "+44 7700 900481"
AUTHOR = "Marta Kowalczyk"
COMMENTER = "Kenji Arakawa"
SCAN_NAME = "Priya Raman"
SCAN_EMAIL = "priya.raman@example.com"
ROTATED_EMAIL = "tomas.berg@example.com"
MISSED_EMAIL = "lena.fischer@example.com"

# Every value that must be gone from every place the file keeps text
VALUES = (NAME, EMAIL, PHONE, AUTHOR, COMMENTER, "Daniel Osei", SCAN_NAME, SCAN_EMAIL, ROTATED_EMAIL)

NAME_TAG = "[REDACTED: PERSON_NAME]"

LINES = [
    "Onboarding record",
    f"This record confirms that {NAME} joined the Risk Analytics team in London.",
    f"Email:{EMAIL} or phone {PHONE} for building access.",
    "The security induction is booked for week one.",
    "Holloway completes the induction with the risk team.",
]


def font(size):
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass

    return ImageFont.load_default(size=size)


def text_image(lines, width=1000, size=36):
    image = Image.new("RGB", (width, 40 + len(lines) * int(size * 1.7)), "white")
    draw = ImageDraw.Draw(image)

    for number, line in enumerate(lines):
        draw.text((20, 20 + number * int(size * 1.7)), line, fill="black", font=font(size))

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def scan(lines):
    # A scanned A4 page: one picture, no text layer
    return text_image(lines, width=1240, size=44)


def build(path):
    pdf = pymupdf.open()

    # --- Page 1: a text layer, in 11 pt lines 13 pt apart ---
    page = pdf.new_page()

    for number, line in enumerate(LINES):
        page.insert_text((60, 80 + number * 13), line, fontsize=11)

    # A link whose target holds the email, over other text (a link over a
    # redacted value goes with the redaction)
    title_box = page.search_for("Onboarding record")[0]
    page.insert_link({"kind": pymupdf.LINK_URI, "from": title_box, "uri": f"mailto:{EMAIL}"})

    comment = page.add_text_annot((400, 60), f"Checked with {NAME}")
    comment.set_info(title=COMMENTER)
    comment.update()

    page.insert_image(
        pymupdf.Rect(60, 200, 360, 260), stream=text_image(["Approved by: Daniel Osei", "Mobile +44 7700 900733"])
    )

    # --- Page 2: scanned ---
    page = pdf.new_page()
    page.insert_image(page.rect, stream=scan([
        "Incident statement", f"Owner: {SCAN_NAME}", f"Contact: {SCAN_EMAIL}", "Phone +44 7700 900155",
    ]))

    # --- Page 3: scanned, shown rotated ---
    page = pdf.new_page()
    page.set_rotation(90)
    page.insert_image(page.mediabox, stream=scan([
        "Reviewer contact details for the incident", f"Email: {ROTATED_EMAIL}",
        "The statement and the timeline were reviewed", "No further action is needed at this stage",
    ]))

    # --- Where the extractor never looks ---
    pdf.set_metadata({"author": AUTHOR, "title": "Onboarding record", "keywords": f"onboarding, {NAME}"})
    pdf.set_xml_metadata(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f'<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>{AUTHOR}</dc:creator>'
        '</rdf:Description></rdf:RDF></x:xmpmeta>'
    )
    pdf.set_toc([[1, f"Record of {NAME}", 1]])
    pdf.embfile_add("notes.txt", f"Buddy for {NAME}".encode("utf-8"))

    pdf.save(path)


def build_plain(path, text):
    pdf = pymupdf.open()
    pdf.new_page().insert_text((60, 80), text, fontsize=11)
    pdf.save(path)


def run_pipeline(folder, source, false_positive=None):
    """
    Steps 1-4 as main.py runs them, then steps 6 and 7. false_positive:
    the value whose finding a reviewer marked as a false positive.
    """

    json_path = folder / f"{source.stem}.json"
    report_path = findings_path(json_path)
    extract_document(source).save_json(json_path)
    report = scan_file(json_path, report_path, nlp)

    if false_positive:
        for item in report["findings"]:
            if item["text"] == false_positive:
                item["review_status"] = "false_positive"

        save_json(report, report_path)

    redact_file(json_path, report_path)
    copy_validation, _ = validate_file(json_path, report_path, nlp)
    record, output_path, _ = redact_original(json_path, source, report_path)
    validation, _ = validate_original(json_path, nlp)

    return json_path, report, copy_validation, record, output_path, validation


def page_ocr(page):
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(3, 3))
    return ocr_image(Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB"))[0]


nlp = LocalNLPDetector()

if nlp.error:
    sys.exit(f"The spaCy model is needed: {nlp.error}")

failures = 0
CASES = []

with tempfile.TemporaryDirectory() as temp:
    folder = Path(temp)

    # --- The full document ---
    source = folder / "upload" / "record.pdf"
    source.parent.mkdir()
    build(source)

    json_path, report, copy_validation, record, output_path, validation = run_pipeline(folder, source)
    found_types = sorted({item["type"] for item in report["findings"]})
    everything = " ".join(text for _, text in pdf_contents(output_path).strings)
    leaks = sorted(value for value in VALUES if value in everything)

    with pymupdf.open(output_path) as redacted:
        first, second, third = redacted
        text = first.get_text()
        words = [word[4] for word in first.get_text("words")]
        links = first.get_links()
        comments = [annot for annot in first.annots() if annot.type[0] == pymupdf.PDF_ANNOT_TEXT]
        metadata = redacted.metadata
        xmp = redacted.get_xml_metadata()
        toc = redacted.get_toc()
        attachments = redacted.embfile_names()
        image = redacted.extract_image(first.get_images(full=True)[0][0])["image"]
        image_text = ocr_image(Image.open(io.BytesIO(image)).convert("RGB"))[0]
        scan_text, rotated_text = page_ocr(second), page_ocr(third)

        # The colour at the middle of where the name was, on the page as shown
        with pymupdf.open(source) as original:
            name_box = original[0].search_for(NAME)[0]

        rendered = first.get_pixmap()
        name_pixel = rendered.pixel(int((name_box.x0 + name_box.x1) / 2), int((name_box.y0 + name_box.y1) / 2))

    CASES += [
        ("The values are found and the sanitized copy passes", copy_validation["verdict"] == "PASS"),
        ("The redacted file is written with nothing left unplaced", record["status"] == "completed"),
        ("The redacted file passes its check", validation["verdict"] == "PASS"),
        ("No value is left anywhere the PDF keeps text", not leaks),
        (
            "No tag is written in the PDF, and the tight lines around the values keep every word",
            "[REDACTED" not in text
            and all(word in words for word in ("Onboarding", "record", "confirms", "joined", "security", "induction")),
        ),
        ("A value is covered by a solid black box", max(name_pixel[:3]) < 40),
        ("A label glued to an email keeps its characters", any(word.startswith("Email:") for word in words)),
        (
            "A mailto link keeps a valid URI without the email",
            [link.get("uri") for link in links] == ["mailto:%5BREDACTED:%20EMAIL%5D"],
        ),
        ("The comment is removed", not comments),
        (
            "The document information is redacted, and its XMP copy removed",
            metadata.get("author") == NAME_TAG and NAME not in metadata.get("keywords", "") and not xmp,
        ),
        ("The bookmark loses the name", toc and NAME not in toc[0][1]),
        ("The attachment is removed", not attachments),
        (
            "The picture no longer shows the name or phone",
            "Osei" not in image_text and "900733" not in image_text and "Approved by" in image_text,
        ),
        (
            "The scanned page no longer shows its values",
            "Raman" not in scan_text and "priya" not in scan_text and "900155" not in scan_text
            and "Incident" in scan_text,
        ),
        (
            "The rotated scanned page no longer shows its email",
            "tomas" not in rotated_text and "Reviewer" in rotated_text,
        ),
    ]

    try:
        CASES.append(("approved_file() releases a PASS file", approved_file(json_path) == output_path))
    except NotApproved as reason:
        print(f"        approved_file: {reason}")
        CASES.append(("approved_file() releases a PASS file", False))

    # A file changed after it was checked is refused
    original_bytes = output_path.read_bytes()
    output_path.write_bytes(original_bytes + b"\n%")

    try:
        approved_file(json_path)
        refused = False
    except NotApproved:
        refused = True

    output_path.write_bytes(original_bytes)
    CASES.append(("approved_file() refuses a file changed after its check", refused))

    # A value hidden back in the file (its subject), with its record
    # updated to match: the check finds it, and deletes the file
    with pymupdf.open(output_path) as redacted:
        redacted.set_metadata({**redacted.metadata, "subject": EMAIL})
        tampered_bytes = redacted.tobytes()

    output_path.write_bytes(tampered_bytes)
    tampered_record = json.loads(record_path(json_path).read_text(encoding="utf-8"))
    tampered_record["files"]["redacted_sha256"] = sha256(output_path)
    save_json(tampered_record, record_path(json_path))
    tampered, _ = validate_original(json_path, nlp)

    CASES.append((
        "A value hidden where the extractor never looks fails the check, and the file is deleted",
        tampered["summary"]["known_values"] == "FAIL" and tampered["verdict"] == "FAIL"
        and not output_path.exists() and tampered["files"]["deleted"],
    ))

    # --- A reviewed false positive is kept ---
    plain_folder = folder / "plain"
    plain_source = plain_folder / "upload" / "desk.pdf"
    plain_source.parent.mkdir(parents=True)
    build_plain(plain_source, f"Call the service desk on {PHONE} for access.")

    plain_json, plain_report, _, _, plain_output, plain_validation = run_pipeline(
        plain_folder, plain_source, false_positive=PHONE
    )

    with pymupdf.open(plain_output) as plain:
        kept = plain[0].get_text()

    CASES.append((
        "A value reviewed as a false positive stays in the file, which passes",
        any(item["text"] == PHONE for item in plain_report["findings"])
        and PHONE in kept and plain_validation["verdict"] == "PASS",
    ))

    # --- Another file under the same name is refused, and nothing is left ---
    other = folder / "other" / "desk.pdf"
    other.parent.mkdir()
    build_plain(other, "Some other text entirely.")

    try:
        redact_original(plain_json, other)
        refused_other = False
    except ValueError:
        refused_other = True

    CASES.append((
        "A file that is not the extracted document's is refused, with no output left",
        refused_other and not redacted_path(plain_json, "desk.pdf").exists()
        and not record_path(plain_json).exists(),
    ))

    # --- A value the first scan missed on a scanned page: found by the
    # file's check when it reads the page again, redacted in another pass ---
    missed_folder = folder / "missed"
    missed_source = missed_folder / "upload" / "escalation.pdf"
    missed_source.parent.mkdir(parents=True)

    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_image(page.rect, stream=scan([
            "Escalation contact for the review", f"Email: {MISSED_EMAIL}",
            "Available on weekdays during office hours", "The review closes at the end of the month",
        ]))
        pdf.save(missed_source)

    missed_json = missed_folder / "escalation.json"
    missed_report_path = findings_path(missed_json)
    extract_document(missed_source).save_json(missed_json)
    missed_report = scan_file(missed_json, missed_report_path, nlp)
    seen_by_scan = any(item["text"] == MISSED_EMAIL for item in missed_report["findings"])

    # As if the scan had missed it
    missed_report["findings"] = [item for item in missed_report["findings"] if item["text"] != MISSED_EMAIL]
    save_json(missed_report, missed_report_path)
    redact_file(missed_json, missed_report_path)
    validate_file(missed_json, missed_report_path, nlp)

    redact_original(missed_json, missed_source)
    first_check, _ = validate_original(missed_json, nlp)
    passes = [first_check]

    # As main.py and jobs.py run it
    while leftovers := another_pass(passes[-1]):
        redact_original(missed_json, missed_source, leftovers=leftovers)
        passes.append(validate_original(missed_json, nlp)[0])

    missed_output = redacted_path(missed_json, "escalation.pdf")
    missed_text = ""

    if missed_output.exists():
        with pymupdf.open(missed_output) as redacted:
            missed_text = page_ocr(redacted[0])

    CASES += [
        (
            "The file's check places a value the first scan missed on a scanned page",
            seen_by_scan and first_check["verdict"] == "FAIL" and first_check["leftovers_complete"]
            and any(item["type"] == "EMAIL" and item["page_number"] == 1 for item in first_check["leftovers"]),
        ),
        (
            "Another pass redacts it, and the file passes its check",
            len(passes) == 2 and passes[-1]["verdict"] == "PASS" and passes[-1]["redaction_pass"] == 2
            and "lena" not in missed_text.lower() and "Escalation" in missed_text,
        ),
    ]

    # --- When another pass is given ---
    def check_result(verdict="FAIL", complete=True, redaction_pass=1, text="PASS", found_now=True):
        return {
            "verdict": verdict, "leftovers_complete": complete, "redaction_pass": redaction_pass,
            "checks": [{"name": "text", "status": text}, {"name": "rescan", "status": verdict}],
            "leftovers": [{"page_number": 1, "box": [0, 0, 10, 10], "type": "EMAIL",
                           "found_in_pass": redaction_pass if found_now else redaction_pass - 1}],
        }

    CASES.append((
        "Another pass is given only for leftovers found now, all placed, within MAX_PASSES, nothing else failing",
        another_pass(check_result()) is not None
        and another_pass(check_result(verdict="PASS")) is None
        and another_pass(check_result(complete=False)) is None
        and another_pass(check_result(redaction_pass=MAX_PASSES)) is None
        and another_pass(check_result(text="FAIL")) is None
        and another_pass(check_result(redaction_pass=2, found_now=False)) is None,
    ))

    details = {
        "types found": found_types,
        "missed: passes": [(check["verdict"], check["redaction_pass"], check["summary"]) for check in passes],
        "missed: first issues": [item for check in first_check["checks"] for item in check["issues"]],
        "leaks": leaks,
        "file checks": validation["summary"],
        "file notes": {check["name"]: check["notes"] for check in validation["checks"]},
        "file issues": [item for check in validation["checks"] for item in check["issues"]],
        "copy checks": copy_validation["summary"],
        "copy issues": [item for check in copy_validation["checks"] for item in check["issues"]],
        "summary": record["summary"],
        "errors": record["errors"],
    }

print("========== ORIGINAL-FORMAT PDF TEST ==========\n")

for description, passed in CASES:
    failures += not passed
    print(f"{'PASS' if passed else 'FAIL'}  {description}")

if failures:
    for key, value in details.items():
        print(f"        {key}: {value}")

print(f"\n{len(CASES) - failures} of {len(CASES)} cases passed")
print("==============================================")

sys.exit(1 if failures else 0)
