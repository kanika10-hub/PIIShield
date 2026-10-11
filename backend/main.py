"""
Run the whole pipeline on one file.

    python main.py input/risk_policy.pdf
    python main.py output/risk_policy.json      (already extracted: steps 2-4)

With no file given, it asks for one.

1. Extract the PDF, PPTX or DOCX   -> output/<name>.json             (common JSON)
2. Scan the extracted document     -> output/<name>_pii.json         (PII report)
3. Tag and redact the findings     -> output/<name>_tagged.json      (values labelled:
                                                                      still holds PII)
                                      output/<name>_sanitized.json   (values replaced by
                                                                      redaction tags)
4. Validate the sanitized copy     -> output/<name>_validation.json  (PASS, FAIL or
                                                                      NEEDS_REVIEW)

Only a sanitized copy whose verdict is PASS may be sent to an LLM.

Given an extracted JSON file, the outputs are written next to it.

Each step reads the file the step before it wrote, exactly as the steps
do when run on their own (src/pii/redactor.py, src/pii/validator.py),
so running a step again later gives the same result.

The outputs of an earlier run of the same file are deleted first, so a
run that stops part way never leaves an older sanitized copy beside a
newer report. If extraction or the scan fails, the PII report is still
written, with status "failed". If redaction or validation fails, the
files they had written are deleted.

Exit code: 0 PASS, 1 FAIL or an error, 2 NEEDS_REVIEW.
"""

import json
import sys
from pathlib import Path

from src.extractors.docx_extractor import extract_docx
from src.extractors.pdf_extractor import extract_pdf
from src.extractors.pptx_extractor import extract_pptx
from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import failed_report, findings_path, save_findings, scan_file
from src.pii.redactor import output_paths, redact_file
from src.pii.validator import EXIT_CODES, FAIL, PASS, print_validation, validate_file, validation_path
from src.structure.heading_classifier import classify_headings
from src.structure.pdf_tables import reconstruct_tables


OUTPUT_DIR = Path(__file__).parent / "output"
SUPPORTED_TYPES = (".pdf", ".pptx", ".docx")


def extract_document(input_path):
    """
    Step 1: run the extractor for the file type and return the
    extracted document in the common format.
    """

    input_path = Path(input_path)
    file_type = input_path.suffix.lower()

    if file_type == ".pdf":
        document = extract_pdf(str(input_path))
        # Tables on scanned pages first, so table text is not classified as headings
        document = reconstruct_tables(document, str(input_path))
        document = classify_headings(document)

    elif file_type == ".pptx":
        document = extract_pptx(str(input_path))

    else:
        document = extract_docx(str(input_path))

    return document


def read_extracted(json_path):
    """
    An extracted document given instead of a PDF, PPTX or DOCX. A PII
    report, or a tagged or sanitized copy, given by mistake is refused:
    the outputs would describe the wrong document.
    """

    try:
        with open(json_path, "r", encoding="utf-8") as file:
            document = json.load(file)

    except (OSError, ValueError) as error:
        sys.exit(f"Cannot read {json_path}: {error}")

    if not isinstance(document, dict) or not {"document_id", "file_name", "pages"} <= document.keys():
        sys.exit(f"{json_path} is not an extracted document (common JSON).")

    redaction = document.get("metadata", {}).get("redaction")

    if redaction is not None:
        sys.exit(f"{json_path} is the {redaction.get('variant')} copy. Give the extracted document instead.")

    return document


def extracted_from(json_path):
    """
    The name of the file an existing extracted JSON file was made from,
    or None if it cannot be read.
    """

    try:
        with open(json_path, "r", encoding="utf-8") as file:
            return json.load(file).get("file_name")

    except (OSError, ValueError, AttributeError):
        return None


def pipeline_outputs(json_path):
    """
    The files steps 2-4 write for an extracted JSON file, in step order:
    PII report, tagged copy, sanitized copy, validation report.
    """

    tagged_path, sanitized_path = output_paths(json_path)

    return [findings_path(json_path), tagged_path, sanitized_path, validation_path(json_path)]


