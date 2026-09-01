// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Suite Infra Settings', {
	dns_provider(frm) {
		if (frm.doc.dns_provider && frm.doc.dns_provider === 'GoDaddy') {
			frappe.msgprint({
				title: __('Limited Access to GoDaddy DNS APIs'),
				indicator: 'yellow',
				message: __(
					'Access to GoDaddy’s Domain Management and DNS APIs is restricted to accounts with 10 or more domains or an active Pro Discount Domain Club membership. Please verify that your account meets these requirements before proceeding.',
				),
			})
		}
	},
})
