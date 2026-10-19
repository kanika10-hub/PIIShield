"""
What redacting a Word and a PowerPoint file share (docx_file.py and
pptx_file.py): text replaced inside the XML elements it came
from, image pixels filled, and every part of the package searched for
the values the scan found.

    Unread                          an image reader for locate mode: images
                                    are not OCR'd again
    located_pieces(source)          a TextSource's pieces, at their offsets
                                    in the block text
    replace_pieces(located, spans)  each span replaced by its tag
    paragraph_pieces(paragraph)     every text element of a w:p or a:p,
                                    tracked deletions and field codes too
    sweep_tree(root, find)          every value find() reports replaced in
                                    an XML tree: paragraphs, other text,
                                    attributes
    sweep_zip(data, find)           the same inside an embedded package
    sweep_target(target, find)      the same in a link target
    PartTrees                       the XML of the parts a library keeps
                                    as bytes, parsed once, written back
    image_areas(plan, block, spans) the areas of an image to fill
    drop_svg_copies(part, trees)    the SVG copies of pictures removed:
                                    the raster image, read and
                                    redacted, is what shows
"""

import zipfile
from io import BytesIO
from urllib.parse import quote, unquote

from lxml import etree

from src.extractors.text_sources import W_NS, Piece, a, w
from src.models.document import DocumentBlock
from src.original_format.images import covered_words, span_areas
from src.original_format.plan import span_triples


MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"

# The SVG copy Office keeps of a picture, in an extension of its a:blip
SVG_BLIP = "{http://schemas.microsoft.com/office/drawing/2016/SVG/main}svgBlip"
R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"

PARAGRAPH_TAGS = (w("p"), a("p"))

# Elements whose text is part of a paragraph's text, and can be edited
TEXT_TAGS = (w("t"), w("delText"), w("instrText"), a("t"))

# Elements that stand for a character of a paragraph's text
SEPARATORS = {
    w("tab"): "\t", w("ptab"): "\t", w("br"): "\n", w("cr"): "\n",
    w("noBreakHyphen"): "-", a("br"): "\n",
}

XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

# Untrusted files: no entities, no network
PARSER = etree.XMLParser(resolve_entities=False, no_network=True)

# Embedded packages inside embedded packages, at most
MAX_ZIP_DEPTH = 3


class Unread:
    """
    The image reader for an extractor in locate mode: an image block
    without OCR. Its words come from the extracted document, which is
    what the findings point at.
    """

    def block(self, block_id, key, image_bytes, bounding_box=None, metadata=None):
        return DocumentBlock(
            block_id=block_id, block_type="image", bounding_box=bounding_box, metadata=dict(metadata or {})
        )


# --------------------------------------------------
# TEXT AT OFFSETS
# --------------------------------------------------

def located_pieces(source):
    """
    (start, end, piece) for each piece of a TextSource, at its offsets
    in the block text: paragraphs joined by "\\n", then stripped if the
    source strips.
    """

    located = []
    position = 0

    for number, paragraph in enumerate(source.paragraphs):
        if number:
            position += 1                   # the "\n" between paragraphs

        for piece in source.rule(paragraph):
            located.append((position, position + len(piece.text), piece))
            position += len(piece.text)

    if source.strip:
        raw = "\n".join(
            "".join(piece.text for piece in source.rule(paragraph)) for paragraph in source.paragraphs
        )
        lead = len(raw) - len(raw.lstrip())
        located = [(start - lead, end - lead, piece) for start, end, piece in located]

    return located


def set_text(element, text):
    element.text = text

    # Word drops leading and trailing spaces unless told to keep them
    if element.tag.startswith(f"{{{W_NS}}}") and text != text.strip():
        element.set(XML_SPACE, "preserve")


