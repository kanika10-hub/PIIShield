
import ipaddress
import re

# Optional local NLP support. No text is sent to an external service.
try:
    import spacy
except ImportError:
    spacy = None


# --------------------------------------------------
# 0. CONTEXT HELPERS
# --------------------------------------------------

# Capital and small letters of the Latin scripts, for regex character
# classes: names are "Łukasz Wiśniewski" and "José Álvarez" too, not only
# A-Z. Python's re has no \p{Lu}, so the classes are listed.
UPPER = "A-Z" + "".join(letter for letter in map(chr, range(0xC0, 0x250)) if letter.isupper())
LOWER = "a-z" + "".join(letter for letter in map(chr, range(0xC0, 0x250)) if letter.islower())

# Labels that announce the value after them, e.g. "SSN 900-55-2043".
LABEL_PATTERNS = {
    pii_type: re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE)
    for pii_type, pattern in {
        "PHONE": r"phone|tel|telephone|mobile|mob|cell|fax|contact|call",
        "US_SSN": r"ssn|social security",
        "INDIAN_PAN": r"pan",
        "PASSPORT_NUMBER": r"passport|travel document",
        # "Employee ID", not just "Employee": a header like
        # "Employee count" must not turn "1500" into an ID
        "EMPLOYEE_ID": r"(?:employee|staff|emp)\.?\s*(?:id|no|number|code)",
        "DATE_OF_BIRTH": r"date of birth|birth ?date|dob|d\.o\.b|born(?: on)?",
        "NATIONAL_ID": (
            r"pesel|aadhaar|national (?:id|identity|identification|insurance)(?: number| no| card)?"
            r"|ni number|nino|personal id(?:entification)? number"
        ),
        "PAYMENT_CARD": r"card|visa|mastercard|amex",
        # Not "account" alone: "Account 9876543210" can be any reference
        "BANK_ACCOUNT": r"iban|(?:bank )?account (?:number|no)|acct|a/c|bank account",
        # Not "IP address", "email address": those values have their own types
        "POSTAL_ADDRESS": (
            r"(?<!ip )(?<!mac )(?<!web )(?<!email )(?<!e-mail )(?<!server )"
            r"(?:(?:home|residential|postal|mailing|permanent|correspondence|private) )?address"
            r"|resides at|residing at|lives at|living at"
        ),
        # Column headers and labels that hold people's names. "Name" only
        # on its own or after a person word: "RULE NAME" holds rule titles
        "PERSON_NAME": (
            r"(?<![A-Za-z] )names?|(?:full|first|last|employee|member|user|contact) names?"
            r"|members?|users?|employee|individual|attendee|incumbent"
            r"|chair|secretary|owner|author|reviewer|approver|requestor|sponsor"
            r"|lead|analyst|manager|officer|deputy|primary|alternate|contact"
            r"|ceo|cro|cfo|ciso|dpo|presenter|speaker"
            r"|(?:accepted|approved|prepared|recommended|booked|determined"
            r"|owned|reviewed|chaired|coordinated|attested|modified"
            r"|led|headed|presented|submitted|authored|signed(?: off)?) by"
            r"|(?:issued|assigned) to"
        ),
    }.items()
}

# A label only counts if it is in the same line or clause as the value.
# A full stop ends a clause, except after an abbreviation ("Tel. 555-0147").
# Python lookbehinds must be fixed-width, so each abbreviation gets its own.
ABBREVIATIONS = ("tel", "ph", "mob", "fax", "ext", "no", "mr", "mrs", "ms", "dr")

CLAUSE_BREAK = re.compile(
    r"[\n;|]|"
    + "".join(rf"(?<!\b{abbreviation})" for abbreviation in ABBREVIATIONS)
    + r"\.\s",
    re.IGNORECASE
)

# A person label counts for a name spaCy missed only if it stands close
# before it: "Owner: Jane Okafor", "led by Jane Okafor". Not "Contact
# the Service Desk" (an article between) nor "Manager approval is
# needed for Remote Access" (too many words between).
MAX_LABEL_GAP_WORDS = 2
ARTICLE = re.compile(r"\b(?:the|a|an|our|your|their|its|this|that|these|those)\b", re.IGNORECASE)


def find_label(text, start, pii_type, context="", window=40):
    """
    The label for pii_type shortly before the value, or in the context
    (e.g. the column header of a table cell): "Tel", "Employee ID".
    None if there is no label.
    """

    before = CLAUSE_BREAK.split(text[max(0, start - window):start])[-1]
    pattern = LABEL_PATTERNS[pii_type]
    match = pattern.search(before) or (pattern.search(context) if context else None)

    return match.group() if match else None


def person_label(text, start, context=""):
    """
    The person label for a name spaCy missed at text[start:]: the label
    nearest before it in the same clause, if few words and no article
    stand between them (see MAX_LABEL_GAP_WORDS), or else a label in the
    context (a column header). None if there is no such label.
    """

    pattern = LABEL_PATTERNS["PERSON_NAME"]
    before = CLAUSE_BREAK.split(text[max(0, start - 40):start])[-1]
    labels = list(pattern.finditer(before))

    if labels:
        gap = before[labels[-1].end():]

        if not ARTICLE.search(gap) and len(re.findall(r"\w+", gap)) <= MAX_LABEL_GAP_WORDS:
            return labels[-1].group()

    match = pattern.search(context) if context else None

    return match.group() if match else None


