"""
Measure how accurate each step of the pipeline is, end to end, on
documents whose PII is known.

    python evaluation/run_evaluation.py                   whole corpus
    python evaluation/run_evaluation.py --only "*.pdf"    some files
    python evaluation/run_evaluation.py --rebuild         rebuild the synthetic corpus first

Each document under evaluation/corpus goes through main.py's main(),
exactly as `python main.py <file>` runs it: input check, upload,
extraction, format check, scan, redaction, validation, exposure report
and the approved_copy() release gate. Only three things differ, none of
which changes a result: uploads and outputs go in this run's results
folder instead of backend/uploads and backend/output, spaCy is loaded
once for every document, and each step is timed.

The outputs are then compared with the document's labels
(<file>.labels.json, see build_corpus.py), step by step:

    0 input check   invalid files refused, valid files accepted
    1 extraction    each labelled value is in the extracted JSON: exactly,
                    approximately (OCR damage, see FUZZY_MATCH) or not at all
    1b structure    each labelled element is found as the right kind in the
                    right place, neighbours in the reading order stay
                    neighbours, and the text around each redaction is kept
                    (target: STRUCTURE_TARGET)
    2 detection     precision, recall and F1 of the PII report against the
                    labels, by type, format, place and detector
    3 redaction     no labelled value is left in the sanitized copy (part of
                    a name left counts too), and no not_pii value is removed
    4 validation    the verdict against the truth. A copy released (PASS)
                    that still holds PII is an unsafe release
    5 reports       no labelled value is in the validation or exposure
                    reports, which are meant to be shareable
    gate, exit      approved_copy() releases exactly the PASS copies, and
                    main.py's exit code matches the verdict

When archive/tests/evaluate_detector.py is present, the detector is also
scored alone on its labelled text snippets (dev and eval splits).

Results go in evaluation/results/<timestamp>/:

    summary.md      every score, compared with the previous run, then each
                    miss, false positive, leak and over-redaction. It holds
                    the labelled values: do not share it for real documents
    metrics.json    the scores, which the next run compares with
    details.json    each label's outcome at each step, per document
    runs/<file>/    the pipeline's own uploads, outputs and log

Exit code: 1 if anything unsafe happened (PII released, PII in a shareable
report, an invalid file accepted, a crash), else 0. Low accuracy alone
does not fail the run.
"""

import argparse
import fnmatch
import importlib.util
import io
import json
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BACKEND = REPO / "backend"

# The pipeline imports src..., from the backend folder; build_corpus is here
for folder in (HERE, BACKEND):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import main as pipeline  # noqa: E402
from src.pii.detectors import LocalNLPDetector  # noqa: E402
from src.pii.document_scanner import SCANNER_VERSION  # noqa: E402
from src.pii.exposure_report import EXPOSURE_VERSION  # noqa: E402
from src.pii.redactor import REDACTION_TAG, REDACTOR_VERSION  # noqa: E402
from src.pii.validator import (  # noqa: E402
    EXIT_CODES, PASS, VALIDATOR_VERSION, NotApproved, approved_copy
)

import build_corpus  # noqa: E402


CORPUS_DIR = HERE / "corpus"
RESULTS_DIR = HERE / "results"
SNIPPET_EVALUATOR = REPO / "archive" / "tests" / "evaluate_detector.py"

# A value read with OCR damage ("EMP-7O412"), or a finding with a word
# more or less ("Dr. Elena Varga"), still matches its label when this
# alike (difflib ratio, 0-1)
FUZZY_MATCH = 0.85

# The extraction must keep at least this share of a document's elements
# (found, as the right kind, in the right place) and of their neighbours
# in the reading order
STRUCTURE_TARGET = 0.80

# The functions main.py calls for each step, timed by name
TIMED_STEPS = {
    "extract": "extract_document",
    "scan": "scan_file",
    "redact": "redact_file",
    "validate": "validate_file",
    "report": "exposure_file",
}

# Files the pipeline writes to be shared: they must hold no PII value
SHAREABLE = ("*_validation.json", "*_exposure.json", "exposure_report.md", "exposure_report.csv")

# Release outcomes: what the gate did, against what the copy holds
UNSAFE_RELEASE = "UNSAFE release"
CORRECT_RELEASE = "correct release"
CORRECT_BLOCK = "correct block"
OVER_BLOCK = "over-block"

# Headline scores: (name, path in metrics.json, higher is better)
HEADLINE = [
    ("Invalid files refused", ("input_check", "invalid_refused_rate"), True),
    ("Valid files accepted", ("input_check", "valid_accepted_rate"), True),
    ("Extraction: PII values readable", ("extraction", "recall"), True),
    ("Structure: elements kept", ("structure", "structure_rate"), True),
    ("Structure: neighbours kept (order)", ("structure", "order_rate"), True),
    (f"Structure: documents below {STRUCTURE_TARGET}", ("structure", "documents_below_target"), False),
    ("Context around redactions kept", ("structure", "context_rate"), True),
    ("Detection precision", ("detection", "overall", "precision"), True),
    ("Detection recall", ("detection", "overall", "recall"), True),
    ("Detection F1", ("detection", "overall", "f1"), True),
    ("Redaction: PII removed", ("redaction", "removal_rate"), True),
    ("Redaction: look-alikes kept", ("redaction", "kept_rate"), True),
    ("Release gate correct", ("validation", "gate_accuracy"), True),
    ("Unsafe releases", ("validation", "unsafe_releases"), False),
    ("Over-blocked clean copies", ("validation", "over_blocks"), False),
    ("PII values in shareable reports", ("reports", "values_found"), False),
    ("Snippet benchmark F1 (dev)", ("snippets", "dev", "f1"), True),
    ("Snippet benchmark F1 (eval)", ("snippets", "eval", "f1"), True),
]


# --------------------------------------------------
# TEXT
# --------------------------------------------------

def normalise(text):
    return " ".join(str(text).split())


def strings(node):
    """
    Every string value in a JSON tree, whitespace-normalised.
    """

    if isinstance(node, str):
        yield normalise(node)
    elif isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)


def contains(texts, value):
    """
    True if value is in one of texts as a whole: "Rao" is not found in
    "Raoul". Case matters, so "Holloway" is not found in an email address.
    """

    pattern = re.compile(rf"(?<!\w){re.escape(normalise(value))}(?!\w)")

    return any(pattern.search(text) for text in texts)


def similarity(first, second):
    return SequenceMatcher(None, normalise(first), normalise(second)).ratio()


