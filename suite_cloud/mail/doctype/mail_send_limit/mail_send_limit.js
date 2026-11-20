// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Mail Send Limit', {
	refresh(frm) {
		if (frm.is_new()) return
		frm.add_custom_button(__('Sync Throttle'), () =>
			frm.call('sync_throttle').then(({ message }) => {
				const text = message ? __('Throttle created on the cluster') : __('The cluster has the throttle')
				frappe.show_alert({ message: text, indicator: 'green' })
			}),
		)
	},
})
