"""
Step 6: the redacted file in its original format.

    python -m src.original_format.redactor output/<job_id>/<name>.json [uploads/<job_id>/<file>]

reads the extracted document <name>.json, its PII report <name>_pii.json
and the file it was extracted from (by default the stored upload,
uploads/<job_id>/<file_name>), and writes, next to the JSON:

    <name>_redacted.<ext>       the file, each finding replaced by its
                                redaction tag (plan.py says what is
                                replaced, the writer for the file type
                                how). It is not checked yet: validator.py
                                (step 7) is, and approved_file() is the
                                way to release it
    <name>_file_redaction.json  what was done: counts, and each finding
                                that could not be placed (block_id,
                                finding_id and reason, never the value)

A PDF can be redacted again with the leftovers its check found where
its scans still show PII (validator.another_pass): the file is written
again from the original, those places blanked too, and the record says
which pass it is.

If anything fails, neither file is kept.
"""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.original_format.docx_file import redact_docx
from src.original_format.pdf_file import redact_pdf
from src.original_format.plan import make_plan
from src.original_format.pptx_file import redact_pptx
from src.pii.document_scanner import findings_path
from src.pii.redactor import save_json


# Change whenever what is replaced in a file, or how, changes
FILE_REDACTOR_VERSION = "1.4.0"

WRITERS = {
    ".docx": redact_docx,
    ".pptx": redact_pptx,
    ".pdf": redact_pdf,
}


def redacted_path(json_path, file_name):
    """
    Where the redacted file goes: next to the extracted JSON file, as
    <name>_redacted.<ext>, with the extension of the original file.
    """

    json_path = Path(json_path)

    return json_path.with_name(f"{json_path.stem}_redacted{Path(file_name).suffix.lower()}")


def record_path(json_path):
    json_path = Path(json_path)

    return json_path.with_name(f"{json_path.stem}_file_redaction.json")


def sha256(path):
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def stored_upload(json_path, document, upload_dir="uploads"):
    """
    The stored upload a job's JSON was extracted from:
    <upload_dir>/<job_id>/<file_name>, job_id being the JSON's folder.
    """

    return Path(upload_dir) / Path(json_path).parent.name / document["file_name"]


def redact_original(json_path, source_path, report_path=None, leftovers=None):
    """
    Write the redacted copy of the file an extracted JSON file was
    extracted from, and its record. Returns (record, redacted file,
    record file). Raises ValueError for a file type with no writer yet,
    a file that is not the document's, or a report that does not belong
    to it.

    leftovers: for a PDF, the places its check found PII left on its
    scans (validator.another_pass), blanked too.
    """

    json_path, source_path = Path(json_path), Path(source_path)
    report_path = report_path or findings_path(json_path)

    document = json.loads(Path(json_path).read_text(encoding="utf-8"))
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))

    output_path = redacted_path(json_path, document["file_name"])
    record_file = record_path(json_path)

    # An older file, or its record, must not outlive a failed run
    for path in (output_path, record_file):
        path.unlink(missing_ok=True)

    writer = WRITERS.get(source_path.suffix.lower())

    if writer is None:
        raise ValueError(f"{source_path.suffix} files cannot be redacted in their own format yet")

    if source_path.name != document["file_name"]:
        raise ValueError(f"{source_path.name} is not {document['file_name']}, the file the document was extracted from")

    if leftovers and source_path.suffix.lower() != ".pdf":
        raise ValueError("only a PDF is redacted again with the leftovers of its check")

    plan = make_plan(document, report)

    try:
        summary, errors = writer(source_path, plan, output_path, **({"leftovers": leftovers} if leftovers else {}))
        errors = plan.errors + errors
        failed_ids = {error["finding_id"] for error in errors}

        record = {
            "file_redactor_version": FILE_REDACTOR_VERSION,
            "redacted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "document_id": document.get("document_id"),
            "file_name": document.get("file_name"),
            "report_scan_timestamp": report.get("scan_timestamp"),
            "status": "needs_review" if errors else "completed",
            # The first pass, or one more than the latest leftover's
            "redaction_pass": 1 + max((item["found_in_pass"] for item in leftovers or []), default=0),
            "leftovers": list(leftovers or []),
            "summary": {
                "findings": len(report["findings"]),
                "redacted": sum(
                    1 for spans in plan.spans.values() for span in spans for item in span["items"]
                    if item.get("finding_id") not in failed_ids
                ),
                "skipped": sum(1 for outcome in plan.outcomes.values() if outcome["status"] == "skipped"),
                "failed": len(failed_ids),
                **summary,
            },
            "errors": errors,
            "files": {
                "source": str(source_path),
                "source_sha256": sha256(source_path),
                "redacted": str(output_path),
                # The exact file written: the check refuses any other
                "redacted_sha256": sha256(output_path),
            },
        }

        save_json(record, record_file)

    except Exception:
        for path in (output_path, record_file):
            path.unlink(missing_ok=True)

        raise

    return record, output_path, record_file


def main():

    if len(sys.argv) < 2:
        sys.exit("Usage: python -m src.original_format.redactor output/<job_id>/<name>.json [original file]")

    json_path = Path(sys.argv[1])

    if not json_path.is_file():
        sys.exit(f"File not found: {json_path}")

    document = json.loads(json_path.read_text(encoding="utf-8"))
    source_path = Path(sys.argv[2]) if len(sys.argv) > 2 else stored_upload(json_path, document)

    if not source_path.is_file():
        sys.exit(f"File not found: {source_path}")

    record, output_path, record_file = redact_original(json_path, source_path)
    summary = record["summary"]

    print(f"Redacted file: {output_path}  (not checked yet: run src.original_format.validator)")
    print(f"Record:        {record_file}")
    print(f"\nStatus: {record['status']}")
    print(
        f"Findings: {summary['findings']}  redacted: {summary['redacted']}  skipped: {summary['skipped']}  "
        f"failed: {summary['failed']}  images: {summary['images_redacted']}  "
        f"other places: {summary['values_swept']}"
    )

    for error in record["errors"]:
        print(f"  Error: {error.get('finding_id') or ''} {error['block_id']}: {error['reason']}")


if __name__ == "__main__":
    main()
