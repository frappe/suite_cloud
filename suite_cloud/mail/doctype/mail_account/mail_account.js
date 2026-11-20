// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Mail Account', {
	setup(frm) {
		frm.set_query('send_limit', () => ({ filters: { cluster: frm.doc.cluster || '' } }))
	},

	refresh(frm) {
		if (frm.doc.__islocal) return
		frm.add_custom_button(__('Reset Password'), () => frm.trigger('reset_password'), __('Credentials'))
		frm.add_custom_button(__('Rotate App Password'), () => frm.trigger('rotate_app_password'), __('Credentials'))
		frm.add_custom_button(__('Rotate API Key'), () => frm.trigger('rotate_api_key'), __('Credentials'))
		frm.trigger('show_send_usage')
	},

	tenant(frm) {
		// The tenant follows the domain; a new account starts at its default limit, if it has one.
		if (!frm.is_new() || !frm.doc.tenant) return
		frappe.db.get_value('Suite Tenant', frm.doc.tenant, 'default_send_limit').then(({ message }) => {
			if (!frm.doc.send_limit && message.default_send_limit) frm.set_value('send_limit', message.default_send_limit)
		})
	},

	show_send_usage(frm) {
		const usage = frm.doc.__onload?.send_usage
		if (!usage) return
		const limit = frm.doc.daily_send_limit
		const ratio = limit ? usage.day / limit : 0
		const text = __('Sent today: {0} / {1} · This month: {2}', [
			format_number(usage.day, null, 0),
			format_number(limit, null, 0),
			format_number(usage.month, null, 0),
		])
		frm.dashboard.add_indicator(text, ratio >= 1 ? 'red' : ratio >= 0.8 ? 'orange' : 'blue')
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
		frappe.confirm(__('Mint a new app password for this account and revoke the current one? The tenant must be given the new one.'), () => {
			frm.events.call(frm, 'rotate_app_password', {}, __('Rotating...'), (secret) => {
				frm.events.reveal(__('App Password'), secret, __('Shown once; the previous app password no longer works.'))
			})
		})
	},

	rotate_api_key(frm) {
		frappe.confirm(__('Mint a new API key for this account and revoke the current one?'), () => {
			frm.events.call(frm, 'rotate_api_key', {}, __('Rotating...'), (key) => {
				frm.events.reveal(__('API Key'), key, __('Shown once; the previous key no longer works.'))
			})
		})
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
