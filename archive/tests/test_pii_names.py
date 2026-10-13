
import sys

from src.pii.detectors import LocalNLPDetector, detect_pii
from src.pii.document_scanner import DocumentNLP, scan_document

# Person-name detection: spaCy entities cleaned by name-shape rules,
# names recovered next to a person label, and surname-only mentions.
# Texts are taken from the extracted case-study documents.

nlp = LocalNLPDetector()

if nlp.nlp is None:
    print("spaCy model unavailable:", nlp.error)
    sys.exit(1)

# Ordinary words are learned from the whole document: capitalised
# "Trigger Type" is ordinary vocabulary because the document also says
# "trigger" and "type". These sentences are from the same TPRM document.
TPRM_SENTENCES = [
    "Action dropdown within the rule setup offers multiple options that can "
    "be used to generate risks, create tasks, or trigger follow-up actions.",
    "Cadence’s third-party workflows in OneTrust are now fully automated and "
    "tailored to the criticality and type of vendor.",
    "Cadence’s OneTrust environment has been configured with automated workflows "
    "that manage the lifecycle of third-party assessments based on both vendor "
    "category and risk tier.",
    "This workflow design ensures:",
]

# (description, text, context, expected PERSON_NAME texts[, rest of document])
CASES = [
    (
        "Names on separate lines are separate names",
        "Daniela Figueroa\nBridgett Gatewood\nJyoti Karwal",
        "USERS",
        {"Daniela Figueroa", "Bridgett Gatewood", "Jyoti Karwal"},
    ),
    (
        "A job title on the next line is not part of the name",
        "T. Lindqvist\nCIO",
        "Activity",
        {"T. Lindqvist"},
    ),
    (
        "Initials split from the surname by a line break",
        "N. K.\nOsadebe",
        "ACCOUNTABLE OWNER",
        {"N. K.\nOsadebe"},
    ),
    (
        "OCR junk and possessives are trimmed off",
        "Reviewed by Fumiko L. Nakagawa's team; contact M. Achebe * +1",
        "",
        {"Fumiko L. Nakagawa", "M. Achebe"},
    ),
    (
        "Surnames with an apostrophe or a Mc/Mac prefix",
        "The review was led by Aoife O'Brien, Callum McDonald and Priya D'Souza.",
        "",
        {"Aoife O'Brien", "Callum McDonald", "Priya D'Souza"},
    ),
    (
        "Name missed by spaCy, recovered from the column header",
        "Lars-Erik Brannstrom",
        "DIVISIONAL CEO | Global Operations & Technology",
        {"Lars-Erik Brannstrom"},
    ),
    (
        "Name missed by spaCy, recovered from a label in the text",
        "Committee (Chair: Rosalind Featherstone)",
        "",
        {"Rosalind Featherstone"},
    ),
    (
        "Workflow vocabulary is not a name",
        "Trigger Name: On_T_Assign Workflow\nTrigger Type: Vendor Created\nChange Workflow",
        "WORKFLOW RULE",
        set(),
        TPRM_SENTENCES,
    ),
    (
        "Codes and tiers are not names",
        "QR.35 applies to Tier 2 and Vendor Tier reviews (RSK-4488)",
        "",
        set(),
        TPRM_SENTENCES,
    ),
    (
        "A RULE NAME column holds rule titles, not people",
        "Disaster Recovery Plan",
        "RULE NAME | QR.55",
        set(),
    ),
    (
        "Job titles and lists are not names",
        "Chief Executive Officer; Database, Cloud",
        "Role",
        set(),
    ),
]

failures = 0

print("========== PERSON NAME TEST ==========\n")

for description, text, context, expected, *document in CASES:

    # As in document_scanner: spaCy and ordinary words for the document
    detector = DocumentNLP(nlp, [text] + (document[0] if document else []))
    findings = detect_pii(text, detector, context=context)
    actual = {item["text"] for item in findings if item["type"] == "PERSON_NAME"}
    offsets_ok = all(text[item["start"]:item["end"]] == item["text"] for item in findings)

    if actual == expected and offsets_ok:
        print(f"PASS  {description}")
        continue

    failures += 1
    print(f"FAIL  {description}")
    print(f"        expected: {sorted(expected)}")
    print(f"        actual:   {sorted(actual)}")

    if not offsets_ok:
        print("        offsets do not match the text")

# Surname mentions: a person named in full is also found by surname alone
document = {
    "document_id": "names_test",
    "file_name": "test.pdf",
    "source_type": "PDF",
    "pages": [{
        "page_number": 1,
        "blocks": [
            {"block_id": "b1", "block_type": "paragraph", "metadata": {},
             "text": "Control CTL-IAM-04 is owned by Caleb A. Whitfield.", "children": []},
            {"block_id": "b2", "block_type": "paragraph", "metadata": {},
             "text": "The review was signed off by Whitfield in May; c.whitfield@cadencefg.com was copied.",
             "children": []},
        ]
    }]
}

surnames = [
    (item["block_id"], item["text"], item["detector"])
    for item in scan_document(document, nlp)["findings"]
    if item["type"] == "PERSON_NAME"
]

expected_surnames = [("b1", "Caleb A. Whitfield", "nlp"), ("b2", "Whitfield", "surname")]

if surnames == expected_surnames:
    print("PASS  Surname-only mention found; the email address is not")
else:
    failures += 1
    print("FAIL  Surname-only mention found; the email address is not")
    print(f"        expected: {expected_surnames}")
    print(f"        actual:   {surnames}")

total = len(CASES) + 1

print(f"\n{total - failures} of {total} cases passed")
print("======================================")

sys.exit(1 if failures else 0)
