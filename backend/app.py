"""
The web interface: upload a PDF, PPTX or DOCX file, follow its scan step
by step, and review what was found, tagged, redacted and validated.

    python app.py              then open http://127.0.0.1:5000

The server listens on this computer only (127.0.0.1): documents and the
PII in them never leave it, and the page loads nothing from the internet.
Each upload runs the same pipeline as main.py (src/web/jobs.py), in a
job folder of its own: uploads/<job_id>/ and output/<job_id>/.

API, all JSON:

    POST   /api/jobs                        upload a file ("file") and start its job: a
                                            PDF, PPTX or DOCX document, or an extracted
                                            JSON file, which skips extraction
    GET    /api/jobs/<id>                   its progress, and its results when done
    GET    /api/jobs/<id>/document/<copy>   original, tagged or sanitized text
    GET    /api/jobs/<id>/findings          the PII report's findings
    GET    /api/jobs/<id>/validation        verdict, checks, warnings, redaction
    GET    /api/jobs/<id>/download/<file>   extracted, findings, sanitized,
                                            validation, audit-json or audit-md
    GET    /api/jobs/<id>/download/redacted the redacted PDF, PPTX or DOCX file,
                                            only if approved_file() releases it
                                            (403 with the reason otherwise)
    DELETE /api/jobs/<id>                   delete the job's files

    Review of a finished job's validation issues (src/pii/review.py), each
    named by its key; the body is JSON. Each queues the job again (202):

    POST   /api/jobs/<id>/review/hide              {key, reviewer}: hide the
                                                   issue's text
    POST   /api/jobs/<id>/review/hide-all          {reviewer}: hide the text
                                                   of every OCR issue at once
    POST   /api/jobs/<id>/review/sign-off-all      {reviewer}: "checked, all
                                                   fine" for every OCR issue
    DELETE /api/jobs/<id>/review/sign-off-all      undo that
    POST   /api/jobs/<id>/review/sign-offs         {key, reviewer}: "checked,
                                                   it's fine"
    DELETE /api/jobs/<id>/review/sign-offs/<key>   undo a sign-off
"""

import json
import sys
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file

from src.original_format.validator import approved_file
from src.pii.document_scanner import findings_path
from src.pii.exposure_report import exposure_path, markdown
from src.pii.redactor import output_paths
from src.pii.review import ReviewError
from src.pii.validator import NotApproved, review_path, validation_path
from src.web.jobs import InputRejected, JobBusy, Jobs
from src.web.views import document_view, findings_view, validation_view


HOST = "127.0.0.1"
PORT = 5000

# The sample PDF is 19 MB; scanned documents can be larger
MAX_UPLOAD_MB = 100

# Raise with any change to the API the page relies on: a page served
# beside a server still running older code then says to restart it
API_VERSION = 3

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.json.sort_keys = False

jobs = Jobs()

# The page's scripts, styles and requests stay on this server
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


@app.after_request
def secure(response):
    response.headers.update(SECURITY_HEADERS)

    # Responses hold PII: never kept by the browser
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"

    return response


def api_error(message, status):
    return jsonify({"error": message}), status


@app.errorhandler(413)
def too_large(_):
    return api_error(f"The file is larger than {MAX_UPLOAD_MB} MB.", 413)


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return api_error(error.description or "Not found.", 404)

    return error


# --------------------------------------------------
# FILES OF A JOB
# --------------------------------------------------

def job_or_404(job_id):
    job = jobs.get(job_id)

    if job is None:
        abort(404, description="No such scan: it may have been deleted, or the server restarted.")

    return job


def job_files(job):
    json_path = job.json_path
    tagged_path, sanitized_path = output_paths(json_path)

    return {
        "original": json_path,
        "findings": findings_path(json_path),
        "tagged": tagged_path,
        "sanitized": sanitized_path,
        "validation": validation_path(json_path),
        "exposure": exposure_path(json_path),
        "review": review_path(json_path),
    }


def load(path):
    """
    A JSON output file, or None if the job has not written it (yet).
    """

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --------------------------------------------------
# PAGE AND API
# --------------------------------------------------

@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/config")
def config():
    return jsonify({
        "api_version": API_VERSION,
        "max_upload_mb": MAX_UPLOAD_MB,
        "document_types": [".pdf", ".pptx", ".docx"],
        "extracted_types": [".json"],
    })


@app.post("/api/jobs")
def create_job():
    upload = request.files.get("file")

    if upload is None or not upload.filename:
        return api_error("No file was uploaded.", 400)

    try:
        job = jobs.submit(upload)
    except InputRejected as error:
        return api_error(str(error), 400)

    return jsonify(job.to_dict()), 202


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    return jsonify(job_or_404(job_id).to_dict())


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id):
    job_or_404(job_id)

    try:
        deleted = jobs.delete(job_id)
    except OSError as error:
        return api_error(str(error), 500)

    if not deleted:
        return api_error("The scan is still running: its files can be deleted when it finishes.", 409)

    return jsonify({"deleted": job_id})


