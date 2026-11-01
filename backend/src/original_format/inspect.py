"""
Everything a redacted file holds, read straight from it rather than
through the code that wrote it or the extractor, so the check
(validator.py) does not share their blind spots: a Word or PowerPoint
file from its zip archive (package_contents), a PDF through every place
PyMuPDF can find text in (pdf_contents).

    file_contents(path) -> Contents

        strings     (location, text): the text of each paragraph (its runs
                    joined, tracked deletions and field codes included),
                    every other element's text, and every attribute value
                    (link targets decoded), in every XML file, embedded
                    packages included. location is the file inside the
                    archive (and the attribute's name), never the text
        images      the image files it holds, embedded packages included
        unread      files that are neither XML nor images (embedded
                    objects, fonts, damaged XML): never read
"""

import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import unquote

import pymupdf
from lxml import etree

from src.original_format.ooxml import PARAGRAPH_TAGS, MAX_ZIP_DEPTH, is_zip, paragraph_pieces, parse_xml


# Images a picture can be stored as. SVG is XML too: its text is read,
# but a picture it draws is not
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".jpe", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".emf", ".wmf", ".svg"}


@dataclass
class Contents:
    strings: list = field(default_factory=list)
    images: list = field(default_factory=list)
    unread: list = field(default_factory=list)


def tree_strings(root, location):
    """
    (location, text) for every text of an XML tree: each paragraph's
    text, the text of elements outside paragraph runs, attribute values.
    """

    in_paragraphs = set()

    for paragraph in root.iter(*PARAGRAPH_TAGS):
        pieces = paragraph_pieces(paragraph)
        in_paragraphs.update(piece.element for piece in pieces if piece.editable)
        yield location, "".join(piece.text for piece in pieces)

    for element in root.iter():
        if not isinstance(element.tag, str):
            continue

        if element not in in_paragraphs and element.text and element.text.strip():
            yield location, element.text

        if element.tail and element.tail.strip():
            yield location, element.tail

        for name, value in element.attrib.items():
            local = etree.QName(name).localname
            yield f"{location}@{local}", unquote(value) if local == "Target" else value


def read_archive(data, prefix, contents, depth=1):

    with zipfile.ZipFile(BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue

            location = f"{prefix}{info.filename}"
            content = archive.read(info)
            suffix = PurePosixPath(info.filename).suffix.lower()

            if suffix in IMAGE_SUFFIXES:
                contents.images.append(location)

            root = parse_xml(content)

            if root is not None:
                contents.strings.extend(tree_strings(root, location))

            elif is_zip(content) and depth < MAX_ZIP_DEPTH:
                try:
                    read_archive(content, f"{location}!", contents, depth + 1)
                except zipfile.BadZipFile:
                    contents.unread.append(location)

            elif suffix not in IMAGE_SUFFIXES:
                contents.unread.append(location)


def package_contents(path):
    """
    What a Word or PowerPoint file holds (see the module docstring).
    """

    contents = Contents()

    with open(path, "rb") as file:
        read_archive(file.read(), "", contents)

    return contents


def pdf_contents(path):
    """
    What a PDF holds, read with PyMuPDF: the text layer of each page, the
    document information and XMP metadata, bookmarks, annotations (their
    text and author), link targets (decoded), form field names and
    values, attachment names, and the dictionary of every object. No
    image is listed: a PDF's images are read through its pages (a
    scanned page is OCR'd whole, pictures by the extractor's rules).
    Attachments are never read.
    """

    contents = Contents()
    strings = contents.strings

    with pymupdf.open(str(path)) as pdf:
        strings.extend((f"metadata/{key}", value) for key, value in (pdf.metadata or {}).items() if value)
        strings.append(("metadata/XMP", pdf.get_xml_metadata() or ""))
        strings.extend(("bookmarks", entry[1]) for entry in pdf.get_toc())

        for name in pdf.embfile_names():
            strings.append(("attachments", name))
            contents.unread.append(f"attachment {name}")

        for page in pdf:
            where = f"page {page.number + 1}"
            strings.append((where, page.get_text()))

            for annot in page.annots() or []:
                strings.extend(
                    (f"{where} annotation/{key}", value) for key, value in annot.info.items()
                    if key in ("content", "title", "subject") and value
                )

            strings.extend((f"{where} link", unquote(link["uri"])) for link in page.get_links() if link.get("uri"))

            for widget in page.widgets() or []:
                strings.extend(
                    (f"{where} form field", str(value))
                    for value in (widget.field_name, widget.field_label, widget.field_value) if value
                )

        for xref in range(1, pdf.xref_length()):
            try:
                strings.append((f"object {xref}", pdf.xref_object(xref, compressed=False)))
            except (RuntimeError, ValueError):
                pass                        # a free or damaged object

    return contents


def file_contents(path):
    """
    What a redacted file holds: a PDF, or a Word or PowerPoint package.
    """

    return pdf_contents(path) if PurePosixPath(str(path)).suffix.lower() == ".pdf" else package_contents(path)
