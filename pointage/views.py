import csv
import io
import secrets
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status as http, viewsets
from rest_framework.decorators import action, api_view, parser_classes, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView

from .exports import export_table, report_pdf
from .models import (
    Attendance, AttendanceEvent, AuditLog, Break, CorrectionRequest, Department, Employee, Holiday,
    HourlyReport, LeaveRequest, Notification, Profile, ReportAttachment, Team, WorkReport, WorkSetting,
)
from .serializers import (
    AttendanceEventSerializer, AttendanceSerializer, AuditSerializer, BreakSerializer, CorrectionSerializer,
    DepartmentSerializer, EmployeeSerializer, HolidaySerializer, HourlyReportSerializer, LeaveSerializer,
    LoginSerializer, NotificationSerializer, ReportAttachmentSerializer, TeamSerializer, UserSerializer,
    WorkReportSerializer, WorkSettingSerializer,
)
from .services import (
    IsAdmin, IsManagerOrAdmin, ReadAuthenticatedWriteAdmin, audit, can_manage, check_restrictions, client_ip,
    day_rows, display_name, expected_start, is_admin, is_manager_or_admin, managers_of, notify, notify_missing_checkouts,
    parse_date, profile_of, recompute, snapshot, user_role, validate_upload, visible_users, work_setting,
    working_days_between,
)

User = get_user_model()

ATTENDANCE_FIELDS = ["date", "check_in", "check_out", "status", "late_minutes", "worked_minutes", "overtime_minutes"]
LEAVE_LABELS = {"annual": "Congé annuel", "sick": "Maladie", "unpaid": "Sans solde", "maternity": "Maternité / paternité", "exceptional": "Exceptionnel", "other": "Autre"}
STATUS_LABELS = {"on_time": "À l'heure", "late": "Retard", "absent": "Absent", "leave": "Congé", "holiday": "Férié"}


def broadcast(kind, **data):
    try:
        async_to_sync(get_channel_layer().group_send)("attendance", {"type": "attendance.update", "data": {"kind": kind, **data}})
    except Exception:  # live updates are best effort, never block the request
        pass


def fmt_time(value):
    return timezone.localtime(value).strftime("%H:%M") if value else ""


def hours(minutes):
    return round((minutes or 0) / 60, 2)


def period_from(request, default_days=30):
    today = timezone.localdate()
    end = parse_date(request.query_params.get("end"), today)
    start = parse_date(request.query_params.get("start"), end - timedelta(days=default_days - 1))
    if start > end:
        raise ValidationError({"start": "La date de début doit précéder la date de fin."})
    if (end - start).days > 400:
        raise ValidationError({"start": "Période trop longue (400 jours maximum)."})
    return start, end


def month_from(request):
    value = request.query_params.get("month") or timezone.localdate().strftime("%Y-%m")
    try:
        start = datetime.strptime(value, "%Y-%m").date()
    except ValueError:
        raise ValidationError({"month": "Format attendu : AAAA-MM."})
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start, end


def target_users(request):
    """Visible active users narrowed by the `user`, `department` and `team` query params."""
    users = visible_users(request.user).filter(is_active=True).select_related("profile").order_by("first_name", "username")
    if request.query_params.get("user"):
        users = users.filter(id=request.query_params["user"])
    if request.query_params.get("department"):
        users = users.filter(profile__department_name=request.query_params["department"])
    if request.query_params.get("team"):
        users = users.filter(profile__team_id=request.query_params["team"])
    return users


def summarize(user, start, end):
    rows = day_rows(user, start, end)
    summary = {"present": 0, "late": 0, "late_minutes": 0, "absent": 0, "leave": 0, "worked_minutes": 0, "overtime_minutes": 0, "days": {}}
    for kind, row in rows:
        if kind == "attendance":
            summary["present"] += 1
            summary["worked_minutes"] += row.worked_minutes
            summary["overtime_minutes"] += row.overtime_minutes
            if row.status == "late":
                summary["late"] += 1
                summary["late_minutes"] += row.late_minutes
            summary["days"][row.date] = "late" if row.status == "late" else "present"
        else:
            if kind in ("absent", "leave"):
                summary[kind] += 1
            summary["days"][row["date"]] = kind
    return summary


# --- Authentication ------------------------------------------------------

class LoginView(TokenObtainPairView):
    serializer_class = LoginSerializer


@api_view(["POST"])
@permission_classes([IsAdmin])
def register(request):
    serializer = UserSerializer(data=request.data, context={"request": request})
    serializer.is_valid(raise_exception=True)
    user = serializer.save()
    audit(request.user, "create_user", f"User:{user.username}")
    return Response(UserSerializer(user, context={"request": request}).data, status=201)


SELF_EDITABLE = {"first_name", "last_name", "email", "phone", "photo", "language"}


