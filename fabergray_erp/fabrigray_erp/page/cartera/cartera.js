// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["cartera"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Cartera"),
		single_column: true,
	});
	new fabergray_erp.Cartera(page);
};

// Fase 27.2 -- Page Cartera: dashboard operativo de SOLO LECTURA sobre la
// fundación de 27.1 (Cartera Obligacion / Cartera Pago). Esta Page es UI
// solamente:
// - acceso a la ruta: cartera.json's roles (Cartera, System Manager); cada
//   endpoint de api/cartera.py vuelve a validar rol, permiso y empresa --
//   ocultar la Page nunca es la frontera de seguridad;
// - todo número (KPIs, saldos, días a vencer) llega calculado por el
//   servidor con el `today` del sitio; aquí solo se formatea, nunca se
//   suman las tarjetas cargadas ni se recalcula un saldo;
// - buscador, chips, orden y paginación son server-side (get_obligations);
// - 27.3: las escrituras son REGISTRAR COBRO (panel dentro del detalle,
//   multipart con un UUID por formulario que se conserva en cada reintento),
//   CONFIRMAR / RECHAZAR el pago o reporte del conductor, y SINCRONIZAR. Cada
//   acción responde con el detalle y los KPIs ya recalculados por el
//   servidor (sin recargar la Page); la lista se refresca una vez al volver.
//   Qué acción aparece lo decide el servidor (can_register_payment,
//   can_confirm_driver_payment, can_reject_driver_report) y cada endpoint
//   lo vuelve a validar bajo bloqueo. Nada se contabiliza: "COBRO
//   REGISTRADO EN CARTERA";
// - todo texto que viene del servidor pasa por esc() (escape_html) antes
//   de entrar al HTML; el comprobante se muestra como data: URL validada
//   (tipo de imagen permitido + base64 estricto), nunca con una URL de File.
fabergray_erp.Cartera = class Cartera {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.cartera.";
		this.busy = false;
		this.view = "dashboard"; // "dashboard" | "detail"

		this.dashboard = null;
		this.dashboard_error = false;

		this.list_filter = "todos";
		this.list_search = "";
		this.list_page = 1;
		this.list = null; // respuesta de get_obligations
		this.list_error = false;
		this.list_loading = false;
		this._search_debounce = null;
		this._list_request_seq = 0; // descarta respuestas fuera de orden

		this.detail = null;
		this.detail_name = null;
		this._detail_request_seq = 0;

		// 27.3 -- formularios del detalle (uno a la vez) y estado de acciones.
		this.pay_form = null; // REGISTRAR COBRO
		this.reject_form = null; // RECHAZAR PAGO / REPORTE
		this.action_busy = false;
		this.list_stale = false; // una acción cambió datos: refrescar la lista al volver

		this.syncing = false;

		this.$app = $('<div class="fg-shell fg-cartera">').appendTo(this.page.body);
		this.render_shell();
		this.load_all();
	}

	// -------------------------------------------------------------------
	// Thin API wrapper -- frappe.call() no devuelve un Promise real (mismo
	// idiom que clientes.js/recorridos.js).
	// -------------------------------------------------------------------
	call(method, args, extra) {
		return new Promise((resolve, reject) => {
			frappe.call(
				Object.assign(
					{
						method: this.method_prefix + method,
						args: args || {},
						callback: (r) => resolve(r.message),
						error: (r) => reject(r),
					},
					extra || {}
				)
			);
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
					<span class="fg-header-title">${__("CARTERA")}</span>
				</div>
				<div class="fg-header-user">
					<div class="fg-header-user-info">
						<div class="fg-header-user-name">${esc(fullname)}</div>
						<div class="fg-header-user-role">${__("Cartera")}</div>
					</div>
					<div class="fg-header-avatar">${esc(get_initials(fullname))}</div>
					<button type="button" class="fg-refresh-btn" title="${__("Actualizar")}">${icon("refresh-cw")}</button>
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
	// Dashboard: KPIs + lista (un fetch de cada uno, en paralelo)
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
		return this.call("get_dashboard")
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
		this.list_loading = true;
		return this.call("get_obligations", {
			filter: this.list_filter,
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
			})
			.finally(() => {
				if (seq === this._list_request_seq) this.list_loading = false;
			});
	}

	render_skeleton() {
		this.$body.html(`
			<div class="fg-skeleton-kpis fg-cartera-skeleton-kpis">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
			<div class="fg-skeleton-cards">
				<div class="fg-skeleton"></div><div class="fg-skeleton"></div><div class="fg-skeleton"></div>
			</div>
		`);
	}

	render_dashboard() {
		if (this.view !== "dashboard") return;
		this.$body.html(`
			<div class="fg-cartera-kpis-slot">${this.render_kpis_html()}</div>
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Obligaciones")}</div>
				<div class="fg-cm-head-actions">${this.render_bulk_button_html()}${this.render_sync_button_html()}</div>
			</div>
			${render_search_bar_html(this.list_search)}
			<div class="fg-cartera-chips">${this.render_chips_html()}</div>
			<div class="fg-cartera-cards">${this.render_cards_html()}</div>
			<div class="fg-cartera-pagination">${this.render_pagination_html()}</div>
		`);
		this.bind_dashboard_events();
	}

	// -- KPIs -----------------------------------------------------------
	render_kpis_html() {
		if (this.dashboard_error || !this.dashboard) {
			return `
				<div class="fg-cartera-error fg-cartera-kpis-error">
					<div>${__("No se pudieron cargar los indicadores de cartera.")}</div>
					<button type="button" class="fg-btn fg-btn--ghost fg-cartera-retry-kpis">${icon("refresh-cw", "fg-icon-sm")} ${__(
						"REINTENTAR"
					)}</button>
				</div>
			`;
		}
		const k = this.dashboard.kpis || {};
		const currency = this.dashboard.currency;
		const month = this.dashboard.month ? this.dashboard.month.label : "";
		const cards = [
			{ key: "cartera_actual", label: __("CARTERA ACTUAL"), i: "wallet", mod: "actual", unit: "obligation" },
			{ key: "por_vencer", label: __("POR VENCER"), i: "calendar-clock", mod: "por-vencer", unit: "obligation" },
			{ key: "vencida", label: __("VENCIDA"), i: "triangle-alert", mod: "vencida", unit: "obligation" },
			{ key: "cobrado_mes", label: __("COBRADO EN {0}", [esc(month)]), i: "circle-check-big", mod: "cobrado", unit: "payment" },
			{ key: "por_confirmar", label: __("PAGOS POR CONFIRMAR"), i: "hourglass", mod: "por-confirmar", unit: "unconfirmed" },
		];
		const html = cards
			.map((c) => {
				const v = k[c.key] || { amount: 0, count: 0 };
				return `
					<div class="fg-kpi fg-cartera-kpi fg-cartera-kpi--${c.mod}" data-kpi="${c.key}">
						<div class="fg-cartera-kpi-top">
							<div class="fg-kpi-icon">${icon(c.i)}</div>
							<div class="fg-kpi-label">${c.label}</div>
						</div>
						${money_html(v.amount, currency, "fg-cartera-kpi-amount")}
						<div class="fg-cartera-kpi-count">${count_label(v.count, c.unit)}</div>
					</div>
				`;
			})
			.join("");
		return `<div class="fg-kpis fg-cartera-kpis">${html}</div>`;
	}

	render_sync_button_html() {
		if (!can_sync()) return "";
		return `
			<button type="button" class="fg-btn fg-btn--ghost fg-cartera-sync-btn" ${this.syncing ? "disabled" : ""}>
				${icon("refresh-ccw", "fg-icon-sm")} ${__("SINCRONIZAR")}
			</button>
		`;
	}

	// -- Chips ----------------------------------------------------------
	render_chips_html() {
		return FILTERS.map(
			(f) => `
				<button type="button" class="fg-cartera-chip ${this.list_filter === f.key ? "is-active" : ""}" data-filter="${f.key}">${
				f.label
			}</button>
			`
		).join("");
	}

	// -- Tarjetas -------------------------------------------------------
	render_cards_html() {
		if (this.list_error) {
			return `
				<div class="fg-cartera-error">
					<div>${__("No se pudo cargar la cartera.")}</div>
					<button type="button" class="fg-btn fg-btn--solid-primary fg-cartera-retry-list">${icon("refresh-cw", "fg-icon-sm")} ${__(
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
						<div>${__("Prueba buscando por cliente, pedido, pick list o recorrido.")}</div>
					</div>
				`;
			}
			return `<div class="fg-empty fg-cartera-empty">${empty_message(this.list_filter)}</div>`;
		}
		return items.map((item) => render_obligation_card(item)).join("");
	}

	render_pagination_html() {
		if (!this.list || this.list_error || !this.list.total) return "";
		const total = cint(this.list.total);
		const page = cint(this.list.page) || 1;
		const page_length = cint(this.list.page_length) || PAGE_LENGTH;
		const start = (page - 1) * page_length + 1;
		const end = Math.min(page * page_length, total);
		return `
			<div class="fg-cartera-pagination-info">${__("Mostrando {0} a {1} de {2}", [start, end, total])}</div>
			<div class="fg-cartera-pagination-controls">
				<button type="button" class="fg-cartera-pagination-btn fg-cartera-pagination-prev" title="${__("Anterior")}" ${
			page <= 1 ? "disabled" : ""
		}>${icon("chevron-left")}</button>
				<span class="fg-cartera-pagination-page">${page}</span>
				<button type="button" class="fg-cartera-pagination-btn fg-cartera-pagination-next" title="${__("Siguiente")}" ${
			this.list.has_more ? "" : "disabled"
		}>${icon("chevron-right")}</button>
			</div>
		`;
	}

	// Solo reemplaza tarjetas + paginación -- nunca el input ni los chips,
	// para no perder foco mientras el usuario escribe. Los KPIs no se
	// vuelven a pedir: no dependen del filtro ni de la búsqueda.
	refresh_list() {
		this.$body.find(".fg-cartera-cards").html(
			`<div class="fg-skeleton-cards"><div class="fg-skeleton"></div><div class="fg-skeleton"></div></div>`
		);
		this.$body.find(".fg-cartera-pagination").html("");
		return this.fetch_list().then((is_current) => {
			if (!is_current || this.view !== "dashboard") return;
			this.$body.find(".fg-cartera-cards").html(this.render_cards_html());
			this.$body.find(".fg-cartera-pagination").html(this.render_pagination_html());
		});
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

		$b.find(".fg-cartera-chips").on("click", ".fg-cartera-chip", (e) => {
			const key = $(e.currentTarget).data("filter");
			if (!key || key === this.list_filter) return;
			this.list_filter = key;
			this.list_page = 1;
			$b.find(".fg-cartera-chip").removeClass("is-active");
			$(e.currentTarget).addClass("is-active");
			this.refresh_list();
		});

		$b.find(".fg-cartera-cards").on("click", ".fg-cartera-card-detail", (e) => {
			this.open_detail($(e.currentTarget).attr("data-name"));
		});
		$b.find(".fg-cartera-cards").on("click", ".fg-cartera-retry-list", () => this.refresh_list());
		$b.find(".fg-cartera-kpis-slot").on("click", ".fg-cartera-retry-kpis", () => this.reload_kpis());

		$b.find(".fg-cartera-pagination").on("click", ".fg-cartera-pagination-prev", () => {
			if (this.list_page <= 1) return;
			this.list_page -= 1;
			this.refresh_list();
		});
		$b.find(".fg-cartera-pagination").on("click", ".fg-cartera-pagination-next", () => {
			if (!this.list || !this.list.has_more) return;
			this.list_page += 1;
			this.refresh_list();
		});

		$b.find(".fg-cartera-sync-btn").on("click", () => this.sync());
		$b.find(".fg-cm-open-btn").on("click", () => this.open_bulk_dialog());
	}

	reload_kpis() {
		return this.fetch_dashboard().then(() => {
			if (this.view === "dashboard") this.$body.find(".fg-cartera-kpis-slot").html(this.render_kpis_html());
		});
	}

	// -- SINCRONIZAR (reconciliador idempotente de 27.1) ------------------
	sync() {
		if (this.syncing || !can_sync()) return;
		this.syncing = true;
		this.$body.find(".fg-cartera-sync-btn").prop("disabled", true);
		this.call("sync_missing_obligations", {}, { freeze: true, freeze_message: __("Sincronizando cartera...") })
			.then((res) => {
				const r = res || {};
				frappe.msgprint({
					title: __("Sincronización de cartera"),
					indicator: cint(r.failed) ? "orange" : "green",
					message: sync_summary_html(r),
				});
				return this.load_all();
			})
			.catch(() => {
				// frappe.call() ya mostró el error real del servidor.
			})
			.finally(() => {
				this.syncing = false;
				this.$body.find(".fg-cartera-sync-btn").prop("disabled", false);
			});
	}

	// =====================================================================
	// Cartera masiva -- "Autorizar cobros y pagos" (System Manager),
	// api/cartera_masiva.py. Fase A: autoriza (CONFIRMAR PAGO) los pagos
	// que el conductor ya registró con comprobante; nunca crea un pago ni
	// cambia un valor. "Registrar pagos" queda deshabilitado (Próximamente).
	// Toda regla vive en el servidor (elegibilidad, filtros, permisos,
	// revalidación bajo bloqueo, idempotencia); aquí solo se recogen los
	// filtros, se muestra la vista previa del servidor y se sigue el
	// progreso del lote. Los totales de la selección se suman en CENTAVOS
	// enteros (índice exacto del servidor), nunca con decimales flotantes.
	// =====================================================================
	render_bulk_button_html() {
		if (!frappe.user.has_role("System Manager")) return "";
		return `
			<button type="button" class="fg-btn fg-cm-open-btn" title="${__("Autoriza en bloque los cobros ya registrados por el conductor")}">
				${icon("check-check", "fg-icon-sm")} ${__("Autorizar cobros y pagos")}
			</button>
		`;
	}

	cm_call(method, args) {
		return new Promise((resolve, reject) => {
			frappe.call({
				method: "fabergray_erp.api.cartera_masiva." + method,
				args: args || {},
				callback: (r) => resolve(r.message),
				error: (r) => reject(r),
			});
		});
	}

	open_bulk_dialog() {
		const today = frappe.datetime.get_today();
		this.cm = {
			screen: "authorize", // "authorize" | "history"
			options: null,
			date_type: "facturacion",
			date_from: today.slice(0, 8) + "01",
			date_to: today,
			customers: new Map(), // customer -> label
			customer_query: "",
			customer_results: [],
			issuer: "",
			method: "",
			status: "todos",
			search: "",
			view: "eligible",
			preview: null,
			index: new Map(), // obligation -> [customer, cents]
			select_all: true,
			excluded: new Set(),
			selected: new Set(),
			reason: "",
			review: false,
			request_id: null,
			batch: null,
			history: null,
			busy: false,
		};
		const dialog = new frappe.ui.Dialog({
			title: `<span class="fg-cm-title">${icon("check-check")}${__("Autorizar cobros y pagos")}</span>`,
			size: "extra-large",
			fields: [{ fieldtype: "HTML", fieldname: "cm_html" }],
		});
		dialog.$wrapper.addClass("fg-cm-dialog");
		dialog.onhide = () => {
			clearTimeout(this._cm_poll);
			clearTimeout(this._cm_customer_debounce);
			if (this.cm && this.cm.batch) this.load_all();
		};
		this.cm_dialog = dialog;
		this.$cm = dialog.fields_dict.cm_html.$wrapper;
		this.bind_bulk_events();
		dialog.show();
		this.$cm.html(`<div class="fg-cm"><div class="fg-cm-muted">${__("Cargando...")}</div></div>`);
		this.cm_call("get_bulk_authorization_options")
			.then((options) => {
				this.cm.options = options;
				this.cm.date_type = options.default_date_type || "facturacion";
				if (options.active_batch) this.show_bulk_progress(options.active_batch.name);
				else this.render_bulk();
			})
			.catch(() => this.cm_dialog.hide());
	}

	// -- Selection (exact, in integer cents) ------------------------------
	cm_selected_entries() {
		const e = this.cm;
		if (!e.preview) return [];
		const out = [];
		e.index.forEach((value, name) => {
			const chosen = e.select_all ? !e.excluded.has(name) : e.selected.has(name);
			if (chosen) out.push([name, value[0], value[1]]);
		});
		return out;
	}

	cm_selection_totals() {
		const by = new Map();
		let cents = 0;
		const entries = this.cm_selected_entries();
		for (const [, customer, c] of entries) {
			cents += c;
			const row = by.get(customer) || { count: 0, cents: 0 };
			row.count += 1;
			row.cents += c;
			by.set(customer, row);
		}
		return { count: entries.length, cents, by };
	}

	cm_can_confirm() {
		const e = this.cm;
		const t = this.cm_selection_totals();
		const limits = (e.options && e.options.limits) || {};
		return (
			!e.busy &&
			e.preview &&
			e.preview.index_complete &&
			t.count > 0 &&
			t.count <= (limits.max_lines || 2000) &&
			e.reason.trim().length >= (limits.reason_min_length || 10) &&
			e.review
		);
	}

	// -- Render -----------------------------------------------------------
	render_bulk() {
		const e = this.cm;
		if (e.batch) return this.render_bulk_progress();
		const modes = ((e.options && e.options.modes) || [])
			.map(
				(m) => `<button type="button" class="fg-cm-mode-btn ${m.enabled && e.screen === "authorize" ? "is-active" : ""}" data-screen="authorize" ${
					m.enabled ? "" : "disabled"
				}>${m.enabled ? icon("check-check", "fg-icon-sm") : icon("wallet", "fg-icon-sm")} ${esc(m.label)}${
					m.enabled ? "" : ` <span class="fg-cm-soon">${esc(m.note || __("Próximamente"))}</span>`
				}</button>`
			)
			.join("");
		const tabs = `<div class="fg-cm-modes">${modes}
			<button type="button" class="fg-cm-mode-btn ${e.screen === "history" ? "is-active" : ""}" data-screen="history">${icon("history", "fg-icon-sm")} ${__("Historial de lotes")}</button>
		</div>`;
		if (e.screen === "history") {
			this.$cm.html(`<div class="fg-cm">${tabs}<div class="fg-cm-history">${this.render_history_html()}</div></div>`);
			return;
		}
		this.$cm.html(`
			<div class="fg-cm">
				${tabs}
				<div class="fg-cm-criteria">
					${icon("info", "fg-icon-sm")}
					<span>${__(
						"Autoriza los cobros que el conductor ya registró con comprobante (POR CONFIRMAR). No crea pagos ni cambia valores, saldos o vencimientos: solo deja la confirmación auditada de cada cobro."
					)}</span>
				</div>
				<div class="fg-cm-filters">${this.render_filters_html()}</div>
				<button type="button" class="fg-btn fg-cm-search-btn" ${e.busy ? "disabled" : ""}>${icon("search", "fg-icon-sm")} ${__("Buscar cobros")}</button>
				<div class="fg-cm-results">${e.preview ? this.render_bulk_results_html() : ""}</div>
			</div>
		`);
	}

	render_filters_html() {
		const e = this.cm;
		const o = e.options || {};
		const today = frappe.datetime.get_today();
		const opt = (value, label, current) => `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(label)}</option>`;
		const chips = Array.from(e.customers.entries())
			.map(([name, label]) => `<span class="fg-cm-chip">${esc(label)}<button type="button" class="fg-cm-chip-remove" data-customer="${esc(name)}" title="${__("Quitar")}">×</button></span>`)
			.join("");
		const suggestions = e.customer_results.length
			? `<div class="fg-cm-suggestions">${e.customer_results
					.map(
						(c) =>
							`<button type="button" class="fg-cm-suggestion" data-customer="${esc(c.customer)}" data-label="${esc(
								c.customer_commercial_name || c.customer_name || c.customer
							)}"><span>${esc(c.customer_commercial_name || c.customer_name || c.customer)}${
								c.customer_commercial_name && c.customer_name ? ` <span class="fg-cm-muted">${esc(c.customer_name)}</span>` : ""
							}</span><span class="fg-cm-muted">${cint(c.count)}</span></button>`
					)
					.join("")}</div>`
			: "";
		return `
			<label class="fg-cm-field"><span>${__("Tipo de fecha")}</span>
				<select class="fg-cm-date-type">${(o.date_types || []).map((d) => opt(d.key, d.label, e.date_type)).join("")}</select></label>
			<label class="fg-cm-field"><span>${__("Fecha inicial")}</span><input type="date" class="fg-cm-date-from" value="${esc(e.date_from)}"></label>
			<label class="fg-cm-field"><span>${__("Fecha final")}</span><input type="date" class="fg-cm-date-to" value="${esc(e.date_to)}"></label>
			<div class="fg-cm-field fg-cm-field--wide fg-cm-customers"><span>${__("Clientes")} <span class="fg-cm-muted">${
				e.customers.size ? __("{0} seleccionados", [e.customers.size]) : __("Todos los clientes")
			}</span></span>
				${chips ? `<div class="fg-cm-chips">${chips}</div>` : ""}
				<input type="search" class="fg-cm-customer-input" placeholder="${__("Buscar y agregar clientes (vacío = todos)")}" value="${esc(e.customer_query)}" autocomplete="off">
				${suggestions}
			</div>
			<label class="fg-cm-field"><span>${__("Emisor")}</span>
				<select class="fg-cm-issuer">${opt("", __("Todos los emisores"), e.issuer)}${(o.issuers || []).map((i) => opt(i, i, e.issuer)).join("")}</select></label>
			<label class="fg-cm-field"><span>${__("Forma de pago de la factura")}</span>
				<select class="fg-cm-method">${opt("", __("Todas"), e.method)}${(o.invoice_payment_methods || []).map((m) => opt(m.key, m.label, e.method)).join("")}</select></label>
			<label class="fg-cm-field"><span>${__("Estado")}</span>
				<select class="fg-cm-status">${(o.statuses || []).map((s) => opt(s.key, s.label, e.status)).join("")}</select></label>
			<label class="fg-cm-field fg-cm-field--wide"><span>${__("Buscar")}</span>
				<input type="search" class="fg-cm-search-input" maxlength="140" placeholder="${__("Cliente, pedido, pick list, recorrido o referencia del pago")}" value="${esc(e.search)}"></label>
			<div class="fg-cm-muted fg-cm-field--wide">${__("Rango inclusivo según la hora del sitio (hoy: {0}). Las obligaciones sin la fecha elegida no se incluyen: nunca se inventa una fecha.", [format_date(today)])}</div>
		`;
	}

	render_bulk_results_html() {
		const e = this.cm;
		const p = e.preview;
		const s = p.summary;
		const t = this.cm_selection_totals();
		const limits = (e.options && e.options.limits) || {};
		const currency = (p.rows[0] && p.rows[0].currency) || "COP";
		const excluded_html = s.excluded_by_reason.length
			? `<ul class="fg-cm-reasons">${s.excluded_by_reason
					.map(
						(r) =>
							`<li class="${r.needs_review ? "is-review" : ""}"><strong>${cint(r.count)}</strong> ${esc(r.label)}${
								r.needs_review ? ` <span class="fg-cm-tag fg-cm-tag--review">${__("REVISAR")}</span>` : ""
							}</li>`
					)
					.join("")}</ul>`
			: `<div class="fg-cm-muted">${__("Ningún cobro excluido.")}</div>`;
		const names = new Map((s.by_customer || []).map((c) => [c.customer, c.customer_commercial_name || c.customer_name || c.customer]));
		const customer_rows = Array.from(t.by.entries())
			.sort((a, b) => b[1].cents - a[1].cents)
			.map(
				([customer, row]) =>
					`<tr><td>${esc(names.get(customer) || customer)}</td><td class="fg-cm-num">${row.count}</td><td class="fg-cm-num">${format_money(row.cents / 100, currency)}</td></tr>`
			)
			.join("");
		const over_limit = t.count > (limits.max_lines || 2000);
		return `
			<div class="fg-cm-kpis">
				<div class="fg-cm-kpi"><span>${__("Cobros encontrados")}</span><strong>${cint(s.found)}</strong></div>
				<div class="fg-cm-kpi fg-cm-kpi--ok"><span>${__("Por confirmar")}</span><strong>${cint(s.eligible)}</strong></div>
				<div class="fg-cm-kpi fg-cm-kpi--muted"><span>${__("Excluidos")}</span><strong>${cint(s.excluded)}</strong></div>
				<div class="fg-cm-kpi"><span>${__("Valor por confirmar")}</span><strong>${format_money(s.total_amount, currency)}</strong></div>
			</div>
			${
				cint(s.missing_date)
					? `<div class="fg-cm-warning">${icon("triangle-alert", "fg-icon-sm")} ${__("{0} cobros reportados no tienen «{1}» y no se incluyen en el rango.", [
							cint(s.missing_date),
							esc(p.date_type_label),
					  ])}</div>`
					: ""
			}
			<div class="fg-cm-tabs">
				<button type="button" class="fg-cm-tab ${e.view === "eligible" ? "is-active" : ""}" data-view="eligible">${__("Por confirmar")} (${cint(s.eligible)})</button>
				<button type="button" class="fg-cm-tab ${e.view === "excluded" ? "is-active" : ""}" data-view="excluded">${__("Excluidos")} (${cint(s.excluded)})</button>
			</div>
			${e.view === "eligible" ? this.render_bulk_table_html() : `<div class="fg-cm-excluded-summary">${excluded_html}</div>${this.render_bulk_table_html()}`}
			<div class="fg-cm-by-customer">
				<div class="fg-cm-label">${__("Total por cliente")} <span class="fg-cm-muted">(${__("selección actual, todas las páginas")})</span></div>
				<div class="fg-cm-table-wrap"><table class="fg-cm-table"><thead><tr><th>${__("Cliente")}</th><th class="fg-cm-num">${__("Cobros")}</th><th class="fg-cm-num">${__("Valor")}</th></tr></thead><tbody>
					${customer_rows || `<tr><td colspan="3" class="fg-cm-empty">${__("Sin cobros seleccionados.")}</td></tr>`}
					<tr class="fg-cm-total-row"><td>${__("Total general")}</td><td class="fg-cm-num">${t.count}</td><td class="fg-cm-num">${format_money(t.cents / 100, currency)}</td></tr>
				</tbody></table></div>
			</div>
			<div class="fg-cm-confirm-box">
				<div class="fg-cm-selection">${__("Seleccionados")}: <strong>${t.count}</strong> ${__("cobros")} · ${__("Valor seleccionado")}: <strong>${format_money(
			t.cents / 100,
			currency
		)}</strong></div>
				${over_limit ? `<div class="fg-cm-warning">${icon("triangle-alert", "fg-icon-sm")} ${__("Un lote admite como máximo {0} cobros; reduce el rango o los filtros.", [limits.max_lines || 2000])}</div>` : ""}
				<label class="fg-cm-field fg-cm-reason"><span>${__("Motivo de la autorización (obligatorio)")}</span><textarea class="fg-cm-reason-input" rows="2" maxlength="500" placeholder="${__(
					"Ej.: comprobantes del conductor revisados contra el extracto bancario de noviembre"
				)}">${esc(e.reason)}</textarea></label>
				<label class="fg-cm-check"><input type="checkbox" class="fg-cm-review" ${e.review ? "checked" : ""}> ${__("Confirmo que revisé estos cobros y sus comprobantes.")}</label>
				<div class="fg-cm-irreversible">${icon("lock", "fg-icon-sm")} ${__(
					"Esta operación no se puede deshacer desde la interfaz. Cada cobro quedará CONFIRMADO a tu nombre y con el número de lote; no se crea ningún pago nuevo ni cambia ningún valor."
				)}</div>
				<button type="button" class="fg-btn fg-cm-confirm-btn" ${this.cm_can_confirm() ? "" : "disabled"}>${icon("check-check", "fg-icon-sm")} ${__("Confirmar autorización")}</button>
			</div>
		`;
	}

	render_bulk_table_html() {
		const e = this.cm;
		const p = e.preview;
		const rows = p.rows || [];
		const eligible = e.view === "eligible";
		const pages = Math.max(Math.ceil(p.total_in_view / p.page_length), 1);
		const is_checked = (r) => (e.select_all ? !e.excluded.has(r.obligation) : e.selected.has(r.obligation));
		const body = rows.length
			? rows
					.map(
						(r) => `<tr>
							${eligible ? `<td><input type="checkbox" class="fg-cm-row-check" data-ob="${esc(r.obligation)}" ${is_checked(r) ? "checked" : ""}></td>` : ""}
							<td><strong>${esc(r.commercial_name || "—")}</strong><div class="fg-cm-muted">${esc(r.obligation)} · ${esc(r.pick_list || "")}</div></td>
							<td>${esc(r.customer_commercial_name || r.customer_name || "—")}${r.customer_commercial_name ? `<div class="fg-cm-muted">${esc(r.customer_name || "")}</div>` : ""}</td>
							<td>${esc(r.invoice_issuer || "—")}<div class="fg-cm-muted">${esc(r.invoice_payment_method || __("Sin forma de pago"))}</div></td>
							<td>${format_date(r.invoice_date)}<div class="fg-cm-muted">${__("Entrega")} ${format_date(r.delivery_date)}</div></td>
							${
								eligible
									? `<td>${format_date(r.payment_date)}<div class="fg-cm-muted">${esc(r.driver_payment || "")}</div></td>
									   <td class="fg-cm-num">${format_money(r.amount, r.currency)}</td>`
									: `<td>${esc(r.reason || "")}${r.reason_detail ? `<div class="fg-cm-muted">${esc(r.reason_detail)}</div>` : ""}${
											r.needs_review ? ` <span class="fg-cm-tag fg-cm-tag--review">${__("REVISAR")}</span>` : ""
									  }</td>`
							}
						</tr>`
					)
					.join("")
			: `<tr><td colspan="7" class="fg-cm-empty">${__("Sin cobros en esta vista.")}</td></tr>`;
		const head = eligible
			? `<th><input type="checkbox" class="fg-cm-select-all" ${e.select_all && !e.excluded.size ? "checked" : ""} title="${__(
					"Seleccionar todos los resultados (todas las páginas)"
			  )}"></th><th>${__("Pedido")}</th><th>${__("Cliente")}</th><th>${__("Emisor / forma de pago")}</th><th>${__("Facturado")}</th><th>${__("Pago reportado")}</th><th class="fg-cm-num">${__("Valor")}</th>`
			: `<th>${__("Pedido")}</th><th>${__("Cliente")}</th><th>${__("Emisor / forma de pago")}</th><th>${__("Facturado")}</th><th>${__("Motivo")}</th>`;
		return `
			${eligible ? `<div class="fg-cm-select-all-row">${__("Casilla del encabezado: selecciona los {0} cobros de todas las páginas.", [cint(p.summary.eligible)])}</div>` : ""}
			<div class="fg-cm-table-wrap"><table class="fg-cm-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>
			<div class="fg-cm-pager">
				<button type="button" class="fg-btn fg-cm-page" data-page="${p.page - 1}" ${p.page <= 1 ? "disabled" : ""}>${icon("chevron-left", "fg-icon-sm")}</button>
				<span>${__("Página {0} de {1}", [p.page, pages])}</span>
				<button type="button" class="fg-btn fg-cm-page" data-page="${p.page + 1}" ${p.has_more ? "" : "disabled"}>${icon("chevron-right", "fg-icon-sm")}</button>
			</div>
		`;
	}

	render_history_html() {
		const h = this.cm.history;
		if (!h) return `<div class="fg-cm-muted">${__("Cargando...")}</div>`;
		const rows = (h.items || [])
			.map(
				(b) => `<tr class="fg-cm-history-row" data-batch="${esc(b.name)}">
					<td><strong>${esc(b.name)}</strong><div class="fg-cm-muted">${esc(b.mode)}</div></td>
					<td>${format_datetime(b.queued_on)}<div class="fg-cm-muted">${esc(b.requested_by_name || b.requested_by || "")}</div></td>
					<td>${esc(b.date_type || "")}<div class="fg-cm-muted">${format_date(b.date_from)} – ${format_date(b.date_to)}</div></td>
					<td><span class="fg-cm-tag ${cint(b.failed_count) ? "fg-cm-tag--fail" : ""}">${esc(b.status)}</span></td>
					<td class="fg-cm-num">${cint(b.authorized_count)} / ${cint(b.total_selected)}<div class="fg-cm-muted">${cint(b.skipped_count)} ${__("omitidos")} · ${cint(
					b.failed_count
				)} ${__("fallidos")}</div></td>
					<td class="fg-cm-num">${format_money(b.authorized_amount, b.currency)}</td>
				</tr>`
			)
			.join("");
		const pages = Math.max(Math.ceil(cint(h.total) / cint(h.page_length || 20)), 1);
		return `
			<div class="fg-cm-table-wrap"><table class="fg-cm-table"><thead><tr><th>${__("Lote")}</th><th>${__("Solicitado")}</th><th>${__("Filtro de fechas")}</th><th>${__(
			"Estado"
		)}</th><th class="fg-cm-num">${__("Autorizados")}</th><th class="fg-cm-num">${__("Valor autorizado")}</th></tr></thead><tbody>
				${rows || `<tr><td colspan="6" class="fg-cm-empty">${__("Todavía no hay lotes.")}</td></tr>`}
			</tbody></table></div>
			<div class="fg-cm-pager">
				<button type="button" class="fg-btn fg-cm-history-page" data-page="${cint(h.page) - 1}" ${cint(h.page) <= 1 ? "disabled" : ""}>${icon("chevron-left", "fg-icon-sm")}</button>
				<span>${__("Página {0} de {1}", [cint(h.page), pages])}</span>
				<button type="button" class="fg-btn fg-cm-history-page" data-page="${cint(h.page) + 1}" ${h.has_more ? "" : "disabled"}>${icon("chevron-right", "fg-icon-sm")}</button>
			</div>
		`;
	}

	// -- Events -----------------------------------------------------------
	bind_bulk_events() {
		const $w = this.$cm;
		const e = () => this.cm;
		const reset_preview = () => {
			e().preview = null;
			e().index = new Map();
			e().request_id = null;
		};
		const on_filter = (selector, key) =>
			$w.on("change", selector, (ev) => {
				e()[key] = ev.target.value;
				reset_preview();
				this.refresh_bulk_results();
			});
		on_filter(".fg-cm-date-type", "date_type");
		on_filter(".fg-cm-date-from", "date_from");
		on_filter(".fg-cm-date-to", "date_to");
		on_filter(".fg-cm-issuer", "issuer");
		on_filter(".fg-cm-method", "method");
		on_filter(".fg-cm-status", "status");
		$w.on("input", ".fg-cm-search-input", (ev) => {
			e().search = ev.target.value;
			reset_preview();
			this.refresh_bulk_results();
		});
		$w.on("input", ".fg-cm-customer-input", (ev) => {
			e().customer_query = ev.target.value;
			clearTimeout(this._cm_customer_debounce);
			this._cm_customer_debounce = setTimeout(() => this.search_customers(), SEARCH_DEBOUNCE_MS);
		});
		$w.on("click", ".fg-cm-suggestion", (ev) => {
			const $b = $(ev.currentTarget);
			e().customers.set(String($b.attr("data-customer")), String($b.attr("data-label")));
			e().customer_query = "";
			e().customer_results = [];
			reset_preview();
			this.render_bulk();
		});
		$w.on("click", ".fg-cm-chip-remove", (ev) => {
			e().customers.delete(String($(ev.currentTarget).attr("data-customer")));
			reset_preview();
			this.render_bulk();
		});
		$w.on("click", ".fg-cm-mode-btn", (ev) => {
			const screen = $(ev.currentTarget).data("screen");
			if ($(ev.currentTarget).prop("disabled") || screen === e().screen) return;
			e().screen = screen;
			if (screen === "history") this.load_history(1);
			this.render_bulk();
		});
		$w.on("click", ".fg-cm-history-page", (ev) => this.load_history(cint($(ev.currentTarget).data("page"))));
		$w.on("click", ".fg-cm-history-row", (ev) => this.show_bulk_progress(String($(ev.currentTarget).attr("data-batch"))));
		$w.on("click", ".fg-cm-search-btn", () => this.load_bulk_preview(1, true));
		$w.on("click", ".fg-cm-tab", (ev) => {
			e().view = $(ev.currentTarget).data("view");
			this.load_bulk_preview(1, false);
		});
		$w.on("click", ".fg-cm-page", (ev) => this.load_bulk_preview(cint($(ev.currentTarget).data("page")), false));
		$w.on("change", ".fg-cm-row-check", (ev) => {
			const ob = String($(ev.currentTarget).attr("data-ob"));
			const target = e().select_all ? e().excluded : e().selected;
			const wants = e().select_all ? !ev.target.checked : ev.target.checked;
			if (wants) target.add(ob);
			else target.delete(ob);
			this.refresh_bulk_results();
		});
		$w.on("change", ".fg-cm-select-all", (ev) => {
			e().select_all = ev.target.checked;
			e().excluded = new Set();
			e().selected = new Set();
			this.refresh_bulk_results();
		});
		$w.on("input", ".fg-cm-reason-input", (ev) => {
			e().reason = ev.target.value;
			this.refresh_bulk_confirm();
		});
		$w.on("change", ".fg-cm-review", (ev) => {
			e().review = ev.target.checked;
			this.refresh_bulk_confirm();
		});
		$w.on("click", ".fg-cm-confirm-btn:not(.fg-cm-done-btn)", () => this.confirm_bulk());
		$w.on("click", ".fg-cm-resume-btn", () => this.cm_recovery("resume_bulk_authorization"));
		$w.on("click", ".fg-cm-close-batch-btn", () => this.cm_recovery("close_bulk_authorization"));
		$w.on("click", ".fg-cm-back-btn", () => {
			clearTimeout(this._cm_poll);
			e().batch = null;
			if (e().screen === "history") this.load_history(1);
			this.render_bulk();
		});
		$w.on("click", ".fg-cm-done-btn", () => this.cm_dialog.hide());
	}

	refresh_bulk_results() {
		this.$cm.find(".fg-cm-results").html(this.cm.preview ? this.render_bulk_results_html() : "");
	}

	refresh_bulk_confirm() {
		this.$cm.find(".fg-cm-confirm-btn:not(.fg-cm-done-btn)").prop("disabled", !this.cm_can_confirm());
	}

	cm_filters() {
		const e = this.cm;
		return {
			date_type: e.date_type,
			date_from: e.date_from,
			date_to: e.date_to,
			customers: e.customers.size ? JSON.stringify(Array.from(e.customers.keys())) : null,
			issuer: e.issuer || null,
			invoice_payment_method: e.method || null,
			status: e.status,
			search: (e.search || "").trim() || null,
		};
	}

	search_customers() {
		const e = this.cm;
		const query = (e.customer_query || "").trim();
		const seq = (this._cm_customer_seq = (this._cm_customer_seq || 0) + 1);
		if (!query) {
			e.customer_results = [];
			this.render_bulk();
			return;
		}
		this.cm_call("search_bulk_customers", { search: query, limit: 20 })
			.then((rows) => {
				if (seq !== this._cm_customer_seq) return;
				const fresh = [];
				for (const r of rows || []) if (!e.customers.has(r.customer)) fresh.push(r);
				e.customer_results = fresh;
				this.render_bulk();
				const $input = this.$cm.find(".fg-cm-customer-input");
				$input.trigger("focus");
				const el = $input.get(0);
				if (el && el.setSelectionRange) el.setSelectionRange(el.value.length, el.value.length);
			})
			.catch(() => {});
	}

	load_bulk_preview(page, refresh) {
		const e = this.cm;
		if (e.busy) return;
		e.busy = true;
		this.$cm.find(".fg-cm-search-btn").prop("disabled", true).html(`${icon("loader-circle", "fg-icon-sm fg-spin")} ${__("Buscando...")}`);
		if (refresh) {
			e.select_all = true;
			e.excluded = new Set();
			e.selected = new Set();
			e.view = "eligible";
			e.request_id = null;
		}
		this.cm_call("get_bulk_authorization_preview", Object.assign(this.cm_filters(), { view: e.view, page: page, refresh: refresh ? 1 : 0 }))
			.then((p) => {
				e.preview = p;
				e.index = new Map((p.eligible_index || []).map((r) => [r[0], [r[1], cint(r[2])]]));
			})
			.catch(() => {})
			.finally(() => {
				e.busy = false;
				this.render_bulk();
			});
	}

	confirm_bulk() {
		const e = this.cm;
		if (!this.cm_can_confirm()) return;
		const t = this.cm_selection_totals();
		const currency = (e.preview.rows[0] && e.preview.rows[0].currency) || "COP";
		frappe.confirm(
			__("Se autorizarán {0} cobros por {1} ({2} clientes). No se crea ningún pago nuevo. Esta operación no se puede deshacer. ¿Continuar?", [
				t.count,
				format_money(t.cents / 100, currency),
				t.by.size,
			]),
			() => {
				e.busy = true;
				e.request_id = e.request_id || frappe.utils.get_random(32);
				this.refresh_bulk_confirm();
				this.cm_call("confirm_bulk_authorization",
					Object.assign(this.cm_filters(), {
						reason: e.reason.trim(),
						review_confirmed: 1,
						select_all: e.select_all ? 1 : 0,
						obligations: e.select_all ? null : JSON.stringify(Array.from(e.selected)),
						excluded_obligations: e.select_all ? JSON.stringify(Array.from(e.excluded)) : null,
						client_request_id: e.request_id,
					})
				)
					.then((status) => {
						if (status.not_eligible && status.not_eligible.length) {
							frappe.show_alert({ message: __("{0} cobros ya no se podían autorizar y se omitieron.", [status.not_eligible.length]), indicator: "orange" }, 7);
						}
						this.show_bulk_progress(status.name, status);
					})
					.catch(() => {
						e.busy = false;
						this.refresh_bulk_confirm();
					});
			}
		);
	}

	load_history(page) {
		this.cm_call("get_bulk_authorization_history", { page: page })
			.then((h) => {
				this.cm.history = h;
				if (this.cm.screen === "history" && !this.cm.batch) this.$cm.find(".fg-cm-history").html(this.render_history_html());
			})
			.catch(() => {});
	}

	// -- Progress / summary -----------------------------------------------
	show_bulk_progress(batch_name, status) {
		this.cm.batch = { name: batch_name, status: status || null };
		this.render_bulk_progress();
		this.poll_bulk_status();
	}

	poll_bulk_status() {
		clearTimeout(this._cm_poll);
		const batch = this.cm && this.cm.batch;
		if (!batch) return;
		this.cm_call("get_bulk_authorization_status", { batch_name: batch.name })
			.then((st) => {
				if (this.cm.batch !== batch) return;
				batch.status = st;
				this.render_bulk_progress();
				if (!st.finished && !st.can_resume && this.cm_dialog.display) {
					this._cm_poll = setTimeout(() => this.poll_bulk_status(), 1500);
				}
			})
			.catch(() => {});
	}

	cm_recovery(method) {
		const batch = this.cm.batch;
		const label =
			method === "resume_bulk_authorization"
				? __("¿Reanudar el lote desde los cobros pendientes? Los ya autorizados no se repiten.")
				: __("¿Cerrar el lote sin procesar los cobros pendientes?");
		frappe.confirm(label, () => {
			this.cm_call(method, { batch_name: batch.name }).then((st) => {
				batch.status = st;
				this.render_bulk_progress();
				this.poll_bulk_status();
			});
		});
	}

	render_bulk_progress() {
		const st = this.cm.batch.status;
		if (!st) {
			this.$cm.html(`<div class="fg-cm fg-cm-progress"><div class="fg-cm-muted">${__("Cargando...")}</div></div>`);
			return;
		}
		const pct = Math.min(Math.max(Number(st.progress_percent) || 0, 0), 100);
		const issues = (st.issues || [])
			.map(
				(i) =>
					`<tr><td><strong>${esc(i.commercial_name || i.cartera_obligacion)}</strong><div class="fg-cm-muted">${esc(i.cartera_obligacion)}</div></td><td>${esc(
						i.customer_name || "—"
					)}</td><td class="fg-cm-num">${format_money(i.amount, st.currency)}</td><td><span class="fg-cm-tag ${i.result === "Fallido" ? "fg-cm-tag--fail" : ""}">${esc(
						i.result
					)}</span></td><td>${esc(i.message || "")}</td></tr>`
			)
			.join("");
		const customers = (st.by_customer || [])
			.map((c) => `<tr><td>${esc(c.customer_name || c.customer)}</td><td class="fg-cm-num">${cint(c.count)}</td><td class="fg-cm-num">${format_money(c.amount, st.currency)}</td></tr>`)
			.join("");
		const head = st.finished
			? `<div class="fg-cm-done ${st.failed ? "has-errors" : ""}">${icon(st.failed ? "triangle-alert" : "circle-check")} ${
					st.status === "Cerrado" ? __("Lote cerrado") : st.failed ? __("Autorización finalizada con errores") : __("Autorización finalizada")
			  } · ${esc(st.name)}</div>`
			: st.can_resume
			? `<div class="fg-cm-done has-errors">${icon("triangle-alert")} ${__(
					"El proceso se interrumpió o no avanza. Puedes reanudarlo (no repite cobros ya autorizados) o cerrar el lote."
			  )}</div>`
			: `<div class="fg-cm-running">${icon("loader-circle", "fg-spin")} ${__("Procesando en segundo plano… puedes cerrar esta ventana; el proceso continúa.")}</div>`;
		this.$cm.html(`
			<div class="fg-cm fg-cm-progress">
				${head}
				<div class="fg-cm-bar-track"><div class="fg-cm-bar-fill" style="width:${pct}%"></div><span>${pct}%</span></div>
				<div class="fg-cm-kpis">
					<div class="fg-cm-kpi"><span>${__("Seleccionados")}</span><strong>${cint(st.total_selected)}</strong></div>
					<div class="fg-cm-kpi fg-cm-kpi--ok"><span>${__("Autorizados")}</span><strong>${cint(st.authorized)}</strong></div>
					<div class="fg-cm-kpi fg-cm-kpi--muted"><span>${__("Omitidos")}</span><strong>${cint(st.skipped)}</strong></div>
					<div class="fg-cm-kpi fg-cm-kpi--fail"><span>${__("Fallidos")}</span><strong>${cint(st.failed)}</strong></div>
				</div>
				<div class="fg-cm-meta">
					${__("Valor seleccionado")}: <strong>${format_money(st.total_amount, st.currency)}</strong> ·
					${__("Valor autorizado")}: <strong>${format_money(st.authorized_amount, st.currency)}</strong> ·
					${esc(st.date_type || "")}: <strong>${format_date(st.date_from)} – ${format_date(st.date_to)}</strong>
				</div>
				<div class="fg-cm-meta">${__("Solicitado por")} <strong>${esc(st.requested_by_name || st.requested_by || "—")}</strong> · ${__("Inicio")} ${format_datetime(
			st.started_on || st.queued_on
		)}${st.finished_on ? ` · ${__("Fin")} ${format_datetime(st.finished_on)}` : ""} · ${__("Motivo")}: ${esc(st.reason || "")}</div>
				${
					customers
						? `<div class="fg-cm-by-customer"><div class="fg-cm-label">${__("Total por cliente")} <span class="fg-cm-muted">(${__("autorizados")})</span></div>
							<div class="fg-cm-table-wrap"><table class="fg-cm-table"><thead><tr><th>${__("Cliente")}</th><th class="fg-cm-num">${__("Cobros")}</th><th class="fg-cm-num">${__(
								"Valor"
						  )}</th></tr></thead><tbody>${customers}</tbody></table></div></div>`
						: ""
				}
				${
					issues
						? `<div class="fg-cm-table-wrap"><table class="fg-cm-table"><thead><tr><th>${__("Pedido")}</th><th>${__("Cliente")}</th><th class="fg-cm-num">${__("Valor")}</th><th>${__(
								"Resultado"
						  )}</th><th>${__("Detalle")}</th></tr></thead><tbody>${issues}</tbody></table></div>`
						: ""
				}
				<div class="fg-cm-actions">
					${
						st.can_resume
							? `<button type="button" class="fg-btn fg-cm-resume-btn">${icon("rotate-ccw", "fg-icon-sm")} ${__("Reanudar")}</button>
							   <button type="button" class="fg-btn fg-cm-close-batch-btn">${icon("circle-x", "fg-icon-sm")} ${__("Cerrar lote")}</button>`
							: ""
					}
					${st.finished ? `<button type="button" class="fg-btn fg-cm-back-btn">${icon("chevron-left", "fg-icon-sm")} ${__("Volver")}</button>` : ""}
					${st.finished ? `<button type="button" class="fg-btn fg-cm-confirm-btn fg-cm-done-btn">${__("Listo")}</button>` : ""}
				</div>
			</div>
		`);
	}

	// =====================================================================
	// Detalle (vista dentro de la misma Page, patrón Recorridos)
	// =====================================================================
	open_detail(name) {
		if (!name) return;
		this.reset_detail_forms();
		const seq = ++this._detail_request_seq;
		this.view = "detail";
		this.detail_name = name;
		this.detail = null;
		this.render_detail_skeleton();
		this.call("get_obligation_detail", { obligation_name: name })
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

	// Vuelve a la lista con los datos ya cargados. Si una acción cambió
	// datos, los KPIs ya vienen actualizados en la respuesta y la lista se
	// pide UNA vez (misma página, filtro y búsqueda).
	back_to_dashboard() {
		this._detail_request_seq++;
		this.reset_detail_forms();
		this.view = "dashboard";
		this.detail = null;
		this.detail_name = null;
		if (!(this.dashboard || this.list)) return this.load_all();
		this.render_dashboard();
		if (this.list_stale) {
			this.list_stale = false;
			this.refresh_list();
		}
	}

	reset_detail_forms() {
		if (this.pay_form && this.pay_form.proof_url) URL.revokeObjectURL(this.pay_form.proof_url);
		this.pay_form = null;
		this.reject_form = null;
	}

	render_detail_header_html() {
		return `
			<div class="fg-np-header fg-cartera-detail-header">
				<button type="button" class="fg-np-back fg-cartera-back">${icon("arrow-left")} ${__("Volver")}</button>
				<div class="fg-np-title">${__("Detalle de cartera")}</div>
			</div>
		`;
	}

	render_detail_skeleton() {
		this.$body.html(`
			${this.render_detail_header_html()}
			<div class="fg-skeleton fg-cartera-detail-skeleton"></div>
			<div class="fg-skeleton fg-cartera-detail-skeleton"></div>
		`);
		this.$body.find(".fg-cartera-back").on("click", () => this.back_to_dashboard());
	}

	render_detail_error() {
		this.$body.html(`
			${this.render_detail_header_html()}
			<div class="fg-cartera-error">
				<div>${__("No se pudo cargar el detalle de esta obligación.")}</div>
				<button type="button" class="fg-btn fg-btn--solid-primary fg-cartera-retry-detail">${icon("refresh-cw", "fg-icon-sm")} ${__(
					"REINTENTAR"
				)}</button>
			</div>
		`);
		this.$body.find(".fg-cartera-back").on("click", () => this.back_to_dashboard());
		this.$body.find(".fg-cartera-retry-detail").on("click", () => this.open_detail(this.detail_name));
	}

	render_detail() {
		const d = this.detail;
		this.$body.html(`
			${this.render_detail_header_html()}
			${render_detail_html(d, { pay_form: this.pay_form, reject_form: this.reject_form, busy: this.action_busy })}
		`);
		this.$body.find(".fg-cartera-back").on("click", () => this.back_to_dashboard());
		this.$body.find(".fg-cartera-proof-btn").on("click", (e) => {
			this.show_proof("get_driver_payment_proof", { obligation_name: d.name }, __("Comprobante del conductor"), $(e.currentTarget));
		});
		this.$body.find(".fg-cartera-payment-proof-btn").on("click", (e) => {
			const $btn = $(e.currentTarget);
			this.show_proof("get_payment_proof", { payment_name: $btn.attr("data-payment") }, __("Comprobante del cobro"), $btn);
		});
		this.$body.find(".fg-cartera-open-pay").on("click", () => this.open_pay_form());
		this.$body.find(".fg-cartera-confirm-btn").on("click", () => this.confirm_driver_payment());
		this.$body.find(".fg-cartera-open-reject").on("click", (e) => this.open_reject_form($(e.currentTarget).attr("data-kind")));
		if (this.pay_form) this.bind_pay_form();
		if (this.reject_form) this.bind_reject_form();
	}

	// =====================================================================
	// 27.3 -- REGISTRAR COBRO (panel dentro del detalle, nunca un diálogo)
	// =====================================================================
	open_pay_form() {
		const d = this.detail;
		if (!d || !d.can_register_payment) return;
		this.reset_detail_forms();
		// Un UUID por formulario: se conserva en cada envío/reintento y solo
		// se renueva al abrir un formulario nuevo después de un éxito.
		this.pay_form = new_pay_form(d);
		this.render_detail();
		this.$body.find(".fg-cartera-pay-amount").trigger("focus");
	}

	close_pay_form() {
		this.reset_detail_forms();
		this.render_detail();
	}

	bind_pay_form() {
		const $p = this.$body.find(".fg-cartera-pay-panel");
		const form = this.pay_form;
		$p.find(".fg-cartera-pay-amount")
			.on("input", (e) => {
				form.amount_text = $(e.currentTarget).val() || "";
				this.update_pay_state();
			})
			.on("blur", (e) => {
				const n = parse_money_input(form.amount_text);
				if (n !== null && !isNaN(n)) {
					form.amount_text = format_money_input(n);
					$(e.currentTarget).val(form.amount_text);
				}
			});
		$p.find(".fg-cartera-pay-full").on("click", () => {
			form.amount_text = format_money_input(this.detail.outstanding_amount);
			$p.find(".fg-cartera-pay-amount").val(form.amount_text);
			this.update_pay_state();
		});
		$p.find(".fg-cartera-pay-date").on("change input", (e) => {
			form.payment_date = $(e.currentTarget).val() || "";
			this.update_pay_state();
		});
		$p.find(".fg-cartera-method").on("click", (e) => {
			form.payment_method = $(e.currentTarget).attr("data-method");
			$p.find(".fg-cartera-method").each((_i, el) => {
				const on = $(el).attr("data-method") === form.payment_method;
				$(el).toggleClass("is-active", on).attr("aria-pressed", on ? "true" : "false");
			});
			this.update_pay_state();
		});
		$p.find(".fg-cartera-pay-reference").on("input", (e) => {
			form.reference = $(e.currentTarget).val() || "";
			this.update_pay_state();
		});
		$p.find(".fg-cartera-pay-notes").on("input", (e) => {
			form.notes = $(e.currentTarget).val() || "";
			this.update_pay_state();
		});
		$p.find(".fg-cartera-pay-proof-input").on("change", (e) => this.on_pay_proof_selected(e.currentTarget));
		$p.find(".fg-cartera-pay-proof-remove").on("click", () => {
			if (form.proof_url) URL.revokeObjectURL(form.proof_url);
			form.proof_blob = null;
			form.proof_url = null;
			this.render_detail();
		});
		$p.find(".fg-cartera-pay-cancel").on("click", () => this.close_pay_form());
		$p.find(".fg-cartera-pay-submit").on("click", () => this.submit_payment());
		this.update_pay_state();
	}

	// Solo actualiza el resumen y el botón -- nunca re-renderiza los inputs
	// (no se pierde el foco mientras se escribe).
	update_pay_state() {
		const form = this.pay_form;
		if (!form) return;
		const $p = this.$body.find(".fg-cartera-pay-panel");
		$p.find(".fg-cartera-pay-summary").html(payment_summary_html(this.detail, form));
		$p.find(".fg-cartera-pay-submit").prop("disabled", !pay_form_ready(this.detail, form));
	}

	on_pay_proof_selected(input) {
		const form = this.pay_form;
		const file = input.files && input.files[0];
		input.value = "";
		if (!file || !form) return;
		form.proof_processing = true;
		this.update_pay_state();
		prepare_payment_proof(file)
			.then((blob) => {
				if (this.pay_form !== form) return;
				if (form.proof_url) URL.revokeObjectURL(form.proof_url);
				form.proof_blob = blob;
				form.proof_url = URL.createObjectURL(blob);
			})
			.catch(() => {
				frappe.msgprint(__("No se pudo leer la imagen del comprobante. Intenta con otra foto."));
			})
			.finally(() => {
				if (this.pay_form !== form) return;
				form.proof_processing = false;
				this.render_detail();
			});
	}

	submit_payment() {
		const d = this.detail;
		const form = this.pay_form;
		if (!form || form.submitting || !pay_form_ready(d, form)) return;
		form.submitting = true;
		this.update_pay_state();

		const data = new FormData();
		data.append("obligation_name", d.name);
		data.append("amount", amount_to_wire(parse_money_input(form.amount_text)));
		data.append("payment_date", form.payment_date);
		data.append("payment_method", form.payment_method);
		data.append("reference", form.reference || "");
		data.append("notes", form.notes || "");
		data.append("client_request_id", form.request_id); // el MISMO en cada reintento
		if (form.proof_blob) data.append("payment_proof", form.proof_blob, "comprobante.jpg");

		post_multipart("fabergray_erp.api.cartera.register_payment", data)
			.then((res) => {
				const r = (res && res.result) || {};
				this.apply_action_response(
					res,
					r.already_registered ? __("Este cobro ya estaba registrado en cartera.") : __("COBRO REGISTRADO EN CARTERA")
				);
			})
			.catch(() => {
				// El error real ya se mostró; el formulario (y su UUID) se
				// conservan para reintentar sin duplicar el cobro.
				if (this.pay_form === form) {
					form.submitting = false;
					this.update_pay_state();
				}
			});
	}

	// =====================================================================
	// 27.3 -- CONFIRMAR / RECHAZAR el pago o reporte del conductor
	// =====================================================================
	confirm_driver_payment() {
		const d = this.detail;
		if (!d || !d.can_confirm_driver_payment || this.action_busy) return;
		frappe.confirm(__("¿CONFIRMAS QUE CARTERA RECIBIÓ {0}?", [format_money(d.paid_amount, d.currency)]), () => {
			this.run_action("confirm_driver_payment", { obligation_name: d.name }, __("PAGO DEL CONDUCTOR CONFIRMADO"));
		});
	}

	open_reject_form(kind) {
		const d = this.detail;
		if (!d || !d.can_reject_driver_report) return;
		this.reset_detail_forms();
		this.reject_form = { kind: kind === "payment" ? "payment" : "report", reason: "" };
		this.render_detail();
		this.$body.find(".fg-cartera-reject-reason").trigger("focus");
	}

	bind_reject_form() {
		const $p = this.$body.find(".fg-cartera-reject-panel");
		const form = this.reject_form;
		const refresh = () => {
			$p.find(".fg-cartera-reject-count").text(`${(form.reason || "").trim().length}/${REASON_MAX_LENGTH}`);
			$p.find(".fg-cartera-reject-submit").prop("disabled", !reject_form_ready(form) || this.action_busy);
		};
		$p.find(".fg-cartera-reject-reason").on("input", (e) => {
			form.reason = $(e.currentTarget).val() || "";
			refresh();
		});
		$p.find(".fg-cartera-reject-cancel").on("click", () => {
			this.reject_form = null;
			this.render_detail();
		});
		$p.find(".fg-cartera-reject-submit").on("click", () => {
			if (!reject_form_ready(form)) return;
			this.run_action(
				"reject_driver_payment",
				{ obligation_name: this.detail.name, reason: form.reason.trim() },
				form.kind === "payment" ? __("PAGO DEL CONDUCTOR RECHAZADO") : __("REPORTE DEL CONDUCTOR RECHAZADO")
			);
		});
		refresh();
	}

	run_action(method, args, success_message) {
		if (this.action_busy) return;
		this.action_busy = true;
		this.$body.find(".fg-cartera-action-btn").prop("disabled", true);
		this.call(method, args)
			.then((res) => this.apply_action_response(res, success_message))
			.catch(() => {
				// frappe.call() ya mostró el error real (estado/permiso).
			})
			.finally(() => {
				this.action_busy = false;
				if (this.view === "detail") this.$body.find(".fg-cartera-action-btn").prop("disabled", false);
			});
	}

	// Detalle + KPIs recalculados por el servidor, sin recargar la Page.
	apply_action_response(res, message) {
		if (!res || !res.detail) return;
		this.reset_detail_forms();
		this.detail = res.detail;
		if (res.dashboard) this.dashboard = res.dashboard;
		this.list_stale = true;
		if (this.view === "detail" && this.detail_name === res.detail.name) this.render_detail();
		frappe.show_alert({ message: "✓ " + message, indicator: "green" }, 5);
	}

	// -- Comprobantes (endpoints controlados, nunca una URL de File) ------
	show_proof(method, args, title, $btn) {
		$btn && $btn.prop("disabled", true);
		this.call(method, args)
			.then((res) => {
				const src = proof_data_url(res);
				if (!src) {
					frappe.msgprint(__("El comprobante no es una imagen válida."));
					return;
				}
				const dialog = new frappe.ui.Dialog({
					title: title,
					fields: [{ fieldtype: "HTML", fieldname: "proof" }],
				});
				const $img = $('<img class="fg-cartera-proof-img" alt="">').attr("alt", __("Comprobante de pago"));
				$img.attr("src", src); // data: URL ya validada, nunca HTML concatenado
				dialog.fields_dict.proof.$wrapper.empty().append($('<div class="fg-cartera-proof-wrap">').append($img));
				dialog.$wrapper.addClass("fg-cartera-proof-dialog");
				dialog.show();
			})
			.catch(() => {
				// frappe.call() ya mostró el error real (permiso/no encontrado).
			})
			.finally(() => {
				$btn && $btn.prop("disabled", false);
			});
	}
};

