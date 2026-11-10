"""Module by module checks of the paths that the scenario tests do not reach."""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_to_date, format_datetime, now_datetime, nowdate

from dynamic_notification import api, install
from dynamic_notification.dynamic_notification.doctype.notification_delivery_log.notification_delivery_log import (
	NotificationDeliveryLog,
)
from dynamic_notification.dynamic_notification.doctype.notification_rule.notification_rule import (
	ensure_date_index,
)
from dynamic_notification.engine import documents, render, runner
from dynamic_notification.engine.recipients import Recipient, RecipientResolver
from dynamic_notification.tests.test_engine import HR_USER, LOG, PREFIX, EngineTestCase

ONE_DAY_BEFORE = [{"schedule_type": "Before Date", "days": 1}]


class TestApi(EngineTestCase):
	def test_field_choices_follow_the_doctype(self):
		employee = api.get_doctype_fields("Employee")
		dates = {option["value"]: option["label"] for option in employee["date_fields"]}
		self.assertEqual(dates["valid_upto"], "Passport Expiry Date (valid_upto)")
		self.assertIn("creation", dates)
		self.assertEqual(employee["employee_fields"][0]["value"], "name")

		todo = api.get_doctype_fields("ToDo")
		self.assertIn("date", {option["value"] for option in todo["date_fields"]})
		self.assertNotIn("valid_upto", {option["value"] for option in todo["date_fields"]})
		self.assertIn("allocated_to", {option["value"] for option in todo["email_fields"]})

	def test_password_fields_are_never_offered(self):
		fields = api.get_doctype_fields("Email Account")["message_fields"]
		self.assertNotIn("password", {option["value"] for option in fields})

	def test_preview_shows_the_message_and_sends_nothing(self):
		todo = self.make_todo(description="Preview me")
		rule = self.make_rule(conditions=frappe.as_json([["ToDo", "name", "=", todo.name]]))
		queued = frappe.db.count("Email Queue")
		result = api.preview(rule.name)
		self.assertEqual(result["docname"], todo.name)
		self.assertIn("Preview me", result["subject"])
		self.assertEqual(result["recipients"], [HR_USER])
		self.assertTrue(result["matches_conditions"])
		self.assertEqual((frappe.db.count("Email Queue"), self.logs(rule)), (queued, []))

	def test_preview_explains_when_nothing_matches(self):
		rule = self.make_rule(conditions=frappe.as_json([["ToDo", "name", "=", "no such document"]]))
		with self.assertRaisesRegex(frappe.ValidationError, "nothing to preview"):
			api.preview(rule.name)
		frappe.clear_messages()

	def test_run_now_queues_one_job_for_the_person_who_pressed_it(self):
		rule = self.make_rule(reminders=[*ONE_DAY_BEFORE, {"schedule_type": "On Date"}])
		with patch("frappe.enqueue") as enqueue:
			api.run_now(rule.name)
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["user"], "Administrator")
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])

	def test_run_now_reports_what_really_happened(self):
		self.make_todo()
		rule = self.make_rule(reminders=[*ONE_DAY_BEFORE, {"schedule_type": "On Date"}], send_email=0)
		with patch("frappe.publish_realtime") as publish:
			runner.run_rule_now(rule.name, HR_USER)
		self.assertEqual(publish.call_args.args[1]["counts"], {"Sent": 1})
		self.assertEqual(publish.call_args.kwargs["user"], HR_USER)

		with patch("frappe.publish_realtime") as publish:
			runner.run_rule_now(rule.name, HR_USER)
		self.assertEqual(publish.call_args.args[1], {"rule": rule.name, "counts": {}, "errors": []})
		self.assertEqual(len(self.logs(rule)), 1)

	def test_count_matching(self):
		self.make_todo(description="one")
		self.make_todo(description="two")
		conditions = frappe.as_json([["ToDo", "description", "like", f"{PREFIX}%"]])
		self.assertEqual(api.count_matching("ToDo", conditions), 2)


