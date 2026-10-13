
from src.pii.detectors import (
    LocalNLPDetector,
    detect_pii
)

sample = (
    "Employee John Smith can be contacted at "
    "john.smith@cadence.com. Employee ID: EMP-12345. "
    "IP address: 192.168.1.10."
)

nlp = LocalNLPDetector()
findings = detect_pii(sample, nlp)

print("NLP status:", nlp.error or "Model loaded")
print("\nDetection results:")

for finding in findings:
    print(finding)

print("\nOffset checks:")

for finding in findings:
    extracted = sample[finding["start"]:finding["end"]]
    print(
        finding["type"],
        "| offsets valid:", extracted == finding["text"]
    )
