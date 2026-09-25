// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["produccion"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Producción"),
		single_column: true,
	});
	new fabergray_erp.Produccion(page);
};

// Fase 28.3 -- Page Producción: dashboard operativo de SOLO LECTURA sobre
// los Work Orders que 28.2 enruta desde un Reporte de Faltante.
// - acceso a la ruta: produccion.json's roles (Producción, Jefe de
//   Producción, System Manager); cada endpoint de api/produccion.py vuelve a
//   validar rol y empresa -- ocultar la Page nunca es la frontera;
// - todo estado, nivel de materiales y cantidad llega calculado por el
//   servidor (KPIs, pestañas, tarjetas y detalle salen de la misma SQL);
//   aquí solo se formatea, nunca se suman tarjetas ni se recalcula;
// - búsqueda, pestañas, filtros y paginación son server-side;
// - solo lectura: todas las llamadas son GET y no existe ningún botón de
//   fabricar, consumir, cancelar, editar BOM ni comprar (28.4);
// - todo texto que viene del servidor pasa por esc() antes de entrar al
//   HTML.
fabergray_erp.Produccion = class Produccion {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.produccion.";
		this.busy = false;
		this.view = "dashboard"; // "dashboard" | "detail"

		this.dashboard = null;
		this.dashboard_error = false;

		this.list_tab = "todas";
		this.list_material = "";
		this.list_date = "";
		this.list_search = "";
		this.list_page = 1;
		this.list = null; // respuesta de get_production_orders
		this.list_error = false;
		this._search_debounce = null;
		this._list_request_seq = 0; // descarta respuestas fuera de orden

		this.detail = null;
		this.detail_name = null;
		this._detail_request_seq = 0;

		this.$app = $('<div class="fg-shell fg-produccion">').appendTo(this.page.body);
		this.render_shell();
		this.load_all();
	}

	// Solo GET: api/produccion.py acepta únicamente GET (lectura).
	call(method, args) {
		return new Promise((resolve, reject) => {
			frappe.call({
				method: this.method_prefix + method,
				type: "GET",
				args: args || {},
				callback: (r) => resolve(r.message),
				error: (r) => reject(r),
			});
		});
	}

	// -------------------------------------------------------------------
	// Shell
	// -------------------------------------------------------------------
	render_shell() {
		const fullname = frappe.session.user_fullname || frappe.session.user;
		this.$app.html(`
			<div class="fg-header">
				<div class="fg-header-brand">
					<span class="fg-header-logo">FABRIGRAY</span>
					<span class="fg-header-sep">|</span>
					<span class="fg-header-title">${__("PRODUCCIÓN")}</span>
				</div>
				<div class="fg-header-user">
					<div class="fg-header-user-info">
						<div class="fg-header-user-name">${esc(fullname)}</div>
						<div class="fg-header-user-role">${esc(role_label())}</div>
					</div>
					<div class="fg-header-avatar">${esc(get_initials(fullname))}</div>
					<button type="button" class="fg-refresh-btn" title="${__("Actualizar")}" aria-label="${__("Actualizar")}">${icon("refresh-cw")}</button>
				</div>
			</div>
			<div class="fg-body"></div>
		`);
		this.$body = this.$app.find(".fg-body");
		this.$app.find(".fg-refresh-btn").on("click", () => {
			if (this.busy) return;
			if (this.view === "detail" && this.detail_name) this.open_detail(this.detail_name);
			else this.load_all();
		});
	}

	set_busy(is_busy) {
		this.busy = !!is_busy;
		this.$app.find(".fg-refresh-btn").prop("disabled", this.busy);
		this.$app.toggleClass("fg-loading", this.busy);
	}

	// =====================================================================
	// Dashboard: KPIs + listado (un fetch de cada uno, en paralelo)
	// =====================================================================
	load_all() {
		this.view = "dashboard";
		this.set_busy(true);
		if (!this.dashboard) this.render_skeleton();
		return Promise.all([this.fetch_dashboard(), this.fetch_list()])
			.then(() => this.render_dashboard())
			.catch(() => this.render_dashboard())
			.finally(() => this.set_busy(false));
	}

	fetch_dashboard() {
		return this.call("get_production_dashboard")
			.then((dashboard) => {
				this.dashboard = dashboard;
				this.dashboard_error = false;
			})
			.catch(() => {
				this.dashboard_error = true;
			});
	}

	fetch_list() {
		const seq = ++this._list_request_seq;
		return this.call("get_production_orders", {
			tab: this.list_tab,
			material: this.list_material,
			date_range: this.list_date,
			search: this.list_search,
			page: this.list_page,
			page_length: PAGE_LENGTH,
		})
			.then((res) => {
				if (seq !== this._list_request_seq) return false; // respuesta obsoleta
				this.list = res;
				this.list_error = false;
				return true;
			})
			.catch(() => {
				if (seq !== this._list_request_seq) return false;
				this.list_error = true;
				return true;
			});
	}

	render_skeleton() {
		this.$body.html(`
			<div class="fg-skeleton-kpis fg-prod-skeleton-kpis">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
			<div class="fg-skeleton-cards">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
		`);
	}

	render_dashboard() {
		if (this.view !== "dashboard") return;
		this.$body.html(`
			<div class="fg-prod-intro">
				<h1 class="fg-prod-title">${__("PRODUCCIÓN")}</h1>
				<p class="fg-prod-subtitle">${__("Órdenes y necesidades de fabricación")}</p>
			</div>
			<div class="fg-prod-kpis-slot">${render_kpis_html(this.dashboard, this.dashboard_error, this.list_tab)}</div>
			${render_search_bar_html(this.list_search)}
			<div class="fg-prod-tabs" role="tablist">${render_tabs_html(this.list_tab)}</div>
			<div class="fg-prod-filters">${render_filters_html(this.list_material, this.list_date)}</div>
			<div class="fg-prod-cards">${this.render_cards_html()}</div>
			<div class="fg-prod-pagination">${render_pagination_html(this.list, this.list_error)}</div>
		`);
		this.bind_dashboard_events();
	}

	render_cards_html() {
		if (this.list_error) {
			return `
				<div class="fg-prod-error">
					<div>${__("No se pudieron cargar las órdenes de producción.")}</div>
					<button type="button" class="fg-btn fg-btn--solid-primary fg-prod-retry-list">${icon("refresh-cw", "fg-icon-sm")} ${__(
						"REINTENTAR"
					)}</button>
				</div>
			`;
		}
		if (!this.list) return `<div class="fg-skeleton-cards"><div class="fg-skeleton"></div><div class="fg-skeleton"></div></div>`;
		const items = this.list.items || [];
		if (!items.length) {
			if (this.list.search) {
				return `
					<div class="fg-search-empty">
						<strong>${__("No se encontraron resultados")}</strong>
						<div>${__("Prueba buscando por orden, producto, pedido o cliente.")}</div>
					</div>
				`;
			}
			return `<div class="fg-empty fg-prod-empty">${empty_message(this.list_tab)}</div>`;
		}
		return items.map((item) => render_order_card(item)).join("");
	}

	// Solo reemplaza tarjetas + paginación -- nunca el input, para no
	// perder foco mientras el usuario escribe. Los KPIs no dependen de los
	// filtros.
	refresh_list() {
		this.$body.find(".fg-prod-cards").html(`<div class="fg-skeleton-cards"><div class="fg-skeleton"></div><div class="fg-skeleton"></div></div>`);
		this.$body.find(".fg-prod-pagination").html("");
		return this.fetch_list().then((is_current) => {
			if (!is_current || this.view !== "dashboard") return;
			this.$body.find(".fg-prod-cards").html(this.render_cards_html());
			this.$body.find(".fg-prod-pagination").html(render_pagination_html(this.list, this.list_error));
		});
	}

	set_tab(tab) {
		if (!tab || tab === this.list_tab || !TABS.some((t) => t.key === tab)) return;
		this.list_tab = tab;
		this.list_page = 1;
		this.$body.find(".fg-prod-tab").each((_i, el) => {
			const active = $(el).attr("data-tab") === tab;
			$(el).toggleClass("is-active", active).attr("aria-selected", active ? "true" : "false");
		});
		this.$body.find(".fg-prod-kpi").each((_i, el) => {
			$(el).toggleClass("is-active", $(el).attr("data-tab") === tab);
		});
		this.refresh_list();
	}

	bind_dashboard_events() {
		const $b = this.$body;

		$b.find(".fg-search-input").on("input", (e) => {
			const val = $(e.currentTarget).val() || "";
			$b.find(".fg-search-clear").toggleClass("is-visible", !!val.trim());
			clearTimeout(this._search_debounce);
			this._search_debounce = setTimeout(() => {
				if (val.trim() === this.list_search.trim()) return;
				this.list_search = val;
				this.list_page = 1;
				this.refresh_list();
			}, SEARCH_DEBOUNCE_MS);
		});
		$b.find(".fg-search-clear").on("click", () => {
			clearTimeout(this._search_debounce);
			$b.find(".fg-search-input").val("").trigger("focus");
			$b.find(".fg-search-clear").removeClass("is-visible");
			if (!this.list_search) return;
			this.list_search = "";
			this.list_page = 1;
			this.refresh_list();
		});

		$b.find(".fg-prod-tabs").on("click", ".fg-prod-tab", (e) => this.set_tab($(e.currentTarget).attr("data-tab")));
		$b.find(".fg-prod-kpis-slot").on("click", ".fg-prod-kpi", (e) => this.set_tab($(e.currentTarget).attr("data-tab")));
		$b.find(".fg-prod-kpis-slot").on("click", ".fg-prod-retry-kpis", () => this.reload_kpis());

		$b.find(".fg-prod-material-filter").on("change", (e) => {
			this.list_material = $(e.currentTarget).val() || "";
			this.list_page = 1;
			this.refresh_list();
		});
		$b.find(".fg-prod-date-filter").on("change", (e) => {
			this.list_date = $(e.currentTarget).val() || "";
			this.list_page = 1;
			this.refresh_list();
		});

		$b.find(".fg-prod-cards").on("click", ".fg-prod-card-detail", (e) => this.open_detail($(e.currentTarget).attr("data-name")));
		$b.find(".fg-prod-cards").on("click", ".fg-prod-retry-list", () => this.refresh_list());

		$b.find(".fg-prod-pagination").on("click", ".fg-prod-pagination-prev", () => {
			if (this.list_page <= 1) return;
			this.list_page -= 1;
			this.refresh_list();
		});
		$b.find(".fg-prod-pagination").on("click", ".fg-prod-pagination-next", () => {
			if (!this.list || !this.list.has_more) return;
			this.list_page += 1;
			this.refresh_list();
		});
	}

	reload_kpis() {
		return this.fetch_dashboard().then(() => {
			if (this.view === "dashboard") {
				this.$body.find(".fg-prod-kpis-slot").html(render_kpis_html(this.dashboard, this.dashboard_error, this.list_tab));
			}
		});
	}

	// =====================================================================
	// Detalle (vista dentro de la misma Page, patrón Cartera/Recorridos)
	// =====================================================================
	open_detail(name) {
		if (!name) return;
		const seq = ++this._detail_request_seq;
		this.view = "detail";
		this.detail_name = name;
		this.detail = null;
		this.render_detail_skeleton();
		this.call("get_production_order_detail", { work_order: name })
			.then((detail) => {
				if (seq !== this._detail_request_seq || this.view !== "detail") return;
				this.detail = detail;
				this.render_detail();
			})
			.catch(() => {
				if (seq !== this._detail_request_seq || this.view !== "detail") return;
				this.render_detail_error();
			});
		window.scrollTo(0, 0);
	}

	back_to_dashboard() {
		this._detail_request_seq++;
		this.view = "dashboard";
		this.detail = null;
		this.detail_name = null;
		if (!(this.dashboard || this.list)) return this.load_all();
		this.render_dashboard();
	}

	render_detail_skeleton() {
		this.$body.html(`
			${render_detail_header_html()}
			<div class="fg-skeleton fg-prod-detail-skeleton"></div>
			<div class="fg-skeleton fg-prod-detail-skeleton"></div>
		`);
		this.$body.find(".fg-prod-back").on("click", () => this.back_to_dashboard());
	}

	render_detail_error() {
		this.$body.html(`
			${render_detail_header_html()}
			<div class="fg-prod-error">
				<div>${__("No se pudo cargar el detalle de esta orden.")}</div>
				<button type="button" class="fg-btn fg-btn--solid-primary fg-prod-retry-detail">${icon("refresh-cw", "fg-icon-sm")} ${__(
					"REINTENTAR"
				)}</button>
			</div>
		`);
		this.$body.find(".fg-prod-back").on("click", () => this.back_to_dashboard());
		this.$body.find(".fg-prod-retry-detail").on("click", () => this.open_detail(this.detail_name));
	}

	render_detail() {
		this.$body.html(`${render_detail_header_html()}${render_detail_html(this.detail)}`);
		this.$body.find(".fg-prod-back").on("click", () => this.back_to_dashboard());
	}
};

