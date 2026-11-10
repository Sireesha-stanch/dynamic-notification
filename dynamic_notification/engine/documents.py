"""Finds the documents a rule applies to."""

import json
from collections.abc import Iterator

import frappe
from frappe import _
from frappe.model import default_fields, table_fields

BATCH_SIZE = 500

ALLOWED_OPERATORS = {
	"=", "!=", ">", "<", ">=", "<=", "like", "not like", "in", "not in", "is",
	"between", "timespan", "previous", "next",
	"descendants of", "not descendants of", "descendants of (inclusive)", "ancestors of", "not ancestors of",
}  # fmt: skip

DOCSTATUS_FILTERS = {"Submitted Only": 1, "Draft Only": 0, "All Except Cancelled": ["<", 2]}


def parse_conditions(conditions: str | list | None) -> list[list]:
	if isinstance(conditions, list):
		return conditions
	try:
		parsed = json.loads(conditions) if conditions else []
	except ValueError:
		parsed = None
	if not isinstance(parsed, list):
		_throw_condition(_("The conditions could not be read."))
	return parsed


def _throw_condition(problem: str) -> None:
	frappe.throw(
		_("{0} Please choose the Condition again, or correct it under Custom Condition.").format(problem),
		title=_("Please Check the Conditions"),
	)


def require_read(doctype: str) -> None:
	"""The engine reads with system rights, so access is checked here, in words a user can act on."""
	if not frappe.has_permission(doctype, "read"):
		frappe.throw(
			_(
				"You don't have access to {0}, so it can't be used in a notification. Please ask your administrator for access."
			).format(_(doctype)),
			frappe.PermissionError,
			title=_("No Access"),
		)


def validate_conditions(doctype: str, conditions: list[list]) -> None:
	"""Conditions arrive as JSON from the filter builder, so check them like any other user input."""
	meta = frappe.get_meta(doctype)
	# Child tables have no permissions of their own; their fields follow the parent's.
	readable_levels = meta.get_permlevel_access("read")
	child_doctypes = {df.options for df in meta.fields if df.fieldtype in table_fields}

	for condition in conditions:
		if not isinstance(condition, list) or len(condition) < 4:
			_throw_condition(_("One of the conditions is incomplete."))

		condition_doctype, fieldname, operator = condition[0], condition[1], condition[2]
		if condition_doctype != doctype and condition_doctype not in child_doctypes:
			_throw_condition(
				_("A condition uses {0}, which isn't part of {1}.").format(_(condition_doctype), _(doctype))
			)
		if str(operator).lower() not in ALLOWED_OPERATORS:
			_throw_condition(
				_("A condition uses the comparison {0}, which isn't available here.").format(operator)
			)
		if fieldname in default_fields:
			continue
		df = frappe.get_meta(condition_doctype).get_field(fieldname)
		if not df:
			_throw_condition(
				_("A condition uses {0}, which no longer exists on {1}.").format(
					fieldname, _(condition_doctype)
				)
			)
		# A filter reveals values through what it matches, so it needs the same access as reading them.
		if df.fieldtype == "Password" or df.permlevel not in readable_levels:
			_throw_condition(
				_("{0} can't be used in a condition, because you don't have access to its value.").format(
					_(df.label or fieldname)
				)
			)


def get_base_filters(rule) -> list[list]:
	filters = [list(condition[:4]) for condition in parse_conditions(rule.conditions)]
	if frappe.get_meta(rule.document_type).is_submittable:
		docstatus = DOCSTATUS_FILTERS.get(rule.docstatus_filter or "All Except Cancelled")
		if docstatus is not None:
			operator, value = docstatus if isinstance(docstatus, list) else ("=", docstatus)
			filters.append([rule.document_type, "docstatus", operator, value])
	return filters


def date_range_filters(rule, start, end) -> list[list]:
	"""Half-open so it also covers the whole last day of Datetime fields. `end` is inclusive."""
	from frappe.utils import add_days

	return [
		[rule.document_type, rule.date_field, ">=", str(start)],
		[rule.document_type, rule.date_field, "<", add_days(end, 1)],
	]


def joins_child_table(doctype: str, filters: list[list]) -> bool:
	"""A condition on a child table joins it, so a query must not repeat the parent per child row."""
	return any(condition[0] != doctype for condition in filters)


def iter_documents(rule, extra_filters=None, batch_size=BATCH_SIZE) -> Iterator[frappe._dict]:
	"""Yields only `name` and the rule's date field, a page at a time.

	Pages by `name > last` rather than OFFSET: the cost stays flat however deep into a large table
	we are, and rows delivered and committed between pages cannot shift the page boundaries.
	"""
	filters = get_base_filters(rule) + (extra_filters or [])
	fields = ["name"]
	if rule.date_field and rule.date_field != "name":
		fields.append(rule.date_field)
	distinct = joins_child_table(rule.document_type, filters)

	last_name = None
	while True:
		page_filters = filters + ([[rule.document_type, "name", ">", last_name]] if last_name else [])
		page = frappe.get_all(
			rule.document_type,
			filters=page_filters,
			fields=fields,
			order_by="name asc",
			limit_page_length=batch_size,
			distinct=distinct,
		)
		yield from page
		if len(page) < batch_size:
			return
		last_name = page[-1].name


def count_documents(rule) -> int:
	filters = get_base_filters(rule)
	if joins_child_table(rule.document_type, filters):
		# One row per matching child row; count parents, not rows.
		return sum(1 for _doc in iter_documents(rule))
	return frappe.db.count(rule.document_type, filters=filters)


def matches(rule, docname: str) -> bool:
	filters = [*get_base_filters(rule), [rule.document_type, "name", "=", docname]]
	return bool(frappe.get_all(rule.document_type, filters=filters, pluck="name", limit_page_length=1))
