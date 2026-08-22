// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["bodega"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Bodega"),
		single_column: true,
	});
	new fabergray_erp.Bodega(page);
};

// All server communication in this file goes through these six methods --
// nothing here queries Bin, edits Pick List directly, computes permissions,
// decides shortage rules, or touches Sales Order. That logic lives entirely
// in fabergray_erp.api.bodega (see Commit 4). Commit 5.1 only changes how
// the same data is rendered (fg-* markup/CSS) -- no new business rules.
fabergray_erp.Bodega = class Bodega {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.bodega.";
		// Qty-stepper coalescing state (see request_picked_qty/sync_row below):
		// desired_qty is the latest value the user has asked for per row,
		// updated synchronously on every click; inflight_rows tracks which
		// rows currently have a set_picked_qty request in flight. At most one
		// request is ever in flight per row_name -- other rows are untouched.
		this.desired_qty = new Map();
		this.inflight_rows = new Set();
		this.busy = false;
		this.last_changed_row = null;
		this.state = {
			view: "queue",
			queue: null,
			filter_bucket: null,
			pick_list: null,
			detail: null,
		};

		this.$app = $('<div class="fg-bodega">').appendTo(this.page.body);
		this.render_shell();
		this.load_queue();
	}

	// -------------------------------------------------------------------
	// Thin API wrappers -- the only place that talks to the server.
	// -------------------------------------------------------------------
	call(method, args) {
		return frappe.call({ method: this.method_prefix + method, args: args || {} }).then((r) => r.message);
	}

	load_queue() {
		this.set_shell_busy(true);
		if (this.$body) this.render_skeleton_queue();
		return this.call("get_queue")
			.then((data) => {
				this.state.view = "queue";
				this.state.queue = data;
				this.state.pick_list = null;
				this.state.detail = null;
				this.render_body();
			})
			.finally(() => this.set_shell_busy(false));
	}

	load_detail(pick_list) {
		this.set_shell_busy(true);
		if (this.$body) this.render_skeleton_detail();
		return this.call("get_pick_list", { name: pick_list })
			.then((data) => {
				this.state.view = "detail";
				this.state.pick_list = pick_list;
				this.state.detail = data;
				this.render_body();
			})
			.finally(() => this.set_shell_busy(false));
	}

	// -------------------------------------------------------------------
	// Shell: header (logo, title, user, refresh) stays fixed across views.
	//
	// Refresh button: same SPA refresh pattern as page/ventas/ventas.js's
	// own .fg-refresh-btn -- click -> re-call the real endpoint -> replace
	// local state with the fresh response -> re-render, never a cached or
	// stale render. Deliberately never window.location.reload() or any
	// full Desk reload. set_shell_busy() below is the exact same
	// disable-button + toggle(".fg-loading") pair ventas.js's own
	// set_busy() uses -- .fg-loading's CSS spinner (bodega.css) mirrors
	// ventas.css's own -- and, like ventas.js's load_dashboard(), the calls
	// below have no explicit .catch(): frappe.call() already shows its own
	// native error dialog on failure regardless, and .finally() always
	// restores the button whether the call succeeded or failed.
	//
	// Bodega's version covers one more case than Ventas's: which endpoint
	// to re-call depends on the currently active view (get_queue() for the
	// queue dashboard, get_pick_list() for the detail/picking screen) --
	// Ventas only ever has the one dashboard view to refresh.
	// -------------------------------------------------------------------
	render_shell() {
		const fullname = frappe.session.user_fullname || frappe.session.user;
		this.$app.html(`
			<div class="fg-header">
				<div class="fg-header-brand">
					<span class="fg-header-logo">FABRIGRAY</span>
					<span class="fg-header-sep">|</span>
					<span class="fg-header-title">${__("Bodega")}</span>
				</div>
				<div class="fg-header-user">
					<div class="fg-header-user-info">
						<div class="fg-header-user-name">${frappe.utils.escape_html(fullname)}</div>
						<div class="fg-header-user-role">${__("Bodega")}</div>
					</div>
					<div class="fg-header-avatar">${get_initials(fullname)}</div>
					<button type="button" class="fg-refresh-btn" title="${__("Actualizar")}">${icon("refresh-cw")}</button>
				</div>
			</div>
			<div class="fg-body"></div>
		`);
		this.$body = this.$app.find(".fg-body");
		this.$app.find(".fg-refresh-btn").on("click", () => {
			if (this.state.view === "detail" && this.state.pick_list) {
				this.load_detail(this.state.pick_list);
			} else {
				this.load_queue();
			}
		});
	}

	set_shell_busy(is_busy) {
		this.$app.find(".fg-refresh-btn").prop("disabled", is_busy);
		this.$app.toggleClass("fg-loading", !!is_busy);
	}

	render_body() {
		if (this.state.view === "detail") {
			this.render_detail();
		} else {
			this.render_queue();
		}
	}

	render_skeleton_queue() {
		this.$body.html(`
			<div class="fg-skeleton-kpis">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div>
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
			<div class="fg-skeleton-cards">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
		`);
	}

	render_skeleton_detail() {
		this.$body.html(`
			<div class="fg-skeleton" style="height:88px;margin-bottom:22px;"></div>
			<div class="fg-skeleton" style="height:104px;margin-bottom:22px;"></div>
			<div class="fg-skeleton-cards">
				<div class="fg-skeleton" style="height:108px;"></div>
				<div class="fg-skeleton" style="height:108px;"></div>
				<div class="fg-skeleton" style="height:108px;"></div>
			</div>
		`);
	}

	// -------------------------------------------------------------------
	// Queue view
	// -------------------------------------------------------------------
	static bucket_meta() {
		return {
			pendientes: { label: __("Pendientes"), icon: "clipboard-list" },
			en_alistamiento: { label: __("En alistamiento"), icon: "clock" },
			con_faltantes: { label: __("Con faltantes"), icon: "triangle-alert" },
			listos: { label: __("Listos"), icon: "circle-check-big" },
		};
	}

	render_queue() {
		const queue = this.state.queue || {};
		const meta = Bodega.bucket_meta();
		const order = ["con_faltantes", "en_alistamiento", "pendientes", "listos"];

		const kpi_html = order
			.map((bucket) => {
				const count = (queue[bucket] || []).length;
				const active = this.state.filter_bucket === bucket;
				return `
					<button type="button" class="fg-kpi fg-kpi--${bucket} ${active ? "is-active" : ""}" data-bucket="${bucket}">
						<div class="fg-kpi-icon">${icon(meta[bucket].icon)}</div>
						<div class="fg-kpi-number">${count}</div>
						<div class="fg-kpi-label">${meta[bucket].label}</div>
						<span class="fg-kpi-link">${__("Ver pedidos")} ${icon("chevron-right", "fg-icon-sm")}</span>
					</button>
				`;
			})
			.join("");

		const buckets_to_show = this.state.filter_bucket ? [this.state.filter_bucket] : order;
		let cards_html = "";
		let any = false;
		buckets_to_show.forEach((bucket) => {
			(queue[bucket] || []).forEach((pl) => {
				any = true;
				cards_html += this.render_queue_card(pl, bucket);
			});
		});
		if (!any) {
			cards_html = `<div class="fg-empty">${__("No hay pedidos en este estado.")}</div>`;
		}

		this.$body.html(`
			<div class="fg-kpis">${kpi_html}</div>
			<div class="fg-section-head">
				<div>
					<div class="fg-section-title">${__("Pedidos de hoy")}</div>
					<div class="fg-section-date">${format_today_es()}</div>
				</div>
			</div>
			<div class="fg-cards">${cards_html}</div>
			${render_bottom_nav(queue)}
		`);

		this.$body.find(".fg-kpi").on("click", (e) => {
			const bucket = $(e.currentTarget).data("bucket");
			this.state.filter_bucket = this.state.filter_bucket === bucket ? null : bucket;
			this.render_queue();
		});

		this.$body.find(".fg-order-card").on("click", (e) => {
			const $card = $(e.currentTarget);
			this.open_pick_list($card.data("name"), $card.data("bucket"));
		});
	}

	render_queue_card(pl, bucket) {
		const meta = Bodega.bucket_meta()[bucket];
		const customer = pl.customer ? frappe.utils.escape_html(pl.customer) : __("Sin cliente");
		const started_line =
			bucket !== "pendientes" && pl.fg_started_by
				? `<div class="fg-order-meta">${icon("user", "fg-icon-sm")} ${__("Iniciado por")} ${frappe.utils.escape_html(
						pl.fg_started_by
				  )}</div>`
				: "";

		let button_label = __("VER DETALLE");
		let button_class = "fg-btn--outline-success";
		if (bucket === "pendientes") {
			button_label = __("INICIAR ALISTAMIENTO");
			button_class = "fg-btn--solid-primary";
		} else if (bucket === "en_alistamiento") {
			button_label = __("CONTINUAR");
			button_class = "fg-btn--outline-warning";
		} else if (bucket === "con_faltantes") {
			button_label = __("VER FALTANTES");
			button_class = "fg-btn--outline-danger";
		}

		return `
			<div class="fg-order-card fg-order-card--${bucket} fg-kpi--${bucket}" data-name="${frappe.utils.escape_html(
			pl.name
		)}" data-bucket="${bucket}">
				<div class="fg-order-main">
					<div class="fg-order-icon">${icon(meta.icon)}</div>
					<div>
						<div class="fg-order-id">${__("PEDIDO")} #${frappe.utils.escape_html(pl.name)}</div>
						<div class="fg-order-customer">${customer}</div>
					</div>
				</div>
				<div class="fg-order-info">
					<div class="fg-order-meta">${icon("package", "fg-icon-sm")} ${pl.item_count} ${
			pl.item_count === 1 ? __("producto") : __("productos")
		}</div>
					${started_line}
				</div>
				<div class="fg-progress-block">
					<span class="fg-badge fg-badge--${bucket}">${meta.label}</span>
					${queue_progress_markup(bucket)}
				</div>
				<button type="button" class="fg-btn ${button_class} fg-order-card-btn">${button_label}</button>
			</div>
		`;
	}

	open_pick_list(pick_list, bucket) {
		if (bucket === "pendientes") {
			this.set_shell_busy(true);
			this.call("start_picking", { name: pick_list })
				.then(() => this.load_detail(pick_list))
				.catch(() => this.load_queue())
				.finally(() => this.set_shell_busy(false));
		} else {
			this.load_detail(pick_list);
		}
	}

	// -------------------------------------------------------------------
	// Detail / picking view
	// -------------------------------------------------------------------
	render_detail() {
		const detail = this.state.detail;
		if (!detail) return;

		const is_open = detail.docstatus === 0;
		const rows = detail.rows || [];
		const picked_lines = rows.filter((r) => flt(r.qty_alistada) >= flt(r.qty_solicitada)).length;
		const pct = rows.length ? Math.round((picked_lines / rows.length) * 100) : 0;

		const started_banner = detail.fg_started_by
			? `
				<div class="fg-started-banner">
					${icon("user")} ${__("Alistamiento iniciado por")} <b>${frappe.utils.escape_html(detail.fg_started_by)}</b>
					<span class="fg-started-banner-time">${
						detail.fg_started_on ? frappe.datetime.str_to_user(detail.fg_started_on) : ""
					}</span>
				</div>
			`
			: "";

		const done_banner = !is_open
			? `<div class="fg-done-banner">${icon("circle-check-big")} ${__("Pedido alistado correctamente")}</div>`
			: "";

		const subtitle_parts = [];
		if (detail.customer) subtitle_parts.push(frappe.utils.escape_html(detail.customer));
		if (detail.parent_warehouse) subtitle_parts.push(frappe.utils.escape_html(detail.parent_warehouse));

		this.$body.html(`
			<div class="fg-detail-top">
				<div>
					<button type="button" class="fg-back-btn">${icon("arrow-left")} ${__("Volver a pedidos")}</button>
					<div class="fg-detail-heading">
						<div class="fg-detail-title">${__("PEDIDO")} #${frappe.utils.escape_html(detail.name)}</div>
						<div class="fg-detail-subtitle">${subtitle_parts.join(" · ") || "—"}</div>
					</div>
				</div>
				${started_banner}${done_banner}
			</div>

			<div class="fg-progress-card">
				<div>
					<div class="fg-progress-card-head">${__("Progreso del alistamiento")}</div>
					<div class="fg-progress-card-count">${picked_lines} / ${rows.length} <small>${__("productos")}</small></div>
				</div>
				<div class="fg-progress-card-bar">
					<div class="fg-progress-track"><div class="fg-progress-fill" style="--fg-progress-width:${pct}%"></div></div>
				</div>
				<div class="fg-progress-card-pct">${pct}%</div>
			</div>

			<div class="fg-item-cards">${rows.map((row) => this.render_item_card(row, is_open)).join("")}</div>

			${
				is_open
					? `
				<div class="fg-finish-bar">
					<button type="button" class="fg-finish-btn">${icon("circle-check-big")} ${__("TERMINAR ALISTAMIENTO")}</button>
					<div class="fg-finish-note">${icon("lock", "fg-icon-sm")} ${__(
							"Se validarán cantidades y faltantes antes de finalizar"
					  )}</div>
				</div>
			`
					: ""
			}
		`);

		this.last_changed_row = null;

		this.$body.find(".fg-back-btn").on("click", () => this.load_queue());
		if (is_open) {
			this.bind_item_card_events();
			this.$body.find(".fg-finish-btn").on("click", () => this.finish_picking());
		}
	}

	render_item_card(row, is_open) {
		const solicitada = flt(row.qty_solicitada);
		const disponible = flt(row.qty_disponible);
		const alistada = flt(row.qty_alistada);
		const uom = row.uom || "";
		const complete = solicitada > 0 ? alistada >= solicitada : true;
		const shortfall = Math.max(solicitada - alistada, 0);
		const disponible_short = disponible < solicitada;
		const flash = row.row_name === this.last_changed_row ? "fg-row-flash" : "";

		let status_html;
		if (row.has_shortage_report) {
			status_html = `<span class="fg-status-pill fg-status-pill--warn">${icon("triangle-alert", "fg-icon-sm")} ${__(
				"Faltante reportado"
			)}</span>`;
		} else if (complete) {
			status_html = `<span class="fg-status-pill fg-status-pill--ok">${icon("circle-check-big", "fg-icon-sm")} ${__(
				"Completo"
			)}</span>`;
		} else if (is_open) {
			status_html = `
				<span class="fg-status-pill fg-status-pill--warn">${icon("triangle-alert", "fg-icon-sm")} ${__("Faltan")} ${format_qty(
				shortfall
			)} ${uom}</span>
				<button type="button" class="fg-btn fg-btn--danger-sm fg-report-shortage-btn">${__("REPORTAR FALTANTE")}</button>
			`;
		} else {
			status_html = `<span class="fg-status-pill fg-status-pill--warn">${icon("triangle-alert", "fg-icon-sm")} ${__(
				"Faltan"
			)} ${format_qty(shortfall)} ${uom}</span>`;
		}

		const qty_cols = `
			<div class="fg-qty-cols-mobile">
				<div class="fg-qty-col">
					<div class="fg-qty-col-label">${__("Solicitado")}</div>
					<div class="fg-qty-col-value">${format_qty(solicitada)}</div>
					<div class="fg-qty-col-unit">${uom}</div>
				</div>
				<div class="fg-qty-col">
					<div class="fg-qty-col-label">${__("Disponible")}</div>
					<div class="fg-qty-col-value ${disponible_short ? "fg-qty-col-value--short" : "fg-qty-col-value--ok"}">${format_qty(
			disponible
		)}</div>
					<div class="fg-qty-col-unit">${uom}</div>
				</div>
				<div class="fg-qty-col fg-qty-col-alistado-desktop-only">
					<div class="fg-qty-col-label">${__("Alistado")}</div>
					<div class="fg-qty-col-value">${format_qty(alistada)}</div>
					<div class="fg-qty-col-unit">${uom}</div>
				</div>
			</div>
		`;

		const control_html = is_open
			? `
				<div class="fg-stepper-label-mobile">${__("Alistado")}</div>
				<div class="fg-stepper">
					<button type="button" class="fg-stepper-btn fg-stepper-minus" ${alistada <= 0 ? "disabled" : ""}>${icon(
					"minus"
			  )}</button>
					<input type="number" inputmode="decimal" class="fg-stepper-input" value="${alistada}" min="0">
					<button type="button" class="fg-stepper-btn fg-stepper-plus" ${
						solicitada > 0 && alistada >= solicitada ? "disabled" : ""
					}>${icon("plus")}</button>
				</div>
			`
			: `<div class="fg-item-readonly"><small>${__("Alistado")}</small>${format_qty(alistada)} ${uom}</div>`;

		return `
			<div class="fg-item-card ${flash}" data-row="${frappe.utils.escape_html(row.row_name)}">
				<div class="fg-item-identity">
					<div class="fg-item-thumb">${icon("package")}</div>
					<div>
						<div class="fg-item-name">${frappe.utils.escape_html(row.item_name || row.item_code)}</div>
						<div class="fg-item-code">${frappe.utils.escape_html(row.item_code)}</div>
						${uom ? `<span class="fg-item-uom">${frappe.utils.escape_html(uom)}</span>` : ""}
					</div>
				</div>
				${qty_cols}
				${control_html}
				<div class="fg-item-status">${status_html}</div>
			</div>
		`;
	}

	bind_item_card_events() {
		this.$body.find(".fg-item-card").each((i, el) => {
			const $card = $(el);
			const row_name = $card.data("row");
			const $input = $card.find(".fg-stepper-input");

			$card.find(".fg-stepper-minus").on("click", () => {
				const current = this.desired_qty.has(row_name) ? this.desired_qty.get(row_name) : flt($input.val());
				this.request_picked_qty(row_name, Math.max(current - 1, 0), $card);
			});
			$card.find(".fg-stepper-plus").on("click", () => {
				const current = this.desired_qty.has(row_name) ? this.desired_qty.get(row_name) : flt($input.val());
				this.request_picked_qty(row_name, current + 1, $card);
			});
			$input.on("change", () => {
				this.request_picked_qty(row_name, Math.max(flt($input.val()), 0), $card);
			});
			$card.find(".fg-report-shortage-btn").on("click", () => {
				this.open_report_shortage_dialog(row_name, $card);
			});
		});
	}

	// -------------------------------------------------------------------
	// Qty-stepper coalescing.
	//
	// Goal: a burst of +/- clicks on one row must never be silently dropped
	// and must never queue up N serial requests either. At most one
	// set_picked_qty request is in flight per row_name at any time; extra
	// clicks that land while that request is in flight only update
	// desired_qty (and the on-screen input, optimistically) -- they do not
	// start a second request. When the in-flight request resolves, we
	// compare what the server just confirmed against the *current*
	// desired_qty: if the user asked for something else in the meantime, we
	// immediately re-sync straight to that latest value (skipping whatever
	// intermediate values were requested and abandoned along the way).
	// Only once server and desired_qty agree do we do the one full
	// load_detail() refresh. Other rows are completely unaffected -- the
	// lock is per row_name, never global.
	// -------------------------------------------------------------------
	request_picked_qty(row_name, qty, $card) {
		qty = Math.max(flt(qty), 0);
		this.desired_qty.set(row_name, qty);
		this.update_stepper_display(row_name, qty, $card);

		if (this.inflight_rows.has(row_name)) {
			// A request for this row is already in flight; it will pick up
			// this newer desired_qty itself once it resolves (see below).
			return;
		}
		this.sync_row(row_name, $card);
	}

	sync_row(row_name, $card) {
		const sent_qty = this.desired_qty.get(row_name);
		if (sent_qty === undefined) return;

		this.inflight_rows.add(row_name);
		$card.addClass("fg-row-syncing");

		this.call("set_picked_qty", { name: this.state.pick_list, row_name: row_name, qty: sent_qty })
			.then(() => {
				this.inflight_rows.delete(row_name);
				const latest_desired = this.desired_qty.get(row_name);
				// Compare against what THIS request just sent, not against the
				// server's (possibly rounded) echo -- otherwise a field
				// precision rounding on save could make this loop forever
				// resending the same value.
				if (latest_desired !== undefined && flt(latest_desired) !== flt(sent_qty)) {
					// The user asked for something new while this request was
					// in flight -- chase the newest value directly, no
					// intermediate step.
					this.sync_row(row_name, $card);
					return;
				}
				this.desired_qty.delete(row_name);
				$card.removeClass("fg-row-syncing");
				this.last_changed_row = row_name;
				this.load_detail(this.state.pick_list);
			})
			.catch(() => {
				// Server rejected this value (over the limit, doc no longer
				// open, concurrent edit, etc). Drop the optimistic guess for
				// this row entirely and reload the real state from the
				// server -- never leave a false optimistic number on screen.
				// This is the one case where controls are briefly blocked,
				// via fg-row-busy's pointer-events:none, until that reload
				// finishes and re-renders the row from scratch.
				this.inflight_rows.delete(row_name);
				this.desired_qty.delete(row_name);
				$card.removeClass("fg-row-syncing").addClass("fg-row-busy");
				this.last_changed_row = row_name;
				this.load_detail(this.state.pick_list);
			});
	}

	// Optimistic, local-only UI update: reflects the requested qty on the
	// input immediately, and soft-caps the buttons using the qty_solicitada
	// already present in state.detail (no new data fetched for this). The
	// server-side check in set_picked_qty (over_delivery_receipt_allowance)
	// remains the real authority -- this is only a visual courtesy so the
	// stepper doesn't invite obviously-invalid clicks.
	update_stepper_display(row_name, qty, $card) {
		$card.find(".fg-stepper-input").val(qty);
		$card.find(".fg-stepper-minus").prop("disabled", qty <= 0);

		const row = (this.state.detail && this.state.detail.rows || []).find((r) => r.row_name === row_name);
		const solicitada = row ? flt(row.qty_solicitada) : 0;
		$card.find(".fg-stepper-plus").prop("disabled", solicitada > 0 && qty >= solicitada);
	}

	open_report_shortage_dialog(row_name, $card) {
		const row = (this.state.detail.rows || []).find((r) => r.row_name === row_name);
		if (!row) return;

		frappe.model.with_doctype("Reporte de Faltante", () => {
			const shortage_field = frappe
				.get_meta("Reporte de Faltante")
				.fields.find((f) => f.fieldname === "shortage_reason");
			const reason_options = (shortage_field.options || "")
				.split("\n")
				.map((o) => o.trim())
				.filter(Boolean);

			const dialog = new frappe.ui.Dialog({
				title: __("Reportar faltante"),
				fields: [
					{ fieldtype: "HTML", fieldname: "summary_html" },
					{
						fieldtype: "Select",
						fieldname: "shortage_reason",
						label: __("Motivo"),
						options: reason_options,
						reqd: 1,
					},
					{
						fieldtype: "Float",
						fieldname: "qty_disponible",
						label: __("Cantidad disponible / encontrada"),
						default: row.qty_alistada,
						reqd: 1,
					},
					{
						fieldtype: "Small Text",
						fieldname: "resolution_note",
						label: __("Nota (opcional)"),
					},
				],
				primary_action_label: __("REPORTAR FALTANTE"),
				primary_action: (values) => {
					dialog.disable_primary_action();
					this.call("report_shortage", {
						pick_list: this.state.pick_list,
						row_name: row_name,
						qty_disponible: values.qty_disponible,
						shortage_reason: values.shortage_reason,
						resolution_note: values.resolution_note,
					})
						.then(() => {
							dialog.hide();
							frappe.show_alert({ message: "✓ " + __("Faltante reportado"), indicator: "green" });
							this.last_changed_row = row_name;
							this.load_detail(this.state.pick_list);
						})
						.catch(() => {
							dialog.enable_primary_action();
						});
				},
			});

			dialog.$wrapper.addClass("fg-shortage-dialog");

			const render_summary = () => {
				const found = flt(dialog.get_value("qty_disponible"));
				const faltante = Math.max(flt(row.qty_solicitada) - found, 0);
				dialog.fields_dict.summary_html.$wrapper.html(`
					<div class="fg-shortage-summary">
						<div class="fg-shortage-summary-item">${frappe.utils.escape_html(row.item_name || row.item_code)}</div>
						<div>
							<div class="fg-shortage-summary-field-label">${__("Solicitado")}</div>
							<div class="fg-shortage-summary-field-value">${format_qty(row.qty_solicitada)} ${row.uom || ""}</div>
						</div>
						<div>
							<div class="fg-shortage-summary-field-label">${__("Encontrado")}</div>
							<div class="fg-shortage-summary-field-value">${format_qty(found)} ${row.uom || ""}</div>
						</div>
						<div>
							<div class="fg-shortage-summary-field-label">${__("Faltante")}</div>
							<div class="fg-shortage-summary-field-value fg-shortage-summary-field-value--danger">${format_qty(
								faltante
							)} ${row.uom || ""}</div>
						</div>
					</div>
				`);
			};

			render_summary();
			dialog.fields_dict.qty_disponible.df.onchange = render_summary;
			dialog.show();
		});
	}

	finish_picking() {
		if (this.busy) return;
		frappe.confirm(__("¿Confirmas que terminaste de alistar este pedido?"), () => {
			this.busy = true;
			const $btn = this.$body.find(".fg-finish-btn").prop("disabled", true).addClass("fg-btn--loading");
			this.call("finish_picking", { name: this.state.pick_list })
				.then(() => {
					frappe.show_alert({ message: "✓ " + __("Pedido alistado correctamente"), indicator: "green" });
					this.load_queue();
				})
				.catch(() => {
					// Server already showed the exact validation/concurrency message;
					// reload so the screen reflects the current true state.
					this.load_detail(this.state.pick_list);
				})
				.finally(() => {
					this.busy = false;
					$btn.prop("disabled", false).removeClass("fg-btn--loading");
				});
		});
	}
};

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, no server calls, no state.
// -------------------------------------------------------------------------
function icon(name, extra_class) {
	return `<svg class="fg-icon ${extra_class || ""}"><use href="#icon-${name}"></use></svg>`;
}

