
from src.pii.detectors import combine_detections

email = {
    "type": "EMAIL",
    "text": "john@example.com",
    "start": 10,
    "end": 26,
    "confidence": 0.99,
    "detector": "regex"
}

name = {
    "type": "PERSON_NAME",
    "text": "John",
    "start": 10,
    "end": 14,
    "confidence": 0.80,
    "detector": "nlp"
}

duplicate_email = {
    **email,
    "confidence": 0.95,
    "detector": "custom_rule"
}

results = combine_detections(
    [email, name],
    [duplicate_email]
)

for result in results:
    print(result)

assert len(results) == 2
