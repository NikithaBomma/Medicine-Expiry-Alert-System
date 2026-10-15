import csv
import io
import os
import re
import sqlite3
import tempfile
import unittest
from io import BytesIO
from datetime import date, timedelta
from urllib.parse import urlsplit

from app import create_app
from notifications import run_due_notifications


class MedicineAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = os.path.join(self.temp_dir.name, "test.db")
        self.sent_deliveries = []
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": database_path,
                "APP_BASE_URL": "http://127.0.0.1:5000",
                "SAMPLE_DATA": False,
                "MANAGEMENT_EMAIL": "manager@example.org",
                "MANAGEMENT_PASSWORD": "Manager-Test-Pass-2026",
                "SMTP_HOST": "smtp.example.org",
                "SMTP_SENDER": "alerts@example.org",
                "NOTIFICATION_SENDERS": {
                    "email": lambda recipient, subject, body: self.sent_deliveries.append(
                        ("email", recipient, subject, body)
                    )
                },
            }
        )
        self.client = self.app.test_client()
        response = self.client.post(
            "/login",
            data={"email": "manager@example.org", "password": "Manager-Test-Pass-2026"},
        )
        self.assertEqual(response.status_code, 302)

    def tearDown(self):
        self.temp_dir.cleanup()

    def medicine_payload(self, **overrides):
        payload = {
            "name": "Test medicine",
            "batch_number": "TST-100",
            "quantity": 12,
            "expiry_date": (date.today() + timedelta(days=30)).isoformat(),
            "uploader_name": "Test User",
            "uploader_email": "test@example.org",
            "uploader_whatsapp": "",
            "notify_email": True,
            "notify_whatsapp": False,
        }
        payload.update(overrides)
        return payload

    def create_medicine(self, **overrides):
        return self.client.post("/api/medicines", json=self.medicine_payload(**overrides))

    def test_dashboard_serves_application(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Medicine inventory", response.data)
        self.assertIn(b"Branches &amp; staff", response.data)
        self.assertIn(b'id="medicine-branch"', response.data)
        self.assertIn(b'name="uploader_email" type="email" required', response.data)
        self.assertNotIn(b'name="uploader_whatsapp"', response.data)
        self.assertNotIn(b'name="notify_whatsapp"', response.data)
        self.assertNotIn(b'type="file"', response.data)
        self.assertNotIn(b">PHOTO<", response.data)

    def test_login_and_management_route_permissions(self):
        client = self.app.test_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/login?next=/")
        response = client.get("/api/medicines")
        self.assertEqual(response.status_code, 401)

        user_client = self.app.test_client()
        registered = user_client.post(
            "/register",
            data={
                "name": "Private User",
                "email": "private@example.org",
                "password": "Private-User-Pass-2026",
                "confirm_password": "Private-User-Pass-2026",
                "expiry_email_enabled": "on",
            },
        )
        self.assertEqual(registered.status_code, 302)
        user_dashboard = user_client.get("/")
        self.assertIn(b"My medicines", user_dashboard.data)
        self.assertIn(b'name="uploader_email"', user_dashboard.data)
        self.assertNotIn(b'name="uploader_whatsapp"', user_dashboard.data)
        self.assertNotIn(b'name="notify_whatsapp"', user_dashboard.data)
        self.assertIn(b'data-expiry-filter="15"', user_dashboard.data)
        self.assertIn(b'id="voice-alerts-toggle"', user_dashboard.data)
        self.assertNotIn(b'type="file"', user_dashboard.data)
        self.assertNotIn(b"medicine-photo-input", user_dashboard.data)
        self.assertEqual(user_client.get("/api/notifications/settings").status_code, 403)
        self.assertEqual(user_client.get("/api/export").status_code, 403)

    def test_management_can_create_branches_staff_and_branch_scoped_medicines(self):
        created_branch = self.client.post(
            "/api/branches",
            json={
                "name": "Northside",
                "notification_email": "northside-alerts@example.org",
            },
        )
        self.assertEqual(created_branch.status_code, 201, created_branch.json)
        branch = created_branch.json["branch"]
        staff_response = self.client.post(
            "/api/branch-staff",
            json={
                "name": "Northside Staff",
                "email": "northside-staff@example.org",
                "password": "Northside-Staff-Pass-2026",
                "branch_id": branch["id"],
            },
        )
        self.assertEqual(staff_response.status_code, 201, staff_response.json)
        self.assertEqual(staff_response.json["staff"]["branch_name"], "Northside")

        another_branch = self.client.post(
            "/api/branches",
            json={
                "name": "Southside",
                "notification_email": "southside-alerts@example.org",
            },
        )
        self.assertEqual(another_branch.status_code, 201)
        other_medicine = self.create_medicine(
            name="Southside-only medicine",
            branch_id=another_branch.json["branch"]["id"],
        )
        self.assertEqual(other_medicine.status_code, 201)

        staff_client = self.app.test_client()
        login = staff_client.post(
            "/login",
            data={
                "email": "northside-staff@example.org",
                "password": "Northside-Staff-Pass-2026",
            },
        )
        self.assertEqual(login.status_code, 302)
        self.assertIn(b"Northside", staff_client.get("/").data)
        self.assertEqual(staff_client.get("/api/branches").json["branches"][0]["name"], "Northside")

        northside_medicine = staff_client.post(
            "/api/medicines",
            json={
                "name": "Northside medicine",
                "batch_number": "NORTH-01",
                "quantity": 20,
                "expiry_date": (date.today() + timedelta(days=40)).isoformat(),
                "uploader_name": "Northside Staff",
                "uploader_email": "northside-staff@example.org",
                "notify_email": True,
                "notify_whatsapp": False,
            },
        )
        self.assertEqual(northside_medicine.status_code, 201, northside_medicine.json)
        inventory = staff_client.get("/api/medicines").json["medicines"]
        self.assertEqual([item["name"] for item in inventory], ["Northside medicine"])
        self.assertEqual(inventory[0]["branch_name"], "Northside")
        self.assertEqual(staff_client.get("/api/notifications/settings").status_code, 403)
        export_rows = list(
            csv.reader(io.StringIO(staff_client.get("/api/export").get_data(as_text=True)))
        )
        self.assertEqual(len(export_rows), 2)
        self.assertEqual(export_rows[1][2], "Northside")
        self.assertEqual(
            staff_client.delete(f"/api/medicines/{other_medicine.json['medicine']['id']}").status_code,
            404,
        )

    def test_branch_email_receives_immediate_add_and_update_alerts(self):
        branch = self.client.post(
            "/api/branches",
            json={
                "name": "East Branch",
                "notification_email": "east-branch@example.org",
            },
        ).json["branch"]
        payload = self.medicine_payload(
            uploader_email="uploader@example.org",
            branch_id=branch["id"],
        )
        response = self.client.post("/api/medicines", json=payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            {(channel, recipient) for channel, recipient, _, _ in self.sent_deliveries},
            {
                ("email", "uploader@example.org"),
                ("email", "east-branch@example.org"),
            },
        )
        self.sent_deliveries.clear()
        expiry_results = run_due_notifications(self.app, today=date.today())
        self.assertEqual(len(expiry_results), 2)
        self.assertEqual(
            {recipient for _, recipient, _, _ in self.sent_deliveries},
            {"uploader@example.org", "east-branch@example.org"},
        )
        self.sent_deliveries.clear()
        updated = self.client.put(
            f"/api/medicines/{response.json['medicine']['id']}",
            json={**payload, "name": "Updated branch medicine"},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(
            {recipient for _, recipient, _, _ in self.sent_deliveries},
            {"uploader@example.org", "east-branch@example.org"},
        )

    def test_personal_accounts_cannot_create_branches_or_staff(self):
        user_client = self.app.test_client()
        user_client.post(
            "/register",
            data={
                "name": "Personal User",
                "email": "personal-branch@example.org",
                "password": "Personal-Branch-Pass-2026",
                "confirm_password": "Personal-Branch-Pass-2026",
            },
        )
        self.assertEqual(
            user_client.post(
                "/api/branches",
                json={"name": "Unauthorized", "notification_email": "x@example.org"},
            ).status_code,
            403,
        )
        self.assertEqual(user_client.post("/api/branch-staff", json={}).status_code, 403)

    def test_registration_requires_matching_password_confirmation(self):
        client = self.app.test_client()
        registration_page = client.get("/register")
        self.assertEqual(registration_page.status_code, 200)
        self.assertIn(b'name="confirm_password"', registration_page.data)
        self.assertIn(b'class="password-visibility"', registration_page.data)
        response = client.post(
            "/register",
            data={
                "name": "Mismatch User",
                "email": "mismatch@example.org",
                "password": "Matching-Password-2026",
                "confirm_password": "Different-Password-2026",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"The passwords do not match.", response.data)
        connection = sqlite3.connect(self.app.config["DATABASE"])
        try:
            account = connection.execute(
                "SELECT id FROM users WHERE email = ?", ("mismatch@example.org",)
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNone(account)

    def test_session_cookie_uses_http_only_and_same_site_protections(self):
        self.assertTrue(self.app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(self.app.config["SESSION_COOKIE_SAMESITE"], "Lax")

    def test_password_reset_email_updates_password_and_is_single_use(self):
        user_client = self.app.test_client()
        registration = user_client.post(
            "/register",
            data={
                "name": "Password Reset User",
                "email": "password-reset@example.org",
                "password": "Old-Password-For-Reset-2026",
                "confirm_password": "Old-Password-For-Reset-2026",
            },
        )
        self.assertEqual(registration.status_code, 302)

        request_response = self.client.post(
            "/forgot-password",
            data={"email": "password-reset@example.org"},
        )
        self.assertEqual(request_response.status_code, 200)
        self.assertIn(b"If an account uses that address", request_response.data)
        self.assertEqual(len(self.sent_deliveries), 1)
        channel, recipient, subject, body = self.sent_deliveries[0]
        self.assertEqual(channel, "email")
        self.assertEqual(recipient, "password-reset@example.org")
        self.assertIn("reset", subject.lower())
        reset_url = re.search(r"https?://\S+", body).group(0)
        reset_path = urlsplit(reset_url).path
        reset_page = self.client.get(reset_path)
        self.assertEqual(reset_page.status_code, 200)
        self.assertIn(b'name="confirm_password"', reset_page.data)

        reset_response = self.client.post(
            reset_path,
            data={
                "password": "New-Password-For-Reset-2026",
                "confirm_password": "New-Password-For-Reset-2026",
            },
        )
        self.assertEqual(reset_response.status_code, 302)
        self.assertIn("password_reset=1", reset_response.location)
        self.assertEqual(self.client.get(reset_path).status_code, 400)
        self.assertEqual(user_client.get("/api/session").status_code, 401)

        user_client.post("/logout")
        old_password_login = user_client.post(
            "/login",
            data={
                "email": "password-reset@example.org",
                "password": "Old-Password-For-Reset-2026",
            },
        )
        self.assertEqual(old_password_login.status_code, 401)
        new_password_login = user_client.post(
            "/login",
            data={
                "email": "password-reset@example.org",
                "password": "New-Password-For-Reset-2026",
            },
        )
        self.assertEqual(new_password_login.status_code, 302)
        self.assertEqual(user_client.get("/api/session").status_code, 200)

    def test_password_reset_does_not_disclose_unknown_email(self):
        existing = self.client.post(
            "/forgot-password",
            data={"email": "manager@example.org"},
        )
        unknown = self.client.post(
            "/forgot-password",
            data={"email": "not-registered@example.org"},
        )
        self.assertEqual(existing.status_code, 200)
        self.assertEqual(unknown.status_code, 200)
        self.assertIn(b"If an account uses that address", existing.data)
        self.assertIn(b"If an account uses that address", unknown.data)
        self.assertNotIn(b"not-registered@example.org", unknown.data)

    def test_personal_user_only_sees_their_own_medicine_details_without_photo_access(self):
        user_client = self.app.test_client()
        user_client.post(
            "/register",
            data={
                "name": "Avery Patient",
                "email": "avery@example.org",
                "password": "Avery-Patient-Pass-2026",
                "confirm_password": "Avery-Patient-Pass-2026",
                "expiry_email_enabled": "on",
            },
        )
        manager_record = self.create_medicine(name="Management stock")
        personal_response = user_client.post(
            "/api/medicines",
            json={
                "name": "Personal prescription",
                "expiry_date": (date.today() + timedelta(days=90)).isoformat(),
            },
        )
        self.assertEqual(personal_response.status_code, 201, personal_response.json)
        personal = personal_response.json["medicine"]
        self.assertNotIn("quantity", personal)
        self.assertNotIn("batch_number", personal)
        self.assertNotIn("uploader_email", personal)
        self.assertNotIn("photo_url", personal)
        self.assertEqual(personal_response.json["notifications"], [])
        self.assertEqual(user_client.get("/api/medicines").json["medicines"][0]["name"], "Personal prescription")
        self.assertEqual(user_client.put(f"/api/medicines/{personal['id']}").status_code, 403)
        self.assertEqual(user_client.get(f"/api/medicines/{manager_record.json['medicine']['id']}/photo").status_code, 404)
        management_names = {item["name"] for item in self.client.get("/api/medicines").json["medicines"]}
        self.assertEqual(management_names, {"Management stock", "Personal prescription"})

    def test_different_users_can_register_sign_in_and_switch_without_sharing_medicines(self):
        first_client = self.app.test_client()
        first_client.post(
            "/register",
            data={
                "name": "Casey One",
                "email": "casey.one@example.org",
                "password": "Casey-One-Password-2026",
                "confirm_password": "Casey-One-Password-2026",
            },
        )
        created = first_client.post(
            "/api/medicines",
            json={
                "name": "Casey private medicine",
                "expiry_date": (date.today() + timedelta(days=80)).isoformat(),
            },
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(
            first_client.get("/api/medicines").json["medicines"][0]["name"],
            "Casey private medicine",
        )

        second_client = self.app.test_client()
        registered = second_client.post(
            "/register",
            data={
                "name": "Riley Two",
                "email": "riley.two@example.org",
                "password": "Riley-Two-Password-2026",
                "confirm_password": "Riley-Two-Password-2026",
            },
        )
        self.assertEqual(registered.status_code, 302)
        self.assertEqual(second_client.get("/api/medicines").json["medicines"], [])

        first_client.post("/logout")
        self.assertEqual(first_client.get("/").status_code, 302)
        signed_in = first_client.post(
            "/login",
            data={
                "email": "riley.two@example.org",
                "password": "Riley-Two-Password-2026",
            },
        )
        self.assertEqual(signed_in.status_code, 302)
        self.assertEqual(first_client.get("/api/medicines").json["medicines"], [])

    def test_photo_uploads_are_rejected_for_users_and_management(self):
        user_client = self.app.test_client()
        user_client.post(
            "/register",
            data={
                "name": "Photo User",
                "email": "photo@example.org",
                "password": "Photo-User-Pass-2026",
                "confirm_password": "Photo-User-Pass-2026",
            },
        )
        fields = {
            "name": "Medicine package",
            "expiry_date": (date.today() + timedelta(days=25)).isoformat(),
        }
        created = user_client.post("/api/medicines", json=fields)
        self.assertEqual(created.status_code, 201, created.json)
        user_upload = user_client.post(
            "/api/medicines",
            data={**fields, "photo": (BytesIO(b"%PDF-1.7 not a photo"), "medicine.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(user_upload.status_code, 400)
        self.assertIn("disabled", user_upload.json["error"].lower())
        management_upload = self.client.post(
            "/api/medicines",
            json={**self.medicine_payload(), "photo": "data:image/png;base64,AAAA"},
        )
        self.assertEqual(management_upload.status_code, 400)
        self.assertIn("disabled", management_upload.json["error"].lower())
        self.assertEqual(
            user_client.get(f"/api/medicines/{created.json['medicine']['id']}/photo").status_code,
            404,
        )

    def test_personal_user_can_enable_and_pause_expiry_notifications(self):
        user_client = self.app.test_client()
        user_client.post(
            "/register",
            data={
                "name": "Reminder User",
                "email": "reminder@example.org",
                "password": "Reminder-User-Pass-2026",
                "confirm_password": "Reminder-User-Pass-2026",
                "expiry_email_enabled": "on",
            },
        )
        self.assertTrue(user_client.get("/api/my/notifications").json["email_enabled"])
        response = user_client.post(
            "/api/my/notifications", json={"email_enabled": False}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["email_enabled"])

    def test_personal_email_preference_reports_missing_smtp_provider(self):
        self.app.config.update(SMTP_HOST="", SMTP_SENDER="")
        user_client = self.app.test_client()
        user_client.post(
            "/register",
            data={
                "name": "SMTP Not Configured",
                "email": "smtp-missing@example.org",
                "password": "SMTP-Missing-Password-2026",
                "confirm_password": "SMTP-Missing-Password-2026",
            },
        )
        response = user_client.post(
            "/api/my/notifications", json={"email_enabled": True}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["email_enabled"])
        self.assertFalse(response.json["email_provider_ready"])

    def test_personal_reminders_contain_expiry_only_and_never_low_stock(self):
        deliveries = []
        self.app.config["NOTIFICATION_SENDERS"] = {
            "email": lambda recipient, subject, body: deliveries.append(
                (recipient, subject, body)
            )
        }
        user_client = self.app.test_client()
        user_client.post(
            "/register",
            data={
                "name": "Expiry Only",
                "email": "expiry-only@example.org",
                "password": "Expiry-Only-Pass-2026",
                "confirm_password": "Expiry-Only-Pass-2026",
                "expiry_email_enabled": "on",
            },
        )
        created = user_client.post(
            "/api/medicines",
            json={
                "name": "User pain relief",
                "expiry_date": (date.today() + timedelta(days=15)).isoformat(),
            },
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(deliveries, [])
        results = run_due_notifications(self.app)
        self.assertEqual(len(results), 1)
        self.assertEqual(deliveries[0][0], "expiry-only@example.org")
        self.assertIn("expiry", deliveries[0][1].lower())
        self.assertIn("15 days", deliveries[0][2])
        self.assertNotIn("stock", deliveries[0][2].lower())
        self.assertNotIn("quantity", deliveries[0][2].lower())

    def test_personal_upload_rejects_whatsapp_contact_details(self):
        user_client = self.app.test_client()
        user_client.post(
            "/register",
            data={
                "name": "WhatsApp User",
                "email": "whatsapp-user@example.org",
                "password": "WhatsApp-User-Password-2026",
                "confirm_password": "WhatsApp-User-Password-2026",
                "expiry_email_enabled": "on",
            },
        )
        created = user_client.post(
            "/api/medicines",
            json={
                "name": "User medicine with WhatsApp",
                "expiry_date": (date.today() + timedelta(days=15)).isoformat(),
                "uploader_email": "expiry-alerts@example.org",
                "uploader_whatsapp": "+14155552671",
                "notify_whatsapp": True,
            },
        )
        self.assertEqual(created.status_code, 400)
        self.assertIn("email only", created.json["error"].lower())

    def test_personal_upload_rejects_invalid_email_address(self):
        user_client = self.app.test_client()
        user_client.post(
            "/register",
            data={
                "name": "Email Validation",
                "email": "email-validation@example.org",
                "password": "Email-Validation-Pass-2026",
                "confirm_password": "Email-Validation-Pass-2026",
            },
        )
        response = user_client.post(
            "/api/medicines",
            json={
                "name": "Medicine with invalid email",
                "expiry_date": (date.today() + timedelta(days=30)).isoformat(),
                "uploader_email": "not-an-email",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("valid email", response.json["error"].lower())

    def test_personal_upload_rejects_whatsapp_opt_in(self):
        user_client = self.app.test_client()
        user_client.post(
            "/register",
            data={
                "name": "WhatsApp Consent",
                "email": "whatsapp-consent@example.org",
                "password": "WhatsApp-Consent-Password-2026",
                "confirm_password": "WhatsApp-Consent-Password-2026",
            },
        )
        fields = {
            "name": "WhatsApp test medicine",
            "expiry_date": (date.today() + timedelta(days=30)).isoformat(),
            "notify_whatsapp": True,
        }
        missing_number = user_client.post("/api/medicines", json=fields)
        self.assertEqual(missing_number.status_code, 400)
        self.assertIn("email only", missing_number.json["error"].lower())
        invalid_number = user_client.post(
            "/api/medicines", json={**fields, "uploader_whatsapp": "555-0100"}
        )
        self.assertEqual(invalid_number.status_code, 400)
        self.assertIn("email only", invalid_number.json["error"].lower())

    def test_management_upload_sends_immediate_notice_and_15_day_expiry_reminder(self):
        deliveries = []
        self.app.config["NOTIFICATION_SENDERS"] = {
            "email": lambda recipient, subject, body: deliveries.append(
                (recipient, subject, body)
            )
        }
        created = self.create_medicine(
            expiry_date=(date.today() + timedelta(days=15)).isoformat()
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json["notifications"][0]["status"], "sent")
        self.assertEqual(len(deliveries), 1)
        self.assertIn("added", deliveries[0][1].lower())

        results = run_due_notifications(self.app)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "sent")
        self.assertIn("15 days", deliveries[-1][1])
        self.assertEqual(len(deliveries), 2)

    def test_management_expiry_email_includes_low_stock_status(self):
        deliveries = []
        self.app.config["NOTIFICATION_SENDERS"] = {
            "email": lambda recipient, subject, body: deliveries.append(
                (recipient, subject, body)
            )
        }
        created = self.create_medicine(
            quantity=10,
            expiry_date=(date.today() + timedelta(days=15)).isoformat(),
        )
        self.assertEqual(created.status_code, 201)
        deliveries.clear()

        results = run_due_notifications(self.app)

        self.assertEqual(len(results), 2)
        expiry_email = next(
            delivery for delivery in deliveries if "expiry reminder" in delivery[1].lower()
        )
        self.assertIn("LOW STOCK: 10 units remain", expiry_email[2])
        self.assertIn("threshold: 10 units", expiry_email[2])

    def test_create_and_list_medicine_with_statistics(self):
        response = self.create_medicine()
        self.assertEqual(response.status_code, 201)
        medicine_id = response.json["medicine"]["id"]
        listing = self.client.get("/api/medicines")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json["medicines"][0]["id"], medicine_id)
        self.assertEqual(listing.json["medicines"][0]["status"], "30")
        self.assertEqual(listing.json["medicines"][0]["uploader_name"], "Test User")
        self.assertEqual(response.json["notifications"][0]["recipient"], "test@example.org")
        self.assertEqual(listing.json["stats"], {
            "total": 1, "expiring_soon": 1, "expired": 0, "low_stock": 0
        })

    def test_expiry_boundaries_and_low_stock_are_calculated(self):
        for offset, expected_status in (
            (-1, "expired"), (0, "7"), (7, "7"), (8, "15"),
            (15, "15"), (16, "30"), (30, "30"), (31, "90"),
            (90, "90"), (91, "healthy"),
        ):
            with self.subTest(offset=offset):
                response = self.create_medicine(
                    name=f"Medicine {offset}",
                    batch_number=f"B-{offset}",
                    expiry_date=(date.today() + timedelta(days=offset)).isoformat(),
                    quantity=10 if offset == 0 else 11,
                )
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json["medicine"]["status"], expected_status)
                self.assertEqual(response.json["medicine"]["low_stock"], offset == 0)

    def test_search_and_status_filters(self):
        self.create_medicine(name="Aspirin", batch_number="ASP-4", quantity=4)
        self.create_medicine(name="Vitamin D", batch_number="VIT-9", quantity=25)
        self.assertEqual(
            [m["name"] for m in self.client.get("/api/medicines?q=asp").json["medicines"]],
            ["Aspirin"],
        )
        self.assertEqual(
            len(self.client.get("/api/medicines?status=low-stock").json["medicines"]), 1
        )
        self.assertEqual(self.client.get("/api/medicines?status=invalid").status_code, 400)

    def test_alert_and_expiry_window_filters_include_nested_windows(self):
        for name, offset, quantity in (
            ("Expired", -2, 20),
            ("Urgent", 4, 20),
            ("15 day reminder", 15, 20),
            ("Soon", 18, 20),
            ("Upcoming", 65, 20),
            ("Low stock", 120, 2),
            ("In date", 120, 20),
        ):
            self.create_medicine(
                name=name,
                batch_number=f"{name}-BATCH",
                expiry_date=(date.today() + timedelta(days=offset)).isoformat(),
                quantity=quantity,
            )

        self.assertEqual(len(self.client.get("/api/medicines?status=alerts").json["medicines"]), 6)
        self.assertEqual(len(self.client.get("/api/medicines?status=15").json["medicines"]), 2)
        self.assertEqual(len(self.client.get("/api/medicines?status=30").json["medicines"]), 3)
        self.assertEqual(len(self.client.get("/api/medicines?status=90").json["medicines"]), 4)
        searched = self.client.get("/api/medicines?q=In%20date").json
        self.assertEqual(len(searched["medicines"]), 1)
        self.assertEqual(len(searched["alerts"]), 6)

    def test_update_delete_and_missing_records(self):
        medicine_id = self.create_medicine().json["medicine"]["id"]
        updated = self.client.put(
            f"/api/medicines/{medicine_id}",
            json=self.medicine_payload(name="Updated medicine", quantity=0),
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json["medicine"]["name"], "Updated medicine")
        self.assertTrue(updated.json["medicine"]["low_stock"])
        self.assertEqual(self.client.delete(f"/api/medicines/{medicine_id}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/medicines/{medicine_id}").status_code, 404)
        self.assertEqual(
            self.client.put("/api/medicines/999", json=self.medicine_payload()).status_code,
            404,
        )

    def test_personal_user_can_remove_only_their_own_medicines(self):
        manager_medicine = self.create_medicine(name="Management medicine")
        user_client = self.app.test_client()
        registered = user_client.post(
            "/register",
            data={
                "name": "Medicine Owner",
                "email": "medicine-owner@example.org",
                "password": "Medicine-Owner-Password-2026",
                "confirm_password": "Medicine-Owner-Password-2026",
            },
        )
        self.assertEqual(registered.status_code, 302)
        created = user_client.post(
            "/api/medicines",
            json={
                "name": "My medicine to remove",
                "expiry_date": (date.today() + timedelta(days=40)).isoformat(),
            },
        )
        self.assertEqual(created.status_code, 201)
        medicine_id = created.json["medicine"]["id"]
        self.assertEqual(user_client.delete(f"/api/medicines/{medicine_id}").status_code, 200)
        self.assertEqual(user_client.delete(f"/api/medicines/{manager_medicine.json['medicine']['id']}").status_code, 404)
        self.assertEqual(
            [medicine["name"] for medicine in self.client.get("/api/medicines").json["medicines"]],
            ["Management medicine"],
        )

    def test_invalid_input_is_rejected(self):
        invalid_payloads = (
            ({"name": "", "batch_number": "X", "quantity": 1, "expiry_date": "2030-01-01"}),
            (self.medicine_payload(quantity=-1)),
            (self.medicine_payload(quantity=1.5)),
            (self.medicine_payload(expiry_date="2030-02-30")),
            (self.medicine_payload(expiry_date="2030-1-1")),
            (self.medicine_payload(uploader_email="", uploader_whatsapp="")),
            (self.medicine_payload(uploader_whatsapp="5551234567")),
            (self.medicine_payload(uploader_email="not-an-email")),
            (self.medicine_payload(notify_email=False, notify_whatsapp=False)),
            (self.medicine_payload(notify_email=True, uploader_email="")),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.post("/api/medicines", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json)
        self.assertEqual(self.client.post("/api/medicines", data="not json").status_code, 400)

    def test_csv_export_contains_inventory_and_calculated_status(self):
        self.create_medicine()
        response = self.client.get("/api/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.content_type)
        rows = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
        self.assertEqual(rows[0], [
            "Medicine", "Batch number", "Branch", "Quantity", "Expiry date", "Status", "Days remaining"
        ])
        self.assertEqual(rows[1][0:4], ["Test medicine", "TST-100", "", "12"])
        self.assertEqual(rows[1][5], "Expires in 30 days")

    def test_first_run_seeds_sample_inventory(self):
        seeded_app = create_app(
            {
                "TESTING": True,
                "DATABASE": os.path.join(self.temp_dir.name, "seeded.db"),
                "SAMPLE_DATA": True,
                "MANAGEMENT_EMAIL": "manager@example.org",
                "MANAGEMENT_PASSWORD": "Manager-Test-Pass-2026",
            }
        )
        seeded_client = seeded_app.test_client()
        seeded_client.post(
            "/login",
            data={"email": "manager@example.org", "password": "Manager-Test-Pass-2026"},
        )
        response = seeded_client.get("/api/medicines")
        self.assertEqual(response.json["stats"]["total"], 6)
        self.assertEqual(response.json["stats"]["expired"], 1)
        self.assertGreaterEqual(response.json["stats"]["low_stock"], 2)

    def test_management_upload_rejects_whatsapp_contact_details(self):
        response = self.create_medicine(
            uploader_name="Jordan Lee",
            uploader_email="jordan@example.org",
            uploader_whatsapp="+14155552671",
            notify_email=True,
            notify_whatsapp=True,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("email only", response.json["error"].lower())

    def test_management_upload_requires_email_address(self):
        response = self.create_medicine(
            uploader_email="",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("valid email", response.json["error"].lower())

    def test_failed_provider_delivery_is_reported_and_logged(self):
        def fail_delivery(_recipient, _subject, _body):
            raise OSError("SMTP server unavailable")

        self.app.config["NOTIFICATION_SENDERS"] = {"email": fail_delivery}
        response = self.create_medicine()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["notifications"][0]["status"], "failed")
        inventory = self.client.get("/api/medicines").json["medicines"]
        self.assertEqual([medicine["name"] for medicine in inventory], ["Test medicine"])
        history = self.client.get("/api/notifications/settings").json["history"]
        self.assertEqual(history[0]["status"], "failed")
        self.assertEqual(history[0]["error"], "SMTP server unavailable")

    def test_daily_milestone_reminders_are_deduplicated_per_uploader(self):
        deliveries = []
        self.app.config.update(
            SMTP_HOST="smtp.example.org",
            SMTP_SENDER="alerts@example.org",
            NOTIFICATION_SENDERS={
                "email": lambda recipient, subject, body: deliveries.append(
                    (recipient, subject, body)
                )
            },
        )
        self.create_medicine(expiry_date=(date.today() + timedelta(days=90)).isoformat())
        self.assertEqual(len(deliveries), 1)
        first_run = run_due_notifications(self.app)
        self.assertEqual(len(first_run), 1)
        self.assertEqual(first_run[0]["status"], "sent")
        self.assertIn("expires in 90 days", deliveries[-1][1])
        second_run = run_due_notifications(self.app)
        self.assertEqual(second_run[0]["message"], "Already sent or currently being delivered.")
        self.assertEqual(len(deliveries), 2)

    def test_notification_channels_can_be_disabled(self):
        response = self.client.post(
            "/api/notifications/settings",
            json={"email_enabled": False, "whatsapp_enabled": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["settings"]["email_enabled"])
        self.assertFalse(response.json["settings"]["whatsapp_enabled"])

    def test_notification_settings_report_provider_readiness_when_saved(self):
        self.app.config.update(SMTP_HOST="", SMTP_SENDER="")
        response = self.client.post(
            "/api/notifications/settings",
            json={"email_enabled": True, "whatsapp_enabled": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["settings"]["email_enabled"])
        self.assertFalse(response.json["providers"]["email"]["ready"])

    def test_email_provider_rejects_an_email_address_as_smtp_host(self):
        self.app.config.update(
            SMTP_HOST="manager@example.org",
            SMTP_SENDER="alerts@example.org",
            SMTP_USERNAME="mailer@example.org",
            SMTP_PASSWORD="app-password",
        )
        response = self.client.get("/api/notifications/settings")
        self.assertFalse(response.json["providers"]["email"]["ready"])
        self.assertIn("SMTP_HOST", response.json["providers"]["email"]["message"])

    def test_notification_settings_save_reminder_days_and_weekly_digest(self):
        response = self.client.post(
            "/api/notifications/settings",
            json={
                "email_enabled": True,
                "whatsapp_enabled": False,
                "weekly_digest_enabled": True,
                "reminder_days": [60, 14, 3, 0],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["settings"]["weekly_digest_enabled"])
        self.assertEqual(response.json["settings"]["reminder_days"], [60, 14, 3, 0])
        self.assertFalse(response.json["settings"]["whatsapp_enabled"])

        invalid = self.client.post(
            "/api/notifications/settings",
            json={
                "email_enabled": True,
                "whatsapp_enabled": False,
                "reminder_days": [30, 30],
            },
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("reminder days", invalid.json["error"].lower())

    def test_custom_expiry_reminder_day_is_delivered(self):
        settings = self.client.post(
            "/api/notifications/settings",
            json={
                "email_enabled": True,
                "whatsapp_enabled": False,
                "reminder_days": [21],
            },
        )
        self.assertEqual(settings.status_code, 200)
        self.create_medicine(expiry_date=(date.today() + timedelta(days=21)).isoformat())
        self.sent_deliveries.clear()

        results = run_due_notifications(self.app, today=date.today())

        self.assertEqual([result["status"] for result in results], ["sent"])
        self.assertEqual(len(self.sent_deliveries), 1)
        self.assertIn("expires in 21 days", self.sent_deliveries[0][2])

    def test_weekly_digest_is_opt_in_and_keeps_personal_inventory_private(self):
        self.client.post(
            "/api/notifications/settings",
            json={
                "email_enabled": True,
                "whatsapp_enabled": False,
                "weekly_digest_enabled": True,
            },
        )
        user_client = self.app.test_client()
        user_client.post(
            "/register",
            data={
                "name": "Digest User",
                "email": "digest-user@example.org",
                "password": "Digest-User-Password-2026",
                "confirm_password": "Digest-User-Password-2026",
                "expiry_email_enabled": "on",
            },
        )
        preferences = user_client.post(
            "/api/my/notifications",
            json={"email_enabled": True, "weekly_digest_enabled": True},
        )
        self.assertEqual(preferences.status_code, 200)
        self.assertTrue(preferences.json["weekly_digest_enabled"])

        management_medicine = self.create_medicine(
            name="Management digest medicine",
            quantity=35,
            expiry_date=(date.today() + timedelta(days=60)).isoformat(),
        )
        self.assertEqual(management_medicine.status_code, 201)
        personal_medicine = user_client.post(
            "/api/medicines",
            json={
                "name": "Personal digest medicine",
                "expiry_date": (date.today() + timedelta(days=75)).isoformat(),
                "uploader_email": "digest-user@example.org",
            },
        )
        self.assertEqual(personal_medicine.status_code, 201)
        self.sent_deliveries.clear()

        monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7)
        results = run_due_notifications(self.app, today=monday)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["status"] == "sent" for result in results))
        bodies = {delivery[1]: delivery[3] for delivery in self.sent_deliveries}
        self.assertIn("Management digest medicine", bodies["test@example.org"])
        self.assertNotIn("Personal digest medicine", bodies["test@example.org"])
        self.assertIn("Personal digest medicine", bodies["digest-user@example.org"])
        self.assertNotIn("Management digest medicine", bodies["digest-user@example.org"])

        second_run = run_due_notifications(self.app, today=monday)
        self.assertEqual(second_run, [])
        self.assertEqual(len(self.sent_deliveries), 2)

    def test_failed_weekly_digest_retries_on_a_later_day(self):
        self.client.post(
            "/api/notifications/settings",
            json={
                "email_enabled": True,
                "whatsapp_enabled": False,
                "weekly_digest_enabled": True,
            },
        )
        self.create_medicine(
            name="Weekly retry medicine",
            quantity=40,
            expiry_date=(date.today() + timedelta(days=60)).isoformat(),
        )
        attempts = []

        def fail_once(recipient, subject, body):
            attempts.append((recipient, subject, body))
            if len(attempts) == 1:
                raise OSError("Temporary SMTP error")

        self.app.config["NOTIFICATION_SENDERS"] = {"email": fail_once}
        monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7)

        first_run = run_due_notifications(self.app, today=monday)
        retry = run_due_notifications(self.app, today=monday + timedelta(days=1))

        self.assertEqual([item["status"] for item in first_run], ["failed"])
        self.assertEqual([item["status"] for item in retry], ["sent"])
        self.assertEqual(len(attempts), 2)

    def test_notification_test_requires_contact_details(self):
        response = self.client.post("/api/notifications/test", json={})
        self.assertEqual(response.status_code, 400)

    def test_test_delivery_uses_contact_details_without_adding_a_medicine(self):
        deliveries = []
        self.app.config.update(
            TWILIO_ACCOUNT_SID="AC123",
            TWILIO_AUTH_TOKEN="test-token",
            TWILIO_WHATSAPP_FROM="whatsapp:+14155550100",
            NOTIFICATION_SENDERS={
                "email": lambda recipient, subject, body: deliveries.append(("email", recipient)),
                "whatsapp": lambda recipient, subject, body: deliveries.append(("whatsapp", recipient)),
            },
        )
        response = self.client.post(
            "/api/notifications/test",
            json={"uploader_email": "jordan@example.org", "uploader_whatsapp": "+14155552671"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(deliveries),
            {("email", "jordan@example.org"), ("whatsapp", "+14155552671")},
        )
        self.assertEqual(self.client.get("/api/medicines").json["stats"]["total"], 0)

    def test_existing_database_is_migrated_with_uploader_contact_fields(self):
        old_database = os.path.join(self.temp_dir.name, "legacy.db")
        connection = sqlite3.connect(old_database)
        connection.execute(
            """CREATE TABLE medicines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                batch_number TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                expiry_date TEXT NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO medicines (name, batch_number, quantity, expiry_date) VALUES (?, ?, ?, ?)",
            ("Legacy medicine", "OLD-1", 15, (date.today() + timedelta(days=60)).isoformat()),
        )
        connection.commit()
        connection.close()
        migrated = create_app(
            {
                "TESTING": True,
                "DATABASE": old_database,
                "SAMPLE_DATA": False,
                "SCHEDULER_ENABLED": False,
                "MANAGEMENT_EMAIL": "manager@example.org",
                "MANAGEMENT_PASSWORD": "Manager-Test-Pass-2026",
            }
        )
        migrated_client = migrated.test_client()
        migrated_client.post(
            "/login",
            data={"email": "manager@example.org", "password": "Manager-Test-Pass-2026"},
        )
        medicine = migrated_client.get("/api/medicines").json["medicines"][0]
        self.assertEqual(medicine["name"], "Legacy medicine")
        self.assertEqual(medicine["uploader_name"], "")
        self.assertEqual(medicine["uploader_email"], "")
        self.assertEqual(medicine["uploader_whatsapp"], "")


if __name__ == "__main__":
    unittest.main()
