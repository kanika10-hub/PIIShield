
from pathlib import Path

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import nsmap, qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from lxml import etree

from src.extractors.common import office_properties, page_text_metadata, text_method
from src.models.document import (
    ExtractedDocument,
    DocumentPage,
    DocumentBlock
)
from src.ocr.image_ocr import ImageReader

# Plain lxml elements (e.g. w:sdt) need the namespace map passed to xpath()
NAMESPACES = {
    "w": nsmap["w"],
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006"
}

# Word stores each text box and drawing twice: as a drawing, and as a
# VML copy inside mc:Fallback for old readers. Only the first is read.
TEXT_BOXES = etree.XPath(
    ".//w:txbxContent[not(ancestor::mc:Fallback)]", namespaces=NAMESPACES
)
IMAGES = etree.XPath(
    ".//a:blip[not(ancestor::mc:Fallback)]/@r:embed", namespaces=NAMESPACES
)
TEXT_BOX_PARAGRAPHS = etree.XPath(".//w:p", namespaces=NAMESPACES)
PARAGRAPH_TEXT = etree.XPath(".//w:t/text()", namespaces=NAMESPACES)

# A cell's paragraphs, including those of tables nested in it
CELL_PARAGRAPHS = etree.XPath(
    ".//w:p[not(ancestor::w:txbxContent)]", namespaces=NAMESPACES
)

LIST_LEVEL = etree.XPath("./w:pPr/w:numPr/w:ilvl/@w:val", namespaces=NAMESPACES)
IS_LIST = etree.XPath("boolean(./w:pPr/w:numPr)", namespaces=NAMESPACES)
ENDS_SECTION = etree.XPath("boolean(./w:pPr/w:sectPr)", namespaces=NAMESPACES)

# Footnotes and endnotes that are separators, not notes
NOTE_SEPARATORS = ("separator", "continuationSeparator", "continuationNotice")

# Every header and footer a section can define
HEADER_FOOTER_PARTS = (
    "header", "footer",
    "first_page_header", "first_page_footer",
    "even_page_header", "even_page_footer"
)


def content_control_name(sdt):
    """
    Return a readable name for a content control (w:sdt),
    e.g. "Table of Contents" or a cover page field alias.
    """

    for query in (
        "./w:sdtPr/w:docPartObj/w:docPartGallery/@w:val",
        "./w:sdtPr/w:alias/@w:val",
        "./w:sdtPr/w:tag/@w:val"
    ):
        values = sdt.xpath(query, namespaces=NAMESPACES)

        if values:
            return values[0]

    return "content_control"


def iter_block_elements(parent, content_control=None):
    """
    Yield (element, content_control) for every paragraph and table
    directly under parent, in document order.

    Block-level content controls such as the table of contents wrap
    their own paragraphs and tables, so their content is yielded in
    place, tagged with the control's name.
    """

    for element in parent.iterchildren():

        if element.tag in (qn("w:p"), qn("w:tbl")):
            yield element, content_control

        elif element.tag == qn("w:sdt"):
            for content in element.iterchildren(qn("w:sdtContent")):
                yield from iter_block_elements(
                    content,
                    content_control_name(element)
                )


def paragraph_text(paragraph):
    """
    Return the full text of a paragraph.

    paragraph.text skips runs inside inline content controls, which
    hold cover page fields such as the client name and project title.
    This reads every run in the paragraph instead, except runs inside
    text boxes: those are stored twice (drawing and VML fallback).
    """

    runs = paragraph._p.xpath(".//w:r[not(ancestor::w:txbxContent)]")

    return "".join(Run(run, paragraph).text for run in runs)


