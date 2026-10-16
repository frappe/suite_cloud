# Signup

Module `suite_cloud.api.signup`. A visitor asks for a Frappe Suite workspace, proves their email address, and gets a hosted site. These methods need no credentials. They answer only while **Allow Signup** is checked in Suite Cloud Settings; otherwise they answer 403 with the type `SignupClosedError`.

Every method is a POST, whatever its name: the code and the request key travel in the JSON body, never in a URL. Each method is rate limited per IP address. Once the site is ready, it invites the owner from its own notification address. The invite is for an account at the tenant's mail domain, e.g. `john@acme.example.com` for `john@example.com`, and the owner chooses their password when they accept it. No password is ever mailed.

## `create_request`

`POST signup.create_request`

| Parameter | Type | Notes |
| --- | --- | --- |
| `email` | string | Receives the one-time code and becomes the owner's login |
| `full_name` | string | Required |
| `label` | string | Lowercase letters, digits and dashes. Names the site `<label>.<site zone>` and the mail domain `<label>.<tenant mail zone>` |
| `timezone` | string | Optional, e.g. `Asia/Kolkata`; the site's time zone |

Mails a six-digit code to `email` and answers `{"request": "<id>"}`. A label that is reserved, or that a site, an open signup, a cluster or a record in either zone already uses, answers 409 with the type `LabelNotAvailableError`. Five requests per hour per address.

## `verify_otp`

`POST signup.verify_otp`

| Parameter | Type | Notes |
| --- | --- | --- |
| `request` | string | From `create_request` |
| `otp` | string | The code from the email; valid for 10 minutes and 5 tries |

Starts creating the workspace and answers `{"request_key": "<key>"}`. Keep the key: it is the only way to read the status.

## `get_status`

`POST signup.get_status`

| Parameter | Type | Notes |
| --- | --- | --- |
| `request` | string | From `create_request` |
| `request_key` | string | From `verify_otp` |

```json
{"status": "Completed", "stage": "Active", "site_url": "https://acme.sites.example.com"}
```

`status` is `Pending`, `Verified`, `Processing`, `Completed`, `Failed` or `Expired`. `stage` is the status of the site while it is set up. `site_url` is set once the request is `Completed`; by then the owner's invite is on its way. An invite stays valid for 7 days. A wrong key answers 404.
