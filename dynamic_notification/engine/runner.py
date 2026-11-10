"""The notification engine: decides what is due, delivers it once, and records the outcome.

	tick()                on every scheduler wake, finds due schedule rows and enqueues a job for each
	process_schedule()    one job: works out which documents are due and delivers to each of them
	deliver_document()    one document: renders, resolves recipients, sends, logs

Why a re-run is always safe: every delivery first inserts a Notification Delivery Log row whose
`dedupe_key` is unique in the database. The key identifies the reminder by meaning (rule, document,
occurrence, recipient, channel), so a crashed job, two workers racing, or HR editing and re-saving the
schedule cannot notify anyone twice. A row is committed together with the Email Queue or
Notification Log entry it stands for, one document at a time.
"""

import hashlib
import re
import traceback
from datetime import timedelta

import frappe
from frappe.query_builder import DocType
from frappe.utils import add_days, add_to_date, get_datetime, getdate, now_datetime, strip_html

from dynamic_notification.engine import channels as channel_registry
from dynamic_notification.engine import documents, schedule
from dynamic_notification.engine.recipients import Recipient, RecipientResolver
from dynamic_notification.engine.render import Renderer

LOG_DOCTYPE = "Notification Delivery Log"
RULE_DOCTYPE = "Notification Rule"
ROW_DOCTYPE = "Notification Schedule"

SENT, QUEUED, FAILED, SKIPPED = "Sent", "Queued", "Failed", "Skipped"

MAX_DUE_PER_TICK = 200
MAX_RETRIES = 3
RETRY_BASE_MINUTES = 15
RETRIES_PER_TICK = 100
EMAIL_SYNC_PER_TICK = 500
FAILED_RUN_BACKOFF_MINUTES = 30
JOB_TIMEOUT = 3600


# -- Scheduler entry point


def tick() -> None:
	for row in get_due_rows():
		frappe.enqueue(
			"dynamic_notification.engine.runner.process_schedule",
			queue="long",
			timeout=JOB_TIMEOUT,
			job_id=f"dynamic-notification-{row.name}",
			deduplicate=True,
			rule_name=row.parent,
			row_name=row.name,
		)
	# Housekeeping steps are independent: one failing must not stop the other.
	for name in ("retry_failed", "sync_email_status"):
		try:
			globals()[name]()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Notification engine: {name} failed")


def get_due_rows() -> list:
	rule, row = DocType(RULE_DOCTYPE), DocType(ROW_DOCTYPE)
	return (
		frappe.qb.from_(row)
		.join(rule)
		.on(row.parent == rule.name)
		.select(row.name, row.parent)
		.where(
			(row.next_run <= now_datetime())
			& (row.enabled == 1)
			& (rule.enabled == 1)
			& (row.parenttype == RULE_DOCTYPE)
		)
		.orderby(row.next_run)
		.limit(MAX_DUE_PER_TICK)
		.run(as_dict=True)
	)


# -- One schedule row


def process_schedule(rule_name: str, row_name: str, manual: bool = False) -> dict:
	"""Run one schedule row. `manual` is Run Now: it scans today without moving the schedule, so the
	regular run later today still happens and picks up only what Run Now could not have seen."""
	rule = frappe.get_doc(RULE_DOCTYPE, rule_name)
	row = rule.get("reminders", {"name": row_name})
	row = row[0] if row else None
	if not row or not row.enabled or not rule.enabled:
		return {}

	now = now_datetime()
	if not manual and (not row.next_run or get_datetime(row.next_run) > now):
		return {}  # Another job already handled it, or HR moved the schedule while this one queued.

	label = row.summary or row.schedule_type
	try:
		counts = _deliver_row(rule, row, now, manual)
	except Exception:
		frappe.db.rollback()
		frappe.clear_messages()
		error = traceback.format_exc()
		frappe.log_error(title=f"Notification Rule failed: {rule_name}", message=error)
		if not manual:
			# Keep the window: the retry resumes and the unique key skips whatever was delivered.
			_finish_row(
				rule,
				row,
				status=FAILED,
				error=explain_failure(error),
				next_run=add_to_date(now, minutes=FAILED_RUN_BACKOFF_MINUTES),
			)
			frappe.db.commit()
		return {"error": label}

	if not manual:
		row.last_run = now
		if schedule.needs_date_field(row):
			row.processed_through = now.date()
		_finish_row(rule, row, status="Success", error=None, next_run=schedule.compute_next_run(row, now))
		frappe.db.commit()
	return counts


