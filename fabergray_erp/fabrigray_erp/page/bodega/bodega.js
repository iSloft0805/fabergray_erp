// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["bodega"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Bodega"),
		single_column: true,
	});
	new fabergray_erp.Bodega(page);
};

// All server communication in this file goes through get_queue(), get_pick_list(),
// get_shortages(), get_inventory(), start_picking(), set_picked_qty(),
// report_shortage() and finish_picking() -- nothing here queries Bin, Item,
// Reporte de Faltante or Pick List directly, computes permissions, decides
// shortage rules, or touches Sales Order. That logic lives entirely in
// fabergray_erp.api.bodega. This file only changes how that data is rendered
// and navigated -- no new business rules.
fabergray_erp.Bodega = class Bodega {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.bodega.";
		// Qty-stepper coalescing state (see request_picked_qty/sync_row below):
		// desired_qty is the latest value the user has asked for per row,
		// updated synchronously on every click; inflight_rows tracks which
		// rows currently have a set_picked_qty request in flight. At most one
		// request is ever in flight per row_name -- other rows are untouched.
		this.desired_qty = new Map();
		this.inflight_rows = new Set();
		this.busy = false;
		this.last_changed_row = null;

		// Bottom-nav sections: "home" | "orders" | "shortages" | "history" | "more".
		// view: "list" (whatever the active section shows) | "detail" (a Pick
		// List opened from any section). return_section is which section's
		// list the back button (or finishing/cancelling out of a detail)
		// goes back to -- captured at the moment a detail is opened, since a
		// Pick List can be reached from Inicio, Pedidos, or Faltantes.
		this.state = {
			section: "home",
			view: "list",
			return_section: "home",
			queue: null, // get_queue() -- shared by home, orders and history sections
			filter_bucket: null, // Inicio's own KPI-click filter (unchanged from before)
			orders_filter: null, // Pedidos' own chip filter -- independent of Inicio's
			shortages: null, // get_shortages() -- fetched once, filtered client-side by chip
			shortages_filter: null,
			history_search: "",
			history_date_from: "",
			history_date_to: "",
			more_view: "menu", // "menu" | "inventory" | "profile" | "help"
			inventory: null, // get_inventory() -- fetched once, filtered client-side by search
			inventory_search: "",
			pick_list: null,
			detail: null,
		};

		this.$app = $('<div class="fg-bodega">').appendTo(this.page.body);
		this.render_shell();
		this.load_queue();
	}

	// -------------------------------------------------------------------
	// Thin API wrappers -- the only place that talks to the server.
	//
	// frappe.call() itself does NOT return a real Promise -- it returns the
	// jQuery Deferred/jqXHR from $.ajax(), whose promise object only
	// implements state/always/catch/pipe/then/done/fail/progress/notify
	// (see node_modules/jquery/dist/jquery.js, Deferred's `promise` object)
	// -- there is no .finally(). call() wraps it in `new Promise(...)`,
	// exactly like frappe's own frappe.xcall(), so every .then()/.catch()/
	// .finally() below behaves like a standard Promise.
	// -------------------------------------------------------------------
	call(method, args) {
		return new Promise((resolve, reject) => {
			frappe.call({
				method: this.method_prefix + method,
				args: args || {},
				callback: (r) => resolve(r.message),
				error: (r) => reject(r),
			});
		});
	}

	// -------------------------------------------------------------------
	// Data loaders. Each one only fetches + stores state + re-renders --
	// none of them decide navigation (section/view), so they are safe to
	// call from go_to_section(), refresh_active_section() or a failure
	// fallback without fighting over what should be on screen.
	// -------------------------------------------------------------------
	load_queue() {
		this.set_shell_busy(true);
		if (this.$body) {
			if (this.state.section === "home") this.render_skeleton_queue();
			else this.render_skeleton_list();
		}
		return this.call("get_queue")
			.then((data) => {
				this.state.queue = data;
				this.render_body();
			})
			.finally(() => this.set_shell_busy(false));
	}

	load_shortages() {
		this.set_shell_busy(true);
		if (this.$body) this.render_skeleton_list();
		return this.call("get_shortages")
			.then((data) => {
				this.state.shortages = data;
				this.render_body();
			})
			.finally(() => this.set_shell_busy(false));
	}

	load_inventory() {
		this.set_shell_busy(true);
		if (this.$body) this.render_skeleton_list();
		return this.call("get_inventory")
			.then((data) => {
				this.state.inventory = data;
				this.render_body();
			})
			.finally(() => this.set_shell_busy(false));
	}

	load_detail(pick_list) {
		this.set_shell_busy(true);
		if (this.$body) this.render_skeleton_detail();
		return this.call("get_pick_list", { name: pick_list })
			.then((data) => {
				this.state.view = "detail";
				this.state.pick_list = pick_list;
				this.state.detail = data;
				this.render_body();
			})
			.finally(() => this.set_shell_busy(false));
	}

	// -------------------------------------------------------------------
	// Shell: header (logo, title, user, refresh) stays fixed across every
	// section and view -- only this.$body's content changes.
	//
	// Refresh button: same SPA refresh pattern as page/ventas/ventas.js's
	// own .fg-refresh-btn -- click -> re-call the real endpoint(s) for
	// whatever is currently on screen -> replace local state with the
	// fresh response -> re-render, never a cached or stale render, never
	// window.location.reload(). set_shell_busy() is the exact same
	// disable-button + toggle(".fg-loading") pair ventas.js's own
	// set_busy() uses. The calls below have no explicit .catch(): frappe's
	// own native error dialog already covers failures, and .finally()
	// (a real Promise now, see call() above) always restores the button.
	// -------------------------------------------------------------------
	render_shell() {
		const fullname = frappe.session.user_fullname || frappe.session.user;
		this.$app.html(`
			<div class="fg-header">
				<div class="fg-header-brand">
					<span class="fg-header-logo">FABRIGRAY</span>
					<span class="fg-header-sep">|</span>
					<span class="fg-header-title">${__("Bodega")}</span>
				</div>
				<div class="fg-header-user">
					<div class="fg-header-user-info">
						<div class="fg-header-user-name">${frappe.utils.escape_html(fullname)}</div>
						<div class="fg-header-user-role">${__("Bodega")}</div>
					</div>
					<div class="fg-header-avatar">${get_initials(fullname)}</div>
					<button type="button" class="fg-refresh-btn" title="${__("Actualizar")}">${icon("refresh-cw")}</button>
				</div>
			</div>
			<div class="fg-body"></div>
		`);
		this.$body = this.$app.find(".fg-body");
		this.$app.find(".fg-refresh-btn").on("click", () => {
			this.refresh_active_section();
		});
	}

	set_shell_busy(is_busy) {
		this.$app.find(".fg-refresh-btn").prop("disabled", is_busy);
		this.$app.toggleClass("fg-loading", !!is_busy);
	}

	// Re-fetches whatever endpoint backs the section/view currently on
	// screen. Never window.location.reload(). Independent of the stepper's
	// own per-row pending state -- this only ever touches state.queue/
	// shortages/inventory/detail, never desired_qty/inflight_rows.
	refresh_active_section() {
		if (this.state.view === "detail" && this.state.pick_list) {
			return this.load_detail(this.state.pick_list);
		}
		switch (this.state.section) {
			case "home":
			case "orders":
			case "history":
				return this.load_queue();
			case "shortages":
				return this.load_shortages();
			case "more":
				if (this.state.more_view === "inventory") {
					return this.load_inventory();
				}
				return Promise.resolve();
			default:
				return Promise.resolve();
		}
	}

	// -------------------------------------------------------------------
	// Bottom-nav navigation.
	// -------------------------------------------------------------------
	go_to_section(section) {
		if (this.state.section === section && this.state.view === "list") {
			return; // already there -- no redundant re-fetch on a repeat tap
		}
		this.state.section = section;
		this.state.view = "list";
		this.state.pick_list = null;
		this.state.detail = null;
		if (section === "more") this.state.more_view = "menu";

		if ((section === "home" || section === "orders" || section === "history") && !this.state.queue) {
			this.load_queue();
			return;
		}
		if (section === "shortages" && !this.state.shortages) {
			this.load_shortages();
			return;
		}
		this.render_body();
	}

	// Every place that opens a Pick List detail (from Inicio, Pedidos or
	// Faltantes) goes through this so the back button always knows which
	// section's list to return to.
	open_pick_list_from(section, pick_list, bucket) {
		this.state.return_section = section;
		this.open_pick_list(pick_list, bucket);
	}

	back_to_list() {
		this.state.view = "list";
		this.state.pick_list = null;
		this.state.detail = null;
		this.state.section = this.state.return_section || "home";
		this.render_body();
	}

	// -------------------------------------------------------------------
	// Body dispatcher -- the one place that decides what this.$body shows.
	// The bottom nav itself is appended here, unconditionally, so it is
	// never lost switching sections or opening/closing a detail.
	// -------------------------------------------------------------------
	render_body() {
		let content_html;
		if (this.state.view === "detail") {
			content_html = this.render_detail_html();
		} else {
			switch (this.state.section) {
				case "orders":
					content_html = this.render_orders_html();
					break;
				case "shortages":
					content_html = this.render_shortages_html();
					break;
				case "history":
					content_html = this.render_history_html();
					break;
				case "more":
					content_html = this.render_more_html();
					break;
				default:
					content_html = this.render_home_html();
			}
		}

		this.$body.html(content_html + this.render_bottom_nav_html());
		this.bind_bottom_nav_events();

		if (this.state.view === "detail") {
			this.bind_detail_events();
		} else {
			switch (this.state.section) {
				case "orders":
					this.bind_orders_events();
					break;
				case "shortages":
					this.bind_shortages_events();
					break;
				case "history":
					this.bind_history_events();
					break;
				case "more":
					this.bind_more_events();
					break;
				default:
					this.bind_home_events();
			}
		}
	}

	render_skeleton_queue() {
		this.$body.html(`
			<div class="fg-skeleton-kpis">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div>
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
			<div class="fg-skeleton-cards">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
			${this.render_bottom_nav_html()}
		`);
	}

	// Generic list skeleton for every section that isn't Inicio's KPI
	// dashboard (Pedidos, Faltantes, Historial, Inventario).
	render_skeleton_list() {
		this.$body.html(`
			<div class="fg-skeleton" style="height:38px;margin-bottom:16px;"></div>
			<div class="fg-skeleton-cards">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
			${this.render_bottom_nav_html()}
		`);
	}

	render_skeleton_detail() {
		this.$body.html(`
			<div class="fg-skeleton" style="height:88px;margin-bottom:22px;"></div>
			<div class="fg-skeleton" style="height:104px;margin-bottom:22px;"></div>
			<div class="fg-skeleton-cards">
				<div class="fg-skeleton" style="height:108px;"></div>
				<div class="fg-skeleton" style="height:108px;"></div>
				<div class="fg-skeleton" style="height:108px;"></div>
			</div>
			${this.render_bottom_nav_html()}
		`);
	}

	// -------------------------------------------------------------------
	// Bottom nav -- now real navigation, not decorative. Section state
	// lives on `this`, so it always reflects whatever is actually showing.
	// -------------------------------------------------------------------
	render_bottom_nav_html() {
		const faltantes_count = ((this.state.queue || {}).con_faltantes || []).length;
		const active = this.state.view === "detail" ? this.state.return_section : this.state.section;
		const items = [
			{ key: "home", icon_name: "house", label: __("Inicio") },
			{ key: "orders", icon_name: "clipboard-list", label: __("Pedidos") },
			{ key: "shortages", icon_name: "triangle-alert", label: __("Faltantes"), badge: faltantes_count },
			{ key: "history", icon_name: "clock", label: __("Historial") },
			{ key: "more", icon_name: "ellipsis", label: __("Más") },
		];
		const items_html = items
			.map(
				(it) => `
					<button type="button" class="fg-nav-item ${active === it.key ? "is-active" : ""}" data-section="${it.key}">
						${icon(it.icon_name)}<span>${it.label}</span>
						${it.badge ? `<span class="fg-nav-badge">${it.badge}</span>` : ""}
					</button>
				`
			)
			.join("");
		return `<div class="fg-bottom-nav">${items_html}</div>`;
	}

	bind_bottom_nav_events() {
		this.$body.find(".fg-nav-item").on("click", (e) => {
			const section = $(e.currentTarget).data("section");
			this.go_to_section(section);
		});
	}

	// -------------------------------------------------------------------
	// Inicio -- unchanged dashboard: KPIs, "Pedidos de hoy", filters,
	// access to alistamiento. Only the bottom nav below it is now real.
	// -------------------------------------------------------------------
	static bucket_meta() {
		return {
			pendientes: { label: __("Pendientes"), icon: "clipboard-list" },
			en_alistamiento: { label: __("En alistamiento"), icon: "clock" },
			con_faltantes: { label: __("Con faltantes"), icon: "triangle-alert" },
			listos: { label: __("Listos"), icon: "circle-check-big" },
		};
	}

	render_home_html() {
		const queue = this.state.queue || {};
		const meta = Bodega.bucket_meta();
		const order = ["con_faltantes", "en_alistamiento", "pendientes", "listos"];

		const kpi_html = order
			.map((bucket) => {
				const count = (queue[bucket] || []).length;
				const active = this.state.filter_bucket === bucket;
				return `
					<button type="button" class="fg-kpi fg-kpi--${bucket} ${active ? "is-active" : ""}" data-bucket="${bucket}">
						<div class="fg-kpi-icon">${icon(meta[bucket].icon)}</div>
						<div class="fg-kpi-number">${count}</div>
						<div class="fg-kpi-label">${meta[bucket].label}</div>
						<span class="fg-kpi-link">${__("Ver pedidos")} ${icon("chevron-right", "fg-icon-sm")}</span>
					</button>
				`;
			})
			.join("");

		const buckets_to_show = this.state.filter_bucket ? [this.state.filter_bucket] : order;
		let cards_html = "";
		let any = false;
		buckets_to_show.forEach((bucket) => {
			(queue[bucket] || []).forEach((pl) => {
				any = true;
				cards_html += this.render_queue_card(pl, bucket);
			});
		});
		if (!any) {
			cards_html = `<div class="fg-empty">${__("No hay pedidos en este estado.")}</div>`;
		}

		return `
			<div class="fg-kpis">${kpi_html}</div>
			<div class="fg-section-head">
				<div>
					<div class="fg-section-title">${__("Pedidos de hoy")}</div>
					<div class="fg-section-date">${format_today_es()}</div>
				</div>
			</div>
			<div class="fg-cards">${cards_html}</div>
		`;
	}

	bind_home_events() {
		this.$body.find(".fg-kpi").on("click", (e) => {
			const bucket = $(e.currentTarget).data("bucket");
			this.state.filter_bucket = this.state.filter_bucket === bucket ? null : bucket;
			this.render_body();
		});

		this.$body.find(".fg-order-card").on("click", (e) => {
			const $card = $(e.currentTarget);
			this.open_pick_list_from("home", $card.data("name"), $card.data("bucket"));
		});
	}

	// Shared by Inicio and Pedidos -- same card, same click target.
	// PEDIDO-N is now the resolved commercial name (root_commercial_name(),
	// same helper Ventas uses, see fabergray_erp/sales_order_naming.py) when
	// a Sales Order can be derived, falling back to the raw Pick List name
	// exactly like before when it can't.
	render_queue_card(pl, bucket) {
		const meta = Bodega.bucket_meta()[bucket];
		const pedido_label = pl.commercial_name || pl.sales_order || pl.name;
		const customer = pl.customer ? frappe.utils.escape_html(pl.customer) : __("Sin cliente");
		const started_line =
			bucket !== "pendientes" && pl.fg_started_by
				? `<div class="fg-order-meta">${icon("user", "fg-icon-sm")} ${__("Iniciado por")} ${frappe.utils.escape_html(
						pl.fg_started_by
				  )}</div>`
				: "";

		let button_label = __("VER DETALLE");
		let button_class = "fg-btn--outline-success";
		if (bucket === "pendientes") {
			button_label = __("INICIAR ALISTAMIENTO");
			button_class = "fg-btn--solid-primary";
		} else if (bucket === "en_alistamiento") {
			button_label = __("CONTINUAR");
			button_class = "fg-btn--outline-warning";
		} else if (bucket === "con_faltantes") {
			button_label = __("VER FALTANTES");
			button_class = "fg-btn--outline-danger";
		}

		return `
			<div class="fg-order-card fg-order-card--${bucket} fg-kpi--${bucket}" data-name="${frappe.utils.escape_html(
			pl.name
		)}" data-bucket="${bucket}">
				<div class="fg-order-main">
					<div class="fg-order-icon">${icon(meta.icon)}</div>
					<div>
						<div class="fg-order-id">${__("PEDIDO")} #${frappe.utils.escape_html(pedido_label)}</div>
						<div class="fg-order-customer">${customer}</div>
					</div>
				</div>
				<div class="fg-order-info">
					<div class="fg-order-meta">${icon("package", "fg-icon-sm")} ${pl.item_count} ${
			pl.item_count === 1 ? __("producto") : __("productos")
		}</div>
					${started_line}
				</div>
				<div class="fg-progress-block">
					<span class="fg-badge fg-badge--${bucket}">${meta.label}</span>
					${queue_progress_markup(bucket)}
				</div>
				<button type="button" class="fg-btn ${button_class} fg-order-card-btn">${button_label}</button>
			</div>
		`;
	}

	open_pick_list(pick_list, bucket) {
		if (bucket === "pendientes") {
			this.set_shell_busy(true);
			this.call("start_picking", { name: pick_list })
				.then(() => this.load_detail(pick_list))
				.catch(() => {
					this.state.view = "list";
					this.state.pick_list = null;
					return this.load_queue();
				})
				.finally(() => this.set_shell_busy(false));
		} else {
			this.load_detail(pick_list);
		}
	}

	// -------------------------------------------------------------------
	// Pedidos -- all Pick Lists visible to the user (get_queue()'s four
	// buckets, no date restriction), filterable by chip instead of Inicio's
	// KPI cards. Same card, same open action as Inicio.
	// -------------------------------------------------------------------
	render_orders_html() {
		const queue = this.state.queue || {};
		const meta = Bodega.bucket_meta();
		const filter_order = ["pendientes", "en_alistamiento", "con_faltantes", "listos"];
		const active_filter = this.state.orders_filter || "todos";

		const chip_html = ["todos", ...filter_order]
			.map((key) => {
				const label = key === "todos" ? __("Todos") : meta[key].label;
				const count =
					key === "todos"
						? filter_order.reduce((sum, b) => sum + (queue[b] || []).length, 0)
						: (queue[key] || []).length;
				return `<button type="button" class="fg-chip ${
					active_filter === key ? "is-active" : ""
				}" data-filter="${key}">${label} (${count})</button>`;
			})
			.join("");

		const buckets_to_show = active_filter === "todos" ? filter_order : [active_filter];
		let cards_html = "";
		let any = false;
		buckets_to_show.forEach((bucket) => {
			(queue[bucket] || []).forEach((pl) => {
				any = true;
				cards_html += this.render_queue_card(pl, bucket);
			});
		});
		if (!any) {
			cards_html = `<div class="fg-empty">${__("No hay pedidos en este estado.")}</div>`;
		}

		return `
			<div class="fg-section-head"><div class="fg-section-title">${__("Pedidos")}</div></div>
			<div class="fg-chip-row">${chip_html}</div>
			<div class="fg-cards">${cards_html}</div>
		`;
	}

	bind_orders_events() {
		this.$body.find(".fg-chip").on("click", (e) => {
			this.state.orders_filter = $(e.currentTarget).data("filter");
			this.render_body();
		});
		this.$body.find(".fg-order-card").on("click", (e) => {
			const $card = $(e.currentTarget);
			this.open_pick_list_from("orders", $card.data("name"), $card.data("bucket"));
		});
	}

	// -------------------------------------------------------------------
	// Faltantes -- get_shortages(), fetched once per section entry/refresh,
	// filtered client-side by status chip (same interaction as Pedidos).
	// -------------------------------------------------------------------
	render_shortages_html() {
		const chip_html = this.render_shortage_chips_html();
		return `
			<div class="fg-section-head"><div class="fg-section-title">${__("Faltantes")}</div></div>
			<div class="fg-chip-row">${chip_html}</div>
			<div class="fg-cards fg-shortage-cards">${this.render_shortage_cards_html()}</div>
		`;
	}

	render_shortage_chips_html() {
		const reports = this.state.shortages || [];
		const statuses = ["Abierto", "En Proceso", "Resuelto"];
		const active_filter = this.state.shortages_filter || "todos";
		return ["todos", ...statuses]
			.map((key) => {
				const label = key === "todos" ? __("Todos") : key;
				const count = key === "todos" ? reports.length : reports.filter((r) => r.status === key).length;
				return `<button type="button" class="fg-chip ${
					active_filter === key ? "is-active" : ""
				}" data-filter="${key}">${label} (${count})</button>`;
			})
			.join("");
	}

	render_shortage_cards_html() {
		const reports = this.state.shortages || [];
		const active_filter = this.state.shortages_filter || "todos";
		const filtered = active_filter === "todos" ? reports : reports.filter((r) => r.status === active_filter);
		if (!filtered.length) {
			return `<div class="fg-empty">${__("No hay faltantes en este estado.")}</div>`;
		}
		return filtered.map((r) => this.render_shortage_card(r)).join("");
	}

	render_shortage_card(r) {
		const pedido_label = r.commercial_name || r.sales_order || null;
		const status_class =
			{ Abierto: "con_faltantes", "En Proceso": "en_alistamiento", Resuelto: "listos" }[r.status] || "pendientes";
		return `
			<div class="fg-order-card fg-shortage-card" data-pick-list="${frappe.utils.escape_html(r.pick_list || "")}">
				<div class="fg-order-main">
					<div class="fg-order-icon">${icon("triangle-alert")}</div>
					<div>
						<div class="fg-order-id">${
							pedido_label
								? __("PEDIDO") + " #" + frappe.utils.escape_html(pedido_label)
								: __("Sin pedido vinculado")
						}</div>
						<div class="fg-order-customer">${frappe.utils.escape_html(r.item_name || r.item_code)}</div>
					</div>
				</div>
				<div class="fg-order-info fg-shortage-info">
					<div>${__("Solicitado")}: <b>${format_qty(r.qty_solicitada)}</b></div>
					<div>${__("Disponible")}: <b>${format_qty(r.qty_disponible)}</b></div>
					<div>${__("Faltante")}: <b>${format_qty(r.qty_faltante)}</b></div>
					<div>${__("Motivo")}: ${frappe.utils.escape_html(r.shortage_reason || "—")}</div>
					<div>${r.reported_on ? frappe.datetime.str_to_user(r.reported_on) : ""}</div>
				</div>
				<span class="fg-badge fg-badge--${status_class}">${frappe.utils.escape_html(r.status)}</span>
				${
					r.pick_list
						? `<button type="button" class="fg-btn fg-btn--outline-danger fg-shortage-open-btn">${__(
								"VER PEDIDO"
						  )}</button>`
						: ""
				}
			</div>
		`;
	}

	bind_shortages_events() {
		this.$body.find(".fg-chip").on("click", (e) => {
			this.state.shortages_filter = $(e.currentTarget).data("filter");
			this.$body.find(".fg-chip-row").html(this.render_shortage_chips_html());
			this.$body.find(".fg-shortage-cards").html(this.render_shortage_cards_html());
			this.bind_shortages_events();
		});
		this.$body.find(".fg-shortage-open-btn").on("click", (e) => {
			e.stopPropagation();
			const pick_list = $(e.currentTarget).closest(".fg-shortage-card").data("pick-list");
			if (pick_list) this.open_pick_list_from("shortages", pick_list, "con_faltantes");
		});
	}

	// -------------------------------------------------------------------
	// Historial -- finished (docstatus=1) Pick Lists only, i.e. get_queue()
	// ["listos"] -- the same fetch Inicio/Pedidos already use, not a new
	// endpoint. Search/date filters are entirely client-side over that one
	// payload. Only fg_started_on is shown/filtered as a timestamp: there is
	// no reliable "finished at" field on Pick List (confirmed against its
	// doctype JSON -- no Date/Datetime field at all besides the standard
	// `modified`, which is a last-write timestamp, not a semantic
	// completion time, so it is deliberately never shown as one here).
	// -------------------------------------------------------------------
	render_history_html() {
		return `
			<div class="fg-section-head"><div class="fg-section-title">${__("Historial")}</div></div>
			<div class="fg-history-filters">
				<input type="text" class="fg-history-search" placeholder="${__(
					"Buscar por pedido o cliente..."
				)}" value="${frappe.utils.escape_html(this.state.history_search || "")}">
				<div class="fg-history-date-range">
					<input type="date" class="fg-history-date-from" value="${this.state.history_date_from || ""}">
					<span class="fg-history-date-sep">–</span>
					<input type="date" class="fg-history-date-to" value="${this.state.history_date_to || ""}">
				</div>
			</div>
			<div class="fg-cards fg-history-cards">${this.render_history_cards_html()}</div>
		`;
	}

	render_history_cards_html() {
		const rows = ((this.state.queue || {}).listos || []).slice();
		const search = (this.state.history_search || "").trim().toLowerCase();
		const from = this.state.history_date_from;
		const to = this.state.history_date_to;

		const filtered = rows.filter((pl) => {
			if (search) {
				const haystack = [pl.commercial_name, pl.sales_order, pl.name, pl.customer]
					.filter(Boolean)
					.join(" ")
					.toLowerCase();
				if (!haystack.includes(search)) return false;
			}
			if (from || to) {
				if (!pl.fg_started_on) return false;
				const started_date = pl.fg_started_on.slice(0, 10);
				if (from && started_date < from) return false;
				if (to && started_date > to) return false;
			}
			return true;
		});

		if (!filtered.length) {
			return `<div class="fg-empty">${__("No hay alistamientos finalizados que coincidan.")}</div>`;
		}
		return filtered.map((pl) => this.render_history_card(pl)).join("");
	}

	render_history_card(pl) {
		const pedido_label = pl.commercial_name || pl.sales_order || pl.name;
		const customer = pl.customer ? frappe.utils.escape_html(pl.customer) : __("Sin cliente");
		return `
			<div class="fg-order-card fg-order-card--listos" data-name="${frappe.utils.escape_html(pl.name)}">
				<div class="fg-order-main">
					<div class="fg-order-icon">${icon("circle-check-big")}</div>
					<div>
						<div class="fg-order-id">${__("PEDIDO")} #${frappe.utils.escape_html(pedido_label)}</div>
						<div class="fg-order-customer">${customer}</div>
					</div>
				</div>
				<div class="fg-order-info">
					<div class="fg-order-meta">${icon("package", "fg-icon-sm")} ${pl.item_count} ${
			pl.item_count === 1 ? __("producto") : __("productos")
		}</div>
					${
						pl.fg_started_by
							? `<div class="fg-order-meta">${icon("user", "fg-icon-sm")} ${__("Alistado por")} ${frappe.utils.escape_html(
									pl.fg_started_by
							  )}</div>`
							: ""
					}
					${
						pl.fg_started_on
							? `<div class="fg-order-meta">${icon("clock", "fg-icon-sm")} ${__("Inicio")}: ${frappe.datetime.str_to_user(
									pl.fg_started_on
							  )}</div>`
							: ""
					}
				</div>
				<span class="fg-badge fg-badge--listos">${__("Listo")}</span>
				<button type="button" class="fg-btn fg-btn--outline-success fg-history-open-btn">${__("VER DETALLE")}</button>
			</div>
		`;
	}

	bind_history_events() {
		const rerender_cards = () => this.$body.find(".fg-history-cards").html(this.render_history_cards_html());

		this.$body.find(".fg-history-search").on("input", (e) => {
			this.state.history_search = $(e.currentTarget).val();
			rerender_cards();
		});
		this.$body.find(".fg-history-date-from").on("change", (e) => {
			this.state.history_date_from = $(e.currentTarget).val();
			rerender_cards();
		});
		this.$body.find(".fg-history-date-to").on("change", (e) => {
			this.state.history_date_to = $(e.currentTarget).val();
			rerender_cards();
		});
		// Delegated on the stable .fg-history-cards container -- survives
		// rerender_cards() replacing its children, no need to re-bind.
		this.$body.find(".fg-history-cards").on("click", ".fg-order-card", (e) => {
			this.open_pick_list_from("history", $(e.currentTarget).data("name"), "listos");
		});
	}

	// -------------------------------------------------------------------
	// Más -- V1 menu: Inventario (read-only), Mi usuario, Ayuda. No
	// administrative configuration.
	// -------------------------------------------------------------------
	render_more_html() {
		switch (this.state.more_view) {
			case "inventory":
				return this.render_inventory_html();
			case "profile":
				return this.render_profile_html();
			case "help":
				return this.render_help_html();
			default:
				return this.render_more_menu_html();
		}
	}

	render_more_menu_html() {
		return `
			<div class="fg-section-head"><div class="fg-section-title">${__("Más")}</div></div>
			<div class="fg-more-menu">
				<button type="button" class="fg-more-menu-item" data-more="inventory">
					${icon("package")}<span>${__("Inventario")}</span>${icon("chevron-right", "fg-icon-sm")}
				</button>
				<button type="button" class="fg-more-menu-item" data-more="profile">
					${icon("user")}<span>${__("Mi usuario")}</span>${icon("chevron-right", "fg-icon-sm")}
				</button>
				<button type="button" class="fg-more-menu-item" data-more="help">
					${icon("circle-help")}<span>${__("Ayuda")}</span>${icon("chevron-right", "fg-icon-sm")}
				</button>
			</div>
		`;
	}

	render_more_subheader(title) {
		return `
			<div class="fg-more-subheader">
				<button type="button" class="fg-back-btn fg-more-back-btn">${icon("arrow-left")} ${__("Volver")}</button>
				<div class="fg-section-title">${title}</div>
			</div>
		`;
	}

	// Inventario: read-only. No Stock Entry, no adjustment control, no
	// write action anywhere in this markup -- get_inventory() itself only
	// ever reads Bin + Item (see api/bodega.py).
	render_inventory_html() {
		return `
			${this.render_more_subheader(__("Inventario"))}
			<input type="text" class="fg-inventory-search" placeholder="${__(
				"Buscar producto..."
			)}" value="${frappe.utils.escape_html(this.state.inventory_search || "")}">
			<div class="fg-inventory-list">${this.render_inventory_rows_html()}</div>
		`;
	}

	render_inventory_rows_html() {
		const items = this.state.inventory || [];
		const search = (this.state.inventory_search || "").trim().toLowerCase();
		const filtered = search
			? items.filter(
					(i) =>
						(i.item_name || "").toLowerCase().includes(search) || i.item_code.toLowerCase().includes(search)
			  )
			: items;
		if (!filtered.length) {
			return `<div class="fg-empty">${__("No hay productos que coincidan.")}</div>`;
		}
		return filtered
			.map(
				(i) => `
					<div class="fg-inventory-row">
						<div class="fg-inventory-row-main">
							<div class="fg-inventory-row-name">${frappe.utils.escape_html(i.item_name || i.item_code)}</div>
							<div class="fg-inventory-row-code">${frappe.utils.escape_html(i.item_code)} · ${frappe.utils.escape_html(
					i.warehouse
				)}</div>
						</div>
						<div class="fg-inventory-row-qty">
							<div><b>${format_qty(i.actual_qty)}</b> ${frappe.utils.escape_html(i.uom || "")}</div>
							<div class="fg-order-meta">${__("Disponible")}: ${format_qty(i.available_qty)}</div>
						</div>
					</div>
				`
			)
			.join("");
	}

	render_profile_html() {
		const fullname = frappe.session.user_fullname || frappe.session.user;
		return `
			${this.render_more_subheader(__("Mi usuario"))}
			<div class="fg-profile-card">
				<div class="fg-header-avatar fg-profile-avatar">${get_initials(fullname)}</div>
				<div class="fg-profile-name">${frappe.utils.escape_html(fullname)}</div>
				<div class="fg-profile-email">${frappe.utils.escape_html(frappe.session.user)}</div>
				<span class="fg-badge fg-badge--pendientes">${__("Bodega")}</span>
			</div>
		`;
	}

	render_help_html() {
		return `
			${this.render_more_subheader(__("Ayuda"))}
			<div class="fg-help-block">
				<p><b>${__("Inicio")}</b> — ${__(
			"KPIs y pedidos del día, con acceso rápido a iniciar o continuar un alistamiento."
		)}</p>
				<p><b>${__("Pedidos")}</b> — ${__("Todos los pedidos visibles para ti, filtrables por estado.")}</p>
				<p><b>${__("Faltantes")}</b> — ${__(
			"Reportes de faltante, filtrables por estado, con acceso al pedido relacionado."
		)}</p>
				<p><b>${__("Historial")}</b> — ${__(
			"Alistamientos ya finalizados, con búsqueda por pedido, cliente y fecha de inicio."
		)}</p>
				<p><b>${__("Inventario")}</b> — ${__("Existencias actuales de solo lectura para tu bodega.")}</p>
			</div>
		`;
	}

	bind_more_events() {
		if (this.state.more_view === "menu") {
			this.$body.find(".fg-more-menu-item").on("click", (e) => {
				const target = $(e.currentTarget).data("more");
				this.state.more_view = target;
				if (target === "inventory" && !this.state.inventory) {
					this.load_inventory();
					return;
				}
				this.render_body();
			});
			return;
		}

		this.$body.find(".fg-more-back-btn").on("click", () => {
			this.state.more_view = "menu";
			this.render_body();
		});

		if (this.state.more_view === "inventory") {
			this.$body.find(".fg-inventory-search").on("input", (e) => {
				this.state.inventory_search = $(e.currentTarget).val();
				this.$body.find(".fg-inventory-list").html(this.render_inventory_rows_html());
			});
		}
	}

	// -------------------------------------------------------------------
	// Detail / picking view -- unchanged content and behaviour (including
	// the qty stepper below), only the back button now returns to
	// return_section instead of always going to Inicio.
	// -------------------------------------------------------------------
	render_detail_html() {
		const detail = this.state.detail;
		if (!detail) return "";

		const is_open = detail.docstatus === 0;
		const rows = detail.rows || [];
		const picked_lines = rows.filter((r) => flt(r.qty_alistada) >= flt(r.qty_solicitada)).length;
		const pct = rows.length ? Math.round((picked_lines / rows.length) * 100) : 0;
		const pedido_label = detail.commercial_name || detail.sales_order || detail.name;

		const started_banner = detail.fg_started_by
			? `
				<div class="fg-started-banner">
					${icon("user")} ${__("Alistamiento iniciado por")} <b>${frappe.utils.escape_html(detail.fg_started_by)}</b>
					<span class="fg-started-banner-time">${
						detail.fg_started_on ? frappe.datetime.str_to_user(detail.fg_started_on) : ""
					}</span>
				</div>
			`
			: "";

		const done_banner = !is_open
			? `<div class="fg-done-banner">${icon("circle-check-big")} ${__("Pedido alistado correctamente")}</div>`
			: "";

		const subtitle_parts = [];
		if (detail.customer) subtitle_parts.push(frappe.utils.escape_html(detail.customer));
		if (detail.parent_warehouse) subtitle_parts.push(frappe.utils.escape_html(detail.parent_warehouse));

		this.last_changed_row = null;

		return `
			<div class="fg-detail-top">
				<div>
					<button type="button" class="fg-back-btn">${icon("arrow-left")} ${__("Volver a pedidos")}</button>
					<div class="fg-detail-heading">
						<div class="fg-detail-title">${__("PEDIDO")} #${frappe.utils.escape_html(pedido_label)}</div>
						<div class="fg-detail-subtitle">${subtitle_parts.join(" · ") || "—"}</div>
					</div>
				</div>
				${started_banner}${done_banner}
			</div>

			<div class="fg-progress-card">
				<div>
					<div class="fg-progress-card-head">${__("Progreso del alistamiento")}</div>
					<div class="fg-progress-card-count">${picked_lines} / ${rows.length} <small>${__("productos")}</small></div>
				</div>
				<div class="fg-progress-card-bar">
					<div class="fg-progress-track"><div class="fg-progress-fill" style="--fg-progress-width:${pct}%"></div></div>
				</div>
				<div class="fg-progress-card-pct">${pct}%</div>
			</div>

			<div class="fg-item-cards">${rows.map((row) => this.render_item_card(row, is_open)).join("")}</div>

			${
				is_open
					? `
				<div class="fg-finish-bar">
					<button type="button" class="fg-finish-btn">${icon("circle-check-big")} ${__("TERMINAR ALISTAMIENTO")}</button>
					<div class="fg-finish-note">${icon("lock", "fg-icon-sm")} ${__(
							"Se validarán cantidades y faltantes antes de finalizar"
					  )}</div>
				</div>
			`
					: ""
			}
		`;
	}

	bind_detail_events() {
		const is_open = this.state.detail && this.state.detail.docstatus === 0;
		this.$body.find(".fg-back-btn").on("click", () => this.back_to_list());
		if (is_open) {
			this.bind_item_card_events();
			this.$body.find(".fg-finish-btn").on("click", () => this.finish_picking());
		}
	}

	render_item_card(row, is_open) {
		const solicitada = flt(row.qty_solicitada);
		const disponible = flt(row.qty_disponible);
		const alistada = flt(row.qty_alistada);
		const uom = row.uom || "";
		const complete = solicitada > 0 ? alistada >= solicitada : true;
		const shortfall = Math.max(solicitada - alistada, 0);
		const disponible_short = disponible < solicitada;
		const flash = row.row_name === this.last_changed_row ? "fg-row-flash" : "";

		let status_html;
		if (row.has_shortage_report) {
			status_html = `<span class="fg-status-pill fg-status-pill--warn">${icon("triangle-alert", "fg-icon-sm")} ${__(
				"Faltante reportado"
			)}</span>`;
		} else if (complete) {
			status_html = `<span class="fg-status-pill fg-status-pill--ok">${icon("circle-check-big", "fg-icon-sm")} ${__(
				"Completo"
			)}</span>`;
		} else if (is_open) {
			status_html = `
				<span class="fg-status-pill fg-status-pill--warn">${icon("triangle-alert", "fg-icon-sm")} ${__("Faltan")} ${format_qty(
				shortfall
			)} ${uom}</span>
				<button type="button" class="fg-btn fg-btn--danger-sm fg-report-shortage-btn">${__("REPORTAR FALTANTE")}</button>
			`;
		} else {
			status_html = `<span class="fg-status-pill fg-status-pill--warn">${icon("triangle-alert", "fg-icon-sm")} ${__(
				"Faltan"
			)} ${format_qty(shortfall)} ${uom}</span>`;
		}

		const qty_cols = `
			<div class="fg-qty-cols-mobile">
				<div class="fg-qty-col">
					<div class="fg-qty-col-label">${__("Solicitado")}</div>
					<div class="fg-qty-col-value">${format_qty(solicitada)}</div>
					<div class="fg-qty-col-unit">${uom}</div>
				</div>
				<div class="fg-qty-col">
					<div class="fg-qty-col-label">${__("Disponible")}</div>
					<div class="fg-qty-col-value ${disponible_short ? "fg-qty-col-value--short" : "fg-qty-col-value--ok"}">${format_qty(
			disponible
		)}</div>
					<div class="fg-qty-col-unit">${uom}</div>
				</div>
				<div class="fg-qty-col fg-qty-col-alistado-desktop-only">
					<div class="fg-qty-col-label">${__("Alistado")}</div>
					<div class="fg-qty-col-value">${format_qty(alistada)}</div>
					<div class="fg-qty-col-unit">${uom}</div>
				</div>
			</div>
		`;

		const control_html = is_open
			? `
				<div class="fg-stepper-label-mobile">${__("Alistado")}</div>
				<div class="fg-stepper">
					<button type="button" class="fg-stepper-btn fg-stepper-minus" ${alistada <= 0 ? "disabled" : ""}>${icon(
					"minus"
			  )}</button>
					<input type="number" inputmode="decimal" class="fg-stepper-input" value="${alistada}" min="0">
					<button type="button" class="fg-stepper-btn fg-stepper-plus" ${
						solicitada > 0 && alistada >= solicitada ? "disabled" : ""
					}>${icon("plus")}</button>
				</div>
			`
			: `<div class="fg-item-readonly"><small>${__("Alistado")}</small>${format_qty(alistada)} ${uom}</div>`;

		return `
			<div class="fg-item-card ${flash}" data-row="${frappe.utils.escape_html(row.row_name)}">
				<div class="fg-item-identity">
					<div class="fg-item-thumb">${icon("package")}</div>
					<div>
						<div class="fg-item-name">${frappe.utils.escape_html(row.item_name || row.item_code)}</div>
						<div class="fg-item-code">${frappe.utils.escape_html(row.item_code)}</div>
						${uom ? `<span class="fg-item-uom">${frappe.utils.escape_html(uom)}</span>` : ""}
					</div>
				</div>
				${qty_cols}
				${control_html}
				<div class="fg-item-status">${status_html}</div>
			</div>
		`;
	}

	bind_item_card_events() {
		this.$body.find(".fg-item-card").each((i, el) => {
			const $card = $(el);
			const row_name = $card.data("row");
			const $input = $card.find(".fg-stepper-input");

			$card.find(".fg-stepper-minus").on("click", () => {
				const current = this.desired_qty.has(row_name) ? this.desired_qty.get(row_name) : flt($input.val());
				this.request_picked_qty(row_name, Math.max(current - 1, 0), $card);
			});
			$card.find(".fg-stepper-plus").on("click", () => {
				const current = this.desired_qty.has(row_name) ? this.desired_qty.get(row_name) : flt($input.val());
				this.request_picked_qty(row_name, current + 1, $card);
			});
			$input.on("change", () => {
				this.request_picked_qty(row_name, Math.max(flt($input.val()), 0), $card);
			});
			$card.find(".fg-report-shortage-btn").on("click", () => {
				this.open_report_shortage_dialog(row_name, $card);
			});
		});
	}

	// -------------------------------------------------------------------
	// Qty-stepper coalescing -- unchanged (approved separately). A burst of
	// +/- clicks on one row must never be silently dropped and must never
	// queue up N serial requests either. At most one set_picked_qty request
	// is in flight per row_name at any time; extra clicks that land while
	// that request is in flight only update desired_qty (and the on-screen
	// input, optimistically) -- they do not start a second request. When
	// the in-flight request resolves, we compare what THIS request just
	// sent against the *current* desired_qty: if the user asked for
	// something else in the meantime, we immediately re-sync straight to
	// that latest value (skipping whatever intermediate values were
	// requested and abandoned along the way). Only once server and
	// desired_qty agree do we do the one full load_detail() refresh. Other
	// rows are completely unaffected -- the lock is per row_name, never
	// global.
	// -------------------------------------------------------------------
	request_picked_qty(row_name, qty, $card) {
		qty = Math.max(flt(qty), 0);
		this.desired_qty.set(row_name, qty);
		this.update_stepper_display(row_name, qty, $card);

		if (this.inflight_rows.has(row_name)) {
			// A request for this row is already in flight; it will pick up
			// this newer desired_qty itself once it resolves (see below).
			return;
		}
		this.sync_row(row_name, $card);
	}

	sync_row(row_name, $card) {
		const sent_qty = this.desired_qty.get(row_name);
		if (sent_qty === undefined) return;

		this.inflight_rows.add(row_name);
		$card.addClass("fg-row-syncing");

		this.call("set_picked_qty", { name: this.state.pick_list, row_name: row_name, qty: sent_qty })
			.then(() => {
				this.inflight_rows.delete(row_name);
				const latest_desired = this.desired_qty.get(row_name);
				// Compare against what THIS request just sent, not against the
				// server's (possibly rounded) echo -- otherwise a field
				// precision rounding on save could make this loop forever
				// resending the same value.
				if (latest_desired !== undefined && flt(latest_desired) !== flt(sent_qty)) {
					// The user asked for something new while this request was
					// in flight -- chase the newest value directly, no
					// intermediate step.
					this.sync_row(row_name, $card);
					return;
				}
				this.desired_qty.delete(row_name);
				$card.removeClass("fg-row-syncing");
				this.last_changed_row = row_name;
				this.load_detail(this.state.pick_list);
			})
			.catch(() => {
				// Server rejected this value (over the limit, doc no longer
				// open, concurrent edit, etc). Drop the optimistic guess for
				// this row entirely and reload the real state from the
				// server -- never leave a false optimistic number on screen.
				// This is the one case where controls are briefly blocked,
				// via fg-row-busy's pointer-events:none, until that reload
				// finishes and re-renders the row from scratch.
				this.inflight_rows.delete(row_name);
				this.desired_qty.delete(row_name);
				$card.removeClass("fg-row-syncing").addClass("fg-row-busy");
				this.last_changed_row = row_name;
				this.load_detail(this.state.pick_list);
			});
	}

	// Optimistic, local-only UI update: reflects the requested qty on the
	// input immediately, and soft-caps the buttons using the qty_solicitada
	// already present in state.detail (no new data fetched for this). The
	// server-side check in set_picked_qty (over_delivery_receipt_allowance)
	// remains the real authority -- this is only a visual courtesy so the
	// stepper doesn't invite obviously-invalid clicks.
	update_stepper_display(row_name, qty, $card) {
		$card.find(".fg-stepper-input").val(qty);
		$card.find(".fg-stepper-minus").prop("disabled", qty <= 0);

		const row = ((this.state.detail && this.state.detail.rows) || []).find((r) => r.row_name === row_name);
		const solicitada = row ? flt(row.qty_solicitada) : 0;
		$card.find(".fg-stepper-plus").prop("disabled", solicitada > 0 && qty >= solicitada);
	}

	open_report_shortage_dialog(row_name, $card) {
		const row = (this.state.detail.rows || []).find((r) => r.row_name === row_name);
		if (!row) return;

		frappe.model.with_doctype("Reporte de Faltante", () => {
			const shortage_field = frappe
				.get_meta("Reporte de Faltante")
				.fields.find((f) => f.fieldname === "shortage_reason");
			const reason_options = (shortage_field.options || "")
				.split("\n")
				.map((o) => o.trim())
				.filter(Boolean);

			const dialog = new frappe.ui.Dialog({
				title: __("Reportar faltante"),
				fields: [
					{ fieldtype: "HTML", fieldname: "summary_html" },
					{
						fieldtype: "Select",
						fieldname: "shortage_reason",
						label: __("Motivo"),
						options: reason_options,
						reqd: 1,
					},
					{
						fieldtype: "Float",
						fieldname: "qty_disponible",
						label: __("Cantidad disponible / encontrada"),
						default: row.qty_alistada,
						reqd: 1,
					},
					{
						fieldtype: "Small Text",
						fieldname: "resolution_note",
						label: __("Nota (opcional)"),
					},
				],
				primary_action_label: __("REPORTAR FALTANTE"),
				primary_action: (values) => {
					dialog.disable_primary_action();
					this.call("report_shortage", {
						pick_list: this.state.pick_list,
						row_name: row_name,
						qty_disponible: values.qty_disponible,
						shortage_reason: values.shortage_reason,
						resolution_note: values.resolution_note,
					})
						.then(() => {
							dialog.hide();
							frappe.show_alert({ message: "✓ " + __("Faltante reportado"), indicator: "green" });
							this.last_changed_row = row_name;
							this.load_detail(this.state.pick_list);
						})
						.catch(() => {
							dialog.enable_primary_action();
						});
				},
			});

			dialog.$wrapper.addClass("fg-shortage-dialog");

			const render_summary = () => {
				const found = flt(dialog.get_value("qty_disponible"));
				const faltante = Math.max(flt(row.qty_solicitada) - found, 0);
				dialog.fields_dict.summary_html.$wrapper.html(`
					<div class="fg-shortage-summary">
						<div class="fg-shortage-summary-item">${frappe.utils.escape_html(row.item_name || row.item_code)}</div>
						<div>
							<div class="fg-shortage-summary-field-label">${__("Solicitado")}</div>
							<div class="fg-shortage-summary-field-value">${format_qty(row.qty_solicitada)} ${row.uom || ""}</div>
						</div>
						<div>
							<div class="fg-shortage-summary-field-label">${__("Encontrado")}</div>
							<div class="fg-shortage-summary-field-value">${format_qty(found)} ${row.uom || ""}</div>
						</div>
						<div>
							<div class="fg-shortage-summary-field-label">${__("Faltante")}</div>
							<div class="fg-shortage-summary-field-value fg-shortage-summary-field-value--danger">${format_qty(
								faltante
							)} ${row.uom || ""}</div>
						</div>
					</div>
				`);
			};

			render_summary();
			dialog.fields_dict.qty_disponible.df.onchange = render_summary;
			dialog.show();
		});
	}

	finish_picking() {
		if (this.busy) return;
		frappe.confirm(__("¿Confirmas que terminaste de alistar este pedido?"), () => {
			this.busy = true;
			const $btn = this.$body.find(".fg-finish-btn").prop("disabled", true).addClass("fg-btn--loading");
			this.call("finish_picking", { name: this.state.pick_list })
				.then(() => {
					frappe.show_alert({ message: "✓ " + __("Pedido alistado correctamente"), indicator: "green" });
					this.state.view = "list";
					this.state.pick_list = null;
					this.state.detail = null;
					this.state.section = this.state.return_section || "home";
					return this.load_queue();
				})
				.catch(() => {
					// Server already showed the exact validation/concurrency message;
					// reload so the screen reflects the current true state.
					return this.load_detail(this.state.pick_list);
				})
				.finally(() => {
					this.busy = false;
					$btn.prop("disabled", false).removeClass("fg-btn--loading");
				});
		});
	}
};

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, no server calls, no state.
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

