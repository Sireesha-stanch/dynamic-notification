import frappe
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import Now


class NotificationDeliveryLog(Document):
	@staticmethod
	def clear_old_logs(days=365):
		table = frappe.qb.DocType("Notification Delivery Log")
		frappe.db.delete(table, filters=(table.creation < (Now() - Interval(days=days))))


def on_doctype_update():
	frappe.db.add_index("Notification Delivery Log", ["status", "next_retry_at"])
	frappe.db.add_index("Notification Delivery Log", ["rule", "status"])