// -------------------------------------------------------------------------
// Constantes + helpers puros (sin estado, sin llamadas al servidor). Se
// prueban ejecutándolos con node (test_cartera_ui_contract.py).
// -------------------------------------------------------------------------
const PAGE_LENGTH = 20;
const SEARCH_DEBOUNCE_MS = 300;
const LONG_MONEY_CHARS = 14;

const FILTERS = [
	{ key: "todos", label: __("TODOS") },
	{ key: "pendientes", label: __("PENDIENTES") },
	{ key: "credito", label: __("CRÉDITO") },
	{ key: "vencidos", label: __("VENCIDOS") },
	{ key: "pagados", label: __("PAGADOS") },
	{ key: "por_validar", label: __("POR VALIDAR") },
	{ key: "por_confirmar", label: __("POR CONFIRMAR") },
];

const EMPTY_MESSAGES = {
	todos: __("NO HAY CARTERA REGISTRADA"),
	pendientes: __("NO HAY CARTERA PENDIENTE"),
	credito: __("NO HAY CRÉDITOS PENDIENTES"),
	vencidos: __("NO HAY CRÉDITOS VENCIDOS"),
	pagados: __("NO HAY OBLIGACIONES PAGADAS"),
	por_validar: __("NO HAY OBLIGACIONES POR VALIDAR"),
	por_confirmar: __("NO HAY PAGOS POR CONFIRMAR"),
};

