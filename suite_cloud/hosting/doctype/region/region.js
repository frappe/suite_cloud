// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Region', {
	refresh(frm) {
		if (frm.doc.enabled && !frm.doc.proxy_zone) {
			frm.dashboard.add_comment(__('The proxy zone is learned from the first server set up in this region.'), 'blue', true)
		}
	},
})
