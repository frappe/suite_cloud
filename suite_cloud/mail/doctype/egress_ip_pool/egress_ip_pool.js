// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Egress IP Pool', {
	setup(frm) {
		frm.set_query('gateway', 'addresses', () => ({ filters: { cluster: frm.doc.cluster } }))
	},

	refresh(frm) {
		if (frm.doc.__islocal) return
		frm.add_custom_button(__('Verify PTR'), () => frm.events.verify_ptr(frm), __('Actions'))
		frm.trigger('warn_unverified_ptr')
	},

	warn_unverified_ptr(frm) {
		const unverified = (frm.doc.addresses || []).filter((row) => !row.ptr_verified)
		const ips = (rows) => rows.map((row) => frappe.utils.escape_html(row.ip_address)).join(', ')
		const blocked = unverified.filter((row) => !row.allow_without_ptr)
		const allowed = unverified.filter((row) => row.allow_without_ptr)
		if (blocked.length) {
			frm.dashboard.add_comment(
				__('Reverse DNS is not verified for {0}. The gateway does not send from them until Verify PTR passes or Allow Without PTR is set.', [ips(blocked)]),
				'orange',
				true,
			)
		}
		if (allowed.length) {
			frm.dashboard.add_comment(
				__('Reverse DNS is not verified for {0}, but Allow Without PTR is set, so the gateway sends from them.', [ips(allowed)]),
				'yellow',
				true,
			)
		}
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