@app.get("/api/jobs/<job_id>/document/<copy>")
def get_document(job_id, copy):
    files = job_files(job_or_404(job_id))

    if copy not in ("original", "tagged", "sanitized"):
        abort(404, description="Unknown copy: use original, tagged or sanitized.")

    document = load(files[copy])

    if document is None:
        abort(404, description=f"The {copy} text is not available for this scan.")

    return jsonify(document_view(document))


@app.get("/api/jobs/<job_id>/findings")
def get_findings(job_id):
    files = job_files(job_or_404(job_id))
    report = load(files["findings"])

    if report is None:
        abort(404, description="The PII report is not available for this scan.")

    return jsonify(findings_view(report, load(files["exposure"])))


@app.get("/api/jobs/<job_id>/validation")
def get_validation(job_id):
    job = job_or_404(job_id)
    files = job_files(job)
    report = load(files["findings"])

    if report is None:
        abort(404, description="The PII report is not available for this scan.")

    view = validation_view(
        report, load(files["sanitized"]), load(files["validation"]), load(files["exposure"]), load(files["review"])
    )
    view["approved"] = job.to_dict()["result"].get("approved", False)
    view["approval_reason"] = job.to_dict()["result"].get("reason")

    return jsonify(view)


def review_answer(job_id, answer, body=True):
    """
    Apply a reviewer's answer to a job's validation issue: answer(job,
    body). The page then polls the job, queued again, until it finishes.
    """

    job = job_or_404(job_id)

    # JSON only: a form posted from another website is refused
    data = request.get_json(silent=True) if body else {}

    if not isinstance(data, dict):
        return api_error("Send the answer as a JSON object.", 400)

    try:
        answer(job, data)
    except ReviewError as error:
        return api_error(str(error), 400)
    except JobBusy as error:
        return api_error(str(error), 409)

    return jsonify(job.to_dict()), 202


@app.post("/api/jobs/<job_id>/review/hide")
def review_hide(job_id):
    return review_answer(job_id, lambda job, data: jobs.hide(job, str(data.get("key", "")), data.get("reviewer")))


@app.post("/api/jobs/<job_id>/review/hide-all")
def review_hide_all(job_id):
    return review_answer(job_id, lambda job, data: jobs.hide_all(job, data.get("reviewer")))


@app.post("/api/jobs/<job_id>/review/sign-off-all")
def review_sign_off_all(job_id):
    return review_answer(job_id, lambda job, data: jobs.sign_off_all(job, data.get("reviewer")))


@app.delete("/api/jobs/<job_id>/review/sign-off-all")
def review_withdraw_all(job_id):
    return review_answer(job_id, lambda job, _: jobs.withdraw_all(job), body=False)


@app.post("/api/jobs/<job_id>/review/sign-offs")
def review_sign_off(job_id):
    return review_answer(job_id, lambda job, data: jobs.sign_off(job, str(data.get("key", "")), data.get("reviewer")))


@app.delete("/api/jobs/<job_id>/review/sign-offs/<key>")
def review_withdraw(job_id, key):
    return review_answer(job_id, lambda job, _: jobs.withdraw(job, key), body=False)


@app.get("/api/jobs/<job_id>/download/<name>")
def download(job_id, name):
    job = job_or_404(job_id)
    files = job_files(job)
    stem = job.json_path.stem

    if name == "audit-md":
        exposure = load(files["exposure"])

        if exposure is None:
            abort(404, description="The audit report is not available for this scan.")

        exposure["report_file"] = files["exposure"].name
        text = markdown([exposure], f"output/{job.job_id}")

        return Response(
            text, mimetype="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{stem}_audit.md"'}
        )

    # The redacted file leaves only through its release gate
    if name == "redacted":
        try:
            path = approved_file(job.json_path)
        except NotApproved as reason:
            return api_error(
                f"The redacted file may not be shared: {str(reason).replace(str(job.json_path.parent), 'output')}",
                403
            )

        return send_file(path, as_attachment=True, download_name=path.name)

    choices = {
        # Upload it again as an extracted JSON file to rescan without re-extracting
        "extracted": (files["original"], f"{stem}.json"),
        "findings": (files["findings"], f"{stem}_pii.json"),
        "validation": (files["validation"], f"{stem}_validation.json"),
        "audit-json": (files["exposure"], f"{stem}_exposure.json"),
        # The name says so when the copy may not go to an LLM
        "sanitized": (
            files["sanitized"],
            f"{stem}_sanitized.json" if job.to_dict()["result"].get("approved")
            else f"{stem}_sanitized_NOT_APPROVED.json"
        ),
    }

    if name not in choices:
        abort(404, description="Unknown download.")

    path, download_name = choices[name]

    if not path.is_file():
        abort(404, description="That file is not available for this scan.")

    return send_file(path, mimetype="application/json", as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    print(f"PII Exposure Scanner: http://{HOST}:{port}  (this computer only; Ctrl+C to stop)")
    app.run(host=HOST, port=port, debug=False, threaded=True)
