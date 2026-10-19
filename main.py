"""
Run the whole pipeline on one file.

    python main.py                                  (opens a file chooser)
    python main.py D:/documents/any_report.docx
    python main.py uploads/<job_id>/report.pdf      (a stored upload, again)
    python main.py output/<job_id>/report.json      (already extracted: steps 2-5)

Any PDF, PPTX or DOCX file can be given, from any folder and under any
name. It is uploaded first: copied into a new folder of its own,
uploads/<job_id>/ (src/uploads.py). Every step after that reads the
copy and writes to output/<job_id>/, written <job>/ below, so a run
never depends on the file's name or place, and two files with the same
name never overwrite each other's outputs.

0. Check the input file: it exists, is not empty, and is the PDF, DOCX
   or PPTX its name says (not renamed, password-protected or damaged),
   then upload it                  -> uploads/<job_id>/<file>       (the copy, and
                                                                     upload.json)
1. Extract the PDF, PPTX or DOCX   -> <job>/<name>.json             (common JSON)
   with the extractor for its type (EXTRACTORS), and check it follows
   the common JSON format (src/models/schema.py)
2. Scan the extracted document     -> <job>/<name>_pii.json         (PII report)
3. Tag and redact the findings     -> <job>/<name>_tagged.json      (values labelled:
                                                                     still holds PII)
                                      <job>/<name>_sanitized.json   (values replaced by
                                                                     redaction tags)
4. Validate the sanitized copy     -> <job>/<name>_validation.json  (PASS, FAIL or
                                                                     NEEDS_REVIEW)
5. Report the exposure             -> <job>/<name>_exposure.json    (each finding: where,
                                                                     type, detector,
                                                                     confidence, action;
                                                                     no PII values)
                                      output/exposure_report.csv    (every document in
                                      output/exposure_report.md      output/, combined)
6. Write the redacted file in its  -> <job>/<name>_redacted.<ext>   (the PDF, PPTX or
   own format, from the stored                                       DOCX, each finding
   upload (src/original_format)                                      replaced by its tag)
                                      <job>/<name>_file_redaction.json
7. Check the redacted file         -> <job>/<name>_file_validation.json  (PASS, FAIL or
                                                                     NEEDS_REVIEW; a FAIL
                                                                     file is deleted)
   A PDF whose scans, read again, still show PII the first reading
   missed is written and checked again with those places blanked too
   (validator.another_pass), at most MAX_PASSES times in all

Only a sanitized copy whose verdict is PASS may be sent to an LLM, and
src.pii.validator.approved_copy() is the way to load it for that: it
refuses any other copy. The redacted file is released the same way, by
src.original_format.validator.approved_file(): only a PASS file beside
an approved copy.

Given an extracted JSON file, the outputs are written next to it.

Each step reads the file the step before it wrote, exactly as the steps
do when run on their own (src/pii/redactor.py, src/pii/validator.py,
src/pii/exposure_report.py),
so running a step again later gives the same result.

An input file that fails its check is refused before anything is
uploaded, extracted or deleted. Given a stored upload or an extracted
JSON file again, the outputs of its earlier run are deleted first, so a
run that stops part way never leaves an older sanitized copy beside a
newer report. If extraction, the format check or the scan fails, the
PII report is still written, with status "failed", and nothing is
tagged or redacted. If redaction or validation fails, the files they
had written are deleted. The combined exposure report is rebuilt once
the old outputs are deleted, so it never lists a document whose outputs
are gone. If steps 6-7 fail, the files they wrote are deleted and the
sanitized copy and its reports stand: only the redacted file is missing.
They are skipped for an extracted JSON file whose stored upload is gone.

Exit code: 0 PASS, 1 FAIL or an error, 2 NEEDS_REVIEW: the worse of the
sanitized copy's release and the redacted file's (a redacted file that
could not be written or checked counts as FAIL).
"""

import json
import sys
import zipfile
from pathlib import Path

from src.extractors.docx_extractor import extract_docx
from src.extractors.pdf_extractor import extract_pdf
from src.extractors.pptx_extractor import extract_pptx
from src.models.schema import format_problems
from src.original_format.redactor import WRITERS, record_path, redact_original, redacted_path, stored_upload
from src.original_format.validator import another_pass, approved_file, file_validation_path, validate_original
from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import failed_report, findings_path, save_findings, scan_file
from src.pii.exposure_report import (
    combined_folder, exposure_file, exposure_path, print_exposure, write_combined
)
from src.pii.redactor import output_paths, redact_file
from src.pii.validator import (
    EXIT_CODES, FAIL, NEEDS_REVIEW, PASS, NotApproved, approved_copy,
    print_validation, review_path, validate_file, validation_path, worst
)
from src.structure.heading_classifier import classify_headings
from src.structure.pdf_tables import reconstruct_tables
from src.uploads import store_upload, stored_job_id


