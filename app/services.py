import re
import logging
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import HTTPException, UploadFile
from langchain_core.prompts import ChatPromptTemplate
from pypdf import PdfReader

from .config import settings
from .schemas import InvoiceData, LineItemData


ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".txt"}
logger = logging.getLogger(__name__)


async def save_upload(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "invoice").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "Upload a PDF, PNG, JPG, JPEG, or TXT file.")
    content = await upload.read()
    if not content:
        raise HTTPException(400, "The uploaded file is empty.")
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File exceeds the {settings.max_upload_mb} MB limit.")
    safe_stem = re.sub(r"[^A-Za-z0-9_-]", "_", Path(upload.filename or "invoice").stem)[:80]
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    path = settings.upload_dir / f"{stamp}_{safe_stem}{suffix}"
    path.write_bytes(content)
    return path


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        elif suffix == ".txt":
            text = path.read_text(encoding="utf-8", errors="replace")
        else:
            import pytesseract
            from PIL import Image
            text = pytesseract.image_to_string(Image.open(path))
    except Exception as exc:
        raise HTTPException(422, f"Could not read this invoice: {exc}") from exc
    if not text.strip():
        raise HTTPException(422, "No readable text was found in the invoice.")
    return text[:50000]


def _money(value: str | None) -> Decimal:
    if not value:
        return Decimal("0")
    try:
        return Decimal(re.sub(r"[^0-9.-]", "", value).replace(",", ""))
    except InvalidOperation:
        return Decimal("0")


def _find(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _date(value: str | None):
    if not value:
        return None
    value = re.sub(r"\s+", " ", value.strip()).replace(" ,", ",")
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%b %d, %Y", "%B %d, %Y", "%A, %B %d, %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_invoice_locally(text: str) -> InvoiceData:
    number = _find(r"(?:invoice\s*(?:number|no\.?|#))\s*(?:[:#-]\s*)*([A-Z0-9][A-Z0-9_/-]*)", text)
    if not number:
        number = _find(r"^Order\s*#\s*([^\s]+)", text)
    if not number:
        number = _find(r"^Invoice\s*\n\s*([A-Z0-9][A-Z0-9_/-]+)$", text)
    vendor = _find(r"(?:vendor|seller|from)\s*:\s*(.+)", text)
    if not vendor:
        vendor = next((line.strip() for line in text.splitlines() if line.strip()), "Unknown vendor")
    customer = _find(r"(?:bill\s*to|customer)\s*:\s*(.+)", text)
    invoice_date = _date(_find(r"(?:invoice\s+date|date)\s*:\s*([^\n]+)", text))
    due_date = _date(_find(r"due\s+date\s*:\s*([^\n]+)", text))
    total_matches = re.findall(r"^(?:grand\s+total|order\s+total|total\s+due|total\s+amount\s+due|total\s+for\s+this\s+invoice|total)\b(?:[^\d\n]*\n){0,2}[^\d\n]*([\d,]+\.\d{2})", text, re.IGNORECASE | re.MULTILINE)
    total = _money(total_matches[-1] if total_matches else None)
    subtotal_matches = re.findall(r"^sub[ -]?total\b(?:[^\d\n]*\n){0,2}[^\d\n]*([\d,]+\.\d{2})", text, re.IGNORECASE | re.MULTILINE)
    subtotal = _money(subtotal_matches[-1] if subtotal_matches else None)
    tax_matches = re.findall(r"^(?:tax|vat)(?:\s+\d+(?:\.\d+)?%)?\b[^\d\n]*(?:\n[^\d\n]*)?([\d,]+\.\d{2})", text, re.IGNORECASE | re.MULTILINE)
    tax = _money(tax_matches[-1] if tax_matches else None)
    explicit_currency = _find(r"^(?:currency|currency code)\s*:\s*([A-Z]{3})\b", text)
    currency_codes = ("USD", "GBP", "EUR", "INR", "AUD", "CAD", "JPY", "CNY", "CHF", "SGD", "AED")
    detected_code = next((code for code in currency_codes if re.search(rf"\b{code}\b", text, re.IGNORECASE)), None)
    symbol_currency = next(
        (code for symbol, code in (("₹", "INR"), ("Rs", "INR"), ("£", "GBP"), ("€", "EUR"), ("¥", "JPY"), ("$", "USD")) if symbol in text),
        None,
    )
    location_currency = "INR" if re.search(r"\bIndia\b|\bMaharashtra\b|\.in\b", text, re.IGNORECASE) else None
    currency = (explicit_currency or detected_code or symbol_currency or location_currency or "USD").upper()
    items = []
    item_pattern = re.compile(r"^(.+?)\s+(\d+(?:\.\d+)?)\s+[$£€]?([\d,]+\.\d{2})\s+[$£€]?([\d,]+\.\d{2})$", re.MULTILINE)
    for match in item_pattern.finditer(text):
        items.append(LineItemData(description=match.group(1).strip(), quantity=_money(match.group(2)), unit_price=_money(match.group(3)), amount=_money(match.group(4))))
    if not items and total:
        items = [LineItemData(description="Invoice total", quantity=1, unit_price=total, amount=total)]
    return InvoiceData(invoice_number=number or "Unknown", vendor_name=vendor[:255], customer_name=customer, invoice_date=invoice_date, due_date=due_date, currency=currency, subtotal=subtotal or max(total - tax, Decimal("0")), tax=tax, total=total, line_items=items)


def extract_invoice(text: str) -> InvoiceData:
    if not settings.openai_api_key:
        return parse_invoice_locally(text)
    try:
        from langchain_openai import ChatOpenAI
        prompt = ChatPromptTemplate.from_messages([
            ("system", """You extract structured data from invoice and receipt text.
Use only values explicitly present in the document. Extract every purchased product or service as a line item; do not include subtotal, tax, total, payment, or summary rows as items. Combine wrapped description lines belonging to the same item. Preserve decimal quantities exactly. Subtotal means the pre-tax subtotal; total means the final amount due. Determine the ISO 4217 currency code from an explicit code, currency name, symbol, or vendor locale. Convert locale-formatted numbers to JSON decimals with a period and no thousands separator.

Return one JSON object using exactly these keys: invoice_number, vendor_name, customer_name, invoice_date, due_date, currency, subtotal, tax, total, line_items. Each line_items entry must use exactly: description, quantity, unit_price, amount. When a service row prints only an amount with no quantity or unit price, use quantity 1 and set unit_price to that printed amount. Do not invent any other missing values."""),
            ("human", "Extract this invoice into the requested schema:\n\n{invoice_text}"),
        ])
        model = ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            max_tokens=16384,
            reasoning_effort="low",
        )
        structured_model = model.with_structured_output(InvoiceData, method="json_mode")
        return (prompt | structured_model).invoke({"invoice_text": text})
    except Exception as exc:
        logger.warning("AI invoice extraction failed; using local parser: %s", exc)
        return parse_invoice_locally(text)