def closest(value, texts):
    """
    The run of words in texts most like value, and how alike (0-1): what
    OCR made of a value it misread.
    """

    value = normalise(value)
    size = len(value.split())
    best, best_score = None, 0.0

    for text in texts:
        if len(text) < len(value) // 2:
            continue

        words = text.split()

        for length in {max(1, size - 1), size, size + 1}:
            for start in range(len(words) - length + 1):
                window = " ".join(words[start:start + length]).strip(".,;:")
                matcher = SequenceMatcher(None, value, window)

                if matcher.real_quick_ratio() <= best_score or matcher.quick_ratio() <= best_score:
                    continue

                score = matcher.ratio()

                if score > best_score:
                    best, best_score = window, score

    return best, round(best_score, 3)


# --------------------------------------------------
# CORPUS
# --------------------------------------------------

def labels_path(document_path):
    return document_path.with_name(f"{document_path.name}.labels.json")


def load_labels(document_path):
    """
    The document's labels, with each PII value once: a value labelled in
    two places is one value to find, with both places. None if the
    document has no labels file.
    """

    path = labels_path(document_path)

    if not path.is_file():
        return None

    labels = json.loads(path.read_text(encoding="utf-8"))
    pii = {}

    for item in labels.get("pii", []):
        key = (normalise(item["text"]), item["type"])
        entry = pii.setdefault(key, {"text": item["text"], "type": item["type"], "where": []})
        entry["where"].append(item.get("where", "unspecified"))

    labels["pii"] = list(pii.values())
    labels["not_pii"] = list({normalise(item["text"]): item for item in labels.get("not_pii", [])}.values())

    return labels


def corpus_files(corpus, only=None):
    files = sorted(
        path for path in Path(corpus).rglob("*")
        if path.suffix.lower() in pipeline.SUPPORTED_TYPES and path.is_file()
    )

    if only:
        files = [path for path in files if fnmatch.fnmatch(path.name, only)]

    return files


# --------------------------------------------------
# RUNNING THE PIPELINE
# --------------------------------------------------

@contextmanager
def patched(target, **values):
    old = {name: getattr(target, name) for name in values}

    for name, value in values.items():
        setattr(target, name, value)

    try:
        yield
    finally:
        for name, value in old.items():
            setattr(target, name, value)


def timed(function, step, timings):
    def run(*args, **kwargs):
        started = time.perf_counter()

        try:
            return function(*args, **kwargs)
        finally:
            timings[step] = round(time.perf_counter() - started, 2)

    return run


def run_pipeline(source, run_dir, nlp):
    """
    Run main.py on one file, as `python main.py <source>` does, with its
    uploads and outputs in run_dir. Returns the exit code, the message it
    stopped with or the traceback it crashed with, the step timings and
    the job folder (None if the file was refused before upload).
    """

    timings = {}
    log = io.StringIO()
    exit_code, message, crash = 0, None, None
    steps = {name: timed(getattr(pipeline, name), step, timings) for step, name in TIMED_STEPS.items()}
    started = time.perf_counter()

    with (
        patched(
            pipeline, UPLOAD_DIR=run_dir / "uploads", OUTPUT_DIR=run_dir / "output",
            LocalNLPDetector=lambda: nlp, **steps
        ),
        patched(sys, argv=["main.py", str(source)]),
        redirect_stdout(log),
        redirect_stderr(log),
    ):
        try:
            pipeline.main()
        except SystemExit as stop:
            if isinstance(stop.code, str):
                exit_code, message = 1, stop.code
            else:
                exit_code = stop.code or 0
        except Exception:
            exit_code, crash = 1, traceback.format_exc()

    total = round(time.perf_counter() - started, 2)

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "pipeline.log").write_text(
        log.getvalue() + (f"\n{message}\n" if message else "") + (f"\n{crash}" if crash else ""),
        encoding="utf-8"
    )

    jobs = sorted(path for path in (run_dir / "output").glob("*") if path.is_dir())

    return {
        "exit_code": exit_code,
        "message": message,
        "crash": crash,
        "timings": {**timings, "total": total},
        "job_dir": jobs[0] if jobs else None,
    }


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --------------------------------------------------
# SCORING ONE DOCUMENT
# --------------------------------------------------

def score_extraction(labels, extracted_texts):
    """
    Where each labelled value is in the extracted text: "exact",
    "approximate" (OCR damage) or "missing", and the text found for it.
    """

    for label in labels:
        if contains(extracted_texts, label["text"]):
            label.update(extraction="exact", extracted_as=normalise(label["text"]), similarity=1.0)
            continue

        found, score = closest(label["text"], extracted_texts)
        label.update(
            extraction="approximate" if score >= FUZZY_MATCH else "missing",
            extracted_as=found, similarity=score
        )


def predicted_values(report):
    """
    The distinct (value, type) pairs the PII report found, each with the
    first finding that reported it. Findings a reviewer marked
    false_positive are not predictions: they are left unredacted.
    """

    pairs = {}

    for item in (report or {}).get("findings", []):
        if item.get("review_status") != "false_positive":
            pairs.setdefault((normalise(item["text"]), item["type"]), item)

    return pairs


def score_detection(labels, pairs):
    """
    Match each labelled (value, type) to a finding of the same type: the
    same value, one FUZZY_MATCH alike (what OCR read, a title kept), or
    one that covers the whole value and more ("Remind Arun Pillai"): the
    value is still redacted. A finding covering only part of the value
    leaves the rest showing, and does not match. The most alike pairs are
    matched first, each finding once. Sets each label's "detection";
    returns the findings that match no label.
    """

    candidates = []

    for index, label in enumerate(labels):
        variants = [variant for variant in {normalise(label["text"]), label.get("extracted_as")} if variant]

        for key in pairs:
            if key[1] == label["type"]:
                score = max(similarity(variant, key[0]) for variant in variants)

                if score >= FUZZY_MATCH or any(contains([key[0]], variant) for variant in variants):
                    candidates.append((score, index, key))

    matched, used = {}, set()

    for score, index, key in sorted(candidates, key=lambda item: -item[0]):
        if index not in matched and key not in used:
            matched[index] = (key, score)
            used.add(key)

    other_types = defaultdict(list)

    for key, item in pairs.items():
        if key not in used:
            other_types[key[0]].append(key[1])

    for index, label in enumerate(labels):
        if index in matched:
            key, score = matched[index]
            item = pairs[key]
            label.update(
                detection="found" if score == 1.0 else "found (approximate)",
                found_as=key[0], detector=item.get("detector"), rule_id=item.get("rule_id")
            )
        elif other_types.get(label.get("extracted_as")) or other_types.get(normalise(label["text"])):
            found = other_types.get(label.get("extracted_as")) or other_types[normalise(label["text"])]
            label.update(detection=f"found as {', '.join(found)}")
        else:
            label.update(detection="missed")

    return [
        {
            "text": item["text"], "type": item["type"], "detector": item.get("detector"),
            "rule_id": item.get("rule_id"), "confidence": item.get("confidence"),
            "block_id": item.get("block_id"), "place": place_of(item),
        }
        for key, item in pairs.items()
        if key not in used
    ]


