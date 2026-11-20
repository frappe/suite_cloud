# Sending limits

A Mail Account's **Send Limit** counts recipients per UTC day. A message to 50 recipients counts as 50. Stalwart enforces it. Suite Cloud only counts what Stalwart accepted, in Mail Send Usage, for billing. A failure or outage of Suite Cloud does not change what an account may send.

The code is in `suite_cloud/mail/doctype/mail_send_limit/` (the limits), `suite_cloud/mail/cluster/quota.py` (the Stalwart config and the lookup keys), `suite_cloud/mail/send_usage.py` (the counting), `suite_cloud/api/send_usage.py` (the WebHook endpoint) and `suite_cloud/mail/doctype/mail_send_usage/mail_send_usage.py` (the stored totals).

## Mail Send Limit

A **Mail Send Limit** is one daily limit the accounts of one cluster may have, such as 1,000 recipients per day. Stalwart has one throttle per Mail Send Limit, so an operator keeps the list short. An account links to one of its cluster's limits; accounts with the same limit share its throttle, each with its own counter.

| Field | Rule |
| --- | --- |
| Mail Send Limit **Daily Limit** | At least 1, with no upper bound. Fixed once saved: to change a limit, add another and move the accounts to it. A limit an account or tenant links to cannot be deleted |
| Mail Send Limit **Default** | One per cluster. A tenant's Default Send Limit starts as it, and Stalwart applies it to every signed-in account that has no key. The default cannot be deleted. Marking another as the default clears the old one and pushes the cluster |
| Mail Account **Send Limit** | A link to any of its cluster's limits; an admin sets it on the form. Blank on save takes the tenant's Default Send Limit. **Daily Send Limit** shows its value |
| Suite Tenant **Default Send Limit** | Required once the tenant has a cluster: one of the cluster's limits, which an admin may change. Left blank, it takes the cluster's default; changing the cluster clears it. It is only the default for new accounts. The tenant object answers its value as `default_daily_send_limit` and lists the cluster's limits as `send_limits` |
| Stalwart Cluster **Submission Rate (Messages per Minute)** | At least 1, 90 by default. The messages each signed-in sender may submit per minute, whatever its limit. A saved change pushes the cluster |

A new cluster starts with 50 (the default), 250, 500, 1,000, 2,500, 5,000, 10,000, 25,000, 50,000 and 1,00,000. An admin adds a limit for anything else. The cluster form lists its limits, accounts, groups and lists under Connections. An admin sets an account's limit on its form; a tenant sets it through the API only while **Tenant May Set Send Limits** is on, by its value, which must be one of `send_limits`. There is no monthly limit. Mail Send Usage still keeps monthly totals.

## Stalwart config

The cluster plan holds two operations, from `quota.cluster_operations()`:

| Operation | Purpose |
| --- | --- |
| `upsert` MtaInboundThrottle, matched on `description` | `suite-cloud-send-limit-<daily limit>` for each Mail Send Limit: key `authenticatedAs`, `rate` the limit per day. The default takes every signed-in account without a key: `!key_exists('suite-cloud-send-limit', authenticated_as)`. Each other limit takes the accounts whose key holds it: `key_get('suite-cloud-send-limit', authenticated_as) == '<daily limit>'`. Each `match` also reads `rcpt`, so Stalwart checks it once per recipient at RCPT. `suite-cloud-submission-rate` caps each signed-in sender at the cluster's **Submission Rate (Messages per Minute)**, 90 by default, checked at MAIL FROM, whatever its limit |
| `upsert` WebHook `sending-usage`, matched on the header `X-Suite-Cloud-Hook: sending-usage` | Sends `queue.authenticated-message-queued` to `suite_cloud.api.send_usage.record` with the calling node's `SUITE_CLOUD_TOKEN`. It stays switched off until every Provisioned, Active and Draining node of the cluster has **Suite Cloud Token Installed**. **Enable Telemetry** does not matter. `discardAfter` is 24 hours |

Each account not on its cluster's default has a `MemoryLookupKeyValue` in the namespace `suite-cloud-send-limit`: the key is its address, the value its daily limit. Mail Account owns the limit and the key is a copy. A save that changes the limit, an insert and a delete write the key at once and run `ReloadLookupStores`. Stalwart reads keys only after that action; `ReloadSettings` does not reload them. A refused write fails the save.

A key whose limit has no throttle would leave the account unlimited. So before it writes the key, a save creates the limit's throttle when the cluster lacks it, then runs `ReloadSettings`. A new Mail Send Limit is not pushed on its own: the first account that takes it, its **Sync Throttle** button or the next Sync Config creates the throttle. Deleting a limit pushes the cluster, which deletes its throttle.

Sync Config applies the plan, then `quota.sync_send_limits()`: it deletes every MtaInboundThrottle that is not one of the plan's (one per Mail Send Limit and the submission rate), so Stalwart's default **Sender IP throttle** and **Sender address to recipient throttle**, the old `suite-cloud-quota-backstop` and the throttles of deleted limits go. Stalwart adds its defaults again only on a start that finds no throttle. It also deletes the old RCPT MtaHook (header `X-Suite-Cloud-Hook: sending-limits`) and makes the keys match the accounts. Then it reloads the settings.