class TestRunnerEdges(EngineTestCase):
	def failed_log(self, rule):
		with patch("frappe.sendmail", side_effect=frappe.OutgoingEmailError("smtp down")):
			self.run_row(rule)
		frappe.clear_messages()
		return self.logs(rule, status="Failed")[0]

	def test_tick_queues_a_deduplicated_job_for_each_due_row(self):
		rule = self.make_rule()
		frappe.db.set_value(
			rule.reminders[0].doctype, rule.reminders[0].name, "next_run", add_days(nowdate(), -1)
		)
		with patch("frappe.enqueue") as enqueue:
			runner.tick()
		call = next(c for c in enqueue.call_args_list if c.kwargs["rule_name"] == rule.name)
		self.assertTrue(call.kwargs["deduplicate"])
		self.assertEqual(call.kwargs["queue"], "long")

	def test_a_job_that_is_no_longer_due_does_nothing(self):
		self.make_todo()
		rule = self.make_rule()
		row = rule.reminders[0]
		frappe.db.set_value(row.doctype, row.name, "next_run", add_days(nowdate(), 2))
		self.assertEqual(runner.process_schedule(rule.name, row.name), {})
		frappe.db.set_value(row.doctype, row.name, {"next_run": add_days(nowdate(), -1), "enabled": 0})
		self.assertEqual(runner.process_schedule(rule.name, row.name), {})
		self.assertEqual(self.logs(rule), [])

	def test_one_bad_document_does_not_stop_the_others(self):
		bad, good = self.make_todo(description="bad"), self.make_todo(description="good")
		real = RecipientResolver.resolve

		def resolve(resolver, doc):
			if doc.name == bad.name:
				raise RuntimeError("cannot resolve this one")
			return real(resolver, doc)

		rule = self.make_rule()
		with patch.object(RecipientResolver, "resolve", resolve):
			self.run_row(rule)
			self.run_row(rule, rescan=True)
		blocked = [log for log in self.logs(rule) if log.reference_name == bad.name]
		self.assertEqual([(log.status, log.channel) for log in blocked], [("Failed", "-")])
		self.assertEqual(blocked[0].error_message.splitlines()[0], "cannot resolve this one")
		self.assertTrue([log for log in self.logs(rule) if log.reference_name == good.name])

	def test_a_rendering_failure_is_logged_and_retried(self):
		self.make_todo()
		rule = self.make_rule()
		with patch.object(render.Renderer, "render", side_effect=RuntimeError("cannot render")):
			self.run_row(rule)
		logs = self.logs(rule)
		self.assertTrue(logs)
		self.assertTrue(all(log.status == "Failed" and log.next_retry_at for log in logs))

	def test_two_workers_cannot_claim_the_same_delivery(self):
		rule = self.make_rule()
		delivery = runner._Delivery(rule, rule.reminders[0])
		recipient = Recipient(HR_USER, HR_USER)
		claim = lambda: delivery._claim("k" * 64, "DOC", "at:1", recipient, "Email", "subject")  # noqa: E731
		self.assertTrue(claim())
		self.assertIsNone(claim())

	def test_run_now_never_delivers_a_future_occurrence(self):
		self.make_todo()
		later = add_to_date(now_datetime(), hours=1)
		rule = self.make_rule(reminders=[{"schedule_type": "One Time", "run_at": later}])
		runner.process_schedule(rule.name, rule.reminders[0].name, manual=True)
		self.assertEqual(self.logs(rule), [])
		self.assertIsNone(runner._fixed_occurrence(frappe._dict(schedule_type="One Time"), later, False))

	def test_a_retry_is_skipped_when_the_document_no_longer_applies(self):
		self.make_todo()
		log = self.failed_log(self.make_rule(send_system_notification=0))
		with patch.object(documents, "matches", return_value=False):
			self.assertEqual(runner.retry_delivery(log.name), "Skipped")
		self.assertIsNone(frappe.db.get_value(LOG, log.name, "next_retry_at"))
		self.assertEqual(runner.retry_delivery(log.name), "Skipped")  # Only a failure can be retried.

	def test_a_retry_waits_while_its_rule_is_switched_off(self):
		self.make_todo()
		rule = self.make_rule(send_system_notification=0)
		log = self.failed_log(rule)
		frappe.db.set_value("Notification Rule", rule.name, "enabled", 0)
		self.assertEqual(runner.retry_delivery(log.name), "Failed")
		self.assertEqual(frappe.db.get_value(LOG, log.name, "retry_count"), 0)
		self.assertTrue(frappe.db.get_value(LOG, log.name, "next_retry_at"))

	def test_an_email_that_finally_failed_reaches_the_log_with_its_reason(self):
		self.make_todo()
		rule = self.make_rule(send_system_notification=0)
		self.run_row(rule)
		log = self.logs(rule, status="Queued")[0]
		error = (
			"Traceback (most recent call last):\n  x\nfrappe.exceptions.ValidationError: Mail server refused"
		)
		frappe.db.set_value("Email Queue", log.email_queue, {"status": "Error", "error": error})
		runner.sync_email_status()
		status, message, retry_at = frappe.db.get_value(
			LOG, log.name, ["status", "error_message", "next_retry_at"]
		)
		self.assertEqual((status, message.splitlines()[0], retry_at), ("Failed", "Mail server refused", None))

	def test_upcoming_covers_each_kind_of_reminder(self):
		later = add_to_date(now_datetime(), hours=2)
		rule = self.make_rule(
			reminders=[
				{"schedule_type": "One Time", "run_at": later},
				{"schedule_type": "Recurring", "frequency": "Daily", "recur_from": "Document Date", "send_time": "09:00:00"},
				{"schedule_type": "One Time", "run_at": add_to_date(later, hours=1), "enabled": 0},
			]
		)  # fmt: skip
		rule.reload()
		documents_listed = [item["document"] for item in runner.get_upcoming(rule, 10)]
		self.assertEqual(
			sorted(documents_listed), ["All matching documents", "Documents on their recurrence day"]
		)


