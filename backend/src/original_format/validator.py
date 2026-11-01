"""
Step 7: check the redacted file (redactor.py) before it is released.

    python -m src.original_format.validator output/<job_id>/<name>.json

reads the extracted document <name>.json, its PII report, its sanitized
copy, the record <name>_file_redaction.json and the redacted file, and
writes <name>_file_validation.json. Checks, in order:

    redaction       the record belongs to this document and scan, lists
                    no finding that could not be placed, and the file is
                    byte for byte the one it describes
    text            the file, read again by its extractor, says what the
                    sanitized copy says: the same blocks, with the same
                    text or more redaction tags (images are re-read, so
                    only their presence is compared). A PDF read again
                    does not keep its blocks, so each page with a text
                    layer is compared whole, word by word, tags left out;
                    its scanned pages are read again by OCR
    rescan          the scanner finds no PII in the file read again, its
                    images and scanned pages OCR'd again. Filling part of
                    a line can change how OCR reads the rest of it, and
                    OCR reads the tags in the filled areas: a finding only
                    such noise made (misreadings()) is noted, not failed
    known_values    no value the scan found is anywhere in the file: every
                    paragraph, text and attribute of every XML file in it,
                    embedded packages included, or every place a PDF
                    keeps text (inspect.py), and the OCR text of its
                    images and scanned pages. Part of a name is NEEDS_REVIEW,
                    unless the sanitized copy holds it too: its review
                    answered for it
    unread          images no block shows (the SVG version of a picture,
                    a watermark), and images that could not be read
                    again: nothing has looked at their pixels

The verdict is the worst of the checks, as in src/pii/validator.py. On
FAIL the redacted file is deleted. Issues name the block, finding and
file part, never the PII itself.

Reading a redacted scan again can find what the first reading missed:
text OCR read badly, or not at all, before. For a PDF the check records
where on the page each such leftover lies (leftovers: page, box and
type, never the text) when every issue left lies where OCR read it.
another_pass() hands them to the redactor (redactor.redact_original),
which redacts the file again from the original with them, at most
MAX_PASSES times in all.

approved_file() is the way to release the file: only a PASS file,
unchanged since it was checked, whose sanitized copy approved_copy()
approves. The copy's review covers what the file shares with it:
images never read, embedded objects, low-confidence OCR.
"""

import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

from src.extractors.dispatch import extract_document, extract_pdf_document
from src.original_format.images import PAD, box_of, covered_words, line_boxes, span_areas
from src.original_format.inspect import file_contents
from src.original_format.pdf_file import PIXEL_PAD, image_placement, padded
from src.original_format.redactor import record_path, redacted_path, sha256
from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import LOW_OCR_WORD_CONFIDENCE, findings_path
from src.pii.redactor import REDACTION_TAG, output_paths, save_json, text_slots, walk
from src.pii.validator import (
    EXIT_CODES, FAIL, MAX_ISSUES, MAX_LOCATIONS, NEEDS_REVIEW, PASS, SEVERITY, NotApproved,
    alternation, approved_copy, check_rescan, issue, normalise, only_tags_differ, print_validation,
    searched_values, strings, validation_path, worst
)


# Change whenever the checks or the release rule change
FILE_VALIDATOR_VERSION = "1.3.0"

IMAGE_TYPES = ("image", "image_ocr")

# A word read again lies in an area the redactor filled when this share
# of its box is inside the area
INSIDE_SHARE = 0.8

# How many times in all a PDF is redacted (the first time, then again
# with the leftovers its check found): each pass reads its scans again
MAX_PASSES = 3


def file_validation_path(json_path):
    json_path = Path(json_path)

    return json_path.with_name(f"{json_path.stem}_file_validation.json")


def is_image(holder, key):
    return key == "text" and holder.get("block_type") in IMAGE_TYPES


# --------------------------------------------------
# CHECKS
# Each returns (issues, notes).
# --------------------------------------------------