// =========================================================================
// Pure helpers (sin estado; ejecutados por test_produccion_ui_contract.py)
// =========================================================================

const PAGE_LENGTH = 20;
const SEARCH_DEBOUNCE_MS = 350;

// Mirror of api/produccion.py TABS (the server validates again).
const TABS = [
	{ key: "todas", label: __("TODAS") },
	{ key: "pendientes", label: __("PENDIENTES") },
	{ key: "en_produccion", label: __("EN PRODUCCIÓN") },
	{ key: "falta_material", label: __("FALTA MATERIAL") },
	{ key: "completadas", label: __("COMPLETADAS") },
];

const MATERIAL_FILTERS = [
	{ value: "", label: __("Todos los materiales") },
	{ value: "ok", label: __("Materiales disponibles") },
	{ value: "parcial", label: __("Materiales parciales") },
	{ value: "falta", label: __("Falta material") },
	{ value: "config", label: __("Configuración incompleta") },
];

const DATE_FILTERS = [
	{ value: "", label: __("Todas las fechas") },
	{ value: "today", label: __("Órdenes de hoy") },
	{ value: "7d", label: __("Últimos 7 días") },
	{ value: "30d", label: __("Últimos 30 días") },
];

const EMPTY_MESSAGES = {
	todas: __("NO HAY ÓRDENES DE PRODUCCIÓN PENDIENTES"),
	pendientes: __("NO HAY ÓRDENES PENDIENTES"),
	en_produccion: __("NO HAY ÓRDENES EN PRODUCCIÓN"),
	falta_material: __("NINGUNA ORDEN TIENE FALTA DE MATERIAL"),
	completadas: __("NO HAY ÓRDENES COMPLETADAS"),
};

