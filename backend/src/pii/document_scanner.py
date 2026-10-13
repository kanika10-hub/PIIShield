"""
Run PII detection over an extracted document (the common JSON format
produced by the PDF, PPTX and DOCX extractors).

Table cells are scanned with their table title, column header and row
label as context, so a bare value such as "TK4820913" under a
"Passport Number" header is recognised.

The report, saved as <name>_pii.json:

    schema_version, document_id, file_name, source_type,
    scan_timestamp, scanner_version, status
    summary    blocks_scanned, unique_texts_scanned, total_findings,
               by_type, pages_scanned, tables_scanned, extraction_warnings
    tables     title rows, header rows and column headers of each table
    findings   one record per detected value (see scan_document)

status is "completed", "completed_with_warnings" (extraction_warnings
is not empty) or "failed" (see failed_report).
"""

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from src.pii.detectors import NAME_WORD, detect_pii, finding


# Version of the report format, and of the detection code. Change
# SCANNER_VERSION whenever detection rules change, so reports made by
# different code can be told apart.
SCHEMA_VERSION = "1.2"
SCANNER_VERSION = "1.1.1"

# OCR'd blocks below this Tesseract confidence (0-100) are reported
LOW_OCR_CONFIDENCE = 60

# File properties that hold text (dates are left out)
TEXT_PROPERTIES = (
    "title", "subject", "author", "keywords", "comments", "category",
    "last_modified_by", "creator", "producer"
)

# A label (header or row label) is short text, not a value
MAX_LABEL_CHARS = 60
MAX_LABEL_WORDS = 8
MAX_LABEL_DIGITS = 4        # allows years as column headers
MAX_HEADER_ROWS = 3

# Titles that may stand before a surname: "Dr. Haddad", "Ms Okafor"
HONORIFICS = {"Mr", "Mrs", "Ms", "Mx", "Miss", "Dr", "Prof", "Sir", "Dame"}

# The word just before or just after a value, on the same line
WORD_BEFORE = re.compile(r"(\S+)[ \t]+$")
WORD_AFTER = re.compile(r"^[ \t]+(\S+)")

# Initials written as one word: "C.", "CA", "F.L.", "L.-E."
INITIALS = re.compile(r"(?:[A-Z]\.?-?){1,3}")


def clean(text):
    return " ".join((text or "").split())


def is_label(text):
    """
    True for header-like text: "Passport Number", "M. Vossberg CEO".
    False for values: emails, phone numbers, IDs, long descriptions.
    """

    return (
        0 < len(text) <= MAX_LABEL_CHARS
        and len(text.split()) <= MAX_LABEL_WORDS
        and "@" not in text
        and sum(character.isdigit() for character in text) <= MAX_LABEL_DIGITS
    )


# --------------------------------------------------
# TABLE STRUCTURE
# --------------------------------------------------

def build_grid(table):
    """
    Map every (row, column) position to the cell covering it.
    A merged cell covers several positions.
    """

    grid = {}

    for cell in table["children"]:
        metadata = cell["metadata"]
        row, column = metadata["row"], metadata["column"]

        for r in range(row, row + metadata.get("row_span", 1)):
            for c in range(column, column + metadata.get("column_span", 1)):
                grid[(r, c)] = cell

    return grid


def row_cells(grid, row, columns):
    """
    Distinct cells covering a row, left to right.
    """

    cells = []

    for column in range(1, columns + 1):
        cell = grid.get((row, column))

        if cell is not None and (not cells or cells[-1] is not cell):
            cells.append(cell)

    return cells


def is_title_row(grid, row, columns):
    """
    A title row has one filled cell, usually merged across the table:
    "Third-Party Risk Assessment".
    """

    filled = [cell for cell in row_cells(grid, row, columns) if clean(cell["text"])]

    return columns > 1 and len(filled) == 1


def is_label_row(grid, row, columns):
    """
    A header row candidate: at least two filled cells, most cells filled,
    and every filled cell is a label.
    """

    cells = row_cells(grid, row, columns)
    texts = [clean(cell["text"]) for cell in cells if clean(cell["text"])]

    return (
        len(texts) >= 2
        and len(texts) * 2 >= len(cells)
        and all(is_label(text) for text in texts)
    )