function get_initials(name) {
	const parts = (name || "").trim().split(/\s+/).filter(Boolean);
	if (!parts.length) return "?";
	const first = parts[0][0] || "";
	const second = parts.length > 1 ? parts[1][0] : "";
	return (first + second).toUpperCase();
}

function format_today_es() {
	const d = new Date();
	try {
		const label = d.toLocaleDateString("es-CO", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
		return label.charAt(0).toUpperCase() + label.slice(1);
	} catch (e) {
		return frappe.datetime.str_to_user(frappe.datetime.nowdate());
	}
}

function queue_progress_markup(bucket) {
	if (bucket === "pendientes") {
		return `
			<div class="fg-progress-label-row"><span>${__("Progreso")}</span><span class="fg-progress-pct">0%</span></div>
			<div class="fg-progress-track"><div class="fg-progress-fill" style="--fg-progress-width:0%"></div></div>
		`;
	}
	if (bucket === "listos") {
		return `
			<div class="fg-progress-label-row"><span>${__("Progreso")}</span><span class="fg-progress-pct">100%</span></div>
			<div class="fg-progress-track"><div class="fg-progress-fill" style="--fg-progress-width:100%"></div></div>
		`;
	}
	// en_alistamiento / con_faltantes: get_queue() does not return per-order
	// qty_alistada/qty_solicitada, so no numeric percentage is invented here --
	// only an indeterminate bar + status label. The exact % is shown in the
	// detail view, where get_pick_list() provides real per-line quantities.
	const label = bucket === "con_faltantes" ? __("Con faltantes") : __("En progreso");
	return `
		<div class="fg-progress-label-row"><span>${__("Progreso")}</span><span class="fg-progress-pct">${label}</span></div>
		<div class="fg-progress-track"><div class="fg-progress-fill fg-progress-fill--indeterminate"></div></div>
	`;
}

function render_bottom_nav(queue) {
	const faltantes_count = ((queue || {}).con_faltantes || []).length;
	return `
		<div class="fg-bottom-nav">
			<div class="fg-nav-item is-active">${icon("house")}<span>${__("Inicio")}</span></div>
			<div class="fg-nav-item">${icon("clipboard-list")}<span>${__("Pedidos")}</span></div>
			<div class="fg-nav-item">${icon("triangle-alert")}<span>${__("Faltantes")}</span>${
		faltantes_count ? `<span class="fg-nav-badge">${faltantes_count}</span>` : ""
	}</div>
			<div class="fg-nav-item">${icon("clock")}<span>${__("Historial")}</span></div>
			<div class="fg-nav-item">${icon("ellipsis")}<span>${__("Más")}</span></div>
		</div>
	`;
}

function flt(v) {
	return frappe.utils.flt ? frappe.utils.flt(v) : parseFloat(v) || 0;
}

function format_qty(v) {
	const n = flt(v);
	return Number.isInteger(n) ? String(n) : n.toFixed(2);
}
