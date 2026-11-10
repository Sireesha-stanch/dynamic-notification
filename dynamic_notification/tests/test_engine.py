"""The engine end to end against a real database.

The engine commits per document by design, so these tests clean up after themselves instead of relying
on a rollback. They use ToDo as well as Employee to prove nothing is specific to one DocType. The sends
are real as far as Notification Log and Email Queue rows; the test email account is never reachable.

Run them on a test site: rules created here match every document of their DocType on that site.
"""

from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_to_date, get_datetime, now_datetime, nowdate

from dynamic_notification.engine import documents, runner
from dynamic_notification.engine.recipients import RecipientResolver
from dynamic_notification.engine.render import Renderer

PREFIX = "DN Test"
HR_USER = "dn-test-hr@example.com"
OTHER_USER = "dn-test-other@example.com"
OUTSIDER = "dn-test-outsider@example.com"
LOG = "Notification Delivery Log"


def ensure_user(email, roles=()):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	user.add_roles(*roles)
	return user


class EngineTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ensure_user(HR_USER, ["HR Manager"])
		ensure_user(OTHER_USER)
		ensure_user(OUTSIDER)
		if not frappe.db.exists("Notification Type", "Alert"):
			frappe.get_doc({"doctype": "Notification Type", "type_name": "Alert"}).insert(
				ignore_permissions=True
			)
		# Outgoing mail goes no further than the queue, so no real address is ever contacted.
		if not frappe.db.exists("Email Account", "DN Test Outgoing"):
			frappe.get_doc(
				{
					"doctype": "Email Account",
					"email_account_name": "DN Test Outgoing",
					"email_id": "dn-test-sender@example.com",
					"enable_outgoing": 1,
					"default_outgoing": 1,
					"smtp_server": "localhost",
					"no_smtp_authentication": 1,
				}
			).db_insert()
		frappe.db.commit()
		frappe.clear_cache()

	def setUp(self):
		frappe.set_user("Administrator")
		self.cleanup()

	def tearDown(self):
		frappe.set_user("Administrator")
		self.cleanup()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.delete("Email Account", {"name": "DN Test Outgoing"})
		for user in (HR_USER, OTHER_USER, OUTSIDER):
			frappe.db.delete("Notification Log", {"for_user": user})
			frappe.db.delete("ToDo", {"owner": user})
			frappe.delete_doc("User", user, force=True, ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_cache()
		super().tearDownClass()

	def cleanup(self):
		rules = frappe.get_all("Notification Rule", {"name": ["like", f"{PREFIX}%"]}, pluck="name")
		for rule in rules:
			queues = frappe.get_all(LOG, {"rule": rule, "email_queue": ["is", "set"]}, pluck="email_queue")
			if queues:
				frappe.db.delete("Email Queue Recipient", {"parent": ["in", queues]})
				frappe.db.delete("Email Queue", {"name": ["in", queues]})
			frappe.db.delete(LOG, {"rule": rule})
			frappe.delete_doc("Notification Rule", rule, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ["like", f"{PREFIX}%"]})
		frappe.db.delete("Notification Log", {"subject": ["like", f"%{PREFIX}%"]})
		frappe.db.delete("Employee", {"employee_name": ["like", f"{PREFIX}%"]})
		frappe.db.commit()

	def make_rule(self, **kw):
		doc = {
			"doctype": "Notification Rule",
			"rule_name": f"{PREFIX} {frappe.generate_hash(length=6)}",
			"document_type": "ToDo",
			"date_field": "date",
			"send_email": 1,
			"send_system_notification": 1,
			"subject": PREFIX + " {{ description }}",
			"message": "Due {{ date }}",
			"reminders": [{"schedule_type": "On Date", "send_time": "09:00:00"}],
			"recipients": [{"recipient_type": "User", "user": HR_USER}],
		}
		doc.update(kw)
		return frappe.get_doc(doc).insert(ignore_permissions=True)

	def make_todo(self, days=0, description="Task", **kw):
		return frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": f"{PREFIX} {description}",
				"date": add_days(nowdate(), days),
				**kw,
			}
		).insert(ignore_permissions=True)

	def run_row(self, rule, idx=0, rescan=False, expect_error=False):
		"""Runs a row as the scheduler would, pretending it has just come due. `rescan` forgets that
		today was scanned, which is what a job crashing before it records its progress looks like."""
		rule.reload()
		row = rule.reminders[idx]
		frappe.db.set_value(row.doctype, row.name, "next_run", add_to_date(now_datetime(), minutes=-1))
		if rescan:
			frappe.db.set_value(row.doctype, row.name, "processed_through", None)
		frappe.db.commit()
		result = runner.process_schedule(rule.name, row.name)
		# The engine swallows and logs job errors by design, which must not read as a passing test.
		self.assertEqual(
			"error" in result, expect_error, frappe.db.get_value(row.doctype, row.name, "last_error")
		)
		return result

	def logs(self, rule, **filters):
		return frappe.get_all(LOG, filters={"rule": rule.name, **filters}, fields=["*"])