def continues_into_next_row(grid, row, columns):
    """
    True if the header carries on below this row: a cell merged down
    into the next row, or a group heading spanning several columns
    with sub-headings underneath.
    """

    for column in range(1, columns + 1):
        cell = grid.get((row, column))

        if cell is None:
            continue

        if grid.get((row + 1, column)) is cell:
            return True

        if cell["metadata"].get("column_span", 1) > 1:
            return True

    return False


def find_title_and_header_rows(grid, rows, columns):
    """
    Title rows (one filled cell) at the top, then header rows:
    label-like, with at least one data row below.
    """

    row = 1

    while row <= rows and is_title_row(grid, row, columns):
        row += 1

    title_rows = list(range(1, row))
    header_rows = []

    while row < rows and len(header_rows) < MAX_HEADER_ROWS:

        if header_rows and not continues_into_next_row(grid, row - 1, columns):
            break

        if not is_label_row(grid, row, columns):
            break

        header_rows.append(row)
        row += 1

    return title_rows, header_rows


def analyse_table(table):
    """
    Work out a table's title rows, header rows and the context
    (title, column header, row label) for every cell.

    The first row is not assumed to be the header: title rows are
    skipped, and a header needs label-like cells with data below it.
    An extractor that knows the header rows (the PDF table rebuild:
    the dark header band) records them in the table metadata.
    """

    rows = table["metadata"]["rows"]
    columns = table["metadata"]["columns"]
    grid = build_grid(table)

    known_header_rows = table["metadata"].get("header_rows")

    if known_header_rows is not None:
        title_rows, header_rows = [], list(known_header_rows)

    else:
        title_rows, header_rows = find_title_and_header_rows(grid, rows, columns)

    title = " ".join(
        clean(cell["text"])
        for title_row in title_rows
        for cell in row_cells(grid, title_row, columns)
        if clean(cell["text"])
    )

    # A column under a group heading gets "Group / Sub-heading".
    # Rows continued from a table on the previous page carry that
    # table's headers in their metadata.
    inherited = table["metadata"].get("column_headers") or {}
    column_headers = {int(column): header for column, header in inherited.items()}

    for column in range(1, columns + 1):

        if column in column_headers:
            continue

        parts = []

        for header_row in header_rows:
            cell = grid.get((header_row, column))
            text = clean(cell["text"]) if cell else ""

            if text and text not in parts:
                parts.append(text)

        column_headers[column] = " / ".join(parts)

    # Context per cell. Both the column header and the row label are
    # given, so header-on-top tables and label/value tables both work:
    # context is only searched for labels, never for values.
    # Title and header cells only get the title: the first cell of a
    # header row is a column header, not a label for the cells beside it.
    contexts = {}

    for cell in table["children"]:
        metadata = cell["metadata"]
        row, column = metadata["row"], metadata["column"]

        parts = [title]

        if row not in title_rows and row not in header_rows:
            parts.append(column_headers.get(column, ""))

            label_cell = grid.get((row, 1))

            if column > 1 and label_cell is not None and label_cell is not cell:
                label = clean(label_cell["text"])

                if is_label(label):
                    parts.append(label)

        contexts[cell["block_id"]] = " | ".join(
            dict.fromkeys(part for part in parts if part)
        )

    return {
        "table_id": table["block_id"],
        "rows": rows,
        "columns": columns,
        "title_rows": title_rows,
        "header_rows": header_rows,
        "column_headers": {
            column: header
            for column, header in column_headers.items()
            if header
        },
        "contexts": contexts,
    }


# --------------------------------------------------
# DOCUMENT SCAN
# --------------------------------------------------

def collect_units(document):
    """
    Return the text units to scan, in reading order, and the analysis
    of every table. A unit is (page_number, block, table, context).

    Table cells carry their table context. Children of other blocks
    (OCR words of a PDF paragraph) repeat their parent's text and are
    skipped.
    """

    units = []
    tables = []

    for page in document["pages"]:
        page_number = page["page_number"]

        for block in page["blocks"]:

            if block["block_type"] == "table":
                analysis = analyse_table(block)
                contexts = analysis.pop("contexts")

                tables.append({"page_number": page_number, **analysis})

                for cell in block["children"]:
                    if clean(cell["text"]):
                        units.append((
                            page_number, cell, block, contexts[cell["block_id"]]
                        ))

            elif clean(block.get("text")):
                units.append((page_number, block, None, ""))

            # A comment's author names a person
            author = block.get("metadata", {}).get("author")

            if clean(author):
                units.append((
                    page_number, property_block(f"{block['block_id']}_author", "author", author),
                    None, "author"
                ))

    # File metadata names people too ("author", "last modified by")
    for name, value in document.get("metadata", {}).get("properties", {}).items():
        if name in TEXT_PROPERTIES and clean(value):
            units.append((
                None, property_block(f"property_{name}", name, value),
                None, name.replace("_", " ")
            ))

    return units, tables


