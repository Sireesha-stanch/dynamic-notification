import frappe
from frappe import _
from frappe.model import no_value_fields
from frappe.query_builder import DocType
from frappe.query_builder.functions import Count
from frappe.utils import add_days, nowdate

from dynamic_notification.engine import documents, runner
from dynamic_notification.engine.recipients import RecipientResolver
from dynamic_notification.engine.render import Renderer, _is_exposed, field_label

RULE = "Notification Rule"
LOG = "Notification Delivery Log"
STATS_DAYS = 30
MAX_DASHBOARD_RULES = 100
MAX_DASHBOARD_LOGS = 20
LOG_FIELDS = [
	"name",
	"creation",
	"rule",
	"reference_doctype",
	"reference_name",
	"recipient",
	"channel",
	"status",
	"error_message",
]


@frappe.whitelist()
def get_doctype_fields(doctype: str) -> dict:
	"""Field choices for the rule form, by purpose, for a DocType the user can read."""
	documents.require_read(doctype)
	meta = frappe.get_meta(doctype)

	def option(df):
		return {"value": df.fieldname, "label": f"{field_label(doctype, df)} ({df.fieldname})"}

	fields = [df for df in meta.fields if _is_exposed(df) and df.fieldtype not in no_value_fields]
	real_fields = [df for df in fields if not df.is_virtual]

	employee_fields = [
		option(df) for df in real_fields if df.fieldtype == "Link" and df.options == "Employee"
	]
	if doctype == "Employee":
		employee_fields.insert(0, {"value": "name", "label": _("This Employee (name)")})

	return {
		"date_fields": [option(df) for df in real_fields if df.fieldtype in ("Date", "Datetime")]
		+ [{"value": "creation", "label": _("Created On (creation)")}],
		"employee_fields": employee_fields,
		"email_fields": [
			option(df)
			for df in real_fields
			if (df.fieldtype == "Link" and df.options == "User")
			or (df.fieldtype == "Data" and (df.options == "Email" or "email" in df.fieldname))
		],
		"message_fields": [{"value": "name", "label": _("ID (name)")}, *map(option, fields)],
		"is_submittable": meta.is_submittable,
		"condition_presets": _get_condition_presets(doctype, meta),
	}


def _get_condition_presets(doctype: str, meta) -> list[dict]:
	"""One-click conditions: each option of the DocType's Status field, and any declared by a hook whose
	fields this site has and the user may use."""
	presets = []
	status = meta.get_field("status")
	if status and status.fieldtype == "Select" and _is_exposed(status):
		presets += [
			{"label": f"{_(status.label)}: {_(option)}", "conditions": [[doctype, "status", "=", option]]}
			for option in (status.options or "").split("\n")
			if option
		]

	for preset in frappe.get_hooks("dynamic_notification_condition_presets"):
		fields = [meta.get_field(condition[0]) for condition in preset["conditions"]]
		if preset["doctype"] == doctype and all(df and _is_exposed(df) for df in fields):
			presets.append(
				{
					"label": _(preset["label"]),
					"conditions": [[doctype, *condition] for condition in preset["conditions"]],
				}
			)
	return presets


@frappe.whitelist()
def count_matching(doctype: str, conditions: str | None = None, docstatus_filter: str | None = None) -> int:
	"""How many documents the conditions select right now, shown as the user builds them."""
	documents.require_read(doctype)
	parsed = documents.parse_conditions(conditions)
	documents.validate_conditions(doctype, parsed)
	rule = frappe._dict(document_type=doctype, conditions=parsed, docstatus_filter=docstatus_filter)
	return documents.count_documents(rule)


@frappe.whitelist()
def preview(rule: str, docname: str | None = None) -> dict:
	"""Shows exactly what a document would produce, without sending anything."""
	rule_doc = _get_rule(rule, "read")
	documents.require_read(rule_doc.document_type)

	if not docname:
		docname = next((row.name for row in documents.iter_documents(rule_doc, batch_size=1)), None)
		if not docname:
			frappe.throw(
				_(
					"There is no {0} that matches this notification yet, so there is nothing to preview. Create one, or change the Condition."
				).format(_(rule_doc.document_type)),
				title=_("Nothing to Preview"),
			)

	doc = frappe.get_doc(rule_doc.document_type, docname)
	_require_doc_access(doc, "read")
	subject, message = Renderer(rule_doc).render(doc)
	return {
		"docname": docname,
		"subject": subject,
		"message": frappe.utils.sanitize_html(message),
		"matches_conditions": documents.matches(rule_doc, docname),
		"recipients": [r.user or r.email for r in RecipientResolver(rule_doc).resolve(doc)],
	}


@frappe.whitelist()
def get_upcoming(rule: str, limit: int = 10) -> list[dict]:
	rule_doc = _get_rule(rule, "read")
	documents.require_read(rule_doc.document_type)
	return runner.get_upcoming(rule_doc, min(int(limit), 50))


