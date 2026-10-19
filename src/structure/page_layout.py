"""
Page layout of an extracted PDF: running headers, footers and page
numbers, and the reading order of a page set in two columns.

Nothing here changes text, block IDs or positions. Blocks are only
labelled (metadata.location "header" or "footer") and put in reading
order, so a paragraph keeps the paragraphs before and after it: the
page furniture of one page does not stand between the last paragraph
of that page and the first of the next, and the left column is read
before the right one.
"""

import re
from difflib import SequenceMatcher


TEXT_TYPES = ("paragraph", "heading")

# A running header or footer sits in this share of the page height at
# the top or the bottom of the page
HEADER_BAND = 0.08
FOOTER_BAND = 0.08

# Text in a band is a running header or footer if it is a page number,
# or if the same text (numbers aside) is in the same band of another page.
# OCR may read a repeated line a little differently each time.
SAME_TEXT = 0.85
PAGE_NUMBER = re.compile(
    r"^(?:page\s*)?[-–—(]?\s*\d{1,4}\s*(?:(?:of|/)\s*\d{1,4})?\s*[-–—)]?$", re.IGNORECASE
)

# Two columns: a vertical gap at least MIN_GUTTER points wide, within
# CENTRE_ZONE of the page width, with at least MIN_COLUMN_BLOCKS
# multi-line text blocks on each side of it, side by side. Narrow
# single-line blocks (a label beside its value) do not make columns.
MIN_GUTTER = 12
CENTRE_ZONE = (0.3, 0.7)
MIN_COLUMN_BLOCKS = 2
MAX_COLUMN_SHARE = 0.5          # of the page width: wider blocks span both columns


def band_of(block, page_height):
    """
    "header" or "footer" if the block lies wholly in the top or bottom
    band of the page, else None.
    """

    box = block.bounding_box

    if box is None or not page_height:
        return None

    if box.y + box.height <= HEADER_BAND * page_height:
        return "header"

    if box.y >= (1 - FOOTER_BAND) * page_height:
        return "footer"

    return None


def without_numbers(text):
    return re.sub(r"\d+", "#", " ".join(text.lower().split()))


def mark_running_text(pages):
    """
    Label the running headers and footers of a document's pages, and put
    each page's headers first and footers last.
    """

    candidates = []     # (page, block, band, text without numbers)

    for page in pages:
        for block in page.blocks:
            band = band_of(block, page.metadata.get("height"))

            if band and block.block_type in TEXT_TYPES and block.text.strip():
                candidates.append((page, block, band, without_numbers(block.text)))

    for page, block, band, text in candidates:
        repeated = any(
            other_page is not page and other_band == band
            and SequenceMatcher(None, text, other_text).ratio() >= SAME_TEXT
            for other_page, _, other_band, other_text in candidates
        )

        if repeated or PAGE_NUMBER.match(" ".join(block.text.split())):
            block.metadata["location"] = band

    for page in pages:
        location = [block.metadata.get("location") for block in page.blocks]
        page.blocks = (
            [block for block, place in zip(page.blocks, location) if place == "header"]
            + [block for block, place in zip(page.blocks, location) if place not in ("header", "footer")]
            + [block for block, place in zip(page.blocks, location) if place == "footer"]
        )

        for order, block in enumerate(page.blocks, start=1):
            block.order = order


def top_to_bottom(block):
    return (round(block.bounding_box.y), block.bounding_box.x)


def find_gutter(blocks, page_width):
    """
    The (left, right) x-range of the gap between two columns of text on
    a page, or None if the page is not set in two columns.
    """

    narrow = [block for block in blocks if block.bounding_box.width < MAX_COLUMN_SHARE * page_width]
    free = [
        x for x in range(int(CENTRE_ZONE[0] * page_width), int(CENTRE_ZONE[1] * page_width) + 1)
        if not any(
            block.bounding_box.x < x < block.bounding_box.x + block.bounding_box.width
            for block in narrow
        )
    ]

    # The widest run of free positions
    runs = []

    for x in free:
        if runs and x == runs[-1][1] + 1:
            runs[-1][1] = x
        else:
            runs.append([x, x])

    if not runs:
        return None

    start, end = max(runs, key=lambda run: run[1] - run[0])

    if end - start < MIN_GUTTER:
        return None

    def column_text(block):
        return block.block_type in TEXT_TYPES and block.metadata.get("line_count", 1) >= 2

    left = [block for block in narrow if column_text(block) and block.bounding_box.x + block.bounding_box.width <= start]
    right = [block for block in narrow if column_text(block) and block.bounding_box.x >= end]

    if len(left) < MIN_COLUMN_BLOCKS or len(right) < MIN_COLUMN_BLOCKS:
        return None

    # Side by side, not one above the other
    def span(column):
        return (
            min(block.bounding_box.y for block in column),
            max(block.bounding_box.y + block.bounding_box.height for block in column)
        )

    (left_top, left_bottom), (right_top, right_bottom) = span(left), span(right)

    if left_top >= right_bottom or right_top >= left_bottom:
        return None

    return start, end


def reading_order(blocks, page_width):
    """
    Blocks of a page with a text layer in reading order: top to bottom,
    then left to right. On a page in two columns, the left column is read
    before the right one; a block across both columns (a title, a wide
    table) ends the columns above it.
    """

    blocks = sorted(blocks, key=top_to_bottom)
    gutter = find_gutter(blocks, page_width) if blocks else None

    if gutter is None:
        return blocks

    start, end = gutter
    ordered, left, right = [], [], []

    for block in blocks:
        box = block.bounding_box

        if box.x + box.width <= start:
            left.append(block)
        elif box.x >= end:
            right.append(block)
        else:
            ordered.extend(left + right)
            left, right = [], []
            ordered.append(block)

    return ordered + left + right
