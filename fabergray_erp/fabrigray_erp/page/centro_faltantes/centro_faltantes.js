// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["centro-faltantes"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Centro de Faltantes"),
		single_column: true,
	});
	new fabergray_erp.CentroFaltantes(page);
};

// Commit 22.9 -- Centro de Faltantes/Compras. Toda la lógica de negocio
// (validaciones, cuenta contable, trazabilidad, idempotencia) vive
// exclusivamente en api/jefe_bodega.py (Commit 22.8) -- esta Page solo
// llama a get_shortage_center()/get_shortage_center_summary() (lectura,
// Commit 22.9) y a receive_shortage_purchase()/get_shortage_purchase_status()
// (Commit 22.8, reutilizados tal cual, nunca duplicados).
fabergray_erp.CentroFaltantes = class CentroFaltantes {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.jefe_bodega.";
		this.busy = false;

		this.status = "";
		this.txt = "";
		this.list_page = 1;
		this.rows = [];
		this.total = 0;
		this.summary = null;
		this._search_debounce = null;

		this.$app = $('<div class="fg-shell fg-centro-faltantes">').appendTo(this.page.body);
		this.render_shell();
		this.load_all();
	}

	call(method, args) {
		return frappe.call({ method: this.method_prefix + method, args: args || {} }).then((r) => r.message);
	}

	set_busy(is_busy) {
		this.busy = !!is_busy;
		this.$app.find(".fg-refresh-btn").prop("disabled", this.busy);
		this.$app.toggleClass("fg-loading", !!is_busy);
	}

	render_shell() {
		const fullname = frappe.session.user_fullname || frappe.session.user;
		this.$app.html(`
			<div class="fg-header">
				<div class="fg-header-brand">
					<button type="button" class="fg-back-btn">${icon("arrow-left")} ${__("Jefe de Bodega")}</button>
					<span class="fg-header-logo">FABRIGRAY</span>
					<span class="fg-header-sep">|</span>
					<span class="fg-header-title">${__("CENTRO DE FALTANTES")}</span>
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
		this.$app.find(".fg-back-btn").on("click", () => frappe.set_route("jefe-de-bodega"));
		this.$app.find(".fg-refresh-btn").on("click", () => this.load_all());
	}

	load_all() {
		this.set_busy(true);
		this.render_skeleton();
		return Promise.all([this.call("get_shortage_center_summary"), this.load_list()])
			.then(([summary]) => {
				this.summary = summary;
				this.render_body();
			})
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	load_list() {
		return this.call("get_shortage_center", {
			status: this.status || null,
			txt: this.txt,
			start: (this.list_page - 1) * PAGE_SIZE,
			page_length: PAGE_SIZE,
		}).then((res) => {
			this.rows = res.reports || [];
			this.total = res.total || 0;
		});
	}

	refresh_list() {
		this.set_busy(true);
		return this.load_list()
			.then(() => {
				this.$body.find(".fg-cf-cards").html(this.render_cards_html());
				this.$body.find(".fg-cf-pagination").html(this.render_pagination_html());
			})
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	render_skeleton() {
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

	render_body() {
		this.$body.html(`
			${this.render_kpis()}
			${this.render_toolbar()}
			<div class="fg-cf-cards">${this.render_cards_html()}</div>
			<div class="fg-cf-pagination">${this.render_pagination_html()}</div>
		`);
		this.bind_events();
	}

	render_kpis() {
		const s = this.summary || {};
		const cards = [
			{ key: "abiertos", label: __("Faltantes abiertos"), icon: "triangle-alert", mod: "cf-abiertos" },
			{ key: "en_proceso", label: __("En proceso"), icon: "clock", mod: "cf-proceso" },
			{ key: "compras_recibidas_hoy", label: __("Compras recibidas hoy"), icon: "package-check", mod: "cf-compras" },
			{ key: "resueltos_hoy", label: __("Resueltos hoy"), icon: "circle-check-big", mod: "cf-resueltos" },
		];
		const html = cards
			.map(
				(c) => `
				<div class="fg-kpi fg-kpi--${c.mod}">
					<div class="fg-kpi-icon">${icon(c.icon)}</div>
					<div class="fg-kpi-number">${s[c.key] ?? 0}</div>
					<div class="fg-kpi-label">${c.label}</div>
				</div>
			`
			)
			.join("");
		return `<div class="fg-kpis fg-kpis--cf">${html}</div>`;
	}

	render_toolbar() {
		const tabs = [
			{ key: "", label: __("TODOS") },
			{ key: "Abierto", label: __("ABIERTOS") },
			{ key: "En Proceso", label: __("EN PROCESO") },
			{ key: "Resuelto", label: __("RESUELTOS") },
		];
		const tabs_html = tabs
			.map(
				(t) =>
					`<button type="button" class="fg-cf-tab ${
						this.status === t.key ? "is-active" : ""
					}" data-status="${t.key}">${t.label}</button>`
			)
			.join("");

		return `
			<div class="fg-cf-toolbar">
				<div class="fg-cf-tabs">${tabs_html}</div>
				<div class="fg-cf-search-wrap">
					${icon("search", "fg-cf-search-icon")}
					<input type="text" class="fg-cf-search-input" placeholder="${__(
						"Buscar por Item, Pedido o Faltante..."
					)}" value="${frappe.utils.escape_html(this.txt || "")}">
				</div>
			</div>
		`;
	}

	render_cards_html() {
		if (!this.rows.length) {
			return `<div class="fg-empty">${__("No hay reportes de faltante que coincidan.")}</div>`;
		}
		return this.rows.map((r) => this.render_card(r)).join("");
	}

	render_card(r) {
		const status_meta = {
			Abierto: { label: __("ABIERTO"), mod: "cf-abierto" },
			"En Proceso": { label: __("EN PROCESO"), mod: "cf-proceso" },
			Resuelto: { label: __("RESUELTO"), mod: "cf-resuelto" },
		}[r.status] || { label: r.status, mod: "cf-abierto" };

		const pedido = r.sales_order ? `${__("Pedido")} #${frappe.utils.escape_html(r.sales_order)}` : __("Sin pedido asociado");
		const resuelto = r.status === "Resuelto";

		return `
			<div class="fg-cf-card" data-name="${frappe.utils.escape_html(r.name)}">
				<div class="fg-cf-card-top">
					<span class="fg-cf-card-name">${frappe.utils.escape_html(r.name)}</span>
					<span class="fg-status-pill fg-status-pill--${status_meta.mod}">${status_meta.label}</span>
				</div>
				<div class="fg-cf-card-title">${frappe.utils.escape_html(r.item_name)}</div>
				<div class="fg-cf-card-meta">${pedido}</div>
				<div class="fg-cf-card-meta">${__("Almacén")}: ${frappe.utils.escape_html(r.warehouse || "—")}</div>
				<div class="fg-cf-card-qty">
					<div class="fg-cf-card-qty-col">
						<div class="fg-cf-card-qty-label">${__("Solicitado")}</div>
						<div class="fg-cf-card-qty-value">${format_qty(r.qty_faltante)}</div>
					</div>
					<div class="fg-cf-card-qty-col">
						<div class="fg-cf-card-qty-label">${__("Recibido")}</div>
						<div class="fg-cf-card-qty-value fg-cf-card-qty-value--success">${format_qty(r.received_qty)}</div>
					</div>
					<div class="fg-cf-card-qty-col">
						<div class="fg-cf-card-qty-label">${__("Pendiente")}</div>
						<div class="fg-cf-card-qty-value ${r.remaining_qty > 0 ? "fg-cf-card-qty-value--danger" : ""}">${format_qty(
			r.remaining_qty
		)}</div>
					</div>
				</div>
				<div class="fg-cf-card-meta">${__("Motivo")}: ${frappe.utils.escape_html(r.shortage_reason || "—")}</div>
				<div class="fg-cf-card-actions">
					${
						resuelto
							? ""
							: `<button type="button" class="fg-btn fg-btn--solid-primary fg-cf-card-register">${__(
									"REGISTRAR COMPRA"
							  )}</button>`
					}
					<button type="button" class="fg-btn fg-btn--outline-primary fg-cf-card-history">${__("VER HISTORIAL")}</button>
				</div>
			</div>
		`;
	}

	render_pagination_html() {
		if (!this.total) return "";
		const page_count = Math.max(Math.ceil(this.total / PAGE_SIZE), 1);
		const start = (this.list_page - 1) * PAGE_SIZE + 1;
		const end = Math.min(this.list_page * PAGE_SIZE, this.total);
		return `
			<div class="fg-cf-pagination-info">${__("Mostrando {0} a {1} de {2}", [start, end, this.total])}</div>
			<div class="fg-cf-pagination-controls">
				<button type="button" class="fg-cf-pagination-btn fg-cf-pagination-prev" ${
					this.list_page <= 1 ? "disabled" : ""
				}>${icon("chevron-left")}</button>
				<span class="fg-cf-pagination-page">${this.list_page}</span>
				<button type="button" class="fg-cf-pagination-btn fg-cf-pagination-next" ${
					this.list_page >= page_count ? "disabled" : ""
				}>${icon("chevron-right")}</button>
			</div>
		`;
	}

	bind_events() {
		this.$body.find(".fg-cf-tab").on("click", (e) => {
			this.status = $(e.currentTarget).data("status") || "";
			this.list_page = 1;
			this.$body.find(".fg-cf-tab").removeClass("is-active");
			$(e.currentTarget).addClass("is-active");
			this.refresh_list();
		});
		this.$body.find(".fg-cf-search-input").on("input", (e) => {
			const val = $(e.currentTarget).val();
			clearTimeout(this._search_debounce);
			this._search_debounce = setTimeout(() => {
				this.txt = val;
				this.list_page = 1;
				this.refresh_list();
			}, 300);
		});
		this.$body.find(".fg-cf-cards").on("click", ".fg-cf-card-register", (e) => {
			const name = $(e.currentTarget).closest(".fg-cf-card").data("name");
			this.open_register_dialog(name);
		});
		this.$body.find(".fg-cf-cards").on("click", ".fg-cf-card-history", (e) => {
			const name = $(e.currentTarget).closest(".fg-cf-card").data("name");
			this.open_history_dialog(name);
		});
		this.$body.find(".fg-cf-pagination").on("click", ".fg-cf-pagination-prev", () => {
			this.list_page = Math.max(this.list_page - 1, 1);
			this.refresh_list();
		});
		this.$body.find(".fg-cf-pagination").on("click", ".fg-cf-pagination-next", () => {
			this.list_page = this.list_page + 1;
			this.refresh_list();
		});
	}

	// -------------------------------------------------------------------
	// REGISTRAR COMPRA -- llama a receive_shortage_purchase() (Commit 22.8)
	// tal cual, sin reimplementar ninguna validación (todas viven en el
	// servidor). warehouse nunca se envía -- el servidor siempre usa el del
	// propio Reporte de Faltante y rechaza cualquier otro (Commit 22.8).
	// -------------------------------------------------------------------
	open_register_dialog(name) {
		this.set_busy(true);
		this.call("get_shortage_purchase_status", { shortage_report: name })
			.then((status) => this.render_register_dialog(status))
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	render_register_dialog(status) {
		const total_html = (qty, rate) => `
			<div class="fg-cf-total">
				${format_qty(qty)} × ${frappe.format(rate, { fieldtype: "Currency" })}
				= <strong>${frappe.format(qty * rate, { fieldtype: "Currency" })}</strong>
			</div>
		`;

		const dialog = new frappe.ui.Dialog({
			title: `${__("Registrar compra")} — ${status.item_name}`,
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "info_html",
					options: `
						<div class="fg-cf-dialog-info">
							${cf_row(__("Almacén"), status.warehouse)}
							${cf_row(__("Pendiente"), format_qty(status.remaining_qty))}
						</div>
					`,
				},
				{ fieldtype: "Float", fieldname: "qty", label: __("Cantidad recibida"), reqd: 1, onchange: () => refresh_total() },
				{
					fieldtype: "Currency",
					fieldname: "purchase_rate",
					label: __("Valor de compra unitario"),
					reqd: 1,
					onchange: () => refresh_total(),
				},
				{ fieldtype: "Data", fieldname: "purchase_reference", label: __("Referencia de compra") },
				{ fieldtype: "Small Text", fieldname: "note", label: __("Observación") },
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
						__("La cantidad recibida supera el faltante pendiente ({0}).", [format_qty(status.remaining_qty)])
					);
					return;
				}
				dialog.disable_primary_action();
				this.call("receive_shortage_purchase", {
					shortage_report: status.shortage_report,
					qty: values.qty,
					purchase_rate: values.purchase_rate,
					purchase_reference: values.purchase_reference || null,
					note: values.note || null,
				})
					.then((result) => {
						dialog.hide();
						this.refresh_list();
						this.load_all();
						frappe.show_alert(
							{
								message: __("Compra registrada correctamente ({0}).", [result.stock_entry]),
								indicator: "green",
							},
							6
						);
					})
					.catch(() => dialog.enable_primary_action());
			},
			secondary_action_label: __("CANCELAR"),
			secondary_action: () => dialog.hide(),
		});

		const refresh_total = () => {
			const values = dialog.get_values(true) || {};
			dialog.fields_dict.total_html.set_value(total_html(flt(values.qty), flt(values.purchase_rate)));
		};

		dialog.$wrapper.addClass("fg-cf-dialog");
		dialog.show();
	}

	// -------------------------------------------------------------------
	// VER HISTORIAL -- llama a get_shortage_purchase_status() (Commit
	// 22.8) tal cual; solo pinta receipts[] ya calculado por el servidor.
	// -------------------------------------------------------------------
	open_history_dialog(name) {
		this.set_busy(true);
		this.call("get_shortage_purchase_status", { shortage_report: name })
			.then((status) => this.render_history_dialog(status))
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	render_history_dialog(status) {
		const receipts_html = (status.receipts || []).length
			? status.receipts
					.map(
						(r) => `
					<div class="fg-cf-history-row">
						<span class="fg-cf-history-entry">${frappe.utils.escape_html(r.stock_entry)}</span>
						<span>${format_qty(r.qty)} und × ${frappe.format(r.purchase_rate, { fieldtype: "Currency" })}</span>
						<span class="fg-cf-history-date">${r.posting_date ? frappe.datetime.str_to_user(r.posting_date) : "—"}</span>
					</div>
				`
					)
					.join("")
			: `<div class="fg-empty">${__("Todavía no se ha registrado ninguna compra para este faltante.")}</div>`;

		const dialog = new frappe.ui.Dialog({
			title: `${__("Historial de compra")} — ${status.item_name}`,
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "history_html",
					options: `
						<div class="fg-cf-history-list">${receipts_html}</div>
						<div class="fg-cf-history-total">${__("Total recibido")}: <strong>${format_qty(status.received_qty)}</strong></div>
					`,
				},
			],
			primary_action_label: __("CERRAR"),
			primary_action: () => dialog.hide(),
		});
		dialog.$wrapper.addClass("fg-cf-dialog");
		dialog.show();
	}
};

function cf_row(label, value) {
	return `<div class="fg-cf-dialog-row"><span>${label}</span><strong>${frappe.utils.escape_html(String(value))}</strong></div>`;
}

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, intentionally duplicated.
// -------------------------------------------------------------------------
const PAGE_SIZE = 10;

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
