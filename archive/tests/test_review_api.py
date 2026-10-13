
import json
import sys
import tempfile
import time
from pathlib import Path

import app as web
import src.web.jobs as jobs_module
from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import findings_path, save_findings
from src.web.jobs import Job

# The review answers of the web interface (app.py): "Hide it" and
# "Checked, it's fine" on a validation issue, through Flask's test
# client. Each job is registered as an upload would be, on a temporary
# folder (nothing is written under uploads/ or output/), and runs the
# real redaction, validation and audit steps.

nlp = LocalNLPDetector()

if nlp.nlp is None:
    print("spaCy model unavailable:", nlp.error)
    sys.exit(1)

REVIEWER = "KR"


def block(block_id, text, block_type="paragraph", metadata=None):
    return {
        "block_id": block_id, "block_type": block_type, "text": text, "order": 1,
        "metadata": metadata or {}, "bounding_box": None, "children": [],
    }


def document(*blocks):
    return {
        "document_id": "review_api_test", "file_name": "test.pdf", "source_type": "PDF",
        "extraction_method": "native",
        "pages": [{"page_number": 1, "metadata": {}, "blocks": list(blocks)}],
        "metadata": {},
    }


def finding(number, block_id, text, value):
    start = text.index(value)

    return {
        "finding_id": f"F-{number:04d}", "type": "PERSON_NAME", "text": value,
        "start": start, "end": start + len(value), "confidence": 0.9, "detector": "nlp",
        "page_number": 1, "block_id": block_id, "block_type": "paragraph",
        "review_status": "unreviewed", "action": "REDACT",
    }


def report(*findings):
    return {
        "document_id": "review_api_test", "file_name": "test.pdf", "source_type": "PDF",
        "status": "completed", "scan_timestamp": "2026-10-14T00:00:00+00:00",
        "summary": {"total_findings": len(findings), "by_type": {}, "extraction_warnings": []},
        "findings": list(findings),
    }


FIRST = "Approved by Ifeoma Adeyemi on 3 March."
SECOND = "Escalate to Ifeoma Adeyemi before release."

NAME_LEFT = (
    document(block("b1", FIRST), block("b2", SECOND)),
    report(finding(1, "b1", FIRST, "Ifeoma Adeyemi"), finding(2, "b2", SECOND, "Adeyemi")),
)

UNREADABLE = (
    document(
        block("b1", "Passpon TK4820013,", metadata={"ocr_confidence": 44.0}),
        block("b2", "", "image", metadata={"requires_ocr": True}),
    ),
    report(),
)


def register(job_id, original, pii_report):
    """
    A finished job, as the pipeline leaves it after the scan: its
    document and PII report saved, then redacted, validated and audited.
    """

    job = Job(job_id, jobs_module.UPLOAD_DIR / job_id / "test.json")
    job.json_path.parent.mkdir(parents=True)
    job.json_path.write_text(json.dumps(original), encoding="utf-8")
    save_findings(pii_report, findings_path(job.json_path))

    web.jobs.jobs[job_id] = job
    web.jobs.finish(job, nlp)
    job.update(state="completed")

    return job


def wait(client, job_id):
    for _ in range(600):
        job = client.get(f"/api/jobs/{job_id}").get_json()

        if job["state"] in ("completed", "failed"):
            return job

        time.sleep(0.1)

    raise TimeoutError(f"job {job_id} did not finish")


def issue_keys(client, job_id, check_name=None):
    validation = client.get(f"/api/jobs/{job_id}/validation").get_json()["validation"]

    return [
        item["key"]
        for check in validation["checks"]
        for item in check["issues"]
        if item["severity"] == "NEEDS_REVIEW" and check_name in (None, check["name"])
    ]


def post(client, job_id, path, body):
    return client.post(f"/api/jobs/{job_id}/review/{path}", json=body)


# --------------------------------------------------
# CASES
# --------------------------------------------------

