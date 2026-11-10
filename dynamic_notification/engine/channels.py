"""Delivery channels. Each is a dict describing how to reach a recipient:

	name:    stored on the delivery log
	flag:    Check field on Notification Rule that switches the channel on
	address: "email" or "user", the Recipient attribute this channel needs
	send:    callable(rule, doc, recipient, subject, message) -> ChannelResult

Another app adds a channel such as WhatsApp without changing this one: it adds a Check custom field
to Notification Rule and lists a dotted path to its dict in its own hooks.py:

	dynamic_notification_channels = ["my_app.whatsapp.channel"]
"""

from typing import NamedTuple

import frappe
from frappe.desk.doctype.notification_settings.notification_settings import is_notifications_enabled

QUEUED = "Queued"
SENT = "Sent"
SKIPPED = "Skipped"


class ChannelResult(NamedTuple):
	status: str
	note: str | None = None
	email_queue: str | None = None


def send_email(rule, doc, recipient, subject, message) -> ChannelResult:
	queued = frappe.sendmail(
		recipients=[recipient.email],
		subject=subject,
		message=message,
		reference_doctype=doc.doctype,
		reference_name=doc.name,
		# Frappe re-parses its whole email stylesheet for every email unless told otherwise, which is
		# most of the cost of sending one. Reminders are plain text and do not need it.
		add_css=False,
	)
	if not queued:
		return ChannelResult(SKIPPED, "Recipient has unsubscribed from these emails")
	# Delivery itself happens later in Frappe's email worker; the log follows the queue's outcome.
	return ChannelResult(QUEUED, email_queue=queued.name)


def send_system_notification(rule, doc, recipient, subject, message) -> ChannelResult:
	from dynamic_notification.engine.render import system_notification_title

	if not is_notifications_enabled(recipient.user):
		return ChannelResult(SKIPPED, "Recipient has turned off system notifications")

	log = frappe.new_doc("Notification Log")
	log.update(
		{
			"for_user": recipient.user,
			"type": "Alert",  # Alerts never trigger Frappe's own extra email for the same event.
			"subject": system_notification_title(subject),
			"email_content": message,
			"document_type": doc.doctype,
			"document_name": doc.name,
		}
	)
	log.insert(ignore_permissions=True)
	return ChannelResult(SENT)


BUILTIN_CHANNELS = [
	{"name": "Email", "flag": "send_email", "address": "email", "send": send_email},
	{
		"name": "System Notification",
		"flag": "send_system_notification",
		"address": "user",
		"send": send_system_notification,
	},
]


def get_all_channels() -> dict[str, dict]:
	channels = list(BUILTIN_CHANNELS)
	channels += [frappe.get_attr(path) for path in frappe.get_hooks("dynamic_notification_channels")]
	return {channel["name"]: channel for channel in channels}


def get_enabled_channels(rule) -> list[dict]:
	return [channel for channel in get_all_channels().values() if rule.get(channel["flag"])]
