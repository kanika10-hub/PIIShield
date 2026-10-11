from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
from pptx.util import Emu

from src.extractors.common import combined_method, office_properties, page_text_metadata, text_method
from src.models.document import (
    ExtractedDocument,
    DocumentPage,
    DocumentBlock,
    BoundingBox
)
from src.ocr.image_ocr import ImageReader


DIAGRAM_URI = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
DRAWING_NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}

# SmartArt text: each node's paragraphs in the diagram data part
DIAGRAM_PARAGRAPHS = etree.XPath(".//a:p", namespaces=DRAWING_NS)
PARAGRAPH_TEXT = etree.XPath(".//a:t/text()", namespaces=DRAWING_NS)

# Placeholders whose paragraphs are bullets unless they say otherwise
BULLET_PLACEHOLDERS = (PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT)

# A shape inside a group has its position in the group's own coordinate
# space. A transform (offset_x, offset_y, scale_x, scale_y) maps it to
# the slide: slide = offset + position * scale, in EMU.
SLIDE_TRANSFORM = (0, 0, 1, 1)


def get_bounding_box(shape, transform=SLIDE_TRANSFORM):
    """
    Return the shape position on the slide in points, or None if
    unavailable.
    """

    if None in (shape.left, shape.top, shape.width, shape.height):
        return None

    offset_x, offset_y, scale_x, scale_y = transform

    return BoundingBox(
        x=Emu(int(offset_x + shape.left * scale_x)).pt,
        y=Emu(int(offset_y + shape.top * scale_y)).pt,
        width=Emu(int(shape.width * scale_x)).pt,
        height=Emu(int(shape.height * scale_y)).pt
    )


def group_transform(group, transform):
    """
    The transform for shapes inside a group shape.
    """

    xfrm = group._element.grpSpPr.xfrm

    if xfrm is None or xfrm.chOff is None or xfrm.chExt is None or not xfrm.chExt.cx or not xfrm.chExt.cy:
        return transform

    offset_x, offset_y, scale_x, scale_y = transform
    group_scale_x = xfrm.ext.cx / xfrm.chExt.cx
    group_scale_y = xfrm.ext.cy / xfrm.chExt.cy

    return (
        offset_x + (xfrm.off.x - xfrm.chOff.x * group_scale_x) * scale_x,
        offset_y + (xfrm.off.y - xfrm.chOff.y * group_scale_y) * scale_y,
        scale_x * group_scale_x,
        scale_y * group_scale_y
    )


def find_title_shape(slide):
    """
    Find the shape that acts as the slide title.

    Uses the title placeholder if there is one. Otherwise falls back
    to the first text shape with the largest font size on the slide.
    """

    for shape in slide.shapes:
        if shape.is_placeholder and shape.placeholder_format.type in (
            PP_PLACEHOLDER.TITLE,
            PP_PLACEHOLDER.CENTER_TITLE
        ):
            return shape

    title_shape = None
    largest_size = 0

    for shape in slide.shapes:
        if not shape.has_text_frame or not shape.text_frame.text.strip():
            continue

        sizes = [
            run.font.size.pt
            for paragraph in shape.text_frame.paragraphs
            for run in paragraph.runs
            if run.font.size
        ]

        if sizes and max(sizes) > largest_size:
            largest_size = max(sizes)
            title_shape = shape

    return title_shape


def shape_metadata(shape, location="body"):

    metadata = {
        "source": "native",
        "location": location,
        "shape_id": shape.shape_id,
        "shape_name": shape.name
    }

    if shape.is_placeholder:
        metadata["placeholder"] = str(shape.placeholder_format.type)

    return metadata


def clean_text(text):
    # python-pptx gives a line break inside a paragraph as a vertical tab
    return text.replace("\v", "\n")


def is_bullet(paragraph, shape):
    """
    True if the paragraph shows a bullet or number: set on the paragraph
    itself, or inherited by a body placeholder from the slide master.
    """

    properties = paragraph._p.pPr

    if properties is not None:
        for child in properties:
            name = etree.QName(child).localname

            if name in ("buChar", "buAutoNum", "buBlip"):
                return True

            if name == "buNone":
                return False

    return shape.is_placeholder and shape.placeholder_format.type in BULLET_PLACEHOLDERS


