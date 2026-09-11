// Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt
frappe.ui.form.on('DNS Record', {
	refresh(frm) {
		frm.trigger('add_actions')
		frm.trigger('add_comments')
	},

	add_actions(frm) {
		if (!frm.doc.__islocal) {
			frm.add_custom_button(
				__('Verify DNS Record'),
				() => {
					frm.trigger('verify_dns_record')
				},
				__('Actions'),
			)

			if (!frm.doc.is_verified) {
				frm.add_custom_button(
					__('Sync DNS Record'),
					() => {
						frm.trigger('sync_dns_record')
					},
					__('Actions'),
				)
			}
		}
	},

	add_comments(frm) {
		if (!frm.doc.__islocal && !frm.doc.is_verified) {
			const zone_link = `<a href="/app/dns-zone/${encodeURIComponent(frm.doc.dns_zone)}">${frm.doc.dns_zone}</a>`
			const msg = __(
				'This record is not verified. If the DNS Zone {0} has no provider configured, add the record at the provider by hand.',
				[zone_link],
			)
			frm.dashboard.add_comment(msg, 'yellow', true)
		}
	},

	verify_dns_record(frm) {
		frappe.call({
			doc: frm.doc,
			method: 'verify_dns_record',
			args: {
				save: true,
			},
			freeze: true,
			freeze_message: __('Verifying DNS Record...'),
			callback: (r) => {
				if (!r.exc) {
					frm.refresh()
				}
			},
		})
	},

	sync_dns_record(frm) {
		frappe.call({
			doc: frm.doc,
			method: 'sync_dns_record',
			freeze: true,
			freeze_message: __('Syncing DNS Record...'),
			callback: (r) => {
				if (!r.exc) {
					frm.refresh()
				}
			},
		})
	},
})
