// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Suite Tenant', {
	setup(frm) {
		frm.set_query('egress_pool', () => ({ filters: { cluster: frm.doc.stalwart_cluster || '' } }))
	},

	refresh(frm) {
		if (frm.is_new()) return
		frm.add_custom_button(__('Rotate Secret'), () => rotate_secret(frm), __('Actions'))
		if (frm.doc.status !== 'Active' || !frm.doc.stalwart_cluster) return
		frm.add_custom_button(__('Adopt Directory'), () => adopt_directory(frm), __('Actions'))
	},

	stalwart_cluster(frm) {
		// A pool belongs to one cluster; a picked one is stale once the cluster changes.
		frm.set_value('egress_pool', null)
	},
})

// The secret is stored encrypted and shown only here; the tenant sends it with its id.
function rotate_secret(frm) {
	frappe.confirm(__('Issue a new secret for {0}? The current one stops working at once.', [frm.doc.title]), () =>
		frm.call('rotate_secret').then((r) => {
			frappe.msgprint({
				title: __('New secret'),
				indicator: 'green',
				message: __('Tenant {0}<br>Secret {1}<br>Copy both into Suite Settings; the secret is not shown again.', [
					frappe.utils.escape_html(frm.doc.name),
					frappe.utils.escape_html(r.message),
				]),
			})
			frm.reload_doc()
		}),
	)
}

// Records what the cluster already holds (one Stalwart per tenant, before Suite Cloud) as this
// tenant's directory. Nothing is pushed to the cluster; objects that already exist are skipped.
function adopt_directory(frm) {
	frappe.confirm(
		__('Record every domain, account, group and mailing list on cluster {0} as belonging to {1}? Objects that already exist are skipped. Nothing is changed on the cluster.', [
			frm.doc.stalwart_cluster,
			frm.doc.name,
		]),
		() =>
			frm.call('adopt_directory').then((r) => {
				const report = r.message || {}
				const count = (part) => Object.values(part || {}).reduce((n, names) => n + names.length, 0)
				const lines = Object.entries(report.skipped || {}).flatMap(([doctype, rows]) =>
					rows.map(([name, reason]) => `${doctype} ${name}: ${reason}`),
				)
				frappe.msgprint({
					title: __('Directory adopted'),
					indicator: count(report.skipped) ? 'orange' : 'green',
					message: [__('{0} adopted, {1} skipped.', [count(report.adopted), count(report.skipped)]), ...lines].join('<br>'),
				})
			}),
	)
}