def finding(pii_type, text, start, end, confidence, detector,
            rule_id, detection_method, matched_pattern, context_trigger=None):
    """
    One detected value, and how it was found:

    rule_id           the rule that reported it: "PHONE_REGEX"
    detection_method  "regex", "context_rule" or "ner"
    matched_pattern   the pattern or function that matched the value
    context_trigger   the label that made it PII ("Tel"), if any

    confidence is a hand-assigned ranking between rules, not a
    calibrated probability.
    """

    return {
        "type": pii_type,
        "text": text[start:end],
        "start": start,
        "end": end,
        "confidence": confidence,
        "detector": detector,
        "rule_id": rule_id,
        "detection_method": detection_method,
        "matched_pattern": matched_pattern,
        "context_trigger": context_trigger
    }


# --------------------------------------------------
# 1. REGEX DETECTOR
# --------------------------------------------------

EMAIL = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE
)

# OCR breaks addresses: a space or line break beside the "@"
# ("j.okafor @ example.com"), or the domain lost, so the text ends at the
# "@" ("j.okafor @"). Without a domain, the part before the "@" must look
# like one: four characters or more, with a letter and a dot or digit.
EMAIL_SPACED = re.compile(
    r"\b[A-Z0-9._%+-]+(?:\s@\s?|\s?@\s)[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE
)
EMAIL_DANGLING = re.compile(
    r"(?<![\w.%+@-])(?=[A-Z0-9._%+-]*[A-Z])(?=[A-Z0-9._%+-]*[.\d])"
    r"[A-Z0-9][A-Z0-9._%+-]{2,}[A-Z0-9] ?@(?![\w.-])",
    re.IGNORECASE
)

# Phone candidates. Separators are limited to one line, and a candidate
# cannot start inside an ID ("ISS-2026-0112"), date ("18/04/2026"),
# version ("10.0.19041") or a longer number.
PHONE = re.compile(
    r"(?<![\w./+-])"
    r"(?:\+\d{1,3}[ .-]?)?"        # country code: +44
    r"(?:\(\d{1,5}\)[ .-]?)?"      # area code in brackets: (212)
    r"\d[\d .-]*\d"                # number: 555-0147, 7555 0163
    r"(?![\w/-])"
)

# PDF line wraps split numbers: "+1 (212)\n555-0121", "+49 69\n5555 0177".
# A country code (and area code) ending the previous line joins the match.
WRAPPED_PHONE_PREFIX = re.compile(
    r"(?<![\w+])\+\d{1,3}(?:[ .-]?\(?\d{1,5}\)?)?[ \t]*\n[ \t]*$"
)

SSN = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")

# The 4th character of a PAN encodes the holder type (P = person, C = company...)
INDIAN_PAN = re.compile(r"(?<![\w-])[A-Z]{3}[ABCFGHJLPT][A-Z]\d{4}[A-Z](?![\w-])")

IP_ADDRESS = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# Payment card numbers: 13-19 digits, grouped by spaces or hyphens. Only
# those passing the Luhn check count, and without a card label only with
# a known card prefix (Visa 4, Mastercard 51-55 and 22-27, Amex 34/37,
# Diners 30/36/38, JCB 35, Discover 6011/64/65, UnionPay 62).
CARD = re.compile(r"(?<![\w.+-])\d(?:[ -]?\d){12,18}(?![\w-])")
CARD_PREFIX = re.compile(r"4|5[1-5]|2[2-7]|3[0478]|35|6(?:011|4[4-9]|5)|62")

# IBAN: country, check digits, then the account in groups of four
IBAN = re.compile(r"(?<![\w-])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?(?![\w-])")

# Polish PESEL: 11 digits that encode the birth date, with a check digit
PESEL = re.compile(r"(?<![\w-])\d{11}(?![\w-])")
PESEL_WEIGHTS = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)

# Indian Aadhaar: 12 digits, not starting 0 or 1, with a Verhoeff check
# digit. Without a label, only in its usual 4-4-4 grouping. Never three
# groups of a longer number: a card number or an IBAN goes on after them.
AADHAAR = re.compile(r"(?<![\w-])(?<!\d )[2-9]\d{3}([ -]?)\d{4}\1\d{4}(?![\w-]|[ -]\d)")

