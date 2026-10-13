# Pipeline evaluation

Measures how accurate every step of the pipeline is, end to end, on documents whose PII is known in advance.

```powershell
.venv\Scripts\Activate.ps1                                   # from the repo root
python evaluation\run_evaluation.py                          # whole corpus (about 10 s)
python evaluation\run_evaluation.py --only "*.pdf"           # some files
python evaluation\run_evaluation.py --rebuild                # rebuild the synthetic documents first
python evaluation\run_evaluation.py --corpus path\to\folder  # another folder of documents
```

It runs from any folder. It needs the same setup as `main.py`: Tesseract on `PATH` and the spaCy model.

## What it does

Every PDF, DOCX and PPTX under `corpus/` goes through `main.py`'s `main()`, exactly as `python main.py <file>` runs it: input check, upload, extraction, format check, scan, redaction, validation, exposure report, and the `approved_copy()` release gate. Only three things differ, and none of them changes a result:

- uploads and outputs go in `results/<run>/runs/<file>/` instead of `backend/uploads` and `backend/output`;
- spaCy is loaded once for all documents;
- each step is timed.

The outputs are then compared with the document's labels, step by step:

| Step | What is measured |
|---|---|
| 0. Input check | Invalid files (empty, renamed, not what the extension says) are refused; valid files are accepted. |
| 1. Extraction | Each labelled PII value is in the extracted JSON: exactly, approximately (OCR damage, at least `FUZZY_MATCH` = 0.85 alike), or not at all. |
| 1b. Structure and context | From the labels' `structure` list: each element (heading, paragraph, list item, table cell, image text, header, footer, notes, comment) is *kept* if found as the right kind, in the right place, a cell in its row and column, a comment attached to its text. *Neighbours kept*: pairs of elements next to each other in the body's reading order are next to each other in the extraction too. *Context kept*: around each element holding PII, the sanitized copy differs from the extracted text only by the tags in place of the PII. Every document should keep at least `STRUCTURE_TARGET` = 0.80 of its elements and neighbours. |
| 2. Detection | Precision, recall and F1 of `_pii.json` against the labels. Broken down by PII type, by format (`PDF (ocr)`, `DOCX (mixed)`...), by place in the document (header, notes, image...), and by detector rule. |
| 3. Redaction | No labelled value is left anywhere in `_sanitized.json`. A name with one word still showing counts as *partly leaked*. Values labelled `not_pii` (dates, versions, team names) must still be there; a missing one counts as *over-redacted*. |
| 4. Validation | The verdict against the truth. **Unsafe release**: the gate released a copy that still holds PII. **Over-block**: no PII was left, but the copy was held back. |
| 5. Reports | No labelled value appears in `_validation.json`, `_exposure.json` or `exposure_report.*`, which are meant to be shareable. |
| Gate, exit code | `approved_copy()` releases exactly the PASS copies, and `main.py`'s exit code matches the verdict. |

It also scores the detector alone on the labelled text snippets of `archive/tests/data/pii_ground_truth.json`, using `archive/tests/evaluate_detector.py`'s own functions. This step is skipped when that file is missing. Tune rules on the *dev* split only; the *eval* split shows whether a change still holds on text it was not tuned on.

### How detection is matched

Each labelled value counts once per document, with one exception: when a value is found in one place but missed in another, the missed copy shows up as a redaction leak, not as a detection miss. A finding matches a label when it has the same type and one of these is true:

- it is the same value;
- it is at least 0.85 alike (OCR damage, or a title kept: "Dr. Elena Varga");
- it covers the whole value and more ("Remind Arun Pillai").

A finding that covers only part of the value (just "Arun") leaves the rest showing, so it does not match. Matches that are not exact are listed under *Found, not exactly*. A value found with the wrong type counts as both a miss and a false positive for detection, but redaction still removes it.

## Results

Each run writes `results/<YYYYmmdd-HHMMSS>/`:

| File | Contents |
|---|---|
| `summary.md` | Headline scores compared with the previous run over the same files, a per-document table, every breakdown, then each miss, false positive, leak and over-redaction. **It quotes the labelled values**, so share it only if every document is synthetic. |
| `metrics.json` | All the scores. The next run compares with these. |
| `details.json` | Each label's outcome at each step, per document. |
| `runs/<file>/` | The pipeline's own uploads, outputs and `pipeline.log`, the same files `main.py` writes. |

