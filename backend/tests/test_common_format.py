from src.models.document import (
    ExtractedDocument,
    DocumentPage,
    DocumentBlock
)


def main():

    paragraph = DocumentBlock(
        block_id="B001",
        block_type="paragraph",
        text="Contact John Smith at john@example.com.",
        order=1
    )

    heading = DocumentBlock(
        block_id="B002",
        block_type="heading",
        text="Employee Information",
        order=2
    )

    page = DocumentPage(
        page_number=1,
        blocks=[
            paragraph,
            heading
        ],
        metadata={
            "content_type": "page"
        }
    )

    document = ExtractedDocument(
        document_id="TEST001",
        file_name="test.pdf",
        source_type="PDF",
        extraction_method="test",
        pages=[page]
    )

    document.save_json(
        "output/test_document.json"
    )

    print("Common document format created successfully.")


if __name__ == "__main__":
    main()
