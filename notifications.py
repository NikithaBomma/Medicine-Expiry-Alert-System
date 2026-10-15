import json
import smtplib
import base64
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app


DEFAULT_SETTINGS = {
    "email_enabled": "1",
    "whatsapp_enabled": "1",
    "reminder_days": "90,30,15,7,0",
    "weekly_digest_enabled": "0",
}


def read_settings(connection):
    settings = dict(DEFAULT_SETTINGS)
    settings.update(
        {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key, value FROM notification_settings")
        }
    )
    return settings


def provider_status():
    config = current_app.config
    smtp_host = str(config.get("SMTP_HOST", "")).strip()
    smtp_username = str(config.get("SMTP_USERNAME", "")).strip()
    email_ready = bool(
        smtp_host
        and "@" not in smtp_host
        and config.get("SMTP_SENDER")
        and (not smtp_username or config.get("SMTP_PASSWORD"))
    )
    whatsapp_ready = bool(
        config.get("TWILIO_ACCOUNT_SID")
        and config.get("TWILIO_AUTH_TOKEN")
        and config.get("TWILIO_WHATSAPP_FROM", "").startswith("whatsapp:+")
    )
    return {
        "email": {
            "ready": email_ready,
            "message": (
                "SMTP is configured."
                if email_ready
                else "Set SMTP_HOST to a mail server name (such as smtp.gmail.com), SMTP_SENDER, and any required login credentials."
            ),
        },
        "whatsapp": {
            "ready": whatsapp_ready,
            "template_ready": bool(config.get("TWILIO_CONTENT_SID")),
            "message": (
                "Twilio WhatsApp is configured."
                if whatsapp_ready
                else "Set Twilio credentials and TWILIO_WHATSAPP_FROM on the server."
            ),
        },
        "schedule": {
            "hour": config["REMINDER_HOUR"],
            "timezone": config["REMINDER_TIMEZONE"],
        },
    }


def validate_settings(payload):
    if not isinstance(payload, dict):
        return None, "Provide notification settings as a JSON object."
    if not isinstance(payload.get("email_enabled"), bool):
        return None, "Choose whether email notifications are enabled."
    if not isinstance(payload.get("whatsapp_enabled"), bool):
        return None, "Choose whether WhatsApp notifications are enabled."
    settings = {
        "email_enabled": "1" if payload["email_enabled"] else "0",
        "whatsapp_enabled": "1" if payload["whatsapp_enabled"] else "0",
    }
    if "weekly_digest_enabled" in payload:
        if not isinstance(payload["weekly_digest_enabled"], bool):
            return None, "Choose whether weekly email digests are enabled."
        settings["weekly_digest_enabled"] = (
            "1" if payload["weekly_digest_enabled"] else "0"
        )
    if "reminder_days" in payload:
        days = payload["reminder_days"]
        if (
            not isinstance(days, list)
            or not days
            or len(days) > 12
            or any(
                isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 365
                for day in days
            )
            or len(set(days)) != len(days)
        ):
            return None, "Reminder days must be a list of 1–12 unique whole numbers from 0 to 365."
        settings["reminder_days"] = ",".join(
            str(day) for day in sorted(days, reverse=True)
        )
    return settings, None


def configured_targets(connection, medicine):
    settings = read_settings(connection)
    status = provider_status()
    targets = []
    for channel, contact_key in (("email", "uploader_email"), ("whatsapp", "uploader_whatsapp")):
        recipient = medicine[contact_key]
        enabled = settings[f"{channel}_enabled"] == "1"
        if enabled and medicine[f"notify_{channel}"] and recipient and status[channel]["ready"]:
            targets.append((channel, recipient))
    branch_email = medicine.get("branch_email", "")
    if (
        branch_email
        and settings["email_enabled"] == "1"
        and status["email"]["ready"]
        and ("email", branch_email) not in targets
    ):
        targets.append(("email", branch_email))
    return targets


