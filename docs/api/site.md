# Site

Module `suite_cloud.api.site`. The conventions are in the [overview](README.md).

## `ping`

`GET site.ping`

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
    "max_disk_gb": 500,
    "default_disk_quota_gb": 5
  },
  "usage": {
    "domains": 2,
    "accounts": 41,
    "groups": 3,
    "mailing_lists": 1,
    "allocated_disk_gb": 205
  }
}
```

`status` is `Active`, `Suspended` or `Archived`. A limit of `null` means no limit. `allocated_disk_gb` is the sum of the disk quotas of the site's accounts and groups, which the site's `max_disk_gb` bounds.

## `update_site_profile`

`PATCH site.update_site_profile`

| Parameter | Type | Meaning |
| --- | --- | --- |
| `title` | string | The site's workspace name, shown to operators. An empty string resets it to the site name. |
| `contact_email` | string | Where Suite Cloud sends site-specific notices. An empty string clears it. |

Only the fields passed change. Returns the site profile as `ping` does. The Suite app sends both fields whenever its settings change.