def run_rule_now(rule_name: str, user: str) -> None:
	"""Run Now: every reminder of a rule, then tells the person who pressed it what really happened."""
	counts, errors = {}, []
	for row in frappe.get_all(ROW_DOCTYPE, filters={"parent": rule_name}, pluck="name", order_by="idx"):
		result = process_schedule(rule_name, row, manual=True)
		if "error" in result:
			errors.append(result["error"])
			continue
		for status, count in result.items():
			counts[status] = counts.get(status, 0) + count
	frappe.publish_realtime(
		"notification_run_finished", {"rule": rule_name, "counts": counts, "errors": errors}, user=user
	)


def _deliver_row(rule, row, now, manual) -> dict:
	counts = {}
	delivery = _Delivery(rule, row)
	for docname, occurrence in _iter_due(rule, row, now, manual):
		for status, count in delivery.deliver_document(docname, occurrence).items():
			counts[status] = counts.get(status, 0) + count
	return counts


def _iter_due(rule, row, now, manual):
	"""Yields (document name, occurrence key) for everything this run of the row should notify."""
	today = now.date()
	processed_through = getdate(row.processed_through) if row.processed_through else None
	window = [today] if manual else schedule.get_scan_window(processed_through, today)
	if not window and schedule.needs_date_field(row):
		return  # Today has already been scanned.

	if schedule.is_relative(row):
		offset = schedule.signed_offset(row)
		start, end = window[0] - timedelta(days=offset), window[-1] - timedelta(days=offset)
		for doc in documents.iter_documents(rule, documents.date_range_filters(rule, start, end)):
			# Keyed on the document's own date: when a passport is renewed, its new expiry date
			# is a new occurrence and reminds again, but the same date never does.
			yield doc.name, f"offset:{offset}:{getdate(doc[rule.date_field])}"

	elif schedule.is_document_recurring(row):
		every, unit = schedule.get_interval(row)
		extra = [
			[rule.document_type, rule.date_field, "is", "set"],
			[rule.document_type, rule.date_field, "<", add_days(today, 1)],
		]
		for doc in documents.iter_documents(rule, extra):
			anchor = getdate(doc[rule.date_field])
			for day in window:
				if schedule.is_recurrence_day(anchor, day, every, unit):
					yield doc.name, f"recur:{day}"

	else:
		# One-time and fixed recurring rows notify every matching document at a given moment.
		occurrence = _fixed_occurrence(row, now, manual)
		if occurrence:
			for doc in documents.iter_documents(rule):
				yield doc.name, f"at:{occurrence.isoformat()}"


def _fixed_occurrence(row, now, manual):
	"""Which occurrence this run delivers, worked out from the schedule. Never from `next_run`: a failed
	run pushes that back, and its retry must count as the same occurrence or everyone is notified twice.
	Run Now delivers the occurrence already due, not a future one."""
	is_one_time = row.schedule_type == schedule.ONE_TIME
	start = row.run_at if is_one_time else row.starts_on
	if not start:
		return None
	start = get_datetime(start)
	if manual and start > now:
		return None
	if is_one_time:
		return start
	every, unit = schedule.get_interval(row)
	return schedule.last_fixed_occurrence(start, every, unit, max(now, start))


