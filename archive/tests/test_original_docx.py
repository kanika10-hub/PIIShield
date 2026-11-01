import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.shared import Inches
from lxml import etree
from PIL import Image, ImageDraw, ImageFont

from src.extractors.docx_extractor import extract_docx
from src.ocr.image_ocr import ocr_image
from src.original_format.redactor import record_path, redact_original, redacted_path, sha256
from src.original_format.validator import approved_file, misreadings, validate_original
from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import findings_path, scan_file
from src.pii.redactor import output_paths, redact_file, save_json
from src.pii.validator import NotApproved, validate_file

# The redacted DOCX file (src/original_format, steps 6 and 7) of a Word
# document built here, with PII in places the extractor reads and in
# places it does not: runs with different formatting, a hyperlink
# target, a tracked deletion, a field code, a text box and the copy Word
# keeps of it for old readers, alt text, a chart, a document variable,
# an image and a thumbnail. Every step runs as the pipeline runs it, in
# a temporary folder. Needs the spaCy model and Tesseract.

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
V = "urn:schemas-microsoft-com:vml"
SVG = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"

NAME = "Grace Holloway"
EMAIL = "grace.holloway@example.com"
PHONE = "+44 7700 900481"
AUTHOR = "Marta Kowalczyk"
EDITOR = "Kenji Arakawa"
BUDDY_EMAIL = "omar.siddiqui@example.com"

# Every value that must be gone from the file, in any XML of it
VALUES = (NAME, EMAIL, PHONE, AUTHOR, EDITOR, BUDDY_EMAIL)

CHART = f"""<c:chartSpace xmlns:c="{C}" xmlns:a="{A}"><c:chart>
<c:title><c:tx><c:rich><a:p><a:r><a:t>Induction hours</a:t></a:r></a:p></c:rich></c:tx></c:title>
<c:plotArea><c:barChart><c:ser>
<c:tx><c:strRef><c:strCache><c:pt idx="0"><c:v>{NAME}</c:v></c:pt></c:strCache></c:strRef></c:tx>
<c:cat><c:strRef><c:strCache><c:pt idx="0"><c:v>FY2025</c:v></c:pt></c:strCache></c:strRef></c:cat>
</c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>"""


def xml(text):
    return etree.fromstring(text)


def font(size):
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass

    return ImageFont.load_default(size=size)


