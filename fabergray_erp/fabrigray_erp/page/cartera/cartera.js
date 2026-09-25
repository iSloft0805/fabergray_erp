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
// - no hay ningún botón económico (registrar/confirmar/rechazar pagos
//   llegan en 27.3). La única escritura es SINCRONIZAR (reconciliador
//   idempotente ya existente desde 27.1);
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
				${this.render_sync_button_html()}
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
	// Detalle (vista dentro de la misma Page, patrón Recorridos)
	// =====================================================================
	open_detail(name) {
		if (!name) return;
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

	// Vuelve a la lista con los datos ya cargados (sin volver a pedirlos).
	back_to_dashboard() {
		this._detail_request_seq++;
		this.view = "dashboard";
		this.detail = null;
		this.detail_name = null;
		if (this.dashboard || this.list) this.render_dashboard();
		else this.load_all();
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
			${render_detail_html(d)}
		`);
		this.$body.find(".fg-cartera-back").on("click", () => this.back_to_dashboard());
		this.$body.find(".fg-cartera-proof-btn").on("click", (e) => {
			const $btn = $(e.currentTarget);
			this.show_driver_proof(d.name, $btn);
		});
	}

	// -- Comprobante del conductor (endpoint controlado) -----------------
	show_driver_proof(obligation_name, $btn) {
		if (!obligation_name) return;
		$btn && $btn.prop("disabled", true);
		this.call("get_driver_payment_proof", { obligation_name: obligation_name })
			.then((res) => {
				const src = proof_data_url(res);
				if (!src) {
					frappe.msgprint(__("El comprobante no es una imagen válida."));
					return;
				}
				const dialog = new frappe.ui.Dialog({
					title: __("Comprobante del conductor"),
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
	if (item.credit_days > 0) badges.push(render_badge(__("CRÉDITO {0}", [plural_days(cint(item.credit_days))]), "neutral"));
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

function detail_field(label, value_html) {
	return `
		<div class="fg-cartera-detail-field">
			<div class="fg-cartera-detail-label">${label}</div>
			<div class="fg-cartera-detail-value">${value_html}</div>
		</div>
	`;
}

function render_detail_html(d) {
	const meta = bucket_meta(d.bucket);
	const due = due_label(d);
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
			</div>`
			: is_unproven_driver_report(d)
			? `
			<div class="fg-cartera-detail-section fg-cartera-reported">
				<div class="fg-cartera-reported-title">${__("PAGO REPORTADO POR CONDUCTOR SIN COMPROBANTE")}</div>
				<div class="fg-cartera-reported-warning">⚠ ${__("POR VALIDAR: no hay pago registrado todavía.")}</div>
			</div>`
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

	const credit_html = d.due_date
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
		</div>

		${amount_warning}
		${reported_block}
		${issues_block}

		<div class="fg-cartera-detail-card">
			<div class="fg-cartera-section-title">${__("REPORTE DE ENTREGA")}</div>
			<div class="fg-cartera-detail-grid">
				${detail_field(__("PAGO REPORTADO"), esc(DRIVER_STATUS_LABELS[d.driver_payment_status] || d.driver_payment_status) || "—")}
				${detail_field(__("CONDUCTOR"), esc(d.delivered_by_name || d.delivered_by) || "—")}
				${detail_field(__("FECHA / HORA DE ENTREGA"), format_datetime(d.delivered_on))}
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
					p.has_payment_proof
						? p.proof_is_driver_proof
							? `<button type="button" class="fg-btn fg-btn--ghost fg-cartera-proof-btn">${icon("image", "fg-icon-sm")} ${__(
									"VER COMPROBANTE"
							  )}</button>`
							: `<div class="fg-cartera-muted">${__("Comprobante adjunto.")}</div>`
						: ""
				}
			</div>
		`
		)
		.join("")}</div>`;
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
