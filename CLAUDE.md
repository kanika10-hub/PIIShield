# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local PII detection and redaction pipeline. It takes a PDF, PPTX or DOCX file, extracts it into one common JSON format, finds PII, writes a redacted copy of the text (the sanitized JSON), validates it, reports exposure, then writes the redacted file in the upload's own format (DOCX in, DOCX out; PPTX, PPTX; PDF, PDF) and checks that too. Only a validated sanitized copy may be sent to an LLM, and only a checked redacted file may be downloaded. Everything runs locally: regex rules, the spaCy `en_core_web_sm` model and Tesseract OCR. No document text leaves the machine.

`README.md` is a short overview (what, why, how, components, setup, pictures from the synthetic corpus in `docs/images/`). `docs/DETAILED_GUIDE.md` is the earlier detailed reference: the format of each step, the output files and the glossary. Some of its paths are out of date, and it does not describe steps 6–7 (see "Repo state" below).

## Working with the user

- Before changing code, explain what each change would improve and what it could break, then wait for approval. If a change could cause problems, say so and don't make it.
- Ask before running scripts that read the sample files or write under `output/`. The self-checking tests and `evaluation\run_evaluation.py` only use their own data.
- After a change to extraction, detection or redaction, run `evaluation\run_evaluation.py` and compare it with the previous run in `evaluation/results/`.

## Commands

Run everything from `backend/`, on Windows with PowerShell. The code imports `src.…` and uses relative paths such as `output/`. Run from anywhere else, it fails with `No module named 'src'`. The venv is `.venv` at the repo root, with Python 3.14.

```powershell
.venv\Scripts\Activate.ps1                     # from the repo root; pip install -r requirements.txt
cd backend

python main.py path\to\file.docx               # full pipeline (steps 0-7); no argument opens a file chooser
python main.py output\<job_id>\<name>.json     # skip extraction (slow OCR), redo steps 2-7
python main.py uploads\<job_id>\<file>.pdf     # re-extract a stored upload, replacing that job's outputs
python app.py [port]                           # web UI at http://127.0.0.1:5000

# Single steps, keeping hand edits made to <name>_pii.json (main.py would rescan and overwrite them)
python -m src.pii.redactor        output\<job_id>\<name>.json
python -m src.pii.validator       output\<job_id>\<name>.json
python -m src.pii.exposure_report output\<job_id>\<name>.json
python -m src.original_format.redactor  output\<job_id>\<name>.json [uploads\<job_id>\<file>]   # step 6, one pass
python -m src.original_format.validator output\<job_id>\<name>.json                            # step 7, one pass
```

`app.py` loads the code once, when it starts: restart it after a code change, or the page keeps running the old code. Jobs live in memory, so after a restart the page lists none (their folders stay). Don't start it yourself; the user runs it. Test the web flow with `test_original_api`/`test_review_api` (Flask test client) instead.

The exit code of `main.py` is 0 for PASS, 1 for FAIL or an error, and 2 for NEEDS_REVIEW: the worse of the sanitized copy's release and the redacted file's (a redacted file that could not be written or checked counts as FAIL; a PASS file waiting for its copy's review takes the copy's outcome).

Tesseract (5.4, at `C:\Program Files\Tesseract-OCR`) must be installed separately and be on `PATH`. Without it, scanned PDF pages cannot be read at all. Without the spaCy model, the pipeline still runs but detects no person names, and it prints a warning. OpenCV is not installed and not a dependency.

### Tests

There is no pytest, linter or build step. Tests are plain scripts that print `PASS`/`FAIL` for each case and exit 1 if any case failed. They live in `archive/tests/`, so from `backend/` put `backend` on the path:

```powershell
$env:PYTHONPATH = "."; python ..\archive\tests\test_redactor.py      # one test
python ..\archive\tests\evaluate_detector.py                         # precision/recall vs archive/tests/data/pii_ground_truth.json
```

