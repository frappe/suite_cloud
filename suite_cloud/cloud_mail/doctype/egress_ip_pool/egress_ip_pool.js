// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Egress IP Pool', {
	setup(frm) {
		frm.set_query('gateway', 'addresses', () => ({ filters: { cluster: frm.doc.cluster } }))
	},

	refresh(frm) {
		if (frm.doc.__islocal) return
		frm.add_custom_button(__('Verify PTR'), () => frm.events.verify_ptr(frm), __('Actions'))
	},

	verify_ptr(frm, address) {
		if (frm.is_dirty()) {
			frappe.msgprint(__('Save the pool before verifying reverse DNS.'))
			return
		}
		frappe.call({
			doc: frm.doc,
			method: 'verify_ptr',
			args: { address },
			freeze: true,
			freeze_message: __('Resolving...'),
			callback: (r) => {
				const failed = Object.entries(r.message || {}).filter(([, ok]) => !ok).map(([ip]) => ip)
				if (failed.length) {
					frappe.msgprint(__('Reverse DNS does not match for: {0}', [failed.join(', ')]))
				} else {
					frappe.show_alert({ message: __('Reverse DNS verified'), indicator: 'green' })
				}
				frm.reload_doc()
			},
		})
	},
})

frappe.ui.form.on('Egress IP Pool Address', {
	verify_ptr(frm, cdt, cdn) {
		frm.events.verify_ptr(frm, cdn)
	},
})