def delete(paths):
    for path in paths:
        Path(path).unlink(missing_ok=True)


def main():

    if len(sys.argv) > 1:
        input_path = Path(sys.argv[1])
    else:
        input_path = Path(input("File to scan (PDF, PPTX, DOCX or extracted JSON): ").strip().strip('"'))

    if not input_path.is_file():
        sys.exit(f"File not found: {input_path}")

    file_type = input_path.suffix.lower()

    if file_type not in (*SUPPORTED_TYPES, ".json"):
        sys.exit(
            f"Unsupported file type '{input_path.suffix}'. "
            "Use a PDF, PPTX or DOCX file, or an extracted JSON file."
        )

    already_extracted = file_type == ".json"

    if already_extracted:
        json_path = input_path
        # A failed scan is reported under the original file's name
        source_path = Path(read_extracted(json_path)["file_name"])
    else:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        json_path = OUTPUT_DIR / f"{input_path.stem}.json"
        source_path = input_path

        # Outputs are named by file name only: report.pdf and report.docx
        # would delete and overwrite each other's
        previous = extracted_from(json_path) if json_path.is_file() else None

        if previous is not None and previous != input_path.name:
            sys.exit(
                f"{json_path} holds the outputs of {previous}, not {input_path.name}. "
                f"Rename {input_path.name}, or delete {json_path} to replace them."
            )

    outputs = pipeline_outputs(json_path)
    report_path = outputs[0]

    delete(outputs if already_extracted else [json_path, *outputs])

    try:
        if already_extracted:
            print(f"[1/4] Extraction skipped: using {json_path}")
        else:
            print(f"[1/4] Extracting {input_path.name} ...")
            document = extract_document(input_path)
            document.save_json(json_path)
            print(f"      Common JSON: {json_path}")

        print("[2/4] Scanning for PII ...")
        nlp = LocalNLPDetector()

        if nlp.error:
            print(f"      Warning: {nlp.error} Person names will not be detected.")

        report = scan_file(json_path, report_path, nlp)

    except Exception as error:
        save_findings(failed_report(source_path, error), report_path)
        print(f"\nFailed. Report with status 'failed': {report_path}")
        raise

    summary = report["summary"]
    print(f"      PII report:  {report_path}")
    print(f"      Status:      {report['status']}")
    print(f"      Findings:    {summary['total_findings']} {summary['by_type']}")

    for warning in summary["extraction_warnings"]:
        print(f"      Warning: {warning}")

    try:
        print("[3/4] Tagging and redacting ...")
        result, tagged_path, sanitized_path = redact_file(json_path, report_path)
        redaction = result["redaction"]
        print(f"      Tagged:      {tagged_path}  (still holds PII: for review only)")
        print(f"      Sanitized:   {sanitized_path}")
        print(
            f"      Redaction:   {redaction['status']}, {redaction['summary']['redacted']} "
            f"of {redaction['summary']['findings']} findings redacted"
        )

        print("[4/4] Validating the sanitized copy ...")
        validation, validation_report_path = validate_file(json_path, report_path, nlp)
        print(f"      Validation:  {validation_report_path}")

    except Exception:
        # A part-made or unchecked sanitized copy must not be mistaken for a checked one
        delete(outputs[1:])
        print("\nFailed. No tagged or sanitized copy was kept.")
        raise

    print_validation(validation)

    verdict = validation["verdict"]

    if verdict == PASS:
        print(f"\nSanitized output: {sanitized_path}\nIt passed validation and may be sent to an LLM.")

    elif verdict == FAIL:
        print(
            f"\nDo not send {sanitized_path} to an LLM: PII remains, or the copy does "
            f"not match its document. See {validation_report_path}"
        )

    else:
        print(
            f"\nDo not send {sanitized_path} to an LLM until a person has reviewed "
            f"the issues in {validation_report_path}"
        )

    sys.exit(EXIT_CODES[verdict])


if __name__ == "__main__":
    main()
