
from src.pii.detectors import regex_detector

cases = [
    ("Date", "Meeting date: 04-29-2026"),
    ("Phone with abbreviation", "Tel. 555-0147"),
    ("International phone", "Call +44 20 7946 0958"),
    ("IP address", "IP: 192.168.1.10"),
]

for label, text in cases:
    print(f"\n{label}: {text}")

    for result in regex_detector(text):
        print(result)