def _finish_row(rule, row, status, error, next_run) -> None:
	row.next_run = next_run
	frappe.db.set_value(
		ROW_DOCTYPE,
		row.name,
		{
			"next_run": next_run,
			"last_run": row.last_run,
			"processed_through": row.processed_through,
			"last_status": status,
			"last_error": error,
		},
		update_modified=False,
	)
	# Read from the database: sibling rows may have moved on during this job, which can run for a while.
	soonest = frappe.get_all(
		ROW_DOCTYPE,
		filters=[["parent", "=", rule.name], ["enabled", "=", 1], ["next_run", "is", "set"]],
		pluck="next_run",
		order_by="next_run asc",
		limit_page_length=1,
	)
	frappe.db.set_value(
		RULE_DOCTYPE,
		rule.name,
		{
			"next_run": soonest[0] if soonest else None,
			"last_run": now_datetime(),
			"last_status": status,
		},
		update_modified=False,
	)


# -- One document


def make_dedupe_key(rule, doctype, docname, occurrence, recipient_key, channel) -> str:
	raw = "|".join(map(str, (rule, doctype, docname, occurrence, recipient_key, channel)))
	return hashlib.sha256(raw.encode()).hexdigest()


class _Delivery:
	def __init__(self, rule, row):
		self.rule = rule
		self.label = row.summary or row.schedule_type
		self.channels = channel_registry.get_enabled_channels(rule)
		self.resolver = RecipientResolver(rule)
		self.renderer = Renderer(rule)

	def deliver_document(self, docname: str, occurrence: str) -> dict:
		"""Commits on the way out, so a crash later in the run keeps what has been sent."""
		counts = {}
		rule = self.rule
		try:
			doc = frappe.get_doc(rule.document_type, docname)
			targets = [
				(recipient, channel)
				for recipient in self.resolver.resolve(doc)
				for channel in self.channels
				if getattr(recipient, channel["address"])
			]
		except Exception:
			# One bad document must not stop the reminders for the thousands after it.
			frappe.db.rollback()
			self._log_blocked(docname, occurrence, FAILED, explain_failure(traceback.format_exc()))
			frappe.db.commit()
			return {FAILED: 1}

		if not targets:
			self._log_blocked(
				docname, occurrence, SKIPPED, "No recipient could be resolved for this document"
			)
			frappe.db.commit()
			return {SKIPPED: 1}

		keyed = {
			make_dedupe_key(rule.name, doc.doctype, doc.name, occurrence, r.key, c["name"]): (r, c)
			for r, c in targets
		}
		done = set(
			frappe.get_all(LOG_DOCTYPE, filters={"dedupe_key": ["in", list(keyed)]}, pluck="dedupe_key")
		)
		pending = {key: target for key, target in keyed.items() if key not in done}
		if not pending:
			return counts

		subject, message, render_error = None, None, None
		try:
			subject, message = self.renderer.render(doc)
		except Exception:
			render_error = traceback.format_exc()

		for key, (recipient, channel) in pending.items():
			log = self._claim(key, doc.name, occurrence, recipient, channel["name"], subject)
			if not log:
				continue  # Another worker got there first.
			if render_error:
				_apply_failure(log, render_error)
			else:
				_dispatch(log, channel, rule, doc, recipient, subject, message)
			log.db_update()
			counts[log.status] = counts.get(log.status, 0) + 1

		frappe.db.commit()
		return counts

	def _new_log(self, docname, occurrence, dedupe_key, **fields):
		log = frappe.new_doc(LOG_DOCTYPE)
		log.update(
			{
				"rule": self.rule.name,
				"schedule_label": self.label,
				"reference_doctype": self.rule.document_type,
				"reference_name": docname,
				"occurrence": occurrence,
				"dedupe_key": dedupe_key,
				**fields,
			}
		)
		return log

	def _claim(self, key, docname, occurrence, recipient, channel, subject):
		log = self._new_log(
			docname,
			occurrence,
			key,
			channel=channel,
			recipient=recipient.user or recipient.email,
			recipient_user=recipient.user,
			recipient_email=recipient.email,
			subject=subject,
			status=QUEUED,
		)
		savepoint = f"claim_{key[:12]}"
		frappe.db.savepoint(savepoint)
		try:
			log.insert(ignore_permissions=True, ignore_links=True)
		except frappe.UniqueValidationError, frappe.DuplicateEntryError:
			frappe.db.rollback(save_point=savepoint)
			frappe.clear_messages()
			return None
		return log

	def _log_blocked(self, docname, occurrence, status, error) -> None:
		"""Records a document nothing could be sent for, once per occurrence, so that HR can see why
		rather than wonder. Later runs find the same key and stay quiet."""
		key = make_dedupe_key(self.rule.name, self.rule.document_type, docname, occurrence, "-", "-")
		if frappe.db.exists(LOG_DOCTYPE, {"dedupe_key": key}):
			return
		log = self._new_log(
			docname, occurrence, key, channel="-", recipient="-", status=status, error_message=error
		)
		log.insert(ignore_permissions=True, ignore_links=True)


