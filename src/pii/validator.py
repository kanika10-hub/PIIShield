"""
Check a sanitized document (redactor.py) before any of it is sent to
an LLM.

    python -m src.pii.validator output/risk_policy.json [output/risk_policy_pii.json]

reads the extracted document <name>.json, its PII report <name>_pii.json
and <name>_sanitized.json, and writes <name>_validation.json.

Checks, in order:

    provenance      the report and the sanitized copy belong to this
                    document, and the copy was made from this report
    report          the report's offsets point at its values in the
                    original; overlapping findings are listed
    replacements    every finding meant for redaction was replaced: the
                    redactor recorded a tag for it, and its value is
                    gone from its block
    ocr             OCR words over a redacted value hold only tags;
                    OCR text too unreliable to scan and images never
                    read are listed for review
    extraction      content the extractor could not read: declared
                    limitations, embedded objects never opened, pages
                    with little or no text, listed for review
    rescan          the scanner finds no PII in the sanitized copy
    known_values    no value found anywhere in the document is left
                    anywhere in the sanitized copy, nor part of a name
    structure       pages, blocks, tables, word boxes and reading order
                    are unchanged, and text differs only where a
                    redaction tag stands

Each check, and the verdict, is one of:

    PASS            nothing found
    FAIL            PII is known to remain, or the sanitized copy does
                    not match its document and report
    NEEDS_REVIEW    nothing known to remain, but something could not be
                    confirmed: part of a name, OCR text too unreliable
                    to scan, an image never read

The verdict is the worst of the checks. Only a PASS document may be
sent to an LLM, or a NEEDS_REVIEW one whose every issue a reviewer has
signed off for this exact copy (<name>_review.json, see review.py and
review_approval). A FAIL can never be signed off. approved_copy() is the
way to load a copy for that, and refuses any other, or a copy changed
since it was validated.

Issues name the block, finding and rule, never the PII itself, so the
validation report can be shared. Look a finding_id up in the PII report
to see the value.
"""

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.pii.detectors import NAME_WORD, LocalNLPDetector, lowercase_words
from src.pii.document_scanner import (
    LOW_OCR_CONFIDENCE, LOW_OCR_WORD_CONFIDENCE, findings_path, scan_document,
    uncertain_value_words, word_lists
)
from src.pii.redactor import (
    REDACTION_TAG, check_finding, merge_spans, output_paths, save_json,
    text_slots, walk
)


# Change whenever the checks or the release rule change
VALIDATOR_VERSION = "1.5.0"

PASS, NEEDS_REVIEW, FAIL = "PASS", "NEEDS_REVIEW", "FAIL"
SEVERITY = {PASS: 0, NEEDS_REVIEW: 1, FAIL: 2}

EXIT_CODES = {PASS: 0, FAIL: 1, NEEDS_REVIEW: 2}

# Issues kept per check (issues_total counts them all), and places kept
# per issue (occurrences counts them all)
MAX_ISSUES = 100
MAX_LOCATIONS = 10


def issue(severity, reason, **where):
    """
    One problem a check found. where: block_id, finding_id, page_number,
    occurrences, locations. Never the PII value itself.
    """

    return {
        "severity": severity,
        **{key: value for key, value in where.items() if value is not None},
        "reason": reason,
    }


def worst(statuses):
    return max(statuses, key=SEVERITY.get, default=PASS)


def is_false_positive(item):
    return item.get("review_status") == "false_positive"


# The type of OCR text a reviewer hid because it was read too badly to
# scan (review.py). A misreading is not a value: it is not searched for
# anywhere else in the copy, nor in the exposure report
UNREADABLE_TEXT = "UNREADABLE_TEXT"


def is_unreadable_text(item):
    return item.get("type") == UNREADABLE_TEXT


# --------------------------------------------------
# TEXT HELPERS
# --------------------------------------------------

def normalise(text):
    return " ".join(text.split())


def only_tags(text):
    """
    True if text is one or more redaction tags and nothing else.
    """

    return bool(text) and not REDACTION_TAG.sub("", text)


def fully_redacted(text):
    """
    True if text holds redaction tags and nothing else but whitespace.
    """

    return bool(REDACTION_TAG.search(text or "")) and not REDACTION_TAG.sub("", text).strip()


def only_tags_differ(original, sanitized):
    """
    True if sanitized is original with some parts (each at least one
    character) replaced by redaction tags, and nothing else changed.

    The text between tags is matched, in order, at its first place
    after the previous one. For a pattern of fixed parts and gaps this
    first place is always a safe choice, so no backtracking is needed.
    """

    parts = REDACTION_TAG.split(sanitized)

    if len(parts) == 1:
        return original == sanitized

    first, *middle, last = parts

    if not original.startswith(first):
        return False

    position = len(first)

    for part in middle:
        found = original.find(part, position + 1)

        if found < 0:
            return False

        position = found + len(part)

    return len(original) - len(last) > position and original.endswith(last)


