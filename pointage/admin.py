from django.contrib import admin
from .models import AttendanceEvent, Employee, HourlyReport


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("code", "full_name", "department", "active")
    search_fields = ("code", "full_name", "department")


@admin.register(AttendanceEvent)
class AttendanceEventAdmin(admin.ModelAdmin):
    list_display = ("employee", "event_type", "timestamp")
    list_filter = ("event_type", "timestamp")


@admin.register(HourlyReport)
class HourlyReportAdmin(admin.ModelAdmin):
    list_display = ("period_start", "period_end", "present_count", "absent_count", "total_hours")
