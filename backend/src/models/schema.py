"""
Check that an extracted document follows the common JSON format
(src/models/document.py), before anything relies on it.

    format_problems(document)   every way the document breaks the
                                format; empty if it follows it

main.py runs it on every extracted document before the PII scan: the
scan, redaction and validation all depend on this structure, so a
document that breaks it is stopped there.
"""

TOP_KEYS = ["document_id", "file_name", "source_type", "extraction_method", "pages", "metadata"]
BLOCK_KEYS = ["block_id", "block_type", "text", "order", "metadata", "bounding_box", "children"]
BLOCK_TYPES = {
    "heading", "paragraph", "list_item", "table", "table_cell", "image_ocr",
    "image", "chart", "diagram", "embedded_object", "word"
}
LOCATIONS = {"body", "header", "footer", "text_box", "notes", "footnote", "endnote", "comment", "template"}
METHODS = {"native", "ocr", "mixed"}
CONTENT_TYPES = {"PDF": "pdf_page", "PPTX": "slide", "DOCX": "docx_body"}
IMAGE_KEYS = {"ocr_engine", "image_width", "image_height", "ocr_confidence", "word_count", "words"}


def all_blocks(blocks):
    for block in blocks:
        yield block
        yield from all_blocks(block.get("children", []))


def check_box(box):
    return box is None or all(
        isinstance(box.get(key), (int, float)) and box[key] >= 0
        for key in ("x", "y", "width", "height")
    )


def format_problems(document):
    """
    Every way the document (a dictionary) breaks the common format.
    """

    problems = []

    def problem(message):
        if message not in problems:
            problems.append(message)

    if not isinstance(document, dict):
        return ["the document is not a JSON object"]

    if list(document) != TOP_KEYS:
        problem(f"top-level keys {list(document)}")

    source_type = document.get("source_type")

    if source_type not in CONTENT_TYPES:
        problem(f"source_type {source_type!r}")

    if document.get("extraction_method") not in METHODS:
        problem(f"extraction_method {document.get('extraction_method')!r}")

    metadata = document.get("metadata", {})

    if metadata.get("page_count") != len(document.get("pages", [])):
        problem("metadata.page_count does not match the pages")

    if not isinstance(metadata.get("properties"), dict) or not isinstance(metadata.get("limitations"), list):
        problem("metadata needs properties (dict) and limitations (list)")

    ids = []

    for page in document.get("pages", []):
        page_metadata = page.get("metadata", {})
        page_number = page.get("page_number")

        if page_metadata.get("content_type") != CONTENT_TYPES.get(source_type):
            problem(f"page {page_number}: content_type {page_metadata.get('content_type')!r}")

        if page_metadata.get("extraction_method") not in METHODS:
            problem(f"page {page_number}: extraction_method {page_metadata.get('extraction_method')!r}")

        if not isinstance(page_metadata.get("char_count"), int) or not isinstance(page_metadata.get("low_text"), bool):
            problem(f"page {page_number}: needs char_count and low_text")

        if source_type in ("PDF", "PPTX") and not (page_metadata.get("width") and page_metadata.get("height")):
            problem(f"page {page_number}: needs width and height")

        if [block.get("order") for block in page.get("blocks", [])] != list(range(1, len(page.get("blocks", [])) + 1)):
            problem(f"page {page_number}: block order is not 1..n")

        for block in all_blocks(page.get("blocks", [])):
            block_id = block.get("block_id")
            block_type = block.get("block_type")
            meta = block.get("metadata", {})
            ids.append(block_id)

            if list(block) != BLOCK_KEYS:
                problem(f"{block_id}: keys {list(block)}")

            if block_type not in BLOCK_TYPES:
                problem(f"{block_id}: block_type {block_type!r}")

            if not check_box(block.get("bounding_box")):
                problem(f"{block_id}: bad bounding_box")

            if block_type != "word" and (block.get("text") or "").strip():
                if meta.get("source") not in ("native", "ocr"):
                    problem(f"{block_type} blocks need source native/ocr, e.g. {block_id}")
                if meta.get("location") not in LOCATIONS:
                    problem(f"{block_type} blocks need a location, e.g. {block_id}")

            if block_type == "table" and not (meta.get("rows") and meta.get("columns")):
                problem(f"{block_id}: table needs rows and columns")

            if block_type == "table_cell" and not all(
                isinstance(meta.get(key), int) and meta[key] >= 1
                for key in ("row", "column", "row_span", "column_span")
            ):
                problem(f"{block_id}: cell needs row, column, row_span, column_span")

            if block_type == "list_item" and not isinstance(meta.get("level"), int):
                problem(f"{block_id}: list_item needs level")

            if block_type == "image_ocr":
                if not IMAGE_KEYS <= set(meta):
                    problem(f"{block_id}: image_ocr needs {sorted(IMAGE_KEYS - set(meta))}")
                elif block["text"].split() != [word["text"] for word in meta["words"]]:
                    problem(f"{block_id}: OCR words do not match the text")

            if block_type == "image" and not meta.get("requires_ocr"):
                problem(f"{block_id}: image needs requires_ocr")

    seen = set()
    duplicates = {block_id for block_id in ids if block_id in seen or seen.add(block_id)}

    if duplicates:
        problem(f"duplicate block_ids: {sorted(map(str, duplicates))[:5]}")

    return problems
