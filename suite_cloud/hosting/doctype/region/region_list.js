// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings['Region'] = {
	get_indicator(doc) {
		const colors = { Active: 'green' }
		return [__(doc.status), colors[doc.status] || 'gray', `status,=,${doc.status}`]
	},
}