- **Self-checking and safe to run:** `test_pii_accuracy`, `test_pii_names`, `test_pii_surnames`, `test_pii_table_context`, `test_redactor`, `test_validator`, `test_exposure_report`, `test_review`, `test_review_api` (patches the job folders to a temp folder), `test_uploads`, `test_page_layout`, `test_docx_structure`, `test_original_docx`, `test_original_pptx`, `test_original_pdf`, `test_original_api` (Flask test client, job folders in a temp folder), and `test_extractor_format --samples`. They build their own documents and do not touch `output/`.
- `evaluate_detector` (and `run_evaluation.py`, which calls it) rewrites the tracked `archive/output/detector_errors.json`, usually only its timestamp. Restore it with `git checkout` unless the change is wanted.
- **Not safe to run blindly:** the other scripts (`test_pdf_extractor`, `test_pptx_extractor`, `test_pdf_tables`, `test_pii_document_scan`, `validate_*`, `inspect_*`, `check_*`) read the sample files or rewrite fixed files under `output/`.
- **Detector accuracy:** in `evaluate_detector`, tune rules only against `"split": "dev"` examples. The `eval` split exists only to measure.

### Evaluation

`python evaluation\run_evaluation.py` (from any folder) runs `main.main()` on every labelled document in `evaluation/corpus/` and scores each step: input check, extraction, structure and context (from each labels file's `structure` list: elements kept as the right kind in the right place, neighbours kept in reading order, text around each redaction kept; every document should reach `STRUCTURE_TARGET` = 0.80), detection P/R/F1, redaction leaks and over-redaction, verdict against the truth (unsafe releases), and PII in shareable reports. It also runs `evaluate_detector`. Steps 6–7 run for every document: the step 4 table has `File` (the redacted file's verdict) and `File released` columns, and the expected exit code counts the file. It patches only `UPLOAD_DIR`/`OUTPUT_DIR` (results go to the gitignored `evaluation/results/<timestamp>/`, with `summary.md` and `metrics.json`), shares one spaCy model across documents, and times each step. A change to `main.py`'s step function names must be mirrored in `TIMED_STEPS`. The synthetic corpus comes from `evaluation/build_corpus.py`; see `evaluation/README.md` for the label format. Exit code 1 means something unsafe happened, not low accuracy.

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
| 6. Redact the file | `src/original_format/redactor.redact_original`, from the stored upload | `<name>_redacted.<pdf\|pptx\|docx>`, `<name>_file_redaction.json` |
| 7. Check the file | `src/original_format/validator.validate_original` | `<name>_file_validation.json` (PASS / NEEDS_REVIEW / FAIL; a FAIL file is deleted) |
| Review (web UI only) | `src/pii/review.py`, through `Jobs.hide`/`sign_off`/`withdraw` in `jobs.py` | manual findings (`M-0001`…) added to `<name>_pii.json`, then steps 3–7 again; or `<name>_review.json`, then step 5 again |

Review happens in the Review section of the Safety check tab, which lists every NEEDS_REVIEW issue; its "Check needed" badge is clickable (the list of all checks, shown only with technical details, lists issues read-only). "Hide it" adds the issue's text as manual findings, deletes `_review.json`, redacts and validates again, gives the new validation the sign-offs of issues that came back unchanged (`review.kept_review()`), then audits. It works on known_values name parts (the leftover words, chosen by `validator.searched_values()`, the same rule the check uses, as `PERSON_NAME`) and on the two OCR issues that `validator.unreadable_issue()` recognises by their reason text (a low-confidence block: all of its text not already in a finding; or low-confidence value-like words: each such word wherever it stands in its block, as `UNREADABLE_TEXT`). A low-confidence block whose sanitized text is only tags raises no issue. `UNREADABLE_TEXT` findings are misreadings, not values, so neither `check_known_values` nor the exposure report's value check searches for them. The page answers these OCR issues only all together: the Review section above the checks shows them as one line ("N places where OCR text was read with low confidence", each place listed read-only under a fold; the OCR check only points there) with "Hide all" (`review.hidden_all_report()`, `POST …/review/hide-all`: every listed OCR issue not signed off, in one run) and "Checked, all fine" (`review.sign_off_all()`, `POST …/review/sign-off-all`; `DELETE` undoes it). The per-issue API still works for them. The Review section (`views.review_view()`) also lists the blocks hidden (block IDs, types, who and when, never the text) and counts hidden, checked and open issues. "Checked, it's fine" signs one NEEDS_REVIEW issue off (by `validator.issue_key()`) with the reviewer's typed name and the time. Once every issue is signed off, `review_approval()` reports `final_verdict` PASS (and each answered check as PASS), which the job result and the exposure report show as "PASS (after review)" (the page: "Safe (after review)"). The `verdict` in `_validation.json` stays NEEDS_REVIEW, because `approved_copy()` reads it to know that sign-offs must be checked. Manual findings are lost when a document is scanned again from scratch, and `main.py` deletes `_review.json` at the start of a run.

