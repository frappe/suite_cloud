// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Server Job', {
	refresh(frm) {
		frm.disable_save()
		if (frm.doc.status === 'Failed') {
			frm.add_custom_button(__('Retry'), () => {
				frappe.call({ doc: frm.doc, method: 'retry', freeze: true, callback: () => frm.reload_doc() })
			})
		}
		frappe.realtime.on('server_job_progress', (data) => {
			if (data.job !== frm.doc.name) return
			frm.dashboard.show_progress(__('Tasks'), (data.progress / data.total) * 100, data.task)
			if (data.progress >= data.total) frm.reload_doc()
		})
	},
})
