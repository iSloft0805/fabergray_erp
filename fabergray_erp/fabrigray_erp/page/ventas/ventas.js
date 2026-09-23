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
// writes Pick List/Reporte de Faltante/Material Request (Vendedora has no
// permission on any of those, Commit 18.1). Commit 18.3/18.4/18.5/18.5b render
// exactly what those fourteen endpoints return -- no economic field (rate/
// price_list_rate/discount/amount/taxes/grand_total or any equivalent) is ever
// read from a server response or constructed here. The only payload this file
// ever sends to create_and_submit_sales_order()/update_draft_sales_order()/
// modify_submitted_sales_order() is built by build_order_payload() below, which
// is the single place a request body is assembled -- read that function before
// touching anything related to "Nuevo pedido"/"Editar pedido"/"Modificar pedido".
fabergray_erp.Ventas = class Ventas {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.ventas.";
		this.busy = false;

		// Dashboard data (view: "dashboard").
		this.summary = null;
		this.orders = null; // active orders only (get_my_orders(view="active")) -- never includes docstatus=2
		// Commit 25.10 -- a SEPARATE list, fetched lazily (get_my_orders(view="cancelled"))
		// the first time "Cancelados" is opened, never mixed into `this.orders`
		// above. `null` here specifically means "not fetched yet" -- distinct
		// from `[]` ("fetched, there are none") -- see set_order_filter().
		this.cancelled_orders = null;
		this.order_filter = null; // null | "pedidos_hoy" | "pendientes" | "entregados" | "cancelados"
		// Commit 25.20 -- free-text search, ALWAYS applied AFTER order_filter
		// (section 12's own explicit "la búsqueda debe aplicarse DESPUÉS del
		// filtro seleccionado") -- never a replacement for it.
		this.order_search = "";

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
			editing_order_name: null, // Commit 18.5: null -> "Nuevo pedido"; a Draft Sales Order name -> "Editar pedido"
			modifying_order_name: null, // Commit 18.5b: a Submitted Sales Order name -> "Modificar pedido"
			customer: null, // {name, customer_name}
			cart: new Map(), // item_code -> {item_code, item_name, description, stock_uom, image, qty_disponible, qty}
			customer_results: [],
			item_results: [],
			observations: "",
			// Commit 25.8.6 -- "2. Agregar productos" now has two input modes,
			// sharing the ONE cart above -- neither mode owns its own copy of
			// "what's in the order". "manual" is always the default per pedido
			// (brief section 2); switching modes never clears the other mode's
			// own state (quick_order below), only which one is currently shown.
			item_mode: "manual", // "manual" | "quick_order"
			quick_order: this.blank_quick_order_state(),
		};
	}

	// Commit 25.8.6 -- "Pedido rápido" working state, entirely client-side
	// (brief section 5: "NO persistir nada todavía") -- reset by
	// open_nuevo_pedido() (via blank_nuevo_pedido_state() above) and by
	// clear_quick_order() (explicit "Limpiar pedido rápido" or after a
	// successful apply_quick_order_to_cart()), never anywhere else. `lines`
	// is empty until interpret_quick_order() succeeds; each entry there is
	// this.build_quick_order_line_state()'s own shape -- see that function's
	// own docstring for exactly what's server data vs. client-only selection
	// state.
	blank_quick_order_state() {
		return { text: "", lines: [], loading: false };
	}

	// -------------------------------------------------------------------
	// Thin API wrapper -- the only place that talks to the server.
	// -------------------------------------------------------------------
	call(method, args) {
		return frappe.call({ method: this.method_prefix + method, args: args || {} }).then((r) => r.message);
	}

	// -------------------------------------------------------------------
	// Shell: header (logo, title, user, refresh) stays fixed across views.
	//
	// Refresh button: real SPA refresh -- click -> re-call the real
	// endpoint -> replace local state with the fresh response -> render.
	// page/bodega/bodega.js's own .fg-refresh-btn follows this identical
	// pattern (see its own render_shell() comment), just covering one more
	// view (its detail/picking screen); this one only ever refreshes the
	// dashboard, disabling itself outside it (see set_busy() below).
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
	// Commit 25.10 -- always refreshes the active list; also refreshes
	// `this.cancelled_orders` if "Cancelados" was already opened at least
	// once this session (never fetches it for the first time here -- see
	// set_order_filter(), the one place that lazy-load happens), so a
	// manual refresh while looking at Cancelados doesn't show stale data.
	load_dashboard() {
		this.set_busy(true);
		this.state.view = "dashboard";
		this.render_skeleton_dashboard();

		// Commit 25.20 -- `limit: 500` (was the server-side default of 50):
		// the search bar below filters `this.orders` client-side, over
		// whatever this call actually fetched -- a search that silently
		// only ever looked at the first 50 orders would be exactly the
		// "búsqueda incompleta" section 8 explicitly warns against. 500 is
		// a generous ceiling for this app's own real scale (never claimed
		// as literally unlimited); get_my_orders() itself is unchanged.
		const calls = [
			this.call("get_sales_summary"),
			this.call("get_my_orders", { view: "active", limit: 500 }),
		];
		if (this.cancelled_orders !== null) {
			calls.push(this.call("get_my_orders", { view: "cancelled", limit: 500 }));
		}

		return Promise.all(calls)
			.then(([summary, orders, cancelled_orders]) => {
				this.summary = summary;
				this.orders = orders;
				if (cancelled_orders !== undefined) this.cancelled_orders = cancelled_orders;
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
			{ key: "pendientes", label: __("Pendientes"), sub: __("Pedidos por completar"), i: "clock", mod: "ventas-pendientes" },
			// Commit 25.18 -- "en_ruta"/"entregados_logistica" (Recorridos),
			// deliberately distinct keys from the native "entregados" card
			// below (a different, ERPNext-internal concept, kept unchanged --
			// section 3's own "mantener los KPIs actuales"). Sub-labels call
			// out the distinction explicitly so the two "Entregados"-sounding
			// cards are never confused with each other.
			{ key: "en_ruta", label: __("En ruta"), sub: __("Despachados"), i: "truck", mod: "ventas-en-ruta" },
			{
				key: "entregados_logistica",
				label: __("Entregados"),
				sub: __("Confirmado en recorrido"),
				i: "check-circle",
				mod: "ventas-entregados-logistica",
			},
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
	// Commit 25.10 -- "cancelados" is deliberately NOT one of the branches
	// here anymore: it is never a client-side filter over `this.orders`
	// (which, since this commit, is fetched with view="active" and can
	// never contain a docstatus=2 order to begin with -- see
	// load_dashboard()) -- it is its own separate list, `this.cancelled_orders`,
	// switched to by render_orders_section()/set_order_filter() below. The
	// three real filters here all apply to the active list only, which is
	// what already keeps a cancelled-today order from ever matching
	// "pedidos_hoy" -- there is no cancelled order left in `this.orders` for
	// it to match against.
	order_matches_filter(o, filter) {
		if (!filter) return true;
		if (filter === "pedidos_hoy") return o.transaction_date === frappe.datetime.nowdate();
		if (filter === "pendientes") return ["To Deliver and Bill", "To Deliver"].includes(o.status);
		if (filter === "entregados") return o.status === "Completed";
		// Commit 25.18 -- "en_ruta"/"entregados_logistica" read `o.
		// logistics_status` directly, the exact value get_sales_summary()'s
		// own per-order classification already produced server-side
		// (_resolve_sales_order_logistics_status(), api/ventas.py) -- never
		// re-derived from `o.status`/any other native field here. Section
		// 15's own explicit "frontend no inventa estado desde Sales Order.
		// status".
		if (filter === "en_ruta") return o.logistics_status === "IN_ROUTE";
		if (filter === "entregados_logistica") return o.logistics_status === "DELIVERED";
		return true;
	}

	// Commit 25.20 -- client-side (this.orders is already fully fetched,
	// limit: 500, see load_dashboard()) -- the ONE shared matcher
	// (fg_search.js, globally loaded) every operational Page's own search
	// bar calls, never a bespoke haystack built here. `customer`/
	// `customer_name` cover section 5's "cliente" (both, so a raw Customer
	// ID still matches even without its display name); `transaction_date`
	// is Ventas' own real operational date, section 6.
	order_matches_search(o) {
		return fabergray_erp.search.matches_operational_search(o, this.order_search, {
			text_fields: ["customer_name", "customer", "name", "commercial_name"],
			date_fields: ["transaction_date"],
		});
	}

	// Commit 25.20 -- split out from render_orders_section() so the search
	// `input` handler (bind_orders_section_events()) can re-render just
	// this part, never the search bar itself -- see render_search_bar_
	// html()'s own comment for why that matters.
	render_orders_results_html() {
		const is_cancelled_view = this.order_filter === "cancelados";
		const all = is_cancelled_view ? this.cancelled_orders || [] : this.orders || [];
		const filtered = is_cancelled_view ? all : all.filter((o) => this.order_matches_filter(o, this.order_filter));
		// Section 12 -- search applies AFTER the existing filter, never
		// instead of it.
		const list = filtered.filter((o) => this.order_matches_search(o));

		const filter_labels = {
			pedidos_hoy: __("Pedidos de hoy"),
			pendientes: __("Pendientes"),
			entregados: __("Entregados"),
			cancelados: __("Cancelados"),
			en_ruta: __("En ruta"),
			entregados_logistica: __("Entregados (recorrido)"),
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
			: this.order_search
				? render_search_empty_html()
				: `<div class="fg-empty">${__("No tienes pedidos para mostrar.")}</div>`;

		return `${chip}<div class="fg-order-list">${cards}</div>`;
	}

	render_orders_section() {
		return `
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Pedidos")}</div>
			</div>
			${render_search_bar_html(this.order_search)}
			<div class="fg-orders-results">${this.render_orders_results_html()}</div>
		`;
	}

	render_order_card(o) {
		const status = status_meta(o.status);
		const customer_label = frappe.utils.escape_html(o.customer_name || o.customer || "—");
		const entrega = o.delivery_date ? frappe.datetime.str_to_user(o.delivery_date) : "—";
		const obs = o.observations
			? `<div class="fg-order-card-obs">${icon("file-text", "fg-icon-sm")} ${frappe.utils.escape_html(o.observations)}</div>`
			: "";
		// Commit 25.17 -- native Quotation<->Sales Order link
		// (`o.quotation`, get_my_orders()/get_order_detail(), read off
		// Sales Order Item's own `prevdoc_docname`, set by ERPNext's
		// native `make_sales_order()` mapper) -- `null`/absent for the
		// overwhelming majority of orders (built directly, never from a
		// Quotation), same "nothing to show -> nothing rendered"
		// convention `obs` above already establishes.
		const quotation_origin_html = o.quotation
			? `<div class="fg-order-card-quotation-origin">${icon("file-text", "fg-icon-sm")} ${__("Cotización")} #${frappe.utils.escape_html(o.quotation)}</div>`
			: "";
		const logistics = render_logistics_block(o);
		// Commit 25.12 -- only ever rendered for a genuinely cancelled card
		// (o.status === "Cancelled"); `o.cancellation_reason` is `null` for
		// any Sales Order cancelled before this commit, or cancelled
		// outside this app (native Desk) -- "No registrada" is shown for
		// that case, NEVER a guessed/inferred value (Commit 25.12 brief,
		// section 9). `cancellation_note` is genuinely optional even for a
		// cancellation made through this app (only mandatory when
		// `cancellation_reason === "Otro"`, enforced server-side in
		// cancel_sales_order()) -- so its own line is omitted entirely
		// when absent, matching `obs` above's own "nothing to show ->
		// nothing rendered" convention.
		const cancellation_html =
			o.status === "Cancelled"
				? `
					<div class="fg-order-card-cancellation">
						<div>${icon("x", "fg-icon-sm")} ${__("Razón")}: ${frappe.utils.escape_html(o.cancellation_reason || __("No registrada"))}</div>
						${
							o.cancellation_note
								? `<div>${__("Detalle")}: ${frappe.utils.escape_html(o.cancellation_note)}</div>`
								: ""
						}
					</div>
				`
				: "";

		return `
			<div class="fg-order-card">
				<div class="fg-order-card-top">
					<div class="fg-order-card-id">#${frappe.utils.escape_html(o.commercial_name || o.name)}</div>
					<span class="fg-badge fg-badge--${status.mod}">${status.label}</span>
					${logistics.badge_html}
				</div>
				<div class="fg-order-card-customer">${icon("user", "fg-icon-sm")} ${customer_label}</div>
				${quotation_origin_html}
				<div class="fg-order-card-meta">
					<span>${icon("calendar", "fg-icon-sm")} ${format_order_date_with_time(o.transaction_date, o.creation)}</span>
					<span>${icon("truck", "fg-icon-sm")} ${__("Entrega")}: ${entrega}</span>
				</div>
				<div class="fg-order-card-counts">
					<span>${o.item_count} ${o.item_count === 1 ? __("referencia") : __("referencias")}</span>
					<span>${format_qty(o.total_qty)} ${__("unidades")}</span>
				</div>
				${logistics.info_html}
				${obs}
				${cancellation_html}
				${this.render_order_card_actions(o)}
			</div>
		`;
	}

	// Commit 18.5: Draft -> VER/EDITAR/ELIMINAR; Cancelled -> VER only;
	// anything else (an active, submitted order) -> VER/MODIFICAR PEDIDO/
	// CANCELAR PEDIDO (Commit 18.5b adds MODIFICAR). `o.status` is the
	// native Sales Order.status string already returned by get_my_orders()
	// -- no new field needed to tell Draft/Cancelled/active apart.
	// `o.modifiable` (Commit 18.5b) is the same non-authoritative pre-check
	// get_modification_status() exposes standalone -- when false, the
	// button is omitted entirely rather than shown disabled, matching
	// ELIMINAR/EDITAR's own already-established "not applicable -> not
	// shown" convention for the other two states above. The real gate
	// still runs server-side regardless of what this button shows.
	render_order_card_actions(o) {
		const name_attr = `data-order-name="${frappe.utils.escape_html(o.name)}"`;
		const view_btn = `
			<button type="button" class="fg-order-card-action fg-order-card-view" ${name_attr}>
				${icon("eye", "fg-icon-sm")} ${__("VER")}
			</button>
		`;

		if (o.status === "Draft") {
			return `
				<div class="fg-order-card-actions">
					${view_btn}
					<button type="button" class="fg-order-card-action fg-order-card-edit" ${name_attr}>
						${icon("pencil", "fg-icon-sm")} ${__("EDITAR")}
					</button>
					<button type="button" class="fg-order-card-action fg-order-card-action--danger fg-order-card-delete" ${name_attr}>
						${icon("trash-2", "fg-icon-sm")} ${__("ELIMINAR")}
					</button>
				</div>
			`;
		}

		if (o.status === "Cancelled") {
			return `<div class="fg-order-card-actions">${view_btn}</div>`;
		}

		const modify_btn = o.modifiable
			? `
				<button type="button" class="fg-order-card-action fg-order-card-modify" ${name_attr}>
					${icon("pencil", "fg-icon-sm")} ${__("MODIFICAR PEDIDO")}
				</button>
			`
			: "";

		return `
			<div class="fg-order-card-actions">
				${view_btn}
				${modify_btn}
				<button type="button" class="fg-order-card-action fg-order-card-action--danger fg-order-card-cancel" ${name_attr}>
					${icon("x", "fg-icon-sm")} ${__("CANCELAR PEDIDO")}
				</button>
			</div>
		`;
	}

	bind_dashboard_events() {
		this.$body.find(".fg-new-order-btn").on("click", () => this.open_nuevo_pedido());

		this.$body.find(".fg-kpi[data-filter]").on("click", (e) => {
			const key = $(e.currentTarget).data("filter");
			this.set_order_filter(this.order_filter === key ? null : key);
		});

		this.bind_orders_section_events();
	}

	// Commit 25.10 -- the ONE place `this.order_filter` changes, for both the
	// KPI chips (bind_dashboard_events()) and the "clear filter" chip
	// (bind_orders_section_events()) -- consolidated so "Cancelados" gets
	// its lazy-load exactly once, from a single call site, instead of that
	// logic being duplicated (or forgotten) at a second one. Fetches
	// `this.cancelled_orders` from the server only the FIRST time
	// "Cancelados" is opened this session (`=== null` check -- an empty
	// array from a real, empty result never re-fetches); every other
	// filter change just re-renders from data already in memory.
	set_order_filter(filter) {
		this.order_filter = filter;
		this.$body.find(".fg-kpi").removeClass("is-active");
		if (filter) this.$body.find(`.fg-kpi[data-filter="${filter}"]`).addClass("is-active");

		if (filter === "cancelados" && this.cancelled_orders === null) {
			this.render_orders_section_loading();
			this.call("get_my_orders", { view: "cancelled" }).then((orders) => {
				this.cancelled_orders = orders || [];
				this.$body.find(".fg-orders-section").html(this.render_orders_section());
				this.bind_orders_section_events();
			});
			return;
		}

		this.$body.find(".fg-orders-section").html(this.render_orders_section());
		this.bind_orders_section_events();
		document.querySelector(".fg-orders-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
	}

	render_orders_section_loading() {
		this.$body.find(".fg-orders-section").html(`
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Pedidos")}</div>
			</div>
			<div class="fg-skeleton-cards">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
		`);
	}

	// Commit 25.20 -- bound ONCE per full render_orders_section() (never
	// re-bound by the search `input` handler itself, which only ever
	// touches .fg-orders-results -- re-binding this every keystroke would
	// stack N duplicate handlers after N keystrokes).
	bind_orders_search_events() {
		this.$body.find(".fg-search-input").on("input", (e) => {
			this.order_search = $(e.currentTarget).val();
			this.$body.find(".fg-search-clear").toggleClass("is-visible", !!this.order_search.trim());
			this.$body.find(".fg-orders-results").html(this.render_orders_results_html());
			this.bind_orders_results_events();
		});
		this.$body.find(".fg-search-clear").on("click", () => {
			this.order_search = "";
			this.$body.find(".fg-orders-section").html(this.render_orders_section());
			this.bind_orders_section_events();
		});
	}

	// Card actions + the filter chip's own clear button -- re-bound after
	// EVERY re-render of .fg-orders-results (both the search input's own
	// partial re-render and set_order_filter()'s full one), since those
	// DOM nodes are recreated each time.
	bind_orders_results_events() {
		this.$body.find(".fg-filter-chip-clear").on("click", () => this.set_order_filter(null));

		this.$body.find(".fg-order-card-view").on("click", (e) => {
			this.open_order_detail($(e.currentTarget).data("order-name"));
		});
		this.$body.find(".fg-order-card-edit").on("click", (e) => {
			this.open_edit_pedido($(e.currentTarget).data("order-name"));
		});
		this.$body.find(".fg-order-card-modify").on("click", (e) => {
			this.open_modify_pedido($(e.currentTarget).data("order-name"));
		});
		this.$body.find(".fg-order-card-delete").on("click", (e) => {
			this.confirm_delete_draft($(e.currentTarget).data("order-name"));
		});
		this.$body.find(".fg-order-card-cancel").on("click", (e) => {
			this.confirm_cancel_order($(e.currentTarget).data("order-name"));
		});
	}

	bind_orders_section_events() {
		this.bind_orders_search_events();
		this.bind_orders_results_events();
	}

	// =====================================================================
	// Eliminar Draft / Cancelar Submitted (Commit 18.5)
	// =====================================================================
	confirm_delete_draft(name) {
		if (!name) return;
		frappe.confirm(__("¿Eliminar este borrador?"), () => {
			this.call("delete_draft_sales_order", { name: name })
				.then(() => {
					frappe.show_alert({ message: __("Borrador eliminado."), indicator: "green" }, 5);
					this.load_dashboard();
				})
				.catch(() => {
					// the server's own frappe.call error dialog already showed the
					// real validation/permission error -- nothing more to do here.
				});
		});
	}

	// Commit 25.12 -- a reason is now mandatory to cancel a pedido, so a
	// plain frappe.confirm() (yes/no only) is no longer enough; this opens
	// a real frappe.ui.Dialog with a "Razón de cancelación" Select
	// (`reqd: 1`) + a "Detalle" Small Text (`mandatory_depends_on` for
	// `reason === "Otro"`, matching cancel_sales_order()'s own server-side
	// rule exactly). `reason_options` is read from the Sales Order
	// doctype's OWN meta (`fg_cancellation_reason`'s Custom Field
	// `options`) at dialog-open time via `frappe.model.with_doctype()` --
	// never a second, hand-typed copy of the enum here -- same pattern
	// page/bodega/bodega.js's own `open_report_shortage_dialog()` already
	// established for "Reporte de Faltante"'s `shortage_reason`. That
	// keeps this dialog and `CANCELLATION_REASONS` (api/ventas.py) from
	// ever silently drifting apart: whatever the fixture ships is exactly
	// what both the UI offers and the server accepts.
	//
	// Frappe's own `Dialog.get_values()` already refuses to invoke
	// `primary_action` at all while a `reqd`/`mandatory_depends_on` field
	// is empty (confirmed by reading frappe/public/js/frappe/ui/dialog.js
	// directly) -- so "sin razón" / "Otro sin detalle" never reach the
	// server through the framework alone. The two checks repeated at the
	// top of `primary_action` below are a deliberate, explicit second
	// line of defense (same "never trust framework wiring alone for a
	// hard requirement" convention `confirm_order()` already follows for
	// its own three validations) -- not there because the framework check
	// is known to be insufficient.
	//
	// Double-click protection is scoped to this dialog's own primary
	// button (`dialog.disable_primary_action()`/`enable_primary_action()`,
	// same convention as `open_report_shortage_dialog()`'s own "REPORTAR
	// FALTANTE" button) -- `this.busy`/`.fg-confirm-btn` (Commit 25.11:
	// ALWAYS enabled, no exceptions) are never touched by this function.
	confirm_cancel_order(name) {
		if (!name) return;

		frappe.model.with_doctype("Sales Order", () => {
			const reason_field = frappe.get_meta("Sales Order").fields.find((f) => f.fieldname === "fg_cancellation_reason");
			const reason_options = ["", ...(reason_field.options || "").split("\n").map((o) => o.trim()).filter(Boolean)];

			const dialog = new frappe.ui.Dialog({
				title: __("Cancelar pedido"),
				fields: [
					{
						fieldtype: "Select",
						fieldname: "reason",
						label: __("Razón de cancelación"),
						options: reason_options,
						reqd: 1,
					},
					{
						fieldtype: "Small Text",
						fieldname: "note",
						label: __("Detalle"),
						mandatory_depends_on: "eval:doc.reason === 'Otro'",
					},
				],
				primary_action_label: __("Confirmar cancelación"),
				primary_action: (values) => {
					if (!values.reason) {
						frappe.msgprint(__("Selecciona una razón de cancelación."));
						return;
					}
					if (values.reason === "Otro" && !(values.note || "").trim()) {
						frappe.msgprint(__('Escribe el detalle de la cancelación cuando la razón es "Otro".'));
						return;
					}

					dialog.disable_primary_action();
					this.call("cancel_sales_order", {
						name: name,
						reason: values.reason,
						note: (values.note || "").trim() || null,
					})
						.then(() => {
							dialog.hide();
							frappe.show_alert({ message: __("Pedido cancelado."), indicator: "green" }, 5);
							// Commit 25.10.1 -- explicit cache invalidation, not left as
							// an incidental side effect of load_dashboard()'s own "also
							// refetch cancelled_orders if it happened to be loaded
							// already" behaviour (which stays, unchanged, below -- this
							// is additive, not a replacement for it). Setting this back
							// to `null` (never a manually-appended array entry -- see
							// this commit's own report, section C, for why: docstatus=2
							// on the server stays the one authority) is what forces the
							// NEXT time "Cancelados" is opened to run a fresh
							// get_my_orders(view="cancelled") in set_order_filter(),
							// instead of possibly still holding whatever snapshot was
							// cached from before this cancellation.
							this.cancelled_orders = null;
							this.load_dashboard();
						})
						.catch(() => {
							// Native ERPNext blocks (submitted Pick List/Material Request/
							// Purchase Order still linked, etc.) surface here via the
							// server's own real error message -- never swallowed, never
							// bypassed, no manual cleanup attempted client-side.
							dialog.enable_primary_action();
						});
				},
				secondary_action_label: __("Volver"),
				secondary_action: () => dialog.hide(),
			});

			dialog.show();
		});
	}

	// =====================================================================
	// Detalle de pedido ("VER PEDIDO") -- operational only, same non-
	// economic allowlist as get_order_detail() (Commit 18.4). No line item
	// here ever carries rate/amount/price_list_rate/etc.
	// =====================================================================
	open_order_detail(name) {
		if (!name) return;
		this.render_order_detail_overlay(null, true);
		this.call("get_order_detail", { name: name })
			.then((detail) => this.render_order_detail_overlay(detail, false))
			.catch(() => this.close_order_detail());
	}

	render_order_detail_overlay(detail, loading) {
		this.$app.find(".fg-order-detail-overlay").remove();

		const $overlay = $('<div class="fg-order-detail-overlay"></div>').appendTo(this.$app);
		$overlay.on("mousedown", (e) => {
			if (e.target === $overlay[0]) this.close_order_detail();
		});

		if (loading) {
			$overlay.html(`
				<div class="fg-order-detail-panel">
					<div class="fg-skeleton fg-product-skeleton"></div>
					<div class="fg-skeleton fg-product-skeleton"></div>
				</div>
			`);
			return;
		}

		const status = status_meta(detail.status);
		const logistics = render_logistics_block(detail);
		const entrega = detail.delivery_date ? frappe.datetime.str_to_user(detail.delivery_date) : "—";
		const obs = detail.observations
			? `<div class="fg-order-detail-obs">${icon("file-text", "fg-icon-sm")} ${frappe.utils.escape_html(
					detail.observations
			  )}</div>`
			: "";
		const lines = (detail.items || [])
			.map(
				(l) => `
				<div class="fg-order-detail-line">
					<div class="fg-order-detail-line-info">
						<span class="fg-order-detail-line-name">${frappe.utils.escape_html(l.item_name)}</span>
						<span class="fg-order-detail-line-code">${frappe.utils.escape_html(l.item_code)}</span>
					</div>
					<span class="fg-order-detail-line-qty">${format_qty(l.qty)} ${frappe.utils.escape_html(l.stock_uom || "")}</span>
				</div>
			`
			)
			.join("");

		$overlay.html(`
			<div class="fg-order-detail-panel">
				<div class="fg-order-detail-header">
					<div class="fg-order-detail-id">#${frappe.utils.escape_html(detail.commercial_name || detail.name)}</div>
					<span class="fg-badge fg-badge--${status.mod}">${status.label}</span>
					${logistics.badge_html}
					<button type="button" class="fg-order-detail-close" title="${__("Cerrar")}">${icon("x")}</button>
				</div>
				<div class="fg-order-detail-customer">
					${icon("user", "fg-icon-sm")} ${frappe.utils.escape_html(detail.customer_name || detail.customer || "—")}
				</div>
				${
					detail.quotation
						? `<div class="fg-order-card-quotation-origin">${icon("file-text", "fg-icon-sm")} ${__("Cotización")} #${frappe.utils.escape_html(detail.quotation)}</div>`
						: ""
				}
				<div class="fg-order-detail-meta">
					<span>${icon("calendar", "fg-icon-sm")} ${format_order_date_with_time(detail.transaction_date, detail.creation)}</span>
					<span>${icon("truck", "fg-icon-sm")} ${__("Entrega")}: ${entrega}</span>
				</div>
				${logistics.info_html}
				${obs}
				<div class="fg-order-detail-section-title">${__("Productos")}</div>
				<div class="fg-order-detail-lines">
					${lines || `<div class="fg-empty fg-empty--sm">${__("Sin productos.")}</div>`}
				</div>
				<div class="fg-order-detail-footer">
					${detail.item_count} ${detail.item_count === 1 ? __("referencia") : __("referencias")}
					&middot;
					${format_qty(detail.total_qty)} ${__("unidades")}
				</div>
			</div>
		`);
		$overlay.find(".fg-order-detail-close").on("click", () => this.close_order_detail());
	}

	close_order_detail() {
		this.$app.find(".fg-order-detail-overlay").remove();
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
	}

	// Commit 18.5: reuses the exact same "Nuevo pedido" screen, prefilled
	// via get_editable_order() (server already enforces docstatus==0 --
	// only a Draft can ever reach this). Never submits on save -- see
	// save_draft_edit()/confirm_order() below.
	open_edit_pedido(name) {
		if (!name) return;
		this.np = this.blank_nuevo_pedido_state();
		this._item_info_cache = new Map();
		this.state.view = "nuevo_pedido";
		this.set_busy(true);

		this.call("get_editable_order", { name: name })
			.then((detail) => {
				this.np.editing_order_name = detail.name;
				this.np.customer = { name: detail.customer, customer_name: detail.customer_name };
				this.np.observations = detail.observations || "";
				for (const item of detail.items || []) {
					this.np.cart.set(item.item_code, {
						item_code: item.item_code,
						item_name: item.item_name,
						stock_uom: item.stock_uom,
						qty: item.qty,
					});
				}
				this.render_nuevo_pedido();
			})
			.catch(() => this.back_to_dashboard())
			.finally(() => this.set_busy(false));
	}

	// Commit 18.5b: same "Nuevo pedido" screen again, prefilled via
	// get_order_for_modification() -- which re-derives the authoritative
	// modification gate itself (never trusts o.modifiable, which only
	// decided whether this button rendered at all) and throws if the
	// order can no longer be modified; the .catch() below sends the
	// Vendedora back to the dashboard in that case, same as
	// open_edit_pedido()'s own failure handling.
	open_modify_pedido(name) {
		if (!name) return;
		this.np = this.blank_nuevo_pedido_state();
		this._item_info_cache = new Map();
		this.state.view = "nuevo_pedido";
		this.set_busy(true);

		this.call("get_order_for_modification", { name: name })
			.then((detail) => {
				this.np.modifying_order_name = detail.name;
				this.np.customer = { name: detail.customer, customer_name: detail.customer_name };
				this.np.observations = detail.observations || "";
				for (const item of detail.items || []) {
					this.np.cart.set(item.item_code, {
						item_code: item.item_code,
						item_name: item.item_name,
						stock_uom: item.stock_uom,
						qty: item.qty,
					});
				}
				this.render_nuevo_pedido();
			})
			.catch(() => this.back_to_dashboard())
			.finally(() => this.set_busy(false));
	}

	back_to_dashboard() {
		this.load_dashboard();
	}

	render_nuevo_pedido() {
		const editing = !!this.np.editing_order_name;
		const modifying = !!this.np.modifying_order_name;
		const title = modifying ? __("Modificar pedido") : editing ? __("Editar pedido") : __("Nuevo pedido");
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
				<div class="fg-item-mode-switch" role="tablist">
					<button type="button" class="fg-item-mode-btn ${this.np.item_mode === "manual" ? "is-active" : ""}" data-mode="manual" role="tab" aria-selected="${this.np.item_mode === "manual"}">
						${icon("search", "fg-icon-sm")} ${__("Buscar manualmente")}
					</button>
					<button type="button" class="fg-item-mode-btn ${this.np.item_mode === "quick_order" ? "is-active" : ""}" data-mode="quick_order" role="tab" aria-selected="${this.np.item_mode === "quick_order"}">
						${icon("clipboard-list", "fg-icon-sm")} ${__("Pedido rápido")}
					</button>
				</div>
				<div class="fg-item-mode-body"></div>
			</div>

			<div class="fg-np-section">
				<div class="fg-np-section-title">${__("3. Resumen del pedido")}</div>
				<div class="fg-np-summary"></div>
			</div>
		`);
		this.render_customer_area();
		this.render_item_mode_body();
		this.render_summary();
		this.bind_nuevo_pedido_events();
	}

	// Commit 25.8.6 -- dispatches "2. Agregar productos"' single body area to
	// whichever of the two input modes is currently active, WITHOUT losing
	// either mode's own state (this.np.item_results / this.np.quick_order
	// both live on this.np regardless of which one is rendered right now --
	// switching modes back and forth just re-renders from what's already
	// there, see blank_nuevo_pedido_state()'s own comment).
	render_item_mode_body() {
		const $wrap = this.$body.find(".fg-item-mode-body");
		if (this.np.item_mode === "quick_order") {
			$wrap.html(this.quick_order_panel_html());
			this.bind_quick_order_panel_events();
			this.render_quick_order_lines();
		} else {
			$wrap.html(`
				<div class="fg-search-box">
					${icon("search")}
					<input type="text" class="fg-search-box-input fg-item-search-input" placeholder="${__("Buscar producto...")}">
				</div>
				<div class="fg-item-results"></div>
			`);
			this.render_item_results_empty_prompt();
			this.bind_item_search_input();
		}
	}

	render_item_results_skeleton() {
		this.$body.find(".fg-item-results").html(`
			<div class="fg-skeleton fg-product-skeleton"></div>
			<div class="fg-skeleton fg-product-skeleton"></div>
			<div class="fg-skeleton fg-product-skeleton"></div>
			<div class="fg-skeleton fg-product-skeleton"></div>
		`);
	}

	// Initial state of "2. Agregar productos" -- no catalog preload (Commit
	// 18.4): the full Item list never renders until the Vendedora actually
	// types something into the search box.
	render_item_results_empty_prompt() {
		this.$body.find(".fg-item-results").html(`
			<div class="fg-empty">${__("Escribe para buscar productos")}</div>
		`);
	}

	bind_nuevo_pedido_events() {
		this.$body.find(".fg-np-back").on("click", () => this.back_to_dashboard());

		this.$body.find(".fg-item-mode-btn").on("click", (e) => {
			const mode = $(e.currentTarget).data("mode");
			if (mode === this.np.item_mode) return;
			this.np.item_mode = mode;
			this.$body.find(".fg-item-mode-btn").removeClass("is-active").attr("aria-selected", "false");
			$(e.currentTarget).addClass("is-active").attr("aria-selected", "true");
			this.render_item_mode_body();
		});
	}

	// Extracted out of bind_nuevo_pedido_events() (Commit 25.8.6) -- the
	// manual search box is now conditionally rendered (only when
	// this.np.item_mode === "manual"), so its own input listener has to be
	// (re)bound every time render_item_mode_body() puts it back on screen,
	// not just once when the whole "Nuevo pedido" screen first renders.
	bind_item_search_input() {
		const $item_input = this.$body.find(".fg-item-search-input");
		const debounced_item_search = frappe.utils.debounce((txt) => this.search_items(txt), 300);
		$item_input.on("input", (e) => {
			const txt = $(e.currentTarget).val();
			if (!txt || !txt.trim()) {
				// Clearing the box goes back to the empty prompt immediately --
				// no debounce, no server call, matching search_items()'s own guard.
				this._item_search_seq++; // invalidate any in-flight search
				this.np.item_results = [];
				this.render_item_results_empty_prompt();
				return;
			}
			debounced_item_search(txt);
		});
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
				this.np.customer_results = [];
				this.render_customer_area();
				this.refresh_confirm_state();
			});
			return;
		}

		// No catalog preload (Commit 18.4): just the search box, closed --
		// nothing is fetched until the Vendedora types something.
		$area.html(`
			<div class="fg-search-box">
				${icon("search")}
				<input type="text" class="fg-search-box-input fg-customer-search-input" placeholder="${__("Buscar cliente...")}">
			</div>
			<div class="fg-search-dropdown"></div>
		`);

		const $input = $area.find(".fg-customer-search-input");
		const debounced = frappe.utils.debounce((txt) => this.search_customers(txt), 300);
		$input.on("input", (e) => {
			const txt = $(e.currentTarget).val();
			if (!txt || !txt.trim()) {
				this._customer_search_seq++; // invalidate any in-flight search
				this.np.customer_results = [];
				this.render_customer_dropdown();
				return;
			}
			debounced(txt);
		});
		$input.on("focus", () => {
			if (this.np.customer_results.length) $area.find(".fg-search-dropdown").addClass("is-open");
		});
		$input.on("blur", () => {
			// Small delay so a result row's own "mousedown" (fires before
			// blur) can still register the selection before this closes it.
			setTimeout(() => $area.find(".fg-search-dropdown").removeClass("is-open"), 150);
		});
	}

	search_customers(txt) {
		if (!txt || !txt.trim()) {
			// Commit 18.4: never fetch/show the full customer list -- only a
			// real search triggers a server call.
			this._customer_search_seq++;
			this.np.customer_results = [];
			this.render_customer_dropdown();
			return Promise.resolve();
		}
		const seq = ++this._customer_search_seq;
		return this.call("search_customers", { txt: txt }).then((results) => {
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
		if (!txt || !txt.trim()) {
			// Commit 18.4: never fetch/show the full catalog -- only a real
			// search triggers a server call.
			this._item_search_seq++;
			this.np.item_results = [];
			this.render_item_results_empty_prompt();
			return Promise.resolve();
		}
		const seq = ++this._item_search_seq;
		this.render_item_results_skeleton();
		return this.call("search_items", { txt: txt }).then((results) => {
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
		const has_qty = !!info && info.qty_disponible != null;
		const disponible = has_qty ? format_qty(info.qty_disponible) : "—";
		// Three states (Commit 18.4): verde (>0), rojo (=0), gris (no
		// determinado -- item.item_defaults sin bodega, get_item_info()
		// devolvió qty_disponible: null). Nunca deshabilita el stepper --
		// la disponibilidad es puramente informativa.
		const disponible_class = !has_qty
			? "fg-product-avail--none"
			: flt(info.qty_disponible) > 0
			? "fg-product-avail--ok"
			: "fg-product-avail--zero";
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

	// -- Pedido rápido (Commit 25.8.6) -----------------------------------------
	//
	// Talks to fabergray_erp.api.ventas.parse_quick_order (Commit 25.8.5) --
	// read-only, never creates/submits anything server-side. Every business
	// rule this section renders (high/medium/low, ambiguous, preselected_item)
	// comes straight from that response -- nothing here recomputes a score
	// threshold or an ambiguity margin (brief section 8: those live server-
	// side, in fabergray_erp/quick_order/scoring.py, on purpose).
	//
	// The ONLY way this section ever touches the real cart is by calling
	// set_cart_qty() (apply_quick_order_to_cart() below) -- the exact same
	// public method the manual product-card stepper already calls. Nothing
	// here writes to this.np.cart directly, and nothing here ever calls
	// confirm_order()/build_order_payload() or any create_*/update_*/
	// modify_* server method -- "CONFIRMAR PEDIDO"/"GUARDAR CAMBIOS" (Paso 3,
	// untouched by this commit) remains the one and only place a Sales Order
	// is created or changed.

	quick_order_panel_html() {
		return `
			<div class="fg-qo-panel">
				<div class="fg-qo-intro">
					<div class="fg-qo-title">${__("Captura rápida de pedido")}</div>
					<div class="fg-qo-subtitle">
						${__("Pega el pedido recibido por WhatsApp.")}<br>
						${__("Revisa los productos sugeridos antes de agregarlos.")}
					</div>
				</div>
				<label class="fg-qo-textarea-label" for="fg-quick-order-textarea">${__("Pedido")}</label>
				<textarea
					id="fg-quick-order-textarea"
					class="fg-quick-order-textarea"
					rows="6"
					placeholder="${frappe.utils.escape_html(
						"2 cajas guantes talla L negro\n1 paquete bolsa blanca 70x90\n3 galones desengrasante"
					)}"
				>${frappe.utils.escape_html(this.np.quick_order.text || "")}</textarea>
				<div class="fg-qo-actions-row">
					<button type="button" class="fg-btn fg-btn--solid-primary fg-quick-order-interpret-btn">
						${icon("search", "fg-icon-sm")} ${__("Interpretar pedido")}
					</button>
					<button type="button" class="fg-btn fg-btn--ghost fg-quick-order-clear-btn">
						${icon("trash-2", "fg-icon-sm")} ${__("Limpiar pedido rápido")}
					</button>
				</div>
				<div class="fg-quick-order-results"></div>
				<div class="fg-quick-order-apply-bar"></div>
			</div>
		`;
	}

	bind_quick_order_panel_events() {
		this.$body.find(".fg-quick-order-textarea").on("input", (e) => {
			this.np.quick_order.text = $(e.currentTarget).val();
		});
		this.$body.find(".fg-quick-order-interpret-btn").on("click", () => this.interpret_quick_order());
		this.$body.find(".fg-quick-order-clear-btn").on("click", () => this.clear_quick_order());
	}

	// Sends exactly what's in the textarea (trimmed) to parse_quick_order()
	// -- disables the button and shows a loading state for the duration
	// (brief section 4: "impedir doble click"), always restores it in
	// .finally() even on error. A failed call leaves the textarea and the
	// cart both exactly as they were -- frappe.call()'s own default error
	// dialog already shows the real server message (same convention every
	// other .catch() in this file already follows), nothing custom needed
	// here. Re-running this REPLACES this.np.quick_order.lines wholesale
	// (brief section 21: "reemplazar, NO duplicar") -- never appends.
	interpret_quick_order() {
		if (this.np.quick_order.loading) return;

		const $textarea = this.$body.find(".fg-quick-order-textarea");
		const text = ($textarea.val() || "").trim();
		if (!text) {
			frappe.show_alert({ message: __("Pega un pedido antes de interpretar."), indicator: "orange" });
			return;
		}

		this.np.quick_order.loading = true;
		this.np.quick_order.text = text;
		const $btn = this.$body.find(".fg-quick-order-interpret-btn").prop("disabled", true).addClass("fg-btn--loading");
		this.render_quick_order_results_skeleton();

		this.call("parse_quick_order", { text: text })
			.then((response) => {
				this.np.quick_order.lines = (response.lines || []).map((line) => this.build_quick_order_line_state(line));
				this.render_quick_order_lines();
			})
			.catch(() => {
				this.render_quick_order_lines(); // back to the empty prompt, textarea untouched
			})
			.finally(() => {
				this.np.quick_order.loading = false;
				$btn.prop("disabled", false).removeClass("fg-btn--loading");
			});
	}

	// The one place a parse_quick_order() line becomes this Page's own
	// working state -- everything through `candidates` is server data,
	// copied as-is; everything after is client-only selection state that
	// this screen owns until "Agregar productos al pedido" is pressed.
	// `selected` starts from `preselected_item` when the server sent one
	// (brief section 8: ONLY preselected_item ever auto-selects anything --
	// never top_candidate on its own, never a client-side score check).
	build_quick_order_line_state(server_line) {
		return {
			source_text: server_line.source_text,
			qty: server_line.qty,
			detected_uom: server_line.detected_uom,
			confidence: server_line.confidence,
			ambiguous: server_line.ambiguous,
			candidates: server_line.candidates || [],
			selected: server_line.preselected_item
				? {
						item_code: server_line.preselected_item.item_code,
						item_name: server_line.preselected_item.item_name,
						stock_uom: server_line.preselected_item.stock_uom,
				  }
				: null,
			ignored: false,
			manual_search_open: false,
			manual_search_txt: "",
			manual_search_results: [],
			_search_seq: 0,
		};
	}

	render_quick_order_results_skeleton() {
		this.$body.find(".fg-quick-order-results").html(`
			<div class="fg-skeleton fg-product-skeleton"></div>
			<div class="fg-skeleton fg-product-skeleton"></div>
		`);
		this.$body.find(".fg-quick-order-apply-bar").empty();
	}

	render_quick_order_lines() {
		const $area = this.$body.find(".fg-quick-order-results");
		if (!$area.length) return; // mode switched away before this resolved
		const lines = this.np.quick_order.lines;

		if (!lines.length) {
			$area.html(`<div class="fg-empty fg-empty--sm">${__('Pega un pedido y presiona "Interpretar pedido".')}</div>`);
			this.$body.find(".fg-quick-order-apply-bar").empty();
			return;
		}

		$area.html(lines.map((line, i) => this.render_quick_order_line(line, i)).join(""));
		lines.forEach((_, i) => this.bind_quick_order_line_events(i));
		this.render_quick_order_apply_bar();
	}

	// Re-renders ONE line card in place (never the whole list) -- every
	// per-line action below (qty edit, candidate pick, ignore toggle, manual
	// search open/close/pick) calls this, not render_quick_order_lines(),
	// so the rest of the review list never loses scroll position or gets
	// needlessly rebuilt for an edit to a single row.
	render_quick_order_line_at(index) {
		const line = this.np.quick_order.lines[index];
		const $old = this.$body.find(`.fg-qo-line[data-index="${index}"]`);
		if (!line || !$old.length) return;
		$old.replaceWith(this.render_quick_order_line(line, index));
		this.bind_quick_order_line_events(index);
		this.render_quick_order_apply_bar();
	}

	// The five states from the brief's own section 7 table -- "sin
	// candidatos" is checked FIRST (a line whose server confidence happens
	// to be "low" with zero candidates must say "Producto no encontrado",
	// never "Selecciona producto").
	quick_order_line_status(line) {
		if (!line.candidates.length) return { label: __("Producto no encontrado"), mod: "not-found" };
		if (line.confidence === "high" && !line.ambiguous) return { label: __("Coincidencia alta"), mod: "high" };
		if (line.confidence === "high" && line.ambiguous) return { label: __("Revisar alternativas"), mod: "review" };
		if (line.confidence === "medium") return { label: __("Revisar sugerencia"), mod: "review" };
		return { label: __("Selecciona producto"), mod: "low" };
	}

	// A small, non-selection tag shown on the FIRST candidate only (brief
	// section 9): "Seleccionado" when it's genuinely this line's current
	// pick (server preselected it, or she clicked it herself); otherwise a
	// plain hint -- "Mejor coincidencia" for an ambiguous high-confidence
	// top pick, "Sugerencia principal" for medium -- that never implies a
	// selection was made. Low confidence gets no tag at all (brief: "NO
	// mostrarlo como si fuera una sugerencia confiable").
	quick_order_candidate_tag(line, candidate, index) {
		if (line.selected && line.selected.item_code === candidate.item_code) return __("Seleccionado");
		if (index !== 0) return null;
		if (line.confidence === "high" && line.ambiguous) return __("Mejor coincidencia");
		if (line.confidence === "medium") return __("Sugerencia principal");
		return null;
	}

	render_quick_order_line(line, index) {
		const status = this.quick_order_line_status(line);
		const candidates_html = line.candidates.length
			? line.candidates.map((c, i) => this.render_quick_order_candidate(line, c, i, index)).join("")
			: `<div class="fg-empty fg-empty--sm">${__("Sin coincidencias")}</div>`;

		return `
			<div class="fg-qo-line ${line.ignored ? "fg-qo-line--ignored" : ""}" data-index="${index}">
				<div class="fg-qo-line-header">
					<div class="fg-qo-line-source">
						<span class="fg-qo-line-label">${__("Texto original")}</span>
						<span class="fg-qo-line-text">${frappe.utils.escape_html(line.source_text)}</span>
					</div>
					<span class="fg-qo-status fg-qo-status--${status.mod}">${status.label}</span>
				</div>

				<div class="fg-qo-line-body">
					<div class="fg-qo-field">
						<label for="fg-qo-qty-${index}">${__("Cantidad")}</label>
						<input
							id="fg-qo-qty-${index}"
							type="number"
							inputmode="decimal"
							class="fg-qo-qty-input"
							value="${line.qty}"
							min="0"
							${line.ignored ? "disabled" : ""}
						>
					</div>
					<div class="fg-qo-field">
						<label>${__("Unidad detectada")}</label>
						<div class="fg-qo-uom">${
							line.detected_uom ? __("Detectado") + ": " + frappe.utils.escape_html(line.detected_uom) : "—"
						}</div>
					</div>
					<div class="fg-qo-field fg-qo-field--product">
						<label>${__("Producto")}</label>
						${this.render_quick_order_selected_product(line)}
					</div>
				</div>

				<div class="fg-qo-line-candidates">
					<div class="fg-qo-line-label">${__("Alternativas")}</div>
					<div class="fg-qo-candidate-list">${candidates_html}</div>
				</div>

				${this.render_quick_order_manual_search(line, index)}

				<div class="fg-qo-line-actions">
					<button type="button" class="fg-qo-line-search-toggle" data-index="${index}">
						${icon("search", "fg-icon-sm")} ${line.manual_search_open ? __("Cerrar búsqueda") : __("Buscar otro producto")}
					</button>
					<button type="button" class="fg-qo-line-ignore" data-index="${index}" aria-pressed="${line.ignored}">
						${line.ignored ? icon("circle-check", "fg-icon-sm") + " " + __("Reactivar") : icon("x", "fg-icon-sm") + " " + __("Ignorar")}
					</button>
				</div>
			</div>
		`;
	}

	render_quick_order_selected_product(line) {
		if (line.selected) {
			return `
				<div class="fg-selected-chip fg-qo-selected-chip">
					${icon("check", "fg-icon-sm")}
					<span>${frappe.utils.escape_html(line.selected.item_name)}</span>
					<span class="fg-qo-selected-code">${frappe.utils.escape_html(line.selected.item_code)}</span>
				</div>
			`;
		}
		return `<div class="fg-qo-no-selection">${__("Sin seleccionar")}</div>`;
	}

	// Never renders price/stock/cost/warehouse (brief section 10) -- these
	// fields are all parse_quick_order() ever sends per candidate in the
	// first place (Commit 25.8.5), so there is nothing to omit here, only
	// to display: item_name/item_code/stock_uom/score/confidence.
	render_quick_order_candidate(line, candidate, index, line_index) {
		const is_selected = line.selected && line.selected.item_code === candidate.item_code;
		const tag = this.quick_order_candidate_tag(line, candidate, index);
		return `
			<button
				type="button"
				class="fg-qo-candidate ${is_selected ? "is-selected" : ""}"
				data-line="${line_index}"
				data-item-code="${frappe.utils.escape_html(candidate.item_code)}"
				aria-pressed="${!!is_selected}"
			>
				<div class="fg-qo-candidate-main">
					<span class="fg-qo-candidate-name">${frappe.utils.escape_html(candidate.item_name)}</span>
					<span class="fg-qo-candidate-code">${frappe.utils.escape_html(candidate.item_code)}</span>
				</div>
				<div class="fg-qo-candidate-meta">
					${candidate.stock_uom ? `<span>${frappe.utils.escape_html(candidate.stock_uom)}</span>` : ""}
					<span class="fg-qo-candidate-score fg-qo-candidate-score--${candidate.confidence}">${candidate.score}%</span>
					${tag ? `<span class="fg-qo-candidate-tag">${tag}</span>` : ""}
				</div>
			</button>
		`;
	}

	// -- Búsqueda manual por fila (brief section 11) ---------------------------
	//
	// Reuses the real search_items() SERVER call directly (this.call(...)),
	// never the class's own search_items() METHOD -- that one mutates
	// this.np.item_results/renders .fg-item-results, the shared state behind
	// the main manual-search box; a per-line search here needs its own,
	// isolated result list instead. No second matching algorithm of any kind
	// is introduced -- same endpoint, same server-side logic, just a
	// separate client-side results array scoped to this one Quick Order line.

	render_quick_order_manual_search(line, index) {
		if (!line.manual_search_open) return "";
		return `
			<div class="fg-qo-manual-search">
				<div class="fg-search-box fg-search-box--sm">
					${icon("search", "fg-icon-sm")}
					<input
						type="text"
						class="fg-qo-manual-search-input"
						data-index="${index}"
						placeholder="${__("Buscar producto...")}"
						value="${frappe.utils.escape_html(line.manual_search_txt || "")}"
					>
				</div>
				<div class="fg-qo-manual-search-results">
					${this.quick_order_manual_results_html(line, index)}
				</div>
			</div>
		`;
	}

	quick_order_manual_results_html(line, index) {
		const results = line.manual_search_results || [];
		if (results.length) return results.map((r) => this.render_quick_order_manual_result_row(r, index)).join("");
		if (line.manual_search_txt) return `<div class="fg-empty fg-empty--sm">${__("Sin resultados")}</div>`;
		return "";
	}

	render_quick_order_manual_result_row(r, index) {
		return `
			<button
				type="button"
				class="fg-qo-manual-result"
				data-index="${index}"
				data-item-code="${frappe.utils.escape_html(r.item_code)}"
				data-item-name="${frappe.utils.escape_html(r.item_name)}"
				data-stock-uom="${frappe.utils.escape_html(r.stock_uom || "")}"
			>
				<span>${frappe.utils.escape_html(r.item_name)}</span>
				<span class="fg-qo-manual-result-code">${frappe.utils.escape_html(r.item_code)}</span>
			</button>
		`;
	}

	// Surgical update -- only the results list inside ONE line's manual
	// search reflows while she types, never the whole line/card (which
	// would drop input focus on every keystroke).
	render_quick_order_manual_results_only(index) {
		const line = this.np.quick_order.lines[index];
		if (!line) return;
		const $results = this.$body.find(`.fg-qo-line[data-index="${index}"] .fg-qo-manual-search-results`);
		if (!$results.length) return;
		$results.html(this.quick_order_manual_results_html(line, index));
		$results.find(".fg-qo-manual-result").on("click", (e) => {
			const $btn = $(e.currentTarget);
			this.select_quick_order_manual_result(index, {
				item_code: $btn.data("item-code"),
				item_name: $btn.data("item-name"),
				stock_uom: $btn.data("stock-uom"),
			});
		});
	}

	search_quick_order_item(index, txt) {
		const line = this.np.quick_order.lines[index];
		if (!line) return;
		if (!txt || !txt.trim()) {
			line.manual_search_results = [];
			this.render_quick_order_manual_results_only(index);
			return;
		}
		line._search_seq = (line._search_seq || 0) + 1;
		const seq = line._search_seq;
		this.call("search_items", { txt: txt }).then((results) => {
			if (line._search_seq !== seq) return; // a newer search for this same line superseded this one
			line.manual_search_results = results || [];
			this.render_quick_order_manual_results_only(index);
		});
	}

	toggle_quick_order_manual_search(index) {
		const line = this.np.quick_order.lines[index];
		if (!line) return;
		line.manual_search_open = !line.manual_search_open;
		if (!line.manual_search_open) {
			line.manual_search_txt = "";
			line.manual_search_results = [];
		}
		this.render_quick_order_line_at(index);
	}

	select_quick_order_manual_result(index, item) {
		const line = this.np.quick_order.lines[index];
		if (!line) return;
		line.selected = { item_code: item.item_code, item_name: item.item_name, stock_uom: item.stock_uom };
		line.manual_search_open = false;
		line.manual_search_txt = "";
		line.manual_search_results = [];
		this.render_quick_order_line_at(index);
	}

	select_quick_order_candidate(index, item_code) {
		const line = this.np.quick_order.lines[index];
		if (!line) return;
		const candidate = line.candidates.find((c) => c.item_code === item_code);
		if (!candidate) return;
		line.selected = { item_code: candidate.item_code, item_name: candidate.item_name, stock_uom: candidate.stock_uom };
		line.manual_search_open = false;
		this.render_quick_order_line_at(index);
	}

	toggle_quick_order_ignore(index) {
		const line = this.np.quick_order.lines[index];
		if (!line) return;
		line.ignored = !line.ignored;
		this.render_quick_order_line_at(index);
	}

	// Same clamp set_cart_qty()'s own callers already use everywhere else in
	// this file (bind_item_result_events()/bind_cart_line_events()) --
	// 0/negative/NaN/empty all normalize to 0 here too (brief section 12: no
	// new, incompatible rule). A qty of 0 simply fails
	// validate_quick_order_lines() below for an active (non-ignored) line --
	// it is never silently treated as valid.
	set_quick_order_qty(index, raw_value) {
		const line = this.np.quick_order.lines[index];
		if (!line) return;
		line.qty = Math.max(flt(raw_value), 0);
		this.render_quick_order_line_at(index);
	}

	bind_quick_order_line_events(index) {
		const $line = this.$body.find(`.fg-qo-line[data-index="${index}"]`);
		if (!$line.length) return;

		$line.find(".fg-qo-qty-input").on("change", (e) => {
			this.set_quick_order_qty(index, $(e.currentTarget).val());
		});
		$line.find(".fg-qo-candidate").on("click", (e) => {
			this.select_quick_order_candidate(index, $(e.currentTarget).data("item-code"));
		});
		$line.find(".fg-qo-line-ignore").on("click", () => this.toggle_quick_order_ignore(index));
		$line.find(".fg-qo-line-search-toggle").on("click", () => this.toggle_quick_order_manual_search(index));

		const $manual_input = $line.find(".fg-qo-manual-search-input");
		const debounced_manual_search = frappe.utils.debounce((txt) => this.search_quick_order_item(index, txt), 300);
		$manual_input.on("input", (e) => {
			const txt = $(e.currentTarget).val();
			const line = this.np.quick_order.lines[index];
			if (line) line.manual_search_txt = txt;
			if (!txt || !txt.trim()) {
				if (line) line.manual_search_results = [];
				this.render_quick_order_manual_results_only(index);
				return;
			}
			debounced_manual_search(txt);
		});

		$line.find(".fg-qo-manual-result").on("click", (e) => {
			const $btn = $(e.currentTarget);
			this.select_quick_order_manual_result(index, {
				item_code: $btn.data("item-code"),
				item_name: $btn.data("item-name"),
				stock_uom: $btn.data("stock-uom"),
			});
		});
	}

	// -- Validación + aplicar al carrito (brief sections 16-19) ----------------

	// Ignored lines never count, toward either the "at least one active
	// line" requirement or the "every active line resolved" one -- an
	// ignored line with no Item selected is not an error, it is simply not
	// part of this batch.
	validate_quick_order_lines() {
		const active = this.np.quick_order.lines.filter((l) => !l.ignored);
		const missing = active.filter((l) => !l.selected || !(flt(l.qty) > 0));
		return { valid: active.length > 0 && missing.length === 0, active_count: active.length, missing_count: missing.length };
	}

	render_quick_order_apply_bar() {
		const $bar = this.$body.find(".fg-quick-order-apply-bar");
		if (!$bar.length) return;
		if (!this.np.quick_order.lines.length) {
			$bar.empty();
			return;
		}
		const { valid, missing_count } = this.validate_quick_order_lines();
		$bar.html(`
			${missing_count > 0 ? `<div class="fg-qo-warning">${icon("triangle-alert", "fg-icon-sm")} ${__("Faltan {0} línea(s) por resolver antes de agregar.", [missing_count])}</div>` : ""}
			<button type="button" class="fg-btn fg-btn--solid-primary fg-quick-order-apply-btn" ${valid ? "" : "disabled"}>
				${icon("shopping-cart", "fg-icon-sm")} ${__("Agregar productos al pedido")}
			</button>
		`);
		$bar.find(".fg-quick-order-apply-btn").on("click", () => this.apply_quick_order_to_cart());
	}

	// The one place Quick Order ever touches the real cart -- exclusively
	// through set_cart_qty(), never a direct this.np.cart.set() (brief
	// section 17). Groups every active, resolved line by item_code and SUMS
	// their quantities FIRST (brief section 18 -- "2 guantes..." + "3
	// guantes..." resolving to the same Item must add up to 5, never let one
	// line silently overwrite the other: set_cart_qty() itself REPLACES a
	// line's qty, it does not add), then adds that sum on top of whatever
	// qty the cart already had for that Item (cart_qty(item_code), read
	// BEFORE any of this batch's own set_cart_qty() calls) -- an item
	// already in the cart at qty 4 plus a Quick Order total of 5 ends at 9,
	// never at a bare 5 (this commit's own report, section K/L, has the full
	// audit of set_cart_qty()'s replace-not-add semantics that makes this
	// necessary).
	apply_quick_order_to_cart() {
		const { valid } = this.validate_quick_order_lines();
		if (!valid) return; // apply button is already disabled in this case -- defensive guard only

		const additions = new Map(); // item_code -> {qty_to_add, item_name, stock_uom}
		for (const line of this.np.quick_order.lines) {
			if (line.ignored || !line.selected) continue;
			const qty = flt(line.qty);
			if (qty <= 0) continue;
			const existing = additions.get(line.selected.item_code);
			if (existing) {
				existing.qty_to_add += qty;
			} else {
				additions.set(line.selected.item_code, {
					qty_to_add: qty,
					item_name: line.selected.item_name,
					stock_uom: line.selected.stock_uom,
				});
			}
		}
		if (!additions.size) return;

		for (const [item_code, addition] of additions) {
			// set_cart_qty() resolves item_name/stock_uom for a NEW cart line by
			// checking this.np.item_results, then this._item_info_cache, then
			// (only as a last resort) falls back to the bare item_code -- an
			// Item that only ever came from a Quick Order candidate/manual
			// search was never in either of those, so it would otherwise show
			// its raw code instead of its real name. Seeding the info cache
			// here (only if not already present -- never overwrite a fresher
			// entry) is what makes set_cart_qty() resolve it correctly, without
			// touching set_cart_qty() itself at all.
			if (!this._item_info_cache.has(item_code)) {
				this._item_info_cache.set(item_code, {
					item_code: item_code,
					item_name: addition.item_name,
					stock_uom: addition.stock_uom,
				});
			}
			this.set_cart_qty(item_code, this.cart_qty(item_code) + addition.qty_to_add);
		}

		frappe.show_alert(
			{
				message: `${icon("check", "fg-icon-sm")} ${__("{0} producto(s) agregado(s) al pedido", [additions.size])}`,
				indicator: "green",
			},
			5
		);

		this.clear_quick_order(); // results + textarea only -- render_summary() above already reflects the cart
	}

	// "Limpiar pedido rápido" (brief section 15) AND the automatic cleanup
	// after a successful apply (brief section 19) both funnel through here
	// -- one implementation, never two. Only ever touches
	// this.np.quick_order + its own textarea; this.np.cart is never part of
	// this reset.
	clear_quick_order() {
		this.np.quick_order = this.blank_quick_order_state();
		this.$body.find(".fg-quick-order-textarea").val("");
		this.render_quick_order_lines();
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
			<button type="button" class="fg-btn fg-btn--solid-primary fg-btn--lg fg-confirm-btn">
				${icon("check")} ${
					this.np.editing_order_name || this.np.modifying_order_name ? __("GUARDAR CAMBIOS") : __("CONFIRMAR PEDIDO")
				}
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

	// Commit 25.11 -- CONFIRMAR PEDIDO/GUARDAR CAMBIOS stays visually and
	// functionally enabled ALWAYS, through the entire Nuevo Pedido/Editar/
	// Modificar flow, regardless of customer/cart/qty state AND regardless
	// of a request being in flight -- this button must never receive
	// disabled=true for any reason. Double-submit protection is the
	// `if (this.busy) return;` guard at the top of confirm_order() alone
	// (see that function's own comment), never the disabled attribute.
	// What used to gate this (no customer, empty cart) is validated
	// explicitly, with a specific message per case, at the top of
	// confirm_order() itself. Real bug this fixes: an asesora correcting/
	// continuing a pedido could find the button disabled with no visible
	// reason (cart temporarily empty while swapping products, customer
	// chip just removed to change it, a request still in flight, etc.) --
	// she now always gets a clickable button and, if truly not ready, an
	// explicit, actionable message instead of a silently inert button.
	refresh_confirm_state() {
		this.$body.find(".fg-confirm-btn").prop("disabled", false);
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

	// Commit 25.10 -- every "is this pedido actually ready" check now lives
	// HERE, explicitly, checked at click time -- never as a precondition for
	// the button to be clickable at all (see refresh_confirm_state()'s own
	// comment). Three distinct, specific messages, in the order a real
	// pedido is built (cliente -> productos -> cantidades) -- never a
	// single generic "invalid" message, and never a request sent to the
	// server for any of these: `this.call(...)` is only ever reached below
	// once every one of these has already passed. The button itself is
	// never disabled by any of this -- it stays clickable so she can fix
	// the issue and press it again immediately.
	confirm_order() {
		if (this.busy) return;

		if (!this.np.customer) {
			frappe.show_alert({ message: __("Selecciona un cliente antes de confirmar."), indicator: "orange" });
			return;
		}
		if (this.np.cart.size === 0) {
			frappe.show_alert({ message: __("Agrega al menos un producto al pedido."), indicator: "orange" });
			return;
		}

		const payload = this.build_order_payload();
		if (!payload.items.length) {
			// this.np.cart has entries (checked above) but every single one
			// filtered out of build_order_payload() for qty <= 0 -- a
			// distinct message from "cart is empty", since the asesora
			// added products, she just needs to fix their quantities.
			frappe.show_alert({ message: __("Revisa las cantidades de los productos."), indicator: "orange" });
			return;
		}

		if (this.np.editing_order_name) {
			// Commit 18.5: "GUARDAR CAMBIOS" never submits -- straight to
			// update_draft_sales_order(), no confirmation dialog (matches
			// ordinary "save" conventions; ELIMINAR/CANCELAR are the two
			// destructive actions that get an explicit confirm instead).
			this.save_draft_edit(payload);
			return;
		}

		if (this.np.modifying_order_name) {
			// Commit 18.5b: same "no confirmation dialog" convention as
			// save_draft_edit() above -- straight to modify_submitted_
			// sales_order(), which does the real cancel+amend server-side.
			this.save_submitted_modification(payload);
			return;
		}

		this.busy = true;
		const $btn = this.$body.find(".fg-confirm-btn").addClass("fg-btn--loading");

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
						$btn.removeClass("fg-btn--loading");
						this.refresh_confirm_state();
					});
			},
			() => {
				this.busy = false;
				$btn.removeClass("fg-btn--loading");
			}
		);
	}

	save_draft_edit(payload) {
		this.busy = true;
		const $btn = this.$body.find(".fg-confirm-btn").addClass("fg-btn--loading");

		this.call("update_draft_sales_order", {
			name: this.np.editing_order_name,
			customer: payload.customer,
			items: payload.items,
			observations: payload.observations,
		})
			.then((result) => {
				frappe.show_alert(
					{
						message: `${icon("check", "fg-icon-sm")} ${__("Cambios guardados")} — #${frappe.utils.escape_html(
							result.name
						)}`,
						indicator: "green",
					},
					5
				);
				this.back_to_dashboard();
			})
			.catch(() => {
				// same reasoning as confirm_order()'s own .catch() -- the server's
				// default error dialog already showed the real message.
			})
			.finally(() => {
				this.busy = false;
				$btn.removeClass("fg-btn--loading");
				this.refresh_confirm_state();
			});
	}

	// Commit 18.5b: modify_submitted_sales_order() does the real cancel+
	// amend server-side -- the authoritative gate is re-checked there
	// (never trusted from what got this screen open in the first place).
	// A block surfaces here via the server's own real error message
	// (Bodega started picking a moment ago, etc.) exactly like
	// cancel_sales_order()'s own native-block .catch() already does --
	// never swallowed, never bypassed, no manual retry attempted.
	save_submitted_modification(payload) {
		this.busy = true;
		const $btn = this.$body.find(".fg-confirm-btn").addClass("fg-btn--loading");

		this.call("modify_submitted_sales_order", {
			name: this.np.modifying_order_name,
			customer: payload.customer,
			items: payload.items,
			observations: payload.observations,
		})
			.then((result) => {
				frappe.show_alert(
					{
						message: `${icon("check", "fg-icon-sm")} ${__("Cambios guardados")} — ${__(
							"Pedido"
						)} #${frappe.utils.escape_html(result.commercial_name)}`,
						indicator: "green",
					},
					5
				);
				this.back_to_dashboard();
			})
			.catch(() => {
				// same reasoning as confirm_order()'s/save_draft_edit()'s own
				// .catch() -- the server's default error dialog already showed
				// the real message (a blocker found, ownership, etc.).
			})
			.finally(() => {
				this.busy = false;
				$btn.removeClass("fg-btn--loading");
				this.refresh_confirm_state();
			});
	}
};

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, no server calls, no state.
// Intentionally duplicated (not imported) from bodega.js/jefe_de_bodega.js,
// same reasoning as Commit 6: a few lines each, zero business logic, keeps
// this Page's asset loading independent of theirs.
// -------------------------------------------------------------------------
// Commit 25.20 -- the unified search bar markup, ONE shape shared by
// every operational Page (section 2's own "debe verse igual en los seis
// módulos") -- reproduced per-page (never imported: each Page's own
// icon() is page-local, same "self-contained bundle" convention this
// whole file already documents at its own top) but never re-derived
// differently -- exact same classes (fg_shell.css, global), exact same
// placeholder text, exact same clear-button behavior in every Page that
// has one of these.
function render_search_bar_html(value) {
	const has_value = !!(value && value.trim());
	// The clear button is always in the DOM (visibility toggled via the
	// "is-visible" class, never conditionally rendered) -- the `input`
	// handler below only ever touches the RESULTS container, never this
	// search bar itself, so the field never loses focus/cursor position
	// while the user is still typing (same pattern bodega.js's own
	// pre-existing Pedidos search already established).
	return `
		<div class="fg-search-bar">
			${icon("search", "fg-search-icon")}
			<input type="text" class="fg-search-input" placeholder="${__("Buscar por cliente o fecha...")}" value="${frappe.utils.escape_html(
				value || ""
			)}">
			<button type="button" class="fg-search-clear ${has_value ? "is-visible" : ""}" title="${__("Limpiar")}">${icon("x", "fg-icon-sm")}</button>
		</div>
	`;
}

function render_search_empty_html() {
	return `
		<div class="fg-search-empty">
			<strong>${__("No se encontraron resultados")}</strong>
			<div>${__("Prueba buscando por nombre del cliente o fecha.")}</div>
		</div>
	`;
}

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

// Hotfix 25.26.3 -- hora de creación del pedido (Sales Order.creation del
// pedido vigente), shared by the order cards and "VER PEDIDO". Frappe
// stores datetimes in the SYSTEM time zone; convert_to_user_tz() moves
// it to the user's effective time zone (frappe.boot.time_zone) -- no
// manual offset, no hardcoded zone. 12-hour, no seconds.
const CREATED_TIME_FORMAT = "h:mm A";

function format_created_time(creation) {
	if (!creation) return "";
	const m = frappe.datetime.convert_to_user_tz(creation, false);
	return m && m.isValid() ? m.format(CREATED_TIME_FORMAT) : "";
}

// "23-09-2026 · 3:42 PM" -- the date stays exactly what it always was
// (transaction_date in the site's date format); the time is appended only
// when creation is present and valid, never "undefined"/"Invalid date".
function format_order_date_with_time(transaction_date, creation) {
	const date = frappe.datetime.str_to_user(transaction_date);
	const time = format_created_time(creation);
	return time ? `${date} · ${time}` : date;
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
// Commit 25.18 -- shared between render_order_card() and the "VER PEDIDO"
// detail overlay, so the card and the detail view can never show a
// different logistics story for the same order (single source of layout
// logic, mirroring _resolve_sales_order_logistics_status()'s own "single
// source of classification logic" on the server). `o`/`detail` both carry
// the exact same five fields (get_my_orders()/get_order_detail(), api/
// ventas.py) -- this function only ever reads `logistics_status`/
// `route_name`/`driver_name`/`dispatched_on`/`delivered_on`, never `status`
// (section 15.O -- never re-derive from Sales Order.status client-side).
function render_logistics_block(o) {
	const meta =
		o.logistics_status === "DELIVERED"
			? { label: __("ENTREGADO"), mod: "logistics-delivered", icon: "check-circle" }
			: o.logistics_status === "IN_ROUTE"
				? { label: __("EN RUTA"), mod: "logistics-in-route", icon: "truck" }
				: null;

	if (!meta) {
		return { badge_html: "", info_html: "" };
	}

	const badge_html = `<span class="fg-badge fg-badge--${meta.mod} fg-order-card-logistics-badge">${meta.label}</span>`;

	// Section 6/7 -- route/driver/date lines, each rendered ONLY when that
	// specific field actually came back populated (never guessed/invented)
	// -- "si no hay repartidor/fecha persistidos, mostrar solo EN RUTA/
	// ENTREGADO".
	const info_html = `
		<div class="fg-order-card-logistics fg-order-card-logistics--${meta.mod === "logistics-delivered" ? "delivered" : "in-route"}">
			${icon(meta.icon, "fg-icon-sm")}
			<div>
				<strong>${meta.label}</strong>
				${o.route_name ? `<div>${__("Recorrido")}: ${frappe.utils.escape_html(o.route_name)}</div>` : ""}
				${o.driver_name ? `<div>${__("Repartidor")}: ${frappe.utils.escape_html(o.driver_name)}</div>` : ""}
				${
					o.logistics_status === "IN_ROUTE" && o.dispatched_on
						? `<div>${__("Fecha de salida")}: ${frappe.datetime.str_to_user(o.dispatched_on)}</div>`
						: ""
				}
				${
					o.logistics_status === "DELIVERED" && o.delivered_on
						? `<div>${__("Fecha de entrega")}: ${frappe.datetime.str_to_user(o.delivered_on)}</div>`
						: ""
				}
			</div>
		</div>
	`;

	return { badge_html, info_html };
}

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
