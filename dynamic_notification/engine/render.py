"""Renders a rule's subject and message for one document.

Templates are written by business users, not developers, so they run in their own locked down Jinja
environment rather than frappe.render_template, whose globals expose frappe.* database helpers. A
template here only ever sees plain values of the one document it is rendered for: no Document
methods, no password fields and no fields above permlevel 0.
"""

import html
import re

import frappe
from frappe import _
from frappe.model import default_fields, no_value_fields, table_fields
from frappe.utils import escape_html, format_datetime, formatdate, get_url_to_form, getdate, strip_html, today
from jinja2 import TemplateSyntaxError, meta, nodes
from jinja2.sandbox import ImmutableSandboxedEnvironment

EXTRA_VARIABLES = {"doc", "days_left", "rule_name", "doc_url"}


_JINJA_BLOCK = re.compile(r"{{.*?}}|{%.*?%}", re.DOTALL)


class _Environment(ImmutableSandboxedEnvironment):
	"""Prefers a document's fields over dict methods: Jinja looks attributes up before keys, so
	`{{ doc.items }}` would otherwise be dict.items rather than an invoice's item rows."""

	def getattr(self, obj, attribute):
		if isinstance(obj, dict) and attribute in obj:
			return obj[attribute]
		return super().getattr(obj, attribute)


def _make_env(autoescape: bool) -> ImmutableSandboxedEnvironment:
	env = _Environment(autoescape=autoescape)
	env.filters["format_date"] = lambda value: formatdate(value) if value else ""
	return env


_html_env = _make_env(autoescape=True)
_text_env = _make_env(autoescape=False)


def _unescape_jinja(template: str) -> str:
	"""Browsers and paste buffers turn quotes in `{{ x | default("-") }}` into entities."""
	return _JINJA_BLOCK.sub(lambda m: html.unescape(m.group()), template or "")


def get_aliases(doctype: str) -> dict[str, str]:
	"""Other names for a field, from the `dynamic_notification_field_aliases` hook. They are
	configuration rather than engine logic, so any app can add them for any DocType."""
	return {
		entry["alias"]: entry["fieldname"]
		for entry in frappe.get_hooks("dynamic_notification_field_aliases")
		if entry["doctype"] == doctype
	}


def field_label(doctype: str, df) -> str:
	"""A field's label for people choosing it, using the meaningful name where one is known."""
	for alias, fieldname in get_aliases(doctype).items():
		if fieldname == df.fieldname:
			return _(alias.replace("_", " ").title())
	return _(df.label or df.fieldname)


def _is_exposed(df) -> bool:
	return df.fieldtype != "Password" and not df.permlevel and not df.is_virtual


def _plain_values(doc) -> dict:
	values = {name: doc.get(name) for name in default_fields if name not in ("doctype", "idx")}
	for df in doc.meta.fields:
		if not _is_exposed(df):
			continue
		if df.fieldtype in table_fields:
			values[df.fieldname] = [_plain_values(row) for row in doc.get(df.fieldname) or []]
		elif df.fieldtype not in no_value_fields:
			values[df.fieldname] = doc.get(df.fieldname)
	return frappe._dict(values)


def _display_values(doc, plain_values: dict) -> dict:
	"""Field values as a person expects to read them: dates in the site's format, money with its
	currency, and an empty string rather than "None" for anything unset."""
	values = {}
	for name, value in plain_values.items():
		if value is None:
			values[name] = ""
			continue
		df = doc.meta.get_field(name)
		fieldtype = df.fieldtype if df else None
		if fieldtype == "Date":
			values[name] = formatdate(value)
		elif fieldtype == "Datetime":
			values[name] = format_datetime(value)
		elif fieldtype == "Currency":
			values[name] = doc.get_formatted(name)
		else:
			values[name] = value
	return values


def get_context(rule, doc) -> dict:
	plain_values = _plain_values(doc)
	context = _display_values(doc, plain_values)
	for alias, fieldname in get_aliases(doc.doctype).items():
		context[alias] = context.get(fieldname, "")
	context["doc"] = plain_values
	context["rule_name"] = rule.rule_name
	context["doc_url"] = get_url_to_form(doc.doctype, doc.name)
	date_value = doc.get(rule.date_field) if rule.date_field else None
	context["days_left"] = (getdate(date_value) - getdate(today())).days if date_value else ""
	return context


class Renderer:
	"""Compiles a rule's templates once, so rendering many documents does not re-parse them."""

	def __init__(self, rule):
		self.rule = rule
		self.subject = _text_env.from_string(_unescape_jinja(rule.subject))
		self.message = _html_env.from_string(_unescape_jinja(rule.message))

	def render(self, doc) -> tuple[str, str]:
		"""Returns the subject as plain text and the message as HTML."""
		context = get_context(self.rule, doc)
		subject = strip_html(self.subject.render(context)).strip()
		message = self.message.render(context).strip().replace("\n", "<br>\n")
		return subject, message


def system_notification_title(subject: str) -> str:
	return escape_html(subject)


def validate_template(template: str, label: str, doctype: str) -> None:
	"""Reject syntax errors and unknown fields at save time. Without this, a typo in a field name
	would silently print nothing in every reminder for months."""
	source = _unescape_jinja(template)
	try:
		parsed = _text_env.parse(source)
		_text_env.from_string(parsed)  # Compiling also finds what parsing cannot: an unknown filter.
	except TemplateSyntaxError as e:
		frappe.throw(
			_(
				"The {0} has a placeholder that isn't written correctly, near line {1}. A placeholder "
				"looks like {2}. Click Insert Field to see the names you can use."
			).format(label, e.lineno, "{{ employee_name }}"),
			title=_("Please Check the {0}").format(label),
		)

	# The sandbox would stop these at send time anyway; refusing them here means no failed reminders.
	for node in parsed.find_all((nodes.Getattr, nodes.Getitem)):
		name = node.attr if isinstance(node, nodes.Getattr) else getattr(node.arg, "value", None)
		if isinstance(name, str) and name.startswith("_"):
			frappe.throw(
				_(
					"The {0} uses {1}, which can't be used in a notification. Click Insert Field to see the names you can use."
				).format(label, name),
				title=_("Please Check the {0}").format(label),
			)

	valid = EXTRA_VARIABLES | set(default_fields) | set(get_aliases(doctype))
	valid |= {df.fieldname for df in frappe.get_meta(doctype).fields if _is_exposed(df)}
	if unknown := sorted(meta.find_undeclared_variables(parsed) - valid):
		frappe.throw(
			_(
				"The {0} uses {1}, but {2} has no field with that name. Click Insert Field to see the names you can use."
			).format(label, ", ".join(frappe.bold("{{ " + name + " }}") for name in unknown), _(doctype)),
			title=_("Please Check the {0}").format(label),
		)
