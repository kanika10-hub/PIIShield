"""
PDF: the redacted copy of a PDF file.

The extractor kept every word with its box: the word children of each
text block (in points; on a scanned page, the OCR words of the rendered
page) and the OCR words of each embedded picture (in its pixels). The
file is first checked to be the one the document was extracted from:
the same pages, with the same words in each text layer. Then, page by
page, each finding's words give the boxes to redact:

    pixels      every box is blanked in the images under it: a scanned
                page, a picture, the scan behind a searchable PDF's text
                (PDF_REDACT_IMAGE_PIXELS), and filled black
    characters  the value's characters are removed from the text layer,
                and only those: within a word, the boxes of its own
                characters (a value glued to a label, "Tel:+44 ..."). A
                character is removed if its box touches the area, and in
                tight lines the boxes of the lines above and below reach
                into a word's box, so the area is kept to the middle of
                its line (TEXT_MARGIN)
    leftovers   the places the file's check found PII left on its scans,
                in another pass (validator.another_pass): blanked the same
                way

Nothing is written in the black boxes: OCR reads nothing back from
them when the file is checked.

and then, for what the extractor never reads:

    metadata    the document information as the scan redacted it, every
                other value the scan found swept from it; XMP removed
    markup      comments, highlights, stamps and other annotations removed;
                link targets, form fields and bookmarks swept (a link
                over a redacted value goes with the redaction)
    attachments removed
    saved       in full, unused objects dropped (garbage=4): nothing
                removed stays in the file

    redact_pdf(source_path, plan, output_path, leftovers=()) -> (summary, errors)
"""

import re
from collections import Counter

import pymupdf

from src.original_format.images import PAD, line_boxes
from src.original_format.ooxml import finder, replace_text, sweep_target
from src.original_format.plan import span_triples


# A redaction is a solid black box, as images.py fills image pixels
FILL = (0, 0, 0)

# Share of a word box's height kept clear at its top and bottom, and
# points kept clear at its sides, when its characters are removed
TEXT_MARGIN = 0.25
EDGE = 0.3

# Points around a box whose image pixels are blanked: OCR boxes are tight
PIXEL_PAD = 1

# Where PyMuPDF keeps each property the extractor reads (pdf_properties)
PROPERTY_KEYS = {"created": "creationDate", "modified": "modDate"}


# --------------------------------------------------
# THE FILE IS THE DOCUMENT'S
# --------------------------------------------------

def native_words(page_data):
    """
    The words of a page's text layer, as the extractor kept them: every
    word child of its blocks not read by OCR.
    """

    words = Counter()

    def visit(blocks):
        for block in blocks:
            if block["block_type"] == "word":
                continue

            if block.get("metadata", {}).get("source") != "ocr" and block["block_type"] != "image_ocr":
                words.update(child["text"] for child in block.get("children", []) if child["block_type"] == "word")

            visit([child for child in block.get("children", []) if child["block_type"] != "word"])

    visit(page_data["blocks"])

    return words


def page_differs(page, page_data):
    """
    True if a page is not the one its extracted page was read from: a
    page with a text layer by its words; a scanned page, whose text OCR
    read from its pixels, by its size and the little text layer it has.
    """

    metadata = page_data["metadata"]

    if metadata.get("extraction_method") == "ocr":
        return (
            len("".join(page.get_text().split())) != metadata.get("native_char_count")
            or abs(page.rect.width - metadata.get("width", 0)) > 0.5
            or abs(page.rect.height - metadata.get("height", 0)) > 0.5
        )

    return Counter(word[4] for word in page.get_text("words")) != native_words(page_data)


def check_matches(pdf, document, file_name):
    """
    Refuse a file that is not the one the document was extracted from:
    the findings' boxes would land on other text.
    """

    pages = document["pages"]

    if len(pdf) != len(pages):
        raise ValueError(f"{file_name} has {len(pdf)} pages, its extracted document {len(pages)}")

    differ = [page_data["page_number"] for page, page_data in zip(pdf, pages) if page_differs(page, page_data)]

    if differ:
        raise ValueError(
            f"{file_name} does not match its extracted document: the text of pages "
            f"{', '.join(map(str, differ[:5]))} differs"
        )


# --------------------------------------------------
# BOXES
# --------------------------------------------------

def block_words(block):
    """
    A block's words in the order of its text, as dicts with text, box
    (x, y, width, height) and the word's OCR confidence if it has one; or
    None if they are not its text. An image's words are in its pixels,
    a text block's in points.
    """

    if block["block_type"] == "image_ocr":
        return block["metadata"].get("words") or None

    tokens = (block.get("text") or "").split()
    children = [
        child for child in block.get("children", [])
        if child["block_type"] == "word" and child.get("bounding_box")
    ]

    for order in (children, sorted(children, key=lambda c: (c["metadata"].get("line", 0), c.get("order") or 0))):
        if [child["text"] for child in order] == tokens:
            return [{"text": child["text"], **child["bounding_box"]} for child in order]

    return None


