// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Suite Site', {
	setup(frm) {
		frm.set_query('egress_pool', () => ({ filters: { cluster: frm.doc.cluster || '' } }))
	},

	cluster(frm) {
		// A pool belongs to one cluster; a picked one is stale once the cluster changes.
		frm.set_value('egress_pool', null)
	},
})
