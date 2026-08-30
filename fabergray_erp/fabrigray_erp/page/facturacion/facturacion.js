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
		this.busy = false;

		this.summary = null;
		this.rows = [];
		this.total = 0;
		this.queue_filter = ""; // "" (todos) | "Pendiente" | "Facturado"
		this.queue_search = "";
		this.queue_page = 1;
		this._search_debounce = null;

		// Review modal state -- see open_review_dialog(). Reset every time a
		// modal opens/closes so a stale detail from a previous Pick List can
		// never leak into a newly-opened one.
		this._review_dialog = null;
		this._review_pick_list = null;
		this._review_detail = null;
		this._review_saving_rows = new Set(); // row_name -> in-flight set_invoicing_item_checked() call

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
		return Promise.all([this.call("get_invoicing_summary"), this.load_queue()])
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
							"Buscar por PEDIDO-N, cliente o Pick List..."
						)}" value="${frappe.utils.escape_html(this.queue_search || "")}">
					</div>
				</div>
				${this.render_tabs_html()}
				<div class="fg-fact-queue-cards">${this.render_cards_html()}</div>
				<div class="fg-fact-queue-pagination">${this.render_queue_pagination_html()}</div>
			</div>
		`);
		this.bind_body_events();
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
};

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, no server calls, no state.
// Intentionally duplicated (not imported) from ventas.js/bodega.js/
// jefe_de_bodega.js/cotizaciones.js, same reasoning as Commit 6: a few
// lines each, zero business logic, keeps this Page's asset loading
// independent of theirs.
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
