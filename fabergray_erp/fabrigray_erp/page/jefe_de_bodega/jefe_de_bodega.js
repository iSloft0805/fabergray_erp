// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["jefe-de-bodega"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Jefe de Bodega"),
		single_column: true,
	});
	new fabergray_erp.JefeDeBodega(page);
};

// Read-only supervision screen (Commit 6) -- all server communication goes
// through fabergray_erp.api.jefe_bodega.* endpoints; nothing here queries
// Pick List/Reporte de Faltante directly or computes bucketing.
// "VER PICK LIST"/quick access still navigate to frappe's own standard
// Form/List/Tree views -- no duplicated screens for those.
//
// Commit 22.8 -- "VER FALTANTE" now opens this Page's own operational modal
// (open_shortage_detail()) instead of routing straight to the native
// Reporte de Faltante form: shows the same read-only detail plus a
// "COMPRA / MERCANCÍA RECIBIDA" section wired to receive_shortage_purchase().
// The native form is still one click away (open_shortage_detail()'s own
// secondary action) -- this Page never stops being able to fall back to it,
// it just isn't the first thing "VER FALTANTE" does anymore. Every
// server-side rule (permissions, qty/rate validation, the accounting-account
// safety check) lives entirely in api/jefe_bodega.py -- this file only
// renders what the server already decided and disables its own submit
// button while a request is in flight, exactly like page/clientes/
// clientes.js's own dialogs already do; it is never the real security
// boundary.
fabergray_erp.JefeDeBodega = class JefeDeBodega {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.jefe_bodega.";
		this.busy = false;

		this.$app = $('<div class="fg-shell fg-jefe-bodega">').appendTo(this.page.body);
		this.render_shell();
		this.load_all();
	}

	call(method, args) {
		return frappe.call({ method: this.method_prefix + method, args: args || {} }).then((r) => r.message);
	}

	load_all() {
		this.set_busy(true);
		this.render_skeleton();
		return Promise.all([
			this.call("get_summary"),
			this.call("get_open_shortage_reports"),
			this.call("get_active_pick_lists"),
		])
			.then(([summary, shortage_reports, active_pick_lists]) => {
				this.summary = summary;
				this.shortage_reports = shortage_reports;
				this.active_pick_lists = active_pick_lists;
				this.render_body();
			})
			.finally(() => this.set_busy(false));
	}

	// -------------------------------------------------------------------
	// Shell: header stays fixed, mirrors the Bodega header markup 1:1
	// (same classes, from the shared fg-shell.css) with the Jefe title.
	// -------------------------------------------------------------------
	render_shell() {
		const fullname = frappe.session.user_fullname || frappe.session.user;
		this.$app.html(`
			<div class="fg-header">
				<div class="fg-header-brand">
					<span class="fg-header-logo">FABRIGRAY</span>
					<span class="fg-header-sep">|</span>
					<span class="fg-header-title">${__("Jefe de Bodega")}</span>
				</div>
				<div class="fg-header-user">
					<div class="fg-header-user-info">
						<div class="fg-header-user-name">${frappe.utils.escape_html(fullname)}</div>
						<div class="fg-header-user-role">${__("Jefe de Bodega")}</div>
					</div>
					<div class="fg-header-avatar">${get_initials(fullname)}</div>
					<button type="button" class="fg-refresh-btn" title="${__("Actualizar")}">${icon("refresh-cw")}</button>
				</div>
			</div>
			<div class="fg-body"></div>
		`);
		this.$body = this.$app.find(".fg-body");
		this.$app.find(".fg-refresh-btn").on("click", () => this.load_all());
	}

	set_busy(is_busy) {
		this.$app.find(".fg-refresh-btn").prop("disabled", is_busy);
		this.$app.toggleClass("fg-loading", !!is_busy);
	}

	render_skeleton() {
		this.$body.html(`
			<div class="fg-skeleton-kpis">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
			<div class="fg-skeleton-cards">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
		`);
	}

	render_body() {
		this.$body.html(`
			${this.render_kpis()}
			${this.render_attention_section()}
			${this.render_active_section()}
			${this.render_quick_actions()}
		`);
		this.bind_events();
	}

	// -------------------------------------------------------------------
	// KPI row -- Pendientes/En alistamiento/Con faltantes are informative
	// only (no faithful native filter exists for those buckets without
	// reimplementing get_queue()'s shortage join, so they never navigate).
	// Listos/Faltantes abiertos have an exact native filter and are the
	// only two rendered as clickable buttons.
	// -------------------------------------------------------------------
	render_kpis() {
		const s = this.summary || {};
		const cards = [
			{ bucket: "pendientes", label: __("Pendientes"), icon: "clipboard-list", clickable: false },
			{ bucket: "en_alistamiento", label: __("En alistamiento"), icon: "clock", clickable: false },
			{ bucket: "con_faltantes", label: __("Con faltantes"), icon: "triangle-alert", clickable: false },
			{ bucket: "listos", label: __("Listos"), icon: "circle-check-big", clickable: true },
			{ bucket: "faltantes_abiertos", label: __("Faltantes abiertos"), icon: "triangle-alert", clickable: true },
		];

		const html = cards
			.map((c) => {
				const tag = c.clickable ? "button" : "div";
				const type_attr = c.clickable ? 'type="button"' : "";
				const action_attr = c.clickable ? `data-action="${c.bucket}"` : "";
				const link_html = c.clickable
					? `<span class="fg-kpi-link">${__("Ver")} ${icon("chevron-right", "fg-icon-sm")}</span>`
					: "";
				return `
					<${tag} ${type_attr} class="fg-kpi fg-kpi--${c.bucket}" ${action_attr}>
						<div class="fg-kpi-icon">${icon(c.icon)}</div>
						<div class="fg-kpi-number">${s[c.bucket] ?? 0}</div>
						<div class="fg-kpi-label">${c.label}</div>
						${link_html}
					</${tag}>
				`;
			})
			.join("");

		return `<div class="fg-kpis">${html}</div>`;
	}

	// -------------------------------------------------------------------
	// Requieren atención -- open Reporte de Faltante cards.
	// -------------------------------------------------------------------
	render_attention_section() {
		const reports = this.shortage_reports || [];
		const cards = reports.length
			? reports.map((r) => this.render_shortage_card(r)).join("")
			: `<div class="fg-empty">${__("No hay reportes de faltante abiertos.")}</div>`;

		return `
			<div class="fg-section-head">
				<div class="fg-section-title">${icon("triangle-alert", "fg-icon-sm")} ${__("Requieren atención")}</div>
			</div>
			<div class="fg-attention-grid">${cards}</div>
		`;
	}

	render_shortage_card(r) {
		const pedido = r.sales_order
			? `${__("Pedido")} #${frappe.utils.escape_html(r.sales_order)}`
			: __("Sin pedido asociado");
		const motivo = r.shortage_reason ? frappe.utils.escape_html(r.shortage_reason) : "—";
		const reportado_por = r.reported_by_fullname ? frappe.utils.escape_html(r.reported_by_fullname) : "—";
		const hace = r.reported_on ? frappe.datetime.comment_when(r.reported_on) : "—";

		return `
			<div class="fg-shortage-card" data-name="${frappe.utils.escape_html(r.name)}">
				<div class="fg-shortage-card-head">
					<span class="fg-status-pill fg-status-pill--warn">${icon(
						"triangle-alert",
						"fg-icon-sm"
					)} ${__("Faltante")}</span>
				</div>
				<div class="fg-shortage-card-title">${frappe.utils.escape_html(r.item_name)}</div>
				<div class="fg-shortage-card-meta">${pedido}</div>
				<div class="fg-shortage-card-meta">${__("Bodega")}: ${frappe.utils.escape_html(r.warehouse || "—")}</div>
				<div class="fg-shortage-card-qty">
					<div class="fg-shortage-card-qty-col">
						<div class="fg-shortage-card-qty-label">${__("Solicitado")}</div>
						<div class="fg-shortage-card-qty-value">${format_qty(r.qty_solicitada)}</div>
					</div>
					<div class="fg-shortage-card-qty-col">
						<div class="fg-shortage-card-qty-label">${__("Disponible")}</div>
						<div class="fg-shortage-card-qty-value">${format_qty(r.qty_disponible)}</div>
					</div>
					<div class="fg-shortage-card-qty-col">
						<div class="fg-shortage-card-qty-label">${__("Faltan")}</div>
						<div class="fg-shortage-card-qty-value fg-shortage-card-qty-value--danger">${format_qty(
							r.qty_faltante
						)}</div>
					</div>
				</div>
				<div class="fg-shortage-card-meta">${__("Motivo")}: ${motivo}</div>
				<div class="fg-shortage-card-footer">
					<span>${icon("user", "fg-icon-sm")} ${__("Reportado por")} ${reportado_por}</span>
					<span>${icon("clock", "fg-icon-sm")} ${hace}</span>
				</div>
				<button type="button" class="fg-btn fg-btn--outline-danger fg-shortage-card-btn">${__(
					"VER FALTANTE"
				)}</button>
			</div>
		`;
	}

	// -------------------------------------------------------------------
	// Alistamientos activos.
	// -------------------------------------------------------------------
	render_active_section() {
		const pick_lists = this.active_pick_lists || [];
		const rows = pick_lists.length
			? pick_lists.map((pl) => this.render_active_row(pl)).join("")
			: `<div class="fg-empty">${__("No hay alistamientos activos en este momento.")}</div>`;

		return `
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Alistamientos activos")}</div>
			</div>
			<div class="fg-active-list">${rows}</div>
		`;
	}

	render_active_row(pl) {
		const customer = pl.customer ? frappe.utils.escape_html(pl.customer) : __("Sin cliente");
		const bodeguero = pl.fg_started_by_fullname
			? frappe.utils.escape_html(pl.fg_started_by_fullname)
			: __("—");
		const inicio = pl.fg_started_on ? frappe.datetime.str_to_user(pl.fg_started_on) : "—";
		const progreso =
			pl.items_totales != null && pl.items_completos != null
				? `<div class="fg-active-row-progress">${pl.items_completos} / ${pl.items_totales} ${__(
						"productos"
				  )}</div>`
				: "";

		return `
			<div class="fg-active-row" data-name="${frappe.utils.escape_html(pl.name)}">
				<div class="fg-active-row-main">
					<div class="fg-active-row-id">${__("PEDIDO")} #${frappe.utils.escape_html(pl.name)}</div>
					<div class="fg-active-row-customer">${customer}</div>
				</div>
				<div class="fg-active-row-meta">
					<div>${icon("user", "fg-icon-sm")} ${__("Bodeguero")}: ${bodeguero}</div>
					<div>${icon("clock", "fg-icon-sm")} ${__("Inicio")}: ${inicio}</div>
					<div>${icon("package", "fg-icon-sm")} ${pl.item_count} ${
			pl.item_count === 1 ? __("producto") : __("productos")
		}</div>
				</div>
				${progreso}
				<button type="button" class="fg-btn fg-btn--outline-success fg-active-row-btn">${__(
					"VER PICK LIST"
				)}</button>
			</div>
		`;
	}

	// -------------------------------------------------------------------
	// Accesos rápidos -- all standard Frappe/ERPNext views, nothing custom.
	// -------------------------------------------------------------------
	render_quick_actions() {
		return `
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Accesos rápidos")}</div>
			</div>
			<div class="fg-quick-actions">
				<button type="button" class="fg-btn fg-btn--solid-primary" data-quick="pick_lists">${icon(
					"clipboard-list"
				)} ${__("Pick Lists")}</button>
				<button type="button" class="fg-btn fg-btn--solid-primary" data-quick="shortage_reports">${icon(
					"triangle-alert"
				)} ${__("Reportes de Faltante")}</button>
				<button type="button" class="fg-btn fg-btn--ghost" data-quick="inventory">${icon(
					"package"
				)} ${__("Inventario")}</button>
				<button type="button" class="fg-btn fg-btn--ghost" data-quick="warehouses">${icon(
					"house"
				)} ${__("Almacenes")}</button>
			</div>
		`;
	}

	// -------------------------------------------------------------------
	// Events / navigation -- every action here is a route to a standard
	// Frappe/ERPNext surface (Form, List or Tree). Nothing is duplicated.
	// -------------------------------------------------------------------
	bind_events() {
		this.$body.find('.fg-kpi[data-action="listos"]').on("click", () => {
			frappe.route_options = { docstatus: ["=", 1] };
			frappe.set_route("List", "Pick List");
		});
		this.$body.find('.fg-kpi[data-action="faltantes_abiertos"]').on("click", () => {
			frappe.route_options = { status: "Abierto" };
			frappe.set_route("List", "Reporte de Faltante");
		});

		this.$body.find(".fg-shortage-card-btn").on("click", (e) => {
			const name = $(e.currentTarget).closest(".fg-shortage-card").data("name");
			this.open_shortage_detail(name);
		});

		this.$body.find(".fg-active-row-btn").on("click", (e) => {
			const name = $(e.currentTarget).closest(".fg-active-row").data("name");
			frappe.set_route("Form", "Pick List", name);
		});

		this.$body.find('[data-quick="pick_lists"]').on("click", () => frappe.set_route("List", "Pick List"));
		this.$body
			.find('[data-quick="shortage_reports"]')
			.on("click", () => frappe.set_route("List", "Reporte de Faltante"));
		this.$body.find('[data-quick="inventory"]').on("click", () => frappe.set_route("List", "Bin"));
		this.$body.find('[data-quick="warehouses"]').on("click", () => frappe.set_route("Tree", "Warehouse"));
	}

	// =====================================================================
	// Commit 22.8 -- "VER FALTANTE": detalle + "COMPRA / MERCANCÍA RECIBIDA".
	// Un solo Dialog operativo propio, con la Reporte de Faltante nativa
	// siempre a un clic de distancia (secondary_action). Toda regla de
	// negocio (permisos, validaciones, la cuenta contable) vive en
	// api/jefe_bodega.py -- este Dialog solo refleja lo que el servidor ya
	// decidió y deshabilita su propio botón mientras la request está en
	// vuelo, igual que page/clientes/clientes.js.
	// =====================================================================
	open_shortage_detail(name) {
		this.set_busy(true);
		return this.call("get_shortage_purchase_status", { shortage_report: name })
			.then((status) => this.render_shortage_receive_dialog(status))
			.catch(() => {
				// frappe.call() ya mostró su propio diálogo de error real.
			})
			.finally(() => this.set_busy(false));
	}

	render_shortage_receive_dialog(status) {
		const total_html = (qty, rate) => `
			<div class="fg-shortage-receive-total">
				${format_qty(qty)} × ${frappe.format(rate, { fieldtype: "Currency" })}
				= <strong>${frappe.format(qty * rate, { fieldtype: "Currency" })}</strong>
			</div>
		`;

		const dialog = new frappe.ui.Dialog({
			title: `${__("Faltante")} ${status.shortage_report}`,
			fields: [
				{ fieldtype: "HTML", fieldname: "info_html", options: this.render_shortage_info_html(status) },
				{ fieldtype: "Section Break", label: __("COMPRA / MERCANCÍA RECIBIDA") },
				{
					fieldtype: "Float",
					fieldname: "qty",
					label: __("Cantidad recibida"),
					reqd: 1,
					onchange: () => refresh_total(),
				},
				{
					fieldtype: "Currency",
					fieldname: "purchase_rate",
					label: __("Valor de compra unitario"),
					reqd: 1,
					onchange: () => refresh_total(),
				},
				{ fieldtype: "Column Break" },
				{
					// Commit 22.8, revisado: este flujo resuelve UN faltante
					// específico -- el almacén nunca es editable aquí (evita
					// recibir por accidente en un almacén distinto, p.ej.
					// Devoluciones, mientras se cree estar resolviendo un
					// faltante de Producto Terminado). Solo lectura, no se
					// envía al servidor -- api/jefe_bodega.py siempre usa el
					// warehouse del propio Reporte de Faltante y rechaza
					// cualquier otro.
					fieldtype: "Link",
					fieldname: "warehouse",
					label: __("Almacén destino"),
					options: "Warehouse",
					default: status.warehouse,
					read_only: 1,
				},
				{ fieldtype: "Data", fieldname: "purchase_reference", label: __("Referencia de compra") },
				{ fieldtype: "Small Text", fieldname: "note", label: __("Observación") },
				{ fieldtype: "Section Break" },
				{ fieldtype: "HTML", fieldname: "total_html", options: total_html(0, 0) },
			],
			primary_action_label: __("REGISTRAR EN INVENTARIO"),
			primary_action: (values) => {
				if (flt(values.qty) <= 0) {
					frappe.msgprint(__("La cantidad recibida debe ser mayor que cero."));
					return;
				}
				if (flt(values.purchase_rate) <= 0) {
					frappe.msgprint(__("El valor de compra unitario debe ser mayor que cero."));
					return;
				}
				if (flt(values.qty) > flt(status.remaining_qty)) {
					frappe.msgprint(
						__("La cantidad recibida supera el faltante pendiente ({0}).", [
							format_qty(status.remaining_qty),
						])
					);
					return;
				}
				dialog.disable_primary_action();
				this.call("receive_shortage_purchase", {
					shortage_report: status.shortage_report,
					qty: values.qty,
					purchase_rate: values.purchase_rate,
					// warehouse deliberadamente NO se envía -- ver el
					// comentario del campo "warehouse" arriba.
					purchase_reference: values.purchase_reference || null,
					note: values.note || null,
				})
					.then((result) => {
						dialog.hide();
						this.load_all(); // refresca KPIs/tarjetas en segundo plano
						this.show_receipt_confirmation(result);
					})
					.catch(() => dialog.enable_primary_action());
			},
			secondary_action_label: __("VER FALTANTE (NATIVO)"),
			secondary_action: () => {
				dialog.hide();
				frappe.set_route("Form", "Reporte de Faltante", status.shortage_report);
			},
		});

		const refresh_total = () => {
			const values = dialog.get_values(true) || {};
			dialog.fields_dict.total_html.set_value(total_html(flt(values.qty), flt(values.purchase_rate)));
		};

		dialog.$wrapper.addClass("fg-shortage-receive-dialog");
		dialog.show();
	}

	render_shortage_info_html(status) {
		const pedido = status.sales_order
			? `${__("Pedido")} #${frappe.utils.escape_html(status.sales_order)}`
			: __("Sin pedido asociado");
		const alistamiento = status.pick_list ? frappe.utils.escape_html(status.pick_list) : "—";
		const progreso =
			status.received_qty > 0
				? `
					<div class="fg-shortage-receive-progress">
						<span>${__("Solicitado")}: ${format_qty(status.qty_faltante)}</span>
						<span>${__("Recibido para este faltante")}: ${format_qty(status.received_qty)}</span>
						<span>${__("Pendiente")}: ${format_qty(status.remaining_qty)}</span>
					</div>
				`
				: "";

		return `
			<div class="fg-shortage-receive-info">
				${this._info_row(__("Producto"), status.item_name)}
				${this._info_row(__("Item Code"), status.item_code)}
				${this._info_row(__("Pedido"), pedido, true)}
				${this._info_row(__("Lista de Alistamiento"), alistamiento, true)}
				${this._info_row(__("Almacén"), status.warehouse)}
				${this._info_row(__("Cantidad solicitada"), format_qty(status.qty_solicitada), true)}
				${this._info_row(__("Cantidad disponible"), format_qty(status.qty_disponible), true)}
				${this._info_row(__("Cantidad faltante"), format_qty(status.qty_faltante), true)}
				${this._info_row(__("Motivo"), status.shortage_reason || "—")}
				${progreso}
			</div>
		`;
	}

	show_receipt_confirmation(result) {
		const dialog = new frappe.ui.Dialog({
			title: __("Compra registrada correctamente"),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "confirm_html",
					options: `
						<div class="fg-shortage-receive-info">
							${this._info_row(__("Producto"), result.item_name)}
							${this._info_row(__("Item Code"), result.item_code)}
							${this._info_row(__("Cantidad recibida"), format_qty(result.qty))}
							${this._info_row(__("Precio unitario"), frappe.format(result.purchase_rate, { fieldtype: "Currency" }))}
							${this._info_row(__("Total"), frappe.format(result.amount, { fieldtype: "Currency" }))}
							${this._info_row(__("Movimiento"), result.stock_entry)}
							${this._info_row(__("Stock actual"), format_qty(result.current_stock))}
							${this._info_row(__("Faltante pendiente"), format_qty(result.remaining_qty))}
							${this._info_row(__("Estado"), result.status)}
						</div>
					`,
				},
			],
			primary_action_label: __("VER MOVIMIENTO"),
			primary_action: () => {
				dialog.hide();
				frappe.set_route("Form", "Stock Entry", result.stock_entry);
			},
			secondary_action_label: __("VOLVER AL DASHBOARD"),
			secondary_action: () => dialog.hide(),
		});
		dialog.$wrapper.addClass("fg-shortage-receive-dialog");
		dialog.show();
	}

	_info_row(label, value, already_safe) {
		const safe_value = already_safe ? value : frappe.utils.escape_html(value == null ? "—" : value);
		return `<div class="fg-shortage-receive-info-row"><span>${label}</span><strong>${safe_value}</strong></div>`;
	}
};

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, no server calls, no state.
// Intentionally duplicated (not imported) from bodega.js: 3-4 lines each,
// zero business logic, and importing would couple this Page's asset loading
// to bodega.js's file existing/being unchanged.
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

function flt(v) {
	return frappe.utils.flt ? frappe.utils.flt(v) : parseFloat(v) || 0;
}

function format_qty(v) {
	const n = flt(v);
	return Number.isInteger(n) ? String(n) : n.toFixed(2);
}