The exit code is 1 if anything unsafe happened: PII released, PII in a shareable report, an invalid file accepted, or a crash. Otherwise it is 0. Lower accuracy alone does not fail a run; compare the scores instead.

`results/` is gitignored because its outputs hold the PII of whatever was evaluated.

## The corpus

`build_corpus.py` writes `corpus/synthetic/`. Every value in it is invented: example.com addresses, 555-01xx and Ofcom drama-range phone numbers, documentation IP ranges, the voided SSN 078-05-1120, and made-up names.

| Document | Exercises |
|---|---|
| `hr_onboarding.docx` | Body text, a table whose header gives the context (`CFG-7731` under "Employee ID"), header, footer, comment author (and the text the comment is about), image text (OCR), file properties, a surname mention |
| `customer_record.docx` | Dates of birth, home addresses (in a sentence, and over three lines in a table cell), card numbers in full and masked, an IBAN and an account number, a PESEL and a National Insurance number, beside dates and numbers that are not PII |
| `vendor_review.pptx` | Title slide, bullets, text box, a table under "Passport No.", speaker notes, image text (OCR), properties |
| `org_chart.pptx` | Names after "Presented by" and "Led by", a first name that ends the line above its surname, the same people in full in a table |
| `access_policy.pdf` | Text layer: a phone number broken over two lines, a ruled table, image text (OCR), properties |
| `incident_scan.pdf` | A scanned page with no text layer: OCR, a title, a table with a dark header band rebuilt by `pdf_tables.py`, a page number |
| `two_column.pdf` | Text layer in two columns: the left column must be read before the right |
| `multipage_report.pdf` | Three pages with a running header and a page number on each, paragraphs continuing across pages |
| `clean_memo.docx` | No PII at all, only look-alikes: every finding is a false positive, and the verdict should be PASS |
| `invalid/*` | An empty PDF, text renamed to `.docx`, and a PDF renamed to `.pptx`: all must be refused |

The text and the labels are written by the same code (`Labels.pii()` and `Labels.element()` return the text they label), so they cannot drift apart. To add a case, add a builder function to `build_corpus.py` and to `BUILDERS`, then run with `--rebuild`.

### Your own documents

Put a document and a labels file next to each other, for example in `corpus/private/` (gitignored, for real documents):

```
corpus/private/contract.pdf
corpus/private/contract.pdf.labels.json
```

```json
{
  "file": "contract.pdf",
  "expected_outcome": "processed",
  "pii": [
    {"text": "Jane Doe", "type": "PERSON_NAME", "where": "signature block"},
    {"text": "+1 (212) 555-0147", "type": "PHONE", "where": "body"}
  ],
  "not_pii": [
    {"text": "Acme Corp", "why": "company"}
  ]
}
```

Label every PII value that appears in the document. An unlabelled one shows up as a false positive when it is found, and is never counted when it is missed. Follow the labelling rules in `archive/tests/evaluate_detector.py`:

- the value without its label;
- a name without its title;
- a name inside an email address is part of the EMAIL;
- a value broken over lines keeps its line break: `"+44 7700\n900218"`.

The types are EMAIL, PHONE, US_SSN, INDIAN_PAN, IP_ADDRESS, EMPLOYEE_ID, PASSPORT_NUMBER, PERSON_NAME, DATE_OF_BIRTH, NATIONAL_ID, PAYMENT_CARD, BANK_ACCOUNT and POSTAL_ADDRESS. `"where"` is free text that the *by place* breakdown groups on.

To score structure and context too, add a `structure` list: every element of the document in reading order, as `{"text", "kind", "where"}` plus `"page"`, `"table"`, `"row"`, `"column"` for table cells, and `"anchor"` (the text a comment is about). Kinds: heading, paragraph, list_item, table_cell, image_text. Places: body, header, footer, notes, comment, text_box.

A document with no labels file still goes through the pipeline. Its verdict and release decision are reported, but there is nothing to score it against.