def value_pattern(value):
    """
    A regex for value with any whitespace between its words, not as part
    of a longer word: "Fir" is not found in "First".
    """

    words = value.split()

    if not words:
        return re.escape(value)

    pattern = r"\s+".join(re.escape(word) for word in words)

    if re.match(r"\w", words[0][0]):
        pattern = r"(?<!\w)" + pattern

    if re.match(r"\w", words[-1][-1]):
        pattern += r"(?!\w)"

    return pattern


def alternation(values):
    """
    One regex matching any of values, longest first. None if no values.
    """

    if not values:
        return None

    return re.compile("|".join(
        f"(?:{value_pattern(value)})"
        for value in sorted(values, key=len, reverse=True)
    ))


def strings(node, block_id=None, path=""):
    """
    Every string in a document, as (block_id, path, text): the block it
    belongs to (a word belongs to the block it is part of; None outside
    blocks) and its path inside that block.
    """

    if isinstance(node, str):
        yield block_id, path, node

    elif isinstance(node, dict):
        if "block_id" in node and node.get("block_type") != "word":
            block_id, path = node["block_id"], ""

        for key, value in node.items():
            yield from strings(value, block_id, f"{path}.{key}" if path else key)

    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from strings(value, block_id, f"{path}[{index}]")


def blocks_by_id(document):
    return {
        block["block_id"]: (page["page_number"], block)
        for page in document["pages"]
        for block in walk(page["blocks"])
    }


def findings_to_redact(document, report):
    """
    The report's findings that should be redacted and can be: not
    reviewed as false positives, and with valid offsets. By block_id.
    """

    slots = text_slots(document)
    by_block = defaultdict(list)

    for item in report.get("findings", []):
        if not is_false_positive(item) and check_finding(item, slots) is None:
            by_block[item["block_id"]].append(item)

    return by_block


# --------------------------------------------------
# CHECKS
# Each returns (issues, notes).
# --------------------------------------------------

def check_provenance(document, report, sanitized):
    """
    The report and the sanitized copy belong to this document, and the
    copy is the sanitized one, made from this run of the report.
    """

    issues = []
    notes = []

    if report.get("status") == "failed":
        issues.append(issue(FAIL, "the PII scan failed: there are no findings to check against"))

    for field in ("document_id", "file_name"):
        for source, data in (("PII report", report), ("sanitized copy", sanitized)):
            if data.get(field) != document.get(field):
                issues.append(issue(
                    FAIL, f"the {source} is for {field} {data.get(field)!r}, "
                          f"not {document.get(field)!r}"
                ))

    redaction = sanitized.get("metadata", {}).get("redaction")

    if redaction is None:
        issues.append(issue(FAIL, "the sanitized copy has no redaction record: redactor.py did not make it"))
        return issues, notes

    if redaction.get("variant") != "sanitized":
        issues.append(issue(FAIL, f"this is the {redaction.get('variant')} copy, not the sanitized one"))

    if redaction.get("report_scan_timestamp") != report.get("scan_timestamp"):
        issues.append(issue(FAIL, "the sanitized copy was made from another run of the PII report"))

    # Errors about one finding are checked under replacements
    for error in redaction.get("errors", []):
        if not error.get("finding_id"):
            issues.append(issue(
                NEEDS_REVIEW, f"redactor: {error['reason']}", block_id=error.get("block_id")
            ))

    notes.append(f"redactor status: {redaction.get('status')}")

    if redaction.get("warnings"):
        notes.append(
            f"{len(redaction['warnings'])} blocks had OCR words that could not be placed: "
            "all their words were hidden"
        )

    return issues, notes


def check_report_findings(document, report):
    """
    Every finding's offsets point at its value in the original text.
    Overlapping findings are listed: the redactor replaces each group
    as one span.
    """

    issues = []
    notes = []
    findings = report.get("findings", [])
    slots = text_slots(document)

    for finding_id, count in Counter(item.get("finding_id") for item in findings).items():
        if count > 1:
            issues.append(issue(FAIL, f"{count} findings share this finding_id", finding_id=finding_id))

    for item in findings:
        problem = check_finding(item, slots)

        if problem:
            issues.append(issue(
                FAIL, problem, block_id=item.get("block_id"), finding_id=item.get("finding_id")
            ))

    groups = [
        f"{block_id} ({', '.join(item['finding_id'] for item in span['items'])})"
        for block_id, items in findings_to_redact(document, report).items()
        for span in merge_spans(items)
        if len(span["items"]) > 1
    ]

    if groups:
        notes.append(
            f"{len(groups)} groups of overlapping findings, each redacted as one span: "
            + ", ".join(groups[:MAX_LOCATIONS])
        )

    false_positives = sum(1 for item in findings if is_false_positive(item))

    if false_positives:
        notes.append(f"{false_positives} findings were reviewed as false positives and are kept")

    notes.append(f"{len(findings)} findings checked")

    return issues, notes