// KPI -> pestaña que abre (COMPLETADAS HOY abre COMPLETADAS).
const KPIS = [
	{ key: "pendientes", label: __("PENDIENTES"), i: "clock", mod: "pendiente", tab: "pendientes" },
	{ key: "en_produccion", label: __("EN PRODUCCIÓN"), i: "factory", mod: "en-produccion", tab: "en_produccion" },
	{ key: "falta_material", label: __("FALTA MATERIAL"), i: "triangle-alert", mod: "falta", tab: "falta_material" },
	{ key: "completadas_hoy", label: __("COMPLETADAS HOY"), i: "circle-check-big", mod: "completada", tab: "completadas" },
];

// Estado operativo (calculado por el servidor) -> etiqueta + color.
const STATE_META = {
	pendiente: { label: __("PENDIENTE"), mod: "pendiente" },
	en_produccion: { label: __("EN PRODUCCIÓN"), mod: "en-produccion" },
	completada: { label: __("COMPLETADA"), mod: "completada" },
	detenida: { label: __("DETENIDA"), mod: "neutral" },
	cerrada: { label: __("CERRADA"), mod: "neutral" },
};

// Semáforo: nunca solo color -- siempre texto + ícono.
const MATERIAL_META = {
	ok: { label: __("MATERIALES DISPONIBLES"), mod: "ok", i: "circle-check-big" },
	parcial: { label: __("MATERIALES PARCIALES"), mod: "parcial", i: "circle-alert" },
	falta: { label: __("FALTA MATERIAL"), mod: "falta", i: "triangle-alert" },
	config: { label: __("CONFIGURACIÓN INCOMPLETA"), mod: "falta", i: "settings" },
};

