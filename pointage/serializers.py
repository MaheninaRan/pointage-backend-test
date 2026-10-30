import secrets

from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.contrib.auth.models import update_last_login
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.settings import api_settings
from .models import (
    AttendanceEvent,
    Employee,
    HourlyReport,
    Department,
    Team,
    Attendance,
    WorkReport,
    ReportAttachment,
    CorrectionRequest,
    LeaveRequest,
    Holiday,
    AuditLog,
    Profile,
    Break,
    Notification,
    WorkSetting,
)

User = get_user_model()


def absolute_url(serializer, file_field):
    if not file_field:
        return None
    request = serializer.context.get("request")
    return request.build_absolute_uri(file_field.url) if request else file_field.url


class LoginSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        profile, _ = Profile.objects.get_or_create(user=user)
        token["role"] = profile.role
        token["username"] = user.username
        return token

    def validate(self, attrs):
        identifier = str(attrs.get(self.username_field, "")).strip()
        password = attrs.get("password", "")

        user_obj = User.objects.filter(
            Q(username__iexact=identifier) | Q(email__iexact=identifier)
        ).first()

        if user_obj:
            profile, _ = Profile.objects.get_or_create(user=user_obj)
            if not user_obj.is_active:
                raise AuthenticationFailed(
                    {"detail": "Compte désactivé. Veuillez contacter l'administrateur RH.", "code": "account_disabled"}
                )
            if profile.is_locked:
                raise AuthenticationFailed(
                    {"detail": "Compte bloqué suite à trop de tentatives échouées. Réinitialisez votre mot de passe ou contactez un administrateur.", "code": "account_locked"}
                )
            if not user_obj.check_password(password):
                profile.failed_login_attempts += 1
                if profile.failed_login_attempts >= 5:
                    profile.is_locked = True
                profile.save(update_fields=["failed_login_attempts", "is_locked"])
                if profile.is_locked:
                    raise AuthenticationFailed(
                        {"detail": "Compte bloqué après 5 tentatives incorrectes.", "code": "account_locked"}
                    )
                remaining = 5 - profile.failed_login_attempts
                raise AuthenticationFailed(
                    {"detail": f"Identifiants incorrects. ({remaining} tentative(s) restante(s))", "code": "invalid_credentials"}
                )
            if profile.failed_login_attempts > 0:
                profile.failed_login_attempts = 0
                profile.save(update_fields=["failed_login_attempts"])
        else:
            raise AuthenticationFailed(
                {"detail": "Identifiants incorrects (email/identifiant ou mot de passe invalide).", "code": "invalid_credentials"}
            )

        # The password is already verified above; issuing tokens directly avoids hashing it a second time via authenticate().
        self.user = user_obj
        refresh = self.get_token(user_obj)
        if api_settings.UPDATE_LAST_LOGIN:
            update_last_login(None, user_obj)
        return {"refresh": str(refresh), "access": str(refresh.access_token)}


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)
    role = serializers.CharField(required=False, write_only=True)
    employee_code = serializers.CharField(required=False, allow_blank=True, allow_null=True, write_only=True)
    department_name = serializers.CharField(required=False, allow_blank=True, write_only=True)
    team = serializers.PrimaryKeyRelatedField(queryset=Team.objects.all(), required=False, allow_null=True, write_only=True)
    phone = serializers.CharField(required=False, allow_blank=True, write_only=True)
    photo = serializers.ImageField(required=False, allow_null=True, write_only=True)
    work_start_time = serializers.TimeField(required=False, write_only=True)
    work_end_time = serializers.TimeField(required=False, write_only=True)
    annual_leave_balance = serializers.DecimalField(max_digits=5, decimal_places=1, required=False, write_only=True)
    is_locked = serializers.BooleanField(required=False, write_only=True)
    language = serializers.CharField(required=False, write_only=True)

    class Meta:
        model = User
        fields = [
            "id", "username", "first_name", "last_name", "email", "is_active",
            "password", "role", "employee_code", "department_name", "team",
            "phone", "photo", "work_start_time", "work_end_time",
            "annual_leave_balance", "is_locked", "language", "last_login", "date_joined",
        ]
        read_only_fields = ["last_login", "date_joined"]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        profile, _ = Profile.objects.get_or_create(user=instance)
        data["full_name"] = f"{instance.first_name} {instance.last_name}".strip() or instance.username
        data["role"] = profile.role
        data["employee_code"] = profile.employee_code
        data["department_name"] = profile.department_name
        data["team"] = profile.team_id
        data["team_name"] = profile.team.name if profile.team else ""
        data["phone"] = profile.phone
        data["photo"] = absolute_url(self, profile.photo)
        data["work_start_time"] = str(profile.work_start_time)[:5] if profile.work_start_time else "08:00"
        data["work_end_time"] = str(profile.work_end_time)[:5] if profile.work_end_time else "17:00"
        data["annual_leave_balance"] = float(profile.annual_leave_balance)
        data["is_locked"] = profile.is_locked
        data["language"] = profile.language or "fr"
        return data

    def validate_email(self, value):
        value = (value or "").strip()
        if value and User.objects.filter(email__iexact=value).exclude(pk=getattr(self.instance, "pk", None)).exists():
            raise serializers.ValidationError("Cet email est déjà utilisé.")
        return value

    def validate_role(self, value):
        if value not in Profile.Role.values:
            raise serializers.ValidationError("Rôle invalide.")
        return value

    def validate_employee_code(self, value):
        value = (value or "").strip() or None
        if value and Profile.objects.filter(employee_code=value).exclude(user_id=getattr(self.instance, "pk", None)).exists():
            raise serializers.ValidationError("Ce matricule est déjà attribué.")
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", None) or secrets.token_urlsafe(9)
        role = validated_data.pop("role", "employee")
        employee_code = validated_data.pop("employee_code", None)
        department_name = validated_data.pop("department_name", "")
        team = validated_data.pop("team", None)
        phone = validated_data.pop("phone", "")
        photo = validated_data.pop("photo", None)
        work_start_time = validated_data.pop("work_start_time", "08:00")
        work_end_time = validated_data.pop("work_end_time", "17:00")
        annual_leave_balance = validated_data.pop("annual_leave_balance", 30.0)
        is_locked = validated_data.pop("is_locked", False)
        language = validated_data.pop("language", "fr")
        is_active = validated_data.pop("is_active", True)

        user = User.objects.create_user(
            username=validated_data["username"],
            email=validated_data.get("email", ""),
            password=password,
            first_name=validated_data.get("first_name", ""),
            last_name=validated_data.get("last_name", ""),
        )
        user.is_active = is_active
        user.save(update_fields=["is_active"])

        Profile.objects.create(
            user=user, role=role, employee_code=employee_code or None,
            department_name=department_name, team=team, phone=phone, photo=photo,
            work_start_time=work_start_time, work_end_time=work_end_time,
            annual_leave_balance=annual_leave_balance, is_locked=is_locked, language=language,
        )
        if employee_code:
            full_name = f"{user.first_name} {user.last_name}".strip() or user.username
            Employee.objects.update_or_create(
                code=employee_code,
                defaults={"full_name": full_name, "department": department_name, "active": is_active},
            )
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        role = validated_data.pop("role", None)
        employee_code = validated_data.pop("employee_code", None)
        department_name = validated_data.pop("department_name", None)
        team = validated_data.pop("team", "__unset__")
        phone = validated_data.pop("phone", None)
        photo = validated_data.pop("photo", None)
        work_start_time = validated_data.pop("work_start_time", None)
        work_end_time = validated_data.pop("work_end_time", None)
        annual_leave_balance = validated_data.pop("annual_leave_balance", None)
        is_locked = validated_data.pop("is_locked", None)
        language = validated_data.pop("language", None)

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()

        profile, _ = Profile.objects.get_or_create(user=instance)
        if role is not None:
            profile.role = role
        if employee_code is not None:
            profile.employee_code = employee_code or None
        if department_name is not None:
            profile.department_name = department_name
        if team != "__unset__":
            profile.team = team
        if phone is not None:
            profile.phone = phone
        if photo is not None:
            profile.photo = photo
        if work_start_time is not None:
            profile.work_start_time = work_start_time
        if work_end_time is not None:
            profile.work_end_time = work_end_time
        if annual_leave_balance is not None:
            profile.annual_leave_balance = annual_leave_balance
        if is_locked is not None:
            profile.is_locked = is_locked
            if not is_locked:
                profile.failed_login_attempts = 0
        if language is not None:
            profile.language = language
        profile.save()

        if profile.employee_code:
            full_name = f"{instance.first_name} {instance.last_name}".strip() or instance.username
            Employee.objects.update_or_create(
                code=profile.employee_code,
                defaults={"full_name": full_name, "department": profile.department_name, "active": instance.is_active},
            )
        return instance


