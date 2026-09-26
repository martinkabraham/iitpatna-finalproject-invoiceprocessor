from collections import Counter
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import BASE_DIR, settings
from .database import Base, engine, get_db
from .models import Invoice, LineItem
from .schemas import InvoiceOut, MonthlyGSTReportParams
from .services import (
    extract_invoice,
    extract_text,
    generate_invoice_insights,
    generate_invoices_report,
    generate_invoices_summary,
    generate_monthly_gst_csv,
    generate_purchase_recommendations,
    save_upload,
)


Base.metadata.create_all(bind=engine)
app = FastAPI(title=settings.app_name, version="1.0.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.mount("/samples", StaticFiles(directory=BASE_DIR / "sample_invoices"), name="samples")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


def store_invoice(db: Session, data, filename: str, raw_text: str) -> Invoice:
    invoice = Invoice(invoice_number=data.invoice_number, vendor_name=data.vendor_name, customer_name=data.customer_name, invoice_date=data.invoice_date, due_date=data.due_date, currency=data.currency, subtotal=data.subtotal, tax=data.tax, total=data.total, source_filename=filename, raw_text=raw_text)
    invoice.items = [LineItem(description=item.description, quantity=item.quantity, unit_price=item.unit_price, amount=item.amount) for item in data.line_items]
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return invoice


def invoice_list_data(db: Session) -> dict:
    invoices = db.scalars(select(Invoice).order_by(Invoice.processed_at.desc())).all()
    monthly_counts = Counter(invoice.processed_at.strftime("%Y-%m") for invoice in invoices)
    year_counts = Counter(invoice.processed_at.strftime("%Y") for invoice in invoices)
    largest_month = max(monthly_counts.values(), default=1)
    monthly_chart = [
        {
            "key": key,
            "label": invoice_month_label(key),
            "count": monthly_counts[key],
            "height": max(8, round(monthly_counts[key] / largest_month * 100)),
        }
        for key in sorted(monthly_counts)
    ]
    return {
        "invoices": invoices,
        "count": len(invoices),
        "monthly_chart": monthly_chart,
        "year_counts": sorted(year_counts.items()),
    }


def invoice_month_label(key: str) -> str:
    from datetime import datetime

    return datetime.strptime(key, "%Y-%m").strftime("%b %Y")


@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "index.html", invoice_list_data(db))


@app.post("/invoices/summary", response_class=HTMLResponse)
def invoices_summary_page(request: Request, db: Session = Depends(get_db)):
    context = invoice_list_data(db)
    try:
        context["portfolio_result"] = generate_invoices_summary(context["invoices"])
        context["portfolio_title"] = "Invoice summary"
    except Exception as exc:
        context["portfolio_error"] = f"Could not generate summary: {exc}"
    return templates.TemplateResponse(request, "index.html", context)


@app.post("/invoices/report", response_class=HTMLResponse)
def invoices_report_page(request: Request, db: Session = Depends(get_db)):
    context = invoice_list_data(db)
    try:
        context["portfolio_result"] = generate_invoices_report(context["invoices"])
        context["portfolio_title"] = "Invoice portfolio report"
    except Exception as exc:
        context["portfolio_error"] = f"Could not generate report: {exc}"
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/invoices/gst-report")
def download_monthly_gst_report(
    period: Annotated[MonthlyGSTReportParams, Depends()],
    db: Session = Depends(get_db),
):
    month_start = date(period.year, period.month, 1)
    month_end = date(period.year + (period.month == 12), period.month % 12 + 1, 1)
    invoices = db.scalars(
        select(Invoice)
        .where(
            Invoice.currency == "INR",
            Invoice.invoice_date >= month_start,
            Invoice.invoice_date < month_end,
        )
        .order_by(Invoice.invoice_date, Invoice.invoice_number)
    ).all()
    content = "\ufeff" + generate_monthly_gst_csv(invoices, period.year, period.month)
    filename = f"india-gst-report-{period.year:04d}-{period.month:02d}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/upload")
async def upload_page(file: UploadFile = File(...), db: Session = Depends(get_db)):
    path = await save_upload(file)
    try:
        text = extract_text(path)
        invoice = store_invoice(db, extract_invoice(text), file.filename or path.name, text)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return RedirectResponse(f"/invoices/{invoice.id}", status_code=303)


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_page(invoice_id: int, request: Request, db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    return templates.TemplateResponse(request, "detail.html", {"invoice": invoice})


@app.post("/invoices/{invoice_id}/insights", response_class=HTMLResponse)
def invoice_insights_page(invoice_id: int, request: Request, db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    try:
        insight = generate_invoice_insights(invoice)
        context = {"invoice": invoice, "ai_insights": insight}
    except Exception as exc:
        context = {"invoice": invoice, "ai_error": f"Could not generate insights: {exc}"}
    return templates.TemplateResponse(request, "detail.html", context)


@app.post("/invoices/{invoice_id}/recommendations", response_class=HTMLResponse)
def invoice_recommendations_page(invoice_id: int, request: Request, db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    try:
        recommendations = generate_purchase_recommendations(invoice)
        context = {"invoice": invoice, "purchase_recommendations": recommendations}
    except Exception as exc:
        context = {"invoice": invoice, "ai_error": f"Could not generate recommendations: {exc}"}
    return templates.TemplateResponse(request, "detail.html", context)


@app.post("/invoices/{invoice_id}/delete")
def delete_invoice_page(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    db.delete(invoice)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/invoices/upload", response_model=InvoiceOut, status_code=201)
async def upload_api(file: UploadFile = File(...), db: Session = Depends(get_db)):
    path = await save_upload(file)
    try:
        text = extract_text(path)
        return store_invoice(db, extract_invoice(text), file.filename or path.name, text)
    except Exception:
        path.unlink(missing_ok=True)
        raise


@app.get("/api/invoices", response_model=list[InvoiceOut])
def list_invoices(db: Session = Depends(get_db)):
    return db.scalars(select(Invoice).order_by(Invoice.processed_at.desc())).all()


@app.get("/api/invoices/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    return invoice


@app.delete("/api/invoices/{invoice_id}", status_code=204)
def delete_invoice(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.get(Invoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Invoice not found")
    db.delete(invoice)
    db.commit()