// bucket (calculado por el servidor) -> etiqueta + modificador visual.
const BUCKET_META = {
	vencido: { label: __("VENCIDO"), mod: "vencido" },
	por_vencer: { label: __("POR VENCER"), mod: "por-vencer" },
	pendiente: { label: __("PENDIENTE"), mod: "pendiente" },
	por_validar: { label: __("POR VALIDAR"), mod: "atencion" },
	pagado: { label: __("PAGADO"), mod: "pagado" },
	anulada: { label: __("ANULADA"), mod: "anulada" },
};

const DRIVER_STATUS_LABELS = {
	Pagado: __("Pagado"),
	"Pendiente por Pago": __("Pendiente por Pago"),
	"Crédito": __("Crédito"),
};

const PROOF_CONTENT_TYPES = ["image/jpeg", "image/png"];

// 27.3 -- mirrors cartera_service (the server validates again).
const PAYMENT_METHODS = [
	{ value: "Transferencia", label: __("TRANSFERENCIA") },
	{ value: "Efectivo", label: __("EFECTIVO") },
	{ value: "Consignación", label: __("CONSIGNACIÓN") },
	{ value: "Otro", label: __("OTRO") },
];
const REFERENCE_MAX_LENGTH = 140;
const NOTES_MAX_LENGTH = 500;
const REASON_MIN_LENGTH = 5;
const REASON_MAX_LENGTH = 500;
const PROOF_MAX_SIDE = 1600;
const PROOF_QUALITY = 0.85;
const PROOF_ACCEPT = "image/jpeg,image/png,image/webp";

