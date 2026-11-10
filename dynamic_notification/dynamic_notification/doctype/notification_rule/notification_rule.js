const API = "dynamic_notification.api";

frappe.ui.form.on("Notification Rule", {
	setup(frm) {
		frm.set_query("document_type", () => ({
			filters: { istable: 0, issingle: 0, is_virtual: 0 },
		}));
	},

	refresh(frm) {
		load_field_choices(frm);
		render_conditions(frm);
		if (frm.is_new()) return;

		frm.add_custom_button(
			__("Preview"),
			() => dynamic_notification.show_preview(frm.doc.name, frm.doc.document_type),
			__("Actions")
		);
		frm.add_custom_button(__("Upcoming"), () => show_upcoming(frm), __("Actions"));
		frm.add_custom_button(__("Delivery History"), () => {
			frappe.set_route("List", "Notification Delivery Log", { rule: frm.doc.name });
		});
		if (frm.perm[0].write) {
			frm.add_custom_button(__("Run Now"), () => dynamic_notification.run_now(frm.doc.name), __("Actions"));
		}
	},

	document_type(frm) {
		frm.field_choices = null;
		frm.set_value("date_field", "");
		frm.set_value("conditions", "[]");
		load_field_choices(frm);
		render_conditions(frm);
	},

	docstatus_filter(frm) {
		render_conditions(frm);
	},

	insert_field(frm) {
		show_insert_field(frm);
	},
});

// The field lists depend on the chosen DocType, so they are filled in here rather than in the schema.
function load_field_choices(frm) {
	if (!frm.doc.document_type) return;
	frappe
		.call({ method: `${API}.get_doctype_fields`, args: { doctype: frm.doc.document_type } })
		.then(({ message: choices }) => {
			frm.field_choices = choices;
			frm.doc.is_submittable = choices.is_submittable;
			frm.refresh_field("docstatus_filter");
			frm.set_df_property("date_field", "options", [
				{ value: "", label: "" },
				...choices.date_fields,
			]);
			frm.refresh_field("date_field");

			const grid = frm.fields_dict.recipients.grid;
			grid.update_docfield_property("employee_field", "options", [
				{ value: "", label: "" },
				...choices.employee_fields,
			]);
			grid.update_docfield_property("email_field", "options", [
				{ value: "", label: "" },
				...choices.email_fields,
			]);
			frm.refresh_field("recipients");
			render_conditions(frm);
		});
}

function get_conditions(frm) {
	return JSON.parse(frm.doc.conditions || "[]");
}

const CUSTOM_CONDITION = "__custom__";

function set_conditions(frm, conditions) {
	frm.set_value("conditions", JSON.stringify(conditions));
	render_conditions(frm);
}

function render_conditions(frm) {
	const wrapper = $(frm.get_field("conditions_html").wrapper);
	if (!frm.doc.document_type) {
		wrapper.empty();
		return;
	}
	// Field labels come from the DocType's metadata, which may not be in the browser yet.
	frappe.model.with_doctype(frm.doc.document_type, () => draw_conditions(frm, wrapper));
}

const same_condition = (a, b) =>
	a[0] === b[0] && a[1] === b[1] && a[2] === b[2] && String(a[3]) === String(b[3]);

function get_selected_choice(conditions, presets) {
	if (!conditions.length) return "";
	const match = presets.find(
		(preset) =>
			preset.conditions.length === conditions.length &&
			preset.conditions.every((p) => conditions.some((c) => same_condition(p, c)))
	);
	return match ? match.label : CUSTOM_CONDITION;
}

function draw_conditions(frm, wrapper) {
	wrapper.empty();
	const presets = frm.field_choices?.condition_presets || [];
	const conditions = get_conditions(frm);
	const selected = get_selected_choice(conditions, presets);

	const control = frappe.ui.form.make_control({
		parent: wrapper,
		df: {
			fieldtype: "Select",
			fieldname: "condition_choice",
			label: __("Condition"),
			read_only: frm.perm[0].write ? 0 : 1,
			options: [
				{ value: "", label: __("All Documents") },
				...presets.map((preset) => ({ value: preset.label, label: preset.label })),
				{ value: CUSTOM_CONDITION, label: __("Custom Condition...") },
			],
		},
		render_input: true,
	});
	control.set_input(selected);
	control.$input.on("change", () => {
		const value = control.$input.val();
		if (value === CUSTOM_CONDITION) {
			edit_conditions(frm);
			return;
		}
		set_conditions(frm, presets.find((preset) => preset.label === value)?.conditions || []);
	});

	const help = $(`<p class="help-box small text-muted"></p>`).appendTo(wrapper);
	if (selected === CUSTOM_CONDITION) {
		help.append(document.createTextNode(`${conditions.map(describe_condition).join(", ")} `));
		if (frm.perm[0].write) {
			$(`<a class="edit-conditions">${__("Edit")}</a>`)
				.on("click", () => edit_conditions(frm))
				.appendTo(help);
			help.append(" ");
		}
	}
	show_match_count(frm, $(`<span class="match-count"></span>`).appendTo(help));
}

