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

		// -- Historial (Completado/Cancelado) -------------------------------
		this.hist_rows = [];
		this.hist_total = 0;
		this.hist_page = 1;
		this.hist_status_filter = ""; // "" (todos) | "Completado" | "Cancelado"

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
		this.$app.find(".fg-refresh-btn").on("click", () => this.load_all());
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
		const address = r.address_display ? frappe.utils.escape_html(r.address_display) : __("Sin dirección registrada");
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
				const address = r.address_display ? frappe.utils.escape_html(r.address_display) : __("Sin dirección registrada");
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
			<div class="fg-recorridos-filter-chips">${filters_html}</div>
			<div class="fg-recorridos-route-cards">${this.render_route_cards_html(this.hist_rows, true)}</div>
			<div class="fg-recorridos-pagination" data-scope="hist">${this.render_pagination_html(this.hist_page, this.hist_total)}</div>
		`;
	}

	bind_history_events() {
		const $t = this.$body.find(".fg-recorridos-tab-body");
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
						const address = s.address_display ? frappe.utils.escape_html(s.address_display) : __("Sin dirección registrada");
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
						const geo_configure_btn =
							is_borrador && !is_geo_ready && s.customer_address
								? can_administer_geolocation
									? `<button type="button" class="fg-route-geo-configure-btn" data-action="configure-location" data-name="${s.name}">${icon(
											"map-pin",
											"fg-icon-sm"
									  )} ${__("Configurar ubicación")}</button>`
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
		if (is_borrador) {
			dialog.set_primary_action(`${icon("calendar-check", "fg-icon-sm")} ${__("Planificar recorrido")}`, () =>
				this.confirm_plan_route_from_detail()
			);
		} else {
			dialog.get_primary_btn().addClass("hide");
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
					default: stop.address_display || __("Sin dirección registrada"),
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
