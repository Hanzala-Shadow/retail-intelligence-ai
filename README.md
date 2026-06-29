# Retail Intelligence Pipeline

2-week sprint: collect SEC 10-K filings and sustainability/ESG reports for approximately 45 publicly traded retailers, parse and chunk them into a structured PostgreSQL database.

## Folder Structure

- data/00_reference/ - companies.csv, filings.csv, sustainability_urls.csv
- data/01_raw/10k/ - raw downloaded 10-K HTML files
- data/01_raw/sustainability/ - raw ESG/Sustainability PDFs
- data/02_interim/ - cleaned text files
- data/03_sections/ - extracted document sections
- data/04_chunks/ - chunked text
- data/05_db/ - database validation outputs
- logs/ - application logs
- src/ - Python source code

## Getting Started

python -m venv venv

Windows:
venv\Scripts\Activate.ps1

Install dependencies:

pip install -r requirements.txt

Copy:

Copy-Item .env.template .env

Fill in your credentials inside .env.

## Team

Project Owner: Dr. Ayse Cetinel

Daily Technical Leads:
- Hanzala
- Ibraheem