function describe_condition(condition) {
	const df = frappe.meta.get_docfield(condition[0], condition[1]);
	return `${df ? __(df.label) : condition[1]} ${condition[2]} ${condition[3] ?? ""}`.trim();
}

function show_match_count(frm, target) {
	frappe
		.call({
			method: `${API}.count_matching`,
			args: {
				doctype: frm.doc.document_type,
				conditions: frm.doc.conditions || "[]",
				docstatus_filter: frm.doc.docstatus_filter,
			},
		})
		.then(({ message: count }) => {
			target.text(__("{0} document(s) match today", [count]));
		});
}

function edit_conditions(frm) {
	const dialog = new frappe.ui.Dialog({
		title: __("Custom Condition"),
		size: "large",
		fields: [{ fieldtype: "HTML", fieldname: "filter_area" }],
		primary_action_label: __("Update"),
		primary_action() {
			frm.set_value(
				"conditions",
				JSON.stringify(filter_group.get_filters().map((f) => f.slice(0, 4)))
			);
			dialog.hide();
		},
	});
	// Also covers cancelling, which puts the Condition field back to what is really saved.
	dialog.onhide = () => render_conditions(frm);

	const filter_group = new frappe.ui.FilterGroup({
		parent: dialog.get_field("filter_area").$wrapper,
		doctype: frm.doc.document_type,
		on_change() {},
	});
	// The filter builder needs the DocType's metadata to offer its fields and their value pickers.
	frappe.model.with_doctype(frm.doc.document_type, () => {
		filter_group.add_filters_to_filter_group(get_conditions(frm));
		dialog.show();
		if (!get_conditions(frm).length) dialog.$wrapper.find(".add-filter").first().trigger("click");
	});
}

function show_insert_field(frm) {
	if (!frm.field_choices) {
		frappe.msgprint(__("Please choose a DocType first. The fields you can use depend on it."));
		return;
	}
	const dialog = new frappe.ui.Dialog({
		title: __("Insert Field"),
		fields: [
			{
				fieldtype: "Select",
				fieldname: "field",
				label: __("Field"),
				reqd: 1,
				options: frm.field_choices.message_fields,
			},
			{
				fieldtype: "Select",
				fieldname: "target",
				label: __("Insert Into"),
				reqd: 1,
				default: "message",
				options: [
					{ value: "message", label: __("Message") },
					{ value: "subject", label: __("Subject") },
				],
			},
		],
		primary_action_label: __("Insert"),
		primary_action({ field, target }) {
			insert_at_caret(frm, target, `{{ ${field} }}`);
			dialog.hide();
		},
	});
	dialog.show();
}

function insert_at_caret(frm, fieldname, text) {
	const input = frm.get_field(fieldname).$input?.get(0);
	const value = frm.doc[fieldname] || "";
	const at = input?.selectionStart ?? value.length;
	frm.set_value(fieldname, value.slice(0, at) + text + value.slice(at));
}

function show_upcoming(frm) {
	frappe
		.call({ method: `${API}.get_upcoming`, args: { rule: frm.doc.name } })
		.then(({ message: items }) => {
			const rows = items
				.map(
					(item) => `<tr>
						<td>${frappe.datetime.str_to_user(item.at)}</td>
						<td>${frappe.utils.escape_html(item.document)}</td>
						<td>${frappe.utils.escape_html(item.label)}</td>
					</tr>`
				)
				.join("");
			frappe.msgprint({
				title: __("Upcoming Notifications"),
				message: rows
					? `<table class="table table-bordered">
						<thead><tr><th>${__("When")}</th><th>${__("Document")}</th><th>${__("Reminder")}</th></tr></thead>
						<tbody>${rows}</tbody></table>`
					: __("Nothing is scheduled: no document reaches a reminder date yet."),
			});
		});
}
