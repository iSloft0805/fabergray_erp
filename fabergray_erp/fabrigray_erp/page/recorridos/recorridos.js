// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["recorridos"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Recorridos"),
		single_column: true,
	});
	new fabergray_erp.Recorridos(page);
};

// Commit 24.2 -- visual layer for the Commit 24.1 Recorridos backend
// (api/recorridos.py). This Page never re-implements ANY business rule
// already enforced server-side (eligibility, double-assignment locking,
// Borrador-only editing, status transitions, company isolation) -- every
// mutation here is a thin call into get_available_orders()/get_routes()/
// get_routes_summary()/get_route_detail()/create_route()/
// update_route_stops()/plan_route()/cancel_route(), the exact same
// whitelisted functions Commit 24.1's own backend tests already cover.
// NO maps/geocoding/Waze/Google Maps/GPS/tracking/signature/photo/
// delivery-proof/novedades/driver-mobile-UI/automatic-route-optimization
// in this commit -- those are 24.3+.
//
// Reorder (creating a route, or editing an existing Borrador's stops) uses
// plain up/down buttons, not drag-and-drop: this app has no existing
// drag-and-drop precedent anywhere and no sortable-list dependency already
// loaded, so up/down buttons are the lower-risk, zero-new-dependency
// choice that is just as usable on a touch/iPad screen as a mouse drag.
// update_route_stops() always receives the COMPLETE desired pick_lists
// list in the exact visual order -- never a partial diff -- matching its
// own "full replacement" server-side semantics exactly.
fabergray_erp.Recorridos = class Recorridos {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.recorridos.";
		this.busy = false;

		this.summary = null;
		this.active_tab = "disponibles"; // "disponibles" | "recorridos" | "historial"

		// Fase 26.2 -- "list" (KPIs + tabs) | "active-route" (Modo Recorrido,
		// rendered in this same Page body, never inside a frappe.ui.Dialog).
		this.view = "list";
		this.active_route = null;

		// -- Pedidos disponibles ------------------------------------------
		this.avail_rows = [];
		this.avail_total = 0;
		this.avail_search = "";
		this.avail_page = 1;
		this._avail_search_debounce = null;
		// Map<pick_list, row> -- insertion order IS the route-creation order
		// shown/edited inside the CREAR RECORRIDO modal (see open_create_dialog()).
		this.selected = new Map();

		// -- Recorridos (Borrador/Planificado/En Ruta) ---------------------
		this.routes_rows = [];
		this.routes_total = 0;
		this.routes_page = 1;
		this.routes_status_filter = ""; // "" (todos) | "Borrador" | "Planificado" | "En Ruta"
		// Commit 25.20 -- server-side (get_routes() is page-paginated,
		// section 8), debounced ~300ms (section 9).
		this.routes_search = "";
		this._routes_search_debounce = null;

		// -- Historial (Completado/Cancelado) -------------------------------
		this.hist_rows = [];
		this.hist_total = 0;
		this.hist_page = 1;
		this.hist_status_filter = ""; // "" (todos) | "Completado" | "Cancelado"
		this.hist_search = "";
		this._hist_search_debounce = null;

		this.$app = $('<div class="fg-shell fg-recorridos">').appendTo(this.page.body);
		this.render_shell();
		this.load_all();
	}

	// -------------------------------------------------------------------
	// Thin API wrapper -- same idiom as page/facturacion/facturacion.js's
	// own _frappe_call(): frappe.call() itself does not return a real
	// Promise, so every .then()/.catch()/.finally() chain below needs this.
	// -------------------------------------------------------------------
	_frappe_call(method, args, extra) {
		return new Promise((resolve, reject) => {
			frappe.call(
				Object.assign(
					{
						method: method,
						args: args || {},
						callback: (r) => resolve(r.message),
						error: (r) => reject(r),
					},
					extra || {}
				)
			);
		});
	}

	call(method, args, extra) {
		return this._frappe_call(this.method_prefix + method, args, extra);
	}

	// create_route()/update_route_stops() are the only two calls that can
	// hit a real double-assignment race (brief section 23 -- another user
	// claimed one of these same Pick Lists while this screen was open).
	// error_handlers below is Frappe's own mechanism (frappe/public/js/
	// frappe/request.js -- keyed by the server exception's exc_type,
	// i.e. the Python exception CLASS NAME) for replacing the default
	// error dialog for exactly ONE exc_type, without hiding any OTHER
	// error -- every other failure still shows frappe.call's normal error
	// dialog untouched, matching this app's "no esconder errores"
	// convention (facturacion.js's own .catch() comment).
	call_route_write(method, args) {
		return this.call(method, args, {
			error_handlers: {
				PickListAlreadyAssignedError: () => {
					frappe.show_alert(
						{
							message: __("Uno o más pedidos ya fueron asignados a otro recorrido. La lista será actualizada."),
							indicator: "orange",
						},
						6
					);
				},
			},
		});
	}

	// -------------------------------------------------------------------
	// Shell: header stays fixed, tabs + body swap underneath.
	// -------------------------------------------------------------------
	render_shell() {
		const fullname = frappe.session.user_fullname || frappe.session.user;
		this.$app.html(`
			<div class="fg-header">
				<div class="fg-header-brand">
					<span class="fg-header-logo">FABRIGRAY</span>
					<span class="fg-header-sep">|</span>
					<span class="fg-header-title">${__("RECORRIDOS")}</span>
				</div>
				<div class="fg-header-user">
					<div class="fg-header-user-info">
						<div class="fg-header-user-name">${frappe.utils.escape_html(fullname)}</div>
						<div class="fg-header-user-role">${__("Recorrido")}</div>
					</div>
					<div class="fg-header-avatar">${get_initials(fullname)}</div>
					<button type="button" class="fg-refresh-btn" title="${__("Actualizar")}">${icon("refresh-cw")}</button>
				</div>
			</div>
			<div class="fg-body"></div>
		`);
		this.$body = this.$app.find(".fg-body");
		this.$app.find(".fg-refresh-btn").on("click", () => {
			if (this.view === "active-route" && this.active_route) this.open_active_route(this.active_route.name);
			else this.load_all();
		});
	}

	set_busy(is_busy) {
		this.busy = !!is_busy;
		this.$app.find(".fg-refresh-btn").prop("disabled", this.busy);
		this.$app.toggleClass("fg-loading", !!is_busy);
	}

	// =====================================================================
	// Load + render
	// =====================================================================
	load_all() {
		this.set_busy(true);
		this.render_skeleton();
		const tab_load =
			this.active_tab === "recorridos"
				? this.load_routes()
				: this.active_tab === "historial"
				? this.load_history()
				: this.load_available();

		return Promise.all([this.call("get_routes_summary"), tab_load])
			.then(([summary]) => {
				this.summary = summary;
				this.render_body();
			})
			.catch(() => {
				// The server already showed the real error via frappe.call()'s
				// own default error dialog (or, for the one known race case,
				// call_route_write()'s friendlier alert) -- nothing to
				// improvise here, same convention as every other Page.
			})
			.finally(() => this.set_busy(false));
	}

	render_skeleton() {
		this.$body.html(`
			<div class="fg-skeleton-kpis">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
			<div class="fg-skeleton-cards">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
		`);
	}

	render_body() {
		this.$body.html(`
			${this.render_kpis()}
			<div class="fg-recorridos-tabs-nav">${this.render_top_tabs_html()}</div>
			<div class="fg-recorridos-tab-body"></div>
		`);
		this.render_active_tab();
		this.bind_body_events();
	}

	render_active_tab() {
		const $t = this.$body.find(".fg-recorridos-tab-body");
		if (this.active_tab === "recorridos") {
			$t.html(this.render_routes_section_html());
			this.bind_routes_events();
		} else if (this.active_tab === "historial") {
			$t.html(this.render_history_section_html());
			this.bind_history_events();
		} else {
			$t.html(this.render_available_section_html());
			this.bind_available_events();
			this.render_selection_bar();
		}
	}

	switch_tab(tab, extra) {
		this.active_tab = tab;
		if (extra && extra.status !== undefined) {
			if (tab === "recorridos") this.routes_status_filter = extra.status;
			if (tab === "historial") this.hist_status_filter = extra.status;
		}
		const needs_load =
			(tab === "recorridos" && !this._routes_loaded) ||
			(tab === "historial" && !this._hist_loaded) ||
			(tab === "disponibles" && !this._avail_loaded);
		this.$body.find(".fg-recorridos-tabs-nav").replaceWith(this.render_top_tabs_html());
		if (needs_load || extra) {
			this.set_busy(true);
			const loader =
				tab === "recorridos" ? this.load_routes() : tab === "historial" ? this.load_history() : this.load_available();
			loader
				.then(() => this.render_active_tab())
				.catch(() => {})
				.finally(() => this.set_busy(false));
		} else {
			this.render_active_tab();
		}
	}

	// =====================================================================
	// KPIs (brief section 4) -- get_routes_summary(), Commit 24.2's own
	// small read-only endpoint (see api/recorridos.py's own docstring for
	// why get_available_orders()/get_route_detail() alone could not serve
	// this). Each card is a real button: clicking it jumps straight to the
	// matching tab/status filter, same interactive-KPI pattern fg_shell.css
	// already supports (button.fg-kpi).
	// =====================================================================
	render_kpis() {
		const s = this.summary || {};
		const cards = [
			{
				key: "available_orders",
				label: __("PEDIDOS DISPONIBLES"),
				i: "package",
				mod: "recorridos-disponibles",
				tab: "disponibles",
			},
			{
				key: "borrador",
				label: __("RUTAS EN BORRADOR"),
				i: "file-text",
				mod: "recorridos-borrador",
				tab: "recorridos",
				status: "Borrador",
			},
			{
				key: "planificado",
				label: __("PLANIFICADAS"),
				i: "calendar-check",
				mod: "recorridos-planificado",
				tab: "recorridos",
				status: "Planificado",
			},
			{
				key: "en_ruta",
				label: __("EN RUTA"),
				i: "truck",
				mod: "recorridos-en-ruta",
				tab: "recorridos",
				status: "En Ruta",
			},
		];
		const html = cards
			.map(
				(c) => `
				<button type="button" class="fg-kpi fg-kpi--${c.mod}" data-tab="${c.tab}" data-status="${c.status || ""}">
					<div class="fg-kpi-icon">${icon(c.i)}</div>
					<div class="fg-kpi-number">${s[c.key] ?? 0}</div>
					<div class="fg-kpi-label">${c.label}</div>
				</button>
			`
			)
			.join("");
		return `<div class="fg-kpis fg-kpis--recorridos">${html}</div>`;
	}

	render_top_tabs_html() {
		const s = this.summary || {};
		const tabs = [
			{ key: "disponibles", label: __("PEDIDOS DISPONIBLES"), i: "package", count: s.available_orders ?? 0 },
			{
				key: "recorridos",
				label: __("RECORRIDOS"),
				i: "route",
				count: (s.borrador ?? 0) + (s.planificado ?? 0) + (s.en_ruta ?? 0),
			},
			{ key: "historial", label: __("HISTORIAL"), i: "history" },
		];
		const html = tabs
			.map(
				(t) => `
				<button type="button" class="fg-recorridos-tab ${this.active_tab === t.key ? "is-active" : ""}" data-tab="${
					t.key
				}">${icon(t.i, "fg-icon-sm")} ${t.label}${t.count !== undefined ? ` (${t.count})` : ""}</button>
			`
			)
			.join("");
		return `<div class="fg-recorridos-tabs">${html}</div>`;
	}

	bind_body_events() {
		this.$body.find(".fg-kpis--recorridos").on("click", ".fg-kpi", (e) => {
			const $b = $(e.currentTarget);
			this.switch_tab($b.data("tab"), { status: $b.data("status") || "" });
		});
		this.$body.find(".fg-recorridos-tabs-nav").on("click", ".fg-recorridos-tab", (e) => {
			this.switch_tab($(e.currentTarget).data("tab"));
		});
	}

	// =====================================================================
	// TAB: Pedidos disponibles (brief section 6/7) -- get_available_orders()
	// =====================================================================
	load_available() {
		return this.call("get_available_orders", {
			txt: this.avail_search,
			start: (this.avail_page - 1) * PAGE_SIZE,
			page_length: PAGE_SIZE,
		}).then((r) => {
			this.avail_rows = r.pick_lists;
			this.avail_total = r.total;
			this._avail_loaded = true;
		});
	}

	refresh_available() {
		return this.load_available().then(() => {
			if (this.active_tab === "disponibles") {
				this.$body.find(".fg-recorridos-tab-body").html(this.render_available_section_html());
				this.bind_available_events();
				this.render_selection_bar();
			}
		});
	}

	render_available_section_html() {
		return `
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Pedidos facturados disponibles")}</div>
			</div>
			<div class="fg-recorridos-toolbar">
				<div class="fg-recorridos-search-wrap">
					${icon("search", "fg-recorridos-search-icon")}
					<input type="text" class="fg-recorridos-search-input" placeholder="${__(
						"Buscar por pedido, Pick List, cliente o dirección..."
					)}" value="${frappe.utils.escape_html(this.avail_search || "")}">
				</div>
			</div>
			<div class="fg-recorridos-avail-cards">${this.render_available_cards_html()}</div>
			<div class="fg-recorridos-pagination" data-scope="avail">${this.render_pagination_html(
				this.avail_page,
				this.avail_total
			)}</div>
		`;
	}

	render_available_cards_html() {
		if (!this.avail_rows.length) {
			return `
				<div class="fg-empty">
					<div class="fg-empty-title">${__("Todo al día")}</div>
					<div>${__("No hay pedidos facturados pendientes de asignar a un recorrido.")}</div>
				</div>
			`;
		}
		return this.avail_rows.map((r) => this.render_available_card(r)).join("");
	}

	// Card fields exactly per brief section 6: Pedido / Pick List / Cliente
	// / Dirección / cantidad de productos / cantidad total / Estado
	// (Facturado -- always true here, a Pick List can only reach this list
	// via fg_invoicing_status=Facturado, see get_available_orders()'s own
	// filter). Never rate/amount/grand_total/account/price -- see this
	// same guarantee enforced server-side by
	// test_no_economic_values_anywhere.
	render_available_card(r) {
		const is_selected = this.selected.has(r.pick_list);
		const pedido_label = r.commercial_name || r.sales_order || r.pick_list;
		const address = address_html(r.address_display);
		return `
			<label class="fg-recorridos-avail-card ${is_selected ? "is-selected" : ""}" data-pick-list="${frappe.utils.escape_html(
			r.pick_list
		)}">
				<input type="checkbox" class="fg-recorridos-avail-checkbox" ${is_selected ? "checked" : ""}>
				<div class="fg-recorridos-avail-card-body">
					<div class="fg-recorridos-avail-card-top">
						<div class="fg-recorridos-avail-card-id">${__("PEDIDO")} #${frappe.utils.escape_html(pedido_label)}</div>
						<span class="fg-badge fg-badge--route-facturado">${icon("check", "fg-icon-sm")} ${__("FACTURADO")}</span>
					</div>
					<div class="fg-recorridos-avail-card-customer">${icon("user", "fg-icon-sm")} ${frappe.utils.escape_html(
			r.customer_name || r.customer || __("Sin cliente")
		)}</div>
					<div class="fg-recorridos-avail-card-address">${icon("map-pin", "fg-icon-sm")} ${address}</div>
					<div class="fg-recorridos-avail-card-meta">
						<span>${icon("package", "fg-icon-sm")} ${r.item_count} ${r.item_count === 1 ? __("producto") : __("productos")}</span>
						<span>${icon("boxes", "fg-icon-sm")} ${format_qty(r.total_qty)} ${__("unidades")}</span>
						<span class="fg-recorridos-avail-card-picklist">${icon("clipboard-list", "fg-icon-sm")} ${frappe.utils.escape_html(
			r.pick_list
		)}</span>
					</div>
				</div>
			</label>
		`;
	}

	render_pagination_html(page, total) {
		if (!total) return "";
		const page_count = Math.max(Math.ceil(total / PAGE_SIZE), 1);
		const start = (page - 1) * PAGE_SIZE + 1;
		const end = Math.min(page * PAGE_SIZE, total);
		return `
			<div class="fg-recorridos-pagination-info">${__("Mostrando {0} a {1} de {2}", [start, end, total])}</div>
			<div class="fg-recorridos-pagination-controls">
				<button type="button" class="fg-recorridos-pagination-btn" data-dir="prev" ${
					page <= 1 ? "disabled" : ""
				}>${icon("chevron-left")}</button>
				<span class="fg-recorridos-pagination-page">${page}</span>
				<button type="button" class="fg-recorridos-pagination-btn" data-dir="next" ${
					page >= page_count ? "disabled" : ""
				}>${icon("chevron-right")}</button>
			</div>
		`;
	}

	bind_available_events() {
		const $t = this.$body.find(".fg-recorridos-tab-body");

		$t.find(".fg-recorridos-search-input").on("input", (e) => {
			const val = $(e.currentTarget).val();
			clearTimeout(this._avail_search_debounce);
			this._avail_search_debounce = setTimeout(() => {
				this.avail_search = val;
				this.avail_page = 1;
				this.refresh_available();
			}, 300);
		});

		$t.find(".fg-recorridos-avail-cards").on("change", ".fg-recorridos-avail-checkbox", (e) => {
			const $card = $(e.currentTarget).closest(".fg-recorridos-avail-card");
			const pick_list = $card.data("pick-list");
			const row = this.avail_rows.find((r) => r.pick_list === pick_list);
			if (e.currentTarget.checked) {
				this.selected.set(pick_list, row);
			} else {
				this.selected.delete(pick_list);
			}
			$card.toggleClass("is-selected", e.currentTarget.checked);
			this.render_selection_bar();
		});

		$t.find('.fg-recorridos-pagination[data-scope="avail"]').on("click", ".fg-recorridos-pagination-btn", (e) => {
			this.avail_page += $(e.currentTarget).data("dir") === "prev" ? -1 : 1;
			this.refresh_available();
		});
	}

	// -- Sticky selection bar (brief section 7) --------------------------
	render_selection_bar() {
		this.$app.find(".fg-recorridos-selection-bar").remove();
		if (!this.selected.size || this.active_tab !== "disponibles") return;
		const $bar = $(`
			<div class="fg-recorridos-selection-bar">
				<span class="fg-recorridos-selection-count">${__("{0} pedidos seleccionados", [this.selected.size])}</span>
				<div class="fg-recorridos-selection-actions">
					<button type="button" class="fg-btn fg-btn--ghost fg-recorridos-clear-btn">${__("LIMPIAR")}</button>
					<button type="button" class="fg-btn fg-btn--solid-primary fg-recorridos-create-btn">${icon(
						"route",
						"fg-icon-sm"
					)} ${__("CREAR RECORRIDO")}</button>
				</div>
			</div>
		`);
		$bar.find(".fg-recorridos-clear-btn").on("click", () => this.clear_selection());
		$bar.find(".fg-recorridos-create-btn").on("click", () => this.open_create_dialog());
		this.$app.append($bar);
	}

	clear_selection() {
		this.selected.clear();
		if (this.active_tab === "disponibles") {
			this.$body.find(".fg-recorridos-avail-cards").find(".fg-recorridos-avail-card").removeClass("is-selected");
			this.$body.find(".fg-recorridos-avail-checkbox").prop("checked", false);
		}
		this.render_selection_bar();
	}

	// =====================================================================
	// Modal: CREAR RECORRIDO (brief section 8/9/10; Commit 24.2's own
	// visual redesign against design_references/recorridos_crear_v2.png --
	// presentation only, create_route() itself untouched below.)
	// =====================================================================
	open_create_dialog() {
		if (!this.selected.size) return;
		this._create_order = Array.from(this.selected.keys());

		const dialog = new frappe.ui.Dialog({
			title: `
				<div class="fg-route-dialog-title">
					<div class="fg-route-dialog-title-icon fg-route-dialog-title-icon--blue">${icon("route")}</div>
					<div class="fg-route-dialog-title-text">
						<div class="fg-route-dialog-title-main">${__("Crear recorrido")}</div>
						<div class="fg-route-dialog-title-sub">${__(
							"Organiza tu ruta de entrega seleccionando los pedidos en el orden en que deseas visitarlos."
						)}</div>
					</div>
				</div>
			`,
			size: "extra-large",
			fields: [
				{
					fieldtype: "Date",
					fieldname: "route_date",
					label: __("Fecha del recorrido"),
					default: frappe.datetime.get_today(),
					reqd: 1,
				},
				{ fieldtype: "Column Break" },
				{
					fieldtype: "Link",
					fieldname: "driver",
					label: __("Conductor"),
					options: "Driver",
					placeholder: __("Selecciona un conductor"),
				},
				{ fieldtype: "Column Break" },
				{
					fieldtype: "Link",
					fieldname: "vehicle",
					label: __("Vehículo"),
					options: "Vehicle",
					placeholder: __("Selecciona un vehículo"),
				},
				{ fieldtype: "Section Break" },
				{
					fieldtype: "Small Text",
					fieldname: "start_address",
					label: __("Punto de salida"),
					placeholder: __("Ej: Bodega principal, Cra 15 # 20-30, Bucaramanga"),
					description: __("Opcional. Punto de inicio del recorrido."),
				},
				{ fieldtype: "Column Break" },
				{
					fieldtype: "Small Text",
					fieldname: "notes",
					label: __("Notas (opcional)"),
					placeholder: __("Notas adicionales sobre el recorrido..."),
					description: __("Información adicional para el conductor."),
				},
				{ fieldtype: "Section Break" },
				{ fieldtype: "HTML", fieldname: "stops_html" },
			],
			primary_action_label: `${icon("send", "fg-icon-sm")} ${__("Crear recorrido")}`,
			primary_action: () => this.submit_create_route(dialog),
			secondary_action_label: __("Cancelar"),
			secondary_action: () => dialog.hide(),
		});
		dialog.$wrapper.addClass("fg-route-dialog fg-recorridos-create-dialog");
		// modal-dialog-scrollable is Bootstrap 4's OWN opt-in for exactly
		// this shape (fixed header/footer, internally-scrolling body) --
		// see this class's own big comment further down for why adding it
		// (not a custom scrollTop(0) timing hack) is what actually fixes
		// the "opens scrolled past Fecha/Conductor/Vehículo" bug: without
		// it, Bootstrap's own modal.js _showElement() resets scrollTop on
		// the OUTER .modal element (the wrong one, in the `else` branch of
		// its own `if ($(dialog).hasClass('modal-dialog-scrollable') &&
		// modalBody) { modalBody.scrollTop = 0 } else { this._element.
		// scrollTop = 0 }`) -- with it, Bootstrap resets .modal-body's own
		// scrollTop to 0 itself, natively, on every show(), which is both
		// correct and needs no JS of ours at all.
		dialog.$wrapper.find(".modal-dialog").addClass("modal-dialog-scrollable");
		dialog.custom_actions.html(`
			<div class="fg-route-callout">
				${icon("info", "fg-icon-sm")}
				<span>${__("Usa las flechas ↑ ↓ para cambiar el orden de la ruta.")}</span>
			</div>
		`);
		this.decorate_route_dialog_fields(dialog, ["driver", "vehicle", "start_address", "notes"]);
		this._create_dialog = dialog;
		this.render_create_stops(dialog);
		dialog.show();
	}

	// Decorative left-icon overlay -- Conductor/Vehículo (Link) and Punto
	// de salida/Notas (Small Text). The control itself (label, input/
	// textarea, placeholder, description, value, validation, Link
	// autocomplete/Advanced Search) is completely untouched; this only
	// positions a pointer-events:none icon over the field's own
	// position:relative wrapper, same technique the search inputs
	// elsewhere on this page already use. Never applied to the Date field
	// -- it keeps Frappe's own native calendar affordance as-is.
	decorate_route_dialog_fields(dialog, fieldnames) {
		const icons = { driver: "user", vehicle: "truck", start_address: "map-pin", notes: "sticky-note" };
		fieldnames.forEach((fieldname) => {
			const field = dialog.fields_dict[fieldname];
			if (!field || !field.$wrapper) return;
			const $control = field.$wrapper.find(".control-input, textarea, input").first();
			if (!$control.length || field.$wrapper.find(".fg-route-field-icon").length) return;
			field.$wrapper.find(".control-input-wrapper, .control-input").first().css("position", "relative");
			$(`<span class="fg-route-field-icon">${icon(icons[fieldname] || "info", "fg-icon-sm")}</span>`).insertBefore($control);
		});
	}

	render_create_stops(dialog) {
		const count = this._create_order.length;
		const rows = this._create_order
			.map((pick_list, idx) => {
				const r = this.selected.get(pick_list) || {};
				const pedido_label = r.commercial_name || r.sales_order || pick_list;
				const address = address_html(r.address_display);
				return `
					<div class="fg-route-card" data-pick-list="${frappe.utils.escape_html(pick_list)}">
						<div class="fg-route-card-handle">${icon("grip-vertical")}</div>
						<div class="fg-route-card-num">${idx + 1}</div>
						<div class="fg-route-card-avatar">${icon("shopping-bag")}</div>
						<div class="fg-route-card-info">
							<div class="fg-route-card-title-row">
								<span class="fg-route-card-name">${frappe.utils.escape_html(r.customer_name || r.customer || __("Sin cliente"))}</span>
								<span class="fg-badge fg-badge--route-pedido">${frappe.utils.escape_html(pedido_label)}</span>
							</div>
							<div class="fg-route-card-address">${icon("map-pin", "fg-icon-sm")} ${address}</div>
							<div class="fg-route-card-tags">
								<span class="fg-badge fg-badge--route-products">${r.item_count ?? 0} ${__("productos")}</span>
								<span class="fg-badge fg-badge--route-units">${format_qty(r.total_qty)} ${__("unidades")}</span>
							</div>
						</div>
						<div class="fg-route-card-actions">
							<button type="button" class="fg-route-sqbtn fg-route-sqbtn--up" data-action="up" ${
								idx === 0 ? "disabled" : ""
							} title="${__("Subir")}">${icon("chevron-up")}</button>
							<button type="button" class="fg-route-sqbtn" data-action="down" ${
								idx === count - 1 ? "disabled" : ""
							} title="${__("Bajar")}">${icon("chevron-down")}</button>
							<button type="button" class="fg-route-sqbtn fg-route-sqbtn--danger" data-action="remove" title="${__(
								"Quitar"
							)}">${icon("trash-2")}</button>
						</div>
					</div>
				`;
			})
			.join("");

		const $html = dialog.fields_dict.stops_html.$wrapper;
		$html.html(`
			<div class="fg-route-section-head">
				<div class="fg-route-section-head-main">
					${icon("route", "fg-icon-sm")}
					<span class="fg-route-section-title">${__("Pedidos seleccionados")}</span>
					<span class="fg-badge fg-badge--route-count">${count}</span>
				</div>
				<span class="fg-route-section-sub">${__("Ordena los pedidos según la ruta que deseas seguir")}</span>
			</div>
			<div class="fg-route-cards-list">${rows || `<div class="fg-empty">${__("No hay pedidos seleccionados.")}</div>`}</div>
		`);

		$html.find('[data-action="up"]').on("click", (e) => this.move_create_stop(dialog, $(e.currentTarget).closest(".fg-route-card").data("pick-list"), -1));
		$html.find('[data-action="down"]').on("click", (e) => this.move_create_stop(dialog, $(e.currentTarget).closest(".fg-route-card").data("pick-list"), 1));
		$html.find('[data-action="remove"]').on("click", (e) => {
			const pick_list = $(e.currentTarget).closest(".fg-route-card").data("pick-list");
			this._create_order = this._create_order.filter((p) => p !== pick_list);
			this.selected.delete(pick_list);
			this.render_create_stops(dialog);
			this.render_selection_bar();
			if (this.active_tab === "disponibles") {
				const $card = this.$body.find(`.fg-recorridos-avail-card[data-pick-list="${$.escapeSelector(pick_list)}"]`);
				$card.removeClass("is-selected").find(".fg-recorridos-avail-checkbox").prop("checked", false);
			}
			if (!this._create_order.length) dialog.hide();
		});
	}

	move_create_stop(dialog, pick_list, direction) {
		const idx = this._create_order.indexOf(pick_list);
		const swap_idx = idx + direction;
		if (idx < 0 || swap_idx < 0 || swap_idx >= this._create_order.length) return;
		[this._create_order[idx], this._create_order[swap_idx]] = [this._create_order[swap_idx], this._create_order[idx]];
		this.render_create_stops(dialog);
	}

	submit_create_route(dialog) {
		if (!this._create_order.length || dialog.$wrapper.hasClass("fg-route-dialog-busy")) return;
		const values = dialog.get_values(true) || {};
		dialog.$wrapper.addClass("fg-route-dialog-busy");
		dialog.disable_primary_action();
		// Swap the button's own label/content in place -- same real
		// .btn-modal-primary element, same primary_action, never a second
		// button/handler. disable_primary_action() already adds Frappe's
		// own .disabled class (pointer-events:none); .fg-route-dialog-busy
		// on the wrapper is this method's own re-entrancy guard above.
		dialog.get_primary_btn().html(`<span class="fg-route-btn-spinner"></span> ${__("Creando recorrido...")}`);
		this.set_busy(true);

		this.call_route_write("create_route", {
			route_date: values.route_date,
			pick_lists: this._create_order,
			driver: values.driver || null,
			vehicle: values.vehicle || null,
			start_address: values.start_address || null,
			notes: values.notes || null,
		})
			.then((route) => {
				frappe.show_alert({ message: "✓ " + __("Recorrido {0} creado correctamente.", [route.name]), indicator: "green" }, 5);
				this.clear_selection();
				dialog.hide();
				this.switch_tab("recorridos", { status: "" });
			})
			.catch(() => {
				// Either frappe.call's own default error dialog (an unexpected
				// failure) or call_route_write()'s own friendlier alert for the
				// one known double-assignment race already ran -- refresh the
				// available-orders list either way so a now-stale row (someone
				// else claimed it) never lingers on screen.
				this.refresh_available();
			})
			.finally(() => {
				dialog.$wrapper.removeClass("fg-route-dialog-busy");
				dialog.enable_primary_action();
				// Restores the button's own real label -- irrelevant on the
				// success path (dialog.hide() already ran above) but
				// required on failure, where the dialog stays open and the
				// user must be able to try again.
				dialog.get_primary_btn().html(`${icon("send", "fg-icon-sm")} ${__("Crear recorrido")}`);
				this.set_busy(false);
			});
	}

	// =====================================================================
	// TAB: Recorridos (Borrador/Planificado/En Ruta) -- get_routes()
	// =====================================================================
	load_routes() {
		return this.call("get_routes", {
			status: this.routes_status_filter ? [this.routes_status_filter] : ["Borrador", "Planificado", "En Ruta"],
			start: (this.routes_page - 1) * PAGE_SIZE,
			page_length: PAGE_SIZE,
			txt: this.routes_search,
		}).then((r) => {
			this.routes_rows = r.routes;
			this.routes_total = r.total;
			this._routes_loaded = true;
		});
	}

	refresh_routes() {
		return this.load_routes().then(() => {
			if (this.active_tab === "recorridos") {
				this.$body.find(".fg-recorridos-tab-body").html(this.render_routes_section_html());
				this.bind_routes_events();
			}
		});
	}

	render_routes_section_html() {
		const filters = [
			{ key: "", label: __("Todos") },
			{ key: "Borrador", label: __("Borrador") },
			{ key: "Planificado", label: __("Planificado") },
			{ key: "En Ruta", label: __("En Ruta") },
		];
		const filters_html = filters
			.map(
				(f) => `
				<button type="button" class="fg-recorridos-filter-chip ${this.routes_status_filter === f.key ? "is-active" : ""}" data-status="${
					f.key
				}">${f.label}</button>
			`
			)
			.join("");
		return `
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Recorridos activos")}</div>
			</div>
			${render_search_bar_html(this.routes_search)}
			<div class="fg-recorridos-filter-chips">${filters_html}</div>
			<div class="fg-recorridos-route-cards">${this.render_route_cards_html(this.routes_rows, false)}</div>
			<div class="fg-recorridos-pagination" data-scope="routes">${this.render_pagination_html(
				this.routes_page,
				this.routes_total
			)}</div>
		`;
	}

	render_route_cards_html(rows, is_history) {
		if (!rows.length) {
			// Commit 25.20, section 11 -- a search that matched nothing gets
			// its own explicit message, distinct from "genuinely nothing
			// here yet" (the search is server-side, so `rows` being empty
			// while a query is active unambiguously means "no matches").
			const search = is_history ? this.hist_search : this.routes_search;
			if (search) return render_search_empty_html();
			return `
				<div class="fg-empty">
					<div>${is_history ? __("Aún no hay recorridos completados o cancelados.") : __("No hay recorridos activos.")}</div>
				</div>
			`;
		}
		return rows.map((r) => this.render_route_card(r, is_history)).join("");
	}

	// Card fields per brief section 12/18: name, estado, fecha, conductor,
	// vehículo, cantidad de paradas, acciones según estado.
	render_route_card(r, is_history) {
		const date_label = r.route_date ? frappe.datetime.str_to_user(r.route_date) : "—";
		let actions = `<button type="button" class="fg-btn fg-btn--ghost fg-recorridos-view-btn">${icon(
			"eye",
			"fg-icon-sm"
		)} ${__("VER RECORRIDO")}</button>`;
		if (!is_history && r.status === "Borrador") {
			actions += `<button type="button" class="fg-btn fg-btn--solid-primary fg-recorridos-plan-btn">${icon(
				"calendar-check",
				"fg-icon-sm"
			)} ${__("PLANIFICAR")}</button>`;
		}
		if (!is_history && r.status === "En Ruta") {
			actions += `<button type="button" class="fg-btn fg-btn--solid-primary fg-recorridos-continue-btn">${icon(
				"navigation",
				"fg-icon-sm"
			)} ${__("CONTINUAR")}</button>`;
		}
		return `
			<div class="fg-recorridos-route-card" data-name="${frappe.utils.escape_html(r.name)}" data-creation="${frappe.utils.escape_html(
			r.creation || ""
		)}">
				<div class="fg-recorridos-route-card-top">
					<div class="fg-recorridos-route-card-id">${frappe.utils.escape_html(r.name)}</div>
					${status_badge_html(r.status)}
				</div>
				<div class="fg-recorridos-route-card-grid">
					<div><span class="fg-recorridos-route-card-label">${__("Fecha")}</span><span>${date_label}</span></div>
					<div><span class="fg-recorridos-route-card-label">${__("Conductor")}</span><span>${
			r.driver_name ? frappe.utils.escape_html(r.driver_name) : "—"
		}</span></div>
					<div><span class="fg-recorridos-route-card-label">${__("Vehículo")}</span><span>${
			r.vehicle ? frappe.utils.escape_html(r.vehicle) : "—"
		}</span></div>
					<div><span class="fg-recorridos-route-card-label">${__("Pedidos")}</span><span>${r.stop_count}</span></div>
				</div>
				<div class="fg-recorridos-route-card-actions">${actions}</div>
			</div>
		`;
	}

	bind_routes_events() {
		const $t = this.$body.find(".fg-recorridos-tab-body");
		// Commit 25.20 -- server-side, debounced 300ms, same idiom as this
		// file's own pre-existing avail_search (bind_available_events()
		// above) -- full section re-render on each debounced fetch, same
		// established tradeoff that pattern already accepts.
		$t.find(".fg-search-input").on("input", (e) => {
			const val = $(e.currentTarget).val();
			clearTimeout(this._routes_search_debounce);
			this._routes_search_debounce = setTimeout(() => {
				this.routes_search = val;
				this.routes_page = 1;
				this.refresh_routes();
			}, 300);
		});
		$t.find(".fg-search-clear").on("click", () => {
			this.routes_search = "";
			this.routes_page = 1;
			this.refresh_routes();
		});
		$t.find(".fg-recorridos-filter-chips").on("click", ".fg-recorridos-filter-chip", (e) => {
			this.routes_status_filter = $(e.currentTarget).data("status") || "";
			this.routes_page = 1;
			this.refresh_routes();
		});
		$t.find(".fg-recorridos-route-cards").on("click", ".fg-recorridos-view-btn", (e) => {
			const $card = $(e.currentTarget).closest(".fg-recorridos-route-card");
			this.open_detail_dialog($card.data("name"), $card.data("creation"));
		});
		$t.find(".fg-recorridos-route-cards").on("click", ".fg-recorridos-plan-btn", (e) => {
			e.stopPropagation();
			this.confirm_plan_route($(e.currentTarget).closest(".fg-recorridos-route-card").data("name"));
		});
		$t.find(".fg-recorridos-route-cards").on("click", ".fg-recorridos-continue-btn", (e) => {
			e.stopPropagation();
			this.open_active_route($(e.currentTarget).closest(".fg-recorridos-route-card").data("name"));
		});
		$t.find('.fg-recorridos-pagination[data-scope="routes"]').on("click", ".fg-recorridos-pagination-btn", (e) => {
			this.routes_page += $(e.currentTarget).data("dir") === "prev" ? -1 : 1;
			this.refresh_routes();
		});
	}

	// =====================================================================
	// TAB: Historial (Completado/Cancelado) -- get_routes(), read-only
	// =====================================================================
	load_history() {
		return this.call("get_routes", {
			status: this.hist_status_filter ? [this.hist_status_filter] : ["Completado", "Cancelado"],
			start: (this.hist_page - 1) * PAGE_SIZE,
			page_length: PAGE_SIZE,
			txt: this.hist_search,
		}).then((r) => {
			this.hist_rows = r.routes;
			this.hist_total = r.total;
			this._hist_loaded = true;
		});
	}

	refresh_history() {
		return this.load_history().then(() => {
			if (this.active_tab === "historial") {
				this.$body.find(".fg-recorridos-tab-body").html(this.render_history_section_html());
				this.bind_history_events();
			}
		});
	}

	render_history_section_html() {
		const filters = [
			{ key: "", label: __("Todos") },
			{ key: "Completado", label: __("Completado") },
			{ key: "Cancelado", label: __("Cancelado") },
		];
		const filters_html = filters
			.map(
				(f) => `
				<button type="button" class="fg-recorridos-filter-chip ${this.hist_status_filter === f.key ? "is-active" : ""}" data-status="${
					f.key
				}">${f.label}</button>
			`
			)
			.join("");
		return `
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Historial de recorridos")}</div>
			</div>
			${render_search_bar_html(this.hist_search)}
			<div class="fg-recorridos-filter-chips">${filters_html}</div>
			<div class="fg-recorridos-route-cards">${this.render_route_cards_html(this.hist_rows, true)}</div>
			<div class="fg-recorridos-pagination" data-scope="hist">${this.render_pagination_html(this.hist_page, this.hist_total)}</div>
		`;
	}

	bind_history_events() {
		const $t = this.$body.find(".fg-recorridos-tab-body");
		$t.find(".fg-search-input").on("input", (e) => {
			const val = $(e.currentTarget).val();
			clearTimeout(this._hist_search_debounce);
			this._hist_search_debounce = setTimeout(() => {
				this.hist_search = val;
				this.hist_page = 1;
				this.refresh_history();
			}, 300);
		});
		$t.find(".fg-search-clear").on("click", () => {
			this.hist_search = "";
			this.hist_page = 1;
			this.refresh_history();
		});
		$t.find(".fg-recorridos-filter-chips").on("click", ".fg-recorridos-filter-chip", (e) => {
			this.hist_status_filter = $(e.currentTarget).data("status") || "";
			this.hist_page = 1;
			this.refresh_history();
		});
		$t.find(".fg-recorridos-route-cards").on("click", ".fg-recorridos-view-btn", (e) => {
			const $card = $(e.currentTarget).closest(".fg-recorridos-route-card");
			this.open_detail_dialog($card.data("name"), $card.data("creation"));
		});
		$t.find('.fg-recorridos-pagination[data-scope="hist"]').on("click", ".fg-recorridos-pagination-btn", (e) => {
			this.hist_page += $(e.currentTarget).data("dir") === "prev" ? -1 : 1;
			this.refresh_history();
		});
	}

	confirm_plan_route(route_name) {
		frappe.confirm(__("Una vez planificado no podrás modificar las paradas desde esta pantalla."), () => {
			this.set_busy(true);
			this.call("plan_route", { route_name: route_name })
				.then(() => {
					frappe.show_alert({ message: "✓ " + __("Recorrido planificado correctamente."), indicator: "green" }, 5);
					return this.refresh_routes();
				})
				.finally(() => this.set_busy(false));
		});
	}

	// =====================================================================
	// Modal: Detalle de Recorrido (brief section 13/14/15/16/17)
	// =====================================================================
	open_detail_dialog(route_name, creation) {
		if (!route_name) return;
		this._detail_creation_hint = creation || null;
		this._detail_dialog = new frappe.ui.Dialog({
			title: frappe.utils.escape_html(route_name),
			size: "extra-large",
			fields: [{ fieldtype: "HTML", fieldname: "detail_html" }],
			secondary_action_label: __("Cerrar"),
			secondary_action: () => this._detail_dialog.hide(),
		});
		this._detail_dialog.$wrapper.addClass("fg-route-dialog fg-recorridos-detail-dialog");
		// See open_create_dialog()'s own comment for why this class (not a
		// scrollTop(0) call) is the real, Bootstrap-native fix for the
		// modal opening scrolled past its own top. A manual reset is
		// STILL needed below too, unlike the create dialog: this body's
		// real content loads asynchronously (get_route_detail()), well
		// after Bootstrap's own show()-time reset already ran against a
		// body that, at that point, only had the short "Cargando..."
		// placeholder in it.
		this._detail_dialog.$wrapper.find(".modal-dialog").addClass("modal-dialog-scrollable");
		this._detail_dialog.fields_dict.detail_html.$wrapper.html(`<div class="fg-recorridos-detail-loading">${__("Cargando...")}</div>`);
		this._detail_dialog.show();
		// scrollTop reset only after this FIRST load (not on every later
		// reload_detail() a reorder/quitar/agregar/planificar triggers --
		// those must never snap the view back to the top mid-interaction).
		this.reload_detail(route_name).then(() => {
			if (this._detail_dialog) this._detail_dialog.$wrapper.find(".modal-body").scrollTop(0);
		});
	}

	reload_detail(route_name) {
		return this.call("get_route_detail", { route_name: route_name }).then((detail) => {
			if (!detail.creation && this._detail_creation_hint) detail.creation = this._detail_creation_hint;
			this.detail = detail;
			if (this._detail_dialog && this._detail_dialog.is_visible) this.render_detail_body();
		});
	}

	// Status -> short, human subtitle shown right under the route name in
	// the detail dialog header (brief section 13's own "Recorrido en
	// preparación" for Borrador, adapted per status so every state reads
	// naturally, not just Borrador).
	_detail_status_subtitle(status) {
		const map = {
			Borrador: __("Recorrido en preparación"),
			Planificado: __("Recorrido planificado"),
			"En Ruta": __("Recorrido en curso"),
			Completado: __("Recorrido completado"),
			Cancelado: __("Recorrido cancelado"),
		};
		return map[status] || "";
	}

	build_detail_title_html(d) {
		const created = d.creation
			? `<div class="fg-route-dialog-title-meta">${icon("calendar", "fg-icon-sm")} ${__("Creado el {0}", [
					frappe.datetime.str_to_user(d.creation),
			  ])}</div>`
			: "";
		return `
			<div class="fg-route-dialog-title">
				<div class="fg-route-dialog-title-icon fg-route-dialog-title-icon--violet">${icon("route")}</div>
				<div class="fg-route-dialog-title-text">
					<div class="fg-route-dialog-title-main">
						<span>${frappe.utils.escape_html(d.name)}</span>
						${status_badge_html(d.status)}
					</div>
					<div class="fg-route-dialog-title-sub">${this._detail_status_subtitle(d.status)}</div>
					${created}
				</div>
			</div>
		`;
	}

	render_detail_body() {
		const d = this.detail;
		const is_borrador = d.status === "Borrador";
		const is_planificado = d.status === "Planificado";
		const dialog = this._detail_dialog;
		const $html = dialog.fields_dict.detail_html.$wrapper;

		dialog.set_title(this.build_detail_title_html(d));

		const summary_cards = [
			{
				i: "calendar",
				mod: "blue",
				label: __("FECHA"),
				value: d.route_date ? frappe.datetime.str_to_user(d.route_date, false, true) : "—",
				sub: d.route_date ? weekday_label(d.route_date) : "",
			},
			{ i: "user", mod: "green", label: __("CONDUCTOR"), value: d.driver_name ? frappe.utils.escape_html(d.driver_name) : "" },
			{ i: "car", mod: "blue", label: __("VEHÍCULO"), value: d.vehicle ? frappe.utils.escape_html(d.vehicle) : "" },
			{ i: "map-pin", mod: "orange", label: __("PUNTO DE SALIDA"), value: d.start_address ? frappe.utils.escape_html(d.start_address) : "" },
		];
		const summary_html = summary_cards
			.map(
				(c) => `
				<div class="fg-route-summary-card">
					<div class="fg-route-summary-card-icon fg-route-summary-card-icon--${c.mod}">${icon(c.i)}</div>
					<div class="fg-route-summary-card-label">${c.label}</div>
					<div class="fg-route-summary-card-value ${c.value ? "" : "is-muted"}">${c.value || __("Sin asignar")}</div>
					${c.sub ? `<div class="fg-route-summary-card-sub">${c.sub}</div>` : ""}
				</div>
			`
			)
			.join("");
		const estado_card = `
			<div class="fg-route-summary-card">
				<div class="fg-route-summary-card-icon fg-route-summary-card-icon--violet">${icon("badge-check")}</div>
				<div class="fg-route-summary-card-label">${__("ESTADO")}</div>
				<div class="fg-route-summary-card-value">${status_badge_html(d.status)}</div>
			</div>
		`;

		const notes_html = d.notes
			? `
				<div class="fg-route-notes-card">
					<div class="fg-route-notes-icon">${icon("sticky-note")}</div>
					<div>
						<div class="fg-route-summary-card-label">${__("NOTAS")}</div>
						<div class="fg-route-notes-text">${frappe.utils.escape_html(d.notes)}</div>
					</div>
				</div>
			`
			: "";

		let total_items = 0;
		let total_qty = 0;
		d.stops.forEach((s) => {
			total_items += cint(s.item_count);
			total_qty += flt(s.total_qty);
		});

		// Commit 24.3 -- geographic readiness, computed straight from
		// get_route_detail()'s own stops (already carries geolocation_
		// status/latitude/longitude per stop) -- no extra API round-trip
		// needed just to render this card.
		const geo_total = d.stops.length;
		const geo_ready_count = d.stops.filter((s) => s.geolocation_status === "Geolocalizado").length;
		const geo_pending_count = geo_total - geo_ready_count;
		const geo_ready_for_routing = geo_total > 0 && geo_pending_count === 0;

		// Turn-4 security audit -- set_address_geolocation() only ever
		// succeeds for a role that already owns Address write natively
		// (Gestión de Clientes/System Manager); Recorrido is a CONSUMER of
		// geolocation, never an ADMINISTRATOR of it. This is UX only (the
		// server-side frappe.has_permission("Address", "write") check is
		// the real boundary either way) -- it just avoids showing a
		// Recorrido user a button that would always fail.
		const can_administer_geolocation = frappe.user.has_role(["Gestión de Clientes", "System Manager"]);

		const stops_html = d.stops.length
			? d.stops
					.map((s, idx) => {
						const pedido_label = s.commercial_name || s.sales_order || s.pick_list;
						const address = address_html(s.address_display);
						const edit_actions = is_borrador
							? `
							<div class="fg-route-card-actions">
								<button type="button" class="fg-route-sqbtn fg-route-sqbtn--up" data-action="up" data-name="${s.name}" ${
									idx === 0 ? "disabled" : ""
								} title="${__("Subir")}">${icon("chevron-up")}</button>
								<button type="button" class="fg-route-sqbtn" data-action="down" data-name="${s.name}" ${
									idx === d.stops.length - 1 ? "disabled" : ""
								} title="${__("Bajar")}">${icon("chevron-down")}</button>
								<button type="button" class="fg-route-sqbtn fg-route-sqbtn--danger" data-action="remove" data-name="${
									s.name
								}" title="${__("Quitar")}">${icon("trash-2")}</button>
							</div>
						`
							: "";
						const is_geo_ready = s.geolocation_status === "Geolocalizado";
						const geo_badge = is_geo_ready
							? `<span class="fg-route-geo-badge fg-route-geo-badge--ready">● ${__("UBICACIÓN LISTA")}</span>`
							: `<span class="fg-route-geo-badge fg-route-geo-badge--pending">● ${__("UBICACIÓN PENDIENTE")}</span>`;
						const geo_coords =
							is_geo_ready && s.latitude && s.longitude
								? `<div class="fg-route-geo-coords">${flt(s.latitude).toFixed(5)}, ${flt(s.longitude).toFixed(5)}</div>`
								: "";
						// Commit 24.4 -- "OBTENER UBICACIÓN" (automatic, Google) sits
						// NEXT TO "Configurar ubicación" (manual, Commit 24.3),
						// never replacing it: brief section 17's own manual
						// confirm/correct flow via set_address_geolocation() stays
						// exactly as it was. Same can_administer_geolocation gate
						// as the manual button -- Recorrido only ever sees the
						// plain "Ubicación pendiente" text, brief section 16's
						// own "Rol Recorrido: solo visualiza estado".
						// Fase 26.2 -- also offered while Planificado:
						// refresh_route_geolocation() now accepts Planificado so
						// a missing location can be fixed before INICIAR
						// RECORRIDO (start_route() requires one on every stop).
						const geo_configure_btn =
							(is_borrador || is_planificado) && !is_geo_ready && s.customer_address
								? can_administer_geolocation
									? `
									<button type="button" class="fg-route-geo-auto-btn" data-action="auto-geocode" data-name="${s.name}">${icon(
											"sparkles",
											"fg-icon-sm"
									  )} ${__("Obtener ubicación")}</button>
									<button type="button" class="fg-route-geo-configure-btn" data-action="configure-location" data-name="${s.name}">${icon(
											"map-pin",
											"fg-icon-sm"
									  )} ${__("Configurar ubicación")}</button>
								`
									: `<span class="fg-route-geo-pending-text">${__("Ubicación pendiente")}</span>`
								: "";
						return `
						<div class="fg-route-card fg-route-card--white" data-name="${s.name}">
							<div class="fg-route-card-handle">${icon("grip-vertical")}</div>
							<div class="fg-route-card-num fg-route-card-num--violet">${idx + 1}</div>
							<div class="fg-route-card-avatar ${idx % 2 === 0 ? "fg-route-card-avatar--violet" : "fg-route-card-avatar--green"}">${icon(
							"store"
						)}</div>
							<div class="fg-route-card-info">
								<div class="fg-route-card-title-row">
									<span class="fg-route-card-name">${frappe.utils.escape_html(s.customer_name || s.customer || __("Sin cliente"))}</span>
								</div>
								<div class="fg-route-card-codes">
									<span class="fg-badge fg-badge--route-pedido">${__("PEDIDO")} #${frappe.utils.escape_html(pedido_label)}</span>
									<span class="fg-badge fg-badge--route-picklist">${frappe.utils.escape_html(s.pick_list)}</span>
								</div>
								<div class="fg-route-card-address">${icon("map-pin", "fg-icon-sm")} ${address}</div>
								<div class="fg-route-card-tags">
									<span class="fg-badge fg-badge--route-products">${s.item_count} ${s.item_count === 1 ? __("producto") : __("productos")}</span>
									<span class="fg-badge fg-badge--route-units">${format_qty(s.total_qty)} ${__("unidades")}</span>
									${parada_status_badge_html(s.status)}
								</div>
								<div class="fg-route-geo-row">
									${geo_badge}
									${geo_coords}
									${geo_configure_btn}
								</div>
							</div>
							${edit_actions}
						</div>
					`;
					})
					.join("")
			: `<div class="fg-empty">${__("Este recorrido no tiene paradas.")}</div>`;

		const add_btn = is_borrador
			? `<button type="button" class="fg-route-add-btn fg-recorridos-add-stops-btn">${icon("plus", "fg-icon-sm")} ${__(
					"Agregar pedidos"
			  )}</button>`
			: "";

		const callout = is_borrador
			? `
				<div class="fg-route-callout">
					${icon("info", "fg-icon-sm")}
					<span>${__("Usa las flechas para cambiar el orden de las paradas.")}</span>
				</div>
			`
			: "";

		$html.html(`
			<div class="fg-route-summary-grid">${summary_html}${estado_card}</div>
			${notes_html}
			<div class="fg-route-detail-columns">
				<div class="fg-route-detail-main">
					<div class="fg-route-section-head">
						<div class="fg-route-section-head-main">
							<span class="fg-route-section-title">${__("Paradas")} (${d.total_stops})</span>
						</div>
						${add_btn}
					</div>
					<div class="fg-route-section-sub fg-route-section-sub--block">${__("Ordena las paradas según el recorrido que deseas seguir.")}</div>
					<div class="fg-route-cards-list">${stops_html}</div>
					${callout}
				</div>
				<div class="fg-route-detail-aside">
					<div class="fg-route-aside-card fg-route-aside-card--blue">
						<div class="fg-route-aside-card-title">${icon("flag", "fg-icon-sm")} ${__("Resumen del recorrido")}</div>
						<div class="fg-route-aside-row"><span>${__("Total paradas")}</span><strong>${d.total_stops}</strong></div>
						<div class="fg-route-aside-row"><span>${__("Total productos")}</span><strong>${total_items}</strong></div>
						<div class="fg-route-aside-row"><span>${__("Total unidades")}</span><strong>${format_qty(total_qty)}</strong></div>
					</div>
					<div class="fg-route-aside-card fg-route-aside-card--geo">
						<div class="fg-route-aside-card-title">${icon("map-pin", "fg-icon-sm")} ${__("Preparación de ruta")}</div>
						<div class="fg-route-geo-progress">${geo_ready_count} / ${geo_total} ${__("ubicaciones listas")}</div>
						${
							geo_ready_for_routing
								? `<div class="fg-route-geo-status fg-route-geo-status--ok">
										<div class="fg-route-geo-status-title">${icon("circle-check", "fg-icon-sm")} ${__("Ruta preparada")}</div>
										<div class="fg-route-geo-status-sub">${__("Todas las paradas tienen una ubicación válida.")}</div>
									</div>`
								: `<div class="fg-route-geo-status fg-route-geo-status--warn">
										<div class="fg-route-geo-status-title">${icon("triangle-alert", "fg-icon-sm")} ${__("{0} ubicaciones pendientes", [geo_pending_count])}</div>
										<div class="fg-route-geo-status-sub">${__("Completa las ubicaciones antes de calcular la ruta.")}</div>
										${
											is_borrador && can_administer_geolocation
												? `<button type="button" class="fg-route-geo-batch-btn" data-action="auto-geocode-pending">${icon(
														"sparkles",
														"fg-icon-sm"
												  )} ${__("Obtener ubicaciones pendientes")}</button>`
												: ""
										}
									</div>`
						}
					</div>
					<div class="fg-route-aside-card fg-route-aside-card--green">
						<div class="fg-route-aside-card-title">${icon("lightbulb", "fg-icon-sm")} ${__("Consejos")}</div>
						<div class="fg-route-tip">${icon("circle-check", "fg-icon-sm")} ${__("Puedes reordenar las paradas usando las flechas.")}</div>
						<div class="fg-route-tip">${icon("circle-check", "fg-icon-sm")} ${__("Agrega más pedidos si necesitas ampliar el recorrido.")}</div>
						<div class="fg-route-tip">${icon("circle-check", "fg-icon-sm")} ${__("Planifica el recorrido cuando esté listo.")}</div>
					</div>
				</div>
			</div>
		`);

		dialog.custom_actions.empty();
		if (is_borrador || is_planificado) {
			dialog.add_custom_action(
				`${icon("trash-2", "fg-icon-sm")} ${__("Cancelar recorrido")}`,
				() => this.confirm_cancel_route_from_detail(),
				"fg-route-btn-cancel"
			);
		}
		// Fase 26.2 -- the primary action per status. INICIAR/CONTINUAR get
		// .fg-route-btn-start (a solid, taller CTA) so they read as far more
		// important than "Cancelar recorrido"; removed again for any other
		// status since this same footer button is reused across re-renders.
		const $primary = dialog.get_primary_btn();
		$primary.removeClass("hide fg-route-btn-start");
		if (is_borrador) {
			dialog.set_primary_action(`${icon("calendar-check", "fg-icon-sm")} ${__("Planificar recorrido")}`, () =>
				this.confirm_plan_route_from_detail()
			);
		} else if (is_planificado) {
			dialog.set_primary_action(`${icon("play", "fg-icon-sm")} ${__("INICIAR RECORRIDO")}`, () =>
				this.confirm_start_route_from_detail()
			);
			$primary.addClass("fg-route-btn-start");
		} else if (d.status === "En Ruta") {
			dialog.set_primary_action(`${icon("navigation", "fg-icon-sm")} ${__("CONTINUAR RECORRIDO")}`, () => {
				const detail = this.detail;
				dialog.hide();
				this.enter_active_route(detail);
			});
			$primary.addClass("fg-route-btn-start");
		} else {
			$primary.addClass("hide");
		}

		this.bind_detail_events($html);
	}

	bind_detail_events($html) {
		$html.find('[data-action="up"]').on("click", (e) => this.move_detail_stop($(e.currentTarget).data("name"), -1));
		$html.find('[data-action="down"]').on("click", (e) => this.move_detail_stop($(e.currentTarget).data("name"), 1));
		$html.find('[data-action="remove"]').on("click", (e) => this.confirm_remove_stop($(e.currentTarget).data("name")));
		$html.find(".fg-recorridos-add-stops-btn").on("click", () => this.open_add_pick_lists_dialog());
		$html.find('[data-action="configure-location"]').on("click", (e) => {
			const stop = this.detail.stops.find((s) => s.name === $(e.currentTarget).data("name"));
			if (stop) this.open_configure_location_dialog(stop);
		});
		$html.find('[data-action="auto-geocode"]').on("click", (e) => {
			const stop = this.detail.stops.find((s) => s.name === $(e.currentTarget).data("name"));
			if (stop) this.auto_geocode_stop($(e.currentTarget), stop);
		});
		$html.find('[data-action="auto-geocode-pending"]').on("click", (e) => this.auto_geocode_pending($(e.currentTarget)));
	}

	// =====================================================================
	// Commit 24.4 -- Obtener ubicación (automático, Google Maps Platform).
	// geocode_customer_address()/geocode_route_pending_addresses() ->
	// refresh_route_geolocation() -> refrescar detalle. NEXT TO, never
	// instead of, Commit 24.3's own manual "Configurar ubicación" --
	// brief section 17's own manual confirm/correct flow is untouched.
	// =====================================================================
	auto_geocode_stop($btn, stop) {
		const original_html = $btn.html();
		$btn.prop("disabled", true).html(`<span class="fg-route-btn-spinner"></span> ${__("Buscando ubicación...")}`);

		this.call("geocode_customer_address", { address_name: stop.customer_address })
			.then((result) => this.call("refresh_route_geolocation", { route_name: this.detail.name }).then(() => result))
			.then((result) => {
				this.show_geocode_result_alert(result && result.status);
				return this.reload_detail(this.detail.name);
			})
			.finally(() => {
				$btn.prop("disabled", false).html(original_html);
			});
	}

	auto_geocode_pending($btn) {
		const original_html = $btn.html();
		$btn.prop("disabled", true).html(`<span class="fg-route-btn-spinner"></span> ${__("Buscando ubicaciones...")}`);
		this.set_busy(true);

		this.call("geocode_route_pending_addresses", { route_name: this.detail.name })
			.then((result) => {
				frappe.show_alert(
					{
						message: __("Geolocalizadas: {0} · Por revisar: {1} · Errores: {2}", [
							result.geocoded + result.already_geolocated,
							result.review,
							result.errors,
						]),
						indicator: result.errors ? "orange" : "green",
					},
					6
				);
				return this.reload_detail(this.detail.name);
			})
			.finally(() => {
				$btn.prop("disabled", false).html(original_html);
				this.set_busy(false);
			});
	}

	show_geocode_result_alert(status) {
		if (status === "Geolocalizado") {
			frappe.show_alert({ message: "✓ " + __("UBICACIÓN LISTA"), indicator: "green" }, 5);
		} else if (status === "Revisar") {
			frappe.show_alert({ message: "⚠ " + __("REVISAR UBICACIÓN"), indicator: "orange" }, 5);
		} else {
			frappe.show_alert({ message: "✕ " + __("NO ENCONTRADA"), indicator: "red" }, 5);
		}
	}

	// =====================================================================
	// Commit 24.3 -- Configurar ubicación (manual, brief section 15/16).
	// set_address_geolocation() -> refresh_route_geolocation() -> refrescar
	// detalle -- nunca inventa coordenadas, nunca llama un proveedor
	// externo. Mismo .fg-route-dialog compartido (icon-fix, min-height:0,
	// etc.) que los otros dos modales -- ninguna regla de ese scope se
	// tocó para construir este.
	// =====================================================================
	open_configure_location_dialog(stop) {
		if (!stop.customer_address) return;

		const dialog = new frappe.ui.Dialog({
			title: `
				<div class="fg-route-dialog-title">
					<div class="fg-route-dialog-title-icon fg-route-dialog-title-icon--violet">${icon("map-pin")}</div>
					<div class="fg-route-dialog-title-text">
						<div class="fg-route-dialog-title-main">${__("Configurar ubicación")}</div>
						<div class="fg-route-dialog-title-sub">${__(
							"Ingresa manualmente las coordenadas de esta dirección. Nunca se inventan ni se calculan automáticamente."
						)}</div>
					</div>
				</div>
			`,
			fields: [
				{ fieldtype: "Data", fieldname: "customer_display", label: __("Cliente"), read_only: 1, default: stop.customer_name || stop.customer || "" },
				{
					fieldtype: "Small Text",
					fieldname: "address_display_field",
					label: __("Dirección"),
					read_only: 1,
					default: address_text(stop.address_display, "\n") || __("Sin dirección registrada"),
				},
				{ fieldtype: "Section Break" },
				{
					fieldtype: "Float",
					fieldname: "latitude",
					label: __("Latitud"),
					precision: 6,
					reqd: 1,
					default: stop.latitude || "",
				},
				{ fieldtype: "Column Break" },
				{
					fieldtype: "Float",
					fieldname: "longitude",
					label: __("Longitud"),
					precision: 6,
					reqd: 1,
					default: stop.longitude || "",
				},
			],
			primary_action_label: `${icon("check", "fg-icon-sm")} ${__("Guardar ubicación")}`,
			primary_action: () => this.submit_configure_location(dialog, stop),
			secondary_action_label: __("Cancelar"),
			secondary_action: () => dialog.hide(),
		});
		dialog.$wrapper.addClass("fg-route-dialog fg-recorridos-geo-dialog");
		dialog.$wrapper.find(".modal-dialog").addClass("modal-dialog-scrollable");
		dialog.show();
	}

	submit_configure_location(dialog, stop) {
		const values = dialog.get_values(true);
		if (!values) return;
		dialog.disable_primary_action();
		this.set_busy(true);

		this.call("set_address_geolocation", {
			address_name: stop.customer_address,
			latitude: values.latitude,
			longitude: values.longitude,
			source: "Manual",
		})
			.then(() => this.call("refresh_route_geolocation", { route_name: this.detail.name }))
			.then(() => {
				frappe.show_alert({ message: "✓ " + __("Ubicación guardada correctamente."), indicator: "green" }, 5);
				dialog.hide();
				return this.reload_detail(this.detail.name);
			})
			.finally(() => {
				dialog.enable_primary_action();
				this.set_busy(false);
			});
	}

	_detail_pick_lists_in_order() {
		return this.detail.stops.map((s) => s.pick_list);
	}

	move_detail_stop(stop_name, direction) {
		const stops = this.detail.stops;
		const idx = stops.findIndex((s) => s.name === stop_name);
		const swap_idx = idx + direction;
		if (idx < 0 || swap_idx < 0 || swap_idx >= stops.length) return;
		[stops[idx], stops[swap_idx]] = [stops[swap_idx], stops[idx]];
		this.set_busy(true);
		this.call_route_write("update_route_stops", { route_name: this.detail.name, pick_lists: this._detail_pick_lists_in_order() })
			.then(() => this.reload_detail(this.detail.name))
			.finally(() => this.set_busy(false));
	}

	confirm_remove_stop(stop_name) {
		frappe.confirm(__("¿Quitar este pedido del recorrido?"), () => {
			const remaining = this.detail.stops.filter((s) => s.name !== stop_name).map((s) => s.pick_list);
			this.set_busy(true);
			this.call_route_write("update_route_stops", { route_name: this.detail.name, pick_lists: remaining })
				.then(() => Promise.all([this.reload_detail(this.detail.name), this.refresh_available(), this.refresh_summary_only()]))
				.finally(() => this.set_busy(false));
		});
	}

	refresh_summary_only() {
		return this.call("get_routes_summary").then((summary) => {
			this.summary = summary;
			if (this.$body.find(".fg-kpis--recorridos").length) this.$body.find(".fg-kpis--recorridos").replaceWith(this.render_kpis());
			this.$body.find(".fg-recorridos-tabs-nav").replaceWith(this.render_top_tabs_html());
		});
	}

	confirm_plan_route_from_detail() {
		frappe.confirm(__("Una vez planificado no podrás modificar las paradas desde esta pantalla."), () => {
			this.set_busy(true);
			this.call("plan_route", { route_name: this.detail.name })
				.then(() => {
					frappe.show_alert({ message: "✓ " + __("Recorrido planificado correctamente."), indicator: "green" }, 5);
					return Promise.all([this.reload_detail(this.detail.name), this.refresh_routes(), this.refresh_summary_only()]);
				})
				.finally(() => this.set_busy(false));
		});
	}

	confirm_cancel_route_from_detail() {
		frappe.confirm(__("¿Cancelar este recorrido? Esta acción no se puede deshacer y sus pedidos volverán a estar disponibles."), () => {
			this.set_busy(true);
			this.call("cancel_route", { route_name: this.detail.name })
				.then(() => {
					frappe.show_alert({ message: "✓ " + __("Recorrido cancelado."), indicator: "green" }, 5);
					if (this._detail_dialog) this._detail_dialog.hide();
					return this.load_all();
				})
				.finally(() => this.set_busy(false));
		});
	}

	// =====================================================================
	// Fase 26.2 -- INICIAR RECORRIDO (Planificado -> En Ruta).
	// A small dedicated Dialog (not frappe.confirm(), whose buttons are a
	// fixed Sí/No) so the actions read CANCELAR / INICIAR. INICIAR is
	// disabled while start_route() runs; start_route() is idempotent
	// server-side anyway, so a retried request still lands in Modo Recorrido.
	// =====================================================================
	confirm_start_route_from_detail() {
		const route_name = this.detail && this.detail.name;
		if (!route_name) return;
		const confirm = new frappe.ui.Dialog({
			title: __("¿Iniciar recorrido?"),
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "message",
					options: `<p class="fg-route-start-confirm-text">${__(
						"Una vez iniciado comenzarás la ruta de entrega. Las paradas conservarán el orden planificado."
					)}</p>`,
				},
			],
			primary_action_label: __("INICIAR"),
			primary_action: () => this.submit_start_route(confirm, route_name),
			secondary_action_label: __("CANCELAR"),
			secondary_action: () => confirm.hide(),
		});
		confirm.$wrapper.addClass("fg-route-start-confirm");
		confirm.show();
	}

	submit_start_route(confirm, route_name) {
		if (confirm.$wrapper.hasClass("fg-route-dialog-busy")) return;
		confirm.$wrapper.addClass("fg-route-dialog-busy");
		confirm.disable_primary_action();
		confirm.get_primary_btn().html(`<span class="fg-route-btn-spinner"></span> ${__("Iniciando...")}`);
		this.set_busy(true);

		this.call("start_route", { route_name: route_name })
			.then((detail) => {
				frappe.show_alert({ message: "✓ " + __("Recorrido iniciado."), indicator: "green" }, 5);
				confirm.hide();
				if (this._detail_dialog) this._detail_dialog.hide();
				this.enter_active_route(detail);
			})
			.catch(() => {
				// frappe.call's own error dialog already showed the real reason
				// (sin conductor, ubicaciones faltantes, estado inválido...).
			})
			.finally(() => {
				confirm.$wrapper.removeClass("fg-route-dialog-busy");
				confirm.enable_primary_action();
				confirm.get_primary_btn().html(__("INICIAR"));
				this.set_busy(false);
			});
	}

	// =====================================================================
	// Fase 26.2 -- MODO RECORRIDO (this.view === "active-route").
	// Rendered straight into this Page's own body (never a Dialog), mobile
	// first. Reuses get_route_detail() -- no extra endpoint. The current
	// stop is DERIVED (current_stop_of()), never persisted.
	// =====================================================================
	open_active_route(route_name) {
		if (!route_name) return;
		this.set_busy(true);
		return this.call("get_route_detail", { route_name: route_name })
			.then((detail) => {
				if (detail.status !== "En Ruta") {
					frappe.show_alert({ message: __("El recorrido {0} no está en ruta.", [detail.name]), indicator: "orange" }, 5);
					if (this.view === "active-route") this.exit_active_route();
					return;
				}
				this.enter_active_route(detail);
			})
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	enter_active_route(detail) {
		this.view = "active-route";
		this.active_route = detail;
		// Fase 26.3 -- "stop" (current stop + Waze/Maps) | "delivery"
		// (ENTREGAR PEDIDO panel). Any evidence already captured for a stop
		// stays in this._delivery, so a refresh never loses it.
		this.active_panel = "stop";
		this.render_active_route();
		window.scrollTo(0, 0);
	}

	exit_active_route() {
		this.view = "list";
		this.active_route = null;
		this.reset_delivery_state();
		this.active_tab = "recorridos";
		this._routes_loaded = false;
		return this.load_all();
	}

	render_active_route() {
		if (this.active_panel === "delivery") return this.render_delivery_panel();
		const d = this.active_route;
		const stops = d.stops || [];
		const current = current_stop_of(stops);
		const total = stops.length;
		const current_idx = current ? stops.indexOf(current) : -1;
		const next = current_idx >= 0 ? current_stop_of(stops.slice(current_idx + 1)) : null;

		const meta = [
			d.driver_name ? `${icon("user", "fg-icon-sm")} ${frappe.utils.escape_html(d.driver_name)}` : "",
			d.vehicle ? `${icon("truck", "fg-icon-sm")} ${frappe.utils.escape_html(d.vehicle)}` : "",
			d.started_on ? `${icon("clock", "fg-icon-sm")} ${__("Salida")} ${frappe.datetime.str_to_user(d.started_on)}` : "",
		]
			.filter(Boolean)
			.map((m) => `<span>${m}</span>`)
			.join("");

		const current_html = current
			? this.render_active_stop_html(current, current_idx + 1, total)
			: `
				<div class="fg-active-route-done">
					${icon("circle-check")}
					<div class="fg-active-route-done-title">${__("TODAS LAS PARADAS FUERON PROCESADAS")}</div>
					<div class="fg-active-route-done-sub">${__("Este recorrido ya no tiene entregas pendientes.")}</div>
				</div>
			`;

		const next_html = next
			? `
				<div class="fg-active-route-next">
					<div class="fg-active-route-label">${__("Próxima parada")}</div>
					<div class="fg-active-route-next-name">${frappe.utils.escape_html(next.customer_name || next.customer || __("Sin cliente"))}</div>
					<div class="fg-active-route-next-address">${
						address_html(next.address_display)
					}</div>
				</div>
			`
			: "";

		const all_stops_html = stops
			.map(
				(s) => `
				<li class="fg-active-route-stop-row ${s === current ? "is-current" : ""}">
					<span class="fg-active-route-stop-num">${cint(s.sequence)}</span>
					<span class="fg-active-route-stop-name">${frappe.utils.escape_html(s.customer_name || s.customer || __("Sin cliente"))}</span>
					${parada_status_badge_html(s.status)}
				</li>
			`
			)
			.join("");

		this.$body.html(`
			<div class="fg-active-route">
				<div class="fg-active-route-bar">
					<button type="button" class="fg-btn fg-btn--ghost fg-active-route-back">${icon("arrow-left", "fg-icon-sm")} ${__(
			"Recorridos"
		)}</button>
					<div class="fg-active-route-bar-status">
						${status_badge_html(d.status)}
						<span class="fg-active-route-bar-id">${frappe.utils.escape_html(d.name)}</span>
					</div>
				</div>
				${meta ? `<div class="fg-active-route-meta">${meta}</div>` : ""}
				${current_html}
				${next_html}
				<details class="fg-active-route-all">
					<summary>${icon("list", "fg-icon-sm")} ${__("Ver todas las paradas ({0})", [total])}</summary>
					<ol class="fg-active-route-stop-list">${all_stops_html}</ol>
				</details>
			</div>
		`);

		this.$body.find(".fg-active-route-back").on("click", () => this.exit_active_route());
		this.$body.find(".fg-active-route-deliver-btn").on("click", (e) => this.open_delivery_panel($(e.currentTarget).data("name")));
	}

	render_active_stop_html(stop, position, total) {
		const pedido_label = stop.commercial_name || stop.sales_order || stop.pick_list;
		const links = navigation_links(stop);
		// Real <a href> rendered up front -- never window.open() after an
		// await (mobile browsers block that as a popup). No href at all
		// when the coordinates are not valid.
		const nav_html = links
			? `
				<div class="fg-active-route-nav">
					<a class="fg-btn fg-active-route-nav-btn fg-active-route-nav-btn--waze" href="${links.waze}" target="_blank" rel="noopener noreferrer">${icon(
					"navigation"
			  )} ${__("ABRIR EN WAZE")}</a>
					<a class="fg-btn fg-active-route-nav-btn fg-active-route-nav-btn--maps" href="${links.maps}" target="_blank" rel="noopener noreferrer">${icon(
					"map"
			  )} ${__("GOOGLE MAPS")}</a>
				</div>
			`
			: `
				<div class="fg-active-route-nav fg-active-route-nav--unavailable">
					${icon("map-pin-off")} ${__("UBICACIÓN NO DISPONIBLE")}
				</div>
			`;

		return `
			<div class="fg-active-route-stop" data-name="${frappe.utils.escape_html(stop.name)}">
				<div class="fg-active-route-position">${__("PARADA {0} DE {1}", [position, total])}</div>
				<div class="fg-active-route-label">${__("Cliente")}</div>
				<div class="fg-active-route-customer">${frappe.utils.escape_html(stop.customer_name || stop.customer || __("Sin cliente"))}</div>
				<div class="fg-active-route-label">${__("Dirección")}</div>
				<div class="fg-active-route-address">${
					address_html(stop.address_display)
				}</div>
				<div class="fg-active-route-order">
					<span class="fg-badge fg-badge--route-pedido">${__("PEDIDO")} #${frappe.utils.escape_html(pedido_label || "")}</span>
					<span class="fg-active-route-qty">${cint(stop.item_count)} ${cint(stop.item_count) === 1 ? __("referencia") : __(
			"referencias"
		)} · ${format_qty(stop.total_qty)} ${__("uds")}</span>
				</div>
				${nav_html}
				${
					this.active_route && this.active_route.status === "En Ruta"
						? `<button type="button" class="fg-btn fg-active-route-deliver-btn" data-name="${frappe.utils.escape_html(stop.name)}">${icon(
								"package-check"
						  )} ${__("ENTREGAR PEDIDO")}</button>`
						: ""
				}
			</div>
		`;
	}

	// =====================================================================
	// Fase 26.3 -- ENTREGAR PEDIDO: foto + firma + observaciones.
	// A panel INSIDE active-route (this.active_panel = "delivery"), never a
	// Dialog. Evidence lives only in memory (this._delivery) until the one
	// multipart deliver_stop() request -- the server creates the private
	// Files; this page never uploads anything beforehand and never sends a
	// file_url. A failed request keeps the photo/signature for a retry.
	// =====================================================================
	open_delivery_panel(stop_name) {
		const stop = (this.active_route.stops || []).find((s) => s.name === stop_name);
		if (!stop || stop.status !== "Pendiente" || this.active_route.status !== "En Ruta") return;
		if (!this._delivery || this._delivery.stop_name !== stop_name) {
			this.reset_delivery_state();
			this._delivery = {
				stop_name: stop_name,
				photo_blob: null,
				photo_url: null,
				signature_blob: null,
				signature_url: null,
				notes: "",
				processing_photo: false,
				submitting: false,
				// Fase 26.3 (extensión) -- faltantes / cambios + pago.
				has_issues: false,
				issues_text: "",
				payment_status: null,
				payment_note: "",
				proof_blob: null,
				proof_url: null,
				processing_proof: false,
			};
		}
		this.active_panel = "delivery";
		this.render_active_route();
		window.scrollTo(0, 0);
	}

	close_delivery_panel() {
		// Evidence is kept (this._delivery) -- coming back to the same stop
		// restores it. Only a successful delivery or leaving the route
		// discards it.
		this.teardown_signature_pad();
		this.active_panel = "stop";
		this.render_active_route();
	}

	reset_delivery_state() {
		this.teardown_signature_pad();
		if (this._delivery) {
			if (this._delivery.photo_url) URL.revokeObjectURL(this._delivery.photo_url);
			if (this._delivery.signature_url) URL.revokeObjectURL(this._delivery.signature_url);
			if (this._delivery.proof_url) URL.revokeObjectURL(this._delivery.proof_url);
		}
		this._delivery = null;
	}

	render_delivery_panel() {
		const d = this.active_route;
		const state = this._delivery;
		const stop = state && (d.stops || []).find((s) => s.name === state.stop_name);
		if (!stop || stop.status !== "Pendiente") {
			this.reset_delivery_state();
			this.active_panel = "stop";
			return this.render_active_route();
		}
		const pedido_label = stop.commercial_name || stop.sales_order || stop.pick_list;

		this.$body.html(`
			<div class="fg-active-route fg-delivery">
				<div class="fg-active-route-bar">
					<button type="button" class="fg-btn fg-btn--ghost fg-delivery-back">${icon("arrow-left", "fg-icon-sm")} ${__("VOLVER")}</button>
					<div class="fg-active-route-bar-status">
						${status_badge_html(d.status)}
						<span class="fg-active-route-bar-id">${frappe.utils.escape_html(d.name)}</span>
					</div>
				</div>

				<div class="fg-active-route-stop fg-delivery-summary">
					<div class="fg-delivery-title">${__("ENTREGAR PEDIDO")}</div>
					<div class="fg-active-route-label">${__("Cliente")}</div>
					<div class="fg-active-route-customer">${frappe.utils.escape_html(stop.customer_name || stop.customer || __("Sin cliente"))}</div>
					<div class="fg-active-route-label">${__("Pedido")}</div>
					<div><span class="fg-badge fg-badge--route-pedido">${__("PEDIDO")} #${frappe.utils.escape_html(pedido_label || "")}</span></div>
					<div class="fg-active-route-label">${__("Dirección")}</div>
					<div class="fg-active-route-address">${
						address_html(stop.address_display)
					}</div>
				</div>

				<section class="fg-active-route-stop fg-delivery-section">
					<div class="fg-delivery-section-title">${icon("camera", "fg-icon-sm")} ${__("FOTO DE ENTREGA")}</div>
					<div class="fg-delivery-photo-preview"></div>
					<div class="fg-delivery-photo-actions">
						<label class="fg-btn fg-delivery-photo-btn fg-delivery-photo-btn--camera">
							<input type="file" class="fg-delivery-photo-input" accept="image/*" capture="environment" hidden>
							${icon("camera", "fg-icon-sm")} ${__("TOMAR FOTO")}
						</label>
						<label class="fg-btn fg-delivery-photo-btn fg-delivery-photo-btn--gallery">
							<input type="file" class="fg-delivery-photo-input" accept="image/*" hidden>
							${icon("image", "fg-icon-sm")} ${__("SUBIR FOTO")}
						</label>
					</div>
				</section>

				<section class="fg-active-route-stop fg-delivery-section">
					<div class="fg-delivery-section-title">${icon("pen-line", "fg-icon-sm")} ${__("FIRMA DEL CLIENTE")}</div>
					<div class="fg-delivery-signature"></div>
				</section>

				<section class="fg-active-route-stop fg-delivery-section fg-delivery-issues"></section>

				<section class="fg-active-route-stop fg-delivery-section fg-delivery-payment"></section>

				<section class="fg-active-route-stop fg-delivery-section">
					<label class="fg-delivery-section-title" for="fg-delivery-notes">${__("OBSERVACIONES DE ENTREGA")} <span class="fg-delivery-optional">${__("(opcional)")}</span></label>
					<textarea id="fg-delivery-notes" class="fg-delivery-notes" maxlength="${DELIVERY_NOTES_MAX_LENGTH}" rows="3" placeholder="${__(
			"Ej: recibió el administrador, portería, etc."
		)}"></textarea>
				</section>

				<div class="fg-delivery-confirm-bar">
					<button type="button" class="fg-btn fg-delivery-confirm-btn" disabled>${icon("circle-check")} ${__("CONFIRMAR ENTREGA")}</button>
				</div>
			</div>
		`);

		this.$body.find(".fg-delivery-notes").val(state.notes || "").on("input", (e) => {
			state.notes = e.currentTarget.value;
		});
		this.$body.find(".fg-delivery-back").on("click", () => this.close_delivery_panel());
		this.$body.find(".fg-delivery-photo-input").on("change", (e) => this.on_delivery_photo_selected(e.currentTarget));
		this.$body.find(".fg-delivery-confirm-btn").on("click", () => this.submit_delivery());

		this.render_delivery_photo_preview();
		this.render_signature_area();
		this.render_issues_section();
		this.render_payment_section();
		this.update_delivery_confirm_state();
	}

	// -- Faltantes / cambios (Fase 26.3 extensión) ----------------------------
	render_issues_section() {
		const state = this._delivery;
		const $section = this.$body.find(".fg-delivery-issues");
		$section.html(`
			<div class="fg-delivery-section-title">${icon("triangle-alert", "fg-icon-sm")} ${__("FALTANTES / CAMBIOS")}</div>
			<div class="fg-delivery-question">${__("¿Quedó algún faltante, cambio o pendiente del pedido?")}</div>
			<div class="fg-choice-group fg-choice-group--2" role="radiogroup" aria-label="${__("Faltantes / cambios")}">
				<button type="button" class="fg-choice ${state.has_issues ? "" : "is-selected"}" data-issues="0" role="radio" aria-checked="${!state.has_issues}">${__("NO")}</button>
				<button type="button" class="fg-choice ${state.has_issues ? "is-selected" : ""}" data-issues="1" role="radio" aria-checked="${!!state.has_issues}">${__("SÍ")}</button>
			</div>
			${
				state.has_issues
					? `
				<label class="fg-delivery-subtitle" for="fg-delivery-issues-text">${__("DETALLE DE FALTANTES / CAMBIOS")}</label>
				<textarea id="fg-delivery-issues-text" class="fg-delivery-notes fg-delivery-issues-text" maxlength="${DELIVERY_NOTES_MAX_LENGTH}" rows="3" placeholder="${__("Ej: faltó 1 galón, cliente solicita cambio de producto...")}"></textarea>
			`
					: ""
			}
		`);
		$section.find(".fg-delivery-issues-text").val(state.issues_text || "").on("input", (e) => {
			state.issues_text = e.currentTarget.value;
			this.update_delivery_confirm_state();
		});
		$section.find(".fg-choice").on("click", (e) => {
			const has_issues = $(e.currentTarget).data("issues") === 1;
			if (has_issues === state.has_issues) return;
			state.has_issues = has_issues;
			// NO -> the detail is discarded, never sent.
			if (!has_issues) state.issues_text = "";
			this.render_issues_section();
			this.update_delivery_confirm_state();
			if (has_issues) this.$body.find(".fg-delivery-issues-text").trigger("focus");
		});
	}

	// -- Estado del pago + comprobante + observación (Fase 26.3 extensión) --
	render_payment_section() {
		const state = this._delivery;
		const $section = this.$body.find(".fg-delivery-payment");
		const selected = PAYMENT_STATUS_OPTIONS.find((o) => o.value === state.payment_status);
		const is_paid = state.payment_status === PAYMENT_STATUS_PAID;

		$section.html(`
			<div class="fg-delivery-section-title">${icon("wallet", "fg-icon-sm")} ${__("ESTADO DEL PAGO")}</div>
			<div class="fg-choice-group fg-choice-group--payment" role="radiogroup" aria-label="${__("Estado del pago")}">
				${PAYMENT_STATUS_OPTIONS.map(
					(o) => `
					<button type="button" class="fg-choice ${o.value === state.payment_status ? "is-selected" : ""}" data-payment="${escape_text(
						o.value
					)}" role="radio" aria-checked="${o.value === state.payment_status}">${__(o.label)}</button>`
				).join("")}
			</div>
			${
				is_paid
					? `
				<div class="fg-delivery-subtitle">${icon("receipt", "fg-icon-sm")} ${__("COMPROBANTE DE PAGO")} <span class="fg-delivery-optional">${__("(opcional)")}</span></div>
				<div class="fg-delivery-photo-preview fg-delivery-proof-preview"></div>
				<div class="fg-delivery-photo-actions">
					<label class="fg-btn fg-delivery-photo-btn fg-delivery-photo-btn--camera">
						<input type="file" class="fg-delivery-proof-input" accept="image/*" capture="environment" hidden>
						${icon("camera", "fg-icon-sm")} ${__("TOMAR FOTO")}
					</label>
					<label class="fg-btn fg-delivery-photo-btn fg-delivery-photo-btn--gallery">
						<input type="file" class="fg-delivery-proof-input" accept="image/*" hidden>
						${icon("image", "fg-icon-sm")} ${__("SUBIR FOTO")}
					</label>
				</div>
			`
					: ""
			}
			${
				selected
					? `
				<label class="fg-delivery-subtitle" for="fg-delivery-payment-note">${__("OBSERVACIÓN DEL PAGO")} <span class="fg-delivery-optional">${__("(opcional)")}</span></label>
				<textarea id="fg-delivery-payment-note" class="fg-delivery-notes fg-delivery-payment-note" maxlength="${DELIVERY_NOTES_MAX_LENGTH}" rows="2" placeholder="${escape_text(
							__(selected.placeholder)
					  )}"></textarea>
			`
					: ""
			}
		`);

		$section.find(".fg-choice").on("click", (e) => this.select_payment_status($(e.currentTarget).data("payment")));
		$section.find(".fg-delivery-proof-input").on("change", (e) => this.on_payment_proof_selected(e.currentTarget));
		$section.find(".fg-delivery-payment-note").val(state.payment_note || "").on("input", (e) => {
			state.payment_note = e.currentTarget.value;
		});
		if (is_paid) this.render_payment_proof_preview();
	}

	select_payment_status(value) {
		const state = this._delivery;
		if (!state || !PAYMENT_STATUS_OPTIONS.some((o) => o.value === value) || value === state.payment_status) return;
		state.payment_status = value;
		// Only "Pagado" may carry a proof: switching away discards it.
		if (value !== PAYMENT_STATUS_PAID) this.clear_payment_proof();
		this.render_payment_section();
		this.update_delivery_confirm_state();
	}

	clear_payment_proof() {
		const state = this._delivery;
		if (!state) return;
		if (state.proof_url) URL.revokeObjectURL(state.proof_url);
		state.proof_url = null;
		state.proof_blob = null;
	}

	on_payment_proof_selected(input) {
		const state = this._delivery;
		const file = input.files && input.files[0];
		input.value = "";
		if (!state || !file || state.payment_status !== PAYMENT_STATUS_PAID) return;
		if (file.type && !file.type.startsWith("image/")) {
			frappe.msgprint(__("El archivo seleccionado no es una imagen."));
			return;
		}
		state.processing_proof = true;
		this.render_payment_proof_preview();
		this.update_delivery_confirm_state();

		prepare_delivery_photo(file)
			.then((blob) => {
				if (this._delivery !== state || state.payment_status !== PAYMENT_STATUS_PAID) return;
				if (state.proof_url) URL.revokeObjectURL(state.proof_url);
				state.proof_blob = blob;
				state.proof_url = URL.createObjectURL(blob);
			})
			.catch(() => {
				frappe.msgprint(__("No se pudo procesar el comprobante. Tómalo de nuevo o sube otra imagen."));
			})
			.finally(() => {
				state.processing_proof = false;
				if (this._delivery === state && this.active_panel === "delivery") {
					this.render_payment_proof_preview();
					this.update_delivery_confirm_state();
				}
			});
	}

	render_payment_proof_preview() {
		const state = this._delivery;
		const $preview = this.$body.find(".fg-delivery-proof-preview");
		if (state.processing_proof) {
			$preview.html(`<div class="fg-delivery-placeholder"><span class="fg-route-btn-spinner"></span> ${__("Procesando comprobante...")}</div>`);
		} else if (state.proof_url) {
			$preview.html(`
				<img class="fg-delivery-photo-img" src="${state.proof_url}" alt="${__("Comprobante de pago")}">
				<button type="button" class="fg-btn fg-delivery-proof-remove">${icon("x", "fg-icon-sm")} ${__("QUITAR COMPROBANTE")}</button>
			`);
			$preview.find(".fg-delivery-proof-remove").on("click", () => {
				this.clear_payment_proof();
				this.render_payment_proof_preview();
			});
		} else {
			$preview.html(`<div class="fg-delivery-placeholder">${icon("receipt")} ${__("Sin comprobante (opcional)")}</div>`);
		}
	}

	// -- Foto ---------------------------------------------------------------
	on_delivery_photo_selected(input) {
		const state = this._delivery;
		const file = input.files && input.files[0];
		input.value = "";
		if (!state || !file) return;
		if (file.type && !file.type.startsWith("image/")) {
			frappe.msgprint(__("El archivo seleccionado no es una imagen."));
			return;
		}
		state.processing_photo = true;
		this.render_delivery_photo_preview();
		this.update_delivery_confirm_state();

		prepare_delivery_photo(file)
			.then((blob) => {
				if (this._delivery !== state) return;
				if (state.photo_url) URL.revokeObjectURL(state.photo_url);
				state.photo_blob = blob;
				state.photo_url = URL.createObjectURL(blob);
			})
			.catch(() => {
				frappe.msgprint(__("No se pudo procesar la foto. Tómala de nuevo o sube otra imagen."));
			})
			.finally(() => {
				state.processing_photo = false;
				if (this._delivery === state && this.active_panel === "delivery") {
					this.render_delivery_photo_preview();
					this.update_delivery_confirm_state();
				}
			});
	}

	render_delivery_photo_preview() {
		const state = this._delivery;
		const $preview = this.$body.find(".fg-delivery-photo-preview");
		if (state.processing_photo) {
			$preview.html(`<div class="fg-delivery-placeholder"><span class="fg-route-btn-spinner"></span> ${__("Procesando foto...")}</div>`);
		} else if (state.photo_url) {
			$preview.html(`<img class="fg-delivery-photo-img" src="${state.photo_url}" alt="${__("Foto de entrega")}">`);
		} else {
			$preview.html(`<div class="fg-delivery-placeholder">${icon("camera")} ${__("Sin foto todavía")}</div>`);
		}
	}

	// -- Firma --------------------------------------------------------------
	render_signature_area() {
		const state = this._delivery;
		const $area = this.$body.find(".fg-delivery-signature");
		this.teardown_signature_pad();

		if (state.signature_url) {
			$area.html(`
				<img class="fg-signature-preview" src="${state.signature_url}" alt="${__("Firma del cliente")}">
				<div class="fg-signature-actions">
					<button type="button" class="fg-btn fg-signature-redo-btn">${icon("rotate-ccw", "fg-icon-sm")} ${__("FIRMAR DE NUEVO")}</button>
				</div>
			`);
			$area.find(".fg-signature-redo-btn").on("click", () => {
				if (state.signature_url) URL.revokeObjectURL(state.signature_url);
				state.signature_url = null;
				state.signature_blob = null;
				this.render_signature_area();
				this.update_delivery_confirm_state();
			});
			return;
		}

		$area.html(`
			<div class="fg-signature-box">
				<canvas class="fg-signature-canvas" aria-label="${__("Área de firma del cliente")}"></canvas>
				<div class="fg-signature-hint">${__("Firme aquí con el dedo")}</div>
			</div>
			<div class="fg-signature-actions">
				<button type="button" class="fg-btn fg-signature-clear-btn">${icon("eraser", "fg-icon-sm")} ${__("LIMPIAR")}</button>
				<button type="button" class="fg-btn fg-signature-accept-btn" disabled>${icon("check", "fg-icon-sm")} ${__("ACEPTAR FIRMA")}</button>
			</div>
		`);

		const $accept = $area.find(".fg-signature-accept-btn");
		const $box = $area.find(".fg-signature-box");
		this._signature_pad = create_signature_pad($area.find(".fg-signature-canvas")[0], () => {
			const empty = this._signature_pad.is_empty();
			$accept.prop("disabled", empty);
			$box.toggleClass("has-ink", !empty);
		});
		$area.find(".fg-signature-clear-btn").on("click", () => this._signature_pad && this._signature_pad.clear());
		$accept.on("click", () => this.accept_signature());
	}

	accept_signature() {
		const state = this._delivery;
		const pad = this._signature_pad;
		if (!state || !pad) return;
		if (pad.is_empty()) {
			frappe.msgprint(__("La firma está vacía. Pide al cliente que firme."));
			return;
		}
		pad.to_blob()
			.then((blob) => {
				if (this._delivery !== state) return;
				state.signature_blob = blob;
				state.signature_url = URL.createObjectURL(blob);
				this.render_signature_area();
				this.update_delivery_confirm_state();
			})
			.catch(() => frappe.msgprint(__("No se pudo guardar la firma. Intenta de nuevo.")));
	}

	teardown_signature_pad() {
		if (this._signature_pad) this._signature_pad.destroy();
		this._signature_pad = null;
	}

	// -- Confirmar ----------------------------------------------------------
	update_delivery_confirm_state() {
		const state = this._delivery;
		const ready = delivery_evidence_ready(state);
		const $btn = this.$body.find(".fg-delivery-confirm-btn");
		$btn.prop("disabled", !ready);
		if (state && state.submitting) {
			$btn.html(`<span class="fg-route-btn-spinner"></span> ${__("Confirmando entrega...")}`);
		} else {
			$btn.html(`${icon("circle-check")} ${__("CONFIRMAR ENTREGA")}`);
		}
	}

	submit_delivery() {
		const state = this._delivery;
		if (!delivery_evidence_ready(state)) return;
		const route_name = this.active_route.name;
		state.submitting = true;
		this.update_delivery_confirm_state();

		const form = new FormData();
		form.append("route_name", route_name);
		form.append("stop_name", state.stop_name);
		form.append("notes", state.notes || "");
		form.append("photo", state.photo_blob, "foto.jpg");
		form.append("signature", state.signature_blob, "firma.png");
		form.append("has_delivery_issues", state.has_issues ? "1" : "0");
		if (state.has_issues) form.append("delivery_issues", state.issues_text || "");
		form.append("payment_status", state.payment_status);
		form.append("payment_note", state.payment_note || "");
		if (state.payment_status === PAYMENT_STATUS_PAID && state.proof_blob) {
			form.append("payment_proof", state.proof_blob, "comprobante.jpg");
		}

		post_multipart("fabergray_erp.api.recorridos.deliver_stop", form)
			.then(() => this.call("get_route_detail", { route_name: route_name }))
			.then((detail) => {
				frappe.show_alert({ message: "✓ " + __("Entrega confirmada."), indicator: "green" }, 5);
				this.reset_delivery_state();
				this.active_route = detail;
				this.active_panel = "stop";
				this.render_active_route();
				window.scrollTo(0, 0);
			})
			.catch(() => {
				// The server's (or a network) error was already shown; the
				// photo/signature stay in memory for a retry.
			})
			.finally(() => {
				if (this._delivery === state) {
					state.submitting = false;
					if (this.active_panel === "delivery") this.update_delivery_confirm_state();
				}
			});
	}

	// -- Sub-modal: AGREGAR PEDIDOS (brief section 14) -- reuses
	// get_available_orders() (already excludes this same route's own
	// current stops, since those Pick Lists are already claimed by it)
	// and update_route_stops() (full replacement -- never a separate
	// "add stop" endpoint, per the brief's own instruction). ------------
	open_add_pick_lists_dialog() {
		this._add_search = "";
		this._add_rows = [];
		this._add_selected = new Map();

		const dialog = new frappe.ui.Dialog({
			title: `${icon("plus")} ${__("Agregar pedidos")}`,
			size: "large",
			fields: [{ fieldtype: "HTML", fieldname: "add_html" }],
			primary_action_label: __("AGREGAR"),
			primary_action: () => this.confirm_add_pick_lists(dialog),
			secondary_action_label: __("CANCELAR"),
			secondary_action: () => dialog.hide(),
		});
		dialog.$wrapper.addClass("fg-recorridos-add-dialog");
		dialog.disable_primary_action();
		this._add_dialog = dialog;
		dialog.show();
		this.load_add_pick_lists();
	}

	load_add_pick_lists() {
		const $html = this._add_dialog.fields_dict.add_html.$wrapper;
		$html.html(`<div class="fg-recorridos-detail-loading">${__("Cargando...")}</div>`);
		return this.call("get_available_orders", { txt: this._add_search, start: 0, page_length: 50 }).then((r) => {
			this._add_rows = r.pick_lists;
			this.render_add_pick_lists();
		});
	}

	render_add_pick_lists() {
		const $html = this._add_dialog.fields_dict.add_html.$wrapper;
		const rows_html = this._add_rows.length
			? this._add_rows
					.map((r) => {
						const checked = this._add_selected.has(r.pick_list);
						const pedido_label = r.commercial_name || r.sales_order || r.pick_list;
						return `
						<label class="fg-recorridos-avail-card fg-recorridos-avail-card--compact ${checked ? "is-selected" : ""}" data-pick-list="${frappe.utils.escape_html(
							r.pick_list
						)}">
							<input type="checkbox" class="fg-recorridos-avail-checkbox" ${checked ? "checked" : ""}>
							<div class="fg-recorridos-avail-card-body">
								<div class="fg-recorridos-avail-card-top">
									<div class="fg-recorridos-avail-card-id">${__("PEDIDO")} #${frappe.utils.escape_html(pedido_label)}</div>
								</div>
								<div class="fg-recorridos-avail-card-customer">${frappe.utils.escape_html(r.customer_name || r.customer || __("Sin cliente"))}</div>
								<div class="fg-recorridos-avail-card-meta">
									<span>${r.item_count} ${__("productos")}</span>
									<span>${format_qty(r.total_qty)} ${__("unidades")}</span>
								</div>
							</div>
						</label>
					`;
					})
					.join("")
			: `<div class="fg-empty">${__("No hay pedidos disponibles.")}</div>`;

		$html.html(`
			<div class="fg-recorridos-search-wrap">
				${icon("search", "fg-recorridos-search-icon")}
				<input type="text" class="fg-recorridos-search-input" placeholder="${__("Buscar...")}" value="${frappe.utils.escape_html(
			this._add_search || ""
		)}">
			</div>
			<div class="fg-recorridos-avail-cards fg-recorridos-avail-cards--compact">${rows_html}</div>
		`);

		$html.find(".fg-recorridos-search-input").on("input", (e) => {
			const val = $(e.currentTarget).val();
			clearTimeout(this._add_search_debounce);
			this._add_search_debounce = setTimeout(() => {
				this._add_search = val;
				this.load_add_pick_lists();
			}, 300);
		});

		$html.find(".fg-recorridos-avail-cards").on("change", ".fg-recorridos-avail-checkbox", (e) => {
			const $card = $(e.currentTarget).closest(".fg-recorridos-avail-card");
			const pick_list = $card.data("pick-list");
			const row = this._add_rows.find((r) => r.pick_list === pick_list);
			if (e.currentTarget.checked) this._add_selected.set(pick_list, row);
			else this._add_selected.delete(pick_list);
			$card.toggleClass("is-selected", e.currentTarget.checked);
			if (this._add_selected.size) this._add_dialog.enable_primary_action();
			else this._add_dialog.disable_primary_action();
		});
	}

	confirm_add_pick_lists(dialog) {
		if (!this._add_selected.size) return;
		const combined = [...this._detail_pick_lists_in_order(), ...Array.from(this._add_selected.keys())];
		dialog.disable_primary_action();
		this.set_busy(true);
		this.call_route_write("update_route_stops", { route_name: this.detail.name, pick_lists: combined })
			.then(() => {
				dialog.hide();
				return Promise.all([this.reload_detail(this.detail.name), this.refresh_available(), this.refresh_summary_only()]);
			})
			.catch(() => this.load_add_pick_lists())
			.finally(() => {
				dialog.enable_primary_action();
				this.set_busy(false);
			});
	}
};

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, no server calls, no state.
// Intentionally duplicated (not imported) from facturacion.js/bodega.js/
// jefe_de_bodega.js/cotizaciones.js/ventas.js, same reasoning stated in
// every one of those files: a few lines each, zero business logic, keeps
// this Page's asset loading independent of theirs.
// -------------------------------------------------------------------------
const PAGE_SIZE = 10;

// Commit 25.20 -- unified search bar markup, same shape/classes as every
// other operational Page's own copy (page/ventas/ventas.js's own
// render_search_bar_html() carries the full "why reproduced, not
// imported" comment). Clear button visibility is simply re-rendered from
// `value` here (never toggled via a separate class-flip in the input
// handler) -- this Page's own search is server-side + debounced, so every
// change already triggers a full section re-render (refresh_routes()/
// refresh_history()), unlike the client-side Pages.
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

// Commit 24.2's own detail-header weekday label ("Sábado" under the date,
// design_references/recorridos_detalle_borrador_v2.png) -- same
// try/catch + capitalize idiom page/bodega/bodega.js's own
// format_today_es() already uses, but for an explicit `route_date`
// ("YYYY-MM-DD") rather than "today". Parses year/month/day components
// directly (never `new Date("YYYY-MM-DD")`) so the weekday never shifts
// by one day in a negative-UTC-offset timezone.
function weekday_label(date_str) {
	if (!date_str) return "";
	try {
		const [y, m, day] = date_str.split(" ")[0].split("-").map(Number);
		const label = new Date(y, m - 1, day).toLocaleDateString("es-CO", { weekday: "long" });
		return label.charAt(0).toUpperCase() + label.slice(1);
	} catch (e) {
		return "";
	}
}

function status_badge_html(status) {
	const map = {
		Borrador: { cls: "borrador", label: __("BORRADOR") },
		Planificado: { cls: "planificado", label: __("PLANIFICADO") },
		"En Ruta": { cls: "en-ruta", label: __("EN RUTA") },
		Completado: { cls: "completado", label: __("COMPLETADO") },
		Cancelado: { cls: "cancelado", label: __("CANCELADO") },
	};
	const m = map[status] || { cls: "borrador", label: status || "" };
	return `<span class="fg-badge fg-badge--route-${m.cls}">${m.label}</span>`;
}

// Fase 26.2 -- Modo Recorrido helpers. Pure: no server calls, no state.

// Mirror of geocoding.is_valid_coordinate_pair() (the server's one central
// rule): finite numbers, -90..90 / -180..180, never the 0,0 sentinel.
// Returns the parsed pair, or null.
function valid_coordinate_pair(latitude, longitude) {
	if (latitude === null || latitude === undefined || latitude === "") return null;
	if (longitude === null || longitude === undefined || longitude === "") return null;
	const lat = Number(latitude);
	const lng = Number(longitude);
	if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
	if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
	if (lat === 0 && lng === 0) return null;
	return { lat, lng };
}

// External navigation links for ONE stop, built from its own coordinate
// snapshot -- no SDK, no API request. toFixed(6) (~0.1 m) keeps the URL to
// digits, "." and "-" only. null when the coordinates are not valid, so the
// caller never renders an href.
function navigation_links(stop) {
	const pair = stop ? valid_coordinate_pair(stop.latitude, stop.longitude) : null;
	if (!pair) return null;
	const ll = `${pair.lat.toFixed(6)},${pair.lng.toFixed(6)}`;
	return {
		waze: `https://waze.com/ul?ll=${ll}&navigate=yes`,
		maps: `https://www.google.com/maps/dir/?api=1&destination=${ll}&travelmode=driving`,
	};
}

