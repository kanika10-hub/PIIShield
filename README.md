# PII Detection & Redaction Pipeline 

This project takes business documents (**PDF, PowerPoint, Word**), finds the
**personally identifiable information (PII)** inside them (names, emails,
phone numbers, government IDs and so on), and produces a **redacted copy** that is
safe to hand to an AI model (LLM).

The idea is simple: before a document's text goes to an LLM for analysis,
every piece of personal data in it should be found and removed.

> **New here?** Read the sections below in order. They go from "what does it do"
> to "where is the code for X". The [Glossary](#glossary) at the end explains
> the project's terms.

---

## Contents

1. [The big picture](#1-the-big-picture)
2. [Setup](#2-setup)
3. [Running it](#3-running-it)
4. [Folder map](#4-folder-map)
5. [The five steps in detail](#5-the-five-steps-in-detail)
6. [The output files](#6-the-output-files)
7. [Tests and helper scripts](#7-tests-and-helper-scripts)
8. [Common tasks](#8-common-tasks)
9. [Gotchas](#9-gotchas)
10. [Leftover and legacy files](#10-leftover-and-legacy-files)
11. [Suggested reading order](#11-suggested-reading-order)
12. [Glossary](#glossary)

---

## 1. The big picture

Every document goes through the same **five steps**. Your file is first copied
into a folder of its own, `backend/uploads/<job_id>/`. Each step then writes one
JSON file into the run's own folder, `backend/output/<job_id>/` (`<job>/`
below), and the next step reads that file.

```
   your file (.pdf / .pptx / .docx), from any folder, under any name
            │
            ▼
  ┌──────────────────────┐  code: main.py, src/uploads.py
  │ 0. CHECK and UPLOAD  │──►  uploads/<job_id>/<file>
  └──────────────────────┘      A local copy (+ upload.json: origin, SHA-256).
            │                   Every later step reads this copy
            ▼
  ┌──────────────────────┐  code: src/extractors/  (+ src/ocr/, src/structure/)
  │ 1. EXTRACT the text  │──►  <job>/<name>.json
  └──────────────────────┘      All the text, with its page, position, type
            │                   (heading, table cell, ...) and how it was read.
            │                   Checked against the common format before step 2
            ▼
  ┌──────────────────────┐  code: src/pii/detectors.py, src/pii/document_scanner.py
  │ 2. SCAN for PII      │──►  <job>/<name>_pii.json
  └──────────────────────┘      One "finding" per PII value: what, where, how found
            │
            ▼
  ┌──────────────────────┐  code: src/pii/redactor.py
  │ 3. TAG and REDACT    │──►  <job>/<name>_tagged.json      "[PERSON_NAME: Jane Doe]"  (still has PII)
  └──────────────────────┘──►  <job>/<name>_sanitized.json   "[REDACTED: PERSON_NAME]"
            │
            ▼
  ┌──────────────────────┐  code: src/pii/validator.py
  │ 4. VALIDATE the copy │──►  <job>/<name>_validation.json
  └──────────────────────┘      Verdict: PASS / NEEDS_REVIEW / FAIL
            │
            ▼
  ┌──────────────────────┐  code: src/pii/exposure_report.py
  │ 5. REPORT exposure   │──►  <job>/<name>_exposure.json    One row per finding: where, type,
  └──────────────────────┘──►  output/exposure_report.md     detector, confidence, action.
                          ──►  output/exposure_report.csv    No PII values: shareable
            │
            ▼
   Only a sanitized copy with the verdict PASS may be sent to an LLM,
   and approved_copy() in src/pii/validator.py is the way to load it.
```

`backend/main.py` runs all five steps in a row. Each step can also be run
on its own (see [Common tasks](#8-common-tasks)). Before step 1 it checks
the input file itself, and refuses an empty, renamed, password-protected or
damaged file before anything is uploaded, extracted or deleted.

### Why is it built this way?

- **Every file format becomes the same JSON first.** PDF, PPTX and DOCX are
  very different inside, so each one has its own *extractor*. They all produce the
  same "common JSON" format, so the PII steps only need to understand one format.
- **Everything can be traced.** Every piece of text has an ID (`block_id`),
  and every finding records its block and its exact character offsets in that
  block. You can always tell exactly where a redaction came from. The
  exposure report (step 5) keeps that trail without the values themselves.
- **The project checks its own work.** The validator rescans the redacted copy
  and compares it with the original. It does not assume the redaction worked.
- **Everything runs locally.** Detection uses regular expressions plus a local
  spaCy language model, and OCR uses Tesseract installed on your machine. No
  document text is sent to an outside service.

---

## 2. Setup

You need **three things**: Python, the Python packages, and **Tesseract OCR**
(a separate program, not a pip package).

### 2.1 Python and packages

The project was developed with **Python 3.14** in a virtual environment at
`.venv/`. From the project root (Windows PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` installs:

| Package | Used for |
|---|---|
| `PyMuPDF` | Reading PDFs and rendering PDF pages to images |
| `python-pptx` | Reading PowerPoint files |
| `python-docx` | Reading Word files |
| `pytesseract` | The Python wrapper that calls Tesseract OCR |
| `Pillow`, `numpy` | Image handling (cleaning images before OCR, finding PDF tables) |
| `spacy` + `en_core_web_sm` | Finding **person names** (the small English model is downloaded by the URL in `requirements.txt`) |
| `Flask` | The web interface (`app.py`), served on this computer only |

### 2.2 Tesseract OCR

Tesseract reads text out of images: scanned PDF pages and screenshots inside
documents. Install it separately:

- Windows: use the installer from the [UB Mannheim Tesseract builds](https://github.com/UB-Mannheim/tesseract/wiki)
  and make sure its folder (usually `C:\Program Files\Tesseract-OCR`) is on your `PATH`.
- Check that it works: `tesseract --version`

> If Tesseract is missing, scanned PDF pages **cannot be read at all**, and
> images in PPTX/DOCX files are marked "could not be OCR'd". If the spaCy model
> is missing, the pipeline still runs, but **person names are not detected**
> (it prints a warning).

---

## 3. Running it

**Always run commands from inside `backend/`**, because the code imports
`src....` and uses relative paths like `input/` and `output/`.

### 3.1 Web interface

```powershell
cd backend
python app.py
```

Then open **http://127.0.0.1:5000** in a browser. (`python app.py 5050` uses
another port.)

The page, **PII Exposure Scanner**, lets you:

1. **Choose the input** in one of the two boxes under *Input file*, as with
   `main.py` (drag and drop, or click to choose):
   - **Document file** (PDF, PPTX or DOCX): a copy is stored, then extracted
     into common JSON.
   - **Extracted JSON**: a document already extracted, e.g. the *Extracted
     JSON* download of an earlier scan. It is loaded and checked against the
     common format, and extraction is skipped. A PII report, a tagged or
     sanitized copy, or JSON that breaks the format is refused.

   The file is checked in the browser (type, size up to 100 MB; a file in the
   wrong box is refused with a pointer to the right one) and again on the
   server (a document is really that type: not empty, renamed,
   password-protected or damaged).
2. **Follow the scan.** *Processing Status* shows each step as it runs: upload,
   text extraction, extraction check, PII detection, tagging and sanitization,
   validation, audit report. *PII Findings* shows the real counts by type once
   detection has finished.
3. **Review the results** in five tabs:
   - **Extracted Text:** the document page by page, tables as tables, with
     every finding highlighted. Select a highlight to open its finding.
   - **PII Findings:** a searchable table (type, value, confidence, page or
     slide, location, review status). Select a row to see the rule that found
     it, what was done with it, and the surrounding text.
   - **Tagged Text** and **Sanitized Text:** the two copies the redactor wrote,
     side by side with the original.
   - **Validation and Audit:** the verdict, whether the copy is **approved or
     blocked for AI processing** (by `approved_copy()`, the same gate as
     `main.py`), every check and issue, extraction warnings, redaction errors,
     and downloads: extracted JSON, findings JSON, sanitized JSON, validation
     report, and audit report (Markdown and JSON).
4. **Scan another document**, or **delete this scan's files**.

Each upload runs the same steps as `main.py` (`src/web/jobs.py`), in its own
`uploads/<job_id>/` and `output/<job_id>/` folders, one scan at a time. The
server listens on `127.0.0.1` only and the page loads nothing from the
internet, so documents and findings stay on this computer.

### 3.2 Command line

```powershell
cd backend
python main.py input/risk_policy.pdf
python main.py "D:\anywhere\Q3 vendor review.docx"    # any PDF, PPTX or DOCX, from any folder
```

If you run `python main.py` with no file name, it opens a file chooser to pick
the file. Where no window can be opened (for example over SSH), it asks you to
type the path instead.

**Each run gets its own folders.** The file is checked, then copied into
`uploads/<job_id>/` under its original name, with an `upload.json` recording
where it came from, its size and SHA-256. Every step reads that copy, and writes
its outputs into `output/<job_id>/`. The job ID is the upload time, the file
name and a random suffix, e.g. `20261012-143005_risk_policy_3f9a1c`. So no
file name is fixed or special, and `report.pdf`, `report.docx` and a newer
`report.pdf` never overwrite each other's outputs. The extractor is chosen from
the file's type (`EXTRACTORS` in `main.py`), after the check has confirmed the
content really is that type.

What you will see (shortened):

```
[0/5] Uploading risk_policy.pdf ...
      Stored copy: uploads\20261012-143005_risk_policy_3f9a1c\risk_policy.pdf
      Outputs:     output\20261012-143005_risk_policy_3f9a1c
[1/5] Extracting risk_policy.pdf ...
      Common JSON: output\20261012-143005_risk_policy_3f9a1c\risk_policy.json
      Format:      OK (35 pages)
[2/5] Scanning for PII ...
      PII report:  output\20261012-143005_risk_policy_3f9a1c\risk_policy_pii.json
      Findings:    ... {'PERSON_NAME': ..., 'EMAIL': ..., 'PHONE': ...}
[3/5] Tagging and redacting ...
      Sanitized:   output\20261012-143005_risk_policy_3f9a1c\risk_policy_sanitized.json
[4/5] Validating the sanitized copy ...
[5/5] Reporting the PII exposure ...
      Exposure:    output\20261012-143005_risk_policy_3f9a1c\risk_policy_exposure.json  (no PII values: shareable)
      Findings:    ..., redacted ..., not redacted 0, flagged by validation ...
      Combined:    output\exposure_report.md, output\exposure_report.csv (3 documents)
Verdict: NEEDS_REVIEW
  PASS          provenance    0 issues
  ...
Blocked: do not send output\20261012-143005_risk_policy_3f9a1c\risk_policy_sanitized.json to an LLM (the validation verdict is NEEDS_REVIEW: ...).
A person must review the issues first.
```

(The paths are printed in full; they are shortened here.)

The last line is the **release decision**: `Approved` only for a PASS copy,
otherwise `Blocked` with the reason.

**Exit code:** `0` = PASS, `1` = FAIL or an error, `2` = NEEDS_REVIEW. This makes
the script easy to call from other scripts.

**When it stops early.** A bad input file is refused with one line saying why,
and nothing is uploaded or changed. If the extracted JSON doesn't follow the common format,
the run stops before the scan: the `_pii.json` is written with status `failed`,
and no tagged or sanitized copy is made.

**Re-running without re-extracting.** OCR on the 35-page PDF is slow. If you
only changed detection, redaction or validation code, pass the already extracted
JSON instead. Step 1 is then skipped, and the outputs are written next to it:

```powershell
python main.py output/<job_id>/risk_policy.json
```

**Re-running a stored upload.** To extract the same file again (for example
after changing an extractor), pass its stored copy. It is not copied again: the
run reuses its job folder and replaces that job's outputs.

```powershell
python main.py uploads/<job_id>/risk_policy.pdf
```

### The three sample documents

These are in `backend/input/`. Identical copies are in `samples/`.

| File | What it is | How the text is read |
|---|---|---|
| `risk_policy.pdf` | 35-page "Group Risk Management Policy". **Scanned**: the pages are images with no text layer | OCR on every page, then tables and headings are rebuilt |
| `organizational_pack.pptx` | 9-slide org pack: org charts, RACI tables, a contact directory | Native PowerPoint reading (no OCR needed except for pictures) |
| `tprm_training.docx` | Training guide with many tables and 33 embedded screenshots/diagrams | Native Word reading, plus OCR of the screenshots |

The detailed analysis of these files (structure, where the PII is, what is hard
about each one) is in [`docs/phase1/Phase1_Artifact_Analysis_Matrix.pdf`](docs/phase1/Phase1_Artifact_Analysis_Matrix.pdf).

> **Don't expect PASS on the samples.** At the time of writing (October 2026),
> the stored outputs give `NEEDS_REVIEW` for the PPTX and DOCX and `FAIL` for
> the PDF. This is the validator doing its job: it flags low-confidence OCR,
> name fragments left in the copy, and similar issues that a person should check.

---

## 4. Folder map

```
optivcase2/
├── README.md                     ← you are here
├── requirements.txt              Python dependencies (including the spaCy model)
├── docs/phase1/                  Analysis of the 3 sample files (.docx and .pdf)
├── samples/                      Original copies of the 3 sample input files
├── .venv/                        Local Python virtual environment (not project code)
│
└── backend/                      ★ ALL the code lives here
    ├── main.py                   ★ ENTRY POINT: runs the 5-step pipeline on one file
    ├── app.py                    ★ WEB INTERFACE: python app.py, then http://127.0.0.1:5000
    ├── static/                   The page: index.html, app.css, app.js (no external libraries)
    ├── input/                    The 3 sample files (any other file can be given from anywhere)
    ├── uploads/                  A copy of every file given to main.py: uploads/<job_id>/ (not committed)
    ├── output/                   Everything the pipeline writes: one folder per run, output/<job_id>/
    │                             (not committed), plus the combined exposure report
    │
    ├── src/
    │   ├── uploads.py            STEP 0: stores each input file in a job folder of its own
    │   │
    │   ├── web/                  Used by app.py
    │   │   ├── jobs.py               Runs main.py's steps for each upload, in the background
    │   │   └── views.py              Turns the output files into what the page shows
    │   │
    │   ├── models/
    │   │   ├── document.py       The common JSON data model (Document → Page → Block)
    │   │   └── schema.py         Checks a document follows that format (run before the scan)
    │   │
    │   ├── extractors/           STEP 1: one extractor per file type
    │   │   ├── pdf_extractor.py      PDF: text layer if there is one, OCR if the page is scanned
    │   │   ├── pptx_extractor.py     PowerPoint: shapes, tables, charts, SmartArt, groups, notes, pictures
    │   │   ├── docx_extractor.py     Word: body, tables, lists, text boxes, headers/footers,
    │   │   │                         footnotes, comments, pictures
    │   │   ├── common.py             Helpers all three extractors share
    │   │   ├── docx_image_extractor.py   (legacy, not used by main.py; see section 10)
    │   │   └── merge_docx_content.py     (legacy, not used by main.py; see section 10)
    │   │
    │   ├── ocr/
    │   │   └── image_ocr.py      OCR for pictures inside documents (each image read once)
    │   │
    │   ├── structure/            PDF only: cleans up after OCR
    │   │   ├── pdf_tables.py         Rebuilds tables on scanned pages (OCR only returns words)
    │   │   └── heading_classifier.py Labels OCR paragraphs as headings ("1. Purpose", "Appendix A")
    │   │
    │   └── pii/                  STEPS 2-5
    │       ├── detectors.py          What counts as PII: regexes, label rules, spaCy names
    │       ├── document_scanner.py   STEP 2: runs the detectors over a whole document
    │       ├── redactor.py           STEP 3: writes the tagged and sanitized copies
    │       ├── validator.py          STEP 4: checks the sanitized copy and gives the verdict
    │       └── exposure_report.py    STEP 5: what was found, where, and what was done (no values)
    │
    └── tests/                    Test scripts, single-step scripts and inspection scripts
                                  (see section 7: they are NOT all tests)
```

---

## 5. The five steps in detail

### Step 0: Check and upload (`main.py`, `src/uploads.py`)

**Goal:** accept any input file, and make the run independent of where the
file is and what it is called.

- **Check.** `input_problem()` in `main.py` refuses a file that is missing,
  empty, of an unsupported type, or not what its extension says (a PDF renamed
  to `.docx`, a password-protected or damaged Office file). Nothing has been
  copied or deleted yet.
- **Upload.** `store_upload()` copies the file into a new folder,
  `uploads/<job_id>/`, under its original name (the extractors take
  `document_id` and `file_name` from it), and writes `upload.json` beside it
  with the SHA-256 of the copy. All outputs go to `output/<job_id>/`.
- **Routing.** `EXTRACTORS` in `main.py` maps each supported extension to its
  extractor. Adding a file type means adding an extractor there and its
  content check in `input_problem()`.

### Step 1: Extract (`src/extractors/`, `src/ocr/`, `src/structure/`)

**Goal:** turn any input file into the **common JSON format** defined in
[`src/models/document.py`](backend/src/models/document.py). The docstring at
the top of that file is the format's reference.

The format is a tree:

```
Document                      document_id, file_name, source_type (PDF/PPTX/DOCX),
│                             extraction_method (native / ocr / mixed), metadata
└── Page                      a PDF page, a PPTX slide, or the whole DOCX (Word has no fixed pages)
    └── Block                 one piece of content, in reading order
        └── children          table → table_cell → word ; paragraph → word
```

A block looks like this (trimmed):

```json
{
  "block_id": "p1_b3",
  "block_type": "paragraph",
  "text": "Contact Jane Doe at jane.doe@example.com",
  "order": 3,
  "bounding_box": { "x": 72.0, "y": 140.5, "width": 300.2, "height": 12.1 },
  "metadata": { "source": "ocr", "location": "body", "ocr_confidence": 91.4 },
  "children": [ "... one 'word' block per word, each with its own bounding box ..." ]
}
```

- **`block_type`** is one of: `heading`, `paragraph`, `list_item`, `table`,
  `table_cell`, `image_ocr` (text read from a picture), `image` (a picture that
  could not be read), `chart`, `diagram`, `embedded_object`, `word`.
- **`metadata.source`** is `native` (real text in the file) or `ocr` (read from an image).
- **`metadata.location`** is `body`, `header`, `footer`, `text_box`, `notes`,
  `footnote`, `endnote` or `comment`. PII hides in all of these.
- **`bounding_box`** is the position in points on the page. It is `None` for DOCX,
  which has no fixed layout. Positions make it possible to black out the
  original file later.

How each format is handled:

| Format | File | How |
|---|---|---|
| **PDF** | `pdf_extractor.py` | A page with a text layer is read directly (PyMuPDF), including its tables. A **scanned** page (fewer than 50 characters of text) is rendered at 4× zoom, white-on-dark bars are inverted, and the image is OCR'd with Tesseract. Then `structure/pdf_tables.py` finds the tables in the page image (dark header band, thin row lines) and moves the OCR words into table cells. `structure/heading_classifier.py` then marks headings. |
| **PPTX** | `pptx_extractor.py` | Walks every shape on each slide, including shapes inside groups: titles, bullets, tables (merged cells too), chart titles and labels, SmartArt text, and speaker notes. Pictures are OCR'd. Blocks are sorted top-to-bottom, left-to-right. |
| **DOCX** | `docx_extractor.py` | Reads the body in order, plus headers/footers, footnotes/endnotes and comments (with their authors). Covers lists, nested and merged table cells, text boxes and content controls. Each picture is OCR'd **where it appears** in the document. |

File **metadata** (author, last modified by, ...) is also kept, because it often names people.

**Format check.** Before the scan, `main.py` checks the extracted JSON with
`format_problems()` in [`src/models/schema.py`](backend/src/models/schema.py):
the required keys, block types, `location` and `source` on every text block,
table and cell fields, OCR word lists, unique `block_id`s and so on. The scan,
redaction and validation all rely on this structure, so a document that breaks
it goes no further. The same check is used by `tests/test_extractor_format`.

### Step 2: Scan for PII (`src/pii/detectors.py`, `src/pii/document_scanner.py`)

**`detectors.py`** decides *what is PII* in a single piece of text. It has three
detectors, and their results are merged:

| Type | Example (fake) | How it's found |
|---|---|---|
| `EMAIL` | `jane.doe@example.com` | Regex, plus OCR-broken forms: a space or line break beside the `@`, or a local part ending at a dangling `@` whose domain OCR lost |
| `PHONE` | `+1 (212) 555-0100` | Regex, plus rules that reject look-alikes: dates, year ranges, version numbers, IP addresses. A bare run of digits only counts if a label like "Tel" or "Mobile" is next to it |
| `US_SSN` | `123-45-6789` | Regex plus the official SSN validity rules. An "SSN" label overrides the rules (test data often uses invalid numbers) |
| `INDIAN_PAN` | `ABCPE1234F` | Regex |
| `IP_ADDRESS` | `192.168.1.10` | Regex, checked with Python's `ipaddress` module |
| `EMPLOYEE_ID` | `EMP-12345` | Prefix regex, or any ID after "Employee ID:" or under an "Employee ID" column |
| `PASSPORT_NUMBER` | `X1234567` | Only next to a passport label or under a passport column |
| `PERSON_NAME` | `Jane Doe` | spaCy's name recognition (NER), filtered by name-shape rules (no job titles, no team or office words). Plus names close after a person label ("Owner: ...", "Led by ...", "Presented by ...", a "Name" column), and a surname alone where the document names that person in full elsewhere, with the first name or initials before it |
| `DATE_OF_BIRTH` | `14/05/1984` | A date after "Date of birth", "DOB" or "born", or under such a column. Other dates are not PII |
| `NATIONAL_ID` | `44051401359` | PESEL (check digit and birth date), Aadhaar (Verhoeff check digit; without a label only as `dddd dddd dddd`), or an ID after a national ID label ("National Insurance number") or under such a column |
| `PAYMENT_CARD` | `4111 1111 1111 1111` | 13-19 digits passing the Luhn check, with a card prefix or a card label; masked numbers (`**** 4417`, "card ending in 4417") after a card label |
| `BANK_ACCOUNT` | `GB82 WEST 1234 5698 7654 32` | IBAN with a valid checksum, or an account number after "Account no.", "A/C" or under such a column |
| `POSTAL_ADDRESS` | `12 Ashwood Lane, Leeds LS6 2AB` | A house number, street name and street word (or a Polish "ul. ..."), with town and postcode, even over several lines; or the text after an address label ("Home address:") holding a house number, postcode or street word |

Things to know about detection:

- **Labels matter.** `find_label()` looks a short distance before a value, in
  the same line or clause, for words like "Tel", "SSN" or "Passport No.". A
  label raises the confidence or turns a doubtful value into a finding.
- **A value found once is found everywhere.** The scanner (`document_scanner.py`)
  reports every other occurrence of a value it found, with the same type
  (`KNOWN_VALUE`): an ID found under an "EMP ID" header is redacted under a
  "VENDOR ID" header too. Only values of at least 6 characters with a digit
  or `@`, and names of two words or more, spread. A name found without the
  person's first names ("M. Okonjo" in "Marguerite M. Okonjo") is widened over
  them when the document names the person in full.
- **Confidence is a ranking, not a probability.** Each rule has a hand-picked
  number (0.75–0.99) used to choose between competing findings.
- **spaCy's small model makes many mistakes**, for example tagging "Vendor Tier"
  or "APPROVER" as people. So its results are cut into name-shaped pieces and
  checked: no job titles, no street names, no words the same document also
  uses in lowercase, at least 2 tokens, and so on.

**`document_scanner.py`** runs the detectors over a **whole document** and
adds the document-level logic:

- **Table context.** A table cell holding only `X1234567` means nothing on its
  own. The scanner works out each table's title row, header rows and row labels,
  and passes them as *context*. Under a "Passport Number" header, the same
  value becomes a finding.
- **Surname mentions.** If "Jane A. Doe" is found anywhere, a later "approved
  by Doe" is also reported, at lower confidence (`surname_mentions()`). The
  finding's `context_trigger` names the person it can stand for. Rules that
  keep this precise:
  - **Shared surnames.** If two different people share a surname, both are
    named: `"Anna Park; James Park"`. The same person written two ways ("T. Doe"
    and "Tom Doe") counts as one person, named in full.
  - **Ordinary words.** A surname that is also a word only counts where spaCy
    reads it as a proper noun: not "Read" in "Read the policy", and not "Green"
    as a RAG status.
  - **Other names.** A surname inside another capitalised name is skipped:
    "Ford Motor Company", "the Treasure Hunt".
  - **Initials and titles.** The person's own initials, first name or a title
    before the surname always make it count: "CA Doe", "L.-E. Doe", "Dr. Doe".
- **Speed.** Each unique text is scanned once, and spaCy runs in batches over
  the whole document.
- **Extraction warnings.** The report lists anything that could hide PII from
  the scan: scanned pages, low OCR confidence, unreadable images, embedded
  objects that were not opened.

### Step 3: Tag and redact (`src/pii/redactor.py`)

Uses the findings to write **two copies** of the document:

| Copy | Looks like | Purpose |
|---|---|---|
| `<name>_tagged.json` | `Contact [PERSON_NAME: Jane Doe]` | For humans to review what was found. **Still contains PII.** |
| `<name>_sanitized.json` | `Contact [REDACTED: PERSON_NAME]` | The only copy that may go to an LLM, after validation |

Rules worth knowing:

- Text is replaced at the **exact offsets** the scanner reported. If a finding's
  offsets don't match the text (for example, the document changed after the
  scan), the redactor does **not** guess. It records an error and the status
  becomes `needs_review`.
- **Overlapping findings merge** into one redaction. A name inside an email
  becomes a single `[REDACTED: EMAIL]`, so no part of either value is left.
- Text often appears **more than once** in the JSON: OCR word lists, a
  picture's word list, column headers copied into a continued table. The
  sanitized copy hides those copies too, while keeping the word positions.
- If a reviewer sets a finding's `review_status` to `"false_positive"` in the
  `_pii.json`, that value is left alone.
- Every finding gets an **audit** record (redacted / skipped / failed) in
  `metadata.redaction`.

### Step 4: Validate (`src/pii/validator.py`)

Does not trust step 3: it **checks** the sanitized copy with eight checks:

| Check | Question it answers |
|---|---|
| `provenance` | Do the report and the sanitized copy belong to this document, and was the copy made from this report? |
| `report` | Do the report's offsets really point at its values in the original? |
| `replacements` | Was every finding replaced, and is its value gone from its block? |
| `ocr` | Do OCR words over a redacted value hold only tags? Is any OCR text too unreliable to trust, or any image unread? |
| `extraction` | Did the extractor miss anything? Its declared limitations, embedded objects it never opened, and pages with little or no text all need a person to check the original |
| `rescan` | Does running the scanner again on the sanitized copy find **nothing**? |
| `known_values` | Is any PII value found anywhere in the document, or part of a name, still present **anywhere** in the copy? |
| `structure` | Are the pages, blocks, tables and reading order unchanged, with differences **only** where tags were inserted? |

Each check returns **PASS**, **NEEDS_REVIEW** (nothing known to remain, but
something could not be confirmed) or **FAIL** (PII is known to remain, or the
files don't match). The overall **verdict is the worst result** among the checks.

The validation report never contains the PII values themselves. It only refers
to `block_id`s and `finding_id`s, so it is safe to share. To see an actual value,
look up the `finding_id` in the `_pii.json`.

**The release gate.** `approved_copy(json_path)` in `validator.py` is **the only
way code should load a sanitized copy to send to an LLM**. It returns the
document only if its validation report says PASS **and** the file is byte for
byte the one that was validated: the report records its SHA-256 fingerprint.
Otherwise it raises `NotApproved` with the reason. So a FAIL, an unresolved
NEEDS_REVIEW, a copy edited after validation, or a copy never validated are all
blocked. `main.py` uses the same gate for its final `Approved`/`Blocked` line.

### Step 5: Report the exposure (`src/pii/exposure_report.py`)

Shows **what was found, where, how, and what was done with it**, without
repeating any PII value. This is the report to share with whoever owns the
documents. One row per finding:

| Document | Location | PII Type | Detector | Confidence | Action |
|---|---|---|---|---|---|
| risk_policy.pdf | Page 2, table row 4, column 2 | PERSON_NAME | NER | 0.90 | Redacted |
| risk_policy.pdf | Page 2, table row 4, column 2 | EMAIL | Regex | 0.99 | Redacted |
| risk_policy.pdf | Page 2, table row 4, column 2 | EMPLOYEE_ID | Rule | 0.95 | Redacted |

- **Location** is the page (PDF), slide (PPTX) or part (DOCX `Body`, `Header`,
  ...), plus the table cell, picture, speaker notes, comment author or file
  property the value was in.
- **Detector** is `NER` (spaCy), `Regex`, or `Rule` (labels, table headers,
  surname mentions). The exact `rule_id` is in its own column.
- **Action** is `Redacted`, or why not (reviewed as a false positive, redaction
  failed). The **Validation** column adds the worst validator issue naming the
  finding: e.g. `FAIL` when the same value is still elsewhere in the copy.
- **Traceability.** Each row keeps its `finding_id` (the value is in the
  `_pii.json` under it), `block_id`, page, table cell and character offsets, and
  the report records the scan, redaction and validation runs it was built from.
  It refuses to build from files of different runs.
- **Exposure summary.** Per document: findings by type, detector, action and
  page; the verdict and whether the copy may go to an LLM; every validator issue
  (the residual exposure); and the scan's coverage gaps (unread images, low OCR
  confidence, ...).
- **No values.** Fields that can hold a value are left out (`text`, `context`,
  `context_trigger`, `matched_pattern`). Before anything is written, every string
  in the report is searched for every value in the PII report, and a match
  stops the report.

After each document, `exposure_report.md` and `exposure_report.csv` are rebuilt
over **every** `<name>_exposure.json` in `backend/output/` and its subfolders,
so they always cover all processed documents.

---

## 6. The output files

Step 0 stores your file as `backend/uploads/<job_id>/<file>`, with
`upload.json` beside it (job ID, original name and path, size, SHA-256, upload
time). The copy **has PII**, like the original.

For an input called `<name>`, the run's folder `backend/output/<job_id>/` gets:

| File | Written by | Contains | Has PII? |
|---|---|---|---|
| `<name>.json` | Step 1 | The extracted document (common JSON) | **Yes** |
| `<name>_pii.json` | Step 2 | The PII report: `summary`, `tables`, `findings` | **Yes** (each finding's `text`) |
| `<name>_tagged.json` | Step 3 | The document with values labelled in place | **Yes** |
| `<name>_sanitized.json` | Step 3 | The document with values replaced by tags | Should not. Trust it only with a PASS verdict |
| `<name>_validation.json` | Step 4 | The verdict, each check's result, issues, and the fingerprint of the sanitized copy it approved | No (designed to be shareable) |
| `<name>_exposure.json` | Step 5 | One row per finding (location, type, detector, confidence, action), the exposure summary and the validator's issues | No (checked before it is written) |
| `exposure_report.md`, `exposure_report.csv` (in `backend/output/` itself) | Step 5 | The same rows and summaries for every processed document, combined | No |

A single finding in `<name>_pii.json`:

```json
{
  "finding_id": "F-0001",
  "type": "EMAIL",
  "text": "jane.doe@example.com",
  "start": 20, "end": 40,
  "confidence": 0.99,
  "detector": "regex", "rule_id": "EMAIL_REGEX", "detection_method": "regex",
  "context_trigger": null,
  "context": "",
  "page_number": 1, "block_id": "p1_b3", "block_type": "paragraph",
  "table_id": null, "row": null, "column": null,
  "bounding_box": { "...": "..." },
  "image": null,
  "review_status": "unreviewed",
  "action": "REDACT"
}
```

`start`/`end` are character offsets in that block's `text`. `rule_id` tells you
which rule fired. `context_trigger` is the label that made it PII (e.g. `"Tel"`),
if there was one. For text read from a picture, `image.word_boxes` gives the
pixel positions to black out in the image.

**Tip:** open `<name>.json`, `<name>_tagged.json` and `<name>_pii.json` side by
side in your editor and search for a `block_id`. It's the fastest way to see
how the pieces connect.

---

## 7. Tests and helper scripts

`backend/tests/` holds **four kinds of scripts**. None of them use pytest:
each is a plain script you run from `backend/`:

```powershell
cd backend
python -m tests.test_redactor
```

### 7.1 Self-checking tests (safe to run any time)

These build their own small documents in memory (or in a temp folder), print
`PASS`/`FAIL` per case, and exit with `1` if anything failed. They **do not touch
`output/`**.

| Script | Checks |
|---|---|
| `test_pii_accuracy` | Detectors find what they should and ignore look-alikes (real text from the samples) |
| `test_pii_names` | Person-name rules, labelled names, names like O'Brien and McDonald |
| `test_pii_surnames` | Surname-only mentions: full names, initials, shared surnames, ordinary words, overlapping findings |
| `test_pii_table_context` | Table title/header detection and context-based findings |
| `test_redactor` | Redaction rules (offsets, overlaps, word lists, false positives, ...) |
| `test_validator` | Each validation check and verdict, including tampered copies, extraction gaps, and the release gate |
| `test_exposure_report` | Exposure rows, locations, actions, the no-values check, refusal of mismatched runs, the combined CSV/Markdown |
| `test_extractor_format --samples` | All three extractors produce valid common JSON for generated sample files (drop `--samples` to also check `input/`) |
| `test_uploads` | Uploaded copies keep their name and bytes, same-named files get separate job folders, job IDs are safe folder names, stored copies are recognised when given again |

### 7.2 Scripts that REWRITE files in `output/`

These run one step for real and **overwrite** generated files. ⚠️ If a script
rewrites a `<name>_pii.json`, the existing `_sanitized.json` and
`_validation.json` no longer match it. Fix that by running
`python main.py output/<name>.json`.

| Script | Rewrites |
|---|---|
| `test_pdf_extractor` | `risk_policy.json` and `risk_policy_pii.json` (full OCR: slow) |
| `test_pptx_extractor` | `organizational_pack.json` and `organizational_pack_pii.json` |
| `test_pdf_tables` | Rebuilds tables in `risk_policy.json` without re-OCR, then rescans |
| `test_pii_document_scan` | Rescans all three `*_pii.json` files. **Currently crashes** with `KeyError: 'property_author'` after rewriting the first file (`tprm_training_pii.json`) |
| `test_common_format` | `test_document.json` (a tiny hand-made example) |
| `validate_all_extractors` | `extraction_validation_report.json` (structure statistics for the three extracted files) |
| `test_docx_image_extractor` | `tprm_images_ocr.json` (legacy, see section 10) |

### 7.3 Inspection / demo scripts (read-only, print to the console)

Use these to look inside a file while debugging: `validate_pdf`, `validate_pptx`,
`validate_docx`, `validate_headings`, `inspect_pdf_coordinates`,
`inspect_empty_pptx_cells`, `verify_docx_headers_footers`,
`check_docx_headers_footers`, `check_docx_images`, `test_pdf_ocr`,
`test_pii_detectors`, `test_pii_edge_cases`, `test_pii_overlap`.

`try_scanner.py` is a scratchpad: edit the table cells in it and run it to see
what the scanner finds.

### 7.4 Detector accuracy evaluation

```powershell
python -m tests.evaluate_detector
```

Unlike the other scripts, this one also runs as a file, from any folder:
`python tests/evaluate_detector.py`, or the editor's ▶ Run button.

Instead of counting findings, this measures **how accurate detection is**. It
compares the detector against hand-labelled examples in
`tests/data/pii_ground_truth.json` and prints **precision** (how many findings
were right), **recall** (how much of the PII was found) and **F1**. The results
are broken down by PII type, source format (PDF/PPTX/DOCX) and extraction
method (native/OCR). The metrics are saved to `output/detector_errors.json`
with two lists to work from: `false_negatives` (labelled PII that was missed:
improve recall) and `false_positives` (findings that are not PII, with the rule
that reported each: improve precision).

- **Two splits.** Examples marked `"split": "dev"` are for tuning rules. Never
  tune a rule to fix an `"eval"` example. The eval split only tells you whether
  a change also helps on text it wasn't fitted to.
- **Adding examples.** The field list and labelling rules are in the docstring
  at the top of `tests/evaluate_detector.py`. Use made-up values that keep the
  *shape* of real text (OCR errors, line breaks, table headers), not values
  copied from real documents. The script stops with a list of mistakes if a
  label isn't in its text, an id is used twice, or a type is unknown.
- **Scope.** This tests detection on text snippets. It can't tell you whether
  the extractors missed text before it ever reached the detector.

---

## 8. Common tasks

**Process a new document**

```powershell
python main.py C:\path\to\some_file.docx
python main.py                              # or pick it in a file chooser
```

The file is copied to `uploads/<job_id>/` and its outputs go to
`output/<job_id>/`; the job ID is printed at the start. Files with the same
name, or the same file given twice, get separate job folders.

**Clean up a run.** Delete its `uploads/<job_id>/` and `output/<job_id>/`
folders. Then rebuild the combined exposure report so it no longer lists them:
`python -c "from src.pii.exposure_report import write_combined; write_combined('output')"`.

**Send a document's text to an LLM**

```python
from src.pii.validator import NotApproved, approved_copy

try:
    document = approved_copy("output/<job_id>/risk_policy.json")   # the sanitized copy
except NotApproved as reason:
    ...   # do not send anything: the reason says why
```

Never open `<name>_sanitized.json` directly for this: only `approved_copy()`
checks that it passed validation and hasn't changed since.

**Review findings and mark false positives**

1. Open `output/<job_id>/<name>_pii.json` and set `"review_status": "false_positive"` on the wrong findings.
2. Re-run **only** redaction, validation and the exposure report, so your edits are kept:
   ```powershell
   python -m src.pii.redactor output/<job_id>/<name>.json
   python -m src.pii.validator output/<job_id>/<name>.json
   python -m src.pii.exposure_report output/<job_id>/<name>.json
   ```
   (Don't use `main.py` here: it rescans and replaces the `_pii.json`, losing your edits.)

**Add a new PII type**

1. In `src/pii/detectors.py`, add a regex and a loop in `regex_detector()` (or
   `custom_rule_detector()` for label-based rules) that calls `finding(...)`.
2. If the type uses labels, add them to `LABEL_PATTERNS` at the top of the file.
3. Add "must find" and "must not find" cases to `tests/test_pii_accuracy.py`,
   and labelled examples to `tests/data/pii_ground_truth.json` (both splits).
   Then run `python -m tests.evaluate_detector`.
4. Bump `SCANNER_VERSION` in `src/pii/document_scanner.py`, so reports made by old and new code can be told apart.

**Tune a threshold**

Tunable numbers are module-level constants at the top of each file, each with a
comment. For example: `ZOOM` and `NATIVE_TEXT_MIN_CHARS` in `pdf_extractor.py`,
`LOW_OCR_CONFIDENCE` in `document_scanner.py`, `MAX_NAME_TOKENS` / `TITLE_WORDS`
in `detectors.py`, and the table-detection constants in `structure/pdf_tables.py`.

---

## 9. Gotchas

- **Run from `backend/`.** Running from anywhere else fails with
  `ModuleNotFoundError: No module named 'src'` or "file not found".
- **Tesseract is a separate install** (see [Setup](#22-tesseract-ocr)).
- **Treat `uploads/` and `output/` as sensitive.** `uploads/` holds a copy of
  every file you processed. In `output/`, everything except `_sanitized.json`
  (with a PASS verdict), `_validation.json`, `_exposure.json` and
  `exposure_report.*` contains PII. `.gitignore` keeps `uploads/` and the
  per-run `output/<job_id>/` folders out of git; delete a run's two folders when
  you no longer need them (see [Common tasks](#8-common-tasks)). The PDF and PPTX
  samples say their data is synthetic. The DOCX does not, so treat its names as real.
- **The web interface keeps its scans in memory.** After `app.py` restarts, the
  page no longer lists earlier scans, but their `uploads/<job_id>/` and
  `output/<job_id>/` folders stay until you delete them (the page's **Delete
  this scan's files** button does it for the current scan). Scans run one at a
  time; a second upload waits for the first. The findings table, extracted
  and tagged text show PII values on purpose, for the person reviewing them:
  the server listens on this computer only.
- **Pictures are only redacted in the JSON.** The sanitized JSON hides OCR'd
  text, but the original image files still show it. `image.word_boxes` in the
  PII report gives the pixel positions, so the images can be blacked out later.
- **`table_cell` text is scanned with its headers as context**, but word-level
  `children` are not scanned separately, because they repeat their parent's
  text. If you add a new block type with children, check `collect_units()` in
  `document_scanner.py`.
- **Versions.** `SCHEMA_VERSION`/`SCANNER_VERSION` (scanner), `REDACTOR_VERSION`,
  `VALIDATOR_VERSION` and `EXPOSURE_VERSION` are written into every output. Bump the right one when
  you change rules.
- **Not a git repository** (yet). There is no history to fall back on, so copy
  `output/` before experiments if you want to compare.

---

## 10. Leftover and legacy files

Some files come from earlier versions of the project. `main.py` **does not use
them**, and you can ignore them when learning the codebase:

| File | What it was |
|---|---|
| `src/extractors/docx_image_extractor.py` | Old approach: OCR every DOCX image separately into `output/docx_images/` |
| `src/extractors/merge_docx_content.py` | Old approach: append that image text to the **end** of the DOCX JSON (it now goes in place, via `src/ocr/image_ocr.py`) |
| `tests/test_docx_extractor.py` | An early prototype DOCX extractor. It only defines functions, so running it does nothing |
| `tests/validate_docx_complete.py` | Inspects `tprm_training_complete.json` from the old merge approach |
| `output/docx_images/`, `output/tprm_training_images/` | Image files saved by the old approach |
| `output/tprm_images_ocr.json`, `output/tprm_training_images_ocr.json`, `output/tprm_training_complete.json` | JSON from the old approach |
| `output/test_document.json` | Example written by `tests/test_common_format.py` |
| `output/extraction_validation_report.json` | Written by `tests/validate_all_extractors.py` |

---

## 11. Suggested reading order

1. **`backend/main.py`** (≈440 lines). The docstring and `main()` show the whole flow.
2. **`backend/src/models/document.py`**. The docstring describes the data format everything uses.
3. **Run the pipeline once**, then open the output files side by side (see the tip in [section 6](#6-the-output-files)).
4. **`src/pii/detectors.py`**. What counts as PII and why.
5. **`src/pii/document_scanner.py`**, then **`redactor.py`**, then **`validator.py`**. Each starts with a long docstring explaining its job and output.
6. **The extractors** (`src/extractors/`, `src/structure/`), only when you need to change how text is read from a file.
7. **`tests/test_pii_accuracy.py`** and **`tests/test_redactor.py`**. The test cases are short real examples of each rule.

---

## Glossary

| Term | Meaning |
|---|---|
| **PII** | Personally identifiable information: anything that identifies a person (name, email, phone, ID number, ...) |
| **Common JSON** | The single format all three extractors produce (`src/models/document.py`) |
| **Block** | One piece of content in the common JSON (paragraph, heading, table cell, OCR'd image, word, ...) |
| **`block_id`** | The unique ID of a block. Findings and validation issues point to it |
| **Native vs OCR** | *Native* text is stored in the file as text. *OCR* text was read from an image and may have misread characters |
| **Finding** | One detected PII value, with its type, exact location and the rule that found it (`F-0001`, ...) |
| **Context** | Extra text given to the detectors only to find labels, e.g. a table cell's column header. It is never searched for PII itself |
| **Label / context trigger** | A word that tells you what a value is ("Tel", "SSN", "Employee ID") |
| **Tagged copy** | The document with PII labelled in place. For human review; still contains PII |
| **Sanitized copy** | The document with PII replaced by `[REDACTED: TYPE]`. The only thing that may go to an LLM |
| **Verdict** | The validator's overall result: PASS, NEEDS_REVIEW or FAIL |
| **Release gate** | `approved_copy()`: hands out a sanitized copy only if it passed validation and is unchanged since; otherwise raises `NotApproved` |
| **Provenance** | Proof that the report and the sanitized copy were made from this exact document and report |
| **spaCy / NER** | The local language model used to recognise person names (Named Entity Recognition) |
| **Tesseract** | The open-source OCR engine that reads text from images |
| **TPRM** | Third-Party Risk Management (the subject of the DOCX sample) |
| **RACI** | Responsible / Accountable / Consulted / Informed: a role table used in the PPTX sample |
