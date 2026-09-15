// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Suite Site', {
	setup(frm) {
		frm.set_query('egress_pool', () => ({ filters: { cluster: frm.doc.cluster || '' } }))
	},

	refresh(frm) {
		if (frm.is_new() || frm.doc.status !== 'Active') return
		frm.add_custom_button(__('Adopt Directory'), () => adopt_directory(frm), __('Actions'))
	},

	cluster(frm) {
		// A pool belongs to one cluster; a picked one is stale once the cluster changes.
		frm.set_value('egress_pool', null)
	},
})

// Records what the cluster already holds (one Stalwart per site, before Suite Cloud) as this
// site's directory. Nothing is pushed to the cluster; objects that already exist are skipped.
function adopt_directory(frm) {
	frappe.confirm(
		__('Record every domain, account, group and mailing list on cluster {0} as belonging to {1}? Objects that already exist are skipped. Nothing is changed on the cluster.', [
			frm.doc.cluster,
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
