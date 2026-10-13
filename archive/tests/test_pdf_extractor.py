from src.extractors.pdf_extractor import extract_pdf
from src.structure.heading_classifier import classify_headings
from src.structure.pdf_tables import reconstruct_tables
from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import findings_path, save_findings, scan_document


INPUT_FILE = "input/risk_policy.pdf"
OUTPUT_FILE = "output/risk_policy.json"


document = extract_pdf(INPUT_FILE)

# Tables first, so table text is not classified as headings
document = reconstruct_tables(document, INPUT_FILE)

document = classify_headings(document)

document.save_json(OUTPUT_FILE)

# PII scan of the document just extracted
findings = scan_document(document, LocalNLPDetector())
save_findings(findings, findings_path(OUTPUT_FILE))

print("PDF extraction completed.")
print("Pages:", len(document.pages))
print("Tables:", document.metadata["table_reconstruction"]["tables"])
print("Output:", OUTPUT_FILE)
print("PII findings:", findings["summary"]["total_findings"], findings["summary"]["by_type"])
print("PII output:", findings_path(OUTPUT_FILE))
