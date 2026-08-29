// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["jefe-pick-lists"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Pick Lists"),
		single_column: true,
	});
	new fabergray_erp.JefePickLists(page);
};

// Commit 22.9 -- resumen operativo/historial de Pick Lists para Jefe de
// Bodega. Lectura solamente -- ningún botón de esta Page escribe nada;
// "VER DETALLE" reutiliza api.bodega.get_pick_list() (ya existente, Commit
// 8) sin duplicar su lógica. El estado visual de cada tarjeta (listo/con
// faltantes/en alistamiento/pendiente) viene tal cual del servidor
// (api.jefe_bodega.get_pick_list_history(), que reutiliza la misma regla de
// bucketing de api.bodega.get_queue() -- nunca se decide aquí).
fabergray_erp.JefePickLists = class JefePickLists {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.jefe_bodega.";
		this.busy = false;

		this.filters = {
			date_preset: "hoy",
			date_from: frappe.datetime.get_today(),
			date_to: frappe.datetime.get_today(),
			status: "",
			warehouse: "",
			txt: "",
		};
		this.list_page = 1;
		this.rows = [];
		this.total = 0;
		this.summary = null;
		this.warehouses = [];
		this._search_debounce = null;

		this.$app = $('<div class="fg-shell fg-jefe-pick-lists">').appendTo(this.page.body);
		this.render_shell();
		this.load_all();
	}

	call(method, args) {
		return frappe.call({ method: this.method_prefix + method, args: args || {} }).then((r) => r.message);
	}

	call_full(method, args) {
		return frappe.call({ method: method, args: args || {} }).then((r) => r.message);
	}

	set_busy(is_busy) {
		this.busy = !!is_busy;
		this.$app.find(".fg-refresh-btn").prop("disabled", this.busy);
		this.$app.toggleClass("fg-loading", !!is_busy);
	}

	render_shell() {
		const fullname = frappe.session.user_fullname || frappe.session.user;
		this.$app.html(`
			<div class="fg-header">
				<div class="fg-header-brand">
					<button type="button" class="fg-back-btn">${icon("arrow-left")} ${__("Jefe de Bodega")}</button>
					<span class="fg-header-logo">FABRIGRAY</span>
					<span class="fg-header-sep">|</span>
					<span class="fg-header-title">${__("PICK LISTS")}</span>
				</div>
				<div class="fg-header-user">
					<div class="fg-header-user-info">
						<div class="fg-header-user-name">${frappe.utils.escape_html(fullname)}</div>
						<div class="fg-header-user-role">${__("Jefe de Bodega")}</div>
					</div>
					<div class="fg-header-avatar">${get_initials(fullname)}</div>
					<button type="button" class="fg-refresh-btn" title="${__("Actualizar")}">${icon("refresh-cw")}</button>
				</div>
			</div>
			<div class="fg-body"></div>
		`);
		this.$body = this.$app.find(".fg-body");
		this.$app.find(".fg-back-btn").on("click", () => frappe.set_route("jefe-de-bodega"));
		this.$app.find(".fg-refresh-btn").on("click", () => this.load_all());
	}

	load_all() {
		this.set_busy(true);
		this.render_skeleton();
		// get_warehouse_summary() (Commit 22.9) ya devuelve exactamente los
		// almacenes reales y operativos de la compañía -- reutilizado aquí
		// solo para poblar el filtro, una vez por carga de Page (no en cada
		// refresh de la lista).
		return Promise.all([
			this.call("get_pick_list_history_summary"),
			this.call("get_warehouse_summary"),
			this.load_list(),
		])
			.then(([summary, warehouse_summary]) => {
				this.summary = summary;
				this.warehouses = warehouse_summary.warehouses || [];
				this.render_body();
			})
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	load_list() {
		return this.call("get_pick_list_history", {
			status: this.filters.status || null,
			date_from: this.filters.date_from || null,
			date_to: this.filters.date_to || null,
			warehouse: this.filters.warehouse || null,
			txt: this.filters.txt,
			start: (this.list_page - 1) * PAGE_SIZE,
			page_length: PAGE_SIZE,
		}).then((res) => {
			this.rows = res.pick_lists || [];
			this.total = res.total || 0;
		});
	}

	refresh_list() {
		this.set_busy(true);
		return this.load_list()
			.then(() => {
				this.$body.find(".fg-pl-cards").html(this.render_cards_html());
				this.$body.find(".fg-pl-pagination").html(this.render_pagination_html());
			})
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	render_skeleton() {
		this.$body.html(`
			<div class="fg-skeleton-kpis">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
			<div class="fg-skeleton-cards">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
		`);
	}

	render_body() {
		this.$body.html(`
			${this.render_kpis()}
			${this.render_filters()}
			<div class="fg-pl-cards">${this.render_cards_html()}</div>
			<div class="fg-pl-pagination">${this.render_pagination_html()}</div>
		`);
		this.bind_events();
	}

	render_kpis() {
		const s = this.summary || {};
		const cards = [
			{ key: "total_hoy", label: __("Total hoy"), icon: "clipboard-list", mod: "pl-total" },
			{ key: "listos", label: __("Listos"), icon: "circle-check-big", mod: "pl-listos" },
			{ key: "con_faltantes", label: __("Con faltantes"), icon: "triangle-alert", mod: "pl-faltantes" },
			{ key: "en_alistamiento", label: __("En alistamiento"), icon: "clock", mod: "pl-alistamiento" },
			{ key: "completados", label: __("Completados"), icon: "package-check", mod: "pl-completados" },
		];
		const html = cards
			.map(
				(c) => `
				<div class="fg-kpi fg-kpi--${c.mod}">
					<div class="fg-kpi-icon">${icon(c.icon)}</div>
					<div class="fg-kpi-number">${s[c.key] ?? 0}</div>
					<div class="fg-kpi-label">${c.label}</div>
				</div>
			`
			)
			.join("");
		return `<div class="fg-kpis fg-kpis--pl">${html}</div>`;
	}

	render_filters() {
		const presets = [
			{ key: "hoy", label: __("Hoy") },
			{ key: "ayer", label: __("Ayer") },
			{ key: "7dias", label: __("Últimos 7 días") },
			{ key: "custom", label: __("Rango personalizado") },
		];
		const preset_html = presets
			.map(
				(p) =>
					`<button type="button" class="fg-pl-preset ${
						this.filters.date_preset === p.key ? "is-active" : ""
					}" data-preset="${p.key}">${p.label}</button>`
			)
			.join("");

		const status_tabs = [
			{ key: "", label: __("Todos") },
			{ key: "listos", label: __("Listos") },
			{ key: "con_faltantes", label: __("Con faltantes") },
			{ key: "en_alistamiento", label: __("En alistamiento") },
			{ key: "pendientes", label: __("Pendientes") },
		];
		const status_html = status_tabs
			.map(
				(t) =>
					`<button type="button" class="fg-pl-tab ${
						this.filters.status === t.key ? "is-active" : ""
					}" data-status="${t.key}">${t.label}</button>`
			)
			.join("");

		const custom_range =
			this.filters.date_preset === "custom"
				? `
			<div class="fg-pl-custom-range">
				<input type="date" class="fg-pl-date-from" value="${this.filters.date_from || ""}">
				<span>${__("a")}</span>
				<input type="date" class="fg-pl-date-to" value="${this.filters.date_to || ""}">
			</div>
		`
				: "";

		const warehouse_options = this.warehouses
			.map(
				(w) =>
					`<option value="${frappe.utils.escape_html(w.name)}" ${
						this.filters.warehouse === w.name ? "selected" : ""
					}>${frappe.utils.escape_html(w.warehouse_name)}</option>`
			)
			.join("");

		return `
			<div class="fg-pl-filters">
				<div class="fg-pl-presets">${preset_html}</div>
				${custom_range}
				<div class="fg-pl-tabs">${status_html}</div>
				<div class="fg-pl-toolbar">
					<select class="fg-pl-warehouse-select">
						<option value="">${__("Todos los almacenes")}</option>
						${warehouse_options}
					</select>
					<div class="fg-pl-search-wrap">
						${icon("search", "fg-pl-search-icon")}
						<input type="text" class="fg-pl-search-input" placeholder="${__(
							"Buscar por Pedido / Pick List / Cliente..."
						)}" value="${frappe.utils.escape_html(this.filters.txt || "")}">
					</div>
				</div>
			</div>
		`;
	}

	render_cards_html() {
		if (!this.rows.length) {
			return `<div class="fg-empty">${__("No hay Pick Lists que coincidan con estos filtros.")}</div>`;
		}
		return this.rows.map((r) => this.render_card(r)).join("");
	}

	render_card(r) {
		const status_meta = {
			listos: { label: __("LISTO"), mod: "pl-listo" },
			con_faltantes: { label: __("CON FALTANTES"), mod: "pl-faltante" },
			en_alistamiento: { label: __("EN ALISTAMIENTO"), mod: "pl-alistamiento" },
			pendientes: { label: __("PENDIENTE"), mod: "pl-pendiente" },
		}[r.state] || { label: r.state, mod: "pl-pendiente" };
		const label = r.is_completed ? __("COMPLETADO") : status_meta.label;

		const pedido = r.sales_order
			? `${__("Pedido")}: ${frappe.utils.escape_html(r.commercial_name || r.sales_order)}`
			: __("Sin pedido asociado");
		const finalizado =
			r.state === "listos" && r.modified
				? `${__("Finalizado")}: ${frappe.datetime.str_to_user(r.modified)}`
				: null;

		return `
			<div class="fg-pl-card" data-name="${frappe.utils.escape_html(r.name)}">
				<div class="fg-pl-card-top">
					<span class="fg-status-pill fg-status-pill--${status_meta.mod}">${label}</span>
					<span class="fg-pl-card-name">${frappe.utils.escape_html(r.name)}</span>
				</div>
				<div class="fg-pl-card-meta">${pedido}</div>
				<div class="fg-pl-card-meta">${__("Cliente")}: ${frappe.utils.escape_html(r.customer || "—")}</div>
				<div class="fg-pl-card-meta">${__("Almacén")}: ${frappe.utils.escape_html(r.parent_warehouse || "—")}</div>
				<div class="fg-pl-card-qty">
					<div class="fg-pl-card-qty-col">
						<div class="fg-pl-card-qty-label">${__("Productos")}</div>
						<div class="fg-pl-card-qty-value">${r.item_count}</div>
					</div>
					<div class="fg-pl-card-qty-col">
						<div class="fg-pl-card-qty-label">${__("Cantidad total")}</div>
						<div class="fg-pl-card-qty-value">${format_qty(r.qty_requerida)}</div>
					</div>
					<div class="fg-pl-card-qty-col">
						<div class="fg-pl-card-qty-label">${__("Faltantes")}</div>
						<div class="fg-pl-card-qty-value ${r.shortage_count ? "fg-pl-card-qty-value--danger" : ""}">${
			r.shortage_count
		}</div>
					</div>
				</div>
				${finalizado ? `<div class="fg-pl-card-meta fg-pl-card-meta--muted">${finalizado}</div>` : ""}
				<button type="button" class="fg-btn fg-btn--outline-primary fg-pl-card-btn">${__("VER DETALLE")}</button>
			</div>
		`;
	}

	render_pagination_html() {
		if (!this.total) return "";
		const page_count = Math.max(Math.ceil(this.total / PAGE_SIZE), 1);
		const start = (this.list_page - 1) * PAGE_SIZE + 1;
		const end = Math.min(this.list_page * PAGE_SIZE, this.total);
		return `
			<div class="fg-pl-pagination-info">${__("Mostrando {0} a {1} de {2}", [start, end, this.total])}</div>
			<div class="fg-pl-pagination-controls">
				<button type="button" class="fg-pl-pagination-btn fg-pl-pagination-prev" ${
					this.list_page <= 1 ? "disabled" : ""
				}>${icon("chevron-left")}</button>
				<span class="fg-pl-pagination-page">${this.list_page}</span>
				<button type="button" class="fg-pl-pagination-btn fg-pl-pagination-next" ${
					this.list_page >= page_count ? "disabled" : ""
				}>${icon("chevron-right")}</button>
			</div>
		`;
	}

	bind_events() {
		this.$body.find(".fg-pl-preset").on("click", (e) => {
			const preset = $(e.currentTarget).data("preset");
			this.apply_date_preset(preset);
		});
		this.$body.find(".fg-pl-date-from").on("change", (e) => {
			this.filters.date_from = $(e.currentTarget).val();
			this.list_page = 1;
			this.refresh_list();
		});
		this.$body.find(".fg-pl-date-to").on("change", (e) => {
			this.filters.date_to = $(e.currentTarget).val();
			this.list_page = 1;
			this.refresh_list();
		});
		this.$body.find(".fg-pl-tab").on("click", (e) => {
			this.filters.status = $(e.currentTarget).data("status") || "";
			this.list_page = 1;
			this.$body.find(".fg-pl-tab").removeClass("is-active");
			$(e.currentTarget).addClass("is-active");
			this.refresh_list();
		});
		this.$body.find(".fg-pl-warehouse-select").on("change", (e) => {
			this.filters.warehouse = $(e.currentTarget).val();
			this.list_page = 1;
			this.refresh_list();
		});
		this.$body.find(".fg-pl-search-input").on("input", (e) => {
			const val = $(e.currentTarget).val();
			clearTimeout(this._search_debounce);
			this._search_debounce = setTimeout(() => {
				this.filters.txt = val;
				this.list_page = 1;
				this.refresh_list();
			}, 300);
		});
		this.$body.find(".fg-pl-cards").on("click", ".fg-pl-card-btn", (e) => {
			const name = $(e.currentTarget).closest(".fg-pl-card").data("name");
			this.open_detail(name);
		});
		this.$body.find(".fg-pl-pagination").on("click", ".fg-pl-pagination-prev", () => {
			this.list_page = Math.max(this.list_page - 1, 1);
			this.refresh_list();
		});
		this.$body.find(".fg-pl-pagination").on("click", ".fg-pl-pagination-next", () => {
			this.list_page = this.list_page + 1;
			this.refresh_list();
		});
	}

	apply_date_preset(preset) {
		this.filters.date_preset = preset;
		const today = frappe.datetime.get_today();
		if (preset === "hoy") {
			this.filters.date_from = today;
			this.filters.date_to = today;
		} else if (preset === "ayer") {
			const yesterday = frappe.datetime.add_days(today, -1);
			this.filters.date_from = yesterday;
			this.filters.date_to = yesterday;
		} else if (preset === "7dias") {
			this.filters.date_from = frappe.datetime.add_days(today, -6);
			this.filters.date_to = today;
		}
		// "custom": deja date_from/date_to como estén -- el usuario los edita
		// en los dos <input type="date"> que aparecen para ese preset.
		this.list_page = 1;
		this.set_busy(true);
		Promise.all([this.call("get_pick_list_history_summary"), this.load_list()])
			.then(([summary]) => {
				this.summary = summary;
				this.render_body();
			})
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	// -------------------------------------------------------------------
	// Detalle -- reutiliza api.bodega.get_pick_list() (Commit 8), no
	// duplica su lógica. Enriquecido (Commit 22.9, aditivo) con
	// modified/modified_by para "usuario/timestamp de finalización".
	// -------------------------------------------------------------------
	open_detail(name) {
		this.set_busy(true);
		this.call_full("fabergray_erp.api.bodega.get_pick_list", { name: name })
			.then((detail) => this.render_detail_dialog(detail))
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	render_detail_dialog(detail) {
		const rows_html = (detail.rows || [])
			.map(
				(row) => `
				<tr class="${row.has_shortage_report ? "fg-pl-detail-row--shortage" : ""}">
					<td>${frappe.utils.escape_html(row.item_code)}</td>
					<td>${frappe.utils.escape_html(row.item_name || "")}</td>
					<td>${format_qty(row.qty_solicitada)}</td>
					<td>${format_qty(row.qty_alistada)}</td>
					<td>${row.has_shortage_report ? icon("triangle-alert", "fg-icon-sm") : "—"}</td>
				</tr>
			`
			)
			.join("");

		const has_shortage = (detail.rows || []).some((row) => row.has_shortage_report);
		const is_ready = detail.docstatus === 1;

		const dialog = new frappe.ui.Dialog({
			title: `${__("Pick List")} ${detail.name}`,
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "detail_html",
					options: `
						<div class="fg-pl-detail-info">
							${row_kv(__("Pedido"), detail.sales_order ? `${detail.commercial_name || detail.sales_order}` : "—")}
							${row_kv(__("Cliente"), detail.customer || "—")}
							${row_kv(__("Almacén"), detail.parent_warehouse || "—")}
							${row_kv(__("Estado nativo"), detail.status || "—")}
							${row_kv(__("Iniciado por"), detail.fg_started_by || "—")}
							${row_kv(__("Iniciado el"), detail.fg_started_on ? frappe.datetime.str_to_user(detail.fg_started_on) : "—")}
						</div>
						<table class="fg-pl-detail-table">
							<thead>
								<tr><th>${__("Item")}</th><th>${__("Nombre")}</th><th>${__("Solicitado")}</th><th>${__("Alistado")}</th><th></th></tr>
							</thead>
							<tbody>${rows_html}</tbody>
						</table>
					`,
				},
			],
			primary_action_label: has_shortage ? __("VER FALTANTES") : is_ready ? __("VER ALISTAMIENTO") : null,
			primary_action: has_shortage
				? () => {
						dialog.hide();
						frappe.route_options = { pick_list: detail.name };
						frappe.set_route("List", "Reporte de Faltante");
				  }
				: is_ready
				? () => {
						dialog.hide();
						frappe.set_route("Form", "Pick List", detail.name);
				  }
				: null,
			secondary_action_label: __("CERRAR"),
			secondary_action: () => dialog.hide(),
		});
		if (!dialog.primary_action) {
			// frappe.ui.Dialog siempre pinta un botón primario si hay label;
			// sin ninguno de los dos casos, se oculta explícitamente.
			dialog.get_primary_btn().hide();
		}
		dialog.$wrapper.addClass("fg-pl-detail-dialog");
		dialog.show();
	}
};

function row_kv(label, value) {
	return `<div class="fg-pl-detail-row"><span>${label}</span><strong>${frappe.utils.escape_html(String(value))}</strong></div>`;
}

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, intentionally duplicated from
// jefe_de_bodega.js/bodega.js (same reasoning as Commit 6).
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

function flt(v) {
	return frappe.utils.flt ? frappe.utils.flt(v) : parseFloat(v) || 0;
}

function format_qty(v) {
	const n = flt(v);
	return Number.isInteger(n) ? String(n) : n.toFixed(2);
}