def span_parts(text, words, spans):
    """
    For each span (start, end, tag) of text, the words it touches, each
    as (word, start, end): the span's characters within the word. None if
    the words are not the text's tokens.
    """

    tokens = list(re.finditer(r"\S+", text))

    if [token.group() for token in tokens] != [word["text"] for word in words]:
        return None

    return [
        [
            (word, max(start, token.start()) - token.start(), min(end, token.end()) - token.start())
            for token, word in zip(tokens, words)
            if token.start() < end and start < token.end()
        ]
        for start, end, _ in spans
    ]


def box_of(word):
    return (word["x"], word["y"], word["x"] + word["width"], word["y"] + word["height"])


def share_of(word, start, end):
    """
    The part of a word's box its characters start:end take, by their
    share of its length, widened by half a character on each side: OCR
    gives no character boxes.
    """

    x0, y0, x1, y1 = box_of(word)
    length = len(word["text"]) or 1
    step = (x1 - x0) / length

    return (max(x0, x0 + step * (start - 0.5)), y0, min(x1, x0 + step * (end + 0.5)), y1)


class TextLayer:
    """
    The characters of a page's text layer, with their boxes, read once.
    """

    def __init__(self, page):
        self.chars = [
            (char["c"], pymupdf.Rect(char["bbox"]))
            for block in page.get_text("rawdict")["blocks"] if block.get("type") == 0
            for line in block["lines"]
            for span in line["spans"]
            for char in span["chars"]
            if not char["c"].isspace()
        ]

    def part(self, word, start, end):
        """
        The box of a word's characters start:end, from their own boxes;
        None if the word's characters are not found.
        """

        x0, y0, x1, y1 = box_of(word)
        inside = [
            (char, box) for char, box in self.chars
            if x0 - 0.5 <= (box.x0 + box.x1) / 2 <= x1 + 0.5 and y0 - 0.5 <= (box.y0 + box.y1) / 2 <= y1 + 0.5
        ]
        boxes = [box for _, box in inside[start:end]]

        if "".join(char for char, _ in inside) != word["text"] or not boxes:
            return None

        return (min(box.x0 for box in boxes), y0, max(box.x1 for box in boxes), y1)


def image_placement(page, block):
    """
    to_page(box) for an embedded picture: a box in its pixels (x0, y0,
    x1, y1) to the rectangle it covers on the page, through the
    placement at the block's position. None if the picture is not there.
    """

    metadata = block["metadata"]
    xref, width, height = metadata.get("image_xref"), metadata.get("image_width"), metadata.get("image_height")
    position = block.get("bounding_box") or {}

    if not (xref and width and height):
        return None

    for rect, matrix in page.get_image_rects(xref, transform=True):
        if abs(rect.x0 - position.get("x", -1)) < 0.5 and abs(rect.y0 - position.get("y", -1)) < 0.5:

            def to_page(box):
                corners = [
                    pymupdf.Point(x / width, y / height) * matrix
                    for x in (box[0], box[2]) for y in (box[1], box[3])
                ]
                return pymupdf.Rect(
                    min(p.x for p in corners), min(p.y for p in corners),
                    max(p.x for p in corners), max(p.y for p in corners)
                )

            return to_page

    return None


def padded(rect, pad):
    return pymupdf.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)


def text_area(rect):
    """
    The middle of a word box's line, where only its own characters reach.
    """

    margin = TEXT_MARGIN * rect.height
    edge = min(EDGE, rect.width / 4)

    return pymupdf.Rect(rect.x0 + edge, rect.y0 + margin, rect.x1 - edge, rect.y1 - margin)


# --------------------------------------------------
# WHAT THE EXTRACTOR NEVER READS
# --------------------------------------------------

def sweep_metadata(pdf, plan, properties):
    """
    The document information with the scan's redactions (properties:
    name -> sanitized text) and every other value the scan found
    replaced; the XMP copy of it removed. Returns the values replaced.
    """

    find = finder(plan.values, plan)
    metadata = dict(pdf.metadata or {})
    count = 0

    for name, text in properties.items():
        metadata[PROPERTY_KEYS.get(name, name)] = text

    for key, value in metadata.items():
        if isinstance(value, str) and value:
            spans = find(value)

            if spans:
                metadata[key] = replace_text(value, spans)
                count += len(spans)

    pdf.set_metadata({key: value for key, value in metadata.items() if key != "format" and key != "encryption"})
    pdf.del_xml_metadata()

    return count


def sweep_markup(pdf, plan):
    """
    Annotations removed but links and form fields, whose values are swept,
    as are link targets and bookmarks. Returns (values replaced,
    annotations removed).
    """

    find = finder(plan.values, plan)
    swept = removed = 0

    for page in pdf:
        for annot in list(page.annots() or []):
            if annot.type[0] not in (pymupdf.PDF_ANNOT_LINK, pymupdf.PDF_ANNOT_WIDGET):
                page.delete_annot(annot)
                removed += 1

        for link in page.get_links():
            target = sweep_target(link["uri"], find) if link.get("uri") else None

            if target is not None:
                link["uri"] = target
                page.update_link(link)
                swept += 1

        for widget in page.widgets() or []:
            changed = False

            for attribute in ("field_value", "field_label"):
                value = getattr(widget, attribute, None)
                spans = find(value) if isinstance(value, str) else []

                if spans:
                    setattr(widget, attribute, replace_text(value, spans))
                    swept += len(spans)
                    changed = True

            if changed:
                widget.update()

    toc = pdf.get_toc(simple=False)
    changed = False

    for entry in toc:
        spans = find(entry[1])

        if spans:
            entry[1] = replace_text(entry[1], spans)
            swept += len(spans)
            changed = True

    if changed:
        pdf.set_toc(toc)

    return swept, removed