function esc(value) {
	if (value === null || value === undefined) return "";
	return frappe.utils.escape_html(String(value));
}

function empty_message(filter) {
	return EMPTY_MESSAGES[filter] || EMPTY_MESSAGES.todos;
}

function bucket_meta(bucket) {
	return BUCKET_META[bucket] || BUCKET_META.pendiente;
}

function can_sync() {
	return !!(frappe.user && (frappe.user.has_role("Cartera") || frappe.user.has_role("System Manager")));
}

// "$ 1.250.000" -- separador de miles ".", decimales "," SOLO si el valor
// los tiene (nunca redondea a pesos: 1250000.5 -> "$ 1.250.000,50").
// currency se conserva: COP (o vacío) usa "$"; otra moneda muestra su código.
function format_money(value, currency) {
	if (value === null || value === undefined || value === "") return "—";
	const n = Number(value);
	if (!isFinite(n)) return "—";
	const cents = Math.round(Math.abs(n) * 100);
	const int_part = Math.floor(cents / 100);
	const frac = cents % 100;
	const int_str = String(int_part).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
	const body = frac ? `${int_str},${String(frac).padStart(2, "0")}` : int_str;
	const symbol = !currency || currency === "COP" ? "$" : String(currency).replace(/[^A-Z]/g, "");
	return `${n < 0 && cents ? "-" : ""}${symbol} ${body}`;
}