def check_record(document, report, record, content):

    issues = []

    for field in ("document_id", "file_name"):
        if record.get(field) != document.get(field):
            issues.append(issue(FAIL, f"the redaction record is for {field} {record.get(field)!r}"))

    if record.get("report_scan_timestamp") != report.get("scan_timestamp"):
        issues.append(issue(FAIL, "the file was redacted from another scan of the document"))

    if sha256_of(content) != record.get("files", {}).get("redacted_sha256"):
        issues.append(issue(FAIL, "the redacted file has changed since it was written"))

    for error in record.get("errors", []):
        issues.append(issue(
            FAIL, f"the finding was not redacted in the file: {error['reason']}",
            block_id=error.get("block_id"), finding_id=error.get("finding_id")
        ))

    summary = record.get("summary", {})

    return issues, [
        f"{summary.get('redacted', 0)} findings redacted, {summary.get('images_redacted', 0)} images changed, "
        f"{summary.get('values_swept', 0)} values replaced in other places"
    ]


def check_text(sanitized, refetched):
    """
    The file read again against the sanitized copy, text slot by text
    slot (blocks, comment authors, file properties). A PDF's pages are
    compared whole (check_pdf_text).
    """

    if sanitized.get("source_type") == "PDF":
        return check_pdf_text(sanitized, refetched)

    return check_slots(text_slots(sanitized), text_slots(refetched))


def check_slots(expected, actual):
    """
    Text slots (slot_id -> (holder, key)) of the file read again against
    the sanitized copy's.
    """

    issues = []
    more = 0

    for slot_id, (holder, key) in expected.items():
        if slot_id not in actual:
            issues.append(issue(FAIL, "missing from the redacted file", block_id=slot_id))
            continue

        if is_image(holder, key):
            continue

        want = normalise(holder.get(key) or "")
        got = normalise(actual[slot_id][0].get(actual[slot_id][1]) or "")

        if want == got:
            continue

        if only_tags_differ(want, got):
            more += 1
        else:
            issues.append(issue(FAIL, "the redacted file's text is not the sanitized copy's", block_id=slot_id))

    for slot_id in actual.keys() - expected.keys():
        issues.append(issue(FAIL, "in the redacted file but not in the sanitized copy", block_id=slot_id))

    notes = [f"{len(expected)} texts compared"]

    if more:
        notes.append(f"{more} texts hold more redaction tags than the sanitized copy: a value found elsewhere")

    return issues, notes


def page_words(page):
    """
    The words of a page's text blocks, redaction tags left out.
    """

    words = Counter()

    for block in walk(page["blocks"]):
        if block["block_type"] not in ("word", "table", *IMAGE_TYPES):
            words.update(REDACTION_TAG.sub(" ", block.get("text") or "").split())

    return words


def check_pdf_text(sanitized, refetched):
    """
    A PDF read again does not keep its blocks: removing a value can
    regroup a page's words. Each page with a text layer is compared
    whole: its words, redaction tags left out, must be the sanitized
    copy's. A scanned page is read again by OCR, whose reading differs
    by its nature: rescan and known_values check it. File properties are
    compared as text slots.
    """

    want_pages, got_pages = sanitized["pages"], refetched["pages"]

    if len(want_pages) != len(got_pages):
        return [issue(FAIL, f"the redacted file has {len(got_pages)} pages, the sanitized copy {len(want_pages)}")], []

    issues = []
    scanned = 0

    for want_page, got_page in zip(want_pages, got_pages):
        if want_page["metadata"].get("extraction_method") == "ocr":
            scanned += 1
            continue

        want, got = page_words(want_page), page_words(got_page)

        for words, reason in (
            (got - want, "text on this page of the redacted file is not in the sanitized copy"),
            (want - got, "text of the sanitized copy is missing from this page of the redacted file"),
        ):
            if words:
                issues.append(issue(
                    FAIL, reason, page_number=want_page["page_number"], occurrences=sum(words.values())
                ))

    def properties(document):
        return {slot_id: slot for slot_id, slot in text_slots(document).items() if slot_id.startswith("property_")}

    slot_issues, slot_notes = check_slots(properties(sanitized), properties(refetched))

    return issues + slot_issues, [
        f"{len(want_pages) - scanned} pages compared word by word"
        + (f", {scanned} scanned pages read again by OCR" if scanned else ""),
        f"{slot_notes[0]} (file properties)", *slot_notes[1:],
    ]


