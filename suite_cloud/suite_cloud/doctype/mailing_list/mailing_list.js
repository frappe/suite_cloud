// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Mailing List', {
	refresh(frm) {
		if (frm.doc.__islocal) return
		frappe.db.count('Mailing List Recipient', { filters: { mailing_list: frm.doc.name } }).then((count) => {
			frm.dashboard.add_comment(__('{0} recipients. Recipients are separate documents so lists can grow large; use the Recipients button to browse them.', [count]), 'blue', true)
		})
		frm.add_custom_button(__('Recipients'), () => frappe.set_route('List', 'Mailing List Recipient', { mailing_list: frm.doc.name }))
		frm.add_custom_button(__('Add Recipients'), () => frm.trigger('add_recipients'))
	},

	add_recipients(frm) {
		const dialog = new frappe.ui.Dialog({
			title: __('Add Recipients'),
			fields: [{ fieldname: 'text', fieldtype: 'Small Text', label: __('Addresses'), description: __('One per line, or comma-separated. Addresses already on the list are skipped.'), reqd: 1 }],
			primary_action_label: __('Add'),
			primary_action: ({ text }) => {
				dialog.hide()
				frappe.call({
					doc: frm.doc,
					method: 'add_recipients_from_text',
					args: { text },
					freeze: true,
					freeze_message: __('Adding recipients...'),
					callback: (r) => {
						if (r.exc) return
						frappe.show_alert({ message: __('{0} recipients added', [r.message]), indicator: 'green' })
						frm.reload_doc()
					},
				})
			},
		})
		dialog.show()
	},
})
