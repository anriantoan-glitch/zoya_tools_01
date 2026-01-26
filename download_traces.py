import argparse
import csv
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/project/src/.pw-browsers")

from playwright.sync_api import Playwright, sync_playwright, TimeoutError as PWTimeoutError


BASE_URL = "https://webgate.ec.europa.eu/tracesnt/directory/publication/organic-operator/index"
BROWSERS_PATH = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")).expanduser()


def ensure_playwright_browsers() -> None:
    if not BROWSERS_PATH:
        return
    chromium_glob = BROWSERS_PATH.glob("chromium-*/chrome-linux/chrome")
    if any(chromium_glob):
        return
    BROWSERS_PATH.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=True,
        env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS_PATH)},
    )


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^\w\-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "unknown"


def read_suppliers(csv_path: Path) -> list[str]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Suppliers CSV not found: {csv_path}")
    suppliers: list[str] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            value = row[0].strip()
            if not value:
                continue
            if value.lower() in {"supplier", "suppliers", "name", "names"}:
                continue
            suppliers.append(value)
    return suppliers


def first_visible(locator):
    try:
        if locator.count() == 0:
            return None
        first = locator.first
        if first.is_visible():
            return first
    except PWTimeoutError:
        return None
    return None


def find_search_input(page):
    candidates = [
        "input#search",
        "input[name='search']",
        "input[placeholder*='Search']",
        "input[type='search']",
        "input[type='text']",
    ]
    for selector in candidates:
        loc = page.locator(selector)
        item = first_visible(loc)
        if item:
            return item
    return None


def find_search_button(page):
    candidates = [
        "button:has-text('Search')",
        "input[type='submit']",
        "button[type='submit']",
    ]
    for selector in candidates:
        loc = page.locator(selector)
        item = first_visible(loc)
        if item:
            return item
    return None


def download_pdf_for_supplier(page, supplier: str, out_dir: Path, timeout_ms: int) -> bool:
    page.goto(BASE_URL, wait_until="domcontentloaded")

    search_input = find_search_input(page)
    if not search_input:
        raise RuntimeError("Search input not found on page.")
    search_input.fill(supplier)

    search_button = find_search_button(page)
    if not search_button:
        raise RuntimeError("Search button not found on page.")
    search_button.click()
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PWTimeoutError:
        pass

    view_button = page.locator("button:has-text('View')").first
    if view_button.count() == 0 or not view_button.is_visible():
        return False

    view_button.click()

    pdf_item = page.locator("text=PDF certificate").first
    if pdf_item.count() == 0:
        return False

    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = slugify(supplier)

    with page.expect_download(timeout=timeout_ms) as download_info:
        pdf_item.click()
    download = download_info.value
    suggested = download.suggested_filename or "certificate.pdf"
    target_path = out_dir / f"{safe_name}__{suggested}"
    download.save_as(target_path)
    return True


def run(
    playwright: Playwright,
    suppliers: list[str],
    out_dir: Path,
    headed: bool,
    timeout_ms: int,
    delay_seconds: int,
    on_message: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
):
    browser = playwright.chromium.launch(headless=not headed)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()

    total = len(suppliers)
    ok = 0

    def log(message: str) -> None:
        print(message)
        if on_message:
            on_message(message)

    for i, supplier in enumerate(suppliers, start=1):
        log(f"[{i}/{total}] {supplier}")
        try:
            success = download_pdf_for_supplier(page, supplier, out_dir, timeout_ms)
            if success:
                ok += 1
                log("  -> downloaded")
            else:
                log("  -> not found")
        except PWTimeoutError:
            log("  -> timeout")
        except Exception as exc:
            log(f"  -> error: {exc}")
        if on_progress:
            on_progress(i, total, ok)
        if delay_seconds > 0 and i < total:
            time.sleep(delay_seconds)

    log(f"Done. Downloaded {ok} of {total}.")
    context.close()
    browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download organic operator certificates from TRACES.")
    parser.add_argument("--suppliers", default="suppliers.csv", help="Path to suppliers CSV")
    parser.add_argument("--out", default="downloads", help="Output folder for PDFs")
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode")
    parser.add_argument("--timeout", type=int, default=45000, help="Download timeout in ms")
    parser.add_argument("--delay", type=int, default=10, help="Delay between suppliers in seconds")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    suppliers = read_suppliers(Path(args.suppliers))
    if not suppliers:
        print("No suppliers found in CSV.")
        return 1

    out_dir = Path(args.out)
    ensure_playwright_browsers()
    with sync_playwright() as playwright:
        run(
            playwright,
            suppliers,
            out_dir,
            args.headed,
            args.timeout,
            args.delay,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
