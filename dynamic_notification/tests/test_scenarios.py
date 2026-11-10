"""One test per scenario in the assessment brief, numbered by its section (B1 = section 2.B, item 1).

The passport use case of section 3 is in test_engine.TestPassportUseCase.
"""

from datetime import timedelta
from unittest.mock import patch

import frappe
from dateutil.relativedelta import relativedelta
from frappe.utils import add_days, add_to_date, get_datetime, getdate, now_datetime, nowdate

from dynamic_notification import api
from dynamic_notification.engine import channels, documents, runner, schedule
from dynamic_notification.engine.recipients import RecipientResolver
from dynamic_notification.engine.render import Renderer
from dynamic_notification.tests.test_engine import HR_USER, LOG, OTHER_USER, OUTSIDER, PREFIX, EngineTestCase

RAW_PREFIX = "DNT-"
CUSTOM_SENT = []


def custom_channel_send(rule, doc, recipient, subject, message):
	CUSTOM_SENT.append((doc.name, recipient.user, subject))
	return channels.ChannelResult("Sent")


CUSTOM_CHANNEL = {"name": "Test Messenger", "flag": "enabled", "address": "user", "send": custom_channel_send}


def raw_insert(doctype, rows):
	"""Bare rows for DocTypes whose real creation needs a full accounting set-up. The engine only
	reads them, so the business validation that is skipped here plays no part in what is tested."""
	now = now_datetime()
	for row in rows:
		row.update(creation=now, modified=now, owner="Administrator", modified_by="Administrator")
		fields = list(row)
		frappe.db.bulk_insert(doctype, fields, [tuple(row[f] for f in fields)])


