"""
Step 1 of the pipeline: the extractor for each supported file type.

    extract_document(path)      the document in the common format
    EXTRACTORS                  extension -> extractor
    SUPPORTED_TYPES             the extensions, ".pdf", ".pptx", ".docx"

main.py and the web interface (src/web/jobs.py) extract with these, and
the original-format validator (src/original_format/validator.py) reads
each redacted file back with them. Adding a file type means adding its
extractor here and a content check in main.input_problem().
"""

from pathlib import Path

from src.extractors.docx_extractor import extract_docx
from src.extractors.pdf_extractor import extract_pdf
from src.extractors.pptx_extractor import extract_pptx
from src.structure.heading_classifier import classify_headings
from src.structure.pdf_tables import reconstruct_tables


def extract_pdf_document(input_path, ocr_pages=()):
    """
    A PDF, with the tables of its scanned pages rebuilt and their
    headings classified. ocr_pages: see extract_pdf.
    """

    document = extract_pdf(input_path, ocr_pages)
    # Tables on scanned pages first, so table text is not classified as headings
    document = reconstruct_tables(document, input_path)
    return classify_headings(document)


# The extractor for each supported file type. main.input_problem() has
# checked that a file's content is the type its extension says.
EXTRACTORS = {
    ".pdf": extract_pdf_document,
    ".pptx": extract_pptx,
    ".docx": extract_docx,
}

SUPPORTED_TYPES = tuple(EXTRACTORS)


def extract_document(input_path):
    """
    Run the extractor for the file type and return the extracted
    document in the common format.
    """

    input_path = Path(input_path)
    extractor = EXTRACTORS.get(input_path.suffix.lower())

    if extractor is None:
        raise ValueError(f"No extractor for '{input_path.suffix}' files: {input_path.name}")

    return extractor(str(input_path))