def text_image(lines):
    image = Image.new("RGB", (1000, 40 + len(lines) * 60), "white")
    draw = ImageDraw.Draw(image)

    for number, line in enumerate(lines):
        draw.text((20, 20 + number * 60), line, fill="black", font=font(36))

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def build(path):
    doc = Document()
    doc.core_properties.author = AUTHOR
    doc.core_properties.last_modified_by = EDITOR

    # A name over two runs, bold then italic
    paragraph = doc.add_paragraph("This record confirms that ")
    paragraph.add_run("Grace ").bold = True
    paragraph.add_run("Holloway").italic = True
    paragraph.add_run(" joined the Risk Analytics team in London.")

    doc.add_paragraph(f"She can be reached at {EMAIL} or on {PHONE}.")

    # A hyperlink whose target holds the email, not its text
    rel_id = doc.part.relate_to(f"mailto:{EMAIL}", RT.HYPERLINK, is_external=True)
    paragraph = doc.add_paragraph("Questions go to ")
    paragraph._p.append(xml(
        f'<w:hyperlink xmlns:w="{W}" xmlns:r="{R}" r:id="{rel_id}"><w:r><w:t>the new starter</w:t></w:r></w:hyperlink>'
    ))

    # A tracked deletion of the phone number, by the last editor
    paragraph = doc.add_paragraph("Old desk number: ")
    paragraph._p.append(xml(
        f'<w:del xmlns:w="{W}" w:id="91" w:author="{EDITOR}" w:date="2026-01-05T10:00:00Z">'
        f'<w:r><w:delText>{PHONE}</w:delText></w:r></w:del>'
    ))

    # A field code holding the email, split over two runs
    paragraph = doc.add_paragraph("Mail ")
    for run in (
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>',
        '<w:r><w:instrText xml:space="preserve"> HYPERLINK "mailto:grace.holloway@</w:instrText></w:r>',
        '<w:r><w:instrText xml:space="preserve">example.com" </w:instrText></w:r>',
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>',
        '<w:r><w:t>her</w:t></w:r>',
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>',
    ):
        paragraph._p.append(xml(run.replace("<w:r>", f'<w:r xmlns:w="{W}">', 1)))

    doc.add_paragraph("Holloway completes the security induction in week one.")

    # A text box, and the copy Word keeps of it for old readers
    box = f'<w:txbxContent><w:p><w:r><w:t>Buddy contact: {BUDDY_EMAIL}</w:t></w:r></w:p></w:txbxContent>'
    doc.add_paragraph().add_run()._r.append(xml(
        f'<mc:AlternateContent xmlns:mc="{MC}" xmlns:w="{W}" xmlns:wp="{WP}" xmlns:a="{A}" '
        f'xmlns:wps="{WPS}" xmlns:v="{V}">'
        '<mc:Choice Requires="wps"><w:drawing><wp:inline><wp:extent cx="914400" cy="457200"/>'
        '<wp:docPr id="20" name="Text Box 1"/><a:graphic>'
        f'<a:graphicData uri="{WPS}"><wps:wsp><wps:txbx>{box}</wps:txbx><wps:bodyPr/></wps:wsp></a:graphicData>'
        '</a:graphic></wp:inline></w:drawing></mc:Choice>'
        f'<mc:Fallback><w:pict><v:shape><v:textbox>{box}</v:textbox></v:shape></w:pict></mc:Fallback>'
        '</mc:AlternateContent>'
    ))

    # An image with text, and alt text naming the person
    doc.add_paragraph("Sign-off sheet:")
    run = doc.add_paragraph().add_run()
    run.add_picture(io.BytesIO(text_image(["Approved by: Daniel Osei", "Mobile +44 7700 900733"])), width=Inches(4))
    run._r.xpath(".//wp:docPr")[0].set("descr", f"Signed form of {NAME}")

    # The SVG copy Office keeps of the picture, and shows instead of it
    svg_part = Part(
        PackURI("/word/media/image2.svg"), "image/svg+xml",
        f'<svg xmlns="http://www.w3.org/2000/svg"><text>Approved by {NAME}</text></svg>'.encode("utf-8"),
        doc.part.package
    )
    svg_id = doc.part.relate_to(svg_part, RT.IMAGE)
    run._r.xpath(".//a:blip")[0].append(xml(
        f'<a:extLst xmlns:a="{A}"><a:ext uri="{{96DAC541-7B7A-43D3-8B79-37D633B846F1}}">'
        f'<asvg:svgBlip xmlns:asvg="{SVG}" xmlns:r="{R}" r:embed="{svg_id}"/></a:ext></a:extLst>'
    ))

    # A chart naming the person in a series
    chart_part = Part(
        PackURI("/word/charts/chart1.xml"),
        "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
        CHART.encode("utf-8"), doc.part.package
    )
    chart_id = doc.part.relate_to(chart_part, RT.CHART)
    doc.add_paragraph().add_run()._r.append(xml(
        f'<w:drawing xmlns:w="{W}" xmlns:wp="{WP}" xmlns:a="{A}"><wp:inline>'
        '<wp:extent cx="914400" cy="914400"/><wp:docPr id="21" name="Chart 1"/>'
        f'<a:graphic><a:graphicData uri="{C}"><c:chart xmlns:c="{C}" xmlns:r="{R}" r:id="{chart_id}"/>'
        '</a:graphicData></a:graphic></wp:inline></w:drawing>'
    ))

    # A document variable: never shown, never extracted
    doc.settings.element.append(xml(
        f'<w:docVars xmlns:w="{W}"><w:docVar w:name="client" w:val="{NAME}"/></w:docVars>'
    ))

    # A preview picture of the first page
    thumbnail = Part(PackURI("/docProps/thumbnail.png"), "image/png", text_image([NAME]), doc.part.package)
    doc.part.package.relate_to(thumbnail, RT.THUMBNAIL)

    doc.save(path)


def build_plain(path, text):
    doc = Document()
    doc.add_paragraph(text)
    doc.save(path)


def run_pipeline(folder, source, false_positive=None):
    """
    Steps 1-4 as main.py runs them, then steps 6 and 7. false_positive:
    the value whose finding a reviewer marked as a false positive.
    """

    json_path = folder / f"{source.stem}.json"
    report_path = findings_path(json_path)
    extract_docx(str(source)).save_json(json_path)
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