class TestAssessmentScenarios(EngineTestCase):
	def cleanup(self):
		super().cleanup()
		for doctype in ("Sales Invoice", "Quotation", "Quotation Item"):
			frappe.db.delete(doctype, {"name": ["like", f"{RAW_PREFIX}%"]})
		frappe.db.delete("Notification Log", {"document_name": ["like", f"{RAW_PREFIX}%"]})
		frappe.db.delete("Employee", {"employee_name": ["like", f"{PREFIX}%"]})
		frappe.db.delete("Department", {"department_name": ["like", f"{PREFIX}%"]})
		frappe.db.commit()

	def names(self, rule, **filters):
		return {log.reference_name for log in self.logs(rule, **filters)}

	# -- 2.B Scheduling

	def test_b1_one_time_is_due_only_once_its_time_comes(self):
		run_at = add_to_date(now_datetime(), hours=1)
		rule = self.make_rule(
			date_field=None, message="x", reminders=[{"schedule_type": "One Time", "run_at": run_at}]
		)
		row = rule.reminders[0]
		self.assertEqual(get_datetime(row.next_run), get_datetime(run_at))
		self.assertNotIn(row.name, [r.name for r in runner.get_due_rows()])

		frappe.db.set_value(row.doctype, row.name, "next_run", add_to_date(now_datetime(), minutes=-1))
		self.assertIn(row.name, [r.name for r in runner.get_due_rows()])

	def test_b1_a_sent_one_time_reminder_can_be_rescheduled(self):
		self.make_todo()
		rule = self.make_rule(
			send_email=0,
			date_field=None,
			message="x",
			reminders=[{"schedule_type": "One Time", "run_at": add_to_date(now_datetime(), hours=1)}],
		)
		self.run_row(rule)
		rule.reload()
		self.assertIsNone(rule.reminders[0].next_run)

		new_time = add_to_date(now_datetime(), days=2)
		rule.reminders[0].run_at = new_time
		rule.save(ignore_permissions=True)
		self.assertEqual(get_datetime(rule.reminders[0].next_run), get_datetime(new_time))

	def test_b1_one_time_in_the_past_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "future"):
			self.make_rule(
				date_field=None,
				message="x",
				reminders=[{"schedule_type": "One Time", "run_at": add_to_date(now_datetime(), hours=-1)}],
			)
		frappe.clear_messages()

	def test_b2_b3_before_and_after_need_at_least_one_day(self):
		for when in ("Before Date", "After Date"):
			with self.assertRaises(frappe.ValidationError):
				self.make_rule(reminders=[{"schedule_type": when, "days": 0}])
			frappe.clear_messages()

	def test_b4_recurring_from_each_documents_date(self):
		today = getdate(nowdate())
		cases = [
			("Daily", None, None, today - timedelta(days=3), today + timedelta(days=1)),
			("Weekly", None, None, today - timedelta(weeks=2), today - timedelta(days=10)),
			("Yearly", None, None, today - relativedelta(years=1), today - relativedelta(years=1, days=1)),
			("Custom", 3, "Days", today - timedelta(days=6), today - timedelta(days=4)),
			("Custom", 2, "Months", today - relativedelta(months=4), today - relativedelta(months=3)),
		]
		if today.day <= 28:  # Month ends clamp, which test_schedule covers on its own.
			cases.append(("Monthly", None, None, today - relativedelta(months=1), today - timedelta(days=20)))

		for frequency, every, unit, due_date, other_date in cases:
			with self.subTest(frequency=frequency, every=every, unit=unit):
				due = self.make_todo(description=f"due {frequency}")
				other = self.make_todo(description=f"other {frequency}")
				frappe.db.set_value("ToDo", due.name, "date", due_date)
				frappe.db.set_value("ToDo", other.name, "date", other_date)
				rule = self.make_rule(
					send_email=0,
					conditions=frappe.as_json([["ToDo", "name", "in", [due.name, other.name]]]),
					reminders=[
						{
							"schedule_type": "Recurring",
							"frequency": frequency,
							"repeat_every": every,
							"repeat_unit": unit,
							"recur_from": "Document Date",
							"send_time": "09:00:00",
						}
					],
				)
				self.run_row(rule)
				self.assertEqual(self.names(rule), {due.name})

	def test_b4_fixed_recurrence_schedules_each_frequency(self):
		start = add_to_date(now_datetime(), minutes=-1).replace(microsecond=0)
		for frequency, every, unit in (
			("Daily", None, None),
			("Weekly", None, None),
			("Monthly", None, None),
			("Yearly", None, None),
			("Custom", 2, "Hours"),
		):
			with self.subTest(frequency=frequency):
				rule = self.make_rule(
					date_field=None,
					message="x",
					reminders=[
						{
							"schedule_type": "Recurring",
							"frequency": frequency,
							"repeat_every": every,
							"repeat_unit": unit,
							"recur_from": "Fixed Start Date",
							"starts_on": start,
						}
					],
				)
				step, step_unit = schedule.get_interval(rule.reminders[0])
				self.assertEqual(
					get_datetime(rule.reminders[0].next_run), schedule.add_interval(start, step, step_unit)
				)

	def test_b4_recurrence_stops_at_its_end_date(self):
		rule = self.make_rule(
			date_field=None,
			message="x",
			reminders=[
				{
					"schedule_type": "Recurring",
					"frequency": "Weekly",
					"recur_from": "Fixed Start Date",
					"starts_on": add_to_date(now_datetime(), minutes=-1),
					"ends_on": add_days(nowdate(), 3),
				}
			],
		)
		self.assertIsNone(rule.reminders[0].next_run)

	def test_b5_multiple_reminders_each_fire_on_their_own_day(self):
		todos = {days: self.make_todo(days=days, description=f"in {days}") for days in (30, 15, 7, 0)}
		rule = self.make_rule(
			send_email=0,
			reminders=[
				{"schedule_type": "Before Date", "days": 30},
				{"schedule_type": "Before Date", "days": 15},
				{"schedule_type": "Before Date", "days": 7},
				{"schedule_type": "On Date"},
			],
		)
		for idx in range(4):
			self.run_row(rule, idx)
		by_doc = {}
		for log in self.logs(rule):
			by_doc.setdefault(log.reference_name, []).append(log.schedule_label)
		for days, todo in todos.items():
			self.assertEqual(len(by_doc[todo.name]), 1, days)

	def test_a_rule_needs_a_reminder_and_a_recipient(self):
		for missing, message in (
			("reminders", "at least one reminder"),
			("recipients", "at least one recipient"),
		):
			with self.assertRaisesRegex(frappe.ValidationError, message):
				self.make_rule(**{missing: []})
			frappe.clear_messages()

	def test_b5_a_repeated_reminder_row_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "same as an earlier"):
			self.make_rule(
				reminders=[
					{"schedule_type": "Before Date", "days": 30},
					{"schedule_type": "Before Date", "days": 30},
				]
			)
		frappe.clear_messages()

	# -- 2.C Recipients

	def make_employee(self, name, **kw):
		return frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": f"{PREFIX} {name}",
				"employee_name": f"{PREFIX} {name}",
				"gender": frappe.get_all("Gender", pluck="name", limit=1)[0],
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"company": frappe.get_all("Company", pluck="name", limit=1)[0],
				"status": "Active",
				**kw,
			}
		).insert(ignore_permissions=True)

	def test_c_department_and_document_employee(self):
		company = frappe.get_all("Company", pluck="name", limit=1)[0]
		department = frappe.get_doc(
			{"doctype": "Department", "department_name": f"{PREFIX} Dept", "company": company}
		).insert(ignore_permissions=True)
		manager = self.make_employee("manager", user_id=OTHER_USER, department=department.name)
		left = self.make_employee("left", department=department.name, personal_email="left@example.com")
		frappe.db.set_value("Employee", left.name, "status", "Left")
		report = self.make_employee("report", reports_to=manager.name, company_email="report@example.com")

		dept_rule = self.make_rule(
			recipients=[{"recipient_type": "Department", "department": department.name}]
		)
		found = {
			r.user or r.email
			for r in RecipientResolver(dept_rule).resolve(frappe.get_doc("ToDo", self.make_todo().name))
		}
		self.assertEqual(found, {OTHER_USER})  # The employee who left is not included.

		employee_rule = self.make_rule(
			document_type="Employee",
			date_field="date_of_joining",
			subject="x",
			message="y",
			recipients=[
				{"recipient_type": "Document Employee", "employee_field": "name"},
				{"recipient_type": "Document Employee", "employee_field": "reports_to"},
			],
		)
		found = RecipientResolver(employee_rule).resolve(frappe.get_doc("Employee", report.name))
		self.assertEqual({r.user or r.email for r in found}, {"report@example.com", OTHER_USER})
		self.assertEqual([r.user for r in found if r.email == "report@example.com"], [None])

	def test_c_employee_email_prefers_login_over_personal_address(self):
		employee = self.make_employee("login", user_id=OTHER_USER, personal_email="own@example.com")
		rule = self.make_rule(
			document_type="Employee",
			date_field="date_of_joining",
			subject="x",
			message="y",
			recipients=[{"recipient_type": "Document Employee", "employee_field": "name"}],
		)
		login_email = frappe.db.get_value("User", OTHER_USER, "email")
		found = RecipientResolver(rule).resolve(frappe.get_doc("Employee", employee.name))
		self.assertEqual([(r.user, r.email) for r in found], [(OTHER_USER, login_email)])

		# Unless the employee has chosen another address as their preferred one.
		frappe.db.set_value("Employee", employee.name, "prefered_email", "own@example.com")
		found = RecipientResolver(rule).resolve(frappe.get_doc("Employee", employee.name))
		self.assertEqual([(r.user, r.email) for r in found], [(OTHER_USER, "own@example.com")])

	# -- 2.D Channels

	def test_d_each_channel_alone(self):
		self.make_todo()
		for send_email, send_system, expected in ((1, 0, {"Email"}), (0, 1, {"System Notification"})):
			with self.subTest(expected=expected):
				rule = self.make_rule(send_email=send_email, send_system_notification=send_system)
				self.run_row(rule)
				self.assertEqual({log.channel for log in self.logs(rule)}, expected)

	def test_d_no_channel_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "at least one"):
			self.make_rule(send_email=0, send_system_notification=0)
		frappe.clear_messages()

	def test_d_a_channel_added_by_another_app(self):
		"""Proves the extension point for WhatsApp and similar works without changing this app."""
		todo = self.make_todo()
		original = frappe.get_hooks

		def get_hooks(hook=None, *args, **kwargs):
			if hook == "dynamic_notification_channels":
				return ["dynamic_notification.tests.test_scenarios.CUSTOM_CHANNEL"]
			return original(hook, *args, **kwargs)

		CUSTOM_SENT.clear()
		with patch.object(frappe, "get_hooks", get_hooks):
			rule = self.make_rule(send_email=0, send_system_notification=0)
			self.run_row(rule)
		self.assertEqual([(d, u) for d, u, _s in CUSTOM_SENT], [(todo.name, HR_USER)])
		self.assertEqual({log.channel for log in self.logs(rule)}, {"Test Messenger"})

	# -- 2.E Message

	def test_e_message_can_loop_over_child_rows(self):
		raw_insert(
			"Quotation",
			[{"name": f"{RAW_PREFIX}Q1", "docstatus": 1, "status": "Open", "valid_till": nowdate()}],
		)
		raw_insert(
			"Quotation Item",
			[
				{
					"name": f"{RAW_PREFIX}QI{i}",
					"parent": f"{RAW_PREFIX}Q1",
					"parenttype": "Quotation",
					"parentfield": "items",
					"idx": i,
					"item_code": code,
					"qty": 1,
				}
				for i, code in enumerate(("Pen", "Ink"), 1)
			],
		)
		frappe.db.commit()
		rule = self.make_rule(
			document_type="Quotation",
			date_field="valid_till",
			subject="Quote {{ name }}",
			message="{% for item in doc.items %}{{ item.item_code }};{% endfor %}",
		)
		from dynamic_notification.engine.render import Renderer

		_subject, message = Renderer(rule).render(frappe.get_doc("Quotation", f"{RAW_PREFIX}Q1"))
		self.assertEqual(message, "Pen;Ink;")

	# -- 2.F Conditions

	def test_f_submitted_cancelled_and_outstanding_invoices(self):
		"""The brief's example: notify 7 days after the due date, only for invoices still owing."""
		due = add_days(nowdate(), -7)
		raw_insert(
			"Sales Invoice",
			[
				{"name": f"{RAW_PREFIX}SI-OWING", "docstatus": 1, "outstanding_amount": 100, "due_date": due},
				{"name": f"{RAW_PREFIX}SI-PAID", "docstatus": 1, "outstanding_amount": 0, "due_date": due},
				{
					"name": f"{RAW_PREFIX}SI-CANCELLED",
					"docstatus": 2,
					"outstanding_amount": 100,
					"due_date": due,
				},
				{"name": f"{RAW_PREFIX}SI-DRAFT", "docstatus": 0, "outstanding_amount": 100, "due_date": due},
				{
					"name": f"{RAW_PREFIX}SI-OTHER-DAY",
					"docstatus": 1,
					"outstanding_amount": 100,
					"due_date": nowdate(),
				},
			],
		)
		frappe.db.commit()
		owing = [["Sales Invoice", "outstanding_amount", ">", 0]]
		base = dict(
			document_type="Sales Invoice",
			date_field="due_date",
			subject="Overdue {{ name }}",
			message="{{ outstanding_amount }}",
			send_email=0,
			conditions=frappe.as_json(owing),
			reminders=[{"schedule_type": "After Date", "days": 7}],
		)
		for docstatus_filter, expected in (
			("Submitted Only", {"SI-OWING"}),
			("All Except Cancelled", {"SI-OWING", "SI-DRAFT"}),
			("Draft Only", {"SI-DRAFT"}),
			("All", {"SI-OWING", "SI-DRAFT", "SI-CANCELLED"}),
		):
			with self.subTest(docstatus_filter=docstatus_filter):
				rule = self.make_rule(docstatus_filter=docstatus_filter, **base)
				self.run_row(rule)
				self.assertEqual(self.names(rule), {RAW_PREFIX + name for name in expected})

	def test_f_quotation_status_and_a_child_table_condition(self):
		valid_till = add_days(nowdate(), 3)
		raw_insert(
			"Quotation",
			[
				{"name": f"{RAW_PREFIX}Q-OPEN", "docstatus": 1, "status": "Open", "valid_till": valid_till},
				{"name": f"{RAW_PREFIX}Q-LOST", "docstatus": 1, "status": "Lost", "valid_till": valid_till},
			],
		)
		raw_insert(
			"Quotation Item",
			[
				{
					"name": f"{RAW_PREFIX}QI-{i}",
					"parent": f"{RAW_PREFIX}Q-OPEN",
					"parenttype": "Quotation",
					"parentfield": "items",
					"idx": i,
					"item_code": "Pen",
					"qty": 1,
				}
				for i in (1, 2)  # Two matching rows must still mean one notification.
			],
		)
		frappe.db.commit()
		conditions = [["Quotation", "status", "=", "Open"], ["Quotation Item", "item_code", "=", "Pen"]]
		rule = self.make_rule(
			document_type="Quotation",
			date_field="valid_till",
			subject="Follow up {{ name }}",
			message="x",
			send_email=0,
			conditions=frappe.as_json(conditions),
			reminders=[{"schedule_type": "Before Date", "days": 3}],
		)
		self.run_row(rule)
		self.assertEqual([log.reference_name for log in self.logs(rule)], [f"{RAW_PREFIX}Q-OPEN"])
		self.assertEqual(api.count_matching("Quotation", frappe.as_json(conditions)), 1)

	def test_f_sensitive_fields_cannot_be_conditions(self):
		with self.assertRaisesRegex(frappe.ValidationError, "can.t be used"):
			documents.validate_conditions("Email Account", [["Email Account", "password", "=", "guess"]])
		frappe.clear_messages()

	def test_f_creation_date_works_as_a_datetime_date_field(self):
		todo = self.make_todo()
		rule = self.make_rule(send_email=0, date_field="creation")
		self.run_row(rule)
		self.assertIn(todo.name, self.names(rule))

	# -- 2.G Management

	def test_g_dashboard_shows_rules_history_and_next_notification(self):
		self.make_todo(days=5)
		rule = self.make_rule(send_email=0, reminders=[{"schedule_type": "Before Date", "days": 5}])
		later = self.make_rule(send_email=0, reminders=[{"schedule_type": "Before Date", "days": 2}])
		self.run_row(rule)

		data = api.get_dashboard()
		rows = {r.name: r for r in data["rules"]}
		self.assertEqual(rows[rule.name].sent, 1)
		self.assertTrue(rows[rule.name].can_write)
		self.assertEqual(rows[later.name].next_notification["at"].date(), add_days(getdate(nowdate()), 3))
		self.assertIn(rule.name, {log.rule for log in data["recent"]})

	def test_g_a_broken_rule_does_not_break_the_dashboard(self):
		rule = self.make_rule()
		frappe.db.set_value("Notification Rule", rule.name, "date_field", "field_that_was_deleted")
		frappe.db.commit()
		rows = {r.name: r for r in api.get_dashboard()["rules"]}
		self.assertEqual(rows[rule.name].last_status, "Failed")

	def test_g_disable_enable_and_run_now(self):
		self.make_todo()
		rule = self.make_rule(send_email=0)
		api.set_enabled(rule.name, 0)
		rule.reload()
		self.assertIsNone(rule.next_run)
		self.assertNotIn(rule.name, {r.parent for r in runner.get_due_rows()})
		with self.assertRaisesRegex(frappe.ValidationError, "switched off"):
			api.run_now(rule.name)
		frappe.clear_messages()

		# Switched off for a while: that time is not an outage to catch up on.
		frappe.db.set_value(
			rule.reminders[0].doctype, rule.reminders[0].name, "processed_through", "2020-01-01"
		)
		api.set_enabled(rule.name, 1)
		rule.reload()
		self.assertTrue(rule.next_run)
		self.assertIsNone(rule.reminders[0].processed_through)

	def test_g_retry_from_the_dashboard(self):
		self.make_todo()
		rule = self.make_rule(send_system_notification=0)
		with patch.object(frappe, "sendmail", side_effect=frappe.OutgoingEmailError("smtp down")):
			self.run_row(rule)
		frappe.clear_messages()
		failed = self.logs(rule, status="Failed")[0]
		self.assertEqual(api.retry_delivery(failed.name), "Queued")

	def test_housekeeping_steps_are_isolated(self):
		synced = []
		with (
			patch.object(runner, "retry_failed", side_effect=RuntimeError("retry broke")),
			patch.object(runner, "sync_email_status", side_effect=lambda: synced.append(1)),
		):
			runner.tick()
		self.assertEqual(synced, [1])
		frappe.db.delete("Error Log", {"method": ["like", "Notification engine:%"]})
		frappe.db.commit()

	def test_permissions_hr_without_employee_access_cannot_write_employee_rules(self):
		frappe.set_user(OUTSIDER)
		with self.assertRaises(frappe.PermissionError):
			api.get_upcoming("Employee Passport Expiry Notification")
		frappe.clear_messages()

	# -- Coverage added after end-to-end testing

	def test_e_the_briefs_own_passport_example_works(self):
		expires = add_days(nowdate(), 30)
		employee = self.make_employee("brief", valid_upto=expires)
		rule = self.make_rule(
			document_type="Employee",
			date_field="valid_upto",
			subject="Passport of {{ employee_name }}",
			message="Dear {{ employee_name }},\nYour passport will expire on {{ passport_expiry_date }}.",
		)
		message = Renderer(rule).render(frappe.get_doc("Employee", employee.name))[1]
		self.assertIn(frappe.utils.formatdate(expires), message)
		self.assertIn("Passport Expiry Date", rule.reminders[0].summary)

	def test_e_what_would_fail_at_send_time_is_refused_on_save(self):
		for template in ("{{ doc.__class__ }}", "{{ name | no_such_filter }}"):
			with self.subTest(template=template), self.assertRaises(frappe.ValidationError):
				self.make_rule(message=template)
			frappe.clear_messages()

	def test_failure_reason_is_readable_with_the_detail_kept(self):
		error = (
			'Traceback (most recent call last):\n  File "x.py", line 1\n'
			"frappe.exceptions.OutgoingEmailError: Please set up <b>default</b> account"
		)
		explained = runner.explain_failure(error)
		self.assertEqual(explained.splitlines()[0], "Please set up default account")
		self.assertIn("x.py", explained)
		self.assertEqual(runner.explain_failure("Recipient has unsubscribed"), "Recipient has unsubscribed")

	def test_g_dashboard_does_not_load_each_rule_document(self):
		rule = self.make_rule()
		real = frappe.get_doc

		def no_rule_documents(*args, **kwargs):
			if args and args[0] == "Notification Rule":
				raise AssertionError("the dashboard should read rows in bulk")
			return real(*args, **kwargs)

		with patch("frappe.get_doc", no_rule_documents):
			dashboard = api.get_dashboard()
		row = next(r for r in dashboard["rules"] if r.name == rule.name)
		self.assertEqual(row.schedule, [r.summary for r in rule.reminders])

	def test_blank_required_settings_are_refused_in_plain_words(self):
		"""The form asks for these too, but a rule saved without one would silently never send."""
		later = add_to_date(now_datetime(), hours=1)
		start = {"recur_from": "Fixed Start Date", "starts_on": later}
		for reminder, expected in (
			({"schedule_type": "One Time"}, "Date and Time"),
			({"schedule_type": "Recurring", "frequency": "", **start}, "Repeat"),
			({"schedule_type": "Recurring", "frequency": "Daily", "recur_from": "Fixed Start Date"}, "Starts On"),
			({"schedule_type": "Recurring", "frequency": "Custom", "repeat_every": 2, "repeat_unit": "", **start}, "Repeat Unit"),
		):  # fmt: skip
			with (
				self.subTest(reminder=reminder),
				self.assertRaisesRegex(frappe.ValidationError, f"Reminder 1: .*{expected}"),
			):
				self.make_rule(reminders=[reminder])
			frappe.clear_messages()

		for recipient, expected in (
			({"recipient_type": "Role"}, "Role"),
			({"recipient_type": "User"}, "User"),
			({"recipient_type": "Department"}, "Department"),
			({"recipient_type": "Email Address"}, "Email Address"),
			({"recipient_type": "Email Address", "email_address": "not-an-email"}, "look like an email"),
		):
			with (
				self.subTest(recipient=recipient),
				self.assertRaisesRegex(frappe.ValidationError, f"Recipient 1: .*{expected}"),
			):
				self.make_rule(recipients=[recipient])
			frappe.clear_messages()

	def test_refused_access_says_what_to_do(self):
		frappe.set_user(OUTSIDER)
		try:
			with self.assertRaisesRegex(frappe.PermissionError, "ask your administrator"):
				api.get_dashboard()
		finally:
			frappe.set_user("Administrator")
			frappe.clear_messages()

	# -- Installation, uninstallation and the reusable demonstration

	def test_installing_again_changes_nothing(self):
		from dynamic_notification.install import PASSPORT_RULE, QUOTATION_RULE, after_install

		after_install()
		frappe.db.set_value("Notification Rule", QUOTATION_RULE, "subject", f"{PREFIX} changed by HR")
		after_install()
		for rule in (PASSPORT_RULE, QUOTATION_RULE):
			self.assertEqual(frappe.db.count("Notification Rule", {"name": rule}), 1)
		self.assertEqual(
			frappe.db.get_value("Notification Rule", QUOTATION_RULE, "subject"), f"{PREFIX} changed by HR"
		)
		frappe.db.rollback()

	def test_uninstall_removes_the_scheduled_job(self):
		from dynamic_notification.install import before_uninstall

		filters = {"method": "dynamic_notification.engine.runner.tick"}
		self.assertTrue(frappe.db.exists("Scheduled Job Type", filters))
		before_uninstall()
		self.assertFalse(frappe.db.exists("Scheduled Job Type", filters))
		frappe.db.rollback()

	def test_the_installed_quotation_rule_works_on_another_doctype(self):
		from dynamic_notification.install import QUOTATION_RULE, create_quotation_followup_rule

		create_quotation_followup_rule()
		installed = frappe.get_doc("Notification Rule", QUOTATION_RULE)
		self.assertEqual((installed.document_type, installed.date_field), ("Quotation", "valid_till"))

		rule = frappe.copy_doc(installed)
		rule.rule_name = f"{PREFIX} Quotation"
		rule.enabled = 1
		rule.insert(ignore_permissions=True)
		valid_till = add_days(nowdate(), 3)
		common = {
			"docstatus": 1,
			"valid_till": valid_till,
			"owner": HR_USER,
			"company": "x",
			"customer_name": "Acme",
		}
		raw_insert(
			"Quotation",
			[
				{"name": f"{RAW_PREFIX}Q-OPEN", "status": "Open", **common},
				{"name": f"{RAW_PREFIX}Q-LOST", "status": "Lost", **common},
			],
		)
		self.run_row(rule)
		self.assertEqual(self.names(rule), {f"{RAW_PREFIX}Q-OPEN"})

	def test_aliases_are_configuration_not_engine_logic(self):
		from dynamic_notification.engine.render import get_aliases

		self.assertEqual(get_aliases("Employee"), {"passport_expiry_date": "valid_upto"})
		self.assertEqual(get_aliases("Quotation"), {})
		with patch("frappe.get_hooks", return_value=[]), self.assertRaises(frappe.ValidationError):
			self.make_rule(
				document_type="Employee", date_field="valid_upto", message="{{ passport_expiry_date }}"
			)
		frappe.clear_messages()

	def test_g_dashboard_lists_failed_deliveries_with_their_reason(self):
		self.make_todo()
		rule = self.make_rule()
		with patch("frappe.sendmail", side_effect=frappe.OutgoingEmailError("smtp down")):
			self.run_row(rule)
		frappe.clear_messages()
		failed = [log for log in api.get_dashboard()["failures"] if log.rule == rule.name]
		self.assertTrue(failed)
		self.assertEqual(failed[0].error_message.splitlines()[0], "smtp down")

	# -- Found by reading the code

	def test_g_upcoming_lists_a_document_once_with_a_child_table_condition(self):
		valid_till = add_days(nowdate(), 20)
		raw_insert(
			"Quotation",
			[{"name": f"{RAW_PREFIX}Q-UP", "docstatus": 1, "status": "Open", "valid_till": valid_till}],
		)
		raw_insert(
			"Quotation Item",
			[
				{"name": f"{RAW_PREFIX}UP-{i}", "parent": f"{RAW_PREFIX}Q-UP", "parenttype": "Quotation", "parentfield": "items", "idx": i, "item_code": "Pen", "qty": 1}
				for i in (1, 2)
			],
		)  # fmt: skip
		frappe.db.commit()
		conditions = frappe.as_json([["Quotation Item", "item_code", "=", "Pen"]])
		for date_field in ("valid_till", "creation"):
			with self.subTest(date_field=date_field):
				rule = self.make_rule(
					document_type="Quotation",
					date_field=date_field,
					subject="x",
					message="y",
					conditions=conditions,
					reminders=[{"schedule_type": "After Date", "days": 1, "send_time": "23:59:00"}],
				)
				found = [
					i["document"] for i in api.get_upcoming(rule.name) if i["document"] == f"{RAW_PREFIX}Q-UP"
				]
				self.assertLessEqual(len(found), 1)

	def test_a_rule_switched_on_later_gets_its_date_index(self):
		rule = self.make_rule(enabled=0)
		rule.reload()
		rule.enabled = 1
		with patch("frappe.enqueue") as enqueue:
			rule.save(ignore_permissions=True)
		self.assertTrue(any(call.kwargs.get("fieldname") == "date" for call in enqueue.call_args_list))

	def test_e_a_private_attribute_by_subscript_is_refused_on_save(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_rule(message="{{ doc['__class__'] }}")
		frappe.clear_messages()

	def test_f_malformed_conditions_are_refused_in_plain_words(self):
		for bad in ("not json", "5", '{"a": 1}'):
			with self.subTest(bad=bad), self.assertRaisesRegex(frappe.ValidationError, "Custom Condition"):
				documents.validate_conditions("ToDo", documents.parse_conditions(bad))
			frappe.clear_messages()

	def test_g_dashboard_works_with_no_rules_and_sends_only_the_reason(self):
		self.assertEqual(api._get_enabled_reminders([]), {})
		self.make_todo()
		rule = self.make_rule()
		with patch("frappe.sendmail", side_effect=frappe.OutgoingEmailError("smtp down")):
			self.run_row(rule)
		frappe.clear_messages()
		failed = [log for log in api.get_dashboard()["failures"] if log.rule == rule.name]
		self.assertTrue(failed)
		self.assertNotIn("Traceback", failed[0].error_message)

	# -- Employee status and notice period conditions

	def test_f_employee_status_options_are_offered_as_quick_conditions(self):
		presets = api.get_doctype_fields("Employee")["condition_presets"]
		labels = [preset["label"] for preset in presets]
		self.assertEqual(
			labels[:4], ["Status: Active", "Status: Inactive", "Status: Suspended", "Status: Left"]
		)
		self.assertIn("Serving notice period", labels)
		self.assertEqual(presets[0]["conditions"], [["Employee", "status", "=", "Active"]])

	def test_f_quick_conditions_are_built_from_each_doctypes_own_status(self):
		quotation = api.get_doctype_fields("Quotation")["condition_presets"]
		self.assertIn("Status: Open", [preset["label"] for preset in quotation])
		self.assertNotIn("Serving notice period", [preset["label"] for preset in quotation])
		self.assertEqual(api.get_doctype_fields("Note")["condition_presets"], [])

	def test_f_outstanding_invoices_are_offered_as_a_condition(self):
		presets = {
			p["label"]: p["conditions"] for p in api.get_doctype_fields("Sales Invoice")["condition_presets"]
		}
		self.assertEqual(presets["Has outstanding amount"], [["Sales Invoice", "outstanding_amount", ">", 0]])

	def test_f_a_quick_condition_on_a_missing_field_is_not_offered(self):
		broken = [{"doctype": "ToDo", "label": "Broken", "conditions": [["no_such_field", "=", "1"]]}]
		meta = frappe.get_meta("ToDo")
		real = frappe.get_hooks

		def hooks(name=None, *args, **kwargs):
			return broken if name == "dynamic_notification_condition_presets" else real(name, *args, **kwargs)

		with patch("frappe.get_hooks", hooks):
			labels = [preset["label"] for preset in api._get_condition_presets("ToDo", meta)]
		self.assertNotIn("Broken", labels)

	def test_f_notifications_follow_active_and_notice_period_conditions(self):
		valid_upto = add_days(nowdate(), 30)
		employees = {}
		for name, status, relieving, notice in (
			("active", "Active", None, 0),
			("notice", "Active", add_days(nowdate(), 40), 60),
			("left", "Left", add_days(nowdate(), -5), 60),
			("inactive", "Inactive", None, 0),
		):
			doc = self.make_employee(name, valid_upto=valid_upto)
			frappe.db.set_value(
				"Employee",
				doc.name,
				{"status": status, "relieving_date": relieving, "notice_number_of_days": notice},
			)
			employees[name] = doc.name
		frappe.db.commit()

		presets = {
			p["label"]: p["conditions"] for p in api.get_doctype_fields("Employee")["condition_presets"]
		}
		for label, conditions, expected in (
			("active only", presets["Status: Active"], {"active", "notice"}),
			("left only", presets["Status: Left"], {"left"}),
			("notice period", presets["Serving notice period"], {"notice"}),
			("notice days", [["Employee", "notice_number_of_days", ">", 0]], {"notice", "left"}),
			(
				"active, not on notice",
				[["Employee", "status", "=", "Active"], ["Employee", "relieving_date", "is", "not set"]],
				{"active"},
			),
		):
			with self.subTest(label=label):
				rule = self.make_rule(
					document_type="Employee",
					date_field="valid_upto",
					subject="x",
					message="y",
					send_email=0,
					conditions=frappe.as_json(conditions),
					reminders=[{"schedule_type": "Before Date", "days": 30}],
				)
				self.run_row(rule)
				notified = {name for name, value in employees.items() if value in self.names(rule)}
				self.assertEqual(notified, expected)

	def test_g_upcoming_says_which_document_to_open(self):
		todo = self.make_todo(days=5)
		later = add_to_date(now_datetime(), hours=2)
		rule = self.make_rule(
			conditions=frappe.as_json([["ToDo", "name", "=", todo.name]]),
			reminders=[
				{"schedule_type": "Before Date", "days": 1, "send_time": "09:00:00"},
				{"schedule_type": "One Time", "run_at": later},
			],
		)
		rule.reload()
		items = {item["document"]: item for item in runner.get_upcoming(rule, 10)}
		self.assertEqual(items[todo.name]["reference_doctype"], "ToDo")
		self.assertNotIn("reference_doctype", items["All matching documents"])
