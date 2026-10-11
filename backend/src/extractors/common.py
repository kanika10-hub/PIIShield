"""
Helpers shared by the PDF, PPTX and DOCX extractors, so all three fill
in the common format (see src/models/document.py) the same way.
"""

from datetime import datetime


# A page with fewer extracted characters than this has little or no text
LOW_TEXT_CHARS = 50


def all_blocks(blocks):
    for block in blocks:
        yield block
        yield from all_blocks(block.children)


def page_text_metadata(blocks):
    """
    char_count and low_text for a page's metadata. Word blocks repeat
    their paragraph's or cell's text, so they are not counted again.
    """

    chars = sum(
        len(block.text.strip())
        for block in all_blocks(blocks)
        if block.block_type != "word"
    )

    return {"char_count": chars, "low_text": chars < LOW_TEXT_CHARS}


def text_method(blocks, default="native"):
    """
    "native", "ocr" or "mixed": how the text of these blocks was read,
    from each block's metadata.source. default if there is no text.
    """

    sources = {
        block.metadata["source"]
        for block in all_blocks(blocks)
        if block.text.strip() and "source" in block.metadata
    }

    return combined_method(sources) if sources else default


def combined_method(methods):
    methods = set(methods)
    return methods.pop() if len(methods) == 1 else "mixed"


def clean_properties(values):
    """
    File metadata under the common names, without empty values.
    Dates become ISO 8601 strings.
    """

    properties = {}

    for name, value in values.items():
        if isinstance(value, datetime):
            value = value.isoformat()

        if value is not None and str(value).strip():
            properties[name] = str(value).strip()

    return properties


def office_properties(core_properties):
    """
    The file metadata of a DOCX or PPTX (python-docx / python-pptx
    core_properties).
    """

    return clean_properties({
        name: getattr(core_properties, name, None)
        for name in (
            "title", "subject", "author", "keywords", "comments",
            "category", "last_modified_by", "created", "modified"
        )
    })