class DepartmentSerializer(serializers.ModelSerializer):
    teams_count = serializers.SerializerMethodField()

    class Meta:
        model = Department
        fields = ["id", "name", "description", "teams_count"]

    def get_teams_count(self, obj):
        return obj.teams.count()


class TeamSerializer(serializers.ModelSerializer):
    manager_name = serializers.SerializerMethodField()
    department_name = serializers.CharField(source="department.name", read_only=True, default="")
    member_ids = serializers.ListField(child=serializers.IntegerField(), write_only=True, required=False)
    members_info = serializers.SerializerMethodField()

    class Meta:
        model = Team
        fields = ["id", "name", "manager", "manager_name", "department", "department_name", "member_ids", "members_info"]

    def get_manager_name(self, obj):
        if not obj.manager:
            return ""
        return f"{obj.manager.first_name} {obj.manager.last_name}".strip() or obj.manager.username

    def get_members_info(self, obj):
        profiles = obj.members.select_related("user").all()
        return [
            {
                "id": p.user_id,
                "username": p.user.username,
                "full_name": f"{p.user.first_name} {p.user.last_name}".strip() or p.user.username,
            }
            for p in profiles
        ]

    def create(self, validated_data):
        member_ids = validated_data.pop("member_ids", None)
        team = super().create(validated_data)
        if member_ids is not None:
            Profile.objects.filter(user_id__in=member_ids).update(team=team)
        return team

    def update(self, instance, validated_data):
        member_ids = validated_data.pop("member_ids", None)
        team = super().update(instance, validated_data)
        if member_ids is not None:
            Profile.objects.filter(team=team).exclude(user_id__in=member_ids).update(team=None)
            Profile.objects.filter(user_id__in=member_ids).update(team=team)
        return team


