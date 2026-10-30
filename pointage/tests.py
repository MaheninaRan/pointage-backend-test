from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Attendance, AuditLog, CorrectionRequest, LeaveRequest, Notification, Profile, Team, WorkReport

User = get_user_model()


def make_user(username, role="employee", team=None, **extra):
    user = User.objects.create_user(username=username, password="Secret-pass-42", email=f"{username}@test.local", **extra)
    Profile.objects.create(user=user, role=role, team=team)
    return user


def last_workday():
    day = timezone.localdate() - timedelta(days=1)
    while day.isoweekday() > 5:
        day -= timedelta(days=1)
    return day


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class BaseCase(TestCase):
    def setUp(self):
        self.admin = make_user("rh", "admin")
        self.manager = make_user("chef", "manager")
        self.team = Team.objects.create(name="Dev", manager=self.manager)
        self.employee = make_user("alice", team=self.team)
        self.outsider = make_user("bob")
        self.client = APIClient()

    def as_user(self, user):
        self.client.force_authenticate(user)
        return self.client

    def attendance(self, user, day, start=time(8, 0), end=time(17, 0)):
        tz = timezone.get_current_timezone()
        return Attendance.objects.create(
            user=user, date=day,
            check_in=timezone.make_aware(datetime.combine(day, start), tz),
            check_out=timezone.make_aware(datetime.combine(day, end), tz) if end else None,
        )


class PermissionTests(BaseCase):
    def test_anonymous_is_rejected(self):
        self.assertEqual(APIClient().get("/api/reports/").status_code, 401)

    def test_register_requires_admin(self):
        payload = {"username": "intrus", "password": "Secret-pass-42", "role": "admin"}
        self.assertEqual(self.as_user(self.employee).post("/api/auth/register/", payload).status_code, 403)
        self.assertEqual(self.as_user(self.admin).post("/api/auth/register/", payload).status_code, 201)

    def test_employee_cannot_escalate_role_on_self(self):
        self.as_user(self.employee).patch("/api/auth/me/", {"role": "admin", "phone": "034"}, format="json")
        self.employee.profile.refresh_from_db()
        self.assertEqual(self.employee.profile.role, "employee")
        self.assertEqual(self.employee.profile.phone, "034")
        self.assertEqual(self.client.patch(f"/api/users/{self.employee.id}/", {"role": "admin"}, format="json").status_code, 403)

    def test_manager_sees_only_team(self):
        ids = {u["id"] for u in self.as_user(self.manager).get("/api/users/").json()}
        self.assertIn(self.employee.id, ids)
        self.assertNotIn(self.outsider.id, ids)
        self.assertEqual(self.client.get(f"/api/attendance/history/?user={self.outsider.id}").status_code, 403)

    def test_admin_only_endpoints(self):
        for url in ["/api/audit/", "/api/exports/payroll/"]:
            self.assertEqual(self.as_user(self.manager).get(url).status_code, 403, url)
        self.assertEqual(self.as_user(self.employee).patch("/api/settings/", {"late_tolerance_minutes": 0}, format="json").status_code, 403)
        self.assertEqual(self.as_user(self.employee).get("/api/team/dashboard/").status_code, 403)