def configured_reminder_days(connection):
    settings = read_settings(connection)
    try:
        days = [int(day) for day in settings["reminder_days"].split(",")]
        if (
            not days
            or len(days) > 12
            or any(day < 0 or day > 365 for day in days)
            or len(set(days)) != len(days)
        ):
            raise ValueError("Reminder days are outside the supported range.")
        return set(days)
    except (TypeError, ValueError):
        current_app.logger.error("Invalid reminder_days setting; using default milestones.")
        return {90, 30, 15, 7, 0}


def send_notification(channel, recipient, subject, body):
    sender = current_app.config.get("NOTIFICATION_SENDERS", {}).get(channel)
    if sender:
        return sender(recipient, subject, body)
    if channel == "email":
        return send_email(recipient, subject, body)
    if channel == "whatsapp":
        return send_whatsapp(recipient, body)
    raise ValueError(f"Unsupported notification channel: {channel}")


def send_email(recipient, subject, body):
    config = current_app.config
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["SMTP_SENDER"]
    message["To"] = recipient
    message.set_content(body)
    with smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"], timeout=15) as server:
        server.ehlo()
        if config["SMTP_USE_TLS"]:
            server.starttls()
            server.ehlo()
        if config["SMTP_USERNAME"]:
            server.login(config["SMTP_USERNAME"], config["SMTP_PASSWORD"])
        server.send_message(message)


