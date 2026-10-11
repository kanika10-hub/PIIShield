
from pathlib import Path

from docx import Document
from docx.oxml.ns import nsmap, qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from src.models.document import (
    ExtractedDocument,
    DocumentPage,
    DocumentBlock
)

NAMESPACES = {"w": nsmap["w"]}


def content_control_name(sdt):
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
    """Yield paragraphs and tables in their original XML order."""

    for element in parent.iterchildren():

        if element.tag in (qn("w:p"), qn("w:tbl")):
            yield element, content_control

        elif element.tag == qn("w:sdt"):
            for content in element.iterchildren(qn("w:sdtContent")):
                yield from iter_block_elements(
                    content, content_control_name(element)
                )


def paragraph_text(paragraph):
    """Read paragraph runs, including runs inside inline controls."""

    runs = paragraph._p.xpath(
        ".//w:r[not(ancestor::w:txbxContent)]"
    )
    return "".join(Run(run, paragraph).text for run in runs)


def extract_table_cells(table, table_number):
    """Preserve physical cells and merged-cell spans."""

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
            for element, _ in iter_block_elements(cell._tc)
            if element.tag == qn("w:p")
        ).strip()

        cells.append(DocumentBlock(
            block_id=(
                f"table_{table_number}_"
                f"row_{span['row']}_cell_{span['column']}"
            ),
            block_type="table_cell",
            text=text,
            order=cell_order,
            metadata={
                "source": "native_docx",
                "row": span["row"],
                "column": span["column"],
                "row_span": len(span["rows"]),
                "column_span": len(span["columns"])
            }
        ))

    return cells


def extract_part_images(part, prefix, visible, section_index,
                        part_type, linked_to_previous=False):
    """Record image references without claiming their text was OCR'd."""

    blocks = []
    seen = set()

    for blip in part._element.xpath(".//a:blip"):
        relationship_id = blip.get(
            "{http://schemas.openxmlformats.org/"
            "officeDocument/2006/relationships}embed"
        )

        if not relationship_id or relationship_id in seen:
            continue

        seen.add(relationship_id)

        try:
            image_part = part.related_parts[relationship_id]
            image_name = str(image_part.partname)
            content_type = image_part.content_type
        except (KeyError, AttributeError):
            image_name = None
            content_type = None

        blocks.append(DocumentBlock(
            block_id=f"{prefix}_image_{len(blocks) + 1}",
            block_type="image",
            text="",
            order=len(blocks) + 1,
            metadata={
                "source": "docx_header_footer",
                "section": section_index,
                "part_type": part_type,
                "relationship_id": relationship_id,
                "image_part": image_name,
                "content_type": content_type,
                "visible": visible,
                "linked_to_previous": linked_to_previous,
                "ocr_status": "not_processed_by_this_extractor"
            }
        ))

    return blocks


def extract_header_footer_part(part, section_index, part_type,
                               visible, blocks, order,
                               linked_to_previous=False):
    """Extract text, tables, text boxes, and image references."""

    prefix = f"section_{section_index}_{part_type}"

    # A linked part inherits content from an earlier section.
    # Record the link rather than duplicating the inherited content.
    if linked_to_previous:
        blocks.append(DocumentBlock(
            block_id=f"{prefix}_linked",
            block_type="header_footer_reference",
            text="",
            order=order,
            metadata={
                "source": "native_docx",
                "section": section_index,
                "part_type": part_type,
                "linked_to_previous": True,
                "visible": visible,
                "content_extracted_here": False
            }
        ))
        return order + 1

    paragraph_number = 0
    table_number = 0

    for element, control in iter_block_elements(part._element):

        if element.tag == qn("w:p"):
            paragraph = Paragraph(element, part)
            value = paragraph_text(paragraph).strip()

            # Text boxes are extracted separately below.
            if value:
                paragraph_number += 1
                metadata = {
                    "source": "native_docx",
                    "source_part": prefix,
                    "section": section_index,
                    "part_type": part_type,
                    "paragraph_index": paragraph_number,
                    "style": paragraph.style.name,
                    "visible": visible,
                    "linked_to_previous": False,
                    "field_instructions": element.xpath(
                        ".//w:instrText/text()"
                    )
                }

                if control:
                    metadata["content_control"] = control

                blocks.append(DocumentBlock(
                    block_id=f"{prefix}_paragraph_{paragraph_number}",
                    block_type="header_footer",
                    text=value,
                    order=order,
                    metadata=metadata
                ))
                order += 1

        elif element.tag == qn("w:tbl"):
            table_number += 1
            table = Table(element, part)

            blocks.append(DocumentBlock(
                block_id=f"{prefix}_table_{table_number}",
                block_type="table",
                order=order,
                children=extract_table_cells(
                    table, f"{prefix}_{table_number}"
                ),
                metadata={
                    "source": "native_docx",
                    "source_part": prefix,
                    "section": section_index,
                    "part_type": part_type,
                    "rows": len(table.rows),
                    "columns": len(table.columns),
                    "visible": visible,
                    "linked_to_previous": False
                }
            ))
            order += 1

    # Text-box text is separate from ordinary paragraph text.
    # Do not include the same text twice.
    text_box_number = 0

    for text_box in part._element.xpath(".//w:txbxContent"):
        value = "".join(text_box.xpath(".//w:t/text()")).strip()

        if value:
            text_box_number += 1
            blocks.append(DocumentBlock(
                block_id=f"{prefix}_textbox_{text_box_number}",
                block_type="text_box",
                text=value,
                order=order,
                metadata={
                    "source": "native_docx",
                    "source_part": prefix,
                    "section": section_index,
                    "part_type": part_type,
                    "visible": visible
                }
            ))
            order += 1

    image_blocks = extract_part_images(
        part, prefix, visible, section_index, part_type
    )

    for image_block in image_blocks:
        image_block.order = order
        blocks.append(image_block)
        order += 1

    return order


