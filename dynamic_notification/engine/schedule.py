"""Date arithmetic for reminder schedules.

Everything here works on naive datetimes in the site's system timezone and touches neither the
database nor the session, so it can be unit tested without a site.
"""

from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta
from frappe.utils import get_datetime, get_time, getdate

BEFORE = "Before Date"
ON = "On Date"
AFTER = "After Date"
ONE_TIME = "One Time"
RECURRING = "Recurring"
FROM_DOCUMENT = "Document Date"
FROM_START = "Fixed Start Date"

FREQUENCIES = {
	"Daily": (1, "Days"),
	"Weekly": (1, "Weeks"),
	"Monthly": (1, "Months"),
	"Yearly": (1, "Years"),
}

# How far back a daily scan reaches when the scheduler was down. Bounded so that a long outage
# does not release a flood of stale reminders.
CATCH_UP_DAYS = 3


def is_relative(row) -> bool:
	return row.schedule_type in (BEFORE, ON, AFTER)


def is_document_recurring(row) -> bool:
	return row.schedule_type == RECURRING and row.recur_from == FROM_DOCUMENT


def needs_date_field(row) -> bool:
	return is_relative(row) or is_document_recurring(row)


def signed_offset(row) -> int:
	"""Days between the document's date and the day it is notified: -30 for "30 days before"."""
	if row.schedule_type == BEFORE:
		return -int(row.days or 0)
	if row.schedule_type == AFTER:
		return int(row.days or 0)
	return 0


def get_interval(row) -> tuple[int, str]:
	if row.frequency in FREQUENCIES:
		return FREQUENCIES[row.frequency]
	return int(row.repeat_every or 1), row.repeat_unit or "Days"


def add_interval(start: datetime | date, count: int, unit: str):
	"""Add `count` units to `start`, always measured from `start`, so 31 Jan + 2 months is 31 Mar
	rather than the 28 Mar that stepping through February would give."""
	return start + relativedelta(**{unit.lower(): count})


def _last_index(start: datetime, every: int, unit: str, moment: datetime) -> int:
	"""Largest k with `start + k * every units` not later than `moment`. Requires start <= moment."""
	seconds = {"Hours": 3600, "Days": 86400, "Weeks": 604800, "Months": 2419200, "Years": 31536000}[unit]
	# Jump close to the answer so that an old start date does not mean thousands of iterations. The
	# unit sizes are only approximate, so the estimate is corrected in both directions.
	k = int((moment - start).total_seconds() // (seconds * every))
	while k > 0 and add_interval(start, k * every, unit) > moment:
		k -= 1
	while add_interval(start, (k + 1) * every, unit) <= moment:
		k += 1
	return k


def next_fixed_occurrence(start: datetime, every: int, unit: str, after: datetime) -> datetime:
	"""First occurrence of `start + k * every units` strictly later than `after`."""
	if start > after:
		return start
	return add_interval(start, (_last_index(start, every, unit, after) + 1) * every, unit)


def last_fixed_occurrence(start: datetime, every: int, unit: str, at: datetime) -> datetime | None:
	"""Latest occurrence not later than `at`, or None when the schedule has not started."""
	if start > at:
		return None
	return add_interval(start, _last_index(start, every, unit, at) * every, unit)


def is_recurrence_day(anchor: date, day: date, every: int, unit: str) -> bool:
	"""Whether `day` is `anchor + k * every units`, k >= 0. Month-end anchors clamp, so a 31 Jan
	anchor recurs on 28 Feb, and 29 Feb recurs on 28 Feb in a common year."""
	if anchor > day:
		return False
	if unit in ("Days", "Weeks"):
		step = every * (7 if unit == "Weeks" else 1)
		return (day - anchor).days % step == 0

	months = (day.year - anchor.year) * 12 + day.month - anchor.month
	step = every * (12 if unit == "Years" else 1)
	return months >= 0 and months % step == 0 and anchor + relativedelta(months=months) == day


def get_scan_window(processed_through: date | None, today: date) -> list[date]:
	"""Calendar days a daily scan has to cover: everything since the last completed scan, capped
	at CATCH_UP_DAYS. Dates are `today` only on a first run, so a brand new rule never back-fires."""
	start = processed_through + timedelta(days=1) if processed_through else today
	start = max(start, today - timedelta(days=CATCH_UP_DAYS))
	return [start + timedelta(days=i) for i in range((today - start).days + 1)]


def compute_next_run(row, now: datetime) -> datetime | None:
	"""When the engine next has to look at this row, or None when it has nothing left to do."""
	if row.schedule_type == ONE_TIME:
		return None if row.last_run or not row.run_at else get_datetime(row.run_at)

	if row.schedule_type == RECURRING and not is_document_recurring(row):
		if not row.starts_on:
			return None
		every, unit = get_interval(row)
		next_run = next_fixed_occurrence(get_datetime(row.starts_on), every, unit, now)
	else:
		today = now.date()
		send_time = get_time(row.send_time or "09:00:00")
		next_run = datetime.combine(today, send_time)
		done_today = row.processed_through and getdate(row.processed_through) >= today
		if next_run <= now or done_today:
			next_run += timedelta(days=1)

	if row.schedule_type == RECURRING and row.ends_on and next_run.date() > getdate(row.ends_on):
		return None
	return next_run


def describe(row, date_label: str | None = None) -> str:
	"""A one line, human readable version of the row, shown in the grid and on the dashboard."""
	date_label = date_label or "the date"
	at = f"at {get_time(row.send_time or '09:00:00').strftime('%H:%M')}"

	if row.schedule_type == ONE_TIME:
		return f"Once on {get_datetime(row.run_at).strftime('%d %b %Y %H:%M')}" if row.run_at else "Once"
	if row.schedule_type == BEFORE:
		return f"{row.days} day(s) before {date_label} {at}"
	if row.schedule_type == AFTER:
		return f"{row.days} day(s) after {date_label} {at}"
	if row.schedule_type == ON:
		return f"On {date_label} {at}"

	every, unit = get_interval(row)
	label = row.frequency if row.frequency in FREQUENCIES else f"Every {every} {unit.lower()}"
	if is_document_recurring(row):
		text = f"{label} from {date_label} {at}"
	else:
		starts = get_datetime(row.starts_on).strftime("%d %b %Y %H:%M") if row.starts_on else "?"
		text = f"{label} from {starts}"
	return f"{text} until {getdate(row.ends_on).strftime('%d %b %Y')}" if row.ends_on else text
