// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Stalwart Node', {
	refresh(frm) {
		if (frm.doc.__islocal) return

		frm.add_custom_button(__('Verify SSH'), () => frm.events.call(frm, 'verify_ssh', __('Connecting...')), __('Actions'))

		if (['Pending', 'Failed'].includes(frm.doc.status)) {
			frm.add_custom_button(__('Provision'), () => frm.events.call(frm, 'provision', __('Queueing provisioning...')), __('Actions'))
		}
		if (frm.doc.status === 'Provisioned') {
			frm.add_custom_button(__('Check Health'), () => frm.events.call(frm, 'check_health', __('Checking...')), __('Actions'))
		}
		if (frm.doc.status === 'Active') {
			frm.add_custom_button(__('Drain'), () => frm.events.call(frm, 'drain', __('Removing from ingress DNS...')), __('Actions'))
			frm.add_custom_button(__('Upgrade'), () => frm.events.call(frm, 'upgrade', __('Queueing upgrade...')), __('Actions'))
		}
		if (frm.doc.status === 'Draining') {
			frm.add_custom_button(__('Restore'), () => frm.events.call(frm, 'restore', __('Adding to ingress DNS...')), __('Actions'))
		}
		frm.add_custom_button(__('Verify PTR'), () => frm.events.call(frm, 'verify_ptr', __('Resolving...')), __('Actions'))

		if (!frm.doc.ssh_verified) {
			frm.dashboard.add_comment(
				__("Add the cluster's SSH public key to this VPS, then run Verify SSH."),
				'yellow',
				true,
			)
		}
	},

	call(frm, method, freeze_message) {
		frappe.call({
			doc: frm.doc,
			method,
			freeze: true,
			freeze_message,
			callback: (r) => {
				if (!r.exc) frm.reload_doc()
			},
		})
	},
})
