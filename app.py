import io
import os
import shutil
import time
import zipfile
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread
from uuid import uuid4

DEFAULT_PW_PATH = "/ms-playwright"
FALLBACK_PW_PATH = "/opt/render/project/src/.pw-browsers"
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = (
        DEFAULT_PW_PATH if Path(DEFAULT_PW_PATH).exists() else FALLBACK_PW_PATH
    )

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from authlib.integrations.flask_client import OAuth
from playwright.sync_api import sync_playwright

from download_traces import ensure_playwright_browsers, read_suppliers, run


APP_ROOT = Path(__file__).resolve().parent
RUNS_DIR = APP_ROOT / "web_runs"
ZIP_TTL_SECONDS = 24 * 60 * 60
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_MIME_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "text/plain",
}
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
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", os.urandom(24))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "1") == "1"

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def login_required(fn):
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    wrapper.__name__ = fn.__name__
    return wrapper


def cleanup_runs() -> None:
    cutoff = time.time() - ZIP_TTL_SECONDS
    if not RUNS_DIR.exists():
        return
    for run_dir in RUNS_DIR.iterdir():
        if not run_dir.is_dir():
            continue
        try:
            mtime = run_dir.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            shutil.rmtree(run_dir, ignore_errors=True)

    with JOBS_LOCK:
        stale_ids = []
        for job_id, job in JOBS.items():
            zip_path = Path(job.get("zip_path", ""))
            try:
                mtime = zip_path.stat().st_mtime
            except OSError:
                mtime = 0
            if mtime and mtime < cutoff:
                stale_ids.append(job_id)
        for job_id in stale_ids:
            JOBS.pop(job_id, None)


def cleanup_loop() -> None:
    while True:
        cleanup_runs()
        time.sleep(60 * 60)


Thread(target=cleanup_loop, daemon=True).start()


@app.get("/")
@login_required
def index():
    return render_template("index.html", job_id=None, error=None)


@app.post("/download")
@login_required
def download():
    upload = request.files.get("csv_file")
    if upload is None or upload.filename == "":
        return render_template("index.html", job_id=None, error="Please upload a CSV file.")
    if upload.mimetype not in ALLOWED_MIME_TYPES:
        return render_template("index.html", job_id=None, error="Invalid file type. Please upload a CSV.")
    if not upload.filename.lower().endswith(".csv"):
        return render_template("index.html", job_id=None, error="Invalid file name. Please upload a .csv file.")

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
            "cancel": False,
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

            def should_cancel() -> bool:
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    return bool(job and job.get("cancel"))

            ensure_playwright_browsers()
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
                    should_cancel=should_cancel,
                )

            if not out_dir.exists():
                update_job(job_id, status="error", error="No downloads were created.")
                return

            if should_cancel():
                update_job(job_id, status="cancelled")
                return
            create_zip_file(out_dir, zip_path)
            update_job(job_id, status="done")
        except Exception as exc:
            update_job(job_id, status="error", error=str(exc))

    Thread(target=worker, daemon=True).start()
    return render_template("index.html", job_id=job_id, error=None)


@app.get("/status/<job_id>")
@login_required
def status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"status": "missing"}), 404
        return jsonify(job)


@app.post("/cancel/<job_id>")
@login_required
def cancel(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"status": "missing"}), 404
        if job["status"] != "running":
            return jsonify({"status": job["status"]}), 400
        job["cancel"] = True
    return jsonify({"status": "cancelling"})


@app.get("/result/<job_id>")
@login_required
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


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
    )
    return response


@app.get("/login")
def login():
    if session.get("user"):
        return redirect(url_for("index"))
    redirect_uri = os.environ.get("OAUTH_REDIRECT_URL") or url_for("auth", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.get("/auth")
def auth():
    token = oauth.google.authorize_access_token()
    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = oauth.google.parse_id_token(token)
    session["user"] = {
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "picture": userinfo.get("picture"),
    }
    return redirect(url_for("index"))


@app.get("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=False)
