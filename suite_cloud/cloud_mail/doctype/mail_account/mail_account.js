// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Mail Account', {
	refresh(frm) {
		if (frm.doc.__islocal) return
		frm.add_custom_button(__('Reset Password'), () => frm.trigger('reset_password'), __('Credentials'))
		frm.add_custom_button(__('Rotate App Password'), () => frm.trigger('rotate_app_password'), __('Credentials'))
		if (frm.doc.app_password) {
			frm.add_custom_button(__('Show App Password'), () => frm.trigger('show_app_password'), __('Credentials'))
		}
		frm.add_custom_button(
			frm.doc.api_key ? __('Rotate API Key') : __('Create API Key'),
			() => frm.trigger('rotate_api_key'),
			__('Credentials'),
		)
		if (frm.doc.api_key) {
			frm.add_custom_button(__('Show API Key'), () => frm.trigger('show_api_key'), __('Credentials'))
		}
	},

	reset_password(frm) {
		const dialog = new frappe.ui.Dialog({
			title: __('Reset Password'),
			fields: [
				{
					fieldname: 'password',
					fieldtype: 'Password',
					label: __('New Password'),
					description: __('Leave blank to generate one. At least 8 characters.'),
				},
			],
			primary_action_label: __('Reset'),
			primary_action: ({ password }) => {
				dialog.hide()
				frm.events.call(frm, 'reset_password', { password }, __('Updating the cluster...'), (result) => {
					frm.events.reveal(__('New Password'), result, __('Shown once; hand it to the user now.'))
				})
			},
		})
		dialog.show()
	},

	rotate_app_password(frm) {
		frappe.confirm(__('Mint a new app password for this account and revoke the current one? The Suite site must be given the new one.'), () => {
			frm.events.call(frm, 'rotate_app_password', {}, __('Rotating...'), (secret) => {
				frm.events.reveal(__('App Password'), secret, __('The previous app password no longer works.'))
			})
		})
	},

	show_app_password(frm) {
		frm.events.call(frm, 'show_app_password', {}, __('Fetching...'), (secret) => frm.events.reveal(__('App Password'), secret))
	},

	rotate_api_key(frm) {
		const question = frm.doc.api_key ? __('Mint a new API key for this account and revoke the current one?') : __('Create an API key for this account?')
		frappe.confirm(question, () => {
			frm.events.call(frm, 'rotate_api_key', {}, __('Rotating...'), (key) => {
				frm.events.reveal(__('API Key'), key, __('The previous key no longer works.'))
			})
		})
	},

	show_api_key(frm) {
		frm.events.call(frm, 'show_api_key', {}, __('Fetching...'), (key) => frm.events.reveal(__('API Key'), key))
	},

	reveal(title, secret, note) {
		frappe.msgprint({
			title,
			message: `<code>${frappe.utils.escape_html(secret)}</code>${note ? `<p class="mt-2 text-muted">${note}</p>` : ''}`,
		})
	},

	call(frm, method, args, freeze_message, done) {
		frappe.call({
			doc: frm.doc,
			method,
			args,
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