def property_block(block_id, name, value):
    """
    A metadata value (a file property, a comment's author) as a block,
    so it is scanned and reported like text. It has no page position.
    """

    return {
        "block_id": block_id,
        "block_type": "property",
        "text": str(value),
        "metadata": {"name": name},
        "bounding_box": None
    }


class DocumentNLP:
    """
    Wraps LocalNLPDetector for one document: spaCy runs once over all
    its texts in batches, and the document's ordinary (lowercase) words
    are collected once, then each detect_pii() call reuses both.
    """

    def __init__(self, detector, texts):
        self.detector = detector
        analysed = dict(zip(texts, detector.analyse(texts)))
        self.spans = {text: spans for text, (spans, _) in analysed.items()}
        self.parts_of_speech = {text: tags for text, (_, tags) in analysed.items()}
        self.common_words = detector.common_words(texts)

    def detect(self, text, context=""):
        return self.detector.detect(
            text, context,
            spans=self.spans.get(text, []),
            common_words=self.common_words
        )


def initials_of(name):
    """
    The initials of every name but the surname: "CA" for "Caleb A.
    Whitfield" and "C.A. Whitfield", "LE" for "Lars-Erik Brannstrom".
    """

    return "".join(
        re.sub(r"[.-]", "", part) if INITIALS.fullmatch(part)
        else "".join(piece[0] for piece in part.split("-") if piece)
        for part in name.split()[:-1]
    )


def same_person(first, second):
    """
    True if two full names with the same surname can name one person:
    the initials of one begin the other's, and the first names agree
    where both are written out ("Tobias Lindqvist", "T. Lindqvist").
    "Anna Park" and "Alan Park" are two people.
    """

    a, b = initials_of(first), initials_of(second)
    given = [name.split()[0] for name in (first, second)]

    return (
        (a.startswith(b) or b.startswith(a))
        and (given[0] == given[1] or any(INITIALS.fullmatch(name) for name in given))
    )


def introduces(word, names):
    """
    True if word, just before a surname, belongs with it: a title
    ("Dr.", "Ms"), the person's first name or initials ("Caleb", "CA",
    "C.", "L.-E."), or any single initial ("J Smith": a name, if perhaps
    another person's).
    """

    if word.rstrip(".") in HONORIFICS:
        return True

    letters = re.sub(r"[.-]", "", word)

    if len(letters) == 1 and letters.isupper():
        return True

    return any(
        word in name.split()[:-1]
        or (letters.isalpha() and letters.isupper() and initials_of(name).startswith(letters))
        for name in names
    )


def used_as_name(text, start, end, names, tags):
    """
    True if the surname at text[start:end] stands for one of the full
    names here, and is not one word of another capitalised name ("Ford
    Motor Company", "the Treasure Hunt") or an ordinary word ("Read the
    policy"). tags: spaCy's part of speech for the text, by word offset.
    """

    before = WORD_BEFORE.search(text[:start])
    after = WORD_AFTER.match(text[end:])

    # A title, first name or initials before it: "Dr. Haddad", "CA Whitfield"
    if before and introduces(before.group(1), names):
        return True

    name_parts = {part for name in names for part in name.split()}

    if after:
        word = after.group(1).rstrip(".,;:)]")

        if NAME_WORD.fullmatch(word) and word not in name_parts:
            return False

    if before:
        word = before.group(1)

        # Punctuation ends a phrase: "Approved: Haddad"
        if word[0].isupper() and not word.endswith((".", ",", ";", ":", ")", "]")):
            # A word that starts a sentence is capitalised for that reason
            # alone: "Contact Hunt"
            earlier = text[:before.start()].rstrip(" \t")

            if earlier and not earlier.endswith(("\n", ".", "!", "?", ":", ";")):
                return False

    return tags.get(start, "PROPN") == "PROPN"


