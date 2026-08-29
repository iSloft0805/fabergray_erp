// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["almacenes"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Almacenes"),
		single_column: true,
	});
	new fabergray_erp.Almacenes(page);
};

// Commit 22.9 -- vista visual de Warehouses + drill-down de productos.
// Read-only en toda la Page: ningún botón escribe Bin ni ningún otro
// documento -- api.jefe_bodega.get_warehouse_summary()/get_warehouse_items()
// (Commit 22.9) solo leen. Click en un producto abre /app/inventario con ese
// Item (Commit 22.4/22.6, sin duplicar esa lógica aquí).
fabergray_erp.Almacenes = class Almacenes {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.jefe_bodega.";
		this.busy = false;

		this.summary = null;
		this.state = { view: "list" }; // "list" | "detail"
		this.detail_warehouse = null;
		this.detail = null;
		this.detail_txt = "";
		this.detail_page = 1;
		this.detail_rows = [];
		this.detail_total = 0;
		this._search_debounce = null;

		this.$app = $('<div class="fg-shell fg-almacenes">').appendTo(this.page.body);
		this.render_shell();
		this.load_summary();
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
					<span class="fg-header-title">${__("ALMACENES")}</span>
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
		this.$app.find(".fg-refresh-btn").on("click", () => {
			if (this.state.view === "list") this.load_summary();
			else if (this.detail_warehouse) this.open_warehouse(this.detail_warehouse);
		});
	}

	// =====================================================================
	// Lista de Almacenes
	// =====================================================================
	load_summary() {
		this.set_busy(true);
		this.state.view = "list";
		this.render_skeleton();
		return this.call("get_warehouse_summary")
			.then((summary) => {
				this.summary = summary;
				this.render_list();
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

	render_list() {
		const s = this.summary || {};
		const cards = [
			{ key: "active_warehouses", label: __("Almacenes activos"), icon: "house", mod: "wh-activos" },
			{ key: "items_with_stock", label: __("Productos con stock"), icon: "package", mod: "wh-productos" },
			{ key: "total_units", label: __("Unidades totales"), icon: "boxes", mod: "wh-unidades", fmt: format_qty },
			{
				key: "total_stock_value",
				label: __("Stock total distribuido"),
				icon: "circle-dollar-sign",
				mod: "wh-valor",
				fmt: (v) => frappe.format(v, { fieldtype: "Currency" }),
			},
		];
		const kpis_html = cards
			.map((c) => {
				const value = c.fmt ? c.fmt(s[c.key] ?? 0) : s[c.key] ?? 0;
				return `
					<div class="fg-kpi fg-kpi--${c.mod}">
						<div class="fg-kpi-icon">${icon(c.icon)}</div>
						<div class="fg-kpi-number">${value}</div>
						<div class="fg-kpi-label">${c.label}</div>
					</div>
				`;
			})
			.join("");

		const warehouses = s.warehouses || [];
		const cards_html = warehouses.length
			? warehouses.map((w) => this.render_warehouse_card(w)).join("")
			: `<div class="fg-empty">${__("No hay almacenes activos configurados.")}</div>`;

		this.$body.html(`
			<div class="fg-kpis fg-kpis--wh">${kpis_html}</div>
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Almacenes")}</div>
			</div>
			<div class="fg-wh-cards">${cards_html}</div>
		`);

		this.$body.find(".fg-wh-card-btn").on("click", (e) => {
			const name = $(e.currentTarget).closest(".fg-wh-card").data("name");
			this.open_warehouse(name);
		});
	}

	render_warehouse_card(w) {
		return `
			<div class="fg-wh-card" data-name="${frappe.utils.escape_html(w.name)}">
				<div class="fg-wh-card-title">${frappe.utils.escape_html((w.warehouse_name || w.name).toUpperCase())}</div>
				<div class="fg-wh-card-name">${frappe.utils.escape_html(w.name)}</div>
				<div class="fg-wh-card-stats">
					<div class="fg-wh-card-stat">
						<span class="fg-wh-card-stat-label">${__("Productos con stock")}</span>
						<span class="fg-wh-card-stat-value">${w.items_with_stock}</span>
					</div>
					<div class="fg-wh-card-stat">
						<span class="fg-wh-card-stat-label">${__("Unidades")}</span>
						<span class="fg-wh-card-stat-value">${format_qty(w.total_qty)}</span>
					</div>
				</div>
				<button type="button" class="fg-btn fg-btn--solid-primary fg-wh-card-btn">${__("VER PRODUCTOS")}</button>
			</div>
		`;
	}

	// =====================================================================
	// Detalle de un Warehouse
	// =====================================================================
	open_warehouse(name) {
		this.detail_warehouse = name;
		this.detail_txt = "";
		this.detail_page = 1;
		this.state.view = "detail";
		this.set_busy(true);
		this.render_skeleton();
		return this.load_detail()
			.then(() => this.render_detail())
			.catch(() => (this.state.view = "list"))
			.finally(() => this.set_busy(false));
	}

	load_detail() {
		return this.call("get_warehouse_items", {
			warehouse: this.detail_warehouse,
			txt: this.detail_txt,
			start: (this.detail_page - 1) * ITEM_PAGE_SIZE,
			page_length: ITEM_PAGE_SIZE,
		}).then((res) => {
			this.detail = res;
			this.detail_rows = res.items || [];
			this.detail_total = res.total || 0;
		});
	}

	refresh_detail_rows() {
		this.set_busy(true);
		return this.load_detail()
			.then(() => {
				this.$body.find(".fg-wh-item-rows").html(this.render_item_rows_html());
				this.$body.find(".fg-wh-item-pagination").html(this.render_item_pagination_html());
			})
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	render_detail() {
		const d = this.detail || {};
		this.$body.html(`
			<div class="fg-np-header">
				<button type="button" class="fg-np-back">${icon("arrow-left")} ${__("Volver a Almacenes")}</button>
				<div class="fg-np-title">${frappe.utils.escape_html(d.warehouse_name || d.warehouse || "")}</div>
			</div>
			<div class="fg-wh-detail-summary">
				<div class="fg-wh-detail-stat"><span>${__("Total productos")}</span><strong>${d.total_products}</strong></div>
				<div class="fg-wh-detail-stat"><span>${__("Cantidad total")}</span><strong>${format_qty(d.total_qty)}</strong></div>
			</div>
			<div class="fg-wh-search-wrap">
				${icon("search", "fg-wh-search-icon")}
				<input type="text" class="fg-wh-search-input" placeholder="${__("Buscar por código o nombre de producto...")}">
			</div>
			<div class="fg-wh-item-rows">${this.render_item_rows_html()}</div>
			<div class="fg-wh-item-pagination">${this.render_item_pagination_html()}</div>
		`);

		this.$body.find(".fg-np-back").on("click", () => this.load_summary());
		this.$body.find(".fg-wh-search-input").on("input", (e) => {
			const val = $(e.currentTarget).val();
			clearTimeout(this._search_debounce);
			this._search_debounce = setTimeout(() => {
				this.detail_txt = val;
				this.detail_page = 1;
				this.refresh_detail_rows();
			}, 300);
		});
		this.bind_item_row_events();
	}

	render_item_rows_html() {
		if (!this.detail_rows.length) {
			return `<div class="fg-empty">${__("No hay productos con stock en este almacén.")}</div>`;
		}
		return this.detail_rows
			.map(
				(item) => `
				<div class="fg-wh-item-row" data-item-code="${frappe.utils.escape_html(item.item_code)}">
					<div class="fg-wh-item-code">${frappe.utils.escape_html(item.item_code)}</div>
					<div class="fg-wh-item-name">
						<div>${frappe.utils.escape_html(item.item_name || "")}</div>
						<div class="fg-wh-item-group">${frappe.utils.escape_html(item.item_group || "")}</div>
					</div>
					<div class="fg-wh-item-qty">${format_qty(item.actual_qty)} ${frappe.utils.escape_html(item.stock_uom || "")}</div>
					<div class="fg-wh-item-rate">${
						item.selling_rate != null ? frappe.format(item.selling_rate, { fieldtype: "Currency" }) : "—"
					}</div>
				</div>
			`
			)
			.join("");
	}

	render_item_pagination_html() {
		if (!this.detail_total) return "";
		const page_count = Math.max(Math.ceil(this.detail_total / ITEM_PAGE_SIZE), 1);
		const start = (this.detail_page - 1) * ITEM_PAGE_SIZE + 1;
		const end = Math.min(this.detail_page * ITEM_PAGE_SIZE, this.detail_total);
		return `
			<div class="fg-wh-pagination-info">${__("Mostrando {0} a {1} de {2}", [start, end, this.detail_total])}</div>
			<div class="fg-wh-pagination-controls">
				<button type="button" class="fg-wh-pagination-btn fg-wh-pagination-prev" ${
					this.detail_page <= 1 ? "disabled" : ""
				}>${icon("chevron-left")}</button>
				<span class="fg-wh-pagination-page">${this.detail_page}</span>
				<button type="button" class="fg-wh-pagination-btn fg-wh-pagination-next" ${
					this.detail_page >= page_count ? "disabled" : ""
				}>${icon("chevron-right")}</button>
			</div>
		`;
	}

	bind_item_row_events() {
		this.$body.find(".fg-wh-item-rows").on("click", ".fg-wh-item-row", (e) => {
			const item_code = $(e.currentTarget).data("item-code");
			this.open_in_inventario(item_code);
		});
		this.$body.find(".fg-wh-item-pagination").on("click", ".fg-wh-pagination-prev", () => {
			this.detail_page = Math.max(this.detail_page - 1, 1);
			this.refresh_detail_rows();
		});
		this.$body.find(".fg-wh-item-pagination").on("click", ".fg-wh-pagination-next", () => {
			this.detail_page = this.detail_page + 1;
			this.refresh_detail_rows();
		});
	}

	// Click en producto -> /app/inventario con ese Item -- reutiliza la Page
	// Inventario existente (Commit 22.4/22.6), nunca un segundo detalle de
	// producto propio.
	open_in_inventario(item_code) {
		frappe.route_options = { item_code: item_code };
		frappe.set_route("inventario");
	}
};

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, intentionally duplicated.
// -------------------------------------------------------------------------
const ITEM_PAGE_SIZE = 20;

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
