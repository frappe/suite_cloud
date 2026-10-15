# Groups

Module `suite_cloud.api.mail.groups`. The conventions are in the [overview](README.md). A group is an address that delivers to every member account; the cluster holds it as a group object with its own quota.

| Method | Verb | Purpose |
| --- | --- | --- |
| `mail.groups.list_groups` | GET | A page of groups |
| `mail.groups.get_group` | GET | One group with its disk usage |
| `mail.groups.create_group` | POST | Creates a group |
| `mail.groups.update_group` | PATCH | Changes the description, quotas or receiving |
| `mail.groups.set_aliases` | PUT | Replaces the alias set |
| `mail.groups.add_alias` | POST | Adds one alias |
| `mail.groups.remove_alias` | DELETE | Removes one alias |
| `mail.groups.set_alias_enabled` | PUT | Turns one alias on or off |
| `mail.groups.set_members` | PUT | Replaces the member set |
| `mail.groups.delete_group` | DELETE | Removes the group |

## The group object

```json
{
  "email": "sales@acme.example",
  "domain": "acme.example",
  "description": "Sales team",
  "disable_receiving": false,
  "disk_quota_gb": 5,
  "quotas": { "maxDiskQuota": 5368709120 },
  "used_disk_bytes": 0,
  "aliases": [],
  "members": ["jane@acme.example", "john@acme.example"],
  "created_at": "2026-02-01T09:00:00+00:00"
}
```

`members` lists the site's account addresses that belong to the group, sorted. `used_disk_bytes`, `quotas`, `disk_quota_gb` and `aliases` work as they do for [accounts](accounts.md#the-account-object). Membership lives on the accounts: `set_members` here and `set_groups` on an account change the same thing from either side.

## `list_groups`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `search` | string | | Matches the address or description |
| `start` | integer | 0 | Offset |
| `limit` | integer | 100 | Page length, at most 500 |

Answers `{"items": [group, …], "total": n}` ordered by address.

## `get_group`

Takes `email`. Returns the group object with its usage.

## `create_group`

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `email` | string, required | | On an active domain of the site |
| `description` | string | | |
| `aliases` | array | | As for [accounts](accounts.md#aliases) |
| `members` | array of strings | | Account addresses of the site, at most 500 |
| `disk_quota_gb` | number | no limit | As for [accounts](accounts.md#quotas) |
| `quotas` | object | | As for accounts |
| `disable_receiving` | boolean | `false` | Mail sent to the group's address bounces; the members' own mail is not affected |

Answers 201 with the group object. An address that is not one of the site's accounts is a 404 and nothing is created; a taken address is a 409; an inactive domain or the site's group limit is a 422.

## `update_group`

Takes `email` and any of `description`, `disk_quota_gb`, `quotas` and `disable_receiving`. Only the fields passed change. Returns the group object.

## `set_aliases`, `add_alias`, `remove_alias`, `set_alias_enabled`

The same parameters and rules as for [accounts](accounts.md#set_aliases-add_alias-remove_alias-set_alias_enabled). Each returns the group object.

## `set_members`

Takes `email` and `members`, an array of the site's account addresses (at most 500; empty or omitted removes every member). Only the accounts whose membership changes are touched, each saved on its own. An unknown address is a 404 and nothing changes. Returns the group object.

## `delete_group`

Takes `email`. Answers 204. The member accounts are not affected.