class TestSelection(EngineTestCase):
	def test_before_after_and_on_date_each_pick_their_own_documents(self):
		before = self.make_todo(days=30, description="in 30")
		after = self.make_todo(days=-7, description="7 ago")
		on = self.make_todo(days=0, description="today")
		self.make_todo(days=29, description="in 29")

		for schedule_row, expected in (
			({"schedule_type": "Before Date", "days": 30}, before),
			({"schedule_type": "After Date", "days": 7}, after),
			({"schedule_type": "On Date"}, on),
		):
			rule = self.make_rule(reminders=[schedule_row], send_email=0)
			self.run_row(rule)
			self.assertEqual({l.reference_name for l in self.logs(rule)}, {expected.name})

	def test_conditions_exclude_documents(self):
		match = self.make_todo(priority="High", description="high")
		self.make_todo(priority="Low", description="low")
		rule = self.make_rule(conditions=frappe.as_json([["ToDo", "priority", "=", "High"]]), send_email=0)
		self.run_row(rule)
		self.assertEqual({l.reference_name for l in self.logs(rule)}, {match.name})

	def test_paging_reaches_every_document(self):
		todos = {self.make_todo(description=str(i)).name for i in range(7)}
		rule = self.make_rule()
		found = {d.name for d in documents.iter_documents(rule, batch_size=3)}
		self.assertTrue(todos <= found)

	def test_catch_up_covers_missed_days_but_not_older_ones(self):
		yesterday = self.make_todo(days=-1, description="yesterday")
		ancient = self.make_todo(days=-30, description="ancient")
		rule = self.make_rule(send_email=0)
		row = rule.reminders[0]
		frappe.db.set_value(row.doctype, row.name, "processed_through", add_days(nowdate(), -2))
		self.run_row(rule)
		names = {l.reference_name for l in self.logs(rule)}
		self.assertIn(yesterday.name, names)
		self.assertNotIn(ancient.name, names)

	def test_first_run_does_not_back_fire(self):
		yesterday = self.make_todo(days=-1)
		rule = self.make_rule(send_email=0)
		self.run_row(rule)
		self.assertNotIn(yesterday.name, {l.reference_name for l in self.logs(rule)})

	def test_recurring_from_document_date(self):
		anniversary = self.make_todo(days=-14, description="fortnight")
		self.make_todo(days=-15, description="off cycle")
		self.make_todo(days=14, description="future")
		rule = self.make_rule(
			send_email=0,
			reminders=[
				{
					"schedule_type": "Recurring",
					"frequency": "Weekly",
					"recur_from": "Document Date",
					"send_time": "09:00:00",
				}
			],
		)
		self.run_row(rule)
		self.assertEqual({l.reference_name for l in self.logs(rule)}, {anniversary.name})

	def test_fixed_recurring_notifies_all_and_each_occurrence_separately(self):
		todo = self.make_todo(description="any")
		start = add_to_date(now_datetime(), days=-3)
		rule = self.make_rule(
			send_email=0,
			date_field=None,
			message="Hello",
			conditions=frappe.as_json([["ToDo", "name", "=", todo.name]]),
			reminders=[
				{
					"schedule_type": "Recurring",
					"frequency": "Daily",
					"recur_from": "Fixed Start Date",
					"starts_on": start,
				}
			],
		)
		self.run_row(rule)
		self.assertEqual(len(self.logs(rule)), 1)
		rule.reload()
		self.assertGreater(get_datetime(rule.reminders[0].next_run), now_datetime())

		with patch.object(runner, "now_datetime", return_value=now_datetime() + timedelta(days=1)):
			self.run_row(rule)  # A day later: the next occurrence.
		self.assertEqual(len(self.logs(rule)), 2)

	def test_one_time_runs_once_then_retires(self):
		self.make_todo()
		rule = self.make_rule(
			send_email=0,
			date_field=None,
			message="Hello",
			reminders=[{"schedule_type": "One Time", "run_at": add_to_date(now_datetime(), hours=1)}],
		)
		self.run_row(rule)
		rule.reload()
		self.assertIsNone(rule.reminders[0].next_run)
		self.assertTrue(rule.reminders[0].last_run)
		self.assertNotIn(rule.reminders[0].name, [r.name for r in runner.get_due_rows()])


