# PII Pipeline Test: Conclusion

**Test date:** 13 October 2026 · **Pipeline run:** `evaluation/results/20261013-233019` · **Versions:** scanner 1.4.0, redactor 1.0.0, validator 1.3.0

**Test documents:** `evaluation/panel_test/` (2 Word, 2 PowerPoint, 2 PDF, one of them a scanned page). Every name, number and email in them is made up. Each document has a `.labels.json` answer key listing its PII.

**Detailed data:** `evaluation/panel_test_report.json` (every PII value and whether it was found and removed).

## Conclusion

The pipeline **found and removed all 91 PII values** in the six test documents (116 occurrences, all 13 supported PII types). **No PII leaked** into the sanitized output, and **no document holding PII was released.**

It made **4 false detections**: ordinary text taken for a person's name. Because of two of them, **2 clean documents were held for human review** instead of being released. That is the safe kind of mistake, but it costs a reviewer's time.

| Score | Result | 95% confidence range |
|---|---:|---|
| **Recall** (PII found / all PII) | **100.0%** (91/91) | 96.0% to 100.0% |
| **Precision** (real PII / everything flagged) | **95.8%** (91/95) | 89.6% to 98.8% |
| **F1 score** (balance of the two) | **97.8%** | - |
| **Accuracy** (correct decisions / all decisions judged) | **97.4%** (150/154) | 93.5% to 99.3% |
| **Removal rate** (PII gone from the output) | **100.0%** (91/91 values, 116/116 occurrences) | 96.0% to 100.0% |
| **Leaked values** | **0** | - |
| **Look-alikes left alone** (not PII, correctly kept) | **98.3%** (59/60) | 91.1% to 100.0% |
| **Text extraction** (PII values read correctly from the file) | **100.0%** (91/91 exact) | 96.0% to 100.0% |
| **Release decision correct** | **4 of 6** | 22.3% to 95.7% |
| **Unsafe releases** (PII released) | **0 of 6** | - |
| **Structure kept** (headings, tables, lists kept as such) | **90.1%** (183/203) | 85.2% to 93.9% |
| **Reading order kept** | **100.0%** (183/183) | 98.0% to 100.0% |
| **Context around redactions kept** | **98.6%** (71/72) | 92.5% to 100.0% |

**How to read the confidence range:** the test used a limited number of values, so a score is an estimate. The range shows where the true score most likely lies (95% exact binomial interval). For example, finding 91 of 91 values does not prove the pipeline finds 100% of PII. It shows, with 95% confidence, that it finds at least 96.0% on documents like these. With only 6 documents, the release-decision range is wide: the test is too small to pin that number down.

**How accuracy is counted:** every item the test can judge is one decision. That means 91 PII values (should be redacted), 60 look-alikes (should be left alone) and 3 pieces of ordinary text the pipeline flagged without being asked. 150 of 154 decisions were correct. Accuracy over every word in the documents would be close to 100% and say little, so it is not used.

## Against the case-study brief

| Brief target | Result | Met? |
|---|---|---|
| PII detection should approach 100% coverage | 100.0% recall, 0 leaked (95% range 96.0% to 100.0%) | Yes |
| Extracted text keeps at least 80% of its structure, with context before and after | 90.1% structure, 100.0% reading order, 98.6% context; 5 of 6 documents reach 80% | Yes, except the scanned form (44%) |

## Results per document

| Document | Type | PII | Found | Missed | False flags | Precision | Recall | F1 | Removed | Leaked | Look-alikes kept | Structure | Result | Time |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| `contractor_onboarding.docx` | Word | 15 | 15 | 0 | 0 | 100.0% | 100.0% | 100.0% | 15/15 | 0 | 11/11 | 100% | Released (PASS) | 0.3 s |
| `assignment_record.docx` | Word | 22 | 22 | 0 | 0 | 100.0% | 100.0% | 100.0% | 22/22 | 0 | 16/16 | 100% | Released (PASS) | 0.4 s |
| `tprm_training.pptx` | PowerPoint | 11 | 11 | 0 | 0 | 100.0% | 100.0% | 100.0% | 11/11 | 0 | 8/8 | 100% | Released (PASS) | 0.4 s |
| `incident_briefing.pptx` | PowerPoint | 14 | 14 | 0 | 1 | 93.3% | 100.0% | 96.6% | 14/14 | 0 | 9/10 | 100% | Held for review | 0.4 s |
| `due_diligence_report.pdf` | PDF (text) | 20 | 20 | 0 | 1 | 95.2% | 100.0% | 97.6% | 20/20 | 0 | 9/9 | 82% | Released (PASS) | 0.5 s |
| `access_request_scan.pdf` | PDF (scanned) | 9 | 9 | 0 | 2 | 81.8% | 100.0% | 90.0% | 9/9 | 0 | 6/6 | 44% | Held for review | 1.4 s |

## Results per PII type

