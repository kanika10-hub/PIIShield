"""
Keep a local copy of each file given to main.py, so a run depends only
on the file itself: not on its name, or on where it was or still is.

    store_upload(path, upload_dir)      -> (job_id, stored_path)
    store_file(name, original, write, upload_dir)
                                        -> (job_id, stored_path), for bytes
                                           received by the web interface
    stored_job_id(path, upload_dir)     -> job_id of a stored copy, or None

Each file is copied into a new folder of its own, under its original
name, with upload.json beside it:

    uploads/<job_id>/<file name>
    uploads/<job_id>/upload.json        job_id, file_name, original_path,
                                        size_bytes, sha256, uploaded_at

The job ID is the upload time, the file name and a random suffix, e.g.
20261012-143005_risk_policy_3f9a1c: unique, and sorted by upload time.
main.py writes the outputs of a job to output/<job_id>/, so two files
with the same name (report.pdf and report.docx, or two versions of one
report) never overwrite each other's outputs, and the sha256 ties the
outputs to the exact bytes that were extracted.

The copy keeps the original file name because the extractors take the
document's document_id and file_name from it.

Uploaded files hold the PII the pipeline looks for: uploads/ is not
committed (see .gitignore), and a job's folders should be deleted when
they are no longer needed.
"""

import hashlib
import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path


UPLOAD_RECORD = "upload.json"

# The file name in a job ID: other characters become "_", and long names
# are cut, so the job folder name is safe and short on every system
UNSAFE_CHARACTERS = re.compile(r"[^A-Za-z0-9.-]+")
MAX_NAME_CHARS = 40


def new_job_id(file_name, now):

    name = UNSAFE_CHARACTERS.sub("_", Path(file_name).stem).strip("._")[:MAX_NAME_CHARS]

    return f"{now:%Y%m%d-%H%M%S}_{name or 'document'}_{uuid.uuid4().hex[:6]}"


def store_upload(source_path, upload_dir):
    """
    Copy a file into a new folder of its own under upload_dir and record
    where it came from. Returns the job ID and the path of the copy.
    """

    source_path = Path(source_path)

    return store_file(
        source_path.name, str(source_path.resolve()),
        lambda stored_path: shutil.copy2(source_path, stored_path), upload_dir
    )


def store_file(file_name, original_path, write, upload_dir):
    """
    Store a file under file_name in a new folder of its own under
    upload_dir: write(path) writes its bytes there. original_path says
    where it came from (a path, or the web interface). Returns the job
    ID and the path of the stored file.
    """

    now = datetime.now().astimezone()

    job_id = new_job_id(file_name, now)
    folder = Path(upload_dir) / job_id
    folder.mkdir(parents=True)

    stored_path = folder / file_name
    write(stored_path)

    # Of the copy: the bytes every later step reads
    with open(stored_path, "rb") as file:
        sha256 = hashlib.file_digest(file, "sha256").hexdigest()

    record = {
        "job_id": job_id,
        "file_name": file_name,
        "original_path": original_path,
        "size_bytes": stored_path.stat().st_size,
        "sha256": sha256,
        "uploaded_at": now.isoformat(timespec="seconds"),
    }

    with open(folder / UPLOAD_RECORD, "w", encoding="utf-8") as file:
        json.dump(record, file, indent=2, ensure_ascii=False)

    return job_id, stored_path


def stored_job_id(path, upload_dir):
    """
    The job ID of a file store_upload() already stored, given again to
    run it again, or None for any other file.
    """

    path = Path(path).resolve()
    folder = path.parent

    if folder.parent == Path(upload_dir).resolve() and (folder / UPLOAD_RECORD).is_file():
        return folder.name

    return None