class TestDuplicates(EngineTestCase):
	def test_rerunning_a_row_sends_nothing_new(self):
		self.make_todo()
		rule = self.make_rule()
		self.run_row(rule)
		first = len(self.logs(rule))
		self.assertEqual(first, 2)  # Email and system notification.

		self.run_row(rule, rescan=True)
		self.assertEqual(len(self.logs(rule)), first)
		system_logs = frappe.get_all(
			"Notification Log", {"for_user": HR_USER, "subject": ["like", f"{PREFIX}%"]}
		)
		self.assertEqual(len(system_logs), 1)

	def test_a_second_run_on_the_same_day_is_a_quiet_no_op(self):
		self.make_todo()
		rule = self.make_rule(send_email=0)
		self.run_row(rule)
		count = len(self.logs(rule))
		self.assertEqual(self.run_row(rule), {})
		self.assertEqual(len(self.logs(rule)), count)

	def test_editing_the_schedule_does_not_repeat_a_sent_reminder(self):
		self.make_todo()
		rule = self.make_rule(send_email=0)
		self.run_row(rule)
		self.assertEqual(len(self.logs(rule)), 1)

		# Recreating the same reminder gives a new child row name, but it means the same thing.
		rule.reload()
		rule.set("reminders", [{"schedule_type": "On Date", "send_time": "23:59:00"}])
		rule.save(ignore_permissions=True)
		self.run_row(rule, rescan=True)
		self.assertEqual(len(self.logs(rule)), 1)

	def test_a_changed_document_date_is_a_new_occurrence(self):
		"""A renewed passport has to remind again when the new expiry date comes around."""
		todo = self.make_todo(days=10)
		rule = self.make_rule(send_email=0, reminders=[{"schedule_type": "Before Date", "days": 10}])
		self.run_row(rule)
		self.assertEqual(len(self.logs(rule)), 1)

		frappe.db.set_value("ToDo", todo.name, "date", add_days(nowdate(), 11))
		frappe.db.commit()
		with patch.object(runner, "now_datetime", return_value=now_datetime() + timedelta(days=1)):
			self.run_row(rule)
		self.assertEqual(len(self.logs(rule)), 2)
		self.assertEqual(len({log.occurrence for log in self.logs(rule)}), 2)

	def test_database_rejects_a_duplicate_key(self):
		self.make_todo()
		rule = self.make_rule(send_email=0)
		self.run_row(rule)
		existing = self.logs(rule)[0]
		duplicate = frappe.get_doc(
			{"doctype": LOG, "rule": rule.name, "status": "Sent", "dedupe_key": existing.dedupe_key}
		)
		with self.assertRaises(frappe.UniqueValidationError):
			duplicate.insert(ignore_permissions=True, ignore_links=True)
		frappe.db.rollback()
		frappe.clear_messages()

	def test_a_person_found_two_ways_is_notified_once(self):
		self.make_todo(allocated_to=HR_USER, assigned_by=OTHER_USER)
		rule = self.make_rule(
			send_email=0,
			recipients=[
				{"recipient_type": "User", "user": HR_USER},
				{"recipient_type": "Role", "role": "HR Manager"},
			],
		)
		self.run_row(rule)
		self.assertEqual([l.recipient for l in self.logs(rule) if l.recipient == HR_USER], [HR_USER])