class AttendanceTests(BaseCase):
    def test_cannot_check_in_twice(self):
        client = self.as_user(self.employee)
        self.assertEqual(client.post("/api/attendance/check/", {"event_type": "entry"}).status_code, 201)
        self.assertEqual(client.post("/api/attendance/check/", {"event_type": "entry"}).status_code, 400)
        client.post("/api/attendance/break/", {"action": "start"})
        response = client.post("/api/attendance/check/", {"event_type": "exit"})
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["active_break"])
        self.assertEqual(client.post("/api/attendance/check/", {"event_type": "exit"}).status_code, 400)

    def test_exit_requires_entry(self):
        self.assertEqual(self.as_user(self.employee).post("/api/attendance/check/", {"event_type": "exit"}).status_code, 400)

    def test_ip_restriction(self):
        self.as_user(self.admin).patch("/api/settings/", {"require_ip": True, "allowed_ips": "10.0.0.*"}, format="json")
        response = self.as_user(self.employee).post("/api/attendance/check/", {"event_type": "entry"}, REMOTE_ADDR="192.168.1.5")
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/attendance/check/", {"event_type": "entry"}, REMOTE_ADDR="10.0.0.7")
        self.assertEqual(response.status_code, 201)

    def test_late_status_and_absent_rows(self):
        day = last_workday()
        record = self.attendance(self.employee, day, start=time(9, 0))
        from .services import recompute
        recompute(record)
        self.assertEqual(record.status, "late")
        self.assertEqual(record.late_minutes, 60)
        self.assertEqual(record.worked_minutes, 8 * 60)
        self.employee.date_joined = timezone.now() - timedelta(days=30)
        self.employee.save()
        items = self.as_user(self.employee).get("/api/attendance/history/").json()["items"]
        self.assertTrue(any(i["status"] == "absent" for i in items))

    def test_admin_edit_is_audited_and_requires_reason(self):
        record = self.attendance(self.employee, last_workday())
        client = self.as_user(self.admin)
        self.assertEqual(client.patch(f"/api/attendance/records/{record.id}/", {"check_out": None}, format="json").status_code, 400)
        response = client.patch(f"/api/attendance/records/{record.id}/", {"check_out": record.check_out.replace(hour=15).isoformat(), "reason": "Oubli"}, format="json")
        self.assertEqual(response.status_code, 200)
        log = AuditLog.objects.get(action="admin_edit_attendance")
        self.assertIn("check_out", log.before_state)
        self.assertNotEqual(log.before_state, log.after_state)
        self.assertEqual(self.as_user(self.manager).patch(f"/api/attendance/records/{record.id}/", {"reason": "x"}, format="json").status_code, 403)

    def test_exports(self):
        self.attendance(self.employee, last_workday())
        for fmt, mime in [("csv", "text/csv"), ("xlsx", "spreadsheet"), ("pdf", "pdf")]:
            response = self.as_user(self.employee).get(f"/api/attendance/export/?format={fmt}")
            self.assertEqual(response.status_code, 200)
            self.assertIn(mime, response["Content-Type"])
        self.assertEqual(self.as_user(self.manager).get("/api/exports/monthly/?format=pdf").status_code, 200)
        self.assertEqual(self.as_user(self.admin).get("/api/exports/payroll/?format=csv").status_code, 200)


