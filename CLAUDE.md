# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local PII detection and redaction pipeline. It takes a PDF, PPTX or DOCX file, extracts it into one common JSON format, finds PII, writes a redacted copy, validates that copy, and reports exposure. Only a validated sanitized copy may be sent to an LLM. Everything runs locally: regex rules, the spaCy `en_core_web_sm` model and Tesseract OCR. No document text leaves the machine.

`README.md` is the detailed reference: the format of each step, the output files and the glossary. Some of its paths are out of date (see "Repo state" below).

## Commands

Run everything from `backend/`, on Windows with PowerShell. The code imports `src.…` and uses relative paths such as `output/`. Run from anywhere else, it fails with `No module named 'src'`.

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

Tesseract must be installed separately and be on `PATH`. Without it, scanned PDF pages cannot be read at all. Without the spaCy model, the pipeline still runs but detects no person names, and it prints a warning.

### Tests

There is no pytest, linter or build step. Tests are plain scripts that print `PASS`/`FAIL` for each case and exit 1 if any case failed. They now live in `archive/tests/`, so from `backend/` put `backend` on the path:

```powershell
$env:PYTHONPATH = "."; python ..\archive\tests\test_redactor.py      # one test
python ..\archive\tests\evaluate_detector.py                         # precision/recall vs tests/data/pii_ground_truth.json
```

- **Self-checking and safe to run:** `test_pii_accuracy`, `test_pii_names`, `test_pii_surnames`, `test_pii_table_context`, `test_redactor`, `test_validator`, `test_exposure_report`, `test_uploads`, and `test_extractor_format --samples`. They build their own documents and do not touch `output/`.
- **Not safe to run blindly:** the other scripts (`test_pdf_extractor`, `test_pptx_extractor`, `test_pdf_tables`, `test_pii_document_scan`, `validate_*`, `inspect_*`, `check_*`) read the sample files or rewrite fixed files under `output/`.
- **Detector accuracy:** in `evaluate_detector`, tune rules only against `"split": "dev"` examples. The `eval` split exists only to measure.

### Evaluation

`python evaluation\run_evaluation.py` (from any folder) runs `main.main()` on every labelled document in `evaluation/corpus/` and scores each step: input check, extraction, detection P/R/F1, redaction leaks and over-redaction, verdict against the truth (unsafe releases), and PII in shareable reports. It also runs `evaluate_detector`. It patches only `UPLOAD_DIR`/`OUTPUT_DIR` (results go to the gitignored `evaluation/results/`), shares one spaCy model across documents, and times each step. A change to `main.py`'s step function names must be mirrored in `TIMED_STEPS`. The synthetic corpus comes from `evaluation/build_corpus.py`; see `evaluation/README.md` for the label format. Exit code 1 means something unsafe happened, not low accuracy.

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

### Orchestration rule

`main.py` (the command line) and `src/web/jobs.py` (the web UI, through `app.py`) only connect the steps. All extraction, detection, redaction and validation logic lives in `src/` and must be reused, never reimplemented in either entry point.

- Call `scan_file`/`scan_document`, not `detect_pii` directly. The scanner adds table header context, surname mentions and extraction warnings that the raw detectors lack.
- Build one `LocalNLPDetector` and pass it to both `scan_file` and `validate_file`, because the validator rescans the sanitized copy and loading the model is slow.
- `jobs.py` runs the same steps as `main.py`, so a change to the order or the failure handling in one must be made in the other.
- Known debt: `jobs.py` imports shared helpers from `main.py`: `input_problem`, `load_extracted`, `extract_document`/`EXTRACTORS`/`SUPPORTED_TYPES`, `pipeline_outputs`, `delete` and `format_summary`. They should move into `src/` (for example a `src/pipeline.py`, with `extract_document` in `src/extractors/`) so that `main.py` is only the CLI.
- Adding a file type means adding its extractor to `EXTRACTORS` and a content check in `input_problem()`.

### Invariants to preserve

- **Release gate.** Code that sends text to an LLM must load it with `src.pii.validator.approved_copy()`. It returns the copy only if the verdict is PASS and the file's SHA-256 matches the one recorded at validation. Otherwise it raises `NotApproved`. Never open `_sanitized.json` directly for this.
- **Failure cleanup.** If extraction or the scan fails, `main.py` still writes `_pii.json` with status `failed` and redacts nothing. If redaction or validation fails, their outputs are deleted, so an unchecked sanitized copy is never left on disk.
- **Exact offsets.** The redactor replaces text at the scanner's exact offsets and never guesses. If the offsets don't match the text, it records an error and the status becomes `needs_review`. Overlapping findings merge into one redaction. Duplicated text (OCR word lists, copied table headers) must be redacted too.
- **Shareable reports.** The validation and exposure reports must never contain PII values, only `block_id` and `finding_id`. Before writing, `exposure_report` searches its output for every value in the PII report and stops on a match.
- **False positives.** A finding with `review_status: "false_positive"` in `_pii.json` is left unredacted.
- **Version constants.** Outputs record `SCHEMA_VERSION`/`SCANNER_VERSION` (`document_scanner.py`), `REDACTOR_VERSION`, `VALIDATOR_VERSION` and `EXPOSURE_VERSION`. Bump the matching one when you change rules.
- **Word children.** Word-level `children` are not scanned separately, because they repeat their parent's text. If you add a block type with children, check `collect_units()` in `document_scanner.py`.
- **Tunable constants.** Thresholds are module-level constants with comments: `ZOOM` and `NATIVE_TEXT_MIN_CHARS` in `pdf_extractor.py`, `LOW_OCR_CONFIDENCE` in `document_scanner.py`, `MAX_NAME_TOKENS`/`TITLE_WORDS` in `detectors.py`.

### Adding a PII type

1. Add a regex and its loop in `regex_detector()`, or a label rule in `custom_rule_detector()`, in `src/pii/detectors.py`. Put any labels in `LABEL_PATTERNS`.
2. Add cases to `test_pii_accuracy.py` and examples in both splits of `tests/data/pii_ground_truth.json`.
3. Run `evaluate_detector` and bump `SCANNER_VERSION`.

## Sensitive data

`uploads/` and `output/<job_id>/` hold real PII and are gitignored. In `output/`, only `_validation.json`, `_exposure.json`, `exposure_report.*`, and `_sanitized.json` with a PASS verdict are free of PII. The DOCX sample is not marked as synthetic, so treat the names in it as real.

## Repo state

- Tests, the sample inputs, old outputs and the phase-1 analysis docs were moved into `archive/`: `archive/tests`, `archive/input`, `archive/samples`, `archive/output` and `archive/phase1`. This move is not committed yet. The README still points at `backend/tests/`, `backend/input/`, `samples/` and `docs/`.
- `src/extractors/docx_image_extractor.py` and `merge_docx_content.py` are legacy and nothing imports them. DOCX images are now OCR'd in place by `docx_extractor.py` through `src/ocr/image_ocr.py`.