def check_replacements(report, sanitized):
    """
    Every finding meant for redaction has a successful replacement: the
    redactor recorded a tag for it, the tag is in its block, and the
    value is no longer there.
    """

    issues = []
    redaction = sanitized.get("metadata", {}).get("redaction") or {}
    audit = {entry.get("finding_id"): entry for entry in redaction.get("audit", [])}
    slots = text_slots(sanitized)
    replaced = 0

    for item in report.get("findings", []):

        if is_false_positive(item):
            continue

        where = {"block_id": item.get("block_id"), "finding_id": item.get("finding_id")}
        entry = audit.get(item.get("finding_id"))

        if entry is None:
            issues.append(issue(FAIL, "no redaction is recorded for this finding", **where))
            continue

        if entry.get("status") != "redacted":
            issues.append(issue(
                FAIL, f"not redacted: {entry.get('reason') or entry.get('status')}", **where
            ))
            continue

        if item.get("block_id") not in slots:
            issues.append(issue(FAIL, "the block is not in the sanitized copy", **where))
            continue

        holder, key = slots[item["block_id"]]
        text = holder.get(key) or ""
        replacement = entry.get("replacement") or ""

        if not REDACTION_TAG.fullmatch(replacement) or replacement not in text:
            issues.append(issue(FAIL, "the recorded redaction tag is not in the block", **where))

        # Searched outside the tags: a hidden OCR misreading such as ":" or
        # "REDACTED" is not left behind because a tag spells it
        elif re.search(value_pattern(item["text"]), REDACTION_TAG.sub("\0", text)):
            issues.append(issue(FAIL, "the value is still in its block", **where))

        else:
            replaced += 1

    return issues, [f"{replaced} findings replaced"]


# The reasons check_ocr gives a low-confidence block and low-confidence
# words that look like part of a value: keep them in step
UNREADABLE_REASON = re.compile(
    r"^OCR confidence \S+ is below \d+:|^\d+ OCR words that look like part of a value"
)


def unreadable_issue(check_name, item):
    """
    True for an ocr issue about OCR text read too badly to scan, whose
    text a reviewer can hide: a low-confidence block, or low-confidence
    words that look like part of a value. Not an image never read: it
    has no text.
    """

    return (
        check_name == "ocr"
        and item.get("severity") == NEEDS_REVIEW
        and bool(item.get("block_id"))
        and UNREADABLE_REASON.search(item.get("reason") or "") is not None
    )


