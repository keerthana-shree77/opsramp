"""OpsRamp Firmware Compliance and Comparison Portal - web layer."""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import uuid
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from flask import (  # noqa: E402  (import after load_dotenv by design)
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.utils import secure_filename  # noqa: E402

from config import Config, configure_logging  # noqa: E402
from firmware.models import STATUS_TONE  # noqa: E402
from firmware.normalizer import (  # noqa: E402
    CATEGORY_DISPLAY,
    SELECTABLE_CATEGORIES,
    normalize_category,
)
from jobs import JobStore, STATUS_DONE, STATUS_ERROR  # noqa: E402
from opsramp.client import OpsRampClient, OpsRampError  # noqa: E402
from opsramp.tenants import list_tenants  # noqa: E402
from overview import OverviewStore  # noqa: E402
from recipes import RecipeError, RecipeLibrary, clean_name  # noqa: E402
from store import Database  # noqa: E402
from recipe.loader import RecipeLoadError, load_recipe  # noqa: E402
from recipe.validator import Issue, validate_recipe  # noqa: E402
from reports import csv_report, json_report, summary as summary_report  # noqa: E402
from reports.excel_report import build_workbook  # noqa: E402
from service import Tenant  # noqa: E402

configure_logging()
logger = logging.getLogger("portal")

app = Flask(__name__)
app.config.from_object(Config)
if not app.config.get("SECRET_KEY"):
    # The app still starts so the operator sees the configuration page, but the
    # session key is ephemeral: restarting invalidates every login.
    app.config["SECRET_KEY"] = secrets.token_hex(32)
    logger.error("SECRET_KEY is not set - sessions will not survive a restart.")

if Config.TRUSTED_PROXY_HOPS:
    # Only when the operator has said how many proxies are in front. Applied
    # unconditionally it would let any caller claim, in a header, to have
    # arrived over HTTPS from somewhere else.
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=Config.TRUSTED_PROXY_HOPS,
        x_proto=Config.TRUSTED_PROXY_HOPS,
        x_host=Config.TRUSTED_PROXY_HOPS,
        x_prefix=Config.TRUSTED_PROXY_HOPS,
    )
    logger.info(
        "Trusting the forwarding headers of %d proxy hop(s).",
        Config.TRUSTED_PROXY_HOPS,
    )

jobs = JobStore(
    max_jobs=Config.MAX_JOBS, retention_seconds=Config.JOB_RETENTION_SECONDS
)

# The matrix on the home page is built by sweeping every account against every
# recipe in the library, so it is populated without anyone choosing a tenant or
# uploading a file. It is cached in SQLite, which is what lets the page render
# immediately on arrival and survive a restart.
database = Database(Config.CACHE_DB_PATH)
recipe_library = RecipeLibrary(Config, database)
overview_store = OverviewStore(
    Config,
    jobs,
    lambda: make_client(),
    lambda client: list_tenants(client),
    library=recipe_library,
    db=database,
)

_client: OpsRampClient | None = None


# --------------------------------------------------------------------- helpers


def make_client() -> OpsRampClient:
    """A dedicated client (own connection pool) for a background job."""
    return OpsRampClient(Config)


def get_client() -> OpsRampClient:
    """The shared client used by ordinary web requests."""
    global _client
    if _client is None:
        _client = OpsRampClient(Config)
    return _client


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def csrf_token() -> str:
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


@app.context_processor
def inject_globals():
    return {
        "csrf_token": csrf_token,
        "categories": SELECTABLE_CATEGORIES,
        "status_tone": STATUS_TONE,
        "app_title": "OpsRamp Firmware Compliance Portal",
    }


@app.before_request
def enforce_csrf():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("_csrf", "")
    if not expected or not hmac.compare_digest(str(sent), str(expected)):
        logger.warning("Rejected %s %s: CSRF token mismatch", request.method, request.path)
        abort(400, description="Your session expired. Please reload the page and retry.")
    return None