UPLOAD_DIR = Path(__file__).parent / "uploads"
OUTPUT_DIR = Path(__file__).parent / "output"

# A PDF starts with this (readers accept it within the first 1024 bytes).
# A DOCX or PPTX is a zip file holding this part; a password-protected
# one is not a zip file at all.
PDF_SIGNATURE = b"%PDF-"
OFFICE_PARTS = {".docx": "word/document.xml", ".pptx": "ppt/presentation.xml"}


class PipelineStop(Exception):
    """
    A check stopped the pipeline: reported without a traceback.
    """


def input_problem(input_path):
    """
    Why the input file cannot be processed, or None.
    """

    file_type = input_path.suffix.lower()

    if not input_path.is_file():
        return f"File not found: {input_path}"

    if file_type not in (*SUPPORTED_TYPES, ".json"):
        return (
            f"Unsupported file type '{input_path.suffix}'. "
            "Use a PDF, PPTX or DOCX file, or an extracted JSON file."
        )

    if input_path.stat().st_size == 0:
        return f"{input_path} is empty."

    if file_type == ".pdf":
        with open(input_path, "rb") as file:
            if PDF_SIGNATURE not in file.read(1024):
                return f"{input_path} is not a PDF file, or is damaged."

    if file_type in OFFICE_PARTS:
        kind = file_type[1:].upper()

        try:
            with zipfile.ZipFile(input_path) as archive:
                if OFFICE_PARTS[file_type] not in archive.namelist():
                    return f"{input_path} is not a {kind} file."

        except zipfile.BadZipFile:
            return f"{input_path} is not a {kind} file, or is password-protected or damaged."

    return None


def format_summary(problems):
    """
    The first few format problems, one per line.
    """

    lines = [f"  - {problem}" for problem in problems[:10]]

    if len(problems) > 10:
        lines.append(f"  ... and {len(problems) - 10} more")

    return "\n".join(lines)


def extract_pdf_document(input_path):
    document = extract_pdf(input_path)
    # Tables on scanned pages first, so table text is not classified as headings
    document = reconstruct_tables(document, input_path)
    return classify_headings(document)


# The extractor for each supported file type. input_problem() has
# checked that a file's content is the type its extension says.
EXTRACTORS = {
    ".pdf": extract_pdf_document,
    ".pptx": extract_pptx,
    ".docx": extract_docx,
}

SUPPORTED_TYPES = tuple(EXTRACTORS)


def extract_document(input_path):
    """
    Step 1: run the extractor for the file type and return the
    extracted document in the common format.
    """

    input_path = Path(input_path)
    extractor = EXTRACTORS.get(input_path.suffix.lower())

    if extractor is None:
        raise ValueError(f"No extractor for '{input_path.suffix}' files: {input_path.name}")

    return extractor(str(input_path))


