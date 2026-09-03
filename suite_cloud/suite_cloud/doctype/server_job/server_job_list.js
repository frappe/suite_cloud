frappe.listview_settings['Server Job'] = {
	get_indicator(doc) {
		const colors = { Pending: 'orange', Running: 'blue', Success: 'green', Failed: 'red' }
		return [__(doc.status), colors[doc.status] || 'gray', `status,=,${doc.status}`]
	},
}