def place_of(item):
    if item.get("image"):
        return "image (OCR)"
    if item.get("block_type") == "property":
        return "property"
    if item.get("table_id"):
        return "table"
    return item.get("block_type") or "?"


def score_redaction(labels, decoys, extracted_texts, sanitized_texts):
    """
    Whether each labelled value is left in the sanitized copy: "removed",
    "LEAKED" or, for a name, "PARTLY LEAKED" (one of its words left). A
    word labelled as a value of its own ("Holloway", a surname mention)
    is scored there, not here. Whether each not_pii value is "kept" or
    "removed" (over-redaction).
    """

    single_words = {normalise(label["text"]) for label in labels if len(label["text"].split()) == 1}

    for label in labels:
        if label["extraction"] == "missing":
            label["redaction"] = "not extracted"
            continue

        if contains(sanitized_texts, label["text"]) or contains(sanitized_texts, label["extracted_as"]):
            label["redaction"] = "LEAKED"
            continue

        name_words = label["extracted_as"].split()
        words = [word for word in name_words if len(word.strip(".")) > 2 and word not in single_words]

        if label["type"] == "PERSON_NAME" and len(name_words) > 1 and any(contains(sanitized_texts, word) for word in words):
            label["redaction"] = "PARTLY LEAKED"
        else:
            label["redaction"] = "removed"

    for decoy in decoys:
        if not contains(extracted_texts, decoy["text"]):
            decoy["status"] = "not extracted"
        else:
            decoy["status"] = "kept" if contains(sanitized_texts, decoy["text"]) else "removed"


# --------------------------------------------------
# STRUCTURE AND CONTEXT
# --------------------------------------------------

# Element kinds of the labels, by the extractor's block types
KINDS = {"image_ocr": "image_text"}

# Places outside the body's reading order: whether an element there is
# found, and as what, is scored; its position in the reading order is not
OUTSIDE_FLOW = ("header", "footer", "notes", "comment")

# An element the extractor split over up to this many consecutive blocks
# is still found
MAX_SPLIT = 5

# Stands for any redaction tag when texts around redactions are compared
TAG = "[REDACTED]"


def extracted_units(document):
    """
    The extracted document as units in reading order: each page's blocks,
    and a table as its cells, row by row. Words belong to their block.
    """

    units = []

    for page in document["pages"]:
        for block in page["blocks"]:
            parts = [block]

            if block["block_type"] == "table":
                parts = sorted(
                    block.get("children") or [],
                    key=lambda cell: (cell["metadata"].get("row", 0), cell["metadata"].get("column", 0))
                )

            for part in parts:
                metadata = part.get("metadata") or {}
                units.append({
                    "block_id": part["block_id"],
                    "kind": KINDS.get(part["block_type"], part["block_type"]),
                    "where": metadata.get("location") or "body",
                    "page": page["page_number"],
                    "row": metadata.get("row"),
                    "column": metadata.get("column"),
                    "anchor": metadata.get("anchor_block_id"),
                    "text": normalise(part.get("text") or ""),
                })

    return units


def locate(text, units, after=0, page=None):
    """
    Where an element's text is among the units, as (first, last) unit
    positions: a unit with exactly that text, a unit holding it, a run
    of up to MAX_SPLIT consecutive units holding it together, or a unit
    at least FUZZY_MATCH alike (OCR damage). Of several matches, the
    first at or after position after wins. None if it is not there.
    """

    target = normalise(text)
    candidates = [index for index, unit in enumerate(units) if page is None or unit["page"] == page]

    def first(matches):
        later = [match for match in matches if match[0] >= after]
        return (later or matches or [None])[0]

    for test in (lambda unit: unit["text"] == target, lambda unit: target in unit["text"]):
        match = first([(index, index) for index in candidates if test(units[index])])

        if match:
            return match

    runs = []

    for start in candidates:
        joined = ""

        for end in range(start, min(start + MAX_SPLIT, len(units))):
            if page is not None and units[end]["page"] != page:
                break

            joined = f"{joined} {units[end]['text']}".strip()

            if target in joined:
                runs.append((start, end))
                break

    match = first(runs)

    if match:
        return match

    score, index = max(((similarity(target, units[index]["text"]), index) for index in candidates), default=(0, None))

    return (index, index) if index is not None and score >= FUZZY_MATCH else None


def with_tags(text, values):
    """
    text with each of values, and each redaction tag, written as TAG.
    """

    text = REDACTION_TAG.sub(TAG, normalise(text))

    for value in sorted(values, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(normalise(value))}(?!\w)", TAG, text)

    # Adjacent redactions are one
    return re.sub(rf"{re.escape(TAG)}(?:\s*{re.escape(TAG)})+", TAG, text)


