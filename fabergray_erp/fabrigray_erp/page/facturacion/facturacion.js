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

// Commit 21.4 -- Page Facturación: dashboard + cola, read-only, one action
// (REVISAR). Commit 21.5 adds the only write this Page ever performs: a
// per-line "VERIFICADO" checklist (frontend-only, never persisted -- no
// Custom Field, no write to Pick List, reset on every detail load/exit,
// never localStorage) gating a GENERAR FACTURA button that calls
// fabergray_erp.api.facturacion.generate_invoice(pick_list_name) with
// nothing else -- no qty/rate/amount/checks are ever sent, the backend
// recomputes everything from the real Pick List via its own audited
// native mapper (Commit 21.3). All server communication in this file goes
// through the four endpoints in fabergray_erp.api.facturacion: the three
// read-only ones from Commit 21.2 plus generate_invoice() from Commit
// 21.3, called exactly once per confirmed click. No economic field is
// hidden here (unlike Ventas/Cotizaciones) -- Facturación's own permission
// model (Commit 21.1) already allows seeing rate/amount, so this Page
// renders whatever the server returns as-is.
fabergray_erp.Facturacion = class Facturacion {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.facturacion.";
		this.busy = false;

		// Dashboard/cola data (view: "dashboard").
		this.summary = null;
		this.queue = null;
		this.queue_filter = "todos"; // "todos" | "pendientes" | "parciales" | "con_incidencia"
		this.queue_search = "";
		this.queue_page = 1;

		// Detalle (view: "detail") -- loaded via REVISAR.
		this.detail = null;
		this.detail_pick_list = null;
		this.detail_has_shortage = false; // pulled from the queue card that opened this detail, never from a new endpoint

		// Commit 21.5 -- "VERIFICADO" checklist state, keyed by row_name.
		// Frontend-only for V1 (explicit instruction): never written to Pick
		// List, no Custom Field, no localStorage -- reset on every detail
		// open/close, so reloading the page always starts unchecked again.
		this.verified_rows = new Set();

		this.state = { view: "dashboard" };

		this.$app = $('<div class="fg-shell fg-facturacion">').appendTo(this.page.body);
		this.render_shell();
		this.load_dashboard();
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
		this.$app.find(".fg-refresh-btn").on("click", () => {
			if (this.state.view === "dashboard") this.load_dashboard();
		});
	}

	// Refresh is only ever enabled on the dashboard view, and always comes
	// back (never left stuck disabled) once the in-flight call settles --
	// every load path below reaches set_busy(false) via .finally(), success
	// or failure alike.
	set_busy(is_busy) {
		this.busy = !!is_busy;
		this.$app.find(".fg-refresh-btn").prop("disabled", this.busy || this.state.view !== "dashboard");
		this.$app.toggleClass("fg-loading", !!is_busy);
	}

	// =====================================================================
	// Dashboard + Cola
	// =====================================================================
	load_dashboard() {
		this.set_busy(true);
		this.state.view = "dashboard";
		this.render_skeleton_dashboard();
		return Promise.all([this.call("get_facturacion_summary"), this.call("get_pending_pick_lists")])
			.then(([summary, queue]) => {
				this.summary = summary;
				this.queue = queue || [];
				this.queue_page = 1;
				this.render_dashboard();
			})
			.catch(() => {
				// The server already showed the real error via frappe.call()'s
				// own default error dialog -- nothing to improvise here.
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
			<div class="fg-fact-queue-section">${this.render_queue_section()}</div>
		`);
		this.bind_queue_section_events();
	}

	// Purely informational -- these 4 cards are never clickable. Filtering
	// the cola below happens only through its own Tabs (Todos/Pendientes/
	// Parciales/Con incidencia), a deliberately separate control.
	render_kpis() {
		const s = this.summary || {};
		const cards = [
			{ key: "pendientes", label: __("Pendientes de facturar"), sub: __("Sin ninguna unidad facturada"), i: "clock", mod: "fact-pendientes" },
			{ key: "parciales", label: __("Facturación parcial"), sub: __("Parcialmente entregados"), i: "truck", mod: "fact-parciales" },
			{ key: "facturados_hoy", label: __("Facturados hoy"), sub: __("Facturas emitidas hoy"), i: "check", mod: "fact-facturados-hoy" },
			{ key: "con_incidencia", label: __("Con incidencia"), sub: __("Faltante abierto"), i: "triangle-alert", mod: "fact-con-incidencia" },
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

	// -- Cola: tabs + búsqueda + paginación, todo client-side sobre la
	// misma lista que devuelve get_pending_pick_lists(). --------------------

	matches_queue_tab(pl, filter) {
		if (!filter || filter === "todos") return true;
		if (filter === "pendientes") return pl.delivery_status === "Not Delivered";
		if (filter === "parciales") return pl.delivery_status === "Partly Delivered";
		if (filter === "con_incidencia") return !!pl.has_open_shortage;
		return true;
	}

	matches_queue_search(pl, search) {
		const haystack = [pl.commercial_name, pl.sales_order, pl.name, pl.customer_name, pl.cliente]
			.filter(Boolean)
			.join(" ")
			.toLowerCase();
		return haystack.includes(search);
	}

	get_filtered_queue() {
		const queue = this.queue || [];
		let items = queue.filter((pl) => this.matches_queue_tab(pl, this.queue_filter));
		const search = (this.queue_search || "").trim().toLowerCase();
		if (search) items = items.filter((pl) => this.matches_queue_search(pl, search));
		return items;
	}

	render_queue_section() {
		const queue = this.queue || [];
		const tabs = ["todos", "pendientes", "parciales", "con_incidencia"];
		const tab_meta = {
			todos: { label: __("Todos"), i: "layout-grid" },
			pendientes: { label: __("Pendientes"), i: "clock" },
			parciales: { label: __("Parciales"), i: "truck" },
			con_incidencia: { label: __("Con incidencia"), i: "triangle-alert" },
		};

		const tabs_html = tabs
			.map((key) => {
				const count = queue.filter((pl) => this.matches_queue_tab(pl, key)).length;
				const meta = tab_meta[key];
				return `<button type="button" class="fg-fact-tab ${
					this.queue_filter === key ? "is-active" : ""
				}" data-filter="${key}">${icon(meta.i, "fg-icon-sm")} ${meta.label} (${count})</button>`;
			})
			.join("");

		const filtered = this.get_filtered_queue();
		const paged = paginate(filtered, this.queue_page, PAGE_SIZE);
		this.queue_page = paged.page;

		const cards_html = paged.page_items.length
			? paged.page_items.map((pl) => this.render_queue_card(pl)).join("")
			: `<div class="fg-empty">${__("No hay pedidos que coincidan.")}</div>`;

		return `
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Cola de facturación")}</div>
			</div>
			<div class="fg-fact-toolbar">
				<div class="fg-fact-search-wrap">
					${icon("search", "fg-fact-search-icon")}
					<input type="text" class="fg-fact-search-input" placeholder="${__(
						"Buscar por PEDIDO-N, cliente o Pick List..."
					)}" value="${frappe.utils.escape_html(this.queue_search || "")}">
				</div>
			</div>
			<div class="fg-fact-tabs">${tabs_html}</div>
			<div class="fg-fact-queue-cards">${cards_html}</div>
			<div class="fg-fact-queue-pagination">${this.render_queue_pagination_html(
				paged.total,
				paged.page,
				paged.page_count
			)}</div>
		`;
	}

	render_queue_pagination_html(total, page, page_count) {
		if (!total) return "";
		const start = (page - 1) * PAGE_SIZE + 1;
		const end = Math.min(page * PAGE_SIZE, total);
		return `
			<div class="fg-fact-pagination-info">${__("Mostrando {0} a {1} de {2} pedidos", [start, end, total])}</div>
			<div class="fg-fact-pagination-controls">
				<button type="button" class="fg-fact-pagination-btn fg-fact-pagination-prev" ${
					page <= 1 ? "disabled" : ""
				}>${icon("chevron-left")}</button>
				<span class="fg-fact-pagination-page">${page}</span>
				<button type="button" class="fg-fact-pagination-btn fg-fact-pagination-next" ${
					page >= page_count ? "disabled" : ""
				}>${icon("chevron-right")}</button>
			</div>
		`;
	}

	// Card fields, exactly per brief: PEDIDO-N, cliente, fecha, cantidad de
	// referencias, unidades, bodeguero, delivery_status, progreso
	// per_delivered, indicador de incidencia -- and one action, REVISAR.
	render_queue_card(pl) {
		const status = delivery_status_meta(pl.delivery_status);
		const pedido_label = pl.commercial_name || pl.sales_order || pl.name;
		const customer_label = frappe.utils.escape_html(pl.customer_name || pl.cliente || __("Sin cliente"));
		const fecha = pl.fecha ? frappe.datetime.str_to_user(pl.fecha) : "—";
		const bodeguero = pl.fg_started_by_fullname || pl.fg_started_by || __("Sin asignar");
		const pct = flt(pl.per_delivered);

		const shortage_badge = pl.has_open_shortage
			? `<span class="fg-badge fg-badge--fact-incidencia">${icon("triangle-alert", "fg-icon-sm")} ${__(
					"Incidencia"
			  )}</span>`
			: "";

		return `
			<div class="fg-fact-queue-card" data-name="${frappe.utils.escape_html(pl.name)}">
				<div class="fg-fact-queue-card-top">
					<div class="fg-fact-queue-card-id">${__("PEDIDO")} #${frappe.utils.escape_html(pedido_label)}</div>
					<div class="fg-fact-queue-card-badges">
						<span class="fg-badge fg-badge--${status.mod}">${status.label}</span>
						${shortage_badge}
					</div>
				</div>
				<div class="fg-fact-queue-card-customer">${icon("user", "fg-icon-sm")} ${customer_label}</div>
				<div class="fg-fact-queue-card-meta">
					<span>${icon("calendar", "fg-icon-sm")} ${fecha}</span>
					<span>${icon("package", "fg-icon-sm")} ${pl.item_count} ${
			pl.item_count === 1 ? __("referencia") : __("referencias")
		}</span>
					<span>${format_qty(pl.total_qty)} ${__("unidades")}</span>
				</div>
				<div class="fg-fact-queue-card-meta">
					<span>${icon("clipboard-list", "fg-icon-sm")} ${frappe.utils.escape_html(bodeguero)}</span>
					<span class="fg-fact-queue-card-picklist">${frappe.utils.escape_html(pl.name)}</span>
				</div>
				<div class="fg-progress-block">
					<div class="fg-progress-label-row">
						<span>${__("Progreso de entrega")}</span>
						<span class="fg-progress-pct">${format_qty(pct)}%</span>
					</div>
					<div class="fg-progress-track"><div class="fg-progress-fill" style="--fg-progress-width:${pct}%"></div></div>
				</div>
				<button type="button" class="fg-btn fg-btn--solid-primary fg-fact-review-btn">
					${icon("eye", "fg-icon-sm")} ${__("REVISAR")}
				</button>
			</div>
		`;
	}

	bind_queue_section_events() {
		// Search re-renders only the cards + pagination containers, never
		// the search input itself -- otherwise it would lose focus on
		// every keystroke (same convention as page/bodega/bodega.js).
		const rerender_cards = () => {
			const filtered = this.get_filtered_queue();
			const paged = paginate(filtered, this.queue_page, PAGE_SIZE);
			this.queue_page = paged.page;
			const cards_html = paged.page_items.length
				? paged.page_items.map((pl) => this.render_queue_card(pl)).join("")
				: `<div class="fg-empty">${__("No hay pedidos que coincidan.")}</div>`;
			this.$body.find(".fg-fact-queue-cards").html(cards_html);
			this.$body
				.find(".fg-fact-queue-pagination")
				.html(this.render_queue_pagination_html(paged.total, paged.page, paged.page_count));
		};

		this.$body.find(".fg-fact-search-input").on("input", (e) => {
			this.queue_search = $(e.currentTarget).val();
			this.queue_page = 1;
			rerender_cards();
		});

		this.$body.find(".fg-fact-tab").on("click", (e) => {
			this.queue_filter = $(e.currentTarget).data("filter");
			this.queue_page = 1;
			this.$body.find(".fg-fact-queue-section").html(this.render_queue_section());
			this.bind_queue_section_events();
		});

		// Delegated on the stable container -- survives rerender_cards()'s
		// .html() swap without needing to be re-bound on every keystroke.
		this.$body.find(".fg-fact-queue-cards").on("click", ".fg-fact-queue-card", (e) => {
			this.open_detail($(e.currentTarget).data("name"));
		});

		this.$body.find(".fg-fact-queue-pagination").on("click", ".fg-fact-pagination-prev", () => {
			this.queue_page = Math.max(this.queue_page - 1, 1);
			rerender_cards();
		});
		this.$body.find(".fg-fact-queue-pagination").on("click", ".fg-fact-pagination-next", () => {
			this.queue_page = this.queue_page + 1;
			rerender_cards();
		});
	}

	// =====================================================================
	// Detalle ("REVISAR") -- get_pick_list_for_facturacion() for the read
	// side, generate_invoice() (Commit 21.3) for the one write this Page
	// performs, gated by the frontend-only "VERIFICADO" checklist below.
	// =====================================================================
	open_detail(name) {
		if (!name) return;
		this.detail_pick_list = name;
		this.detail = null;
		// has_open_shortage is only ever returned by get_pending_pick_lists()
		// (the queue), never by get_pick_list_for_facturacion() -- read it
		// off the already-fetched queue entry instead of adding a new field
		// to the detail endpoint. Purely a display flag, never a gate.
		const pl = (this.queue || []).find((p) => p.name === name);
		this.detail_has_shortage = pl ? !!pl.has_open_shortage : false;
		this.verified_rows = new Set();
		this.state.view = "detail";
		this.set_busy(true);
		this.render_detail_skeleton();

		this.call("get_pick_list_for_facturacion", { name: name })
			.then((detail) => {
				this.detail = detail;
				this.render_detail();
			})
			.catch(() => this.back_to_dashboard())
			.finally(() => this.set_busy(false));
	}

	// Reached from "Volver", from a failed detail load, and (via the
	// success dialog's onhide) after a factura is generated -- every path
	// discards the checklist (never persisted, per the brief) and reloads
	// both the KPIs and the queue from the server, so a Pick List that just
	// became Fully Delivered disappears and one that's now Partly Delivered
	// shows only the real remainder on its next detail load.
	back_to_dashboard() {
		this.detail = null;
		this.detail_pick_list = null;
		this.detail_has_shortage = false;
		this.verified_rows = new Set();
		this.load_dashboard();
	}

	render_detail_skeleton() {
		this.$body.html(`
			<div class="fg-np-header">
				<button type="button" class="fg-np-back">${icon("arrow-left")} ${__("Volver")}</button>
				<div class="fg-np-title">${__("Revisar pedido")}</div>
			</div>
			<div class="fg-skeleton fg-product-skeleton"></div>
			<div class="fg-skeleton fg-product-skeleton"></div>
			<div class="fg-skeleton fg-product-skeleton"></div>
		`);
		this.$body.find(".fg-np-back").on("click", () => this.back_to_dashboard());
	}

	render_detail() {
		const d = this.detail;
		const status = delivery_status_meta(d.delivery_status);
		const pedido_label = d.commercial_name || d.sales_order || d.pick_list;
		const customer_label = frappe.utils.escape_html(d.customer_name || d.customer || __("Sin cliente"));
		const fecha = d.fecha ? frappe.datetime.str_to_user(d.fecha) : "—";
		const bodeguero = d.fg_started_by_fullname || d.fg_started_by || __("Sin asignar");
		const pct = flt(d.per_delivered);

		const rows = d.rows || [];
		const rows_html = rows.length
			? rows.map((r) => this.render_detail_item_card(r)).join("")
			: `<div class="fg-empty fg-empty--sm">${__("Sin productos.")}</div>`;

		this.$body.html(`
			<div class="fg-np-header">
				<button type="button" class="fg-np-back">${icon("arrow-left")} ${__("Volver")}</button>
				<div class="fg-np-title">${__("Revisar pedido")}</div>
			</div>

			<div class="fg-fact-detail-card">
				<div class="fg-fact-detail-top">
					<div class="fg-fact-detail-id">${__("PEDIDO")} #${frappe.utils.escape_html(pedido_label)}</div>
					<span class="fg-badge fg-badge--${status.mod}">${status.label}</span>
				</div>
				<div class="fg-fact-detail-picklist">
					${icon("clipboard-list", "fg-icon-sm")} ${__("Pick List")}: ${frappe.utils.escape_html(d.pick_list)}
				</div>
				<div class="fg-fact-detail-meta">
					<span>${icon("user", "fg-icon-sm")} ${customer_label}</span>
					<span>${icon("package", "fg-icon-sm")} ${bodeguero}</span>
					<span>${icon("calendar", "fg-icon-sm")} ${fecha}</span>
				</div>
				<div class="fg-progress-block">
					<div class="fg-progress-label-row">
						<span>${__("Progreso de entrega")}</span>
						<span class="fg-progress-pct">${format_qty(pct)}%</span>
					</div>
					<div class="fg-progress-track"><div class="fg-progress-fill" style="--fg-progress-width:${pct}%"></div></div>
				</div>
			</div>

			<div class="fg-np-section">
				<div class="fg-np-section-title">${__("Productos")}</div>
				<div class="fg-fact-detail-items">${rows_html}</div>
			</div>

			<div class="fg-fact-generate-wrap">${this.render_generate_section()}</div>
		`);
		this.$body.find(".fg-np-back").on("click", () => this.back_to_dashboard());
		this.bind_item_verify_checkboxes();
		this.bind_generate_section_events();
	}

	// Per producto, exactly per brief: item_name, item_code, qty alistada,
	// qty ya facturada, qty pendiente de facturar, stock actual
	// (informativo), precio unitario, importe. actual_qty never influences
	// anything here -- it is rendered purely as information, same as the
	// server's own contract for it. Commit 21.5 adds the one interactive
	// element on this card: a "VERIFICADO" checkbox, only for lines that
	// actually have something pending (qty_to_invoice > 0) -- a line
	// already fully invoiced never demands a check, per the brief.
	render_detail_item_card(r) {
		const has_pending = flt(r.qty_to_invoice) > 0;
		const is_verified = this.verified_rows.has(r.row_name);

		const verify_html = has_pending
			? `
				<label class="fg-fact-verify-row">
					<input type="checkbox" class="fg-fact-verify-checkbox" ${is_verified ? "checked" : ""}>
					<span>${__("VERIFICADO")}</span>
				</label>
			`
			: `
				<div class="fg-fact-verify-row fg-fact-verify-row--none">
					${icon("check", "fg-icon-sm")} ${__("Nada pendiente de facturar")}
				</div>
			`;

		return `
			<div class="fg-fact-item-card ${is_verified ? "is-verified" : ""}" data-row="${frappe.utils.escape_html(
			r.row_name
		)}">
				<div class="fg-fact-item-identity">
					<div class="fg-fact-item-thumb">${icon("package")}</div>
					<div>
						<div class="fg-fact-item-name">${frappe.utils.escape_html(r.item_name || r.item_code)}</div>
						<div class="fg-fact-item-code">${frappe.utils.escape_html(r.item_code)}</div>
					</div>
				</div>
				<div class="fg-fact-item-qtygrid">
					<div class="fg-fact-qty-col">
						<div class="fg-fact-qty-label">${__("Alistada")}</div>
						<div class="fg-fact-qty-value">${format_qty(r.picked_qty)}</div>
					</div>
					<div class="fg-fact-qty-col">
						<div class="fg-fact-qty-label">${__("Ya facturada")}</div>
						<div class="fg-fact-qty-value">${format_qty(r.delivered_qty)}</div>
					</div>
					<div class="fg-fact-qty-col fg-fact-qty-col--pending">
						<div class="fg-fact-qty-label">${__("Pendiente")}</div>
						<div class="fg-fact-qty-value">${format_qty(r.qty_to_invoice)}</div>
					</div>
					<div class="fg-fact-qty-col fg-fact-qty-col--stock">
						<div class="fg-fact-qty-label">${__("Stock actual")} <small>(${__("informativo")})</small></div>
						<div class="fg-fact-qty-value">${format_qty(r.actual_qty)}</div>
					</div>
				</div>
				<div class="fg-fact-item-price">
					<div class="fg-fact-price-col">
						<div class="fg-fact-qty-label">${__("Precio unitario")}</div>
						<div class="fg-fact-qty-value">${fg_format_currency(r.rate)}</div>
					</div>
					<div class="fg-fact-price-col fg-fact-price-col--amount">
						<div class="fg-fact-qty-label">${__("Importe")}</div>
						<div class="fg-fact-qty-value">${fg_format_currency(r.amount)}</div>
					</div>
				</div>
				${verify_html}
			</div>
		`;
	}

	bind_item_verify_checkboxes() {
		// Delegated on the stable items container -- render_detail() always
		// replaces the whole $body via .html() before this runs, so there is
		// never a stale duplicate binding to worry about.
		this.$body.find(".fg-fact-detail-items").on("change", ".fg-fact-verify-checkbox", (e) => {
			const $cb = $(e.currentTarget);
			const $card = $cb.closest(".fg-fact-item-card");
			const row_name = $card.data("row");
			if ($cb.is(":checked")) {
				this.verified_rows.add(row_name);
			} else {
				this.verified_rows.delete(row_name);
			}
			$card.toggleClass("is-verified", $cb.is(":checked"));
			this.refresh_generate_gate();
		});
	}

	// =====================================================================
	// GENERAR FACTURA -- gated purely by the frontend-only checklist above;
	// the request itself never carries a check, a qty, a rate or a total --
	// only pick_list_name. The server (generate_invoice(), Commit 21.3)
	// recomputes everything from the real, current Pick List.
	// =====================================================================
	get_required_rows() {
		const rows = (this.detail && this.detail.rows) || [];
		return rows.filter((r) => flt(r.qty_to_invoice) > 0);
	}

	can_generate_invoice() {
		const required = this.get_required_rows();
		if (!required.length || this.busy) return false;
		return required.every((r) => this.verified_rows.has(r.row_name));
	}

	// Resumen previo + botón. Total a facturar is a plain sum of the
	// `amount` values the server already returned for the still-pending
	// lines -- no rate/amount is ever recomputed here.
	render_generate_section() {
		const required = this.get_required_rows();
		const verified_count = required.filter((r) => this.verified_rows.has(r.row_name)).length;
		const total = required.reduce((sum, r) => sum + flt(r.amount), 0);
		const can_generate = this.can_generate_invoice();

		const shortage_html = this.detail_has_shortage
			? `
				<div class="fg-fact-shortage-warning">
					${icon("triangle-alert", "fg-icon-sm")}
					${__("Este pedido tiene un faltante/incidencia abierta.")}
				</div>
			`
			: "";

		return `
			${shortage_html}
			<div class="fg-fact-generate-section">
				<div class="fg-fact-generate-summary">
					<span>${__("Productos verificados")}: <strong>${verified_count} / ${required.length}</strong></span>
					<span>${__("Total a facturar")}: <strong>${fg_format_currency(total)}</strong></span>
				</div>
				<button type="button" class="fg-btn fg-btn--solid-primary fg-btn--lg fg-fact-generate-btn" ${
					can_generate ? "" : "disabled"
				}>
					${icon("check")} ${this.busy ? __("Generando factura...") : __("GENERAR FACTURA")}
				</button>
			</div>
		`;
	}

	bind_generate_section_events() {
		this.$body.find(".fg-fact-generate-btn").on("click", () => this.open_generate_invoice_dialog());
	}

	// Re-renders only the summary/button block -- checkbox toggles and the
	// busy state around generate_invoice() never re-render the whole
	// detail (would lose scroll position and re-fetch nothing new).
	refresh_generate_gate() {
		const $wrap = this.$body.find(".fg-fact-generate-wrap");
		if (!$wrap.length) return;
		$wrap.html(this.render_generate_section());
		this.bind_generate_section_events();
	}

	// Confirmación previa a la llamada real -- cliente, número de productos,
	// unidades y total, todos leídos de this.detail (ya devuelto por el
	// backend), nunca recalculados.
	open_generate_invoice_dialog() {
		if (!this.can_generate_invoice()) return;
		const d = this.detail;
		const required = this.get_required_rows();

		const pedido_label = d.commercial_name || d.sales_order || d.pick_list;
		const customer_label = frappe.utils.escape_html(d.customer_name || d.customer || __("Sin cliente"));
		const total_units = required.reduce((sum, r) => sum + flt(r.qty_to_invoice), 0);
		const total = required.reduce((sum, r) => sum + flt(r.amount), 0);

		const dialog = new frappe.ui.Dialog({
			title: __("Generar factura"),
			fields: [{ fieldtype: "HTML", fieldname: "confirm_html" }],
			primary_action_label: __("GENERAR FACTURA"),
			primary_action: () => {
				dialog.hide();
				this.submit_generate_invoice();
			},
			secondary_action_label: __("CANCELAR"),
			secondary_action: () => dialog.hide(),
		});

		dialog.$wrapper.addClass("fg-fact-confirm-dialog");
		dialog.fields_dict.confirm_html.$wrapper.html(`
			<div class="fg-fact-confirm-summary">
				<div class="fg-fact-confirm-question">${__("¿Generar factura para PEDIDO #{0}?", [
					frappe.utils.escape_html(pedido_label),
				])}</div>
				<div class="fg-fact-confirm-row"><span>${__("Cliente")}</span><strong>${customer_label}</strong></div>
				<div class="fg-fact-confirm-row"><span>${__("Productos")}</span><strong>${required.length}</strong></div>
				<div class="fg-fact-confirm-row"><span>${__("Unidades")}</span><strong>${format_qty(total_units)}</strong></div>
				<div class="fg-fact-confirm-row"><span>${__("Total")}</span><strong>${fg_format_currency(total)}</strong></div>
			</div>
		`);
		dialog.show();
	}

	// The one write in this Page -- pick_list_name only, nothing else.
	// this.busy is the same single-flight guard render_generate_section()
	// already reads to disable/relabel the button, so a second click while
	// a request is in flight cannot start a second one (the button is
	// disabled) and this early-return covers any other path that could
	// reach here (e.g. a stale dialog reference).
	submit_generate_invoice() {
		if (this.busy) return;
		const pick_list_name = this.detail_pick_list;
		if (!pick_list_name) return;

		this.set_busy(true);
		this.refresh_generate_gate();

		this.call("generate_invoice", { pick_list_name: pick_list_name })
			.then((result) => {
				this.show_invoice_success_dialog(result);
			})
			.catch(() => {
				// The server already showed the real error via frappe.call()'s
				// own default error dialog -- including the known 131505-
				// Ventas-FG accounting blocker, never worked around here. The
				// user stays on the detail, the checklist is left exactly as
				// it was (never cleared on failure), and nothing here assumes
				// an invoice was actually created.
			})
			.finally(() => {
				this.set_busy(false);
				this.refresh_generate_gate();
			});
	}

	// ✓ FACTURA GENERADA -- every number here comes straight from
	// generate_invoice()'s own response (Commit 21.3:
	// {sales_invoice, pick_list, sales_order, commercial_name, status,
	// item_count, total_qty, grand_total}), nothing recomputed. Closing the
	// dialog by ANY means (either action button, the close icon, or ESC)
	// always routes through onhide -> back_to_dashboard(), so the checklist
	// is cleared and the KPIs/cola are reloaded from the server exactly
	// once, regardless of how the user leaves this screen.
	show_invoice_success_dialog(result) {
		const dialog = new frappe.ui.Dialog({
			title: "✓ " + __("FACTURA GENERADA"),
			fields: [{ fieldtype: "HTML", fieldname: "success_html" }],
			primary_action_label: __("VOLVER A FACTURACIÓN"),
			primary_action: () => dialog.hide(),
			secondary_action_label: __("VER FACTURA"),
			secondary_action: () => {
				dialog.hide();
				frappe.set_route("Form", "Sales Invoice", result.sales_invoice);
			},
			onhide: () => this.back_to_dashboard(),
		});

		dialog.$wrapper.addClass("fg-fact-success-dialog");
		dialog.fields_dict.success_html.$wrapper.html(`
			<div class="fg-fact-success-summary">
				<div class="fg-fact-success-pedido">${frappe.utils.escape_html(
					result.commercial_name || result.sales_order || result.pick_list || ""
				)}</div>
				<div class="fg-fact-success-invoice">${__("Factura")}: <strong>${frappe.utils.escape_html(
			result.sales_invoice
		)}</strong></div>
				<div class="fg-fact-success-counts">
					<span>${result.item_count} ${result.item_count === 1 ? __("referencia") : __("referencias")}</span>
					<span>${format_qty(result.total_qty)} ${__("unidades")}</span>
				</div>
				<div class="fg-fact-success-total">${__("Total")}: <strong>${fg_format_currency(result.grand_total)}</strong></div>
			</div>
		`);
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

function paginate(items, page, page_size) {
	const total = items.length;
	const page_count = Math.max(Math.ceil(total / page_size), 1);
	const safe_page = Math.min(Math.max(page || 1, 1), page_count);
	const start = (safe_page - 1) * page_size;
	return { page_items: items.slice(start, start + page_size), total, page_count, page: safe_page };
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

function format_qty(v) {
	const n = flt(v);
	return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

// Facturación is the one Page in this app allowed to render money (Commit
// 21.1's own permission model) -- reuses frappe's own global
// format_currency() the same way Desk's own list views/reports do, with a
// plain fallback only if it is somehow unavailable. Named fg_format_currency
// (not format_currency) so this file's own top-level declaration can never
// shadow/collide with frappe's real global of the same name.
function fg_format_currency(v) {
	const n = flt(v);
	return window.format_currency ? window.format_currency(n) : n.toFixed(2);
}

// Pure presentation mapping of Pick List.delivery_status's two native
// values that can ever appear in this queue (Fully Delivered is excluded
// server-side, per api/facturacion.py's own _QUEUE_FILTERS) to a Spanish
// label + badge color. Never invents a new state -- delivery_status itself
// is read verbatim from the server on every card/detail.
function delivery_status_meta(status) {
	const map = {
		"Not Delivered": { label: __("Pendiente"), mod: "fact-pendiente" },
		"Partly Delivered": { label: __("Parcial"), mod: "fact-parcial" },
		"Fully Delivered": { label: __("Facturado"), mod: "fact-facturado" },
	};
	return map[status] || { label: status || "—", mod: "fact-pendiente" };
}