// The current stop is derived, never persisted: the first stop still
// "Pendiente", by sequence ASC.
function current_stop_of(stops) {
	return (
		(stops || [])
			.slice()
			.sort((a, b) => cint(a.sequence) - cint(b.sequence))
			.find((s) => s.status === "Pendiente") || null
	);
}

// Fase 26.3 -- Address.address_display arrives as HTML from Frappe's address
// template ("Calle 1<br>Bucaramanga<br>Colombia<br>"). Escaping it as-is showed
// literal "<br>" on screen; injecting it as HTML would trust Address content.
// Instead: <br> variants -> line breaks, every other tag stripped, the few
// entities Frappe emits decoded, then EACH line escaped again -- the only
// markup in the result is the <br> this helper adds itself.
const HTML_ENTITIES = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&#x27;": "'", "&nbsp;": " " };

function escape_text(value) {
	return String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function address_lines(value) {
	if (value === null || value === undefined) return [];
	return String(value)
		.replace(/<br\s*\/?>/gi, "\n")
		.replace(/<[^>]*>/g, "")
		.replace(/&(amp|lt|gt|quot|#39|#x27|nbsp);/g, (entity) => HTML_ENTITIES[entity])
		.split(/\n/)
		.map((line) => line.replace(/\s+/g, " ").trim())
		.filter(Boolean);
}

// Safe HTML for display: escaped lines joined by <br>.
function address_html(value) {
	const lines = address_lines(value);
	return lines.length ? lines.map(escape_text).join("<br>") : escape_text(__("Sin dirección registrada"));
}

// Plain text (e.g. a Frappe field value, which the control escapes itself).
function address_text(value, separator) {
	return address_lines(value).join(separator === undefined ? ", " : separator);
}

// Fase 26.3 -- ENTREGAR PEDIDO helpers. Pure / DOM-local, no Frappe state.

const DELIVERY_PHOTO_MAX_SIDE = 1600;
const DELIVERY_PHOTO_QUALITY = 0.8;
const DELIVERY_NOTES_MAX_LENGTH = 1000;
// A signature needs at least this many drawn points (not a single tap).
const SIGNATURE_MIN_POINTS = 5;

// Payment status REPORTED by the driver -- never an accounting confirmation.
// Same three values as recorrido_parada.PAYMENT_STATUSES on the server.
const PAYMENT_STATUS_PAID = "Pagado";
const PAYMENT_STATUS_OPTIONS = [
	{ value: PAYMENT_STATUS_PAID, label: "PAGADO", placeholder: "Ej: pago recibido en efectivo / transferencia Bancolombia." },
	{ value: "Pendiente por Pago", label: "PENDIENTE POR PAGO", placeholder: "Ej: cliente indica que realizará la transferencia mañana." },
	{ value: "Crédito", label: "CRÉDITO", placeholder: "Ej: factura a crédito 30 días." },
];

// CONFIRMAR ENTREGA is enabled only with a processed photo, an accepted
// signature and an explicitly chosen payment status -- plus a detail when
// "faltantes / cambios" is SÍ -- and never while an image is still being
// processed or a request is already in flight. The payment proof and every
// note stay optional.
function delivery_evidence_ready(state) {
	if (!state || !state.photo_blob || !state.signature_blob) return false;
	if (state.processing_photo || state.processing_proof || state.submitting) return false;
	if (!PAYMENT_STATUS_OPTIONS.some((o) => o.value === state.payment_status)) return false;
	if (state.has_issues && !(state.issues_text || "").trim()) return false;
	return true;
}

function canvas_to_blob(canvas, type, quality) {
	return new Promise((resolve, reject) => {
		canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("toBlob failed"))), type, quality);
	});
}

