"""
Run the pipeline on files uploaded through the web interface (app.py),
one job at a time in a background thread, and keep each job's progress
for the page to show.

A job goes through the steps of main.py, with the same functions and
the same rules for failures. There are two kinds of upload, as for
main.py: a document file, or an extracted JSON file (a document already
extracted, e.g. by an earlier run), which skips extraction:

    upload      uploads/<job_id>/<file>              store_file, then input_problem;
                                                     a JSON file must pass load_extracted
    extract     output/<job_id>/<name>.json          the extractor for the type
                                                     (a JSON file is copied there instead)
    check       the common JSON format               format_problems
    scan        <name>_pii.json                      scan_file
    redact      <name>_tagged.json, <name>_sanitized.json
    validate    <name>_validation.json
    audit       <name>_exposure.json                 exposure_file
    then        approved_copy: may the sanitized copy go to an LLM?

If extraction, the format check or the scan fails, the PII report is
written with status "failed" and nothing is tagged or redacted. If
redaction or validation fails, the files they wrote are deleted. A step
that fails stops the job, and the steps after it are "skipped".

Jobs are kept in memory: after a restart the page no longer lists them,
but their folders stay until deleted (Jobs.delete, or by hand).
"""

import json
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from werkzeug.utils import secure_filename

from main import (
    OUTPUT_DIR, SUPPORTED_TYPES, UPLOAD_DIR, delete, extract_document, format_summary,
    input_problem, load_extracted, pipeline_outputs
)
from src.models.schema import format_problems
from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import failed_report, save_findings, scan_file
from src.pii.exposure_report import combined_folder, exposure_file, exposure_path, write_combined
from src.pii.redactor import redact_file
from src.pii.validator import NotApproved, approved_copy, validate_file
from src.uploads import store_file


# The steps the page shows, in order
STEPS = [
    ("extract", "Text extraction"),
    ("check", "Extraction check"),
    ("scan", "PII detection"),
    ("redact", "Tagging and sanitization"),
    ("validate", "Validation"),
    ("audit", "Audit report"),
]

ORIGINAL_PATH = "uploaded through the web interface"

# An extracted document (common JSON) skips extraction
EXTRACTED_TYPE = ".json"

# How the text was read (ExtractedDocument.extraction_method)
READ_AS = {"native": "native text", "ocr": "read by OCR", "mixed": "native text and OCR"}


class InputRejected(Exception):
    """
    The uploaded file cannot be processed: nothing was kept.
    """


class StepFailed(Exception):
    """
    A step found a problem that stops the job (not a crash).
    """


class Job:
    """
    One uploaded file and its progress. Its paths stay on the server:
    to_dict() is what the page sees.
    """

    def __init__(self, job_id, stored_path, source_name=None):
        self.job_id = job_id
        self.stored_path = stored_path
        # An extracted JSON upload names the document it was extracted from
        self.extracted = stored_path.suffix.lower() == EXTRACTED_TYPE
        self.source_name = source_name or stored_path.name
        self.json_path = OUTPUT_DIR / job_id / f"{stored_path.stem}.json"
        self.created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.state = "queued"
        self.steps = {name: {"name": name, "label": label, "status": "pending", "detail": None}
                      for name, label in STEPS}
        self.error = None
        self.result = {}
        self.lock = threading.Lock()

    @contextmanager
    def step(self, name):
        """
        Run one step: running, then done, or failed with the error.
        The body sets the step's detail through the yielded dict.
        """

        with self.lock:
            self.steps[name]["status"] = "running"

        detail = {}

        try:
            yield detail

        except Exception as error:
            with self.lock:
                self.steps[name]["status"] = "failed"
                self.steps[name]["detail"] = error_text(error)
            raise

        with self.lock:
            self.steps[name]["status"] = "done"
            self.steps[name]["detail"] = detail.get("text")

    def skip(self, name, detail):
        """
        A step this job does not need: an extracted JSON upload skips
        extraction.
        """

        with self.lock:
            self.steps[name]["status"] = "skipped"
            self.steps[name]["detail"] = detail

    def update(self, **fields):
        with self.lock:
            for key, value in fields.items():
                setattr(self, key, value)

    def fail(self, error):
        with self.lock:
            self.state = "failed"
            self.error = error_text(error)

            for step in self.steps.values():
                if step["status"] in ("pending", "running"):
                    step["status"] = "skipped"

    def to_dict(self):
        with self.lock:
            return {
                "job_id": self.job_id,
                "file_name": self.stored_path.name,
                "file_type": self.stored_path.suffix.lstrip(".").upper(),
                "input_kind": "extracted_json" if self.extracted else "document",
                "source_file": self.source_name,
                "size_bytes": self.stored_path.stat().st_size if self.stored_path.exists() else None,
                "created_at": self.created_at,
                "state": self.state,
                "steps": [dict(step) for step in self.steps.values()],
                "error": self.error,
                "result": dict(self.result),
            }

    def finished(self):
        return self.state in ("completed", "failed")