def overlaps(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def ocr_words(block):
    """
    What OCR read in a block, in the order of its text, as dicts with
    text, box and confidence: an image's words (in its pixels), or the
    word children of a block of a scanned PDF page (in points). None if
    OCR did not read it, or its words are not its text.
    """

    if block["block_type"] == "image_ocr":
        return block["metadata"].get("words") or None

    if block.get("metadata", {}).get("source") != "ocr":
        return None

    words = [
        {"text": child["text"], **child["bounding_box"], "confidence": child["metadata"].get("ocr_confidence", -1)}
        for child in block.get("children", [])
        if child["block_type"] == "word" and child.get("bounding_box")
    ]

    return words if [word["text"] for word in words] == (block.get("text") or "").split() else None


def ocr_surfaces(doc):
    """
    Where OCR read a document: block_id -> (surface, block, its words).
    A surface is an image (("image", block_id)) or a scanned page
    (("page", page_number)): the words of one surface are compared by
    position, whichever block they are in.
    """

    found = {}

    for page in doc["pages"]:
        for block in walk(page["blocks"]):
            words = ocr_words(block)

            if words is not None:
                surface = (
                    ("image", block["block_id"]) if block["block_type"] == "image_ocr"
                    else ("page", page["page_number"])
                )
                found[block["block_id"]] = (surface, block, words)

    return found


def inside(box, area, share=INSIDE_SHARE):
    width = max(0, min(box[2], area[2]) - max(box[0], area[0]))
    height = max(0, min(box[3], area[3]) - max(box[1], area[1]))
    size = (box[2] - box[0]) * (box[3] - box[1])

    return size > 0 and width * height >= share * size


def misreadings(document, report, refetched):
    """
    misreading(finding) for the rescan of the redacted file: True for a
    finding in an image or a scanned page that only OCR noise made.
    Filling part of a line can change how OCR reads the rest of it, and
    OCR reads the tags written in the filled areas. Each word of the
    finding must be one of:

        ours        inside an area the redactor filled: its pixels are
                    the grey box and the tag
        misread     read with low confidence (below LOW_OCR_WORD_CONFIDENCE),
                    outside the filled areas, over words the first reading
                    read with confidence and no finding covered
        unchanged   the word the first reading read there, with
                    confidence, and no finding covered (letter case aside)

    and at least one of them ours or misread: a finding the same words
    make again is not noise.
    """

    before, after = ocr_surfaces(document), ocr_surfaces(refetched)
    first = defaultdict(list)               # surface -> the words the first reading read there
    areas = defaultdict(list)               # surface -> every finding's words there, padded; None: unknown
    spans = defaultdict(list)

    for surface, _, words in before.values():
        first[surface].extend(words)

    for item in report["findings"]:
        if item.get("block_id") in before:
            spans[item["block_id"]].append((item["start"], item["end"], None))

    # Every finding's words, filled or kept as false positives: never noise
    for block_id, block_spans in spans.items():
        surface, block, words = before[block_id]
        covered = covered_words(block.get("text") or "", words, block_spans)

        areas[surface].extend(
            [None] if covered is None else [
                (x0 - PAD, y0 - PAD, x1 + PAD, y1 + PAD)
                for (x0, y0, x1, y1), _ in span_areas(block_spans, covered)
            ]
        )

    def confident(word):
        return float(word["confidence"]) >= LOW_OCR_WORD_CONFIDENCE

    def misreading(item):
        if item.get("block_id") not in after:
            return False

        surface, block, words = after[item["block_id"]]
        found = covered_words(block.get("text") or "", words, [(item["start"], item["end"], None)])

        if not found or not found[0] or None in areas[surface]:
            return False

        noise = False

        for word in found[0]:
            box = box_of(word)

            if any(inside(box, area) for area in areas[surface]):
                noise = True
                continue

            under = [old for old in first[surface] if overlaps(box, box_of(old))]

            if (
                not under or not all(confident(old) for old in under)
                or any(overlaps(box, area) for area in areas[surface])
            ):
                return False

            if not confident(word):
                noise = True
            elif not (len(under) == 1 and under[0]["text"].casefold() == word["text"].casefold()):
                return False

        return noise

    return misreading


def check_known_values(document, report, sanitized, contents, refetched, matches=None):
    """
    Every value and name part the scan found (validator.searched_values),
    searched for in everything the file holds, and in the OCR text of
    its images read again. matches: a list each match that is an issue
    is added to, as (location, start, end, type).
    """

    values, name_parts = searched_values(document, report)
    types = {item["finding_id"]: item["type"] for item in report["findings"]}
    value_regex = alternation(values)
    part_regex = alternation(name_parts)

    texts = list(contents.strings) + [
        (block["block_id"], block.get("text") or "")
        for page in refetched["pages"]
        for block in walk(page["blocks"])
        if block["block_type"] == "image_ocr" or block.get("metadata", {}).get("source") == "ocr"
    ]

    # Name parts the sanitized copy holds too: its validation listed them
    in_copy = {
        match.group()
        for _, _, text in strings(sanitized)
        for match in (part_regex.finditer(text) if part_regex else ())
    }

    found = defaultdict(list)               # (severity, finding_id) -> locations
    reviewed = set()

    for location, text in texts:
        full = []

        if value_regex:
            for match in value_regex.finditer(text):
                full.append((match.start(), match.end()))
                finding_id = values[normalise(match.group())]
                found[(FAIL, finding_id)].append(location)

                if matches is not None:
                    matches.append((location, match.start(), match.end(), types.get(finding_id)))

        if part_regex:
            for match in part_regex.finditer(text):
                if any(start < match.end() and match.start() < end for start, end in full):
                    continue

                if match.group() in in_copy:
                    reviewed.add(match.group())
                else:
                    found[(NEEDS_REVIEW, name_parts[match.group()])].append(location)

                    if matches is not None:
                        matches.append((location, match.start(), match.end(), "PERSON_NAME"))

    issues = [
        issue(
            severity,
            "a value the scan found is still in the redacted file"
            if severity == FAIL else
            "part of a name the scan found is in the redacted file, but not in the sanitized copy",
            finding_id=finding_id,
            occurrences=len(locations),
            locations=list(dict.fromkeys(locations))[:MAX_LOCATIONS]
        )
        for (severity, finding_id), locations in found.items()
    ]
    issues.sort(key=lambda item: (-SEVERITY[item["severity"]], -item["occurrences"]))

    notes = [f"{len(values)} values and {len(name_parts)} name parts searched in {len(texts)} strings"]

    if reviewed:
        notes.append(f"{len(reviewed)} name parts are in the sanitized copy too: its review answered for them")

    return issues, notes


def check_unread(contents, sanitized, refetched):
    """
    Images in the file whose pixels nothing has read.
    """

    sanitized_blocks = {
        block["block_id"]: block for page in sanitized["pages"] for block in walk(page["blocks"])
    }
    image_blocks = [
        block for page in refetched["pages"] for block in walk(page["blocks"])
        if block["block_type"] in IMAGE_TYPES
    ]
    shown = {
        str(block["metadata"].get("image_part") or "").lstrip("/") for block in image_blocks
    }
    issues = []

    for image in contents.images:
        if image not in shown:
            issues.append(issue(
                NEEDS_REVIEW, f"an image no block shows: its pixels were never read ({image})"
            ))

    for block in image_blocks:
        before = sanitized_blocks.get(block["block_id"], {})

        if block["block_type"] == "image" and before.get("block_type") == "image_ocr":
            issues.append(issue(
                NEEDS_REVIEW, "the image could not be read again, so it was not rescanned",
                block_id=block["block_id"]
            ))

    notes = [f"{len(contents.images)} images in the file, {len(image_blocks)} shown by blocks"]

    if contents.unread:
        notes.append(
            f"{len(contents.unread)} files in it are neither XML nor images (embedded objects, fonts): "
            "the sanitized copy's validation listed the embedded objects"
        )

    return issues, notes


# --------------------------------------------------
# LEFTOVERS
# --------------------------------------------------

def place_leftovers(redacted, refetched, items, redaction_pass):
    """
    Where on a redacted PDF's pages each leftover lies: items are
    (block_id, start, end, type) in the file read again. Returns
    (leftovers, complete): each leftover {"page_number", "box" (in
    unrotated page points, as redaction annotations take it), "type",
    "found_in_pass"}, never the text; complete if every item lies where
    OCR read it, a scanned page or a picture, and could be placed.
    """

    surfaces = ocr_surfaces(refetched)
    page_of = {
        block["block_id"]: page["page_number"] for page in refetched["pages"] for block in walk(page["blocks"])
    }
    leftovers = []
    complete = True

    with pymupdf.open(str(redacted)) as pdf:
        for block_id, start, end, pii_type in items:
            surface, block, words = surfaces.get(block_id, (None, None, None))
            covered = covered_words(block.get("text") or "", words, [(start, end, None)]) if block else None

            if not covered or not covered[0]:
                complete = False
                continue

            page = pdf[page_of[block_id] - 1]
            boxes = line_boxes([box_of(word) for word in covered[0]])

            if surface[0] == "image":
                to_page = image_placement(page, block)

                if to_page is None:
                    complete = False
                    continue

                rects = [to_page(padded(pymupdf.Rect(box), PAD)) for box in boxes]
            else:
                # OCR read the page as shown; annotations take it unrotated
                rects = [padded(pymupdf.Rect(box), PIXEL_PAD) for box in boxes]
                rects = [rect * page.derotation_matrix for rect in rects] if page.rotation else rects

            leftovers.extend(
                {
                    "page_number": page_of[block_id], "box": [round(value, 2) for value in rect],
                    "type": pii_type or "UNREADABLE_TEXT", "found_in_pass": redaction_pass,
                }
                for rect in rects
            )

    return leftovers, complete


def another_pass(validation):
    """
    The leftovers to redact the file again with, or None. Only for a PDF
    that did not pass for no other reason than PII read again where OCR
    reads it (leftovers_complete), with leftovers this pass found, and
    fewer than MAX_PASSES passes so far.
    """

    if (
        validation.get("verdict") == PASS or not validation.get("leftovers_complete")
        or validation.get("redaction_pass", 1) >= MAX_PASSES
        or any(
            check["status"] != PASS for check in validation["checks"]
            if check["name"] not in ("rescan", "known_values")
        )
        or not any(item["found_in_pass"] == validation.get("redaction_pass", 1) for item in validation["leftovers"])
    ):
        return None

    return validation["leftovers"]


# --------------------------------------------------
# VALIDATION
# --------------------------------------------------

def sha256_of(content):
    return hashlib.sha256(content).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_again(path, document):
    """
    The redacted file read by its extractor. The pages of a PDF that were
    scanned are OCR'd again: their text layer is now only the redaction
    tags written on them.
    """

    if Path(path).suffix.lower() == ".pdf":
        return extract_pdf_document(str(path), ocr_pages={
            page["page_number"] for page in document["pages"]
            if page["metadata"].get("extraction_method") == "ocr"
        })

    return extract_document(path)


def validate_original(json_path, nlp_detector=None):
    """
    Check the redacted file of an extracted JSON file, and save the
    result as <name>_file_validation.json. A FAIL file is deleted.
    Returns (validation, path of the validation report).
    """

    json_path = Path(json_path)
    document = read_json(json_path)
    report = read_json(findings_path(json_path))
    sanitized = read_json(output_paths(json_path)[1])
    record = read_json(record_path(json_path))
    redacted = redacted_path(json_path, document["file_name"])
    content = redacted.read_bytes()

    try:
        copy_validated_at = read_json(validation_path(json_path)).get("validated_at")
    except (OSError, ValueError):
        copy_validated_at = None

    read = {}

    def refetched():
        if "document" not in read:
            read["document"] = read_again(redacted, document).to_dict()

        return read["document"]

    def contents():
        if "contents" not in read:
            read["contents"] = file_contents(redacted)

        return read["contents"]

    checks = []
    failed, matches = [], []                # what rescan and known_values found, to place
    crashed = False

    for name, run in (
        ("redaction", lambda: check_record(document, report, record, content)),
        ("text", lambda: check_text(sanitized, refetched())),
        ("rescan", lambda: check_rescan(
            report, refetched(), nlp_detector, misreadings(document, report, refetched()), failed
        )),
        ("known_values", lambda: check_known_values(
            document, report, sanitized, contents(), refetched(), matches
        )),
        ("unread", lambda: check_unread(contents(), sanitized, refetched())),
    ):
        try:
            issues, notes = run()
        except Exception as error:
            issues, notes = [issue(FAIL, f"the check could not run: {type(error).__name__}: {error}")], []
            crashed = True

        checks.append({
            "name": name,
            "status": worst(item["severity"] for item in issues),
            "issues_total": len(issues),
            "notes": notes,
            "issues": issues[:MAX_ISSUES],
        })

    verdict = worst(check["status"] for check in checks)
    redaction_pass = record.get("redaction_pass", 1)
    leftovers, complete = list(record.get("leftovers", [])), False

    # Where a PDF still shows what its scans were read as holding, for another pass
    if verdict != PASS and redacted.suffix.lower() == ".pdf" and not crashed:
        items = [(item["block_id"], item["start"], item["end"], item["type"]) for item in failed] + matches
        found, complete = place_leftovers(redacted, refetched(), items, redaction_pass)
        leftovers.extend(item for item in found if item not in leftovers)

    # A file known to hold PII is not kept beside the others
    if verdict == FAIL:
        redacted.unlink(missing_ok=True)

    validation = {
        "file_validator_version": FILE_VALIDATOR_VERSION,
        "validated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "document_id": document.get("document_id"),
        "file_name": document.get("file_name"),
        "report_scan_timestamp": report.get("scan_timestamp"),
        "verdict": verdict,
        "summary": {check["name"]: check["status"] for check in checks},
        "checks": checks,
        # The pass this file was written in, and the places of what its
        # scans still show (boxes and types only): another_pass()
        "redaction_pass": redaction_pass,
        "leftovers": leftovers,
        "leftovers_complete": complete,
        "files": {
            "redacted": str(redacted),
            "redacted_sha256": sha256_of(content),
            "deleted": verdict == FAIL,
            # The validation of the sanitized copy this file was checked
            # beside: approved_file refuses it after any other
            "copy_validated_at": copy_validated_at,
        },
    }

    output_path = file_validation_path(json_path)
    save_json(validation, output_path)

    return validation, output_path


# --------------------------------------------------
# RELEASE
# --------------------------------------------------

def approved_file(json_path):
    """
    The path of the redacted file of an extracted JSON file, to release.
    The one way to get it for that: it raises NotApproved unless the
    sanitized copy is approved (approved_copy, with its review), the
    file's verdict is PASS, it was checked beside the copy's current
    validation, and it is byte for byte the file that was checked.
    """

    json_path = Path(json_path)
    approved_copy(json_path)

    report_path = file_validation_path(json_path)

    try:
        validation = read_json(report_path)
        copy_validation = read_json(validation_path(json_path))
    except (OSError, ValueError) as error:
        raise NotApproved(f"the redacted file was not checked: {error}") from error

    if validation.get("verdict") != PASS:
        raise NotApproved(f"the redacted file's verdict is {validation.get('verdict')}: see {report_path}")

    if validation.get("files", {}).get("copy_validated_at") != copy_validation.get("validated_at"):
        raise NotApproved("the redacted file was checked beside an older validation of the sanitized copy")

    path = redacted_path(json_path, validation.get("file_name") or "")

    if not path.is_file() or sha256(path) != validation["files"].get("redacted_sha256"):
        raise NotApproved(f"{path} has changed since it was checked")

    return path


def main():
    """
    Exit code: 0 PASS, 1 FAIL, 2 NEEDS_REVIEW.
    """

    if len(sys.argv) < 2:
        sys.exit("Usage: python -m src.original_format.validator output/<job_id>/<name>.json")

    json_path = Path(sys.argv[1])

    for path in (json_path, record_path(json_path)):
        if not path.is_file():
            sys.exit(f"File not found: {path}")

    nlp = LocalNLPDetector()

    if nlp.error:
        print(f"Warning: {nlp.error} Person names will not be rescanned.")

    validation, output_path = validate_original(json_path, nlp)

    print(f"File validation report: {output_path}")
    print_validation(validation)

    if validation["files"]["deleted"]:
        print("\nThe redacted file was deleted: PII is known to remain in it.")

    try:
        path = approved_file(json_path)
        print(f"\nApproved: {path} may be released.")
    except NotApproved as reason:
        print(f"\nBlocked: do not release the redacted file ({reason}).")

    sys.exit(EXIT_CODES[validation["verdict"]])


if __name__ == "__main__":
    main()