// A money figure in its own block; very long figures ("$ 125.450.000,50")
// get .is-long so the CSS can shrink them instead of breaking the digits.
function money_html(value, currency, css_class) {
	const text = format_money(value, currency);
	return `<div class="${css_class}${text.length >= LONG_MONEY_CHARS ? " is-long" : ""}">${text}</div>`;
}

// "2026-09-25" | "2026-09-25 14:03:11.123" -> "25-09-2026". Sin Date():
// la fecha ya es el día del sitio calculado por el servidor, no se
// reinterpreta en la zona horaria del navegador.
function format_date(value) {
	const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value || "");
	return m ? `${m[3]}-${m[2]}-${m[1]}` : "—";
}

function format_datetime(value) {
	const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(value || "");
	return m ? `${m[3]}-${m[2]}-${m[1]} ${m[4]}:${m[5]}` : format_date(value);
}

// "1 obligación" / "4 obligaciones" -- the KPI's own count, from the server.
function count_label(count, unit) {
	const n = cint(count);
	const words = {
		obligation: [__("obligación"), __("obligaciones")],
		payment: [__("pago"), __("pagos")],
		unconfirmed: [__("por confirmar"), __("por confirmar")],
	}[unit] || ["", ""];
	return `${n} ${n === 1 ? words[0] : words[1]}`;
}