def score_structure(expected, document, sanitized, pii):
    """
    How much of the document's structure the extraction kept, from the
    elements the labels list in reading order:

        elements    each found, as the right kind (heading, list item...),
                    in the right place (body, header, notes...), a table
                    cell in its row and column, a comment attached to its
                    text: "kept"; otherwise what went wrong
        neighbours  each pair of elements next to each other in the body's
                    reading order is next to each other in the extraction
                    too: the text before and after every element is kept
        context     around each element holding PII, the sanitized copy
                    differs from the extracted text only by the tags in
                    place of the labelled values: no word of context was
                    redacted or lost
    """

    units = extracted_units(document)
    position = {unit["block_id"]: index for index, unit in enumerate(units)}
    elements = []
    cursor = 0

    for item in expected:
        span = locate(item["text"], units, cursor, item.get("page"))
        element = {"text": item["text"], "kind": item["kind"], "where": item["where"], "span": span}
        elements.append(element)

        if span is None:
            element["status"] = "not found"
            continue

        unit = units[span[0]]
        problems = []

        if unit["kind"] != item["kind"]:
            problems.append(f"read as {unit['kind']}")

        if unit["where"] != item["where"]:
            problems.append(f"placed in {unit['where']}")

        if item["kind"] == "table_cell" and (unit["row"], unit["column"]) != (item.get("row"), item.get("column")):
            problems.append(f"in row {unit['row']}, column {unit['column']}")

        if "anchor" in item:
            anchor = position.get(unit["anchor"])

            if anchor is None or normalise(item["anchor"]) not in units[anchor]["text"]:
                problems.append("not attached to the text it is about")

        element["status"] = "; ".join(problems) or "kept"

        if item["where"] not in OUTSIDE_FLOW:
            cursor = span[0]

    # The body's reading order: units the extraction did not put aside
    flow = {index: number for number, index in enumerate(
        index for index, unit in enumerate(units) if unit["where"] not in OUTSIDE_FLOW
    )}
    body = [
        element for element in elements
        if element["where"] not in OUTSIDE_FLOW and element["span"]
        and element["span"][0] in flow and element["span"][1] in flow
    ]
    broken = []

    for before, after in zip(body, body[1:]):
        if not (
            after["span"][0] == before["span"][1]
            or flow[after["span"][0]] == flow[before["span"][1]] + 1
        ):
            broken.append((before["text"], after["text"]))

    sanitized_text = {}

    if sanitized:
        for page in sanitized["pages"]:
            stack = list(page["blocks"])

            while stack:
                block = stack.pop()
                sanitized_text[block["block_id"]] = block.get("text") or ""
                stack.extend(child for child in block.get("children") or [] if child["block_type"] != "word")

    values = [label["text"] for label in pii]
    context = []

    for element in elements:
        if not element["span"] or not any(normalise(value) in normalise(element["text"]) for value in values):
            continue

        ids = [units[index]["block_id"] for index in range(element["span"][0], element["span"][1] + 1)]
        wanted = with_tags(" ".join(units[position[block_id]]["text"] for block_id in ids), values)
        actual = with_tags(" ".join(sanitized_text.get(block_id, "") for block_id in ids), [])
        context.append({"text": element["text"], "kept": wanted == actual, "expected": wanted, "actual": actual})

    kept = sum(1 for element in elements if element["status"] == "kept")
    relations = max(len(body) - 1, 0)

    return {
        "elements": len(elements),
        "found": sum(1 for element in elements if element["span"]),
        "kept": kept,
        "structure_rate": ratio(kept, len(elements)),
        "relations": relations,
        "neighbours_kept": relations - len(broken),
        "order_rate": ratio(relations - len(broken), relations),
        "pii_elements": len(context),
        "context_kept": sum(1 for item in context if item["kept"]),
        "context_rate": ratio(sum(1 for item in context if item["kept"]), len(context)),
        "problems": [element for element in elements if element["status"] != "kept"],
        "broken": broken,
        "context_lost": [item for item in context if not item["kept"]],
    }


def shareable_leaks(job_dir, labels):
    """
    Labelled values found in the files meant to be shared.
    """

    leaks = []

    for pattern in SHAREABLE:
        for path in sorted(job_dir.glob(pattern)):
            texts = [normalise(path.read_text(encoding="utf-8"))]

            for label in labels:
                values = {label["text"], label.get("extracted_as") or label["text"]}

                if any(contains(texts, value) for value in values):
                    leaks.append({"file": path.name, "type": label["type"], "text": label["text"]})

    return leaks


def detected(label):
    return label["detection"] in ("found", "found (approximate)")


def expected_exit_code(verdict, released):
    """
    What main.py exits with: 0 for a released copy, else the verdict's
    code, and 1 with no verdict or for a PASS copy the gate refused.
    """

    if released:
        return EXIT_CODES[PASS]

    return 1 if verdict in (None, PASS) else EXIT_CODES[verdict]


def release_outcome(released, holds_pii):
    if released:
        return UNSAFE_RELEASE if holds_pii else CORRECT_RELEASE

    return CORRECT_BLOCK if holds_pii else OVER_BLOCK


def score_document(source, corpus, labels, run):
    """
    Everything known about one document's run: what each step wrote, and,
    if it has labels, how each labelled value fared at each step.
    """

    job_dir = run["job_dir"]
    expected = (labels or {}).get("expected_outcome", "processed")

    result = {
        "file": source.relative_to(corpus).as_posix(),
        "labelled": labels is not None,
        "expected_outcome": expected,
        "accepted": job_dir is not None,
        "exit_code": run["exit_code"],
        "message": run["message"],
        "crash": run["crash"].strip().splitlines()[-1] if run["crash"] else None,
        "timings": run["timings"],
    }

    result["input_check_ok"] = result["accepted"] == (expected == "processed")

    if job_dir is None:
        return result

    json_path = job_dir / f"{source.stem}.json"
    document = load(json_path)
    report = load(pipeline.pipeline_outputs(json_path)[0])
    sanitized = load(pipeline.pipeline_outputs(json_path)[2])
    validation = load(pipeline.pipeline_outputs(json_path)[3])
    exposure = load(pipeline.pipeline_outputs(json_path)[4])

    redaction = ((sanitized or {}).get("metadata") or {}).get("redaction") or {}
    verdict = (validation or {}).get("verdict")

    try:
        approved_copy(json_path)
        released, gate_reason = True, None
    except NotApproved as reason:
        released, gate_reason = False, str(reason)

    result.update({
        "format": f"{document['source_type']} ({document.get('extraction_method')})" if document else "?",
        "report_status": (report or {}).get("status"),
        "extraction_warnings": len(((report or {}).get("summary") or {}).get("extraction_warnings", [])),
        "findings": len((report or {}).get("findings", [])),
        "redaction_status": redaction.get("status"),
        "redaction_errors": len(redaction.get("errors", [])),
        "verdict": verdict,
        "checks": (validation or {}).get("summary", {}),
        "released": released,
        "gate_reason": gate_reason,
        "gate_consistent": released == (verdict == PASS),
        "exit_code_ok": run["exit_code"] == expected_exit_code(verdict, released),
        "exposure_counts_agree": (
            exposure is not None and report is not None
            and exposure["summary"]["total_findings"] == len(report["findings"])
            and exposure["summary"]["redacted"] == redaction.get("summary", {}).get("redacted")
        ),
    })

    if labels is None:
        result["outcome"] = "released" if released else "blocked"
        return result

    pii = [dict(label) for label in labels["pii"]]
    decoys = [dict(decoy) for decoy in labels["not_pii"]]
    extracted_texts = list(dict.fromkeys(strings(document))) if document else []

    score_extraction(pii, extracted_texts)
    false_positives = score_detection(pii, predicted_values(report))

    if sanitized is not None:
        score_redaction(pii, decoys, extracted_texts, list(dict.fromkeys(strings(sanitized))))
    else:
        for item in pii:
            item["redaction"] = "no sanitized copy"
        for decoy in decoys:
            decoy["status"] = "no sanitized copy"

    holds_pii = any(label["redaction"] in ("LEAKED", "PARTLY LEAKED") for label in pii)

    if labels.get("structure") and document:
        result["structure"] = score_structure(labels["structure"], document, sanitized, pii)

    result.update({
        "pii": pii,
        "not_pii": decoys,
        "false_positives": false_positives,
        "report_leaks": shareable_leaks(job_dir, pii),
        "holds_pii": holds_pii,
        "outcome": release_outcome(released, holds_pii) if sanitized is not None else "no sanitized copy",
    })

    return result