const WAREHOUSE_PROBLEMS = {
	sin_bodega: __("BODEGA NO CONFIGURADA"),
	no_existe: __("BODEGA INEXISTENTE"),
	otra_empresa: __("BODEGA DE OTRA EMPRESA"),
	invalida: __("BODEGA NO VÁLIDA (GRUPO O DESHABILITADA)"),
};

const MONTHS_SHORT = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];

const REPORT_STATUS_MOD = { Abierto: "falta", "En Proceso": "en-produccion", Resuelto: "completada" };

function esc(value) {
	if (value === null || value === undefined) return "";
	return frappe.utils.escape_html(String(value));
}

function icon(name, extra_class) {
	return `<svg class="fg-icon ${extra_class || ""}" aria-hidden="true"><use href="#icon-${name}"></use></svg>`;
}

function get_initials(name) {
	const parts = (name || "").trim().split(/\s+/).filter(Boolean);
	if (!parts.length) return "?";
	const first = parts[0][0] || "";
	const second = parts.length > 1 ? parts[1][0] : "";
	return (first + second).toUpperCase();
}

function cint(v) {
	const n = parseInt(v, 10);
	return isNaN(n) ? 0 : n;
}

function role_label() {
	const has = (r) => !!(frappe.user && frappe.user.has_role && frappe.user.has_role(r));
	if (has("Jefe de Producción")) return __("Jefe de Producción");
	if (has("Producción")) return __("Producción");
	return __("Administración");
}

