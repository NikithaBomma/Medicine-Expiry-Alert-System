# MediTrack — Medicine Expiry Alert System

A responsive pharmacy inventory dashboard built with Flask, SQLite, HTML, CSS, and JavaScript. Track medicine batches and quantities, identify expiry and low-stock risks, deliver notifications to the person who uploaded each batch, and export your inventory as CSV.

## Features

- Separate personal medicine-user and management accounts with sign-in and role-based access.
- Multi-branch administration: management creates branches with a notification email and provisions staff accounts assigned to one branch.
- Branch staff can manage inventory only within their assigned branch; personal accounts remain private and separate from branch workspaces.
- Branch medicine additions/updates and expiry alerts are emailed to both the medicine uploader and the branch notification address.
- Responsive sign-in and registration pages, password confirmation and visibility controls, and HTTP-only, SameSite=Lax session cookies.
- Medicine users see only their own medicines and expiry alerts; they cannot access inventory management, other users' records, or stock details.
- Management sees complete inventory details, batch and quantity data, low-stock alerts, exports, and notification controls.
- Photo uploads and photo viewing are disabled for all account types.
- Track medicine name, batch number, quantity, and expiry date.
- Calculate days remaining and classify medicines as expired, expiring in 7/30/90 days, or in date.
- Dashboard statistics for total medicines, medicines expiring within 90 days, expired medicines, and low-stock medicines.
- Prioritized, color-coded expiry and low-stock alerts. Low stock means 10 units or fewer.
- A right-side expiry panel with one-click filters for expired medicines and items expiring within 7, 15, or 90 days; it moves below the inventory on small screens.
- Spoken expiry alerts start automatically when the dashboard opens. Use the voice control to mute or turn them back on; the browser remembers the choice for that account and avoids repeating the same alert during a session.
- Download the full inventory, including calculated expiry status, as CSV.
- Optional browser notifications for medicines needing attention. Permission is requested only when you select an enable-notifications control.
- Store account owner and uploader contact details per medicine batch.
- Medicine uploads collect an email address only; they do not ask for a phone or WhatsApp number. Personal users can opt into email expiry reminders at sign-up or from their dashboard, and confirm or change the email for each medicine upload. Personal users receive expiry reminders only, with no upload, inventory, quantity, or low-stock alerts.
- Management can configure system-wide expiry reminder days (defaults: 90, 30, 15, 7, and 0), enable opt-in weekly email summaries, and receive daily low-stock reminders for managed inventory. Managed-inventory expiry emails also flag batches at or below the low-stock threshold.
- Weekly digests are sent each Monday and grouped by uploader email. Personal digests are opt-in per account and contain only that user's medicines; failed digests are retried on later scheduler runs.
- Configure SMTP credentials on the server, send email test notifications, and view recent delivery results. Previously stored WhatsApp contacts remain supported for legacy records.
- Idempotent scheduled notifications and SQLite delivery history; failed reminders are retried on a later scheduler run.
- SQLite persistence and six realistic sample medicine batches on first run.
- Responsive layout with a mobile-friendly inventory table.

## Requirements

- Python 3.9 or newer.
- pip.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

Open <http://127.0.0.1:5000>. The SQLite database is created at `instance/medicines.db` and seeded with sample data the first time the application runs. Set `DATABASE_PATH` to use a different SQLite file.

Edit `.env` to configure mail credentials. For Gmail, use an App Password in `SMTP_PASSWORD` rather than your usual Google password. `.env` is ignored by Git; do not share it or commit it. Set `APP_BASE_URL` to the URL people use to reach MediTrack, then restart MediTrack after changing environment settings.

On the sign-in page, **Forgot your password?** sends a one-time reset link to the account email when SMTP is configured. Links expire after 30 minutes and are invalidated after use; changing a password also signs out existing sessions. The response is the same whether or not the email is registered. Management can use the same flow if its email account is configured.