def _invoice_context(invoice) -> str:
    items = "\n".join(
        f"- {item.description}: quantity {item.quantity}, unit price {invoice.currency} {item.unit_price}, amount {invoice.currency} {item.amount}"
        for item in invoice.items
    ) or "- No line items were extracted"
    return (
        f"Invoice number: {invoice.invoice_number}\n"
        f"Vendor: {invoice.vendor_name}\n"
        f"Customer: {invoice.customer_name or 'Not provided'}\n"
        f"Invoice date: {invoice.invoice_date or 'Not provided'}\n"
        f"Due date: {invoice.due_date or 'Not provided'}\n"
        f"Subtotal: {invoice.currency} {invoice.subtotal}\n"
        f"Tax: {invoice.currency} {invoice.tax}\n"
        f"Total: {invoice.currency} {invoice.total}\n"
        f"Line items:\n{items}"
    )


def _invoke_invoice_assistant(system_prompt: str, invoice) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("Configure OPENAI_API_KEY to use AI invoice insights.")
    from langchain_openai import ChatOpenAI

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "Review this structured invoice:\n\n{invoice}"),
    ])
    model = ChatOpenAI(
        model=settings.openai_insights_model,
        api_key=settings.openai_api_key,
        max_tokens=1200,
        reasoning_effort="low",
        request_timeout=45,
        max_retries=1,
    )
    response = (prompt | model).invoke({"invoice": _invoice_context(invoice)})
    return str(response.content).strip()


def generate_invoice_insights(invoice) -> str:
    return _invoke_invoice_assistant(
        """You are an accounts-payable analyst. Summarize the invoice in concise plain text.
Cover the vendor, purpose of the purchase, important dates, largest cost drivers, tax, total, and any useful anomalies such as a missing due date or unusual quantity. Use short paragraphs or bullets. Never invent information and do not output Markdown tables.""",
        invoice,
    )


def generate_purchase_recommendations(invoice) -> str:
    return _invoke_invoice_assistant(
        """You are a procurement assistant. Based only on the supplied line-item descriptions, suggest practical ways to seek better prices from other vendors. For each meaningful item, give useful vendor types, marketplaces, manufacturers, distributors, or comparison-search terms and mention bulk, substitute, or negotiation opportunities where relevant. Do not claim to have searched the web, do not fabricate current prices, discounts, savings, or availability, and tell the user to verify quotes, shipping, tax, warranty, and product equivalence. Use concise bullets and no Markdown tables.""",
        invoice,
    )