function empty_message(tab) {
	return EMPTY_MESSAGES[tab] || EMPTY_MESSAGES.todas;
}

function state_meta(state) {
	return STATE_META[state] || STATE_META.pendiente;
}

function material_meta(level) {
	return MATERIAL_META[level] || null;
}

// 1234.5 -> "1.234,5"; hasta 3 decimales, sin ceros sobrantes. Nunca
// redondea a entero una cantidad fraccionaria (kg, litros).
function format_qty(value) {
	if (value === null || value === undefined || value === "") return "—";
	const n = Number(value);
	if (!isFinite(n)) return "—";
	const rounded = Math.round(Math.abs(n) * 1000);
	const int_part = Math.floor(rounded / 1000);
	const frac = rounded % 1000;
	const int_str = String(int_part).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
	const frac_str = frac ? "," + String(frac).padStart(3, "0").replace(/0+$/, "") : "";
	return `${n < 0 && rounded ? "-" : ""}${int_str}${frac_str}`;
}

function qty_with_uom(value, uom) {
	const q = format_qty(value);
	return uom && q !== "—" ? `${q} ${esc(uom)}` : q;
}

// "2026-09-25 10:35:12" -> "25 sep · 10:35 AM" (sin Date(): la hora ya es
// la del sitio, no se reinterpreta en la zona del navegador).
function format_datetime_short(value) {
	const m = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/.exec(value || "");
	if (!m) return "—";
	const day = `${cint(m[3])} ${MONTHS_SHORT[cint(m[2]) - 1] || ""}`;
	if (m[4] === undefined) return day;
	const h24 = cint(m[4]);
	const h12 = h24 % 12 === 0 ? 12 : h24 % 12;
	return `${day} · ${h12}:${m[5]} ${h24 < 12 ? "AM" : "PM"}`;
}

function render_badge(text, mod) {
	return `<span class="fg-badge fg-prod-badge fg-prod-badge--${mod}">${text}</span>`;
}

function render_material_pill(level) {
	const meta = material_meta(level);
	if (!meta) return "";
	return `<span class="fg-prod-material fg-prod-material--${meta.mod}">${icon(meta.i, "fg-icon-sm")}<span>${meta.label}</span></span>`;
}

function progress_pct(produced, qty) {
	const q = Number(qty) || 0;
	if (q <= 0) return 0;
	return Math.max(0, Math.min(100, Math.round(((Number(produced) || 0) / q) * 100)));
}

function render_progress_html(produced, qty, uom) {
	const pct = progress_pct(produced, qty);
	return `
		<div class="fg-prod-progress">
			<div class="fg-prod-progress-row">
				<span class="fg-prod-metric-label">${__("PRODUCIDO")}</span>
				<span class="fg-prod-progress-value">${format_qty(produced)} / ${qty_with_uom(qty, uom)}</span>
			</div>
			<div class="fg-prod-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}">
				<div class="fg-prod-progress-fill" style="width: ${pct}%"></div>
			</div>
		</div>
	`;
}

function metric(label, value_html, mod) {
	return `
		<div class="fg-prod-metric${mod ? ` fg-prod-metric--${mod}` : ""}">
			<div class="fg-prod-metric-label">${label}</div>
			<div class="fg-prod-metric-value">${value_html}</div>
		</div>
	`;
}

// Las tres magnitudes nunca se mezclan: PLANEADO (Work Order.qty),
// PRODUCIDO (produced_qty, en la barra) y ASIGNADO A PEDIDOS
// (SUM(production_qty_allocated)); DISPONIBLE DEL LOTE = planeado - asignado.
function render_quantities_html(o) {
	return `
		<div class="fg-prod-metrics">
			${metric(__("PLANEADO"), qty_with_uom(o.qty, o.stock_uom))}
			${metric(__("ASIGNADO A PEDIDOS"), format_qty(o.allocated_qty))}
			${metric(__("DISPONIBLE DEL LOTE"), format_qty(o.available_lot_qty))}
		</div>
		${render_progress_html(o.produced_qty, o.qty, o.stock_uom)}
	`;
}

