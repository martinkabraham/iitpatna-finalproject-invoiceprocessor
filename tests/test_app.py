from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import InvoiceData
from app.services import _invoices_context, generate_monthly_gst_csv, parse_invoice_locally, validate_invoice_document


def test_local_invoice_parser():
    text = """ACME Supplies
Invoice Number: INV-1001
Invoice Date: 2025-04-20
Due Date: 2025-05-20
Bill To: Example Ltd
Widget 2 25.00 50.00
Subtotal: $50.00
Tax: $5.00
Total: $55.00
"""
    result = parse_invoice_locally(text)
    assert result.invoice_number == "INV-1001"
    assert result.vendor_name == "ACME Supplies"
    assert result.total == Decimal("55.00")
    assert len(result.line_items) == 1


def test_non_invoice_document_is_rejected():
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException, match="does not appear to be a valid invoice"):
        validate_invoice_document("Quarterly project meeting notes\nAttendees: Alice and Bob\nNext meeting: Friday")


def test_invoice_document_is_accepted():
    validate_invoice_document("ACME Ltd\nINVOICE\nInvoice Number: INV-12\nInvoice Date: 2026-09-01\nTotal Amount Due: $42.00")


def test_order_receipt_document_is_accepted():
    validate_invoice_document(
        "Nirmal Coffee\nOrder# NC-12\nBilling Information\nDate: 2026-09-01\nTax: 2.00\nOrder total 42.00"
    )


def test_browser_upload_displays_non_invoice_error():
    response = TestClient(app).post(
        "/upload",
        files={"file": ("notes.txt", b"Quarterly meeting notes and action items", "text/plain")},
    )
    assert response.status_code == 422
    assert 'id="upload-error"' in response.text
    assert "does not appear to be a valid invoice" in response.text


def test_local_parser_detects_invoice_currency():
    result = parse_invoice_locally("Invoice Number: UK-1\nCurrency: GBP\nTotal: £120.00")
    assert result.currency == "GBP"
    assert result.total == Decimal("120.00")


def test_local_parser_handles_order_receipt_labels():
    text = """Order# 111-order_TVGrQqYhsUlV7I
https://shop.example.in
Date: Friday, August 28, 2026
Address: India, Maharashtra, Mumbai
Sub-total: 815.00
Tax: 40.75
Order total 1,155.61
"""
    result = parse_invoice_locally(text)
    assert result.invoice_number == "111-order_TVGrQqYhsUlV7I"
    assert result.currency == "INR"
    assert result.subtotal == Decimal("815.00")
    assert result.total == Decimal("1155.61")


def test_ai_amount_only_line_items_are_normalized():
    result = InvoiceData.model_validate({
        "currency_code": "USD",
        "vendor": "Amazon Web Services, Inc.",
        "total": "4.11",
        "line_items": [{"description": "AWS Data Transfer", "quantity": None, "unit_price": None, "amount": "0.01"}],
    })
    assert result.vendor_name == "Amazon Web Services, Inc."
    assert result.currency == "USD"
    assert result.line_items[0].quantity == Decimal("1")
    assert result.line_items[0].unit_price == Decimal("0.01")


def test_ai_human_readable_date_is_normalized():
    result = InvoiceData.model_validate({
        "invoice_date": "Friday, August 28, 2026",
        "line_items": [],
    })
    assert result.invoice_date.isoformat() == "2026-08-28"


def test_portfolio_context_stays_bounded_for_many_invoices():
    from app.database import SessionLocal
    from app.models import Invoice

    with SessionLocal() as db:
        invoice = db.query(Invoice).first()
        if invoice is None:
            return
        context = _invoices_context([invoice] * 300)
    assert "PORTFOLIO SIZE: 300 invoices" in context
    assert "240 older invoice rows omitted" in context
    assert len(context) < 40000


def test_health_endpoint():
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_home_page_has_processing_and_refresh_controls():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert 'href="/static/style.css"' in response.text
    assert 'href="/static/actions.css"' in response.text
    assert 'id="processing-overlay"' in response.text
    assert "Refresh list" in response.text
    assert "Invoices processed by month" in response.text
    assert "Summarize Invoices" in response.text
    assert "Generate Report" in response.text
    assert "Download India GST Report" in response.text
    assert "/samples/NirmalCoffee.pdf" in response.text
    assert "/samples/AmazonWebServices.pdf" not in response.text
    assert "/samples/AzureInterior.pdf" in response.text
    assert "/samples/FlipkartInvoice.pdf" not in response.text


def test_sample_invoice_can_be_downloaded():
    response = TestClient(app).get("/samples/NirmalCoffee.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


def test_browser_delete_missing_invoice():
    response = TestClient(app).post("/invoices/999999/delete")
    assert response.status_code == 404


def test_ai_routes_reject_missing_invoice():
    client = TestClient(app)
    assert client.post("/invoices/999999/insights").status_code == 404
    assert client.post("/invoices/999999/recommendations").status_code == 404


def test_monthly_gst_report_download():
    response = TestClient(app).get("/invoices/gst-report?year=2026&month=8")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="india-gst-report-2026-08.csv"' in response.headers["content-disposition"]
    assert "India GST Purchase Register,2026-08" in response.text
    assert "Taxable Value (INR)" in response.text


def test_gst_csv_totals_invoices():
    from datetime import date
    from types import SimpleNamespace

    invoice = SimpleNamespace(
        invoice_date=date(2026, 8, 14),
        invoice_number="GST-1",
        vendor_name="Example India Ltd",
        subtotal=Decimal("100.00"),
        tax=Decimal("18.00"),
        total=Decimal("118.00"),
        source_filename="gst-1.pdf",
    )
    report = generate_monthly_gst_csv([invoice], 2026, 8)
    assert "GST-1,Example India Ltd,100.00,18.00,118.00" in report
    assert "TOTAL,,,100.00,18.00,118.00" in report
