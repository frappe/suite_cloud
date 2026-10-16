// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Suite Tenant Request', {
	refresh(frm) {
		if (frm.doc.status === 'Completed') {
			// The same invite while it is valid, a fresh one once it expired.
			frm.add_custom_button(__('Resend Invite'), () =>
				frm.call('resend_invite').then(() => frappe.show_alert({ message: __('Invite sent.'), indicator: 'green' })),
			)
		}
		if (frm.doc.status === 'Failed') {
			frm.add_custom_button(__('Retry'), () => frm.call('retry').then(() => frm.reload_doc()))
		}
	},
})
