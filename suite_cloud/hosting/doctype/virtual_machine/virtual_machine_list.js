// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.listview_settings['Virtual Machine'] = {
	get_indicator(doc) {
		const colors = { Pending: 'orange', Provisioning: 'blue', Deploying: 'blue', Running: 'green', Paused: 'yellow', Stopped: 'gray', Failed: 'red', Terminated: 'gray' }
		return [__(doc.status), colors[doc.status] || 'gray', `status,=,${doc.status}`]
	},
}