def extract_table_cells(table, table_number, location="body"):
    """
    Return one table_cell block per physical cell, in row order.

    python-docx repeats a merged cell for every grid position it
    covers. Each cell is emitted once, at its top-left position,
    with the number of rows and columns it spans. A table nested in a
    cell is part of that cell's text.
    """

    spans = {}

    for row_number, row in enumerate(table.rows, start=1):
        for column_number, cell in enumerate(row.cells, start=1):

            span = spans.setdefault(cell._tc, {
                "cell": cell,
                "row": row_number,
                "column": column_number,
                "rows": set(),
                "columns": set()
            })

            span["rows"].add(row_number)
            span["columns"].add(column_number)

    cells = []

    for cell_order, span in enumerate(spans.values(), start=1):

        cell = span["cell"]

        text = "\n".join(
            paragraph_text(Paragraph(element, cell))
            for element in CELL_PARAGRAPHS(cell._tc)
        ).strip()

        # Empty cells are kept so the table grid stays intact
        cells.append(
            DocumentBlock(
                block_id=(
                    f"table_{table_number}_"
                    f"row_{span['row']}_"
                    f"cell_{span['column']}"
                ),
                block_type="table_cell",
                text=text,
                order=cell_order,
                metadata={
                    "source": "native",
                    "location": location,
                    "row": span["row"],
                    "column": span["column"],
                    "row_span": len(span["rows"]),
                    "column_span": len(span["columns"])
                }
            )
        )

    return cells


def lines_text(paragraphs):
    """
    Text of paragraph elements read with lxml (text boxes, notes):
    one line per paragraph.
    """

    return "\n".join("".join(PARAGRAPH_TEXT(paragraph)) for paragraph in paragraphs).strip()


class StoryReader:
    """
    Reads one story of the document (the body, or a header or footer)
    into blocks, in reading order. Each paragraph or table is followed
    by the text boxes and images inside it.
    """

    def __init__(self, story, prefix, metadata, reader, section=1, track_sections=False):
        self.story = story                  # python-docx parent of its paragraphs
        self.prefix = prefix                # block ID prefix, "" for the body
        self.metadata = metadata            # location (and part) of every block
        self.reader = reader
        self.section = section
        self.track_sections = track_sections
        self.counts = {}
        self.blocks = []

    def next_number(self, kind):
        self.counts[kind] = self.counts.get(kind, 0) + 1
        return self.counts[kind]

    def block_metadata(self, content_control=None):

        metadata = {"source": "native", **self.metadata, "section": self.section}

        if content_control:
            metadata["content_control"] = content_control

        return metadata

    def read(self, element):

        for child, content_control in iter_block_elements(element):

            if child.tag == qn("w:p"):
                self.paragraph(child, content_control)
            else:
                self.table(child, content_control)

            self.text_boxes(child)
            self.images(child)

            # A paragraph holding section properties ends its section
            if self.track_sections and child.tag == qn("w:p") and ENDS_SECTION(child):
                self.section += 1

        return self.blocks

    def paragraph(self, element, content_control):

        paragraph = Paragraph(element, self.story)
        text = paragraph_text(paragraph).strip()

        if not text:
            return

        try:
            style = paragraph.style.name if paragraph.style is not None else ""
        except (KeyError, ValueError, AttributeError):
            style = ""

        if style.startswith("Heading") or style == "Title":
            block_type = "heading"
        elif IS_LIST(element) or style.startswith("List"):
            block_type = "list_item"
        else:
            block_type = "paragraph"

        number = self.next_number("paragraph")
        metadata = {**self.block_metadata(content_control), "style": style, "paragraph_index": number}

        if block_type == "list_item":
            # Set on the paragraph, or implied by the style ("List Bullet 2")
            levels = LIST_LEVEL(element)
            style_level = style.rsplit(" ", 1)[-1]

            if levels:
                metadata["level"] = int(levels[0])
            elif style_level.isdigit():
                metadata["level"] = int(style_level) - 1
            else:
                metadata["level"] = 0

        self.blocks.append(DocumentBlock(
            block_id=f"{self.prefix}paragraph_{number}",
            block_type=block_type,
            text=text,
            metadata=metadata
        ))

    def table(self, element, content_control):

        table = Table(element, self.story)
        number = self.next_number("table")

        self.blocks.append(DocumentBlock(
            block_id=f"{self.prefix}table_{number}",
            block_type="table",
            children=extract_table_cells(
                table, f"{self.prefix}{number}" if self.prefix else number,
                self.metadata["location"]
            ),
            metadata={
                **self.block_metadata(content_control),
                "rows": len(table.rows),
                "columns": len(table.columns)
            }
        ))

    def text_boxes(self, element):

        for box in TEXT_BOXES(element):
            text = lines_text(TEXT_BOX_PARAGRAPHS(box))

            if text:
                self.blocks.append(DocumentBlock(
                    block_id=f"{self.prefix}textbox_{self.next_number('textbox')}",
                    block_type="paragraph",
                    text=text,
                    metadata={**self.block_metadata(), "location": "text_box", "in": self.metadata["location"]}
                ))

    def images(self, element):

        for relationship_id in IMAGES(element):
            part = self.story.part.related_parts.get(relationship_id)

            if part is None:
                continue                    # a linked image: not in the file

            metadata = {**self.block_metadata(), "image_part": str(part.partname)}
            del metadata["source"]

            self.blocks.append(self.reader.block(
                f"{self.prefix}image_{self.next_number('image')}",
                str(part.partname), part.blob, metadata=metadata
            ))


