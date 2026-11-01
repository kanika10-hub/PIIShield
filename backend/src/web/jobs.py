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
    file_redact <name>_redacted.<ext>, <name>_file_redaction.json
                                                     redact_original, from the stored upload
    file_check  <name>_file_validation.json          validate_original; for a PDF whose scans
                                                     still show PII, file_redact and file_check
                                                     again with those places (another_pass)
    then        approved_copy: may the sanitized copy go to an LLM?
                approved_file: may the redacted file be shared?

If extraction, the format check or the scan fails, the PII report is
written with status "failed" and nothing is tagged or redacted. If
redaction or validation fails, the files they wrote are deleted. A step
that fails stops the job, and the steps after it are "skipped". The
redacted file's steps are the exception, as in main.py: if they fail,
their files are deleted and the job completes without the file. They
are skipped for an extracted JSON upload, which has no original file.

A finished job can then be reviewed, one validation issue at a time
(src/pii/review.py):

    hide        add the issue's text (leftover name parts, unreadable OCR
                text) to the PII report as manual findings, delete
                <name>_review.json (its sign-offs were for the old copy),
                redact and validate again, give the new validation the
                sign-offs of the issues it still holds unchanged
                (review.kept_review), then audit, and write and check
                the redacted file again
    hide all    the same at once for every OCR issue that can be hidden
                and was not signed off
    sign off all
                sign off, or withdraw, every unreadable OCR issue at once
    sign off    save or withdraw a sign-off in <name>_review.json, then
                audit again: the exposure report and the release gate
                read it

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
    OUTPUT_DIR, SUPPORTED_TYPES, UPLOAD_DIR, delete, extract_document, file_outputs, format_summary,
    input_problem, load_extracted, pipeline_outputs
)
from src.original_format.redactor import WRITERS, redact_original
from src.original_format.validator import another_pass, approved_file, file_validation_path, validate_original
from src.models.schema import format_problems
from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import failed_report, findings_path, save_findings, scan_file
from src.pii.exposure_report import combined_folder, exposure_file, exposure_path, write_combined
from src.pii.redactor import redact_file
from src.pii.review import (
    hidden_all_report, hidden_report, kept_review, save_review, signed_off_all_review,
    signed_off_review, standing_sign_offs, withdrawn_all_review, withdrawn_review
)
from src.pii.validator import (
    PASS, NotApproved, approved_copy, load_review, review_approval, review_path,
    validate_file, validation_path
)
from src.uploads import store_file


