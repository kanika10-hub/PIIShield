import hashlib
import json
import sys
import tempfile
from pathlib import Path

from src.uploads import UPLOAD_RECORD, store_upload, stored_job_id

# The local copy main.py keeps of each file it is given (src/uploads.py).
# Every case works in its own temporary folder: nothing in uploads/ or
# output/ is touched.


def make_file(folder, name, data):
    path = Path(folder) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def case_copy_and_record():
    with tempfile.TemporaryDirectory() as folder:
        data = b"%PDF-1.7 example"
        source = make_file(folder, "source/Risk Policy.pdf", data)
        upload_dir = Path(folder) / "uploads"

        job_id, stored = store_upload(source, upload_dir)
        record = json.loads((stored.parent / UPLOAD_RECORD).read_text(encoding="utf-8"))

        return [
            stored == upload_dir / job_id / "Risk Policy.pdf",
            stored.read_bytes() == data,
            source.read_bytes() == data,
            record["job_id"] == job_id,
            record["file_name"] == "Risk Policy.pdf",
            record["original_path"] == str(source.resolve()),
            record["size_bytes"] == len(data),
            record["sha256"] == hashlib.sha256(data).hexdigest(),
        ]


def case_same_name_kept_apart():
    with tempfile.TemporaryDirectory() as folder:
        first = make_file(folder, "a/report.pdf", b"%PDF- first")
        second = make_file(folder, "b/report.pdf", b"%PDF- second")
        upload_dir = Path(folder) / "uploads"

        first_job, first_copy = store_upload(first, upload_dir)
        second_job, second_copy = store_upload(second, upload_dir)
        again_job, _ = store_upload(first, upload_dir)

        return [
            len({first_job, second_job, again_job}) == 3,
            first_copy.read_bytes() == b"%PDF- first",
            second_copy.read_bytes() == b"%PDF- second",
            first_copy.name == second_copy.name == "report.pdf",
        ]


def case_job_id_is_safe():
    with tempfile.TemporaryDirectory() as folder:
        upload_dir = Path(folder) / "uploads"
        names = ["Q3 review (final) – Ünïcode.docx", "§§ ¶¶.docx", f"{'x' * 200}.pptx"]
        jobs = [store_upload(make_file(folder, name, b"PK"), upload_dir)[0] for name in names]

        date, time_and_name = jobs[0].split("-", 1)

        return [
            all(set(job) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-") for job in jobs),
            date.isdigit() and len(date) == 8,
            "Q3_review_final_" in jobs[0],
            "_document_" in jobs[1],
            len(jobs[2]) < 70,
        ]


def case_stored_copy_recognised():
    with tempfile.TemporaryDirectory() as folder:
        source = make_file(folder, "deck.pptx", b"PK")
        upload_dir = Path(folder) / "uploads"
        job_id, stored = store_upload(source, upload_dir)

        # Loose in uploads/, not in a job folder: a new upload
        loose = make_file(upload_dir, "loose.pptx", b"PK")
        # A job folder without its record was not made by store_upload
        unrecorded = make_file(upload_dir / "made_by_hand", "deck.pptx", b"PK")

        return [
            stored_job_id(stored, upload_dir) == job_id,
            stored_job_id(source, upload_dir) is None,
            stored_job_id(loose, upload_dir) is None,
            stored_job_id(unrecorded, upload_dir) is None,
        ]


CASES = [
    ("The copy has the original name and bytes; upload.json records it", case_copy_and_record),
    ("Files with the same name get separate job folders", case_same_name_kept_apart),
    ("Job IDs are safe folder names, whatever the file is called", case_job_id_is_safe),
    ("A stored copy given again is recognised; other files are not", case_stored_copy_recognised),
]

failures = 0

print("========== UPLOADS TEST ==========\n")

for description, case in CASES:
    checks = case()
    passed = all(checks)
    failures += not passed

    print(f"{'PASS' if passed else 'FAIL'}  {description}")

    if not passed:
        print(f"        failed checks: {[number for number, ok in enumerate(checks, start=1) if not ok]}")

print(f"\n{len(CASES) - failures} of {len(CASES)} cases passed")
print("==================================")

sys.exit(1 if failures else 0)
