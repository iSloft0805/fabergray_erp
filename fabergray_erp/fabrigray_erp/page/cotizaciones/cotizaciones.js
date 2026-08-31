// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["cotizaciones"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Cotizaciones"),
		single_column: true,
	});
	new fabergray_erp.Cotizaciones(page);
};

// All server communication in this file goes through fabergray_erp.api.cotizaciones.*
// (Quotation reads/writes -- shared across Company since Commit 25.1, no longer
// if_owner-scoped) and fabergray_erp.api.ventas.
// search_customers/search_items (Commit 20.5's own instruction: reuse those two
// verbatim, they are generic and already whitelisted -- never duplicated here).
// No inventory field is ever requested or rendered (no qty_disponible/Bin/Pick
// List anywhere in this file -- a Quotation may be created with stock 0, by
// design). No economic field (rate/price_list_rate/discount/amount/taxes/
// grand_total or any equivalent) is ever read from a server response or
// constructed here -- build_quotation_payload() below is the one place a
// create_and_submit_quotation() body is assembled, and it only ever sends
// item_code/qty per line plus customer/valid_till/terms at the document level.
fabergray_erp.Cotizaciones = class Cotizaciones {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.cotizaciones.";
		this.ventas_method_prefix = "fabergray_erp.api.ventas.";
		this.busy = false;

		// Dashboard data (view: "dashboard").
		this.summary = null;
		this.quotations = null;
		this.quotation_filter = null; // null | "cotizaciones_hoy" | "pendientes" | "aprobadas" | "vencidas"

		// "Nueva cotización" (view: "nueva_cotizacion") working state -- reset
		// every time open_nueva_cotizacion() runs, never persisted across
		// cotizaciones. No editing/modifying state yet (Commits 20.6/20.7).
		this.nc = this.blank_nueva_cotizacion_state();
		this._customer_search_seq = 0;
		this._item_search_seq = 0;
		this._item_info_cache = new Map(); // item_code -> get_item_info() response

		this.state = { view: "dashboard" };

		this.$app = $('<div class="fg-shell fg-cotizaciones">').appendTo(this.page.body);
		this.render_shell();
		this.load_dashboard();
	}

	blank_nueva_cotizacion_state() {
		return {
			editing_quotation_name: null, // Commit 20.6: null -> "Nueva cotización"; a Draft Quotation name -> "Editar cotización"
			customer: null, // {name, customer_name}
			cart: new Map(), // item_code -> {item_code, item_name, stock_uom, qty}
			customer_results: [],
			item_results: [],
			valid_till: "",
			terms: "",
		};
	}

	// -------------------------------------------------------------------
	// Thin API wrappers -- the only place that talks to the server.
	//
	// frappe.call() itself does NOT return a real Promise -- it returns the
	// jQuery Deferred/jqXHR from $.ajax(), whose promise object never
	// implements .finally() (confirmed in page/bodega/bodega.js's own fix,
	// same root cause -- page/ventas/ventas.js still carries the original
	// bug, not touched here per this commit's explicit scope). Every call
	// site in this file that chains .finally() after a server call relies
	// on _frappe_call() below actually returning a standard Promise, same
	// as frappe's own frappe.xcall() does for the identical reason.
	// -------------------------------------------------------------------
	_frappe_call(method, args) {
		return new Promise((resolve, reject) => {
			frappe.call({
				method: method,
				args: args || {},
				callback: (r) => resolve(r.message),
				error: (r) => reject(r),
			});
		});
	}

	call(method, args) {
		return this._frappe_call(this.method_prefix + method, args);
	}

	call_ventas(method, args) {
		return this._frappe_call(this.ventas_method_prefix + method, args);
	}

	// -------------------------------------------------------------------
	// Shell: header (logo, title, user, refresh) stays fixed across views.
	// Same structure/behaviour as page/ventas/ventas.js's own render_shell().
	// -------------------------------------------------------------------
	render_shell() {
		const fullname = frappe.session.user_fullname || frappe.session.user;
		this.$app.html(`
			<div class="fg-header">
				<div class="fg-header-brand">
					<span class="fg-header-logo">FABRIGRAY</span>
					<span class="fg-header-sep">|</span>
					<span class="fg-header-title">${__("Cotizaciones")}</span>
				</div>
				<div class="fg-header-user">
					<div class="fg-header-user-info">
						<div class="fg-header-user-name">${frappe.utils.escape_html(fullname)}</div>
						<div class="fg-header-user-role">${__("Vendedora")}</div>
					</div>
					<div class="fg-header-avatar">${get_initials(fullname)}</div>
					<button type="button" class="fg-refresh-btn" title="${__("Actualizar")}">${icon("refresh-cw")}</button>
				</div>
			</div>
			<div class="fg-body"></div>
		`);
		this.$body = this.$app.find(".fg-body");
		this.$app.find(".fg-refresh-btn").on("click", () => {
			if (this.state.view === "dashboard") this.load_dashboard();
		});
	}

	set_busy(is_busy) {
		this.$app.find(".fg-refresh-btn").prop("disabled", is_busy || this.state.view !== "dashboard");
		this.$app.toggleClass("fg-loading", !!is_busy);
	}

	// =====================================================================
	// Dashboard
	// =====================================================================
	load_dashboard() {
		this.set_busy(true);
		this.state.view = "dashboard";
		this.render_skeleton_dashboard();
		return Promise.all([this.call("get_quotation_summary"), this.call("get_my_quotations")])
			.then(([summary, quotations]) => {
				this.summary = summary;
				this.quotations = quotations;
				this.render_dashboard();
			})
			.finally(() => this.set_busy(false));
	}

	render_skeleton_dashboard() {
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

	render_dashboard() {
		this.$body.html(`
			${this.render_kpis()}
			<div class="fg-np-cta-row">
				<button type="button" class="fg-btn fg-btn--solid-primary fg-btn--lg fg-new-quotation-btn">
					${icon("plus")} ${__("NUEVA COTIZACIÓN")}
				</button>
			</div>
			<div class="fg-quotations-section">${this.render_quotations_section()}</div>
		`);
		this.bind_dashboard_events();
	}

	render_kpis() {
		const s = this.summary || {};
		const cards = [
			{ key: "cotizaciones_hoy", label: __("Cotizaciones de hoy"), sub: __("Creadas hoy"), i: "calendar", mod: "cotizaciones-hoy" },
			{ key: "pendientes", label: __("Pendientes"), sub: __("Sin resolver"), i: "clock", mod: "cotizaciones-pendientes" },
			{ key: "aprobadas", label: __("Aprobadas"), sub: __("Con pedido generado"), i: "check", mod: "cotizaciones-aprobadas" },
			{ key: "vencidas", label: __("Vencidas"), sub: __("Fuera de vigencia"), i: "x", mod: "cotizaciones-vencidas" },
		];

		const html = cards
			.map(
				(c) => `
				<button type="button" class="fg-kpi fg-kpi--${c.mod} ${this.quotation_filter === c.key ? "is-active" : ""}" data-filter="${c.key}">
					<div class="fg-kpi-icon">${icon(c.i)}</div>
					<div class="fg-kpi-number">${s[c.key] ?? 0}</div>
					<div class="fg-kpi-label">${c.label}</div>
					<div class="fg-kpi-sub">${c.sub}</div>
					<span class="fg-kpi-link">${__("Ver cotizaciones")} ${icon("chevron-right", "fg-icon-sm")}</span>
				</button>
			`
			)
			.join("");

		return `<div class="fg-kpis fg-kpis--cotizaciones">${html}</div>`;
	}

	// Mirrors get_quotation_summary()'s own server-side bucket rules exactly
	// (fabergray_erp/api/cotizaciones.py) -- re-applied client-side only to
	// filter the already-fetched get_my_quotations() list, never to compute
	// a KPI number itself (that number always comes straight from
	// get_quotation_summary()).
	quotation_matches_filter(q, filter) {
		if (!filter) return true;
		if (filter === "cotizaciones_hoy") return q.transaction_date === frappe.datetime.nowdate();
		if (filter === "pendientes") return q.status === "Open";
		if (filter === "aprobadas") return ["Ordered", "Partially Ordered"].includes(q.status);
		if (filter === "vencidas") return q.status === "Expired";
		return true;
	}

	render_quotations_section() {
		const all = this.quotations || [];
		const list = all.filter((q) => this.quotation_matches_filter(q, this.quotation_filter));

		const filter_labels = {
			cotizaciones_hoy: __("Cotizaciones de hoy"),
			pendientes: __("Pendientes"),
			aprobadas: __("Aprobadas"),
			vencidas: __("Vencidas"),
		};
		const chip = this.quotation_filter
			? `
				<div class="fg-filter-chip">
					${__("Filtro")}: <strong>${filter_labels[this.quotation_filter]}</strong>
					<button type="button" class="fg-filter-chip-clear">${icon("x", "fg-icon-sm")}</button>
				</div>`
			: "";

		const cards = list.length
			? list.map((q) => this.render_quotation_card(q)).join("")
			: `<div class="fg-empty">${__("No tienes cotizaciones para mostrar.")}</div>`;

		return `
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Cotizaciones")}</div>
			</div>
			${chip}
			<div class="fg-quotation-list">${cards}</div>
		`;
	}

	render_quotation_card(q) {
		const status = quotation_status_meta(q.status);
		const customer_label = frappe.utils.escape_html(q.customer_name || q.customer || "—");
		const vigencia = q.valid_till ? frappe.datetime.str_to_user(q.valid_till) : "—";

		return `
			<div class="fg-quotation-card">
				<div class="fg-quotation-card-top">
					<div class="fg-quotation-card-id">#${frappe.utils.escape_html(q.name)}</div>
					<span class="fg-badge fg-badge--${status.mod}">${status.label}</span>
				</div>
				<div class="fg-quotation-card-customer">${icon("user", "fg-icon-sm")} ${customer_label}</div>
				<div class="fg-quotation-card-meta">
					<span>${icon("calendar", "fg-icon-sm")} ${frappe.datetime.str_to_user(q.transaction_date)}</span>
					<span>${icon("clock", "fg-icon-sm")} ${__("Vigencia")}: ${vigencia}</span>
				</div>
				<div class="fg-quotation-card-counts">
					<span>${q.item_count} ${q.item_count === 1 ? __("referencia") : __("referencias")}</span>
					<span>${format_qty(q.total_qty)} ${__("unidades")}</span>
				</div>
				${this.render_quotation_card_actions(q)}
			</div>
		`;
	}

	// Commit 20.6: VER COTIZACIÓN always shows; EDITAR only for a Draft
	// (`q.status` is the native Quotation.status string already returned
	// by get_my_quotations() -- no new field needed to tell it apart, same
	// convention as render_order_card_actions() in Ventas). Eliminar/
	// Cancelar are Commits 20.7, not built yet -- omitted entirely, not
	// shown disabled.
	render_quotation_card_actions(q) {
		const name_attr = `data-quotation-name="${frappe.utils.escape_html(q.name)}"`;
		const view_btn = `
			<button type="button" class="fg-order-card-action fg-quotation-card-view" ${name_attr}>
				${icon("eye", "fg-icon-sm")} ${__("VER COTIZACIÓN")}
			</button>
		`;

		if (q.status !== "Draft") {
			return `<div class="fg-quotation-card-actions">${view_btn}</div>`;
		}

		return `
			<div class="fg-quotation-card-actions">
				${view_btn}
				<button type="button" class="fg-order-card-action fg-quotation-card-edit" ${name_attr}>
					${icon("pencil", "fg-icon-sm")} ${__("EDITAR")}
				</button>
			</div>
		`;
	}

	bind_dashboard_events() {
		this.$body.find(".fg-new-quotation-btn").on("click", () => this.open_nueva_cotizacion());

		this.$body.find(".fg-kpi[data-filter]").on("click", (e) => {
			const key = $(e.currentTarget).data("filter");
			this.quotation_filter = this.quotation_filter === key ? null : key;
			this.$body.find(".fg-quotations-section").html(this.render_quotations_section());
			this.bind_quotations_section_events();
			this.$body.find(".fg-kpi").removeClass("is-active");
			if (this.quotation_filter) this.$body.find(`.fg-kpi[data-filter="${this.quotation_filter}"]`).addClass("is-active");
			document.querySelector(".fg-quotations-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
		});

		this.bind_quotations_section_events();
	}

	bind_quotations_section_events() {
		this.$body.find(".fg-filter-chip-clear").on("click", () => {
			this.quotation_filter = null;
			this.$body.find(".fg-kpi").removeClass("is-active");
			this.$body.find(".fg-quotations-section").html(this.render_quotations_section());
			this.bind_quotations_section_events();
		});

		this.$body.find(".fg-quotation-card-view").on("click", (e) => {
			this.open_quotation_detail($(e.currentTarget).data("quotation-name"));
		});
		this.$body.find(".fg-quotation-card-edit").on("click", (e) => {
			this.open_edit_cotizacion($(e.currentTarget).data("quotation-name"));
		});
	}

	// =====================================================================
	// Detalle de cotización ("VER COTIZACIÓN") -- operational only, same
	// non-economic allowlist as get_quotation_detail() (Commit 20.2). No
	// line item here ever carries rate/amount/price_list_rate/etc.
	// =====================================================================
	open_quotation_detail(name) {
		if (!name) return;
		this.render_quotation_detail_overlay(null, true);
		this.call("get_quotation_detail", { name: name })
			.then((detail) => this.render_quotation_detail_overlay(detail, false))
			.catch(() => this.close_quotation_detail());
	}

	render_quotation_detail_overlay(detail, loading) {
		this.$app.find(".fg-quotation-detail-overlay").remove();

		const $overlay = $('<div class="fg-quotation-detail-overlay"></div>').appendTo(this.$app);
		$overlay.on("mousedown", (e) => {
			if (e.target === $overlay[0]) this.close_quotation_detail();
		});

		if (loading) {
			$overlay.html(`
				<div class="fg-quotation-detail-panel">
					<div class="fg-skeleton fg-product-skeleton"></div>
					<div class="fg-skeleton fg-product-skeleton"></div>
				</div>
			`);
			return;
		}

		const status = quotation_status_meta(detail.status);
		const vigencia = detail.valid_till ? frappe.datetime.str_to_user(detail.valid_till) : "—";
		const obs = detail.observations
			? `<div class="fg-quotation-detail-obs">${icon("file-text", "fg-icon-sm")} ${frappe.utils.escape_html(
					detail.observations
			  )}</div>`
			: "";
		const lines = (detail.items || [])
			.map(
				(l) => `
				<div class="fg-quotation-detail-line">
					<div class="fg-quotation-detail-line-info">
						<span class="fg-quotation-detail-line-name">${frappe.utils.escape_html(l.item_name)}</span>
						<span class="fg-quotation-detail-line-code">${frappe.utils.escape_html(l.item_code)}</span>
					</div>
					<span class="fg-quotation-detail-line-qty">${format_qty(l.qty)} ${frappe.utils.escape_html(l.stock_uom || "")}</span>
				</div>
			`
			)
			.join("");

		$overlay.html(`
			<div class="fg-quotation-detail-panel">
				<div class="fg-quotation-detail-header">
					<div class="fg-quotation-detail-id">#${frappe.utils.escape_html(detail.name)}</div>
					<span class="fg-badge fg-badge--${status.mod}">${status.label}</span>
					<button type="button" class="fg-quotation-detail-close" title="${__("Cerrar")}">${icon("x")}</button>
				</div>
				<div class="fg-quotation-detail-customer">
					${icon("user", "fg-icon-sm")} ${frappe.utils.escape_html(detail.customer_name || detail.customer || "—")}
				</div>
				<div class="fg-quotation-detail-meta">
					<span>${icon("calendar", "fg-icon-sm")} ${frappe.datetime.str_to_user(detail.transaction_date)}</span>
					<span>${icon("clock", "fg-icon-sm")} ${__("Vigencia")}: ${vigencia}</span>
				</div>
				${obs}
				<div class="fg-quotation-detail-section-title">${__("Productos")}</div>
				<div class="fg-quotation-detail-lines">
					${lines || `<div class="fg-empty fg-empty--sm">${__("Sin productos.")}</div>`}
				</div>
				<div class="fg-quotation-detail-footer">
					${detail.item_count} ${detail.item_count === 1 ? __("referencia") : __("referencias")}
					&middot;
					${format_qty(detail.total_qty)} ${__("unidades")}
				</div>
			</div>
		`);
		$overlay.find(".fg-quotation-detail-close").on("click", () => this.close_quotation_detail());
	}

	close_quotation_detail() {
		this.$app.find(".fg-quotation-detail-overlay").remove();
	}

	// =====================================================================
	// Nueva cotización
	// =====================================================================
	open_nueva_cotizacion() {
		this.nc = this.blank_nueva_cotizacion_state();
		this._item_info_cache = new Map();
		this.state.view = "nueva_cotizacion";
		this.set_busy(false);
		this.render_nueva_cotizacion();
	}

	// Commit 20.6: reuses the exact same "Nueva cotización" screen,
	// prefilled via get_editable_quotation() (server already enforces
	// docstatus==0 -- only a Draft can ever reach this). Never submits on
	// save -- see save_draft_edit()/confirm_quotation() below. No price is
	// ever fetched or shown here -- get_editable_quotation() never returns
	// one, same as every other read in this module.
	open_edit_cotizacion(name) {
		if (!name) return;
		this.nc = this.blank_nueva_cotizacion_state();
		this._item_info_cache = new Map();
		this.state.view = "nueva_cotizacion";
		this.set_busy(true);

		this.call("get_editable_quotation", { name: name })
			.then((detail) => {
				this.nc.editing_quotation_name = detail.name;
				this.nc.customer = { name: detail.customer, customer_name: detail.customer_name };
				this.nc.valid_till = detail.valid_till || "";
				this.nc.terms = detail.observations || "";
				for (const item of detail.items || []) {
					this.nc.cart.set(item.item_code, {
						item_code: item.item_code,
						item_name: item.item_name,
						stock_uom: item.stock_uom,
						qty: item.qty,
					});
				}
				this.render_nueva_cotizacion();
			})
			.catch(() => this.back_to_dashboard())
			.finally(() => this.set_busy(false));
	}

	back_to_dashboard() {
		this.load_dashboard();
	}

	render_nueva_cotizacion() {
		const editing = !!this.nc.editing_quotation_name;
		const title = editing ? __("Editar cotización") : __("Nueva cotización");
		this.$body.html(`
			<div class="fg-np-header">
				<button type="button" class="fg-np-back">${icon("arrow-left")} ${__("Volver")}</button>
				<div class="fg-np-title">${title}</div>
			</div>

			<div class="fg-np-section">
				<div class="fg-np-section-title">${__("1. Cliente")}</div>
				<div class="fg-np-customer-area"></div>
			</div>

			<div class="fg-np-section">
				<div class="fg-np-section-title">${__("2. Agregar productos")}</div>
				<div class="fg-search-box">
					${icon("search")}
					<input type="text" class="fg-search-input fg-item-search-input" placeholder="${__("Buscar producto...")}">
				</div>
				<div class="fg-item-results"></div>
			</div>

			<div class="fg-np-section">
				<div class="fg-np-section-title">${__("3. Resumen de la cotización")}</div>
				<div class="fg-np-summary"></div>
			</div>
		`);
		this.render_customer_area();
		this.render_item_results_empty_prompt();
		this.render_summary();
		this.bind_nueva_cotizacion_events();
	}

	render_item_results_skeleton() {
		this.$body.find(".fg-item-results").html(`
			<div class="fg-skeleton fg-product-skeleton"></div>
			<div class="fg-skeleton fg-product-skeleton"></div>
			<div class="fg-skeleton fg-product-skeleton"></div>
			<div class="fg-skeleton fg-product-skeleton"></div>
		`);
	}

	// Initial state of "2. Agregar productos" -- no catalog preload, same
	// convention as Page Ventas: the full Item list never renders until the
	// Vendedora actually types something into the search box.
	render_item_results_empty_prompt() {
		this.$body.find(".fg-item-results").html(`
			<div class="fg-empty">${__("Escribe para buscar productos")}</div>
		`);
	}

	bind_nueva_cotizacion_events() {
		this.$body.find(".fg-np-back").on("click", () => this.back_to_dashboard());

		const $item_input = this.$body.find(".fg-item-search-input");
		const debounced_item_search = frappe.utils.debounce((txt) => this.search_items(txt), 300);
		$item_input.on("input", (e) => {
			const txt = $(e.currentTarget).val();
			if (!txt || !txt.trim()) {
				this._item_search_seq++; // invalidate any in-flight search
				this.nc.item_results = [];
				this.render_item_results_empty_prompt();
				return;
			}
			debounced_item_search(txt);
		});
	}

	// -- Paso 1: Cliente -----------------------------------------------------

	render_customer_area() {
		const $area = this.$body.find(".fg-np-customer-area");
		if (this.nc.customer) {
			$area.html(`
				<div class="fg-selected-chip">
					${icon("user", "fg-icon-sm")}
					<span>${frappe.utils.escape_html(this.nc.customer.customer_name)}</span>
					<button type="button" class="fg-chip-remove" title="${__("Cambiar cliente")}">${icon("x", "fg-icon-sm")}</button>
				</div>
			`);
			$area.find(".fg-chip-remove").on("click", () => {
				this.nc.customer = null;
				this.nc.customer_results = [];
				this.render_customer_area();
				this.refresh_confirm_state();
			});
			return;
		}

		// No catalog preload -- just the search box, closed, nothing fetched
		// until the Vendedora types something (same convention as Ventas).
		$area.html(`
			<div class="fg-search-box">
				${icon("search")}
				<input type="text" class="fg-search-input fg-customer-search-input" placeholder="${__("Buscar cliente...")}">
			</div>
			<div class="fg-search-dropdown"></div>
		`);

		const $input = $area.find(".fg-customer-search-input");
		const debounced = frappe.utils.debounce((txt) => this.search_customers(txt), 300);
		$input.on("input", (e) => {
			const txt = $(e.currentTarget).val();
			if (!txt || !txt.trim()) {
				this._customer_search_seq++; // invalidate any in-flight search
				this.nc.customer_results = [];
				this.render_customer_dropdown();
				return;
			}
			debounced(txt);
		});
		$input.on("focus", () => {
			if (this.nc.customer_results.length) $area.find(".fg-search-dropdown").addClass("is-open");
		});
		$input.on("blur", () => {
			// Small delay so a result row's own "mousedown" (fires before
			// blur) can still register the selection before this closes it.
			setTimeout(() => $area.find(".fg-search-dropdown").removeClass("is-open"), 150);
		});
	}

	search_customers(txt) {
		if (!txt || !txt.trim()) {
			this._customer_search_seq++;
			this.nc.customer_results = [];
			this.render_customer_dropdown();
			return Promise.resolve();
		}
		const seq = ++this._customer_search_seq;
		return this.call_ventas("search_customers", { txt: txt }).then((results) => {
			if (seq !== this._customer_search_seq || this.nc.customer) return;
			this.nc.customer_results = results || [];
			this.render_customer_dropdown();
		});
	}

	render_customer_dropdown() {
		const $dropdown = this.$body.find(".fg-search-dropdown");
		if (!$dropdown.length) return;

		const results = this.nc.customer_results;
		if (!results.length) {
			$dropdown.removeClass("is-open").empty();
			return;
		}

		$dropdown.html(
			results
				.map(
					(c) => `
					<div class="fg-search-result" data-name="${frappe.utils.escape_html(c.name)}">
						${icon("user", "fg-icon-sm")}
						<span>${frappe.utils.escape_html(c.customer_name)}</span>
					</div>
				`
				)
				.join("")
		).addClass("is-open");

		$dropdown.find(".fg-search-result").on("mousedown", (e) => {
			e.preventDefault();
			const name = $(e.currentTarget).data("name");
			const found = results.find((c) => c.name === name);
			if (!found) return;
			this.nc.customer = found;
			this.render_customer_area();
			this.refresh_confirm_state();
		});
	}

	// -- Paso 2: Productos ----------------------------------------------------
	// No inventory field is ever requested or shown (no "Disponible", no
	// stock, no faltantes, no projected qty) -- get_item_info() below never
	// returns one either (unlike ventas.get_item_info()'s qty_disponible).
	// A product may be added at any quantity regardless of physical stock.

	search_items(txt) {
		if (!txt || !txt.trim()) {
			this._item_search_seq++;
			this.nc.item_results = [];
			this.render_item_results_empty_prompt();
			return Promise.resolve();
		}
		const seq = ++this._item_search_seq;
		this.render_item_results_skeleton();
		return this.call_ventas("search_items", { txt: txt }).then((results) => {
			if (seq !== this._item_search_seq) return;
			this.nc.item_results = results || [];
			return this.hydrate_item_details(this.nc.item_results).then(() => {
				if (seq !== this._item_search_seq) return;
				this.render_item_results();
			});
		});
	}

	// get_item_info() (api/cotizaciones.py) is called per currently-displayed
	// search result, cached by item_code for this "Nueva cotización" session
	// -- same pattern as Ventas' hydrate_item_availability(), minus any
	// availability field (cotizaciones.get_item_info() never returns one).
	hydrate_item_details(results) {
		return Promise.all(
			results.map((r) => {
				if (this._item_info_cache.has(r.item_code)) return Promise.resolve();
				return this.call("get_item_info", { item_code: r.item_code }).then((info) => {
					this._item_info_cache.set(r.item_code, info);
				});
			})
		);
	}

	render_item_results() {
		const $results = this.$body.find(".fg-item-results");
		const results = this.nc.item_results;

		if (!results.length) {
			$results.html(`<div class="fg-empty">${__("No se encontraron productos.")}</div>`);
			return;
		}

		$results.html(results.map((r) => this.render_item_result_card(r)).join(""));
		this.bind_item_result_events();
	}

	// Product card shows only image/name/code/UOM/stepper -- no
	// disponibilidad, no stock, no faltantes (explicit instruction, Commit
	// 20.5): inventory has no role in a Quotation.
	render_item_result_card(r) {
		const info = this._item_info_cache.get(r.item_code);
		const stock_uom = r.stock_uom || (info && info.stock_uom) || "";
		const qty = this.cart_qty(r.item_code);
		const thumb = r.image
			? `<img class="fg-product-thumb-img" src="${frappe.utils.escape_html(r.image)}" alt="">`
			: icon("image");

		return `
			<div class="fg-product-card ${qty > 0 ? "fg-product-card--in-cart" : ""}" data-item-code="${frappe.utils.escape_html(r.item_code)}">
				<div class="fg-product-thumb">${thumb}</div>
				<div class="fg-product-info">
					<div class="fg-product-name">${frappe.utils.escape_html(r.item_name)}</div>
					<div class="fg-product-code">${frappe.utils.escape_html(r.item_code)}</div>
					<div class="fg-product-meta">
						<span>${frappe.utils.escape_html(stock_uom)}</span>
					</div>
				</div>
				<div class="fg-stepper">
					<button type="button" class="fg-stepper-btn fg-stepper-minus" ${qty <= 0 ? "disabled" : ""}>${icon("minus")}</button>
					<input type="number" inputmode="decimal" class="fg-stepper-input" value="${qty}" min="0">
					<button type="button" class="fg-stepper-btn fg-stepper-plus">${icon("plus")}</button>
				</div>
			</div>
		`;
	}

	bind_item_result_events() {
		this.$body.find(".fg-item-results .fg-product-card").each((i, el) => {
			const $card = $(el);
			const item_code = $card.data("item-code");
			const $input = $card.find(".fg-stepper-input");

			$card.find(".fg-stepper-minus").on("click", () => {
				this.set_cart_qty(item_code, Math.max(this.cart_qty(item_code) - 1, 0));
			});
			$card.find(".fg-stepper-plus").on("click", () => {
				this.set_cart_qty(item_code, this.cart_qty(item_code) + 1);
			});
			$input.on("change", () => {
				this.set_cart_qty(item_code, Math.max(flt($input.val()), 0));
			});
		});
	}

	// -- Cart / Paso 3: Resumen -----------------------------------------------

	cart_qty(item_code) {
		const line = this.nc.cart.get(item_code);
		return line ? line.qty : 0;
	}

	set_cart_qty(item_code, qty) {
		qty = flt(qty);
		if (qty <= 0) {
			this.nc.cart.delete(item_code);
		} else {
			const result = this.nc.item_results.find((r) => r.item_code === item_code);
			const info = this._item_info_cache.get(item_code);
			const existing = this.nc.cart.get(item_code);
			this.nc.cart.set(item_code, {
				item_code: item_code,
				item_name: (result && result.item_name) || (info && info.item_name) || (existing && existing.item_name) || item_code,
				stock_uom: (result && result.stock_uom) || (info && info.stock_uom) || (existing && existing.stock_uom) || "",
				qty: qty,
			});
		}
		this.sync_item_result_card(item_code);
		this.render_summary();
		this.refresh_confirm_state();
	}

	// Updates only the one affected product card's stepper (if it is
	// currently rendered in the search results) instead of re-rendering the
	// whole grid -- keeps the search input focused while tapping +/-.
	sync_item_result_card(item_code) {
		const $card = this.$body.find(`.fg-item-results .fg-product-card[data-item-code="${css_escape(item_code)}"]`);
		if (!$card.length) return;
		const qty = this.cart_qty(item_code);
		$card.toggleClass("fg-product-card--in-cart", qty > 0);
		$card.find(".fg-stepper-input").val(qty);
		$card.find(".fg-stepper-minus").prop("disabled", qty <= 0);
	}

	render_summary() {
		const $summary = this.$body.find(".fg-np-summary");
		const lines = Array.from(this.nc.cart.values());
		const total_units = lines.reduce((sum, l) => sum + flt(l.qty), 0);

		const lines_html = lines.length
			? lines.map((l) => this.render_cart_line(l)).join("")
			: `<div class="fg-empty fg-empty--sm">${__("Aún no has agregado productos.")}</div>`;

		$summary.html(`
			<div class="fg-np-summary-counts">
				${lines.length} ${lines.length === 1 ? __("referencia") : __("referencias")}
				&middot;
				${format_qty(total_units)} ${__("unidades")}
			</div>
			<div class="fg-cart-list">${lines_html}</div>
			<div class="fg-np-field">
				<label>${__("Válida hasta (opcional)")}</label>
				<input type="date" class="fg-valid-till-input" min="${frappe.datetime.nowdate()}" value="${frappe.utils.escape_html(this.nc.valid_till || "")}">
			</div>
			<div class="fg-np-field">
				<label>${__("Observaciones / condiciones (opcional)")}</label>
				<textarea class="fg-observations-input" rows="3" placeholder="${__(
					"Escribe observaciones o condiciones sobre esta cotización..."
				)}">${frappe.utils.escape_html(this.nc.terms || "")}</textarea>
			</div>
			<button type="button" class="fg-btn fg-btn--solid-primary fg-btn--lg fg-confirm-btn" disabled>
				${icon("check")} ${this.nc.editing_quotation_name ? __("GUARDAR CAMBIOS") : __("CREAR COTIZACIÓN")}
			</button>
		`);

		$summary.find(".fg-valid-till-input").on("change", (e) => {
			this.nc.valid_till = $(e.currentTarget).val();
		});
		$summary.find(".fg-observations-input").on("input", (e) => {
			this.nc.terms = $(e.currentTarget).val();
		});

		this.bind_cart_line_events();
		this.$body.find(".fg-confirm-btn").on("click", () => this.confirm_quotation());
		this.refresh_confirm_state();
	}

	render_cart_line(l) {
		return `
			<div class="fg-cart-line" data-item-code="${frappe.utils.escape_html(l.item_code)}">
				<div class="fg-cart-line-name">
					${frappe.utils.escape_html(l.item_name)} &times; ${format_qty(l.qty)} ${frappe.utils.escape_html(l.stock_uom || "")}
				</div>
				<div class="fg-cart-line-controls">
					<div class="fg-stepper fg-stepper--sm">
						<button type="button" class="fg-stepper-btn fg-stepper-minus">${icon("minus")}</button>
						<input type="number" inputmode="decimal" class="fg-stepper-input" value="${l.qty}" min="0">
						<button type="button" class="fg-stepper-btn fg-stepper-plus">${icon("plus")}</button>
					</div>
					<button type="button" class="fg-cart-line-remove" title="${__("Quitar")}">${icon("x", "fg-icon-sm")}</button>
				</div>
			</div>
		`;
	}

	bind_cart_line_events() {
		this.$body.find(".fg-cart-line").each((i, el) => {
			const $line = $(el);
			const item_code = $line.data("item-code");
			const $input = $line.find(".fg-stepper-input");

			$line.find(".fg-stepper-minus").on("click", () => this.set_cart_qty(item_code, this.cart_qty(item_code) - 1));
			$line.find(".fg-stepper-plus").on("click", () => this.set_cart_qty(item_code, this.cart_qty(item_code) + 1));
			$input.on("change", () => this.set_cart_qty(item_code, Math.max(flt($input.val()), 0)));
			$line.find(".fg-cart-line-remove").on("click", () => this.set_cart_qty(item_code, 0));
		});
	}

	refresh_confirm_state() {
		const can_confirm = !!this.nc.customer && this.nc.cart.size > 0;
		this.$body.find(".fg-confirm-btn").prop("disabled", !can_confirm || this.busy);
	}

	// -- Confirmar --------------------------------------------------------------

	// The ONE place a request body for create_and_submit_quotation() is
	// built. Every line is an explicit object literal with exactly
	// item_code and qty -- no other key is ever added here, so there is
	// nothing for a future edit to accidentally smuggle a price/discount/
	// tax field into. valid_till/terms are the only two other fields ever
	// sent, both non-economic, both optional.
	build_quotation_payload() {
		const items = Array.from(this.nc.cart.values())
			.filter((l) => flt(l.qty) > 0)
			.map((l) => ({ item_code: l.item_code, qty: l.qty }));

		return {
			customer: this.nc.customer ? this.nc.customer.name : null,
			items: items,
			valid_till: (this.nc.valid_till || "").trim() || undefined,
			terms: (this.nc.terms || "").trim() || undefined,
		};
	}

	confirm_quotation() {
		if (this.busy) return;

		const payload = this.build_quotation_payload();
		if (!payload.customer) {
			frappe.show_alert({ message: __("Selecciona un cliente antes de confirmar."), indicator: "orange" });
			return;
		}
		if (!payload.items.length) {
			frappe.show_alert({ message: __("Agrega al menos un producto antes de confirmar."), indicator: "orange" });
			return;
		}

		if (this.nc.editing_quotation_name) {
			// Commit 20.6: "GUARDAR CAMBIOS" never submits -- straight to
			// update_draft_quotation(), no confirmation dialog (matches
			// ordinary "save" conventions, same as save_draft_edit() in
			// Ventas' own Commit 18.5).
			this.save_draft_edit(payload);
			return;
		}

		this.busy = true;
		const $btn = this.$body.find(".fg-confirm-btn").prop("disabled", true).addClass("fg-btn--loading");

		frappe.confirm(
			__("¿Confirmas la creación de esta cotización?"),
			() => {
				this.call("create_and_submit_quotation", payload)
					.then((result) => {
						frappe.show_alert(
							{
								message: `${icon("check", "fg-icon-sm")} ${__("Cotización creada correctamente")} — ${frappe.utils.escape_html(
									result.name
								)}`,
								indicator: "green",
							},
							7
						);
						this.back_to_dashboard();
					})
					.catch(() => {
						// The server already showed the exact validation error via its
						// own default frappe.call error dialog -- nothing entered here
						// is lost, the user can correct and retry.
					})
					.finally(() => {
						this.busy = false;
						$btn.prop("disabled", false).removeClass("fg-btn--loading");
						this.refresh_confirm_state();
					});
			},
			() => {
				this.busy = false;
				$btn.prop("disabled", false).removeClass("fg-btn--loading");
			}
		);
	}

	// Commit 20.6: update_draft_quotation() never submits -- the Quotation
	// stays exactly whatever docstatus it already was (Draft, since the
	// server itself rejects editing anything else). No price is ever sent
	// or read back here, same as every other call in this module.
	save_draft_edit(payload) {
		this.busy = true;
		const $btn = this.$body.find(".fg-confirm-btn").prop("disabled", true).addClass("fg-btn--loading");

		this.call("update_draft_quotation", {
			name: this.nc.editing_quotation_name,
			customer: payload.customer,
			items: payload.items,
			valid_till: payload.valid_till,
			terms: payload.terms,
		})
			.then((result) => {
				frappe.show_alert(
					{
						message: `${icon("check", "fg-icon-sm")} ${__("Cambios guardados")} — ${frappe.utils.escape_html(
							result.name
						)}`,
						indicator: "green",
					},
					5
				);
				this.back_to_dashboard();
			})
			.catch(() => {
				// same reasoning as confirm_quotation()'s own .catch() -- the
				// server's default error dialog already showed the real message.
			})
			.finally(() => {
				this.busy = false;
				$btn.prop("disabled", false).removeClass("fg-btn--loading");
				this.refresh_confirm_state();
			});
	}
};

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, no server calls, no state.
// Intentionally duplicated (not imported) from ventas.js/bodega.js/
// jefe_de_bodega.js, same reasoning as Commit 6: a few lines each, zero
// business logic, keeps this Page's asset loading independent of theirs.
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

function css_escape(v) {
	return window.CSS && CSS.escape ? CSS.escape(v) : String(v).replace(/["\\]/g, "\\$&");
}

// Pure presentation mapping of the native Quotation.status values (Draft,
// Open, Replied, Partially Ordered, Ordered, Lost, Cancelled, Expired --
// confirmed against quotation.json during the Fase 5 audit) to a Spanish
// label + badge color. Never changes which quotations are counted where --
// that is entirely get_quotation_summary()'s job on the server.
function quotation_status_meta(status) {
	const map = {
		Draft: { label: __("Borrador"), mod: "qtn-draft" },
		Open: { label: __("Pendiente"), mod: "qtn-open" },
		Replied: { label: __("Respondida"), mod: "qtn-replied" },
		"Partially Ordered": { label: __("Parcialmente pedida"), mod: "qtn-partial" },
		Ordered: { label: __("Aprobada"), mod: "qtn-ordered" },
		Lost: { label: __("Perdida"), mod: "qtn-lost" },
		Cancelled: { label: __("Cancelada"), mod: "qtn-cancelled" },
		Expired: { label: __("Vencida"), mod: "qtn-expired" },
	};
	return map[status] || { label: status || "—", mod: "qtn-draft" };
}