def session_owner() -> str:
    owner = session.get("_owner")
    if not owner:
        owner = uuid.uuid4().hex
        session["_owner"] = owner
    return owner


def config_problems() -> list[str]:
    return Config.missing_required()


# ---------------------------------------------------------------------- routes


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        expected_user = Config.APP_USERNAME
        expected_pass = Config.APP_PASSWORD
        ok = bool(expected_user) and bool(expected_pass)
        ok = ok and hmac.compare_digest(username, expected_user)
        ok = ok and hmac.compare_digest(password, expected_pass)
        if ok:
            session.clear()
            session["user"] = username
            session.permanent = True
            session_owner()
            logger.info("User %s signed in", username)
            target = request.args.get("next", "")
            if target.startswith("/") and not target.startswith("//"):
                return redirect(target)
            return redirect(url_for("new_comparison"))
        logger.warning("Failed sign-in attempt for user %r", username)
        flash("Invalid username or password.", "error")
    return render_template("login.html", problems=config_problems())


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    """Page 1: the compliance matrix, kept current by the background sweep."""
    return render_template(
        "home.html",
        overview=overview_store.snapshot(),
        library=recipe_library.describe(),
        problems=config_problems(),
    )


@app.route("/api/overview")
@login_required
def overview_state():
    """Polled by the matrix so cells fill in as each tenant finishes."""
    return jsonify(overview_store.snapshot())


def _measuring_note(state: str) -> str:
    """What the operator is told about when the figures will appear."""
    if state == "picked-up":
        return (
            "A sweep is already running: the accounts it has not reached yet "
            "are being measured against it now, and the rest the moment that "
            "sweep finishes."
        )
    if state == "queued":
        return (
            "A sweep is already running, so every account will be measured "
            "against it the moment that finishes."
        )
    return "Measuring every account against it from what has already been read."


def _and_list(names: list[str]) -> str:
    """"A", "A and B", "A, B and C" - for a sentence, not a table."""
    names = [n for n in names if n]
    if not names:
        return "nothing"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _checking_note(state: str, name: str) -> str:
    """What the operator is told about the check that follows a choice.

    The figures are already there - every account is measured against every
    recipe - so this is only about how soon the account is looked at again.
    """
    if state == "promoted":
        return (
            "A sweep is running and this account is now next in it, so it will "
            "be checked ahead of the others."
        )
    if state == "sweeping":
        return (
            "A sweep is running and reads every account, so this one is already "
            "being looked at."
        )
    if state == "checking":
        return "Checking it against OpsRamp now, ahead of the routine refresh."
    return ""


@app.route("/overview/recipe", methods=["POST"])
@login_required
def overview_recipe():
    """Judge every account by one recipe, in one go.

    The same choice the per-account dropdown makes, applied across the
    estate. It overwrites whatever each account was set to individually,
    which is what "for all accounts" has to mean to be worth having.
    """
    wanted = [r.strip() for r in request.form.getlist("recipe_id") if r.strip()]
    if "__new__" in wanted:
        flash(
            "Upload the new recipe below, then choose it for every account.",
            "success",
        )
        return redirect(url_for("index") + "#add-recipe")

    accepted, changed, state = overview_store.select_all(wanted)
    if not accepted:
        flash("One of those recipes is no longer in the library.", "error")
        return redirect(url_for("index"))

    if wanted:
        names = [r.name for r in recipe_library.all() if r.id in set(wanted)]
        headline = (
            f"Every account is now shown against {_and_list(names)}."
        )
    else:
        headline = (
            "Every account is back on automatic, and is shown against every "
            "recipe with the closest match marked."
        )
    if changed:
        headline += f" {changed} account(s) changed."
    else:
        headline += " They were all set to it already."
    flash(headline + " " + _applying_note(state), "success")
    return redirect(url_for("index"))