class TestFixedScheduleDuplicates(EngineTestCase):
	def test_rerunning_after_a_crash_does_not_notify_again(self):
		"""The retry moves next_run, which must not change which occurrence this is."""
		later = add_to_date(now_datetime(), hours=1)
		self.make_todo()
		for reminder in (
			{"schedule_type": "One Time", "run_at": later},
			{"schedule_type": "Recurring", "frequency": "Daily", "starts_on": later},
		):
			with self.subTest(reminder=reminder["schedule_type"]):
				rule = self.make_rule(reminders=[reminder])
				original = runner._deliver_row

				def deliver_then_crash(*args):
					original(*args)
					raise RuntimeError("worker crashed after delivering")

				with patch.object(runner, "_deliver_row", deliver_then_crash):
					self.run_row(rule, expect_error=True)
				first = len(self.logs(rule))
				self.assertGreater(first, 0)
				self.run_row(rule)
				self.assertEqual(len(self.logs(rule)), first)


class TestRecipients(EngineTestCase):
	def resolve(self, rule, doc):
		return {r.user or r.email for r in RecipientResolver(rule).resolve(doc)}

	def test_each_recipient_type(self):
		todo = self.make_todo()
		frappe.db.set_value("ToDo", todo.name, "owner", OTHER_USER)
		todo.reload()
		frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": f"{PREFIX} assignment",
				"reference_type": "ToDo",
				"reference_name": todo.name,
				"allocated_to": OUTSIDER,
			}
		).insert(ignore_permissions=True)

		def rule_with(row):
			return self.make_rule(recipients=[row])

		self.assertEqual(
			self.resolve(rule_with({"recipient_type": "User", "user": HR_USER}), todo), {HR_USER}
		)
		self.assertEqual(self.resolve(rule_with({"recipient_type": "Document Owner"}), todo), {OTHER_USER})
		self.assertEqual(self.resolve(rule_with({"recipient_type": "Assigned Users"}), todo), {OUTSIDER})
		self.assertIn(
			HR_USER, self.resolve(rule_with({"recipient_type": "Role", "role": "HR Manager"}), todo)
		)
		self.assertEqual(
			self.resolve(
				rule_with({"recipient_type": "Email Address", "email_address": "x@example.com"}), todo
			),
			{"x@example.com"},
		)
		self.assertEqual(
			self.resolve(rule_with({"recipient_type": "Document Field", "email_field": "owner"}), todo),
			{OTHER_USER},
		)

	def test_disabled_user_is_not_a_recipient(self):
		todo = self.make_todo()
		rule = self.make_rule()
		frappe.db.set_value("User", HR_USER, "enabled", 0)
		try:
			self.assertEqual(self.resolve(rule, todo), set())
		finally:
			frappe.db.set_value("User", HR_USER, "enabled", 1)

	def test_external_email_gets_email_but_no_system_notification(self):
		self.make_todo()
		rule = self.make_rule(
			recipients=[{"recipient_type": "Email Address", "email_address": "x@example.com"}]
		)
		self.run_row(rule)
		self.assertEqual({l.channel for l in self.logs(rule)}, {"Email"})

	def test_no_recipients_is_recorded_once(self):
		todo = self.make_todo()
		rule = self.make_rule(recipients=[{"recipient_type": "Assigned Users"}])
		self.run_row(rule)
		self.run_row(rule, rescan=True)
		skipped = [l for l in self.logs(rule) if l.reference_name == todo.name]
		self.assertEqual([l.status for l in skipped], ["Skipped"])


