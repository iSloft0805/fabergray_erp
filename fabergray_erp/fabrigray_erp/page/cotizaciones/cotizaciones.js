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
// Commit 25.26 -- the ONE exception to "no stock/no price here": the quick
// product search reads get_quick_search_item_details() (one batched call per
// search) and SHOWS, read-only, qty_disponible and the public selling price
// (or "SIN PRECIO"). Stock never blocks adding (a Quotation may be created
// with stock 0, by design) and neither value is ever stored in the cart or
// sent back. No other economic field (rate/price_list_rate/discount/amount/
// taxes/grand_total or any equivalent) is ever read from a server response or
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
		// Commit 25.20 -- free-text search, ALWAYS applied AFTER
		// quotation_filter (section 13's own "mantener" the existing
		// filters), never a replacement for it.
		this.quotation_search = "";

		// "Nueva cotización" (view: "nueva_cotizacion") working state -- reset
		// every time open_nueva_cotizacion() runs, never persisted across
		// cotizaciones. No editing/modifying state yet (Commits 20.6/20.7).
		this.nc = this.blank_nueva_cotizacion_state();
		this._customer_search_seq = 0;
		this._item_search_seq = 0;
		// Commit 25.26 -- item_code -> get_quick_search_item_details() row
		// ({item_code, qty_disponible, public_price, has_public_price}), for
		// display in search results ONLY -- never copied into this.nc.cart.
		this._item_details_cache = new Map();

		this.state = { view: "dashboard" };

		this.$app = $('<div class="fg-shell fg-cotizaciones">').appendTo(this.page.body);
		this.render_shell();
		this.load_dashboard();
	}

	blank_nueva_cotizacion_state() {
		return {
			editing_quotation_name: null, // Commit 20.6: null -> "Nueva cotización"; a Draft Quotation name -> "Editar cotización"
			// Commit 25.13: a SUBMITTED Quotation name -> "Editar cotización"
			// via modify_submitted_quotation() (cancel+amend) instead of
			// update_draft_quotation() -- mutually exclusive with
			// editing_quotation_name above, see confirm_quotation()'s own
			// dispatch.
			modifying_quotation_name: null,
			customer: null, // {name, customer_name}
			cart: new Map(), // item_code -> {item_code, item_name, stock_uom, qty}
			customer_results: [],
			item_results: [],
			// Commit 25.26 -- buscador rápido: which result row ArrowUp/
			// ArrowDown/Enter act on (-1 = none), and the exact input text
			// item_results belong to -- Enter is ignored while the input no
			// longer matches it (results still stale/in flight), so it can
			// never add a product from a previous query.
			active_index: -1,
			results_txt: "",
			// true after Escape/add/clear -- a debounced search already queued
			// before that moment must not reopen the results; the next real
			// keystroke sets it back to false.
			search_dismissed: false,
			// Commit 25.26 -- "2. Agregar productos" has two input modes
			// sharing the ONE cart above: "manual" (buscador rápido, always
			// the default) and "quick" ("Cotización rápida", pasted text
			// interpreted by ventas.parse_quick_order). Switching modes never
			// touches the cart.
			item_mode: "manual",
			quick: this.blank_quick_quote_state(),
			valid_till: "",
			terms: "",
		};
	}

	// Commit 25.26 -- "Cotización rápida" working state, client-side only
	// until "AGREGAR A COTIZACIÓN" feeds the shared cart. `lines` holds one
	// entry per parse_quick_order() line (see build_quick_quote_line()).
	blank_quick_quote_state() {
		return { text: "", lines: [], loading: false };
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
		// Commit 25.20 -- limit: 500, same reasoning ventas.js's own
		// load_dashboard() now carries: the search bar filters this list
		// client-side, so it must not silently be limited to the server's
		// own default 50.
		return Promise.all([this.call("get_quotation_summary"), this.call("get_my_quotations", { limit: 500 })])
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
			{ key: "aprobadas", label: __("Aprobadas"), sub: __("Por Facturación"), i: "check", mod: "cotizaciones-aprobadas" },
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
	//
	// Commit 25.16 -- BUGFIX: "pendientes"/"aprobadas" used to read native
	// `q.status` here (`"Open"`/`["Ordered","Partially Ordered"]`), the
	// same field the card's top badge used to read (see
	// quotation_review_badge_meta() below) -- both wrong for the same
	// reason: native status never changes on its own once Facturación
	// approves a Quotation (no Quotation -> Sales Order conversion exists
	// in this app), so clicking "Aprobadas" never showed anything
	// Facturación had actually approved, and "Pendientes" kept showing
	// Quotations that already were. `fg_billing_review_status` is now the
	// one source of truth here too, exactly matching
	// get_quotation_summary()'s own `pendientes`/`aprobadas` buckets --
	// `docstatus !== 2` excludes an old, superseded price-mode amendment
	// (see that function's own docstring) from ever matching either
	// filter.
	quotation_matches_filter(q, filter) {
		if (!filter) return true;
		if (filter === "cotizaciones_hoy") return q.transaction_date === frappe.datetime.nowdate();
		const vigente = q.docstatus !== 2;
		const billing_status = q.fg_billing_review_status || "Borrador";
		if (filter === "pendientes") return vigente && billing_status === "Pendiente de Facturación";
		if (filter === "aprobadas") return vigente && billing_status === "Aprobada";
		if (filter === "vencidas") return q.status === "Expired";
		return true;
	}

	// Commit 25.20 -- client-side (this.quotations already fully fetched,
	// limit: 500, see load_dashboard()) -- the ONE shared matcher
	// (fg_search.js) every operational Page's search bar calls.
	quotation_matches_search(q) {
		return fabergray_erp.search.matches_operational_search(q, this.quotation_search, {
			text_fields: ["customer_name", "customer"],
			date_fields: ["transaction_date"],
		});
	}

	render_quotations_results_html() {
		const all = this.quotations || [];
		const filtered = all.filter((q) => this.quotation_matches_filter(q, this.quotation_filter));
		const list = filtered.filter((q) => this.quotation_matches_search(q));

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
			: this.quotation_search
				? render_search_empty_html()
				: `<div class="fg-empty">${__("No tienes cotizaciones para mostrar.")}</div>`;

		return `${chip}<div class="fg-quotation-list">${cards}</div>`;
	}

	render_quotations_section() {
		return `
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Cotizaciones")}</div>
			</div>
			${render_search_bar_html(this.quotation_search)}
			<div class="fg-quotations-results">${this.render_quotations_results_html()}</div>
		`;
	}

	render_quotation_card(q) {
		// Commit 25.16 -- the top-right badge used to be quotation_status_meta
		// (q.status) here, see quotation_review_badge_meta()'s own docstring
		// for why that showed a contradictory "PENDIENTE" on an
		// already-Aprobada card.
		const top_badge = quotation_review_badge_meta(q);
		// Commit 25.13 -- null/"" (historical, pre-this-commit Quotation)
		// treated identically to "Borrador" here, section 14's own rule.
		const billing = billing_review_status_meta(q.fg_billing_review_status || "Borrador");
		const customer_label = frappe.utils.escape_html(q.customer_name || q.customer || "—");
		const vigencia = q.valid_till ? frappe.datetime.str_to_user(q.valid_till) : "—";
		const returned_note =
			q.fg_billing_review_status === "Devuelta" && q.fg_billing_review_note
				? `
					<div class="fg-quotation-card-returned-note">
						${icon("alert-triangle", "fg-icon-sm")}
						<div>
							<strong>${__("DEVUELTA POR FACTURACIÓN")}</strong>
							<div>${frappe.utils.escape_html(q.fg_billing_review_note)}</div>
						</div>
					</div>
				`
				: "";

		// Commit 25.17 -- "PEDIDO CREADO" info strip, own row, same
		// "info block outside the actions row" convention returned_note
		// above already establishes -- shown only once a Sales Order
		// actually exists for this exact, vigente Quotation (see
		// render_quotation_card_actions()'s own comment for the button
		// half of this: ENVIAR A PEDIDOS / VER PEDIDO are mutually
		// exclusive with each other, this strip only ever appears
		// alongside VER PEDIDO, never alongside ENVIAR A PEDIDOS).
		const pedido_creado_html = q.sales_order
			? `
				<div class="fg-quotation-card-pedido-note">
					${icon("check-circle", "fg-icon-sm")}
					<div>
						<strong>${__("PEDIDO CREADO")}</strong>
						<div>${frappe.utils.escape_html(q.sales_order.name)}</div>
					</div>
				</div>
			`
			: "";

		return `
			<div class="fg-quotation-card">
				<div class="fg-quotation-card-top">
					<div class="fg-quotation-card-id">#${frappe.utils.escape_html(q.name)}</div>
					<span class="fg-badge fg-badge--${top_badge.mod}">${top_badge.label}</span>
				</div>
				<span class="fg-badge fg-badge--${billing.mod}">${billing.label}</span>
				<div class="fg-quotation-card-customer">${icon("user", "fg-icon-sm")} ${customer_label}</div>
				<div class="fg-quotation-card-meta">
					<span>${icon("calendar", "fg-icon-sm")} ${frappe.datetime.str_to_user(q.transaction_date)}</span>
					<span>${icon("clock", "fg-icon-sm")} ${__("Vigencia")}: ${vigencia}</span>
				</div>
				<div class="fg-quotation-card-counts">
					<span>${q.item_count} ${q.item_count === 1 ? __("referencia") : __("referencias")}</span>
					<span>${format_qty(q.total_qty)} ${__("unidades")}</span>
				</div>
				${returned_note}
				${pedido_creado_html}
				${this.render_quotation_card_actions(q)}
			</div>
		`;
	}

	// Commit 20.6: VER COTIZACIÓN always shows. Commit 25.13 -- EDITAR/
	// ENVIAR A FACTURACIÓN now depend on `fg_billing_review_status`
	// (billing review), not just native `q.status`:
	//   - "Pendiente de Facturación": VER only -- section 5's own explicit
	//     "no permitir reenviar/editar mientras está pendiente" (also
	//     enforced server-side, this is only the UI half).
	//   - a real orphaned Draft (`q.status === "Draft"`, docstatus=0,
	//     Commit 20.6's own rare edge case): EDITAR via
	//     open_edit_cotizacion() (update_draft_quotation()), unchanged.
	//   - every other case (null/"Borrador"/"Devuelta"/"Aprobada" on an
	//     already-submitted Quotation): EDITAR via open_modify_cotizacion()
	//     (modify_submitted_quotation(), this commit's own cancel+amend);
	//     "Borrador"/"Devuelta" ALSO get "ENVIAR A FACTURACIÓN" --
	//     "Aprobada" does not (server rejects a direct re-send while
	//     already Aprobada -- she must edit first, which is exactly what
	//     invalidates the old approval, section 12).
	render_quotation_card_actions(q) {
		const name_attr = `data-quotation-name="${frappe.utils.escape_html(q.name)}"`;
		const view_btn = `
			<button type="button" class="fg-order-card-action fg-quotation-card-view" ${name_attr}>
				${icon("eye", "fg-icon-sm")} ${__("VER COTIZACIÓN")}
			</button>
		`;

		const billing_status = q.fg_billing_review_status || "Borrador";

		if (billing_status === "Pendiente de Facturación") {
			return `<div class="fg-quotation-card-actions">${view_btn}</div>`;
		}

		if (q.status === "Draft") {
			return `
				<div class="fg-quotation-card-actions">
					${view_btn}
					<button type="button" class="fg-order-card-action fg-quotation-card-edit" ${name_attr}>
						${icon("pencil", "fg-icon-sm")} ${__("EDITAR")}
					</button>
				</div>
			`;
		}

		const edit_btn = `
			<button type="button" class="fg-order-card-action fg-quotation-card-modify" ${name_attr}>
				${icon("pencil", "fg-icon-sm")} ${__("EDITAR")}
			</button>
		`;
		const send_btn =
			billing_status === "Borrador" || billing_status === "Devuelta"
				? `
					<button type="button" class="fg-order-card-action fg-quotation-card-send-billing" ${name_attr}>
						${icon("send", "fg-icon-sm")} ${__("ENVIAR A FACTURACIÓN")}
					</button>
				`
				: "";

		// Commit 25.15, section 21 -- VER PDF/DESCARGAR PDF ONLY for the
		// vigente (docstatus != 2), Aprobada version. `docstatus` guards
		// against the exact scenario section 20 warns about: editing an
		// already-Aprobada Quotation (allowed, Commit 25.13's own design)
		// cancels it and creates a new amendment reset to "Borrador" -- the
		// OLD, now-cancelled one keeps `fg_billing_review_status ==
		// "Aprobada"` frozen forever (nothing ever re-reads/rewrites it
		// after cancel), so `billing_status` ALONE is not enough here.
		// Never shown for Borrador/Pendiente de Facturación/Devuelta
		// (already excluded above/by billing_status) or Cancelled
		// (`q.status === "Cancelled"`, native field, independent check).
		const aprobada_vigente = billing_status === "Aprobada" && q.docstatus !== 2 && q.status !== "Cancelled";

		const pdf_btns = aprobada_vigente
			? `
					<button type="button" class="fg-order-card-action fg-quotation-card-view-pdf" ${name_attr}>
						${icon("file-text", "fg-icon-sm")} ${__("VER PDF")}
					</button>
					<button type="button" class="fg-order-card-action fg-quotation-card-download-pdf" ${name_attr}>
						${icon("download", "fg-icon-sm")} ${__("DESCARGAR PDF")}
					</button>
				`
			: "";

		// Commit 25.17 -- "ENVIAR A PEDIDOS" is the commercial CTA once
		// Aprobada+vigente (section 2), same eligibility as pdf_btns above
		// -- deliberately reusing `aprobada_vigente`, never a second,
		// slightly-different condition that could drift out of sync with
		// it. Mutually exclusive with itself: `q.sales_order` (set by
		// get_my_quotations()/get_quotation_detail(), Commit 25.17) means
		// a Sales Order already traces back to this exact Quotation --
		// "VER PEDIDO" replaces "ENVIAR A PEDIDOS" entirely, section 12's
		// own explicit requirement, never both buttons at once.
		const pedido_btn = aprobada_vigente
			? q.sales_order
				? `
					<button type="button" class="fg-order-card-action fg-quotation-card-view-pedido" ${name_attr} data-sales-order="${frappe.utils.escape_html(q.sales_order.name)}">
						${icon("eye", "fg-icon-sm")} ${__("VER PEDIDO")}
					</button>
				`
				: `
					<button type="button" class="fg-order-card-action fg-quotation-card-send-pedido fg-quotation-card-cta" ${name_attr}>
						${icon("send", "fg-icon-sm")} ${__("ENVIAR A PEDIDOS")}
					</button>
				`
			: "";

		return `<div class="fg-quotation-card-actions">${view_btn}${edit_btn}${send_btn}${pdf_btns}${pedido_btn}</div>`;
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

	// Commit 25.20 -- bound ONCE per full render_quotations_section() --
	// never re-bound by the search `input` handler itself (see ventas.js's
	// own bind_orders_search_events() for why that matters).
	bind_quotations_search_events() {
		this.$body.find(".fg-search-input").on("input", (e) => {
			this.quotation_search = $(e.currentTarget).val();
			this.$body.find(".fg-search-clear").toggleClass("is-visible", !!this.quotation_search.trim());
			this.$body.find(".fg-quotations-results").html(this.render_quotations_results_html());
			this.bind_quotations_results_events();
		});
		this.$body.find(".fg-search-clear").on("click", () => {
			this.quotation_search = "";
			this.$body.find(".fg-quotations-section").html(this.render_quotations_section());
			this.bind_quotations_section_events();
		});
	}

	bind_quotations_results_events() {
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
		this.$body.find(".fg-quotation-card-modify").on("click", (e) => {
			this.open_modify_cotizacion($(e.currentTarget).data("quotation-name"));
		});
		this.$body.find(".fg-quotation-card-send-billing").on("click", (e) => {
			this.confirm_send_to_billing($(e.currentTarget).data("quotation-name"));
		});
		this.$body.find(".fg-quotation-card-view-pdf").on("click", (e) => {
			open_fabrigray_quotation_pdf($(e.currentTarget).data("quotation-name"));
		});
		this.$body.find(".fg-quotation-card-download-pdf").on("click", (e) => {
			download_fabrigray_quotation_pdf($(e.currentTarget).data("quotation-name"));
		});
		this.$body.find(".fg-quotation-card-send-pedido").on("click", (e) => {
			this.confirm_send_to_pedidos($(e.currentTarget).data("quotation-name"));
		});
		this.$body.find(".fg-quotation-card-view-pedido").on("click", (e) => {
			open_sales_order_form($(e.currentTarget).data("sales-order"));
		});
	}

	bind_quotations_section_events() {
		this.bind_quotations_search_events();
		this.bind_quotations_results_events();
	}

	// Commit 25.13 -- "ENVIAR A FACTURACIÓN". A plain frappe.confirm() is
	// enough here (unlike Ventas' own cancel dialog, Commit 25.12) --
	// there is no reason/note to collect on THIS side, only a yes/no
	// commitment; every real validation (customer/items/qty/rate) is
	// server-side in send_quotation_to_billing() itself, re-derived from
	// the document, never trusted from this confirm alone.
	confirm_send_to_billing(name) {
		if (!name) return;
		frappe.confirm(__("¿Enviar esta cotización a Facturación para su revisión?"), () => {
			this.set_busy(true);
			this.call("send_quotation_to_billing", { quotation_name: name })
				.then(() => {
					frappe.show_alert({ message: __("Cotización enviada a Facturación."), indicator: "green" }, 5);
					this.load_dashboard();
				})
				.catch(() => {
					// The server already showed the real validation error via its
					// own default frappe.call error dialog.
				})
				.finally(() => this.set_busy(false));
		});
	}

	// Commit 25.17 -- "ENVIAR A PEDIDOS". `q` is read straight out of
	// `this.quotations` (already loaded for the dashboard/card list) --
	// customer_name/item_count/total_qty are all already-fetched,
	// non-economic operational fields (see get_my_quotations()'s own
	// docstring), so this dialog needs no extra round-trip. Deliberately
	// NO total/price/discount anywhere in this dialog -- this whole
	// module's own standing "Vendedora never sees an economic field"
	// policy applies here too, confirmed explicitly for this exact screen
	// (Commit 25.17 review) rather than assumed.
	confirm_send_to_pedidos(name) {
		if (!name) return;
		const q = (this.quotations || []).find((row) => row.name === name);
		if (!q) return;

		const customer_label = frappe.utils.escape_html(q.customer_name || q.customer || "—");

		const d = new frappe.ui.Dialog({
			title: __("Enviar cotización a pedidos"),
			fields: [
				{
					fieldtype: "HTML",
					options: `
						<div class="fg-pedido-confirm">
							<div class="fg-pedido-confirm-row">
								<span>${__("Cliente")}</span>
								<strong>${customer_label}</strong>
							</div>
							<div class="fg-pedido-confirm-row">
								<span>${__("Referencias")}</span>
								<strong>${q.item_count}</strong>
							</div>
							<div class="fg-pedido-confirm-row">
								<span>${__("Unidades")}</span>
								<strong>${format_qty(q.total_qty)}</strong>
							</div>
							<p class="fg-pedido-confirm-msg">
								${__(
									"Se creará un pedido de venta con los productos y valores aprobados por Facturación y será enviado al flujo de alistamiento."
								)}
							</p>
						</div>
					`,
				},
			],
			primary_action_label: __("CREAR PEDIDO"),
			primary_action: () => {
				d.hide();
				this.set_busy(true);
				this.call("create_sales_order_from_quotation", { quotation_name: name })
					.then((result) => this.load_dashboard().then(() => this.open_pedido_success_dialog(result)))
					.catch(() => {
						// The server already showed the real validation error via its
						// own default frappe.call error dialog.
					})
					.finally(() => this.set_busy(false));
			},
			secondary_action_label: __("CANCELAR"),
			secondary_action: () => d.hide(),
		});
		d.show();
	}

	// Commit 25.17, section 19 -- shown once, right after a successful
	// (or idempotent-repeat, `already_exists: true` -- same dialog either
	// way, there is nothing different to tell her) create_sales_order_
	// from_quotation() call. load_dashboard() (called BEFORE this, see
	// confirm_send_to_pedidos() above) has already refreshed the card
	// itself in place -- ENVIAR A PEDIDOS is already gone/replaced by VER
	// PEDIDO underneath this dialog by the time it opens, no full page
	// reload anywhere in this flow (section 19's own explicit ask).
	open_pedido_success_dialog(result) {
		const d = new frappe.ui.Dialog({
			title: __("Pedido creado correctamente"),
			fields: [
				{
					fieldtype: "HTML",
					options: `
						<div class="fg-pedido-confirm">
							<div class="fg-pedido-confirm-row">
								<span>${__("Pedido")}</span>
								<strong>${frappe.utils.escape_html(result.sales_order)}</strong>
							</div>
						</div>
					`,
				},
			],
			primary_action_label: __("VER PEDIDO"),
			primary_action: () => {
				d.hide();
				open_sales_order_form(result.sales_order);
			},
			secondary_action_label: __("CERRAR"),
			secondary_action: () => d.hide(),
		});
		d.show();
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
		this._item_details_cache = new Map();
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
		this._item_details_cache = new Map();
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

	// Commit 25.13 -- reuses the same "Nueva cotización" screen too, but
	// for an already-SUBMITTED Quotation (real docstatus=1 -- the normal
	// case: create_and_submit_quotation() always submits immediately, see
	// this file's own top comment). Prefilled via get_quotation_detail()
	// (never get_editable_quotation(), which requires docstatus==0 and
	// would reject this) -- same non-economic response shape either way.
	// Saving dispatches to save_submitted_modification() ->
	// modify_submitted_quotation() (cancel+amend), never
	// update_draft_quotation() -- see confirm_quotation()'s own dispatch.
	open_modify_cotizacion(name) {
		if (!name) return;
		this.nc = this.blank_nueva_cotizacion_state();
		this._item_details_cache = new Map();
		this.state.view = "nueva_cotizacion";
		this.set_busy(true);

		this.call("get_quotation_detail", { name: name })
			.then((detail) => {
				this.nc.modifying_quotation_name = detail.name;
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
		const editing = !!this.nc.editing_quotation_name || !!this.nc.modifying_quotation_name;
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
				<div class="fg-item-mode-switch" role="tablist">
					<button type="button" class="fg-item-mode-btn ${this.nc.item_mode === "manual" ? "is-active" : ""}" data-mode="manual" role="tab" aria-selected="${this.nc.item_mode === "manual"}">
						${icon("search", "fg-icon-sm")} ${__("Buscar manualmente")}
					</button>
					<button type="button" class="fg-item-mode-btn ${this.nc.item_mode === "quick" ? "is-active" : ""}" data-mode="quick" role="tab" aria-selected="${this.nc.item_mode === "quick"}">
						${icon("clipboard-list", "fg-icon-sm")} ${__("Cotización rápida")}
					</button>
				</div>
				<div class="fg-item-mode-body"></div>
			</div>

			<div class="fg-np-section">
				<div class="fg-np-section-title">${__("3. Resumen de la cotización")}</div>
				<div class="fg-np-summary"></div>
			</div>
		`);
		this.render_customer_area();
		this.render_item_mode_body();
		this.render_summary();
		this.bind_nueva_cotizacion_events();
	}

	// Commit 25.26 -- renders whichever input mode is active into
	// "2. Agregar productos". Manual mode = the buscador rápido (fresh, empty
	// prompt); quick mode = the Cotización rápida panel, re-rendered from
	// this.nc.quick so switching back and forth never loses its text/lines.
	// Neither branch touches this.nc.cart.
	render_item_mode_body() {
		const $wrap = this.$body.find(".fg-item-mode-body");
		if (this.nc.item_mode === "quick") {
			this.close_item_results();
			$wrap.html(this.quick_quote_panel_html());
			this.bind_quick_quote_panel_events();
			this.render_quick_quote_lines();
			return;
		}
		$wrap.html(`
			<div class="fg-search-box">
				${icon("search")}
				<input type="text" class="fg-search-box-input fg-item-search-input" placeholder="${__("Buscar por nombre o código...")}" autocomplete="off" aria-label="${__("Buscar producto")}">
			</div>
			<div class="fg-item-results fg-qs-results" role="listbox"></div>
		`);
		this.close_item_results();
		this.bind_item_search_input();
	}

	set_item_mode(mode) {
		if (mode !== "manual" && mode !== "quick") return;
		if (mode === this.nc.item_mode) return;
		this.nc.item_mode = mode;
		this.$body.find(".fg-item-mode-btn").each((i, el) => {
			const active = $(el).data("mode") === mode;
			$(el).toggleClass("is-active", active).attr("aria-selected", String(active));
		});
		this.render_item_mode_body();
		if (mode === "manual") this.$body.find(".fg-item-search-input").trigger("focus");
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
		this.$body.find(".fg-item-mode-btn").on("click", (e) => this.set_item_mode($(e.currentTarget).data("mode")));
	}

	// (Re)bound every time render_item_mode_body() puts the manual search
	// box back on screen.
	bind_item_search_input() {
		const $item_input = this.$body.find(".fg-item-search-input");
		const debounced_item_search = frappe.utils.debounce((txt) => this.search_items(txt), 300);
		$item_input.on("input", (e) => {
			const txt = $(e.currentTarget).val();
			if (!txt || !txt.trim()) {
				this.close_item_results();
				return;
			}
			this.nc.search_dismissed = false;
			debounced_item_search(txt);
		});
		$item_input.on("keydown", (e) => this.handle_item_search_keydown(e));
	}

	// -- Buscador rápido: teclado (Commit 25.26) ---------------------------------
	//
	// ArrowDown/ArrowUp move the active row, Enter adds the active row
	// (through add_item_from_search(), the SAME function the "+ AGREGAR"
	// button calls -- one add path, never two), Escape closes the results.
	// Enter does nothing while the rendered results don't belong to the
	// input's current text (results_txt) -- typing fast and pressing Enter
	// before the new search lands must never add a product from the
	// previous query.
	handle_item_search_keydown(e) {
		const results = this.nc.item_results;
		const current_txt = $(e.currentTarget).val();
		const fresh = results.length > 0 && this.nc.results_txt === current_txt;

		if (e.key === "ArrowDown") {
			if (!fresh) return;
			e.preventDefault();
			this.set_active_result(Math.min(this.nc.active_index + 1, results.length - 1));
		} else if (e.key === "ArrowUp") {
			if (!fresh) return;
			e.preventDefault();
			this.set_active_result(Math.max(this.nc.active_index - 1, 0));
		} else if (e.key === "Enter") {
			e.preventDefault();
			if (!fresh || this.nc.active_index < 0) return;
			this.add_item_from_search(this.nc.active_index);
		} else if (e.key === "Escape") {
			e.preventDefault();
			this.close_item_results();
		}
	}

	// Moves the highlight without re-rendering the list (keeps focus and
	// scroll untouched), scrolling the new active row into view if needed.
	set_active_result(index) {
		this.nc.active_index = index;
		const $rows = this.$body.find(".fg-qs-results .fg-qs-row");
		$rows.removeClass("is-active").attr("aria-selected", "false");
		const $active = $rows.eq(index);
		$active.addClass("is-active").attr("aria-selected", "true");
		if ($active.length && $active[0].scrollIntoView) $active[0].scrollIntoView({ block: "nearest" });
	}

	// Escape / empty input: invalidates any in-flight search (same
	// _item_search_seq guard search_items() already relies on) and goes back
	// to the empty prompt. Leaves the input's own text alone.
	close_item_results() {
		this._item_search_seq++;
		this.nc.search_dismissed = true;
		this.nc.item_results = [];
		this.nc.active_index = -1;
		this.nc.results_txt = "";
		this.render_item_results_empty_prompt();
	}

	// THE one add path for the quick search -- called by Enter and by the
	// "+ AGREGAR" button alike. Always +1 over whatever the cart already
	// holds for that item_code (this.nc.cart is a Map keyed by item_code,
	// so re-adding never creates a second line), through the existing
	// set_cart_qty(). Stock is never checked here -- stock 0 adds exactly
	// like any other product. Then: clear input, close results, refocus
	// the input so the next product can be typed straight away.
	add_item_from_search(index) {
		const r = this.nc.item_results[index];
		if (!r) return;
		this.set_cart_qty(r.item_code, this.cart_qty(r.item_code) + 1);
		const $input = this.$body.find(".fg-item-search-input");
		$input.val("");
		this.close_item_results();
		$input.trigger("focus");
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

	// -- Paso 2: Productos (buscador rápido, Commit 25.26) ----------------------
	// ventas.search_items() finds the products; ONE batched
	// get_quick_search_item_details() call adds, for display only, the
	// public selling price (or "SIN PRECIO") and an informative stock
	// figure. Stock never blocks anything: a product may be added at any
	// quantity regardless of physical stock, stock 0 included.

	search_items(txt) {
		if (!txt || !txt.trim()) {
			this.close_item_results();
			return Promise.resolve();
		}
		if (this.nc.search_dismissed) return Promise.resolve();
		const seq = ++this._item_search_seq;
		this.render_item_results_skeleton();
		return this.call_ventas("search_items", { txt: txt }).then((results) => {
			if (seq !== this._item_search_seq) return;
			const rows = results || [];
			return this.hydrate_item_details(rows).then(() => {
				if (seq !== this._item_search_seq) return;
				this.nc.item_results = rows;
				this.nc.results_txt = txt;
				// First row pre-selected so "deseng" + Enter adds immediately.
				this.nc.active_index = rows.length ? 0 : -1;
				this.render_item_results();
			});
		});
	}

	// Commit 25.26 -- ONE batched get_quick_search_item_details() call per
	// search (search_items() returns at most 20 rows, the endpoint's own
	// maximum), only for item_codes not already cached in this "Nueva
	// cotización" session -- never one get_item_info() call per row.
	hydrate_item_details(results) {
		const missing = results.map((r) => r.item_code).filter((code) => !this._item_details_cache.has(code));
		if (!missing.length) return Promise.resolve();
		return this.call("get_quick_search_item_details", { item_codes: missing }).then((rows) => {
			for (const row of rows || []) this._item_details_cache.set(row.item_code, row);
		});
	}

	render_item_results() {
		const $results = this.$body.find(".fg-item-results");
		const results = this.nc.item_results;

		if (!results.length) {
			$results.html(`<div class="fg-empty">${__("No se encontraron productos.")}</div>`);
			return;
		}

		$results.html(results.map((r, i) => this.render_item_result_card(r, i)).join(""));
		this.bind_item_result_events();
	}

	// Commit 25.26 -- compact quick-search row: name, code, public price (or
	// "SIN PRECIO"), stock (purely informative, 0 shown as 0, never
	// disables anything) and "+ AGREGAR". Both values come from
	// _item_details_cache for display only.
	render_item_result_card(r, index) {
		const details = this._item_details_cache.get(r.item_code);
		const has_price = !!details && details.has_public_price && details.public_price != null;
		const price_html = has_price
			? `<span class="fg-qs-price">${format_money(details.public_price)}</span>`
			: `<span class="fg-qs-price fg-qs-price--none">${__("SIN PRECIO")}</span>`;
		const has_qty = !!details && details.qty_disponible != null;
		const stock_class = !has_qty ? "fg-qs-stock--none" : flt(details.qty_disponible) > 0 ? "fg-qs-stock--ok" : "fg-qs-stock--zero";
		const stock_txt = has_qty ? format_qty(details.qty_disponible) : "—";
		const in_cart = this.cart_qty(r.item_code);
		const active = index === this.nc.active_index;

		return `
			<div class="fg-qs-row ${active ? "is-active" : ""} ${in_cart > 0 ? "fg-qs-row--in-cart" : ""}" role="option" aria-selected="${active}" data-index="${index}" data-item-code="${frappe.utils.escape_html(r.item_code)}">
				<div class="fg-qs-info">
					<div class="fg-qs-name">${frappe.utils.escape_html(r.item_name)}</div>
					<div class="fg-qs-code">${frappe.utils.escape_html(r.item_code)}</div>
					<div class="fg-qs-meta">
						<span>${__("Precio público")}: ${price_html}</span>
						<span class="${stock_class}">${__("Stock")}: ${stock_txt}</span>
						<span class="fg-qs-in-cart">${in_cart > 0 ? `${__("En cotización")}: ${format_qty(in_cart)}` : ""}</span>
					</div>
				</div>
				<button type="button" class="fg-btn fg-btn--solid-primary fg-qs-add">${icon("plus", "fg-icon-sm")} ${__("AGREGAR")}</button>
			</div>
		`;
	}

	bind_item_result_events() {
		this.$body.find(".fg-qs-results .fg-qs-row").each((i, el) => {
			const $row = $(el);
			const index = cint($row.data("index"));
			// mousedown + preventDefault keeps focus in the search input on
			// desktop, then click runs the SAME add path as Enter.
			$row.find(".fg-qs-add")
				.on("mousedown", (e) => e.preventDefault())
				.on("click", () => this.add_item_from_search(index));
			$row.on("mouseenter", () => this.set_active_result(index));
		});
	}

	// -- Cotización rápida (Commit 25.26) ---------------------------------------
	//
	// Reuses Ventas' quick-order engine as-is: the SAME whitelisted,
	// read-only fabergray_erp.api.ventas.parse_quick_order endpoint (called
	// through call_ventas(), exactly like search_items()), so the same text
	// yields the same interpretation, candidates, confidence, ambiguity and
	// preselection in both pages -- nothing is parsed or scored here. The
	// accepted format is that parser's own (one product per line, quantity
	// at the start: "5 galones desengrasante"); no new syntax.
	//
	// Only a line the SERVER preselected (preselected_item: high confidence
	// AND not ambiguous) starts selected; every other line needs an explicit
	// pick or "Ignorar" before "AGREGAR A COTIZACIÓN" enables. Nothing in the
	// pasted text can set a price: this section only ever produces
	// {item_code, qty} additions to the shared cart, through set_cart_qty().

	quick_quote_panel_html() {
		return `
			<div class="fg-cq-panel">
				<div class="fg-cq-intro">
					<div class="fg-cq-title">${__("COTIZACIÓN RÁPIDA")}</div>
					<div class="fg-cq-subtitle">
						${__("Pega o escribe varios productos con sus cantidades.")}
					</div>
				</div>
				<div class="fg-cq-help">
					<div>${__("Pega o escribe un producto por línea.")}</div>
					<div>${__("Escribe primero la cantidad y después el producto.")}</div>
					<div class="fg-cq-help-label">${__("Ejemplo:")}</div>
					<pre class="fg-cq-help-example">${frappe.utils.escape_html(QUICK_QUOTE_EXAMPLE)}</pre>
				</div>
				<label class="fg-cq-label" for="fg-cq-textarea">${__("Productos")}</label>
				<textarea
					id="fg-cq-textarea"
					class="fg-cq-textarea"
					rows="6"
					placeholder="${frappe.utils.escape_html(QUICK_QUOTE_EXAMPLE)}"
				>${frappe.utils.escape_html(this.nc.quick.text || "")}</textarea>
				<div class="fg-cq-actions-row">
					<button type="button" class="fg-btn fg-btn--solid-primary fg-cq-process-btn">
						${icon("search", "fg-icon-sm")} ${__("PROCESAR COTIZACIÓN")}
					</button>
					<button type="button" class="fg-btn fg-btn--ghost fg-cq-clear-btn">
						${icon("trash-2", "fg-icon-sm")} ${__("Limpiar")}
					</button>
				</div>
				<div class="fg-cq-results"></div>
				<div class="fg-cq-apply-bar"></div>
			</div>
		`;
	}

	bind_quick_quote_panel_events() {
		this.$body.find(".fg-cq-textarea").on("input", (e) => {
			this.nc.quick.text = $(e.currentTarget).val();
		});
		this.$body.find(".fg-cq-process-btn").on("click", () => this.process_quick_quote());
		this.$body.find(".fg-cq-clear-btn").on("click", () => this.clear_quick_quote());
	}

	// Client-side mirrors of parse_quick_order()'s own input limits
	// (QUICK_ORDER_MAX_CHARS/QUICK_ORDER_MAX_LINES in api/ventas.py) so the
	// Vendedora gets a Cotización-worded message instead of the server's
	// "pedido" wording; the server still enforces both on its own.
	process_quick_quote() {
		if (this.nc.quick.loading) return;
		const text = (this.$body.find(".fg-cq-textarea").val() || "").trim();
		if (!text) {
			frappe.show_alert({ message: __("Escribe o pega productos antes de procesar."), indicator: "orange" });
			return;
		}
		if (text.length > QUICK_QUOTE_MAX_CHARS) {
			frappe.show_alert({ message: __("El texto es demasiado largo (máximo {0} caracteres).", [QUICK_QUOTE_MAX_CHARS]), indicator: "orange" });
			return;
		}
		if (text.split("\n").filter((l) => l.trim()).length > QUICK_QUOTE_MAX_LINES) {
			frappe.show_alert({ message: __("Máximo {0} líneas por cotización rápida.", [QUICK_QUOTE_MAX_LINES]), indicator: "orange" });
			return;
		}

		this.nc.quick.loading = true;
		this.nc.quick.text = text;
		const $btn = this.$body.find(".fg-cq-process-btn").prop("disabled", true).addClass("fg-btn--loading");
		this.render_quick_quote_skeleton();

		// Re-processing REPLACES the lines, never appends.
		this.call_ventas("parse_quick_order", { text: text })
			.then((response) => {
				this.nc.quick.lines = ((response && response.lines) || []).map((line) => this.build_quick_quote_line(line));
			})
			.catch(() => {
				// frappe.call's own error dialog already showed the message.
			})
			.finally(() => {
				this.nc.quick.loading = false;
				$btn.prop("disabled", false).removeClass("fg-btn--loading");
				this.render_quick_quote_lines();
			});
	}

	// Server data copied as-is; `selected` starts ONLY from the server's own
	// preselected_item, `ignored` is the Vendedora's explicit opt-out.
	build_quick_quote_line(server_line) {
		const pre = server_line.preselected_item;
		return {
			source_text: server_line.source_text,
			qty: server_line.qty,
			confidence: server_line.confidence,
			ambiguous: server_line.ambiguous,
			candidates: server_line.candidates || [],
			selected: pre ? { item_code: pre.item_code, item_name: pre.item_name, stock_uom: pre.stock_uom } : null,
			ignored: false,
		};
	}

	render_quick_quote_skeleton() {
		this.$body.find(".fg-cq-results").html(`
			<div class="fg-skeleton fg-product-skeleton"></div>
			<div class="fg-skeleton fg-product-skeleton"></div>
		`);
		this.$body.find(".fg-cq-apply-bar").empty();
	}

	render_quick_quote_lines() {
		const $area = this.$body.find(".fg-cq-results");
		if (!$area.length) return; // mode switched away before the response landed
		const lines = this.nc.quick.lines;
		if (!lines.length) {
			$area.html(`<div class="fg-empty fg-empty--sm">${__('Escribe los productos y presiona "PROCESAR COTIZACIÓN".')}</div>`);
			this.$body.find(".fg-cq-apply-bar").empty();
			return;
		}
		$area.html(lines.map((line, i) => this.render_quick_quote_line(line, i)).join(""));
		lines.forEach((_, i) => this.bind_quick_quote_line_events(i));
		this.render_quick_quote_apply_bar();
	}

	render_quick_quote_line_at(index) {
		const line = this.nc.quick.lines[index];
		const $old = this.$body.find(`.fg-cq-line[data-index="${index}"]`);
		if (!line || !$old.length) return;
		$old.replaceWith(this.render_quick_quote_line(line, index));
		this.bind_quick_quote_line_events(index);
		this.render_quick_quote_apply_bar();
	}

	// Same five states, same order and same rules as Ventas' own
	// quick_order_line_status() -- "no encontrado" is checked first.
	quick_quote_line_status(line) {
		if (!line.candidates.length) return { label: __("No encontrado"), mod: "not-found" };
		if (line.confidence === "high" && !line.ambiguous) return { label: __("Encontrado"), mod: "high" };
		if (line.confidence === "high" && line.ambiguous) return { label: __("Revisar alternativas"), mod: "review" };
		if (line.confidence === "medium") return { label: __("Revisar sugerencia"), mod: "review" };
		return { label: __("Selecciona producto"), mod: "low" };
	}

	render_quick_quote_line(line, index) {
		const status = this.quick_quote_line_status(line);
		const title = line.selected ? line.selected.item_name : line.source_text;
		const candidates_html = line.candidates.length
			? line.candidates.map((c) => this.render_quick_quote_candidate(line, c, index)).join("")
			: `<div class="fg-empty fg-empty--sm">${__('Sin coincidencias. Ignora la línea o búscalo en "Buscar manualmente".')}</div>`;

		return `
			<div class="fg-cq-line ${line.ignored ? "fg-cq-line--ignored" : ""}" data-index="${index}">
				<div class="fg-cq-line-header">
					<div class="fg-cq-line-main">
						<div class="fg-cq-line-title">${frappe.utils.escape_html(title)}</div>
						${line.selected ? `<div class="fg-cq-line-code">${frappe.utils.escape_html(line.selected.item_code)}</div>` : ""}
						<div class="fg-cq-line-source">${__("Texto")}: ${frappe.utils.escape_html(line.source_text)}</div>
					</div>
					<span class="fg-cq-status fg-cq-status--${status.mod}">${status.mod === "high" ? "✓ " : status.mod === "not-found" ? "⚠ " : ""}${status.label}</span>
				</div>
				<div class="fg-cq-line-body">
					<label class="fg-cq-qty">
						<span>${__("Cantidad")}</span>
						<input type="number" inputmode="decimal" class="fg-cq-qty-input" value="${line.qty}" min="0" ${line.ignored ? "disabled" : ""}>
					</label>
					<button type="button" class="fg-btn fg-btn--ghost fg-cq-ignore" aria-pressed="${line.ignored}">
						${line.ignored ? icon("circle-check", "fg-icon-sm") + " " + __("Reactivar") : icon("x", "fg-icon-sm") + " " + __("Ignorar")}
					</button>
				</div>
				<div class="fg-cq-candidates">${candidates_html}</div>
			</div>
		`;
	}

	render_quick_quote_candidate(line, candidate, line_index) {
		const is_selected = !!line.selected && line.selected.item_code === candidate.item_code;
		return `
			<button type="button" class="fg-cq-candidate ${is_selected ? "is-selected" : ""}" data-line="${line_index}" data-item-code="${frappe.utils.escape_html(candidate.item_code)}" aria-pressed="${is_selected}" ${line.ignored ? "disabled" : ""}>
				<span class="fg-cq-candidate-name">${frappe.utils.escape_html(candidate.item_name)}</span>
				<span class="fg-cq-candidate-meta">
					<span>${frappe.utils.escape_html(candidate.item_code)}</span>
					<span class="fg-cq-candidate-score fg-cq-candidate-score--${candidate.confidence}">${candidate.score}%</span>
				</span>
			</button>
		`;
	}

	bind_quick_quote_line_events(index) {
		const $line = this.$body.find(`.fg-cq-line[data-index="${index}"]`);
		$line.find(".fg-cq-qty-input").on("change", (e) => {
			const line = this.nc.quick.lines[index];
			if (!line) return;
			line.qty = Math.max(flt($(e.currentTarget).val()), 0);
			this.render_quick_quote_line_at(index);
		});
		$line.find(".fg-cq-candidate").on("click", (e) => {
			const line = this.nc.quick.lines[index];
			const candidate = line && line.candidates.find((c) => c.item_code === $(e.currentTarget).data("item-code"));
			if (!candidate) return;
			line.selected = { item_code: candidate.item_code, item_name: candidate.item_name, stock_uom: candidate.stock_uom };
			this.render_quick_quote_line_at(index);
		});
		$line.find(".fg-cq-ignore").on("click", () => {
			const line = this.nc.quick.lines[index];
			if (!line) return;
			line.ignored = !line.ignored;
			this.render_quick_quote_line_at(index);
		});
	}

	// Same rule as Ventas' validate_quick_order_lines(): every NON-ignored
	// line must have a selected product and qty > 0, and at least one line
	// must be active. Not-found/ambiguous lines therefore block adding until
	// the Vendedora picks a product or explicitly ignores the line -- never
	// silently dropped, never silently added.
	validate_quick_quote_lines() {
		const active = this.nc.quick.lines.filter((l) => !l.ignored);
		const missing = active.filter((l) => !l.selected || !(flt(l.qty) > 0));
		return { valid: active.length > 0 && missing.length === 0, missing_count: missing.length };
	}

	render_quick_quote_apply_bar() {
		const $bar = this.$body.find(".fg-cq-apply-bar");
		if (!$bar.length) return;
		if (!this.nc.quick.lines.length) {
			$bar.empty();
			return;
		}
		const { valid, missing_count } = this.validate_quick_quote_lines();
		$bar.html(`
			${
				missing_count > 0
					? `<div class="fg-cq-warning">${icon("triangle-alert", "fg-icon-sm")} ${__("Faltan {0} línea(s) por resolver: elige un producto o ignora la línea.", [missing_count])}</div>`
					: ""
			}
			<button type="button" class="fg-btn fg-btn--solid-primary fg-btn--lg fg-cq-apply-btn" ${valid ? "" : "disabled"}>
				${icon("plus", "fg-icon-sm")} ${__("AGREGAR A COTIZACIÓN")}
			</button>
		`);
		$bar.find(".fg-cq-apply-btn").on("click", () => this.apply_quick_quote_to_cart());
	}

	// The one place Cotización rápida touches the cart -- the SAME Map the
	// manual search feeds, through the SAME set_cart_qty(). Lines resolving
	// to the same item_code are summed first, then added on top of what the
	// cart already holds (cart 2 + rápida 5 -> 7, one line). Only
	// {item_code, qty} (+ display name/UOM) ever reaches the cart; stock is
	// never consulted, so stock 0 adds like anything else. The Quotation is
	// NOT created here -- CREAR COTIZACIÓN / GUARDAR CAMBIOS stays the only
	// path to create_and_submit_quotation()/update_*/modify_*.
	apply_quick_quote_to_cart() {
		if (!this.validate_quick_quote_lines().valid) return;

		const additions = new Map(); // item_code -> {qty, item_name, stock_uom}
		for (const line of this.nc.quick.lines) {
			if (line.ignored || !line.selected) continue;
			const qty = flt(line.qty);
			if (qty <= 0) continue;
			const current = additions.get(line.selected.item_code);
			if (current) {
				current.qty += qty;
			} else {
				additions.set(line.selected.item_code, { qty: qty, item_name: line.selected.item_name, stock_uom: line.selected.stock_uom });
			}
		}
		if (!additions.size) return;

		for (const [item_code, a] of additions) {
			this.set_cart_qty(item_code, this.cart_qty(item_code) + a.qty, { item_name: a.item_name, stock_uom: a.stock_uom });
		}

		frappe.show_alert(
			{ message: __("{0} producto(s) agregado(s) a la cotización", [additions.size]), indicator: "green" },
			5
		);
		this.clear_quick_quote();
	}

	// "Limpiar" and the post-apply cleanup share this -- never touches the cart.
	clear_quick_quote() {
		this.nc.quick = this.blank_quick_quote_state();
		this.$body.find(".fg-cq-textarea").val("");
		this.render_quick_quote_lines();
	}

	// -- Cart / Paso 3: Resumen -----------------------------------------------

	cart_qty(item_code) {
		const line = this.nc.cart.get(item_code);
		return line ? line.qty : 0;
	}

	// `meta` ({item_name, stock_uom}, optional) -- display fallback for an
	// item that never appeared in the manual search results (Cotización
	// rápida). Name/UOM only: nothing economic is ever stored per line.
	set_cart_qty(item_code, qty, meta) {
		qty = flt(qty);
		if (qty <= 0) {
			this.nc.cart.delete(item_code);
		} else {
			const result = this.nc.item_results.find((r) => r.item_code === item_code);
			const existing = this.nc.cart.get(item_code);
			this.nc.cart.set(item_code, {
				item_code: item_code,
				item_name: (result && result.item_name) || (existing && existing.item_name) || (meta && meta.item_name) || item_code,
				stock_uom: (result && result.stock_uom) || (existing && existing.stock_uom) || (meta && meta.stock_uom) || "",
				qty: qty,
			});
		}
		this.sync_item_result_card(item_code);
		this.render_summary();
		this.refresh_confirm_state();
	}

	// Updates only the one affected quick-search row's "En cotización"
	// badge (if it is currently rendered) instead of re-rendering the whole
	// list -- keeps the search input focused while editing the cart.
	sync_item_result_card(item_code) {
		const $row = this.$body.find(`.fg-qs-results .fg-qs-row[data-item-code="${css_escape(item_code)}"]`);
		if (!$row.length) return;
		const qty = this.cart_qty(item_code);
		$row.toggleClass("fg-qs-row--in-cart", qty > 0);
		$row.find(".fg-qs-in-cart").text(qty > 0 ? `${__("En cotización")}: ${format_qty(qty)}` : "");
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
				${icon("check")} ${
					this.nc.editing_quotation_name || this.nc.modifying_quotation_name
						? __("GUARDAR CAMBIOS")
						: __("CREAR COTIZACIÓN")
				}
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

		if (this.nc.modifying_quotation_name) {
			// Commit 25.13 -- same "no confirmation dialog" convention as
			// save_draft_edit() above -- straight to
			// modify_submitted_quotation(), which does the real
			// cancel+amend server-side (and invalidates any previous
			// billing-review approval as a side effect, section 12).
			this.save_submitted_modification(payload);
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

	// Commit 25.13 -- modify_submitted_quotation() does the real
	// cancel+amend server-side; a stale billing-review approval never
	// survives it (section 12, enforced server-side -- this is only the
	// client half of that already-real guarantee).
	save_submitted_modification(payload) {
		this.busy = true;
		const $btn = this.$body.find(".fg-confirm-btn").prop("disabled", true).addClass("fg-btn--loading");

		this.call("modify_submitted_quotation", {
			name: this.nc.modifying_quotation_name,
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
				// server's default error dialog already showed the real message
				// (e.g. "pendiente de revisión de Facturación y no puede
				// editarse mientras tanto").
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

// Commit 25.15 review fix, section 2/4 -- NEITHER of these freely builds
// a `/printview`/`download_pdf` URL from just a `name` any more (that
// WOULD skip server-side validation up front, relying only on the
// `before_print` hook firing correctly downstream -- exactly what the
// review explicitly ruled out as "JS must never be the only defense").
//
// VER PDF calls `get_fabrigray_quotation_pdf_view_url()` FIRST (a real
// `frappe.call()`, runs the full "Aprobada"/not-cancelled/same-Company
// check server-side, api/cotizaciones.py) and only navigates to the URL
// it hands back. A blank tab is opened SYNCHRONOUSLY, on the click itself
// (`window.open("about:blank")`, before the async call even starts) and
// only its own `.location` is set once the server responds -- opening a
// new tab from inside a `.then()` callback, after an AJAX round trip,
// gets silently blocked as a popup by most browsers; a tab opened
// synchronously on the original click gesture does not.
//
// DESCARGAR PDF still navigates directly to its own endpoint
// (`download_fabrigray_quotation_pdf`, a whitelisted GET) -- that
// endpoint now ALSO runs the full eligibility check itself, first, before
// generating anything (same function, same check, no longer relying
// solely on `before_print` either) -- see its own docstring. The format
// name is hardcoded server-side in BOTH endpoints; neither ever accepts
// one from the client, so nothing here could smuggle in a different
// print format even if it wanted to.
function open_fabrigray_quotation_pdf(name) {
	if (!name) return;
	const tab = window.open("about:blank");
	// frappe.xcall() -- unlike a bare frappe.call(), returns a REAL
	// Promise (confirmed by reading frappe/public/js/frappe/request.js
	// directly) that resolves straight to `r.message`, already unwrapped.
	frappe
		.xcall("fabergray_erp.api.cotizaciones.get_fabrigray_quotation_pdf_view_url", { quotation_name: name })
		.then((url) => {
			if (url && tab) {
				tab.location = url;
			} else if (tab) {
				tab.close();
			}
		})
		.catch(() => {
			// The server already showed the real error via frappe.call()'s
			// own default error dialog (e.g. "La cotización debe estar
			// aprobada...") -- nothing here assumes it succeeded.
			if (tab) tab.close();
		});
}

function download_fabrigray_quotation_pdf(name) {
	if (!name) return;
	window.open(
		frappe.urllib.get_full_url(
			"/api/method/fabergray_erp.api.cotizaciones.download_fabrigray_quotation_pdf?quotation_name=" +
				encodeURIComponent(name)
		)
	);
}

// Commit 25.17 -- "VER PEDIDO". Opens the native Desk Form for the Sales
// Order directly (`frappe.set_route("Form", "Sales Order", name)`) --
// deliberately NOT a new, custom deep-link into Page Ventas' own
// dashboard state (that page has no route/URL-param mechanism to open a
// specific order today, and section 22's own "no modificar arquitectura
// general de Ventas" explicitly rules out adding one for this commit).
// Vendedora already holds native read on Sales Order (the same Custom
// DocPerm get_order_detail()/get_my_orders() already rely on) -- the
// Module Profile that hides standard Workspace/Desk navigation for her
// (see hooks.py's own "Home Fabrigray" comment) blocks sidebar/workspace
// browsing only, never a direct doctype Form route.
function open_sales_order_form(name) {
	if (!name) return;
	frappe.set_route("Form", "Sales Order", name);
}

// Commit 25.20 -- unified search bar markup, same shape/classes as every
// other operational Page's own copy (page/ventas/ventas.js's own
// render_search_bar_html() carries the full "why reproduced, not
// imported" comment).
function render_search_bar_html(value) {
	const has_value = !!(value && value.trim());
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

// Commit 25.26 -- mirror of api/ventas.py's QUICK_ORDER_MAX_CHARS/
// QUICK_ORDER_MAX_LINES (server remains the authority).
const QUICK_QUOTE_MAX_CHARS = 10000;
const QUICK_QUOTE_MAX_LINES = 50;
// The official Cotización rápida format IS Ventas' quick-order format (same
// parser): quantity FIRST, then the product, one per line. Shown as help
// and as the textarea placeholder.
const QUICK_QUOTE_EXAMPLE = "5 Desengrasante 1 Galón\n3 Escoba Industrial\n10 Hipoclorito Galón";

function cint(v) {
	return frappe.utils.cint ? frappe.utils.cint(v) : parseInt(v, 10) || 0;
}

// Commit 25.26 -- display-only money formatting for the public price the
// server returned (same frappe.format Currency formatter facturacion.js's
// own money() uses). Never parsed back, never sent.
function format_money(v) {
	return frappe.format(v, { fieldtype: "Currency" }, { inline: true });
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

// Commit 25.13 -- fg_billing_review_status is a SEPARATE workflow from
// Quotation.status above, own badge. A historical Quotation (created
// before this commit) has null/"" here -- section 14's own explicit rule:
// treated identically to "Borrador", never shown as a raw blank badge,
// never inferred/backfilled as Aprobada.
function billing_review_status_meta(status) {
	const map = {
		Borrador: { label: __("Sin enviar a Facturación"), mod: "billing-draft" },
		"Pendiente de Facturación": { label: __("Pendiente de Facturación"), mod: "billing-pending" },
		Aprobada: { label: __("Aprobada por Facturación"), mod: "billing-approved" },
		Devuelta: { label: __("Devuelta por Facturación"), mod: "billing-returned" },
	};
	return map[status] || map["Borrador"];
}

// Commit 25.16 -- the card's own TOP-right badge (`.fg-quotation-card-top`),
// BUGFIX: before this commit that badge was quotation_status_meta(q.status)
// -- native Quotation.status, which stays "Open" ("Pendiente") forever once
// submitted, because this app has never implemented Quotation -> Sales
// Order conversion (the only thing that ever moves native status off
// "Open"). A Quotation Facturación had already approved
// (fg_billing_review_status === "Aprobada") therefore kept showing
// "Pendiente" here, directly contradicting the billing-review strip right
// below it (billing_review_status_meta() above, unchanged, still reads
// "Aprobada por Facturación"). fg_billing_review_status is now this
// badge's own source of truth too, exactly matching
// get_quotation_summary()'s Aprobadas/Pendientes KPI definitions and
// quotation_matches_filter() above -- section 2's own closed mapping,
// short labels (this is the compact badge; the full-sentence one stays
// the second badge below it, unchanged).
//
// docstatus===2/"Cancelled" is checked FIRST, before fg_billing_review_
// status: an old, superseded amendment (get_my_quotations() returns every
// version, including ones a later apply_quotation_price_mode()/
// modify_submitted_quotation() already cancelled+replaced) keeps
// fg_billing_review_status frozen at whatever it read the INSTANT BEFORE
// cancellation -- it can still read "Aprobada" long after ceasing to be
// the vigente document. Without this check first, a dead document would
// show a stale "APROBADA" instead of "CANCELADA" -- the exact same class
// of contradictory badge this commit exists to fix, just on a different
// field.
function quotation_review_badge_meta(q) {
	if (q.docstatus === 2 || q.status === "Cancelled") {
		return { label: __("Cancelada"), mod: "review-cancelled" };
	}
	const map = {
		Borrador: { label: __("Borrador"), mod: "review-draft" },
		"Pendiente de Facturación": { label: __("Pendiente"), mod: "review-pending" },
		Aprobada: { label: __("Aprobada"), mod: "review-approved" },
		Devuelta: { label: __("Devuelta"), mod: "review-returned" },
	};
	return map[q.fg_billing_review_status || "Borrador"] || map["Borrador"];
}
