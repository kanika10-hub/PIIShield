# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local PII detection and redaction pipeline. It takes a PDF, PPTX or DOCX file, extracts it into one common JSON format, finds PII, writes a redacted copy, validates that copy, and reports exposure. Only a validated sanitized copy may be sent to an LLM. Everything runs locally: regex rules, the spaCy `en_core_web_sm` model and Tesseract OCR. No document text leaves the machine.

`README.md` is the detailed reference: the format of each step, the output files and the glossary. Some of its paths are out of date (see "Repo state" below).

## Working with the user

- Before changing code, explain what each change would improve and what it could break, then wait for approval. If a change could cause problems, say so and don't make it.
- Ask before running scripts that read the sample files or write under `output/`. The self-checking tests and `evaluation\run_evaluation.py` only use their own data.
- After a change to extraction, detection or redaction, run `evaluation\run_evaluation.py` and compare it with the previous run in `evaluation/results/`.

## Commands

Run everything from `backend/`, on Windows with PowerShell. The code imports `src.…` and uses relative paths such as `output/`. Run from anywhere else, it fails with `No module named 'src'`. The venv is `.venv` at the repo root, with Python 3.14.

```powershell
.venv\Scripts\Activate.ps1                     # from the repo root; pip install -r requirements.txt
cd backend

python main.py path\to\file.docx               # full pipeline; no argument opens a file chooser
python main.py output\<job_id>\<name>.json     # skip extraction (slow OCR), redo steps 2-5
python main.py uploads\<job_id>\<file>.pdf     # re-extract a stored upload, replacing that job's outputs
python app.py [port]                           # web UI at http://127.0.0.1:5000

# Single steps, keeping hand edits made to <name>_pii.json (main.py would rescan and overwrite them)
python -m src.pii.redactor        output\<job_id>\<name>.json
python -m src.pii.validator       output\<job_id>\<name>.json
python -m src.pii.exposure_report output\<job_id>\<name>.json
```

The exit code of `main.py` is 0 for PASS, 1 for FAIL or an error, and 2 for NEEDS_REVIEW.

Tesseract (5.4, at `C:\Program Files\Tesseract-OCR`) must be installed separately and be on `PATH`. Without it, scanned PDF pages cannot be read at all. Without the spaCy model, the pipeline still runs but detects no person names, and it prints a warning. OpenCV is not installed and not a dependency.

### Tests

There is no pytest, linter or build step. Tests are plain scripts that print `PASS`/`FAIL` for each case and exit 1 if any case failed. They live in `archive/tests/`, so from `backend/` put `backend` on the path:

```powershell
$env:PYTHONPATH = "."; python ..\archive\tests\test_redactor.py      # one test
python ..\archive\tests\evaluate_detector.py                         # precision/recall vs archive/tests/data/pii_ground_truth.json
```

- **Self-checking and safe to run:** `test_pii_accuracy`, `test_pii_names`, `test_pii_surnames`, `test_pii_table_context`, `test_redactor`, `test_validator`, `test_exposure_report`, `test_uploads`, `test_page_layout`, `test_docx_structure`, and `test_extractor_format --samples`. They build their own documents and do not touch `output/`.
- `evaluate_detector` (and `run_evaluation.py`, which calls it) rewrites the tracked `archive/output/detector_errors.json`, usually only its timestamp. Restore it with `git checkout` unless the change is wanted.
- **Not safe to run blindly:** the other scripts (`test_pdf_extractor`, `test_pptx_extractor`, `test_pdf_tables`, `test_pii_document_scan`, `validate_*`, `inspect_*`, `check_*`) read the sample files or rewrite fixed files under `output/`.
- **Detector accuracy:** in `evaluate_detector`, tune rules only against `"split": "dev"` examples. The `eval` split exists only to measure.

### Evaluation

