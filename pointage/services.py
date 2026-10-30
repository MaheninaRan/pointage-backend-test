"""Business rules shared by the API views: roles and scope, attendance computation, notifications."""
import json
import math
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db.models import Q
from django.utils import timezone
from rest_framework.permissions import BasePermission

from .models import Attendance, AuditLog, Holiday, LeaveRequest, Notification, Profile, WorkSetting

User = get_user_model()

ALLOWED_UPLOAD_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "gif", "doc", "docx", "xls", "xlsx", "csv", "txt", "odt", "ods"}


# --- Roles and scope -------------------------------------------------------

def profile_of(user):
    profile, _ = Profile.objects.get_or_create(user=user)
    return profile


def user_role(user):
    if user.is_superuser:
        return Profile.Role.ADMIN
    return profile_of(user).role


def is_admin(user):
    return user_role(user) == Profile.Role.ADMIN


def is_manager_or_admin(user):
    return user_role(user) in [Profile.Role.MANAGER, Profile.Role.ADMIN]


class IsManagerOrAdmin(BasePermission):
    message = "Accès réservé aux managers et à la RH."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and is_manager_or_admin(request.user))


class IsAdmin(BasePermission):
    message = "Accès réservé à l'administration RH."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and is_admin(request.user))


class ReadAuthenticatedWriteAdmin(BasePermission):
    message = "Modification réservée à l'administration RH."

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.method in ("GET", "HEAD", "OPTIONS") or is_admin(request.user)


def visible_users(user):
    """Users whose data `user` may see: everyone for admins, their teams for managers, themselves otherwise."""
    role = user_role(user)
    if role == Profile.Role.ADMIN:
        return User.objects.all()
    if role == Profile.Role.MANAGER:
        return User.objects.filter(Q(profile__team__manager=user) | Q(id=user.id)).distinct()
    return User.objects.filter(id=user.id)


def can_manage(actor, target_user):
    """True when `actor` may review requests of `target_user` (never their own unless admin)."""
    role = user_role(actor)
    if role == Profile.Role.ADMIN:
        return True
    if role == Profile.Role.MANAGER and target_user.id != actor.id:
        return visible_users(actor).filter(id=target_user.id).exists()
    return False


def managers_of(user):
    """Recipients for a user's requests: their team manager, plus admins."""
    team = profile_of(user).team
    ids = set(User.objects.filter(profile__role=Profile.Role.ADMIN, is_active=True).values_list("id", flat=True))
    if team and team.manager_id:
        ids.add(team.manager_id)
    ids.discard(user.id)
    return User.objects.filter(id__in=ids)


def display_name(user):
    if not user:
        return ""
    return f"{user.first_name} {user.last_name}".strip() or user.username


# --- Settings, calendar ----------------------------------------------------

def work_setting():
    setting = WorkSetting.objects.order_by("id").first()
    return setting or WorkSetting.objects.create()


def workdays(setting=None):
    setting = setting or work_setting()
    return {int(d) for d in str(setting.workdays).split(",") if d.strip().isdigit()}


def is_workday(day, setting=None, holidays=None):
    if day.isoweekday() not in workdays(setting):
        return False
    if holidays is None:
        return not Holiday.objects.filter(date=day).exists()
    return day not in holidays


def working_days_between(start, end):
    setting = work_setting()
    holidays = set(Holiday.objects.filter(date__range=(start, end)).values_list("date", flat=True))
    count, day = 0, start
    while day <= end:
        if is_workday(day, setting, holidays):
            count += 1
        day += timedelta(days=1)
    return count


def parse_date(value, default=None):
    if not value:
        return default
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return default


def local_dt(day, t):
    return timezone.make_aware(datetime.combine(day, t), timezone.get_current_timezone())


def as_time(value):
    if isinstance(value, str):
        return datetime.strptime(value[:5], "%H:%M").time()
    return value


# --- Attendance computation ------------------------------------------------

def expected_start(user, day):
    profile = profile_of(user)
    start = as_time(profile.work_start_time) or as_time(work_setting().start_time)
    return local_dt(day, start)


def expected_end(user, day):
    profile = profile_of(user)
    end = as_time(profile.work_end_time) or as_time(work_setting().end_time)
    return local_dt(day, end)


