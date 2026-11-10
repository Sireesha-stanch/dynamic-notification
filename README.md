# Dynamic Notification

A configurable notification and reminder engine for ERPNext. An administrator or HR user can set up
reminders for any DocType from the screen: choose the date field, the schedule, the conditions, the
recipients, the channels and the message. No code and no developer involved.

- **Schedules:** before, on or after a document's date, one time, recurring, and any number of reminders per rule
- **Recipients:** users, roles, departments, the document owner, assignees, the related employee, a field in the document, any email address
- **Channels:** email and ERPNext system notification, with a hook for adding others such as WhatsApp
- **Conditions:** built with ERPNext's normal filter builder
- **Dashboard:** all rules with schedules, on/off switches, next scheduled notification, failed deliveries, history, Preview, Run Now and Retry
- **No duplicates:** enforced by a unique key in the database

Includes the *Employee Passport Expiry Notification* rule, ready to use, and a *Quotation Expiry Follow-up* rule, switched off, showing the same engine on another DocType.

## Configure a notification, without a developer

As a user with the *HR Manager*, *Notification Manager* or *System Manager* role. This example rebuilds the passport reminder.

1. Open `/desk/notification-dashboard` and click **New Notification**.
2. Enter a **Notification Name**, choose **DocType** `Employee` and **Date Field** `Passport Expiry Date (valid_upto)`. The field lists follow the chosen DocType.
3. Under **When to Notify**, add a row per reminder: *Before Date* with 30 days, *Before Date* with 15 days, and *On Date*. One Time, After Date and Recurring (daily, weekly, monthly, yearly or a custom interval) are chosen the same way.
4. In **Condition**, choose **Status: Active**. **Custom Condition...** at the end of the list opens the standard filter builder for any other field.
5. Under **Who to Notify**, add a row: **Send To** `Role`, **Role** `HR Manager`.
6. Under **How to Notify**, tick **Email** and **System Notification**.
7. Write the **Subject** and **Message**. **Insert Field** adds placeholders such as `{{ employee_name }}` and `{{ valid_upto }}`.
8. Save. On the dashboard, **Preview** shows the real message without sending, **Run Now** sends today's reminders, and the rule can be switched on or off, edited or retried from there.

Reminders go out shortly after their time, because the scheduler checks about every 4 minutes.

## Architecture

```
Scheduler -> tick() -> one background job per due reminder -> process_schedule()
   -> documents.py (which documents) -> recipients.py (who) -> render.py (what)
   -> Delivery Log row with a UNIQUE key -> channels.py (Email Queue, Notification Log)
```

| Part | Responsibility |
| --- | --- |
| `Notification Rule` with child tables `Notification Schedule` and `Notification Rule Recipient` | The configuration, stored as plain data and validated on save |
| `Notification Delivery Log` | History, failure reasons, retries and the duplicate guard |
| `engine/schedule.py` | Date arithmetic: next run, recurrence, catch-up after missed days |
| `engine/documents.py` | Validates conditions and reads matching documents a page at a time |
| `engine/recipients.py` | Turns recipient rows into people, each notified once |
| `engine/render.py` | Sandboxed Jinja templates, checked on save |
| `engine/channels.py` | Email and system notification, extensible by other apps |
| `engine/runner.py` | The scheduled job, delivery, retries and email status tracking |
| `api.py`, `page/notification_dashboard` | Permission-checked endpoints and the dashboard |

Design decisions:

- **Reusable:** nothing in the engine is tied to one DocType. The passport and quotation rules are ordinary data created on installation.
- **Efficient:** each run reads only the reminders that are due, through an indexed `next_run` column. Documents are paged by name rather than OFFSET, and a rule's date field is indexed automatically.
- **No duplicates:** each delivery has a key from its rule, document, occurrence, recipient and channel, and that column is UNIQUE in the database, so a crashed job or two workers cannot notify anyone twice.
- **Missed runs:** after downtime, a date-based reminder scans the days it missed, up to 3.
- **Failures:** one bad document or failed send does not stop the rest. A failed delivery retries after 15, 30 and 60 minutes, then stops, and can be retried from the dashboard.
- **Security:** conditions are validated and reach the database only as parameterised filters. Templates cannot reach the database or password fields. Saving a rule requires read access to its DocType, and every endpoint checks permissions.
- **Other channels:** another app can add WhatsApp or similar through the `dynamic_notification_channels` hook without changing this app.

## Install on another machine

Needs a Frappe bench on version 16 with ERPNext. Python 3.14, as Frappe 16 requires. HRMS is optional.

```bash
cd frappe-bench

bench get-app dynamic_notification https://github.com/Sireesha-stanch/dynamic-notification --branch main

bench --site <your-site> install-app dynamic_notification
bench --site <your-site> migrate
bench --site <your-site> enable-scheduler
bench restart        # on production; on a development bench, restart `bench start`
```

Installing creates the role, the scheduled job, the dashboard, the passport expiry rule, and a Quotation follow-up rule that starts switched off. Then:

1. **Set up outgoing email** under *Email Account*, or the email channel records every send as Failed
   (system notifications work regardless).
2. **Give people access.** *System Manager*, *HR Manager* and the new *Notification Manager* role can
   all manage rules. Each person also needs read access to the DocTypes they write rules for.
3. **Open** `/desk/notification-dashboard`.

The app must have background workers running to send anything. `bench start` includes them, and a
production bench runs them under supervisor or systemd. Rules are stored per site, so each site has its
own.

To remove it: `bench --site <your-site> uninstall-app dynamic_notification`. This also deletes all
rules and history on that site.

## Run the tests

Use a test site where the ERPNext setup wizard is complete, because the tests need a Company and Gender records. Rules created by the tests match every document of their DocType on that site.

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app dynamic_notification
```

## License

MIT