def surname_mentions(units, found, parts_of_speech=None):
    """
    A person named in full somewhere in the document ("Caleb A. Whitfield")
    is often named by surname alone elsewhere ("owned by Whitfield").
    Those mentions identify the person too, so they are reported, at a
    lower confidence. The people it can stand for are the reason
    (context_trigger): "Anna Park; James Park" if two people share it.
    Each person is named in their fullest form: "T. Lindqvist" and
    "Tobias Lindqvist" are one person (see same_person).

    A surname that is also an ordinary word is only a name where it is
    used as one (see used_as_name). Skipped:

        a word of another capitalised name: "Ford Motor Company"
        a word spaCy reads as something other than a proper noun:
        "Read" in "Read the policy", "Green" as a RAG status

    unless the person's title, first name or initials stand before it.

    found: (unit, finding) pairs so far. parts_of_speech: spaCy's part
    of speech for each text, by word offset (DocumentNLP.parts_of_speech);
    without it, every mention outside another name is reported.
    Returns new pairs.
    """

    # Surname -> the people it can stand for, each by their fullest name,
    # in document order
    full_names = {}

    for _, item in found:
        name = " ".join(item["text"].split())

        if item["type"] == "PERSON_NAME" and len(name.split()) >= 2:
            people = full_names.setdefault(name.split()[-1], [])

            for index, other in enumerate(people):
                if same_person(name, other):
                    people[index] = max(other, name, key=len)
                    break
            else:
                people.append(name)

    covered = {}

    for unit, item in found:
        covered.setdefault(id(unit[1]), []).append((item["start"], item["end"]))

    if not full_names:
        return []

    # Longest first, so "Okonkwo-Bell" wins over a shorter overlap.
    # Not inside an email or a longer word: "r.achterberg@..."
    pattern = re.compile(
        r"(?<![\w.@-])("
        + "|".join(sorted(map(re.escape, full_names), key=len, reverse=True))
        + r")(?![\w@-])"
    )

    mentions = []

    for unit in units:
        text = unit[1]["text"]
        block_covered = covered.get(id(unit[1]), [])
        tags = (parts_of_speech or {}).get(text, {})

        for match in pattern.finditer(text):
            names = full_names[match.group()]

            if (
                any(start < match.end() and match.start() < end for start, end in block_covered)
                or not used_as_name(text, match.start(), match.end(), names, tags)
            ):
                continue

            mentions.append((unit, finding(
                "PERSON_NAME", text, match.start(), match.end(),
                0.75, "surname",
                "PERSON_SURNAME", "context_rule", "surname_mentions",
                "; ".join(names)
            )))

    return mentions


def image_details(block, start, end):
    """
    For a finding in text read from an image (an image_ocr block): the
    image, and the OCR words the finding covers, with their boxes in
    image pixels and the lowest OCR confidence (0-100) among them.
    None for findings in other blocks.

    The block text and its OCR word list come from two OCR passes over
    the same image. If their words differ, the finding cannot be placed
    in the image: word_boxes is None and the whole image must be redacted.
    """

    metadata = block.get("metadata", {})
    words = metadata.get("words")

    if block["block_type"] != "image_ocr" or words is None:
        return None

    tokens = list(re.finditer(r"\S+", block["text"]))
    covered = None

    if [token.group() for token in tokens] == [word["text"] for word in words]:
        covered = [
            word
            for token, word in zip(tokens, words)
            if token.start() < end and start < token.end()
        ]

    return {
        "image_id": block["block_id"],
        "image_part": metadata.get("image_part"),
        "ocr_engine": metadata.get("ocr_engine"),
        "ocr_confidence": min(word["confidence"] for word in covered) if covered else None,
        "word_boxes": None if covered is None else [
            {key: word[key] for key in ("x", "y", "width", "height")}
            for word in covered
        ],
    }


def page_list(numbers):
    """
    "1-3, 7" for the page numbers [1, 2, 3, 7].
    """

    ranges = []

    for number in sorted(set(numbers)):
        if ranges and number == ranges[-1][1] + 1:
            ranges[-1][1] = number
        else:
            ranges.append([number, number])

    return ", ".join(str(first) if first == last else f"{first}-{last}" for first, last in ranges)