def text_blocks(shape, base_id, block_type, transform, location):
    """
    One block per non-empty paragraph of a shape's text frame.
    """

    blocks = []

    for number, paragraph in enumerate(shape.text_frame.paragraphs, start=1):
        text = clean_text(paragraph.text).strip()

        if not text:
            continue

        bullet = block_type == "paragraph" and is_bullet(paragraph, shape)

        metadata = {**shape_metadata(shape, location), "paragraph": number}

        if bullet:
            metadata["level"] = paragraph.level

        blocks.append(DocumentBlock(
            block_id=f"{base_id}_p{number}",
            block_type="list_item" if bullet else block_type,
            text=text,
            bounding_box=get_bounding_box(shape, transform),
            metadata=metadata
        ))

    return blocks


def table_block(shape, table_id, transform):
    """
    A table block with one table_cell per cell. A merged cell is one
    cell at its top-left position, with the rows and columns it spans.
    """

    table = shape.table
    cells = []

    for row_index, row in enumerate(table.rows, start=1):
        for column_index, cell in enumerate(row.cells, start=1):

            if cell.is_spanned:
                continue

            cells.append(DocumentBlock(
                block_id=f"{table_id}_cell_{row_index}_{column_index}",
                block_type="table_cell",
                text=clean_text(cell.text),
                order=len(cells) + 1,
                metadata={
                    "source": "native",
                    "location": "body",
                    "row": row_index,
                    "column": column_index,
                    "row_span": cell.span_height,
                    "column_span": cell.span_width
                }
            ))

    return DocumentBlock(
        block_id=table_id,
        block_type="table",
        bounding_box=get_bounding_box(shape, transform),
        children=cells,
        metadata={
            **shape_metadata(shape),
            "rows": len(table.rows),
            "columns": len(table.columns)
        }
    )


def chart_block(shape, block_id, transform):
    """
    A chart's visible text: title, axis titles, series names and
    category labels. The values come from an embedded workbook that is
    not read.
    """

    chart = shape.chart
    lines = []

    if chart.has_title and chart.chart_title.has_text_frame:
        lines.append(chart.chart_title.text_frame.text)

    for axis_name in ("category_axis", "value_axis"):
        try:
            axis = getattr(chart, axis_name)
            if axis.has_title:
                lines.append(axis.axis_title.text_frame.text)
        except (ValueError, AttributeError):
            pass                            # pie charts have no axes

    for plot in chart.plots:
        lines.extend(str(series.name) for series in plot.series if series.name)
        lines.extend(str(category) for category in plot.categories if category is not None)

    text = "\n".join(dict.fromkeys(clean_text(line).strip() for line in lines if str(line).strip()))

    return DocumentBlock(
        block_id=block_id,
        block_type="chart",
        text=text,
        bounding_box=get_bounding_box(shape, transform),
        metadata={**shape_metadata(shape), "chart_type": str(chart.chart_type)}
    )


def diagram_block(shape, block_id, transform):
    """
    A SmartArt diagram's text, from its data part: one line per paragraph.
    """

    relationship_ids = shape._element.xpath(".//dgm:relIds/@r:dm")
    text = ""

    if relationship_ids:
        data = etree.fromstring(shape.part.related_part(relationship_ids[0]).blob)
        text = "\n".join(
            line
            for line in ("".join(PARAGRAPH_TEXT(paragraph)).strip() for paragraph in DIAGRAM_PARAGRAPHS(data))
            if line
        )

    return DocumentBlock(
        block_id=block_id,
        block_type="diagram",
        text=text,
        bounding_box=get_bounding_box(shape, transform),
        metadata=shape_metadata(shape)
    )


def picture_block(shape, block_id, transform, reader):
    """
    An image_ocr block with the picture's OCR text (word boxes in image
    pixels; bounding_box is the picture on the slide), or an image
    block if it cannot be read.
    """

    metadata = {**shape_metadata(shape), "image_part": None}
    image_bytes = b""

    try:
        part = shape.part.related_part(shape._element.blip_rId)
        metadata["image_part"] = str(part.partname)
        image_bytes = part.blob
    except (KeyError, AttributeError, ValueError):
        pass                                # a linked picture: not in the file

    del metadata["source"]

    return reader.block(
        block_id, metadata["image_part"] or block_id, image_bytes,
        bounding_box=get_bounding_box(shape, transform),
        metadata=metadata
    )


