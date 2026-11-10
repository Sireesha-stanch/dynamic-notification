"""Sets up the assessment's practical use case on installation."""

import frappe

from dynamic_notification.dynamic_notification.doctype.notification_rule.notification_rule import (
	ensure_date_index,
)

PASSPORT_RULE = "Employee Passport Expiry Notification"
QUOTATION_RULE = "Quotation Expiry Follow-up"


def after_install() -> None:
	create_passport_expiry_rule()
	create_quotation_followup_rule()


def before_uninstall() -> None:
	"""Frappe only removes the job at the next migrate. Until then it would fail on every tick,
	because the tables it reads are gone."""
	frappe.db.delete("Scheduled Job Type", {"method": ["like", "dynamic_notification.%"]})


def create_passport_expiry_rule() -> None:
	"""HR owns this rule from here on: it is plain data they can change or delete in the UI. It is
	only created on installation, so a later `bench migrate` will not bring back one they removed."""
	if frappe.db.exists("Notification Rule", PASSPORT_RULE) or not frappe.db.exists("DocType", "Employee"):
		return

	frappe.get_doc(
		{
			"doctype": "Notification Rule",
			"rule_name": PASSPORT_RULE,
			"enabled": 1,
			"document_type": "Employee",
			"date_field": "valid_upto",
			"conditions": frappe.as_json([["Employee", "status", "=", "Active"]], indent=None),
			"send_email": 1,
			"send_system_notification": 1,
			"subject": "Passport expiry: {{ employee_name }} ({{ name }}) on {{ valid_upto }}",
			"message": (
				"Dear HR Team,\n\n"
				"The passport of {{ employee_name }} (Employee ID: {{ name }}) "
				"expires on {{ valid_upto }}, {{ days_left }} day(s) from today.\n\n"
				"Please take the necessary action.\n\n"
				"{{ doc_url }}"
			),
			"reminders": [
				{"schedule_type": "Before Date", "days": 30, "send_time": "09:00:00"},
				{"schedule_type": "Before Date", "days": 15, "send_time": "09:00:00"},
				{"schedule_type": "On Date", "send_time": "09:00:00"},
			],
			"recipients": [{"recipient_type": "Role", "role": "HR Manager"}],
		}
	).insert(ignore_permissions=True)
	ensure_date_index("Employee", "valid_upto")


def create_quotation_followup_rule() -> None:
	"""Shows the same engine working on another DocType. Installed switched off, because turning it on
	starts emailing quotation owners, and that should be somebody's decision."""
	if frappe.db.exists("Notification Rule", QUOTATION_RULE) or not frappe.db.exists("DocType", "Quotation"):
		return

	frappe.get_doc(
		{
			"doctype": "Notification Rule",
			"rule_name": QUOTATION_RULE,
			"enabled": 0,
			"document_type": "Quotation",
			"date_field": "valid_till",
			"docstatus_filter": "Submitted Only",
			"conditions": frappe.as_json([["Quotation", "status", "=", "Open"]], indent=None),
			"send_email": 1,
			"send_system_notification": 1,
			"subject": "Quotation {{ name }} for {{ customer_name }} expires on {{ valid_till }}",
			"message": (
				"Quotation {{ name }} for {{ customer_name }} is valid until {{ valid_till }}, "
				"{{ days_left }} day(s) from today. Total: {{ grand_total }}.\n\n"
				"Items:\n{% for item in doc.items %}{{ item.item_name }} x {{ item.qty }}\n{% endfor %}\n"
				"Please follow up with the customer.\n\n"
				"{{ doc_url }}"
			),
			"reminders": [{"schedule_type": "Before Date", "days": 3, "send_time": "09:00:00"}],
			"recipients": [{"recipient_type": "Document Owner"}],
		}
	).insert(ignore_permissions=True)
	ensure_date_index("Quotation", "valid_till")
