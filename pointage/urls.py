from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from . import views

router = DefaultRouter()
router.register("reports", views.ReportViewSet, basename="reports")
router.register("departments", views.DepartmentViewSet, basename="departments")
router.register("teams", views.TeamViewSet, basename="teams")
router.register("users", views.UserViewSet, basename="users")
router.register("corrections", views.CorrectionViewSet, basename="corrections")
router.register("leaves", views.LeaveViewSet, basename="leaves")
router.register("holidays", views.HolidayViewSet, basename="holidays")
router.register("audit", views.AuditViewSet, basename="audit")
router.register("attendance/records", views.AttendanceRecordViewSet, basename="attendance-records")

urlpatterns = [
    path("auth/login/", views.LoginView.as_view(), name="login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("auth/register/", views.register, name="register"),
    path("auth/me/", views.me, name="me"),
    path("auth/password/forgot/", views.password_forgot, name="password-forgot"),
    path("auth/password/reset/", views.password_reset, name="password-reset"),
    path("auth/password/change/", views.change_password, name="password-change"),
    path("notifications/", views.notifications, name="notifications"),
    path("attendance/time/", views.server_time, name="server-time"),
    path("attendance/today/", views.today_attendance, name="today-attendance"),
    path("attendance/day-status/", views.day_status, name="day-status"),
    path("attendance/check/", views.check_server, name="server-check"),
    path("attendance/break/", views.break_action, name="break-action"),
    path("attendance/history/", views.attendance_history, name="attendance-history"),
    path("attendance/export/", views.export_attendance, name="attendance-export"),
    path("team/dashboard/", views.team_dashboard, name="team-dashboard"),
    path("stats/", views.stats, name="stats"),
    path("exports/monthly/", views.export_monthly, name="export-monthly"),
    path("exports/payroll/", views.export_payroll, name="export-payroll"),
    path("settings/", views.current_settings, name="settings"),
    # legacy badge kiosk
    path("employees/", views.employees, name="employees"),
    path("punch/", views.punch, name="punch"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("hourly-reports/", views.hourly_reports, name="hourly-reports"),
    path("", include(router.urls)),
]