def extract_docx(file_path):
    doc = Document(file_path)

    blocks = []
    order = 1
    paragraph_count = 0
    table_count = 0

    # Main body: retain paragraph/table reading order.
    for element, content_control in iter_block_elements(doc.element.body):

        if element.tag == qn("w:p"):
            paragraph = Paragraph(element, doc)
            value = paragraph_text(paragraph).strip()

            if not value:
                continue

            paragraph_count += 1
            style = paragraph.style.name

            metadata = {
                "style": style,
                "source": "native_docx",
                "paragraph_index": paragraph_count
            }

            if content_control:
                metadata["content_control"] = content_control

            blocks.append(DocumentBlock(
                block_id=f"paragraph_{paragraph_count}",
                block_type=(
                    "heading" if style.startswith("Heading")
                    else "paragraph"
                ),
                text=value,
                order=order,
                metadata=metadata
            ))
            order += 1

        elif element.tag == qn("w:tbl"):
            table = Table(element, doc)
            table_count += 1

            metadata = {
                "source": "native_docx",
                "table_index": table_count,
                "rows": len(table.rows),
                "columns": len(table.columns)
            }

            if content_control:
                metadata["content_control"] = content_control

            blocks.append(DocumentBlock(
                block_id=f"table_{table_count}",
                block_type="table",
                order=order,
                children=extract_table_cells(table, table_count),
                metadata=metadata
            ))
            order += 1

    # Headers and footers are stored as separately identified parts.
    # They are not inserted into the body reading order.
    for section_index, section in enumerate(doc.sections, start=1):
        first_page_enabled = section.different_first_page_header_footer
        odd_even_enabled = doc.settings.odd_and_even_pages_header_footer

        parts = [
            ("default_header", section.header, True),
            ("default_footer", section.footer, True),
            (
                "first_page_header",
                section.first_page_header,
                first_page_enabled
            ),
            (
                "first_page_footer",
                section.first_page_footer,
                first_page_enabled
            ),
            (
                "even_page_header",
                section.even_page_header,
                odd_even_enabled
            ),
            (
                "even_page_footer",
                section.even_page_footer,
                odd_even_enabled
            )
        ]

        for part_type, part, visible in parts:
            linked = part.is_linked_to_previous

            order = extract_header_footer_part(
                part=part,
                section_index=section_index,
                part_type=part_type,
                visible=visible,
                blocks=blocks,
                order=order,
                linked_to_previous=linked
            )

    page = DocumentPage(
        page_number=1,
        blocks=blocks,
        metadata={
            "content_type": "docx_content",
            "note": (
                "Logical extraction order; physical page positions "
                "are not resolved by python-docx."
            )
        }
    )

    return ExtractedDocument(
        document_id="cadence_tprm_training",
        file_name=Path(file_path).name,
        source_type="DOCX",
        extraction_method="native_docx",
        pages=[page],
        metadata={
            "paragraph_count": paragraph_count,
            "table_count": table_count,
            "embedded_image_count": len(doc.inline_shapes),
            "header_footer_extracted": True,
            "limitations": [
                "Physical page positions are not resolved",
                "Header/footer image text requires OCR",
                "Body image OCR must be merged separately",
                "Text inside some complex drawing formats may remain "
                "unextracted"
            ]
        }
    )
