// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Suite Tenant Request', {
	setup(frm) {
		// Once per form: the steps' jobs report each change, and the reload redraws the table.
		frappe.realtime.on('suite_tenant_request_progress', (data) => {
			if (data.request === frm.doc.name) frm.reload_doc()
		})
	},
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
		frm.events.show_progress(frm)
	},
	show_progress(frm) {
		const steps = frm.doc.steps || []
		const done = steps.filter((step) => ['Completed', 'Skipped'].includes(step.status)).length
		const current = steps.find((step) => ['Running', 'Failed'].includes(step.status))
		if (!steps.length || done === steps.length || !current) return
		const message = __('Step {0} of {1}: {2}', [current.idx, steps.length, __(current.title)])
		frm.dashboard.show_progress(__('Progress'), (done / steps.length) * 100, message)
	},
})