VERHOEFF_MULTIPLY = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6), (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8), (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2), (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4), (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
VERHOEFF_PERMUTE = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9), (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2), (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0), (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5), (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def luhn_valid(digits):
    total = 0

    for index, digit in enumerate(reversed(digits)):
        number = int(digit) * (2 if index % 2 else 1)
        total += number - 9 if number > 9 else number

    return total % 10 == 0


def iban_valid(value):
    """
    ISO 13616: moving the first four characters to the end and reading
    letters as numbers (A = 10) gives a number whose remainder by 97 is 1.
    """

    compact = value.replace(" ", "")

    if not 15 <= len(compact) <= 34:
        return False

    return int("".join(str(int(character, 36)) for character in compact[4:] + compact[:4])) % 97 == 1


def pesel_valid(digits):
    """
    The check digit matches, and the month and day can be a birth date.
    The month carries the century (+20 for 2000-2099...), so modulo 20
    it is the calendar month.
    """

    check = (10 - sum(int(digit) * weight for digit, weight in zip(digits, PESEL_WEIGHTS)) % 10) % 10

    return (
        check == int(digits[10])
        and 1 <= int(digits[2:4]) % 20 <= 12
        and 1 <= int(digits[4:6]) <= 31
    )


def verhoeff_valid(digits):
    check = 0

    for index, digit in enumerate(reversed(digits)):
        check = VERHOEFF_MULTIPLY[check][VERHOEFF_PERMUTE[index % 8][int(digit)]]

    return check == 0


def valid_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def valid_ssn(value):
    """
    SSA rules: area is not 000, 666 or 900-999; group is not 00;
    serial is not 0000.
    """

    area, group, serial = value.split("-")

    return (
        area not in ("000", "666")
        and not area.startswith("9")
        and group != "00"
        and serial != "0000"
    )


def looks_like_date(groups):
    """
    Detect common date formats and year ranges to prevent
    date strings from being classified as phone numbers.

    True for 2026-04-29, 29-04-2026, 04-29-2026, 29 04 26
    and year ranges like 2020-2024 or 2022 2023 2024.
    """

    numbers = [int(group) for group in groups]

    # Year ranges and lists of years
    if all(len(group) == 4 and group[:2] in ("19", "20") for group in groups):
        return True

    if len(groups) != 3:
        return False

    # YYYY-MM-DD
    if len(groups[0]) == 4:
        year, month, day = numbers
        return 1900 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31

    # DD-MM-YYYY or MM-DD-YYYY, with a 2- or 4-digit year
    first, second, year = numbers

    if len(groups[2]) == 4 and not 1900 <= year <= 2099:
        return False

    if len(groups[2]) not in (2, 4):
        return False

    day_first = 1 <= first <= 31 and 1 <= second <= 12
    month_first = 1 <= first <= 12 and 1 <= second <= 31

    return day_first or month_first


def phone_confidence(value, labelled):
    """
    Return a confidence for a phone candidate, or None to reject it.
    """

    groups = re.findall(r"\d+", value)
    digits = "".join(groups)

    if not 7 <= len(digits) <= 15:
        return None

    if valid_ip(value) or SSN.fullmatch(value) or looks_like_date(groups):
        return None

    international = value.startswith("+")
    area_code = "(" in value

    if not international:
        # Version and section numbers have single-digit parts ("10.0.19041");
        # phone numbers only do in a leading trunk code ("1-800-555-0199").
        if any(len(group) == 1 for group in groups[1:]):
            return None

        # Phone numbers end in a group of 3+ digits ("555-0147"),
        # unlike lists of small numbers ("12 13 14 15").
        if len(groups[-1]) < 3:
            return None

    if international or area_code:
        return 0.95

    # A bare digit run could be any ID: only accept it with a phone label.
    if len(groups) == 1:
        return 0.85 if labelled else None

    return 0.90 if labelled else 0.75


def regex_detector(text, context=""):
    findings = []

    for match in EMAIL.finditer(text):
        # OCR often attaches stray punctuation: "—_c.whitfield@..."
        start = match.start() + len(match.group()) - len(match.group().lstrip("._%+-_"))
        findings.append(finding(
            "EMAIL", text, start, match.end(), 0.99, "regex",
            "EMAIL_REGEX", "regex", "EMAIL"
        ))

    for pattern, name, confidence in (
        (EMAIL_SPACED, "EMAIL_SPACED", 0.95), (EMAIL_DANGLING, "EMAIL_DANGLING", 0.85)
    ):
        for match in pattern.finditer(text):
            start = match.start() + len(match.group()) - len(match.group().lstrip("._%+-_"))

            if not any(
                item["type"] == "EMAIL" and item["start"] < match.end() and start < item["end"]
                for item in findings
            ):
                findings.append(finding(
                    "EMAIL", text, start, match.end(), confidence, "regex",
                    "EMAIL_OCR", "regex", name
                ))

    for match in PHONE.finditer(text):
        start = match.start()

        wrapped = WRAPPED_PHONE_PREFIX.search(text[max(0, start - 20):start])

        if wrapped:
            start -= len(wrapped.group())

        label = find_label(text, start, "PHONE", context)
        confidence = phone_confidence(text[start:match.end()], label is not None)

        if confidence is not None:
            findings.append(finding(
                "PHONE", text, start, match.end(), confidence, "regex",
                "PHONE_REGEX", "regex",
                "PHONE+WRAPPED_PHONE_PREFIX" if wrapped else "PHONE", label
            ))

    for match in SSN.finditer(text):
        # Test data often uses invalid ranges (900-xx-xxxx),
        # so a label overrides the validity rules.
        label = find_label(text, match.start(), "US_SSN", context)

        if label:
            confidence = 0.99
        elif valid_ssn(match.group()):
            confidence = 0.85
        else:
            continue

        findings.append(finding(
            "US_SSN", text, match.start(), match.end(), confidence, "regex",
            "SSN_REGEX", "regex", "SSN", label
        ))

    for match in INDIAN_PAN.finditer(text):
        label = find_label(text, match.start(), "INDIAN_PAN", context)
        findings.append(finding(
            "INDIAN_PAN", text, match.start(), match.end(),
            0.99 if label else 0.90, "regex",
            "INDIAN_PAN_REGEX", "regex", "INDIAN_PAN", label
        ))

    for match in IP_ADDRESS.finditer(text):
        if valid_ip(match.group()):
            findings.append(finding(
                "IP_ADDRESS", text, match.start(), match.end(), 0.99, "regex",
                "IP_ADDRESS_REGEX", "regex", "IP_ADDRESS"
            ))

    ibans = []

    for match in IBAN.finditer(text):
        if iban_valid(match.group()):
            ibans.append((match.start(), match.end()))
            findings.append(finding(
                "BANK_ACCOUNT", text, match.start(), match.end(), 0.99, "regex",
                "IBAN_REGEX", "regex", "IBAN", find_label(text, match.start(), "BANK_ACCOUNT", context)
            ))

    for match in CARD.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        label = find_label(text, match.start(), "PAYMENT_CARD", context)

        # The digits of an IBAN can pass the Luhn check too
        if (
            not luhn_valid(digits)
            or not (label or CARD_PREFIX.match(digits))
            or any(start < match.end() and match.start() < end for start, end in ibans)
        ):
            continue

        findings.append(finding(
            "PAYMENT_CARD", text, match.start(), match.end(), 0.99 if label else 0.90, "regex",
            "CARD_REGEX", "regex", "CARD+LUHN", label
        ))

    for match in PESEL.finditer(text):
        if pesel_valid(match.group()):
            label = find_label(text, match.start(), "NATIONAL_ID", context)
            findings.append(finding(
                "NATIONAL_ID", text, match.start(), match.end(), 0.99 if label else 0.80, "regex",
                "PESEL_REGEX", "regex", "PESEL", label
            ))

    for match in AADHAAR.finditer(text):
        label = find_label(text, match.start(), "NATIONAL_ID", context)

        # Twelve digits in a row are often just a number: only with a label
        if verhoeff_valid(re.sub(r"\D", "", match.group())) and (label or match.group(1)):
            findings.append(finding(
                "NATIONAL_ID", text, match.start(), match.end(), 0.99 if label else 0.85, "regex",
                "AADHAAR_REGEX", "regex", "AADHAAR", label
            ))

    return findings


# --------------------------------------------------
# 2. CUSTOM RULE DETECTOR
# --------------------------------------------------

# (?!\d) rather than \b, so "EMP-41077_" still matches.
# OCR turns the hyphen into "." or "_".
EMPLOYEE_ID = re.compile(
    r"(?<![\w-])EMP[-_. ]?\d{4,10}(?!\d)",
    re.IGNORECASE
)

# Label-anchored rules: the value must follow its label in the same clause.
# Labels are case-insensitive; values are uppercase letters and digits.
LABEL_SEPARATOR = r"\s*[:#-]?\s*"

# Up to four plain words may sit between label and value:
# "passport number of the employee is TK4820913"
FILLER_WORDS = r"(?P<filler>(?:[A-Za-z]+\s+){0,4}?)"

PASSPORT_VALUE = r"(?=[A-Z0-9]*\d)[A-Z0-9]{6,9}(?![\w-])"

PASSPORT_LABELLED = re.compile(
    r"(?P<label>(?i:\b(?:passport|travel document)(?:\s+(?:no\.?|number|num\.?|#))?))"
    + LABEL_SEPARATOR + FILLER_WORDS
    + rf"(?P<value>{PASSPORT_VALUE})"
)

# OCR breaks SSN separators ("900-42.7701", "90042-1290"),
# so a labelled SSN accepts any separator or none.
SSN_VALUE = r"\d{3}[ .-]?\d{2}[ .-]?\d{4}(?![\w-])"

SSN_LABELLED = re.compile(
    r"(?P<label>(?i:\b(?:ssn|social security(?:\s+(?:number|no\.?))?)))"
    + LABEL_SEPARATOR
    + rf"(?P<value>{SSN_VALUE})"
)

EMPLOYEE_VALUE = r"(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{3,14}(?![\w-])"

EMPLOYEE_LABELLED = re.compile(
    r"(?P<label>(?i:\b(?:employee|staff|emp)\s*(?:id|no\.?|number|code|#)))"
    + LABEL_SEPARATOR
    + rf"(?P<value>{EMPLOYEE_VALUE})"
)

# A date of birth is a date after its label. Other dates are not PII.
MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
DATE_VALUE = (
    r"(?:\d{1,2}[./-]\d{1,2}[./-](?:\d{4}|\d{2})"            # 14/05/1984, 14.05.84
    r"|\d{4}[./-]\d{1,2}[./-]\d{1,2}"                        # 1984-05-14
    rf"|\d{{1,2}}(?:st|nd|rd|th)?[ -](?i:{MONTH})\.?[ ,-]+\d{{4}}"   # 14 May 1984
    rf"|(?i:{MONTH})\.? \d{{1,2}}(?:st|nd|rd|th)?,? \d{{4}})"        # May 14, 1984
    r"(?![\w-])"
)

DOB_LABELLED = re.compile(
    r"(?P<label>(?i:\b(?:date of birth|birth ?date|dob|d\.o\.b|born(?: on)?)\b\.?))"
    + LABEL_SEPARATOR + FILLER_WORDS
    + rf"(?P<value>{DATE_VALUE})"
)

# Card numbers printed masked, all but the last four digits hidden:
# "**** **** **** 4417", or "card ending in 4417"
MASKED_CARD = r"(?:[*xX•]{4}[ -]?){1,3}\d{4}(?![\w-])"

CARD_MASKED_LABELLED = re.compile(
    r"(?P<label>(?i:\b(?:(?:credit|debit|payment|corporate|company) )?"
    r"(?:card|visa|mastercard|amex)(?: (?:no\.?|number))?\b))"
    + LABEL_SEPARATOR + FILLER_WORDS
    + rf"(?P<value>{MASKED_CARD})"
)

CARD_ENDING = re.compile(
    r"(?P<label>(?i:\b(?:card|visa|mastercard|amex)\b(?:[ \t]+\w+){0,3}?[ \t]+ending(?: in)?))"
    r"\s*[:#-]?\s*(?P<value>\d{4})(?![\w-])"
)

# A bank account number after its label, possibly masked ("****5678")
ACCOUNT_VALUE = r"(?:[*xX•]{2,}[ -]?\d{4,}|\d(?:[ -]?\d){5,17})(?![\w-])"

ACCOUNT_LABELLED = re.compile(
    r"(?P<label>(?i:\b(?:(?:bank )?account (?:no\.?|number)|acct\.?(?: (?:no\.?|number))?"
    r"|a/c(?: no\.?)?|bank account)))"
    + LABEL_SEPARATOR + FILLER_WORDS
    + rf"(?P<value>{ACCOUNT_VALUE})"
)

# A national ID after its label: "National Insurance number: QQ 12 34 56 C"
NATIONAL_ID_VALUE = r"(?=[A-Z0-9 -]*\d)[A-Z0-9](?:[ -]?[A-Z0-9]){5,15}(?![\w-])"

NATIONAL_ID_LABELLED = re.compile(
    r"(?P<label>(?i:\b(?:pesel|aadhaar(?: (?:no\.?|number))?"
    r"|national (?:id|identity|identification|insurance)(?: (?:no\.?|number|card))?"
    r"|ni number|nino|personal id(?:entification)? number)))"
    + LABEL_SEPARATOR + FILLER_WORDS
    + rf"(?P<value>{NATIONAL_ID_VALUE})"
)

# Home addresses. A street address: house number, street name and a
# street word ("221B Baker Street"), or a Polish "ul. Kwiatowa 12/4",
# then optionally a flat, town and postcode (UK, US ZIP, Polish).
STREET_SUFFIX = (
    r"Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr|Way|Close|Place|Pl|Boulevard"
    r"|Blvd|Crescent|Terrace|Gardens|Square|Sq|Mews|Parade|Highway|Hwy"
)
# A capitalised word in lowercase after its capital ("Ashwood",
# "McKinley", "Saint-Denis"), so a postcode's "NW" is not taken for a town
CAP_WORD = rf"[{UPPER}][{LOWER}]+(?:[-'’]?[{UPPER}]?[{LOWER}]+)*"
POSTCODE = r"[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}|\d{2}-\d{3}|\d{5}(?:-\d{4})?"

# An address may go on over the next lines: "12 Oak Lane\nLeeds\nLS6 2AB".
# A town on a line of its own counts only with a postcode after it.
ADDRESS_SEPARATOR = r"(?:,? ?\n ?|,? )"
TOWN = rf"{CAP_WORD}(?: {CAP_WORD})?"

STREET_ADDRESS = re.compile(
    rf"(?<![\w-])(?:\d{{1,5}}[A-Za-z]?,? (?:{CAP_WORD} ){{1,3}}(?:{STREET_SUFFIX})\b\.?"
    rf"|(?:ul|al|os)\. ?{CAP_WORD}(?: {CAP_WORD})? \d{{1,4}}[A-Za-z]?(?:/\d{{1,4}})?)"
    rf"(?:,? (?:Apt|Flat|Unit|Suite)\.? ?\w+)?"
    rf"(?:,? (?:{POSTCODE}))?"                      # Polish order: postcode, then town
    rf"(?:,? {TOWN})?"
    rf"(?:{ADDRESS_SEPARATOR}(?:{TOWN}{ADDRESS_SEPARATOR})?(?:[A-Z]{{2}} )?(?:{POSTCODE})(?: {TOWN})?)?"
)

# What makes text after an address label an address, not "Address the
# findings by Friday": a house number, a postcode or a street word
ADDRESS_EVIDENCE = re.compile(
    rf"\d{{1,5}}[A-Za-z]?,? {CAP_WORD}|\b(?:{POSTCODE})\b|\b(?:{STREET_SUFFIX})\b"
)

# One comma-separated part of a labelled address, starting with a capital
# or a digit; a part in lowercase ("..., which she shares") ends it
ADDRESS_PART = re.compile(rf"[{UPPER}0-9](?:[^,\n;|.]|\.(?! ))*")
MAX_ADDRESS_CHARS = 150

ADDRESS_CELL = re.compile(rf"(?s)(?=.*?(?:{ADDRESS_EVIDENCE.pattern})).{{5,{MAX_ADDRESS_CHARS}}}")

# For a table cell the label is usually the column header, not the cell.
# If the header names the type, the whole cell must be a value of it.
CELL_VALUES = {
    "PASSPORT_NUMBER": ("PASSPORT_VALUE", re.compile(PASSPORT_VALUE), 0.90),
    "US_SSN": ("SSN_VALUE", re.compile(SSN_VALUE), 0.95),
    "EMPLOYEE_ID": ("EMPLOYEE_VALUE", re.compile(EMPLOYEE_VALUE), 0.90),
    "DATE_OF_BIRTH": ("DATE_VALUE", re.compile(DATE_VALUE), 0.95),
    "NATIONAL_ID": ("NATIONAL_ID_VALUE", re.compile(NATIONAL_ID_VALUE), 0.90),
    "PAYMENT_CARD": ("MASKED_CARD", re.compile(MASKED_CARD), 0.90),
    "BANK_ACCOUNT": ("ACCOUNT_VALUE", re.compile(ACCOUNT_VALUE), 0.90),
    "POSTAL_ADDRESS": ("ADDRESS_CELL", ADDRESS_CELL, 0.85),
}


def address_findings(text):
    """
    Home addresses: the comma-separated parts after an address label
    ("Home address: 12 Ashwood Lane, Leeds LS6 2AB"), and street
    addresses with or without a label. Overlapping matches are one
    address, reported once over all of them.
    """

    spans = []      # (start, end, label)

    for label in LABEL_PATTERNS["POSTAL_ADDRESS"].finditer(text):
        start = position = label.end() + len(re.match(r"\s*[:#-]?\s*", text[label.end():]).group())
        end = None

        while part := ADDRESS_PART.match(text, position):
            end = part.end()
            comma = re.match(r", *", text[end:])

            if not comma:
                break

            position = end + comma.end()

        if end is None:
            continue

        end = start + len(text[start:end].rstrip(" ."))

        if end - start <= MAX_ADDRESS_CHARS and ADDRESS_EVIDENCE.search(text[start:end]):
            spans.append((start, end, label.group()))

    spans.extend((match.start(), match.end(), None) for match in STREET_ADDRESS.finditer(text))

    merged = []

    for start, end, label in sorted(spans, key=lambda span: span[:2]):
        if merged and start < merged[-1][1]:
            previous = merged[-1]
            merged[-1] = (previous[0], max(previous[1], end), previous[2] or label)
        else:
            merged.append((start, end, label))

    return [
        finding(
            "POSTAL_ADDRESS", text, start, end, 0.90, "custom_rule",
            "ADDRESS_LABELLED", "context_rule", "ADDRESS_LABELLED", label
        )
        if label else
        finding(
            "POSTAL_ADDRESS", text, start, end, 0.80, "custom_rule",
            "STREET_ADDRESS", "regex", "STREET_ADDRESS"
        )
        for start, end, label in merged
    ]


def custom_rule_detector(text, context=""):
    findings = []

    for match in EMPLOYEE_ID.finditer(text):
        findings.append(finding(
            "EMPLOYEE_ID", text, match.start(), match.end(), 0.95, "custom_rule",
            "EMPLOYEE_ID_PREFIX", "regex", "EMPLOYEE_ID"
        ))

    for pii_type, rule_id, pattern, confidence in (
        ("PASSPORT_NUMBER", "PASSPORT_LABELLED", PASSPORT_LABELLED, 0.95),
        ("US_SSN", "SSN_LABELLED", SSN_LABELLED, 0.99),
        ("EMPLOYEE_ID", "EMPLOYEE_LABELLED", EMPLOYEE_LABELLED, 0.90),
        ("DATE_OF_BIRTH", "DOB_LABELLED", DOB_LABELLED, 0.95),
        ("PAYMENT_CARD", "CARD_MASKED_LABELLED", CARD_MASKED_LABELLED, 0.90),
        ("PAYMENT_CARD", "CARD_ENDING", CARD_ENDING, 0.85),
        ("BANK_ACCOUNT", "ACCOUNT_LABELLED", ACCOUNT_LABELLED, 0.90),
        ("NATIONAL_ID", "NATIONAL_ID_LABELLED", NATIONAL_ID_LABELLED, 0.95),
    ):
        for match in pattern.finditer(text):
            # Words between label and value weaken the link
            score = 0.90 if match.groupdict().get("filler") else confidence

            findings.append(finding(
                pii_type, text, match.start("value"), match.end("value"),
                score, "custom_rule",
                rule_id, "context_rule", rule_id, match.group("label")
            ))

    findings.extend(address_findings(text))

    value = text.strip()

    if context and value:
        start = text.index(value)

        for pii_type, (pattern_name, pattern, confidence) in CELL_VALUES.items():
            label = LABEL_PATTERNS[pii_type].search(context)

            if label and pattern.fullmatch(value):
                # Under an address header the whole cell is the address,
                # not only the street line found in it
                if pii_type == "POSTAL_ADDRESS":
                    findings = [item for item in findings if item["type"] != "POSTAL_ADDRESS"]

                findings.append(finding(
                    pii_type, text, start, start + len(value),
                    confidence, "custom_rule",
                    f"{pii_type}_COLUMN", "context_rule", pattern_name, label.group()
                ))

    return findings


# --------------------------------------------------
# 3. LOCAL NLP DETECTOR
# --------------------------------------------------

# spaCy's small model tags many non-names as PERSON ("Trigger", "Vendor
# Tier", "QR.35", "APPROVER") and runs names together across lines
# ("Daniela Figueroa\nBridgett Gatewood"). Its spans are therefore cut
# into name-shaped parts and checked before they are reported.

# Name parts: initials ("M.", "DK", "Ł."), capitalised words ("Achebe",
# "Okonkwo-Bell", "O'Brien", "McDonald", "Wiśniewski") and lowercase
# particles ("van", "de")
NAME_INITIAL = re.compile(rf"(?:[{UPPER}]\.){{1,2}}|[{UPPER}]{{1,2}}")
NAME_WORD = re.compile(rf"(?:[{UPPER}]['’]|Ma?c)?[{UPPER}][{LOWER}]+(?:[-'’][{UPPER}]?[{LOWER}]+)*")
NAME_PARTICLES = {
    "van", "von", "de", "da", "del", "della", "di", "du",
    "bin", "ibn", "al", "el", "le", "la"
}
STREET_WORDS = {
    "Lane", "Street", "Road", "Avenue", "Court", "Drive",
    "Way", "Place", "Close", "Boulevard"
}
# Job titles sit next to names but are not names: "Chief Executive Officer"
TITLE_WORDS = {
    "Chief", "Officer", "Executive", "Director", "Manager", "Head", "President",
    "Counsel", "Engineer", "Analyst", "Secretary", "Chair", "Chairman",
    "Partner", "Associate", "Assistant", "Specialist", "Consultant",
    "Administrator", "Coordinator", "Supervisor", "Controller", "Treasurer", "Auditor"
}
# Teams and offices sit where people's names do ("Contact the Service
# Desk", author "Risk Office") but name no one
TEAM_WORDS = {
    "Desk", "Office", "Team", "Department", "Committee", "Support", "Service",
    "Services", "Board", "Group", "Unit", "Division", "Centre", "Center", "Council"
}
MAX_NAME_TOKENS = 5

# A whole word in lowercase, with at most surrounding punctuation
LOWERCASE_WORD = re.compile(rf"\(?[{LOWER}][{LOWER}'-]+[),.;:]?")


def lowercase_words(texts):
    """
    Words the texts use in lowercase ("workflow", "vendor"): ordinary
    words, so a capitalised "Change Workflow" is not a name. Whole words
    only: "achebe" inside m.achebe@cadencefg.com does not count.
    """

    return {
        word.strip("(),.;:")
        for text in texts
        for word in text.split()
        if LOWERCASE_WORD.fullmatch(word)
    }


def name_token_kind(token):

    if NAME_INITIAL.fullmatch(token):
        return "initial"

    if NAME_WORD.fullmatch(token):
        return "word"

    if token in NAME_PARTICLES:
        return "particle"

    return None


def name_runs(text, start, end):
    """
    Runs of name-like tokens in text[start:end], as lists of
    (token, start, end). Brackets, trailing commas and possessives
    ("Nakagawa's") are trimmed off tokens.

    A run never crosses a line break: lines hold separate names or a
    title ("T. Lindqvist\nCIO"). Initials alone on a line are the
    exception and join the surname on the next line ("N. K.\nOsadebe").
    """

    runs = []
    line_ends = set()       # runs that reach the end of their line
    position = start

    for line_number, line in enumerate(text[start:end].split("\n")):
        run = []

        for match in re.finditer(r"\S+", line):
            raw = match.group()
            token = re.sub(r"['’]s$", "", raw.lstrip("([").rstrip(")],;:"))
            token_start = position + match.start() + len(raw) - len(raw.lstrip("(["))

            if token and name_token_kind(token):
                run.append((token, token_start, token_start + len(token), line_number))

                # A comma ends a name: "Database, Cloud" is a list. So
                # does a closing bracket: "[OC] Aditya Lawand"
                if raw.endswith((",", ";", ":", ")", "]")):
                    runs.append(run)
                    run = []

            elif run:
                runs.append(run)
                run = []

        if run:
            runs.append(run)
            line_ends.add(id(run))

        position += len(line) + 1

    merged = []

    for run in runs:
        previous = merged[-1] if merged else None

        if (
            previous
            and id(previous) in line_ends
            and run[0][3] == previous[-1][3] + 1
            and all(name_token_kind(token[0]) == "initial" for token in previous)
            and name_token_kind(run[0][0]) == "word"
        ):
            merged[-1] = previous + run
        else:
            merged.append(run)

    return [[token[:3] for token in run] for run in merged]


def plausible_name(tokens, common_words):
    """
    (start, end) of the name in a run of name-like tokens, or None.
    """

    while tokens and name_token_kind(tokens[0][0]) == "particle":
        tokens = tokens[1:]

    while tokens and name_token_kind(tokens[-1][0]) == "particle":
        tokens = tokens[:-1]

    words = [token for token, _, _ in tokens if name_token_kind(token) == "word"]

    if (
        2 <= len(tokens) <= MAX_NAME_TOKENS
        # A name ends with a surname, not an initial ("Engineer II")
        and name_token_kind(tokens[-1][0]) == "word"
        # OCR fragments such as "Qi Bi" or "Te Ft"
        and max(len(word) for word in words) >= 4
        # "Change Workflow", "Vendor Tier", "Privacy Shield"
        and not any(word.lower() in common_words for word in words)
        # "Ashwood Lane" is an address, not a person
        and words[-1] not in STREET_WORDS
        and not any(word in TITLE_WORDS or word in TEAM_WORDS for word in words)
    ):
        return tokens[0][1], tokens[-1][2]

    return None


class LocalNLPDetector:
    """
    Person names: spaCy PERSON entities cut into plausible names, plus
    name-shaped text that spaCy missed next to a person label
    ("Chair: ...", a NAME or USERS column header).
    """

    def __init__(self, model_name="en_core_web_sm"):
        self.nlp = None
        self.error = None
        self.stop_words = set()

        if spacy is None:
            self.error = (
                "spaCy is not installed; NLP detection is unavailable."
            )
            return

        try:
            self.nlp = spacy.load(model_name)
            self.stop_words = set(self.nlp.Defaults.stop_words)
        except (OSError, IOError) as exc:
            self.error = (
                f"spaCy model '{model_name}' is unavailable: {exc}"
            )

    def analyse(self, texts, batch_size=64):
        """
        For each text: spaCy's PERSON spans, and the part of speech of
        each word by its start offset ("PROPN" for a proper noun, "VERB"
        for "Read" in "Read the policy"). spaCy processes the texts in
        batches, which is much faster than one call per text.
        """

        if self.nlp is None:
            return [([], {}) for _ in texts]

        return [
            (
                [(entity.start_char, entity.end_char) for entity in doc.ents if entity.label_ == "PERSON"],
                {token.idx: token.pos_ for token in doc}
            )
            for doc in self.nlp.pipe(texts, batch_size=batch_size)
        ]

    def entity_spans(self, texts, batch_size=64):
        """
        spaCy PERSON spans for each text.
        """

        return [spans for spans, _ in self.analyse(texts, batch_size)]

    def common_words(self, texts):
        return self.stop_words | lowercase_words(texts)

    def detect(self, text, context="", spans=None, common_words=None):
        """
        Person names in text. spans and common_words can be computed
        once for a whole document (see document_scanner); otherwise
        they come from this text alone.
        """

        if self.nlp is None:
            return []

        if spans is None:
            spans = self.entity_spans([text])[0]

        if common_words is None:
            common_words = self.common_words([text])

        findings = []

        # Provisional heuristics; not model-calibrated.
        for start, end in spans:
            for tokens in name_runs(text, start, end):
                name = plausible_name(tokens, common_words)

                if name:
                    label = find_label(text, name[0], "PERSON_NAME", context)

                    findings.append(finding(
                        "PERSON_NAME", text, *name, 0.90 if label else 0.85, "nlp",
                        "PERSON_NER", "ner", "spacy:PERSON", label
                    ))

        # Names spaCy missed: name-shaped text close after a person label
        for tokens in name_runs(text, 0, len(text)):
            name = plausible_name(tokens, common_words)
            label = person_label(text, name[0], context) if name else None

            if (
                label
                and not any(
                    name[0] < item["end"] and item["start"] < name[1]
                    for item in findings
                )
            ):
                findings.append(finding(
                    "PERSON_NAME", text, *name, 0.80, "name_rule",
                    "PERSON_LABELLED", "context_rule", "name_runs", label
                ))

        return findings


# --------------------------------------------------
# 4. COMBINE AND DEDUPLICATE
# --------------------------------------------------

def combine_detections(*detector_results):
    """
    Deduplicate findings while preserving valid overlaps.

    Findings that only partly overlap are all keptas well , e.g. PERSON_NAME
    "John" inside EMAIL "john@example.com". Overlaps are resolved later,
    at redaction time.

    Findings on exactly the same span are competing readings of one value
    (e.g. "900-42.7701" as US_SSN and PHONE), so the more confident one wins.
    """

    combined = {}

    for results in detector_results:
        for item in results:
            key = (
                item["start"],
                item["end"]
            )

            existing = combined.get(key)

            if (
                existing is None
                or item["confidence"] > existing["confidence"]
            ):
                combined[key] = item

    return sorted(
        combined.values(),
        key=lambda item: (
            item["start"],
            item["end"],
            item["type"]
        )
    )


def detect_pii(text, nlp_detector=None, context=""):
    """
    Detect PII in text.

    context is extra text used only to find labels, never searched for
    values itself: e.g. the column header of a table cell.
    """

    regex_results = regex_detector(text, context)
    custom_results = custom_rule_detector(text, context)

    nlp_results = (
        nlp_detector.detect(text, context=context)
        if nlp_detector is not None
        else []
    )

    return combine_detections(
        regex_results,
        custom_results,
        nlp_results
    )