def compute_status(attendance):
    """Late status and minutes from the check-in time, the employee's schedule and the tolerance."""
    setting = work_setting()
    start = expected_start(attendance.user, attendance.date)
    late = int((attendance.check_in - start).total_seconds() // 60) if attendance.check_in else 0
    if late > setting.late_tolerance_minutes:
        attendance.status, attendance.late_minutes = "late", late
    else:
        attendance.status, attendance.late_minutes = "on_time", 0


def compute_minutes(attendance):
    """Worked minutes (pauses excluded) and overtime according to the configured rule."""
    if not (attendance.check_in and attendance.check_out):
        attendance.worked_minutes, attendance.overtime_minutes = 0, 0
        return
    total = int((attendance.check_out - attendance.check_in).total_seconds() // 60)
    pauses = 0
    if attendance.pk:
        for pause in attendance.breaks.all():
            end = pause.ended_at or attendance.check_out
            pauses += max(0, int((end - pause.started_at).total_seconds() // 60))
    attendance.worked_minutes = max(0, total - pauses)
    rule = work_setting().overtime_rule
    if rule == "none":
        attendance.overtime_minutes = 0
    elif rule == "after_end_time":
        end = expected_end(attendance.user, attendance.date)
        attendance.overtime_minutes = max(0, int((attendance.check_out - end).total_seconds() // 60))
    else:
        attendance.overtime_minutes = max(0, attendance.worked_minutes - 480)


def recompute(attendance):
    compute_status(attendance)
    compute_minutes(attendance)


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def distance_meters(lat1, lng1, lat2, lng2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def check_restrictions(request):
    """Returns an error message when the punch violates the IP or location restrictions, else None."""
    setting = work_setting()
    if setting.require_ip:
        allowed = [ip.strip() for ip in setting.allowed_ips.replace(";", ",").split(",") if ip.strip()]
        ip = client_ip(request)
        if allowed and not any(ip == a or (a.endswith("*") and ip.startswith(a[:-1])) for a in allowed):
            return f"Pointage refusé : adresse IP {ip} non autorisée."
    if setting.require_location:
        lat, lng = request.data.get("latitude"), request.data.get("longitude")
        if lat in (None, "") or lng in (None, ""):
            return "Pointage refusé : la géolocalisation est obligatoire."
        if setting.office_lat is not None and setting.office_lng is not None:
            dist = distance_meters(float(lat), float(lng), setting.office_lat, setting.office_lng)
            if dist > setting.max_distance_meters:
                return f"Pointage refusé : vous êtes à {int(dist)} m du bureau (max {setting.max_distance_meters} m)."
    return None


def day_rows(user, start, end):
    """One row per day of the range: real attendance, or a synthetic absent/leave/holiday/off row."""
    today = timezone.localdate()
    setting = work_setting()
    holidays = dict(Holiday.objects.filter(date__range=(start, end)).values_list("date", "name"))
    attendances = {a.date: a for a in Attendance.objects.filter(user=user, date__range=(start, end)).prefetch_related("breaks")}
    leaves = list(LeaveRequest.objects.filter(user=user, status="approved", start_date__lte=end, end_date__gte=start))
    joined = timezone.localtime(user.date_joined).date() if user.date_joined else start
    rows, day = [], end
    while day >= start:
        if day in attendances:
            rows.append(("attendance", attendances[day]))
        elif day <= today:
            leave = next((l for l in leaves if l.start_date <= day <= l.end_date), None)
            if day in holidays:
                kind = "holiday"
            elif day.isoweekday() not in workdays(setting):
                kind = "off"
            elif leave:
                kind = "leave"
            elif day >= joined and day < today:
                kind = "absent"
            else:
                kind = None
            if kind and kind != "off":
                rows.append((kind, {"date": day, "status": kind, "label": holidays.get(day, "")}))
        day -= timedelta(days=1)
    return rows


# --- Notifications and audit ----------------------------------------------

def notify(users, title, message, link="", email=True):
    users = [users] if isinstance(users, User) else list(users)
    Notification.objects.bulk_create([Notification(user=u, title=title, message=message, link=link) for u in users])
    if email:
        recipients = [u.email for u in users if u.email]
        if recipients:
            url = f"{settings.FRONTEND_URL}{link}" if link else settings.FRONTEND_URL
            send_mail(f"[Pointage Pro] {title}", f"{message}\n\n{url}", None, recipients, fail_silently=True)


def snapshot(instance, fields):
    data = {}
    for field in fields:
        value = getattr(instance, field, None)
        data[field] = value.isoformat() if hasattr(value, "isoformat") else (str(value) if isinstance(value, Decimal) else value)
    return json.dumps(data, ensure_ascii=False, default=str)


def audit(actor, action, target, details="", before="", after=""):
    AuditLog.objects.create(actor=actor, action=action, target=target, details=details, before_state=before, after_state=after)


def notify_missing_checkouts():
    """Notifies employees (once) who checked in on a previous day but never checked out."""
    pending = Attendance.objects.filter(check_out__isnull=True, missing_checkout_notified=False, date__lt=timezone.localdate()).select_related("user")
    count = 0
    for attendance in pending:
        notify(attendance.user, "Oubli de pointage de départ",
               f"Vous n'avez pas pointé votre départ le {attendance.date:%d/%m/%Y}. Merci de demander une correction.",
               link="/pointage/historique")
        attendance.missing_checkout_notified = True
        attendance.save(update_fields=["missing_checkout_notified"])
        count += 1
    return count


def validate_upload(file_obj):
    ext = file_obj.name.rsplit(".", 1)[-1].lower() if "." in file_obj.name else ""
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return f"Type de fichier non autorisé (.{ext}). Formats acceptés : {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}."
    if file_obj.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
        return f"Fichier trop volumineux (max {settings.MAX_UPLOAD_MB} Mo)."
    return None
