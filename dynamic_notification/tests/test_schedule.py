"""Date arithmetic: no database involved."""

from datetime import date, datetime
from types import SimpleNamespace
from unittest import TestCase

from dynamic_notification.engine import schedule


def row(**kw):
	defaults = dict(
		schedule_type="On Date", days=0, send_time="09:00:00", run_at=None, last_run=None,
		frequency=None, repeat_every=None, repeat_unit=None, recur_from=None,
		starts_on=None, ends_on=None, processed_through=None,
	)  # fmt: skip
	return SimpleNamespace(**{**defaults, **kw})


class TestFixedOccurrence(TestCase):
	def test_future_start_is_the_first_occurrence(self):
		start = datetime(2027, 1, 1, 9)
		self.assertEqual(schedule.next_fixed_occurrence(start, 1, "Days", datetime(2026, 1, 1)), start)

	def test_is_strictly_after(self):
		start = datetime(2026, 1, 1, 9)
		got = schedule.next_fixed_occurrence(start, 1, "Days", datetime(2026, 1, 5, 9))
		self.assertEqual(got, datetime(2026, 1, 6, 9))

	def test_months_do_not_drift_after_a_short_month(self):
		start = datetime(2026, 1, 31, 9)
		self.assertEqual(
			schedule.next_fixed_occurrence(start, 1, "Months", datetime(2026, 2, 28, 9)),
			datetime(2026, 3, 31, 9),
		)

	def test_old_start_dates_resolve_correctly(self):
		for unit, every, after, expected in (
			("Months", 1, datetime(2026, 6, 15), datetime(2026, 7, 1, 9)),
			("Years", 1, datetime(2026, 6, 15), datetime(2027, 1, 1, 9)),
			("Weeks", 2, datetime(2000, 1, 2), datetime(2000, 1, 15, 9)),
			("Hours", 6, datetime(2000, 1, 1, 10), datetime(2000, 1, 1, 15)),
		):
			got = schedule.next_fixed_occurrence(datetime(2000, 1, 1, 9), every, unit, after)
			self.assertEqual(got, expected, unit)


class TestRecurrenceDay(TestCase):
	def test_yearly(self):
		self.assertTrue(schedule.is_recurrence_day(date(1990, 3, 5), date(2026, 3, 5), 1, "Years"))
		self.assertFalse(schedule.is_recurrence_day(date(1990, 3, 5), date(2026, 3, 6), 1, "Years"))

	def test_leap_day_clamps_in_a_common_year(self):
		self.assertTrue(schedule.is_recurrence_day(date(2024, 2, 29), date(2026, 2, 28), 1, "Years"))

	def test_month_end_clamps_without_matching_other_days(self):
		anchor = date(2026, 1, 31)
		self.assertTrue(schedule.is_recurrence_day(anchor, date(2026, 2, 28), 1, "Months"))
		self.assertTrue(schedule.is_recurrence_day(anchor, date(2026, 3, 31), 1, "Months"))
		self.assertFalse(schedule.is_recurrence_day(anchor, date(2026, 3, 28), 1, "Months"))

	def test_custom_step(self):
		self.assertTrue(schedule.is_recurrence_day(date(2026, 1, 1), date(2026, 1, 15), 2, "Weeks"))
		self.assertFalse(schedule.is_recurrence_day(date(2026, 1, 1), date(2026, 1, 8), 2, "Weeks"))
		self.assertTrue(schedule.is_recurrence_day(date(2026, 1, 15), date(2026, 4, 15), 3, "Months"))
		self.assertFalse(schedule.is_recurrence_day(date(2026, 1, 15), date(2026, 2, 15), 3, "Months"))

	def test_never_before_the_anchor(self):
		self.assertFalse(schedule.is_recurrence_day(date(2027, 1, 1), date(2026, 1, 1), 1, "Years"))


class TestScanWindow(TestCase):
	today = date(2026, 6, 10)

	def test_first_run_is_today_only(self):
		self.assertEqual(schedule.get_scan_window(None, self.today), [self.today])

	def test_covers_days_missed_since_last_scan(self):
		window = schedule.get_scan_window(date(2026, 6, 8), self.today)
		self.assertEqual(window, [date(2026, 6, 9), self.today])

	def test_long_outage_is_capped(self):
		window = schedule.get_scan_window(date(2026, 1, 1), self.today)
		self.assertEqual(len(window), schedule.CATCH_UP_DAYS + 1)
		self.assertEqual(window[-1], self.today)

	def test_already_processed_today_scans_nothing(self):
		self.assertEqual(schedule.get_scan_window(self.today, self.today), [])


class TestNextRun(TestCase):
	def test_relative_today_when_time_is_still_ahead(self):
		got = schedule.compute_next_run(row(), datetime(2026, 6, 10, 8))
		self.assertEqual(got, datetime(2026, 6, 10, 9))

	def test_relative_tomorrow_when_time_has_passed(self):
		got = schedule.compute_next_run(row(), datetime(2026, 6, 10, 10))
		self.assertEqual(got, datetime(2026, 6, 11, 9))

	def test_relative_tomorrow_when_already_processed_today(self):
		got = schedule.compute_next_run(row(processed_through="2026-06-10"), datetime(2026, 6, 10, 8))
		self.assertEqual(got, datetime(2026, 6, 11, 9))

	def test_one_time_finishes_after_running(self):
		pending = row(schedule_type="One Time", run_at="2026-12-10 09:00:00")
		self.assertEqual(schedule.compute_next_run(pending, datetime(2026, 6, 1)), datetime(2026, 12, 10, 9))
		done = row(schedule_type="One Time", run_at="2026-12-10 09:00:00", last_run="2026-12-10 09:05:00")
		self.assertIsNone(schedule.compute_next_run(done, datetime(2026, 12, 10, 10)))

	def test_recurring_stops_at_end_date(self):
		r = row(
			schedule_type="Recurring", frequency="Daily", recur_from="Fixed Start Date",
			starts_on="2026-01-01 09:00:00", ends_on="2026-01-03",
		)  # fmt: skip
		self.assertEqual(schedule.compute_next_run(r, datetime(2026, 1, 2, 10)), datetime(2026, 1, 3, 9))
		self.assertIsNone(schedule.compute_next_run(r, datetime(2026, 1, 3, 10)))

	def test_signed_offsets(self):
		self.assertEqual(schedule.signed_offset(row(schedule_type="Before Date", days=30)), -30)
		self.assertEqual(schedule.signed_offset(row(schedule_type="After Date", days=7)), 7)
		self.assertEqual(schedule.signed_offset(row(schedule_type="On Date", days=5)), 0)


class TestScheduleEdges(TestCase):
	def test_a_fixed_row_without_a_start_has_no_next_run(self):
		fixed = row(schedule_type="Recurring", frequency="Daily", recur_from="Fixed Start Date")
		self.assertIsNone(schedule.compute_next_run(fixed, datetime(2026, 1, 1, 9, 0)))

	def test_no_occurrence_before_the_schedule_starts(self):
		start, moment = datetime(2026, 2, 1, 9, 0), datetime(2026, 1, 1, 9, 0)
		self.assertIsNone(schedule.last_fixed_occurrence(start, 1, "Days", moment))
