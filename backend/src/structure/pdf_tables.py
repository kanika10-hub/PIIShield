"""
Rebuild tables on OCR'd PDF pages.

OCR returns words, not tables: Tesseract groups table text into
paragraphs that run across columns ("+1 (212) | GRP-POL-002 ..."). This
step finds each table in the page image and moves its OCR words into
table / table_cell blocks, the same format the DOCX and PPTX extractors
produce. Word blocks keep their original IDs and positions.

Signals used (the table style of the case-study PDF):
- a solid dark header band across the table: one header row
- thin horizontal rules between rows
- one column per header label, split at the white gap between columns
  (works for left-aligned and centred columns)
"""

import numpy as np
import pymupdf

from src.models.document import DocumentBlock, BoundingBox


RENDER_SCALE = 2            # 144 dpi shows the bands and the 0.5pt rules

DARK = 100                  # header band pixels (navy is ~39)
LINE_GREY = (120, 244)      # any thin line; row shading is 247, paper 255
RULE_MIN_GREY = 200         # row rules are ~220-240; gold underlines ~170
MIN_BAND_WIDTH = 0.6        # share of the page width
MIN_BAND_HEIGHT = 8         # points
MAX_BAND_HEIGHT = 40
MIN_RULE_COVERAGE = 0.9     # share of the table width a rule must cover
MAX_ROW_HEIGHT = 120        # points

HEADER_WORD_GAP = 4         # words of one header label are ~2.5pt apart
ALIGN_TOLERANCE = 2         # points
LINE_GAP = 5                # between lines of one row; the page header sits further up
HEADER_ZONE = 0.06          # running page header above this share of the page height
FOOTER_ZONE = 0.9           # blocks below this share of the page height are the footer

TEXT_BLOCK_TYPES = ("paragraph", "heading")


# --------------------------------------------------
# PAGE IMAGE
# --------------------------------------------------

def render_grey(page):

    pixmap = page.get_pixmap(
        matrix=pymupdf.Matrix(RENDER_SCALE, RENDER_SCALE),
        colorspace=pymupdf.csGRAY
    )

    return np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width
    )


def runs(mask):
    """
    (start, end) of each run of True values.
    """

    padded = np.concatenate(([False], mask, [False]))
    changes = np.flatnonzero(padded[1:] != padded[:-1])

    return list(zip(changes[::2], changes[1::2]))


def find_header_bands(grey):
    """
    Solid dark bars across most of the page: table header rows.
    Returns (top, bottom, left, right) in points.
    """

    dark = grey < DARK
    width = grey.shape[1]
    bands = []

    for top, bottom in runs(dark.mean(axis=1) > 0.3):
        columns = np.flatnonzero(dark[top:bottom].mean(axis=0) > 0.5)

        if not len(columns):
            continue

        left, right = columns.min(), columns.max() + 1
        height = (bottom - top) / RENDER_SCALE
        solid = (dark[top:bottom, left:right].mean(axis=0) > 0.5).mean()

        if (
            (right - left) / width >= MIN_BAND_WIDTH
            and MIN_BAND_HEIGHT <= height <= MAX_BAND_HEIGHT
            and solid >= 0.95
        ):
            bands.append(tuple(value / RENDER_SCALE for value in (top, bottom, left, right)))

    return bands


def find_rules(grey, left, right):
    """
    y positions (points) of thin light-grey lines spanning the table
    width. Darker lines (gold section underlines, box borders) are not
    row rules: a heading under a table would otherwise become a row.
    """

    span = grey[:, int(left * RENDER_SCALE):int(right * RENDER_SCALE)]
    low, high = LINE_GREY
    covered = ((span >= low) & (span <= high)).mean(axis=1) >= MIN_RULE_COVERAGE

    return [
        (top + bottom) / 2 / RENDER_SCALE
        for top, bottom in runs(covered)
        if bottom - top <= 3 * RENDER_SCALE                       # a line, not a shaded area
        and np.median(span[top:bottom], axis=1).min() >= RULE_MIN_GREY
    ]


# --------------------------------------------------
# WORD GEOMETRY
# --------------------------------------------------

def x0(word):
    return word.bounding_box.x


def x1(word):
    return word.bounding_box.x + word.bounding_box.width


def y_centre(word):
    return word.bounding_box.y + word.bounding_box.height / 2


def words_between(words, top, bottom, left, right):

    return [
        word for word in words
        if top <= y_centre(word) < bottom
        and left - ALIGN_TOLERANCE <= x0(word) < right
    ]


