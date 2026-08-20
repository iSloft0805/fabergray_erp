// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["ventas"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Ventas"),
		single_column: true,
	});
	new fabergray_erp.Ventas(page);
};

// All server communication in this file goes through fabergray_erp.api.ventas.* --
// nothing here computes pricing, calls the Fulfillment Engine directly, or reads/
// writes Pick List/Reporte de Faltante (Vendedora has no permission on either,
// Commit 18.1). Commit 18.3 renders exactly what those six endpoints return --
// no economic field (rate/price_list_rate/discount/amount/taxes/grand_total or
// any equivalent) is ever read from a server response or constructed here. The
// only payload this file ever sends to create_and_submit_sales_order() is built
// by build_order_payload() below, which is the single place a request body is
// assembled -- read that function before touching anything related to "Nuevo
// pedido".
fabergray_erp.Ventas = class Ventas {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.ventas.";
		this.busy = false;

		// Dashboard data (view: "dashboard").
		this.summary = null;
		this.orders = null;
		this.order_filter = null; // null | "pedidos_hoy" | "pendientes" | "entregados" | "cancelados"

		// "Nuevo pedido" (view: "nuevo_pedido") working state -- reset every
		// time open_nuevo_pedido() runs, never persisted across pedidos.
		this.np = this.blank_nuevo_pedido_state();
		this._customer_search_seq = 0;
		this._item_search_seq = 0;
		this._item_info_cache = new Map(); // item_code -> get_item_info() response

		this.state = { view: "dashboard" };

		this.$app = $('<div class="fg-shell fg-ventas">').appendTo(this.page.body);
		this.render_shell();
		this.load_dashboard();
	}

	blank_nuevo_pedido_state() {
		return {
			customer: null, // {name, customer_name}
			cart: new Map(), // item_code -> {item_code, item_name, description, stock_uom, image, qty_disponible, qty}
			customer_results: [],
			item_results: [],
		};
	}

	// -------------------------------------------------------------------
	// Thin API wrapper -- the only place that talks to the server.
	// -------------------------------------------------------------------
	call(method, args) {
		return frappe.call({ method: this.method_prefix + method, args: args || {} }).then((r) => r.message);
	}

	// -------------------------------------------------------------------
	// Shell: header (logo, title, user, refresh) stays fixed across views.
	// -------------------------------------------------------------------
	render_shell() {
		const fullname = frappe.session.user_fullname || frappe.session.user;
		this.$app.html(`
			<div class="fg-header">
				<div class="fg-header-brand">
					<span class="fg-header-logo">FABRIGRAY</span>
					<span class="fg-header-sep">|</span>
					<span class="fg-header-title">${__("Ventas")}</span>
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
		return Promise.all([this.call("get_sales_summary"), this.call("get_my_orders")])
			.then(([summary, orders]) => {
				this.summary = summary;
				this.orders = orders;
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
				<button type="button" class="fg-btn fg-btn--solid-primary fg-btn--lg fg-new-order-btn">
					${icon("plus")} ${__("NUEVO PEDIDO")}
				</button>
			</div>
			<div class="fg-orders-section">${this.render_orders_section()}</div>
		`);
		this.bind_dashboard_events();
	}

	render_kpis() {
		const s = this.summary || {};
		const cards = [
			{ key: "pedidos_hoy", label: __("Pedidos de hoy"), sub: __("Pedidos creados hoy"), i: "calendar", mod: "ventas-hoy" },
			{ key: "pendientes", label: __("Pendientes"), sub: __("Por entregar o facturar"), i: "clock", mod: "ventas-pendientes" },
			{ key: "entregados", label: __("Entregados"), sub: __("Completados"), i: "check", mod: "ventas-entregados" },
			{ key: "cancelados", label: __("Cancelados"), sub: __("Pedidos cancelados"), i: "x", mod: "ventas-cancelados" },
		];

		const html = cards
			.map(
				(c) => `
				<button type="button" class="fg-kpi fg-kpi--${c.mod} ${this.order_filter === c.key ? "is-active" : ""}" data-filter="${c.key}">
					<div class="fg-kpi-icon">${icon(c.i)}</div>
					<div class="fg-kpi-number">${s[c.key] ?? 0}</div>
					<div class="fg-kpi-label">${c.label}</div>
					<div class="fg-kpi-sub">${c.sub}</div>
					<span class="fg-kpi-link">${__("Ver pedidos")} ${icon("chevron-right", "fg-icon-sm")}</span>
				</button>
			`
			)
			.join("");

		return `<div class="fg-kpis fg-kpis--ventas">${html}</div>`;
	}

	// Mirrors get_sales_summary()'s own server-side bucket rules exactly
	// (fabergray_erp/api/ventas.py) -- re-applied client-side only to filter
	// the already-fetched get_my_orders() list, never to compute a KPI number
	// itself (that number always comes straight from get_sales_summary()).
	order_matches_filter(o, filter) {
		if (!filter) return true;
		if (filter === "pedidos_hoy") return o.transaction_date === frappe.datetime.nowdate();
		if (filter === "pendientes") return ["To Deliver and Bill", "To Deliver"].includes(o.status);
		if (filter === "entregados") return o.status === "Completed";
		if (filter === "cancelados") return o.status === "Cancelled";
		return true;
	}

	render_orders_section() {
		const all = this.orders || [];
		const list = all.filter((o) => this.order_matches_filter(o, this.order_filter));

		const filter_labels = {
			pedidos_hoy: __("Pedidos de hoy"),
			pendientes: __("Pendientes"),
			entregados: __("Entregados"),
			cancelados: __("Cancelados"),
		};
		const chip = this.order_filter
			? `
				<div class="fg-filter-chip">
					${__("Filtro")}: <strong>${filter_labels[this.order_filter]}</strong>
					<button type="button" class="fg-filter-chip-clear">${icon("x", "fg-icon-sm")}</button>
				</div>`
			: "";

		const cards = list.length
			? list.map((o) => this.render_order_card(o)).join("")
			: `<div class="fg-empty">${__("No tienes pedidos para mostrar.")}</div>`;

		return `
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Mis pedidos")}</div>
			</div>
			${chip}
			<div class="fg-order-list">${cards}</div>
		`;
	}

	render_order_card(o) {
		const status = status_meta(o.status);
		const customer_label = frappe.utils.escape_html(o.customer_name || o.customer || "—");
		const entrega = o.delivery_date ? frappe.datetime.str_to_user(o.delivery_date) : "—";
		const obs = o.observations
			? `<div class="fg-order-card-obs">${icon("file-text", "fg-icon-sm")} ${frappe.utils.escape_html(o.observations)}</div>`
			: "";

		return `
			<div class="fg-order-card">
				<div class="fg-order-card-top">
					<div class="fg-order-card-id">#${frappe.utils.escape_html(o.name)}</div>
					<span class="fg-badge fg-badge--${status.mod}">${status.label}</span>
				</div>
				<div class="fg-order-card-customer">${icon("user", "fg-icon-sm")} ${customer_label}</div>
				<div class="fg-order-card-meta">
					<span>${icon("calendar", "fg-icon-sm")} ${frappe.datetime.str_to_user(o.transaction_date)}</span>
					<span>${icon("truck", "fg-icon-sm")} ${__("Entrega")}: ${entrega}</span>
				</div>
				<div class="fg-order-card-counts">
					<span>${o.item_count} ${o.item_count === 1 ? __("referencia") : __("referencias")}</span>
					<span>${format_qty(o.total_qty)} ${__("unidades")}</span>
				</div>
				${obs}
			</div>
		`;
	}

	bind_dashboard_events() {
		this.$body.find(".fg-new-order-btn").on("click", () => this.open_nuevo_pedido());

		this.$body.find(".fg-kpi[data-filter]").on("click", (e) => {
			const key = $(e.currentTarget).data("filter");
			this.order_filter = this.order_filter === key ? null : key;
			this.$body.find(".fg-orders-section").html(this.render_orders_section());
			this.bind_orders_section_events();
			this.$body.find(".fg-kpi").removeClass("is-active");
			if (this.order_filter) this.$body.find(`.fg-kpi[data-filter="${this.order_filter}"]`).addClass("is-active");
			document.querySelector(".fg-orders-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
		});

		this.bind_orders_section_events();
	}

	bind_orders_section_events() {
		this.$body.find(".fg-filter-chip-clear").on("click", () => {
			this.order_filter = null;
			this.$body.find(".fg-kpi").removeClass("is-active");
			this.$body.find(".fg-orders-section").html(this.render_orders_section());
			this.bind_orders_section_events();
		});
	}

	// =====================================================================
	// Nuevo pedido
	// =====================================================================
	open_nuevo_pedido() {
		this.np = this.blank_nuevo_pedido_state();
		this._item_info_cache = new Map(); // fresh availability per pedido, not stale across sessions
		this.state.view = "nuevo_pedido";
		this.set_busy(false);
		this.render_nuevo_pedido();
		this.search_customers("");
		this.search_items("");
	}

	back_to_dashboard() {
		this.load_dashboard();
	}

	render_nuevo_pedido() {
		this.$body.html(`
			<div class="fg-np-header">
				<button type="button" class="fg-np-back">${icon("arrow-left")} ${__("Volver")}</button>
				<div class="fg-np-title">${__("Nuevo pedido")}</div>
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
				<div class="fg-np-section-title">${__("3. Resumen del pedido")}</div>
				<div class="fg-np-summary"></div>
			</div>
		`);
		this.render_customer_area();
		this.render_item_results_skeleton();
		this.render_summary();
		this.bind_nuevo_pedido_events();
	}

	render_item_results_skeleton() {
		this.$body.find(".fg-item-results").html(`
			<div class="fg-skeleton fg-product-skeleton"></div>
			<div class="fg-skeleton fg-product-skeleton"></div>
			<div class="fg-skeleton fg-product-skeleton"></div>
			<div class="fg-skeleton fg-product-skeleton"></div>
		`);
	}

	bind_nuevo_pedido_events() {
		this.$body.find(".fg-np-back").on("click", () => this.back_to_dashboard());

		const $item_input = this.$body.find(".fg-item-search-input");
		const debounced_item_search = frappe.utils.debounce((txt) => this.search_items(txt), 300);
		$item_input.on("input", (e) => debounced_item_search($(e.currentTarget).val()));
	}

	// -- Paso 1: Cliente -----------------------------------------------------

	render_customer_area() {
		const $area = this.$body.find(".fg-np-customer-area");
		if (this.np.customer) {
			$area.html(`
				<div class="fg-selected-chip">
					${icon("user", "fg-icon-sm")}
					<span>${frappe.utils.escape_html(this.np.customer.customer_name)}</span>
					<button type="button" class="fg-chip-remove" title="${__("Cambiar cliente")}">${icon("x", "fg-icon-sm")}</button>
				</div>
			`);
			$area.find(".fg-chip-remove").on("click", () => {
				this.np.customer = null;
				this.render_customer_area();
				this.search_customers("");
				this.refresh_confirm_state();
			});
			return;
		}

		$area.html(`
			<div class="fg-search-box">
				${icon("search")}
				<input type="text" class="fg-search-input fg-customer-search-input" placeholder="${__("Buscar cliente...")}">
			</div>
			<div class="fg-search-dropdown"></div>
		`);
		this.render_customer_dropdown();

		const $input = $area.find(".fg-customer-search-input");
		const debounced = frappe.utils.debounce((txt) => this.search_customers(txt), 300);
		$input.on("input", (e) => debounced($(e.currentTarget).val()));
		$input.on("focus", () => {
			if (this.np.customer_results.length) $area.find(".fg-search-dropdown").addClass("is-open");
		});
	}

	search_customers(txt) {
		const seq = ++this._customer_search_seq;
		return this.call("search_customers", { txt: txt || "" }).then((results) => {
			if (seq !== this._customer_search_seq || this.np.customer) return;
			this.np.customer_results = results || [];
			this.render_customer_dropdown();
		});
	}

	render_customer_dropdown() {
		const $dropdown = this.$body.find(".fg-search-dropdown");
		if (!$dropdown.length) return;

		const results = this.np.customer_results;
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
			this.np.customer = found;
			this.render_customer_area();
			this.refresh_confirm_state();
		});
	}

	// -- Paso 2: Productos ----------------------------------------------------

	search_items(txt) {
		const seq = ++this._item_search_seq;
		this.render_item_results_skeleton();
		return this.call("search_items", { txt: txt || "" }).then((results) => {
			if (seq !== this._item_search_seq) return;
			this.np.item_results = results || [];
			return this.hydrate_item_availability(this.np.item_results).then(() => {
				if (seq !== this._item_search_seq) return;
				this.render_item_results();
			});
		});
	}

	// get_item_info() is per-item (Commit 18.2 API) -- fetched in parallel for
	// every currently-displayed search result (bounded to at most 20 rows,
	// search_items()'s own limit) and cached by item_code for this "Nuevo
	// pedido" session so re-searching the same text doesn't refetch.
	hydrate_item_availability(results) {
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
		const results = this.np.item_results;

		if (!results.length) {
			$results.html(`<div class="fg-empty">${__("No se encontraron productos.")}</div>`);
			return;
		}

		$results.html(results.map((r) => this.render_item_result_card(r)).join(""));
		this.bind_item_result_events();
	}

	render_item_result_card(r) {
		const info = this._item_info_cache.get(r.item_code);
		const disponible = info && info.qty_disponible != null ? format_qty(info.qty_disponible) : "—";
		const disponible_class = info && flt(info.qty_disponible) > 0 ? "fg-product-avail--ok" : "fg-product-avail--zero";
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
						<span>${frappe.utils.escape_html(r.stock_uom || "")}</span>
						<span class="${disponible_class}">${__("Disponible")}: ${disponible}</span>
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
		const line = this.np.cart.get(item_code);
		return line ? line.qty : 0;
	}

	set_cart_qty(item_code, qty) {
		qty = flt(qty);
		if (qty <= 0) {
			this.np.cart.delete(item_code);
		} else {
			const result = this.np.item_results.find((r) => r.item_code === item_code);
			const info = this._item_info_cache.get(item_code);
			const existing = this.np.cart.get(item_code);
			this.np.cart.set(item_code, {
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

	// Updates only the one affected product card's stepper (if it is currently
	// rendered in the search results) instead of re-rendering the whole grid --
	// keeps the search input focused and untouched while tapping +/-.
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
		const lines = Array.from(this.np.cart.values());
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
			<div class="fg-np-observaciones">
				<label>${__("Observaciones (opcional)")}</label>
				<textarea class="fg-observations-input" rows="3" placeholder="${__(
					"Escribe observaciones sobre este pedido..."
				)}">${frappe.utils.escape_html(this.np.observations || "")}</textarea>
			</div>
			<button type="button" class="fg-btn fg-btn--solid-primary fg-btn--lg fg-confirm-btn" disabled>
				${icon("check")} ${__("CONFIRMAR PEDIDO")}
			</button>
		`);

		$summary.find(".fg-observations-input").on("input", (e) => {
			this.np.observations = $(e.currentTarget).val();
		});

		this.bind_cart_line_events();
		this.$body.find(".fg-confirm-btn").on("click", () => this.confirm_order());
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
		const can_confirm = !!this.np.customer && this.np.cart.size > 0;
		this.$body.find(".fg-confirm-btn").prop("disabled", !can_confirm || this.busy);
	}

	// -- Confirmar --------------------------------------------------------------

	// The ONE place a request body for create_and_submit_sales_order() is
	// built. Every line is an explicit object literal with exactly item_code
	// and qty -- no other key is ever added here, so there is nothing for a
	// future edit to accidentally smuggle a price/discount/tax field into.
	build_order_payload() {
		const items = Array.from(this.np.cart.values())
			.filter((l) => flt(l.qty) > 0)
			.map((l) => ({ item_code: l.item_code, qty: l.qty }));

		return {
			customer: this.np.customer ? this.np.customer.name : null,
			items: items,
			observations: (this.np.observations || "").trim() || undefined,
		};
	}

	confirm_order() {
		if (this.busy) return;

		const payload = this.build_order_payload();
		if (!payload.customer) {
			frappe.show_alert({ message: __("Selecciona un cliente antes de confirmar."), indicator: "orange" });
			return;
		}
		if (!payload.items.length) {
			frappe.show_alert({ message: __("Agrega al menos un producto antes de confirmar."), indicator: "orange" });
			return;
		}

		this.busy = true;
		const $btn = this.$body.find(".fg-confirm-btn").prop("disabled", true).addClass("fg-btn--loading");

		frappe.confirm(
			__("¿Confirmas la creación de este pedido?"),
			() => {
				this.call("create_and_submit_sales_order", payload)
					.then((result) => {
						frappe.show_alert(
							{
								message: `${icon("check", "fg-icon-sm")} ${__("Pedido creado correctamente")} — ${__(
									"Pedido"
								)} #${frappe.utils.escape_html(result.name)}`,
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
};

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, no server calls, no state.
// Intentionally duplicated (not imported) from bodega.js/jefe_de_bodega.js,
// same reasoning as Commit 6: a few lines each, zero business logic, keeps
// this Page's asset loading independent of theirs.
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

// Pure presentation mapping of the native Sales Order.status values
// (Draft, On Hold, To Pay, To Deliver and Bill, To Bill, To Deliver,
// Completed, Cancelled, Closed -- confirmed against sales_order.json during
// the Commit 18 design phase) to a Spanish label + badge color. Never
// changes which orders are counted where -- that is entirely
// get_sales_summary()'s job on the server.
function status_meta(status) {
	const map = {
		Draft: { label: __("Borrador"), mod: "so-draft" },
		"On Hold": { label: __("En espera"), mod: "so-onhold" },
		"To Pay": { label: __("Por pagar"), mod: "so-topay" },
		"To Deliver and Bill": { label: __("Por entregar y facturar"), mod: "so-todeliverbill" },
		"To Bill": { label: __("Por facturar"), mod: "so-tobill" },
		"To Deliver": { label: __("Por entregar"), mod: "so-todeliver" },
		Completed: { label: __("Entregado"), mod: "so-completed" },
		Cancelled: { label: __("Cancelado"), mod: "so-cancelled" },
		Closed: { label: __("Cerrado"), mod: "so-closed" },
	};
	return map[status] || { label: status || "—", mod: "so-draft" };
}
