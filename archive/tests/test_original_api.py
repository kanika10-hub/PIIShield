import io
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import pymupdf
from docx import Document

import app as web
import src.web.jobs as jobs_module
from src.pii.detectors import LocalNLPDetector

# The redacted file through the web interface (app.py): an uploaded
# document runs steps 6 and 7 after the audit, as main.py does, and its
# redacted copy in the same format downloads only through
# approved_file(). Flask's test client, with the job folders in a
# temporary folder (nothing is written under uploads/ or output/).
# Needs the spaCy model and Tesseract.

NAME = "Grace Holloway"
EMAIL = "grace.holloway@example.com"
PHONE = "+44 7700 900481"

nlp = LocalNLPDetector()

if nlp.error:
    sys.exit(f"The spaCy model is needed: {nlp.error}")


def docx_bytes():
    doc = Document()
    doc.add_paragraph(f"This record confirms that {NAME} joined the Risk Analytics team in London.")
    doc.add_paragraph(f"She can be reached at {EMAIL} or on {PHONE}.")
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def pdf_bytes():
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((60, 80), f"This record confirms that {NAME} joined the Risk Analytics team in London.", fontsize=11)
    page.insert_text((60, 96), f"She can be reached at {EMAIL} or on {PHONE}.", fontsize=11)
    return pdf.tobytes()


def upload(client, name, data):
    return client.post("/api/jobs", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def wait(client, job_id):
    for _ in range(1200):
        job = client.get(f"/api/jobs/{job_id}").get_json()

        if job["state"] in ("completed", "failed"):
            return job

        time.sleep(0.1)

    raise TimeoutError(f"job {job_id} did not finish")


def statuses(job):
    return {step["name"]: step["status"] for step in job["steps"]}


def xml_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return "".join(archive.read(name).decode("utf-8") for name in archive.namelist() if name.endswith(".xml"))


with tempfile.TemporaryDirectory() as folder:
    jobs_module.UPLOAD_DIR = Path(folder) / "uploads"
    jobs_module.OUTPUT_DIR = Path(folder) / "output"
    web.jobs.nlp = nlp
    client = web.app.test_client()
    CASES = []

    # --- A DOCX upload: its redacted DOCX is written, checked and served ---
    created = upload(client, "starter.docx", docx_bytes())
    job_id = created.get_json()["job_id"]
    job = wait(client, job_id)
    steps = statuses(job)
    result = job["result"]
    served = client.get(f"/api/jobs/{job_id}/download/redacted")
    served_text = xml_text(served.data) if served.status_code == 200 else ""
    # A served file stays open until its response is closed (Windows will not delete it)
    served.close()

    CASES += [
        ("A DOCX upload is accepted and completes", created.status_code == 202 and job["state"] == "completed"),
        ("The redacted file steps run after the audit", steps["file_redact"] == steps["file_check"] == "done"),
        (
            "The job result says the redacted DOCX may be shared",
            result.get("file_verdict") == "PASS" and result.get("file_approved") is True
            and result.get("file_name") == "starter_redacted.docx",
        ),
        (
            "The redacted DOCX downloads as a Word file without the values",
            served.status_code == 200 and "starter_redacted.docx" in served.headers.get("Content-Disposition", "")
            and "[REDACTED: EMAIL]" in served_text
            and not any(value in served_text for value in (NAME, EMAIL, PHONE)),
        ),
    ]

    # --- A redacted file changed after its check is refused ---
    redacted = jobs_module.OUTPUT_DIR / job_id / "starter_redacted.docx"
    original_bytes = redacted.read_bytes()
    redacted.write_bytes(original_bytes + b" ")
    tampered = client.get(f"/api/jobs/{job_id}/download/redacted")
    redacted.write_bytes(original_bytes)

    CASES.append((
        "A redacted file changed after its check is refused (403, with the reason)",
        tampered.status_code == 403 and "changed since it was checked" in tampered.get_json().get("error", ""),
    ))

    # --- An extracted JSON upload has no original file to redact ---
    response = client.get(f"/api/jobs/{job_id}/download/extracted")
    extracted = response.data
    response.close()
    json_job = wait(client, upload(client, "starter.json", extracted).get_json()["job_id"])
    json_steps = statuses(json_job)
    refused = client.get(f"/api/jobs/{json_job['job_id']}/download/redacted")

    CASES.append((
        "An extracted JSON upload skips the redacted file, which is not served",
        json_job["state"] == "completed" and json_steps["file_redact"] == json_steps["file_check"] == "skipped"
        and json_job["result"].get("file_approved") is False and refused.status_code == 403,
    ))

    # --- A PDF upload: its redacted PDF ---
    pdf_job = wait(client, upload(client, "record.pdf", pdf_bytes()).get_json()["job_id"])
    pdf_served = client.get(f"/api/jobs/{pdf_job['job_id']}/download/redacted")

    if pdf_served.status_code == 200:
        with pymupdf.open(stream=pdf_served.data, filetype="pdf") as served_pdf:
            pdf_text = served_pdf[0].get_text()
    else:
        pdf_text = ""

    pdf_served.close()

    CASES.append((
        "A PDF upload gives a redacted PDF without the values",
        statuses(pdf_job)["file_check"] == "done" and pdf_job["result"].get("file_name") == "record_redacted.pdf"
        and pdf_served.status_code == 200 and "joined the Risk Analytics team" in pdf_text
        and not any(value in pdf_text for value in (NAME, EMAIL, PHONE)),
    ))

    details = {
        "steps": steps, "result": {key: result.get(key) for key in result if key.startswith("file")},
        "pdf steps": statuses(pdf_job), "pdf result": {k: v for k, v in pdf_job["result"].items() if k.startswith("file")},
    }

failures = 0

print("========== ORIGINAL-FORMAT API TEST ==========\n")

for description, passed in CASES:
    failures += not passed
    print(f"{'PASS' if passed else 'FAIL'}  {description}")

if failures:
    for key, value in details.items():
        print(f"        {key}: {value}")

print(f"\n{len(CASES) - failures} of {len(CASES)} cases passed")
print("==============================================")

sys.exit(1 if failures else 0)
