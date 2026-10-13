"""
Rule-based heading classification for OCR paragraphs.

Runs after OCR extraction. It never changes any text: it only relabels
paragraph blocks as headings, records the evidence for that decision,
and splits off a heading line that OCR merged into the paragraph below it.
"""

import re

from src.models.document import DocumentBlock, BoundingBox


# "1. Purpose and objectives", "Appendix A — Impact and likelihood scales"
LEVEL_1 = re.compile(r"^(\d{1,2}\.\s+[A-Z]|Appendix [A-Z]\b)")

# "1.1 Objectives", "E.1 Subordinate policies and owners"
LEVEL_2 = re.compile(r"^(\d{1,2}\.\d{1,2}|[A-Z]\.\d{1,2})\s+[A-Z]")

MAX_HEADING_WORDS = 15

# A line ending before this share of the text column's right edge
# is a short standalone line, not a sentence that wraps onto the next line
SHORT_LINE_RATIO = 0.85

# An unnumbered title ("Incident Report"): a paragraph of one line, set
# this much taller than the document's typical OCR line, not ending a
# sentence. Text-layer pages use the same ratio (pdf_extractor).
LARGE_LINE_RATIO = 1.25


def split_lines(block):
    """
    Return the block's words grouped by OCR line, in order.
    """

    lines = {}

    for word in block.children:
        lines.setdefault(word.metadata["line"], []).append(word)

    return [lines[number] for number in sorted(lines)]


def line_height(words):
    """
    Height of the tallest word in a line, used as a font size estimate.
    """

    return max(word.bounding_box.height for word in words)


def line_right_edge(words):
    """
    Right edge of a line, in points.
    """

    return max(word.bounding_box.x + word.bounding_box.width for word in words)


def percentile_90(values):

    values = sorted(values)

    return values[int(len(values) * 0.9)]


def build_block(block_id, block_type, lines, source):
    """
    Build a block from lines of existing OCR words, keeping the words as-is.
    """

    words = [word for line in lines for word in line]

    for word_order, word in enumerate(words, start=1):
        word.order = word_order

    for line_number, line in enumerate(lines, start=1):
        for word in line:
            word.metadata["line"] = line_number

    left = min(w.bounding_box.x for w in words)
    top = min(w.bounding_box.y for w in words)
    right = max(w.bounding_box.x + w.bounding_box.width for w in words)
    bottom = max(w.bounding_box.y + w.bounding_box.height for w in words)

    # Text-layer words (native PDF pages) have no OCR confidence
    confidences = [w.metadata["ocr_confidence"] for w in words if "ocr_confidence" in w.metadata]

    metadata = {
        "source": source.metadata.get("source", "ocr"),
        "location": source.metadata.get("location", "body"),
        "line_count": len(lines),
        "word_count": len(words),
        "split_from": source.block_id
    }

    if confidences:
        metadata["ocr_confidence"] = round(sum(confidences) / len(confidences), 1)

    return DocumentBlock(
        block_id=block_id,
        block_type=block_type,
        text="\n".join(" ".join(w.text for w in line) for line in lines),
        order=source.order,
        bounding_box=BoundingBox(
            x=left,
            y=top,
            width=right - left,
            height=bottom - top
        ),
        children=words,
        metadata=metadata
    )


def classify_line(line, large_text, right_margin):
    """
    Return a heading classification for one OCR line, or None.
    """

    text = " ".join(word.text for word in line)

    if LEVEL_1.match(text):
        level = 1
    elif LEVEL_2.match(text):
        level = 2
    else:
        return None

    if len(line) > MAX_HEADING_WORDS or text.endswith("."):
        return None

    signals = ["numbering"]

    if line_height(line) >= large_text:
        signals.append("larger_text")

    if line_right_edge(line) < right_margin * SHORT_LINE_RATIO:
        signals.append("short_line")

    # Numbering alone is not enough: numbered list items look the same.
    # Main section titles are always set larger than body text.
    if len(signals) == 1 or (level == 1 and "larger_text" not in signals):
        return None

    return {
        "label": "heading",
        "level": level,
        "confidence": "high" if len(signals) == 3 else "medium",
        "signals": signals,
        "method": "rule_based"
    }


def classify_block(block, large_text, right_margin):
    """
    Return the block unchanged, relabelled as a heading, or split into
    parts when OCR merged a heading line into a paragraph.
    """

    if block.block_type != "paragraph" or not block.children:
        return [block]

    lines = split_lines(block)

    classifications = [
        classify_line(line, large_text, right_margin)
        for line in lines
    ]

    if not any(classifications):
        return [block]

    if len(lines) == 1:
        block.block_type = "heading"
        block.metadata["classification"] = classifications[0]
        return [block]

    # Split into runs: each heading line on its own, other lines grouped
    parts = []
    current = []

    for line, classification in zip(lines, classifications):

        if classification:
            if current:
                parts.append(("paragraph", current, None))
                current = []
            parts.append(("heading", [line], classification))
        else:
            current.append(line)

    if current:
        parts.append(("paragraph", current, None))

    blocks = []

    for part_number, (block_type, part_lines, classification) in enumerate(parts, start=1):

        block_id = block.block_id if part_number == 1 else f"{block.block_id}_{part_number}"

        part = build_block(block_id, block_type, part_lines, block)

        if classification:
            part.metadata["classification"] = classification

        blocks.append(part)

    return blocks


def classify_large_lines(document):
    """
    Mark unnumbered titles on OCR'd pages as headings: one-line OCR
    paragraphs set clearly larger than the document's typical OCR line
    (see LARGE_LINE_RATIO). Running headers and footers stay as they are.
    """

    paragraphs = [
        block
        for page in document.pages
        for block in page.blocks
        if block.block_type == "paragraph" and block.children
        and block.metadata.get("source") == "ocr"
    ]

    heights = sorted(line_height(line) for block in paragraphs for line in split_lines(block))

    if not heights:
        return

    typical = heights[len(heights) // 2]

    for block in paragraphs:
        lines = split_lines(block)
        text = block.text.strip()

        if (
            len(lines) == 1
            and len(block.children) <= MAX_HEADING_WORDS
            and not text.endswith((".", ":", ";", ","))
            and block.metadata.get("location", "body") == "body"
            and line_height(lines[0]) >= LARGE_LINE_RATIO * typical
        ):
            block.block_type = "heading"
            block.metadata["classification"] = {
                "label": "heading",
                "level": 1,
                "confidence": "medium",
                "signals": ["larger_text", "single_line"],
                "method": "rule_based"
            }


def classify_headings(document):
    """
    Mark section titles in an OCR-extracted document as headings:
    unnumbered titles set in larger text, and numbered section titles.
    """

    classify_large_lines(document)

    body_paragraphs = [
        block
        for page in document.pages
        for block in page.blocks
        if block.block_type == "paragraph" and block.metadata.get("line_count", 0) >= 3
    ]

    if not body_paragraphs:
        return document

    # Text taller than 90% of body lines counts as larger than body text
    large_text = percentile_90(
        line_height(line)
        for block in body_paragraphs
        for line in split_lines(block)
    )

    # Wrapped lines (every line but the last) reach the column's right edge
    right_margin = percentile_90(
        line_right_edge(line)
        for block in body_paragraphs
        for line in split_lines(block)[:-1]
    )

    for page in document.pages:

        blocks = []

        for block in page.blocks:
            blocks.extend(classify_block(block, large_text, right_margin))

        for order, block in enumerate(blocks, start=1):
            block.order = order

        page.blocks = blocks

    document.metadata["heading_classification"] = {
        "large_text_threshold": large_text,
        "right_margin": right_margin
    }

    return document