class ReportTests(BaseCase):
    def test_report_requires_attendance_and_review_flow(self):
        day = last_workday()
        client = self.as_user(self.employee)
        payload = {"date": str(day), "title": "Journée", "content": "Travail"}
        self.assertEqual(client.post("/api/reports/", payload).status_code, 400)
        self.attendance(self.employee, day)
        report_id = client.post("/api/reports/", payload).json()["id"]
        self.assertEqual(client.post(f"/api/reports/{report_id}/submit/").status_code, 200)
        self.assertEqual(client.delete(f"/api/reports/{report_id}/").status_code, 403)
        self.assertTrue(Notification.objects.filter(user=self.manager, title="Rapport à valider").exists())

        manager = self.as_user(self.manager)
        self.assertEqual(manager.post(f"/api/reports/{report_id}/review/", {"status": "rejected"}).status_code, 400)
        self.assertEqual(manager.post(f"/api/reports/{report_id}/review/", {"status": "rejected", "comment": "Détaillez"}).status_code, 200)

        client = self.as_user(self.employee)
        self.assertEqual(client.patch(f"/api/reports/{report_id}/", {"content": "Plus de détails"}).status_code, 200)
        self.assertEqual(client.post(f"/api/reports/{report_id}/submit/").json()["status"], "submitted")
        self.assertEqual(client.get(f"/api/reports/{report_id}/download/").status_code, 200)

    def test_outside_manager_cannot_review(self):
        day = last_workday()
        self.attendance(self.outsider, day)
        report = WorkReport.objects.create(user=self.outsider, date=day, title="t", content="c", status="submitted")
        self.assertEqual(self.as_user(self.manager).post(f"/api/reports/{report.id}/review/", {"status": "approved"}).status_code, 404)

    def test_upload_validation(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        day = last_workday()
        self.attendance(self.employee, day)
        report = WorkReport.objects.create(user=self.employee, date=day, title="t", content="c")
        client = self.as_user(self.employee)
        bad = SimpleUploadedFile("virus.exe", b"x")
        self.assertEqual(client.post(f"/api/reports/{report.id}/upload_attachment/", {"file": bad}, format="multipart").status_code, 400)
        with override_settings(MEDIA_ROOT=self._media()):
            good = SimpleUploadedFile("notes.txt", b"hello")
            self.assertEqual(client.post(f"/api/reports/{report.id}/upload_attachment/", {"file": good}, format="multipart").status_code, 201)

    def test_database_storage_keeps_uploads(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import RequestFactory
        from .models import ReportAttachment, StoredFile
        from .storage import serve_stored_file
        day = last_workday()
        self.attendance(self.employee, day)
        report = WorkReport.objects.create(user=self.employee, date=day, title="t", content="c")
        storages = {"default": {"BACKEND": "pointage.storage.DatabaseStorage"}, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
        with override_settings(STORAGES=storages):
            response = self.as_user(self.employee).post(f"/api/reports/{report.id}/upload_attachment/", {"file": SimpleUploadedFile("notes.txt", b"hello")}, format="multipart")
            self.assertEqual(response.status_code, 201)
            stored = StoredFile.objects.get()
            self.assertEqual(bytes(stored.content), b"hello")
            self.assertEqual(serve_stored_file(RequestFactory().get("/"), stored.name).content, b"hello")
            attachment = ReportAttachment.objects.get()
            self.assertTrue(attachment.file.url.endswith(stored.name))
            self.assertEqual(self.as_user(self.employee).delete(f"/api/reports/{report.id}/attachments/{attachment.id}/").status_code, 204)
            self.assertFalse(StoredFile.objects.exists())

    def _media(self):
        import tempfile
        return tempfile.mkdtemp()


class CorrectionAndLeaveTests(BaseCase):
    def test_correction_creates_attendance(self):
        day = last_workday()
        tz = timezone.get_current_timezone()
        client = self.as_user(self.employee)
        response = client.post("/api/corrections/", {
            "date": str(day), "reason": "Badge oublié",
            "requested_check_in": timezone.make_aware(datetime.combine(day, time(8)), tz).isoformat(),
            "requested_check_out": timezone.make_aware(datetime.combine(day, time(16)), tz).isoformat(),
        }, format="json")
        self.assertEqual(response.status_code, 201)
        correction_id = response.json()["id"]
        self.assertEqual(self.as_user(self.manager).post(f"/api/corrections/{correction_id}/review/", {"status": "approved"}).status_code, 200)
        self.assertEqual(Attendance.objects.get(user=self.employee, date=day).worked_minutes, 8 * 60)
        self.assertEqual(CorrectionRequest.objects.get(id=correction_id).status, "approved")

    def test_leave_balance_and_cancel(self):
        start = timezone.localdate() + timedelta(days=7)
        while start.isoweekday() != 1:
            start += timedelta(days=1)
        client = self.as_user(self.employee)
        response = client.post("/api/leaves/", {"leave_type": "annual", "start_date": str(start), "end_date": str(start + timedelta(days=6)), "reason": "Vacances"})
        self.assertEqual(response.status_code, 201)
        leave_id = response.json()["id"]
        self.assertEqual(client.post("/api/leaves/", {"leave_type": "annual", "start_date": str(start), "end_date": str(start)}).status_code, 400)
        self.assertEqual(self.as_user(self.manager).post(f"/api/leaves/{leave_id}/review/", {"status": "approved"}).status_code, 200)
        self.employee.profile.refresh_from_db()
        self.assertEqual(float(self.employee.profile.annual_leave_balance), 25.0)
        self.assertEqual(self.as_user(self.employee).post(f"/api/leaves/{leave_id}/cancel/").status_code, 403)

        other = LeaveRequest.objects.create(user=self.employee, start_date=start + timedelta(days=14), end_date=start + timedelta(days=14))
        self.assertEqual(self.client.post(f"/api/leaves/{other.id}/cancel/").json()["status"], "cancelled")

    def test_team_dashboard_and_stats(self):
        self.attendance(self.employee, timezone.localdate(), end=None)
        data = self.as_user(self.manager).get("/api/team/dashboard/").json()
        self.assertEqual([m["id"] for m in data["members"]], [self.employee.id])
        self.assertEqual(data["members"][0]["state"], "present")
        stats = self.as_user(self.admin).get("/api/stats/").json()
        self.assertIn("presence_rate", stats["totals"])


class AuthTests(BaseCase):
    @override_settings(DEBUG=True)
    def test_forgot_and_reset_password(self):
        response = APIClient().post("/api/auth/password/forgot/", {"email": "alice@test.local"})
        token = response.json()["dev_reset_link"].split("token=")[1]
        response = APIClient().post("/api/auth/password/reset/", {"token": token, "new_password": "Nouveau-mdp-2026"})
        self.assertEqual(response.status_code, 200)
        self.employee.refresh_from_db()
        self.assertTrue(self.employee.check_password("Nouveau-mdp-2026"))

    def test_login_by_email_returns_tokens(self):
        response = APIClient().post("/api/auth/login/", {"username": "ALICE@test.local", "password": "Secret-pass-42"})
        self.assertEqual(response.status_code, 200)
        access = response.json()["access"]
        me = APIClient(HTTP_AUTHORIZATION=f"Bearer {access}").get("/api/auth/me/")
        self.assertEqual(me.json()["username"], "alice")
        self.employee.refresh_from_db()
        self.assertIsNotNone(self.employee.last_login)

    def test_lockout_after_five_failures(self):
        client = APIClient()
        for _ in range(5):
            client.post("/api/auth/login/", {"username": "alice", "password": "wrong"})
        response = client.post("/api/auth/login/", {"username": "alice", "password": "Secret-pass-42"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "account_locked")