class TestRendering(EngineTestCase):
	def test_fields_are_rendered_with_display_formats(self):
		todo = self.make_todo(days=5, description="Renew")
		rule = self.make_rule(
			subject="{{ name }}", message="{{ date }} in {{ days_left }} days, raw {{ doc.date }}"
		)
		subject, message = Renderer(rule).render(frappe.get_doc("ToDo", todo.name))
		self.assertEqual(subject, todo.name)
		self.assertIn("in 5 days", message)
		self.assertIn(f"raw {add_days(nowdate(), 5)}", message)

	def test_document_values_cannot_inject_html(self):
		todo = self.make_todo(description="<script>alert(1)</script>")
		rule = self.make_rule(message="{{ description }}")
		_subject, message = Renderer(rule).render(frappe.get_doc("ToDo", todo.name))
		self.assertNotIn("<script>", message)

	def test_template_sandbox(self):
		todo = frappe.get_doc("ToDo", self.make_todo().name)
		for template in (
			"{{ frappe.db.sql('select 1') }}",
			"{{ doc.save() }}",
			"{{ doc.__class__.__mro__ }}",
			"{{ ''.__class__.__mro__ }}",
		):
			rule = self.make_rule(message="x")
			rule.message = template
			try:
				rendered = Renderer(rule).render(todo)[1]
			except Exception:
				continue  # Refusing outright is as good as rendering nothing.
			self.assertNotIn("class", rendered, template)
			self.assertNotIn("1", rendered.replace(todo.name, ""), template)

	def test_unknown_field_and_bad_syntax_are_refused_on_save(self):
		with self.assertRaisesRegex(frappe.ValidationError, "passport_expiry_datee"):
			self.make_rule(message="{{ passport_expiry_datee }}")
		frappe.clear_messages()
		with self.assertRaises(frappe.ValidationError):
			self.make_rule(message="{{ date ")
		frappe.clear_messages()

	def test_password_fields_are_not_exposed(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_rule(document_type="Email Account", date_field=None, message="{{ password }}",
				subject="x", reminders=[{"schedule_type": "One Time", "run_at": add_to_date(now_datetime(), hours=1)}])  # fmt: skip
		frappe.clear_messages()


class TestFailures(EngineTestCase):
	def test_failure_is_logged_retried_and_does_not_stop_the_batch(self):
		self.make_todo(description="a")
		self.make_todo(description="b")
		rule = self.make_rule(send_system_notification=0)
		original = frappe.sendmail
		frappe.sendmail = lambda **kw: (_ for _ in ()).throw(frappe.OutgoingEmailError("smtp down"))
		try:
			self.run_row(rule)
		finally:
			frappe.sendmail = original

		failed = self.logs(rule, status="Failed")
		self.assertGreaterEqual(len(failed), 2)
		self.assertIn("smtp down", failed[0].error_message)
		self.assertTrue(failed[0].next_retry_at)
		frappe.clear_messages()

		for log in failed:
			frappe.db.set_value(LOG, log.name, "next_retry_at", add_to_date(now_datetime(), minutes=-1))
		runner.retry_failed()
		self.assertEqual(self.logs(rule, status="Failed"), [])
		self.assertEqual(len(self.logs(rule, status="Queued")), len(failed))

	def test_retries_stop_at_the_limit(self):
		self.make_todo()
		rule = self.make_rule(send_system_notification=0)
		original = frappe.sendmail
		frappe.sendmail = lambda **kw: (_ for _ in ()).throw(frappe.OutgoingEmailError("smtp down"))
		try:
			self.run_row(rule)
			for _attempt in range(runner.MAX_RETRIES + 1):
				for log in self.logs(rule, status="Failed"):
					if log.next_retry_at:
						frappe.db.set_value(
							LOG, log.name, "next_retry_at", add_to_date(now_datetime(), minutes=-1)
						)
				runner.retry_failed()
		finally:
			frappe.sendmail = original
		log = self.logs(rule, status="Failed")[0]
		self.assertEqual(log.retry_count, runner.MAX_RETRIES)
		self.assertIsNone(log.next_retry_at)
		frappe.clear_messages()

	def test_email_queue_outcome_reaches_the_log(self):
		self.make_todo()
		rule = self.make_rule(send_system_notification=0)
		self.run_row(rule)
		log = self.logs(rule)[0]
		self.assertEqual(log.status, "Queued")

		frappe.db.set_value("Email Queue", log.email_queue, "status", "Sent")
		runner.sync_email_status()
		self.assertEqual(frappe.db.get_value(LOG, log.name, "status"), "Sent")

	def test_a_job_level_failure_backs_off_instead_of_spinning(self):
		rule = self.make_rule()
		original = runner._deliver_row
		runner._deliver_row = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
		try:
			self.run_row(rule, expect_error=True)
		finally:
			runner._deliver_row = original
		rule.reload()
		row = rule.reminders[0]
		self.assertEqual(row.last_status, "Failed")
		self.assertIn("boom", row.last_error)
		self.assertGreater(get_datetime(row.next_run), now_datetime())


class TestFailureRegressions(EngineTestCase):
	def failed_log(self, rule):
		with patch("frappe.sendmail", side_effect=frappe.OutgoingEmailError("smtp down")):
			self.run_row(rule)
		frappe.clear_messages()
		return self.logs(rule, status="Failed")[0]

	def test_a_rule_broken_before_the_send_still_stops_at_the_retry_limit(self):
		self.make_todo()
		log = self.failed_log(self.make_rule(send_system_notification=0))
		with patch.object(documents, "matches", side_effect=RuntimeError("rule is broken")):
			for _attempt in range(runner.MAX_RETRIES + 2):
				if frappe.db.get_value(LOG, log.name, "next_retry_at"):
					frappe.db.set_value(
						LOG, log.name, "next_retry_at", add_to_date(now_datetime(), minutes=-1)
					)
				runner.retry_failed()
		frappe.clear_messages()
		count, next_retry = frappe.db.get_value(LOG, log.name, ["retry_count", "next_retry_at"])
		self.assertEqual((count, next_retry), (runner.MAX_RETRIES, None))

	def test_an_email_missing_from_the_queue_does_not_stay_queued_forever(self):
		self.make_todo()
		rule = self.make_rule(send_system_notification=0)
		self.run_row(rule)
		log = self.logs(rule, status="Queued")[0]
		frappe.db.delete("Email Queue Recipient", {"parent": log.email_queue})
		frappe.db.delete("Email Queue", {"name": log.email_queue})
		runner.sync_email_status()
		self.assertEqual(frappe.db.get_value(LOG, log.name, "status"), "Failed")


class TestPermissions(EngineTestCase):
	def test_rule_requires_read_access_to_the_doctype(self):
		frappe.set_user(OUTSIDER)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{
					"doctype": "Notification Rule",
					"rule_name": f"{PREFIX} sneaky",
					"document_type": "Salary Slip"
					if frappe.db.exists("DocType", "Salary Slip")
					else "Employee",
					"subject": "x",
					"message": "y",
					"reminders": [
						{"schedule_type": "One Time", "run_at": add_to_date(now_datetime(), hours=1)}
					],
					"recipients": [{"recipient_type": "Email Address", "email_address": "x@example.com"}],
				}
			).insert()
		frappe.clear_messages()

	def test_api_refuses_users_without_access(self):
		from dynamic_notification import api

		rule = self.make_rule()
		frappe.set_user(OUTSIDER)
		for call in (
			lambda: api.get_dashboard(),
			lambda: api.preview(rule.name),
			lambda: api.run_now(rule.name),
			lambda: api.set_enabled(rule.name, 0),
			lambda: api.get_doctype_fields("Employee"),
		):
			with self.assertRaises(frappe.PermissionError):
				call()
			frappe.clear_messages()

	def test_bad_conditions_are_refused(self):
		for bad in (
			[["ToDo", "no_such_field", "=", "1"]],
			[["ToDo", "status", "; drop table x", "1"]],
			[["User", "email", "=", "1"]],
		):
			with self.assertRaises(frappe.ValidationError):
				self.make_rule(conditions=frappe.as_json(bad))
			frappe.clear_messages()

	def test_child_and_single_doctypes_are_refused(self):
		for doctype in ("Notification Schedule", "System Settings"):
			with self.assertRaises(frappe.ValidationError):
				self.make_rule(document_type=doctype, date_field=None, reminders=[
					{"schedule_type": "One Time", "run_at": add_to_date(now_datetime(), hours=1)}], message="x", subject="x")  # fmt: skip
			frappe.clear_messages()


