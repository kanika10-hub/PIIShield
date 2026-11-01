import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

from lxml import etree
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.opc.constants import CONTENT_TYPE as CT, RELATIONSHIP_TYPE as RT
from pptx.opc.package import Part
from pptx.opc.packuri import PackURI
from pptx.oxml.shapes.picture import CT_Picture
from pptx.util import Inches

from src.extractors.pptx_extractor import extract_pptx
from src.ocr.image_ocr import ocr_image
from src.original_format.redactor import record_path, redact_original, redacted_path, sha256
from src.original_format.validator import approved_file, validate_original
from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import findings_path, scan_file
from src.pii.redactor import redact_file, save_json
from src.pii.validator import NotApproved, validate_file

# The redacted PPTX file (src/original_format, steps 6 and 7) of a
# PowerPoint deck built here, with PII in places the extractor reads and
# in places it does not: runs with different formatting, a hyperlink
# target, a table, a text box in a group, speaker notes, a slide layout,
# alt text, a chart and its workbook, an image, a picture in a slide
# layout and a background picture in the master, classic and threaded
# comments with their authors, and the thumbnail. Every step runs as the
# pipeline runs it, in a temporary folder. Needs the spaCy model and
# Tesseract.

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P188 = "http://schemas.microsoft.com/office/powerpoint/2018/8/main"
THREADED_COMMENTS = "http://schemas.microsoft.com/office/2018/10/relationships/comments"
THREADED_AUTHORS = "http://schemas.microsoft.com/office/2018/10/relationships/authors"

NAME = "Grace Holloway"
EMAIL = "grace.holloway@example.com"
PHONE = "+44 7700 900481"
AUTHOR = "Marta Kowalczyk"
EDITOR = "Kenji Arakawa"
BUDDY_EMAIL = "omar.siddiqui@example.com"
COMMENTER = "Lena Marquardt"
REVIEWER = "Tobias Lindqvist"
REVIEWER_ACCOUNT = "tobias.lindqvist@example.com"

# Every value that must be gone from the file, in any XML of it
VALUES = (NAME, EMAIL, PHONE, AUTHOR, EDITOR, BUDDY_EMAIL, COMMENTER, REVIEWER, REVIEWER_ACCOUNT)

NAME_TAG = "[REDACTED: PERSON_NAME]"
EMAIL_TAG = "[REDACTED: EMAIL]"

CLASSIC_AUTHORS = f"""<p:cmAuthorLst xmlns:p="{P}">
<p:cmAuthor id="0" name="{COMMENTER}" initials="LM" lastIdx="1" clrIdx="0"/></p:cmAuthorLst>"""

CLASSIC_COMMENTS = f"""<p:cmLst xmlns:p="{P}"><p:cm authorId="0" dt="2026-01-05T10:00:00.000" idx="1">
<p:pos x="10" y="10"/><p:text>Please confirm the desk phone with {NAME} before sending.</p:text></p:cm></p:cmLst>"""

THREADED_AUTHOR_LIST = f"""<p188:authorLst xmlns:p188="{P188}">
<p188:author id="{{6A1B2C3D-0000-4000-8000-000000000001}}" name="{REVIEWER}" initials="TL"
 userId="{REVIEWER_ACCOUNT}" providerId="AD"/></p188:authorLst>"""

