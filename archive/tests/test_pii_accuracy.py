
import sys

from src.pii.detectors import detect_pii

# (description, text, context, expected findings as (type, text))
# Texts are taken from the extracted case-study documents and OCR output.
CASES = [
    # ---------------- Must be found ----------------
    (
        "Contact line with ID, email and two phones",
        "Achebe, Group Chief Risk Officer (EMP-38104) m.achebe@cadencefg.com"
        " | +1 (212) 555-0147 | mobile +1 (917) 555-0143",
        "",
        {
            ("EMPLOYEE_ID", "EMP-38104"),
            ("EMAIL", "m.achebe@cadencefg.com"),
            ("PHONE", "+1 (212) 555-0147"),
            ("PHONE", "+1 (917) 555-0143"),
        },
    ),
    (
        "Labelled SSN, passport and PAN in one sentence",
        "Adaeze Onwuka (EMP-40766, SSN 900-55-2043),\nHiroshi Yamagata "
        "(EMP-41310, passport TK4820913) and Rohan Deshmukh "
        "(EMP-41488, PAN AQBPR7741L).",
        "",
        {
            ("EMPLOYEE_ID", "EMP-40766"),
            ("US_SSN", "900-55-2043"),
            ("EMPLOYEE_ID", "EMP-41310"),
            ("PASSPORT_NUMBER", "TK4820913"),
            ("EMPLOYEE_ID", "EMP-41488"),
            ("INDIAN_PAN", "AQBPR7741L"),
        },
    ),
    (
        "OCR screenshot: SSN with broken separator, labelled mobile",
        "SSN 900-42.7701\nPAN AQUPRT7AIL\nMobile 646-865-0188",
        "",
        {
            ("US_SSN", "900-42.7701"),
            ("PHONE", "646-865-0188"),
        },
    ),
    (
        "OCR screenshot: EMP with a dot, SSN with a misplaced hyphen",
        "(EMP.29871, SSN 90042-1290) departed 28 Feb 2026",
        "",
        {
            ("EMPLOYEE_ID", "EMP.29871"),
            ("US_SSN", "90042-1290"),
        },
    ),
    (
        "Employee ID followed by an underscore",
        "Osadebe EMP-41077_ IAM Engineer II n.osadebe@cadencefg.com "
        "+1 (646) 555-0188",
        "",
        {
            ("EMPLOYEE_ID", "EMP-41077"),
            ("EMAIL", "n.osadebe@cadencefg.com"),
            ("PHONE", "+1 (646) 555-0188"),
        },
    ),
    (
        "Phone with area code in brackets keeps the bracket",
        "changed your mobile number to (305) 555-0143 and",
        "",
        {("PHONE", "(305) 555-0143")},
    ),
    (
        "OCR punctuation stuck to an email",
        "Caleb A. Whitfield, Group CISO —_c.whitfield@cadencefg.com,",
        "",
        {("EMAIL", "c.whitfield@cadencefg.com")},
    ),
    (
        "International numbers with single-digit area code",
        "Europe SA (EMP-41501) +33 1 5555 0144 and +48 22 555 0193",
        "",
        {
            ("EMPLOYEE_ID", "EMP-41501"),
            ("PHONE", "+33 1 5555 0144"),
            ("PHONE", "+48 22 555 0193"),
        },
    ),
    (
        "Phone numbers wrapped onto the next line by the PDF layout",
        "y.sorokina@cadencefg.com, +1 (212)\n555-0121 and +49 69\n5555 0177",
        "",
        {
            ("EMAIL", "y.sorokina@cadencefg.com"),
            ("PHONE", "+1 (212)\n555-0121"),
            ("PHONE", "+49 69\n5555 0177"),
        },
    ),
    (
        "Table cell: passport header gives the context",
        "K8274615",
        "Passport No.",
        {("PASSPORT_NUMBER", "K8274615")},
    ),
    (
        "Table cell: plain digits under a Mobile header",
        "9876543210",
        "Mobile",
        {("PHONE", "9876543210")},
    ),
    (
        "Employee ID label with a non-EMP format",
        "Employee ID: CFG-20931 joined in 2024",
        "",
        {("EMPLOYEE_ID", "CFG-20931")},
    ),
    (
        "IP address is not also a phone",
        "IP address: 192.168.1.10.",
        "",
        {("IP_ADDRESS", "192.168.1.10")},
    ),

    # ---------------- Must NOT be found ----------------
    ("Issue reference", "Issue\nISS-2026-0112 remains open", "", set()),
    (
        "Incident reference followed by a number",
        "INC-2026-0417 1 Evidence folder missing 11/03/2026",
        "",
        set(),
    ),
    (
        "Opportunity number",
        "Opportunity Number: OP-4545941\nVersion Number: 1.0",
        "",
        set(),
    ),
    (
        "Date on the line before a phone is not merged into it",
        "+1 14 Mar 2021\n(212) 555-0108",
        "",
        {("PHONE", "(212) 555-0108")},
    ),
    (
        "Date after an employee ID",
        "Harriet L. Okafor (EMP-41142) 18/04/2026 — tolerance",
        "",
        {("EMPLOYEE_ID", "EMP-41142")},
    ),
    ("Standards and algorithms", "Certified to ISO27001 using SHA256", "", set()),
    (
        "Passport mentioned, but the value is in another sentence",
        "Passport: not provided. Ref AB12345",
        "",
        set(),
    ),
    (
        "Year range, version, ISO date",
        "Delivered 2020-2024 in build 10.0.19041, effective 2026-04-29",
        "",
        set(),
    ),
    ("Numbered list on separate lines", "Questions 12\n\n13\n\n14\n\n15", "", set()),
    ("Policy reference", "GRP-POL-003\n55107", "", set()),
    ("Invalid SSN range without a label", "Ticket 900-55-2043", "", set()),
    ("Plain digit run without a phone label", "Account 9876543210", "", set()),
    (
        "Month-first and day-first dates, list of years",
        "Meeting 04-29-2026, review 29-04-2026, columns 2022 2023 2024",
        "",
        set(),
    ),
    (
        "Abbreviated label keeps its full stop",
        "Tel. 555-0147",
        "",
        {("PHONE", "555-0147")},
    ),
    (
        "Label in an earlier sentence does not carry over",
        "Mobile: none. Account 9876543210",
        "",
        set(),
    ),
]

failures = 0

print("========== PII ACCURACY TEST ==========\n")

for description, text, context, expected in CASES:

    findings = detect_pii(text, context=context)

    actual = {(item["type"], item["text"]) for item in findings}

    offsets_ok = all(
        text[item["start"]:item["end"]] == item["text"]
        for item in findings
    )

    if actual == expected and offsets_ok:
        print(f"PASS  {description}")
        continue

    failures += 1
    print(f"FAIL  {description}")

    for missing in sorted(expected - actual):
        print(f"        missed:     {missing}")

    for extra in sorted(actual - expected):
        print(f"        unexpected: {extra}")

    if not offsets_ok:
        print("        offsets do not match the text")

print(f"\n{len(CASES) - failures} of {len(CASES)} cases passed")
print("=======================================")

sys.exit(1 if failures else 0)