Each medicine user can create an individual account from **Create your own account** on the sign-in page, then sign in using their own email and password. Registration requires a password of at least 10 characters and confirmation before the account is created. Every user's medicines and expiry reminders are private to that account. Use **Switch user** in the dashboard to sign out and let another person sign in on the same device. Medicine users enter the medicine name and expiry date; photo uploads are disabled. Users must opt into expiry emails at registration or enable them from their dashboard. The email field on each medicine upload is prefilled with the account email and can be changed for that medicine. Medicine upload forms collect email only and do not request phone or WhatsApp numbers. Email reminders are sent on the 90-, 30-, 15-, 7-, and 0-day expiry milestones, not immediately when a medicine is added; the dashboard confirms the destination email. Use the **Expiry filters** panel beside the inventory to jump to expired or soon-to-expire medicines. Browser alerts require browser permission and appear only while the dashboard is open. Spoken expiry alerts start automatically when the dashboard opens and can be muted using the voice control.

Management accounts are bootstrapped from server environment variables before the first launch. Set both values before starting MediTrack:

```powershell
$env:MANAGEMENT_EMAIL = "manager@example.com"
$env:MANAGEMENT_PASSWORD = "<choose-a-unique-password-of-at-least-12-characters>"
$env:SECRET_KEY = "<generate-a-long-random-secret>"
python app.py
```

After signing in as management, use **Branches** to create each branch with its notification email, then create a staff login assigned to that branch. Share the temporary password securely with the staff member. Branch staff sign in at the usual MediTrack login page and can view, add, edit, delete, and export records only for their assigned branch. Management can use the branch filter to review branch-specific inventory. Branch notification emails receive add/update alerts and scheduled expiry/low-stock messages alongside each medicine's uploader email; configure SMTP and enable **Email reminders** in management notification settings for delivery.

Management can see the complete inventory and medicine details, edit/delete any record, and access exports and provider settings. Personal users can remove medicines from their own account but cannot edit them or delete another user's/management's medicines. Public registration only creates medicine-user accounts; it cannot create management accounts. Existing databases are migrated automatically. Legacy sample or pre-account medicines without an owner remain management-only until management records/assigns them.

Management medicine entries require name, batch number, quantity, expiry date, uploader identity, and email address. Email delivery requires SMTP credentials; entering an email alone does not establish provider connectivity.

### Configure Gmail delivery (SMTP)

For Gmail, enable 2-Step Verification and create an App Password in your Google Account security settings. Use the App Password, not your regular Gmail password. Set these values in the same PowerShell window before starting or restarting MediTrack:

```powershell
$env:SMTP_HOST = "smtp.gmail.com"
$env:SMTP_PORT = "587"
$env:SMTP_USERNAME = "your-address@gmail.com"
$env:SMTP_SENDER = "your-address@gmail.com"
$env:SMTP_USE_TLS = "true"
$secure = Read-Host "Gmail App Password" -AsSecureString
$env:SMTP_PASSWORD = [System.Net.NetworkCredential]::new("", $secure).Password
python app.py
```

The App Password is entered as a hidden prompt and is not saved in this project. Keep it out of source control and never enter it in a medicine contact field. `SMTP_HOST` must be the mail server hostname (for Gmail, `smtp.gmail.com`), not the email address. In the management dashboard, confirm **Email reminders** is enabled and use **Test delivery** with a destination address. Set the comma-separated reminder days and optionally enable weekly digests in the same panel. For medicine-user accounts, enable **Email expiry reminders** and, separately, **Weekly email digest**. Weekly digests are sent on Mondays. Medicine uploads accept email addresses only. Management medicine uploads send an immediate notice to the uploader's email. Test delivery verifies connectivity immediately.

The running server must be restarted after changing SMTP environment variables. The current machine's browser notification permission is denied; allow notifications for `http://127.0.0.1:5000` in browser site settings, then select **Enable browser alerts**. Browser alerts work only while MediTrack is open.