function format_today_es() {
	const d = new Date();
	try {
		const label = d.toLocaleDateString("es-CO", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
		return label.charAt(0).toUpperCase() + label.slice(1);
	} catch (e) {
		return frappe.datetime.str_to_user(frappe.datetime.nowdate());
	}
}

function queue_progress_markup(bucket) {
	if (bucket === "pendientes") {
		return `
			<div class="fg-progress-label-row"><span>${__("Progreso")}</span><span class="fg-progress-pct">0%</span></div>
			<div class="fg-progress-track"><div class="fg-progress-fill" style="--fg-progress-width:0%"></div></div>
		`;
	}
	if (bucket === "listos") {
		return `
			<div class="fg-progress-label-row"><span>${__("Progreso")}</span><span class="fg-progress-pct">100%</span></div>
			<div class="fg-progress-track"><div class="fg-progress-fill" style="--fg-progress-width:100%"></div></div>
		`;
	}
	// en_alistamiento / con_faltantes: get_queue() does not return per-order
	// qty_alistada/qty_solicitada, so no numeric percentage is invented here --
	// only an indeterminate bar + status label. The exact % is shown in the
	// detail view, where get_pick_list() provides real per-line quantities.
	const label = bucket === "con_faltantes" ? __("Con faltantes") : __("En progreso");
	return `
		<div class="fg-progress-label-row"><span>${__("Progreso")}</span><span class="fg-progress-pct">${label}</span></div>
		<div class="fg-progress-track"><div class="fg-progress-fill fg-progress-fill--indeterminate"></div></div>
	`;
}

function flt(v) {
	return frappe.utils.flt ? frappe.utils.flt(v) : parseFloat(v) || 0;
}

function format_qty(v) {
	const n = flt(v);
	return Number.isInteger(n) ? String(n) : n.toFixed(2);
}