class TestPassportUseCase(EngineTestCase):
	"""Section 3 of the assessment. Works on a copy of the rule that the app installs, so that running
	the tests leaves the real rule's schedule and history alone."""

	INSTALLED = "Employee Passport Expiry Notification"

	def setUp(self):
		super().setUp()
		from dynamic_notification.install import create_passport_expiry_rule

		create_passport_expiry_rule()
		self.installed = frappe.get_doc("Notification Rule", self.INSTALLED)
		self.rule = frappe.copy_doc(self.installed)
		self.rule.rule_name = f"{PREFIX} Passport"
		self.rule.insert(ignore_permissions=True)
		frappe.db.commit()

	def make_employee(self, expires_in, status="Active"):
		name = frappe.generate_hash(length=5)
		doc = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": f"{PREFIX} {name}",
				"employee_name": f"{PREFIX} {name}",
				"gender": frappe.get_all("Gender", pluck="name", limit=1)[0],
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"company": frappe.get_all("Company", pluck="name", limit=1)[0],
				"status": "Active",
				"valid_upto": add_days(nowdate(), expires_in),
			}
		).insert(ignore_permissions=True)
		if status != "Active":
			frappe.db.set_value("Employee", doc.name, "status", status)
		frappe.db.commit()
		return doc

	def by_doc(self):
		found = {}
		for log in self.logs(self.rule):
			found.setdefault(log.reference_name, []).append(log)
		return found

	def run_all(self):
		for idx in range(len(self.rule.reminders)):
			self.run_row(self.rule, idx)

	def test_the_installed_rule_matches_the_brief(self):
		rule = self.installed
		self.assertEqual((rule.document_type, rule.date_field), ("Employee", "valid_upto"))
		self.assertEqual(
			[(r.schedule_type, r.days or 0) for r in rule.reminders],
			[("Before Date", 30), ("Before Date", 15), ("On Date", 0)],
		)
		self.assertTrue(rule.send_email and rule.send_system_notification)
		self.assertEqual(rule.recipients[0].role, "HR Manager")
		self.assertEqual(frappe.parse_json(rule.conditions), [["Employee", "status", "=", "Active"]])

	def test_30_15_and_0_days_notify_hr_on_both_channels(self):
		employees = [self.make_employee(days) for days in (30, 15, 0)]
		self.run_all()
		found = self.by_doc()
		for employee in employees:
			logs = [l for l in found[employee.name] if l.recipient == HR_USER]
			self.assertEqual({l.channel for l in logs}, {"Email", "System Notification"}, employee.name)

	def test_other_days_are_not_notified(self):
		for days in (31, 29, 14, 1, -1):
			self.make_employee(days)
		self.run_all()
		names = {e.name for e in frappe.get_all("Employee", {"employee_name": ["like", f"{PREFIX}%"]})}
		self.assertFalse(names & set(self.by_doc()))

	def test_message_includes_name_id_and_expiry(self):
		employee = self.make_employee(30)
		subject, message = Renderer(self.rule).render(frappe.get_doc("Employee", employee.name))
		for text in (employee.employee_name, employee.name):
			self.assertIn(text, subject)
			self.assertIn(text, message)
		self.assertIn(frappe.utils.formatdate(employee.valid_upto), message)
		self.assertIn("30 day(s)", message)

	def test_inactive_employees_are_excluded(self):
		inactive = self.make_employee(30, status="Inactive")
		suspended = self.make_employee(30, status="Suspended")
		left = self.make_employee(30, status="Left")
		active = self.make_employee(30)
		self.run_row(self.rule, 0)
		found = self.by_doc()
		self.assertIn(active.name, found)
		self.assertNotIn(inactive.name, found)
		self.assertNotIn(suspended.name, found)
		self.assertNotIn(left.name, found)

	def test_running_again_does_not_duplicate(self):
		self.make_employee(30)
		self.run_row(self.rule, 0)
		count = len(self.logs(self.rule))
		self.run_row(self.rule, 0, rescan=True)
		self.assertEqual(len(self.logs(self.rule)), count)

	def test_hr_can_change_the_schedule_without_code(self):
		"""As an HR Manager user, through the permission system."""
		employee = self.make_employee(45)
		frappe.set_user(HR_USER)
		rule = frappe.get_doc("Notification Rule", self.rule.name)
		rule.append("reminders", {"schedule_type": "Before Date", "days": 45, "send_time": "09:00:00"})
		rule.save()
		frappe.set_user("Administrator")
		frappe.db.commit()
		rule.reload()
		self.run_row(rule, len(rule.reminders) - 1)
		self.assertIn(employee.name, self.by_doc())
