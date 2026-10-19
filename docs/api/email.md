# Email

Module `suite_cloud.api.email`. The conventions are in the [overview](README.md).

A site connected to Suite Cloud does not send mail itself. Invites, password resets, shares and all other mail of the site go through Suite Cloud, which sends each email immediately and answers whether it was sent. Nothing is queued on Suite Cloud.

Suite Cloud sends through the outgoing Email Account that **Suite Cloud Settings > Tenant Email** sets for the tenant's type (Suite Site, Team or Development). The sender name is the Email Account's name, or the tenant's title when **Use Tenant Title as Sender Name** is checked. The site's own From, Sender and Reply-To headers are removed. A Reply-To is set only when the Email Account adds one. Each email is recorded as a **Tenant Email**, and the emails sent this month show as `emails_sent` in the [tenant profile](tenant.md#ping).

## `send_email`

`POST email.send_email`

| Parameter | Type | Notes |
| --- | --- | --- |
| `recipient` | string | Required. One email address |
| `message` | string | Required. The whole email in MIME format, at most 25 MB |
| `request_key` | string | Required. The site's key for this email, e.g. `<email queue>:<recipient>` |

The `X-Suite-Email-Type` header of the message names what the email is, e.g. `password-reset` or `account-invite`. Without it the type is `other`. Suite Cloud removes the header before it sends the email.

```json
{
  "name": "0199e1a2-3b4c-7d5e-8f60-123456789abc",
  "status": "Sent",
  "message_id": "invite-1@acme.example.com",
  "error": null
}
```

`status` is `Sent` or `Failed`. When the mail server refuses the email or cannot be reached, the call still answers 200, with `status` `Failed` and the server's reply in `error`, e.g. `550 No such user`. The site then shows the error or tries again.

A `request_key` that was sent before answers the earlier result and does not send the email again. A key that failed before is sent again. So the site can safely retry a request whose answer was lost.

| Error | Status | When |
| --- | --- | --- |
| `InvalidInputError` | 400 | `recipient` is not one email address, or `request_key` is blank |
| `EmailNotConfiguredError` | 422 | No outgoing Email Account is set for the tenant's type |
| `ValidationError` | 422 | The message is larger than 25 MB |
