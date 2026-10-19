# Email

Module `suite_cloud.api.email`. The conventions are in the [overview](README.md).

Suite Cloud sends the emails Suite itself sends, such as invites, password resets and share notices. The site names the email and gives its parameters; Suite Cloud renders it from its own template and sends it immediately. It answers whether the email was sent. Nothing is queued on Suite Cloud. All other mail of the site, e.g. newsletters and Frappe's own notifications, goes through the site's own Email Account, which the site owner sets up.

Suite Cloud sends through the outgoing Email Account that **Suite Cloud Settings > Tenant Email** sets for the tenant's type (Suite Site, Team or Development). The sender name is the Email Account's name, or the tenant's title when **Use Tenant Title as Sender Name** is checked. The Reply-To header follows the Email Account's own settings. Each email is recorded as a **Tenant Email**, and the emails sent this month show as `emails_sent` in the [tenant profile](tenant.md#ping).

## `send_email`

`POST email.send_email`

| Parameter | Type | Notes |
| --- | --- | --- |
| `email_type` | string | Required. One of the types below |
| `recipient` | string | Required. One email address |
| `params` | object | Required. The type's parameters: text, numbers or booleans, at most 2000 characters each |
| `request_key` | string | Required. The site's key for this email |

| Type | Required parameters | Optional parameters |
| --- | --- | --- |
| `password-reset` | `link` | |
| `account-invite` | `inviter`, `link` | |
| `signup-code` | `code`, `expires_in_minutes` | |
| `drive-signup-code` | `code` | |
| `drive-invite` | `inviter`, `link` | `team_name` |
| `drive-share` | `sharer`, `item_type`, `item_name`, `link` | |
| `writer-mention` | `author`, `document`, `link` | |
| `sheet-share` | `sharer`, `sheet`, `access`, `link` | |
| `calendar-rsvp` | `title`, `responder_name`, `responder_email`, `response`, `when`, `organizer_name`, `organizer_email` | `timezone`, `location` |
| `data-exchange` | `app`, `action`, `succeeded`, `link` | |
| `meet-recording` | `room`, `recorded_at`, `status` (`Ready`, `Partial` or `Failed`) | `link` |

A `link` must be a web address. When Suite Cloud hosts the tenant's site, it must also lead to that site: its host name or one of its domains. The templates escape every parameter, so a parameter cannot add markup to the email.

```json
{
  "name": "0199e1a2-3b4c-7d5e-8f60-123456789abc",
  "status": "Sent",
  "message_id": "176087.12.345@cloud.example.com",
  "sent_at": "2026-10-19 12:00:00",
  "error": null
}
```

`status` is `Sent` or `Failed`. When the mail server refuses the email or cannot be reached, the call still answers 200, with `status` `Failed` and the server's reply in `error`, e.g. `550 No such user`. The site then shows the error or tries again.

A `request_key` that was sent before answers the earlier result and does not send the email again. A key that failed before is sent again. A request that arrives while another request with the same key is sending waits for it, then answers its result. So the site can safely retry a request whose answer was lost.

| Error | Status | When |
| --- | --- | --- |
| `InvalidInputError` | 400 | `recipient` is not one email address, or `request_key` is blank |
| `InvalidEmailError` | 400 | An unknown `email_type`, a missing or unknown parameter, or a link that leads elsewhere |
| `EmailNotConfiguredError` | 422 | No outgoing Email Account is set for the tenant's type |