def has_text(word):
    """
    False for OCR noise such as "|" or "—" at a band edge.
    """

    return any(character.isalnum() for character in word.text)


def header_labels(header_words):
    """
    Group header words into labels, as [left, right] x-ranges.
    Words of one label are ~2.5pt apart; labels at least ~5pt.
    Multi-line labels overlap in x and join up.
    """

    labels = []

    for word in sorted((word for word in header_words if has_text(word)), key=x0):

        if labels and x0(word) - labels[-1][1] <= HEADER_WORD_GAP:
            labels[-1][1] = max(labels[-1][1], x1(word))
        else:
            labels.append([x0(word), x1(word)])

    return labels


def find_column_edges(header_words, body_rows, left, right):
    """
    Left edge of each column. Every column has a header label; between
    two neighbouring labels the edge goes in the white gap that no body
    row touches, which works for left-aligned and centred columns.
    Without such a gap, body text is left-aligned with its label, so
    the column starts at the label, unless body text runs across it,
    in which case both labels belong to one column (OCR noise in the
    header such as "ASSESSMENT DUE" read as "> aan").
    """

    step = 0.5
    size = int((right - left) / step) + 1

    def index(x):
        return min(size, max(0, int((x - left) / step)))

    crossings = np.zeros(size, dtype=int)

    for words in body_rows:
        covered = np.zeros(size, dtype=bool)

        for word in words:
            if has_text(word):
                covered[index(x0(word)):index(x1(word)) + 1] = True

        crossings += covered

    def edge_between(label, next_label):
        start, end = index(label[1]), index(next_label[0])

        gaps = runs(crossings[start:end] == 0) if end > start else []

        if gaps:
            gap_start, gap_end = max(gaps, key=lambda run: run[1] - run[0])
            return left + (start + (gap_start + gap_end) / 2) * step

        runs_across = any(
            has_text(word)
            and x0(word) < next_label[0] - ALIGN_TOLERANCE
            and x1(word) > next_label[0] + ALIGN_TOLERANCE
            for words in body_rows
            for word in words
        )

        return None if runs_across else next_label[0] - ALIGN_TOLERANCE

    labels = header_labels(header_words)
    edges = [left]

    if not labels:
        return edges

    current = labels[0]

    for label in labels[1:]:
        edge = edge_between(current, label)

        if edge is None:
            current = [current[0], max(current[1], label[1])]
        else:
            edges.append(edge)
            current = label

    return edges


def column_of(word, edges):

    centre = x0(word) + word.bounding_box.width / 2

    return sum(1 for edge in edges if centre >= edge) or 1


def crossed_edges(words, edges):
    """
    Column edges crossed by any of the words. Table text stays inside
    its column; a paragraph line running across the page does not.
    """

    return {
        edge
        for word in words
        for edge in edges[1:]
        if x0(word) < edge - ALIGN_TOLERANCE and x1(word) > edge + ALIGN_TOLERANCE
    }


def is_paragraph_row(words, edges):
    """
    A row whose text crosses two or more column edges (the only edge,
    in a two-column table) is a paragraph, not a table row. A single
    long entry crosses at most one.
    """

    return len(crossed_edges(words, edges)) >= min(2, len(edges) - 1)


def lines_of(words):
    """
    Group words into visual lines, top to bottom, left to right.

    A word joins a line if its centre falls within the line's height.
    OCR boxes for dashes can be 0.2pt tall ("2—"), so the word's own
    height is not a usable tolerance.
    """

    lines = []

    for word in sorted(words, key=y_centre):

        if lines:
            top = min(other.bounding_box.y for other in lines[-1])
            bottom = max(other.bounding_box.y + other.bounding_box.height for other in lines[-1])

            if top - 1 <= y_centre(word) <= bottom + 1:
                lines[-1].append(word)
                continue

        lines.append([word])

    return [sorted(line, key=x0) for line in lines]


def text_of(lines):
    return "\n".join(" ".join(word.text for word in line) for line in lines)


# --------------------------------------------------
# TABLE DETECTION
# --------------------------------------------------