function render_order_card(o) {
	const meta = state_meta(o.state);
	const more = cint(o.orders_count) > 1 ? `<span class="fg-prod-more">${__("+{0} pedidos", [cint(o.orders_count) - 1])}</span>` : "";
	const origin = o.oldest_sales_order
		? `
			<div class="fg-prod-card-origin">
				<div><span class="fg-prod-origin-label">${__("Pedido más antiguo")}:</span> <strong>${esc(o.oldest_sales_order)}</strong> ${more}</div>
				<div><span class="fg-prod-origin-label">${__("Cliente")}:</span> ${esc(o.oldest_customer_name || o.oldest_customer)}</div>
				<div><span class="fg-prod-origin-label">${__("Reportado")}:</span> ${format_datetime_short(o.oldest_reported_on)}</div>
			</div>`
		: o.oldest_reported_on
		? `<div class="fg-prod-card-origin"><div><span class="fg-prod-origin-label">${__("Reportado")}:</span> ${format_datetime_short(
				o.oldest_reported_on
		  )}</div></div>`
		: "";
	return `
		<article class="fg-prod-card fg-prod-card--${meta.mod}">
			<div class="fg-prod-card-top">
				<div class="fg-prod-card-product">
					<div class="fg-prod-card-name">${esc(o.item_name || o.production_item)}</div>
					<div class="fg-prod-card-code">${esc(o.production_item)}</div>
				</div>
				${render_badge(meta.label, meta.mod)}
			</div>
			<div class="fg-prod-card-wo">${__("WO")}: <strong>${esc(o.name)}</strong></div>
			${render_quantities_html(o)}
			${render_material_pill(o.material_level)}
			${origin}
			<button type="button" class="fg-btn fg-btn--solid-primary fg-prod-card-detail" data-name="${esc(o.name)}">
				${icon("eye", "fg-icon-sm")} ${__("VER DETALLE")}
			</button>
		</article>
	`;
}

function render_kpis_html(dashboard, has_error, active_tab) {
	if (has_error || !dashboard) {
		return `
			<div class="fg-prod-error fg-prod-kpis-error">
				<div>${__("No se pudieron cargar los indicadores de producción.")}</div>
				<button type="button" class="fg-btn fg-btn--ghost fg-prod-retry-kpis">${icon("refresh-cw", "fg-icon-sm")} ${__("REINTENTAR")}</button>
			</div>
		`;
	}
	const k = dashboard.kpis || {};
	const html = KPIS.map(
		(c) => `
			<button type="button" class="fg-kpi fg-prod-kpi fg-prod-kpi--${c.mod}${active_tab === c.tab ? " is-active" : ""}" data-kpi="${c.key}" data-tab="${c.tab}">
				<div class="fg-prod-kpi-top">
					<div class="fg-kpi-icon">${icon(c.i)}</div>
					<div class="fg-kpi-label">${c.label}</div>
				</div>
				<div class="fg-kpi-number">${cint(k[c.key])}</div>
			</button>
		`
	).join("");
	return `<div class="fg-kpis fg-prod-kpis">${html}</div>`;
}

function render_tabs_html(active) {
	return TABS.map(
		(t) => `
			<button type="button" role="tab" class="fg-prod-tab${active === t.key ? " is-active" : ""}" aria-selected="${
			active === t.key ? "true" : "false"
		}" data-tab="${t.key}">${t.label}</button>
		`
	).join("");
}

function render_select_html(css_class, label, options, value) {
	return `
		<label class="fg-prod-select">
			<span class="fg-visually-hidden">${label}</span>
			<select class="${css_class}" aria-label="${label}">
				${options
					.map((o) => `<option value="${esc(o.value)}"${o.value === value ? " selected" : ""}>${o.label}</option>`)
					.join("")}
			</select>
		</label>
	`;
}

function render_filters_html(material, date_range) {
	return (
		render_select_html("fg-prod-material-filter", __("Materiales"), MATERIAL_FILTERS, material || "") +
		render_select_html("fg-prod-date-filter", __("Fecha de la orden"), DATE_FILTERS, date_range || "")
	);
}

