import pymupdf
import pytesseract
import numpy as np
from PIL import Image, ImageOps
from pytesseract import Output
import io
from pathlib import Path

from src.extractors.common import clean_properties, combined_method, page_text_metadata, text_method
from src.models.document import (
    ExtractedDocument,
    DocumentPage,
    DocumentBlock,
    BoundingBox
)
from src.ocr.image_ocr import ImageReader
from src.structure.page_layout import mark_running_text, reading_order

# PyMuPDF prints a hint about an optional layout package on first use
pymupdf.no_recommend_layout()


# Pages are rendered at this zoom for OCR. At 4x the rendered page has more
# pixels than the original scan, which noticeably improves small text such as
# email addresses. OCR positions are divided by it so bounding boxes are stored
# in PDF points, like the PPTX extractor.
ZOOM = 4

# A page with less text-layer text than this is a scanned page: it is OCR'd
NATIVE_TEXT_MIN_CHARS = 50

# On a page with a text layer, a block whose words are this much taller
# than the page's typical word is a heading
HEADING_SIZE_RATIO = 1.25
MAX_HEADING_WORDS = 20

# Embedded images smaller than this (points) are icons or rules: not OCR'd.
# An image covering most of the page is the scan behind a searchable PDF's
# text layer, whose text is already in that layer.
MIN_IMAGE_SIZE = 30
PAGE_SCAN_SHARE = 0.8


def invert_dark_bands(image):
    """
    Turn white-on-dark bars (e.g. table header rows) into dark-on-white.

    Tesseract often drops the first letter of inverted text
    ("CONTACT" -> "ONTACT"), so these bars are inverted before OCR.
    """

    pixels = np.asarray(ImageOps.grayscale(image)).copy()

    dark = pixels < 100

    # A row belongs to a dark bar when most of it is dark
    dark_rows = dark.mean(axis=1) > 0.4

    y = 0

    while y < len(dark_rows):

        if not dark_rows[y]:
            y += 1
            continue

        top = y

        while y < len(dark_rows) and dark_rows[y]:
            y += 1

        columns = np.where(dark[top:y].mean(axis=0) > 0.5)[0]

        if len(columns):
            left = columns.min()
            right = columns.max() + 1
            pixels[top:y, left:right] = 255 - pixels[top:y, left:right]

    return Image.fromarray(pixels)


def to_points(left, top, width, height):
    """
    Convert a box in rendered-image pixels to PDF points.
    """

    return BoundingBox(
        x=left / ZOOM,
        y=top / ZOOM,
        width=width / ZOOM,
        height=height / ZOOM
    )


def build_paragraph(data, indexes, page_number, order):
    """
    Build one paragraph block from the OCR words at the given indexes.

    The paragraph keeps each word as a child block, so every word's
    position and confidence is still available for redaction.
    """

    block_id = f"p{page_number}_b{order}"

    words = []
    lines = []
    current_line = None

    for word_order, i in enumerate(indexes, start=1):

        line_key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])

        if line_key != current_line:
            lines.append([])
            current_line = line_key

        text = data["text"][i].strip()

        lines[-1].append(text)

        words.append(
            DocumentBlock(
                block_id=f"{block_id}_w{word_order}",
                block_type="word",
                text=text,
                order=word_order,
                bounding_box=to_points(
                    data["left"][i],
                    data["top"][i],
                    data["width"][i],
                    data["height"][i]
                ),
                metadata={
                    "ocr_confidence": data["conf"][i],
                    "line": len(lines)
                }
            )
        )

    left = min(data["left"][i] for i in indexes)
    top = min(data["top"][i] for i in indexes)
    right = max(data["left"][i] + data["width"][i] for i in indexes)
    bottom = max(data["top"][i] + data["height"][i] for i in indexes)

    confidences = [data["conf"][i] for i in indexes]

    return DocumentBlock(
        block_id=block_id,
        block_type="paragraph",
        text="\n".join(" ".join(line) for line in lines),
        order=order,
        bounding_box=to_points(left, top, right - left, bottom - top),
        children=words,
        metadata={
            "source": "ocr",
            "location": "body",
            "ocr_confidence": round(sum(confidences) / len(confidences), 1),
            "line_count": len(lines),
            "word_count": len(words)
        }
    )


# --------------------------------------------------
# SCANNED PAGES (OCR)
# --------------------------------------------------