def find_row_edges(band, rules, words, starts, next_band_top):
    """
    Row edges below the header band: one per rule, until the rules stop,
    a row gets too tall, or (once columns are known) a row holds text
    crossing column edges, i.e. a paragraph after the table.
    """

    top, bottom, left, right = band
    edges = [bottom]

    for y in rules:

        if y <= bottom + 2:          # the band's own edge
            continue

        # The next table's band edge, or a row too tall to be one
        if y >= next_band_top - 2 or y - edges[-1] > MAX_ROW_HEIGHT:
            break

        if starts and is_paragraph_row(words_between(words, edges[-1], y, left, right), starts):
            break

        edges.append(y)

    return edges


def find_grid(grey, table_top, table_bottom, column_bounds):
    """
    Row edges of the table grid, and for each column the edges where
    it has a rule.

    A merged cell (e.g. a category spanning three indicator rows) has
    no rule across it, so rules are found column by column: a column
    without a rule at some height keeps one cell across those rows.
    """

    column_lines = [
        [
            y for y in find_rules(grey, column_left + 1, column_right - 1)
            if table_top + 2 < y < table_bottom - 2
        ]
        for column_left, column_right in column_bounds
    ]

    # One grid edge per rule height, merging the same rule seen in
    # different columns
    edges = [table_top]

    for y in sorted(y for lines in column_lines for y in lines):
        if y - edges[-1] > 1.5:
            edges.append(y)

    edges.append(table_bottom)

    def nearest(y):
        return min(range(len(edges)), key=lambda index: abs(edges[index] - y))

    breaks = [
        sorted({0, len(edges) - 1} | {nearest(y) for y in lines})
        for lines in column_lines
    ]

    return edges, breaks


def build_table(table_id, page_number, starts, right, header, header_words,
                grid_edges, breaks, body_words, metadata):
    """
    A table block with one table_cell per physical cell, each holding
    its OCR words as children. A merged cell has a row_span over the
    grid rows it covers.

    header: (top, bottom) of the header band, which becomes row 1, or
    None for rows continuing a table from the previous page.
    """

    column_bounds = list(zip(starts, starts[1:] + [right]))
    first_row = 2 if header else 1

    # Cell position -> (first grid edge index, last grid edge index)
    span_of = {
        (first + first_row, column): (first, last)
        for column, column_breaks in enumerate(breaks, start=1)
        for first, last in zip(column_breaks, column_breaks[1:])
    }

    cells = {position: [] for position in span_of}

    if header:
        cells.update({(1, column): [] for column in range(1, len(column_bounds) + 1)})

        for word in header_words:
            cells[(1, column_of(word, starts))].append(word)

    for word in body_words:
        column = column_of(word, starts)

        for (row, cell_column), (first, last) in span_of.items():
            if cell_column == column and grid_edges[first] <= y_centre(word) < grid_edges[last]:
                cells[(row, column)].append(word)
                break

    children = []

    for order, ((row, column), words) in enumerate(sorted(cells.items()), start=1):
        lines = lines_of(words)
        column_left, column_right = column_bounds[column - 1]

        if (row, column) in span_of:
            first, last = span_of[(row, column)]
            row_top, row_bottom, row_span = grid_edges[first], grid_edges[last], last - first
        else:
            (row_top, row_bottom), row_span = header, 1

        for line_number, line in enumerate(lines, start=1):
            for word in line:
                word.metadata["line"] = line_number

        children.append(DocumentBlock(
            block_id=f"{table_id}_row_{row}_cell_{column}",
            block_type="table_cell",
            text=text_of(lines),
            order=order,
            bounding_box=BoundingBox(
                x=column_left, y=row_top,
                width=column_right - column_left, height=row_bottom - row_top
            ),
            children=[word for line in lines for word in line],
            metadata={
                "source": "ocr",
                "location": "body",
                "row": row,
                "column": column,
                "row_span": row_span,
                "column_span": 1
            }
        ))

    top = header[0] if header else grid_edges[0]

    return DocumentBlock(
        block_id=table_id,
        block_type="table",
        bounding_box=BoundingBox(
            x=starts[0], y=top, width=right - starts[0], height=grid_edges[-1] - top
        ),
        children=children,
        metadata={
            "source": "ocr",
            "location": "body",
            "page_number": page_number,
            "rows": len(grid_edges) - 1 + (1 if header else 0),
            "columns": len(column_bounds),
            **metadata
        }
    )


def column_headers_of(table):
    """
    Header text per column ("1", "2", ...) of a rebuilt table.
    """

    if "column_headers" in table.metadata:
        return table.metadata["column_headers"]

    return {
        str(cell.metadata["column"]): " ".join(cell.text.split())
        for cell in table.children
        if cell.metadata["row"] == 1
    }