`python evaluation\run_evaluation.py` (from any folder) runs `main.main()` on every labelled document in `evaluation/corpus/` and scores each step: input check, extraction, structure and context (from each labels file's `structure` list: elements kept as the right kind in the right place, neighbours kept in reading order, text around each redaction kept; every document should reach `STRUCTURE_TARGET` = 0.80), detection P/R/F1, redaction leaks and over-redaction, verdict against the truth (unsafe releases), and PII in shareable reports. It also runs `evaluate_detector`. It patches only `UPLOAD_DIR`/`OUTPUT_DIR` (results go to the gitignored `evaluation/results/<timestamp>/`, with `summary.md` and `metrics.json`), shares one spaCy model across documents, and times each step. A change to `main.py`'s step function names must be mirrored in `TIMED_STEPS`. The synthetic corpus comes from `evaluation/build_corpus.py`; see `evaluation/README.md` for the label format. Exit code 1 means something unsafe happened, not low accuracy.

## Architecture

### Pipeline

Each step reads the file the step before it wrote, so any step can be re-run alone and give the same result. A run's outputs go in `output/<job_id>/`.

| Step | Code | Writes |
|---|---|---|
| 0. Check and upload | `input_problem()` in `main.py`, `src/uploads.py` | `uploads/<job_id>/<file>` and `upload.json` |
| 1. Extract | `src/extractors/{pdf,pptx,docx}_extractor.py`; for PDF also `src/structure/pdf_tables.py` then `heading_classifier.py`; `src/ocr/image_ocr.py` for pictures | `<name>.json` (common JSON), checked by `src/models/schema.format_problems` |
| 2. Scan | `src/pii/document_scanner.scan_file`, which calls `src/pii/detectors.detect_pii` | `<name>_pii.json` |
| 3. Redact | `src/pii/redactor.redact_file` | `<name>_tagged.json` (still holds PII), `<name>_sanitized.json` |
| 4. Validate | `src/pii/validator.validate_file` | `<name>_validation.json` (PASS / NEEDS_REVIEW / FAIL) |
| 5. Report | `src/pii/exposure_report` | `<name>_exposure.json`, plus `output/exposure_report.{md,csv}` rebuilt over every run |

The common JSON format is a tree: Document → Page → Block, with children for table → table_cell → word. Its reference is the docstring of `src/models/document.py`. Every finding points at a `block_id` and at character offsets in that block's `text`, and everything downstream depends on that.

PDF pages with at least `NATIVE_TEXT_MIN_CHARS` of text-layer text are read natively, and their embedded images are OCR'd through `ImageReader`. Other pages are rendered at `ZOOM` and OCR'd whole by `ocr_page_blocks()`. The only image preprocessing is `invert_dark_bands()`, which turns white-on-dark bars dark-on-white. `src/structure/page_layout.py` (called inside `extract_pdf()`) labels running headers, footers and page numbers (`location` header/footer, moved to the start and end of their page) and reads two-column text-layer pages left column first; it never changes text, IDs or boxes. `heading_classifier.py` marks numbered headings and, on OCR pages, unnumbered one-line titles set larger than the usual text. DOCX comments and notes carry `anchor_block_id` (the block they belong to), and DOCX charts, SmartArt and embedded objects become `chart`, `diagram` and `embedded_object` blocks. Name detection uses only spaCy's PERSON entities (`doc.ents`) and part-of-speech tags (`token.pos_`).

### In progress: redaction in the original format

`src/original_format/` will write the redacted file in its original format: step 6 writes `<name>_redacted.<ext>` and `<name>_file_redaction.json`. Step 7 re-extracts that file, scans it again, writes `<name>_file_validation.json`, and gates release through `approved_file()`. Only `plan.py` and `images.py` exist so far. The package docstring lists the modules still to write (`docx_file`, `pptx_file`, `ooxml`, `pdf_file`, `redactor`, `inspect`, `validator`). Nothing in `main.py` or `jobs.py` calls it yet. The hooks already in place are:

- `extract_pdf(ocr_pages=...)`
- the extractors' "locate mode" with `src/extractors/text_sources.py`, which maps each character back to its XML element
- `scan_texts()` in `document_scanner.py`
- `findings_by_block()` in `redactor.py`

### Orchestration rule

`main.py` (the command line) and `src/web/jobs.py` (the web UI, through `app.py`) only connect the steps. All extraction, detection, redaction and validation logic lives in `src/` and must be reused, never reimplemented in either entry point.

- Call `scan_file`/`scan_document`, not `detect_pii` directly. The scanner adds table header context, surname mentions and extraction warnings that the raw detectors lack.
- Build one `LocalNLPDetector` and pass it to both `scan_file` and `validate_file`, because the validator rescans the sanitized copy and loading the model is slow. The web UI loads it once, in `Jobs.detector()`, and runs one job at a time.
- `jobs.py` runs the same steps as `main.py`, so a change to the order or the failure handling in one must be made in the other.
- Known debt: `jobs.py` imports shared helpers from `main.py`: `input_problem`, `load_extracted`, `extract_document`/`EXTRACTORS`/`SUPPORTED_TYPES`, `pipeline_outputs`, `delete` and `format_summary`. They should move into `src/` so that `main.py` is only the CLI. `src/extractors/dispatch.py` already holds copies of `extract_document`, `EXTRACTORS`, `SUPPORTED_TYPES` and `extract_pdf_document` (that copy also takes `ocr_pages`), but nothing imports it yet. Until `main.py` and `jobs.py` switch to it, make every change to extraction dispatch in both files.
- Adding a file type means adding its extractor to `EXTRACTORS` (in both places, for now) and a content check in `input_problem()`.

### Invariants to preserve

- **Release gate.** Code that sends text to an LLM must load it with `src.pii.validator.approved_copy()`. It returns the copy only if the verdict is PASS and the file's SHA-256 matches the one recorded at validation. Otherwise it raises `NotApproved`. Never open `_sanitized.json` directly for this.
- **Failure cleanup.** If extraction or the scan fails, `main.py` still writes `_pii.json` with status `failed` and redacts nothing. If redaction or validation fails, their outputs are deleted, so an unchecked sanitized copy is never left on disk.
- **Exact offsets.** The redactor replaces text at the scanner's exact offsets and never guesses. If the offsets don't match the text, it records an error and the status becomes `needs_review`. Overlapping findings merge into one redaction. Duplicated text (OCR word lists, copied table headers) must be redacted too.
- **OCR word geometry.** OCR word boxes must stay in the coordinates of the original image. `to_points()` divides them by `ZOOM`. `pdf_tables.py` renders the page again, unrotated, and assigns words to table cells by position. `original_format/images.py` fills word boxes to redact image pixels. Preprocessing that rotates, crops or resizes an image before OCR must map the boxes back. Pixel-only changes such as `invert_dark_bands()` are safe.
- **Shareable reports.** The validation and exposure reports must never contain PII values, only `block_id` and `finding_id`. Before writing, `exposure_report` searches its output for every value in the PII report and stops on a match.
- **False positives.** A finding with `review_status: "false_positive"` in `_pii.json` is left unredacted.
- **Version constants.** Outputs record `SCHEMA_VERSION`/`SCANNER_VERSION` (`document_scanner.py`), `REDACTOR_VERSION`, `VALIDATOR_VERSION` and `EXPOSURE_VERSION`. Bump the matching one when you change rules.
- **Word children.** Word-level `children` are not scanned separately, because they repeat their parent's text. If you add a block type with children, check `collect_units()` in `document_scanner.py`.
- **Tunable constants.** Thresholds are module-level constants with comments: `ZOOM` and `NATIVE_TEXT_MIN_CHARS` in `pdf_extractor.py`; `LOW_OCR_CONFIDENCE`, `LOW_OCR_WORD_CONFIDENCE` and `VALUE_LIKE_WORD` in `document_scanner.py`; `MAX_NAME_TOKENS`, `TITLE_WORDS`, `TEAM_WORDS` and `MAX_LABEL_GAP_WORDS` in `detectors.py`; `HEADER_BAND`/`FOOTER_BAND`, `MIN_GUTTER` and `CENTRE_ZONE` in `page_layout.py`; `LARGE_LINE_RATIO` in `heading_classifier.py`.
- **Shared word lists.** `word_lists()` lives in `document_scanner.py` (the redactor imports it from there, since the redactor already imports the scanner): the scanner, redactor and validator must read OCR words the same way.

### Adding a PII type

1. Add a regex and its loop in `regex_detector()`, or a label rule in `custom_rule_detector()`, in `src/pii/detectors.py`. Put any labels in `LABEL_PATTERNS`, and a column rule in `CELL_VALUES` if a table header can name it.
2. Add cases to `test_pii_accuracy.py` and examples in both splits of `archive/tests/data/pii_ground_truth.json`. Add the type to `PII_TYPES` in `evaluate_detector.py` and to `TYPE_LABELS` in `backend/static/app.js`.
3. Run `evaluate_detector` and bump `SCANNER_VERSION`. Add a labelled case to `evaluation/build_corpus.py` so the whole pipeline is scored on it.

## Known issues (evaluation run 2026-10-13, `evaluation/results/20261013-231322`)

On the synthetic corpus (9 documents, 98 labelled values, 152 structure elements) every headline score is 1.000: detection, redaction, release gate, structure and reading order. No unsafe release, no over-block. Snippet benchmark F1: dev 0.956, eval 0.959. Scanner 1.3.1, validator 1.3.0.

The validator rescans with the same detector, so it cannot catch a value the detector misses. A PASS verdict means "no PII the detector can see", not "no PII". The corpus is small and synthetic: these scores do not show coverage on the real samples, which have not been rerun since these changes.

- **A name with one garbled character inside is missed**: "Tomasz Zieli·ski" (a PDF font without "ń", or OCR damage) is not name-shaped, so neither spaCy's span nor the label rule survives `plausible_name()`. Accented names themselves are found (`UPPER`/`LOWER` in `detectors.py` cover the Latin scripts). The corpus builder embeds a TrueType font in `multipage_report.pdf` for this reason: PyMuPDF's built-in Helvetica writes "ń" as "·".
- **Minor over-redaction.** The spaCy span "Remind Arun Pillai" includes the leading verb, so one word of context is redacted (context around redactions 0.985). Nothing leaks.
- **Phone rule false positive:** a hyphenated reference in 4-4-4 groups ("contract 7731-2209-8812", eval split) is reported as PHONE. Not tuned, since it showed up in the eval split.
- **Unlabelled PESEL rule:** any 11-digit number with a valid check digit and date (about 3% of random ones) is reported as NATIONAL_ID at 0.80. Unlabelled street addresses include office addresses. Both over-redact; neither leaks.
- **Not measured on real files:** the low-confidence OCR word check (`LOW_OCR_WORD_CONFIDENCE`) may hold back noisy scans more often (the 11-Oct report found 18% of the DOCX sample's screenshot words below 50); DOCX embedded objects now lead to NEEDS_REVIEW; the new heading and two-column rules may relabel or reorder blocks in the samples.
- Team mailboxes (DEV-039) are redacted as EMAIL though the benchmark labels them not PII: a policy choice, left as it is.

**Real samples** (`archive/samples/`, run 2026-10-13 with scanner 1.4.0 and validator 1.3.0; jobs `20261013-232035_*` and `20261013-232039_*` in `backend/output/`):

- `organizational_pack.pptx`: PASS (was NEEDS_REVIEW: five first names left beside "Led by [REDACTED]"). All six "Led by" lines hold only a tag.
- `tprm_training.docx`: NEEDS_REVIEW, as before. 4 OCR blocks below confidence 60, and 13 blocks with low-confidence value-like words (new check). Its name-part issues went from 3 to 0.
- `risk_policy.pdf`: FAIL, as before. Now also finds 4 home addresses, 3 national IDs, 1 date of birth, 1 card number.
  - `known_values` FAIL: an EMPLOYEE_ID found under an "EMP ID" column on p28 is still in `p25_t3_row_3_cell_2`, under a "VENDOR ID" column that names no type. The scanner reports a value only where its own context says what it is.
  - 7 name-part NEEDS_REVIEW (was 10): a first name beside a name redacted by another rule (`p27_t3_row_2_cell_3`, `p23_t2_row_2_cell_4`); surnames inside emails that OCR broke with a space before "@" (`p12_b6`); and three single words whose use as a name is unclear.

### Proposed, not done (waiting for the user's approval)

- Propagate known values in the scanner: a value found as PII in one place (at least 6 characters with a digit or "@") is reported wherever else it stands, as `surname_mentions()` does for surnames. Fixes the `risk_policy.pdf` FAIL above. Risk: an over-short or common value spreads; hence the length rule.
- Accept OCR spaces around "@" in emails ("j.smith @cadence...") and look into the two first names left beside a name another rule redacted (`risk_policy.pdf`).
- Trim a leading verb from spaCy PERSON spans using the part-of-speech tags already computed ("Remind Arun Pillai").
- OCR the scanned pages of a PDF in parallel. Tesseract runs as a subprocess, so threads work; set `OMP_THREAD_LIMIT=1`. Output would be identical.
- Load spaCy without `parser` and `lemmatizer`, which nothing uses. Keep this only if detection results are identical, because NER may respect the sentence breaks the parser sets.
- Enlarge small embedded images before OCR, scaling the word boxes back. No measured need yet.
- **Rejected:** the deskew, median-blur and Otsu preprocessing snippet. Measured on `incident_scan.pdf` (in a scratch folder, OpenCV 5.0): its deskew never rotates (it fits the white background: 98% of pixels), and on the page tilted 2 degrees it lost 2 of 10 PII values that plain OCR read (10/10). It also breaks OCR word geometry if the deskew worked, and Tesseract already binarizes internally.

## Sensitive data

`uploads/` and `output/<job_id>/` hold real PII and are gitignored. In `output/`, only `_validation.json`, `_exposure.json`, `exposure_report.*`, and `_sanitized.json` with a PASS verdict are meant to be free of PII (see "Known issues" for misses). The DOCX sample is not marked as synthetic, so treat the names in it as real. The corpus in `evaluation/corpus/synthetic/` is synthetic and safe to read.

`archive/output/` is committed and holds `_pii.json` and `_tagged.json` files from the samples, which contain PII values. Don't print their contents, and don't add more such files to git.

## Repo state

- Tests, the sample inputs, old outputs and the phase-1 analysis docs are in `archive/`: `archive/tests`, `archive/input`, `archive/samples`, `archive/output` and `archive/phase1`. The move was committed in `91bec0d`. The README still points at `backend/tests/`, `backend/input/`, `samples/` and `docs/phase1/`.
- `backend/output/exposure_report.{md,csv}` are tracked and rebuilt by every run, so they usually show as modified.
- `src/extractors/docx_image_extractor.py` and `merge_docx_content.py` are legacy and nothing imports them. DOCX images are now OCR'd in place by `docx_extractor.py` through `src/ocr/image_ocr.py`.
- `presentation/` holds the case-study deck (`Case_Study_02_PII_Detection_and_Redaction.pptx`).
