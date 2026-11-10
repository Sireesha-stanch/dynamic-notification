app_name = "dynamic_notification"
app_title = "Dynamic Notification"
app_publisher = "sireesha"
app_description = "Configurable notification and reminder for ERPNext"
app_email = "sireeshaguntha113@gmail.com"
app_license = "mit"

required_apps = ["frappe/erpnext"]

add_to_apps_screen = [
	{
		"name": "dynamic_notification",
		"logo": "/assets/dynamic_notification/images/logo.svg",
		"title": "Dynamic Notifications",
		"route": "/desk/notification-dashboard",
	}
]

after_install = "dynamic_notification.install.after_install"
before_uninstall = "dynamic_notification.install.before_uninstall"

# The engine only looks at schedule rows that are due, so frequent ticks stay cheap.
scheduler_events = {
	# "all" runs on every wake of Frappe's scheduler (4 minutes by default). A cron of */5 would only
	# be looked at on those wakes, so it would sometimes wait for a second one: 8 minutes between runs.
	"all": ["dynamic_notification.engine.runner.tick"],
}

# Preview and Run Now, which the rule form and the dashboard share.
app_include_js = "/assets/dynamic_notification/js/notification_actions.js"

# Other names a message may use for a field. ERPNext stores an employee's passport expiry in
# `valid_upto`, labelled just "Valid Up To"; the brief calls it Passport Expiry Date.
dynamic_notification_field_aliases = [
	{"doctype": "Employee", "alias": "passport_expiry_date", "fieldname": "valid_upto"},
]

# Choices in the rule form's Condition field, beside those built from a DocType's own Status field.
# An employee who has a relieving date but is still Active is serving their notice period.
dynamic_notification_condition_presets = [
	{
		"doctype": "Employee",
		"label": "Serving notice period",
		"conditions": [["status", "=", "Active"], ["relieving_date", "is", "set"]],
	},
	{
		"doctype": "Sales Invoice",
		"label": "Has outstanding amount",
		"conditions": [["outstanding_amount", ">", 0]],
	},
	{
		"doctype": "Purchase Invoice",
		"label": "Has outstanding amount",
		"conditions": [["outstanding_amount", ">", 0]],
	},
]

# Lets administrators set the retention of the delivery history under Log Settings. It must stay
# longer than any reminder lead time, because the history is also what prevents duplicates.
default_log_clearing_doctypes = {
	"Notification Delivery Log": 365,
}

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "dynamic_notification",
# 		"logo": "/assets/dynamic_notification/logo.png",
# 		"title": "Dynamic Notification",
# 		"route": "/dynamic_notification",
# 		"has_permission": "dynamic_notification.api.permission.has_app_permission",
# 	}
# ]

# The dock, the rail down the left of the desk, is a document rather than a hook. Author it in
# Manage Dock on a developer-mode site and press Export to App, and it is written to
# `dynamic_notification/dock/dynamic_notification/dynamic_notification.json` for git to carry. An app that ships none has no
# rail: its sidebar gets a switcher in the header instead.
#
# A companion app, one that extends a host app rather than standing on its own, says so with
# `mount_on` on that same record, and its entries are appended to the host's rail. Mounting keeps
# the companion off the apps screen, so it takes precedence over any add_to_apps_screen above.

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/dynamic_notification/css/dynamic_notification.css"
# app_include_js = "/assets/dynamic_notification/js/dynamic_notification.js"

# include js, css files in header of web template
# web_include_css = "/assets/dynamic_notification/css/dynamic_notification.css"
# web_include_js = "/assets/dynamic_notification/js/dynamic_notification.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "dynamic_notification/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_kanban_js = {"doctype" : "public/js/doctype_kanban.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "dynamic_notification/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "dynamic_notification.utils.jinja_methods",
# 	"filters": "dynamic_notification.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "dynamic_notification.install.before_install"
# after_install = "dynamic_notification.install.after_install"
before_uninstall = "dynamic_notification.install.before_uninstall"

# Uninstallation
# ------------

# before_uninstall = "dynamic_notification.uninstall.before_uninstall"
# after_uninstall = "dynamic_notification.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "dynamic_notification.utils.before_app_install"
# after_app_install = "dynamic_notification.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "dynamic_notification.utils.before_app_uninstall"
# after_app_uninstall = "dynamic_notification.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "dynamic_notification.build.after_build"

# To hook into the build process of other apps
# The list of apps being built is passed as an argument

# after_app_build = "dynamic_notification.build.after_app_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "dynamic_notification.notifications.get_notification_config"

# Awesome Bar
# -----------
# Extra search results: list of dicts with label, description, route, index.
# route: ["List", "ToDo"], "/desk/docs/some/page", or "https://example.com"
# awesomebar_search = ["dynamic_notification.search.awesomebar_results"]

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"dynamic_notification.tasks.all"
# 	],
# 	"daily": [
# 		"dynamic_notification.tasks.daily"
# 	],
# 	"hourly": [
# 		"dynamic_notification.tasks.hourly"
# 	],
# 	"weekly": [
# 		"dynamic_notification.tasks.weekly"
# 	],
# 	"monthly": [
# 		"dynamic_notification.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "dynamic_notification.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "dynamic_notification.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "dynamic_notification.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "dynamic_notification.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["dynamic_notification.utils.before_request"]
# after_request = ["dynamic_notification.utils.after_request"]

# Job Events
# ----------
# before_job = ["dynamic_notification.utils.before_job"]
# after_job = ["dynamic_notification.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"dynamic_notification.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
