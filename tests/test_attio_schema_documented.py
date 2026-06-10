"""Guardrail: ensure the Attio schema doc section exists in sales-program.md.

If this test fails, the manual Attio UI setup step may have been dropped
from the program doc, which would leave new operators without instructions
to provision the attributes required by workflows/response_classifier.py
and workflows/learn.py.
"""

from pathlib import Path


def test_response_classification_documented_in_program_doc():
    doc = (Path(__file__).parent.parent / "sales-program.md").read_text()
    assert "response_classification" in doc, (
        "sales-program.md must document the response_classification Attio attribute"
    )
    assert "last_response_text" in doc, (
        "sales-program.md must document the last_response_text Attio attribute"
    )
    # The documentation should describe it as a select/status attribute
    assert "Single select" in doc or "Status" in doc or "status" in doc.lower(), (
        "response_classification must be documented as a Status/Select attribute"
    )
    # Manual setup steps must be present
    assert "Manual setup" in doc or "manual setup" in doc.lower(), (
        "sales-program.md must include manual setup steps for operators"
    )