def shape_blocks(shapes, slide_number, title_shape, reader, transform=SLIDE_TRANSFORM, prefix=""):
    """
    Blocks for a slide's shapes, including shapes inside groups.
    Block IDs number the shapes in order; a shape inside a group is
    numbered within it ("3_2").
    """

    blocks = []

    for index, shape in enumerate(shapes, start=1):
        path = f"{prefix}{index}"
        uri = shape._element.xpath("./a:graphic/a:graphicData/@uri")

        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            blocks.extend(shape_blocks(
                shape.shapes, slide_number, title_shape, reader,
                group_transform(shape, transform), f"{path}_"
            ))

        elif shape.has_text_frame and shape.text_frame.text.strip():
            blocks.extend(text_blocks(
                shape, f"slide{slide_number}_text{path}",
                "heading" if shape == title_shape else "paragraph",
                transform,
                "text_box" if shape.shape_type == MSO_SHAPE_TYPE.TEXT_BOX else "body"
            ))

        elif getattr(shape, "has_table", False) and shape.has_table:
            blocks.append(table_block(shape, f"slide{slide_number}_table{path}", transform))

        elif getattr(shape, "has_chart", False) and shape.has_chart:
            blocks.append(chart_block(shape, f"slide{slide_number}_chart{path}", transform))

        elif uri and uri[0] == DIAGRAM_URI:
            blocks.append(diagram_block(shape, f"slide{slide_number}_diagram{path}", transform))

        elif shape.shape_type in (MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT, MSO_SHAPE_TYPE.LINKED_OLE_OBJECT):
            blocks.append(DocumentBlock(
                block_id=f"slide{slide_number}_object{path}",
                block_type="embedded_object",
                bounding_box=get_bounding_box(shape, transform),
                metadata={**shape_metadata(shape), "prog_id": shape.ole_format.prog_id}
            ))

        elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE or (
            shape.is_placeholder and hasattr(shape, "image")
        ):
            blocks.append(picture_block(shape, f"slide{slide_number}_image{path}", transform, reader))

    return blocks


def notes_blocks(slide, slide_number):
    """
    The slide's speaker notes, one block per paragraph.
    """

    if not slide.has_notes_slide or slide.notes_slide.notes_text_frame is None:
        return []

    blocks = []

    for number, paragraph in enumerate(slide.notes_slide.notes_text_frame.paragraphs, start=1):
        text = clean_text(paragraph.text).strip()

        if text:
            blocks.append(DocumentBlock(
                block_id=f"slide{slide_number}_notes_p{number}",
                block_type="paragraph",
                text=text,
                metadata={"source": "native", "location": "notes", "paragraph": number}
            ))

    return blocks


def extract_pptx(file_path: str) -> ExtractedDocument:

    file_path = Path(file_path)

    presentation = Presentation(file_path)
    reader = ImageReader()
    pages = []

    for slide_number, slide in enumerate(presentation.slides, start=1):

        blocks = shape_blocks(slide.shapes, slide_number, find_title_shape(slide), reader)

        # Reading order: top to bottom, then left to right. Paragraphs of
        # one shape share its position and keep their order.
        blocks.sort(key=lambda block: (
            (round(block.bounding_box.y), block.bounding_box.x)
            if block.bounding_box else (float("inf"), 0)
        ))

        blocks.extend(notes_blocks(slide, slide_number))

        for order, block in enumerate(blocks, start=1):
            block.order = order

        pages.append(
            DocumentPage(
                page_number=slide_number,
                blocks=blocks,
                metadata={
                    "content_type": "slide",
                    "extraction_method": text_method(blocks),
                    "width": Emu(presentation.slide_width).pt,
                    "height": Emu(presentation.slide_height).pt,
                    "layout": slide.slide_layout.name,
                    "hidden": slide._element.get("show") == "0",
                    **page_text_metadata(blocks)
                }
            )
        )

    return ExtractedDocument(
        document_id=file_path.stem,
        file_name=file_path.name,
        source_type="PPTX",
        extraction_method=combined_method(
            page.metadata["extraction_method"] for page in pages
        ) if pages else "native",
        pages=pages,
        metadata={
            "page_count": len(pages),
            "properties": office_properties(presentation.core_properties),
            "limitations": []
        }
    )