# --------------------------------------------------
# METRICS
# --------------------------------------------------

def ratio(part, whole):
    return round(part / whole, 3) if whole else None


def scores(counts):
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    f1 = round(2 * precision * recall / (precision + recall), 3) if precision and recall else (
        0.0 if precision == 0 or recall == 0 else None
    )

    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def detection_metrics(results):
    """
    Precision, recall and F1 over the corpus (each value counted once
    per document), and broken down. Places come from the labels, so they
    have recall only; detectors only exist for findings, so they have
    precision only.
    """

    groups = defaultdict(Counter)

    for result in results:
        for label in result.get("pii", []):
            outcome = "tp" if detected(label) else "fn"

            for group in (
                ("overall", "all"), ("type", label["type"]), ("format", result["format"]),
                ("place", label["where"][0]),
            ):
                groups[group][outcome] += 1

            if outcome == "tp":
                groups[("detector", f"{label['detector']} ({label['rule_id']})")]["tp"] += 1

        for item in result.get("false_positives", []):
            for group in (
                ("overall", "all"), ("type", item["type"]), ("format", result["format"]),
                ("detector", f"{item['detector']} ({item['rule_id']})"),
            ):
                groups[group]["fp"] += 1

    metrics = {"overall": scores(groups[("overall", "all")])}

    for name, plural in (("type", "by_type"), ("format", "by_format"), ("place", "by_place"), ("detector", "by_detector")):
        metrics[plural] = {
            value: scores(groups[(kind, value)])
            for kind, value in sorted(groups)
            if kind == name
        }

    for row in metrics["by_place"].values():
        row["precision"] = row["f1"] = None

    for row in metrics["by_detector"].values():
        row["recall"] = row["f1"] = None

    return metrics


def corpus_metrics(results, snippets):
    labelled = [result for result in results if result.get("pii") is not None]
    invalid = [result for result in results if result["expected_outcome"] == "rejected"]
    valid = [result for result in results if result["expected_outcome"] == "processed"]
    labels = [label for result in labelled for label in result["pii"]]
    decoys = [decoy for result in labelled for decoy in result["not_pii"] if decoy["status"] in ("kept", "removed")]
    extraction = Counter(label["extraction"] for label in labels)
    redaction = Counter(label["redaction"] for label in labels)
    removable = sum(redaction[status] for status in ("removed", "LEAKED", "PARTLY LEAKED"))
    outcomes = Counter(result.get("outcome") for result in labelled)
    gated = [result for result in labelled if result.get("outcome") in (UNSAFE_RELEASE, CORRECT_RELEASE, CORRECT_BLOCK, OVER_BLOCK)]
    structured = [result["structure"] for result in labelled if result.get("structure")]
    structure = {
        key: sum(item[key] for item in structured)
        for key in ("elements", "found", "kept", "relations", "neighbours_kept", "pii_elements", "context_kept")
    }
    timing = defaultdict(float)

    for result in results:
        for step, seconds in result["timings"].items():
            timing[step] += seconds

    return {
        "documents": {
            "total": len(results),
            "labelled": len(labelled),
            "crashed": sum(1 for result in results if result["crash"]),
        },
        "input_check": {
            "invalid_files": len(invalid),
            "invalid_refused": sum(1 for result in invalid if result["input_check_ok"]),
            "invalid_refused_rate": ratio(sum(1 for result in invalid if result["input_check_ok"]), len(invalid)),
            "valid_files": len(valid),
            "valid_accepted": sum(1 for result in valid if result["input_check_ok"]),
            "valid_accepted_rate": ratio(sum(1 for result in valid if result["input_check_ok"]), len(valid)),
        },
        "extraction": {
            "values": len(labels),
            **{status: extraction[status] for status in ("exact", "approximate", "missing")},
            "recall": ratio(extraction["exact"] + extraction["approximate"], len(labels)),
            "exact_rate": ratio(extraction["exact"], len(labels)),
        },
        "structure": {
            "documents": len(structured),
            **structure,
            "structure_rate": ratio(structure["kept"], structure["elements"]),
            "order_rate": ratio(structure["neighbours_kept"], structure["relations"]),
            "context_rate": ratio(structure["context_kept"], structure["pii_elements"]),
            "documents_below_target": sum(
                1 for item in structured
                if min(item["structure_rate"] or 0, item["order_rate"] if item["order_rate"] is not None else 1)
                < STRUCTURE_TARGET
            ),
        },
        "detection": detection_metrics(labelled),
        "redaction": {
            "values": removable,
            "removed": redaction["removed"],
            "leaked": redaction["LEAKED"],
            "partly_leaked": redaction["PARTLY LEAKED"],
            "removal_rate": ratio(redaction["removed"], removable),
            "look_alikes": len(decoys),
            "kept": sum(1 for decoy in decoys if decoy["status"] == "kept"),
            "kept_rate": ratio(sum(1 for decoy in decoys if decoy["status"] == "kept"), len(decoys)),
            "redaction_errors": sum(result.get("redaction_errors", 0) for result in results),
        },
        "validation": {
            "verdicts": dict(Counter(result["verdict"] for result in results if result.get("verdict"))),
            "outcomes": dict(outcomes),
            "unsafe_releases": outcomes[UNSAFE_RELEASE],
            "over_blocks": outcomes[OVER_BLOCK],
            "gate_accuracy": ratio(outcomes[CORRECT_RELEASE] + outcomes[CORRECT_BLOCK], len(gated)),
            "gate_inconsistent": sum(1 for result in results if result.get("gate_consistent") is False),
            "exit_code_mismatches": sum(1 for result in results if result.get("exit_code_ok") is False),
        },
        "reports": {
            "values_found": sum(len(result.get("report_leaks", [])) for result in results),
            "count_mismatches": sum(1 for result in results if result.get("exposure_counts_agree") is False),
        },
        "snippets": snippets,
        "seconds": {step: round(seconds, 1) for step, seconds in timing.items()},
    }