def _applying_note(state: str) -> str:
    """Whether anything had to run, and what.

    Nothing is re-read: every account is measured against every recipe as it
    is, so this is a change of view rather than of work.
    """
    if state == "ready":
        return "The figures were already worked out, so this took effect at once."
    if state == "started":
        return (
            "Some accounts had no figures against it yet - they are being "
            "measured now from what has already been read, which takes seconds "
            "and no calls to OpsRamp."
        )
    if state in ("queued", "picked-up"):
        return (
            "A sweep is running; the accounts without figures against it will "
            "have them when it finishes."
        )
    return ""


@app.route("/overview/<tenant_id>/recipe", methods=["POST"])
@login_required
def tenant_recipe(tenant_id: str):
    """Choose which recipe one account is judged by.

    The choice is per account and is posted to a URL that names it, so one
    account's choice can never be applied to another's row. It is remembered
    in the cache database, so it survives a restart.
    """
    wanted = [r.strip() for r in request.form.getlist("recipe_id") if r.strip()]
    if "__new__" in wanted:
        # The menu's own "add a new recipe" entry. With scripting this never
        # posts - the upload form is opened instead - so this is the
        # no-script path.
        flash(
            "Upload the new recipe below, then choose it for this account.",
            "success",
        )
        return redirect(url_for("index") + "#add-recipe")

    accepted, state = overview_store.select(tenant_id, wanted)
    if not accepted:
        flash("One of those recipes is no longer in the library.", "error")
        return redirect(url_for("index"))

    if wanted:
        names = [r.name for r in recipe_library.all() if r.id in set(wanted)]
        flash(
            f"This account is now shown against {_and_list(names)}, "
            + ("each with its own figures. " if len(names) > 1 else "")
            + _checking_note(state, ""),
            "success",
        )
    else:
        flash(
            "This account is back on automatic, and is shown against every "
            "recipe with the closest match marked. "
            + _checking_note(state, ""),
            "success",
        )
    return redirect(url_for("index"))


@app.route("/recipes", methods=["POST"])
@login_required
def recipe_add():
    """Add a recipe to the library, under a name the operator chooses.

    Several files may be uploaded together and become one recipe: the vendor
    publishes the monthly baseline as a CDC matrix and an S4HANA matrix which
    describe it between them, and held apart each would score badly on the
    equipment the other covers.
    """
    uploads = [f for f in request.files.getlist("recipe") if f and f.filename]
    if not uploads:
        flash("Choose a recipe file to upload.", "error")
        return redirect(url_for("index"))

    stored: list[tuple] = []
    errors: list = []
    warnings: list = []
    entries: list = []
    seen: set = set()
    names: list[str] = []

    try:
        for upload in uploads:
            filename, report, problems = _load_one_recipe(upload, keep=True)
            names.append(filename)
            errors.extend(problems)
            if report is None:
                continue
            if report.stored_path is not None:
                stored.append((report.stored_path, filename))
            warnings.extend(
                Issue(i.row, f"{filename}: {i.message}") for i in report.warnings
            )
            for entry in report.entries:
                key = (entry.category, entry.platform_key, entry.model_key,
                       entry.component_key)
                if key in seen:
                    warnings.append(
                        Issue(
                            entry.row_number,
                            f"{filename}: duplicate target for "
                            f"{entry.model or entry.platform} / {entry.component} "
                            "already supplied by an earlier file; the first one "
                            "is used.",
                        )
                    )
                    continue
                seen.add(key)
                entries.append(entry)

        if errors or not entries:
            if not errors:
                errors = [Issue(None, "No usable targets were found in the upload.")]
            logger.info("Recipe upload rejected with %d error(s)", len(errors))
            return render_template(
                "recipe_validation.html",
                filename=", ".join(names),
                errors=errors,
                warnings=warnings,
                report=None,
            ), 400

        name = clean_name(request.form.get("name", ""), names[0])
        try:
            ref, replaced = recipe_library.add(
                stored, name=name, targets=len(entries),
                uploaded_by=session.get("user", ""),
            )
        except (RecipeError, OSError) as exc:
            flash(f"The recipe could not be stored: {exc}", "error")
            return redirect(url_for("index"))
    finally:
        for path, _name in stored:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort cleanup
                logger.warning("Could not remove temporary upload %s", path.name)

    verb = "replaced by" if replaced else "added from"
    files = f"{len(stored)} files" if len(stored) > 1 else names[0]
    # Not a refresh: what is installed has not moved because a recipe arrived,
    # so the estate is measured again from the stored readings rather than
    # being swept from the beginning.
    flash(
        f"{ref.name} {verb} {files} ({ref.targets} targets). "
        + _measuring_note(overview_store.recompare()),
        "success",
    )
    return redirect(url_for("index"))