function load_image_element(file) {
	return new Promise((resolve, reject) => {
		const url = URL.createObjectURL(file);
		const img = new Image();
		img.onload = () => {
			URL.revokeObjectURL(url);
			resolve(img);
		};
		img.onerror = () => {
			URL.revokeObjectURL(url);
			reject(new Error("image decode failed"));
		};
		img.src = url;
	});
}

// Photo -> oriented, resized (max DELIVERY_PHOTO_MAX_SIDE on the long side)
// JPEG blob. createImageBitmap with imageOrientation "from-image" applies the
// EXIF orientation; the <img> fallback relies on the browser's default
// image-orientation. Redrawing on a canvas also drops all EXIF (GPS...).
// The server re-validates and re-encodes anyway.
async function prepare_delivery_photo(file) {
	let source;
	try {
		source = await createImageBitmap(file, { imageOrientation: "from-image" });
	} catch (e) {
		source = await load_image_element(file);
	}
	const width = source.width || source.naturalWidth;
	const height = source.height || source.naturalHeight;
	if (!width || !height) throw new Error("empty image");
	const scale = Math.min(1, DELIVERY_PHOTO_MAX_SIDE / Math.max(width, height));
	const canvas = document.createElement("canvas");
	canvas.width = Math.round(width * scale);
	canvas.height = Math.round(height * scale);
	const ctx = canvas.getContext("2d");
	ctx.fillStyle = "#ffffff";
	ctx.fillRect(0, 0, canvas.width, canvas.height);
	ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
	if (source.close) source.close();
	return canvas_to_blob(canvas, "image/jpeg", DELIVERY_PHOTO_QUALITY);
}

