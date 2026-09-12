// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["facturacion"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Facturación"),
		single_column: true,
	});
	new fabergray_erp.Facturacion(page);
};

// Commit 21.4 built this Page around generate_invoice() (Commit 21.3): a
// per-line "VERIFICADO" checklist (frontend-only, never persisted) gating a
// GENERAR FACTURA button that created a real, submitted Sales Invoice.
// Commit 23.0 replaced that with a single click that flipped a Pick List
// straight to Facturado -- a real regression from the previous flow's own
// per-item review discipline. This correction (same commit) restores the
// review step, this time backed by real server-side persistence
// (api.facturacion.get_invoicing_detail()/set_invoicing_item_checked(), on
// Pick List Item's own fg_invoicing_checked/*_on/*_by Custom Fields) rather
// than a frontend-only Set() -- see api/facturacion.py's own top docstring
// for the full audit trail. mark_as_invoiced() still never creates a Sales
// Invoice/GL Entry/Payment Entry, and nothing in this file calls
// generate_invoice() -- REVISAR PEDIDO opens a review modal, never a Desk
// form, and CONFIRMAR FACTURACIÓN's own confirmation never mentions money
// or "factura electrónica".
//
// get_invoicing_queue() is real, server-side paginated (unlike the old
// get_pending_pick_lists(), fetched whole and filtered/paginated in the
// browser) -- every tab switch, search keystroke (debounced 300ms, same
// idiom as page/jefe_pick_lists/jefe_pick_lists.js) and page click re-hits
// the server. Tab counts come from get_invoicing_summary()'s own
// pendientes/facturados numbers (already fetched alongside the KPIs) rather
// than a second query per tab.
fabergray_erp.Facturacion = class Facturacion {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.facturacion.";
		this.cotizaciones_method_prefix = "fabergray_erp.api.cotizaciones.";
		this.busy = false;

		this.summary = null;
		this.rows = [];
		this.total = 0;
		this.queue_filter = ""; // "" (todos) | "Pendiente" | "Facturado"
		this.queue_search = "";
		this.queue_page = 1;
		this._search_debounce = null;
		// Commit 25.20 -- "Cotizaciones pendientes" own search, client-side
		// (that list is fully fetched, see load_billing_queue()) -- separate
		// state from queue_search above, section 15's own "usar una barra
		// por sección si UX actual lo exige": these are two structurally
		// different documents/lists in the same Page.
		this.billing_search = "";

		// Review modal state -- see open_review_dialog(). Reset every time a
		// modal opens/closes so a stale detail from a previous Pick List can
		// never leak into a newly-opened one.
		this._review_dialog = null;
		this._review_pick_list = null;
		this._review_detail = null;
		this._review_saving_rows = new Set(); // row_name -> in-flight set_invoicing_item_checked() call

		// Commit 25.13 -- "COTIZACIONES PENDIENTES" (billing review of
		// Quotation, entirely separate from the Pick List invoicing queue
		// above -- different document, different API module
		// (fabergray_erp.api.cotizaciones), own dialog state).
		this.billing_summary = null;
		this.billing_quotations = [];
		this._billing_review_dialog = null;
		this._billing_review_quotation = null;
		this._billing_review_detail = null;
		// Commit 25.14 -- "PRECIOS AJUSTADOS". `null` means "no mode
		// selected/detected" (the "Precio personalizado / sin ajustar"
		// state, section 3) -- set on every dialog open/refresh by
		// detect_billing_price_mode(), and again by the user's own click
		// on one of the 3 segmented-control buttons. Purely client-side
		// preview state until "APLICAR PRECIOS" is pressed -- never sent
		// to the server just by changing this.
		this._billing_review_selected_mode = null;
		// Hotfix 25.20.4, section 11 -- true only while an
		// apply_quotation_price_mode() call is actually in flight. Keeps
		// APLICAR PRECIOS (and the 5 mode buttons) disabled for exactly
		// that window so a double click can never fire two cancel+amend
		// cycles; cleared in apply_billing_price_mode()'s own `.finally()`
		// on BOTH the success and the error path, so a server error can
		// never leave the button permanently dead.
		this._billing_review_applying = false;

		this.$app = $('<div class="fg-shell fg-facturacion">').appendTo(this.page.body);
		this.render_shell();
		this.load_all();
	}

	// -------------------------------------------------------------------
	// Thin API wrapper -- frappe.call() itself does NOT return a real
	// Promise (it returns $.ajax()'s jqXHR, whose promise object never
	// implements .finally()). Wrapping it in a real Promise here is what
	// lets every .then()/.catch()/.finally() chain below behave correctly
	// -- the exact bug already fixed in page/bodega/bodega.js and
	// page/cotizaciones/cotizaciones.js, never introduced here in the
	// first place.
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

	call_cotizaciones(method, args) {
		return this._frappe_call(this.cotizaciones_method_prefix + method, args);
	}

	// -------------------------------------------------------------------
	// Shell: header (logo, title, user, refresh) stays fixed.
	// -------------------------------------------------------------------
	render_shell() {
		const fullname = frappe.session.user_fullname || frappe.session.user;
		this.$app.html(`
			<div class="fg-header">
				<div class="fg-header-brand">
					<span class="fg-header-logo">FABRIGRAY</span>
					<span class="fg-header-sep">|</span>
					<span class="fg-header-title">${__("FACTURACIÓN")}</span>
				</div>
				<div class="fg-header-user">
					<div class="fg-header-user-info">
						<div class="fg-header-user-name">${frappe.utils.escape_html(fullname)}</div>
						<div class="fg-header-user-role">${__("Facturación")}</div>
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
		this.busy = !!is_busy;
		this.$app.find(".fg-refresh-btn").prop("disabled", this.busy);
		this.$app.toggleClass("fg-loading", !!is_busy);
	}

	// =====================================================================
	// Load + render (dashboard)
	// =====================================================================
	load_all() {
		this.set_busy(true);
		this.render_skeleton();
		return Promise.all([this.call("get_invoicing_summary"), this.load_queue(), this.load_billing_queue()])
			.then(([summary]) => {
				this.summary = summary;
				this.render_body();
			})
			.catch(() => {
				// The server already showed the real error via frappe.call()'s
				// own default error dialog -- nothing to improvise here.
			})
			.finally(() => this.set_busy(false));
	}

	// Commit 25.13 -- "COTIZACIONES PENDIENTES". Fetched every load_all()
	// alongside the existing Pick List queue -- small, single-page list
	// (no server-side pagination like get_invoicing_queue() has, matching
	// get_pending_billing_review_quotations()'s own "cheap, everything at
	// once" shape, same as page/ventas/ventas.js's own Cancelados list).
	load_billing_queue() {
		return Promise.all([
			this.call_cotizaciones("get_quotation_billing_summary"),
			// Commit 25.20 -- limit: 500 (was the server default 50): the new
			// search bar below filters this list client-side, over whatever
			// this call actually fetched.
			this.call_cotizaciones("get_pending_billing_review_quotations", { limit: 500 }),
		]).then(([summary, quotations]) => {
			this.billing_summary = summary;
			this.billing_quotations = quotations || [];
		});
	}

	refresh_billing_queue() {
		this.set_busy(true);
		return this.load_billing_queue()
			.then(() => {
				this.$body.find(".fg-fact-billing-section").replaceWith(this.render_billing_queue_section());
				this.bind_billing_queue_events();
			})
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	load_queue() {
		return this.call("get_invoicing_queue", {
			status: this.queue_filter || null,
			txt: this.queue_search,
			start: (this.queue_page - 1) * PAGE_SIZE,
			page_length: PAGE_SIZE,
		}).then((res) => {
			this.rows = res.pick_lists || [];
			this.total = res.total || 0;
		});
	}

	refresh_queue() {
		this.set_busy(true);
		return this.load_queue()
			.then(() => {
				this.$body.find(".fg-fact-queue-cards").html(this.render_cards_html());
				this.$body.find(".fg-fact-queue-pagination").html(this.render_queue_pagination_html());
			})
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	// Refreshes KPI numbers + tab counts only, used right after a successful
	// CONFIRMAR FACTURACIÓN so "Pendientes/Facturados hoy/Facturados"
	// reflect the change without a full-page reload.
	refresh_summary() {
		return this.call("get_invoicing_summary").then((summary) => {
			this.summary = summary;
			this.$body.find(".fg-kpis--facturacion").replaceWith(this.render_kpis());
			this.$body.find(".fg-fact-tabs").replaceWith(this.render_tabs_html());
		});
	}

	render_skeleton() {
		this.$body.html(`
			<div class="fg-skeleton-kpis">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
			<div class="fg-skeleton-cards">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
		`);
	}

	render_body() {
		this.$body.html(`
			${this.render_kpis()}
			<div class="fg-fact-queue-section">
				<div class="fg-section-head">
					<div class="fg-section-title">${__("Cola de facturación")}</div>
				</div>
				<div class="fg-fact-toolbar">
					<div class="fg-fact-search-wrap">
						${icon("search", "fg-fact-search-icon")}
						<input type="text" class="fg-fact-search-input" placeholder="${__(
							"Buscar por PEDIDO-N, cliente, fecha o Pick List..."
						)}" value="${frappe.utils.escape_html(this.queue_search || "")}">
					</div>
				</div>
				${this.render_tabs_html()}
				<div class="fg-fact-queue-cards">${this.render_cards_html()}</div>
				<div class="fg-fact-queue-pagination">${this.render_queue_pagination_html()}</div>
			</div>
			${this.render_billing_queue_section()}
		`);
		this.bind_body_events();
		this.bind_billing_queue_events();
	}

	// =====================================================================
	// Commit 25.13 -- "COTIZACIONES PENDIENTES" (billing review of
	// Quotation). Own section, own cards, own dialog -- entirely separate
	// from the Pick List invoicing queue above (different document,
	// different API module).
	// =====================================================================
	// Commit 25.20 -- the shared matcher (fg_search.js). customer_name/
	// customer cover section 5's "cliente" (Quotation.party_name is
	// returned here as `customer`, get_pending_billing_review_quotations()
	// -- api/cotizaciones.py), transaction_date is this document's own
	// real operational date (section 13).
	billing_quotation_matches_search(q) {
		return fabergray_erp.search.matches_operational_search(q, this.billing_search, {
			text_fields: ["customer_name", "customer", "name"],
			date_fields: ["transaction_date"],
		});
	}

	render_billing_queue_cards_html() {
		const filtered = this.billing_quotations.filter((q) => this.billing_quotation_matches_search(q));
		return filtered.length
			? filtered.map((q) => this.render_billing_queue_card(q)).join("")
			: this.billing_search
				? render_search_empty_html()
				: `<div class="fg-empty">${__("No hay cotizaciones pendientes de revisión.")}</div>`;
	}

	render_billing_queue_section() {
		const pendientes = (this.billing_summary || {}).cotizaciones_pendientes ?? 0;

		return `
			<div class="fg-fact-billing-section">
				<div class="fg-section-head">
					<div class="fg-section-title">${__("Cotizaciones pendientes")}</div>
					<span class="fg-fact-billing-count">${pendientes}</span>
				</div>
				${render_search_bar_html(this.billing_search)}
				<div class="fg-fact-billing-cards">${this.render_billing_queue_cards_html()}</div>
			</div>
		`;
	}

	render_billing_queue_card(q) {
		const customer_label = frappe.utils.escape_html(q.customer_name || q.customer || "—");
		const asesora_label = frappe.utils.escape_html(q.owner_fullname || q.owner || "—");
		const total_label = frappe.format(q.grand_total, { fieldtype: "Currency" });

		return `
			<div class="fg-fact-billing-card" data-name="${frappe.utils.escape_html(q.name)}">
				<div class="fg-fact-billing-card-top">
					<div class="fg-fact-billing-card-id">#${frappe.utils.escape_html(q.name)}</div>
					<span class="fg-badge fg-badge--billing-pending">${__("Pendiente de Facturación")}</span>
				</div>
				<div class="fg-fact-billing-card-customer">${icon("user", "fg-icon-sm")} ${customer_label}</div>
				<div class="fg-fact-billing-card-meta">
					<span>${icon("user-check", "fg-icon-sm")} ${__("Asesora")}: ${asesora_label}</span>
					<span>${icon("calendar", "fg-icon-sm")} ${frappe.datetime.str_to_user(q.transaction_date)}</span>
				</div>
				<div class="fg-fact-billing-card-counts">
					<span>${q.item_count} ${q.item_count === 1 ? __("referencia") : __("referencias")}</span>
					<span>${total_label}</span>
				</div>
				<button type="button" class="fg-btn fg-btn--solid-primary fg-fact-billing-review-btn">
					${icon("clipboard-check", "fg-icon-sm")} ${__("REVISAR")}
				</button>
			</div>
		`;
	}

	// Commit 25.20 -- client-side, re-renders only .fg-fact-billing-cards
	// (never .fg-fact-billing-section itself, which also holds the search
	// input -- same "never re-render the input the user is typing into"
	// rule ventas.js's own bind_orders_search_events() already documents).
	bind_billing_queue_search_events() {
		this.$body.find(".fg-fact-billing-section .fg-search-input").on("input", (e) => {
			this.billing_search = $(e.currentTarget).val();
			this.$body
				.find(".fg-fact-billing-section .fg-search-clear")
				.toggleClass("is-visible", !!this.billing_search.trim());
			// .fg-fact-billing-cards itself is never replaced here (only its
			// own .html() content is) -- the delegated click handler bound
			// once in bind_billing_queue_card_events() below still applies
			// to whatever cards end up inside it, no re-bind needed/safe to
			// repeat (delegated events would otherwise stack).
			this.$body.find(".fg-fact-billing-cards").html(this.render_billing_queue_cards_html());
		});
		this.$body.find(".fg-fact-billing-section .fg-search-clear").on("click", () => {
			this.billing_search = "";
			this.$body.find(".fg-fact-billing-section").replaceWith(this.render_billing_queue_section());
			this.bind_billing_queue_events();
		});
	}

	bind_billing_queue_card_events() {
		this.$body.find(".fg-fact-billing-cards").on("click", ".fg-fact-billing-review-btn", (e) => {
			const name = $(e.currentTarget).closest(".fg-fact-billing-card").data("name");
			this.open_billing_review_dialog(name);
		});
	}

	bind_billing_queue_events() {
		this.bind_billing_queue_search_events();
		this.bind_billing_queue_card_events();
	}

	// Purely informational -- never clickable. Filtering happens only
	// through the Tabs (Todos/Pendientes/Facturados) below, a deliberately
	// separate control. Exclusively derived from fg_invoicing_status (this
	// commit's own operational field) -- never from Sales Invoice/
	// delivery_status, unlike the legacy get_facturacion_summary() this
	// replaces on this Page.
	render_kpis() {
		const s = this.summary || {};
		const cards = [
			{ key: "pendientes", label: __("Pendientes"), sub: __("Sin marcar como facturado"), i: "clock", mod: "fact-pendientes" },
			{ key: "facturados_hoy", label: __("Facturados hoy"), sub: __("Marcados como facturado hoy"), i: "check", mod: "fact-facturados-hoy" },
			{ key: "facturados", label: __("Facturados"), sub: __("Total histórico"), i: "circle-check-big", mod: "fact-facturados" },
		];
		const html = cards
			.map(
				(c) => `
				<div class="fg-kpi fg-kpi--${c.mod}">
					<div class="fg-kpi-icon">${icon(c.i)}</div>
					<div class="fg-kpi-number">${s[c.key] ?? 0}</div>
					<div class="fg-kpi-label">${c.label}</div>
					<div class="fg-kpi-sub">${c.sub}</div>
				</div>
			`
			)
			.join("");

		return `<div class="fg-kpis fg-kpis--facturacion">${html}</div>`;
	}

	render_tabs_html() {
		const s = this.summary || {};
		const pendientes = s.pendientes ?? 0;
		const facturados = s.facturados ?? 0;
		const tabs = [
			{ key: "", label: __("Todos"), i: "layout-grid", count: pendientes + facturados },
			{ key: "Pendiente", label: __("Pendientes"), i: "clock", count: pendientes },
			{ key: "Facturado", label: __("Facturados"), i: "check", count: facturados },
		];
		const tabs_html = tabs
			.map(
				(t) => `
				<button type="button" class="fg-fact-tab ${this.queue_filter === t.key ? "is-active" : ""}" data-filter="${
					t.key
				}">${icon(t.i, "fg-icon-sm")} ${t.label} (${t.count})</button>
			`
			)
			.join("");
		return `<div class="fg-fact-tabs">${tabs_html}</div>`;
	}

	render_cards_html() {
		if (!this.rows.length) {
			return `<div class="fg-empty">${__("No hay pedidos que coincidan.")}</div>`;
		}
		return this.rows.map((r) => this.render_queue_card(r)).join("");
	}

	// Card fields, exactly per brief: Pedido, Cliente, Pick List, cantidad
	// de productos, cantidad total, progreso de revisión (Pendiente) --
	// plus, only once Facturado, "Facturado por" + fecha/hora and a green
	// ✓ FACTURADO state (soft green tint on the whole card, same visual
	// language as Bodega's own completed-order cards). Pendiente cards get
	// REVISAR PEDIDO instead of a direct FACTURAR -- nothing here ever
	// reads/shows a $ amount.
	render_queue_card(r) {
		const is_facturado = r.fg_invoicing_status === "Facturado";
		const pedido_label = r.commercial_name || r.sales_order || r.name;
		const customer_label = frappe.utils.escape_html(r.customer_name || r.customer || __("Sin cliente"));

		const status_html = is_facturado
			? `<span class="fg-badge fg-badge--fact-facturado">${icon("check", "fg-icon-sm")} ${__("FACTURADO")}</span>`
			: `<span class="fg-badge fg-badge--fact-pendiente">${__("Pendiente")}</span>`;

		const total_items = r.total_items ?? r.item_count ?? 0;
		const checked_items = r.checked_items ?? 0;

		const footer_html = is_facturado
			? `
				<div class="fg-fact-queue-card-invoiced">
					<span>${__("Facturado por")}: <strong>${frappe.utils.escape_html(
						r.fg_invoiced_by_fullname || r.fg_invoiced_by || "—"
					)}</strong></span>
					<span>${__("Fecha")}: <strong>${
						r.fg_invoiced_on ? frappe.datetime.str_to_user(r.fg_invoiced_on) : "—"
					}</strong></span>
				</div>
			`
			: `
				<div class="fg-fact-queue-card-progress">
					<div class="fg-progress-track"><div class="fg-progress-fill" style="--fg-progress-width:${
						total_items ? (checked_items / total_items) * 100 : 0
					}%"></div></div>
					<span class="fg-fact-queue-card-progress-label">${checked_items}/${total_items} ${__("revisados")}</span>
				</div>
				<button type="button" class="fg-btn fg-btn--solid-primary fg-fact-review-btn">
					${icon("eye", "fg-icon-sm")} ${__("REVISAR PEDIDO")}
				</button>
			`;

		return `
			<div class="fg-fact-queue-card ${is_facturado ? "is-facturado" : ""}" data-name="${frappe.utils.escape_html(
			r.name
		)}">
				<div class="fg-fact-queue-card-top">
					<div class="fg-fact-queue-card-id">${__("PEDIDO")} #${frappe.utils.escape_html(pedido_label)}</div>
					${status_html}
				</div>
				<div class="fg-fact-queue-card-customer">${icon("user", "fg-icon-sm")} ${customer_label}</div>
				<div class="fg-fact-queue-card-meta">
					<span class="fg-fact-queue-card-picklist">${icon("clipboard-list", "fg-icon-sm")} ${frappe.utils.escape_html(
			r.name
		)}</span>
				</div>
				<div class="fg-fact-queue-card-meta">
					<span>${icon("package", "fg-icon-sm")} ${r.item_count} ${
			r.item_count === 1 ? __("referencia") : __("referencias")
		}</span>
					<span>${format_qty(r.total_qty)} ${__("unidades")}</span>
				</div>
				${footer_html}
			</div>
		`;
	}

	render_queue_pagination_html() {
		if (!this.total) return "";
		const page_count = Math.max(Math.ceil(this.total / PAGE_SIZE), 1);
		const start = (this.queue_page - 1) * PAGE_SIZE + 1;
		const end = Math.min(this.queue_page * PAGE_SIZE, this.total);
		return `
			<div class="fg-fact-pagination-info">${__("Mostrando {0} a {1} de {2} pedidos", [start, end, this.total])}</div>
			<div class="fg-fact-pagination-controls">
				<button type="button" class="fg-fact-pagination-btn fg-fact-pagination-prev" ${
					this.queue_page <= 1 ? "disabled" : ""
				}>${icon("chevron-left")}</button>
				<span class="fg-fact-pagination-page">${this.queue_page}</span>
				<button type="button" class="fg-fact-pagination-btn fg-fact-pagination-next" ${
					this.queue_page >= page_count ? "disabled" : ""
				}>${icon("chevron-right")}</button>
			</div>
		`;
	}

	bind_body_events() {
		this.$body.find(".fg-fact-search-input").on("input", (e) => {
			const val = $(e.currentTarget).val();
			clearTimeout(this._search_debounce);
			this._search_debounce = setTimeout(() => {
				this.queue_search = val;
				this.queue_page = 1;
				this.refresh_queue();
			}, 300);
		});

		this.$body.find(".fg-fact-tabs").on("click", ".fg-fact-tab", (e) => {
			this.queue_filter = $(e.currentTarget).data("filter") || "";
			this.queue_page = 1;
			this.$body.find(".fg-fact-tabs").replaceWith(this.render_tabs_html());
			this.refresh_queue();
		});

		// Delegated on the stable container -- survives refresh_queue()'s
		// .html() swap without needing to be re-bound on every reload.
		this.$body.find(".fg-fact-queue-cards").on("click", ".fg-fact-review-btn", (e) => {
			e.stopPropagation();
			const name = $(e.currentTarget).closest(".fg-fact-queue-card").data("name");
			this.open_review_dialog(name);
		});

		this.$body.find(".fg-fact-queue-pagination").on("click", ".fg-fact-pagination-prev", () => {
			this.queue_page = Math.max(this.queue_page - 1, 1);
			this.refresh_queue();
		});
		this.$body.find(".fg-fact-queue-pagination").on("click", ".fg-fact-pagination-next", () => {
			this.queue_page = this.queue_page + 1;
			this.refresh_queue();
		});
	}

	// =====================================================================
	// REVISAR PEDIDO -- large modal listing every Pick List Item row via
	// get_invoicing_detail(). Each checkbox toggle calls
	// set_invoicing_item_checked() IMMEDIATELY (no separate "guardar
	// progreso" button -- every line already persists on its own) and shows
	// a discrete "✓ Guardado" next to that row; CONFIRMAR FACTURACIÓN stays
	// disabled (both visually and by simply not being clickable while
	// disabled) until every row is checked. No qty/rate/amount/importe/
	// total is ever sent or shown here beyond the plain requested/alistada
	// quantity -- this modal never reaches the Sales Invoice engine at all.
	// =====================================================================
	open_review_dialog(pick_list_name) {
		if (!pick_list_name) return;

		this._review_pick_list = pick_list_name;
		this._review_detail = null;
		this._review_saving_rows = new Set();

		const dialog = new frappe.ui.Dialog({
			title: `<span class="fg-fact-review-title"><span class="fg-fact-review-title-icon">${icon(
				"clipboard-check"
			)}</span>${__("Revisar pedido")}</span>`,
			size: "extra-large",
			fields: [{ fieldtype: "HTML", fieldname: "review_html" }],
			primary_action_label: `${icon("check", "fg-icon-sm")} ${__("CONFIRMAR FACTURACIÓN")}`,
			primary_action: () => this.confirm_facturacion_from_dialog(),
			secondary_action_label: `${icon("x", "fg-icon-sm")} ${__("CERRAR")}`,
			secondary_action: () => dialog.hide(),
		});
		dialog.$wrapper.addClass("fg-fact-review-dialog");
		dialog.disable_primary_action();
		dialog.fields_dict.review_html.$wrapper.html(
			`<div class="fg-fact-review-loading">${__("Cargando...")}</div>`
		);
		dialog.show();
		this._review_dialog = dialog;

		this.call("get_invoicing_detail", { pick_list: pick_list_name })
			.then((detail) => {
				if (this._review_pick_list !== pick_list_name) return; // dialog closed/reopened meanwhile
				this._review_detail = detail;
				this.render_review_dialog_body();
			})
			.catch(() => dialog.hide());
	}

	// Two-column info/summary header, progress card, product table and
	// instructional callout -- see design_references/
	// facturacion_revisar_pedido_v2.png for the visual this reproduces.
	// Every number here comes straight from get_invoicing_detail()'s own
	// response, never recomputed/guessed client-side.
	render_review_dialog_body() {
		const dialog = this._review_dialog;
		const d = this._review_detail;
		if (!dialog || !d) return;

		const pedido_label = d.commercial_name || d.sales_order || d.pick_list;
		const customer_label = frappe.utils.escape_html(d.customer_name || d.customer || __("Sin cliente"));
		const is_facturado = d.fg_invoicing_status === "Facturado";
		const pct = review_progress_pct(d);

		const items_html = (d.items || []).length
			? `<div class="fg-fact-review-table">
					<div class="fg-fact-review-table-head">
						<span class="fg-fact-review-col-idx">#</span>
						<span class="fg-fact-review-col-product">${__("Producto")}</span>
						<span class="fg-fact-review-col-code">${__("Código")}</span>
						<span class="fg-fact-review-col-qty">${__("Cantidad solicitada")}</span>
						<span class="fg-fact-review-col-uom">${__("Unidad")}</span>
						<span class="fg-fact-review-col-status">${__("Estado")}</span>
					</div>
					<div class="fg-fact-review-table-body">${d.items
						.map((item, idx) => this.render_review_item_row(item, idx + 1, is_facturado))
						.join("")}</div>
				</div>`
			: `<div class="fg-empty fg-empty--sm">${__("Sin productos.")}</div>`;

		dialog.fields_dict.review_html.$wrapper.html(`
			<div class="fg-fact-review-info">
				<div class="fg-fact-review-info-main">
					<div class="fg-fact-review-pedido-icon">${icon("shopping-cart")}</div>
					<div class="fg-fact-review-pedido-block">
						<div class="fg-fact-review-pedido-title">${__("PEDIDO")} #${frappe.utils.escape_html(pedido_label)}</div>
						<div class="fg-fact-review-info-grid">
							<div class="fg-fact-review-info-item">
								<div class="fg-fact-review-info-label">${__("Cliente")}</div>
								<div class="fg-fact-review-info-value">${customer_label}</div>
							</div>
							<div class="fg-fact-review-info-item">
								<div class="fg-fact-review-info-label">${__("Pick List")}</div>
								<div class="fg-fact-review-info-value fg-fact-review-info-value--link">
									${frappe.utils.escape_html(d.pick_list)}
									<button type="button" class="fg-fact-review-copy-btn" title="${__("Copiar")}">${icon("copy", "fg-icon-sm")}</button>
								</div>
							</div>
						</div>
					</div>
				</div>
				<div class="fg-fact-review-summary-cards">
					<div class="fg-fact-review-summary-card fg-fact-review-summary-card--products">
						<div class="fg-fact-review-summary-icon">${icon("box")}</div>
						<div class="fg-fact-review-summary-label">${__("Productos")}</div>
						<div class="fg-fact-review-summary-value fg-fact-review-summary-value--products">${d.total_items}</div>
						<div class="fg-fact-review-summary-unit">${d.total_items === 1 ? __("producto") : __("productos")}</div>
					</div>
					<div class="fg-fact-review-summary-card fg-fact-review-summary-card--qty">
						<div class="fg-fact-review-summary-icon">${icon("boxes")}</div>
						<div class="fg-fact-review-summary-label">${__("Cantidad total")}</div>
						<div class="fg-fact-review-summary-value fg-fact-review-summary-value--qty">${format_qty(d.total_qty)}</div>
						<div class="fg-fact-review-summary-unit">${__("unidades")}</div>
					</div>
					<div class="fg-fact-review-summary-card fg-fact-review-summary-card--progress">
						<div class="fg-fact-review-summary-icon">${icon("clipboard-list")}</div>
						<div class="fg-fact-review-summary-label">${__("Progreso")}</div>
						<div class="fg-fact-review-summary-value fg-fact-review-summary-value--progress">${format_qty(pct)}%</div>
						<div class="fg-fact-review-summary-unit fg-fact-review-summary-unit--progress">${d.checked_items} ${__(
			"de"
		)} ${d.total_items} ${__("revisados")}</div>
					</div>
				</div>
			</div>

			<div class="fg-fact-review-progress-card ${pct >= 100 ? "is-complete" : ""}">
				<div class="fg-fact-review-progress-head">
					<span>${__("Progreso de revisión")}</span>
					<span class="fg-fact-review-progress-count">${d.checked_items} ${__("de")} ${d.total_items} ${__("productos revisados")}</span>
				</div>
				<div class="fg-fact-review-progress-track">
					<div class="fg-fact-review-progress-fill" style="width:${pct}%"></div>
					<span class="fg-fact-review-progress-pct">${format_qty(pct)}%</span>
				</div>
			</div>

			${items_html}

			<div class="fg-fact-review-callout">
				${icon("info", "fg-icon-sm")}
				<span>${__(
					"Marca cada producto conforme lo revisas. Cuando todos estén revisados, podrás confirmar la facturación."
				)}</span>
			</div>

			${
				is_facturado
					? `<div class="fg-fact-review-readonly-note">${icon("check", "fg-icon-sm")} ${__(
							"Este pedido ya fue facturado -- el checklist es de solo lectura."
					  )}</div>`
					: ""
			}
		`);

		this.bind_review_dialog_events();
		this.refresh_review_primary_action();
	}

	render_review_item_row(item, idx, is_readonly) {
		const checked = !!cint(item.checked);
		return `
			<div class="fg-fact-review-row ${checked ? "is-checked" : ""}" data-row="${frappe.utils.escape_html(item.row_name)}">
				<span class="fg-fact-review-col-idx">${idx}</span>
				<span class="fg-fact-review-col-product">
					<label class="fg-fact-review-checkbox-wrap">
						<input type="checkbox" class="fg-fact-review-checkbox" ${checked ? "checked" : ""} ${
			is_readonly ? "disabled" : ""
		}>
					</label>
					<span class="fg-fact-review-item-name">${frappe.utils.escape_html(item.item_name || item.item_code)}</span>
				</span>
				<span class="fg-fact-review-col-code" data-label="${__("Código")}">${frappe.utils.escape_html(item.item_code)}</span>
				<span class="fg-fact-review-col-qty" data-label="${__("Cantidad solicitada")}">${format_qty(item.qty)}</span>
				<span class="fg-fact-review-col-uom" data-label="${__("Unidad")}">${frappe.utils.escape_html(item.uom || "—")}</span>
				<span class="fg-fact-review-col-status" data-label="${__("Estado")}">
					<span class="fg-fact-review-status-badge ${checked ? "is-revisado" : "is-pendiente"}">
						${checked ? icon("check", "fg-icon-sm") : icon("circle", "fg-icon-sm")}
						${checked ? __("Revisado") : __("Pendiente")}
					</span>
					<span class="fg-fact-review-item-feedback"></span>
				</span>
			</div>
		`;
	}

	bind_review_dialog_events() {
		const dialog = this._review_dialog;
		if (!dialog) return;
		const $wrap = dialog.fields_dict.review_html.$wrapper;

		// Delegated -- rows are replaced individually (never the whole list)
		// on each toggle, so this single binding survives every update.
		$wrap.off("change", ".fg-fact-review-checkbox").on("change", ".fg-fact-review-checkbox", (e) => {
			const $checkbox = $(e.currentTarget);
			const $row = $checkbox.closest(".fg-fact-review-row");
			const row_name = $row.data("row");
			this.toggle_review_item(row_name, $checkbox.is(":checked"));
		});

		$wrap.off("click", ".fg-fact-review-copy-btn").on("click", ".fg-fact-review-copy-btn", () => {
			frappe.utils.copy_to_clipboard(this._review_detail.pick_list);
		});
	}

	// One row, one immediate server call -- "guardado inmediato" per the
	// brief, no separate save button. A per-row in-flight guard (never a
	// page-wide one) blocks a second toggle on the SAME row while its own
	// request is out, so a fast double-click can't race two writes against
	// each other; other rows stay fully interactive meanwhile. Only the
	// touched row + the summary/progress widgets are patched in place --
	// the modal is never fully re-rendered on a toggle.
	toggle_review_item(row_name, checked) {
		if (!row_name || this._review_saving_rows.has(row_name)) return;
		const pick_list_name = this._review_pick_list;
		const dialog = this._review_dialog;
		if (!dialog) return;

		this._review_saving_rows.add(row_name);
		const $row = dialog.fields_dict.review_html.$wrapper.find(
			`.fg-fact-review-row[data-row="${frappe.utils.escape_html(row_name)}"]`
		);
		$row.find(".fg-fact-review-checkbox").prop("disabled", true);
		$row.find(".fg-fact-review-item-feedback").html(icon("loader-circle", "fg-icon-sm fg-spin"));

		this.call("set_invoicing_item_checked", {
			pick_list: pick_list_name,
			pick_list_item: row_name,
			checked: checked ? 1 : 0,
		})
			.then((result) => {
				if (this._review_pick_list !== pick_list_name || !this._review_detail) return;
				const item = this._review_detail.items.find((i) => i.row_name === row_name);
				if (item) item.checked = result.checked;
				this._review_detail.checked_items = result.checked_items;
				this._review_detail.total_items = result.total_items;

				const is_checked = !!result.checked;
				$row.toggleClass("is-checked", is_checked);
				$row.find(".fg-fact-review-checkbox").prop("checked", is_checked).prop("disabled", false);
				$row
					.find(".fg-fact-review-status-badge")
					.toggleClass("is-revisado", is_checked)
					.toggleClass("is-pendiente", !is_checked)
					.html(
						`${is_checked ? icon("check", "fg-icon-sm") : icon("circle", "fg-icon-sm")} ${
							is_checked ? __("Revisado") : __("Pendiente")
						}`
					);
				$row.find(".fg-fact-review-item-feedback").html(`${icon("check", "fg-icon-sm")} ${__("Guardado")}`);
				setTimeout(() => {
					$row.find(".fg-fact-review-item-feedback").fadeOut(200, function () {
						$(this).html("").show();
					});
				}, 1200);

				this.refresh_review_progress();
			})
			.catch(() => {
				// The server already showed the real error via frappe.call()'s
				// own default error dialog -- revert the checkbox to its last
				// known-good state rather than trusting the failed click.
				const item = this._review_detail && this._review_detail.items.find((i) => i.row_name === row_name);
				$row
					.find(".fg-fact-review-checkbox")
					.prop("checked", !!(item && item.checked))
					.prop("disabled", false);
				$row.find(".fg-fact-review-item-feedback").html("");
			})
			.finally(() => {
				this._review_saving_rows.delete(row_name);
			});
	}

	// Patches the progress card, the "Progreso" summary card and the
	// primary action's enabled state -- never a full modal re-render, per
	// the brief's own "NO recargar todo el modal si no es necesario".
	refresh_review_progress() {
		const dialog = this._review_dialog;
		const d = this._review_detail;
		if (!dialog || !d) return;
		const pct = review_progress_pct(d);
		const $wrap = dialog.fields_dict.review_html.$wrapper;

		$wrap
			.find(".fg-fact-review-progress-count")
			.text(`${d.checked_items} ${__("de")} ${d.total_items} ${__("productos revisados")}`);
		$wrap.find(".fg-fact-review-progress-fill").css("width", `${pct}%`);
		$wrap.find(".fg-fact-review-progress-pct").text(`${format_qty(pct)}%`);
		$wrap.find(".fg-fact-review-progress-card").toggleClass("is-complete", pct >= 100);

		$wrap.find(".fg-fact-review-summary-value--progress").text(`${format_qty(pct)}%`);
		$wrap
			.find(".fg-fact-review-summary-unit--progress")
			.text(`${d.checked_items} ${__("de")} ${d.total_items} ${__("revisados")}`);
		$wrap.find(".fg-fact-review-summary-card--progress").toggleClass("is-complete", pct >= 100);

		this.refresh_review_primary_action();
	}

	// Server-side is the real gate (mark_as_invoiced() throws
	// ChecklistIncompleteError otherwise) -- this only mirrors that in the
	// UI so the user isn't told "listo" until it actually is.
	refresh_review_primary_action() {
		const dialog = this._review_dialog;
		const d = this._review_detail;
		if (!dialog || !d) return;
		const complete = d.total_items > 0 && d.checked_items === d.total_items && d.fg_invoicing_status !== "Facturado";
		if (complete) {
			dialog.enable_primary_action();
		} else {
			dialog.disable_primary_action();
		}
	}

	confirm_facturacion_from_dialog() {
		const d = this._review_detail;
		if (!d || d.total_items === 0 || d.checked_items !== d.total_items || d.fg_invoicing_status === "Facturado") {
			return;
		}
		this.submit_mark_as_invoiced(this._review_pick_list);
	}

	// The one write that actually flips fg_invoicing_status -- pick_list_name
	// only, nothing else. On success: closes the review modal, patches the
	// matching card to ✓ FACTURADO in place (no full reload/flicker),
	// refreshes the KPI/tab counts from the server, and shows the exact
	// toast text the brief asks for. Stays on this Page throughout -- no
	// Sales Invoice form, no Desk contable.
	submit_mark_as_invoiced(pick_list_name) {
		if (this.busy) return;
		this.set_busy(true);
		if (this._review_dialog) this._review_dialog.disable_primary_action();

		this.call("mark_as_invoiced", { pick_list_name: pick_list_name })
			.then((result) => {
				if (this._review_dialog) this._review_dialog.hide();

				const row = this.rows.find((r) => r.name === pick_list_name);
				if (row) {
					row.fg_invoicing_status = result.fg_invoicing_status;
					row.fg_invoiced_on = result.fg_invoiced_on;
					row.fg_invoiced_by = result.fg_invoiced_by;
					row.fg_invoiced_by_fullname = result.fg_invoiced_by_fullname;
					this.$body
						.find(`.fg-fact-queue-card[data-name="${frappe.utils.escape_html(pick_list_name)}"]`)
						.replaceWith(this.render_queue_card(row));
				}
				frappe.show_alert({ message: "✓ " + __("Pedido marcado como facturado correctamente."), indicator: "green" }, 5);
				return this.refresh_summary();
			})
			.catch(() => {
				// The server already showed the real error via frappe.call()'s
				// own default error dialog (including "Debes revisar todos los
				// productos..." if the checklist somehow wasn't complete, and
				// "Este pedido ya fue marcado como facturado." for the
				// idempotent double-confirm case) -- nothing here assumes the
				// write succeeded.
				if (this._review_dialog) this.refresh_review_primary_action();
			})
			.finally(() => this.set_busy(false));
	}

	// =====================================================================
	// Commit 25.13 -- Revisar cotización (Facturación). Same "HTML field
	// dialog, content rendered server-response-driven, no full-modal
	// re-render on a small action" convention as open_review_dialog()/
	// render_review_dialog_body() above, applied to
	// get_quotation_billing_detail()'s own response instead of
	// get_invoicing_detail()'s.
	// =====================================================================
	// Commit 25.13.1 -- full visual rewrite of this dialog. Root cause of
	// the broken layout (giant icons, table rendered as running text,
	// DEVOLVER A VENDEDORA oversized, huge empty gaps): EVERY rule this
	// dialog's content relied on was written as `.fg-facturacion .fg-fact-
	// billing-*` in facturacion.css -- but `frappe.ui.Dialog` renders its
	// modal OUTSIDE `.fg-facturacion` (appended straight to `<body>`,
	// exactly like the sibling "Revisar pedido" dialog's own long-standing
	// comment already documents in facturacion.css) so NONE of that CSS
	// ever matched anything in here, `.fg-icon` sizing included -- the
	// dialog rendered as fully unstyled Bootstrap defaults. Every class
	// used below is now styled under `.fg-fact-billing-review-dialog`
	// (this dialog's own `$wrapper` class) in facturacion.css -- see that
	// file's own matching comment for the full audit.
	//
	// "DEVOLVER A VENDEDORA" is now added via the native
	// `dialog.add_custom_action()` (confirmed by reading frappe/public/js/
	// frappe/ui/dialog.js directly: it appends into the SAME `.modal-
	// footer` as the framework's own CERRAR/APROBAR buttons, `.custom-
	// actions` alongside `.standard-actions`) -- never injected into the
	// modal BODY as a giant standalone element, this fix's own first
	// draft's mistake.
	open_billing_review_dialog(name) {
		if (!name) return;

		this._billing_review_quotation = name;
		this._billing_review_detail = null;
		this._billing_review_selected_mode = null;
		this._billing_review_applying = false;

		const dialog = new frappe.ui.Dialog({
			title: __("Revisar cotización"),
			size: "large",
			fields: [{ fieldtype: "HTML", fieldname: "billing_review_html" }],
			primary_action_label: `${icon("check", "fg-icon-sm")} ${__("Aprobar")}`,
			primary_action: () => this.confirm_approve_from_dialog(),
			secondary_action_label: __("Cerrar"),
			secondary_action: () => dialog.hide(),
		});
		dialog.$wrapper.addClass("fg-fact-billing-review-dialog");
		dialog.add_custom_action(
			`${icon("corner-up-left", "fg-icon-sm")} ${__("Devolver a vendedora")}`,
			// Commit 25.14 -- reads `this._billing_review_quotation`, NEVER
			// the closure-captured `name` parameter: apply_billing_price_
			// mode() re-points that to a NEW (amended) document name after
			// a successful price adjustment, and DEVOLVER must always act
			// on whatever is CURRENTLY open in this dialog, not the
			// original, possibly already-cancelled one.
			() => this.open_return_quotation_dialog(this._billing_review_quotation),
			"fg-fact-billing-return-btn"
		);
		dialog.fields_dict.billing_review_html.$wrapper.html(
			`<div class="fg-fact-review-loading">${__("Cargando...")}</div>`
		);
		dialog.show();
		this._billing_review_dialog = dialog;

		this.call_cotizaciones("get_quotation_billing_detail", { name: name })
			.then((detail) => {
				if (this._billing_review_quotation !== name) return; // dialog closed/reopened meanwhile
				this._billing_review_detail = detail;
				// Section 3 -- detected FROM the actual rate/reference_rate on
				// each line, never simply trusted from `fg_billing_price_mode`
				// alone (that field can be stale/blank for a historical
				// Quotation never adjusted through this mechanism) -- opening
				// the dialog NEVER changes a single price, only reads.
				this._billing_review_selected_mode = this.detect_billing_price_mode(detail);
				this.render_billing_review_dialog_body();
			})
			.catch(() => dialog.hide());
	}

	// Header (#COTIZACION-N + Cliente/Asesora/Fecha/Total mini-cards),
	// Disponibilidad ERP banner, price-list note, then the product table
	// -- section 7/8's own explicit column list. "Disponibilidad ERP" is
	// spelled out exactly that way everywhere (never "física") -- this is
	// informational-only, never a substitute for Bodega's later physical
	// check. Desktop renders `.fg-fact-billing-review-table` as a real
	// grid; the SAME row markup collapses to a stacked card at <=860px
	// via `facturacion.css`'s own media query (`data-label` attributes
	// below feed that, same technique the sibling review dialog's own
	// `.fg-fact-review-row` already established) -- one render, no
	// separate mobile branch here.
	render_billing_review_dialog_body() {
		const dialog = this._billing_review_dialog;
		const d = this._billing_review_detail;
		if (!dialog || !d) return;

		const customer_label = frappe.utils.escape_html(d.customer_name || d.customer || __("Sin cliente"));
		const asesora_label = frappe.utils.escape_html(d.owner_fullname || d.owner || "—");
		const total_label = frappe.format(d.grand_total, { fieldtype: "Currency" });
		const price_list_label = frappe.utils.escape_html((d.items && d.items[0] && d.items[0].price_list) || "—");

		const info_cards = [
			{ label: __("Cliente"), value: customer_label },
			{ label: __("Asesora"), value: asesora_label },
			{ label: __("Fecha"), value: frappe.datetime.str_to_user(d.transaction_date) },
			{ label: __("Total"), value: total_label },
		]
			.map(
				(c) => `
					<div class="fg-fact-billing-review-info-card">
						<div class="fg-fact-billing-review-info-label">${c.label}</div>
						<div class="fg-fact-billing-review-info-value">${c.value}</div>
					</div>
				`
			)
			.join("");

		const selected_mode = this._billing_review_selected_mode;
		const rows_html = (d.items || []).map((item) => this.render_billing_review_row(item, selected_mode)).join("");

		dialog.fields_dict.billing_review_html.$wrapper.html(`
			<div class="fg-fact-billing-review-id">#${frappe.utils.escape_html(d.name)}</div>
			<div class="fg-fact-billing-review-info-grid">${info_cards}</div>
			<div class="fg-fact-billing-review-note">
				<strong>${__("Disponibilidad ERP")}</strong>
				<span>${__("Información de referencia. No reemplaza la validación física posterior de Bodega.")}</span>
			</div>
			<div class="fg-fact-billing-review-pricelist">
				${__("Lista de precios de referencia")}: <strong>${price_list_label}</strong>
			</div>
			${this.render_billing_price_mode_section(d, selected_mode)}
			<div class="fg-fact-billing-review-table">
				<div class="fg-fact-billing-review-thead">
					<span>${__("Producto")}</span>
					<span>${__("Cant.")}</span>
					<span>${__("Base")}</span>
					<span>${__("Actual")}</span>
					<span>${__("Ajustado")}</span>
					<span>${__("Dif. vs base")}</span>
					<span>${__("Disponibilidad ERP")}</span>
				</div>
				<div class="fg-fact-billing-review-tbody">${rows_html}</div>
			</div>
		`);

		dialog.$wrapper.find(".fg-fact-billing-pricemode-btn").on("click", (e) => {
			this.select_billing_price_mode($(e.currentTarget).data("mode"));
		});
		dialog.$wrapper.find(".fg-fact-billing-apply-price-btn").on("click", () => {
			this.apply_billing_price_mode();
		});
	}

	// Hotfix 25.20.4 -- THE enable/disable rule for "APLICAR PRECIOS", in
	// one place. Reads the SERVER's own per-mode answer
	// (`price_mode_changes`, computed by has_price_mode_changes() in
	// api/cotizaciones.py from the persisted rate/price_list_rate/
	// discount_percentage of every line) -- never re-derived from this
	// file's own preview multipliers, and never from the
	// `fg_billing_price_mode` audit label, which can disagree with the
	// rates and must never win over them.
	//
	// Returns false (button disabled) ONLY for the three cases the brief
	// allows: a call is already in flight (section 11), the Quotation is
	// not eligible by state/permission (`can_apply_price_mode`, the same
	// "submitted + Pendiente de Facturación" apply_quotation_price_mode()
	// re-validates server-side regardless), or applying the selected mode
	// would not change a single persisted price. `price_mode_changes`
	// missing entirely (an older cached payload) falls back to "enabled
	// whenever a mode is selected" -- the pre-hotfix behaviour, never a
	// silently dead button.
	can_apply_billing_price_mode(d, selected_mode) {
		if (!d || !selected_mode) return false;
		if (this._billing_review_applying) return false;
		if (d.can_apply_price_mode === false) return false;
		if (!d.price_mode_changes) return true;
		return !!d.price_mode_changes[selected_mode];
	}

	// Commit 25.14 -- "PRECIOS AJUSTADOS" segmented control + live preview
	// summary. Purely a render helper (called from render_billing_review_
	// dialog_body() above) -- no state of its own, no server call.
	// "Precio personalizado / sin ajustar" (section 3's own exact wording)
	// shows whenever `selected_mode` is null -- either nothing matched on
	// open (detect_billing_price_mode()) or nothing has been explicitly
	// clicked yet; APLICAR PRECIOS stays disabled in that state (never
	// lets a click apply "nothing").
	// Hotfix 25.20.4 -- the button is no longer enabled merely because a
	// mode is selected: can_apply_billing_price_mode() above decides, and
	// whenever it says no BECAUSE the prices already match the selected
	// mode, the preview line says so explicitly ("Los precios ya
	// corresponden a esta modalidad.") -- a disabled button with no reason
	// next to it is exactly what made this bug unreadable in the first
	// place.
	render_billing_price_mode_section(d, selected_mode) {
		const can_apply = this.can_apply_billing_price_mode(d, selected_mode);
		const applying = !!this._billing_review_applying;
		const options = [
			{ mode: "FULL", label: __("Precio completo") },
			{ mode: "DISCOUNT_10", label: __("-10%") },
			{ mode: "DISCOUNT_15", label: __("-15%") },
			{ mode: "DISCOUNT_20", label: __("-20%") },
			{ mode: "DISCOUNT_25", label: __("-25%") },
		]
			.map(
				(o) => `
					<button type="button" class="fg-fact-billing-pricemode-btn ${
						selected_mode === o.mode ? "is-active" : ""
					}" data-mode="${o.mode}" ${applying ? "disabled" : ""}>
						${o.label}
					</button>
				`
			)
			.join("");

		const items_with_reference = (d.items || []).filter((item) => item.reference_rate != null);
		const state_html = selected_mode
			? (() => {
					const multiplier = PRICE_MODE_MULTIPLIERS[selected_mode];
					const preview_subtotal = items_with_reference.reduce(
						(sum, item) => sum + flt(item.reference_rate) * multiplier * flt(item.qty),
						0
					);
					// Only "ya aplicado" explains a disabled button here;
					// an ineligible Quotation or an in-flight call are both
					// already obvious from the rest of the dialog.
					const already_applied =
						!can_apply && !applying && d.can_apply_price_mode !== false && !!d.price_mode_changes;
					const reason_html = already_applied
						? `<span class="fg-fact-billing-review-pricemode-applied">${__(
								"Los precios ya corresponden a esta modalidad."
						  )}</span>`
						: "";
					return `
						<div class="fg-fact-billing-review-pricemode-preview">
							<span>${__("Subtotal estimado")}: <strong>${frappe.format(preview_subtotal, {
						fieldtype: "Currency",
					})}</strong></span>
							<span class="fg-fact-billing-review-pricemode-note">${__("Impuestos se recalculan al aplicar.")}</span>
							${reason_html}
						</div>
					`;
			  })()
			: `<div class="fg-fact-billing-review-pricemode-custom">${__("Precio personalizado / sin ajustar")}</div>`;

		return `
			<div class="fg-fact-billing-review-pricemode">
				<div class="fg-fact-billing-review-pricemode-title">${__("Precios ajustados")}</div>
				<div class="fg-fact-billing-review-pricemode-row">
					<div class="fg-fact-billing-review-pricemode-options">${options}</div>
					<button type="button" class="fg-btn fg-btn--solid-primary fg-fact-billing-apply-price-btn" ${
						can_apply ? "" : "disabled"
					}>
						${icon("tag", "fg-icon-sm")} ${applying ? __("APLICANDO...") : __("APLICAR PRECIOS")}
					</button>
				</div>
				${state_html}
			</div>
		`;
	}

	// Selecting a mode is a pure client-side preview (section 4's own
	// "no persistir todavía solo por cambiar visualmente el selector") --
	// re-renders the SAME already-fetched detail with a new
	// `selected_mode`, never a server round-trip. `apply_billing_price_
	// mode()` (below) is the only path that ever calls the server.
	select_billing_price_mode(mode) {
		if (!this._billing_review_detail) return;
		this._billing_review_selected_mode = mode;
		this.render_billing_review_dialog_body();
	}

	// Section 3 -- "si la cotización coincide con [modo] -> seleccionar
	// [modo]; si no coincide con ninguno -> personalizado". Compares every
	// line's own already-quoted `rate` against `reference_rate *
	// multiplier` for each of the 5 modes, in order -- ONLY a Quotation
	// where every priced line matches the SAME mode counts as that mode; a
	// mix, or nothing with a reference price at all, is "personalizado"
	// (null). Never trusts the persisted `fg_billing_price_mode` alone --
	// that field can be blank/stale for a historical Quotation this
	// mechanism never touched.
	detect_billing_price_mode(detail) {
		const items = (detail.items || []).filter((item) => item.reference_rate != null);
		if (!items.length) return null;
		for (const mode of ["FULL", "DISCOUNT_10", "DISCOUNT_15", "DISCOUNT_20", "DISCOUNT_25"]) {
			const multiplier = PRICE_MODE_MULTIPLIERS[mode];
			const all_match = items.every((item) => Math.abs(flt(item.rate) - flt(item.reference_rate) * multiplier) < 0.01);
			if (all_match) return mode;
		}
		return null;
	}

	// One line of the product table. Price: `null` reference_rate shows an
	// explicit "Sin precio de referencia" sentence (never a bare "N/D"),
	// diff==0 is styled neutral/success, otherwise visibly colored -- never
	// blocking APROBAR on its own (section 7's own explicit instruction).
	// Hotfix 25.20.4 AUDIT (brief section 8) -- the "Diferencia" column was
	// reported as suspicious for showing $0,00 while "Ajustado" showed a
	// discounted price. Its FORMULA IS CORRECT AND UNCHANGED: the server's
	// `rate_difference` is "Actual - Base" (the persisted rate vs the
	// current Item Price on the Quotation's own selling_price_list), NOT
	// "Actual - Ajustado" -- "Ajustado" is an unpersisted client-side
	// preview of a mode the user is merely trying on, so there is nothing
	// persisted to difference it against. $0,00 there is the TRUE and
	// useful reading: "this line is still quoted at exactly catalog price"
	// -- i.e. the -25% has NOT been applied yet, which is precisely the
	// state in which APLICAR PRECIOS must be enabled. Only the column
	// HEADER changed, to "Dif. vs base", so the number can no longer be
	// misread as belonging to the "Ajustado" column next to it.
	// "Ajustado" (Commit 25.14): the CLIENT-computed preview for whichever
	// mode is currently selected -- `reference_rate * multiplier`, plus
	// the resulting discount % and adjusted amount (`qty * adjusted_rate`)
	// -- "—" whenever no mode is selected or this line has no reference
	// price to adjust from (never a fabricated number). Availability: no
	// warehouse resolved shows a neutral "Almacén sin definir" badge
	// (never fabricated Disponible/Faltante numbers next to it); otherwise
	// a success badge when fully available, a warning/danger badge when
	// short -- always with real Solicitado/Disponible/Faltante numbers and
	// the warehouse name, never a bare "N/D".
	render_billing_review_row(item, selected_mode) {
		const rate_label = frappe.format(item.rate, { fieldtype: "Currency" });

		let reference_html;
		let diff_html;
		if (item.reference_rate == null) {
			reference_html = `<span class="fg-fact-billing-review-muted">${__("Sin precio de referencia")}</span>`;
			diff_html = `<span class="fg-fact-billing-review-muted">—</span>`;
		} else {
			reference_html = frappe.format(item.reference_rate, { fieldtype: "Currency" });
			const diff = flt(item.rate_difference);
			const diff_class = diff === 0 ? "is-neutral" : diff > 0 ? "is-above" : "is-below";
			diff_html = `<span class="fg-fact-billing-review-diff ${diff_class}">${frappe.format(diff, {
				fieldtype: "Currency",
			})}</span>`;
		}

		let adjusted_html;
		if (!selected_mode || item.reference_rate == null) {
			adjusted_html = `<span class="fg-fact-billing-review-muted">—</span>`;
		} else {
			const multiplier = PRICE_MODE_MULTIPLIERS[selected_mode];
			const discount_pct = PRICE_MODE_DISCOUNTS[selected_mode];
			const adjusted_rate = flt(item.reference_rate) * multiplier;
			const adjusted_amount = adjusted_rate * flt(item.qty);
			adjusted_html = `
				<div class="fg-fact-billing-review-adjusted-rate">${frappe.format(adjusted_rate, { fieldtype: "Currency" })}</div>
				<div class="fg-fact-billing-review-adjusted-meta">${discount_pct}% · ${frappe.format(adjusted_amount, {
				fieldtype: "Currency",
			})}</div>
			`;
		}

		let availability_html;
		if (!item.warehouse) {
			availability_html = `
				<div class="fg-fact-billing-review-avail-line">${__("Solicitado")}: ${format_qty(item.requested_qty)}</div>
				<span class="fg-badge fg-badge--billing-avail-neutral">${__("Almacén sin definir")}</span>
			`;
		} else {
			const has_shortage = flt(item.shortage_qty) > 0;
			const badge_class = has_shortage ? "fg-badge--billing-avail-short" : "fg-badge--billing-avail-ok";
			const badge_label = has_shortage ? __("Faltante") : __("Disponible");
			availability_html = `
				<div class="fg-fact-billing-review-avail-line">${__("Solicitado")}: ${format_qty(item.requested_qty)}</div>
				<div class="fg-fact-billing-review-avail-line">${__("Disponible")}: ${format_qty(item.available_qty)}</div>
				<div class="fg-fact-billing-review-avail-line">${__("Faltante")}: ${format_qty(item.shortage_qty)}</div>
				<span class="fg-badge ${badge_class}">${badge_label}</span>
				<div class="fg-fact-billing-review-warehouse">${frappe.utils.escape_html(item.warehouse)}</div>
			`;
		}

		return `
			<div class="fg-fact-billing-review-row">
				<div class="fg-fact-billing-review-cell fg-fact-billing-review-cell-product" data-label="${__("Producto")}">
					<div class="fg-fact-billing-review-row-name">${frappe.utils.escape_html(item.item_name)}</div>
					<div class="fg-fact-billing-review-row-code">${frappe.utils.escape_html(item.item_code)}</div>
				</div>
				<div class="fg-fact-billing-review-cell" data-label="${__("Cant.")}">${format_qty(item.qty)} ${frappe.utils.escape_html(
			item.stock_uom || ""
		)}</div>
				<div class="fg-fact-billing-review-cell" data-label="${__("Base")}">${reference_html}</div>
				<div class="fg-fact-billing-review-cell" data-label="${__("Actual")}">${rate_label}</div>
				<div class="fg-fact-billing-review-cell" data-label="${__("Ajustado")}">${adjusted_html}</div>
				<div class="fg-fact-billing-review-cell" data-label="${__("Dif. vs base")}">${diff_html}</div>
				<div class="fg-fact-billing-review-cell fg-fact-billing-review-cell-avail" data-label="${__(
					"Disponibilidad ERP"
				)}">${availability_html}</div>
			</div>
		`;
	}

	// Commit 25.14, section 5/17 -- APLICAR PRECIOS. Confirmation text is
	// exactly the brief's own wording per mode; success re-fetches the
	// dialog's content from the server under the NEW (amended) name --
	// apply_quotation_price_mode() always cancels the original and
	// creates a fresh document, section 9's own cancel+amend requirement
	// -- and refreshes the tray in the background so its own card (still
	// "Pendiente de Facturación") reflects the new totals too. Never
	// calls approve_quotation_billing() -- section 5's own explicit "NO
	// aprobar automáticamente por ajustar precios".
	apply_billing_price_mode() {
		const d = this._billing_review_detail;
		const mode = this._billing_review_selected_mode;
		// Hotfix 25.20.4, section 11 -- re-checked here, not only in the
		// rendered `disabled` attribute: a keyboard/programmatic click, or
		// a click landing between the confirm dialog and the re-render,
		// must never start a second cancel+amend cycle either.
		if (!this.can_apply_billing_price_mode(d, mode)) return;

		const confirm_messages = {
			FULL: __("Se restaurarán los precios de venta completos de todos los productos."),
			DISCOUNT_10: __(
				"Se aplicará un descuento del 10% sobre el precio de venta de todos los productos de esta cotización. ¿Deseas continuar?"
			),
			DISCOUNT_15: __(
				"Se aplicará un descuento del 15% sobre el precio de venta de todos los productos de esta cotización. ¿Deseas continuar?"
			),
			DISCOUNT_20: __(
				"Se aplicará un descuento del 20% sobre el precio de venta de todos los productos de esta cotización. ¿Deseas continuar?"
			),
			DISCOUNT_25: __(
				"Se aplicará un descuento del 25% sobre el precio de venta de todos los productos de esta cotización. ¿Deseas continuar?"
			),
		};

		frappe.confirm(confirm_messages[mode], () => {
			if (this._billing_review_applying) return;
			this._billing_review_applying = true;
			this.render_billing_review_dialog_body(); // APLICANDO..., both button groups disabled
			this.set_busy(true);
			this.call_cotizaciones("apply_quotation_price_mode", { quotation_name: d.name, price_mode: mode })
				.then((result) => {
					frappe.show_alert({ message: __("Precios actualizados correctamente."), indicator: "green" }, 5);
					this._billing_review_quotation = result.name;
					return this.call_cotizaciones("get_quotation_billing_detail", { name: result.name });
				})
				.then((detail) => {
					this._billing_review_detail = detail;
					this._billing_review_selected_mode = this.detect_billing_price_mode(detail);
					this.render_billing_review_dialog_body();
					return this.refresh_billing_queue();
				})
				.catch(() => {
					// The server already showed the real error via frappe.call()'s
					// own default error dialog (e.g. "El producto X no tiene
					// precio de referencia..." -- section 12) -- nothing here
					// assumes the write succeeded.
				})
				.finally(() => {
					// Hotfix 25.20.4, section 11 -- runs on BOTH paths, so a
					// failed apply always gives the button back instead of
					// leaving it dead. The re-render also repaints the
					// enabled/disabled state from whatever `detail` is now
					// current (the amended document's on success, the
					// untouched original's on error).
					this._billing_review_applying = false;
					this.set_busy(false);
					this.render_billing_review_dialog_body();
				});
		});
	}

	confirm_approve_from_dialog() {
		const d = this._billing_review_detail;
		if (!d) return;
		this.approve_billing_review(d.name);
	}

	// APROBAR -- no note collected inline here (the brief's own optional
	// "note" parameter on approve_quotation_billing() is left null from
	// this button; nothing in the brief asks for a UI text box on the
	// approve path specifically, unlike DEVOLVER's own mandatory reason).
	// Never creates a Sales Order -- this only calls
	// approve_quotation_billing(), nothing else.
	approve_billing_review(name) {
		if (!name) return;
		this.set_busy(true);
		if (this._billing_review_dialog) this._billing_review_dialog.disable_primary_action();

		this.call_cotizaciones("approve_quotation_billing", { quotation_name: name })
			.then((result) => {
				if (this._billing_review_dialog) this._billing_review_dialog.hide();
				frappe.show_alert({ message: "✓ " + __("Cotización aprobada correctamente."), indicator: "green" }, 5);
				// Commit 25.15, section 22 -- `result.name` is ALWAYS the
				// NEW, vigente, just-approved document: if a price
				// adjustment created an amendment earlier in this same
				// review (apply_billing_price_mode() already re-pointed
				// `name` -- the parameter this function received -- to
				// it), approve_quotation_billing() approved THAT
				// document, never the original, now-superseded one.
				frappe.msgprint({
					title: __("Cotización aprobada"),
					message: __("La cotización quedó aprobada correctamente."),
					primary_action: {
						label: __("VER PDF"),
						action: () => open_fabrigray_quotation_pdf(result.name),
					},
				});
				return this.refresh_billing_queue();
			})
			.catch(() => {
				if (this._billing_review_dialog) this._billing_review_dialog.enable_primary_action();
			})
			.finally(() => this.set_busy(false));
	}

	// DEVOLVER A VENDEDORA -- reason is `reqd: 1` (section 10's own
	// "reason obligatorio"), and Dialog.get_values() itself already
	// refuses to invoke primary_action at all while it is empty
	// (confirmed by reading frappe/public/js/frappe/ui/dialog.js directly
	// -- same guarantee Commit 25.12's own cancel-reason dialog in
	// page/ventas/ventas.js relies on) -- return_quotation_from_billing()
	// re-validates server-side regardless, never trusting this alone.
	open_return_quotation_dialog(name) {
		if (!name) return;

		const dialog = new frappe.ui.Dialog({
			title: __("Devolver cotización a la Vendedora"),
			fields: [
				{
					fieldtype: "Small Text",
					fieldname: "reason",
					label: __("Motivo de la devolución"),
					reqd: 1,
				},
			],
			primary_action_label: __("Devolver"),
			primary_action: (values) => {
				dialog.disable_primary_action();
				this.call_cotizaciones("return_quotation_from_billing", {
					quotation_name: name,
					reason: values.reason,
				})
					.then(() => {
						dialog.hide();
						if (this._billing_review_dialog) this._billing_review_dialog.hide();
						frappe.show_alert({ message: __("Cotización devuelta a la Vendedora."), indicator: "orange" }, 5);
						return this.refresh_billing_queue();
					})
					.catch(() => dialog.enable_primary_action());
			},
			secondary_action_label: __("Cancelar"),
			secondary_action: () => dialog.hide(),
		});

		dialog.show();
	}
};

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, no server calls, no state.
// Intentionally duplicated (not imported) from ventas.js/bodega.js/
// jefe_de_bodega.js/cotizaciones.js, same reasoning as Commit 6: a few
// lines each, zero business logic, keeps this Page's asset loading
// independent of theirs.
// -------------------------------------------------------------------------
const PAGE_SIZE = 10;

