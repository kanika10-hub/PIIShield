"""
The common JSON format produced by the PDF, PPTX and DOCX extractors.

Document
    document_id, file_name, source_type ("PDF", "PPTX", "DOCX")
    extraction_method   "native", "ocr" or "mixed": how the text was read
    metadata            page_count, properties (title, author, ...:
                        file metadata, which can name people) and
                        limitations (what the extractor could not read)
    pages

Page                    a PDF page, a PPTX slide, or the whole DOCX
    page_number
    metadata            content_type ("pdf_page", "slide", "docx_body"),
                        extraction_method, char_count, low_text (little
                        or no text extracted), and width / height in
                        points where the format has pages
    blocks              in reading order

Block
    block_id            unique in the document
    block_type          heading, paragraph, list_item, table, table_cell,
                        image_ocr, image, chart, diagram, embedded_object,
                        word (OCR or PDF word inside a paragraph or cell)
    text, order, children
    bounding_box        x, y, width, height in points on the page, or
                        None where the format has no layout (DOCX)
    metadata            every block with text has "source" ("native" or
                        "ocr") and "location" ("body", "header",
                        "footer", "text_box", "notes", "footnote",
                        "endnote", "comment", "template": an image a
                        PPTX slide layout or master puts on a slide).
                        Also, by block type:
        table           rows, columns
        table_cell      row, column, row_span, column_span (1-based)
        list_item       level (0-based)
        image_ocr       ocr_engine, image_width, image_height,
                        ocr_confidence, word_count, words (text and box
                        in image pixels; the words of text, in order),
                        and image_part where the image is a file part
        image           an image that could not be read: requires_ocr,
                        ocr_error
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from pathlib import Path
import json


@dataclass
class BoundingBox:
    """
    Position of a piece of content within a page/slide.

    Coordinates are optional because not every extractor
    will provide them initially.
    """

    x: Optional[float] = None
    y: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None


@dataclass
class DocumentBlock:
    """
    A single logical piece of document content.

    Examples:
        heading
        paragraph
        table
        table_cell
        image
        list
    """

    block_id: str
    block_type: str
    text: str = ""

    order: int = 0

    bounding_box: Optional[BoundingBox] = None

    children: List["DocumentBlock"] = field(default_factory=list)

    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentPage:
    """
    Represents either a PDF page, PPTX slide,
    or logical page/section from another format.
    """

    page_number: int

    blocks: List[DocumentBlock] = field(default_factory=list)

    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedDocument:
    """
    Common representation used by ALL extractors.

    PPTX, PDF and DOCX extractors must eventually
    produce this same structure.
    """

    document_id: str

    file_name: str

    source_type: str

    extraction_method: str

    pages: List[DocumentPage] = field(default_factory=list)

    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        """
        Convert the document into a JSON-compatible dictionary.
        """
        return {
            "document_id": self.document_id,
            "file_name": self.file_name,
            "source_type": self.source_type,
            "extraction_method": self.extraction_method,
            "pages": [
                {
                    "page_number": page.page_number,
                    "metadata": page.metadata,
                    "blocks": [
                        block_to_dict(block)
                        for block in page.blocks
                    ]
                }
                for page in self.pages
            ],
            "metadata": self.metadata
        }

    @classmethod
    def load_json(cls, input_path: str):
        """
        Load a document saved with save_json(), so later steps
        (e.g. table reconstruction) can run without re-extracting.
        """

        with open(input_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return cls(
            document_id=data["document_id"],
            file_name=data["file_name"],
            source_type=data["source_type"],
            extraction_method=data["extraction_method"],
            pages=[
                DocumentPage(
                    page_number=page["page_number"],
                    blocks=[block_from_dict(block) for block in page["blocks"]],
                    metadata=page.get("metadata", {})
                )
                for page in data["pages"]
            ],
            metadata=data.get("metadata", {})
        )

    def save_json(self, output_path: str):
        """
        Save the extracted document as JSON.
        """

        output_path = Path(output_path)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(
                self.to_dict(),
                f,
                indent=2,
                ensure_ascii=False
            )


def block_from_dict(data):
    """
    Rebuild a DocumentBlock (and its children) from its dictionary form.
    """

    box = data.get("bounding_box")

    return DocumentBlock(
        block_id=data["block_id"],
        block_type=data["block_type"],
        text=data.get("text", ""),
        order=data.get("order", 0),
        bounding_box=BoundingBox(**box) if box else None,
        children=[block_from_dict(child) for child in data.get("children", [])],
        metadata=data.get("metadata", {})
    )


def block_to_dict(block: DocumentBlock):
    """
    Convert a DocumentBlock recursively into a dictionary.
    """

    result = {
        "block_id": block.block_id,
        "block_type": block.block_type,
        "text": block.text,
        "order": block.order,
        "metadata": block.metadata
    }

    if block.bounding_box:
        result["bounding_box"] = {
            "x": block.bounding_box.x,
            "y": block.bounding_box.y,
            "width": block.bounding_box.width,
            "height": block.bounding_box.height
        }
    else:
        result["bounding_box"] = None

    result["children"] = [
        block_to_dict(child)
        for child in block.children
    ]

    return result