class BreakSerializer(serializers.ModelSerializer):
    duration_minutes = serializers.SerializerMethodField()

    class Meta:
        model = Break
        fields = ["id", "attendance", "started_at", "ended_at", "duration_minutes"]

    def get_duration_minutes(self, obj):
        if obj.started_at and obj.ended_at:
            return max(0, int((obj.ended_at - obj.started_at).total_seconds() // 60))
        return 0


class AttendanceSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    full_name = serializers.SerializerMethodField()
    department_name = serializers.SerializerMethodField()
    breaks = BreakSerializer(many=True, read_only=True)
    break_minutes = serializers.SerializerMethodField()
    active_break = serializers.SerializerMethodField()

    class Meta:
        model = Attendance
        fields = [
            "id", "user", "username", "full_name", "department_name",
            "date", "check_in", "check_out", "status", "late_minutes", "worked_minutes",
            "overtime_minutes", "note", "ip_address", "latitude", "longitude",
            "breaks", "break_minutes", "active_break",
        ]

    def get_full_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip() or obj.user.username

    def get_department_name(self, obj):
        profile = getattr(obj.user, "profile", None)
        return profile.department_name if profile else ""

    def get_break_minutes(self, obj):
        total = 0
        for b in obj.breaks.all():
            if b.started_at and b.ended_at:
                total += max(0, int((b.ended_at - b.started_at).total_seconds() // 60))
        return total

    def get_active_break(self, obj):
        return obj.breaks.filter(ended_at__isnull=True).exists()


class ReportAttachmentSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = ReportAttachment
        fields = ["id", "report", "file", "file_url", "filename", "uploaded_at"]

    def get_file_url(self, obj):
        return absolute_url(self, obj.file) or ""


class WorkReportSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    full_name = serializers.SerializerMethodField()
    department_name = serializers.SerializerMethodField()
    validated_by_name = serializers.SerializerMethodField()
    attachments = ReportAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = WorkReport
        fields = [
            "id", "user", "username", "full_name", "department_name",
            "date", "title", "content", "tasks", "status", "manager_comment",
            "validated_by", "validated_by_name", "attachments", "created_at", "updated_at",
        ]
        read_only_fields = ["user", "status", "validated_by", "created_at", "updated_at"]

    def get_full_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip() or obj.user.username

    def get_department_name(self, obj):
        profile = getattr(obj.user, "profile", None)
        return profile.department_name if profile else ""

    def get_validated_by_name(self, obj):
        if not obj.validated_by:
            return ""
        return f"{obj.validated_by.first_name} {obj.validated_by.last_name}".strip() or obj.validated_by.username


class CorrectionSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    full_name = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CorrectionRequest
        fields = [
            "id", "user", "username", "full_name", "date",
            "requested_check_in", "requested_check_out", "reason",
            "status", "reviewed_by", "reviewed_by_name", "review_comment", "created_at",
        ]
        read_only_fields = ["user", "status", "reviewed_by", "review_comment", "created_at"]

    def get_full_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip() or obj.user.username

    def get_reviewed_by_name(self, obj):
        if not obj.reviewed_by:
            return ""
        return f"{obj.reviewed_by.first_name} {obj.reviewed_by.last_name}".strip() or obj.reviewed_by.username

    def validate(self, attrs):
        from django.utils import timezone
        if attrs.get("date") and attrs["date"] > timezone.localdate():
            raise serializers.ValidationError({"date": "Impossible de corriger une date future."})
        if not attrs.get("requested_check_in") and not attrs.get("requested_check_out"):
            raise serializers.ValidationError({"requested_check_in": "Indiquez au moins une heure corrigée."})
        if attrs.get("requested_check_in") and attrs.get("requested_check_out") and attrs["requested_check_out"] <= attrs["requested_check_in"]:
            raise serializers.ValidationError({"requested_check_out": "Le départ doit être après l'arrivée."})
        if not str(attrs.get("reason", "")).strip():
            raise serializers.ValidationError({"reason": "Le motif est obligatoire."})
        return attrs


class LeaveSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    full_name = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()
    days_count = serializers.SerializerMethodField()
    attachment_url = serializers.SerializerMethodField()

    class Meta:
        model = LeaveRequest
        fields = [
            "id", "user", "username", "full_name", "leave_type",
            "start_date", "end_date", "days_count", "reason",
            "attachment", "attachment_url", "status",
            "reviewed_by", "reviewed_by_name", "review_comment", "days_deducted", "created_at",
        ]
        read_only_fields = ["user", "status", "reviewed_by", "review_comment", "days_deducted", "created_at"]

    LEAVE_TYPES = ["annual", "sick", "unpaid", "maternity", "exceptional", "other"]

    def validate(self, attrs):
        if attrs.get("leave_type", "annual") not in self.LEAVE_TYPES:
            raise serializers.ValidationError({"leave_type": "Type de congé invalide."})
        start, end = attrs.get("start_date"), attrs.get("end_date")
        if start and end and end < start:
            raise serializers.ValidationError({"end_date": "La date de fin doit être après la date de début."})
        attachment = attrs.get("attachment")
        if attachment:
            from .services import validate_upload
            error = validate_upload(attachment)
            if error:
                raise serializers.ValidationError({"attachment": error})
        return attrs

    def get_full_name(self, obj):
        return f"{obj.user.first_name} {obj.user.last_name}".strip() or obj.user.username

    def get_reviewed_by_name(self, obj):
        if not obj.reviewed_by:
            return ""
        return f"{obj.reviewed_by.first_name} {obj.reviewed_by.last_name}".strip() or obj.reviewed_by.username

    def get_days_count(self, obj):
        if obj.start_date and obj.end_date and obj.end_date >= obj.start_date:
            return (obj.end_date - obj.start_date).days + 1
        return 1

    def get_attachment_url(self, obj):
        return absolute_url(self, obj.attachment) or ""


class HolidaySerializer(serializers.ModelSerializer):
    class Meta:
        model = Holiday
        fields = "__all__"


class AuditSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = ["id", "actor", "actor_name", "action", "target", "before_state", "after_state", "details", "created_at"]

    def get_actor_name(self, obj):
        if not obj.actor:
            return "Système"
        return f"{obj.actor.first_name} {obj.actor.last_name}".strip() or obj.actor.username


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = "__all__"
        read_only_fields = ["user", "created_at"]


class WorkSettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkSetting
        fields = "__all__"

    def validate_workdays(self, value):
        days = [d.strip() for d in str(value).split(",") if d.strip()]
        if not days or any(not d.isdigit() or not 1 <= int(d) <= 7 for d in days):
            raise serializers.ValidationError("Jours ouvrés invalides (1 = lundi … 7 = dimanche).")
        return ",".join(sorted(set(days)))

    def validate_overtime_rule(self, value):
        if value not in ["after_8_hours", "after_end_time", "none"]:
            raise serializers.ValidationError("Règle d'heures supplémentaires invalide.")
        return value


class EmployeeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Employee
        fields = ["id", "code", "full_name", "department", "active", "created_at"]


class AttendanceEventSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    employee_code = serializers.CharField(source="employee.code", read_only=True)

    class Meta:
        model = AttendanceEvent
        fields = ["id", "employee_name", "employee_code", "event_type", "timestamp", "note"]


class HourlyReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = HourlyReport
        fields = "__all__"
