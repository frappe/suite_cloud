// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings['Suite Site'] = {
	get_indicator(doc) {
		const colors = { Pending: 'orange', Creating: 'blue', Ready: 'cyan', Assigned: 'blue', Configuring: 'blue', Active: 'green', Failed: 'red', Dropping: 'orange', Dropped: 'gray' }
		return [__(doc.status), colors[doc.status] || 'gray', `status,=,${doc.status}`]
	},
}