The common JSON format is a tree: Document → Page → Block, with children for table → table_cell → word. Its reference is the docstring of `src/models/document.py`. Every finding points at a `block_id` and at character offsets in that block's `text`, and everything downstream depends on that.

PDF pages with at least `NATIVE_TEXT_MIN_CHARS` of text-layer text are read natively, and their embedded images are OCR'd through `ImageReader`. Other pages are rendered at `ZOOM` and OCR'd whole by `ocr_page_blocks()`. The only image preprocessing is `invert_dark_bands()`, which turns white-on-dark bars dark-on-white. `src/structure/page_layout.py` (called inside `extract_pdf()`) labels running headers, footers and page numbers (`location` header/footer, moved to the start and end of their page) and reads two-column text-layer pages left column first; it never changes text, IDs or boxes. `heading_classifier.py` marks numbered headings and, on OCR pages, unnumbered one-line titles set larger than the usual text. DOCX comments and notes carry `anchor_block_id` (the block they belong to), and DOCX charts, SmartArt and embedded objects become `chart`, `diagram` and `embedded_object` blocks. The PPTX extractor also reads, on each slide, the images it shows that no picture block reads (`template_image_blocks()`: slide background and picture fills, and the layout's and master's pictures, fills and backgrounds, location `template`; each image once, on the first slide that shows it, through a `seen` set the caller passes to every `slide_blocks()` call), and its comments (`comment_blocks()`: classic `p:cm` and threaded PowerPoint 365 `p188:cm`/`p188:reply`, location `comment`, author in metadata as for DOCX). SVG versions of pictures (`asvg:svgBlip`) are not read; the original-format writers remove them. Name detection uses only spaCy's PERSON entities (`doc.ents`) and part-of-speech tags (`token.pos_`).

### Redaction in the original format

`src/original_format/` writes the redacted file in the upload's own format. All three formats are done: `docx_file.py`, `pptx_file.py` and `pdf_file.py`, with what Word and PowerPoint share in `ooxml.py`, image pixels in `images.py`, and what to redact in `plan.py` (the same findings, merged spans and tags as the sanitized JSON). Tests: `archive/tests/test_original_{docx,pptx,pdf,api}.py` (self-checking; need spaCy and Tesseract).

- **Step 6**, `redactor.redact_original`: writes `<name>_redacted.<ext>` and `<name>_file_redaction.json` from the stored upload. It refuses a file that isn't the document's.
- **Step 7**, `validator.validate_original`: re-extracts the file (a PDF's scanned pages are OCR'd again, through `ocr_pages`), rescans it, and searches everything it holds for known values (`inspect.file_contents`: every XML part, attribute and embedded package of a DOCX/PPTX; every place a PDF keeps text). It writes `<name>_file_validation.json` and deletes the file on FAIL.
- **Release**: only `approved_file()` (see "Invariants").
- **Wiring**: `main.py` runs steps 6–7 after step 5. They are skipped, with a message, for an extracted JSON whose stored upload is gone. `jobs.py` runs them as the steps `file_redact` and `file_check` after `audit`, and again after "Hide it". They don't rerun after a sign-off, because `approved_file()` reads the review. It puts `file_verdict`/`file_approved`/`file_name`/`file_reason` in the job result (`file_approval()`). If they fail, their files are deleted (`main.file_outputs()`) and the sanitized copy and its reports stand.
- **Download**: only through `GET /api/jobs/<id>/download/redacted`, which calls `approved_file()` (403 with the reason otherwise). On the Safety check tab, the "Cleaned PDF/Word file/PowerPoint file" button shows under Downloads only when the file is approved (and in the "What to do next" box), and the note under the verdict says why it isn't.