### Legacy WhatsApp settings

New medicine records cannot collect or enable WhatsApp contact details. Twilio delivery support remains only for records created before the email-only change; review those existing contacts and notification settings if you no longer want WhatsApp reminders.

### Scheduled reminders and timezone

The Flask process runs a daily reminder job while it is running. Set its local send time and IANA timezone before startup:

```powershell
$env:REMINDER_TIMEZONE = "Asia/Kolkata"
$env:REMINDER_HOUR = "9"
```

The default is 09:00 UTC. Run a single scheduler-enabled application instance so scheduled work runs once. For production deployments with multiple web workers, set `SCHEDULER_ENABLED=false` on the web workers and start exactly one dedicated scheduler with `python scheduler_worker.py`. Keep that process running for automatic reminders. Adding or editing a batch attempts its notification immediately; provider errors appear in the dashboard and delivery history, and scheduled reminder failures can be retried by a later run.

For example, in PowerShell:

```powershell
$env:DATABASE_PATH = "C:\data\medicines.db"
python app.py
```

To run the development server with Flask's debugger, set `FLASK_DEBUG=true` before starting the app. Do not enable the debugger in production. For production, use a WSGI server such as Waitress or Gunicorn behind HTTPS, set a persistent random `SECRET_KEY`, restrict access to SQLite, and use secure backups. The application includes email-based password reset; SMTP must be configured for reset links to be delivered. Management account recovery uses the same reset flow.

## Browser notifications

Medicine users can enable/pause personal email expiry reminders in the dashboard; the preference saves automatically, but delivery requires configured SMTP. The dashboard reports if SMTP is missing. Browser alert buttons reflect browser permission state. If notifications were previously blocked, change the site permission in the browser settings and reload; the app cannot override a browser-level block. Management channel toggles save automatically and report when a provider is not configured. Use **Test delivery** to check a provider configuration before relying on it. Notification history shows delivery failures; check the application log for provider error details.

In-browser notifications are available in browsers that support the Notifications API when the app is served from `localhost` or a secure HTTPS origin. If permission is denied, update the browser's site settings to allow notifications.

## Run tests

```powershell
python -m unittest discover -s tests -v
```

The test suite uses a temporary SQLite database and does not modify your application inventory.

## API

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/api/medicines` | List medicines and dashboard statistics. Optional `q` searches medicine names and batch numbers; `status` accepts `all`, `expired`, `7`, `15`, `30`, `90`, `healthy`, or `low-stock`. |
| `POST` | `/api/medicines` | Create a medicine. JSON fields: `name`, `batch_number`, `quantity`, and `expiry_date` (`YYYY-MM-DD`). |
| `PUT` | `/api/medicines/<id>` | Replace the editable fields for a medicine. |
| `DELETE` | `/api/medicines/<id>` | Delete a medicine. |
| `GET` | `/api/export` | Management only: download all medicines as `medicine-inventory.csv`, including calculated expiry status and days remaining. |
| `GET` / `POST` | `/api/notifications/settings` | Read or update the email and WhatsApp channel switches, provider readiness, schedule, and recent delivery history. |
| `POST` | `/api/notifications/test` | Send a test to the provided `uploader_email` and/or `uploader_whatsapp` values through enabled, configured channels. |
| `GET` / `POST` | `/api/my/notifications` | A signed-in medicine user reads or changes their own email expiry-reminder preference. |

Public sign-up always creates a medicine-user account. Personal medicine records use `name` and `expiry_date`; batch and quantity are private inventory fields managed internally. Management create/update also accepts `batch_number`, `quantity`, `uploader_name`, contact details, and per-channel notification consent. Photo uploads and the photo-serving endpoint are disabled. Personal medicine lists omit inventory, batch, and contact details. All APIs require a signed-in session, and cross-user records are not accessible. Management credentials are never created from public registration.