def ocr_page_blocks(page, page_number):
    """
    Paragraph blocks of a scanned page, by OCR of the rendered page.
    Its tables are rebuilt afterwards (src/structure/pdf_tables.py).
    """

    pix = page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM))
    image = invert_dark_bands(Image.open(io.BytesIO(pix.tobytes("png"))))

    data = pytesseract.image_to_data(image, output_type=Output.DICT)

    # Group words into paragraphs using Tesseract's own layout analysis
    paragraphs = {}

    for i in range(len(data["text"])):

        if not data["text"][i].strip():
            continue

        paragraphs.setdefault((data["block_num"][i], data["par_num"][i]), []).append(i)

    return [
        build_paragraph(data, indexes, page_number, order)
        for order, indexes in enumerate(paragraphs.values(), start=1)
    ]


# --------------------------------------------------
# PAGES WITH A TEXT LAYER
# --------------------------------------------------

def box_of(x0, y0, x1, y1):
    return BoundingBox(x=x0, y=y0, width=x1 - x0, height=y1 - y0)


def word_blocks(words, parent_id):
    """
    Word blocks for text-layer words (PyMuPDF word tuples), and the
    text they make: one line per text line, in reading order.
    """

    lines = {}
    children = []

    for word in words:
        line = lines.setdefault((word[5], word[6]), len(lines) + 1)

        children.append(DocumentBlock(
            block_id=f"{parent_id}_w{len(children) + 1}",
            block_type="word",
            text=word[4],
            order=len(children) + 1,
            bounding_box=box_of(*word[:4]),
            metadata={"line": line}
        ))

    text = "\n".join(
        " ".join(child.text for child in children if child.metadata["line"] == line)
        for line in range(1, len(lines) + 1)
    )

    return children, text


def centre_inside(word, box):
    x = (word[0] + word[2]) / 2
    y = (word[1] + word[3]) / 2

    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def native_table(table, table_id, words):
    """
    A table block from a PyMuPDF table, and the indexes of the words it
    holds. A merged cell is one cell at its top-left grid position,
    with the number of rows and columns it covers.
    """

    # Grid lines from the narrowest cell in each column, and each row
    column_centres = {}

    for row in table.rows:
        for column, box in enumerate(row.cells):
            if box is not None:
                width = box[2] - box[0]
                if column not in column_centres or width < column_centres[column][1]:
                    column_centres[column] = ((box[0] + box[2]) / 2, width)

    row_centres = [(row.bbox[1] + row.bbox[3]) / 2 for row in table.rows]

    cells = []
    used = set()

    for row_index, row in enumerate(table.rows):
        for column_index, box in enumerate(row.cells):

            if box is None:
                continue

            inside = [index for index, word in enumerate(words) if centre_inside(word, box)]
            used.update(inside)

            cell_id = f"{table_id}_r{row_index + 1}_c{column_index + 1}"
            children, text = word_blocks([words[index] for index in inside], cell_id)

            cells.append(DocumentBlock(
                block_id=cell_id,
                block_type="table_cell",
                text=text,
                order=len(cells) + 1,
                bounding_box=box_of(*box),
                children=children,
                metadata={
                    "source": "native",
                    "location": "body",
                    "row": row_index + 1,
                    "column": column_index + 1,
                    "row_span": max(1, sum(box[1] <= y <= box[3] for y in row_centres)),
                    "column_span": max(1, sum(
                        box[0] <= x <= box[2] for x, _ in column_centres.values()
                    ))
                }
            ))

    block = DocumentBlock(
        block_id=table_id,
        block_type="table",
        bounding_box=box_of(*table.bbox),
        children=cells,
        metadata={
            "source": "native",
            "location": "body",
            "rows": table.row_count,
            "columns": table.col_count
        }
    )

    return block, used