**The page** (`backend/static/`) is written for non-technical users. A "What to do next" box under the upload panel gives the outcome in one sentence with one action (`nextStep()` in `app.js`: it says "ready" only when `file_approved` is true). It starts in a simple view: "Show technical details" (saved in the browser, applied by `theme.js` before drawing) shows what `.tech-only` hides: block and finding IDs, the server's step details, the Marked text tab, all checks, extraction warnings, redaction counts, the JSON downloads and the "Use saved results" upload. Issue reasons are shown in plain words through `PLAIN_ISSUES` in `app.js`, matched on the validator's reason text; the validator's own wording stays as it is, because `issue_key()` hashes it, so rewording it would void sign-offs.

**Writers.**

- **Word**: replaces text at the scanner's offsets in the `w:t` elements (the tag takes the formatting of the run the value starts in). It makes the same edits in Word's `mc:Fallback` copy of a text box and sets comment authors (clearing initials) and core properties. It replaces chart and SmartArt values in every part that holds them. Then it sweeps every part for the values `plan.sweep_values()` lists: tracked deletions, field codes, hyperlink targets (URL-encoded), alt text, document variables and embedded packages; attributes that are only digits are skipped. It removes the thumbnail.
- **PowerPoint**: does the same through the PPTX extractor's locate mode: slides, tables, groups, notes, pictures, template images, charts and their workbooks, SmartArt and comments. It has no text-box copies. It sets comment authors' names (clearing initials) and clears every threaded comment author's `userId` (an account, never scanned). Its sweep also covers slide masters and layouts. python-pptx caches link targets, so `ooxml.sweep_relationships` clears that cache after changing one.
- **Both Office writers**:
  - They fill OCR word boxes in pictures solid black, with nothing written in them (`images.FILL`).
  - They remove the SVG copy Office keeps of a picture (`ooxml.drop_svg_copies`: the `asvg:svgBlip` and, if unused, its file). Office shows that copy instead of the raster image whose pixels were read and redacted.
  - Body text gets the `[REDACTED: TYPE]` tag.
- **PDF**: works from the word boxes kept at extraction; it does no OCR of its own.
  - It first checks the file is the document's: each text-layer page by its words, each scanned page by its size and native character count.
  - Per page, it blanks the image pixels under every value's box (`PDF_REDACT_IMAGE_PIXELS`). That covers scanned pages, pictures (through their placement matrix) and the scan behind a searchable PDF.
  - It removes the value's characters from the text layer, with the box kept to the middle of its line (`TEXT_MARGIN`): in 11 pt type 13 pt apart, a full word box also deleted letters of the lines above and below. For a value glued to a label ("Email:jane@…"), it uses the characters' own boxes.
  - It fills each box solid black with no text (`pdf_file.FILL`; the user chose this on 2026-10-18). OCR reads nothing back from a black box, which also ended the false alarms from re-read tags.
  - OCR boxes of rotated pages go through `derotation_matrix`.
  - It sets the document information as the scan redacted it (with tags) and sweeps the rest.
  - It removes XMP, attachments, and every annotation except links and form fields. A link over a redacted value goes with the redaction.
  - It sweeps link targets, form fields and bookmarks, and saves with `garbage=4`.

**What step 7 allows for.**