def replace_pieces(located, spans):
    """
    Replace each span (start, end, tag) of a text in the elements its
    pieces came from: the tag goes in the first editable element the span
    touches, the span's other characters are removed from theirs, and a
    tab or break inside the span is removed. Spans are in text order and
    do not overlap. Raises ValueError for a span that touches no editable
    element.
    """

    edits = {}                              # id(piece) -> (piece, [(start, end, insert)])
    removed = []

    for start, end, tag in spans:
        placed = False

        for piece_start, piece_end, piece in located:

            if piece_start == piece_end or piece_end <= start or end <= piece_start:
                continue

            if not piece.editable:
                if start <= piece_start and piece_end <= end:
                    removed.append(piece.element)
                continue

            edits.setdefault(id(piece), (piece, []))[1].append((
                max(start, piece_start) - piece_start,
                min(end, piece_end) - piece_start,
                "" if placed else tag
            ))
            placed = True

        if not placed:
            raise ValueError(f"characters {start}-{end} are in no text element that can be edited")

    for piece, piece_edits in edits.values():
        text = piece.element.text or ""

        for start, end, insert in sorted(piece_edits, reverse=True):
            text = text[:start] + insert + text[end:]

        set_text(piece.element, text)

    for element in removed:
        element.getparent().remove(element)


# --------------------------------------------------
# EVERY VALUE, EVERYWHERE
# --------------------------------------------------

def paragraph_pieces(paragraph):
    """
    The pieces of a w:p or a:p: every run's text, including tracked
    deletions and field codes, which the extractors do not read, with
    tabs and breaks as characters. The paragraphs of a text box inside
    it are paragraphs of their own.
    """

    pieces = []

    for element in paragraph.iter(*TEXT_TAGS, *SEPARATORS):
        if next(element.iterancestors(*PARAGRAPH_TAGS), None) is not paragraph:
            continue

        if element.tag in SEPARATORS:
            pieces.append(Piece(element, SEPARATORS[element.tag], False))
        else:
            pieces.append(Piece(element, element.text or "", True))

    return pieces


def in_order(pieces):
    """
    (start, end, piece) for pieces joined into one text.
    """

    located = []
    position = 0

    for piece in pieces:
        located.append((position, position + len(piece.text), piece))
        position += len(piece.text)

    return located


def replace_text(text, spans):
    for start, end, tag in reversed(spans):
        text = text[:start] + tag + text[end:]

    return text


def sweep_target(target, find):
    """
    A link target with every value replaced, still a valid URI, or None
    if it holds none. The value is searched for decoded: "Jane%20Doe".
    """

    decoded = unquote(target)
    spans = find(decoded)

    if not spans:
        return None

    return quote(replace_text(decoded, spans), safe=":/?#@!$&'()*+,;=-._~")


def sweep_tree(root, find):
    """
    Replace every value find(text) reports ((start, end, tag) spans) in
    an XML tree: in each paragraph's text, across its runs; in the text
    of every other element; and in attribute values (a link target in
    an embedded package's relationships decoded first). Attributes that
    are only digits are sizes and IDs, never a value. Returns the number
    of values replaced.
    """

    count = 0
    pieces_seen = set()

    # Lists: edits remove tabs and breaks while the tree is walked
    for paragraph in list(root.iter(*PARAGRAPH_TAGS)):
        pieces = paragraph_pieces(paragraph)
        pieces_seen.update(piece.element for piece in pieces if piece.editable)
        located = in_order(pieces)
        spans = find("".join(piece.text for piece in pieces))

        if spans:
            replace_pieces(located, spans)
            count += len(spans)

    for element in list(root.iter()):
        if not isinstance(element.tag, str):
            continue                        # comments, processing instructions

        if element not in pieces_seen and element.text:
            spans = find(element.text)

            if spans:
                set_text(element, replace_text(element.text, spans))
                count += len(spans)

        if element.tail and element.tail.strip():
            spans = find(element.tail)

            if spans:
                element.tail = replace_text(element.tail, spans)
                count += len(spans)

        for name, value in element.attrib.items():
            if value.isdigit():
                continue

            if etree.QName(name).localname == "Target":
                swept = sweep_target(value, find)

                if swept is not None:
                    element.set(name, swept)
                    count += 1

                continue

            spans = find(value)

            if spans:
                element.set(name, replace_text(value, spans))
                count += len(spans)

    return count


def parse_xml(data):
    """
    The XML tree of a part's bytes, or None if they are not XML.
    """

    if not data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
        return None

    try:
        return etree.fromstring(data, PARSER)
    except etree.XMLSyntaxError:
        return None


def serialise(root):
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def is_zip(data):
    return data[:4] == b"PK\x03\x04"


