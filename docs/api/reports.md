# Reports

Modules `suite_cloud.api.mail.dmarc` and `suite_cloud.api.mail.tls`. The conventions are in the [overview](README.md). The cluster receives DMARC aggregate reports and SMTP TLS reports for the site's domains; Suite Cloud copies them hourly and drops them after their retention. The site sees the reports of its own domains.

| Method | Verb | Purpose |
| --- | --- | --- |
| `mail.dmarc.list_dmarc_reports` | GET | A page of DMARC reports, newest period first |
| `mail.dmarc.get_dmarc_report` | GET | One report with its per-source records |
| `mail.dmarc.get_dmarc_summary` | GET | Message totals by domain, source IP and reporter |
| `mail.tls.list_tls_reports` | GET | A page of TLS reports, newest period first |
| `mail.tls.get_tls_report` | GET | One report with its policies and failure details |
| `mail.tls.get_tls_summary` | GET | Session totals by domain and reporter, failures by result type |

## Listing parameters

Both listings take the same parameters.

| Parameter | Type | Default | Meaning |
| --- | --- | --- | --- |
| `domain` | string | | Only reports about this domain of the site |
| `search` | string | | Matches the domain, the reporter or the report id |
| `since` | datetime | | Only reports whose period ended at or after this moment |
| `until` | datetime | | Only reports whose period began at or before this moment |
| `days` | integer | | Only reports whose period ended within the last so many days; 0 or unset lists everything held |
| `start` | integer | 0 | Offset |
| `limit` | integer | 50 | Page length, at most 500 |

A datetime is anything Frappe's `get_datetime` reads, such as `2026-03-01` or `2026-03-01 00:00:00`; anything else is a 400. A report that counted no messages or sessions says nothing about the domain and is left out of the listings and summaries; `get_dmarc_report` and `get_tls_report` still answer for it by name.

Both summaries take `domain` (optional) and `days` (default 30; 0 counts everything held) and answer `since` and `until` as the window they counted.

## Report envelope

Every report carries these fields:

```json
{
  "name": "0192b6d2-3c4e-7a8f-9b0c-1d2e3f4a5b6c",
  "policy_domain": "acme.example",
  "reporter": "google.com",
  "reporter_email": "noreply-dmarc-support@google.com",
  "report_id": "1234567890",
  "subject": "Report domain: acme.example Submitter: google.com",
  "to": ["postmaster@acme.example"],
  "date_range_begin": "2026-03-03T00:00:00+00:00",
  "date_range_end": "2026-03-04T00:00:00+00:00",
  "received_at": "2026-03-04T02:10:00+00:00"
}
```

## DMARC

A DMARC report adds the published policy and the message totals. A message passes when its aligned DKIM or its aligned SPF check passes.

```json
{
  "version": 1,
  "policy": { "p": "reject", "sp": "reject", "testing_mode": false, "adkim": "r", "aspf": "r" },
  "totals": { "messages": 120, "passed": 118, "failed": 2, "dkim_passed": 117, "spf_passed": 110 },
  "errors": null,
  "records": [
    {
      "source_ip": "198.51.100.7",
      "count": 2,
      "disposition": "reject",
      "dkim": "fail",
      "spf": "fail",
      "header_from": "acme.example",
      "envelope_from": "acme.example",
      "envelope_to": "example.net",
      "override_reasons": null,
      "dkim_results": [ { "…": "…" } ],
      "spf_results": [ { "…": "…" } ]
    }
  ]
}
```

`records` appears only on `get_dmarc_report`. `disposition`, `dkim` and `spf` are the evaluated results in lowercase; `dkim_results` and `spf_results` are the per-check results as the reporter sent them. A report's `name` is a UUID.

### `get_dmarc_summary`

```json
{
  "since": "2026-02-02T05:06:07+00:00",
  "until": "2026-03-04T05:06:07+00:00",
  "totals": { "reports": 31, "messages": 4200, "passed": 4150, "failed": 50, "dkim_passed": 4100, "spf_passed": 3900 },
  "domains": [ { "domain": "acme.example", "reports": 31, "messages": 4200, "passed": 4150, "failed": 50, "dkim_passed": 4100, "spf_passed": 3900 } ],
  "sources": [ { "source_ip": "203.0.113.5", "reports": 30, "messages": 4000, "passed": 4000, "failed": 0, "dkim_passed": 4000, "spf_passed": 3900 } ],
  "reporters": [ { "reporter": "google.com", "reports": 20, "messages": 3000, "passed": 2980, "failed": 20, "dkim_passed": 2950, "spf_passed": 2800 } ]
}
```

`sources` lists the 20 source addresses with the most messages. `since` is `null` when `days` is 0.

## TLS

A TLS report adds the reporter's contact, the policy types it covers and the session totals.

```json
{
  "contact_info": "smtp-tls-reporting@google.com",
  "policy_types": ["sts", "tlsa"],
  "totals": { "sessions": 500, "successful": 498, "failed": 2 },
  "policies": [
    { "policy_type": "sts", "policy_domain": "acme.example", "mx_hosts": ["c1.frappemail.com"], "policy_strings": ["version: STSv1", "mode: enforce"], "successful": 498, "failed": 2 }
  ],
  "failures": [
    {
      "result_type": "certificate-expired",
      "count": 2,
      "policy_type": "sts",
      "policy_domain": "acme.example",
      "sending_mta_ip": "198.51.100.9",
      "receiving_mx_hostname": "c1.frappemail.com",
      "receiving_mx_helo": null,
      "receiving_ip": "203.0.113.20",
      "failure_reason_code": null,
      "additional_information": null
    }
  ]
}
```

`policies` and `failures` appear only on `get_tls_report`. `policy_type` and `result_type` use the names of RFC 8460.

### `get_tls_summary`

```json
{
  "since": "2026-02-02T05:06:07+00:00",
  "until": "2026-03-04T05:06:07+00:00",
  "totals": { "reports": 28, "sessions": 14000, "successful": 13990, "failed": 10 },
  "domains": [ { "domain": "acme.example", "reports": 28, "sessions": 14000, "successful": 13990, "failed": 10 } ],
  "reporters": [ { "reporter": "google.com", "reports": 20, "sessions": 10000, "successful": 9995, "failed": 5 } ],
  "failures": [ { "result_type": "certificate-expired", "reports": 2, "failed": 10 } ]
}
```

`failures` lists the result types with the most failed sessions.
