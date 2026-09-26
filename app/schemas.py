from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LineItemData(BaseModel):
    description: str = "Invoice charge"
    quantity: Decimal = Field(default=Decimal("1"), ge=0)
    unit_price: Decimal = Field(default=Decimal("0"), ge=0)
    amount: Decimal = Field(default=Decimal("0"), ge=0)


class InvoiceData(BaseModel):
    invoice_number: str = "Unknown"
    vendor_name: str = "Unknown vendor"
    customer_name: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    currency: str = Field(default="USD", min_length=3, max_length=3)
    subtotal: Decimal = Field(default=Decimal("0"), ge=0)
    tax: Decimal = Field(default=Decimal("0"), ge=0)
    total: Decimal = Field(default=Decimal("0"), ge=0)
    line_items: list[LineItemData] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_extracted_data(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        aliases = {
            "currency_code": "currency",
            "vendor": "vendor_name",
            "customer": "customer_name",
            "items": "line_items",
        }
        for source, target in aliases.items():
            if target not in data and source in data:
                data[target] = data[source]
        for field_name in ("invoice_date", "due_date"):
            raw_date = data.get(field_name)
            if not isinstance(raw_date, str):
                continue
            for date_format in ("%A, %B %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
                try:
                    data[field_name] = datetime.strptime(raw_date.strip(), date_format).date()
                    break
                except ValueError:
                    continue
        normalized_items = []
        for raw_item in data.get("line_items") or []:
            item = dict(raw_item)
            amount = item.get("amount", item.get("total_price", 0))
            item["amount"] = amount
            item["quantity"] = item.get("quantity") or 1
            item["unit_price"] = item.get("unit_price") or item.get("unitPrice") or amount
            normalized_items.append(item)
        data["line_items"] = normalized_items
        return data

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class LineItemOut(LineItemData):
    id: int
    model_config = ConfigDict(from_attributes=True)


class InvoiceOut(BaseModel):
    id: int
    invoice_number: str
    vendor_name: str
    customer_name: str | None
    invoice_date: date | None
    due_date: date | None
    currency: str
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    status: str
    source_filename: str
    processed_at: datetime
    items: list[LineItemOut]
    model_config = ConfigDict(from_attributes=True)


class MonthlyGSTReportParams(BaseModel):
    """Validated calendar period for an Indian GST purchase report."""

    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
