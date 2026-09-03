// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Egress Gateway', {
	refresh(frm) {
		if (frm.doc.__islocal) return
		frm.add_custom_button(__('Verify SSH'), () => frm.events.call(frm, 'verify_ssh', __('Connecting...')), __('Actions'))
		frm.add_custom_button(__('Preview Plan'), () => frm.events.call(frm, 'preview_plan', __('Rendering...'), (plan) => {
			frappe.msgprint({ title: __('Gateway Plan'), message: `<pre>${frappe.utils.escape_html(plan)}</pre>`, wide: true })
		}), __('Actions'))
		if (['Pending', 'Failed'].includes(frm.doc.status)) {
			frm.add_custom_button(__('Provision'), () => frm.events.call(frm, 'provision', __('Queueing...')), __('Actions'))
		}
		if (frm.doc.status === 'Provisioned') {
			frm.add_custom_button(__('Check Health'), () => frm.events.call(frm, 'check_health', __('Checking...')), __('Actions'))
		}
		if (frm.doc.status === 'Active') {
			frm.add_custom_button(__('Sync Config'), () => frm.events.call(frm, 'sync_config', __('Syncing...')), __('Actions'))
			frm.add_custom_button(__('Upgrade'), () => frm.events.call(frm, 'upgrade', __('Queueing...')), __('Actions'))
		}
	},

	call(frm, method, freeze_message, done) {
		frappe.call({
			doc: frm.doc,
			method,
			freeze: true,
			freeze_message,
			callback: (r) => {
				if (r.exc) return
				frm.reload_doc()
				if (done) done(r.message)
			},
		})
	},
})
