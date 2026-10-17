// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings['Suite Tenant Request'] = {
	get_indicator(doc) {
		const colors = { Pending: 'orange', Verified: 'blue', Processing: 'blue', Completed: 'green', Failed: 'red', Expired: 'gray' }
		return [__(doc.status), colors[doc.status] || 'gray', `status,=,${doc.status}`]
	},
}
