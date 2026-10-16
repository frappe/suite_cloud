// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Suite Tenant Request', {
	refresh(frm) {
		if (frm.doc.status === 'Completed' && frm.doc.error) {
			frm.add_custom_button(__('Resend Login Details'), () => frm.call('resend_login_details').then(() => frm.reload_doc()))
		}
		if (frm.doc.status === 'Failed') {
			frm.add_custom_button(__('Retry'), () => frm.call('retry').then(() => frm.reload_doc()))
		}
	},
})
