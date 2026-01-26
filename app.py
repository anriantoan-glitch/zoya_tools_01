import io
import zipfile
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread
from uuid import uuid4

from flask import Flask, jsonify, render_template, request, send_file
from playwright.sync_api import sync_playwright

from download_traces import read_suppliers, run


APP_ROOT = Path(__file__).resolve().parent
RUNS_DIR = APP_ROOT / "web_runs"
JOBS: dict[str, dict] = {}
JOBS_LOCK = Lock()


def create_zip_file(source_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(source_dir))


def update_job(job_id: str, **updates) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(updates)


def append_log(job_id: str, message: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["logs"].append(message)


app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html", job_id=None, error=None)


@app.post("/download")
def download():
    upload = request.files.get("csv_file")
    if upload is None or upload.filename == "":
        return render_template("index.html", job_id=None, error="Please upload a CSV file.")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    job_dir = RUNS_DIR / f"run_{timestamp}"
    job_dir.mkdir(parents=True, exist_ok=True)

    csv_path = job_dir / "suppliers.csv"
    upload.save(csv_path)

    try:
        delay_seconds = int(request.form.get("delay_seconds", "10"))
    except ValueError:
        delay_seconds = 10
    try:
        timeout_ms = int(request.form.get("timeout_ms", "45000"))
    except ValueError:
        timeout_ms = 45000

    job_id = uuid4().hex
    out_dir = job_dir / "downloads"
    zip_path = job_dir / "traces_pdfs.zip"

    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "current": 0,
            "total": 0,
            "ok": 0,
            "error": None,
            "logs": [],
            "zip_path": str(zip_path),
        }

    def worker() -> None:
        try:
            suppliers = read_suppliers(csv_path)
            if not suppliers:
                update_job(job_id, status="error", error="No suppliers found in the CSV.")
                return

            update_job(job_id, total=len(suppliers))

            def on_message(message: str) -> None:
                append_log(job_id, message)

            def on_progress(current: int, total: int, ok: int) -> None:
                update_job(job_id, current=current, total=total, ok=ok)

            with sync_playwright() as playwright:
                run(
                    playwright=playwright,
                    suppliers=suppliers,
                    out_dir=out_dir,
                    headed=False,
                    timeout_ms=timeout_ms,
                    delay_seconds=delay_seconds,
                    on_message=on_message,
                    on_progress=on_progress,
                )

            if not out_dir.exists():
                update_job(job_id, status="error", error="No downloads were created.")
                return

            create_zip_file(out_dir, zip_path)
            update_job(job_id, status="done")
        except Exception as exc:
            update_job(job_id, status="error", error=str(exc))

    Thread(target=worker, daemon=True).start()
    return render_template("index.html", job_id=job_id, error=None)


@app.get("/status/<job_id>")
def status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"status": "missing"}), 404
        return jsonify(job)


@app.get("/result/<job_id>")
def result(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return "Job not found", 404
        if job["status"] != "done":
            return "Job not finished", 400
        zip_path = Path(job["zip_path"])
    if not zip_path.exists():
        return "ZIP not found", 404
    zip_name = f"traces_pdfs_{job_id}.zip"
    return send_file(
        zip_path,
        mimetype="application/zip",
        as_attachment=True,
        download_name=zip_name,
    )


if __name__ == "__main__":
    app.run(debug=True)
