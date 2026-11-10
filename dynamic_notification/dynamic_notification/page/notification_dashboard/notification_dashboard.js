const DASHBOARD_API = "dynamic_notification.api";
const STATUS_COLORS = { Sent: "green", Queued: "blue", Failed: "red", Skipped: "gray", Success: "green" };

frappe.pages["notification-dashboard"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Notification Dashboard"),
		single_column: true,
	});
	page.set_primary_action(
		__("New Notification"),
		() => frappe.new_doc("Notification Rule"),
		"add"
	);
	page.set_secondary_action(__("Refresh"), () => load(page));
	page.body.append(`<div class="dn-dashboard" style="padding: var(--padding-md)"></div>`);
	load(page);
};

frappe.pages["notification-dashboard"].on_page_show = function (wrapper) {
	if (wrapper.page) load(wrapper.page);
};

const esc = (value) => frappe.utils.escape_html(String(value ?? ""));
const when = (value) => (value ? esc(frappe.datetime.str_to_user(value)) : "-");

const list_route = (doctype) => `/desk/${frappe.router.slug(doctype)}`;
const form_route = (doctype, name) => `${list_route(doctype)}/${encodeURIComponent(name)}`;
const form_link = (doctype, name, label = name) =>
	`<a href="${form_route(doctype, name)}">${esc(label)}</a>`;
const RULE = "Notification Rule";
const LOG = "Notification Delivery Log";

function load(page) {
	frappe.call({ method: `${DASHBOARD_API}.get_dashboard` }).then(({ message: data }) => {
		const body = page.body.find(".dn-dashboard").empty();
		body.append(render_cards(data));
		body.append(render_rules(data));
		body.append(render_upcoming(data));
		body.append(render_failures(data));
		body.append(render_recent(data));
		bind_actions(body, page);
	});
}

function pill(status) {
	if (!status) return "-";
	return `<span class="indicator-pill ${STATUS_COLORS[status] || "gray"}">${esc(__(status))}</span>`;
}

function section(title, content, link = "") {
	return `<div class="frappe-card" style="margin-bottom: var(--margin-md); padding: var(--padding-md)">
		<div class="flex justify-between" style="margin-bottom: var(--margin-sm)">
			<b>${title}</b>${link}
		</div>
		<div style="overflow-x: auto">${content}</div>
	</div>`;
}

function table(headers, rows, empty) {
	if (!rows.length) return `<p class="text-muted" style="margin: 0">${empty}</p>`;
	return `<table class="table table-sm" style="margin: 0">
		<thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
		<tbody>${rows.join("")}</tbody>
	</table>`;
}

function render_cards({ totals, stats_days }) {
	const card = (label, value, route, color = "") => `
		<a href="${route}" class="frappe-card"
			style="flex: 1; min-width: 150px; padding: var(--padding-md); color: inherit; text-decoration: none">
			<div class="text-muted small">${label}</div>
			<div style="font-size: var(--text-2xl); font-weight: 600; color: ${color}">${value}</div>
		</a>`;
	const log_list = (status) => `${list_route(LOG)}?status=${encodeURIComponent(status)}`;
	const last = __("last {0} days", [stats_days]);
	return `<div style="display: flex; flex-wrap: wrap; gap: var(--margin-md); margin-bottom: var(--margin-md)">
		${card(__("Active Notifications"), totals.active, `${list_route(RULE)}?enabled=1`)}
		${card(__("Disabled"), totals.disabled, `${list_route(RULE)}?enabled=0`)}
		${card(__("Delivered ({0})", [last]), totals.sent, log_list("Sent"), "var(--green-600)")}
		${card(__("Failed ({0})", [last]), totals.failed, log_list("Failed"), totals.failed ? "var(--red-600)" : "")}
		${card(__("Skipped ({0})", [last]), totals.skipped, log_list("Skipped"))}
	</div>`;
}