| PII type | Values | Found | Missed | False flags | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Person name | 28 | 28 | 0 | 4 | 87.5% | 100.0% | 93.3% |
| Email | 11 | 11 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| Phone number | 10 | 10 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| Date of birth | 7 | 7 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| Employee ID | 7 | 7 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| IP address | 7 | 7 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| National ID (NI, PESEL, Aadhaar) | 5 | 5 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| Home address | 5 | 5 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| Bank account / IBAN | 3 | 3 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| Passport number | 3 | 3 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| Card number | 3 | 3 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| Indian PAN | 1 | 1 | 0 | 0 | 100.0% | 100.0% | 100.0% |
| US SSN | 1 | 1 | 0 | 0 | 100.0% | 100.0% | 100.0% |

Some types have only one or two examples (US SSN, Indian PAN), so their 100% says little on its own.

## Results per file format

| Format | Values | Found | False flags | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Word (text + images) | 37 | 37 | 0 | 100.0% | 100.0% | 100.0% |
| PDF with text layer (+ image) | 20 | 20 | 1 | 95.2% | 100.0% | 97.6% |
| Scanned PDF (OCR only) | 9 | 9 | 2 | 81.8% | 100.0% | 90.0% |
| PowerPoint (text + images) | 25 | 25 | 1 | 96.2% | 100.0% | 98.0% |

## Where the PII was, and whether it was removed

| Place in the document | Occurrences | Removed |
| --- | ---: | ---: |
| Table | 30 | 30 |
| Body text | 30 | 30 |
| Slide bullet | 14 | 14 |
| Text in an image (OCR) | 10 | 10 |
| Scanned page (OCR) | 9 | 9 |
| Bulleted list | 7 | 7 |
| File properties | 6 | 6 |
| Speaker notes | 5 | 5 |
| Surname on its own | 2 | 2 |
| Footer | 1 | 1 |
| Comment | 1 | 1 |
| Title slide | 1 | 1 |

19 occurrences were inside images or on the scanned page and had to be read by OCR. OCR read every one of them exactly, character for character.

## What went wrong

| Document | Problem | Text | Effect |
| --- | --- | --- | --- |
| `incident_briefing.pptx` | Wrongly flagged as a name | "Security Operations" | Ordinary text redacted in the copy |
| `due_diligence_report.pdf` | Wrongly flagged as a name | "Key Personnel" | Ordinary text redacted in the copy |
| `access_request_scan.pdf` | Wrongly flagged as a name | "A. Requester" | Ordinary text redacted in the copy |
| `access_request_scan.pdf` | Wrongly flagged as a name | "B. Access Requested" | Ordinary text redacted in the copy |
| `incident_briefing.pptx` | Held for review | - | A word from a wrongly flagged name appears elsewhere, so the pipeline held the copy for a person to check. No PII was left in it. |
| `access_request_scan.pdf` | Held for review | - | A word from a wrongly flagged name appears elsewhere, so the pipeline held the copy for a person to check. No PII was left in it. |

Other, smaller issues (no PII involved):

- **Scanned form layout:** OCR read every word correctly, but the title, section headings, table and footer came out as plain paragraphs (44% structure). The table is missed because the page is tilted half a degree; on the same page without the tilt, the table is found.
- **Numbered headings in the PDF** ("1. Summary" to "6. Conclusion") came out as plain paragraphs. Their text and order are intact.
- **"Ms Gallagher"** was redacted as a whole, so the title "Ms" is lost in the copy.

## Second check: detector on 100 labelled text snippets

The same session also scored the detector alone on an older set of short labelled texts. It includes harder cases, so it is a useful second view.

| Set | Snippets | Found | Missed | False flags | Precision | Recall | F1 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| dev (rules were tuned on these) | 59 | 55 | 4 | 1 | 98.2% | 93.2% | 95.6% |
| eval (never used for tuning) | 41 | 37 | 2 | 1 | 97.4% | 94.9% | 96.1% |

All its misses are person names. On text never used for tuning, F1 is 96.1%.

## Speed

3.5 seconds for all six documents (2.6 s of it reading the files, mostly OCR), after loading the language model once.

## Limits of this test

- **Small sample:** 91 PII values in 6 documents. The confidence ranges above show how much that limits the conclusions.
- **Written by the team that built the pipeline:** the documents are realistic but not adversarial, and use the formats the rules support: UK, US, EU, Polish and Indian identifiers, and names in Latin script. Not tested: handwriting, non-Latin scripts, IPv6, other countries' ID formats, badly damaged scans.
- **The validator uses the same detector:** a PASS means "no PII the detector can see". It cannot catch a value the detector would miss.
- **Pictures in the original files are not changed:** the sanitized copy is text. Only that text copy is cleared to go to an LLM.
- **Not run on the real case-study files:** these scores describe the six test documents only.

## How to rerun

```powershell
.venv\Scripts\Activate.ps1
python evaluation\run_evaluation.py --corpus evaluation\panel_test
```
