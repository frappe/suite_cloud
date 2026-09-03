// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Stalwart Cluster', {
	refresh(frm) {
		frm.trigger('set_store_queries')
		frm.trigger('add_actions')
		frm.trigger('add_indicators')
	},

	set_store_queries(frm) {
		const kinds = {
			data_store: 'Data',
			blob_store: 'Blob',
			search_store: 'Search',
			in_memory_store: 'In-Memory',
		}
		for (const [field, kind] of Object.entries(kinds)) {
			frm.set_query(field, () => ({ filters: { kind } }))
		}
	},

	add_actions(frm) {
		if (frm.doc.__islocal) return

		frm.add_custom_button(__('Preview Plan'), () => frm.trigger('preview_plan'), __('Configuration'))

		if (frm.doc.status === 'Active') {
			frm.add_custom_button(__('Sync Config'), () => frm.trigger('sync_config'), __('Configuration'))
			frm.add_custom_button(__('Check Drift'), () => frm.trigger('check_drift'), __('Configuration'))
			frm.add_custom_button(__('Rotate API Key'), () => frm.trigger('rotate_api_key'), __('Access'))
			frm.add_custom_button(__('Upgrade Nodes'), () => frm.trigger('upgrade_nodes'), __('Nodes'))
		}
		if (frm.doc.status === 'Bootstrapping') {
			frm.add_custom_button(__('Finish Bootstrap'), () => frm.trigger('finish_bootstrap'), __('Nodes'))
		}
		if (frappe.session.user === 'Administrator') {
			frm.add_custom_button(__('Show Admin Password'), () => frm.trigger('show_admin_password'), __('Access'))
		}
	},

	add_indicators(frm) {
		if (frm.doc.__islocal) return
		if (frm.doc.status === 'Pending') {
			frm.dashboard.add_comment(
				__('Add the cluster SSH public key to the first node VPS, create a Stalwart Node and provision it.'),
				'blue',
				true,
			)
		}
		if (frm.doc.drift_report) {
			const report = JSON.parse(frm.doc.drift_report)
			if (report.differences && report.differences.length) {
				frm.dashboard.add_comment(
					__('Configuration drift detected ({0} differences). Sync Config restores the generated state.', [
						report.differences.length,
					]),
					'orange',
					true,
				)
			}
		}
	},

	call(frm, method, args, freeze_message, done) {
		frappe.call({
			doc: frm.doc,
			method,
			args,
			freeze: true,
			freeze_message,
			callback: (r) => {
				if (!r.exc) {
					frm.reload_doc()
					if (done) done(r.message)
				}
			},
		})
	},

	preview_plan(frm) {
		frm.events.call(frm, 'preview_plan', {}, __('Rendering plan...'), (plan) => {
			frappe.msgprint({ title: __('Cluster Plan'), message: `<pre>${frappe.utils.escape_html(plan)}</pre>`, wide: true })
		})
	},

	sync_config(frm) {
		frappe.confirm(__('Push the generated configuration to the running cluster?'), () => {
			frm.events.call(frm, 'sync_config', {}, __('Syncing configuration...'), (r) => {
				frappe.show_alert({ message: __('Synced: {0} updated', [r.updated.length]), indicator: 'green' })
			})
		})
	},

	check_drift(frm) {
		frm.events.call(frm, 'check_drift', {}, __('Comparing with the cluster...'))
	},

	finish_bootstrap(frm) {
		frm.events.call(frm, 'finish_bootstrap', {}, __('Checking the bootstrap node...'), (done) => {
			frappe.show_alert({
				message: done ? __('Cluster is active') : __('Not ready yet; the certificate may still be pending.'),
				indicator: done ? 'green' : 'orange',
			})
		})
	},

	rotate_api_key(frm) {
		frappe.confirm(__('Mint a new Stalwart API key and revoke the current one?'), () => {
			frm.events.call(frm, 'rotate_api_key', {}, __('Rotating API key...'))
		})
	},

	upgrade_nodes(frm) {
		frappe.confirm(__('Upgrade every active node to {0}, one at a time?', [frm.doc.stalwart_version]), () => {
			frm.events.call(frm, 'upgrade_nodes', {}, __('Queueing upgrades...'))
		})
	},

	show_admin_password(frm) {
		frm.events.call(frm, 'show_admin_password', {}, __('Fetching...'), (password) => {
			frappe.msgprint({ title: __('Admin Password'), message: `<code>${frappe.utils.escape_html(password)}</code>` })
		})
	},
})
