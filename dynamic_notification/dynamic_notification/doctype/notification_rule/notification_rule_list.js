frappe.listview_settings["Notification Rule"] = {
	add_fields: ["enabled"],
	get_indicator(doc) {
		return doc.enabled
			? [__("Enabled"), "green", "enabled,=,1"]
			: [__("Disabled"), "gray", "enabled,=,0"];
	},
	onload(listview) {
		listview.page.add_inner_button(__("Dashboard"), () => {
			frappe.set_route("notification-dashboard");
		});
	},
};