function render_rules({ rules }) {
	const rows = rules.map((rule) => {
		const next = rule.next_notification;
		const route = `/desk/notification-rule/${encodeURIComponent(rule.name)}`;
		const history = `/desk/notification-delivery-log?rule=${encodeURIComponent(rule.name)}`;
		return `<tr data-rule="${esc(rule.name)}" data-doctype="${esc(rule.document_type)}">
			<td><input type="checkbox" class="dn-toggle" ${rule.enabled ? "checked" : ""}
				${rule.can_write ? "" : "disabled"} title="${__("Enable or disable")}"></td>
			<td><a href="${route}">${esc(rule.name)}</a>
				<div class="text-muted small">${rule.schedule.map(esc).join("; ")}</div></td>
			<td><a href="${list_route(rule.document_type)}">${esc(rule.document_type)}</a></td>
			<td>${next ? `${when(next.at)}<div class="text-muted small">${upcoming_document(next)}</div>` : "-"}</td>
			<td>${when(rule.last_run)}<div>${pill(rule.last_status)}</div></td>
			<td><a href="${history}">${rule.sent}</a></td>
			<td><a href="${history}&status=Failed" class="${rule.failed ? "text-danger" : ""}">${rule.failed}</a></td>
			<td class="text-nowrap">
				<a class="btn btn-default btn-xs" href="${route}">${__("Edit")}</a>
				<button class="btn btn-default btn-xs dn-preview">${__("Preview")}</button>
				${rule.can_write ? `<button class="btn btn-default btn-xs dn-run">${__("Run Now")}</button>` : ""}
			</td>
		</tr>`;
	});
	return section(
		__("Notifications"),
		table(
			[__("On"), __("Name"), __("DocType"), __("Next Notification"), __("Last Run"), __("Delivered"), __("Failed"), ""],
			rows,
			__("No notifications are configured yet.")
		)
	);
}

// Some reminders go to every matching document, so there is no single document to open.
function upcoming_document(item) {
	return item.reference_doctype ? form_link(item.reference_doctype, item.document) : esc(item.document);
}

function render_upcoming({ upcoming }) {
	const rows = upcoming.map(
		(item) => `<tr>
			<td>${when(item.at)}</td>
			<td>${form_link(RULE, item.rule)}</td>
			<td>${upcoming_document(item)}</td>
			<td>${esc(item.label)}</td>
		</tr>`
	);
	return section(
		__("Next Scheduled"),
		table(
			[__("When"), __("Notification"), __("Document"), __("Reminder")],
			rows,
			__("Nothing is scheduled: no document reaches a reminder date yet.")
		)
	);
}

function log_row(log) {
	const retry =
		log.status === "Failed" && log.channel !== "-"
			? `<button class="btn btn-default btn-xs dn-retry" data-log="${esc(log.name)}">${__("Retry")}</button>`
			: "";
	const log_route = `/desk/notification-delivery-log/${encodeURIComponent(log.name)}`;
	// The first line is the plain reason; the technical detail stays on the log itself.
	const reason = ["Failed", "Skipped"].includes(log.status)
		? (log.error_message || "").split("\n")[0]
		: "";
	return `<tr>
		<td><a href="${log_route}">${when(log.creation)}</a></td>
		<td>${form_link(RULE, log.rule)}</td>
		<td>${form_link(log.reference_doctype, log.reference_name)}</td>
		<td>${esc(log.recipient)}</td>
		<td>${esc(log.channel)}</td>
		<td>${pill(log.status)}${reason ? `<div class="text-muted small">${esc(reason)}</div>` : ""}</td>
		<td>${retry}</td>
	</tr>`;
}

const LOG_HEADERS = () => [
	__("Time"),
	__("Notification"),
	__("Document"),
	__("Recipient"),
	__("Channel"),
	__("Status"),
	"",
];

function render_failures({ failures }) {
	return section(
		__("Failed Deliveries"),
		table(LOG_HEADERS(), failures.map(log_row), __("Nothing has failed.")),
		`<a href="/desk/notification-delivery-log?status=Failed">${__("View all")}</a>`
	);
}

function render_recent({ recent }) {
	return section(
		__("Notification History"),
		table(LOG_HEADERS(), recent.map(log_row), __("Nothing has been sent yet.")),
		`<a href="/desk/notification-delivery-log">${__("View all")}</a>`
	);
}

function bind_actions(body, page) {
	const rule_of = (el) => $(el).closest("tr").attr("data-rule");

	body.find(".dn-toggle").on("change", function () {
		frappe
			.call({
				method: `${DASHBOARD_API}.set_enabled`,
				args: { rule: rule_of(this), enabled: this.checked ? 1 : 0 },
				freeze: true,
			})
			.always(() => load(page));
	});

	body.find(".dn-preview").on("click", function () {
		const row = $(this).closest("tr");
		dynamic_notification.show_preview(row.attr("data-rule"), row.attr("data-doctype"));
	});

	body.find(".dn-run").on("click", function () {
		dynamic_notification.run_now(rule_of(this), () => load(page));
	});

	body.find(".dn-retry").on("click", function () {
		frappe
			.call({
				method: `${DASHBOARD_API}.retry_delivery`,
				args: { log: $(this).attr("data-log") },
				freeze: true,
			})
			.then(({ message: status }) => {
				frappe.show_alert({ message: __("Status: {0}", [__(status)]), indicator: "blue" });
				load(page);
			});
	});
}