def _dispatch(log, channel, rule, doc, recipient, subject, message) -> None:
	"""Sends through one channel inside a savepoint, so a failure undoes only this one send."""
	savepoint = f"send_{log.name[:12]}"
	frappe.db.savepoint(savepoint)
	try:
		result = channel["send"](rule, doc, recipient, subject, message)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.clear_messages()
		_apply_failure(log, traceback.format_exc())
		return

	log.status = result.status
	log.error_message = result.note
	log.email_queue = result.email_queue
	log.next_retry_at = None
	log.sent_at = now_datetime() if result.status in (SENT, QUEUED) else None


def explain_failure(error: str | None) -> str:
	"""Puts the reason on the first line for the person reading the log, with the technical detail
	under it. A traceback's last line is its exception; the leading dotted class name means nothing
	to HR."""
	error = (error or "Unknown error").strip()
	if not error.startswith("Traceback"):
		return error
	reason = error.splitlines()[-1].strip()
	return f"{strip_html(re.sub(r'^[A-Za-z_][\w.]*: ', '', reason))}\n\n{error}"


def _apply_failure(log, error: str) -> None:
	log.status = FAILED
	log.error_message = explain_failure(error)
	log.sent_at = None
	retries = log.retry_count or 0
	log.next_retry_at = (
		add_to_date(now_datetime(), minutes=RETRY_BASE_MINUTES * 2**retries)
		if retries < MAX_RETRIES
		else None
	)


# -- Retries and email status


def retry_failed(limit: int = RETRIES_PER_TICK) -> None:
	names = frappe.get_all(
		LOG_DOCTYPE,
		# "is set" matters: Frappe compares NULL dates as a very old date, so `<=` alone would keep
		# retrying the deliveries that have used up their retries.
		filters=[
			["status", "=", FAILED],
			["next_retry_at", "is", "set"],
			["next_retry_at", "<=", now_datetime()],
		],
		order_by="next_retry_at asc",
		limit_page_length=limit,
		pluck="name",
	)
	for name in names:
		retry_delivery(name)
		frappe.db.commit()


def retry_delivery(log_name: str, manual: bool = False) -> str:
	"""Re-sends a failed delivery with fresh content, if it should still go out at all."""
	log = frappe.get_doc(LOG_DOCTYPE, log_name)
	if log.status != FAILED:
		return log.status

	attempts = (log.retry_count or 0) + 1
	try:
		rule = frappe.get_doc(RULE_DOCTYPE, log.rule) if frappe.db.exists(RULE_DOCTYPE, log.rule) else None
		channel = channel_registry.get_all_channels().get(log.channel)
		if not rule or not channel or not documents.matches(rule, log.reference_name):
			log.status = SKIPPED
			log.error_message = "Rule, channel or document no longer applies"
			log.next_retry_at = None
		elif not rule.enabled and not manual:
			# Wait with the rule. Re-enabling it picks the delivery back up at the next tick.
			log.next_retry_at = add_to_date(now_datetime(), hours=1)
		else:
			log.retry_count = attempts
			doc = frappe.get_doc(rule.document_type, log.reference_name)
			log.subject, message = Renderer(rule).render(doc)
			recipient = Recipient(log.recipient_user, log.recipient_email)
			_dispatch(log, channel, rule, doc, recipient, log.subject, message)
	except Exception:
		# A rule broken since (say, a deleted field) counts against the same limit as a failed send.
		frappe.clear_messages()
		log.retry_count = attempts
		_apply_failure(log, traceback.format_exc())
	log.db_update()
	return log.status


