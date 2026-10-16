// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Suite Cloud Settings', {
	refresh(frm) {
		frm.add_custom_button(__('Test Central Connection'), () => test_central_connection(frm), __('Frappe Cloud'))
		frm.add_custom_button(
			__('Sync Regions'),
			() =>
				frm.call({ method: 'sync_regions', freeze: true, freeze_message: __('Reading regions...') }).then((r) =>
					frappe.show_alert({ message: __('{0} regions synced.', [r.message]), indicator: 'green' }),
				),
			__('Frappe Cloud'),
		)
	},
})

function test_central_connection(frm) {
	frm.call({ method: 'test_central_connection', freeze: true, freeze_message: __('Connecting...') }).then((r) =>
		frappe.msgprint({
			title: __('Central connection works'),
			indicator: 'green',
			message: `<pre>${frappe.utils.escape_html(JSON.stringify(r.message, null, 2))}</pre>`,
		}),
	)
}