@frappe.whitelist(methods=["POST"])
def run_now(rule: str) -> None:
	"""Runs today's scan in the background. Already notified documents stay quiet."""
	rule_doc = _get_rule(rule, "write")
	if not rule_doc.enabled:
		frappe.throw(
			_("{0} is switched off. Tick Enabled first, then run it again.").format(rule_doc.name),
			title=_("Notification Is Off"),
		)
	frappe.enqueue(
		"dynamic_notification.engine.runner.run_rule_now",
		queue="long",
		timeout=runner.JOB_TIMEOUT,
		enqueue_after_commit=True,
		job_id=f"dynamic-notification-run-{rule_doc.name}",
		deduplicate=True,
		rule_name=rule_doc.name,
		user=frappe.session.user,
	)


@frappe.whitelist(methods=["POST"])
def set_enabled(rule: str, enabled: int) -> None:
	rule_doc = _get_rule(rule, "write")
	rule_doc.enabled = frappe.utils.cint(enabled)
	rule_doc.save()


@frappe.whitelist(methods=["POST"])
def retry_delivery(log: str) -> str:
	_require_doc_access(frappe.get_doc(LOG, log), "read")
	_get_rule(frappe.db.get_value(LOG, log, "rule"), "write")
	return runner.retry_delivery(log, manual=True)


@frappe.whitelist()
def get_dashboard() -> dict:
	documents.require_read(RULE)

	rules = frappe.get_list(
		RULE,
		fields=[
			"name",
			"enabled",
			"document_type",
			"date_field",
			"conditions",
			"docstatus_filter",
			"next_run",
			"last_run",
			"last_status",
		],
		order_by="enabled desc, next_run asc",
		limit_page_length=MAX_DASHBOARD_RULES,
	)
	stats = _get_delivery_stats()
	reminders = _get_enabled_reminders([rule.name for rule in rules])
	totals = {}
	upcoming = []
	can_write = frappe.has_permission(RULE, "write")

	for rule in rules:
		rule_stats = stats.get(rule.name, {})
		for status, count in rule_stats.items():
			totals[status] = totals.get(status, 0) + count
		rule.sent = rule_stats.get("Sent", 0) + rule_stats.get("Queued", 0)
		rule.failed = rule_stats.get("Failed", 0)

		rule.can_write = can_write
		rule.next_notification = None
		# A rule can break after it was saved, say by a deleted custom field. That must not take the
		# dashboard with it: this is the very screen where somebody would go to fix it.
		try:
			rows = reminders.get(rule.name, [])
			rule.schedule = [row.summary for row in rows]
			if rule.enabled and frappe.has_permission(rule.document_type, "read"):
				next_items = runner.get_upcoming(frappe._dict(rule, reminders=rows), 5)
				rule.next_notification = next_items[0] if next_items else None
				upcoming += [{**item, "rule": rule.name} for item in next_items]
		except Exception:
			frappe.clear_messages()
			rule.schedule = rule.get("schedule") or []
			rule.last_status = "Failed"

	return {
		"rules": rules,
		"totals": {
			"active": sum(1 for rule in rules if rule.enabled),
			"disabled": sum(1 for rule in rules if not rule.enabled),
			"sent": totals.get("Sent", 0) + totals.get("Queued", 0),
			"failed": totals.get("Failed", 0),
			"skipped": totals.get("Skipped", 0),
		},
		"stats_days": STATS_DAYS,
		"upcoming": sorted(upcoming, key=lambda item: item["at"])[:10],
		"failures": _get_recent_logs({"status": "Failed"}),
		"recent": _get_recent_logs(),
	}


def _get_recent_logs(filters: dict | None = None) -> list:
	"""Only the first line of each error, the plain reason: the traceback below it stays on the log."""
	logs = frappe.get_list(
		LOG,
		filters=filters,
		fields=LOG_FIELDS,
		order_by="creation desc",
		limit_page_length=MAX_DASHBOARD_LOGS,
	)
	for log in logs:
		log.error_message = (log.error_message or "").split("\n")[0]
	return logs


def _get_enabled_reminders(rule_names: list[str]) -> dict[str, list]:
	"""Every rule's reminder rows in one query, rather than loading each rule document in turn."""
	rows = frappe.get_all(
		"Notification Schedule",
		filters={"parent": ["in", rule_names], "parenttype": RULE, "enabled": 1},
		fields=[
			"parent",
			"schedule_type",
			"days",
			"send_time",
			"summary",
			"next_run",
			"enabled",
			"recur_from",
		],
		order_by="parent, idx",
	)
	by_rule = {}
	for row in rows:
		by_rule.setdefault(row.parent, []).append(row)
	return by_rule


def _get_delivery_stats() -> dict[str, dict[str, int]]:
	log = DocType(LOG)
	rows = (
		frappe.qb.from_(log)
		.select(log.rule, log.status, Count("*").as_("count"))
		.where(log.creation >= add_days(nowdate(), -STATS_DAYS))
		.groupby(log.rule, log.status)
		.run(as_dict=True)
	)
	stats = {}
	for row in rows:
		stats.setdefault(row.rule, {})[row.status] = row.count
	return stats


def _get_rule(name: str, ptype: str):
	rule = frappe.get_doc(RULE, name)
	_require_doc_access(rule, ptype)
	return rule


def _require_doc_access(doc, ptype: str) -> None:
	if not doc.has_permission(ptype):
		action = _("change") if ptype == "write" else _("view")
		frappe.throw(
			_("You don't have permission to {0} {1}. Please ask your administrator for access.").format(
				action, doc.name
			),
			frappe.PermissionError,
			title=_("No Access"),
		)