class TestRuleValidation(EngineTestCase):
	def assert_refused(self, expected, **rule):
		with self.assertRaisesRegex(frappe.ValidationError, expected):
			self.make_rule(**rule)
		frappe.clear_messages()

	def test_the_apps_own_doctypes_cannot_be_watched(self):
		self.assert_refused("can.t be set up", document_type=LOG, date_field=None)

	def test_the_date_field_must_be_a_date(self):
		self.assert_refused("isn.t a date", date_field="description")

	def test_a_reminder_counting_from_a_date_needs_a_date_field(self):
		self.assert_refused("choose a Date Field", date_field=None, reminders=ONE_DAY_BEFORE)

	def test_recurrence_settings_are_checked(self):
		daily = {"schedule_type": "Recurring", "frequency": "Custom", "recur_from": "Document Date"}
		self.assert_refused("Repeat Every", reminders=[{**daily, "repeat_every": 0, "repeat_unit": "Days"}])
		self.assert_refused("hourly", reminders=[{**daily, "repeat_every": 1, "repeat_unit": "Hours"}])

	def test_repeat_from_defaults_to_a_fixed_start(self):
		rule = self.make_rule(
			reminders=[
				{
					"schedule_type": "Recurring",
					"frequency": "Daily",
					"recur_from": "",
					"starts_on": add_to_date(now_datetime(), hours=1),
				}
			]
		)
		self.assertEqual(rule.reminders[0].recur_from, "Fixed Start Date")

	def test_an_unchanged_overdue_one_time_reminder_can_still_be_saved(self):
		rule = self.make_rule(
			reminders=[{"schedule_type": "One Time", "run_at": add_to_date(now_datetime(), hours=1)}]
		)
		row = rule.reminders[0]
		frappe.db.set_value(row.doctype, row.name, "run_at", add_to_date(now_datetime(), hours=-1))
		rule.reload()
		rule.save(ignore_permissions=True)

	def test_a_recipient_field_must_exist(self):
		self.assert_refused(
			"isn.t a field", recipients=[{"recipient_type": "Document Field", "email_field": "no_such_field"}]
		)

	def test_the_date_index_is_added_once_and_never_for_standard_columns(self):
		with (
			patch("frappe.db.add_index") as add_index,
			patch("frappe.db.get_column_index", return_value=None),
		):
			ensure_date_index("ToDo", "creation")
			add_index.assert_not_called()
			ensure_date_index("ToDo", "date")
			add_index.assert_called_once()
		with patch("frappe.db.add_index") as add_index, patch("frappe.db.get_column_index", return_value="x"):
			ensure_date_index("ToDo", "date")
			add_index.assert_not_called()


class TestSmallerModules(EngineTestCase):
	def test_recipient_from_a_document_field(self):
		resolver = RecipientResolver(self.make_rule())
		self.assertEqual(resolver._from_field(None), [])
		self.assertEqual([r.user for r in resolver._from_field(HR_USER)], [HR_USER])
		self.assertEqual(
			resolver._from_field("outside@example.com"), [Recipient(None, "outside@example.com")]
		)
		self.assertEqual(resolver._from_field("not an email"), [])
		self.assertEqual(resolver._from_employees([]), [])

	def test_a_user_who_turned_off_notifications_is_skipped(self):
		self.make_todo()
		rule = self.make_rule(send_email=0)
		with patch("dynamic_notification.engine.channels.is_notifications_enabled", return_value=False):
			self.run_row(rule)
		self.assertEqual({log.status for log in self.logs(rule)}, {"Skipped"})

	def test_an_incomplete_condition_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "incomplete"):
			documents.validate_conditions("ToDo", [["ToDo", "status"]])
		frappe.clear_messages()

	def test_datetimes_are_displayed_in_the_site_format(self):
		moment = now_datetime()
		event = frappe.new_doc("Event")
		self.assertEqual(
			render._display_values(event, {"starts_on": moment})["starts_on"], format_datetime(moment)
		)

	def test_old_history_is_cleared_and_recent_history_kept(self):
		self.make_todo()
		rule = self.make_rule()
		self.run_row(rule)
		old, recent = self.logs(rule)[:2]
		frappe.db.set_value(LOG, old.name, "creation", add_days(nowdate(), -400), update_modified=False)
		NotificationDeliveryLog.clear_old_logs(days=365)
		self.assertFalse(frappe.db.exists(LOG, old.name))
		self.assertTrue(frappe.db.exists(LOG, recent.name))


class TestInstall(EngineTestCase):
	def test_a_fresh_install_creates_both_rules(self):
		passport, quotation = f"{PREFIX} installed passport", f"{PREFIX} installed quotation"
		with (
			patch.object(install, "PASSPORT_RULE", passport),
			patch.object(install, "QUOTATION_RULE", quotation),
			patch.object(install, "ensure_date_index") as ensure_index,
		):
			install.after_install()
		self.assertEqual(ensure_index.call_count, 2)
		self.assertEqual(frappe.db.get_value("Notification Rule", passport, "enabled"), 1)
		self.assertEqual(frappe.db.get_value("Notification Rule", quotation, "enabled"), 0)