def unsafe(metrics):
    """
    What happened that must never happen, whatever the accuracy.
    """

    problems = []

    if metrics["validation"]["unsafe_releases"]:
        problems.append(f"{metrics['validation']['unsafe_releases']} sanitized copies holding PII were released")
    if metrics["reports"]["values_found"]:
        problems.append(f"{metrics['reports']['values_found']} PII values are in shareable reports")
    if metrics["input_check"]["invalid_refused"] != metrics["input_check"]["invalid_files"]:
        problems.append("an invalid file was accepted")
    if metrics["validation"]["gate_inconsistent"]:
        problems.append("approved_copy() disagreed with a verdict")
    if metrics["documents"]["crashed"]:
        problems.append(f"{metrics['documents']['crashed']} documents crashed the pipeline")

    return problems


# --------------------------------------------------
# DETECTOR ALONE: archive/tests/evaluate_detector.py
# --------------------------------------------------

def snippet_benchmark(nlp):
    """
    The detector's precision, recall and F1 on the labelled text snippets
    of archive/tests/data/pii_ground_truth.json, per split, scored by
    evaluate_detector.py's own functions. None if it is not there.
    """

    if not SNIPPET_EVALUATOR.is_file():
        return None

    spec = importlib.util.spec_from_file_location("evaluate_detector", SNIPPET_EVALUATOR)
    module = importlib.util.module_from_spec(spec)

    try:
        with redirect_stdout(io.StringIO()):
            spec.loader.exec_module(module)
            examples = module.load_examples()
    except SystemExit:
        return None

    totals = {split: defaultdict(Counter) for split in module.SPLITS}

    for example in examples:
        predicted = module.predict(example, nlp)
        correct, missed, unexpected = module.compare(example["expected"], predicted)
        module.tally(totals[example["split"]], example, correct, missed, unexpected)

    benchmark = {}

    for split in module.SPLITS:
        metrics = module.metrics_of(totals[split])
        benchmark[split] = {
            "examples": sum(1 for example in examples if example["split"] == split),
            **metrics["overall"].get("all", {}),
            "by_type": metrics["type"],
        }

    return benchmark


# --------------------------------------------------
# REPORT
# --------------------------------------------------

def get(metrics, path):
    for key in path:
        if not isinstance(metrics, dict) or key not in metrics:
            return None
        metrics = metrics[key]

    return metrics


def show(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def seconds(value):
    return f"{value:.1f}"


def change(now, before, higher_is_better):
    if now is None or before is None or now == before:
        return ""

    delta = now - before
    better = (delta > 0) == higher_is_better
    sign = "+" if delta > 0 else ""
    number = f"{sign}{delta:.3f}" if isinstance(delta, float) else f"{sign}{delta}"

    return f"{number} ({'better' if better else 'WORSE'})"


def table(headers, rows):
    def cell(value):
        return show(value).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(value) for value in row) + " |" for row in rows]

    return "\n".join(lines)


def score_rows(breakdown):
    return [
        [name, row["tp"], row["fp"], row["fn"], row["precision"], row["recall"], row["f1"]]
        for name, row in breakdown.items()
    ]


SCORE_HEADERS = ["", "TP", "FP", "FN", "Precision", "Recall", "F1"]


def previous_metrics(current_dir, corpus, only):
    """
    The metrics of the latest earlier run over the same files (corpus
    and --only), and its folder name: scores over other files are not
    comparable.
    """

    for path in sorted(RESULTS_DIR.glob("*"), reverse=True):
        metrics = load(path / "metrics.json") if path != current_dir else None

        if metrics and metrics.get("corpus") == str(corpus) and metrics.get("only") == only:
            return metrics, path.name

    return None, None


def headline_rows(metrics, previous):
    return [
        [name, get(metrics, path), get(previous, path), change(get(metrics, path), get(previous, path), better)]
        for name, path, better in HEADLINE
    ]


def document_rows(results):
    rows = []

    for result in results:
        if not result["accepted"]:
            rows.append([
                result["file"], "-", "-", "-", "-", "-", "-", "-", "-", "refused",
                "-", "ok" if result["input_check_ok"] else "WRONG: should be processed",
                seconds(result["timings"]["total"]),
            ])
            continue

        pii = result.get("pii")
        counts = Counter("tp" if detected(label) else "fn" for label in pii or [])
        fp = len(result.get("false_positives", []))
        detection = scores({"tp": counts["tp"], "fp": fp, "fn": counts["fn"]}) if pii is not None else {}
        readable = sum(1 for label in pii or [] if label["extraction"] != "missing")
        leaked = sum(1 for label in pii or [] if label["redaction"] in ("LEAKED", "PARTLY LEAKED"))
        decoys = [decoy for decoy in result.get("not_pii", []) if decoy["status"] in ("kept", "removed")]

        rows.append([
            result["file"], result.get("format"),
            f"{readable}/{len(pii)}" if pii is not None else "-",
            detection.get("precision"), detection.get("recall"), detection.get("f1"),
            leaked if pii is not None else "-",
            f"{sum(1 for decoy in decoys if decoy['status'] == 'kept')}/{len(decoys)}" if pii is not None else "-",
            result.get("findings"), result.get("verdict") or "no verdict",
            "yes" if result.get("released") else "no",
            result.get("outcome") if result["input_check_ok"] else "WRONG: should be refused",
            seconds(result["timings"]["total"]),
        ])

    return rows


DOCUMENT_HEADERS = [
    "Document", "Format", "PII readable", "Precision", "Recall", "F1", "Leaked",
    "Look-alikes kept", "Findings", "Verdict", "Released", "Outcome", "Seconds",
]


def short(text, length=60):
    text = normalise(text)
    return text if len(text) <= length else text[:length - 3] + "..."


