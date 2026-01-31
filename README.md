# TRACES Organic Operator Certificate Downloader

This script automates searching the TRACES "Organic operator certificates" directory and downloads the **PDF certificate** for each supplier listed in `suppliers.csv`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install
```

## Usage

```bash
python download_traces.py --suppliers suppliers.csv --out downloads --headed
```

Options:
- `--headed` shows the browser.
- `--timeout 15000` changes the download timeout (ms).
- `--delay 10` adds a delay between suppliers (seconds).
- `--out downloads` sets the local download folder.

## Local web UI

```bash
python app.py
```

Then open `http://127.0.0.1:5000`, upload your CSV, set delay/timeout options, and download the ZIP.

## CSV format

`suppliers.csv` should contain one supplier name per line. A header like `supplier` is allowed and ignored.

Example:
```csv
supplier
Choconut B.V
Nuts 2 B.V
```