function render_search_bar_html(value) {
	const has_value = !!(value && value.trim());
	return `
		<div class="fg-search-bar fg-prod-search">
			${icon("search", "fg-search-icon")}
			<input type="search" class="fg-search-input" aria-label="${__("Buscar")}" placeholder="${__(
				"BUSCAR ORDEN, PRODUCTO, PEDIDO O CLIENTE..."
			)}" value="${esc(value || "")}">
			<button type="button" class="fg-search-clear ${has_value ? "is-visible" : ""}" title="${__("Limpiar")}" aria-label="${__("Limpiar")}">${icon(
				"x",
				"fg-icon-sm"
			)}</button>
		</div>
	`;
}

function render_pagination_html(list, has_error) {
	if (!list || has_error || !cint(list.total)) return "";
	const total = cint(list.total);
	const page = cint(list.page) || 1;
	const page_size = cint(list.page_size) || PAGE_LENGTH;
	const start = (page - 1) * page_size + 1;
	const end = Math.min(page * page_size, total);
	return `
		<div class="fg-prod-pagination-info">${__("Mostrando {0} a {1} de {2}", [start, end, total])}</div>
		<div class="fg-prod-pagination-controls">
			<button type="button" class="fg-prod-pagination-btn fg-prod-pagination-prev" title="${__("Anterior")}" aria-label="${__("Anterior")}" ${
		page <= 1 ? "disabled" : ""
	}>${icon("chevron-left")}</button>
			<span class="fg-prod-pagination-page">${page}</span>
			<button type="button" class="fg-prod-pagination-btn fg-prod-pagination-next" title="${__("Siguiente")}" aria-label="${__("Siguiente")}" ${
		list.has_more ? "" : "disabled"
	}>${icon("chevron-right")}</button>
		</div>
	`;
}

function render_detail_header_html() {
	return `
		<div class="fg-np-header fg-prod-detail-header">
			<button type="button" class="fg-np-back fg-prod-back">${icon("arrow-left")} ${__("Volver")}</button>
			<div class="fg-np-title">${__("DETALLE DE PRODUCCIÓN")}</div>
		</div>
	`;
}

function detail_field(label, value_html) {
	return `
		<div class="fg-prod-detail-field">
			<div class="fg-prod-detail-label">${label}</div>
			<div class="fg-prod-detail-value">${value_html}</div>
		</div>
	`;
}

function render_report_html(r) {
	const mod = REPORT_STATUS_MOD[r.status] || "neutral";
	return `
		<div class="fg-prod-report">
			<div class="fg-prod-report-top">
				<div class="fg-prod-report-customer">
					<div class="fg-prod-report-name">${esc(r.customer_name || r.customer || __("Sin cliente"))}</div>
					<div class="fg-prod-report-order">${r.sales_order ? `${__("Pedido")}: <strong>${esc(r.sales_order)}</strong>` : __("Sin pedido")}</div>
				</div>
				${render_badge(esc(r.status || "—"), mod)}
			</div>
			<div class="fg-prod-report-grid">
				${detail_field(__("Faltante original"), format_qty(r.qty_faltante))}
				${detail_field(__("Asignado a esta orden"), format_qty(r.production_qty_allocated))}
				${detail_field(__("Pick List"), r.pick_list ? esc(r.pick_list) : "—")}
				${detail_field(__("Reportado"), format_datetime_short(r.reported_on))}
			</div>
			<div class="fg-prod-report-item">${esc(r.item_name || r.item_code)} · ${esc(r.item_code)} · ${esc(r.name)}</div>
		</div>
	`;
}

function material_status_html(m) {
	if (m.level === "consumido") return render_badge(__("CONSUMIDO"), "neutral");
	if (m.level === "config") return render_badge(`⚠ ${WAREHOUSE_PROBLEMS[m.warehouse_problem] || __("BODEGA NO VÁLIDA")}`, "falta");
	if (m.level === "ok") return render_badge(`✓ ${__("DISPONIBLE")}`, "ok");
	const text = __("FALTAN {0}", [qty_with_uom(m.shortfall_qty, m.stock_uom)]);
	return render_badge(`${m.level === "falta" ? "✕" : "!"} ${text}`, m.level === "falta" ? "falta" : "parcial");
}