function plural_days(n) {
	return n === 1 ? __("1 DÍA") : __("{0} DÍAS", [n]);
}

// Etiqueta de vencimiento a partir de days_to_due/days_since_delivery (ya
// calculados por el servidor contra el `today` del sitio).
function due_label(item) {
	if (!item || !(Number(item.outstanding_amount) > 0)) return null;
	if (item.due_date) {
		const d = cint(item.days_to_due);
		if (d > 0) return { text: __("VENCE EN {0}", [plural_days(d)]), mod: "por-vencer" };
		if (d === 0) return { text: __("VENCE HOY"), mod: "por-vencer" };
		return { text: __("VENCIDO HACE {0}", [plural_days(-d)]), mod: "vencido" };
	}
	if (item.status === "Pendiente") {
		const s = cint(item.days_since_delivery);
		return {
			text: __("PENDIENTE DE COBRO"),
			sub: s > 0 ? __("{0} DESDE LA ENTREGA", [plural_days(s)]) : __("ENTREGADO HOY"),
			mod: "pendiente",
		};
	}
	return null;
}

// POR CONFIRMAR -- the server's own flag (api/cartera.py POR_CONFIRMAR_SQL:
// a real, submitted driver payment Cartera has not confirmed yet), the same
// population as the KPI and the chip. Never re-derived here.
function is_unconfirmed_driver_payment(item) {
	return !!item && item.por_confirmar === true;
}

// "Pagado" reported by the driver WITHOUT proof: no payment exists, so it
// is POR VALIDAR only (never "por confirmar").
function is_unproven_driver_report(item) {
	return !!item && item.status === "Por Validar" && item.driver_payment_status === "Pagado" && !item.por_confirmar;
}

function render_badge(text, mod) {
	return `<span class="fg-badge fg-cartera-badge fg-cartera-badge--${mod}">${text}</span>`;
}

function render_obligation_card(item) {
	const meta = bucket_meta(item.bucket);
	const due = due_label(item);
	const badges = [];
	if (item.immediate_payment) badges.push(render_badge(immediate_payment_label(item), "neutral"));
	else if (item.credit_days > 0) badges.push(render_badge(__("CRÉDITO {0}", [plural_days(cint(item.credit_days))]), "neutral"));
	if (item.terms_discrepancy) badges.push(render_badge(`⚠ ${__("CONDICIÓN DISCREPANTE")}`, "atencion"));
	if (item.delivery_time_verified === false) badges.push(render_badge(__("REGULARIZACIÓN"), "neutral"));
	if (due && item.due_date) badges.push(render_badge(due.text, due.mod));
	if (is_unconfirmed_driver_payment(item)) badges.push(render_badge(__("PAGO REPORTADO · SIN CONFIRMAR"), "atencion"));
	if (is_unproven_driver_report(item)) badges.push(render_badge(__("PAGO REPORTADO SIN COMPROBANTE"), "atencion"));
	if (!item.amount_available) badges.push(render_badge(__("VALOR POR VALIDAR"), "atencion"));
	if (cint(item.has_delivery_issues)) badges.push(render_badge(`⚠ ${__("FALTANTES / CAMBIOS")}`, "vencido"));

	const pending_block =
		due && !item.due_date
			? `<div class="fg-cartera-card-pending">
					<strong>${due.text}</strong>
					<span>${due.sub}</span>
				</div>`
			: "";

	return `
		<div class="fg-cartera-card fg-cartera-card--${meta.mod}">
			<div class="fg-cartera-card-top">
				<div class="fg-cartera-card-customer">
					<div class="fg-cartera-card-name">${esc(item.customer_name || item.customer)}</div>
					${item.customer_commercial_name ? `<div class="fg-cartera-card-commercial">${esc(item.customer_commercial_name)}</div>` : ""}
				</div>
				${render_badge(meta.label, meta.mod)}
			</div>
			<div class="fg-cartera-card-meta">
				<span class="fg-cartera-card-order">#${esc(item.commercial_name)}</span>
				<span>${__("Entrega")}: ${format_date(item.delivery_date)}</span>
			</div>
			<div class="fg-cartera-card-money">
				<div>
					<div class="fg-cartera-money-label">${__("Valor original")}</div>
					${item.amount_available ? money_html(item.invoice_amount, item.currency, "fg-cartera-money-value") : `<div class="fg-cartera-money-value">—</div>`}
				</div>
				<div>
					<div class="fg-cartera-money-label">${__("Saldo")}</div>
					${money_html(item.outstanding_amount, item.currency, "fg-cartera-money-value fg-cartera-money-value--balance")}
				</div>
			</div>
			${pending_block}
			${badges.length ? `<div class="fg-cartera-card-badges">${badges.join("")}</div>` : ""}
			<button type="button" class="fg-btn fg-btn--solid-primary fg-cartera-card-detail" data-name="${esc(item.name)}">
				${icon("eye", "fg-icon-sm")} ${__("VER DETALLE")}
			</button>
		</div>
	`;
}

// Facturación integration -- "PAGO INMEDIATO · CONTADO". A label only:
// choosing Contado/Efectivo/Transferencia never means the money was received.
function immediate_payment_label(item) {
	return item.invoice_payment_method
		? __("PAGO INMEDIATO · {0}", [esc(String(item.invoice_payment_method).toUpperCase())])
		: __("PAGO INMEDIATO");
}

function term_label(days) {
	const n = cint(days);
	return n === 0 ? __("Mismo día") : n === 1 ? __("1 día") : __("{0} días", [n]);
}

// Condiciones de la factura (payment method, invoicing date, due date, term,
// days overdue) -- every value computed by the server (api/cartera.py
// _serialize_obligation()) against the site's today. Only for obligations
// whose due date came from the invoice; historical ones keep the previous
// CRÉDITO / VENCE EL fields untouched.
function render_invoice_terms_html(d, due) {
	if (!d.due_from_invoice) return "";
	const overdue = cint(d.days_overdue);
	return `
		<div class="fg-cartera-section-title fg-cartera-terms-title">${__("CONDICIONES DE LA FACTURA")}</div>
		<div class="fg-cartera-detail-grid fg-cartera-terms">
			${detail_field(
				__("FORMA DE PAGO"),
				`${esc(d.invoice_payment_method) || "—"} ${d.immediate_payment ? render_badge(__("PAGO INMEDIATO"), "neutral") : ""}`
			)}
			${detail_field(__("FECHA DE FACTURACIÓN"), format_date(d.invoice_date))}
			${detail_field(__("VENCIMIENTO"), `${format_date(d.due_date)} ${due ? render_badge(due.text, due.mod) : ""}`)}
			${detail_field(__("PLAZO"), esc(term_label(d.credit_days)))}
			${detail_field(
				__("DÍAS DE MORA"),
				overdue > 0 ? `<strong class="fg-cartera-overdue">${esc(plural_days(overdue))}</strong>` : __("Sin mora")
			)}
		</div>
	`;
}

// Informative only -- never changes the obligation (the server computes it
// at read time and stores nothing).
function render_terms_discrepancy_html(d) {
	if (!d.terms_discrepancy) return "";
	return `
		<div class="fg-cartera-detail-section fg-cartera-issues fg-cartera-discrepancy">
			<div class="fg-cartera-issues-title">⚠ ${__("CONDICIÓN DISCREPANTE")}</div>
			<div>${esc(d.terms_discrepancy)}</div>
			<div class="fg-cartera-muted">${__("Alerta informativa: no modifica la obligación, el vencimiento ni los pagos.")}</div>
		</div>
	`;
}

function detail_field(label, value_html) {
	return `
		<div class="fg-cartera-detail-field">
			<div class="fg-cartera-detail-label">${label}</div>
			<div class="fg-cartera-detail-value">${value_html}</div>
		</div>
	`;
}