def trim_empty_rows(edges, words, left, right):
    """
    Drop trailing rows without text: boxes or spacing below a table.
    """

    while len(edges) >= 2 and not words_between(words, edges[-2], edges[-1], left, right):
        edges = edges[:-1]

    return edges


def find_banded_table(table_id, page_number, grey, words, band, next_band_top):
    """
    The table under one header band, or None.
    Returns (table, words moved into it).
    """

    top, bottom, left, right = band
    header_words = words_between(words, top, bottom, left, right)

    # Tables already rebuilt have no free header words left
    if not any(has_text(word) for word in header_words):
        return None

    # Rows: follow the rules down, stopping at the first row that is
    # really a paragraph. Header labels give provisional column edges
    # for that test: table text never crosses a label's left edge, a
    # paragraph line running across the page crosses several.
    label_edges = [left] + [
        label_left - ALIGN_TOLERANCE
        for label_left, _ in header_labels(header_words)[1:]
    ]

    if len(label_edges) < 2:
        return None

    edges = trim_empty_rows(
        find_row_edges(band, find_rules(grey, left, right), words, label_edges, next_band_top),
        words, left, right
    )

    if len(edges) < 2:
        return None

    # Final columns from the table's own rows only
    starts = find_column_edges(header_words, [
        words_between(words, row_top, row_bottom, left, right)
        for row_top, row_bottom in zip(edges, edges[1:])
    ], left, right)

    if len(starts) < 2:
        return None

    body_words = words_between(words, bottom, edges[-1], left, right)
    grid_edges, breaks = find_grid(grey, edges[0], edges[-1], list(zip(starts, starts[1:] + [right])))

    table = build_table(
        table_id, page_number, starts, right, (top, bottom), header_words,
        grid_edges, breaks, body_words,
        # The dark band is the header, so the PII scanner need not guess it
        {"header_rows": [1]}
    )

    return table, header_words + body_words


def line_top(line):
    return min(word.bounding_box.y for word in line)


def line_bottom(line):
    return max(word.bounding_box.y + word.bounding_box.height for word in line)


def find_continuation(page, table_id, grey, words, previous_page, limit):
    """
    Rows at the top of a page that continue the last table of the
    previous page. They have no header band of their own, so they take
    that table's columns and, for the PII scan, its column headers.
    Returns (table, words moved into it), or None.
    """

    if previous_page is None:
        return None

    tables = [block for block in previous_page.blocks if block.block_type == "table"]

    if not tables:
        return None

    last = max(tables, key=lambda table: table.bounding_box.y)
    last_bottom = last.bounding_box.y + last.bounding_box.height
    page_height = grey.shape[0] / RENDER_SCALE

    # Already rebuilt on an earlier run
    if any(
        block.block_type == "table"
        and block.metadata.get("continues_table") == last.block_id
        for block in page.blocks
    ):
        return None

    # Only a table running to the foot of its page continues
    footer_top = page_height * FOOTER_ZONE

    if any(
        block is not last and block.bounding_box
        and last_bottom < block.bounding_box.y < footer_top
        for block in previous_page.blocks
    ):
        return None

    first_row_cells = sorted(
        (cell for cell in last.children if cell.metadata["row"] == 1),
        key=lambda cell: cell.metadata["column"]
    )
    starts = [cell.bounding_box.x for cell in first_row_cells]
    left, right = starts[0], last.bounding_box.x + last.bounding_box.width

    rules = [y for y in find_rules(grey, left, right) if y < limit]

    if not rules:
        return None

    # The first row may have no rule above it: it is then the stack of
    # lines just above the first rule. The running page header in the
    # top margin is never part of it.
    lines = lines_of(words_between(
        words, max(page_height * HEADER_ZONE, rules[0] - MAX_ROW_HEIGHT), rules[0], left, right
    ))
    row_lines = []

    for line in reversed(lines):
        if row_lines and line_top(row_lines[-1]) - line_bottom(line) > LINE_GAP:
            break

        row_lines.append(line)

    # No text above the first rule: it is the table's top border
    row_top = line_top(row_lines[-1]) - 2 if row_lines else rules[0]

    edges = trim_empty_rows(
        find_row_edges((row_top, row_top, left, right), rules, words, starts, limit),
        words, left, right
    )

    if len(edges) < 2:
        return None

    body_words = words_between(words, edges[0], edges[-1], left, right)
    grid_edges, breaks = find_grid(grey, edges[0], edges[-1], list(zip(starts, starts[1:] + [right])))

    table = build_table(
        table_id, page.page_number, starts, right, None, [],
        grid_edges, breaks, body_words,
        {
            "header_rows": [],
            "continues_table": last.block_id,
            "column_headers": column_headers_of(last)
        }
    )

    return table, body_words