function render_material_html(m) {
	const warehouse = m.warehouse_problem
		? `<span class="fg-prod-warehouse-problem">${icon("triangle-alert", "fg-icon-sm")} ${WAREHOUSE_PROBLEMS[m.warehouse_problem] || ""}${
				m.source_warehouse ? ` · ${esc(m.source_warehouse)}` : ""
		  }</span>`
		: `${icon("warehouse", "fg-icon-sm")} ${esc(m.source_warehouse)}`;
	const pending_note =
		Number(m.consumed_qty) > 0 && m.level !== "consumido"
			? `<div class="fg-prod-material-note">${__("Pendiente por consumir")}: ${qty_with_uom(m.pending_qty, m.stock_uom)}</div>`
			: "";
	return `
		<div class="fg-prod-material-row fg-prod-material-row--${esc(m.level)}">
			<div class="fg-prod-material-top">
				<div class="fg-prod-material-product">
					<div class="fg-prod-material-name">${esc(m.item_name || m.item_code)}</div>
					<div class="fg-prod-material-code">${esc(m.item_code)}</div>
				</div>
				${material_status_html(m)}
			</div>
			<div class="fg-prod-material-grid">
				${metric(__("NECESARIO"), qty_with_uom(m.required_qty, m.stock_uom))}
				${metric(__("DISPONIBLE"), m.available_qty === null || m.available_qty === undefined ? "—" : qty_with_uom(m.available_qty, m.stock_uom))}
				${metric(__("CONSUMIDO"), qty_with_uom(m.consumed_qty, m.stock_uom))}
				${metric(__("FALTA"), qty_with_uom(m.shortfall_qty, m.stock_uom), Number(m.shortfall_qty) > 0 ? "danger" : "")}
			</div>
			${pending_note}
			<div class="fg-prod-material-warehouse">${warehouse}</div>
		</div>
	`;
}

function render_detail_html(d) {
	const meta = state_meta(d.state);
	const reports = d.reports || [];
	const materials = d.materials || [];
	return `
		<div class="fg-prod-detail">
			<div class="fg-prod-detail-col">
				<section class="fg-prod-detail-section">
					<div class="fg-prod-detail-top">
						<div class="fg-prod-card-product">
							<div class="fg-prod-card-name fg-prod-detail-name">${esc(d.item_name || d.production_item)}</div>
							<div class="fg-prod-card-code">${esc(d.production_item)}</div>
						</div>
						${render_badge(meta.label, meta.mod)}
					</div>
					${render_material_pill(d.material_level)}
					<div class="fg-prod-detail-grid">
						${detail_field(__("Producto"), esc(d.item_name || d.production_item))}
						${detail_field(__("Item Code"), esc(d.production_item))}
						${detail_field(__("Work Order"), esc(d.name))}
						${detail_field(__("BOM"), esc(d.bom_no))}
						${detail_field(__("Bodega producto terminado"), d.fg_warehouse ? esc(d.fg_warehouse) : "—")}
						${detail_field(__("Estado"), `${meta.label} <span class="fg-prod-native">(${esc(d.native_status)})</span>`)}
					</div>
				</section>
				<section class="fg-prod-detail-section">
					<div class="fg-prod-section-title">${__("PEDIDOS / FALTANTES ASOCIADOS")}</div>
					${reports.length ? reports.map(render_report_html).join("") : `<div class="fg-prod-muted">${__("Sin faltantes asociados.")}</div>`}
					<div class="fg-prod-total">
						<span>${__("TOTAL ASIGNADO")}</span>
						<strong>${qty_with_uom(d.reports_allocated_total, d.stock_uom)}</strong>
					</div>
				</section>
			</div>
			<div class="fg-prod-detail-col">
				<section class="fg-prod-detail-section">
					<div class="fg-prod-section-title">${__("RESUMEN")}</div>
					<div class="fg-prod-summary">
						${metric(__("CANTIDAD PLANEADA"), qty_with_uom(d.qty, d.stock_uom))}
						${metric(__("CANTIDAD PRODUCIDA"), qty_with_uom(d.produced_qty, d.stock_uom))}
						${metric(__("CANTIDAD PENDIENTE"), qty_with_uom(d.pending_qty, d.stock_uom))}
						${metric(__("ASIGNADA A FALTANTES"), qty_with_uom(d.allocated_qty, d.stock_uom))}
						${metric(__("DISPONIBLE DEL LOTE"), qty_with_uom(d.available_lot_qty, d.stock_uom))}
					</div>
					${render_progress_html(d.produced_qty, d.qty, d.stock_uom)}
				</section>
				<section class="fg-prod-detail-section">
					<div class="fg-prod-section-title">${__("MATERIAS PRIMAS")}</div>
					${materials.length ? materials.map(render_material_html).join("") : `<div class="fg-prod-muted">${__("Sin materias primas.")}</div>`}
				</section>
			</div>
		</div>
	`;
}
