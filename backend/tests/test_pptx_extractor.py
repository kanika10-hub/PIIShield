from src.extractors.pptx_extractor import extract_pptx
from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import findings_path, save_findings, scan_document


INPUT_FILE = (
    "input/"
    "organizational_pack.pptx"
)

OUTPUT_FILE = (
    "output/"
    "organizational_pack.json"
)


def main():

    document = extract_pptx(INPUT_FILE)

    document.save_json(OUTPUT_FILE)

    # PII scan of the document just extracted
    findings = scan_document(document, LocalNLPDetector())
    save_findings(findings, findings_path(OUTPUT_FILE))

    print("PPTX extraction completed.")
    print(f"File: {document.file_name}")
    print(f"Slides: {len(document.pages)}")
    print(f"Output: {OUTPUT_FILE}")
    print(f"PII findings: {findings['summary']['total_findings']} {findings['summary']['by_type']}")
    print(f"PII output: {findings_path(OUTPUT_FILE)}")


if __name__ == "__main__":
    main()
