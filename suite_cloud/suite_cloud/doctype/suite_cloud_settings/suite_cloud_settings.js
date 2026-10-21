// Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.ui.form.on('Suite Cloud Settings', {
	setup(frm) {
		frm.set_query('default_region', () => ({ filters: { enabled: 1 } }))
	},

	central_url: forget_central_teams,
	central_api_key: forget_central_teams,
	central_api_secret: forget_central_teams,

	onload(frm) {
		frappe.realtime.on('suite_cloud_agent_build', (data) => {
			frm.reload_doc()
			frappe.show_alert({
				message: data.error ? __('Agent build failed: {0}', [data.error]) : __('Agent {0} built.', [data.version]),
				indicator: data.error ? 'red' : 'green',
			})
		})
	},

	refresh(frm) {
		frm.add_custom_button(__('Build Agent'), () => build_agent(frm), __('Telemetry'))
		// Asked when the picker is used, not on every load: Central may be slow or unset.
		frm.fields_dict.central_team.$input.off('focus.central').on('focus.central', () => load_central_teams(frm))
		frm.add_custom_button(__('Test Central Connection'), () => test_central_connection(frm), __('Frappe Cloud'))
		frm.add_custom_button(
			__('Sync Regions'),
			() =>
				frm.call({ doc: frm.doc, method: 'sync_regions', freeze: true, freeze_message: __('Reading regions...') }).then((r) =>
					frappe.show_alert({ message: __('{0} regions synced.', [r.message]), indicator: 'green' }),
				),
			__('Frappe Cloud'),
		)
	},
})

function load_central_teams(frm) {
	const { central_url, central_api_key, central_api_secret } = frm.doc
	if (frm.central_teams_loaded || !central_url || !central_api_key || !central_api_secret) return
	frm.central_teams_loaded = true
	frm.call('get_central_teams').then(
		(r) => {
			const field = frm.fields_dict.central_team
			field.set_data(r.message || [])
			field.set_formatted_input(frm.doc.central_team) // the saved id now shows its title
			field.awesomplete?.evaluate()
		},
		() => (frm.central_teams_loaded = false),
	)
}

function forget_central_teams(frm) {
	frm.central_teams_loaded = false
}

function test_central_connection(frm) {
	frm.call({ doc: frm.doc, method: 'test_central_connection', freeze: true, freeze_message: __('Connecting...') }).then((r) =>
		frappe.msgprint({
			title: __('Central connection works'),
			indicator: 'green',
			message: `<pre>${frappe.utils.escape_html(JSON.stringify(r.message, null, 2))}</pre>`,
		}),
	)
}

function build_agent(frm) {
	frm.call({ doc: frm.doc, method: 'build_agent', freeze: true, freeze_message: __('Queueing the build...') }).then((r) =>
		frappe.show_alert({
			message: r.message.queued ? __('Building agent {0}.', [r.message.version]) : __('A build is already running.'),
			indicator: r.message.queued ? 'green' : 'orange',
		}),
	)
}