def send_whatsapp(recipient, body):
    config = current_app.config
    account_sid = config["TWILIO_ACCOUNT_SID"]
    fields = {
        "From": config["TWILIO_WHATSAPP_FROM"],
        "To": f"whatsapp:{recipient}",
    }
    content_sid = config["TWILIO_CONTENT_SID"]
    if content_sid:
        fields["ContentSid"] = content_sid
        fields["ContentVariables"] = json.dumps({"1": body})
    else:
        fields["Body"] = body
    request = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        headers={
            "Authorization": "Basic "
            + base64.b64encode(
                f"{account_sid}:{config['TWILIO_AUTH_TOKEN']}".encode("utf-8")
            ).decode("ascii"),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status >= 400:
                raise RuntimeError(f"Twilio returned HTTP {response.status}.")
    except urllib.error.HTTPError as error:
        detail = error.read(2048).decode("utf-8", errors="replace")
        raise RuntimeError(f"Twilio returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach Twilio: {error.reason}") from error


def log_notification(connection, medicine, channel, recipient, event, event_date, subject, body):
    log = connection.execute(
        """SELECT id, status FROM notification_logs
           WHERE medicine_id = ? AND channel = ? AND event = ? AND event_date = ? AND recipient = ?""",
        (medicine["id"], channel, event, event_date, recipient),
    ).fetchone()
    if log and log["status"] in ("sent", "pending"):
        return {
            "channel": channel,
            "status": log["status"],
            "recipient": recipient,
            "message": "Already sent or currently being delivered.",
        }
    if log:
        log_id = log["id"]
        connection.execute(
            """UPDATE notification_logs SET recipient = ?, status = 'pending',
               error = NULL, created_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (recipient, log_id),
        )
    else:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO notification_logs
               (medicine_id, medicine_name, batch_number, channel, event, event_date, recipient, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (
                medicine["id"],
                medicine["name"],
                medicine["batch_number"],
                channel,
                event,
                event_date,
                recipient,
            ),
        )
        if cursor.rowcount == 0:
            existing = connection.execute(
                """SELECT id, status FROM notification_logs
                   WHERE medicine_id = ? AND channel = ? AND event = ? AND event_date = ? AND recipient = ?""",
                (medicine["id"], channel, event, event_date, recipient),
            ).fetchone()
            if existing["status"] in ("sent", "pending"):
                return {
                    "channel": channel,
                    "status": existing["status"],
                    "recipient": recipient,
                    "message": "Already sent or currently being delivered.",
                }
            log_id = existing["id"]
            connection.execute(
                """UPDATE notification_logs SET recipient = ?, status = 'pending',
                   error = NULL, created_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (recipient, log_id),
            )
        else:
            log_id = cursor.lastrowid
    connection.commit()
    try:
        send_notification(channel, recipient, subject, body)
    except Exception as error:
        current_app.logger.exception(
            "Medicine alert delivery failed: channel=%s medicine_id=%s event=%s",
            channel,
            medicine["id"],
            event,
        )
        connection.execute(
            "UPDATE notification_logs SET status = 'failed', error = ? WHERE id = ?",
            (str(error)[:500], log_id),
        )
        connection.commit()
        return {"channel": channel, "status": "failed", "recipient": recipient, "message": str(error)}
    connection.execute(
        "UPDATE notification_logs SET status = 'sent', error = NULL WHERE id = ?",
        (log_id,),
    )
    connection.commit()
    return {"channel": channel, "status": "sent", "recipient": recipient, "message": "Sent"}


def notify_medicine(connection, row, action):
    settings = read_settings(connection)
    targets = configured_targets(connection, dict(row))
    providers = provider_status()
    results = []
    medicine = dict(row)
    subject = f"MediTrack: medicine {action}"
    body = (
        f"{medicine['name']} (batch {medicine['batch_number']}) was {action}.\n"
        f"Quantity: {medicine['quantity']} units.\n"
        f"Expiry date: {medicine['expiry_date']} ({medicine_expiry_status(medicine)}).\n"
        "Check the MediTrack inventory dashboard for details."
    )
    for channel, recipient in targets:
        results.append(
            log_notification(
                connection,
                medicine,
                channel,
                recipient,
                action,
                datetime.now().astimezone().isoformat(timespec="microseconds"),
                subject,
                body,
            )
        )
    for channel, contact_key in (("email", "uploader_email"), ("whatsapp", "uploader_whatsapp")):
        recipient = medicine[contact_key]
        if (
            recipient
            and medicine[f"notify_{channel}"]
            and settings[f"{channel}_enabled"] == "1"
            and not providers[channel]["ready"]
        ):
            results.append(
                {
                    "channel": channel,
                    "status": "unavailable",
                    "recipient": recipient,
                    "message": providers[channel]["message"],
                }
            )
    branch_email = medicine.get("branch_email", "")
    if (
        branch_email
        and settings["email_enabled"] == "1"
        and not providers["email"]["ready"]
    ):
        results.append(
            {
                "channel": "email",
                "status": "unavailable",
                "recipient": branch_email,
                "message": providers["email"]["message"],
            }
        )
    return results


def run_due_notifications(app=None, today=None):
    if app is None:
        app = current_app._get_current_object()
    connection_factory = app.config.get("NOTIFICATION_CONNECTION_FACTORY")
    if connection_factory is None:
        import sqlite3

        def connection_factory():
            connection = sqlite3.connect(app.config["DATABASE"], timeout=15)
            connection.row_factory = sqlite3.Row
            return connection

    with app.app_context():
        connection = connection_factory()
        try:
            settings = read_settings(connection)
            if settings["email_enabled"] != "1" and settings["whatsapp_enabled"] != "1":
                return []
            today = today or datetime.now(ZoneInfo(app.config["REMINDER_TIMEZONE"])).date()
            results = []
            reminder_days = configured_reminder_days(connection)
            medicines = connection.execute(
                """SELECT medicines.*, branches.notification_email AS branch_email,
                          users.role AS uploader_role,
                          users.weekly_digest_enabled AS owner_weekly_digest_enabled,
                          users.expiry_email_enabled AS owner_expiry_email_enabled
                   FROM medicines LEFT JOIN users ON users.id = medicines.owner_id
                   LEFT JOIN branches ON branches.id = medicines.branch_id"""
            ).fetchall()
            for row in medicines:
                medicine = dict(row)
                targets = configured_targets(connection, medicine)
                if not targets:
                    continue
                days_remaining = (date.fromisoformat(medicine["expiry_date"]) - today).days
                milestone = {
                    90: "expiry-90",
                    30: "expiry-30",
                    15: "expiry-15",
                    7: "expiry-7",
                    0: "expiry-today",
                }.get(days_remaining, f"expiry-{days_remaining}")
                if days_remaining in reminder_days:
                    if medicine["uploader_role"] == "user":
                        subject = f"Medicine expiry reminder: {medicine['name']}"
                        body = user_expiry_reminder(medicine, days_remaining)
                    else:
                        subject, body = reminder_message(medicine, days_remaining)
                    for channel, recipient in targets:
                        results.append(
                            log_notification(
                                connection,
                                medicine,
                                channel,
                                recipient,
                                milestone,
                                medicine["expiry_date"],
                                subject,
                                body,
                            )
                        )
                if (
                    medicine["uploader_role"] != "user"
                    and medicine["quantity"] <= app.config["LOW_STOCK_THRESHOLD"]
                ):
                    subject = f"MediTrack: low stock — {medicine['name']}"
                    body = (
                        f"{medicine['name']} (batch {medicine['batch_number']}) has "
                        f"only {medicine['quantity']} units remaining.\n"
                        f"The low-stock threshold is {app.config['LOW_STOCK_THRESHOLD']} units."
                    )
                    for channel, recipient in targets:
                        results.append(
                            log_notification(
                                connection,
                                medicine,
                                channel,
                                recipient,
                                "low-stock",
                                today.isoformat(),
                                subject,
                                body,
                            )
                        )
            results.extend(send_weekly_digests(connection, medicines, settings, today))
            return results
        finally:
            connection.close()


def send_weekly_digests(connection, medicines, settings, today):
    if today.weekday() != 0 and not connection.execute(
        """SELECT 1 FROM notification_logs
           WHERE medicine_id IS NULL AND channel = 'email'
             AND event = 'weekly-digest' AND event_date LIKE ? AND status = 'failed'
           LIMIT 1""",
        (f"{(today - timedelta(days=today.weekday())).isoformat()}:%",),
    ).fetchone():
        return []
    if settings["email_enabled"] != "1" or not provider_status()["email"]["ready"]:
        return []

    monday = (today - timedelta(days=today.weekday())).isoformat()
    digest_groups = {}
    for row in medicines:
        medicine = dict(row)
        recipient = medicine["uploader_email"]
        role = medicine["uploader_role"] or "management"
        if not recipient or not medicine["notify_email"]:
            continue
        if role == "user":
            if not medicine["owner_expiry_email_enabled"] or not medicine["owner_weekly_digest_enabled"]:
                continue
        elif settings["weekly_digest_enabled"] != "1":
            continue
        digest_groups.setdefault((role, medicine["owner_id"], recipient), []).append(medicine)

    results = []
    for (role, _owner_id, recipient), group in digest_groups.items():
        digest_event_date = f"{monday}:{role}:{_owner_id or 0}"
        existing = connection.execute(
            """SELECT id, status FROM notification_logs
               WHERE medicine_id IS NULL AND channel = 'email'
                 AND event = 'weekly-digest' AND event_date = ? AND recipient = ?""",
            (digest_event_date, recipient),
        ).fetchone()
        if existing and existing["status"] in ("sent", "pending"):
            continue
        if existing:
            log_id = existing["id"]
            connection.execute(
                """UPDATE notification_logs SET status = 'pending', error = NULL,
                   created_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (log_id,),
            )
        else:
            cursor = connection.execute(
                """INSERT INTO notification_logs
                   (medicine_id, medicine_name, batch_number, channel, event, event_date,
                    recipient, status)
                   VALUES (NULL, ?, ?, 'email', 'weekly-digest', ?, ?, 'pending')""",
                ("Weekly medicine summary", "WEEKLY-DIGEST", digest_event_date, recipient),
            )
            log_id = cursor.lastrowid
        connection.commit()
        subject, body = weekly_digest_message(group, role, today)
        try:
            send_notification("email", recipient, subject, body)
        except Exception as error:
            current_app.logger.exception(
                "Weekly email digest delivery failed: recipient=%s", recipient
            )
            connection.execute(
                "UPDATE notification_logs SET status = 'failed', error = ? WHERE id = ?",
                (str(error)[:500], log_id),
            )
            results.append(
                {"channel": "email", "status": "failed", "recipient": recipient, "message": str(error)}
            )
        else:
            connection.execute(
                "UPDATE notification_logs SET status = 'sent', error = NULL WHERE id = ?",
                (log_id,),
            )
            results.append(
                {"channel": "email", "status": "sent", "recipient": recipient, "message": "Sent"}
            )
        connection.commit()
    return results


def weekly_digest_message(medicines, role, today):
    entries = []
    for medicine in sorted(medicines, key=lambda item: item["expiry_date"]):
        days_remaining = (date.fromisoformat(medicine["expiry_date"]) - today).days
        if role == "user":
            status = (
                f"expired {abs(days_remaining)} days ago"
                if days_remaining < 0
                else "expires today"
                if days_remaining == 0
                else f"expires in {days_remaining} days"
            )
            entries.append(f"- {medicine['name']}: {status} ({medicine['expiry_date']})")
        else:
            status = (
                f"expired {abs(days_remaining)} days ago"
                if days_remaining < 0
                else "expires today"
                if days_remaining == 0
                else f"expires in {days_remaining} days"
            )
            stock_note = (
                f"; low stock: {medicine['quantity']} units"
                if medicine["quantity"] <= current_app.config["LOW_STOCK_THRESHOLD"]
                else ""
            )
            if days_remaining <= 90 or stock_note:
                entries.append(
                    f"- {medicine['name']} (batch {medicine['batch_number']}): "
                    f"{status}{stock_note}"
                )
    if not entries:
        entries.append("No medicines need attention this week.")
    subject = "MediTrack weekly medicine summary"
    body = "Your weekly medicine summary:\n\n" + "\n".join(entries)
    return subject, body


def reminder_message(medicine, days_remaining):
    event_label = "expires today" if days_remaining == 0 else f"expires in {days_remaining} days"
    quantity = medicine["quantity"]
    stock_status = (
        f"\nLOW STOCK: {quantity} units remain "
        f"(threshold: {current_app.config['LOW_STOCK_THRESHOLD']} units)."
        if quantity <= current_app.config["LOW_STOCK_THRESHOLD"]
        else ""
    )
    subject = f"MediTrack expiry reminder: {medicine['name']} {event_label}"
    body = (
        f"Expiry reminder: {medicine['name']} (batch {medicine['batch_number']}) "
        f"{event_label}.\nExpiry date: {medicine['expiry_date']}.\n"
        f"Quantity on hand: {quantity} units.{stock_status}"
    )
    return subject, body


def user_expiry_reminder(medicine, days_remaining):
    expiry_message = (
        "expires today" if days_remaining == 0 else f"expires in {days_remaining} days"
    )
    return (
        f"Expiry reminder for {medicine['name']}: it {expiry_message}.\n"
        f"Expiry date: {medicine['expiry_date']}."
    )


def medicine_expiry_status(medicine):
    today = datetime.now(ZoneInfo(current_app.config["REMINDER_TIMEZONE"])).date()
    days_remaining = (date.fromisoformat(medicine["expiry_date"]) - today).days
    if days_remaining < 0:
        return f"expired {abs(days_remaining)} days ago"
    if days_remaining == 0:
        return "expires today"
    return f"expires in {days_remaining} days"


def start_scheduler(app):
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as error:
        raise RuntimeError("APScheduler is required when scheduled reminders are enabled.") from error

    try:
        timezone = ZoneInfo(app.config["REMINDER_TIMEZONE"])
    except ZoneInfoNotFoundError as error:
        raise RuntimeError(
            f"Unknown reminder timezone: {app.config['REMINDER_TIMEZONE']}"
        ) from error

    scheduler = BackgroundScheduler(timezone=timezone)
    scheduler.add_job(
        run_due_notifications,
        CronTrigger(hour=app.config["REMINDER_HOUR"], minute=0, timezone=timezone),
        args=[app],
        id="medicine-expiry-reminders",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    return scheduler