@app.route("/recipes/<recipe_id>/rename", methods=["POST"])
@login_required
def recipe_rename(recipe_id: str):
    try:
        ref = recipe_library.rename(recipe_id, request.form.get("name", ""))
    except RecipeError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Renamed to {ref.name}. " + _measuring_note(overview_store.recompare()),
              "success")
    return redirect(url_for("index"))


@app.route("/recipes/<recipe_id>/delete", methods=["POST"])
@login_required
def recipe_delete(recipe_id: str):
    name = recipe_library.remove(recipe_id)
    if name:
        # Any account pinned to it goes back to automatic: left pinned, it
        # would be judged by a name nobody can find any more.
        released = overview_store.forget_recipe(recipe_id)
        # Its cells go immediately; the re-measure settles the figures that
        # were derived from it, such as which recipe each account follows.
        note = ""
        if released:
            note = (
                f" {released} account(s) were set to it and are back on the "
                "closest match."
            )
        flash(
            f"{name} has been removed from the library, and its column has gone "
            "from the matrix." + note + " "
            + _measuring_note(overview_store.recompare()),
            "success",
        )
    else:
        flash("That recipe is no longer in the library.", "error")
    return redirect(url_for("index"))


@app.route("/overview/refresh", methods=["POST"])
@login_required
def overview_refresh():
    if not recipe_library.all():
        flash(
            "No approved recipe has been set. Upload one on this page first.",
            "error",
        )
    elif overview_store.refresh():
        logger.info("Compliance sweep requested by %s", session.get("user"))
    else:
        flash("A refresh is already running.", "success")
    return redirect(url_for("index"))


