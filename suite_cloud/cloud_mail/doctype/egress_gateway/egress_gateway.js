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
		if (['Provisioned', 'Active'].includes(frm.doc.status)) {
			frm.add_custom_button(
				__('Re-provision'),
				() =>
					frappe.confirm(__('Run the provisioning playbook again? Stalwart restarts on the gateway.'), () =>
						frm.events.call(frm, 'provision', __('Queueing...')),
					),
				__('Actions'),
			)
		}
		if (frm.doc.status === 'Provisioned') {
			frm.add_custom_button(__('Check Health'), () => frm.events.call(frm, 'check_health', __('Checking...')), __('Actions'))
		}
		if (frm.doc.status === 'Active') {
			frm.add_custom_button(__('Sync Config'), () => frm.events.call(frm, 'sync_config', __('Syncing...')), __('Actions'))
			frm.add_custom_button(__('Upgrade'), () => frm.events.call(frm, 'upgrade', __('Queueing...')), __('Actions'))
		}
		if (frappe.session.user === 'Administrator') {
			frm.add_custom_button(__('Show Admin Password'), () => frm.trigger('show_admin_password'), __('Access'))
		}
	},

	show_admin_password(frm) {
		frappe.call({
			doc: frm.doc,
			method: 'show_admin_password',
			freeze: true,
			freeze_message: __('Fetching...'),
			callback: (r) => {
				if (r.exc) return
				frappe.msgprint({
					title: __('Admin Password'),
					message: `${__('Web admin')}: <a href="${frm.doc.base_url}" target="_blank">${frm.doc.base_url}</a><br>${__('Username')}: <code>${frappe.utils.escape_html(frm.doc.admin_username)}</code><br>${__('Password')}: <code>${frappe.utils.escape_html(r.message)}</code>`,
				})
			},
		})
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