@api_view(["GET", "PATCH"])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def me(request):
    if request.method == "PATCH":
        data = {k: v for k, v in request.data.items() if k in SELF_EDITABLE}
        if "photo" in request.FILES:
            photo = request.FILES["photo"]
            if photo.content_type not in ("image/jpeg", "image/png", "image/webp", "image/gif") or photo.size > 3 * 1024 * 1024:
                return Response({"detail": "Photo invalide : image JPG/PNG/WebP de 3 Mo maximum."}, status=400)
            data["photo"] = photo
        serializer = UserSerializer(request.user, data=data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        audit(request.user, "update_profile", f"User:{request.user.username}", details=", ".join(sorted(data)))
    return Response(UserSerializer(request.user, context={"request": request}).data)


@api_view(["POST"])
def change_password(request):
    old_password = request.data.get("old_password", "")
    new_password = request.data.get("new_password", "")
    if not request.user.check_password(old_password):
        return Response({"detail": "Ancien mot de passe incorrect"}, status=400)
    try:
        validate_password(new_password, request.user)
    except DjangoValidationError as exc:
        return Response({"detail": " ".join(exc.messages)}, status=400)
    request.user.set_password(new_password)
    request.user.save(update_fields=["password"])
    audit(request.user, "change_password", f"User:{request.user.username}")
    return Response({"detail": "Mot de passe modifié"})


def issue_reset_link(user, hours_valid=1):
    profile = profile_of(user)
    profile.reset_token = uuid.uuid4().hex
    profile.reset_token_expires = timezone.now() + timedelta(hours=hours_valid)
    profile.save(update_fields=["reset_token", "reset_token_expires"])
    link = f"{settings.FRONTEND_URL}/reinitialiser?token={profile.reset_token}"
    if user.email:
        send_mail(
            "[Pointage Pro] Réinitialisation du mot de passe",
            f"Bonjour {display_name(user)},\n\nPour définir un nouveau mot de passe, ouvrez ce lien (valable {hours_valid} h) :\n{link}\n\n"
            "Si vous n'êtes pas à l'origine de cette demande, ignorez cet email.",
            None, [user.email], fail_silently=True,
        )
    return link


@api_view(["POST"])
@permission_classes([AllowAny])
def password_forgot(request):
    email = str(request.data.get("email", "")).strip()
    if not email:
        return Response({"detail": "Email requis"}, status=400)
    user = User.objects.filter(email__iexact=email, is_active=True).first()
    payload = {"detail": "Si un compte correspond à cet email, un lien de réinitialisation vient d'être envoyé."}
    if user:
        link = issue_reset_link(user)
        if settings.DEBUG:  # no real mailbox in development: expose the link so the flow can be tested
            payload["dev_reset_link"] = link
    return Response(payload)


@api_view(["POST"])
@permission_classes([AllowAny])
def password_reset(request):
    token = str(request.data.get("token", "")).strip()
    new_password = request.data.get("new_password", "")
    if not token or not new_password:
        return Response({"detail": "Lien et nouveau mot de passe requis"}, status=400)
    profile = Profile.objects.filter(reset_token=token).select_related("user").first()
    if not profile or (profile.reset_token_expires and profile.reset_token_expires < timezone.now()):
        return Response({"detail": "Lien invalide ou expiré. Refaites une demande."}, status=400)
    user = profile.user
    try:
        validate_password(new_password, user)
    except DjangoValidationError as exc:
        return Response({"detail": " ".join(exc.messages)}, status=400)
    user.set_password(new_password)
    user.save(update_fields=["password"])
    profile.reset_token = ""
    profile.reset_token_expires = None
    profile.is_locked = False
    profile.failed_login_attempts = 0
    profile.save(update_fields=["reset_token", "reset_token_expires", "is_locked", "failed_login_attempts"])
    audit(user, "reset_password", f"User:{user.username}")
    return Response({"detail": "Mot de passe réinitialisé. Vous pouvez vous connecter."})


# --- Employee attendance -------------------------------------------------

@api_view(["GET"])
def server_time(request):
    now = timezone.localtime()
    return Response({"server_time": now.isoformat(), "timezone": str(now.tzinfo), "today": str(now.date())})


@api_view(["GET"])
def today_attendance(request):
    notify_missing_checkouts()
    today = timezone.localdate()
    user = request.user
    attendance = Attendance.objects.filter(user=user, date=today).prefetch_related("breaks").first()
    week_start = today - timedelta(days=today.weekday())
    week = summarize(user, week_start, today)
    report_today = WorkReport.objects.filter(user=user, date=today).first()
    setting = work_setting()
    profile = profile_of(user)
    return Response({
        "date": str(today),
        "attendance": AttendanceSerializer(attendance).data if attendance else None,
        "week": {"worked_minutes": week["worked_minutes"], "late": week["late"], "absent": week["absent"], "present": week["present"]},
        "report": {"id": report_today.id, "status": report_today.status} if report_today else None,
        "report_due": bool(attendance) and (not report_today or report_today.status in ("draft", "rejected")),
        "schedule": {"start": str(profile.work_start_time)[:5], "end": str(profile.work_end_time)[:5], "tolerance": setting.late_tolerance_minutes},
        "restrictions": {"location": setting.require_location, "ip": setting.require_ip},
        "is_workday": today.isoweekday() in {int(d) for d in setting.workdays.split(",") if d.strip().isdigit()} and not Holiday.objects.filter(date=today).exists(),
        "on_leave": LeaveRequest.objects.filter(user=user, status="approved", start_date__lte=today, end_date__gte=today).exists(),
    })


@api_view(["GET"])
def day_status(request):
    day = parse_date(request.query_params.get("date"))
    if not day:
        return Response({"detail": "Date requise"}, status=400)
    return Response({"date": str(day), "has_attendance": Attendance.objects.filter(user=request.user, date=day).exists()})


@api_view(["POST"])
@transaction.atomic
def check_server(request):
    event_type = request.data.get("event_type")
    if event_type not in ["entry", "exit"]:
        return Response({"detail": "event_type doit être entry ou exit"}, status=400)
    error = check_restrictions(request)
    if error:
        return Response({"detail": error}, status=403)

    today = timezone.localdate()
    now = timezone.now()
    attendance = Attendance.objects.select_for_update().filter(user=request.user, date=today).first()

    if event_type == "entry":
        if attendance:
            return Response({"detail": f"Arrivée déjà enregistrée aujourd'hui à {fmt_time(attendance.check_in)}."}, status=400)
        lat, lng = request.data.get("latitude"), request.data.get("longitude")
        attendance = Attendance(user=request.user, date=today, check_in=now, ip_address=client_ip(request),
                                latitude=float(lat) if lat not in (None, "") else None,
                                longitude=float(lng) if lng not in (None, "") else None)
        recompute(attendance)
        attendance.save()
    else:
        if not attendance:
            return Response({"detail": "Vous devez pointer votre arrivée avant le départ."}, status=400)
        if attendance.check_out:
            return Response({"detail": f"Départ déjà enregistré aujourd'hui à {fmt_time(attendance.check_out)}."}, status=400)
        attendance.breaks.filter(ended_at__isnull=True).update(ended_at=now)
        attendance.check_out = now
        recompute(attendance)
        attendance.save()

    audit(request.user, event_type, f"Attendance:{attendance.date}", details=f"Heure serveur {timezone.localtime(now):%H:%M:%S} · IP {client_ip(request)}")
    broadcast("check", user=request.user.id, event=event_type)
    return Response(AttendanceSerializer(attendance).data, status=201)


@api_view(["POST"])
@transaction.atomic
def break_action(request):
    action_name = request.data.get("action")
    attendance = Attendance.objects.select_for_update().filter(user=request.user, date=timezone.localdate()).first()
    if not attendance:
        return Response({"detail": "Aucune arrivée enregistrée aujourd'hui"}, status=400)
    if attendance.check_out:
        return Response({"detail": "Journée terminée : départ déjà pointé"}, status=400)
    if action_name == "start":
        if attendance.breaks.filter(ended_at__isnull=True).exists():
            return Response({"detail": "Une pause est déjà en cours"}, status=400)
        pause = Break.objects.create(attendance=attendance, started_at=timezone.now())
        broadcast("break", user=request.user.id)
        return Response(BreakSerializer(pause).data, status=201)
    if action_name == "end":
        pause = attendance.breaks.filter(ended_at__isnull=True).first()
        if not pause:
            return Response({"detail": "Aucune pause en cours"}, status=400)
        pause.ended_at = timezone.now()
        pause.save(update_fields=["ended_at"])
        broadcast("break", user=request.user.id)
        return Response(BreakSerializer(pause).data)
    return Response({"detail": "Action invalide"}, status=400)


def history_user(request):
    """The user whose history is requested: self, or a visible employee for managers/admins."""
    user_id = request.query_params.get("user")
    if not user_id or str(user_id) == str(request.user.id):
        return request.user
    user = visible_users(request.user).filter(id=user_id).first()
    if not user:
        raise PermissionDenied("Vous ne pouvez pas consulter cet employé.")
    return user


def history_rows(user, start, end, status_filter=""):
    result = []
    for kind, row in day_rows(user, start, end):
        if kind == "attendance":
            item = {"kind": kind, **AttendanceSerializer(row).data}
        else:
            item = {"kind": kind, "date": str(row["date"]), "status": kind, "label": row["label"], "check_in": None, "check_out": None,
                    "worked_minutes": 0, "break_minutes": 0, "overtime_minutes": 0, "late_minutes": 0, "breaks": []}
        if not status_filter or item["status"] == status_filter:
            result.append(item)
    return result


@api_view(["GET"])
def attendance_history(request):
    start, end = period_from(request)
    user = history_user(request)
    return Response({"user": {"id": user.id, "full_name": display_name(user)}, "start": str(start), "end": str(end),
                     "items": history_rows(user, start, end, request.query_params.get("status", ""))})


@api_view(["GET"])
def export_attendance(request):
    start, end = period_from(request)
    if request.query_params.get("user") or not request.query_params.get("scope"):
        users = [history_user(request)]
    else:
        if not is_manager_or_admin(request.user):
            raise PermissionDenied("Export réservé aux managers et à la RH.")
        users = list(target_users(request))
    rows = []
    for user in users:
        profile = profile_of(user)
        for item in history_rows(user, start, end, request.query_params.get("status", "")):
            rows.append([display_name(user), profile.employee_code or "", item["date"],
                         fmt_time(parse_iso(item["check_in"])), fmt_time(parse_iso(item["check_out"])),
                         item.get("break_minutes", 0), hours(item["worked_minutes"]), item.get("late_minutes", 0),
                         hours(item["overtime_minutes"]), STATUS_LABELS.get(item["status"], item["status"])])
    headers = ["Employé", "Matricule", "Date", "Arrivée", "Départ", "Pauses (min)", "Heures", "Retard (min)", "Heures supp.", "Statut"]
    title = f"Pointages du {start:%d/%m/%Y} au {end:%d/%m/%Y}"
    return export_table(f"pointages-{start}-{end}", title, headers, rows, request.query_params.get("format", "csv"))


def parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


# --- Manager / admin attendance -------------------------------------------

class AttendanceRecordViewSet(viewsets.ModelViewSet):
    """Team attendance for managers; manual creation and correction (audited) for admins."""
    serializer_class = AttendanceSerializer
    permission_classes = [IsManagerOrAdmin]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = Attendance.objects.filter(user__in=visible_users(self.request.user)).select_related("user", "user__profile").prefetch_related("breaks")
        params = self.request.query_params
        if params.get("user"):
            qs = qs.filter(user_id=params["user"])
        if params.get("department"):
            qs = qs.filter(user__profile__department_name=params["department"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("start"):
            qs = qs.filter(date__gte=params["start"])
        if params.get("end"):
            qs = qs.filter(date__lte=params["end"])
        qs = qs.order_by("-date", "user__first_name")
        return qs[:1000] if self.action == "list" else qs

    def _times(self, data, day):
        values = {}
        for field in ("check_in", "check_out"):
            if field in data:
                raw = data.get(field)
                values[field] = parse_iso(raw) if raw else None
                if values[field] and timezone.is_naive(values[field]):
                    values[field] = timezone.make_aware(values[field])
        return values

    def create(self, request, *args, **kwargs):
        if not is_admin(request.user):
            raise PermissionDenied("Réservé à la RH.")
        reason = str(request.data.get("reason", "")).strip()
        user = User.objects.filter(id=request.data.get("user")).first()
        day = parse_date(request.data.get("date"))
        if not reason or not user or not day:
            raise ValidationError({"detail": "Employé, date et motif obligatoires."})
        if Attendance.objects.filter(user=user, date=day).exists():
            raise ValidationError({"detail": "Un pointage existe déjà ce jour-là : modifiez-le."})
        times = self._times(request.data, day)
        if not times.get("check_in"):
            raise ValidationError({"check_in": "Heure d'arrivée obligatoire."})
        if times.get("check_out") and times["check_out"] <= times["check_in"]:
            raise ValidationError({"check_out": "Le départ doit être après l'arrivée."})
        attendance = Attendance(user=user, date=day, note=reason, **times)
        recompute(attendance)
        attendance.save()
        audit(request.user, "admin_create_attendance", f"Attendance:{user.username}:{day}", details=reason,
              after=snapshot(attendance, ATTENDANCE_FIELDS))
        notify(user, "Pointage ajouté par la RH", f"Un pointage a été ajouté pour le {day:%d/%m/%Y}. Motif : {reason}", link="/pointage/historique")
        return Response(AttendanceSerializer(attendance).data, status=201)

    def partial_update(self, request, *args, **kwargs):
        if not is_admin(request.user):
            raise PermissionDenied("Réservé à la RH.")
        attendance = self.get_object()
        reason = str(request.data.get("reason", "")).strip()
        if not reason:
            raise ValidationError({"reason": "Le motif de la correction est obligatoire."})
        before = snapshot(attendance, ATTENDANCE_FIELDS)
        for field, value in self._times(request.data, attendance.date).items():
            setattr(attendance, field, value)
        if not attendance.check_in:
            raise ValidationError({"check_in": "Heure d'arrivée obligatoire."})
        if attendance.check_out and attendance.check_out <= attendance.check_in:
            raise ValidationError({"check_out": "Le départ doit être après l'arrivée."})
        attendance.note = reason
        recompute(attendance)
        attendance.save()
        audit(request.user, "admin_edit_attendance", f"Attendance:{attendance.user.username}:{attendance.date}", details=reason,
              before=before, after=snapshot(attendance, ATTENDANCE_FIELDS))
        notify(attendance.user, "Pointage corrigé par la RH", f"Votre pointage du {attendance.date:%d/%m/%Y} a été corrigé. Motif : {reason}", link="/pointage/historique")
        return Response(AttendanceSerializer(attendance).data)


@api_view(["GET"])
@permission_classes([IsManagerOrAdmin])
def team_dashboard(request):
    notify_missing_checkouts()
    today = timezone.localdate()
    now = timezone.now()
    setting = work_setting()
    members = visible_users(request.user).filter(is_active=True).exclude(id=request.user.id).select_related("profile", "profile__team").order_by("first_name", "username")
    attendances = {a.user_id: a for a in Attendance.objects.filter(date=today, user__in=members).prefetch_related("breaks")}
    on_leave = set(LeaveRequest.objects.filter(status="approved", start_date__lte=today, end_date__gte=today, user__in=members).values_list("user_id", flat=True))
    workday = today.isoweekday() in {int(d) for d in setting.workdays.split(",") if d.strip().isdigit()} and not Holiday.objects.filter(date=today).exists()
    rows, counts = [], {"present": 0, "absent": 0, "late": 0, "leave": 0, "break": 0, "finished": 0, "not_yet": 0}
    for user in members:
        profile = profile_of(user)
        attendance = attendances.get(user.id)
        late = bool(attendance and attendance.status == "late")
        if attendance:
            if attendance.check_out:
                state = "finished"
            elif any(b.ended_at is None for b in attendance.breaks.all()):
                state = "break"
            else:
                state = "present"
        elif user.id in on_leave:
            state = "leave"
        else:
            start = expected_start(user, today)
            state = "absent" if workday and now > start + timedelta(minutes=setting.late_tolerance_minutes) else "not_yet"
        counts[state] += 1
        if late:
            counts["late"] += 1
        rows.append({
            "id": user.id, "full_name": display_name(user), "department": profile.department_name, "team": profile.team.name if profile.team else "",
            "state": state, "late": late, "late_minutes": attendance.late_minutes if attendance else 0,
            "check_in": attendance.check_in if attendance else None, "check_out": attendance.check_out if attendance else None,
            "worked_minutes": attendance.worked_minutes if attendance else 0,
        })
    visible = visible_users(request.user).exclude(id=request.user.id)
    counts["present_total"] = counts["present"] + counts["break"] + counts["finished"]
    return Response({
        "date": str(today), "is_workday": workday, "counts": counts, "members": rows,
        "pending": {
            "reports": WorkReport.objects.filter(user__in=visible, status=WorkReport.Status.SUBMITTED).count(),
            "corrections": CorrectionRequest.objects.filter(user__in=visible, status="pending").count(),
            "leaves": LeaveRequest.objects.filter(user__in=visible, status="pending").count(),
        },
    })


@api_view(["GET"])
@permission_classes([IsManagerOrAdmin])
def stats(request):
    start, end = period_from(request)
    users = list(target_users(request).exclude(profile__role=Profile.Role.ADMIN) if not request.query_params.get("user") else target_users(request))
    by_day = defaultdict(lambda: {"present": 0, "late": 0, "absent": 0, "leave": 0})
    by_department = defaultdict(lambda: {"present": 0, "late": 0, "absent": 0, "leave": 0, "worked_minutes": 0, "employees": 0})
    by_employee = []
    totals = {"present": 0, "late": 0, "absent": 0, "leave": 0, "worked_minutes": 0, "overtime_minutes": 0, "late_minutes": 0}
    for user in users:
        s = summarize(user, start, end)
        dept = profile_of(user).department_name or "Sans département"
        for key in totals:
            totals[key] += s[key]
        bucket = by_department[dept]
        bucket["employees"] += 1
        for key in ("present", "late", "absent", "leave", "worked_minutes"):
            bucket[key] += s[key]
        for day, kind in s["days"].items():
            if kind in ("present", "late"):
                by_day[day]["present"] += 1
                if kind == "late":
                    by_day[day]["late"] += 1
            elif kind in ("absent", "leave"):
                by_day[day][kind] += 1
        expected = s["present"] + s["absent"]
        by_employee.append({"id": user.id, "full_name": display_name(user), "department": dept, "present": s["present"], "late": s["late"],
                            "absent": s["absent"], "leave": s["leave"], "hours": hours(s["worked_minutes"]), "overtime_hours": hours(s["overtime_minutes"]),
                            "presence_rate": round(100 * s["present"] / expected, 1) if expected else None})

    def rate(present, absent):
        return round(100 * present / (present + absent), 1) if present + absent else None

    days, day = [], start
    while day <= end:
        days.append({"date": str(day), **by_day.get(day, {"present": 0, "late": 0, "absent": 0, "leave": 0})})
        day += timedelta(days=1)
    return Response({
        "start": str(start), "end": str(end), "employees": len(users),
        "totals": {**totals, "hours": hours(totals["worked_minutes"]), "overtime_hours": hours(totals["overtime_minutes"]), "presence_rate": rate(totals["present"], totals["absent"])},
        "by_day": days,
        "by_department": sorted([{"department": k, **v, "hours": hours(v["worked_minutes"]), "presence_rate": rate(v["present"], v["absent"])} for k, v in by_department.items()], key=lambda x: x["department"]),
        "by_employee": sorted(by_employee, key=lambda x: x["full_name"].lower()),
    })


@api_view(["GET"])
@permission_classes([IsManagerOrAdmin])
def export_monthly(request):
    start, end = month_from(request)
    codes = {"present": "P", "late": "R", "absent": "A", "leave": "C", "holiday": "F"}
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    headers = ["Employé", "Matricule", "Département"] + [d.strftime("%d") for d in days] + ["Présent", "Retards", "Absences", "Congés", "Heures"]
    rows = []
    for user in target_users(request):
        s = summarize(user, start, end)
        profile = profile_of(user)
        rows.append([display_name(user), profile.employee_code or "", profile.department_name]
                    + [codes.get(s["days"].get(d), "") for d in days]
                    + [s["present"], s["late"], s["absent"], s["leave"], hours(s["worked_minutes"])])
    title = f"Feuille de présence — {start:%m/%Y} (P présent, R retard, A absent, C congé, F férié)"
    return export_table(f"presence-{start:%Y-%m}", title, headers, rows, request.query_params.get("format", "xlsx"))


@api_view(["GET"])
@permission_classes([IsAdmin])
def export_payroll(request):
    if request.query_params.get("month"):
        start, end = month_from(request)
    else:
        start, end = period_from(request)
    headers = ["Matricule", "Employé", "Département", "Jours ouvrés", "Jours travaillés", "Retards", "Minutes de retard", "Absences",
               "Jours de congé", "Heures travaillées", "Heures supplémentaires"]
    expected_days = working_days_between(start, end)
    rows = []
    for user in target_users(request):
        s = summarize(user, start, end)
        profile = profile_of(user)
        rows.append([profile.employee_code or "", display_name(user), profile.department_name, expected_days, s["present"], s["late"],
                     s["late_minutes"], s["absent"], s["leave"], hours(s["worked_minutes"]), hours(s["overtime_minutes"])])
    title = f"Récapitulatif paie du {start:%d/%m/%Y} au {end:%d/%m/%Y}"
    audit(request.user, "export_payroll", f"Payroll:{start}:{end}")
    return export_table(f"paie-{start}-{end}", title, headers, rows, request.query_params.get("format", "xlsx"))


# --- Notifications --------------------------------------------------------

@api_view(["GET", "POST"])
def notifications(request):
    qs = Notification.objects.filter(user=request.user)
    if request.method == "POST":
        if request.data.get("all"):
            qs.filter(read=False).update(read=True)
        elif request.data.get("id"):
            qs.filter(id=request.data.get("id")).update(read=True)
    return Response({"unread_count": qs.filter(read=False).count(), "items": NotificationSerializer(qs[:50], many=True).data})


# --- Reports --------------------------------------------------------------

class ReportViewSet(viewsets.ModelViewSet):
    serializer_class = WorkReportSerializer

    def get_queryset(self):
        user = self.request.user
        qs = WorkReport.objects.filter(user__in=visible_users(user)).select_related("user", "user__profile", "validated_by").prefetch_related("attachments")
        params = self.request.query_params
        if params.get("mine") or user_role(user) == Profile.Role.EMPLOYEE:
            qs = qs.filter(user=user)
        elif self.action == "list" and not params.get("user"):
            # managers do not see colleagues' drafts
            qs = qs.filter(Q(user=user) | ~Q(status=WorkReport.Status.DRAFT))
        if params.get("user"):
            qs = qs.filter(user_id=params["user"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("start"):
            qs = qs.filter(date__gte=params["start"])
        if params.get("end"):
            qs = qs.filter(date__lte=params["end"])
        if params.get("department"):
            qs = qs.filter(user__profile__department_name=params["department"])
        if params.get("q"):
            qs = qs.filter(Q(title__icontains=params["q"]) | Q(content__icontains=params["q"]) | Q(tasks__icontains=params["q"]))
        return qs

    def get_object(self):
        report = super().get_object()
        if report.status == WorkReport.Status.DRAFT and report.user_id != self.request.user.id:
            raise PermissionDenied("Ce brouillon n'est pas encore soumis.")
        return report

    def _require_attendance(self, user, day):
        if not Attendance.objects.filter(user=user, date=day).exists():
            raise ValidationError({"date": f"Impossible : aucun pointage enregistré le {day:%d/%m/%Y}. Pointez votre arrivée ou demandez une correction."})

    def _require_editable(self, report):
        if report.user_id != self.request.user.id:
            raise PermissionDenied("Seul l'auteur peut modifier ce rapport.")
        if report.status not in (WorkReport.Status.DRAFT, WorkReport.Status.REJECTED):
            raise PermissionDenied("Ce rapport n'est plus modifiable (déjà soumis ou validé).")

    def perform_create(self, serializer):
        self._require_attendance(self.request.user, serializer.validated_data["date"])
        report = serializer.save(user=self.request.user)
        audit(self.request.user, "create_report", f"Report:{report.id}")

    def perform_update(self, serializer):
        self._require_editable(serializer.instance)
        if "date" in serializer.validated_data:
            self._require_attendance(self.request.user, serializer.validated_data["date"])
        report = serializer.save()
        audit(self.request.user, "update_report", f"Report:{report.id}")

    def perform_destroy(self, instance):
        if instance.user_id != self.request.user.id or instance.status != WorkReport.Status.DRAFT:
            raise PermissionDenied("Seuls vos brouillons peuvent être supprimés.")
        audit(self.request.user, "delete_report", f"Report:{instance.id}", before=snapshot(instance, ["date", "title", "status"]))
        instance.delete()

    @action(detail=True, methods=["POST"])
    def submit(self, request, pk=None):
        report = self.get_object()
        self._require_editable(report)
        self._require_attendance(request.user, report.date)
        report.status = WorkReport.Status.SUBMITTED
        report.save(update_fields=["status", "updated_at"])
        notify(managers_of(request.user), "Rapport à valider", f"{display_name(request.user)} a soumis son rapport du {report.date:%d/%m/%Y}.", link="/equipe/rapports")
        audit(request.user, "submit_report", f"Report:{report.id}")
        return Response(self.get_serializer(report).data)

    @action(detail=True, methods=["POST"])
    def review(self, request, pk=None):
        report = self.get_object()
        if not can_manage(request.user, report.user):
            raise PermissionDenied("Vous ne pouvez pas valider ce rapport.")
        if report.status != WorkReport.Status.SUBMITTED:
            return Response({"detail": "Seuls les rapports soumis peuvent être traités."}, status=400)
        status_value = request.data.get("status")
        if status_value not in [WorkReport.Status.APPROVED, WorkReport.Status.REJECTED]:
            return Response({"detail": "Statut invalide"}, status=400)
        comment = str(request.data.get("comment", "")).strip()
        if status_value == WorkReport.Status.REJECTED and not comment:
            return Response({"detail": "Un commentaire est obligatoire pour un rejet"}, status=400)
        report.status, report.manager_comment, report.validated_by = status_value, comment, request.user
        report.save(update_fields=["status", "manager_comment", "validated_by", "updated_at"])
        verdict = "validé" if status_value == WorkReport.Status.APPROVED else "rejeté"
        notify(report.user, f"Rapport {verdict}", f"Votre rapport du {report.date:%d/%m/%Y} a été {verdict}." + (f" Commentaire : {comment}" if comment else ""), link=f"/rapports/{report.id}")
        audit(request.user, f"{status_value}_report", f"Report:{report.id}", details=comment)
        return Response(self.get_serializer(report).data)

    @action(detail=True, methods=["POST"], parser_classes=[MultiPartParser, FormParser])
    def upload_attachment(self, request, pk=None):
        report = self.get_object()
        self._require_editable(report)
        file_obj = request.FILES.get("file")
        if not file_obj:
            return Response({"detail": "Fichier requis"}, status=400)
        error = validate_upload(file_obj)
        if error:
            return Response({"detail": error}, status=400)
        if report.attachments.count() >= 10:
            return Response({"detail": "10 pièces jointes maximum par rapport."}, status=400)
        attachment = ReportAttachment.objects.create(report=report, file=file_obj, filename=file_obj.name)
        return Response(ReportAttachmentSerializer(attachment, context={"request": request}).data, status=201)

    @action(detail=True, methods=["DELETE"], url_path="attachments/(?P<attachment_id>[^/.]+)")
    def delete_attachment(self, request, pk=None, attachment_id=None):
        report = self.get_object()
        self._require_editable(report)
        attachment = report.attachments.filter(id=attachment_id).first()
        if not attachment:
            return Response({"detail": "Pièce jointe introuvable"}, status=404)
        attachment.file.delete(save=False)
        attachment.delete()
        return Response(status=204)

    @action(detail=True, methods=["GET"])
    def download(self, request, pk=None):
        return report_pdf(self.get_object())

    @action(detail=False, methods=["GET"], permission_classes=[IsManagerOrAdmin])
    def export(self, request):
        rows = [[r.date, display_name(r.user), profile_of(r.user).department_name, r.title, r.get_status_display(), display_name(r.validated_by), r.manager_comment, r.attachments.count()]
                for r in self.get_queryset()]
        headers = ["Date", "Employé", "Département", "Titre", "Statut", "Validé par", "Commentaire", "Pièces jointes"]
        return export_table("rapports", "Rapports d'activité", headers, rows, request.query_params.get("format", "csv"))


# --- Corrections ----------------------------------------------------------

class CorrectionViewSet(viewsets.ModelViewSet):
    serializer_class = CorrectionSerializer
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        qs = CorrectionRequest.objects.filter(user__in=visible_users(self.request.user)).select_related("user", "reviewed_by")
        params = self.request.query_params
        if params.get("mine") or user_role(self.request.user) == Profile.Role.EMPLOYEE:
            qs = qs.filter(user=self.request.user)
        elif params.get("team"):
            qs = qs.exclude(user=self.request.user)
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("user"):
            qs = qs.filter(user_id=params["user"])
        return qs

    def perform_create(self, serializer):
        day = serializer.validated_data["date"]
        if CorrectionRequest.objects.filter(user=self.request.user, date=day, status="pending").exists():
            raise ValidationError({"date": "Une demande est déjà en attente pour ce jour."})
        correction = serializer.save(user=self.request.user)
        notify(managers_of(self.request.user), "Correction de pointage à traiter", f"{display_name(self.request.user)} demande une correction pour le {day:%d/%m/%Y}.", link="/equipe/corrections")
        audit(self.request.user, "request_correction", f"Correction:{correction.id}", details=correction.reason)

    def perform_destroy(self, instance):
        if instance.user_id != self.request.user.id or instance.status != "pending":
            raise PermissionDenied("Seules vos demandes en attente peuvent être annulées.")
        audit(self.request.user, "cancel_correction", f"Correction:{instance.id}")
        instance.delete()

    @action(detail=True, methods=["POST"])
    @transaction.atomic
    def review(self, request, pk=None):
        correction = self.get_object()
        if not can_manage(request.user, correction.user):
            raise PermissionDenied("Vous ne pouvez pas traiter cette demande.")
        if correction.status != "pending":
            return Response({"detail": "Demande déjà traitée"}, status=400)
        status_value = request.data.get("status")
        comment = str(request.data.get("comment", "")).strip()
        if status_value not in ("approved", "rejected"):
            return Response({"detail": "Statut invalide"}, status=400)
        if status_value == "rejected" and not comment:
            return Response({"detail": "Un commentaire est obligatoire pour un refus"}, status=400)
        before = after = ""
        if status_value == "approved":
            attendance = Attendance.objects.select_for_update().filter(user=correction.user, date=correction.date).first()
            before = snapshot(attendance, ATTENDANCE_FIELDS) if attendance else ""
            if not attendance:
                if not correction.requested_check_in:
                    return Response({"detail": "Aucun pointage ce jour : l'heure d'arrivée est nécessaire."}, status=400)
                attendance = Attendance(user=correction.user, date=correction.date, check_in=correction.requested_check_in)
            if correction.requested_check_in:
                attendance.check_in = correction.requested_check_in
            if correction.requested_check_out:
                attendance.check_out = correction.requested_check_out
            if attendance.check_out and attendance.check_out <= attendance.check_in:
                return Response({"detail": "Le départ corrigé doit être après l'arrivée."}, status=400)
            attendance.note = f"Correction approuvée : {correction.reason}"
            recompute(attendance)
            attendance.save()
            after = snapshot(attendance, ATTENDANCE_FIELDS)
        correction.status, correction.reviewed_by, correction.review_comment = status_value, request.user, comment
        correction.save(update_fields=["status", "reviewed_by", "review_comment"])
        verdict = "approuvée" if status_value == "approved" else "refusée"
        notify(correction.user, f"Correction {verdict}", f"Votre correction du {correction.date:%d/%m/%Y} a été {verdict}." + (f" Commentaire : {comment}" if comment else ""), link="/pointage/corrections")
        audit(request.user, f"{status_value}_correction", f"Correction:{correction.id}", details=comment, before=before, after=after)
        return Response(self.get_serializer(correction).data)


# --- Leaves ---------------------------------------------------------------

class LeaveViewSet(viewsets.ModelViewSet):
    serializer_class = LeaveSerializer
    http_method_names = ["get", "post", "head", "options"]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_queryset(self):
        qs = LeaveRequest.objects.filter(user__in=visible_users(self.request.user)).select_related("user", "reviewed_by")
        params = self.request.query_params
        if params.get("mine") or user_role(self.request.user) == Profile.Role.EMPLOYEE:
            qs = qs.filter(user=self.request.user)
        elif params.get("team"):
            qs = qs.exclude(user=self.request.user)
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("user"):
            qs = qs.filter(user_id=params["user"])
        return qs

    def perform_create(self, serializer):
        data = serializer.validated_data
        user = self.request.user
        overlap = LeaveRequest.objects.filter(user=user, status__in=["pending", "approved"], start_date__lte=data["end_date"], end_date__gte=data["start_date"])
        if overlap.exists():
            raise ValidationError({"start_date": "Une demande existe déjà sur cette période."})
        days = working_days_between(data["start_date"], data["end_date"])
        if days == 0:
            raise ValidationError({"start_date": "La période ne contient aucun jour ouvré."})
        if data.get("leave_type", "annual") == "annual" and Decimal(days) > profile_of(user).annual_leave_balance:
            raise ValidationError({"end_date": f"Solde insuffisant : {days} jour(s) demandé(s), {profile_of(user).annual_leave_balance} disponible(s)."})
        leave = serializer.save(user=user)
        notify(managers_of(user), "Demande de congé à traiter",
               f"{display_name(user)} demande un {LEAVE_LABELS.get(leave.leave_type, leave.leave_type).lower()} du {leave.start_date:%d/%m/%Y} au {leave.end_date:%d/%m/%Y} ({days} j).", link="/equipe/conges")
        audit(user, "request_leave", f"Leave:{leave.id}")

    @action(detail=True, methods=["POST"])
    def cancel(self, request, pk=None):
        leave = self.get_object()
        if leave.user_id != request.user.id or leave.status != "pending":
            raise PermissionDenied("Seules vos demandes en attente peuvent être annulées.")
        leave.status = "cancelled"
        leave.save(update_fields=["status"])
        audit(request.user, "cancel_leave", f"Leave:{leave.id}")
        return Response(self.get_serializer(leave).data)

    @action(detail=True, methods=["POST"])
    @transaction.atomic
    def review(self, request, pk=None):
        leave = self.get_object()
        if not can_manage(request.user, leave.user):
            raise PermissionDenied("Vous ne pouvez pas traiter cette demande.")
        if leave.status != "pending":
            return Response({"detail": "Demande déjà traitée"}, status=400)
        status_value = request.data.get("status")
        comment = str(request.data.get("comment", "")).strip()
        if status_value not in ["approved", "rejected"]:
            return Response({"detail": "Statut invalide"}, status=400)
        if status_value == "rejected" and not comment:
            return Response({"detail": "Un commentaire est obligatoire pour un refus"}, status=400)
        if status_value == "approved" and leave.leave_type == "annual":
            profile = Profile.objects.select_for_update().get(user=leave.user)
            days = Decimal(working_days_between(leave.start_date, leave.end_date))
            if days > profile.annual_leave_balance:
                return Response({"detail": f"Solde insuffisant ({profile.annual_leave_balance} j disponibles)."}, status=400)
            profile.annual_leave_balance -= days
            profile.save(update_fields=["annual_leave_balance"])
            leave.days_deducted = days
        leave.status, leave.reviewed_by, leave.review_comment = status_value, request.user, comment
        leave.save(update_fields=["status", "reviewed_by", "review_comment", "days_deducted"])
        verdict = "approuvée" if status_value == "approved" else "refusée"
        notify(leave.user, f"Demande de congé {verdict}", f"Votre demande du {leave.start_date:%d/%m/%Y} au {leave.end_date:%d/%m/%Y} a été {verdict}." + (f" Commentaire : {comment}" if comment else ""), link="/conges")
        audit(request.user, f"{status_value}_leave", f"Leave:{leave.id}", details=comment)
        return Response(self.get_serializer(leave).data)

    @action(detail=False, methods=["GET"])
    def calendar(self, request):
        start, end = month_from(request)
        users = visible_users(request.user)
        if user_role(request.user) == Profile.Role.EMPLOYEE:
            team = profile_of(request.user).team
            users = User.objects.filter(profile__team=team) if team else users
        leaves = LeaveRequest.objects.filter(user__in=users, status__in=["approved", "pending"], start_date__lte=end, end_date__gte=start).select_related("user")
        return Response({
            "start": str(start), "end": str(end),
            "leaves": [{"id": l.id, "user": l.user_id, "full_name": display_name(l.user), "leave_type": l.leave_type, "status": l.status,
                        "start_date": str(l.start_date), "end_date": str(l.end_date)} for l in leaves],
            "holidays": HolidaySerializer(Holiday.objects.filter(date__range=(start, end)), many=True).data,
        })


# --- Organisation ---------------------------------------------------------

class DepartmentViewSet(viewsets.ModelViewSet):
    queryset = Department.objects.all().order_by("name")
    serializer_class = DepartmentSerializer
    permission_classes = [ReadAuthenticatedWriteAdmin]

    def perform_create(self, serializer):
        dept = serializer.save()
        audit(self.request.user, "create_department", f"Department:{dept.name}")

    def perform_update(self, serializer):
        old_name = serializer.instance.name
        dept = serializer.save()
        if old_name != dept.name:
            Profile.objects.filter(department_name=old_name).update(department_name=dept.name)
        audit(self.request.user, "update_department", f"Department:{dept.name}", before=old_name, after=dept.name)

    def perform_destroy(self, instance):
        audit(self.request.user, "delete_department", f"Department:{instance.name}")
        instance.delete()


class TeamViewSet(viewsets.ModelViewSet):
    queryset = Team.objects.all().select_related("manager", "department").order_by("name")
    serializer_class = TeamSerializer
    permission_classes = [ReadAuthenticatedWriteAdmin]

    def perform_create(self, serializer):
        team = serializer.save()
        audit(self.request.user, "create_team", f"Team:{team.name}")

    def perform_update(self, serializer):
        team = serializer.save()
        audit(self.request.user, "update_team", f"Team:{team.name}")

    def perform_destroy(self, instance):
        audit(self.request.user, "delete_team", f"Team:{instance.name}")
        instance.delete()


class UserViewSet(viewsets.ModelViewSet):
    serializer_class = UserSerializer
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsManagerOrAdmin()]
        return [IsAdmin()]

    def get_queryset(self):
        qs = visible_users(self.request.user).select_related("profile", "profile__team")
        params = self.request.query_params
        if params.get("q"):
            q = params["q"]
            qs = qs.filter(Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q) | Q(profile__employee_code__icontains=q))
        if params.get("role"):
            qs = qs.filter(profile__role=params["role"])
        if params.get("department"):
            qs = qs.filter(profile__department_name=params["department"])
        if params.get("team"):
            qs = qs.filter(profile__team_id=params["team"])
        if params.get("active") in ("true", "false"):
            qs = qs.filter(is_active=params["active"] == "true")
        ordering = {"name": ("first_name", "last_name", "username"), "-name": ("-first_name", "-last_name"), "username": ("username",),
                    "-date_joined": ("-date_joined",), "role": ("profile__role", "first_name")}.get(params.get("ordering"), ("first_name", "last_name", "username"))
        return qs.order_by(*ordering)

    def create(self, request, *args, **kwargs):
        data = request.data.copy()
        generated = None
        if not data.get("password"):
            generated = secrets.token_urlsafe(9)
            data["password"] = generated
        else:
            try:
                validate_password(data["password"])
            except DjangoValidationError as exc:
                return Response({"password": exc.messages}, status=400)
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        audit(request.user, "create_user", f"User:{user.username}", after=snapshot(user, ["username", "email", "first_name", "last_name"]))
        payload = self.get_serializer(user).data
        if generated:
            payload["initial_password"] = generated
        return Response(payload, status=201)

    def perform_update(self, serializer):
        instance = serializer.instance
        if instance.id == self.request.user.id and (serializer.validated_data.get("is_active") is False or serializer.validated_data.get("role") not in (None, Profile.Role.ADMIN)):
            raise ValidationError({"detail": "Vous ne pouvez pas désactiver votre propre compte ni retirer votre rôle admin."})
        before = snapshot(instance, ["username", "email", "first_name", "last_name", "is_active"])
        if "password" in serializer.validated_data:
            raise ValidationError({"password": "Utilisez l'action « Réinitialiser le mot de passe »."})
        user = serializer.save()
        audit(self.request.user, "update_user", f"User:{user.username}", before=before,
              after=snapshot(user, ["username", "email", "first_name", "last_name", "is_active"]), details=", ".join(sorted(self.request.data.keys())))

    def perform_destroy(self, instance):
        if instance.id == self.request.user.id:
            raise ValidationError({"detail": "Vous ne pouvez pas supprimer votre propre compte."})
        audit(self.request.user, "delete_user", f"User:{instance.username}", before=snapshot(instance, ["username", "email", "first_name", "last_name"]))
        instance.delete()

    @action(detail=True, methods=["POST"])
    def reset_password(self, request, pk=None):
        user = self.get_object()
        temp = secrets.token_urlsafe(9)
        user.set_password(temp)
        user.save(update_fields=["password"])
        profile = profile_of(user)
        profile.is_locked, profile.failed_login_attempts = False, 0
        profile.save(update_fields=["is_locked", "failed_login_attempts"])
        link = issue_reset_link(user, hours_valid=24)
        audit(request.user, "admin_reset_password", f"User:{user.username}")
        return Response({"detail": f"Mot de passe réinitialisé. Un lien a été envoyé à {user.email or 'l’utilisateur (aucun email)'}.",
                         "temporary_password": temp, "reset_link": link})

    @action(detail=False, methods=["POST"], parser_classes=[MultiPartParser, FormParser])
    def import_csv(self, request):
        file_obj = request.FILES.get("file")
        if not file_obj or not file_obj.name.lower().endswith(".csv"):
            return Response({"detail": "Fichier CSV requis"}, status=400)
        text = file_obj.read().decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text.splitlines()[0] if text else ",", delimiters=",;")
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        teams = {t.name.lower(): t.id for t in Team.objects.all()}
        created, errors = [], []
        for line, row in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not row.get("username"):
                errors.append({"line": line, "error": "username manquant"})
                continue
            data = {key: row[key] for key in ("username", "email", "first_name", "last_name", "employee_code", "work_start_time", "work_end_time") if row.get(key)}
            data["role"] = row.get("role") or "employee"
            data["department_name"] = row.get("department") or row.get("department_name") or ""
            if row.get("team"):
                data["team"] = teams.get(row["team"].lower())
            password = row.get("password") or secrets.token_urlsafe(9)
            data["password"] = password
            serializer = UserSerializer(data=data, context={"request": request})
            if serializer.is_valid():
                with transaction.atomic():
                    user = serializer.save()
                created.append({"username": user.username, "initial_password": None if row.get("password") else password})
            else:
                errors.append({"line": line, "error": "; ".join(f"{k}: {' '.join(map(str, v))}" for k, v in serializer.errors.items())})
        audit(request.user, "import_users", f"CSV:{file_obj.name}", details=f"{len(created)} créés, {len(errors)} erreurs")
        return Response({"created": created, "errors": errors})


class HolidayViewSet(viewsets.ModelViewSet):
    queryset = Holiday.objects.all()
    serializer_class = HolidaySerializer
    permission_classes = [ReadAuthenticatedWriteAdmin]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("year"):
            qs = qs.filter(date__year=self.request.query_params["year"])
        return qs

    def perform_create(self, serializer):
        holiday = serializer.save()
        audit(self.request.user, "create_holiday", f"Holiday:{holiday.date}", after=holiday.name)

    def perform_update(self, serializer):
        before = f"{serializer.instance.date} {serializer.instance.name}"
        holiday = serializer.save()
        audit(self.request.user, "update_holiday", f"Holiday:{holiday.date}", before=before, after=f"{holiday.date} {holiday.name}")

    def perform_destroy(self, instance):
        audit(self.request.user, "delete_holiday", f"Holiday:{instance.date}", before=instance.name)
        instance.delete()


SETTING_FIELDS = ["start_time", "end_time", "late_tolerance_minutes", "workdays", "overtime_rule", "require_location", "require_ip", "allowed_ips", "office_lat", "office_lng", "max_distance_meters"]


@api_view(["GET", "PATCH"])
@permission_classes([ReadAuthenticatedWriteAdmin])
def current_settings(request):
    setting = work_setting()
    if request.method == "PATCH":
        before = snapshot(setting, SETTING_FIELDS)
        serializer = WorkSettingSerializer(setting, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        setting = serializer.save()
        audit(request.user, "update_settings", "WorkSetting", before=before, after=snapshot(setting, SETTING_FIELDS))
    return Response(WorkSettingSerializer(setting).data)


class AuditViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = AuditSerializer
    permission_classes = [IsAdmin]

    def get_queryset(self):
        qs = AuditLog.objects.all().select_related("actor")
        params = self.request.query_params
        if params.get("actor"):
            qs = qs.filter(actor_id=params["actor"])
        if params.get("action"):
            qs = qs.filter(action__icontains=params["action"])
        if params.get("q"):
            qs = qs.filter(Q(target__icontains=params["q"]) | Q(details__icontains=params["q"]))
        if params.get("start"):
            qs = qs.filter(created_at__date__gte=params["start"])
        if params.get("end"):
            qs = qs.filter(created_at__date__lte=params["end"])
        return qs[:1000] if self.action == "list" else qs


# --- Legacy kiosk endpoints (badge code), restricted to managers/admins ----

def presence_for(employee):
    latest = employee.events.first()
    return latest.event_type == AttendanceEvent.EventType.ENTRY if latest else False


@api_view(["GET", "POST"])
@permission_classes([IsManagerOrAdmin])
def employees(request):
    if request.method == "POST":
        serializer = EmployeeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=201)
    return Response(EmployeeSerializer(Employee.objects.filter(active=True), many=True).data)


@api_view(["POST"])
@permission_classes([IsManagerOrAdmin])
@transaction.atomic
def punch(request):
    code = str(request.data.get("employee_code", "")).strip()
    event_type = request.data.get("event_type")
    if event_type not in [AttendanceEvent.EventType.ENTRY, AttendanceEvent.EventType.EXIT]:
        return Response({"detail": "event_type doit être entry ou exit"}, status=400)
    employee = Employee.objects.select_for_update().filter(code=code, active=True).first()
    if not employee:
        return Response({"detail": "Employé introuvable"}, status=404)
    event = AttendanceEvent.objects.create(employee=employee, event_type=event_type, note=request.data.get("note", ""))
    broadcast("punch", event=AttendanceEventSerializer(event).data)
    return Response(AttendanceEventSerializer(event).data, status=201)


@api_view(["GET"])
@permission_classes([IsManagerOrAdmin])
def dashboard(request):
    employees_list = list(Employee.objects.filter(active=True))
    start = timezone.make_aware(datetime.combine(timezone.localdate(), datetime.min.time()))
    present = sum(1 for employee in employees_list if presence_for(employee))
    return Response({
        "total_employees": len(employees_list), "present_count": present,
        "absent_count": len(employees_list) - present, "today_event_count": AttendanceEvent.objects.filter(timestamp__gte=start).count(),
        "recent_events": AttendanceEventSerializer(AttendanceEvent.objects.select_related("employee")[:20], many=True).data,
        "hourly_reports": HourlyReportSerializer(HourlyReport.objects.all()[:24], many=True).data,
        "employees_status": [{"id": e.id, "code": e.code, "full_name": e.full_name, "department": e.department, "present": presence_for(e)} for e in employees_list],
    })


@api_view(["GET", "POST"])
@permission_classes([IsManagerOrAdmin])
def hourly_reports(request):
    if request.method == "GET":
        return Response(HourlyReportSerializer(HourlyReport.objects.all()[:24], many=True).data)
    period_end = timezone.now().replace(minute=0, second=0, microsecond=0)
    period_start = period_end - timedelta(hours=1)
    active = Attendance.objects.filter(check_in__lt=period_end).filter(Q(check_out__isnull=True) | Q(check_out__gt=period_start))
    total_users = User.objects.filter(is_active=True).count()
    total_hours = 0.0
    for attendance in active:
        begin = max(attendance.check_in, period_start)
        finish = min(attendance.check_out or period_end, period_end)
        total_hours += max(0, (finish - begin).total_seconds()) / 3600
    present = active.values("user").distinct().count()
    report, _ = HourlyReport.objects.update_or_create(
        period_start=period_start, period_end=period_end,
        defaults={"total_employees": total_users, "present_count": present, "absent_count": max(0, total_users - present), "total_hours": round(total_hours, 2)},
    )
    broadcast("report", report=HourlyReportSerializer(report).data)
    return Response(HourlyReportSerializer(report).data, status=201)