def sweep_zip(data, find, depth=1):
    """
    An embedded package (a chart's workbook, an embedded document) with
    every value replaced in each XML file it holds, and in the packages
    inside it. Returns (bytes, count): bytes is None if nothing changed
    or the package cannot be read.
    """

    if depth > MAX_ZIP_DEPTH:
        return None, 0

    try:
        source = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile:
        return None, 0

    count = 0
    output = BytesIO()

    with source, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            content = source.read(info)
            root = parse_xml(content)

            if root is not None:
                replaced = sweep_tree(root, find)

                if replaced:
                    content = serialise(root)
                    count += replaced

            elif is_zip(content):
                swept, replaced = sweep_zip(content, find, depth + 1)

                if swept is not None:
                    content = swept
                    count += replaced

            target.writestr(info, content)

    return (output.getvalue(), count) if count else (None, 0)


class PartTrees:
    """
    The XML of the parts a library keeps only as bytes, parsed once so
    every edit lands in the same tree, then written back. Parts the
    library parses itself (XmlPart) are edited in place.

    trees: partname -> (part, root) already parsed (SourceMap.trees),
    always written back.
    """

    def __init__(self, trees=None):
        self.trees = dict(trees or {})
        self.changed = set(self.trees)

    def root(self, part):
        """
        The part's XML tree, or None if it is not XML.
        """

        element = getattr(part, "_element", None)

        if element is not None:
            return element

        name = str(part.partname)

        if name not in self.trees:
            self.trees[name] = (part, parse_xml(part.blob))

        return self.trees[name][1]

    def touch(self, part):
        self.changed.add(str(part.partname))

    def write_back(self):
        for name in self.changed:
            part, root = self.trees.get(name, (None, None))

            if root is not None:
                part._blob = serialise(root)


def sweep_part(part, trees, find):
    """
    Every value replaced in one part: its XML, or the packages embedded
    in it. Returns the number replaced.
    """

    root = trees.root(part)

    if root is not None:
        count = sweep_tree(root, find)

        if count:
            trees.touch(part)

        return count

    if is_zip(part.blob):
        swept, count = sweep_zip(part.blob, find)

        if swept is not None:
            part._blob = swept

        return count

    return 0


def sweep_relationships(rels, find):
    """
    Every value replaced in the targets of a part's links to outside the
    file (hyperlinks: "mailto:jane.doe@example.com"). Returns the number
    replaced.
    """

    count = 0

    for rel in rels.values():
        if rel.is_external:
            swept = sweep_target(rel.target_ref, find)

            if swept is not None:
                rel._target = swept
                # python-pptx caches target_ref, which it saves; python-docx does not
                vars(rel).pop("target_ref", None)
                count += 1

    return count


# --------------------------------------------------
# IMAGES
# --------------------------------------------------

def drop_svg_copies(part, trees):
    """
    Remove the SVG copy Office keeps of each picture in a part (an
    asvg:svgBlip beside the a:blip's PNG or JPEG), and its file when
    nothing else uses it. Office shows the SVG when it can, but OCR read
    the raster image, and only its pixels are redacted. Returns the
    number removed.
    """

    root = trees.root(part)

    if root is None:
        return 0

    removed = []

    for svg in list(root.iter(SVG_BLIP)):
        # The a:ext holding it, and the a:extLst if that empties it
        holder = svg.getparent() if svg.getparent().tag == a("ext") else svg
        parent = holder.getparent()
        parent.remove(holder)

        if parent.tag == a("extLst") and len(parent) == 0:
            parent.getparent().remove(parent)

        removed.append(svg.get(R_EMBED))

    if removed:
        trees.touch(part)
        used = {value for element in root.iter() for value in element.attrib.values()}

        for rel_id in removed:
            if rel_id and rel_id not in used and rel_id in part.rels:
                part.rels.pop(rel_id)

    return len(removed)


def image_areas(block, spans):
    """
    The areas to fill in an image block's image for its merged spans,
    or None if its OCR words do not match its text.
    """

    triples = span_triples(spans)
    covered = covered_words(block.get("text") or "", block.get("metadata", {}).get("words") or [], triples)

    return None if covered is None else span_areas(triples, covered)


def finder(matcher, plan):
    """
    find(text) for the sweep: the matcher's spans, without the values a
    reviewer marked as false positives.
    """

    def find(text):
        return [
            span for span in matcher.spans(text)
            if not plan.is_protected(text[span[0]:span[1]])
        ]

    return find