function render_detail_html(d, ui) {
	ui = ui || {};
	const meta = bucket_meta(d.bucket);
	const due = due_label(d);
	const reject_panel = (kind) =>
		ui.reject_form && ui.reject_form.kind === kind ? render_reject_panel_html(d, ui.reject_form, ui.busy) : "";
	const proof_button = d.has_driver_proof
		? `<button type="button" class="fg-btn fg-btn--outline-success fg-cartera-proof-btn">${icon("image", "fg-icon-sm")} ${__(
				"VER COMPROBANTE"
		  )}</button>`
		: "";

	const reported_block =
		is_unconfirmed_driver_payment(d)
			? `
			<div class="fg-cartera-detail-section fg-cartera-reported">
				<div class="fg-cartera-reported-title">${__("PAGO REPORTADO POR CONDUCTOR")}</div>
				<div class="fg-cartera-reported-amount">${format_money(d.paid_amount, d.currency)}</div>
				<div class="fg-cartera-reported-warning">⚠ ${__("PENDIENTE DE CONFIRMACIÓN")}</div>
				${proof_button}
				${
					d.can_confirm_driver_payment && !(ui.reject_form && ui.reject_form.kind === "payment")
						? `<div class="fg-cartera-action-row">
							<button type="button" class="fg-btn fg-btn--solid-primary fg-cartera-action-btn fg-cartera-confirm-btn" ${ui.busy ? "disabled" : ""}>${icon("check", "fg-icon-sm")} ${__("CONFIRMAR PAGO")}</button>
							<button type="button" class="fg-btn fg-btn--outline-danger fg-cartera-action-btn fg-cartera-open-reject" data-kind="payment" ${ui.busy ? "disabled" : ""}>${icon("x", "fg-icon-sm")} ${__("RECHAZAR PAGO")}</button>
						</div>`
						: ""
				}
				${reject_panel("payment")}
			</div>`
			: is_unproven_driver_report(d)
			? `
			<div class="fg-cartera-detail-section fg-cartera-reported">
				<div class="fg-cartera-reported-title">${__("PAGO REPORTADO SIN COMPROBANTE")}</div>
				<div class="fg-cartera-reported-warning">⚠ ${__("POR VALIDAR: no hay pago registrado todavía.")}</div>
				<div class="fg-cartera-muted">${__("Debe rechazarse el reporte antes de registrar un cobro real.")}</div>
				${
					d.can_reject_driver_report && !(ui.reject_form && ui.reject_form.kind === "report")
						? `<div class="fg-cartera-action-row">
							<button type="button" class="fg-btn fg-btn--outline-danger fg-cartera-action-btn fg-cartera-open-reject" data-kind="report" ${ui.busy ? "disabled" : ""}>${icon("x", "fg-icon-sm")} ${__("RECHAZAR REPORTE")}</button>
						</div>`
						: ""
				}
				${reject_panel("report")}
			</div>`
			: "";

	const verification_block = render_verification_html(d);

	const pay_section = d.can_register_payment
		? ui.pay_form
			? render_payment_panel_html(d, ui.pay_form)
			: `<button type="button" class="fg-btn fg-btn--solid-primary fg-cartera-main-action fg-cartera-open-pay">${icon("plus", "fg-icon-sm")} ${__(
					"REGISTRAR COBRO"
			  )}</button>`
		: "";

	const amount_warning = d.amount_available
		? ""
		: `<div class="fg-cartera-detail-section fg-cartera-issues">
				<div class="fg-cartera-issues-title">⚠ ${__("VALOR POR VALIDAR")}</div>
				<div>${__("No se pudo calcular el valor facturado de esta entrega. Requiere revisión.")}</div>
			</div>`;

	const issues_block = cint(d.has_delivery_issues)
		? `<div class="fg-cartera-detail-section fg-cartera-issues">
				<div class="fg-cartera-issues-title">⚠ ${__("ENTREGA CON FALTANTES / CAMBIOS")}</div>
				<div class="fg-cartera-pre">${esc(d.delivery_issues) || "—"}</div>
			</div>`
		: "";

	const credit_html = d.due_from_invoice
		? ""
		: d.due_date
		? `${detail_field(__("CRÉDITO"), esc(plural_days(cint(d.credit_days))))}
			${detail_field(__("VENCE EL"), `${format_date(d.due_date)} ${due ? render_badge(due.text, due.mod) : ""}`)}`
		: due
		? detail_field(__("COBRO"), `${due.text}${due.sub ? ` · ${due.sub}` : ""}`)
		: "";

	return `
		<div class="fg-cartera-detail-card">
			<div class="fg-cartera-detail-top">
				<div>
					<div class="fg-cartera-detail-label">${__("CLIENTE")}</div>
					<div class="fg-cartera-detail-name">${esc(d.customer_name || d.customer)}</div>
					${d.customer_commercial_name ? `<div class="fg-cartera-card-commercial">${esc(d.customer_commercial_name)}</div>` : ""}
				</div>
				<div class="fg-cartera-card-badges">
					${render_badge(meta.label, meta.mod)}
					${is_unconfirmed_driver_payment(d) ? render_badge(__("PAGO REPORTADO · SIN CONFIRMAR"), "atencion") : ""}
				</div>
			</div>

			<div class="fg-cartera-detail-grid">
				${detail_field(__("PEDIDO"), `#${esc(d.commercial_name)}${
					d.sales_order && d.sales_order !== d.commercial_name ? ` <span class="fg-cartera-muted">(${esc(d.sales_order)})</span>` : ""
				}`)}
				${detail_field(__("PICK LIST"), esc(d.pick_list) || "—")}
				${detail_field(__("RECORRIDO"), esc(d.recorrido) || "—")}
				${detail_field(__("FECHA DE ENTREGA"), format_date(d.delivery_date))}
			</div>

			<div class="fg-cartera-detail-money">
				${detail_field(__("VALOR ORIGINAL"), d.amount_available ? format_money(d.invoice_amount, d.currency) : "—")}
				${detail_field(__("PAGADO"), format_money(d.paid_amount, d.currency))}
				${detail_field(__("SALDO PENDIENTE"), `<strong>${format_money(d.outstanding_amount, d.currency)}</strong>`)}
			</div>

			${credit_html ? `<div class="fg-cartera-detail-grid">${credit_html}</div>` : ""}
			${render_invoice_terms_html(d, due)}
		</div>

		${render_terms_discrepancy_html(d)}

		${pay_section}
		${amount_warning}
		${reported_block}
		${verification_block}
		${issues_block}

		<div class="fg-cartera-detail-card">
			<div class="fg-cartera-section-title">${__("REPORTE DE ENTREGA")}</div>
			<div class="fg-cartera-detail-grid">
				${detail_field(__("PAGO REPORTADO"), esc(DRIVER_STATUS_LABELS[d.driver_payment_status] || d.driver_payment_status) || "—")}
				${detail_field(__("CONDUCTOR"), esc(d.delivered_by_name || d.delivered_by) || "—")}
				${
					d.delivery_time_verified === false
						? detail_field(
								__("FECHA DE ENTREGA"),
								`${format_date(d.delivery_date)} ${render_badge(__("REGULARIZACIÓN ADMINISTRATIVA"), "neutral")}<div class="fg-cartera-muted">${__(
									"Entrega registrada por una entrega masiva: sin hora comprobada, foto ni firma."
								)}</div>`
						  )
						: detail_field(__("FECHA / HORA DE ENTREGA"), format_datetime(d.delivered_on))
				}
			</div>
			${d.driver_payment_note ? detail_field(__("OBSERVACIÓN DEL PAGO"), `<div class="fg-cartera-pre">${esc(d.driver_payment_note)}</div>`) : ""}
			${d.has_driver_proof && !is_unconfirmed_driver_payment(d) ? proof_button : ""}
		</div>

		<div class="fg-cartera-detail-card">
			<div class="fg-cartera-section-title">${__("HISTORIAL DE PAGOS")}</div>
			${render_payments_html(d.payments || [], d)}
		</div>
	`;
}

function render_payments_html(payments, d) {
	if (!payments.length) return `<div class="fg-cartera-muted">${__("Sin pagos registrados.")}</div>`;
	return `<div class="fg-cartera-payments">${payments
		.map(
			(p) => `
			<div class="fg-cartera-payment ${p.cancelled ? "is-cancelled" : ""}">
				<div class="fg-cartera-payment-top">
					<strong>${format_money(p.amount, p.currency || d.currency)}</strong>
					<span>${format_date(p.payment_date)}</span>
				</div>
				<div class="fg-cartera-card-badges">
					${render_badge(p.source === "Conductor" ? __("CONDUCTOR") : __("CARTERA"), "neutral")}
					${p.cancelled ? render_badge(__("ANULADO"), "anulada") : ""}
					${render_badge(esc(p.accounting_status || "Sin contabilizar"), "neutral")}
				</div>
				<div class="fg-cartera-payment-grid">
					${detail_field(__("MEDIO"), esc(p.payment_method) || "—")}
					${detail_field(__("REFERENCIA"), esc(p.reference) || "—")}
					${detail_field(__("REGISTRADO POR"), esc(p.recorded_by_name || p.recorded_by) || "—")}
					${detail_field(__("FECHA DE REGISTRO"), format_datetime(p.recorded_on))}
				</div>
				${p.notes ? `<div class="fg-cartera-pre fg-cartera-muted">${esc(p.notes)}</div>` : ""}
				${
					p.cancelled
						? `<div class="fg-cartera-cancel-info">
							<strong>${__("ANULADO")}</strong>
							${p.cancellation_reason ? `<div class="fg-cartera-pre">${esc(p.cancellation_reason)}</div>` : ""}
							<div class="fg-cartera-muted">${__("Por {0} · {1}", [esc(p.cancelled_by_name) || "—", format_datetime(p.cancelled_on)])}</div>
						</div>`
						: ""
				}
				${
					p.proof_kind === "driver"
						? `<button type="button" class="fg-btn fg-btn--ghost fg-cartera-proof-btn">${icon("image", "fg-icon-sm")} ${__(
								"VER COMPROBANTE"
						  )}</button>`
						: p.proof_kind === "payment"
						? `<button type="button" class="fg-btn fg-btn--ghost fg-cartera-payment-proof-btn" data-payment="${esc(p.name)}">${icon(
								"image",
								"fg-icon-sm"
						  )} ${__("VER COMPROBANTE")}</button>`
						: ""
				}
			</div>
		`
		)
		.join("")}</div>`;
}

// -- 27.3 helpers ------------------------------------------------------------

// Confirmación / rechazo ya decididos por Cartera (auditoría del servidor).
function render_verification_html(d) {
	if (d.payment_verification === "Confirmado") {
		return `<div class="fg-cartera-detail-section fg-cartera-verified fg-cartera-verified--ok">
			<div class="fg-cartera-section-title">✓ ${__("PAGO DEL CONDUCTOR CONFIRMADO")}</div>
			<div class="fg-cartera-muted">${__("Por {0} · {1}", [esc(d.payment_verified_by_name || d.payment_verified_by) || "—", format_datetime(d.payment_verified_on)])}</div>
			${d.payment_verified_batch ? `<div class="fg-cartera-muted">${__("Autorización masiva · lote {0}", [esc(d.payment_verified_batch)])}</div>` : ""}
		</div>`;
	}
	if (d.payment_verification === "Rechazado") {
		return `<div class="fg-cartera-detail-section fg-cartera-verified fg-cartera-verified--rejected">
			<div class="fg-cartera-section-title">✕ ${__("REPORTE DEL CONDUCTOR RECHAZADO")}</div>
			<div class="fg-cartera-muted">${__("Por {0} · {1}", [esc(d.payment_verified_by_name || d.payment_verified_by) || "—", format_datetime(d.payment_verified_on)])}</div>
			${d.payment_rejection_reason ? detail_field(__("MOTIVO"), `<div class="fg-cartera-pre">${esc(d.payment_rejection_reason)}</div>`) : ""}
		</div>`;
	}
	return "";
}

