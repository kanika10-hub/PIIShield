"""
The redacted file in its original format: a copy of the uploaded PDF,
PPTX or DOCX with every finding redacted: Word and PowerPoint text
replaced by its redaction tag, PDF pages and pictures covered by solid
black boxes. It is written next to the JSON outputs; the original file
and the JSON reports are not changed.

    plan.py         what to redact: the findings, spans and tags of the
                    sanitized JSON copy (src/pii/redactor.py)
    docx_file.py    Word: text replaced across runs at the scanner's
    pptx_file.py    PowerPoint  offsets, image pixels, hidden content
    ooxml.py        what Word and PowerPoint share
    pdf_file.py     PDF: words removed from the text layer and image
                    pixels blanked at their positions, black boxes
                    in their place
    images.py       text inside images: OCR word boxes filled black
    redactor.py     step 6: writes <name>_redacted.<ext> and its record
                    <name>_file_redaction.json
    inspect.py      reads everything a redacted file holds, independently
                    of how it was written
    validator.py    step 7: reopens and re-extracts the redacted file,
                    scans it again, and writes <name>_file_validation.json
                    (PASS / NEEDS_REVIEW / FAIL). approved_file() is the
                    release gate for the file, as approved_copy() is for
                    the sanitized JSON copy
"""