def problem_lines(result):
    """
    Everything that went wrong for one document, one line each.
    """

    lines = []

    if result["crash"]:
        lines.append(f"- **Crash:** {result['crash']}")
    elif result["message"] and result["accepted"]:
        lines.append(f"- **Stopped:** {result['message']}")

    if not result["input_check_ok"]:
        lines.append(f"- **Input check:** expected {result['expected_outcome']}, but "
                     f"{'accepted' if result['accepted'] else 'refused'}: {result['message']}")

    for label in result.get("pii", []):
        where = ", ".join(label["where"])
        value = f"`{normalise(label['text'])}` ({label['type']}, {where})"

        if label["extraction"] == "missing":
            lines.append(f"- **Not extracted:** {value}; closest text `{label['extracted_as']}` "
                         f"({label['similarity']})")
            continue

        if label["extraction"] == "approximate":
            lines.append(f"- **Extracted with OCR damage:** {value} read as `{label['extracted_as']}` "
                         f"({label['similarity']})")

        if not detected(label):
            lines.append(f"- **Missed by detection:** {value}: {label['detection']}")
        elif label["detection"] == "found (approximate)":
            lines.append(f"- **Found, not exactly:** {value} found as `{label['found_as']}`")

        if label["redaction"] in ("LEAKED", "PARTLY LEAKED"):
            lines.append(f"- **{label['redaction']} into the sanitized copy:** {value}")

    for item in result.get("false_positives", []):
        lines.append(f"- **False positive:** `{normalise(item['text'])}` as {item['type']} "
                     f"({item['detector']} {item['rule_id']}; {item['place']}, {item['block_id']})")

    for decoy in result.get("not_pii", []):
        if decoy["status"] == "removed":
            lines.append(f"- **Over-redacted:** `{decoy['text']}` ({decoy['why']})")

    for leak in result.get("report_leaks", []):
        lines.append(f"- **PII in shareable report {leak['file']}:** `{leak['text']}` ({leak['type']})")

    structure = result.get("structure") or {}

    for element in structure.get("problems", []):
        lines.append(f"- **Structure:** `{short(element['text'])}` ({element['kind']}, {element['where']}): "
                     f"{element['status']}")

    for before, after in structure.get("broken", []):
        lines.append(f"- **Reading order:** `{short(after)}` does not follow `{short(before)}`")

    for item in structure.get("context_lost", []):
        lines.append(f"- **Context around a redaction:** expected `{short(item['expected'], 90)}`, "
                     f"sanitized `{short(item['actual'], 90)}`")

    if result.get("gate_consistent") is False:
        lines.append(f"- **Gate disagrees with verdict {result['verdict']}:** {result['gate_reason']}")

    if result.get("exit_code_ok") is False:
        lines.append(f"- **Exit code {result['exit_code']}** does not match verdict {result['verdict']}")

    if result.get("exposure_counts_agree") is False:
        lines.append("- **Exposure report counts** differ from the PII report or redaction summary")

    return lines


def write_summary(path, metrics, results, previous, previous_name, corpus):
    out = [
        "# Pipeline evaluation",
        "",
        f"Run {path.parent.name} on `{corpus}`: {metrics['documents']['total']} files, "
        f"{metrics['documents']['labelled']} with labels. Scanner {SCANNER_VERSION}, redactor "
        f"{REDACTOR_VERSION}, validator {VALIDATOR_VERSION}, exposure report {EXPOSURE_VERSION}.",
        "",
        "> This summary quotes the labelled PII values. Share it only if every document is synthetic.",
        "",
    ]

    problems = unsafe(metrics)
    out += [f"**UNSAFE:** {problem}." for problem in problems] or ["No unsafe outcome."]

    out += [
        "",
        "## Headline",
        "",
        table(["Metric", "Now", f"Previous ({previous_name or 'none'})", "Change"], headline_rows(metrics, previous)),
        "",
        "## Per document",
        "",
        table(DOCUMENT_HEADERS, document_rows(results)),
        "",
        "Outcome: *correct release* (PASS, no PII left), *correct block* (not released, PII left), "
        "*over-block* (not released, though no PII was left), *UNSAFE release* (released with PII left).",
        "",
        "## Step 1: extraction",
        "",
        table(
            ["Values labelled", "Exact", "Approximate (OCR damage)", "Missing", "Readable"],
            [[metrics["extraction"][key] for key in ("values", "exact", "approximate", "missing", "recall")]]
        ),
        "",
        "## Step 1b: structure and context",
        "",
        "Each element the labels list (heading, paragraph, list item, table cell, image text, header, "
        "footer, notes, comment) is *kept* if it is found as the right kind, in the right place (and a "
        "cell in its row and column). *Neighbours kept* counts the pairs of elements next to each other "
        "in the body's reading order that are next to each other in the extraction too. *Context kept* "
        "counts the elements holding PII whose sanitized text is the extracted text with only the PII "
        f"replaced by tags. Target: at least {STRUCTURE_TARGET} of the elements and of the neighbours "
        "kept in every document.",
        "",
        table(
            ["Document", "Elements", "Found", "Kept", "Elements kept", "Neighbours kept", "Context kept", "Target met"],
            [
                [
                    result["file"], item["elements"], item["found"], item["kept"], item["structure_rate"],
                    item["order_rate"], item["context_rate"],
                    "yes" if min(item["structure_rate"] or 0, item["order_rate"] if item["order_rate"] is not None else 1)
                    >= STRUCTURE_TARGET else "NO",
                ]
                for result in results if (item := result.get("structure"))
            ] + [[
                "all", metrics["structure"]["elements"], metrics["structure"]["found"],
                metrics["structure"]["kept"], metrics["structure"]["structure_rate"],
                metrics["structure"]["order_rate"], metrics["structure"]["context_rate"], "",
            ]]
        ),
        "",
        "## Step 2: detection",
        "",
        "Each labelled value counts once per document. A finding matches a label of the same type with "
        f"the same value, or one at least {FUZZY_MATCH} alike (OCR damage, a title kept).",
        "",
    ]

    for title, key in (
        ("Overall", None), ("By PII type", "by_type"), ("By format", "by_format"),
        ("By place in the document (recall only)", "by_place"), ("By detector and rule (precision only)", "by_detector"),
    ):
        breakdown = {"all": metrics["detection"]["overall"]} if key is None else metrics["detection"][key]
        out += [f"### {title}", "", table(SCORE_HEADERS, score_rows(breakdown)), ""]

    redaction = metrics["redaction"]
    out += [
        "## Step 3: redaction",
        "",
        table(
            ["PII values extracted", "Removed", "Leaked", "Partly leaked", "Removed rate",
             "Look-alikes", "Kept", "Kept rate", "Redaction errors"],
            [[redaction[key] for key in (
                "values", "removed", "leaked", "partly_leaked", "removal_rate",
                "look_alikes", "kept", "kept_rate", "redaction_errors"
            )]]
        ),
        "",
        "## Step 4: validation and release",
        "",
        table(
            ["Document", "Verdict", *pipeline_checks(results), "Released", "Gate reason"],
            [
                [
                    result["file"], result.get("verdict"),
                    *[result.get("checks", {}).get(check) for check in pipeline_checks(results)],
                    "yes" if result.get("released") else "no",
                    (result.get("gate_reason") or "").split(": see ")[0] or None,
                ]
                for result in results if result["accepted"]
            ]
        ),
        "",
        table(
            ["Unsafe releases", "Over-blocks", "Gate correct", "Gate inconsistent", "Exit code mismatches"],
            [[metrics["validation"][key] for key in (
                "unsafe_releases", "over_blocks", "gate_accuracy", "gate_inconsistent", "exit_code_mismatches"
            )]]
        ),
        "",
        "## Step 5: shareable reports",
        "",
        f"PII values found in validation and exposure reports: {metrics['reports']['values_found']}. "
        f"Exposure reports whose counts disagree with the PII report: {metrics['reports']['count_mismatches']}.",
        "",
    ]

    snippets = metrics["snippets"]

    if snippets:
        out += [
            "## Detector alone: labelled snippets",
            "",
            f"`{SNIPPET_EVALUATOR.relative_to(REPO).as_posix()}` on its dataset. Tune rules on *dev* only; "
            "*eval* shows whether a change holds on text it was not tuned on.",
            "",
        ]

        for split, row in snippets.items():
            out += [
                f"### {split} ({row['examples']} examples)", "",
                table(SCORE_HEADERS, score_rows({"all": row, **row["by_type"]})), "",
            ]
    else:
        out += ["## Detector alone: labelled snippets", "", f"Skipped: `{SNIPPET_EVALUATOR}` not found.", ""]

    out += [
        "## Time (seconds, all documents)", "",
        table(list(metrics["seconds"]), [[seconds(value) for value in metrics["seconds"].values()]]), "",
    ]

    out += ["## What went wrong, per document", ""]

    for result in results:
        lines = problem_lines(result)
        out += [f"### {result['file']}", "", *(lines or ["Nothing."]), ""]

    path.write_text("\n".join(out), encoding="utf-8")