def xml_texts(path):
    """
    Every XML file of a zip, as text, with &amp; and the like decoded.
    """

    with zipfile.ZipFile(path) as archive:
        return {
            name: etree.tostring(etree.fromstring(archive.read(name)), method="xml", encoding="unicode")
            for name in archive.namelist()
            if name.endswith((".xml", ".rels"))
        }


def all_text(root):
    return "".join(root.itertext())


nlp = LocalNLPDetector()

if nlp.error:
    sys.exit(f"The spaCy model is needed: {nlp.error}")

failures = 0
CASES = []

with tempfile.TemporaryDirectory() as temp:
    folder = Path(temp)

    # --- The full document ---
    source = folder / "upload" / "starter.docx"
    source.parent.mkdir()
    build(source)

    json_path, report, copy_validation, record, output_path, validation = run_pipeline(folder, source)
    texts = xml_texts(output_path)
    found_types = sorted({item["type"] for item in report["findings"]})

    with zipfile.ZipFile(output_path) as archive:
        names = archive.namelist()
        image = archive.read("word/media/image1.png")
        document_xml = etree.fromstring(archive.read("word/document.xml"))
        rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")

    redacted = Document(str(output_path))
    split = redacted.paragraphs[0]
    tagged_run = next((run for run in split.runs if "[REDACTED: PERSON_NAME]" in run.text), None)
    deletions = document_xml.findall(f".//{{{W}}}del")
    field_code = "".join(t.text or "" for t in document_xml.iter(f"{{{W}}}instrText"))
    boxes = [all_text(box) for box in document_xml.iter(f"{{{W}}}txbxContent")]
    alt_text = document_xml.xpath(".//wp:docPr/@descr", namespaces={"wp": WP})
    ocr_text, _ = ocr_image(Image.open(io.BytesIO(image)).convert("RGB"))
    leaks = sorted({value for value in VALUES for text in texts.values() if value in text})

    CASES += [
        ("The values are found and the sanitized copy passes", copy_validation["verdict"] == "PASS"),
        ("The redacted file is written with nothing left unplaced", record["status"] == "completed"),
        ("The redacted file passes its check", validation["verdict"] == "PASS"),
        ("No value is left in any XML of the file", not leaks),
        (
            "A name over two runs is one tag, in the first run's formatting",
            split.text.startswith("This record confirms that [REDACTED: PERSON_NAME] joined")
            and tagged_run is not None and tagged_run.bold,
        ),
        (
            "A hyperlink target keeps a valid URI without the email",
            "mailto:%5BREDACTED:%20EMAIL%5D" in rels and EMAIL not in rels,
        ),
        (
            "A tracked deletion loses the value and its author's name",
            len(deletions) == 1 and PHONE not in all_text(deletions[0])
            and EDITOR not in deletions[0].get(f"{{{W}}}author", ""),
        ),
        ("A field code split over two runs loses the email", EMAIL not in field_code and "REDACTED" in field_code),
        (
            "A text box and Word's copy of it are both redacted",
            len(boxes) == 2 and all(text == "Buddy contact: [REDACTED: EMAIL]" for text in boxes),
        ),
        ("Alt text loses the name", alt_text and all(NAME not in text for text in alt_text)),
        ("The chart loses the name", NAME not in texts.get("word/charts/chart1.xml", NAME)),
        (
            "The image no longer shows the name or phone",
            "Osei" not in ocr_text and "900733" not in ocr_text and "Approved by" in ocr_text,
        ),
        ("The first-page thumbnail is removed", not any("thumbnail" in name for name in names)),
        (
            "The picture's SVG copy is removed, so its redacted pixels show",
            not any(name.endswith(".svg") for name in names)
            and not document_xml.xpath(".//*[local-name()='svgBlip']"),
        ),
    ]

    # Filling part of a line can change how OCR reads the rest of it.
    # A finding read again is only a misreading if each word of it has
    # low confidence, is outside the filled area, and lies over words
    # read before with confidence and no finding
    def ocr_words(*specs):
        return [
            {"text": text, "x": x, "y": 0, "width": width, "height": 20, "confidence": confidence}
            for text, x, width, confidence in specs
        ]

    def image_document(words):
        return {"pages": [{"blocks": [{
            "block_id": "image_1", "block_type": "image_ocr",
            "text": " ".join(word["text"] for word in words), "metadata": {"words": words},
        }]}]}

    first_read = image_document(ocr_words(
        ("Owner", 0, 50, 90), ("Daniel", 60, 50, 90), ("Osei", 120, 40, 90),
        ("signs", 170, 40, 90), ("off", 220, 30, 40),
    ))
    filled = {"findings": [{"block_id": "image_1", "start": 6, "end": 17}]}

    def misread(*specs, with_owner=False):
        # The finding: the words specs, after "Owner" (read as before), or with it
        read_again = image_document(ocr_words(("Owner", 0, 50, 90), *specs))
        text = read_again["pages"][0]["blocks"][0]["text"]
        start = 0 if with_owner else text.index(specs[0][0])
        item = {"block_id": "image_1", "start": start, "end": len(text)}
        return misreadings(first_read, filled, read_again)(item)

    CASES += [
        (
            "A low-confidence misreading of unchanged, confidently read pixels is not a leak",
            misread(("Bright", 170, 40, 15)) is True,
        ),
        (
            "A word read as before with a misreading beside it is not a leak",
            misread(("Bright", 170, 40, 15), with_owner=True) is True,
        ),
        ("The filled area's tag read again is not a leak", misread(("Bright", 80, 40, 75)) is True),
        ("A confident new reading of the same pixels is still a finding", misread(("Bright", 170, 40, 90)) is False),
        ("A misreading across the filled area's edge is still a finding", misread(("Bright", 150, 40, 15)) is False),
        (
            "A misreading over words first read with low confidence is still a finding",
            misread(("Bright", 222, 26, 15)) is False,
        ),
        ("Words read as before that make a finding again are still a finding", misread(with_owner=True) is False),
    ]

    try:
        CASES.append(("approved_file() releases a PASS file", approved_file(json_path) == output_path))
    except NotApproved as reason:
        print(f"        approved_file: {reason}")
        CASES.append(("approved_file() releases a PASS file", False))

    # A file changed after it was checked is refused
    original_bytes = output_path.read_bytes()
    output_path.write_bytes(original_bytes + b" ")

    try:
        approved_file(json_path)
        refused = False
    except NotApproved:
        refused = True

    output_path.write_bytes(original_bytes)
    CASES.append(("approved_file() refuses a file changed after its check", refused))

    # A value hidden back in the file (the app properties), with its
    # record updated to match: the check finds it, and deletes the file
    with zipfile.ZipFile(output_path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}

    members["docProps/app.xml"] = members["docProps/app.xml"].replace(
        b"</Properties>", f"<Manager>{EMAIL}</Manager></Properties>".encode("utf-8")
    )

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)

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
    plain_source = plain_folder / "upload" / "desk.docx"
    plain_source.parent.mkdir(parents=True)
    build_plain(plain_source, f"Call the service desk on {PHONE} for access.")

    plain_json, plain_report, _, _, plain_output, plain_validation = run_pipeline(
        plain_folder, plain_source, false_positive=PHONE
    )
    kept = Document(str(plain_output)).paragraphs[0].text if plain_output.exists() else ""

    CASES.append((
        "A value reviewed as a false positive stays in the file, which passes",
        any(item["text"] == PHONE for item in plain_report["findings"])
        and PHONE in kept and plain_validation["verdict"] == "PASS",
    ))

    # --- Another file under the same name is refused, and nothing is left ---
    other = folder / "other" / "desk.docx"
    other.parent.mkdir()
    build_plain(other, "Some other text entirely.")

    try:
        redact_original(plain_json, other)
        refused_other = False
    except ValueError:
        refused_other = True

    CASES.append((
        "A file that is not the extracted document's is refused, with no output left",
        refused_other and not redacted_path(plain_json, "desk.docx").exists()
        and not record_path(plain_json).exists(),
    ))

    details = {
        "types found": found_types,
        "leaks": leaks,
        "file checks": validation["summary"],
        "copy checks": copy_validation["summary"],
        "summary": record["summary"],
    }

print("========== ORIGINAL-FORMAT DOCX TEST ==========\n")

for description, passed in CASES:
    failures += not passed
    print(f"{'PASS' if passed else 'FAIL'}  {description}")

if failures:
    for key, value in details.items():
        print(f"        {key}: {value}")

print(f"\n{len(CASES) - failures} of {len(CASES)} cases passed")
print("===============================================")

sys.exit(1 if failures else 0)
