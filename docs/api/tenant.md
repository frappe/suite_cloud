# Tenant

Module `suite_cloud.api.tenant`. The conventions are in the [overview](README.md).

## `ping`

`GET tenant.ping`

Confirms the credentials and returns the site profile, including where its mail lives.

```json
{
  "site": "acme.frappe.cloud",
  "cluster": "c1.frappemail.com",
  "status": "Active",
  "title": "Acme",
  "contact_email": "it@acme.example",
  "enabled": true,
  "allowed_ips": ["203.0.113.10/32"],
  "jmap_url": "https://c1.frappemail.com",
  "mail_hostname": "c1.frappemail.com",
  "limits": {
    "max_domains": 5,
    "max_accounts": 100,
    "max_groups": 20,
    "max_mailing_lists": 20,
    "max_disk_gb": 500
  },
  "usage": {
    "domains": 2,
    "accounts": 41,
    "groups": 3,
    "mailing_lists": 1,
    "used_disk_bytes": 734003200
  }
}
```

`status` is `Active`, `Suspended` or `Archived`. A limit of `null` means no limit. `max_disk_gb` is the disk the site's plan includes; it does not cap the quotas of the site's accounts and groups. `used_disk_bytes` is the figure [`get_disk_usage`](#get_disk_usage) answers, or `null` when the mail server cannot answer.

## `get_disk_usage`

`GET tenant.get_disk_usage`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `fresh` | boolean | `false` | Asks the mail server now instead of answering from the cache |

Answers the disk the site's accounts and groups use together, as the mail server reports it:

```json
{
  "used_disk_bytes": 734003200,
  "fetched_at": "2026-03-04T05:06:07Z"
}
```

Suite Cloud asks the mail server for every account and group of the site in as few calls as the server allows, then keeps the sum for 5 minutes. A call inside that time answers the kept figure and `fetched_at` says when it was read. `fresh` reads the mail server again and replaces the kept figure. When the mail server cannot answer, the call fails with a 503 and nothing is kept, so a partial sum is never served.

## `update_tenant_profile`

`PATCH tenant.update_tenant_profile`

| Parameter | Type | Meaning |
| --- | --- | --- |
| `title` | string | The site's workspace name, shown to operators. An empty string resets it to the site name. |
| `contact_email` | string | Where Suite Cloud sends site-specific notices. An empty string clears it. |

Only the fields passed change. Returns the site profile as `ping` does. The Suite app sends both fields whenever its settings change.