def extraction_warnings(document):
    """
    Extraction issues, read from the extracted JSON, that can hide PII
    from the scan or leave a finding without a position to redact.
    """

    pages = document["pages"]
    blocks = [(page["page_number"], block) for page in pages for block in page["blocks"]]

    def count(block_type):
        return sum(
            1 for _, block in blocks
            if block["block_type"] == block_type and clean(block.get("text"))
        )

    # Limitations the extractor declared itself
    warnings = list(document.get("metadata", {}).get("limitations", []))

    scanned = [
        page["page_number"] for page in pages
        if page.get("metadata", {}).get("extraction_method", "").lower() == "ocr"
    ]

    if scanned:
        warnings.append(
            f"Pages {page_list(scanned)} are scanned images, read by OCR: misread "
            "characters can cause missed or wrong findings"
        )

    low_confidence = [
        page_number
        for page_number, block in blocks
        if (block.get("metadata", {}).get("ocr_confidence") or 100) < LOW_OCR_CONFIDENCE
    ]

    if low_confidence:
        warnings.append(
            f"{len(low_confidence)} OCR'd blocks have confidence below {LOW_OCR_CONFIDENCE} "
            f"(pages {page_list(low_confidence)})"
        )

    if count("image_ocr"):
        warnings.append(
            f"{count('image_ocr')} blocks are text read from images by OCR: misread text can "
            "cause missed or wrong findings, and findings in them must be redacted in the "
            "image itself (see image.word_boxes)"
        )

    unread_images = sum(1 for _, block in blocks if block["block_type"] == "image")

    if unread_images:
        warnings.append(f"{unread_images} images could not be OCR'd: text inside them was not scanned")

    objects = sum(1 for _, block in blocks if block["block_type"] == "embedded_object")

    if objects:
        warnings.append(f"{objects} embedded objects (e.g. spreadsheets) were not opened: their content was not scanned")

    if count("chart"):
        warnings.append(
            f"{count('chart')} charts: their titles, labels and series names were scanned, "
            "but not the data behind them"
        )

    # Older extracted JSON has no low_text: a page without any text then
    low_text = [
        page["page_number"] for page in pages
        if page.get("metadata", {}).get(
            "low_text",
            not any(clean(block.get("text")) or block.get("children") for block in page["blocks"])
        )
    ]

    if low_text:
        warnings.append(f"Little or no text was extracted from pages {page_list(low_text)}")

    return warnings


def detect_units(units, nlp_detector=None):
    """
    Run the detectors over scan units (page_number, block, table,
    context; see collect_units), then look for surname mentions.

    Each unique (text, context) pair is scanned once, and person names
    are detected in one spaCy batch rather than one call per block.

    Returns (found, unique_scans): (unit, finding) pairs in reading
    order, and the number of unique (text, context) pairs scanned.
    """

    unique_texts = list(dict.fromkeys(block["text"] for _, block, _, _ in units))

    nlp = None

    if nlp_detector is not None:
        if hasattr(nlp_detector, "analyse"):
            nlp = DocumentNLP(nlp_detector, unique_texts)
        else:
            nlp = nlp_detector

    cache = {}
    found = []

    for unit in units:
        _, block, _, context = unit
        key = (block["text"], context)

        if key not in cache:
            cache[key] = detect_pii(block["text"], nlp, context=context)

        found.extend((unit, item) for item in cache[key])

    found.extend(surname_mentions(units, found, getattr(nlp, "parts_of_speech", None)))

    # Reading order: block by block, then by position in the block
    order = {id(unit): index for index, unit in enumerate(units)}
    found.sort(key=lambda pair: (
        order[id(pair[0])], pair[1]["start"], pair[1]["end"], pair[1]["type"]
    ))

    # Redaction replaces text[start:end], so the offsets must point at
    # exactly the reported value
    for (_, block, _, _), item in found:
        if block["text"][item["start"]:item["end"]] != item["text"]:
            raise ValueError(
                f"{item['type']} offsets {item['start']}-{item['end']} do not "
                f"match the text of block {block['block_id']}"
            )

    return found, len(cache)


def scan_texts(texts, nlp_detector=None):
    """
    Detect PII in text found outside an extracted document: the parts of
    a file the extractors do not read (slide masters, custom XML, alt
    text, link targets, PDF annotations...). The original-format
    redactor and validator (src/original_format) use it.

    texts: (key, text, context) triples, each key unique. context labels
    the text as a table header labels a cell ("Company Phone",
    "author"), so a bare value under a label is recognised.

    Returns the findings (detector fields: type, text, start, end, ...)
    by key, for the keys with any.
    """

    units = [
        (None, property_block(key, context, text), None, context)
        for key, text, context in texts
        if clean(text)
    ]

    found, _ = detect_units(units, nlp_detector)
    by_key = {}

    for (_, block, _, _), item in found:
        by_key.setdefault(block["block_id"], []).append(item)

    return by_key


