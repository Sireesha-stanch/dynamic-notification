// Preview and Run Now, shared by the rule form and the dashboard.
frappe.provide("dynamic_notification");

dynamic_notification.show_preview = function (rule, document_type, docname) {
	const esc = frappe.utils.escape_html;
	frappe
		.call({ method: "dynamic_notification.api.preview", args: { rule, docname } })
		.then(({ message: result }) => {
			const dialog = new frappe.ui.Dialog({
				title: __("Preview: {0}", [result.docname]),
				size: "large",
				fields: [
					{
						fieldtype: "Link",
						fieldname: "document",
						label: __("Preview With Document"),
						options: document_type,
						default: result.docname,
						onchange() {
							const value = dialog.get_value("document");
							if (value && value !== result.docname) {
								dialog.hide();
								dynamic_notification.show_preview(rule, document_type, value);
							}
						},
					},
					{ fieldtype: "HTML", fieldname: "body" },
				],
			});
			const recipients = result.recipients.length
				? result.recipients.map(esc).join(", ")
				: `<span class="text-danger">${__("Nobody: no recipient could be resolved")}</span>`;
			const warning = result.matches_conditions
				? ""
				: `<p class="text-danger">${__(
						"This document does not match the conditions, so it would not be notified."
				  )}</p>`;
			// The server sanitizes the message and strips the subject of tags.
			dialog.get_field("body").$wrapper.html(`
				${warning}
				<p><b>${__("To")}:</b> ${recipients}</p>
				<p><b>${__("Subject")}:</b> ${esc(result.subject)}</p>
				<hr><div>${result.message}</div>`);
			dialog.show();
		});
};

dynamic_notification.show_run_result = function ({ rule, counts, errors }) {
	const sent = (counts.Sent || 0) + (counts.Queued || 0);
	const failed = counts.Failed || 0;
	let message = __(
		"{0}: nothing new to send. Everyone due today has already been notified, or no document is due today.",
		[rule]
	);
	let indicator = "blue";
	if (errors.length) {
		message = __("{0}: could not finish. See Failed Deliveries or the Error Log.", [rule]);
		indicator = "red";
	} else if (failed) {
		message = __("{0}: {1} sent, {2} failed. See Failed Deliveries.", [rule, sent, failed]);
		indicator = "red";
	} else if (sent) {
		message = __("{0}: {1} notification(s) sent.", [rule, sent]);
		indicator = "green";
	} else if (counts.Skipped) {
		message = __("{0}: nothing sent, {1} skipped. See Notification History.", [rule, counts.Skipped]);
		indicator = "orange";
	}
	frappe.show_alert({ message: frappe.utils.escape_html(message), indicator }, 10);
};

dynamic_notification.run_now = function (rule, on_finished) {
	frappe.confirm(
		__("Send today's reminders for {0} now? Anyone already notified will not be notified again.", [
			frappe.utils.escape_html(rule),
		]),
		() => {
			const event = "notification_run_finished";
			frappe.realtime.off(event);
			frappe.realtime.on(event, (result) => {
				if (result.rule !== rule) return;
				frappe.realtime.off(event);
				dynamic_notification.show_run_result(result);
				on_finished?.();
			});
			frappe.call({ method: "dynamic_notification.api.run_now", args: { rule } }).then(() => {
				frappe.show_alert({ message: __("Checking who is due today..."), indicator: "blue" });
			});
		}
	);
};
