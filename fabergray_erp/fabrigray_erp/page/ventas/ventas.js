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
			editing_order_name: null, // Commit 18.5: null -> "Nuevo pedido"; a Draft Sales Order name -> "Editar pedido"
			modifying_order_name: null, // Commit 18.5b: a Submitted Sales Order name -> "Modificar pedido"
			customer: null, // {name, customer_name}
			cart: new Map(), // item_code -> {item_code, item_name, description, stock_uom, image, qty_disponible, qty}
			customer_results: [],
			item_results: [],
			observations: "",
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
			{ key: "pendientes", label: __("Pendientes"), sub: __("Pedidos por completar"), i: "clock", mod: "ventas-pendientes" },
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
					<div class="fg-order-card-id">#${frappe.utils.escape_html(o.commercial_name || o.name)}</div>
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

	confirm_cancel_order(name) {
		if (!name) return;
		frappe.confirm(
			__(
				"¿Cancelar este pedido? Esta acción retirará el pedido del flujo operativo cuando sea permitido por ERPNext."
			),
			() => {
				this.call("cancel_sales_order", { name: name })
					.then(() => {
						frappe.show_alert({ message: __("Pedido cancelado."), indicator: "green" }, 5);
						this.load_dashboard();
					})
					.catch(() => {
						// Native ERPNext blocks (submitted Pick List/Material Request/
						// Purchase Order still linked, etc.) surface here via the
						// server's own real error message -- never swallowed, never
						// bypassed, no manual cleanup attempted client-side.
					});
			}
		);
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
					<button type="button" class="fg-order-detail-close" title="${__("Cerrar")}">${icon("x")}</button>
				</div>
				<div class="fg-order-detail-customer">
					${icon("user", "fg-icon-sm")} ${frappe.utils.escape_html(detail.customer_name || detail.customer || "—")}
				</div>
				<div class="fg-order-detail-meta">
					<span>${icon("calendar", "fg-icon-sm")} ${frappe.datetime.str_to_user(detail.transaction_date)}</span>
					<span>${icon("truck", "fg-icon-sm")} ${__("Entrega")}: ${entrega}</span>
				</div>
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
		this.render_item_results_empty_prompt();
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

	save_draft_edit(payload) {
		this.busy = true;
		const $btn = this.$body.find(".fg-confirm-btn").prop("disabled", true).addClass("fg-btn--loading");

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
				$btn.prop("disabled", false).removeClass("fg-btn--loading");
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
		const $btn = this.$body.find(".fg-confirm-btn").prop("disabled", true).addClass("fg-btn--loading");

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
				$btn.prop("disabled", false).removeClass("fg-btn--loading");
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