def scan_document(document, nlp_detector=None):
    """
    Detect PII in every text block and table cell of a document.

    Each unique (text, context) pair is scanned once, and person names
    are detected in one spaCy batch rather than one call per block.

    document is the extractor's JSON: a dictionary, or the
    ExtractedDocument an extraction pipeline still holds in memory.

    Each finding, in reading order:

        finding_id          F-0001, F-0002, ...
        type, text          PII category and the exact value
        start, end          offsets of the value in the block's text
        confidence, detector, rule_id, detection_method,
        matched_pattern, context_trigger
                            how it was found (see detectors.finding)
        context             table title | column header | row label
                            given to the detectors ("" outside tables)
        page_number, block_id, block_type, table_id, row, column,
        bounding_box        where it is in the extracted JSON; table_id,
                            row and column are None outside tables.
                            A file property ("property_author") or a
                            comment's author has block_type "property";
                            a file property has no page_number
        image               for text read from an image: image_id,
                            image_part, ocr_engine, ocr_confidence and
                            word_boxes in image pixels (see image_details);
                            None otherwise
        review_status      "unreviewed"; a reviewer sets "confirmed"
                            or "false_positive"
        action              "REDACT"
    """

    if hasattr(document, "to_dict"):
        document = document.to_dict()

    units, tables = collect_units(document)
    found, unique_scans = detect_units(units, nlp_detector)

    # item holds the detector's fields, type to context_trigger
    findings = [
        {
            "finding_id": f"F-{number:04d}",
            **item,
            "context": context,
            "page_number": page_number,
            "block_id": block["block_id"],
            "block_type": block["block_type"],
            "table_id": table["block_id"] if table else None,
            "row": block["metadata"].get("row") if table else None,
            "column": block["metadata"].get("column") if table else None,
            "bounding_box": block.get("bounding_box"),
            "image": image_details(block, item["start"], item["end"]),
            "review_status": "unreviewed",
            "action": "REDACT",
        }
        for number, ((page_number, block, table, context), item) in enumerate(found, start=1)
    ]

    warnings = extraction_warnings(document)

    return {
        **document_info(
            document["document_id"], document["file_name"], document["source_type"],
            "completed_with_warnings" if warnings else "completed"
        ),
        "summary": {
            "blocks_scanned": len(units),
            "unique_texts_scanned": unique_scans,
            "total_findings": len(findings),
            "by_type": dict(Counter(item["type"] for item in findings)),
            "pages_scanned": len(document["pages"]),
            "tables_scanned": len(tables),
            "extraction_warnings": warnings,
        },
        "tables": tables,
        "findings": findings,
    }


def document_info(document_id, file_name, source_type, status):
    """
    The report's first fields: which document was scanned, when, by
    which version of the scanner, and whether the scan completed.
    """

    return {
        "schema_version": SCHEMA_VERSION,
        "document_id": document_id,
        "file_name": file_name,
        "source_type": source_type,
        "scan_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scanner_version": SCANNER_VERSION,
        "status": status,
    }


def failed_report(input_path, error):
    """
    The report for a file whose extraction or scan raised an error.
    """

    input_path = Path(input_path)

    return {
        **document_info(
            input_path.stem, input_path.name,
            input_path.suffix.lstrip(".").upper(), "failed"
        ),
        "error": f"{type(error).__name__}: {error}",
        "summary": None,
        "tables": [],
        "findings": [],
    }


def findings_path(json_path):
    """
    Where the findings for an extracted JSON file go: next to it, as
    <name>_pii.json, so documents scanned separately do not overwrite
    each other's findings.
    """

    json_path = Path(json_path)

    return json_path.with_name(f"{json_path.stem}_pii.json")


def save_findings(result, output_path):

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(result, file, indent=2, ensure_ascii=False)


def scan_file(input_path, output_path=None, nlp_detector=None):
    """
    Scan an already saved extracted JSON file and save its findings.
    """

    with open(input_path, "r", encoding="utf-8") as file:
        document = json.load(file)

    result = scan_document(document, nlp_detector)

    save_findings(result, output_path or findings_path(input_path))

    return result