// Commit 25.14 -- CLIENT-SIDE PREVIEW ONLY. Mirrors api/cotizaciones.py's
// own `PRICE_MODE_DISCOUNTS` exactly (0/5/10), but this copy never
// decides what actually gets persisted -- apply_quotation_price_mode()
// re-resolves and re-validates everything server-side regardless of
// what this preview showed (section 6's own "no confiar en reference_
// rate enviado desde browser", generalized to the whole preview).
// Commit 25.15 review fix, section 6 -- final closed set is FULL/10%/15%/
// 20%/25%; "DISCOUNT_5" (5%) never shipped to a committed state and is
// removed here, matching api/cotizaciones.py's own PRICE_MODE_DISCOUNTS.
const PRICE_MODE_DISCOUNTS = { FULL: 0, DISCOUNT_10: 10, DISCOUNT_15: 15, DISCOUNT_20: 20, DISCOUNT_25: 25 };
const PRICE_MODE_MULTIPLIERS = { FULL: 1, DISCOUNT_10: 0.9, DISCOUNT_15: 0.85, DISCOUNT_20: 0.8, DISCOUNT_25: 0.75 };

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

function icon(name, extra_class) {
	return `<svg class="fg-icon ${extra_class || ""}"><use href="#icon-${name}"></use></svg>`;
}

