import sys
import tempfile
from pathlib import Path

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from lxml import etree

from src.extractors.docx_extractor import extract_docx

# DOCX content outside plain paragraphs and tables: a comment names the
# paragraph it is about (metadata.anchor_block_id), a chart and a SmartArt
# diagram are read as text, and an embedded object is reported. The test
# writes its own document; nothing is OCR'd.

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"

CHART = f"""<c:chartSpace xmlns:c="{C}" xmlns:a="{A}"><c:chart>
<c:title><c:tx><c:rich><a:p><a:r><a:t>Headcount by site</a:t></a:r></a:p></c:rich></c:tx></c:title>
<c:plotArea><c:barChart><c:ser>
<c:tx><c:strRef><c:strCache><c:pt idx="0"><c:v>Leeds office</c:v></c:pt></c:strCache></c:strRef></c:tx>
<c:cat><c:strRef><c:strCache><c:pt idx="0"><c:v>FY2025</c:v></c:pt></c:strCache></c:strRef></c:cat>
</c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>"""

DIAGRAM = f"""<dgm:dataModel xmlns:dgm="{DGM}" xmlns:a="{A}"><dgm:ptLst>
<dgm:pt modelId="1"><dgm:t><a:p><a:r><a:t>Plan</a:t></a:r></a:p></dgm:t></dgm:pt>
<dgm:pt modelId="2"><dgm:t><a:p><a:r><a:t>Review</a:t></a:r></a:p></dgm:t></dgm:pt>
</dgm:ptLst></dgm:dataModel>"""

OLE = (
    f'<w:object xmlns:w="{W}" xmlns:o="urn:schemas-microsoft-com:office:office">'
    '<o:OLEObject Type="Embed" ProgID="Excel.Sheet.12" ShapeID="_x0000_i1025" '
    'DrawAspect="Content" ObjectID="_1"/></w:object>'
)


def add_graphic(doc, number, uri, inner):
    """
    A paragraph holding an inline drawing whose graphic data is inner.
    """

    run = doc.add_paragraph().add_run()
    run._r.append(etree.fromstring(
        f'<w:drawing xmlns:w="{W}" xmlns:wp="{WP}" xmlns:a="{A}"><wp:inline>'
        f'<wp:extent cx="914400" cy="914400"/><wp:docPr id="{number}" name="Graphic {number}"/>'
        f'<a:graphic><a:graphicData uri="{uri}">{inner}</a:graphicData></a:graphic>'
        '</wp:inline></w:drawing>'
    ))


def add_part(doc, name, content_type, xml, relationship):
    part = Part(PackURI(name), content_type, xml.encode("utf-8"), doc.part.package)
    return doc.part.relate_to(part, relationship)


def build(path):
    doc = Document()
    doc.add_paragraph("An opening paragraph without comments.")
    target = doc.add_paragraph("The paragraph the comment is about.")
    doc.add_comment(target.runs, text="Check this figure.", author="Reviewer", initials="RV")

    chart_id = add_part(
        doc, "/word/charts/chart1.xml",
        "application/vnd.openxmlformats-officedocument.drawingml.chart+xml", CHART, RT.CHART
    )
    add_graphic(doc, 1, C, f'<c:chart xmlns:c="{C}" xmlns:r="{R}" r:id="{chart_id}"/>')

    data_id = add_part(
        doc, "/word/diagrams/data1.xml",
        "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml", DIAGRAM, RT.DIAGRAM_DATA
    )
    add_graphic(doc, 2, DGM, f'<dgm:relIds xmlns:dgm="{DGM}" xmlns:r="{R}" r:dm="{data_id}"/>')

    doc.add_paragraph().add_run()._r.append(etree.fromstring(OLE))
    doc.save(path)


with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / "structure.docx"
    build(path)
    blocks = {block.block_id: block for block in extract_docx(str(path)).pages[0].blocks}

by_type = {}

for block in blocks.values():
    by_type.setdefault(block.block_type, []).append(block)

comment = next(block for block in blocks.values() if block.metadata.get("location") == "comment")
anchor = blocks.get(comment.metadata.get("anchor_block_id"))
chart = (by_type.get("chart") or [None])[0]
diagram = (by_type.get("diagram") or [None])[0]
embedded = (by_type.get("embedded_object") or [None])[0]

CASES = [
    (
        "A comment names the paragraph it is about",
        anchor is not None and anchor.text == "The paragraph the comment is about.",
    ),
    (
        "A chart is read: title, series name and category label",
        chart is not None and chart.text.split("\n") == ["Headcount by site", "Leeds office", "FY2025"],
    ),
    (
        "A SmartArt diagram is read: one line per node",
        diagram is not None and diagram.text == "Plan\nReview",
    ),
    (
        "An embedded object is reported with its kind",
        embedded is not None and embedded.metadata.get("prog_id") == "Excel.Sheet.12",
    ),
]

failures = 0

print("========== DOCX STRUCTURE TEST ==========\n")

for description, passed in CASES:
    failures += not passed
    print(f"{'PASS' if passed else 'FAIL'}  {description}")

if failures:
    for block in blocks.values():
        print(f"        {block.block_id} {block.block_type} {block.text!r} {block.metadata}")

print(f"\n{len(CASES) - failures} of {len(CASES)} cases passed")
print("=========================================")

sys.exit(1 if failures else 0)
