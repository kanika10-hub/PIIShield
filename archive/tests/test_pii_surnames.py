import sys

from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import same_person, scan_document, used_as_name

# Surname resolution (document_scanner.surname_mentions): a person named
# in full somewhere in a document is also found where the document uses
# the surname alone. Each case is a small document of paragraphs, scanned
# as a whole, and lists every PERSON_NAME the scan must report.

nlp = LocalNLPDetector()

if nlp.nlp is None:
    print("spaCy model unavailable:", nlp.error)
    sys.exit(1)


def make_document(texts):
    return {
        "document_id": "surname_test",
        "file_name": "surname_test.pdf",
        "source_type": "PDF",
        "metadata": {},
        "pages": [{
            "page_number": 1,
            "metadata": {},
            "blocks": [
                {"block_id": f"b{number}", "block_type": "paragraph", "text": text,
                 "metadata": {}, "bounding_box": None, "children": []}
                for number, text in enumerate(texts, start=1)
            ],
        }],
    }


# (description, paragraphs, every PERSON_NAME as (block_id, text),
#  surname findings' context_trigger by (block_id, text))
CASES = [
    (
        "Surname alone after the full name",
        ["The control is owned by Jane Okafor.",
         "Okafor signed off the exceptions in May."],
        {("b1", "Jane Okafor"), ("b2", "Okafor")},
        {("b2", "Okafor"): "Jane Okafor"},
    ),
    (
        "Surname alone before the full name",
        ["Exceptions were signed off by Okafor.",
         "The control is owned by Jane Okafor."],
        {("b1", "Okafor"), ("b2", "Jane Okafor")},
        {},
    ),
    (
        "Full name written with an initial",
        ["Owner: M. Okafor",
         "The change was approved by Okafor."],
        {("b1", "M. Okafor"), ("b2", "Okafor")},
        {},
    ),
    (
        "Initials on a line of their own",
        ["Accountable owner: N. K.\nOsadebe",
         "The forum is chaired by Osadebe."],
        {("b1", "N. K.\nOsadebe"), ("b2", "Osadebe")},
        {},
    ),
    (
        "Possessive surname",
        ["The policy is owned by Jane Okafor.",
         "Okafor's team reviews it every year."],
        {("b1", "Jane Okafor"), ("b2", "Okafor")},
        {},
    ),
    (
        "Not inside an email address or a longer word",
        ["The policy is owned by Jane Okafor.",
         "Send it to j.okafor@example.com or the Okaforville office."],
        {("b1", "Jane Okafor")},
        {},
    ),
    (
        "Full name and surname in one text are reported once each",
        ["The policy is owned by Jane Okafor; Okafor reviews it every year."],
        {("b1", "Jane Okafor"), ("b1", "Okafor")},
        {},
    ),
    (
        "Two people share a surname: both are named as the reason",
        ["Anna Park leads the Finance team.",
         "James Park leads the Legal team.",
         "The budget was approved by Park."],
        {("b1", "Anna Park"), ("b2", "James Park"), ("b3", "Park")},
        {("b3", "Park"): "Anna Park; James Park"},
    ),
    (
        "Surname that is an ordinary word, starting a sentence",
        ["The register is owned by Mark Read.",
         "Read the policy before you approve a vendor.",
         "Changes were approved by Read in March."],
        {("b1", "Mark Read"), ("b3", "Read")},
        {},
    ),
    (
        "Surname that is an ordinary word, used as a word (a RAG status)",
        ["Laura Green owns the dashboard.",
         "Status: Green",
         "Green"],
        {("b1", "Laura Green")},
        {},
    ),
    (
        "Surname followed by a capitalised word is part of another name",
        ["The account manager is Hannah Ford.",
         "Vehicles are leased from Ford Motor Company.",
         "The lease was confirmed by Ford."],
        {("b1", "Hannah Ford"), ("b3", "Ford")},
        {},
    ),
    (
        "Surname after a capitalised word inside a phrase is part of another name",
        ["The register is owned by Maya Hunt.",
         "Everyone joins the Treasure Hunt event.",
         "Questions go to Hunt directly."],
        {("b1", "Maya Hunt"), ("b3", "Hunt")},
        {},
    ),
    (
        "The person's initials before the surname, in an OCR'd signature list",
        ["The register is owned by Owen B. Hartley.",
         "8. ose-nutu OB Hartley RK Desai"],
        {("b1", "Owen B. Hartley"), ("b2", "Hartley")},
        {},
    ),
    (
        "Dotted, hyphenated initials before the surname",
        ["Owner: Jan-Erik Sandvik",
         "J.-E. Sandvik"],
        {("b1", "Jan-Erik Sandvik"), ("b2", "Sandvik")},
        {},
    ),
    (
        "One person written two ways is one person, named in full",
        ["The register is owned by Tobias Lindgren.",
         "Owner: T. Lindgren",
         "Signed off by Lindgren."],
        {("b1", "Tobias Lindgren"), ("b2", "T. Lindgren"), ("b3", "Lindgren")},
        {("b3", "Lindgren"): "Tobias Lindgren"},
    ),
    (
        "A title before or a role after the surname still counts",
        ["Escalations go to Omar Haddad.",
         "It was approved by Dr. Haddad last week.",
         "Approved: Haddad (CFO)"],
        {("b1", "Omar Haddad"), ("b2", "Haddad"), ("b3", "Haddad")},
        {},
    ),
]