Recovery mode leaves the throttles out: Stalwart adds its default throttles only on a normal start that finds none. The first push after it adds them.

An Egress Gateway throttles nothing: it takes mail only from the cluster's nodes, which have applied the accounts' limits already. Stalwart's defaults would cap each node at 5 messages a second (**Sender IP throttle**) and each sender domain at 25 messages an hour to one recipient. So the gateway plan keeps one disabled throttle, `suite-cloud-gateway-no-throttle`, and its Sync Config deletes every other. Stalwart adds its defaults again on any start that finds no throttle, disabled ones counted, so the placeholder keeps them out.

## Over a limit

A recipient over the account's limit, or a message over the submission rate, gets `452 4.4.5 Rate limit exceeded, try again later.` The text is fixed in Stalwart. 452 is temporary, so an SMTP client keeps the message and tries again later. The other recipients of the same message that fit are accepted. A JMAP `EmailSubmission/set` succeeds even when every recipient is refused: Stalwart writes the refusal only to `EmailSubmission.deliveryStatus.<address>`.

A throttle window is fixed: it starts at the UTC day, since Stalwart divides the time by the period. A refused recipient still counts in the window, so an account stays refused until the day ends. Each throttle has its own counter, so a limit change starts the account at zero in its new limit for the rest of the day.

## Counting

1. Stalwart queues a message from a signed-in sender and logs `queue.authenticated-message-queued` with `queueId`, `from` and the `to` list. Only recipients the throttles accepted are in it.
2. The WebHook sends the waiting events at most every 30 seconds:

   ```http
   POST /api/v2/method/suite_cloud.api.send_usage.record
   Authorization: Bearer <key>.<secret>
   Content-Type: application/json

   {"events": [{"id": "...", "createdAt": "2026-11-20T09:59:00Z", "type": "queue.authenticated-message-queued", "data": {"queueId": 1234, "from": "jane@acme.example", "to": ["a@example.com", "b@example.com"]}}]}
   ```

3. The `authenticate` auth hook takes the bearer as the calling node's Suite Cloud token (`suite_cloud/mail/cluster/server_token.py`). A gateway's token, an unknown key or a wrong secret is a 401.
4. The sender is lowercased and mapped to a Mail Account of the caller's cluster, directly or through an alias. Nodes make a signed-in sender use one of its own addresses. Another sender, a bounce and every other event type are skipped.
5. `SET NX` on `send_usage:queued:<node>:<queueId>` in the Redis cache, for 3 days, marks each message counted. A batch Stalwart sends again counts only its new messages.
6. The recipients are added to the account's row for the UTC day the event was created, in one statement. If the write fails, the marks are deleted and the request fails, so the retried batch counts them.

The answer is `{"data": {"counted": 2}}`.

## Mail Send Usage

One row per account and UTC day, unique on `account` and `date`. `sent` only grows. `send_limit` is the account's daily limit while the day was current. A month's usage, for an account or a tenant, is the sum of its days: Suite Cloud stores no month rows. Nothing counts refused recipients: Stalwart refuses them, and its events do not name the account.

The Mail Account form shows today's and this month's totals: orange from 80% of the daily limit, red at 100%. Suite Cloud keeps each account's days for **Sending Usage Retention (Days)** in Suite Cloud Settings (90 by default) and deletes older days daily, one day per transaction. A month the retention has partly passed sums only the days kept. In Insights, the **Sending Usage** dashboard of the Stalwart Mail workbook shows the totals. Its readings and limit tables show today, the UTC day; its **Date Range** filter, today by default, picks the days of the Sending by Day and Account Sending History charts.

## Accepted losses

| Case | Effect |
| --- | --- |
| Suite Cloud down for less than 24 hours | Stalwart keeps the events and sends them later. They count on the day they were queued |
| Suite Cloud down for more than 24 hours, or Stalwart restarts with events waiting | Those messages are sent but not counted |
| A message refused at DATA, such as by the outbound spam scan | Not queued, so not counted |
| A message queued and then bounced | Counted |
| The same address twice in one message | Counted once: Stalwart keeps one recipient |

## Moving from the RCPT hook

The patch `create_default_mail_send_limits` gives each cluster the default limits plus one Mail Send Limit for each value its accounts and tenant defaults hold, and links them, so no account's limit changes. An account with 0, and a tenant without a default, take the cluster's default. A cluster that already has a default keeps it. It also drops the `refused` column of Mail Send Usage. The old **Max Daily Send Limit** is gone; a cluster's **Submission Rate** starts at 90. The patch `move_send_usage_to_dates` gives each day row of Mail Send Usage its date, deletes the month rows, and drops the old `period` column and its unique key; the `sent` totals stay. Both patches write the rows directly, so no config push runs. After the deploy, run **Sync Config** on each cluster. Until then each cluster keeps the old hook, which calls an endpoint that no longer exists: Stalwart accepts every recipient, and only the old backstop caps the accounts.
