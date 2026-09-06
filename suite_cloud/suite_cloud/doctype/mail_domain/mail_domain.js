// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Mail Domain', {
	setup(frm) {
		frm.set_query('egress_pool', () => ({ filters: { cluster: frm.doc.cluster || '' } }))
	},

	site(frm) {
		// The cluster follows the site; a pool picked for the old cluster no longer fits.
		frm.set_value('egress_pool', null)
	},

	refresh(frm) {
		if (frm.doc.__islocal) return
		frm.add_custom_button(__('Refresh DNS Records'), () => frm.events.call(frm, 'refresh_dns_records', __('Reading zone...')))
		frm.add_custom_button(__('Verify DNS Records'), () => frm.events.call(frm, 'verify_dns_records', __('Resolving...')))
		if (!frm.doc.is_verified) {
			frm.dashboard.add_comment(__('Publish the mandatory records at the domain\'s DNS provider, then verify.'), 'yellow', true)
		}
	},

	call(frm, method, freeze_message) {
		frappe.call({ doc: frm.doc, method, freeze: true, freeze_message, callback: () => frm.reload_doc() })
	},
})