def _invoices_context(invoices) -> str:
    currency_totals = defaultdict(lambda: {"subtotal": Decimal("0"), "tax": Decimal("0"), "total": Decimal("0"), "count": 0})
    vendor_counts = Counter()
    vendor_totals = defaultdict(Decimal)
    monthly_counts = Counter()
    item_totals = defaultdict(Decimal)
    missing_dates = 0
    missing_numbers = 0
    overdue = 0

    for invoice in invoices:
        currency = invoice.currency or "UNKNOWN"
        bucket = currency_totals[currency]
        bucket["subtotal"] += invoice.subtotal or Decimal("0")
        bucket["tax"] += invoice.tax or Decimal("0")
        bucket["total"] += invoice.total or Decimal("0")
        bucket["count"] += 1
        vendor = (invoice.vendor_name or "Unknown vendor").replace("\n", " ")
        vendor_counts[vendor] += 1
        vendor_totals[(currency, vendor)] += invoice.total or Decimal("0")
        monthly_counts[invoice.processed_at.strftime("%Y-%m")] += 1
        missing_dates += int(invoice.invoice_date is None)
        missing_numbers += int(not invoice.invoice_number or invoice.invoice_number == "Unknown")
        overdue += int(bool(invoice.due_date and invoice.due_date < date.today()))
        for item in invoice.items:
            description = item.description.replace("\n", " ")[:120]
            item_totals[(currency, description)] += item.amount or Decimal("0")

    lines = [f"PORTFOLIO SIZE: {len(invoices)} invoices"]
    lines.append("\nTOTALS BY CURRENCY (calculated locally; do not recalculate or combine):")
    for currency, values in sorted(currency_totals.items()):
        lines.append(f"- {currency}: {values['count']} invoices; subtotal {values['subtotal']}; tax {values['tax']}; total {values['total']}")

    lines.append("\nPROCESSING ACTIVITY (most recent 36 months):")
    for month, count in sorted(monthly_counts.items())[-36:]:
        lines.append(f"- {month}: {count}")

    lines.append("\nTOP VENDORS BY CURRENCY:")
    for currency in sorted(currency_totals):
        ranked = sorted(((total, vendor) for (code, vendor), total in vendor_totals.items() if code == currency), reverse=True)[:10]
        lines.extend(f"- {currency} {vendor}: {total} across {vendor_counts[vendor]} invoice(s)" for total, vendor in ranked)

    lines.append("\nTOP LINE ITEMS BY CURRENCY:")
    for currency in sorted(currency_totals):
        ranked = sorted(((total, item) for (code, item), total in item_totals.items() if code == currency), reverse=True)[:10]
        lines.extend(f"- {currency} {item}: {total}" for total, item in ranked)

    lines.append(f"\nDATA QUALITY / RISK: {missing_dates} missing invoice dates; {missing_numbers} missing invoice numbers; {overdue} past-due dates (payment status is not tracked).")
    lines.append("\nMOST RECENT 60 INVOICES:")
    for invoice in invoices[:60]:
        lines.append(
            f"- {invoice.invoice_number} | {(invoice.vendor_name or 'Unknown vendor').replace(chr(10), ' ')[:80]} | "
            f"invoice date {invoice.invoice_date or 'missing'} | due {invoice.due_date or 'missing'} | {invoice.currency} {invoice.total}"
        )
    if len(invoices) > 60:
        lines.append(f"- {len(invoices) - 60} older invoice rows omitted; their values remain included in all aggregates above.")
    return "\n".join(lines)


def _invoke_invoices_assistant(system_prompt: str, invoices) -> str:
    if not invoices:
        raise RuntimeError("Process at least one invoice before using the AI assistant.")
    if not settings.openai_api_key:
        raise RuntimeError("Configure OPENAI_API_KEY to use the AI assistant.")
    from langchain_openai import ChatOpenAI

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("human", "Analyse these processed invoice records:\n\n{invoices}"),
    ])
    model = ChatOpenAI(model=settings.openai_insights_model, api_key=settings.openai_api_key, max_tokens=1800, reasoning_effort="low", request_timeout=75, max_retries=1)
    return str((prompt | model).invoke({"invoices": _invoices_context(invoices)}).content).strip()


def generate_invoices_summary(invoices) -> str:
    return _invoke_invoices_assistant(
        "Summarize the invoice portfolio concisely. Identify invoice count, vendors, important dates, major purchases, notable taxes, anomalies, and overdue risks. Keep monetary totals separated by currency; never combine currencies. Use clear bullets and do not invent data.",
        invoices,
    )


def generate_invoices_report(invoices) -> str:
    return _invoke_invoices_assistant(
        "Create a practical management report for the invoice portfolio. Include an executive overview, totals grouped by currency, vendor analysis, major cost drivers, tax observations, payment or due-date risks, data-quality issues, and recommended next actions. Never combine currencies or invent values. Use headings and bullets, but no Markdown table.",
        invoices,
    )
