"""
Measure how accurately PII is detected, against hand-labelled examples
(tests/data/pii_ground_truth.json).

    python -m tests.evaluate_detector

Prints precision, recall and F1 for each split: overall, by PII type,
by source format and by extraction method. Writes the metrics to
output/detector_errors.json, with two lists to work from:

    false_negatives     labelled PII the detector missed: improve recall
    false_positives     findings that are not PII, with the rule that
                        reported each: improve precision

A finding is correct when its (text, type) pair matches a label. The
same pair twice in one text counts twice. Offsets are not compared yet.

Each example is one text, as the extractors produce it:

    id              DEV-001, EVAL-001, ...
    split           "dev": tune the detection rules on these
                    "eval": never tune a rule on these, so they show
                    whether a change helps on text it was not fitted to
    source_type     PDF, PPTX or DOCX
    extraction      "native" or "ocr": how the text was read
    text            the text to scan
    context         optional: a table cell's title, column header and
                    row label, as document_scanner passes them
    document        optional: other text from the same document. As in
                    a full scan, ordinary words are learned from it, and
                    a surname alone is PII if it names the person in full
    expected        the PII in text: [{"text": ..., "type": ...}]
    note            optional: why the labels are what they are

Labelling rules, so that labels match what redaction must remove:

    - The value only, without its label: "555-0147" in "Tel. 555-0147".
      A phone number includes its country and area code, and a line
      break the layout put inside it.
    - A name as it appears, with middle initials and line breaks, without
      titles ("Dr."), job titles or "'s".
    - A name inside an email address is part of the EMAIL.
    - Organisations, teams, products, places and job titles are not PII.
    - An OCR-damaged value is labelled as it appears ("EMP.30418") if a
      person can still tell what it is.
"""

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Run as a file (python tests/evaluate_detector.py, or the editor's Run
# button) rather than with -m, Python cannot see src/: add the backend folder
BACKEND = Path(__file__).resolve().parent.parent

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from src.pii.detectors import LocalNLPDetector, detect_pii
from src.pii.document_scanner import SCANNER_VERSION, DocumentNLP, surname_mentions


# Next to this file, whichever folder it is run from
DATASET = BACKEND / "tests" / "data" / "pii_ground_truth.json"
ERRORS_FILE = BACKEND / "output" / "detector_errors.json"

SPLITS = ("dev", "eval")
SOURCE_TYPES = ("PDF", "PPTX", "DOCX")
EXTRACTIONS = ("native", "ocr")

# Every type the detectors report
PII_TYPES = (
    "EMAIL", "PHONE", "US_SSN", "INDIAN_PAN", "IP_ADDRESS",
    "EMPLOYEE_ID", "PASSPORT_NUMBER", "PERSON_NAME",
    "DATE_OF_BIRTH", "NATIONAL_ID", "PAYMENT_CARD", "BANK_ACCOUNT", "POSTAL_ADDRESS"
)

# Breakdowns, in print order
GROUPS = ("overall", "type", "source", "extraction")


# --------------------------------------------------
# DATASET
# --------------------------------------------------

def dataset_problems(examples):
    """
    Mistakes in the labelled examples, which would make the scores wrong.
    """

    problems = []
    ids = Counter(example.get("id") for example in examples)

    for example in examples:
        name = example.get("id", "?")

        if ids[name] > 1:
            problems.append(f"{name}: id used {ids[name]} times")

        for field, allowed in (
            ("split", SPLITS), ("source_type", SOURCE_TYPES), ("extraction", EXTRACTIONS)
        ):
            if example.get(field) not in allowed:
                problems.append(f"{name}: {field} must be one of {allowed}")

        if not isinstance(example.get("text"), str) or not isinstance(example.get("expected"), list):
            problems.append(f"{name}: needs text and an expected list")
            continue

        for label in example["expected"]:
            if label.get("type") not in PII_TYPES:
                problems.append(f"{name}: unknown type {label.get('type')!r}")

            if not label.get("text") or label["text"] not in example["text"]:
                problems.append(f"{name}: {label.get('text')!r} is not in the text")

    return problems


def load_examples():

    examples = json.loads(DATASET.read_text(encoding="utf-8"))
    problems = dataset_problems(examples)

    if problems:
        print(f"{DATASET} has mistakes:")

        for problem in problems:
            print(f"  {problem}")

        sys.exit(1)

    return examples


# --------------------------------------------------
# DETECTION
# --------------------------------------------------

def predict(example, nlp):
    """
    The findings in example["text"], found as a full document scan finds
    them: spaCy and ordinary words for the whole document, the context
    for the text, then surname mentions.
    """

    texts = [example["text"], *example.get("document", [])]
    detector = DocumentNLP(nlp, texts)

    # Units as document_scanner builds them: (page, block, table, context)
    units = [
        (None, {"block_id": f"text_{number}", "text": text}, None,
         example.get("context", "") if number == 0 else "")
        for number, text in enumerate(texts)
    ]

    found = [
        (unit, item)
        for unit in units
        for item in detect_pii(unit[1]["text"], detector, context=unit[3])
    ]

    found.extend(surname_mentions(units, found, detector.parts_of_speech))

    return [item for unit, item in found if unit is units[0]]


def compare(expected, predicted):
    """
    (correct, missed, unexpected): Counters of (text, type) pairs.
    """

    expected = Counter((item["text"], item["type"]) for item in expected)
    predicted = Counter((item["text"], item["type"]) for item in predicted)

    return expected & predicted, expected - predicted, predicted - expected


def error_records(example, predicted, missed, unexpected):
    """
    (false_negatives, false_positives) of one example, one record per
    value. A false positive keeps the rule that reported it.
    """

    where = {key: example[key] for key in ("id", "split", "source_type", "extraction")}
    about = {
        "example_text": example["text"],
        "context": example.get("context", ""),
        "note": example.get("note"),
    }

    false_negatives = [
        {**where, "type": pii_type, "value": value, **about}
        for value, pii_type in missed.elements()
    ]

    left = Counter(unexpected)
    false_positives = []

    for item in predicted:
        pair = (item["text"], item["type"])

        if left[pair]:
            left[pair] -= 1
            false_positives.append({
                **where,
                "type": item["type"],
                "value": item["text"],
                "rule_id": item["rule_id"],
                "confidence": item["confidence"],
                "context_trigger": item["context_trigger"],
                **about,
            })

    return false_negatives, false_positives


# --------------------------------------------------
# METRICS
# --------------------------------------------------

def ratio(part, whole):
    return round(part / whole, 3) if whole else None


def scores(counts):
    """
    Precision, recall and F1 from tp, fp and fn. None where undefined:
    precision with nothing found, recall with nothing to find.
    """

    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    precision = ratio(tp, tp + fp)
    recall = ratio(tp, tp + fn)

    f1 = (
        round(2 * precision * recall / (precision + recall), 3)
        if precision and recall else None
    )

    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def tally(totals, example, correct, missed, unexpected):
    """
    Add one example's outcome to its split's counts. A missed or correct
    pair counts under its labelled type, an unexpected one under the
    type the detector gave it.
    """

    for outcome, pairs in (("tp", correct), ("fn", missed), ("fp", unexpected)):
        for (_, pii_type), count in pairs.items():
            for group in (
                ("overall", "all"),
                ("type", pii_type),
                ("source", example["source_type"]),
                ("extraction", example["extraction"]),
            ):
                totals[group][outcome] += count


def metrics_of(totals):
    """
    {group: {value: scores}}, in GROUPS order, values sorted.
    """

    metrics = {}

    for group in GROUPS:
        values = sorted(value for kind, value in totals if kind == group)
        metrics[group] = {value: scores(totals[(group, value)]) for value in values}

    return metrics


def show(value):
    return "-" if value is None else f"{value:.3f}"


def print_metrics(split, count, metrics):

    print(f"\n========== {split.upper()} ({count} examples) ==========\n")
    print(f"{'':12}{'':18}{'TP':>5}{'FP':>5}{'FN':>5}{'Precision':>11}{'Recall':>8}{'F1':>8}")

    for group in GROUPS:
        for value, row in metrics[group].items():
            label = "overall" if group == "overall" else value

            print(
                f"{group if group != 'overall' else '':12}{label:18}"
                f"{row['tp']:>5}{row['fp']:>5}{row['fn']:>5}"
                f"{show(row['precision']):>11}{show(row['recall']):>8}{show(row['f1']):>8}"
            )


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():

    examples = load_examples()
    nlp = LocalNLPDetector()

    # Most of the labels are names: scores without spaCy would mislead
    if nlp.nlp is None:
        print("spaCy model unavailable:", nlp.error)
        sys.exit(1)

    totals = {split: defaultdict(Counter) for split in SPLITS}
    false_negatives = []
    false_positives = []

    for example in examples:
        predicted = predict(example, nlp)
        correct, missed, unexpected = compare(example["expected"], predicted)
        tally(totals[example["split"]], example, correct, missed, unexpected)

        negatives, positives = error_records(example, predicted, missed, unexpected)
        false_negatives.extend(negatives)
        false_positives.extend(positives)

    counts = Counter(example["split"] for example in examples)
    metrics = {split: metrics_of(totals[split]) for split in SPLITS}

    for split in SPLITS:
        print_metrics(split, counts[split], metrics[split])

    report = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scanner_version": SCANNER_VERSION,
        "dataset": DATASET.relative_to(BACKEND).as_posix(),
        "examples": dict(counts),
        "metrics": metrics,
        "false_negatives": false_negatives,
        "false_positives": false_positives,
    }

    ERRORS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ERRORS_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()

    for split in SPLITS:
        missed = sum(1 for item in false_negatives if item["split"] == split)
        wrong = sum(1 for item in false_positives if item["split"] == split)
        print(f"{split}: {missed} false negatives (missed), {wrong} false positives (not PII)")

    print(f"Report: {ERRORS_FILE}")


if __name__ == "__main__":
    main()