def sync_email_status(limit: int = EMAIL_SYNC_PER_TICK) -> None:
	"""Moves Queued deliveries to their final state once Frappe's email worker has tried them."""
	queued = frappe.get_all(
		LOG_DOCTYPE,
		# "is set" keeps out a custom channel's queued rows, which the Email Queue will never settle
		# and which would otherwise take up this batch for good.
		filters=[["status", "=", QUEUED], ["email_queue", "is", "set"]],
		fields=["name", "email_queue"],
		order_by="modified asc",
		limit_page_length=limit,
	)
	if not queued:
		return

	outcomes = frappe.get_all(
		"Email Queue",
		filters={"name": ["in", [row.email_queue for row in queued]]},
		fields=["name", "status", "error"],
	)
	outcomes = {row.name: row for row in outcomes}
	sent = [
		row.name
		for row in queued
		if row.email_queue in outcomes and outcomes[row.email_queue].status == "Sent"
	]
	if sent:
		frappe.db.set_value(LOG_DOCTYPE, {"name": ["in", sent]}, "status", SENT, update_modified=False)
	for row in queued:
		outcome = outcomes.get(row.email_queue)
		if not outcome:
			# Without this the row would stay Queued for good, and keep its place in every batch.
			frappe.db.set_value(
				LOG_DOCTYPE,
				row.name,
				{"status": FAILED, "error_message": "This email is no longer in the Email Queue"},
				update_modified=False,
			)
		elif outcome.status == "Error":
			# Frappe has already used up its own email retries; another round here is manual only.
			frappe.db.set_value(
				LOG_DOCTYPE,
				row.name,
				{"status": FAILED, "error_message": explain_failure(outcome.error), "next_retry_at": None},
				update_modified=False,
			)
	frappe.db.commit()


# -- Upcoming


def get_upcoming(rule, limit: int = 5) -> list[dict]:
	"""The next real notifications of a rule, soonest first. A row's `next_run` is only when the
	engine next looks; this answers when somebody will actually hear something."""
	now = now_datetime()
	upcoming = []
	for row in rule.reminders:
		if not row.enabled:
			continue
		label = row.summary or row.schedule_type
		if schedule.is_relative(row):
			upcoming += _upcoming_relative(rule, row, label, now, limit)
		elif row.next_run and get_datetime(row.next_run) >= now:
			target = (
				"Documents on their recurrence day"
				if schedule.is_document_recurring(row)
				else "All matching documents"
			)
			upcoming.append({"at": get_datetime(row.next_run), "document": target, "label": label})
	return sorted(upcoming, key=lambda item: item["at"])[:limit]


def _upcoming_relative(rule, row, label, now, limit) -> list[dict]:
	offset = schedule.signed_offset(row)
	send_time = schedule.get_time(row.send_time or "09:00:00")
	first_day = now.date() if now.time() < send_time else now.date() + timedelta(days=1)
	filters = [
		*documents.get_base_filters(rule),
		[rule.document_type, rule.date_field, ">=", str(first_day - timedelta(days=offset))],
	]
	rows = frappe.get_all(
		rule.document_type,
		filters=filters,
		fields=["name", rule.date_field],
		order_by=f"{rule.date_field} asc",
		limit_page_length=limit,
		distinct=documents.joins_child_table(rule.document_type, filters),
	)
	return [
		{
			"at": get_datetime(f"{getdate(doc[rule.date_field]) + timedelta(days=offset)} {send_time}"),
			"document": doc.name,
			"reference_doctype": rule.document_type,
			"label": label,
		}
		for doc in rows
	]