THREADED_COMMENT_LIST = f"""<p188:cmLst xmlns:p188="{P188}" xmlns:a="{A}">
<p188:cm id="{{6A1B2C3D-0000-4000-8000-000000000002}}" authorId="{{6A1B2C3D-0000-4000-8000-000000000001}}"
 created="2026-01-05T10:00:00.000"><p188:replyLst>
<p188:reply id="{{6A1B2C3D-0000-4000-8000-000000000003}}" authorId="{{6A1B2C3D-0000-4000-8000-000000000001}}"
 created="2026-01-05T11:00:00.000"><p188:txBody><a:bodyPr/><a:p><a:r><a:t>Done, I called {PHONE}.</a:t></a:r></a:p>
</p188:txBody></p188:reply></p188:replyLst>
<p188:txBody><a:bodyPr/><a:p><a:r><a:t>Loop in {BUDDY_EMAIL} on the welcome pack.</a:t></a:r></a:p></p188:txBody>
</p188:cm></p188:cmLst>"""


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
    prs = Presentation()
    prs.core_properties.author = AUTHOR
    prs.core_properties.last_modified_by = EDITOR

    # A value in a slide layout, which no slide shows and the extractor never reads
    layout = prs.slide_layouts[1]
    layout.placeholders[0].text_frame.text = f"Prepared for {NAME}"

    # A picture the layout puts on its slides, naming a person
    _, rel_id = layout.part.get_or_add_image_part(
        io.BytesIO(text_image(["Owner: Daniel Osei", "Phone +44 7700 900155"]))
    )
    layout.shapes._spTree.insert_element_before(
        CT_Picture.new_pic(90, "Template picture", "", rel_id, 0, 0, Inches(3), Inches(0.6)), "p:extLst"
    )

    # A background picture in the master, under every slide
    _, rel_id = prs.slide_master.part.get_or_add_image_part(io.BytesIO(text_image(["Northwind Risk"])))
    common_slide_data = prs.slide_master._element.find(f"{{{P}}}cSld")

    for old in common_slide_data.findall(f"{{{P}}}bg"):
        common_slide_data.remove(old)

    common_slide_data.insert(0, etree.fromstring(
        f'<p:bg xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"><p:bgPr><a:blipFill><a:blip r:embed="{rel_id}"/>'
        '<a:stretch><a:fillRect/></a:stretch></a:blipFill><a:effectLst/></p:bgPr></p:bg>'
    ))

    # Comment authors: classic, and threaded with a sign-in account
    for name, content_type, xml, reltype in (
        ("/ppt/commentAuthors.xml", CT.PML_COMMENT_AUTHORS, CLASSIC_AUTHORS, RT.COMMENT_AUTHORS),
        ("/ppt/authors.xml", "application/vnd.ms-powerpoint.authors+xml", THREADED_AUTHOR_LIST, THREADED_AUTHORS),
    ):
        prs.part.relate_to(Part(PackURI(name), content_type, prs.part.package, xml.encode("utf-8")), reltype)

    # --- Slide 1: text ---
    slide = prs.slides.add_slide(layout)
    slide.shapes.title.text = "New starter record"
    body = slide.placeholders[1].text_frame

    # A name over two runs, bold then italic
    paragraph = body.paragraphs[0]

    for text, style in (
        ("This record confirms that ", None), ("Grace ", "bold"), ("Holloway", "italic"),
        (" joined the Risk Analytics team in London.", None),
    ):
        run = paragraph.add_run()
        run.text = text

        if style:
            setattr(run.font, style, True)

    body.add_paragraph().text = f"She can be reached at {EMAIL} or on {PHONE}."

    # A hyperlink whose target holds the email, not its text
    paragraph = body.add_paragraph()
    paragraph.add_run().text = "Questions go to "
    run = paragraph.add_run()
    run.text = "the new starter"
    run.hyperlink.address = f"mailto:{EMAIL}"

    slide.notes_slide.notes_text_frame.text = f"Call {PHONE} if the badge does not work."

    # A threaded comment with a reply
    slide.part.relate_to(Part(
        PackURI("/ppt/comments/modernComment_100_0.xml"), "application/vnd.ms-powerpoint.comments+xml",
        prs.part.package, THREADED_COMMENT_LIST.encode("utf-8")
    ), THREADED_COMMENTS)

    # --- Slide 2: a table, a group, a picture and a chart ---
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    table = slide.shapes.add_table(2, 2, Inches(0.5), Inches(0.3), Inches(6), Inches(0.8)).table
    for (row, column), text in {(0, 0): "Name", (0, 1): "Email", (1, 0): NAME, (1, 1): EMAIL}.items():
        table.cell(row, column).text = text

    group = slide.shapes.add_group_shape()
    group.shapes.add_textbox(Inches(0.5), Inches(1.3), Inches(5), Inches(0.5)).text_frame.text = (
        f"Buddy contact: {BUDDY_EMAIL}"
    )

    picture = slide.shapes.add_picture(
        io.BytesIO(text_image(["Approved by: Daniel Osei", "Mobile +44 7700 900733"])),
        Inches(0.5), Inches(2.0), width=Inches(5)
    )
    picture._element.nvPicPr.cNvPr.set("descr", f"Signed form of {NAME}")

    chart_data = CategoryChartData()
    chart_data.categories = ["FY2025"]
    chart_data.add_series(NAME, (12,))
    slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(6), Inches(2.0), Inches(3.5), Inches(3), chart_data
    )

    # A classic comment
    slide.part.relate_to(Part(
        PackURI("/ppt/comments/comment1.xml"), CT.PML_COMMENTS, prs.part.package, CLASSIC_COMMENTS.encode("utf-8")
    ), RT.COMMENTS)

    prs.save(path)


