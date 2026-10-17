// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Virtual Machine', {
	refresh(frm) {
		if (frm.is_new()) return
		const action = (label, method, message, confirm) =>
			frm.add_custom_button(
				label,
				() => (confirm ? frappe.confirm(confirm, () => frm.events.call(frm, method, message)) : frm.events.call(frm, method, message)),
				__('Actions'),
			)

		action(__('Sync Status'), 'sync_status', __('Reading Central...'))
		if (frm.doc.error || frm.doc.status === 'Failed') action(__('Retry'), 'retry', __('Retrying...'))
		if (frm.doc.status === 'Running') {
			action(__('Verify Host'), 'verify_host', __('Asking Pilot...'))
			action(__('Install Suite App'), 'install_suite_app', __('Queueing...'))
			frm.add_custom_button(__('Open Pilot'), () => frm.call('open_pilot').then((r) => window.open(r.message, '_blank')), __('Actions'))
		}
		if (frm.doc.enabled) {
			action(__('Decommission'), 'decommission', __('Saving...'), __('Stop placing new sites on {0}? Its sites stay.', [frm.doc.title]))
		}
		if (frm.doc.central_resource_id && frm.doc.status !== 'Terminated') {
			action(__('Terminate'), 'terminate', __('Asking Central...'), __('Terminate {0} on Central? This cannot be undone.', [frm.doc.title]))
		}
		if (frm.doc.error) frm.dashboard.add_comment(frappe.utils.escape_html(frm.doc.error), 'red', true)
	},

	call(frm, method, freeze_message) {
		frappe.call({ doc: frm.doc, method, freeze: true, freeze_message, callback: (r) => !r.exc && frm.reload_doc() })
	},
})