// Own signature pad: Pointer Events (finger, mouse, stylus), sized to the
// CSS box times devicePixelRatio, white background, touch-action:none in
// CSS so the page does not scroll while signing. `on_change` fires after
// every stroke and on clear().
function create_signature_pad(canvas, on_change) {
	const ctx = canvas.getContext("2d");
	let drawing = false;
	let last = null;
	let points = 0;

	function paint_background() {
		ctx.save();
		ctx.setTransform(1, 0, 0, 1, 0, 0);
		ctx.fillStyle = "#ffffff";
		ctx.fillRect(0, 0, canvas.width, canvas.height);
		ctx.restore();
	}

	function setup() {
		const rect = canvas.getBoundingClientRect();
		const ratio = Math.max(window.devicePixelRatio || 1, 1);
		canvas.width = Math.max(Math.round(rect.width * ratio), 1);
		canvas.height = Math.max(Math.round(rect.height * ratio), 1);
		ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
		ctx.lineWidth = 2.6;
		ctx.lineCap = "round";
		ctx.lineJoin = "round";
		ctx.strokeStyle = "#101828";
		paint_background();
		points = 0;
	}

	function position(e) {
		const rect = canvas.getBoundingClientRect();
		return { x: e.clientX - rect.left, y: e.clientY - rect.top };
	}

	function on_down(e) {
		e.preventDefault();
		if (canvas.setPointerCapture) canvas.setPointerCapture(e.pointerId);
		drawing = true;
		last = position(e);
		ctx.beginPath();
		ctx.arc(last.x, last.y, ctx.lineWidth / 2, 0, Math.PI * 2);
		ctx.fillStyle = ctx.strokeStyle;
		ctx.fill();
		points += 1;
	}

	function on_move(e) {
		if (!drawing) return;
		e.preventDefault();
		const p = position(e);
		ctx.beginPath();
		ctx.moveTo(last.x, last.y);
		ctx.lineTo(p.x, p.y);
		ctx.stroke();
		last = p;
		points += 1;
	}

	function on_up() {
		if (!drawing) return;
		drawing = false;
		last = null;
		on_change && on_change();
	}

	function on_resize() {
		// Resizing a canvas wipes it: only re-fit while still empty.
		if (points === 0) setup();
	}

	setup();
	canvas.addEventListener("pointerdown", on_down);
	canvas.addEventListener("pointermove", on_move);
	canvas.addEventListener("pointerup", on_up);
	canvas.addEventListener("pointercancel", on_up);
	canvas.addEventListener("pointerleave", on_up);
	window.addEventListener("resize", on_resize);

	return {
		is_empty: () => points < SIGNATURE_MIN_POINTS,
		clear() {
			setup();
			on_change && on_change();
		},
		to_blob: () => canvas_to_blob(canvas, "image/png"),
		destroy() {
			canvas.removeEventListener("pointerdown", on_down);
			canvas.removeEventListener("pointermove", on_move);
			canvas.removeEventListener("pointerup", on_up);
			canvas.removeEventListener("pointercancel", on_up);
			canvas.removeEventListener("pointerleave", on_up);
			window.removeEventListener("resize", on_resize);
		},
	};
}