def ask_for_file():
    """
    The file to scan when none is given on the command line: chosen in a
    file dialog, or typed where no window can be opened (no tkinter, or
    no display, e.g. over SSH).
    """

    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        tkinter = None

    try:
        root = tkinter.Tk() if tkinter else None
    except tkinter.TclError:
        root = None

    if root is None:
        return Path(input("File to scan (PDF, PPTX, DOCX or extracted JSON): ").strip().strip('"'))

    root.withdraw()
    # In front of the terminal, not behind it
    root.attributes("-topmost", True)

    chosen = filedialog.askopenfilename(
        parent=root,
        title="Choose a file to scan for PII",
        filetypes=[
            ("PDF, PowerPoint or Word", "*.pdf *.pptx *.docx"),
            ("Extracted JSON", "*.json"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()

    if not chosen:
        sys.exit("No file chosen.")

    return Path(chosen)


def upload(input_path):
    """
    Step 0: the stored copy of the file to extract, and the folder its
    outputs go in, output/<job_id>/. A file given from uploads/ was
    stored by an earlier run: it is not copied again, and that run's
    outputs are replaced.
    """

    job_id = stored_job_id(input_path, UPLOAD_DIR)

    if job_id is None:
        print(f"[0/7] Uploading {input_path.name} ...")
        job_id, stored_path = store_upload(input_path, UPLOAD_DIR)
    else:
        print(f"[0/7] Upload skipped: {input_path.name} is a stored upload")
        stored_path = input_path

    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    print(f"      Stored copy: {stored_path}")
    print(f"      Outputs:     {job_dir}")

    return stored_path, job_dir


def load_extracted(json_path):
    """
    An extracted document given instead of a PDF, PPTX or DOCX, as
    (document, None), or (None, why it cannot be used). A PII report, or
    a tagged or sanitized copy, given by mistake is refused: the outputs
    would describe the wrong document. The web interface (src/web/jobs.py)
    checks uploaded JSON files with it too.
    """

    try:
        with open(json_path, "r", encoding="utf-8") as file:
            document = json.load(file)

    except (OSError, ValueError) as error:
        return None, f"Cannot read {json_path}: {error}"

    if not isinstance(document, dict) or not {"document_id", "file_name", "pages"} <= document.keys():
        return None, f"{json_path} is not an extracted document (common JSON)."

    redaction = (document.get("metadata") or {}).get("redaction")

    if redaction is not None:
        return None, f"{json_path} is the {redaction.get('variant')} copy. Give the extracted document instead."

    problems = format_problems(document)

    if problems:
        return None, f"{json_path} does not follow the common JSON format:\n{format_summary(problems)}"

    return document, None


def read_extracted(json_path):
    """
    load_extracted, for the command line: a file that cannot be used
    stops the run with the reason.
    """

    document, problem = load_extracted(json_path)

    if problem:
        sys.exit(problem)

    return document


def file_outputs(json_path):
    """
    The files steps 6-7 write for an extracted JSON file: the redaction
    record, the redacted file's check, and the redacted file (named for
    each type it could have).
    """

    return [
        record_path(json_path), file_validation_path(json_path),
        *(redacted_path(json_path, f"file{suffix}") for suffix in SUPPORTED_TYPES)
    ]


def pipeline_outputs(json_path):
    """
    The files steps 2-7 write for an extracted JSON file, in step order:
    PII report, tagged copy, sanitized copy, validation report,
    exposure report, then those of the redacted file (file_outputs).
    """

    tagged_path, sanitized_path = output_paths(json_path)

    return [
        findings_path(json_path), tagged_path, sanitized_path,
        validation_path(json_path), exposure_path(json_path), *file_outputs(json_path)
    ]


def original_problem(json_path, source_path):
    """
    Why the redacted file cannot be written for a document (steps 6-7),
    or None: its type has no writer, or its stored upload is gone.
    """

    original = stored_upload(json_path, {"file_name": source_path.name}, UPLOAD_DIR)

    if original.suffix.lower() not in WRITERS:
        return original, f"{original.suffix.upper().lstrip('.')} files cannot be redacted in their own format yet"

    if not original.is_file():
        return original, f"the uploaded file is no longer there ({original})"

    return original, None


def delete(paths):
    for path in paths:
        Path(path).unlink(missing_ok=True)


def main():

    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ask_for_file()

    # Before anything is uploaded, extracted or deleted
    problem = input_problem(input_path)

    if problem:
        sys.exit(problem)

    already_extracted = input_path.suffix.lower() == ".json"

    if already_extracted:
        json_path = input_path
        # A failed scan is reported under the original file's name
        source_path = Path(read_extracted(json_path)["file_name"])
    else:
        # Every later step reads the stored copy, not the file given
        source_path, job_dir = upload(input_path)
        json_path = job_dir / f"{source_path.stem}.json"

    outputs = pipeline_outputs(json_path)
    report_path = outputs[0]

    # A reviewer's sign-offs (web page) were for the copy about to be replaced
    delete([*(outputs if already_extracted else [json_path, *outputs]), review_path(json_path)])
    write_combined(combined_folder(json_path))

    try:
        if already_extracted:
            print(f"[1/7] Extraction skipped: using {json_path}")
            print("      Format:      OK (checked on loading)")
        else:
            print(f"[1/7] Extracting {source_path.name} ...")
            document = extract_document(source_path)
            document.save_json(json_path)
            print(f"      Common JSON: {json_path}")

            # Kept for inspection, but the scan never reads a broken document
            problems = format_problems(document.to_dict())

            if problems:
                raise PipelineStop(
                    f"The extracted document does not follow the common JSON format:\n"
                    f"{format_summary(problems)}"
                )

            print(f"      Format:      OK ({len(document.pages)} pages)")

        print("[2/7] Scanning for PII ...")
        nlp = LocalNLPDetector()

        if nlp.error:
            print(f"      Warning: {nlp.error} Person names will not be detected.")

        report = scan_file(json_path, report_path, nlp)

    except Exception as error:
        save_findings(failed_report(source_path, error), report_path)
        print(f"\nFailed. Report with status 'failed': {report_path}")

        if isinstance(error, PipelineStop):
            sys.exit(str(error))

        raise

    summary = report["summary"]
    print(f"      PII report:  {report_path}")
    print(f"      Status:      {report['status']}")
    print(f"      Findings:    {summary['total_findings']} {summary['by_type']}")

    for warning in summary["extraction_warnings"]:
        print(f"      Warning: {warning}")

    try:
        print("[3/7] Tagging and redacting ...")
        result, tagged_path, sanitized_path = redact_file(json_path, report_path)
        redaction = result["redaction"]
        print(f"      Tagged:      {tagged_path}  (still holds PII: for review only)")
        print(f"      Sanitized:   {sanitized_path}")
        print(
            f"      Redaction:   {redaction['status']}, {redaction['summary']['redacted']} "
            f"of {redaction['summary']['findings']} findings redacted"
        )

        print("[4/7] Validating the sanitized copy ...")
        validation, validation_report_path = validate_file(json_path, report_path, nlp)
        print(f"      Validation:  {validation_report_path}")

    except Exception:
        # A part-made or unchecked sanitized copy must not be mistaken for a checked one
        delete(outputs[1:])
        print("\nFailed. No tagged or sanitized copy was kept.")
        raise

    try:
        print("[5/7] Reporting the PII exposure ...")
        exposure, exposure_report_path = exposure_file(json_path, report_path)
        csv_path, md_path, documents = write_combined(combined_folder(json_path))
        print(f"      Exposure:    {exposure_report_path}  (no PII values: shareable)")
        print_exposure(exposure)
        print(f"      Combined:    {md_path}, {csv_path} ({documents} documents)")

    except Exception:
        # The sanitized copy and its verdict stand; only the report is missing
        delete([exposure_path(json_path)])
        write_combined(combined_folder(json_path))
        print("\nFailed. No exposure report was kept.")
        raise

    # Steps 6-7. If they fail, the sanitized copy and its reports stand
    original, problem = original_problem(json_path, source_path)
    file_verdict = None

    if problem:
        print(f"[6/7] Redacted file skipped: {problem}")
    else:
        try:
            print(f"[6/7] Writing the redacted {original.suffix.upper().lstrip('.')} file ...")
            record, redacted, record_file = redact_original(json_path, original)
            placed = record["summary"]
            print(f"      Redacted:    {redacted}  (not checked yet)")
            print(
                f"      Placed:      {placed['redacted']} of {placed['findings']} findings ({record['status']}), "
                f"{placed['images_redacted']} images changed"
            )

            print("[7/7] Checking the redacted file ...")
            file_validation, file_report_path = validate_original(json_path, nlp)
            file_verdict = file_validation["verdict"]
            print(f"      Check:       {file_report_path}  ({file_verdict})")

            # A scan read again can show what its first reading missed: those places too
            while leftovers := another_pass(file_validation):
                print(f"[6/7] Writing the redacted file again, with {len(leftovers)} places its check found ...")
                record, redacted, record_file = redact_original(json_path, original, leftovers=leftovers)
                print("[7/7] Checking the redacted file again ...")
                file_validation, file_report_path = validate_original(json_path, nlp)
                file_verdict = file_validation["verdict"]
                print(f"      Check:       {file_report_path}  ({file_verdict}, pass {file_validation['redaction_pass']})")

        except Exception as error:
            # An unchecked or part-made file must not be mistaken for a checked one
            delete(file_outputs(json_path))
            file_verdict = FAIL
            print(f"      Failed: {type(error).__name__}: {error}")
            print("      No redacted file was kept. The sanitized copy and its reports stand.")

    print_validation(validation)

    verdict = validation["verdict"]

    # The same gate any code sending text to an LLM must use
    try:
        approved_copy(json_path)
        outcomes = [PASS]

        print(
            f"\nApproved: {sanitized_path} passed validation and may be sent to an LLM.\n"
            "Load it with src.pii.validator.approved_copy(), which refuses any other copy."
        )

    except NotApproved as reason:
        outcomes = [verdict if verdict != PASS else FAIL]
        print(f"\nBlocked: do not send {sanitized_path} to an LLM ({reason}).")

        if verdict == FAIL:
            print("PII remains, or the copy does not match its document.")
        elif verdict == NEEDS_REVIEW:
            print("A person must review the issues first.")

        print(f"See {validation_report_path}")

    # The same gate any code sharing the redacted file must use
    if file_verdict is not None:
        try:
            print(
                f"\nApproved: {approved_file(json_path)} passed its check and may be shared.\n"
                "Release it with src.original_format.validator.approved_file(), which refuses any other file."
            )
            outcomes.append(PASS)

        except NotApproved as reason:
            # A PASS file waits for its sanitized copy's approval; refused beside
            # an approved copy, it changed since its check
            outcomes.append(file_verdict if file_verdict != PASS else outcomes[0] if outcomes[0] != PASS else FAIL)
            print(f"\nBlocked: do not share the redacted file ({reason}).")

    sys.exit(EXIT_CODES[worst(outcomes)])


if __name__ == "__main__":
    main()