# --------------------------------------------------
# REDACTION
# --------------------------------------------------

def redact_pdf(source_path, plan, output_path, leftovers=()):
    """
    Write the redacted copy of the PDF file the plan's document was
    extracted from. Returns (summary, errors): the counts of what was
    replaced, and each finding that could not be (block_id, finding_id,
    reason, never the value). Raises ValueError if the file does not
    match the document. leftovers: places on its pages ({"page_number",
    "box", "type"}) to blank too.
    """

    pdf = pymupdf.open(str(source_path))

    try:
        check_matches(pdf, plan.document, source_path.name)

        errors = []
        properties = {}
        pages = {}                      # page number -> {"pixels": [], "text": []}
        layers = {}                     # page number -> TextLayer
        images = set()
        summary = {
            "spans_replaced": 0, "images_redacted": 0, "values_swept": 0,
            "annotations_removed": 0, "attachments_removed": 0, "leftovers_redacted": 0,
        }

        def failed(block_id, spans, reason):
            errors.extend(
                {"block_id": block_id, "finding_id": item.get("finding_id"), "reason": reason}
                for span in spans for item in span["items"]
            )

        for block_id, spans in plan.spans.items():
            if block_id.startswith("property_"):
                properties[block_id[len("property_"):]] = plan.replaced_slots[block_id]
                summary["spans_replaced"] += len(spans)
                continue

            if block_id not in plan.blocks:
                failed(block_id, spans, "the block was not found in the file")
                continue

            page_number, block = plan.blocks[block_id]
            page = pdf[page_number - 1]
            boxes = pages.setdefault(page_number, {"pixels": [], "text": []})
            words = block_words(block)
            parts = span_parts(block.get("text") or "", words, span_triples(spans)) if words else None
            picture = block["block_type"] == "image_ocr"
            ocr = picture or block.get("metadata", {}).get("source") == "ocr"
            to_page = image_placement(page, block) if picture else None

            if parts is None or (picture and to_page is None):
                failed(block_id, spans, "its words do not match its text, so the values cannot be placed")
                continue

            def area(word, start, end):
                # A whole word's box; within a word, its characters' boxes (OCR: their share)
                if start == 0 and end == len(word["text"]):
                    return box_of(word)

                if ocr:
                    return share_of(word, start, end)

                return layers.setdefault(page_number, TextLayer(page)).part(word, start, end)

            for span, span_words in zip(spans, parts):
                areas = [area(word, start, end) for word, start, end in span_words]

                if not areas or None in areas:
                    failed(block_id, [span], "the characters of the value were not found among its block's words")
                    continue

                for box in line_boxes(areas):
                    if picture:
                        rect = to_page(padded(pymupdf.Rect(box), PAD))
                    elif ocr:
                        rect = padded(pymupdf.Rect(box), PIXEL_PAD)
                        # OCR read the page as shown; annotations take it unrotated
                        rect = rect * page.derotation_matrix if page.rotation else rect
                    else:
                        rect = pymupdf.Rect(box)
                        boxes["text"].append(text_area(rect))
                        rect = padded(rect, PIXEL_PAD)

                    boxes["pixels"].append(rect)

                summary["spans_replaced"] += 1

                if picture:
                    images.add((page_number, block["metadata"].get("image_xref")))

        # What the check of an earlier pass found left on the scans
        for item in leftovers:
            pages.setdefault(item["page_number"], {"pixels": [], "text": []})["pixels"].append(
                pymupdf.Rect(item["box"])
            )
            summary["leftovers_redacted"] += 1

        for page_number, boxes in sorted(pages.items()):
            page = pdf[page_number - 1]

            # Pixels first, under the full boxes; text only in the middle of its line
            for rect in boxes["pixels"]:
                page.add_redact_annot(rect, fill=FILL)

            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_PIXELS, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_NONE
            )

            if boxes["text"]:
                for rect in boxes["text"]:
                    page.add_redact_annot(rect, fill=False)

                page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                    text=pymupdf.PDF_REDACT_TEXT_REMOVE
                )

        summary["images_redacted"] = len(images)
        summary["values_swept"] += sweep_metadata(pdf, plan, properties)
        swept, removed = sweep_markup(pdf, plan)
        summary["values_swept"] += swept
        summary["annotations_removed"] = removed

        for name in pdf.embfile_names():
            pdf.embfile_del(name)
            summary["attachments_removed"] += 1

        pdf.save(str(output_path), garbage=4, deflate=True, clean=True)

    finally:
        pdf.close()

    return summary, errors