- **PDF text**: a PDF read again doesn't keep its blocks, because removing a value can regroup a page's words. So each text-layer page is compared whole, word by word, with tags left out (`check_pdf_text`). Scanned pages are checked by rescan and known values only.
- **OCR noise** (`misreadings()`, through the optional `misreading` argument of `src.pii.validator.check_rescan`; the sanitized copy's validation passes none): a finding in a re-OCR'd image or scanned page is noted, not failed, only if each of its words is one of these:
  - **ours**: at least `INSIDE_SHARE` of its box is inside a filled area, i.e. the black box;
  - **misread**: below `LOW_OCR_WORD_CONFIDENCE`, outside the filled areas, over words the first reading read with confidence and no finding covered;
  - **unchanged**: the same confident, non-finding word as the first reading, case aside.

  At least one word must be ours or misread. Filling part of a line changes how Tesseract reads the rest of it: `tprm_training.docx`, `image_26`, was read as a "name" at confidence 15 and 5.

**Another pass for scans.** Reading a redacted scan again finds what the first OCR pass missed: tiny print, an email first read without its "@", a glued "J.Surname". For a PDF, step 7 records where each remaining rescan finding and known value lies when OCR read it, and whether every issue lies in such a place (`leftovers_complete`). Each entry in `leftovers` has the page, the box in unrotated points, the type and `found_in_pass`, never the text. `another_pass()` returns the leftovers when all of these hold:
  - the file failed for that reason only;
  - this pass found new leftovers;
  - `redaction_pass` < `MAX_PASSES` (3).

`main.py` and `jobs.py` then call `redact_original(..., leftovers=...)` and step 7 again, in the same three-line loop. The file is written again from the original, with those boxes filled black. The redacted file can then hide more than the sanitized copy. Each pass re-OCRs every scanned page (about 3 minutes on the 35-page sample).

Versions: `FILE_REDACTOR_VERSION` (1.4.0), `FILE_VALIDATOR_VERSION` (1.3.0). The hooks it uses are:

- `extract_pdf(ocr_pages=...)`
- the extractors' "locate mode" with `src/extractors/text_sources.py`, which maps each character back to its XML element
- `scan_texts()` in `document_scanner.py`
- `findings_by_block()` in `redactor.py`

### Orchestration rule

`main.py` (the command line) and `src/web/jobs.py` (the web UI, through `app.py`) only connect the steps. All extraction, detection, redaction and validation logic lives in `src/` and must be reused, never reimplemented in either entry point.

- Call `scan_file`/`scan_document`, not `detect_pii` directly. The scanner adds table header context, names widened over first names, surname mentions, values found elsewhere (`known_value_mentions`) and extraction warnings that the raw detectors lack. `evaluate_detector` scores through the scanner's `detect_units()` for the same reason.
- Build one `LocalNLPDetector` and pass it to both `scan_file` and `validate_file`, because the validator rescans the sanitized copy and loading the model is slow. The web UI loads it once, in `Jobs.detector()`, and runs one job at a time.
- `jobs.py` runs the same steps as `main.py`, so a change to the order or the failure handling in one must be made in the other.
- Known debt: `jobs.py` imports shared helpers from `main.py`: `input_problem`, `load_extracted`, `extract_document`/`EXTRACTORS`/`SUPPORTED_TYPES`, `pipeline_outputs`, `delete` and `format_summary`. They should move into `src/` so that `main.py` is only the CLI. `src/extractors/dispatch.py` already holds copies of `extract_document`, `EXTRACTORS`, `SUPPORTED_TYPES` and `extract_pdf_document` (that copy also takes `ocr_pages`); only the original-format validator imports it. `jobs.py` also imports `file_outputs` from `main.py`. Until `main.py` and `jobs.py` switch to it, make every change to extraction dispatch in both files.
- Adding a file type means adding its extractor to `EXTRACTORS` (in both places, for now) and a content check in `input_problem()`.

### Invariants to preserve

- **Release gate.** Code that sends text to an LLM must load it with `src.pii.validator.approved_copy()`. It returns the copy only if the file's SHA-256 matches the one recorded at validation and the verdict is PASS, or NEEDS_REVIEW with every NEEDS_REVIEW issue signed off in `_review.json` for that same validation (`validated_at`) and copy (`sanitized_sha256`), with no check listing fewer issues than its `issues_total` (`review_approval()`). A FAIL can never be signed off. Otherwise it raises `NotApproved`. Never open `_sanitized.json` directly for this. The redacted file is released the same way, only by `src.original_format.validator.approved_file()`: its own verdict PASS, checked beside the copy's current validation, byte for byte unchanged, and `approved_copy()` approving the copy.
- **Failure cleanup.** If extraction or the scan fails, `main.py` still writes `_pii.json` with status `failed` and redacts nothing. If redaction or validation fails, their outputs are deleted, so an unchecked sanitized copy is never left on disk. The same holds for steps 6–7: a file that could not be written or checked is deleted with its record, and a FAIL file is deleted by step 7 itself.
- **Exact offsets.** The redactor replaces text at the scanner's exact offsets and never guesses. If the offsets don't match the text, it records an error and the status becomes `needs_review`. Overlapping findings merge into one redaction. Duplicated text (OCR word lists, copied table headers) must be redacted too.
- **OCR word geometry.** OCR word boxes must stay in the coordinates of the original image. `to_points()` divides them by `ZOOM`. `pdf_tables.py` renders the page again, unrotated, and assigns words to table cells by position. `original_format/images.py` fills word boxes to redact image pixels, and `pdf_file.py` maps a picture's word boxes to the page through its placement matrix (`image_placement()`). Preprocessing that rotates, crops or resizes an image before OCR must map the boxes back. Pixel-only changes such as `invert_dark_bands()` are safe.
- **Shareable reports.** The validation and exposure reports must never contain PII values, only `block_id` and `finding_id`. The same goes for `_file_redaction.json` and `_file_validation.json` (they also hold leftover boxes and types, never text); the evaluation checks them as shareable. Before writing, `exposure_report` searches its output for every value in the PII report and stops on a match.
- **False positives.** A finding with `review_status: "false_positive"` in `_pii.json` is left unredacted.
- **Version constants.** Outputs record `SCHEMA_VERSION`/`SCANNER_VERSION` (`document_scanner.py`), `REDACTOR_VERSION`, `VALIDATOR_VERSION` (also covers the release rule), `EXPOSURE_VERSION`, `REVIEW_VERSION` (`review.py`, the `_review.json` format), `FILE_REDACTOR_VERSION` (`original_format/redactor.py`, what is replaced in a file and how) and `FILE_VALIDATOR_VERSION` (`original_format/validator.py`, the file's checks and release rule). Bump the matching one when you change rules.
- **Word children.** Word-level `children` are not scanned separately, because they repeat their parent's text. If you add a block type with children, check `collect_units()` in `document_scanner.py`.
- **Tunable constants.** Thresholds are module-level constants with comments: `ZOOM` and `NATIVE_TEXT_MIN_CHARS` in `pdf_extractor.py`; `LOW_OCR_CONFIDENCE`, `LOW_OCR_WORD_CONFIDENCE`, `VALUE_LIKE_WORD` and `MIN_KNOWN_VALUE_CHARS` in `document_scanner.py`; `MAX_NAME_TOKENS`, `TITLE_WORDS`, `TEAM_WORDS` and `MAX_LABEL_GAP_WORDS` in `detectors.py`; `HEADER_BAND`/`FOOTER_BAND`, `MIN_GUTTER` and `CENTRE_ZONE` in `page_layout.py`; `LARGE_LINE_RATIO` in `heading_classifier.py`; `PAD` and `FILL` in `original_format/images.py`; `TEXT_MARGIN`, `EDGE`, `PIXEL_PAD` and `FILL` in `original_format/pdf_file.py`; `INSIDE_SHARE` and `MAX_PASSES` in `original_format/validator.py`.
- **Shared word lists.** `word_lists()` lives in `document_scanner.py` (the redactor imports it from there, since the redactor already imports the scanner): the scanner, redactor and validator must read OCR words the same way.

### Adding a PII type

1. Add a regex and its loop in `regex_detector()`, or a label rule in `custom_rule_detector()`, in `src/pii/detectors.py`. Put any labels in `LABEL_PATTERNS`, and a column rule in `CELL_VALUES` if a table header can name it.
2. Add cases to `test_pii_accuracy.py` and examples in both splits of `archive/tests/data/pii_ground_truth.json`. Add the type to `PII_TYPES` in `evaluate_detector.py` and to `TYPE_LABELS` in `backend/static/app.js`.
3. Run `evaluate_detector` and bump `SCANNER_VERSION`. Add a labelled case to `evaluation/build_corpus.py` so the whole pipeline is scored on it.

## Known issues (latest evaluation 2026-10-18, `evaluation/results/20261018-231028`)

On the synthetic corpus (9 documents, 98 labelled values, 152 structure elements) every headline score is 1.000: detection, redaction, release gate, structure and reading order. No unsafe release, no over-block. Snippet benchmark F1: dev 0.957, eval 0.962. Scanner 1.5.0, validator 1.3.0. Re-run on 2026-10-14 (`20261014-025307`, validator 1.5.0, after the OCR "Hide it" and review changes): every score identical. Since 2026-10-17 every run also writes and checks the redacted files (steps 6–7): all 9 pass in one pass and are released.

The validator rescans with the same detector, so it cannot catch a value the detector misses. A PASS verdict means "no PII the detector can see", not "no PII". The same holds for the redacted file's check, which also cannot see text OCR never reads. The corpus is small and synthetic; see "Real samples" below for the case-study files.

- **A name with one garbled character inside is missed**: "Tomasz Zieli·ski" (a PDF font without "ń", or OCR damage) is not name-shaped, so neither spaCy's span nor the label rule survives `plausible_name()`. Accented names themselves are found (`UPPER`/`LOWER` in `detectors.py` cover the Latin scripts). The corpus builder embeds a TrueType font in `multipage_report.pdf` for this reason: PyMuPDF's built-in Helvetica writes "ń" as "·".
- **Minor over-redaction.** The spaCy span "Remind Arun Pillai" includes the leading verb, so one word of context is redacted (context around redactions 0.985). Nothing leaks.
- **Phone rule false positive:** a hyphenated reference in 4-4-4 groups ("contract 7731-2209-8812", eval split) is reported as PHONE. Not tuned, since it showed up in the eval split.
- **Unlabelled PESEL rule:** any 11-digit number with a valid check digit and date (about 3% of random ones) is reported as NATIONAL_ID at 0.80. Unlabelled street addresses include office addresses. Both over-redact; neither leaks.
- **Not measured on real files:** the low-confidence OCR word check (`LOW_OCR_WORD_CONFIDENCE`) may hold back noisy scans more often (the 11-Oct report found 18% of the DOCX sample's screenshot words below 50); DOCX embedded objects now lead to NEEDS_REVIEW; the new heading and two-column rules may relabel or reorder blocks in the samples.
- Team mailboxes (DEV-039) are redacted as EMAIL though the benchmark labels them not PII: a policy choice, left as it is.

**Real samples** (`archive/samples/`). Current status of each:

| Sample | Sanitized copy (steps 3–4) | Redacted file (steps 6–7) | Download |
|---|---|---|---|
| `organizational_pack.pptx` | PASS | PASS (124 findings placed) | yes |
| `tprm_training.docx` | NEEDS_REVIEW: OCR only (4 blocks below confidence 60, 13 with low-confidence value-like words) | PASS (56 findings, 10 images filled, 1 OCR misreading noted, its SVG copy removed) | after the copy's OCR issues are signed off |
| `risk_policy.pdf` (35 scanned pages; also uploaded through the web UI as "Cadence Group Risk Management Policy v6.0") | NEEDS_REVIEW: low-confidence OCR blocks and value-like words, and name parts (three single words whose use as a name is unclear, and the glued "J.Surname" in `p12_b6`, which the surname rule skips because a dot before it usually means an email address) | PASS after 3 passes (black boxes): pass 1 found 18 leftovers on pages 5–23, pass 2 one (an EMPLOYEE_ID on page 6), pass 3 nothing | after the copy's review is signed off |

How these were measured:

- **Copy results** (scanner 1.5.0, validator 1.3.0, 2026-10-13; jobs `20261013-232035_*` and `20261013-232039_*`): `organizational_pack.pptx` went from NEEDS_REVIEW to PASS (all six "Led by" lines hold only a tag). `risk_policy.pdf` went from FAIL to NEEDS_REVIEW, newly finding 4 home addresses, 3 national IDs, 1 date of birth, 1 card number, 4 OCR-broken emails and 1 known value: the EMPLOYEE_ID under a "VENDOR ID" header in `p25_t3_row_3_cell_2`.
- **DOCX and PPTX files**: steps 6–7 run alone, 2026-10-17/18, on jobs `20261017-231910_organizational_pack_fb1b5f` and `20261017-231918_tprm_training_1edd7a`, before black boxes (images were then grey with a tag). `tprm_training.docx` was FAIL until the misreading rule and the SVG removal.
- **PDF file**: 2026-10-19, job `20261013-232035_risk_policy_1460d5`, file redactor 1.4.0, file validator 1.3.0.
  - All findings were placed. Every leftover was something the first OCR pass misread or never read: tiny print (about 2 pt) on page 18, an email first read without its "@", the glued "J.Surname", and words first broken by an apostrophe (page 22, spread to page 25 by the surname rule; not clear they are a name).
  - With grey boxes and tags, OCR also read tags glued to the next word ("PHONE](with", page 7). Black boxes ended that, but they change how OCR reads the rest of a page more, so pass 1 finds more.
  - The web-UI job `20261019-105217_Cadence_Group_Risk_Management_Policy_v6._c9412b` stopped at FAIL after 2 passes with one leftover, which is why `MAX_PASSES` is 3.
- **Evaluation**: `20261018-231028`, after black boxes; `MAX_PASSES` 3 changes nothing there, since every corpus file passes in one pass. All 9 corpus files' redacted files PASS and are released, with 0 exit-code mismatches, and every other score is unchanged from `20261017-231836`.

### Proposed, not done (waiting for the user's approval)

- Show the redacted file's own issues in the Review section, with "Hide it" (fill that spot and check again), so a reviewer can clear what another pass doesn't. Today they are only in `_file_validation.json`, and the page names only the verdict. Offered 2026-10-18.
- When a leftover lies in very small print, fill its whole OCR paragraph: OCR reads tiny print differently each time, so each pass can uncover more. Offered 2026-10-18; the user chose `MAX_PASSES` 3 instead.
- Read small print better at extraction (a higher `ZOOM` for small text), so the first pass misses less. It changes every scanned PDF's extraction, so measure it on the corpus first.
- Describe steps 6–7 in `docs/DETAILED_GUIDE.md` (the short `README.md` covers them).
- Find a surname glued to an initial ("J.Surname") when no "@" follows it.
- Trim a leading verb from spaCy PERSON spans using the part-of-speech tags already computed ("Remind Arun Pillai").
- OCR the scanned pages of a PDF in parallel. Tesseract runs as a subprocess, so threads work; set `OMP_THREAD_LIMIT=1`. Output would be identical. Worth more now: step 7 re-OCRs every scanned page in each of up to `MAX_PASSES` passes.
- Load spaCy without `parser` and `lemmatizer`, which nothing uses. Keep this only if detection results are identical, because NER may respect the sentence breaks the parser sets.
- Enlarge small embedded images before OCR, scaling the word boxes back. No measured need yet.
- **Rejected:** the deskew, median-blur and Otsu preprocessing snippet. Measured on `incident_scan.pdf` (in a scratch folder, OpenCV 5.0): its deskew never rotates (it fits the white background: 98% of pixels), and on the page tilted 2 degrees it lost 2 of 10 PII values that plain OCR read (10/10). It also breaks OCR word geometry if the deskew worked, and Tesseract already binarizes internally.

## Sensitive data

`uploads/` and `output/<job_id>/` hold real PII and are gitignored. In `output/`, only `_validation.json`, `_exposure.json`, `exposure_report.*`, `_review.json` (issue keys, reviewer names and times), `_file_redaction.json` and `_file_validation.json` (block and finding IDs, leftover boxes and types), a `_sanitized.json` that `approved_copy()` releases and a `_redacted.<ext>` that `approved_file()` releases are meant to be free of PII (see "Known issues" for misses). The DOCX sample is not marked as synthetic, so treat the names in it as real. The corpus in `evaluation/corpus/synthetic/` is synthetic and safe to read.

`archive/output/` is committed and holds `_pii.json` and `_tagged.json` files from the samples, which contain PII values. Don't print their contents, and don't add more such files to git.

## Repo state

- Tests, the sample inputs, old outputs and the phase-1 analysis docs are in `archive/`: `archive/tests`, `archive/input`, `archive/samples`, `archive/output` and `archive/phase1`. The move was committed in `91bec0d`. `docs/DETAILED_GUIDE.md` (the old README) still points at `backend/tests/`, `backend/input/`, `samples/` and `docs/phase1/`, and does not describe steps 6–7.
- `backend/output/exposure_report.{md,csv}` are tracked and rebuilt by every run, so they usually show as modified.
- `src/extractors/docx_image_extractor.py` and `merge_docx_content.py` are legacy and nothing imports them. DOCX images are now OCR'd in place by `docx_extractor.py` through `src/ocr/image_ocr.py`.
- `presentation/` holds the case-study deck (`Case_Study_02_PII_Detection_and_Redaction.pptx`).
