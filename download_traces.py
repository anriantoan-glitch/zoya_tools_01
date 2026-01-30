import argparse
import csv
import os
import re
import subprocess
import sys
import time
from urllib.parse import quote_plus
from pathlib import Path
from typing import Callable
import errno

DEFAULT_PW_PATH = "/ms-playwright"
FALLBACK_PW_PATH = "/opt/render/project/src/.pw-browsers"
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = (
        DEFAULT_PW_PATH if Path(DEFAULT_PW_PATH).exists() else FALLBACK_PW_PATH
    )

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
    lock_path = BROWSERS_PATH / ".install.lock"
    with lock_path.open("w") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file, fcntl.LOCK_EX)
        except Exception:
            pass
        # Another worker might have installed while we waited on the lock.
        if any(BROWSERS_PATH.glob("chromium-*/chrome-linux/chrome")):
            return
        for attempt in range(3):
            try:
                subprocess.run(
                    [sys.executable, "-m", "playwright", "install", "chromium"],
                    check=True,
                    env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(BROWSERS_PATH)},
                )
                return
            except OSError as exc:
                if exc.errno == errno.ETXTBSY and attempt < 2:
                    time.sleep(2)
                    continue
                raise


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^\w\-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "unknown"


def read_suppliers(csv_path: Path) -> list[str]:
    if not csv_path.exists():
        raise FileNotFoundError(f"Suppliers CSV not found: {csv_path}")
    suppliers: list[str] = []
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            suppliers.clear()
            with csv_path.open(newline="", encoding=encoding) as f:
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
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
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


def normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def handle_cookie_banner(page) -> None:
    candidates = [
        "button:has-text('Accept')",
        "button:has-text('Accept all')",
        "button:has-text('I agree')",
        "button:has-text('Agree')",
        "button:has-text('Allow all')",
    ]
    for selector in candidates:
        loc = page.locator(selector)
        item = first_visible(loc)
        if item:
            try:
                item.click(timeout=2000)
            except Exception:
                pass
            break


def download_pdf_for_supplier(page, supplier: str, out_dir: Path, timeout_ms: int) -> bool:
    search_url = f"{BASE_URL}#!?query={quote_plus(supplier)}&sort=-issuedOn"
    page.goto(search_url, wait_until="domcontentloaded")
    handle_cookie_banner(page)

    # If the query param didn't bind for any reason, fall back to manual search.
    search_input = find_search_input(page)
    if search_input:
        search_input.fill(supplier)
        try:
            search_input.press("Enter")
        except Exception:
            pass
        search_button = find_search_button(page)
        if search_button:
            search_button.click()
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PWTimeoutError:
        pass

    view_selector = "button:has-text('View'), a:has-text('View')"
    try:
        page.wait_for_selector(view_selector, timeout=timeout_ms)
    except PWTimeoutError:
        return False

    view_button = None
    supplier_norm = normalize_text(supplier)
    rows = page.locator("tr")
    try:
        row_count = rows.count()
    except Exception:
        row_count = 0

    for i in range(row_count):
        try:
            row = rows.nth(i)
            row_text = row.inner_text()
            if supplier_norm and supplier_norm in normalize_text(row_text):
                candidate = row.locator(view_selector).first
                if candidate.count() > 0:
                    view_button = candidate
                    break
        except Exception:
            continue

    if view_button is None:
        view_button = page.locator(view_selector).first
        if view_button.count() == 0 or not view_button.is_visible():
            return False

    view_button.click()

    pdf_item = page.locator("text=PDF certificate").first
    if pdf_item.count() == 0:
        pdf_item = page.locator("a[href$='.pdf']").first
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
    should_cancel: Callable[[], bool] | None = None,
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
        if should_cancel and should_cancel():
            log("  -> cancelled")
            break
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