def check_ocr(document, report, sanitized):
    """
    OCR content the redaction cannot vouch for:

        a word (PDF word child, image OCR word) over a redacted value
        that still holds text: FAIL
        a block read by OCR with low confidence: misread PII ("Passpon
        TK4820013") escapes detection, so a person must read it. Not
        once the redaction hid all of its text
        in a block read well on the whole, a low-confidence word that
        looks like part of a value and was not redacted: the same risk
        an image that could not be OCR'd: its text was never scanned

    A reviewer can hide the text of the first two (review.py, see
    unreadable_issue).
    """

    issues = []
    notes = []
    original_blocks = blocks_by_id(document)
    sanitized_blocks = blocks_by_id(sanitized)
    images_with_values = 0
    words_hidden = 0

    for block_id, items in findings_to_redact(document, report).items():

        # File properties and comment authors have no words
        if block_id not in original_blocks or block_id not in sanitized_blocks:
            continue

        page_number, original = original_blocks[block_id]
        _, sanitized_block = sanitized_blocks[block_id]

        if original["block_type"] == "image_ocr":
            images_with_values += 1

        tokens = list(re.finditer(r"\S+", original["text"]))

        for words, sanitized_words in zip(word_lists(original), word_lists(sanitized_block)):

            # Words that do not match the text cannot be placed: all must be hidden
            placed = [token.group() for token in tokens] == [word.get("text") for word in words]

            for index, sanitized_word in enumerate(sanitized_words):
                covering = None

                if placed and index < len(tokens):
                    token = tokens[index]
                    covering = [
                        item for item in items
                        if item["start"] < token.end() and token.start() < item["end"]
                    ]

                    if not covering:
                        continue

                if only_tags(sanitized_word.get("text")):
                    words_hidden += 1
                    continue

                issues.append(issue(
                    FAIL,
                    f"word {index + 1} is over a redacted value but still holds text"
                    if covering else
                    f"word {index + 1} could not be placed in the block text and was not hidden",
                    block_id=block_id, page_number=page_number,
                    finding_id=covering[0]["finding_id"] if covering else None
                ))

    uncertain_words = 0
    unreadable_hidden = 0

    for block_id, (page_number, block) in original_blocks.items():
        confidence = block.get("metadata", {}).get("ocr_confidence")
        low_confidence = (
            block["block_type"] != "word"
            and confidence is not None
            and confidence < LOW_OCR_CONFIDENCE
            and normalise(block.get("text") or "")
        )

        # A block whose text the redaction hid in full leaves nothing to misread
        if low_confidence and block_id in sanitized_blocks and fully_redacted(sanitized_blocks[block_id][1].get("text")):
            unreadable_hidden += 1

        elif low_confidence:
            issues.append(issue(
                NEEDS_REVIEW,
                f"OCR confidence {confidence} is below {LOW_OCR_CONFIDENCE}: "
                "misread PII cannot be detected",
                block_id=block_id, page_number=page_number
            ))

        # A well-read block can still hold one badly read value. Words
        # the redaction already hid are no risk.
        elif block_id in sanitized_blocks:
            sanitized_lists = word_lists(sanitized_blocks[block_id][1])
            uncertain = [
                index
                for words, sanitized_words in zip(word_lists(block), sanitized_lists)
                for index in uncertain_value_words(words)
                if index >= len(sanitized_words) or not only_tags(sanitized_words[index].get("text"))
            ]

            if uncertain:
                uncertain_words += len(uncertain)
                issues.append(issue(
                    NEEDS_REVIEW,
                    f"{len(uncertain)} OCR words that look like part of a value were read with "
                    f"confidence below {LOW_OCR_WORD_CONFIDENCE}: a misread value cannot be detected",
                    block_id=block_id, page_number=page_number
                ))

        if block["block_type"] == "image":
            issues.append(issue(
                NEEDS_REVIEW, "the image could not be OCR'd: text in it was never scanned",
                block_id=block_id, page_number=page_number
            ))

    notes.append(f"{words_hidden} OCR words over redacted values hold only tags")

    if unreadable_hidden:
        notes.append(f"{unreadable_hidden} low-confidence OCR blocks are fully redacted")

    if uncertain_words:
        notes.append(f"{uncertain_words} low-confidence OCR words that look like values need a look")

    if images_with_values:
        notes.append(
            f"{images_with_values} images held redacted values: the image files themselves "
            "still show them (see image.word_boxes in the PII report)"
        )

    return issues, notes


def check_extraction(document):
    """
    Content the extractor could not read, so the scan never saw it. A
    person must check the original for each:

        a limitation the extractor declared itself
        an embedded object (a spreadsheet in a slide), never opened
        a page with little or no text: a scan or figure nothing read

    Unread images and low-confidence OCR are checked under ocr.
    """

    issues = []
    blocks = blocks_by_id(document)

    for limitation in document.get("metadata", {}).get("limitations", []):
        issues.append(issue(NEEDS_REVIEW, f"the extractor could not read everything: {limitation}"))

    for page in document["pages"]:
        if page.get("metadata", {}).get("low_text"):
            issues.append(issue(
                NEEDS_REVIEW, "little or no text was extracted: PII on this page may never have been read",
                page_number=page["page_number"]
            ))

    for block_id, (page_number, block) in blocks.items():
        if block["block_type"] == "embedded_object":
            issues.append(issue(
                NEEDS_REVIEW, "the embedded object was not opened: its content was never scanned",
                block_id=block_id, page_number=page_number
            ))

    notes = [f"{len(document['pages'])} pages checked"]
    charts = sum(1 for _, block in blocks.values() if block["block_type"] == "chart")

    if charts:
        notes.append(f"{charts} charts: their titles and labels were scanned, not the data behind them")

    return issues, notes


