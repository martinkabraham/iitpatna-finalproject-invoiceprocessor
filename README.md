# Invoice Intelligence

A Python capstone project that uploads invoices, extracts structured data, stores processed records in SQLite, and presents results in a FastAPI-rendered dashboard.

## Features

- Upload PDF or plain-text invoices
- Extract invoice fields into validated Pydantic models
- Use LangChain + OpenAI for natural-language extraction when `OPENAI_API_KEY` is configured
- Fall back to a local rule-based parser when an AI key is unavailable
- Store invoice headers and line items in SQLite using SQLAlchemy
- Browse processed invoices and detailed records with extracted currencies
- Download monthly India GST purchase-register CSV reports for processed INR invoices
- JSON API endpoints and interactive Swagger documentation

## Technology

Python 3.10+, FastAPI, Jinja2, Pydantic, LangChain, SQLAlchemy, SQLite, and PyPDF.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000>. API documentation is at <http://127.0.0.1:8000/docs>.

To enable LangChain/OpenAI extraction, add an API key to `.env`. Without one, the built-in parser is used.

`OPENAI_MODEL` controls invoice extraction. `OPENAI_INSIGHTS_MODEL` controls the faster on-demand summary and procurement suggestions shown on invoice detail pages.

## Project layout

```text
app/
  main.py          FastAPI pages and API routes
  database.py      SQLite engine and session
  models.py        SQLAlchemy database models
  schemas.py       Pydantic validation models
  services.py      file reading and invoice extraction
  templates/       Jinja2 UI
  static/          dashboard styling
tests/             parser and API tests
uploads/           locally stored source invoices (created at runtime)
invoice_reader.db  SQLite database (created at runtime)
```

## API

- `POST /api/invoices/upload` — multipart invoice upload
- `GET /api/invoices` — processed invoice list
- `GET /api/invoices/{id}` — invoice and line-item detail
- `DELETE /api/invoices/{id}` — delete a processed record
- `GET /health` — service health

## Tests

```bash
pytest
```

## Azure App Service

For Linux App Service, configure `DATABASE_URL=sqlite:////home/data/invoice_reader.db` and `UPLOAD_DIR=/home/data/uploads` so SQLite and uploaded files use persistent `/home` storage. Set the App Service **Startup Command** to:

```bash
./startup.sh
```

The script uses Azure's `PORT` and optional `WEB_CONCURRENCY` environment variables, defaulting to port `8000` and two workers.

For a multi-instance production deployment, replace SQLite and local uploads with Azure Database for PostgreSQL and Blob Storage.

## Sample invoices

Test PDFs are available in `sample_invoices/`. Upload any of them on the home page to exercise the extraction flow.

> Uploaded invoices can contain sensitive information. This learning project stores files and data locally. Add authentication, encryption, malware scanning, access control, backups, and a production database before using it with real financial data.