def header_footer_blocks(doc, reader):
    """
    Blocks from every section's headers and footers.

    A header or footer linked to the previous section repeats that
    section's content and is skipped. Ones Word does not display (a
    first-page header while "different first page" is off) are still
    read, since their text is in the file; metadata.visible says which.
    """

    blocks = []
    odd_and_even = doc.settings.odd_and_even_pages_header_footer

    for section_number, section in enumerate(doc.sections, start=1):
        for name in HEADER_FOOTER_PARTS:
            part = getattr(section, name)

            if part.is_linked_to_previous:
                continue

            if name.startswith("first_page"):
                visible = section.different_first_page_header_footer
            elif name.startswith("even_page"):
                visible = odd_and_even
            else:
                visible = True

            story = StoryReader(
                part, f"section_{section_number}_{name}_",
                {
                    "location": "header" if name.endswith("header") else "footer",
                    "part": name,
                    "visible": visible
                },
                reader, section=section_number
            )

            blocks.extend(story.read(part._element))

    return blocks


def note_blocks(doc, relationship, kind):
    """
    One block per footnote or endnote, skipping the separators Word adds.
    """

    try:
        part = doc.part.part_related_by(relationship)
    except KeyError:
        return []

    blocks = []

    for note in etree.fromstring(part.blob):

        if note.get(qn("w:type")) in NOTE_SEPARATORS:
            continue

        text = lines_text(TEXT_BOX_PARAGRAPHS(note))

        if text:
            blocks.append(DocumentBlock(
                block_id=f"{kind}_{note.get(qn('w:id'))}",
                block_type="paragraph",
                text=text,
                metadata={"source": "native", "location": kind, "note_id": note.get(qn("w:id"))}
            ))

    return blocks


def comment_blocks(doc):
    """
    One block per comment. The author is kept in metadata.
    """

    # Reading doc.comments would add an empty comments part
    if not any(rel.reltype == RT.COMMENTS for rel in doc.part.rels.values()):
        return []

    blocks = []

    for comment in doc.comments:
        text = comment.text.strip()

        if text:
            blocks.append(DocumentBlock(
                block_id=f"comment_{comment.comment_id}",
                block_type="paragraph",
                text=text,
                metadata={
                    "source": "native",
                    "location": "comment",
                    "comment_id": comment.comment_id,
                    "author": comment.author,
                    "initials": comment.initials
                }
            ))

    return blocks


def extract_docx(file_path):

    doc = Document(file_path)
    reader = ImageReader()

    # The body in reading order, then headers and footers, then notes and
    # comments: they sit outside the body's reading order
    blocks = (
        StoryReader(doc, "", {"location": "body"}, reader, track_sections=True).read(doc.element.body)
        + header_footer_blocks(doc, reader)
        + note_blocks(doc, RT.FOOTNOTES, "footnote")
        + note_blocks(doc, RT.ENDNOTES, "endnote")
        + comment_blocks(doc)
    )

    for order, block in enumerate(blocks, start=1):
        block.order = order

    # DOCX has no fixed page boundaries: the whole document is page 1
    page = DocumentPage(
        page_number=1,
        blocks=blocks,
        metadata={
            "content_type": "docx_body",
            "extraction_method": text_method(blocks),
            **page_text_metadata(blocks)
        }
    )

    return ExtractedDocument(
        document_id=Path(file_path).stem,
        file_name=Path(file_path).name,
        source_type="DOCX",
        extraction_method=page.metadata["extraction_method"],
        pages=[page],
        metadata={
            "page_count": 1,
            "section_count": len(doc.sections),
            "properties": office_properties(doc.core_properties),
            "limitations": []
        }
    )
