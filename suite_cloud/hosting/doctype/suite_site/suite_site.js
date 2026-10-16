// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Suite Site', {
	refresh(frm) {
		if (frm.is_new() || frm.doc.status === 'Dropped') return
		if (frm.doc.site_created) {
			// A fresh session each click; the link is opened, never stored.
			frm.add_custom_button(__('Login as Administrator'), () =>
				frm.call('login_as_administrator').then((r) => window.open(r.message, '_blank')),
			)
		}
		if (frm.doc.owner_invite) {
			// The same invite while it is valid, a fresh one once it expired.
			frm.add_custom_button(__('Resend Owner Invite'), () => frm.events.call(frm, 'resend_owner_invite', {}, __('Sending...')), __('Actions'))
		}
		if (frm.doc.status === 'Failed' || frm.doc.error) {
			frm.add_custom_button(__('Retry'), () => frm.events.call(frm, 'retry', {}, __('Retrying...')), __('Actions'))
		}
		if (frm.doc.site_created) {
			frm.add_custom_button(__('Add Domain'), () => ask_domain(frm, 'add_domain', __('Add Domain'), show_records), __('Domains'))
			frm.add_custom_button(__('Verify Domain'), () => ask_domain(frm, 'verify_domain', __('Verify Domain')), __('Domains'))
			frm.add_custom_button(__('Make Primary'), () => ask_domain(frm, 'make_primary', __('Make Primary')), __('Domains'))
			frm.add_custom_button(__('Remove Domain'), () => ask_domain(frm, 'remove_domain', __('Remove Domain')), __('Domains'))
		}
		frm.add_custom_button(
			__('Drop Site'),
			() =>
				frappe.confirm(__('Drop {0}? The site, its mail domain and notification account are deleted.', [frm.doc.hostname || frm.doc.name]), () =>
					frm.events.call(frm, 'drop_site', {}, __('Queueing...')),
				),
			__('Actions'),
		)
	},

	call(frm, method, args, freeze_message, then) {
		frappe.call({
			doc: frm.doc,
			method,
			args,
			freeze: true,
			freeze_message,
			callback: (r) => {
				if (r.exc) return
				then?.(r.message)
				frm.reload_doc()
			},
		})
	},
})

function ask_domain(frm, method, title, then) {
	frappe.prompt(
		[{ fieldname: 'domain', fieldtype: 'Data', label: __('Domain'), reqd: 1 }],
		(values) => frm.events.call(frm, method, values, __('Working...'), then),
		title,
	)
}

function show_records(records) {
	const rows = (records || []).map((r) => `<tr><td>${r.type}</td><td>${frappe.utils.escape_html(r.fqdn)}</td><td>${frappe.utils.escape_html(r.value)}</td></tr>`)
	frappe.msgprint({
		title: __('Publish these records'),
		message: `<table class="table table-bordered"><tr><th>${__('Type')}</th><th>${__('Name')}</th><th>${__('Value')}</th></tr>${rows.join('')}</table>${__('Then run Verify Domain.')}`,
	})
}