def check_rescan(report, sanitized, nlp_detector, misreading=None, failed=None):
    """
    Run the scanner over the sanitized copy. Anything it finds is PII
    the redaction left behind, except a value a reviewer already marked
    as a false positive in that block: it was kept on purpose.

    misreading(finding): True for a finding that is only OCR noise (the
    redacted file's images read again, src/original_format/validator.py).
    The sanitized copy is not read again, so its check passes none.
    failed: a list the findings that fail are added to (the redacted
    file's check places them, to redact them in another pass).
    """

    issues = []
    nlp_error = "no NLP detector was given" if nlp_detector is None else getattr(nlp_detector, "error", None)

    if nlp_error:
        issues.append(issue(NEEDS_REVIEW, f"person names were not rescanned: {nlp_error}"))

    kept = {
        (item.get("block_id"), normalise(item.get("text") or ""))
        for item in report.get("findings", [])
        if is_false_positive(item)
    }

    rescan = scan_document(sanitized, nlp_detector)
    excused = 0
    misread = 0

    for item in rescan["findings"]:
        if (item["block_id"], normalise(item["text"])) in kept:
            excused += 1
            continue

        if misreading is not None and misreading(item):
            misread += 1
            continue

        if failed is not None:
            failed.append(item)

        issues.append(issue(
            FAIL, f"{item['type']} found by {item['rule_id']}",
            block_id=item["block_id"], page_number=item["page_number"]
        ))

    notes = [f"{rescan['summary']['blocks_scanned']} blocks rescanned"]

    if excused:
        notes.append(f"{excused} detections are values reviewed as false positives")

    if misread:
        notes.append(
            f"{misread} detections are OCR noise: the redaction marks read again, or low-confidence misreadings "
            "of pixels that were not changed and were read with confidence before, with no PII"
        )

    return issues, notes


def searched_values(document, report):
    """
    What check_known_values searches the sanitized copy for, each mapped
    to the first finding it came from: (values, name_parts).

        values          every value the report found, but a name found
                        as one word
        name_parts      each word of a name the report found, and a name
                        found as one word, unless the document also uses
                        it in lowercase (an ordinary word)

    Unreadable OCR text a reviewer hid (UNREADABLE_TEXT) is a misreading,
    not a value, and is not searched for. review.py hides the name parts
    left behind with the same rule.
    """

    findings = [
        item for item in report.get("findings", [])
        if not is_false_positive(item) and not is_unreadable_text(item) and normalise(item.get("text") or "")
    ]

    values = {}

    for item in findings:
        if item["type"] != "PERSON_NAME" or len(item["text"].split()) > 1:
            values.setdefault(normalise(item["text"]), item["finding_id"])

    vocabulary = lowercase_words(
        block.get("text") or ""
        for _, block in blocks_by_id(document).values()
    )

    name_parts = {}

    for item in findings:
        if item["type"] != "PERSON_NAME":
            continue

        for word in item["text"].split():
            part = word.strip(".,;:()[]")

            if NAME_WORD.fullmatch(part) and part.lower() not in vocabulary and part not in values:
                name_parts.setdefault(part, item["finding_id"])

    return values, name_parts


def check_known_values(document, report, sanitized):
    """
    The scan finds a value in one place but can miss it in another: a
    table without the header that gave it away, OCR text, a name split
    across blocks. Every string of the sanitized copy (text, words,
    metadata) is searched for (see searched_values):

        every value the report found: FAIL
        each word of a name the report found ("Ifeoma" of "Ifeoma C.
        Adeyemi"), and a name found as one word (a surname mention):
        NEEDS_REVIEW, as a word alone may not identify anyone. The scan
        decides by context whether such a word names a person ("Ford"
        is reported, "Ford Motor Company" is not). Words the document
        also uses in lowercase are ordinary words and are not searched.
    """

    values, name_parts = searched_values(document, report)

    value_regex = alternation(values)
    part_regex = alternation(name_parts)
    found = defaultdict(list)       # (severity, finding_id) -> locations
    searched = 0

    for block_id, path, text in strings(sanitized):
        searched += 1
        location = f"{block_id}.{path}" if block_id else path
        full = []

        if value_regex:
            for match in value_regex.finditer(text):
                full.append((match.start(), match.end()))
                found[(FAIL, values[normalise(match.group())])].append(location)

        if part_regex:
            for match in part_regex.finditer(text):
                if not any(start < match.end() and match.start() < end for start, end in full):
                    found[(NEEDS_REVIEW, name_parts[match.group()])].append(location)

    issues = [
        issue(
            severity,
            "a value the scan found elsewhere is still in the sanitized copy"
            if severity == FAIL else
            "part of a name the scan found elsewhere is still in the sanitized copy",
            finding_id=finding_id,
            occurrences=len(locations),
            locations=list(dict.fromkeys(locations))[:MAX_LOCATIONS]
        )
        for (severity, finding_id), locations in found.items()
    ]

    issues.sort(key=lambda item: (-SEVERITY[item["severity"]], -item["occurrences"]))

    return issues, [
        f"{len(values)} values and {len(name_parts)} name parts searched "
        f"in {searched} strings"
    ]


