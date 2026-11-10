frappe.listview_settings["Notification Delivery Log"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const colors = { Sent: "green", Queued: "blue", Failed: "red", Skipped: "gray" };
		return [__(doc.status), colors[doc.status], `status,=,${doc.status}`];
	},
};