def build_plain(path, text):
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1)).text_frame.text = text
    prs.save(path)


def run_pipeline(folder, source, false_positive=None):
    """
    Steps 1-4 as main.py runs them, then steps 6 and 7. false_positive:
    the value whose finding a reviewer marked as a false positive.
    """

    json_path = folder / f"{source.stem}.json"
    report_path = findings_path(json_path)
    extract_pptx(str(source)).save_json(json_path)
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


def xml_texts(data, prefix=""):
    """
    Every XML file of a zip, embedded packages included (a chart's
    workbook), as text.
    """

    texts = {}

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            content = archive.read(name)

            if name.endswith((".xml", ".rels")):
                texts[prefix + name] = etree.tostring(etree.fromstring(content), method="xml", encoding="unicode")
            elif content[:4] == b"PK\x03\x04":
                texts.update(xml_texts(content, f"{prefix}{name}/"))

    return texts


nlp = LocalNLPDetector()

if nlp.error:
    sys.exit(f"The spaCy model is needed: {nlp.error}")

failures = 0
CASES = []

with tempfile.TemporaryDirectory() as temp:
    folder = Path(temp)

    # --- The full deck ---
    source = folder / "upload" / "starter.pptx"
    source.parent.mkdir()
    build(source)

    json_path, report, copy_validation, record, output_path, validation = run_pipeline(folder, source)
    texts = xml_texts(output_path.read_bytes())
    found_types = sorted({item["type"] for item in report["findings"]})

    with zipfile.ZipFile(output_path) as archive:
        names = archive.namelist()

    redacted = Presentation(str(output_path))
    first, second = redacted.slides
    split = first.placeholders[1].text_frame.paragraphs[0]
    tagged_run = next((run for run in split.runs if NAME_TAG in run.text), None)
    table = next(shape for shape in second.shapes if shape.has_table).table
    group = next(shape for shape in second.shapes if shape.shape_type == 6)
    picture = next(shape for shape in second.shapes if shape.shape_type == 13)
    notes = first.notes_slide.notes_text_frame.text
    rels = texts["ppt/slides/_rels/slide1.xml.rels"]
    layouts = [text for name, text in texts.items() if name.startswith("ppt/slideLayouts/slideLayout")]
    charts = [text for name, text in texts.items() if name.startswith(("ppt/charts/", "ppt/embeddings/"))]
    ocr_text, _ = ocr_image(Image.open(io.BytesIO(picture.image.blob)).convert("RGB"))
    leaks =sorted({value for value in VALUES for text in texts.values() if value in text})

    extracted = json.loads(json_path.read_text(encoding="utf-8"))
    template_blocks = [
        block["block_id"] for page in extracted["pages"] for block in page["blocks"]
        if block["metadata"].get("location") == "template"
    ]
    comment_blocks = [
        block["block_id"] for page in extracted["pages"] for block in page["blocks"]
        if block["metadata"].get("location") == "comment"
    ]
    layout_image = next(
        rel.target_part.blob for rel in redacted.slide_layouts[1].part.rels.values() if rel.reltype == RT.IMAGE
    )
    layout_ocr, _ = ocr_image(Image.open(io.BytesIO(layout_image)).convert("RGB"))
    classic_author = etree.fromstring(texts["ppt/commentAuthors.xml"]).find(f"{{{P}}}cmAuthor")
    threaded_author = etree.fromstring(texts["ppt/authors.xml"]).find(f"{{{P188}}}author")
    classic_text = texts["ppt/comments/comment1.xml"]
    threaded_text = texts["ppt/comments/modernComment_100_0.xml"]

    CASES += [
        ("The values are found and the sanitized copy passes", copy_validation["verdict"] == "PASS"),
        ("The redacted file is written with nothing left unplaced", record["status"] == "completed"),
        ("The redacted file passes its check", validation["verdict"] == "PASS"),
        ("No value is left in any XML of the file, embedded workbooks included", not leaks),
        (
            "A name over two runs is one tag, in the first run's formatting",
            split.text.startswith(f"This record confirms that {NAME_TAG} joined")
            and tagged_run is not None and tagged_run.font.bold,
        ),
        (
            "A hyperlink target keeps a valid URI without the email",
            "mailto:%5BREDACTED:%20EMAIL%5D" in rels and EMAIL not in rels,
        ),
        (
            "Table cells are redacted",
            table.cell(1, 0).text == NAME_TAG and table.cell(1, 1).text == EMAIL_TAG
            and table.cell(0, 0).text == "Name",
        ),
        ("A text box in a group is redacted", group.shapes[0].text_frame.text == f"Buddy contact: {EMAIL_TAG}"),
        ("Speaker notes are redacted", PHONE not in notes and "[REDACTED" in notes),
        (
            "A value in a slide layout no slide shows is replaced",
            any(f"Prepared for {NAME_TAG}" in text for text in layouts) and not any(NAME in text for text in layouts),
        ),
        ("Alt text loses the name", NAME not in (picture._element.nvPicPr.cNvPr.get("descr") or NAME)),
        ("The chart and its workbook lose the name", charts and not any(NAME in text for text in charts)),
        (
            "The image no longer shows the name or phone",
            "Osei" not in ocr_text and "900733" not in ocr_text and "Approved by" in ocr_text,
        ),
        ("The first-slide thumbnail is removed", not any("thumbnail" in name for name in names)),
        (
            "The layout picture and master background are read once, as template blocks",
            template_blocks == ["slide1_layout_image1", "slide1_master_image2"],
        ),
        (
            "The layout picture no longer shows the name or phone",
            "Osei" not in layout_ocr and "900155" not in layout_ocr and "Owner" in layout_ocr,
        ),
        ("No image in the file is left unread", validation["summary"]["unread"] == "PASS"),
        (
            "Classic and threaded comments and a reply are read",
            comment_blocks == ["slide1_comment1", "slide1_comment2", "slide2_comment1"],
        ),
        (
            "A classic comment loses the name, and its author the name and initials",
            NAME not in classic_text and NAME_TAG in classic_text
            and classic_author.get("name") == NAME_TAG and classic_author.get("initials") == "",
        ),
        (
            "A threaded comment and its reply lose their values, and the author's account is cleared",
            BUDDY_EMAIL not in threaded_text and PHONE not in threaded_text and EMAIL_TAG in threaded_text
            and threaded_author.get("name") == NAME_TAG and threaded_author.get("userId") == "",
        ),
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
    plain_source = plain_folder / "upload" / "desk.pptx"
    plain_source.parent.mkdir(parents=True)
    build_plain(plain_source, f"Call the service desk on {PHONE} for access.")

    plain_json, plain_report, _, _, plain_output, plain_validation = run_pipeline(
        plain_folder, plain_source, false_positive=PHONE
    )
    kept = Presentation(str(plain_output)).slides[0].shapes[0].text_frame.text if plain_output.exists() else ""

    CASES.append((
        "A value reviewed as a false positive stays in the file, which passes",
        any(item["text"] == PHONE for item in plain_report["findings"])
        and PHONE in kept and plain_validation["verdict"] == "PASS",
    ))

    # --- Another file under the same name is refused, and nothing is left ---
    other = folder / "other" / "desk.pptx"
    other.parent.mkdir()
    build_plain(other, "Some other text entirely.")

    try:
        redact_original(plain_json, other)
        refused_other = False
    except ValueError:
        refused_other = True

    CASES.append((
        "A file that is not the extracted document's is refused, with no output left",
        refused_other and not redacted_path(plain_json, "desk.pptx").exists()
        and not record_path(plain_json).exists(),
    ))

    details = {
        "types found": found_types,
        "leaks": leaks,
        "file checks": validation["summary"],
        "copy checks": copy_validation["summary"],
        "summary": record["summary"],
        "errors": record["errors"],
    }

print("========== ORIGINAL-FORMAT PPTX TEST ==========\n")

for description, passed in CASES:
    failures += not passed
    print(f"{'PASS' if passed else 'FAIL'}  {description}")

if failures:
    for key, value in details.items():
        print(f"        {key}: {value}")

print(f"\n{len(CASES) - failures} of {len(CASES)} cases passed")
print("===============================================")

sys.exit(1 if failures else 0)