def native_page_blocks(pdf, page, page_number, reader):
    """
    Blocks of a page with a text layer: tables found by PyMuPDF,
    paragraphs and headings from the text layer, and the OCR'd text of
    embedded images. Words are kept as child blocks with their
    positions, like on OCR'd pages.
    """

    words = page.get_text("words", sort=True)
    blocks = []
    used = set()

    for number, table in enumerate(page.find_tables().tables, start=1):
        block, table_words = native_table(table, f"p{page_number}_t{number}", words)
        blocks.append(block)
        used.update(table_words)

    # Paragraphs: the text layer's own blocks, in reading order
    paragraphs = {}

    for index, word in enumerate(words):
        if index not in used:
            paragraphs.setdefault(word[5], []).append(word)

    heights = sorted(word[3] - word[1] for word in words) or [0]
    typical_height = heights[len(heights) // 2]

    for number, paragraph_words in enumerate(paragraphs.values(), start=1):
        block_id = f"p{page_number}_b{number}"
        children, text = word_blocks(paragraph_words, block_id)

        block_heights = sorted(word[3] - word[1] for word in paragraph_words)
        heading = (
            block_heights[len(block_heights) // 2] >= HEADING_SIZE_RATIO * typical_height
            and len(paragraph_words) <= MAX_HEADING_WORDS
        )

        blocks.append(DocumentBlock(
            block_id=block_id,
            block_type="heading" if heading else "paragraph",
            text=text,
            bounding_box=box_of(
                min(word[0] for word in paragraph_words),
                min(word[1] for word in paragraph_words),
                max(word[2] for word in paragraph_words),
                max(word[3] for word in paragraph_words)
            ),
            children=children,
            metadata={
                "source": "native",
                "location": "body",
                "line_count": len({child.metadata["line"] for child in children}),
                "word_count": len(children)
            }
        ))

    # Text inside embedded images
    page_area = page.rect.width * page.rect.height
    image_number = 0

    for xref, *_ in page.get_images(full=True):
        for rect in page.get_image_rects(xref):

            if (
                rect.width < MIN_IMAGE_SIZE or rect.height < MIN_IMAGE_SIZE
                or rect.width * rect.height >= PAGE_SCAN_SHARE * page_area
            ):
                continue

            image_number += 1

            try:
                image_bytes = pdf.extract_image(xref)["image"]
            except (RuntimeError, ValueError, KeyError, TypeError):
                image_bytes = b""

            blocks.append(reader.block(
                f"p{page_number}_i{image_number}", xref, image_bytes,
                bounding_box=box_of(rect.x0, rect.y0, rect.x1, rect.y1),
                metadata={"location": "body", "image_xref": xref}
            ))

    # Top to bottom, then left to right; the left column first on a page
    # set in two columns
    return reading_order(blocks, page.rect.width)


# --------------------------------------------------
# DOCUMENT
# --------------------------------------------------

def pdf_properties(pdf):

    metadata = pdf.metadata or {}

    def date(value):
        # PDF dates look like D:20240115093000+05'30'
        return value[2:16] if value and value.startswith("D:") else value

    return clean_properties({
        "title": metadata.get("title"),
        "subject": metadata.get("subject"),
        "author": metadata.get("author"),
        "keywords": metadata.get("keywords"),
        "creator": metadata.get("creator"),
        "producer": metadata.get("producer"),
        "created": date(metadata.get("creationDate")),
        "modified": date(metadata.get("modDate"))
    })


def extract_pdf(file_path, ocr_pages=()):
    """
    Extract a PDF into the common format. Each page is read from its
    text layer when it has one, and OCR'd when it is a scanned page.
    Running headers and footers are labelled (src/structure/page_layout).
    Tables on scanned pages are rebuilt afterwards by
    src/structure/pdf_tables.reconstruct_tables.

    ocr_pages: page numbers to OCR even though they have a text layer.
    A redacted scanned page has one, made of its redaction placeholders;
    the original-format validator reads such pages again by OCR.
    """

    pdf = pymupdf.open(file_path)
    reader = ImageReader()
    pages = []

    for page_index, page in enumerate(pdf):

        page_number = page_index + 1
        native_chars = len("".join(page.get_text().split()))

        if native_chars >= NATIVE_TEXT_MIN_CHARS and page_number not in ocr_pages:
            blocks = native_page_blocks(pdf, page, page_number, reader)
            method = text_method(blocks)
        else:
            blocks = ocr_page_blocks(page, page_number)
            method = "ocr"

        for order, block in enumerate(blocks, start=1):
            block.order = order

        pages.append(DocumentPage(
            page_number=page_number,
            blocks=blocks,
            metadata={
                "content_type": "pdf_page",
                "extraction_method": method,
                "width": page.rect.width,
                "height": page.rect.height,
                "native_char_count": native_chars,
                **page_text_metadata(blocks)
            }
        ))

    # Running headers, footers and page numbers: labelled, and put at the
    # start and end of their page
    mark_running_text(pages)

    return ExtractedDocument(
        document_id=Path(file_path).stem,
        file_name=Path(file_path).name,
        source_type="PDF",
        extraction_method=combined_method(
            page.metadata["extraction_method"] for page in pages
        ) if pages else "native",
        pages=pages,
        metadata={
            "page_count": len(pages),
            "ocr_engine": "Tesseract",
            "properties": pdf_properties(pdf),
            "limitations": []
        }
    )