def case_sign_off_and_undo(client):
    job_id = "20261014-000001_sign_off_aaaaaa"
    register(job_id, *UNREADABLE)
    first, second = issue_keys(client, job_id)

    form = client.post(f"/api/jobs/{job_id}/review/sign-offs", data="key=x", content_type="application/x-www-form-urlencoded")
    unknown = post(client, job_id, "sign-offs", {"key": "not-a-key", "reviewer": REVIEWER})
    nameless = post(client, job_id, "sign-offs", {"key": first, "reviewer": " "})

    # Held behind a running job, so the second answer finds this one queued
    with web.jobs.run_lock:
        queued = post(client, job_id, "sign-offs", {"key": first, "reviewer": REVIEWER})
        busy = post(client, job_id, "sign-offs", {"key": second, "reviewer": REVIEWER})

    half = wait(client, job_id)
    post(client, job_id, "sign-offs", {"key": second, "reviewer": REVIEWER})
    both = wait(client, job_id)

    validation = client.get(f"/api/jobs/{job_id}/validation").get_json()["validation"]
    signed = [item["signed_off"] for check in validation["checks"] for item in check["issues"] if item.get("signed_off")]
    ocr = next(check for check in validation["checks"] if check["name"] == "ocr")
    download = client.get(f"/api/jobs/{job_id}/download/sanitized")

    undo = client.delete(f"/api/jobs/{job_id}/review/sign-offs/{second}")
    undone = wait(client, job_id)

    return [
        form.status_code == 400, unknown.status_code == 400, nameless.status_code == 400,
        queued.status_code == 202, busy.status_code == 409,
        half["state"] == "completed" and not half["result"]["approved"],
        half["result"]["final_verdict"] == "NEEDS_REVIEW",
        both["result"]["approved"] and both["result"]["review"]["after_review"],
        # The validation report keeps its verdict; the review makes it PASS
        both["result"]["verdict"] == "NEEDS_REVIEW" and both["result"]["final_verdict"] == "PASS",
        validation["final_verdict"] == "PASS" and ocr["status"] == "NEEDS_REVIEW" and ocr["final_status"] == "PASS",
        [item["reviewer"] for item in signed] == [REVIEWER, REVIEWER],
        "_NOT_APPROVED" not in download.headers.get("Content-Disposition", ""),
        undo.status_code == 202 and not undone["result"]["approved"],
        undone["result"]["final_verdict"] == "NEEDS_REVIEW",
    ]


def case_hide_leftover_name(client):
    job_id = "20261014-000002_hide_bbbbbb"
    register(job_id, *NAME_LEFT)
    before = client.get(f"/api/jobs/{job_id}").get_json()
    key = issue_keys(client, job_id, "known_values")[0]

    hidden = post(client, job_id, "hide", {"key": key, "reviewer": REVIEWER})
    after = wait(client, job_id)
    findings = client.get(f"/api/jobs/{job_id}/findings").get_json()["findings"]
    manual = [item for item in findings if item["finding_id"].startswith("M-")]
    steps = {step["name"]: step["status"] for step in after["steps"]}

    return [
        before["result"]["verdict"] == "NEEDS_REVIEW" and not before["result"]["approved"],
        hidden.status_code == 202,
        after["state"] == "completed" and after["result"]["verdict"] == "PASS" and after["result"]["approved"],
        after["result"]["findings_total"] == 3,
        [(item["value"], item["review_status"], item["detector"]) for item in manual] == [("Ifeoma", "confirmed", "Manual review")],
        steps["redact"] == steps["validate"] == steps["audit"] == "done",
    ]


def case_hide_unreadable_block(client):
    # The unread image is checked first; hiding the badly read block keeps that check
    job_id = "20261014-000003_hide_ocr_cccccc"
    register(job_id, *UNREADABLE)
    block_key, image_key = issue_keys(client, job_id, "ocr")
    issues = client.get(f"/api/jobs/{job_id}/validation").get_json()["validation"]["checks"]
    can_hide = {item["key"]: item["can_hide"] for check in issues for item in check["issues"]}

    post(client, job_id, "sign-offs", {"key": image_key, "reviewer": REVIEWER})
    wait(client, job_id)
    hidden = post(client, job_id, "hide", {"key": block_key, "reviewer": REVIEWER})
    after = wait(client, job_id)
    findings = client.get(f"/api/jobs/{job_id}/findings").get_json()["findings"]
    manual = [item for item in findings if item["finding_id"].startswith("M-")]

    return [
        can_hide == {block_key: True, image_key: False},
        hidden.status_code == 202,
        [item["type"] for item in manual] == ["UNREADABLE_TEXT"],
        after["state"] == "completed" and after["result"]["verdict"] == "NEEDS_REVIEW",
        after["result"]["approved"] and after["result"]["final_verdict"] == "PASS",
        issue_keys(client, job_id) == [image_key],
    ]


