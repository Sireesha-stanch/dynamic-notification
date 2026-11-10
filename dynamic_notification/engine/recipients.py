"""Turns a rule's recipient rows into the concrete people to notify for one document."""

from typing import NamedTuple

import frappe
from frappe.utils import validate_email_address
from frappe.utils.user import get_users_with_role

ROLE = "Role"
USER = "User"
DEPARTMENT = "Department"
OWNER = "Document Owner"
ASSIGNED = "Assigned Users"
EMPLOYEE = "Document Employee"
FIELD = "Document Field"
EMAIL = "Email Address"


class Recipient(NamedTuple):
	"""`user` drives the system notification, `email` the email. Either may be missing."""

	user: str | None
	email: str | None

	@property
	def key(self) -> str:
		return (self.user or self.email or "").lower()


class RecipientResolver:
	"""Role and department members do not vary per document, so they are looked up once per run."""

	def __init__(self, rule):
		self.rule = rule
		self._shared: list[Recipient] | None = None
		self._users: dict[str, Recipient | None] = {}

	def resolve(self, doc) -> list[Recipient]:
		if self._shared is None:
			self._shared = self._resolve_shared()

		found = list(self._shared)
		for row in self.rule.recipients:
			found += self._resolve_row(row, doc)

		unique = {}
		for recipient in found:
			if recipient.key:
				unique.setdefault(recipient.key, recipient)
		return list(unique.values())

	def _resolve_shared(self) -> list[Recipient]:
		found = []
		for row in self.rule.recipients:
			if row.recipient_type == ROLE:
				found += [self._from_user(user) for user in get_users_with_role(row.role)]
			elif row.recipient_type == USER:
				found.append(self._from_user(row.user))
			elif row.recipient_type == DEPARTMENT:
				found += self._from_employees(
					frappe.get_all(
						"Employee",
						filters={
							"department": ["descendants of (inclusive)", row.department],
							"status": "Active",
						},
						pluck="name",
					)
				)
			elif row.recipient_type == EMAIL:
				found.append(Recipient(None, row.email_address))
		return [r for r in found if r]

	def _resolve_row(self, row, doc) -> list[Recipient]:
		if row.recipient_type == OWNER:
			return [r for r in [self._from_user(doc.owner)] if r]
		if row.recipient_type == ASSIGNED:
			users = frappe.get_all(
				"ToDo",
				filters={"reference_type": doc.doctype, "reference_name": doc.name, "status": "Open"},
				pluck="allocated_to",
			)
			return [r for r in map(self._from_user, users) if r]
		if row.recipient_type == EMPLOYEE:
			employee = doc.name if row.employee_field == "name" else doc.get(row.employee_field)
			return self._from_employees([employee]) if employee else []
		if row.recipient_type == FIELD:
			return self._from_field(doc.get(row.email_field))
		return []

	def _from_field(self, value: str | None) -> list[Recipient]:
		if not value:
			return []
		if user := self._from_user(value):
			return [user]
		email = validate_email_address(value)
		return [Recipient(None, email)] if email else []

	def _from_user(self, user: str | None) -> Recipient | None:
		"""A disabled user is resolved to nothing rather than to a dead mailbox."""
		if not user:
			return None
		if user not in self._users:
			info = frappe.db.get_value("User", user, ["name", "email", "enabled"], as_dict=True)
			self._users[user] = Recipient(info.name, info.email) if info and info.enabled else None
		return self._users[user]

	def _from_employees(self, employees: list[str]) -> list[Recipient]:
		if not employees:
			return []
		rows = frappe.get_all(
			"Employee",
			filters={"name": ["in", employees]},
			fields=["user_id", "prefered_email", "company_email", "personal_email"],
		)
		found = []
		for row in rows:
			user = self._from_user(row.user_id)
			# Same order as HRMS's get_employee_email: work reminders stay off a personal address
			# unless the employee has chosen it as their preferred one.
			login_email = user.email if user else None
			email = row.prefered_email or login_email or row.company_email or row.personal_email
			if user:
				found.append(Recipient(user.user, email))
			elif email:
				found.append(Recipient(None, email))
		return found