def pipeline_checks(results):
    for result in results:
        if result.get("checks"):
            return list(result["checks"])

    return []


def print_console(metrics, previous, previous_name, results, run_dir):
    print()
    print(f"{'Metric':34}{'Now':>9}{'Previous':>10}  Change")

    for name, now, before, delta in headline_rows(metrics, previous):
        print(f"{name:34}{show(now):>9}{show(before):>10}  {delta}")

    print(f"\n{'Document':38}{'Format':16}{'Readable':>9}{'P':>7}{'R':>7}{'F1':>7}{'Leaked':>7}  {'Verdict':13}Outcome")

    for row in document_rows(results):
        print(f"{row[0]:38}{show(row[1]):16}{show(row[2]):>9}{show(row[3]):>7}{show(row[4]):>7}"
              f"{show(row[5]):>7}{show(row[6]):>7}  {show(row[9]):13}{row[11]}")

    problems = unsafe(metrics)

    print()

    for problem in problems:
        print(f"UNSAFE: {problem}")

    print(f"\nSummary: {run_dir / 'summary.md'}")
    print(f"Compared with: {previous_name or 'no earlier run'}")


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Score every pipeline step on labelled documents.")
    parser.add_argument("--corpus", type=Path, default=CORPUS_DIR, help="folder of documents and their .labels.json")
    parser.add_argument("--only", help='file name pattern, e.g. "*.pdf" or "hr_*"')
    parser.add_argument("--rebuild", action="store_true", help="rebuild the synthetic corpus first")
    parser.add_argument("--no-snippets", action="store_true", help="skip the detector snippet benchmark")
    args = parser.parse_args()

    corpus = args.corpus.resolve()

    if args.rebuild or (corpus == CORPUS_DIR and not build_corpus.SYNTHETIC_DIR.is_dir()):
        print("Building the synthetic corpus ...")
        build_corpus.build()

    files = corpus_files(corpus, args.only)

    if not files:
        sys.exit(f"No PDF, DOCX or PPTX files in {corpus}" + (f" matching {args.only}" if args.only else ""))

    run_dir = RESULTS_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)

    print("Loading spaCy ...")
    nlp = LocalNLPDetector()

    if nlp.error:
        print(f"Warning: {nlp.error} Person names will not be detected: the scores will show it.")

    results = []

    for number, source in enumerate(files, start=1):
        name = source.relative_to(corpus).as_posix()
        print(f"[{number}/{len(files)}] {name} ...", end=" ", flush=True)

        run = run_pipeline(source, run_dir / "runs" / name.replace("/", "__"), nlp)
        result = score_document(source, corpus, load_labels(source), run)
        results.append(result)

        print(f"{result.get('verdict') or ('refused' if not result['accepted'] else 'no verdict')} "
              f"({run['timings']['total']}s)")

    snippets = None

    if not args.no_snippets:
        print("Detector snippet benchmark ...")
        snippets = snippet_benchmark(nlp)

    metrics = corpus_metrics(results, snippets)
    previous, previous_name = previous_metrics(run_dir, corpus, args.only)

    (run_dir / "metrics.json").write_text(json.dumps({
        "run": run_dir.name,
        "corpus": str(corpus),
        "only": args.only,
        "versions": {
            "scanner": SCANNER_VERSION, "redactor": REDACTOR_VERSION,
            "validator": VALIDATOR_VERSION, "exposure": EXPOSURE_VERSION,
        },
        **metrics,
    }, indent=2), encoding="utf-8")
    (run_dir / "details.json").write_text(
        json.dumps({"documents": results}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    write_summary(run_dir / "summary.md", metrics, results, previous, previous_name, corpus)

    print_console(metrics, previous, previous_name, results, run_dir)

    sys.exit(1 if unsafe(metrics) else 0)


if __name__ == "__main__":
    main()
