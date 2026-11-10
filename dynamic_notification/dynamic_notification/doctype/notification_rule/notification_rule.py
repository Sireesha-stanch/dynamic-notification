import frappe
from frappe import _
from frappe.model import default_fields
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime, validate_email_address

from dynamic_notification.engine import documents, recipients, schedule
from dynamic_notification.engine.render import field_label, validate_template


class NotificationRule(Document):
	def validate(self):
		self._validate_document_type()
		self._validate_reminders()
		self._validate_conditions()
		self._validate_recipients()
		self._validate_channels()
		validate_template(self.subject, _("Subject"), self.document_type)
		validate_template(self.message, _("Message"), self.document_type)
		self._update_schedule_state()

	def on_update(self):
		"""The daily scan filters on the rule's date field, usually not indexed by its owner DocType.
		Added in the background because altering a large table takes a while."""
		if (
			self.enabled
			and self.date_field
			and not frappe.flags.in_install
			and (self.has_value_changed("date_field") or self.has_value_changed("enabled"))
		):
			frappe.enqueue(
				ensure_date_index,
				queue="long",
				enqueue_after_commit=True,
				doctype=self.document_type,
				fieldname=self.date_field,
			)

	def _validate_document_type(self):
		"""The engine reads documents with system rights, so a rule must not become a way around
		permissions: whoever writes a rule has to be able to read the DocType themselves."""
		meta = frappe.get_meta(self.document_type)
		# Set here rather than fetched in the form: fetching from a DocType record needs read access to
		# DocType itself, which HR and other non-administrators do not have.
		self.is_submittable = meta.is_submittable
		if meta.istable or meta.issingle or meta.is_virtual:
			frappe.throw(
				_(
					"{0} doesn't hold separate records to send reminders about. Please choose another DocType, such as Employee or Sales Invoice."
				).format(_(self.document_type)),
				title=_("Please Choose Another DocType"),
			)
		if self.document_type in ("Notification Rule", "Notification Delivery Log"):
			frappe.throw(
				_("Reminders can't be set up on {0} itself. Please choose another DocType.").format(
					_(self.document_type)
				),
				title=_("Please Choose Another DocType"),
			)
		documents.require_read(self.document_type)

	def _validate_reminders(self):
		# Checked here rather than with `reqd` on the table: a required table starts with a blank row,
		# and a user who then clicks Add Row is blocked by that empty first row.
		if not self.reminders:
			frappe.throw(
				_(
					"Please add at least one reminder under When to Notify, so the system knows when to send this."
				),
				title=_("When Should This Be Sent?"),
			)
		date_df = self._get_date_field()
		date_label = field_label(self.document_type, date_df) if date_df else _("Created On")
		seen = set()
		for row in self.reminders:
			if schedule.needs_date_field(row) and not self.date_field:
				_throw_reminder(
					row, _("it counts from a date, so please choose a Date Field at the top of the form.")
				)
			if row.schedule_type in (schedule.BEFORE, schedule.AFTER) and (row.days or 0) < 1:
				_throw_reminder(
					row,
					_(
						"please enter 1 or more in Number of Days. To notify on the date itself, choose On Date."
					),
				)
			self._validate_recurrence(row)
			self._validate_one_time(row)

			row.summary = schedule.describe(row, date_label)
			if row.summary in seen:
				_throw_reminder(
					row,
					_("it is the same as an earlier one ({0}). Please change it or remove it.").format(
						row.summary
					),
				)
			seen.add(row.summary)

	def _get_date_field(self):
		if not self.date_field or self.date_field == "creation":
			return None
		df = frappe.get_meta(self.document_type).get_field(self.date_field)
		if not df or df.fieldtype not in ("Date", "Datetime"):
			frappe.throw(
				_("{0} isn't a date on {1}. Please choose one from the Date Field list.").format(
					self.date_field, _(self.document_type)
				),
				title=_("Please Choose a Date Field"),
			)
		if df.is_virtual:
			frappe.throw(
				_(
					"{0} is calculated automatically, so reminders can't count from it. Please choose another Date Field."
				).format(_(df.label)),
				title=_("Please Choose a Date Field"),
			)
		return df

	def _validate_recurrence(self, row):
		if row.schedule_type != schedule.RECURRING:
			return
		if not row.frequency:
			_throw_reminder(row, _("please choose how often it should repeat, under Repeat."))
		if row.frequency == "Custom" and (row.repeat_every or 0) < 1:
			_throw_reminder(row, _("please enter 1 or more in Repeat Every."))
		if row.frequency == "Custom" and not row.repeat_unit:
			_throw_reminder(row, _("please choose Hours, Days, Weeks, Months or Years under Repeat Unit."))
		if schedule.is_document_recurring(row) and schedule.get_interval(row)[1] == "Hours":
			_throw_reminder(
				row,
				_(
					"when repeating from each document's own date it can't be hourly. Please choose Days, Weeks, Months or Years, or set Repeat From to Fixed Start Date."
				),
			)
		if not row.recur_from:
			row.recur_from = schedule.FROM_START
		if not schedule.is_document_recurring(row) and not row.starts_on:
			_throw_reminder(row, _("please choose Starts On, the date and time of the first reminder."))

	def _validate_one_time(self, row):
		"""A date in the past is only an error when the user has just entered it. An unchanged row
		that is overdue (the rule was saved while the scheduler was down) should still go out."""
		if row.schedule_type != schedule.ONE_TIME:
			return
		if not row.run_at:
			_throw_reminder(row, _("please choose the Date and Time to send it."))
		before = self.get_doc_before_save()
		previous = before.get("reminders", {"name": row.name}) if before and row.name else []
		if previous and previous[0].run_at and get_datetime(previous[0].run_at) == get_datetime(row.run_at):
			return
		if get_datetime(row.run_at) <= now_datetime():
			_throw_reminder(row, _("that Date and Time has already passed. Please choose one in the future."))
		row.last_run = None  # A new date is a new reminder, even if the old one has already gone out.

	def _validate_conditions(self):
		parsed = documents.parse_conditions(self.conditions)
		documents.validate_conditions(self.document_type, parsed)
		self.conditions = frappe.as_json(parsed, indent=None)

	def _validate_recipients(self):
		if not self.recipients:
			frappe.throw(
				_(
					"Please add at least one recipient under Who to Notify, so the system knows who to send this to."
				),
				title=_("Who Should Receive This?"),
			)
		meta = frappe.get_meta(self.document_type)
		labels = {
			recipients.OWNER: _("Owner of the document"),
			recipients.ASSIGNED: _("Users assigned to the document"),
		}
		for row in self.recipients:
			field = {recipients.EMPLOYEE: row.employee_field, recipients.FIELD: row.email_field}.get(
				row.recipient_type
			)
			self._validate_recipient_choice(row, field)
			if field and field not in default_fields and not meta.get_field(field):
				_throw_recipient(
					row,
					_("{0} isn't a field on {1}. Please choose it again.").format(
						field, _(self.document_type)
					),
				)
			if row.recipient_type == recipients.EMAIL and not validate_email_address(row.email_address):
				_throw_recipient(
					row,
					_(
						"{0} doesn't look like an email address. Please check it, for example name@company.com."
					).format(row.email_address),
				)
			row.target = {
				recipients.ROLE: row.role,
				recipients.USER: row.user,
				recipients.DEPARTMENT: row.department,
				recipients.EMPLOYEE: field,
				recipients.FIELD: field,
				recipients.EMAIL: row.email_address,
			}.get(row.recipient_type) or labels.get(row.recipient_type)

	def _validate_recipient_choice(self, row, field):
		value, label = {
			recipients.ROLE: (row.role, _("Role")),
			recipients.USER: (row.user, _("User")),
			recipients.DEPARTMENT: (row.department, _("Department")),
			recipients.EMPLOYEE: (field, _("Employee Field")),
			recipients.FIELD: (field, _("Field With User or Email")),
			recipients.EMAIL: (row.email_address, _("Email Address")),
		}.get(row.recipient_type, (True, None))
		if not value:
			_throw_recipient(row, _("please fill in {0}.").format(label))

	def _validate_channels(self):
		from dynamic_notification.engine.channels import get_enabled_channels

		if not get_enabled_channels(self):
			frappe.throw(
				_(
					"Please choose at least one way to notify under How to Notify: Email, System Notification, or both."
				),
				title=_("How Should This Be Sent?"),
			)

	def _update_schedule_state(self):
		before = self.get_doc_before_save()
		re_enabled = before and self.enabled and not before.enabled
		now = now_datetime()
		for row in self.reminders:
			if self.is_new():
				# A duplicated rule is a new rule: it must not inherit "already scanned today".
				row.update(
					{"last_run": None, "processed_through": None, "last_status": None, "last_error": None}
				)
			if re_enabled:
				# Time spent disabled is not an outage to catch up on.
				row.processed_through = None
			row.next_run = schedule.compute_next_run(row, now) if row.enabled else None

		upcoming = [get_datetime(row.next_run) for row in self.reminders if row.next_run]
		self.next_run = min(upcoming) if self.enabled and upcoming else None


def _throw_reminder(row, problem: str) -> None:
	frappe.throw(_("Reminder {0}: {1}").format(row.idx, problem), title=_("Please Check When to Notify"))


def _throw_recipient(row, problem: str) -> None:
	frappe.throw(_("Recipient {0}: {1}").format(row.idx, problem), title=_("Please Check Who to Notify"))


def ensure_date_index(doctype: str, fieldname: str) -> None:
	if fieldname in default_fields:
		return  # Standard columns come indexed.
	if not frappe.db.get_column_index(f"tab{doctype}", fieldname, unique=False):
		frappe.db.add_index(doctype, [fieldname])