// Multipart POST to a whitelisted method -- frappe.call() cannot send files.
// Same transport Frappe's own FileUploader uses: same-origin cookies plus the
// X-Frappe-CSRF-Token header. Resolves with `message`; on any failure shows
// the server's own message (or a connectivity message) and rejects.
function post_multipart(method, form_data) {
	return fetch(`/api/method/${method}`, {
		method: "POST",
		body: form_data,
		credentials: "same-origin",
		headers: { Accept: "application/json", "X-Frappe-CSRF-Token": frappe.csrf_token },
	})
		.catch((error) => {
			frappe.msgprint({
				title: __("Sin conexión"),
				message: __("No se pudo enviar la entrega. Revisa la conexión e intenta de nuevo; la foto y la firma se conservan."),
				indicator: "orange",
			});
			throw error;
		})
		.then((response) =>
			response
				.json()
				.catch(() => ({}))
				.then((data) => {
					if (!response.ok || data.exc || data.exc_type) {
						show_server_error(data);
						throw data;
					}
					return data.message;
				})
		);
}

function show_server_error(data) {
	let messages = [];
	try {
		messages = JSON.parse(data._server_messages || "[]").map((m) => {
			try {
				return JSON.parse(m).message;
			} catch (e) {
				return m;
			}
		});
	} catch (e) {
		messages = [];
	}
	frappe.msgprint({
		title: __("No se pudo confirmar la entrega"),
		message: messages.filter(Boolean).join("<br>") || __("Ocurrió un error inesperado. Intenta de nuevo."),
		indicator: "red",
	});
}

// Recorrido Parada.status ("Pendiente"/"Entregado"/"No Entregado") is a
// SEPARATE status domain from Recorrido.status above -- its own small
// badge helper rather than reusing status_badge_html()'s map (whose keys
// are route-level statuses and would only coincidentally look right).
function parada_status_badge_html(status) {
	const map = {
		Pendiente: { cls: "borrador", label: __("Pendiente") },
		Entregado: { cls: "completado", label: __("Entregado") },
		"No Entregado": { cls: "cancelado", label: __("No entregado") },
	};
	const m = map[status] || { cls: "borrador", label: status || "" };
	return `<span class="fg-badge fg-badge--route-${m.cls}">${m.label}</span>`;
}
