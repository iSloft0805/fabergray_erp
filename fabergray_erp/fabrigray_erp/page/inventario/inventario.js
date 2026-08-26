// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["inventario"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Inventario"),
		single_column: true,
	});
	new fabergray_erp.Inventario(page);
};

// Commit 22.5 -- Page Inventario: dashboard + lista + detalle, exclusivamente
// sobre los 3 endpoints de solo lectura ya cerrados en api/inventario.py
// (Commit 22.4). Esta Page es UI solamente -- no reimplementa ninguna regla
// de autorización: el acceso a la ruta entera lo decide inventario.json's
// roles (Bodega, Jefe de Bodega, System Manager), y cada lectura sigue
// pasando por el mismo frappe.has_permission()/check_permission() real del
// servidor.
//
// Sin ninguna acción de escritura: no hay ajustar stock, entrada, salida,
// Stock Reconciliation, Stock Entry, editar Item, cambiar UOM ni cambiar
// grupo -- ni un solo botón de este archivo llama a nada que no sea
// get_inventory_summary()/get_inventory_items()/get_inventory_item_detail().
fabergray_erp.Inventario = class Inventario {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.inventario.";
		this.busy = false;

		this.summary = null;

		// Lista (view: "dashboard").
		this.list_filter = "all"; // "all" | "active" | "disabled" | "with_stock" | "out_of_stock"
		this.list_search = "";
		this.list_page = 1;
		this.list_rows = [];
		this.list_total = 0;
		this._search_debounce = null;
		this._list_request_seq = 0; // descarta respuestas que llegan fuera de orden

		// Detalle (view: "detail").
		this.detail = null;
		this.detail_code = null;

		this.state = { view: "dashboard" };

		this.$app = $('<div class="fg-shell fg-inventario">').appendTo(this.page.body);
		this.render_shell();
		this.load_dashboard();
	}

	// -------------------------------------------------------------------
	// Thin API wrapper -- frappe.call() no devuelve un Promise real (ver
	// page/bodega/bodega.js y page/cotizaciones/cotizaciones.js, cuyos
	// propios comentarios documentan el bug y su fix; page/ventas/ventas.js
	// nunca fue corregido y sigue arrastrando el bug original -- no se
	// copia ese patrón aquí, y tampoco el bug histórico equivalente de
	// encadenar `.finally()` directamente sobre el jQuery Deferred que
	// `frappe.call()` retorna: envolver en `new Promise(...)` es lo que
	// permite que cada `.then()/.catch()/.finally()` de este archivo se
	// comporte como un Promise nativo de verdad, no como el Deferred.
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
	// Shell: header (logo, título, usuario, refresh) fijo en todas las vistas.
	// -------------------------------------------------------------------
	render_shell() {
		const fullname = frappe.session.user_fullname || frappe.session.user;
		this.$app.html(`
			<div class="fg-header">
				<div class="fg-header-brand">
					<span class="fg-header-logo">FABRIGRAY</span>
					<span class="fg-header-sep">|</span>
					<span class="fg-header-title">${__("INVENTARIO")}</span>
				</div>
				<div class="fg-header-user">
					<div class="fg-header-user-info">
						<div class="fg-header-user-name">${frappe.utils.escape_html(fullname)}</div>
						<div class="fg-header-user-role">${__("Inventario")}</div>
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
			else if (this.state.view === "detail" && this.detail_code) this.open_detail(this.detail_code);
		});
	}

	// Refresh siempre vuelve a habilitarse -- cada ruta de carga de abajo
	// llega a set_busy(false) vía .finally(), éxito o error por igual,
	// nunca queda deshabilitado de forma permanente.
	set_busy(is_busy) {
		this.busy = !!is_busy;
		this.$app.find(".fg-refresh-btn").prop("disabled", this.busy);
		this.$app.toggleClass("fg-loading", !!is_busy);
	}

	// =====================================================================
	// Dashboard + Lista
	// =====================================================================
	load_dashboard() {
		this.set_busy(true);
		this.state.view = "dashboard";
		this.render_skeleton_dashboard();
		return this.call("get_inventory_summary")
			.then((summary) => {
				this.summary = summary;
				return this.load_list();
			})
			.then(() => this.render_dashboard())
			.catch(() => {
				// frappe.call() ya mostró su propio diálogo de error real.
			})
			.finally(() => this.set_busy(false));
	}

	// "Con stock" no tiene un valor `status` nativo en get_inventory_items()
	// (solo "all"/"active"/"disabled"/"out_of_stock", Commit 22.4) --
	// api/inventario.py no se modificó para esto (fuera de alcance de este
	// commit, reportado explícitamente en vez de tocar el backend). En vez
	// de eso, se trae con status="all" y se filtra/pagina en el cliente --
	// mismo patrón "un fetch, filtrar+paginar en JS" que ya usa
	// page/clientes/clientes.js para su propia pestaña "Datos incompletos",
	// aplicado solo a esta pestaña porque es la única que el servidor no
	// puede expresar directamente. WITH_STOCK_FETCH_CAP va con margen
	// amplio sobre el total real de productos (~2785), no sobre el
	// subconjunto con stock -- mismo motivo documentado en clientes.js: un
	// cap más chico que el total examinaría solo una ventana alfabética
	// parcial, no "los primeros N con stock" reales.
	//
	// "Agotados" SÍ tiene soporte nativo (status="out_of_stock",
	// Commit 22.4) -- se usa el filtro del servidor directamente, sin
	// traer todo el catálogo.
	load_list() {
		const seq = ++this._list_request_seq;

		if (this.list_filter === "with_stock") {
			return this.call("get_inventory_items", {
				txt: this.list_search,
				status: "all",
				start: 0,
				page_length: WITH_STOCK_FETCH_CAP,
			}).then((res) => {
				if (seq !== this._list_request_seq) return; // respuesta obsoleta, descartada
				const with_stock = (res.items || []).filter((i) => (i.total_actual_qty || 0) > 0);
				const paged = paginate(with_stock, this.list_page, PAGE_SIZE);
				this.list_page = paged.page;
				this.list_rows = paged.page_items;
				this.list_total = paged.total;
			});
		}

		return this.call("get_inventory_items", {
			txt: this.list_search,
			status: this.list_filter,
			start: (this.list_page - 1) * PAGE_SIZE,
			page_length: PAGE_SIZE,
		}).then((res) => {
			if (seq !== this._list_request_seq) return;
			this.list_rows = res.items || [];
			this.list_total = res.total || 0;
		});
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
			<div class="fg-inv-list-section">${this.render_list_section()}</div>
		`);
		this.bind_list_section_events();
	}

	// Los 4 KPI puramente informativos, sin filtro asociado al hacer clic
	// (mismo patrón que page/facturacion/facturacion.js y
	// page/clientes/clientes.js) -- el filtrado de la lista pasa
	// exclusivamente por las Tabs de abajo. "Stock bajo" muestra el texto
	// explícito "Pendiente de configurar" -- nunca un número -- porque
	// get_inventory_summary() devuelve low_stock=null +
	// low_stock_status="not_configured" (sin reorder levels todavía,
	// Commit 22.6).
	render_kpis() {
		const s = this.summary || {};
		const cards = [
			{ key: "references", label: __("Referencias"), i: "package", mod: "inv-referencias" },
			{ key: "total_stock", label: __("Stock total"), i: "boxes", mod: "inv-stock-total" },
			{ key: "out_of_stock", label: __("Agotados"), i: "triangle-alert", mod: "inv-agotados" },
			{ key: "low_stock", label: __("Stock bajo"), i: "gauge", mod: "inv-stock-bajo" },
		];

		const html = cards
			.map((c) => {
				const is_pending = c.key === "low_stock" && s.low_stock === null && s.low_stock_status === "not_configured";
				const number_html = is_pending
					? `<div class="fg-kpi-number fg-kpi-number--pending">${__("Pendiente de configurar")}</div>`
					: `<div class="fg-kpi-number">${s[c.key] ?? 0}</div>`;
				return `
					<div class="fg-kpi fg-kpi--${c.mod}">
						<div class="fg-kpi-icon">${icon(c.i)}</div>
						${number_html}
						<div class="fg-kpi-label">${c.label}</div>
					</div>
				`;
			})
			.join("");

		return `<div class="fg-kpis fg-kpis--inventario">${html}</div>`;
	}

	// -- Lista: buscador (server-side, debounced) + tabs + tarjetas + paginación --

	render_list_section() {
		const tabs = ["all", "active", "disabled", "with_stock", "out_of_stock"];
		const tab_meta = {
			all: { label: __("Todos") },
			active: { label: __("Activos") },
			disabled: { label: __("Inactivos") },
			with_stock: { label: __("Con stock") },
			out_of_stock: { label: __("Agotados") },
		};
		const tabs_html = tabs
			.map(
				(key) =>
					`<button type="button" class="fg-inv-tab ${
						this.list_filter === key ? "is-active" : ""
					}" data-filter="${key}">${tab_meta[key].label}</button>`
			)
			.join("");

		return `
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Productos")}</div>
			</div>
			<div class="fg-inv-toolbar">
				<div class="fg-inv-search-wrap">
					${icon("search", "fg-inv-search-icon")}
					<input type="text" class="fg-inv-search-input" placeholder="${__(
						"Buscar por código o nombre..."
					)}" value="${frappe.utils.escape_html(this.list_search || "")}">
				</div>
			</div>
			<div class="fg-inv-tabs">${tabs_html}</div>
			<div class="fg-inv-cards">${this.render_cards_html()}</div>
			<div class="fg-inv-pagination">${this.render_pagination_html()}</div>
		`;
	}

	render_cards_html() {
		if (!this.list_rows.length) {
			return `<div class="fg-empty">${__("No hay productos que coincidan.")}</div>`;
		}
		return this.list_rows.map((it) => this.render_item_card(it)).join("");
	}

	render_item_card(it) {
		const status = it.disabled
			? { label: __("Inactivo"), mod: "inv-inactivo" }
			: { label: __("Activo"), mod: "inv-activo" };

		const stock_qty = it.total_actual_qty || 0;
		const price_html =
			it.selling_rate != null
				? frappe.format(it.selling_rate, { fieldtype: "Currency" })
				: `<span class="fg-inv-empty">${__("Sin precio")}</span>`;

		return `
			<div class="fg-inv-card" data-code="${frappe.utils.escape_html(it.item_code)}">
				<div class="fg-inv-card-top">
					<div>
						<div class="fg-inv-card-name">${frappe.utils.escape_html(it.item_name)}</div>
						<div class="fg-inv-card-code">${frappe.utils.escape_html(it.item_code)}</div>
					</div>
					<span class="fg-badge fg-badge--${status.mod}">${status.label}</span>
				</div>
				<div class="fg-inv-card-meta">
					<span>${__("Stock")}: <strong>${format_qty(stock_qty)} ${frappe.utils.escape_html(it.stock_uom || "")}</strong></span>
					<span>${__("Grupo")}: ${frappe.utils.escape_html(it.item_group || "")}</span>
					<span>${__("Precio")}: ${price_html}</span>
				</div>
				<div class="fg-inv-card-actions">
					<button type="button" class="fg-btn fg-btn--ghost fg-inv-card-detail" data-code="${frappe.utils.escape_html(
						it.item_code
					)}">${icon("eye", "fg-icon-sm")} ${__("VER DETALLE")}</button>
				</div>
			</div>
		`;
	}

	render_pagination_html() {
		const total = this.list_total;
		if (!total) return "";
		const page_count = Math.max(Math.ceil(total / PAGE_SIZE), 1);
		const start = (this.list_page - 1) * PAGE_SIZE + 1;
		const end = Math.min(this.list_page * PAGE_SIZE, total);
		return `
			<div class="fg-inv-pagination-info">${__("Mostrando {0} a {1} de {2} productos", [start, end, total])}</div>
			<div class="fg-inv-pagination-controls">
				<button type="button" class="fg-inv-pagination-btn fg-inv-pagination-prev" ${
					this.list_page <= 1 ? "disabled" : ""
				}>${icon("chevron-left")}</button>
				<span class="fg-inv-pagination-page">${this.list_page}</span>
				<button type="button" class="fg-inv-pagination-btn fg-inv-pagination-next" ${
					this.list_page >= page_count ? "disabled" : ""
				}>${icon("chevron-right")}</button>
			</div>
		`;
	}

	// Solo reemplaza tarjetas + paginación -- nunca el input de búsqueda ni
	// las tabs, para no perder foco/estado mientras el usuario escribe.
	refresh_list_cards() {
		this.set_busy(true);
		return this.load_list()
			.then(() => {
				this.$body.find(".fg-inv-cards").html(this.render_cards_html());
				this.$body.find(".fg-inv-pagination").html(this.render_pagination_html());
			})
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	bind_list_section_events() {
		this.$body.find(".fg-inv-search-input").on("input", (e) => {
			const val = $(e.currentTarget).val();
			clearTimeout(this._search_debounce);
			this._search_debounce = setTimeout(() => {
				this.list_search = val;
				this.list_page = 1;
				this.refresh_list_cards();
			}, 300);
		});

		this.$body.find(".fg-inv-tab").on("click", (e) => {
			this.list_filter = $(e.currentTarget).data("filter");
			this.list_page = 1;
			this.$body.find(".fg-inv-tab").removeClass("is-active");
			$(e.currentTarget).addClass("is-active");
			this.refresh_list_cards();
		});

		// Delegado sobre el contenedor estable -- sobrevive al .html() de
		// refresh_list_cards() sin necesitar re-bind en cada búsqueda/página.
		this.$body.find(".fg-inv-cards").on("click", ".fg-inv-card-detail", (e) => {
			e.stopPropagation();
			this.open_detail($(e.currentTarget).data("code"));
		});

		this.$body.find(".fg-inv-pagination").on("click", ".fg-inv-pagination-prev", () => {
			this.list_page = Math.max(this.list_page - 1, 1);
			this.refresh_list_cards();
		});
		this.$body.find(".fg-inv-pagination").on("click", ".fg-inv-pagination-next", () => {
			this.list_page = this.list_page + 1;
			this.refresh_list_cards();
		});
	}

	// =====================================================================
	// Detalle ("VER DETALLE") -- SPA: nunca un reload completo, solo
	// re-renderiza this.$body.
	// =====================================================================
	open_detail(item_code) {
		if (!item_code) return;
		this.detail_code = item_code;
		this.detail = null;
		this.state.view = "detail";
		this.set_busy(true);
		this.render_detail_skeleton();

		this.call("get_inventory_item_detail", { item_code: item_code })
			.then((detail) => {
				this.detail = detail;
				this.render_detail();
			})
			.catch(() => this.back_to_dashboard())
			.finally(() => this.set_busy(false));
	}

	back_to_dashboard() {
		this.detail = null;
		this.detail_code = null;
		this.load_dashboard();
	}

	render_detail_skeleton() {
		this.$body.html(`
			<div class="fg-np-header">
				<button type="button" class="fg-np-back">${icon("arrow-left")} ${__("Volver")}</button>
				<div class="fg-np-title">${__("Detalle de producto")}</div>
			</div>
			<div class="fg-skeleton fg-inv-detail-skeleton"></div>
			<div class="fg-skeleton fg-inv-detail-skeleton"></div>
		`);
		this.$body.find(".fg-np-back").on("click", () => this.back_to_dashboard());
	}

	// Todos los campos mostrados vienen tal cual del backend
	// (get_inventory_item_detail(), Commit 22.4) -- nada inventado aquí:
	// stock_by_warehouse es exactamente la lista de Bin {warehouse,
	// actual_qty, ...} que el endpoint ya devuelve, recent_movements es
	// exactamente su propia lista de Stock Ledger Entry
	// {posting_date, posting_time, warehouse, actual_qty, voucher_type,
	// voucher_no}.
	render_detail() {
		const d = this.detail;
		const status = d.disabled
			? { label: __("Inactivo"), mod: "inv-inactivo" }
			: { label: __("Activo"), mod: "inv-activo" };
		const price_html =
			d.selling_rate != null
				? frappe.format(d.selling_rate, { fieldtype: "Currency" })
				: `<span class="fg-inv-empty">${__("Sin precio")}</span>`;

		this.$body.html(`
			<div class="fg-np-header">
				<button type="button" class="fg-np-back">${icon("arrow-left")} ${__("Volver")}</button>
				<div class="fg-np-title">${__("Detalle de producto")}</div>
			</div>

			<div class="fg-inv-detail-card">
				<div class="fg-inv-detail-top">
					<div>
						<div class="fg-inv-detail-name">${frappe.utils.escape_html(d.item_name)}</div>
						<div class="fg-inv-detail-code">${frappe.utils.escape_html(d.item_code)}</div>
					</div>
					<span class="fg-badge fg-badge--${status.mod}">${status.label}</span>
				</div>

				<div class="fg-inv-detail-grid">
					<div class="fg-inv-detail-field">
						<div class="fg-inv-detail-label">${__("Item Group")}</div>
						<div>${frappe.utils.escape_html(d.item_group || "—")}</div>
					</div>
					<div class="fg-inv-detail-field">
						<div class="fg-inv-detail-label">${__("UOM")}</div>
						<div>${frappe.utils.escape_html(d.stock_uom || "—")}</div>
					</div>
					<div class="fg-inv-detail-field">
						<div class="fg-inv-detail-label">${__("Precio Standard Selling")}</div>
						<div>${price_html}</div>
					</div>
					<div class="fg-inv-detail-field">
						<div class="fg-inv-detail-label">${__("Stock total")}</div>
						<div><strong>${format_qty(d.total_stock)} ${frappe.utils.escape_html(d.stock_uom || "")}</strong></div>
					</div>
				</div>
			</div>

			<div class="fg-inv-section">
				<div class="fg-inv-section-title">${__("Stock por almacén")}</div>
				${this.render_warehouse_rows(d.stock_by_warehouse)}
			</div>

			<div class="fg-inv-section">
				<div class="fg-inv-section-title">${__("Movimientos recientes")}</div>
				${this.render_movement_rows(d.recent_movements)}
			</div>
		`);

		this.$body.find(".fg-np-back").on("click", () => this.back_to_dashboard());
	}

	render_warehouse_rows(rows) {
		if (!rows || !rows.length) {
			return `<div class="fg-inv-empty">${__("Sin stock registrado en ningún almacén.")}</div>`;
		}
		return `
			<div class="fg-inv-warehouse-rows">
				${rows
					.map(
						(r) => `
					<div class="fg-inv-warehouse-row">
						<span>${frappe.utils.escape_html(r.warehouse)}</span>
						<span class="fg-inv-warehouse-row-qty">${format_qty(r.actual_qty)}</span>
					</div>
				`
					)
					.join("")}
			</div>
		`;
	}

	render_movement_rows(rows) {
		if (!rows || !rows.length) {
			return `<div class="fg-inv-empty">${__("Sin movimientos registrados todavía.")}</div>`;
		}
		return `
			<div class="fg-inv-movement-rows">
				${rows
					.map((m) => {
						const qty = m.actual_qty || 0;
						const qty_class = qty > 0 ? "is-positive" : qty < 0 ? "is-negative" : "";
						const qty_sign = qty > 0 ? "+" : "";
						const when = m.posting_date
							? `${frappe.datetime.str_to_user(m.posting_date)} ${(m.posting_time || "").split(".")[0]}`
							: "—";
						return `
						<div class="fg-inv-movement-row">
							<div>
								<div>${frappe.utils.escape_html(m.warehouse || "")}</div>
								<div class="fg-inv-movement-when">${when}</div>
							</div>
							<div class="fg-inv-movement-qty ${qty_class}">${qty_sign}${format_qty(qty)}</div>
							<div class="fg-inv-movement-voucher">${frappe.utils.escape_html(m.voucher_type || "")} ${frappe.utils.escape_html(
							m.voucher_no || ""
						)}</div>
						</div>
					`;
					})
					.join("")}
			</div>
		`;
	}
};

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, no server calls, no state.
// Intentionally duplicated (not imported) from clientes.js/ventas.js/
// bodega.js/cotizaciones.js/facturacion.js, same reasoning as Commit 6.
// -------------------------------------------------------------------------
const PAGE_SIZE = 10;
const WITH_STOCK_FETCH_CAP = 6000; // margen amplio sobre el total real de productos (~2785) -- ver comentario en load_list()

function paginate(items, page, page_size) {
	const total = items.length;
	const page_count = Math.max(Math.ceil(total / page_size), 1);
	const safe_page = Math.min(Math.max(page || 1, 1), page_count);
	const start = (safe_page - 1) * page_size;
	return { page_items: items.slice(start, start + page_size), total, page_count, page: safe_page };
}

function format_qty(qty) {
	const n = flt(qty);
	return n % 1 === 0 ? String(n) : n.toFixed(2);
}

function flt(v) {
	const n = parseFloat(v);
	return Number.isNaN(n) ? 0 : n;
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