def metadata_matches(key, original, sanitized):
    """
    Block metadata may only differ where redaction rewrites text: a
    comment's author, OCR words, a continued table's column headers.
    """

    if original == sanitized:
        return True

    if key == "author":
        return isinstance(original, str) and isinstance(sanitized, str) and only_tags_differ(original, sanitized)

    if key == "words":
        return (
            isinstance(original, list) and isinstance(sanitized, list)
            and len(original) == len(sanitized)
            and all(
                set(old) == set(new)
                and all(old[field] == new[field] for field in old if field != "text")
                and only_tags_differ(old.get("text") or "", new.get("text") or "")
                for old, new in zip(original, sanitized)
            )
        )

    if key == "column_headers":
        return (
            isinstance(original, dict) and isinstance(sanitized, dict)
            and set(original) == set(sanitized)
            and all(only_tags_differ(original[column], sanitized[column]) for column in original)
        )

    return False


def block_differences(original, sanitized):
    """
    The fields of a block that changed other than by redaction.
    """

    differences = [
        key
        for key in sorted(set(original) | set(sanitized))
        if key not in ("text", "metadata", "children") and original.get(key) != sanitized.get(key)
    ]

    if not only_tags_differ(original.get("text") or "", sanitized.get("text") or ""):
        differences.append("text")

    original_metadata = original.get("metadata", {})
    sanitized_metadata = sanitized.get("metadata", {})

    for key in sorted(set(original_metadata) | set(sanitized_metadata)):
        if not metadata_matches(key, original_metadata.get(key), sanitized_metadata.get(key)):
            differences.append(f"metadata.{key}")

    return differences


def compare_blocks(original_blocks, sanitized_blocks, issues, parent_id=None):
    """
    Compare two block lists position by position, children included,
    so a block that moved is reported as well as one that changed.
    Returns the number of blocks compared.
    """

    if len(original_blocks) != len(sanitized_blocks):
        issues.append(issue(
            FAIL, f"{len(sanitized_blocks)} blocks where the original has {len(original_blocks)}",
            block_id=parent_id
        ))
        return 0

    compared = 0

    for original, sanitized in zip(original_blocks, sanitized_blocks):
        differences = block_differences(original, sanitized)

        if differences:
            issues.append(issue(
                FAIL, f"changed: {', '.join(differences)}", block_id=original.get("block_id")
            ))

        compared += 1 + compare_blocks(
            original.get("children", []), sanitized.get("children", []),
            issues, original.get("block_id")
        )

    return compared


def check_structure(document, sanitized):
    """
    The sanitized copy is the original document with only redaction
    tags in place of values: the same pages, blocks, tables and cells
    in the same reading order, with the same boxes and metadata.
    """

    issues = []

    for field in ("document_id", "file_name", "source_type", "extraction_method"):
        if document.get(field) != sanitized.get(field):
            issues.append(issue(FAIL, f"{field} changed"))

    original_metadata = document.get("metadata", {})
    sanitized_metadata = {
        key: value
        for key, value in sanitized.get("metadata", {}).items()
        if key != "redaction"
    }

    for key in sorted(set(original_metadata) | set(sanitized_metadata)):
        original, new = original_metadata.get(key), sanitized_metadata.get(key)

        if key == "properties" and isinstance(original, dict) and isinstance(new, dict):
            unchanged = set(original) == set(new) and all(
                only_tags_differ(value, new[name]) if isinstance(value, str) else value == new[name]
                for name, value in original.items()
            )
        else:
            unchanged = original == new

        if not unchanged:
            issues.append(issue(FAIL, f"document metadata.{key} changed"))

    original_pages, sanitized_pages = document["pages"], sanitized["pages"]

    if len(original_pages) != len(sanitized_pages):
        issues.append(issue(FAIL, f"{len(sanitized_pages)} pages where the original has {len(original_pages)}"))
        return issues, []

    compared = 0

    for original, new in zip(original_pages, sanitized_pages):
        if original["page_number"] != new["page_number"] or original.get("metadata") != new.get("metadata"):
            issues.append(issue(FAIL, "page number or page metadata changed", page_number=original["page_number"]))

        compared += compare_blocks(original["blocks"], new["blocks"], issues)

    return issues, [f"{compared} blocks compared on {len(original_pages)} pages"]


# --------------------------------------------------
# VALIDATION
# --------------------------------------------------

