"""
Where each block's text comes from in a Word or PowerPoint file.

The DOCX and PPTX extractors build a paragraph's text from pieces: the
text of each w:t / a:t element, and the character a tab or line break
element stands for. The original-format redactor (src/original_format)
reads the file again with the same extractor, in locate mode: each
block then records its source here, and the same piece functions map
every character of the block's text back to the element it came from.
A finding's offsets in the extracted text are thereby offsets in the
file, so the extractor and the redactor cannot disagree about them.

    Piece           one element's share of a paragraph's text. editable:
                    the element is a w:t / a:t holding exactly that text
    TextSource      a block's paragraphs, the piece rule that reads them,
                    and how they are joined: block text is
                    "\\n".join(paragraph texts), stripped if strip
    PartsSource     parts whose XML holds a block's text, where the text
                    is not read paragraph by paragraph (a chart, SmartArt)
    ImageSource     the image part an image block was read from, and the
                    a:blip elements that show it
    Sources         records nothing: what a normal extraction passes
    SourceMap       records the source of each block (locate mode)
"""

from dataclasses import dataclass, field
from typing import Callable, List

from lxml import etree


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
NAMESPACES = {"w": W_NS, "a": A_NS}


def w(tag):
    return f"{{{W_NS}}}{tag}"


def a(tag):
    return f"{{{A_NS}}}{tag}"


@dataclass
class Piece:
    element: object
    text: str
    editable: bool


# --------------------------------------------------
# WORD
# --------------------------------------------------

# The children of a run that hold text, as python-docx's Run.text reads them
RUN_CONTENT = etree.XPath("w:br | w:cr | w:noBreakHyphen | w:ptab | w:t | w:tab", namespaces=NAMESPACES)

# Every run of a paragraph, including those in hyperlinks, content
# controls and tracked insertions, but not those of a text box inside it
# (text boxes are blocks of their own)
PARAGRAPH_RUNS = etree.XPath(".//w:r[not(ancestor::w:txbxContent)]", namespaces=NAMESPACES)

# The runs python-docx's Paragraph.text reads: direct runs and hyperlinks
DIRECT_RUNS = etree.XPath("w:r | w:hyperlink/w:r", namespaces=NAMESPACES)

# The w:t elements of a paragraph, nested ones included
TEXT_ELEMENTS = etree.XPath(".//w:t", namespaces=NAMESPACES)

# Breaks other than a line break (page, column) show no text
LINE_BREAK_TYPES = (None, "textWrapping")


def run_pieces(run):
    """
    The text of a run's content, element by element, as python-docx's
    Run.text gives it: a tab is "\\t", a line break "\\n", a
    non-breaking hyphen "-".
    """

    pieces = []

    for child in RUN_CONTENT(run):
        name = etree.QName(child).localname

        if name == "t":
            pieces.append(Piece(child, child.text or "", True))
        elif name in ("tab", "ptab"):
            pieces.append(Piece(child, "\t", False))
        elif name == "cr":
            pieces.append(Piece(child, "\n", False))
        elif name == "noBreakHyphen":
            pieces.append(Piece(child, "-", False))
        elif name == "br":
            pieces.append(Piece(child, "\n" if child.get(w("type")) in LINE_BREAK_TYPES else "", False))

    return pieces


def docx_paragraph_pieces(paragraph):
    """
    A body, header, footer or table cell paragraph: every run, except
    those of text boxes inside it.
    """

    return [piece for run in PARAGRAPH_RUNS(paragraph) for piece in run_pieces(run)]


def docx_comment_pieces(paragraph):
    """
    A comment paragraph, read as python-docx's Paragraph.text reads it:
    direct runs and hyperlinks only.
    """

    return [piece for run in DIRECT_RUNS(paragraph) for piece in run_pieces(run)]


def docx_text_pieces(paragraph):
    """
    A text box or note paragraph: the text of every w:t in it, without
    tabs or breaks.
    """

    return [Piece(t, t.text or "", True) for t in TEXT_ELEMENTS(paragraph)]


# --------------------------------------------------
# POWERPOINT
# --------------------------------------------------

def pptx_paragraph_pieces(paragraph):
    """
    An a:p, as python-pptx's paragraph.text reads it (runs, line breaks
    and fields), with a line break as "\\n" rather than "\\v".
    """

    pieces = []

    for child in paragraph:
        if child.tag in (a("r"), a("fld")):
            t = child.find(a("t"))

            if t is not None:
                pieces.append(Piece(t, t.text or "", True))

        elif child.tag == a("br"):
            pieces.append(Piece(child, "\n", False))

    return pieces


def pptx_comment_pieces(text):
    """
    A classic PowerPoint comment's p:text: plain text in one element.
    """

    return [Piece(text, text.text or "", True)]


# --------------------------------------------------
# BLOCK SOURCES
# --------------------------------------------------

def pieces_text(pieces):
    return "".join(piece.text for piece in pieces)


@dataclass
class TextSource:
    paragraphs: List[object]
    rule: Callable
    strip: bool = True

    def text(self):
        text = "\n".join(pieces_text(self.rule(paragraph)) for paragraph in self.paragraphs)
        return text.strip() if self.strip else text


@dataclass
class PartsSource:
    parts: List[object]


@dataclass
class ImageSource:
    part: object
    blips: List[object] = field(default_factory=list)


class Sources:
    """
    Records nothing. A normal extraction passes this.
    """

    def add(self, block_id, source):
        pass

    def xml(self, part):
        """
        The XML of a part the library keeps only as bytes (footnotes,
        endnotes).
        """

        return etree.fromstring(part.blob)


class SourceMap(Sources):
    """
    Records the source of every block (locate mode), and keeps the XML
    of parts read from bytes, so edits made to it can be saved back
    (trees: partname -> (part, root)).
    """

    def __init__(self):
        self.blocks = {}
        self.trees = {}

    def add(self, block_id, source):
        self.blocks[block_id] = source

    def xml(self, part):
        name = str(part.partname)

        if name not in self.trees:
            self.trees[name] = (part, etree.fromstring(part.blob))

        return self.trees[name][1]