# The steps the page shows, in order
STEPS = [
    ("extract", "Text extraction"),
    ("check", "Extraction check"),
    ("scan", "PII detection"),
    ("redact", "Tagging and sanitization"),
    ("validate", "Validation"),
    ("audit", "Audit report"),
    ("file_redact", "Redacted file"),
    ("file_check", "Redacted file check"),
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


class JobBusy(Exception):
    """
    The job is still running: a review can be applied once it finishes.
    """


# The steps a reviewer's answer runs again
AFTER_HIDING = ["redact", "validate", "audit", "file_redact", "file_check"]
AFTER_SIGN_OFF = ["audit"]


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

    def start_review(self, steps):
        """
        Queue a finished job again for a reviewer's answer, with the steps
        it runs again back to pending. False if the job is still running.
        """

        with self.lock:
            if self.state not in ("completed", "failed"):
                return False

            self.state = "queued"
            self.error = None

            for name in steps:
                self.steps[name]["status"] = "pending"
                self.steps[name]["detail"] = None

            return True

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


def approval(json_path):
    """
    What the release gates say of the job's sanitized copy (approved_copy,
    the same gate any code sending text to an LLM must use) and of its
    redacted file (approved_file), and how far the review of the copy's
    issues has got.
    """

    json_path = Path(json_path)

    try:
        approved_copy(json_path)
        result = {"approved": True, "reason": None}
    except NotApproved as reason:
        result = {"approved": False, "reason": str(reason).replace(str(json_path.parent), "output")}

    result.update(file_approval(json_path))

    try:
        validation = json.loads(validation_path(json_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return result

    review = review_approval(validation, load_review(json_path))

    return {
        **result,
        # PASS once the gate lets the copy go, through sign-offs too; the
        # validation report keeps its own verdict
        "final_verdict": PASS if result["approved"] else validation.get("verdict"),
        "review": {
            "after_review": result["approved"] and review["after_review"],
            "signed_off": review["total"] - review["open"],
            "needing_review": review["total"],
        },
    }


def file_approval(json_path):
    """
    What approved_file says of the job's redacted file, and its check's
    verdict (None if it was not checked).
    """

    try:
        verdict = json.loads(file_validation_path(json_path).read_text(encoding="utf-8")).get("verdict")
    except (OSError, ValueError):
        verdict = None

    try:
        name = approved_file(json_path).name
        return {"file_verdict": verdict, "file_approved": True, "file_name": name, "file_reason": None}
    except NotApproved as reason:
        return {
            "file_verdict": verdict, "file_approved": False, "file_name": None,
            "file_reason": str(reason).replace(str(json_path.parent), "output"),
        }


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
        # One review answer is checked and queued at a time
        self.review_lock = threading.Lock()
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

        self.finish(job, nlp)

    def finish(self, job, nlp, sign_offs=None):
        """
        Redact, validate and audit: the steps after the scan, and again
        after a reviewer hides text. sign_offs: after hiding, those given
        to the old copy, kept for the issues the new validation still
        holds unchanged (review.kept_review).
        """

        json_path = job.json_path
        outputs = pipeline_outputs(json_path)
        report_path = outputs[0]

        # The redacted file was checked beside the copy about to be replaced
        delete(file_outputs(json_path))

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

        job.update(result={**job.result, "verdict": validation["verdict"], "checks": validation["summary"]})

        kept = kept_review(sign_offs, validation)

        if kept:
            save_review(json_path, kept)

        self.audit(job)
        self.redacted_file(job, nlp)

    def redacted_file(self, job, nlp):
        """
        Steps 6-7 of main.py: the redacted file in its own format, then
        its check. If they fail, their files are deleted and the job
        completes: the sanitized copy and its reports stand.
        """

        json_path = job.json_path
        suffix = job.stored_path.suffix.lower()

        if job.extracted or suffix not in WRITERS:
            reason = (
                "Not available: the upload is an extracted JSON file, not the document" if job.extracted
                else f"Not available yet for {suffix.lstrip('.').upper()} files"
            )
            job.skip("file_redact", reason)
            job.skip("file_check", reason)

        else:
            written = False

            try:
                with job.step("file_redact") as detail:
                    record, _, _ = redact_original(json_path, job.stored_path)
                    placed = record["summary"]
                    detail["text"] = (
                        f"{placed['redacted']} of {placed['findings']} findings placed in the "
                        f"{suffix.lstrip('.').upper()} file ({record['status']})"
                    )

                written = True

                with job.step("file_check") as detail:
                    file_validation, _ = validate_original(json_path, nlp)

                    # A scan read again can show what its first reading missed: those places too
                    while leftovers := another_pass(file_validation):
                        redact_original(json_path, job.stored_path, leftovers=leftovers)
                        file_validation, _ = validate_original(json_path, nlp)

                    passes = file_validation["redaction_pass"]
                    detail["text"] = (
                        file_validation["verdict"].replace("_", " ") + (f" (after {passes} passes)" if passes > 1 else "")
                    )

            except Exception:
                # An unchecked or part-made file must not be mistaken for a checked one
                delete(file_outputs(json_path))

                if not written:
                    job.skip("file_check", "Not run: the redacted file could not be written")

        job.update(result={**job.result, **approval(json_path)})

    def audit(self, job):
        """
        The exposure report, then the release gate: after the validation,
        and again after a reviewer signs an issue off.
        """

        json_path = job.json_path

        try:
            with job.step("audit") as detail:
                exposure, _ = exposure_file(json_path, findings_path(json_path))
                write_combined(combined_folder(json_path))
                detail["text"] = f"{exposure['summary']['flagged_by_validation']} findings flagged by validation"

        except Exception:
            # The sanitized copy and its verdict stand; only the report is missing
            delete([exposure_path(json_path)])
            write_combined(combined_folder(json_path))
            raise

        job.update(result={**job.result, **approval(json_path)})

    # --------------------------------------------------
    # REVIEW
    # --------------------------------------------------

    def hide(self, job, key, reviewer):
        """
        "Hide it": add the text of the issue with this key (leftover
        name parts, unreadable OCR text) as manual findings, then redact,
        validate and audit again. Raises ReviewError (the answer cannot
        be applied) or JobBusy.
        """

        with self.review_lock:
            self.check_free(job)
            report, _ = hidden_report(job.json_path, key, reviewer)
            sign_offs = standing_sign_offs(job.json_path)
            self.start_review(job, AFTER_HIDING, lambda: self.apply_hidden(job, report, sign_offs))

    def hide_all(self, job, reviewer):
        """
        "Hide all OCR issues": add the text of every OCR issue "Hide it"
        can fix, and no reviewer signed off, as manual findings, then
        redact, validate and audit once. Raises ReviewError or JobBusy.
        """

        with self.review_lock:
            self.check_free(job)
            report, _, _ = hidden_all_report(job.json_path, reviewer)
            sign_offs = standing_sign_offs(job.json_path)
            self.start_review(job, AFTER_HIDING, lambda: self.apply_hidden(job, report, sign_offs))

    def apply_hidden(self, job, report, sign_offs):
        """
        Save a PII report with hidden text added, then run the steps after
        the scan again, keeping the sign-offs of unchanged issues.
        """

        save_findings(report, findings_path(job.json_path))
        # Its sign-offs were for the copy about to be replaced: finish
        # gives back those of the issues that come back unchanged
        delete([review_path(job.json_path)])

        summary = report["summary"]
        job.update(result={
            **job.result, "findings_total": summary["total_findings"], "by_type": summary["by_type"]
        })

        self.finish(job, self.detector(), sign_offs)

    def sign_off(self, job, key, reviewer):
        """
        "Checked, it's fine": sign the issue with this key off, then audit
        again. Raises ReviewError or JobBusy.
        """

        with self.review_lock:
            self.check_free(job)
            review = signed_off_review(job.json_path, key, reviewer)
            self.start_review(job, AFTER_SIGN_OFF, lambda: self.save_and_audit(job, review))

    def withdraw(self, job, key):
        """
        Undo the sign-off of the issue with this key, then audit again.
        """

        with self.review_lock:
            self.check_free(job)
            review = withdrawn_review(job.json_path, key)
            self.start_review(job, AFTER_SIGN_OFF, lambda: self.save_and_audit(job, review))

    def sign_off_all(self, job, reviewer):
        """
        "Checked, all fine": sign off every unreadable OCR issue at once,
        then audit again. Raises ReviewError or JobBusy.
        """

        with self.review_lock:
            self.check_free(job)
            review = signed_off_all_review(job.json_path, reviewer)
            self.start_review(job, AFTER_SIGN_OFF, lambda: self.save_and_audit(job, review))

    def withdraw_all(self, job):
        """
        Undo the sign-offs of every unreadable OCR issue, then audit again.
        """

        with self.review_lock:
            self.check_free(job)
            review = withdrawn_all_review(job.json_path)
            self.start_review(job, AFTER_SIGN_OFF, lambda: self.save_and_audit(job, review))

    def save_and_audit(self, job, review):
        save_review(job.json_path, review)
        self.audit(job)

    @staticmethod
    def check_free(job):
        if not job.finished():
            raise JobBusy("The scan is still running: try again when it finishes.")

    def start_review(self, job, steps, apply):
        """
        Run a reviewer's answer (apply) on a finished job, in the
        background and after any job already running.
        """

        if not job.start_review(steps):
            raise JobBusy("The scan is still running: try again when it finishes.")

        threading.Thread(target=self.run_review, args=(job, apply), daemon=True).start()

    def run_review(self, job, apply):
        with self.run_lock:
            job.update(state="running")

            try:
                apply()
                job.update(state="completed")

            except Exception as error:
                job.fail(error)