def validate_document(document, report, sanitized, nlp_detector=None):
    """
    Run every check on a sanitized copy, against the document and PII
    report it was made from.

    document is the extractor's JSON: a dictionary, or the
    ExtractedDocument an extraction pipeline still holds in memory.
    nlp_detector is LocalNLPDetector for the rescan; without it, person
    names are not rescanned and the rescan needs review.

    Returns the validation report: verdict, a summary of check
    statuses, and each check with its issues and notes.
    """

    if hasattr(document, "to_dict"):
        document = document.to_dict()

    checks = []

    for name, run in (
        ("provenance", lambda: check_provenance(document, report, sanitized)),
        ("report", lambda: check_report_findings(document, report)),
        ("replacements", lambda: check_replacements(report, sanitized)),
        ("ocr", lambda: check_ocr(document, report, sanitized)),
        ("extraction", lambda: check_extraction(document)),
        ("rescan", lambda: check_rescan(report, sanitized, nlp_detector)),
        ("known_values", lambda: check_known_values(document, report, sanitized)),
        ("structure", lambda: check_structure(document, sanitized)),
    ):
        # A check that cannot run has not confirmed anything: FAIL, and
        # the other checks still run so the report shows everything
        try:
            issues, notes = run()
        except Exception as error:
            issues, notes = [issue(FAIL, f"the check could not run: {type(error).__name__}: {error}")], []

        checks.append({
            "name": name,
            "status": worst(item["severity"] for item in issues),
            "issues_total": len(issues),
            "notes": notes,
            "issues": issues[:MAX_ISSUES],
        })

    verdict = worst(check["status"] for check in checks)

    return {
        "validator_version": VALIDATOR_VERSION,
        "validated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "document_id": document.get("document_id"),
        "file_name": document.get("file_name"),
        "report_scan_timestamp": report.get("scan_timestamp"),
        "verdict": verdict,
        "summary": {check["name"]: check["status"] for check in checks},
        "checks": checks,
    }


# --------------------------------------------------
# FILES
# --------------------------------------------------

def validation_path(json_path):
    """
    Where the validation report for an extracted JSON file goes: next
    to it, as <name>_validation.json.
    """

    json_path = Path(json_path)

    return json_path.with_name(f"{json_path.stem}_validation.json")


def validate_file(json_path, report_path=None, nlp_detector=None):
    """
    Validate <name>_sanitized.json against the extracted JSON file and
    its PII report (by default <name>_pii.json), and save the result.
    """

    report_path = report_path or findings_path(json_path)
    _, sanitized_path = output_paths(json_path)

    contents = [Path(path).read_bytes() for path in (json_path, report_path, sanitized_path)]

    validation = validate_document(*map(json.loads, contents), nlp_detector=nlp_detector)
    validation["files"] = {
        "document": str(json_path),
        "report": str(report_path),
        "sanitized": str(sanitized_path),
        # The exact copy validated: approved_copy refuses any other
        "sanitized_sha256": hashlib.sha256(contents[2]).hexdigest(),
    }

    output_path = validation_path(json_path)
    save_json(validation, output_path)

    return validation, output_path


# --------------------------------------------------
# RELEASE
# --------------------------------------------------

class NotApproved(Exception):
    """
    The sanitized copy may not be sent to an LLM.
    """


def review_path(json_path):
    """
    Where a reviewer's sign-offs for an extracted JSON file go: next to
    it, as <name>_review.json (written by review.py). Its notes are free
    text: handle it like the PII report.
    """

    json_path = Path(json_path)

    return json_path.with_name(f"{json_path.stem}_review.json")