function new_request_id() {
	const c = typeof crypto !== "undefined" ? crypto : null;
	if (c && typeof c.randomUUID === "function") return c.randomUUID();
	const b = new Uint8Array(16);
	if (c && c.getRandomValues) c.getRandomValues(b);
	else for (let i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256);
	b[6] = (b[6] & 0x0f) | 0x40;
	b[8] = (b[8] & 0x3f) | 0x80;
	const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
	return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function new_pay_form(d) {
	return {
		request_id: new_request_id(),
		amount_text: "",
		payment_date: d.today || "",
		payment_method: null,
		reference: "",
		notes: "",
		proof_blob: null,
		proof_url: null,
		proof_processing: false,
		submitting: false,
	};
}

// "400.000" / "400000" / "1.250.000,50" / "$ 400.000" -> number (COP: "."
// miles, "," decimales, máx. 2). null = vacío; NaN = formato inválido.
function parse_money_input(text) {
	const t = String(text === null || text === undefined ? "" : text).replace(/[\s$]/g, "");
	if (!t) return null;
	if (!/^(\d{1,3}(\.\d{3})+|\d+)(,\d{1,2})?$/.test(t)) return NaN;
	const [int_part, dec_part] = t.split(",");
	return Number(int_part.replace(/\./g, "") + (dec_part ? "." + dec_part : ""));
}

function to_cents(n) {
	return Math.round(Number(n) * 100);
}

// Número -> texto del input ("1.250.000" o "1.250.000,50").
function format_money_input(n) {
	return format_money(n, "COP").replace(/^-?\$ /, "");
}

// Número -> valor exacto enviado al servidor ("400000" / "400000.50").
function amount_to_wire(n) {
	const cents = to_cents(n);
	const frac = cents % 100;
	return frac ? `${Math.floor(cents / 100)}.${String(frac).padStart(2, "0")}` : String(cents / 100);
}

// SALDO ACTUAL / VALOR RECIBIDO / SALDO DESPUÉS + tipo (siempre en centavos).
function payment_summary(d, form) {
	const outstanding = to_cents(d.outstanding_amount);
	const amount = parse_money_input(form && form.amount_text);
	if (amount === null) return { kind: "vacio", amount: null, after: outstanding / 100, outstanding: outstanding / 100 };
	if (isNaN(amount) || amount <= 0) return { kind: "invalido", amount: null, after: outstanding / 100, outstanding: outstanding / 100 };
	const cents = to_cents(amount);
	const after = outstanding - cents;
	const kind = after < 0 ? "excede" : after === 0 ? "total" : "parcial";
	return { kind, amount: cents / 100, after: after / 100, outstanding: outstanding / 100 };
}

function payment_summary_html(d, form) {
	const s = payment_summary(d, form);
	const tag = {
		total: render_badge(__("PAGO TOTAL"), "pagado"),
		parcial: render_badge(__("ABONO PARCIAL"), "por-vencer"),
		excede: render_badge(__("SUPERA EL SALDO"), "vencido"),
		invalido: render_badge(__("VALOR INVÁLIDO"), "vencido"),
		vacio: "",
	}[s.kind];
	return `
		<div class="fg-cartera-pay-summary-grid">
			${detail_field(__("SALDO ACTUAL"), format_money(s.outstanding, d.currency))}
			${detail_field(__("VALOR RECIBIDO"), s.amount === null ? "—" : format_money(s.amount, d.currency))}
			${detail_field(__("SALDO DESPUÉS"), s.kind === "total" || s.kind === "parcial" ? `<strong>${format_money(s.after, d.currency)}</strong>` : "—")}
		</div>
		${tag ? `<div class="fg-cartera-card-badges">${tag}</div>` : ""}
	`;
}

function pay_form_ready(d, form) {
	if (!form || form.submitting || form.proof_processing) return false;
	const s = payment_summary(d, form);
	if (s.kind !== "total" && s.kind !== "parcial") return false;
	if (!/^\d{4}-\d{2}-\d{2}$/.test(form.payment_date || "") || (d.today && form.payment_date > d.today)) return false;
	if (!PAYMENT_METHODS.some((m) => m.value === form.payment_method)) return false;
	if ((form.reference || "").length > REFERENCE_MAX_LENGTH) return false;
	if ((form.notes || "").length > NOTES_MAX_LENGTH) return false;
	return true;
}

function reject_form_ready(form) {
	const n = ((form && form.reason) || "").trim().length;
	return n >= REASON_MIN_LENGTH && n <= REASON_MAX_LENGTH;
}

function render_payment_panel_html(d, form) {
	const methods = PAYMENT_METHODS.map((m) => {
		const on = form.payment_method === m.value;
		return `<button type="button" class="fg-cartera-method ${on ? "is-active" : ""}" data-method="${esc(m.value)}" aria-pressed="${
			on ? "true" : "false"
		}">${m.label}</button>`;
	}).join("");
	const proof = form.proof_url
		? `<div class="fg-cartera-pay-proof-preview">
				<img class="fg-cartera-pay-proof-img" src="${esc(form.proof_url)}" alt="${__("Comprobante")}">
				<button type="button" class="fg-btn fg-btn--ghost fg-cartera-pay-proof-remove">${icon("x", "fg-icon-sm")} ${__("QUITAR")}</button>
			</div>`
		: `<div class="fg-cartera-pay-proof-inputs">
				<label class="fg-btn fg-btn--ghost fg-cartera-upload-btn">${icon("camera", "fg-icon-sm")} ${__("TOMAR FOTO")}
					<input type="file" class="fg-cartera-pay-proof-input" accept="${PROOF_ACCEPT}" capture="environment" hidden>
				</label>
				<label class="fg-btn fg-btn--ghost fg-cartera-upload-btn">${icon("image", "fg-icon-sm")} ${__("SUBIR COMPROBANTE")}
					<input type="file" class="fg-cartera-pay-proof-input" accept="${PROOF_ACCEPT}" hidden>
				</label>
			</div>`;
	return `
		<div class="fg-cartera-detail-section fg-cartera-pay-panel">
			<div class="fg-cartera-section-title">${__("REGISTRAR COBRO")}</div>
			<label class="fg-cartera-field">
				<span class="fg-cartera-detail-label">${__("VALOR RECIBIDO")}</span>
				<div class="fg-cartera-amount-row">
					<span class="fg-cartera-amount-prefix">$</span>
					<input type="text" inputmode="decimal" autocomplete="off" class="fg-cartera-input fg-cartera-pay-amount" placeholder="0" value="${esc(
						form.amount_text
					)}">
					<button type="button" class="fg-btn fg-btn--ghost fg-cartera-pay-full">${__("PAGO TOTAL")}</button>
				</div>
			</label>
			<div class="fg-cartera-pay-summary">${payment_summary_html(d, form)}</div>
			<label class="fg-cartera-field">
				<span class="fg-cartera-detail-label">${__("FECHA")}</span>
				<input type="date" class="fg-cartera-input fg-cartera-pay-date" value="${esc(form.payment_date)}" max="${esc(d.today || "")}">
			</label>
			<div class="fg-cartera-field">
				<span class="fg-cartera-detail-label">${__("MEDIO DE PAGO")}</span>
				<div class="fg-cartera-methods">${methods}</div>
			</div>
			<label class="fg-cartera-field">
				<span class="fg-cartera-detail-label">${__("REFERENCIA")}</span>
				<input type="text" class="fg-cartera-input fg-cartera-pay-reference" maxlength="${REFERENCE_MAX_LENGTH}" value="${esc(form.reference)}">
			</label>
			<div class="fg-cartera-field">
				<span class="fg-cartera-detail-label">${__("COMPROBANTE (OPCIONAL)")}</span>
				${form.proof_processing ? `<div class="fg-cartera-muted">${__("Procesando imagen...")}</div>` : proof}
			</div>
			<label class="fg-cartera-field">
				<span class="fg-cartera-detail-label">${__("OBSERVACIONES")}</span>
				<textarea class="fg-cartera-input fg-cartera-textarea fg-cartera-pay-notes" maxlength="${NOTES_MAX_LENGTH}" rows="3">${esc(form.notes)}</textarea>
			</label>
			<div class="fg-cartera-muted">${__("El cobro queda registrado en Cartera (sin contabilizar).")}</div>
			<div class="fg-cartera-panel-actions">
				<button type="button" class="fg-btn fg-btn--ghost fg-cartera-pay-cancel" ${form.submitting ? "disabled" : ""}>${__("CANCELAR")}</button>
				<button type="button" class="fg-btn fg-btn--solid-primary fg-cartera-pay-submit" ${pay_form_ready(d, form) ? "" : "disabled"}>${
		form.submitting ? __("REGISTRANDO...") : __("REGISTRAR COBRO")
	}</button>
			</div>
		</div>
	`;
}

function render_reject_panel_html(d, form, busy) {
	const is_payment = form.kind === "payment";
	const warning = is_payment
		? __("EL SALDO VOLVERÁ A {0}", [format_money(d.invoice_amount, d.currency)])
		: __("LA OBLIGACIÓN QUEDARÁ PENDIENTE POR {0}", [format_money(d.outstanding_amount, d.currency)]);
	const n = (form.reason || "").trim().length;
	return `
		<div class="fg-cartera-reject-panel">
			<div class="fg-cartera-reported-warning">⚠ ${warning}</div>
			<label class="fg-cartera-field">
				<span class="fg-cartera-detail-label">${__("MOTIVO DEL RECHAZO (OBLIGATORIO)")}</span>
				<textarea class="fg-cartera-input fg-cartera-textarea fg-cartera-reject-reason" maxlength="${REASON_MAX_LENGTH}" rows="3">${esc(
		form.reason
	)}</textarea>
				<span class="fg-cartera-muted fg-cartera-reject-count">${n}/${REASON_MAX_LENGTH}</span>
			</label>
			<div class="fg-cartera-panel-actions">
				<button type="button" class="fg-btn fg-btn--ghost fg-cartera-reject-cancel">${__("CANCELAR")}</button>
				<button type="button" class="fg-btn fg-btn--outline-danger fg-cartera-action-btn fg-cartera-reject-submit" ${
		reject_form_ready(form) && !busy ? "" : "disabled"
	}>${is_payment ? __("RECHAZAR PAGO") : __("RECHAZAR REPORTE")}</button>
			</div>
		</div>
	`;
}

// Multipart POST (frappe.call no envía archivos): mismas cookies + token
// CSRF que usa Frappe (mismo transporte que Recorridos/26.3).
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
				message: __("No se pudo registrar el cobro. Revisa la conexión e intenta de nuevo; el formulario se conserva."),
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
		title: __("No se pudo registrar el cobro"),
		message: messages.filter(Boolean).join("<br>") || __("Ocurrió un error inesperado. Intenta de nuevo."),
		indicator: "red",
	});
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

// Foto -> JPEG orientado y reducido (lado mayor PROOF_MAX_SIDE), sin EXIF
// (mismo procedimiento UX que 26.3). El servidor valida y re-codifica igual.
async function prepare_payment_proof(file) {
	let source;
	try {
		source = await createImageBitmap(file, { imageOrientation: "from-image" });
	} catch (e) {
		source = await load_image_element(file);
	}
	const width = source.width || source.naturalWidth;
	const height = source.height || source.naturalHeight;
	if (!width || !height) throw new Error("empty image");
	const scale = Math.min(1, PROOF_MAX_SIDE / Math.max(width, height));
	const canvas = document.createElement("canvas");
	canvas.width = Math.round(width * scale);
	canvas.height = Math.round(height * scale);
	const ctx = canvas.getContext("2d");
	ctx.fillStyle = "#ffffff";
	ctx.fillRect(0, 0, canvas.width, canvas.height);
	ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
	if (source.close) source.close();
	return canvas_to_blob(canvas, "image/jpeg", PROOF_QUALITY);
}

function sync_summary_html(r) {
	return `
		<div>${__("{0} nuevas obligaciones", [cint(r.created)])}</div>
		<div>${__("{0} ya existentes", [cint(r.already_existing)])}</div>
		<div>${__("{0} errores", [cint(r.failed)])}</div>
	`;
}

// data: URL segura para el comprobante: solo tipos de imagen permitidos y
// base64 estricto -- cualquier otra cosa se descarta (null).
function proof_data_url(res) {
	if (!res || PROOF_CONTENT_TYPES.indexOf(res.content_type) === -1) return null;
	const data = typeof res.data === "string" ? res.data : "";
	if (!data || !/^[A-Za-z0-9+/]+={0,2}$/.test(data)) return null;
	return `data:${res.content_type};base64,${data}`;
}

function render_search_bar_html(value) {
	const has_value = !!(value && value.trim());
	return `
		<div class="fg-search-bar fg-cartera-search">
			${icon("search", "fg-search-icon")}
			<input type="search" class="fg-search-input" placeholder="${__("BUSCAR CLIENTE O PEDIDO...")}" value="${esc(value || "")}">
			<button type="button" class="fg-search-clear ${has_value ? "is-visible" : ""}" title="${__("Limpiar")}">${icon("x", "fg-icon-sm")}</button>
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
	const n = parseInt(v, 10);
	return isNaN(n) ? 0 : n;
}