// Commit 25.15 review fix, section 2/4 -- NEVER builds the `/printview`
// URL from just a `name` any more (that would skip server-side
// validation up front) -- calls `get_fabrigray_quotation_pdf_view_url()`
// FIRST (a real server-side "Aprobada"/not-cancelled/same-Company check,
// api/cotizaciones.py), and only navigates to the URL it hands back. A
// blank tab is opened SYNCHRONOUSLY on the click itself, before the async
// call starts, and only its own `.location` is set once the server
// responds -- a tab opened later, inside a `.then()` callback, gets
// silently blocked as a popup by most browsers. Intentionally duplicated
// from page/cotizaciones/cotizaciones.js's own identical helper -- same
// "each Page's asset loading stays independent" convention as every
// other small render helper in this file.
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
			if (tab) tab.close();
		});
}

function get_initials(name) {
	const parts = (name || "").trim().split(/\s+/).filter(Boolean);
	if (!parts.length) return "?";
	const first = parts[0][0] || "";
	const second = parts.length > 1 ? parts[1][0] : "";
	return (first + second).toUpperCase();
}

function cint(v) {
	return frappe.utils.cint ? frappe.utils.cint(v) : parseInt(v, 10) || 0;
}

function flt(v) {
	return frappe.utils.flt ? frappe.utils.flt(v) : parseFloat(v) || 0;
}

function format_qty(v) {
	const n = flt(v);
	return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

// Shared by render_review_dialog_body()/refresh_review_progress() so the
// percentage shown in the progress card and the "Progreso" summary card
// can never drift apart -- one formula, read from get_invoicing_detail()/
// set_invoicing_item_checked()'s own checked_items/total_items, never
// recomputed from anything else.
function review_progress_pct(d) {
	return d && d.total_items ? (d.checked_items / d.total_items) * 100 : 0;
}