def overlapping(findings):
    """
    Pairs of PERSON_NAME findings in one block that overlap.
    """

    names = [item for item in findings if item["type"] == "PERSON_NAME"]

    return [
        (first["text"], second["text"])
        for index, first in enumerate(names)
        for second in names[index + 1:]
        if first["block_id"] == second["block_id"]
        and first["start"] < second["end"] and second["start"] < first["end"]
    ]


failures = 0

print("========== SURNAME TEST ==========\n")

for description, texts, expected, triggers in CASES:

    findings = scan_document(make_document(texts), nlp)["findings"]
    names = [item for item in findings if item["type"] == "PERSON_NAME"]

    actual = {(item["block_id"], item["text"]) for item in names}
    actual_triggers = {
        (item["block_id"], item["text"]): item["context_trigger"]
        for item in names if item["detector"] == "surname"
    }

    wrong_triggers = {
        key: actual_triggers.get(key)
        for key, trigger in triggers.items()
        if actual_triggers.get(key) != trigger
    }
    overlaps = overlapping(findings)

    if actual == expected and not wrong_triggers and not overlaps:
        print(f"PASS  {description}")
        continue

    failures += 1
    print(f"FAIL  {description}")

    for missing in sorted(expected - actual):
        print(f"        missed:     {missing}")

    for extra in sorted(actual - expected):
        print(f"        unexpected: {extra}")

    for key, trigger in wrong_triggers.items():
        print(f"        trigger of {key}: {trigger!r}, expected {triggers[key]!r}")

    for pair in overlaps:
        print(f"        overlapping: {pair}")

# spaCy sometimes reads a surname after initials as a common noun
# ("L.-E. Brannstrom"). The initials make it a name all the same; the
# part of speech is given here, so the rule is tested without spaCy.
RULES = [
    (
        "Initials before a surname spaCy tagged as a noun: a name",
        used_as_name("J.-E. Sandvik", 6, 13, ["Jan-Erik Sandvik"], {6: "NOUN"}),
        True,
    ),
    (
        "The same noun-tagged surname without initials: not a name",
        used_as_name("Status: Sandvik", 8, 15, ["Jan-Erik Sandvik"], {8: "NOUN"}),
        False,
    ),
    (
        "A single initial before a surname makes it a name, even another person's",
        used_as_name("1.8 Y Sandvik has your", 6, 13, ["Jan-Erik Sandvik"], {6: "PROPN"}),
        True,
    ),
    (
        "Capitals that are not the person's initials: another name",
        used_as_name("Login history UK Sandvik 3", 17, 24, ["Jan-Erik Sandvik"], {17: "PROPN"}),
        False,
    ),
    (
        "Two people with one surname are told apart by first name",
        same_person("Anna Park", "Alan Park"),
        False,
    ),
    (
        "Initials and a written-out name can be one person",
        same_person("F.L. Okoye", "Folasade L. Okoye"),
        True,
    ),
]

for description, actual, expected in RULES:
    if actual == expected:
        print(f"PASS  {description}")
    else:
        failures += 1
        print(f"FAIL  {description}: {actual}, expected {expected}")

total = len(CASES) + len(RULES)

print(f"\n{total - failures} of {total} cases passed")
print("==================================")

sys.exit(1 if failures else 0)