# --------------------------------------------------
# PAGE UPDATE
# --------------------------------------------------

def rebuild_block(block, kept_words):
    """
    Update a paragraph or heading after some of its words moved
    into a table.
    """

    lines = {}

    for word in kept_words:
        lines.setdefault(word.metadata.get("line", 0), []).append(word)

    block.children = kept_words
    block.text = "\n".join(
        " ".join(word.text for word in lines[number]) for number in sorted(lines)
    )

    left = min(x0(word) for word in kept_words)
    top = min(word.bounding_box.y for word in kept_words)

    block.bounding_box = BoundingBox(
        x=left,
        y=top,
        width=max(x1(word) for word in kept_words) - left,
        height=max(word.bounding_box.y + word.bounding_box.height for word in kept_words) - top
    )

    block.metadata["line_count"] = len(lines)
    block.metadata["word_count"] = len(kept_words)


def move_words_into_tables(page, found):
    """
    Take the words of the new tables out of their OCR paragraphs (dropping
    emptied ones) and put each table in reading order.
    found: (table, words) pairs.
    """

    owner = {
        id(word): block
        for block in page.blocks
        if block.block_type in TEXT_BLOCK_TYPES
        for word in block.children
    }

    moved = set()

    # Keep a link back to the OCR paragraph each word came from
    for _, words in found:
        for word in words:
            word.metadata["source_block_id"] = owner[id(word)].block_id
            moved.add(id(word))

    blocks = []

    for block in page.blocks:

        if block.block_type not in TEXT_BLOCK_TYPES:
            blocks.append(block)
            continue

        kept = [word for word in block.children if id(word) not in moved]

        if len(kept) == len(block.children):
            blocks.append(block)
        elif kept:
            rebuild_block(block, kept)
            blocks.append(block)

    # Each table goes before the first block that starts below its top
    for table, _ in found:
        position = next(
            (
                index for index, block in enumerate(blocks)
                if block.bounding_box and block.bounding_box.y >= table.bounding_box.y
            ),
            len(blocks)
        )
        blocks.insert(position, table)

    for order, block in enumerate(blocks, start=1):
        block.order = order

    page.blocks = blocks


def reconstruct_page_tables(page, grey, previous_page=None):
    """
    Find the tables on one page, including rows continuing a table from
    the previous page, and move their words out of the OCR paragraphs
    into table blocks. Returns the new table blocks.
    """

    words = [
        word
        for block in page.blocks
        if block.block_type in TEXT_BLOCK_TYPES
        for word in block.children
    ]

    bands = find_header_bands(grey)
    found = []
    existing = sum(1 for block in page.blocks if block.block_type == "table")

    def next_id():
        return f"p{page.page_number}_t{existing + len(found) + 1}"

    # Continued rows sit above the page's first header band
    continuation = find_continuation(
        page, next_id(), grey, words, previous_page,
        bands[0][0] if bands else float("inf")
    )

    if continuation:
        found.append(continuation)

    for index, band in enumerate(bands):
        next_band_top = bands[index + 1][0] if index + 1 < len(bands) else float("inf")

        table = find_banded_table(next_id(), page.page_number, grey, words, band, next_band_top)

        if table:
            found.append(table)

    if found:
        move_words_into_tables(page, found)

    return [table for table, _ in found]


def reconstruct_tables(document, pdf_path):
    """
    Rebuild the tables of every OCR'd page of a PDF document.
    """

    pdf = pymupdf.open(pdf_path)
    previous_page = None

    for page in document.pages:
        # Pages read from a text layer have their tables already
        if page.metadata.get("extraction_method", "ocr").lower() == "ocr":
            grey = render_grey(pdf[page.page_number - 1])
            reconstruct_page_tables(page, grey, previous_page)

        previous_page = page

    # Pages already rebuilt are left as they are, so this is the total
    document.metadata["table_reconstruction"] = {
        "method": "header band + row rules + header-labelled columns",
        "tables": sum(
            1 for page in document.pages
            for block in page.blocks
            if block.block_type == "table"
        )
    }

    return document