def error_text(error):
    """
    What the page shows of an error. Errors name files, steps and block
    IDs, never a PII value (the scanner and redactor report offsets).
    """

    if isinstance(error, (StepFailed, InputRejected)):
        return str(error)

    return f"{type(error).__name__}: {error}"


def safe_name(file_name):
    """
    The name the upload is stored under: no folders, only safe
    characters, and its extension kept. None if the type is not one the
    pipeline reads.
    """

    suffix = Path(file_name or "").suffix.lower()

    if suffix not in (*SUPPORTED_TYPES, EXTRACTED_TYPE):
        return None

    stem = secure_filename(Path(file_name).stem) or "document"

    return f"{stem}{suffix}"


class Jobs:
    """
    Every job since the server started. Jobs run one at a time: spaCy is
    shared, and OCR uses every core it can.
    """

    def __init__(self):
        self.jobs = {}
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.nlp = None

    def submit(self, upload):
        """
        Store an uploaded file (a werkzeug FileStorage) in a job folder
        of its own, check it, and start its job. Raises InputRejected,
        keeping nothing, if it cannot be processed.
        """

        name = safe_name(upload.filename)

        if name is None:
            raise InputRejected(
                "Unsupported file type. Upload a PDF, PPTX or DOCX file, or an extracted JSON file."
            )

        job_id, stored_path = store_file(name, ORIGINAL_PATH, upload.save, UPLOAD_DIR)

        # The content, not just the name: empty, renamed, password-protected or
        # damaged; a JSON file must be an extracted document, not a report or copy
        problem = input_problem(stored_path)
        document = None

        if not problem and stored_path.suffix.lower() == EXTRACTED_TYPE:
            document, problem = load_extracted(stored_path)

        if problem:
            shutil.rmtree(stored_path.parent, ignore_errors=True)
            raise InputRejected(problem.replace(str(stored_path), name))

        job = Job(job_id, stored_path, document["file_name"] if document else None)
        job.json_path.parent.mkdir(parents=True, exist_ok=True)

        with self.lock:
            self.jobs[job_id] = job

        threading.Thread(target=self.run, args=(job,), daemon=True).start()

        return job

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def delete(self, job_id):
        """
        Delete a finished job's uploaded file and outputs. False if it
        is unknown or still running. Raises OSError if a file could not
        be deleted (on Windows, a file still open elsewhere): the job is
        kept, so deleting can be tried again.
        """

        job = self.get(job_id)

        if job is None or not job.finished():
            return False

        folders = [UPLOAD_DIR / job_id, OUTPUT_DIR / job_id]

        for folder in folders:
            shutil.rmtree(folder, ignore_errors=True)

        write_combined(OUTPUT_DIR)

        left = [folder.name for folder in folders if folder.exists()]

        if left:
            raise OSError("Some of the scan's files could not be deleted (a file may still be open). Try again.")

        with self.lock:
            del self.jobs[job_id]

        return True

    def detector(self):
        # Loaded once, on the first job: loading spaCy takes seconds
        if self.nlp is None:
            self.nlp = LocalNLPDetector()

        return self.nlp

    def run(self, job):
        with self.run_lock:
            job.update(state="running")

            try:
                self.pipeline(job)
                job.update(state="completed")

            except Exception as error:
                job.fail(error)

    def pipeline(self, job):
        """
        The steps of main.py for one job (see the module docstring).
        """

        json_path = job.json_path
        outputs = pipeline_outputs(json_path)
        report_path = outputs[0]

        try:
            if job.extracted:
                job.skip("extract", f"Not needed: the upload is already extracted (from {job.source_name})")
            else:
                with job.step("extract") as detail:
                    extracted = extract_document(job.stored_path)
                    extracted.save_json(json_path)
                    pages = len(extracted.pages)
                    detail["text"] = f"{pages} page{'' if pages == 1 else 's'}, {READ_AS[extracted.extraction_method]}"

            with job.step("check") as detail:
                # An extracted upload is worked on as a copy beside its outputs, like an extraction
                if job.extracted:
                    shutil.copyfile(job.stored_path, json_path)

                document = json.loads(json_path.read_text(encoding="utf-8"))
                problems = format_problems(document)

                if problems:
                    raise StepFailed(
                        "The extracted document does not follow the common JSON format:\n"
                        + format_summary(problems)
                    )

                pages = len(document["pages"])
                detail["text"] = (
                    f"Common JSON format OK ({pages} page{'' if pages == 1 else 's'}, "
                    f"{READ_AS.get(document['extraction_method'], document['extraction_method'])})"
                    if job.extracted else "Common JSON format OK"
                )

            with job.step("scan") as detail:
                nlp = self.detector()
                report = scan_file(json_path, report_path, nlp)
                summary = report["summary"]
                warnings = len(summary["extraction_warnings"])
                detail["text"] = (
                    f"{summary['total_findings']} findings"
                    + (f", {warnings} extraction warnings" if warnings else "")
                    + (" (person names not detected: spaCy unavailable)" if nlp.error else "")
                )

            job.update(result={
                "findings_total": summary["total_findings"],
                "by_type": summary["by_type"],
                "extraction_warnings": summary["extraction_warnings"],
                "report_status": report["status"],
                "names_detected": nlp.error is None,
            })

        except Exception as error:
            # Reported under the document's name, as main.py does
            save_findings(failed_report(Path(job.source_name), error), report_path)
            raise

        try:
            with job.step("redact") as detail:
                result, _, _ = redact_file(json_path, report_path)
                redaction = result["redaction"]
                detail["text"] = (
                    f"{redaction['summary']['redacted']} of {redaction['summary']['findings']} "
                    f"findings redacted ({redaction['status']})"
                )

            with job.step("validate") as detail:
                validation, _ = validate_file(json_path, report_path, nlp)
                detail["text"] = validation["verdict"].replace("_", " ")

        except Exception:
            # A part-made or unchecked sanitized copy must not be mistaken for a checked one
            delete(outputs[1:])
            raise

        result = {**job.result, "verdict": validation["verdict"], "checks": validation["summary"]}
        job.update(result=result)

        try:
            with job.step("audit") as detail:
                exposure, _ = exposure_file(json_path, report_path)
                write_combined(combined_folder(json_path))
                detail["text"] = f"{exposure['summary']['flagged_by_validation']} findings flagged by validation"

        except Exception:
            # The sanitized copy and its verdict stand; only the report is missing
            delete([exposure_path(json_path)])
            write_combined(combined_folder(json_path))
            raise

        # The same gate any code sending text to an LLM must use
        try:
            approved_copy(json_path)
            approval = {"approved": True, "reason": None}
        except NotApproved as reason:
            approval = {"approved": False, "reason": str(reason).replace(str(json_path.parent), "output")}

        job.update(result={**result, **approval})