def case_hide_all(client):
    # One badly read block and an image never read: "Hide all OCR issues"
    # hides the block; the image is left for a person
    job_id = "20261014-000004_hide_all_dddddd"
    register(job_id, *UNREADABLE)
    before = client.get(f"/api/jobs/{job_id}/validation").get_json()["review"]

    nameless = post(client, job_id, "hide-all", {"reviewer": " "})
    hidden = post(client, job_id, "hide-all", {"reviewer": REVIEWER})
    after = wait(client, job_id)
    progress = client.get(f"/api/jobs/{job_id}/validation").get_json()["review"]
    again = post(client, job_id, "hide-all", {"reviewer": REVIEWER})

    return [
        before["ocr_hideable"] == 1 and before["open"] == 2 and before["hidden"] == [],
        nameless.status_code == 400,
        hidden.status_code == 202,
        after["state"] == "completed" and after["result"]["verdict"] == "NEEDS_REVIEW",
        [(entry["block_id"], entry["types"], entry["added_by"]) for entry in progress["hidden"]] == [("b1", ["UNREADABLE_TEXT"], REVIEWER)],
        progress["open"] == 1 and progress["checked"] == 0 and progress["ocr_hideable"] == 0,
        # Nothing left to hide
        again.status_code == 400,
    ]


def case_hide_all_passes_and_approves(client):
    # Only badly read OCR text: one click makes validation PASS and the copy approved
    job_id = "20261014-000005_hide_all_pass_eeeeee"
    register(job_id, document(block("b1", "Passpon TK4820013,", metadata={"ocr_confidence": 44.0})), report())
    before = client.get(f"/api/jobs/{job_id}").get_json()

    post(client, job_id, "hide-all", {"reviewer": REVIEWER})
    after = wait(client, job_id)
    view = client.get(f"/api/jobs/{job_id}/validation").get_json()
    steps = {step["name"]: step["status"] for step in after["steps"]}

    return [
        before["result"]["verdict"] == "NEEDS_REVIEW" and not before["result"]["approved"],
        after["state"] == "completed",
        after["result"]["verdict"] == "PASS" and after["result"]["final_verdict"] == "PASS",
        after["result"]["approved"] is True,
        view["validation"]["verdict"] == "PASS" and view["review"]["open"] == 0,
        len(view["review"]["hidden"]) == 1,
        steps["redact"] == steps["validate"] == steps["audit"] == "done",
    ]


def case_check_all(client):
    # "Checked, all fine" for the badly read block, then the image on its own:
    # approved. Undo takes the OCR answer back
    job_id = "20261014-000006_check_all_ffffff"
    register(job_id, *UNREADABLE)
    issues = [
        item
        for check in client.get(f"/api/jobs/{job_id}/validation").get_json()["validation"]["checks"]
        for item in check["issues"]
    ]
    image_key = next(item["key"] for item in issues if not item["unreadable"])

    nameless = post(client, job_id, "sign-off-all", {"reviewer": ""})
    checked = post(client, job_id, "sign-off-all", {"reviewer": REVIEWER})
    half = wait(client, job_id)
    post(client, job_id, "sign-offs", {"key": image_key, "reviewer": REVIEWER})
    both = wait(client, job_id)
    undo = client.delete(f"/api/jobs/{job_id}/review/sign-off-all")
    undone = wait(client, job_id)

    return [
        [item["unreadable"] for item in issues] == [True, False],
        nameless.status_code == 400 and checked.status_code == 202,
        not half["result"]["approved"],
        both["result"]["approved"] and both["result"]["final_verdict"] == "PASS",
        undo.status_code == 202 and not undone["result"]["approved"],
    ]


def case_unknown_job(client):
    return [post(client, "no-such-job", "hide", {"key": "x", "reviewer": REVIEWER}).status_code == 404]


with tempfile.TemporaryDirectory() as folder:
    jobs_module.UPLOAD_DIR = Path(folder) / "uploads"
    jobs_module.OUTPUT_DIR = Path(folder) / "output"
    web.jobs.nlp = nlp
    client = web.app.test_client()

    CASES = [
        ("Signing every issue off approves the copy; undo blocks it again", case_sign_off_and_undo),
        ("\"Hide it\" hides the leftover first name, runs again and passes", case_hide_leftover_name),
        ("\"Hide it\" hides a badly read OCR block and keeps the earlier check", case_hide_unreadable_block),
        ("\"Hide all OCR issues\" hides every badly read block in one run", case_hide_all),
        ("With only OCR issues, one click makes validation PASS and the copy approved", case_hide_all_passes_and_approves),
        ("\"Checked, all fine\" answers every OCR issue at once; Undo takes it back", case_check_all),
        ("An answer for an unknown scan is refused", case_unknown_job),
    ]

    failures = 0

    print("========== REVIEW API TEST ==========\n")

    for description, case in CASES:
        checks = case(client)
        passed = all(checks)
        failures += not passed

        print(f"{'PASS' if passed else 'FAIL'}  {description}")

        if not passed:
            print(f"        failed checks: {[number for number, ok in enumerate(checks, start=1) if not ok]}")

    print(f"\n{len(CASES) - failures} of {len(CASES)} cases passed")
    print("=====================================")

sys.exit(1 if failures else 0)
