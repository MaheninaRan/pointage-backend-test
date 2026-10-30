from django.conf import settings
from django.db import models


class Department(models.Model):
    name = models.CharField(max_length=80, unique=True)
    description = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return self.name


class Team(models.Model):
    name = models.CharField(max_length=80, unique=True)
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="managed_teams")
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="teams")

    def __str__(self):
        return self.name


class Profile(models.Model):
    class Role(models.TextChoices):
        EMPLOYEE = "employee", "Employé"
        MANAGER = "manager", "Manager"
        ADMIN = "admin", "Admin / RH"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.EMPLOYEE)
    employee_code = models.CharField(max_length=20, unique=True, blank=True, null=True)
    department_name = models.CharField(max_length=80, blank=True)
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL, related_name="members")
    phone = models.CharField(max_length=30, blank=True)
    photo = models.ImageField(upload_to="profiles/%Y/%m/%d/", blank=True, null=True)
    work_start_time = models.TimeField(default="08:00")
    work_end_time = models.TimeField(default="17:00")
    annual_leave_balance = models.DecimalField(max_digits=5, decimal_places=1, default=30.0)
    failed_login_attempts = models.PositiveIntegerField(default=0)
    is_locked = models.BooleanField(default=False)
    language = models.CharField(max_length=5, default="fr")
    reset_token = models.CharField(max_length=128, blank=True)
    reset_token_expires = models.DateTimeField(null=True, blank=True)


class Employee(models.Model):
    code = models.CharField(max_length=20, unique=True)
    full_name = models.CharField(max_length=120)
    department = models.CharField(max_length=80, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.code} - {self.full_name}"


class AttendanceEvent(models.Model):
    class EventType(models.TextChoices):
        ENTRY = "entry", "Entrée"
        EXIT = "exit", "Sortie"

    employee = models.ForeignKey(Employee, related_name="events", on_delete=models.CASCADE)
    event_type = models.CharField(max_length=5, choices=EventType.choices)
    timestamp = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-timestamp"]


class HourlyReport(models.Model):
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    total_employees = models.PositiveIntegerField(default=0)
    present_count = models.PositiveIntegerField(default=0)
    absent_count = models.PositiveIntegerField(default=0)
    total_hours = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start"]
        unique_together = ("period_start", "period_end")


class Attendance(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="attendances")
    date = models.DateField()
    check_in = models.DateTimeField()
    check_out = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default="on_time")
    late_minutes = models.PositiveIntegerField(default=0)
    missing_checkout_notified = models.BooleanField(default=False)
    worked_minutes = models.PositiveIntegerField(default=0)
    overtime_minutes = models.IntegerField(default=0)
    note = models.TextField(blank=True)
    ip_address = models.CharField(max_length=64, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    class Meta:
        unique_together = ("user", "date")
        ordering = ["-date"]


class WorkReport(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Brouillon"
        SUBMITTED = "submitted", "Soumis"
        APPROVED = "approved", "Validé"
        REJECTED = "rejected", "Rejeté"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reports")
    date = models.DateField()
    title = models.CharField(max_length=160)
    content = models.TextField()
    tasks = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    manager_comment = models.TextField(blank=True)
    validated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="validated_reports")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-created_at"]


class ReportAttachment(models.Model):
    report = models.ForeignKey(WorkReport, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="reports/%Y/%m/%d/")
    filename = models.CharField(max_length=255, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)


class CorrectionRequest(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="corrections")
    date = models.DateField()
    requested_check_in = models.DateTimeField(null=True, blank=True)
    requested_check_out = models.DateTimeField(null=True, blank=True)
    reason = models.TextField()
    status = models.CharField(max_length=20, default="pending")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="reviewed_corrections")
    review_comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class Holiday(models.Model):
    date = models.DateField(unique=True)
    name = models.CharField(max_length=120)

    class Meta:
        ordering = ["date"]


class Break(models.Model):
    attendance = models.ForeignKey(Attendance, on_delete=models.CASCADE, related_name="breaks")
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]


class LeaveRequest(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="leave_requests")
    leave_type = models.CharField(max_length=40, default="annual")
    start_date = models.DateField()
    end_date = models.DateField()
    reason = models.TextField(blank=True)
    attachment = models.FileField(upload_to="leaves/%Y/%m/%d/", blank=True, null=True)
    status = models.CharField(max_length=20, default="pending")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    review_comment = models.TextField(blank=True)
    days_deducted = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class WorkSetting(models.Model):
    start_time = models.TimeField(default="08:00")
    end_time = models.TimeField(default="17:00")
    late_tolerance_minutes = models.PositiveIntegerField(default=10)
    workdays = models.CharField(max_length=20, default="1,2,3,4,5")
    overtime_rule = models.CharField(max_length=40, default="after_8_hours")
    require_location = models.BooleanField(default=False)
    require_qr = models.BooleanField(default=False)
    require_ip = models.BooleanField(default=False)
    allowed_ips = models.CharField(max_length=500, blank=True, default="")
    office_lat = models.FloatField(null=True, blank=True)
    office_lng = models.FloatField(null=True, blank=True)
    max_distance_meters = models.PositiveIntegerField(default=500)


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=160)
    message = models.TextField()
    link = models.CharField(max_length=200, blank=True)
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class AuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=120)
    target = models.CharField(max_length=255)
    before_state = models.TextField(blank=True)
    after_state = models.TextField(blank=True)
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class StoredFile(models.Model):
    """Uploaded file content kept in the database, for hosts without a persistent disk."""
    name = models.CharField(max_length=255, unique=True)
    content = models.BinaryField()
    size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