def load_review(json_path):
    """
    The sign-offs for an extracted JSON file, or None if there are none.
    A file that cannot be read signs off nothing.
    """

    try:
        return json.loads(review_path(json_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def issue_key(check_name, item):
    """
    A short ID for one issue of one check, so a sign-off names exactly
    the issue it was given for: any change to the issue changes its key.
    """

    text = json.dumps({"check": check_name, **item}, sort_keys=True, ensure_ascii=False)

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def review_approval(validation, review=None):
    """
    Whether a validation report, with a reviewer's sign-offs (see
    review.py), lets its sanitized copy go to an LLM:

        PASS            yes
        FAIL            never: a value known to remain cannot be signed off
        NEEDS_REVIEW    only if every NEEDS_REVIEW issue was signed off for
                        this validation of this exact copy (the review's
                        sanitized_sha256 and validated_at), and no check
                        holds more issues than the report lists

    Returns approved, after_review (approved through sign-offs), signed
    (issue key -> sign-off, only those that apply to this validation),
    open and total (issues needing review, and those not yet signed off),
    reason (why it is not approved, or None), and the status once the
    review is counted: final_verdict (PASS when approved, otherwise the
    verdict) and final_checks (check name -> status: a NEEDS_REVIEW
    check whose every issue is listed and signed off is PASS).

    The verdict in the validation report itself never changes: it is
    what tells approved_copy that sign-offs must be checked.
    """

    verdict = validation.get("verdict")
    checks = validation.get("checks", [])
    needing_review = [
        issue_key(check["name"], item)
        for check in checks
        for item in check.get("issues", [])
        if item.get("severity") == NEEDS_REVIEW
    ]

    # Sign-offs given for another copy, or another validation of it, are void
    current = bool(review) and all(
        review.get(field) == validation.get(field) for field in ("document_id", "file_name", "validated_at")
    ) and review.get("sanitized_sha256") == validation.get("files", {}).get("sanitized_sha256")

    signed = {
        entry["key"]: entry
        for entry in (review.get("sign_offs", []) if current else [])
        if entry.get("key") in needing_review
    }

    def final_status(check):
        answered = (
            check.get("status") == NEEDS_REVIEW
            and check.get("issues_total", 0) == len(check.get("issues", []))
            and all(issue_key(check["name"], item) in signed for item in check.get("issues", []))
        )

        return PASS if answered else check.get("status")

    open_issues = sum(1 for key in needing_review if key not in signed)
    approval = {
        "approved": False,
        "after_review": False,
        "signed": signed,
        "open": open_issues,
        "total": len(needing_review),
        "reason": None,
        "final_verdict": verdict,
        "final_checks": {check["name"]: final_status(check) for check in checks},
    }

    if verdict == PASS:
        return {**approval, "approved": True}

    if verdict == FAIL:
        return {**approval, "reason": "a FAIL cannot be signed off: PII is known to remain"}

    if verdict != NEEDS_REVIEW:
        return {**approval, "reason": f"the validation verdict is {verdict}"}

    cut = [
        check["name"] for check in checks
        if check.get("status") == NEEDS_REVIEW and check.get("issues_total", 0) > len(check.get("issues", []))
    ]

    if cut:
        return {**approval, "reason": (
            f"the {', '.join(cut)} check lists only some of its issues, so they cannot "
            "all be signed off: fix them first"
        )}

    if open_issues:
        return {**approval, "reason": f"{open_issues} of {len(needing_review)} issues needing review are not signed off"}

    return {**approval, "approved": True, "after_review": True, "final_verdict": PASS}


def approved_copy(json_path):
    """
    The sanitized copy of an extracted JSON file, to send to an LLM. The
    one way to load it for that: it raises NotApproved unless the copy's
    validation report says PASS, or NEEDS_REVIEW with every issue signed
    off (review_approval), and the copy is byte for byte the one that was
    validated. A FAIL, or a NEEDS_REVIEW nobody has resolved, blocks it.
    """

    _, sanitized_path = output_paths(json_path)
    report_path = validation_path(json_path)

    try:
        validation = json.loads(Path(report_path).read_text(encoding="utf-8"))
        content = Path(sanitized_path).read_bytes()
    except (OSError, ValueError) as error:
        raise NotApproved(f"not validated: {error}") from error

    verdict = validation.get("verdict")

    if verdict == NEEDS_REVIEW:
        approval = review_approval(validation, load_review(json_path))

        if not approval["approved"]:
            raise NotApproved(f"the validation verdict is NEEDS_REVIEW ({approval['reason']}): see {report_path}")

    elif verdict != PASS:
        raise NotApproved(f"the validation verdict is {verdict}: see {report_path}")

    if hashlib.sha256(content).hexdigest() != validation.get("files", {}).get("sanitized_sha256"):
        raise NotApproved(f"{sanitized_path} has changed since it was validated")

    return json.loads(content)


def print_validation(validation):
    """
    Print the verdict, and each check with its notes and first issues.
    """

    print(f"\nVerdict: {validation['verdict']}\n")

    for check in validation["checks"]:
        print(f"  {check['status']:13} {check['name']:13} {check['issues_total']} issues")

        for note in check["notes"]:
            print(f"  {'':13} {'':13} {note}")

        for item in check["issues"][:5]:
            where = " ".join(
                str(item[key]) for key in ("finding_id", "block_id") if key in item
            )
            count = f" ({item['occurrences']}x)" if "occurrences" in item else ""
            print(f"  {'':13} {'':13} - {item['severity']}: {where} {item['reason']}{count}")

        if check["issues_total"] > 5:
            print(f"  {'':13} {'':13}   ... and {check['issues_total'] - 5} more in the report")


def main():
    """
    Exit code: 0 PASS, 1 FAIL, 2 NEEDS_REVIEW, so a script can refuse to
    send anything that did not pass.
    """

    if len(sys.argv) > 1:
        json_path = Path(sys.argv[1])
    else:
        json_path = Path(input("Extracted JSON file: ").strip().strip('"'))

    report_path = Path(sys.argv[2]) if len(sys.argv) > 2 else findings_path(json_path)

    for path in (json_path, report_path, output_paths(json_path)[1]):
        if not path.is_file():
            sys.exit(f"File not found: {path}")

    nlp = LocalNLPDetector()

    if nlp.error:
        print(f"Warning: {nlp.error} Person names will not be rescanned.")

    validation, output_path = validate_file(json_path, report_path, nlp)

    print(f"Validation report: {output_path}")
    print_validation(validation)

    sys.exit(EXIT_CODES[validation["verdict"]])


if __name__ == "__main__":
    main()
