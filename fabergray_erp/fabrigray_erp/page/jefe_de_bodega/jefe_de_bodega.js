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

// Read-only supervision screen. All server communication goes through the
// three fabergray_erp.api.jefe_bodega.* endpoints (Commit 6) -- nothing here
// queries Pick List/Reporte de Faltante directly, computes bucketing, or
// writes anything. "VER FALTANTE"/"VER PICK LIST"/quick access all navigate
// to frappe's own standard Form/List/Tree views -- no duplicated screens.
fabergray_erp.JefeDeBodega = class JefeDeBodega {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.jefe_bodega.";
		this.busy = false;

		this.$app = $('<div class="fg-shell fg-jefe-bodega">').appendTo(this.page.body);
		this.render_shell();
		this.load_all();
	}

	call(method) {
		return frappe.call({ method: this.method_prefix + method }).then((r) => r.message);
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
			frappe.set_route("Form", "Reporte de Faltante", name);
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
