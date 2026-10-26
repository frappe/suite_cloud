// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports['Mail Sending Usage'] = {
	filters: [
		{ fieldname: 'cluster', label: __('Cluster'), fieldtype: 'Link', options: 'Stalwart Cluster' },
		{ fieldname: 'tenant', label: __('Tenant'), fieldtype: 'Link', options: 'Suite Tenant' },
		{
			fieldname: 'near_limit',
			label: __('Near a Limit'),
			fieldtype: 'Check',
			default: 1,
			description: __('Only accounts at 80% of a limit or more, or with refused messages'),
		},
	],

	formatter(value, row, column, data, default_formatter) {
		const formatted = default_formatter(value, row, column, data)
		if (!data || !column.fieldname.endsWith('_percent') || value == null) return formatted
		const color = value >= 100 ? 'red' : value >= 80 ? 'orange' : null
		return color ? `<span style="color: var(--${color}-600)">${formatted}</span>` : formatted
	},
}
