import csv
import functools
import hashlib
import io
import os
import re
import secrets
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from email.utils import parseaddr
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash

from notifications import (
    log_notification,
    notify_medicine,
    provider_status,
    read_settings,
    configured_reminder_days,
    send_notification,
    start_scheduler,
    validate_settings,
)


LOW_STOCK_THRESHOLD = 10


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("current_user") is None:
            if request.path.startswith("/api/"):
                return api_error("Sign in to use MediTrack.", 401)
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def management_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.current_user["role"] != "management":
            return api_error("Management access is required.", 403)
        return view(*args, **kwargs)

    return wrapped


def inventory_manager_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not is_inventory_manager(g.current_user):
            return api_error("Branch staff or management access is required.", 403)
        return view(*args, **kwargs)

    return wrapped


def is_branch_staff(user):
    return bool(user and user["role"] == "user" and user["is_branch_staff"])


def is_inventory_manager(user):
    return bool(user and (user["role"] == "management" or is_branch_staff(user)))


def create_app(test_config=None):
    load_dotenv()
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        DATABASE=os.environ.get(
            "DATABASE_PATH", os.path.join(app.instance_path, "medicines.db")
        ),
        SAMPLE_DATA=True,
        MANAGEMENT_EMAIL=os.environ.get("MANAGEMENT_EMAIL", ""),
        MANAGEMENT_PASSWORD=os.environ.get("MANAGEMENT_PASSWORD", ""),
        APP_BASE_URL=os.environ.get("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/"),
        SMTP_HOST=os.environ.get("SMTP_HOST", ""),
        SMTP_PORT=int(os.environ.get("SMTP_PORT", "587")),
        SMTP_USERNAME=os.environ.get("SMTP_USERNAME", ""),
        SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD", ""),
        SMTP_SENDER=os.environ.get("SMTP_SENDER", ""),
        SMTP_USE_TLS=os.environ.get("SMTP_USE_TLS", "true").lower() == "true",
        TWILIO_ACCOUNT_SID=os.environ.get("TWILIO_ACCOUNT_SID", ""),
        TWILIO_AUTH_TOKEN=os.environ.get("TWILIO_AUTH_TOKEN", ""),
        TWILIO_WHATSAPP_FROM=os.environ.get("TWILIO_WHATSAPP_FROM", ""),
        TWILIO_CONTENT_SID=os.environ.get("TWILIO_CONTENT_SID", ""),
        REMINDER_HOUR=int(os.environ.get("REMINDER_HOUR", "9")),
        REMINDER_TIMEZONE=os.environ.get("REMINDER_TIMEZONE", "UTC"),
        LOW_STOCK_THRESHOLD=LOW_STOCK_THRESHOLD,
        SCHEDULER_ENABLED=os.environ.get("SCHEDULER_ENABLED", "true").lower() == "true",
    )
    if test_config:
        app.config.update(test_config)
    if not 0 <= app.config["REMINDER_HOUR"] <= 23:
        raise ValueError("REMINDER_HOUR must be an integer between 0 and 23.")

    @app.teardown_appcontext
    def close_database(_error=None):
        connection = g.pop("database", None)
        if connection is not None:
            connection.close()

    os.makedirs(app.instance_path, exist_ok=True)
    database_path = app.config["DATABASE"]
    if database_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(database_path)), exist_ok=True)

    with app.app_context():
        initialize_database()

    @app.before_request
    def load_current_user():
        user_id = session.get("user_id")
        g.current_user = None
        if user_id:
            user = get_database().execute(
                """SELECT users.id, users.name, users.email, users.role, users.auth_version,
                          users.branch_id, users.is_branch_staff, branches.name AS branch_name
                   FROM users LEFT JOIN branches ON branches.id = users.branch_id
                   WHERE users.id = ?""",
                (user_id,),
            ).fetchone()
            if user and session.get("auth_version") == user["auth_version"]:
                g.current_user = user
            else:
                session.clear()
        if user_id and g.current_user is None:
            session.clear()

    @app.get("/api/session")
    def session_info():
        if g.current_user is None:
            return api_error("Sign in to use MediTrack.", 401)
        return jsonify(
            {
                "user": {
                    "id": g.current_user["id"],
                    "name": g.current_user["name"],
                    "email": g.current_user["email"],
                    "role": g.current_user["role"],
                    "branch_name": g.current_user["branch_name"],
                }
            }
        )

    @app.get("/login")
    def login():
        if g.current_user:
            return redirect(url_for("dashboard"))
        return render_template(
            "login.html", register=False, error=None, values={}
        )

    @app.post("/login")
    def login_submit():
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_database().execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)
        ).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            return render_template(
                "login.html",
                register=False,
                error="Email or password is incorrect.",
                values={"email": email},
            ), 401
        session.clear()
        session["user_id"] = user["id"]
        session["auth_version"] = user["auth_version"]
        session.permanent = True
        return redirect(url_for("dashboard"))

    @app.get("/register")
    def register():
        if g.current_user:
            return redirect(url_for("dashboard"))
        return render_template(
            "login.html", register=True, error=None, values={}
        )

    @app.post("/register")
    def register_submit():
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        expiry_email_enabled = request.form.get("expiry_email_enabled") == "on"
        values = {"name": name, "email": email}
        if not name or len(name) > 100:
            return render_template(
                "login.html",
                register=True,
                error="Enter your name (100 characters or fewer).",
                values=values,
            ), 400
        if not valid_email(email):
            return render_template(
                "login.html", register=True, error="Enter a valid email address.", values=values
            ), 400
        if len(password) < 10:
            return render_template(
                "login.html",
                register=True,
                error="Use a password with at least 10 characters.",
                values=values,
            ), 400
        if password != confirm_password:
            return render_template(
                "login.html",
                register=True,
                error="The passwords do not match.",
                values=values,
            ), 400
        connection = get_database()
        try:
            cursor = connection.execute(
                """INSERT INTO users (name, email, password_hash, role, expiry_email_enabled)
                   VALUES (?, ?, ?, 'user', ?)""",
                (name, email, generate_password_hash(password), int(expiry_email_enabled)),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            return render_template(
                "login.html",
                register=True,
                error="An account already uses that email address.",
                values=values,
            ), 409
        session.clear()
        session["user_id"] = cursor.lastrowid
        session["auth_version"] = 0
        session.permanent = True
        return redirect(url_for("dashboard"))

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if request.method == "GET":
            return render_template(
                "login.html",
                register=False,
                forgot=True,
                reset=False,
                error=None,
                values={},
                reset_requested=False,
                reset_email_ready=provider_status()["email"]["ready"],
            )

        email = request.form.get("email", "").strip().lower()
        if valid_email(email):
            connection = get_database()
            user = connection.execute(
                "SELECT id, email FROM users WHERE email = ? COLLATE NOCASE",
                (email,),
            ).fetchone()
            if user and provider_status()["email"]["ready"]:
                token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                expires_at = (
                    datetime.now(ZoneInfo("UTC")) + timedelta(minutes=30)
                ).replace(tzinfo=None).isoformat()
                connection.execute(
                    "DELETE FROM password_reset_tokens WHERE user_id = ?",
                    (user["id"],),
                )
                connection.execute(
                    """INSERT INTO password_reset_tokens (user_id, token_hash, expires_at)
                       VALUES (?, ?, ?)""",
                    (user["id"], token_hash, expires_at),
                )
                connection.commit()
                reset_url = (
                    f"{current_app.config['APP_BASE_URL']}"
                    f"{url_for('reset_password', token=token)}"
                )
                try:
                    send_notification(
                        "email",
                        user["email"],
                        "Reset your MediTrack password",
                        "A password reset was requested for your MediTrack account.\n\n"
                        "Choose a new password within 30 minutes using this one-time link:\n"
                        f"{reset_url}\n\n"
                        "If you did not request a password reset, you can ignore this email.",
                    )
                except Exception:
                    current_app.logger.exception(
                        "Password reset email delivery failed for user_id=%s", user["id"]
                    )
                    connection.execute(
                        "DELETE FROM password_reset_tokens WHERE token_hash = ?",
                        (token_hash,),
                    )
                    connection.commit()
            elif user:
                current_app.logger.warning(
                    "Password reset requested but SMTP is not configured for user_id=%s",
                    user["id"],
                )

        return render_template(
            "login.html",
            register=False,
            forgot=True,
            reset=False,
            error=None,
            values={},
            reset_requested=True,
            reset_email_ready=provider_status()["email"]["ready"],
        )

    @app.route("/reset-password/<token>", methods=["GET", "POST"])
    def reset_password(token):
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        connection = get_database()
        reset_record = connection.execute(
            """SELECT id, user_id FROM password_reset_tokens
               WHERE token_hash = ? AND expires_at > ?""",
            (token_hash, datetime.now(ZoneInfo("UTC")).replace(tzinfo=None).isoformat()),
        ).fetchone()
        if reset_record is None:
            return render_template(
                "login.html",
                register=False,
                forgot=False,
                reset=True,
                error="This password reset link is invalid or has expired. Request a new one.",
                values={},
                reset_invalid=True,
            ), 400
        if request.method == "GET":
            return render_template(
                "login.html",
                register=False,
                forgot=False,
                reset=True,
                error=None,
                values={},
                reset_invalid=False,
            )

        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        error = None
        if len(password) < 10:
            error = "Use a password with at least 10 characters."
        elif password != confirm_password:
            error = "The passwords do not match."
        if error:
            return render_template(
                "login.html",
                register=False,
                forgot=False,
                reset=True,
                error=error,
                values={},
                reset_invalid=False,
            ), 400

        connection.execute(
            "UPDATE users SET password_hash = ?, auth_version = auth_version + 1 WHERE id = ?",
            (generate_password_hash(password), reset_record["user_id"]),
        )
        connection.execute(
            "DELETE FROM password_reset_tokens WHERE user_id = ?",
            (reset_record["user_id"],),
        )
        connection.commit()
        session.clear()
        return redirect(url_for("login", password_reset="1"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/api/my/notifications", methods=["GET", "POST"])
    @login_required
    def personal_notification_settings():
        if g.current_user["role"] != "user" or is_branch_staff(g.current_user):
            return api_error("This notification setting is for medicine-user accounts.", 403)
        connection = get_database()
        if request.method == "POST":
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return api_error("Provide valid email notification preferences.", 400)
            email_enabled = payload.get("email_enabled")
            weekly_digest_enabled = payload.get("weekly_digest_enabled")
            if email_enabled is not None and not isinstance(email_enabled, bool):
                return api_error("Choose whether to receive email expiry reminders.", 400)
            if weekly_digest_enabled is not None and not isinstance(weekly_digest_enabled, bool):
                return api_error("Choose whether to receive a weekly email digest.", 400)
            if email_enabled is None and weekly_digest_enabled is None:
                return api_error("Choose at least one email notification preference.", 400)
            if email_enabled is not None:
                connection.execute(
                    "UPDATE users SET expiry_email_enabled = ? WHERE id = ?",
                    (int(email_enabled), g.current_user["id"]),
                )
                connection.execute(
                    "UPDATE medicines SET notify_email = ? WHERE owner_id = ?",
                    (int(email_enabled), g.current_user["id"]),
                )
            if weekly_digest_enabled is not None:
                connection.execute(
                    "UPDATE users SET weekly_digest_enabled = ? WHERE id = ?",
                    (int(weekly_digest_enabled), g.current_user["id"]),
                )
            connection.commit()
        preferences = connection.execute(
            "SELECT email, expiry_email_enabled, weekly_digest_enabled FROM users WHERE id = ?",
            (g.current_user["id"],),
        ).fetchone()
        return jsonify(
            {
                "email_enabled": bool(preferences["expiry_email_enabled"]),
                "weekly_digest_enabled": bool(preferences["weekly_digest_enabled"]),
                "email": preferences["email"],
                "email_provider_ready": provider_status()["email"]["ready"],
                "reminder_days": sorted(configured_reminder_days(connection), reverse=True),
                "browser_supported": True,
                "message": "Expiry reminders only; no stock or management alerts.",
            }
        )

    @app.get("/")
    @login_required
    def dashboard():
        branch_staff = is_branch_staff(g.current_user)
        return render_template(
            "index.html",
            user=dict(g.current_user),
            is_management=is_inventory_manager(g.current_user),
            is_global_management=g.current_user["role"] == "management",
            branch_staff=branch_staff,
        )

    @app.get("/api/branches")
    @login_required
    def list_branches():
        connection = get_database()
        if g.current_user["role"] == "management":
            rows = connection.execute(
                """SELECT branches.id, branches.name, branches.notification_email,
                          COUNT(users.id) AS staff_count
                   FROM branches LEFT JOIN users
                     ON users.branch_id = branches.id AND users.is_branch_staff = 1
                   GROUP BY branches.id ORDER BY branches.name COLLATE NOCASE"""
            ).fetchall()
            return jsonify({"branches": [dict(row) for row in rows]})
        if is_branch_staff(g.current_user):
            branch = connection.execute(
                "SELECT id, name, notification_email FROM branches WHERE id = ?",
                (g.current_user["branch_id"],),
            ).fetchone()
            return jsonify({"branches": [dict(branch)] if branch else []})
        return api_error("Branch access is only available to management and branch staff.", 403)

    @app.post("/api/branches")
    @login_required
    @management_required
    def create_branch():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return api_error("Provide branch details as a JSON object.", 400)
        name = payload.get("name", "")
        email = payload.get("notification_email", "")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            return api_error("Enter a branch name (100 characters or fewer).", 400)
        if not isinstance(email, str) or not valid_email(email.strip().lower()):
            return api_error("Enter a valid branch notification email.", 400)
        connection = get_database()
        try:
            cursor = connection.execute(
                """INSERT INTO branches (name, notification_email)
                   VALUES (?, ?)""",
                (name.strip(), email.strip().lower()),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            return api_error("A branch with that name already exists.", 409)
        branch = connection.execute(
            "SELECT id, name, notification_email FROM branches WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return jsonify({"branch": dict(branch)}), 201

    @app.post("/api/branch-staff")
    @login_required
    @management_required
    def create_branch_staff():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return api_error("Provide staff account details as a JSON object.", 400)
        name = payload.get("name", "")
        email = payload.get("email", "")
        password = payload.get("password", "")
        branch_id = payload.get("branch_id")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            return api_error("Enter the staff member's name (100 characters or fewer).", 400)
        if not isinstance(email, str) or not valid_email(email.strip().lower()):
            return api_error("Enter a valid staff email address.", 400)
        if not isinstance(password, str) or len(password) < 10:
            return api_error("Use a temporary password with at least 10 characters.", 400)
        if isinstance(branch_id, bool) or not isinstance(branch_id, int):
            return api_error("Choose a valid branch for this staff member.", 400)
        connection = get_database()
        if connection.execute("SELECT 1 FROM branches WHERE id = ?", (branch_id,)).fetchone() is None:
            return api_error("Branch not found.", 404)
        try:
            cursor = connection.execute(
                """INSERT INTO users
                   (name, email, password_hash, role, branch_id, is_branch_staff)
                   VALUES (?, ?, ?, 'user', ?, 1)""",
                (name.strip(), email.strip().lower(), generate_password_hash(password), branch_id),
            )
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            return api_error("An account already uses that email address.", 409)
        staff = connection.execute(
            """SELECT users.id, users.name, users.email, branches.name AS branch_name
               FROM users JOIN branches ON branches.id = users.branch_id
               WHERE users.id = ?""",
            (cursor.lastrowid,),
        ).fetchone()
        return jsonify({"staff": dict(staff)}), 201

    @app.get("/api/medicines")
    @login_required
    def list_medicines():
        search = request.args.get("q", "").strip()
        status_filter = request.args.get("status", "all")
        connection = get_database()
        query = """SELECT medicines.*, branches.name AS branch_name
                   FROM medicines LEFT JOIN branches ON branches.id = medicines.branch_id"""
        conditions = []
        parameters = []
        if g.current_user["role"] == "management":
            branch_filter = request.args.get("branch_id", "").strip()
            if branch_filter:
                try:
                    conditions.append("medicines.branch_id = ?")
                    parameters.append(int(branch_filter))
                except ValueError:
                    return api_error("Choose a valid branch filter.", 400)
        elif is_branch_staff(g.current_user):
            conditions.append("medicines.branch_id = ?")
            parameters.append(g.current_user["branch_id"])
        else:
            conditions.append("owner_id = ?")
            parameters.append(g.current_user["id"])

        if search:
            conditions.append(
                "(medicines.name LIKE ? COLLATE NOCASE OR medicines.batch_number LIKE ? COLLATE NOCASE)"
            )
            pattern = f"%{search}%"
            parameters.extend((pattern, pattern))

        is_management = is_inventory_manager(g.current_user)
        if not is_management and status_filter not in ("all", "expired", "7", "15", "30", "90", "healthy"):
            return api_error("Medicine users can filter by expiry status only.", 403)
        if status_filter == "low-stock":
            conditions.append("medicines.quantity <= ?")
            parameters.append(LOW_STOCK_THRESHOLD)
        elif status_filter not in ("all", "expired", "7", "15", "30", "90", "healthy", "alerts"):
            return api_error("Choose a valid medicine filter.", 400)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY medicines.expiry_date ASC, medicines.name COLLATE NOCASE ASC"
        medicines = [serialize_medicine(row) for row in connection.execute(query, parameters)]

        if status_filter == "alerts":
            medicines = [
                medicine
                for medicine in medicines
                if medicine["status"] != "healthy" or medicine["low_stock"]
            ]
        elif status_filter in ("expired", "healthy"):
            medicines = [medicine for medicine in medicines if medicine["status"] == status_filter]
        elif status_filter in ("7", "15", "30", "90"):
            window_days = int(status_filter)
            medicines = [
                medicine
                for medicine in medicines
                if medicine["status"] not in ("expired", "healthy")
                and int(medicine["status"]) <= window_days
            ]

        alert_medicines = [
            serialize_medicine(row)
            for row in connection.execute(
                """SELECT medicines.*, branches.name AS branch_name FROM medicines
                   LEFT JOIN branches ON branches.id = medicines.branch_id"""
                + (
                    " WHERE medicines.branch_id = ?" if is_branch_staff(g.current_user)
                    else " WHERE medicines.owner_id = ?" if not is_management
                    else " WHERE medicines.branch_id = ?" if request.args.get("branch_id")
                    else ""
                ),
                (
                    (g.current_user["branch_id"],) if is_branch_staff(g.current_user)
                    else (g.current_user["id"],) if not is_management
                    else (int(request.args["branch_id"]),) if request.args.get("branch_id")
                    else ()
                ),
            )
        ]
        alert_medicines = [
            medicine
            for medicine in alert_medicines
            if (
                medicine["status"] != "healthy"
                if not is_management
                else medicine["status"] != "healthy" or medicine["low_stock"]
            )
        ]
        if not is_management:
            medicines = [medicine_user_view(medicine) for medicine in medicines]
            alert_medicines = [medicine_user_view(medicine) for medicine in alert_medicines]
        return jsonify(
            {
                "medicines": medicines,
                "alerts": alert_medicines,
                "stats": get_statistics(connection, g.current_user, request.args.get("branch_id")),
                "role": g.current_user["role"],
                "low_stock_threshold": LOW_STOCK_THRESHOLD,
            }
        )

    @app.post("/api/medicines")
    @login_required
    def create_medicine():
        if contains_photo_upload():
            return api_error("Photo uploads are disabled.", 400)
        management = is_inventory_manager(g.current_user)
        if management:
            raw_payload, form_error = management_medicine_payload()
            if form_error:
                return api_error(form_error, 400)
            payload, error = validate_medicine_payload(raw_payload)
        else:
            raw_payload = request.get_json(silent=True) if request.is_json else request.form
            payload, error = validate_user_medicine_payload(raw_payload)
        if error:
            return api_error(error, 400)

        connection = get_database()
        owner_id = g.current_user["id"]
        if is_branch_staff(g.current_user):
            branch_id = g.current_user["branch_id"]
        else:
            branch_id = parse_optional_branch_id(raw_payload.get("branch_id") if management else None)
            if management and raw_payload.get("branch_id") not in (None, "") and branch_id is None:
                return api_error("Choose a valid branch.", 400)
        if branch_id is not None and connection.execute(
            "SELECT 1 FROM branches WHERE id = ?", (branch_id,)
        ).fetchone() is None:
            return api_error("Branch not found.", 404)
        if management:
            uploader = {
                "uploader_name": payload["uploader_name"],
                "uploader_email": payload["uploader_email"],
                "uploader_whatsapp": payload["uploader_whatsapp"],
                "notify_email": int(payload["notify_email"]),
                "notify_whatsapp": int(payload["notify_whatsapp"]),
            }
        else:
            user_preferences = connection.execute(
                "SELECT email, expiry_email_enabled FROM users WHERE id = ?",
                (owner_id,),
            ).fetchone()
            uploader = {
                "uploader_name": g.current_user["name"],
                "uploader_email": payload["uploader_email"],
                "uploader_whatsapp": payload["uploader_whatsapp"],
                "notify_email": user_preferences["expiry_email_enabled"],
                "notify_whatsapp": int(payload["notify_whatsapp"]),
            }
        cursor = connection.execute(
            """INSERT INTO medicines
               (name, batch_number, quantity, expiry_date, uploader_name, uploader_email,
                uploader_whatsapp, notify_email, notify_whatsapp, owner_id, branch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload["name"], payload["batch_number"], payload["quantity"],
                payload["expiry_date"], uploader["uploader_name"],
                uploader["uploader_email"], uploader["uploader_whatsapp"],
                uploader["notify_email"], uploader["notify_whatsapp"],
                None if is_branch_staff(g.current_user) else owner_id, branch_id,
            ),
        )
        connection.commit()
        medicine = connection.execute(
            """SELECT medicines.*, branches.name AS branch_name,
                      branches.notification_email AS branch_email
               FROM medicines LEFT JOIN branches ON branches.id = medicines.branch_id
               WHERE medicines.id = ?""",
            (cursor.lastrowid,),
        ).fetchone()
        notifications = (
            notify_medicine(connection, medicine, "added")
            if management
            else send_user_expiry_notice(connection, medicine)
        )
        result = serialize_medicine(medicine)
        if not management:
            result = medicine_user_view(result)
        return jsonify(
            {"medicine": result, "notifications": notifications}
        ), 201

    @app.put("/api/medicines/<int:medicine_id>")
    @login_required
    @inventory_manager_required
    def update_medicine(medicine_id):
        raw_payload, form_error = management_medicine_payload()
        if form_error:
            return api_error(form_error, 400)
        payload, error = validate_medicine_payload(raw_payload)
        if error:
            return api_error(error, 400)
        if contains_photo_upload():
            return api_error("Photo uploads are disabled.", 400)

        connection = get_database()
        existing_query = "SELECT id, branch_id FROM medicines WHERE id = ?"
        existing_parameters = [medicine_id]
        if is_branch_staff(g.current_user):
            existing_query += " AND branch_id = ?"
            existing_parameters.append(g.current_user["branch_id"])
        existing = connection.execute(existing_query, existing_parameters).fetchone()
        if existing is None:
            return api_error("Medicine not found.", 404)
        if is_branch_staff(g.current_user):
            branch_id = g.current_user["branch_id"]
        else:
            branch_id = parse_optional_branch_id(
                raw_payload.get("branch_id", existing["branch_id"])
            )
            if raw_payload.get("branch_id") not in (None, "") and branch_id is None:
                return api_error("Choose a valid branch.", 400)
        if branch_id is not None and connection.execute(
            "SELECT 1 FROM branches WHERE id = ?", (branch_id,)
        ).fetchone() is None:
            return api_error("Branch not found.", 404)
        cursor = connection.execute(
            """UPDATE medicines
               SET name = ?, batch_number = ?, quantity = ?, expiry_date = ?,
                   uploader_name = ?, uploader_email = ?, uploader_whatsapp = ?,
                   notify_email = ?, notify_whatsapp = ?, branch_id = ?
               WHERE id = ?""",
            (
                payload["name"],
                payload["batch_number"],
                payload["quantity"],
                payload["expiry_date"],
                payload["uploader_name"],
                payload["uploader_email"],
                payload["uploader_whatsapp"],
                int(payload["notify_email"]),
                int(payload["notify_whatsapp"]),
                branch_id,
                medicine_id,
            ),
        )
        if cursor.rowcount == 0:
            connection.rollback()
            return api_error("Medicine not found.", 404)
        connection.commit()
        medicine = connection.execute(
            """SELECT medicines.*, branches.name AS branch_name,
                      branches.notification_email AS branch_email
               FROM medicines LEFT JOIN branches ON branches.id = medicines.branch_id
               WHERE medicines.id = ?""",
            (medicine_id,),
        ).fetchone()
        notifications = notify_medicine(connection, medicine, "updated")
        return jsonify(
            {"medicine": serialize_medicine(medicine), "notifications": notifications}
        )

    @app.delete("/api/medicines/<int:medicine_id>")
    @login_required
    def delete_medicine(medicine_id):
        connection = get_database()
        query = "SELECT id FROM medicines WHERE id = ?"
        parameters = [medicine_id]
        if is_branch_staff(g.current_user):
            query += " AND branch_id = ?"
            parameters.append(g.current_user["branch_id"])
        elif g.current_user["role"] != "management":
            query += " AND owner_id = ?"
            parameters.append(g.current_user["id"])
        medicine = connection.execute(query, parameters).fetchone()
        if medicine is None:
            return api_error("Medicine not found.", 404)
        cursor = connection.execute("DELETE FROM medicines WHERE id = ?", (medicine_id,))
        if cursor.rowcount == 0:
            connection.rollback()
            return api_error("Medicine not found.", 404)
        connection.commit()
        return jsonify({"message": "Medicine deleted."})

    @app.get("/api/export")
    @login_required
    @inventory_manager_required
    def export_medicines():
        connection = get_database()
        query = """SELECT medicines.*, branches.name AS branch_name
                   FROM medicines LEFT JOIN branches ON branches.id = medicines.branch_id"""
        parameters = ()
        if is_branch_staff(g.current_user):
            query += " WHERE medicines.branch_id = ?"
            parameters = (g.current_user["branch_id"],)
        elif request.args.get("branch_id"):
            branch_id = parse_optional_branch_id(request.args["branch_id"])
            if branch_id is None:
                return api_error("Choose a valid branch filter.", 400)
            query += " WHERE medicines.branch_id = ?"
            parameters = (branch_id,)
        query += " ORDER BY expiry_date ASC, name COLLATE NOCASE ASC"
        rows = connection.execute(
            query, parameters
        ).fetchall()
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(
            ("Medicine", "Batch number", "Branch", "Quantity", "Expiry date", "Status", "Days remaining")
        )
        for row in rows:
            medicine = serialize_medicine(row)
            writer.writerow(
                (
                    medicine["name"],
                    medicine["batch_number"],
                    medicine.get("branch_name") or "",
                    medicine["quantity"],
                    medicine["expiry_date"],
                    medicine["status_label"],
                    medicine["days_remaining"],
                )
            )
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": 'attachment; filename="medicine-inventory.csv"'},
        )

    @app.route("/api/notifications/settings", methods=["GET", "POST"])
    @login_required
    @management_required
    def notification_settings():
        connection = get_database()
        if request.method == "POST":
            settings, error = validate_settings(request.get_json(silent=True))
            if error:
                return api_error(error, 400)
            connection.executemany(
                """INSERT INTO notification_settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                list(settings.items()),
            )
            connection.commit()
        settings = read_settings(connection)

        return jsonify(
            {
                "settings": {
                    "reminder_days": sorted(configured_reminder_days(connection), reverse=True),
                    "weekly_digest_enabled": settings["weekly_digest_enabled"] == "1",
                    "email_enabled": settings["email_enabled"] == "1",
                    "whatsapp_enabled": settings["whatsapp_enabled"] == "1",
                },
                "providers": provider_status(),
                "history": get_notification_history(connection),
            }
        )

    @app.post("/api/notifications/test")
    @login_required
    @management_required
    def test_notifications():
        payload, error = validate_uploader_contact(request.get_json(silent=True))
        if error:
            return api_error(error, 400)
        connection = get_database()
        settings = read_settings(connection)
        providers = provider_status()
        targets = [
            (channel, payload[key])
            for channel, key in (("email", "uploader_email"), ("whatsapp", "uploader_whatsapp"))
            if payload[key] and settings[f"{channel}_enabled"] == "1" and providers[channel]["ready"]
        ]
        if not targets:
            return api_error(
                "Enable a channel, enter a test contact, and configure its provider before sending a test.",
                400,
            )
        timestamp = datetime.now().astimezone().isoformat(timespec="microseconds")
        results = []
        for channel, recipient in targets:
            result = {
                "channel": channel,
                "recipient": recipient,
                "status": "sent",
                "message": "Sent",
            }
            cursor = connection.execute(
                """INSERT INTO notification_logs
                   (medicine_name, batch_number, channel, event, event_date, recipient, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                ("MediTrack", "TEST", channel, "test", timestamp, recipient),
            )
            connection.commit()
            try:
                send_notification(
                    channel,
                    recipient,
                    "MediTrack notification test",
                    "Test notification: your MediTrack medicine expiry alerts are connected.",
                )
            except Exception as error:
                current_app.logger.exception("Test notification delivery failed: channel=%s", channel)
                result.update(status="failed", message=str(error))
                connection.execute(
                    "UPDATE notification_logs SET status = 'failed', error = ? WHERE id = ?",
                    (str(error)[:500], cursor.lastrowid),
                )
            else:
                connection.execute(
                    "UPDATE notification_logs SET status = 'sent' WHERE id = ?",
                    (cursor.lastrowid,),
                )
            connection.commit()
            results.append(result)
        return jsonify({"results": results, "history": get_notification_history(connection)})

    return app


def get_database():
    if "database" not in g:
        g.database = sqlite3.connect(current_app.config["DATABASE"])
        g.database.row_factory = sqlite3.Row
        g.database.execute("PRAGMA foreign_keys = ON")
    return g.database


def initialize_database():
    connection = get_database()
    connection.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'management')),
            expiry_email_enabled INTEGER NOT NULL DEFAULT 0,
            branch_id INTEGER REFERENCES branches(id),
            is_branch_staff INTEGER NOT NULL DEFAULT 0,
            auth_version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    user_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(users)")
    }
    if "auth_version" not in user_columns:
        connection.execute(
            "ALTER TABLE users ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS password_reset_tokens_expiry "
        "ON password_reset_tokens (expires_at)"
    )
    user_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(users)")
    }
    if "expiry_email_enabled" not in user_columns:
        connection.execute(
            "ALTER TABLE users ADD COLUMN expiry_email_enabled INTEGER NOT NULL DEFAULT 0"
        )
    if "weekly_digest_enabled" not in user_columns:
        connection.execute(
            "ALTER TABLE users ADD COLUMN weekly_digest_enabled INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            notification_email TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
    if "branch_id" not in user_columns:
        connection.execute(
            "ALTER TABLE users ADD COLUMN branch_id INTEGER REFERENCES branches(id)"
        )
    if "is_branch_staff" not in user_columns:
        connection.execute(
            "ALTER TABLE users ADD COLUMN is_branch_staff INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS medicines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            batch_number TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity >= 0),
            expiry_date TEXT NOT NULL,
            uploader_name TEXT NOT NULL DEFAULT '',
            uploader_email TEXT NOT NULL DEFAULT '',
            uploader_whatsapp TEXT NOT NULL DEFAULT '',
            notify_email INTEGER NOT NULL DEFAULT 0,
            notify_whatsapp INTEGER NOT NULL DEFAULT 0,
            owner_id INTEGER REFERENCES users(id),
            branch_id INTEGER REFERENCES branches(id)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS notification_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )"""
    )
    medicine_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(medicines)")
    }
    for column in (
        "uploader_name", "uploader_email", "uploader_whatsapp",
        "notify_email", "notify_whatsapp", "owner_id",
        "branch_id",
    ):
        if column not in medicine_columns:
            if column.startswith("notify_"):
                data_type = "INTEGER NOT NULL DEFAULT 0"
            elif column == "owner_id":
                data_type = "INTEGER REFERENCES users(id)"
            elif column == "branch_id":
                data_type = "INTEGER REFERENCES branches(id)"
            else:
                data_type = "TEXT NOT NULL DEFAULT ''"
            connection.execute(f"ALTER TABLE medicines ADD COLUMN {column} {data_type}")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS notification_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            medicine_id INTEGER,
            medicine_name TEXT NOT NULL,
            batch_number TEXT NOT NULL,
            channel TEXT NOT NULL,
            event TEXT NOT NULL,
            event_date TEXT NOT NULL,
            recipient TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    connection.execute(
        "DROP INDEX IF EXISTS unique_medicine_alert_delivery"
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS unique_medicine_alert_delivery
           ON notification_logs (medicine_id, channel, event, event_date, recipient)
           WHERE medicine_id IS NOT NULL"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS unique_weekly_digest_delivery
           ON notification_logs (event, event_date, recipient)
           WHERE medicine_id IS NULL AND event = 'weekly-digest'"""
    )
    management_email = current_app.config["MANAGEMENT_EMAIL"].strip().lower()
    management_password = current_app.config["MANAGEMENT_PASSWORD"]
    if management_email and management_password:
        if not valid_email(management_email):
            raise ValueError("MANAGEMENT_EMAIL must be a valid email address.")
        if len(management_password) < 12:
            raise ValueError("MANAGEMENT_PASSWORD must contain at least 12 characters.")
        manager = connection.execute(
            "SELECT id FROM users WHERE email = ? COLLATE NOCASE",
            (management_email,),
        ).fetchone()
        if manager:
            connection.execute(
                """UPDATE users SET name = 'Management', password_hash = ?, role = 'management'
                   WHERE id = ?""",
                (generate_password_hash(management_password), manager["id"]),
            )
        else:
            connection.execute(
                """INSERT INTO users (name, email, password_hash, role)
                   VALUES ('Management', ?, ?, 'management')""",
                (management_email, generate_password_hash(management_password)),
            )
    if connection.execute("SELECT COUNT(*) FROM medicines").fetchone()[0] == 0:
        if current_app.config["SAMPLE_DATA"]:
            today = datetime.now(ZoneInfo(current_app.config["REMINDER_TIMEZONE"])).date()
            samples = (
                ("Amoxicillin 500mg", "AMX-24018", 8, -12),
                ("Cetirizine 10mg", "CTZ-25042", 24, 5),
                ("Paracetamol 500mg", "PCM-25017", 6, 22),
                ("Vitamin C 1000mg", "VTC-24086", 42, 68),
                ("Omeprazole 20mg", "OMP-25103", 15, 145),
                ("Ibuprofen 200mg", "IBU-25067", 4, 320),
            )
            connection.executemany(
                """INSERT INTO medicines (name, batch_number, quantity, expiry_date)
                   VALUES (?, ?, ?, ?)""",
                [
                    (name, batch, quantity, date.fromordinal(today.toordinal() + offset).isoformat())
                    for name, batch, quantity, offset in samples
                ],
            )
    connection.commit()


def validate_medicine_payload(payload):
    if not isinstance(payload, dict):
        return None, "Provide medicine details as a JSON object."

    name = payload.get("name")
    batch_number = payload.get("batch_number")
    expiry_date = payload.get("expiry_date")
    quantity = payload.get("quantity")
    uploader_name = payload.get("uploader_name")
    uploader_email = payload.get("uploader_email", "")
    uploader_whatsapp = payload.get("uploader_whatsapp", "")
    notify_email = payload.get("notify_email")
    notify_whatsapp = payload.get("notify_whatsapp")
    if not isinstance(name, str) or not name.strip():
        return None, "Medicine name is required."
    if len(name.strip()) > 120:
        return None, "Medicine name must be 120 characters or fewer."
    if not isinstance(batch_number, str) or not batch_number.strip():
        return None, "Batch number is required."
    if len(batch_number.strip()) > 80:
        return None, "Batch number must be 80 characters or fewer."
    if isinstance(quantity, str) and re.fullmatch(r"[0-9]+", quantity.strip()):
        quantity = int(quantity.strip())
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        return None, "Quantity must be a whole number greater than or equal to zero."
    if not isinstance(expiry_date, str):
        return None, "A valid expiry date is required."
    if not isinstance(uploader_name, str) or not uploader_name.strip():
        return None, "Uploader name is required so the medicine can be linked to its owner."
    if len(uploader_name.strip()) > 100:
        return None, "Uploader name must be 100 characters or fewer."
    if not isinstance(uploader_email, str) or not valid_email(uploader_email.strip().lower()):
        return None, "Enter a valid email address for medicine alerts."
    if uploader_whatsapp or notify_whatsapp:
        return None, "Medicine uploads accept email only; phone and WhatsApp details are not collected."
    if notify_email is not True:
        return None, "Email alerts are required for medicine uploads."
    try:
        parsed_expiry = date.fromisoformat(expiry_date)
    except ValueError:
        return None, "Expiry date must be a valid date in YYYY-MM-DD format."
    if parsed_expiry.isoformat() != expiry_date:
        return None, "Expiry date must be a valid date in YYYY-MM-DD format."

    return {
        "name": name.strip(),
        "batch_number": batch_number.strip(),
        "quantity": quantity,
        "expiry_date": expiry_date,
        "uploader_name": uploader_name.strip(),
        "uploader_email": uploader_email.strip().lower(),
        "uploader_whatsapp": "",
        "notify_email": True,
        "notify_whatsapp": False,
    }, None


def validate_uploader_contact(payload):
    if not isinstance(payload, dict):
        return None, "Provide uploader contact details as a JSON object."
    email = payload.get("uploader_email", "")
    whatsapp = payload.get("uploader_whatsapp", "")
    if not isinstance(email, str) or len(email.strip()) > 254:
        return None, "Enter a valid email address."
    email = email.strip()
    if email and (
        parseaddr(email)[1] != email
        or "@" not in email
        or any(character.isspace() for character in email)
    ):
        return None, "Enter a valid email address."
    if not isinstance(whatsapp, str) or len(whatsapp.strip()) > 16:
        return None, "WhatsApp number must use international E.164 format (for example, +14155552671)."
    whatsapp = whatsapp.strip()
    if whatsapp and (
        not whatsapp.startswith("+")
        or not whatsapp[1:].isdigit()
        or not 8 <= len(whatsapp[1:]) <= 15
        or whatsapp[1] == "0"
    ):
        return None, "WhatsApp number must use international E.164 format (for example, +14155552671)."
    return {"uploader_email": email, "uploader_whatsapp": whatsapp}, None


def serialize_medicine(row):
    today = datetime.now(ZoneInfo(current_app.config["REMINDER_TIMEZONE"])).date()
    days_remaining = (date.fromisoformat(row["expiry_date"]) - today).days
    if days_remaining < 0:
        status, status_label = "expired", "Expired"
    elif days_remaining <= 7:
        status, status_label = "7", "Expires in 7 days"
    elif days_remaining <= 15:
        status, status_label = "15", "Expires in 15 days"
    elif days_remaining <= 30:
        status, status_label = "30", "Expires in 30 days"
    elif days_remaining <= 90:
        status, status_label = "90", "Expires in 90 days"
    else:
        status, status_label = "healthy", "In date"
    medicine = {
        "id": row["id"],
        "name": row["name"],
        "batch_number": row["batch_number"],
        "quantity": row["quantity"],
        "expiry_date": row["expiry_date"],
        "uploader_name": row["uploader_name"],
        "uploader_email": row["uploader_email"],
        "uploader_whatsapp": row["uploader_whatsapp"],
        "notify_email": bool(row["notify_email"]),
        "notify_whatsapp": bool(row["notify_whatsapp"]),
        "days_remaining": days_remaining,
        "status": status,
        "status_label": status_label,
        "low_stock": row["quantity"] <= LOW_STOCK_THRESHOLD,
    }
    if "branch_id" in row.keys():
        medicine["branch_id"] = row["branch_id"]
    if "branch_name" in row.keys():
        medicine["branch_name"] = row["branch_name"]
    return medicine


def medicine_user_view(medicine):
    if medicine["days_remaining"] < 0:
        status_label = f"Expired {abs(medicine['days_remaining'])} days ago"
    elif medicine["days_remaining"] == 0:
        status_label = "Expires today"
    else:
        status_label = f"Expires in {medicine['days_remaining']} days"
    return {
        "id": medicine["id"],
        "name": medicine["name"],
        "expiry_date": medicine["expiry_date"],
        "days_remaining": medicine["days_remaining"],
        "status": medicine["status"],
        "status_label": status_label,
    }


def get_statistics(connection, user, branch_filter=None):
    if user["role"] == "management":
        if branch_filter:
            branch_id = parse_optional_branch_id(branch_filter)
            if branch_id is None:
                rows = []
            else:
                rows = connection.execute(
                    "SELECT * FROM medicines WHERE branch_id = ?", (branch_id,)
                )
        else:
            rows = connection.execute("SELECT * FROM medicines")
    elif is_branch_staff(user):
        rows = connection.execute(
            "SELECT * FROM medicines WHERE branch_id = ?", (user["branch_id"],)
        )
    else:
        rows = connection.execute("SELECT * FROM medicines WHERE owner_id = ?", (user["id"],))
    medicines = [serialize_medicine(row) for row in rows]
    return {
        "total": len(medicines),
        "expiring_soon": sum(medicine["status"] in ("7", "15", "30", "90") for medicine in medicines),
        "expired": sum(medicine["status"] == "expired" for medicine in medicines),
        "low_stock": sum(medicine["low_stock"] for medicine in medicines)
        if is_inventory_manager(user) else 0,
    }


def parse_optional_branch_id(value):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def valid_email(email):
    return (
        isinstance(email, str)
        and len(email) <= 254
        and parseaddr(email)[1] == email
        and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is not None
    )


def management_medicine_payload():
    if request.mimetype != "multipart/form-data":
        return request.get_json(silent=True), None
    payload = request.form.to_dict()
    try:
        payload["quantity"] = int(payload.get("quantity", ""))
    except ValueError:
        return None, "Quantity must be a whole number greater than or equal to zero."
    payload["notify_email"] = "notify_email" in request.form
    payload["notify_whatsapp"] = "notify_whatsapp" in request.form
    return payload, None


def contains_photo_upload():
    payload = request.get_json(silent=True) if request.is_json else None
    return (
        bool(request.files)
        or "photo" in request.form
        or (isinstance(payload, dict) and "photo" in payload)
    )


def validate_user_medicine_payload(form):
    if not isinstance(form, (dict,)) and not hasattr(form, "get"):
        return None, "Provide medicine details as an object."
    name = form.get("name", "")
    expiry = form.get("expiry_date", "")
    email = form.get("uploader_email", g.current_user["email"])
    notify_whatsapp = form.get("notify_whatsapp", False)
    if not isinstance(name, str):
        return None, "Enter a medicine name (120 characters or fewer)."
    name = name.strip()
    if not isinstance(email, str) or not valid_email(email.strip().lower()):
        return None, "Enter a valid email address for expiry reminders."
    if isinstance(notify_whatsapp, str):
        notify_whatsapp = notify_whatsapp.lower() in ("on", "true", "1")
    if not isinstance(notify_whatsapp, bool):
        return None, "Medicine uploads accept email alerts only."
    if form.get("uploader_whatsapp", "") or notify_whatsapp:
        return None, "Medicine uploads accept email only; phone and WhatsApp details are not collected."
    if not name or len(name) > 120:
        return None, "Enter a medicine name (120 characters or fewer)."
    if not isinstance(expiry, str):
        return None, "Enter a valid medicine expiry date."
    try:
        parsed_expiry = date.fromisoformat(expiry)
        if parsed_expiry.isoformat() != expiry:
            raise ValueError
    except ValueError:
        return None, "Enter a valid medicine expiry date."
    return {
        "name": name,
        "batch_number": f"PERSONAL-{uuid.uuid4().hex[:10].upper()}",
        "quantity": 1,
        "expiry_date": expiry,
        "uploader_email": email.strip().lower(),
        "uploader_whatsapp": "",
        "notify_whatsapp": False,
    }, None


def send_user_expiry_notice(_connection, _medicine):
    return []


def get_notification_history(connection):
    return [
        {
            "id": row["id"],
            "medicine_name": row["medicine_name"],
            "batch_number": row["batch_number"],
            "channel": row["channel"],
            "event": row["event"],
            "recipient": row["recipient"],
            "status": row["status"],
            "error": row["error"],
            "created_at": row["created_at"],
        }
        for row in connection.execute(
            """SELECT id, medicine_name, batch_number, channel, event, recipient,
                      status, error, created_at
               FROM notification_logs ORDER BY id DESC LIMIT 50"""
        )
    ]


def api_error(message, status_code):
    return jsonify({"error": message}), status_code


app = create_app()


if __name__ == "__main__":
    if app.config["SCHEDULER_ENABLED"]:
        app.extensions["medicine_alert_scheduler"] = start_scheduler(app)
    app.run(debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true", use_reloader=False)