@app.route("/new")
@login_required
def new_comparison():
    """Page 2: choose tenants, categories and recipe files."""
    problems = config_problems()
    tenants: list[dict] = []
    error = ""
    if not problems:
        try:
            tenants = list_tenants(get_client())
        except OpsRampError as exc:
            error = str(exc)
            logger.error("Tenant discovery failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            error = "An unexpected error occurred while contacting OpsRamp."
            logger.exception("Tenant discovery crashed: %s", exc)
    return render_template(
        "index.html", tenants=tenants, error=error, problems=problems
    )


def _load_one_recipe(upload, keep: bool = False) -> tuple[str, object, list]:
    """Save, parse and validate a single upload. Returns (name, report, errors).

    With ``keep`` the stored file is left on disk and its path attached to the
    report, so a caller that wants to adopt the recipe has something to copy.
    The caller is then responsible for removing it.
    """
    filename = secure_filename(upload.filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in Config.ALLOWED_RECIPE_EXTENSIONS:
        return filename, None, [
            Issue(None, f"{filename}: unsupported file type. Upload .xlsx, .pdf, "
                        ".csv or .json.")
        ]

    upload_dir = Path(Config.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    # A generated name inside a fixed directory: the client-supplied name never
    # participates in the stored path, so traversal is impossible.
    stored = upload_dir / f"{uuid.uuid4().hex}{suffix}"
    try:
        upload.save(stored)
        if stored.stat().st_size == 0:
            return filename, None, [Issue(None, f"{filename}: the file is empty.")]
        raw = load_recipe(stored, source_name=filename)
    except RecipeLoadError as exc:
        return filename, None, [Issue(None, f"{filename}: {exc}")]
    except Exception as exc:  # noqa: BLE001
        logger.exception("Recipe upload %s failed: %s", filename, exc)
        return filename, None, [
            Issue(None, f"{filename}: the file could not be read. Check the format.")
        ]
    finally:
        if not keep:
            try:
                stored.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort cleanup
                logger.warning("Could not remove temporary upload %s", stored.name)

    report = validate_recipe(raw)
    report.stored_path = stored if keep else None
    errors = [Issue(i.row, f"{filename}: {i.message}") for i in report.errors]
    return filename, report, errors


@app.route("/compare", methods=["POST"])
@login_required
def compare():
    selected = [t.strip() for t in request.form.getlist("tenant_id") if t.strip()]
    names = {
        value.split("|", 1)[0]: value.split("|", 1)[1]
        for value in request.form.getlist("tenant_label")
        if "|" in value
    }
    categories = [
        normalize_category(c) or "all" for c in request.form.getlist("category")
    ] or ["all"]
    if "all" in categories:
        categories = ["all"]
    uploads = [f for f in request.files.getlist("recipe") if f and f.filename]

    if not selected:
        flash("Select at least one tenant before running the comparison.", "error")
        return redirect(url_for("new_comparison"))
    if not uploads:
        flash("Upload at least one firmware recipe file.", "error")
        return redirect(url_for("new_comparison"))

    entries: list = []
    all_errors: list = []
    all_warnings: list = []
    filenames: list[str] = []
    seen: set = set()
    for upload in uploads:
        filename, report, errors = _load_one_recipe(upload)
        filenames.append(filename)
        all_errors.extend(errors)
        if report is None:
            continue
        all_warnings.extend(
            Issue(i.row, f"{filename}: {i.message}") for i in report.warnings
        )
        for entry in report.entries:
            key = (entry.category, entry.platform_key, entry.model_key,
                   entry.component_key)
            if key in seen:
                all_warnings.append(
                    Issue(
                        entry.row_number,
                        f"{filename}: duplicate target for {entry.model or entry.platform}"
                        f" / {entry.component} already supplied by an earlier file; "
                        "the first one is used.",
                    )
                )
                continue
            seen.add(key)
            entries.append(entry)

    if all_errors or not entries:
        if not entries and not all_errors:
            all_errors.append(Issue(None, "No usable targets were found in the upload."))
        logger.info("Recipe upload rejected with %d error(s)", len(all_errors))
        return render_template(
            "recipe_validation.html",
            filename=", ".join(filenames),
            errors=all_errors,
            warnings=all_warnings,
            report=None,
        ), 400

    tenants = [Tenant(tid, names.get(tid, tid)) for tid in selected]
    recipe_label = ", ".join(filenames)
    job = jobs.create(
        tenant_id=tenants[0].id,
        tenant_name=tenants[0].name,
        category=categories[0],
        recipe_name=recipe_label,
        owner=session_owner(),
        tenants=tenants,
        categories=categories,
    )
    logger.info(
        "Job %s queued: %d tenant(s) categories=%s recipes=%s entries=%d",
        job.id,
        len(tenants),
        categories,
        filenames,
        len(entries),
    )
    jobs.start(job, make_client, entries)
    return redirect(url_for("processing", job_id=job.id))


@app.route("/jobs/<job_id>")
@login_required
def processing(job_id: str):
    job = jobs.get(job_id, owner=session_owner())
    if job is None:
        abort(404, description="That comparison job no longer exists.")
    if job.status == STATUS_DONE:
        return redirect(url_for("results", job_id=job.id))
    return render_template("processing.html", job=job)


@app.route("/api/jobs/<job_id>")
@login_required
def job_state(job_id: str):
    job = jobs.get(job_id, owner=session_owner())
    if job is None:
        return jsonify({"error": "not_found"}), 404
    state = job.public_state()
    if job.status == STATUS_DONE:
        state["redirect"] = url_for("results", job_id=job.id)
    return jsonify(state)


@app.route("/results/<job_id>")
@login_required
def results(job_id: str):
    job = jobs.get(job_id, owner=session_owner()) or overview_store.rebuild_job(job_id)
    if job is None:
        abort(404, description="That comparison job no longer exists.")
    if job.status == STATUS_ERROR:
        return render_template("error.html", message=job.error, job=job), 502
    if job.result is None:
        return redirect(url_for("processing", job_id=job.id))

    result = job.result
    return render_template(
        "results.html",
        job=job,
        result=result,
        rows=result.rows,
        cards=summary_report.summary_cards(result),
        options=summary_report.filter_options(result),
        compliance=summary_report.compliance_rate(result),
        text_report=summary_report.text_report(result),
    )


_DOWNLOADS = {
    "csv": ("comparison", "text/csv", lambda r: csv_report.comparison_csv(r), "csv"),
    "raw-csv": ("installed_raw", "text/csv", lambda r: csv_report.raw_csv(r), "csv"),
    "recipe-csv": ("recipe_data", "text/csv", lambda r: csv_report.recipe_csv(r), "csv"),
    "xlsx": (
        "firmware_compliance",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        lambda r: build_workbook(r),
        "xlsx",
    ),
    "json": ("firmware_compliance", "application/json", lambda r: json_report.build_json(r), "json"),
}


@app.route("/results/<job_id>/download/<fmt>")
@login_required
def download(job_id: str, fmt: str):
    job = jobs.get(job_id, owner=session_owner()) or overview_store.rebuild_job(job_id)
    if job is None or job.result is None:
        abort(404, description="That comparison result is no longer available.")
    spec = _DOWNLOADS.get(fmt)
    if spec is None:
        abort(404)
    stem, mimetype, builder, extension = spec
    try:
        payload = builder(job.result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Export %s failed for job %s: %s", fmt, job_id, exc)
        abort(500, description="The report could not be generated.")

    tenant_slug = secure_filename(job.tenant_name) or "tenant"
    filename = f"{stem}_{tenant_slug}_{job.id[:8]}.{extension}"
    return Response(
        payload,
        mimetype=mimetype,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


# --------------------------------------------------------------- error handling


@app.after_request
def security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'",
    )
    return response


@app.errorhandler(400)
def handle_400(exc):
    return render_template("error.html", message=getattr(exc, "description", "Bad request.")), 400


@app.errorhandler(403)
def handle_403(exc):
    return render_template("error.html", message="You are not allowed to view that."), 403


@app.errorhandler(404)
def handle_404(exc):
    return render_template(
        "error.html", message=getattr(exc, "description", "Page not found.")
    ), 404


@app.errorhandler(413)
def handle_413(exc):
    limit_mb = Config.MAX_CONTENT_LENGTH / (1024 * 1024)
    return render_template(
        "error.html",
        message=f"The uploaded file is too large. The limit is {limit_mb:.0f} MB.",
    ), 413


@app.errorhandler(500)
def handle_500(exc):
    logger.exception("Unhandled application error: %s", exc)
    return render_template(
        "error.html",
        message=(
            "An unexpected error occurred. The details have been recorded in the "
            "application log."
        ),
    ), 500


overview_store.start_background()


if __name__ == "__main__":
    missing = Config.missing_required()
    if missing:
        logger.error(
            "Missing required configuration: %s. See .env.example.", ", ".join(missing)
        )
    debug = os.getenv("FLASK_DEBUG", "").lower() in {"1", "true", "yes"}
    if debug:
        # Cookies cannot be Secure-only when developing over plain HTTP.
        app.config["SESSION_COOKIE_SECURE"] = False
    app.run(
        host=os.getenv("BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
        debug=debug,
        threaded=True,
    )
