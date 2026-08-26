// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["clientes"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Clientes"),
		single_column: true,
	});
	new fabergray_erp.Clientes(page);
};

// Commit 22.3 -- Page Clientes: dashboard + lista + detalle + alta/edición +
// activar/desactivar, exclusivamente sobre los 6 endpoints ya cerrados en
// api/clientes.py (Commit 22.1 lectura, 22.2 escritura). Esta Page es UI
// solamente -- no reimplementa ninguna regla de autorización: el acceso a
// la ruta entera lo decide clientes.json's roles (Gestión de Clientes,
// System Manager -- Vendedora deliberadamente no incluida, se asignará el
// rol por usuario más adelante), y cada acción de escritura sigue pasando
// por el mismo frappe.has_permission()/check_permission() real del
// servidor -- si un valor viene deshabilitado aquí es solo cortesía visual,
// nunca la frontera de seguridad real.
//
// access_id_cliente y disabled JAMÁS se envían desde el formulario general
// (create_customer/update_customer no los aceptan -- ver _build_customer_
// payload() abajo, el único lugar donde se arma ese payload). disabled solo
// se toca desde confirm_toggle_disabled(), que llama exclusivamente
// set_customer_disabled().
fabergray_erp.Clientes = class Clientes {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.clientes.";
		this.busy = false;

		this.summary = null;

		// Lista (view: "dashboard").
		this.list_filter = "all"; // "all" | "active" | "inactive" | "incomplete"
		this.list_search = "";
		this.list_page = 1;
		this.list_rows = [];
		this.list_total = 0;
		this._search_debounce = null;
		this._list_request_seq = 0; // descarta respuestas que llegan fuera de orden

		// Detalle (view: "detail").
		this.detail = null;
		this.detail_name = null;

		this.state = { view: "dashboard" };

		this.$app = $('<div class="fg-shell fg-clientes">').appendTo(this.page.body);
		this.render_shell();
		this.load_dashboard();
	}

	// -------------------------------------------------------------------
	// Thin API wrapper -- frappe.call() no devuelve un Promise real (ver
	// page/bodega/bodega.js y page/cotizaciones/cotizaciones.js, cuyos
	// propios comentarios documentan el bug y su fix; page/ventas/ventas.js
	// nunca fue corregido y sigue arrastrando el bug original -- no se
	// copia ese patrón aquí). Envolver en `new Promise(...)` es lo que
	// permite que cada `.then()/.catch()/.finally()` de este archivo se
	// comporte como un Promise nativo de verdad.
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
					<span class="fg-header-title">${__("CLIENTES")}</span>
				</div>
				<div class="fg-header-user">
					<div class="fg-header-user-info">
						<div class="fg-header-user-name">${frappe.utils.escape_html(fullname)}</div>
						<div class="fg-header-user-role">${__("Gestión de Clientes")}</div>
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
			else if (this.state.view === "detail" && this.detail_name) this.open_detail(this.detail_name);
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
		return this.call("get_dashboard_summary")
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

	// El filtro "Datos incompletos" no tiene un valor `status` nativo en
	// search_customers() (solo "all"/"active"/"inactive", Commit 22.1) --
	// api/clientes.py no se modificó para esto (fuera de alcance de este
	// commit). En vez de tocar el backend, se trae con status="all" y se
	// filtra/pagina en el cliente -- mismo patrón "un fetch, filtrar+
	// paginar en JS" que ya usan ventas.js/bodega.js/facturacion.js,
	// aplicado solo a esta pestaña porque es la única que el servidor no
	// puede expresar directamente.
	//
	// IMPORTANTE (encontrado validando con navegador real, no asumido):
	// search_customers() devuelve filas ordenadas alfabéticamente, así que
	// filtrar "incompleto" sobre una ventana acotada NO trae "los primeros
	// N incompletos" -- trae "los primeros N clientes alfabéticos, de los
	// cuales algunos son incompletos". Con un cap de 1000 (< 4091 clientes
	// totales) esto medía 71 incompletos en vez de los 431 reales. El cap
	// tiene que cubrir el TOTAL de clientes a examinar, no el conteo de
	// incompletos -- por eso INCOMPLETE_FETCH_CAP va con margen amplio
	// sobre el total actual (~4091), no sobre el subconjunto incompleto.
	// Si el total de clientes alguna vez supera el cap, esta pestaña
	// dejaría de ser exacta -- señal de que en ese momento sí haría falta
	// un filtro nativo en el backend, no de subir el cap otra vez.
	load_list() {
		const seq = ++this._list_request_seq;

		if (this.list_filter === "incomplete") {
			return this.call("search_customers", {
				txt: this.list_search,
				status: "all",
				start: 0,
				page_length: INCOMPLETE_FETCH_CAP,
			}).then((res) => {
				if (seq !== this._list_request_seq) return; // respuesta obsoleta, descartada
				const incomplete = (res.customers || []).filter((c) => !c.tax_id || !c.access_nombre_comercial);
				const paged = paginate(incomplete, this.list_page, PAGE_SIZE);
				this.list_page = paged.page;
				this.list_rows = paged.page_items;
				this.list_total = paged.total;
			});
		}

		return this.call("search_customers", {
			txt: this.list_search,
			status: this.list_filter,
			start: (this.list_page - 1) * PAGE_SIZE,
			page_length: PAGE_SIZE,
		}).then((res) => {
			if (seq !== this._list_request_seq) return;
			this.list_rows = res.customers || [];
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
			<div class="fg-clientes-list-section">${this.render_list_section()}</div>
		`);
		this.bind_list_section_events();
	}

	// Los 4 KPI puramente informativos -- ninguno es clickeable, el
	// filtrado de la lista pasa exclusivamente por las Tabs de abajo
	// (mismo patrón que page/facturacion/facturacion.js, no el de
	// page/ventas/ventas.js donde el propio KPI es el filtro).
	render_kpis() {
		const s = this.summary || {};
		const cards = [
			{ key: "activos", label: __("Activos"), i: "circle-check-big", mod: "clientes-activos" },
			{ key: "inactivos", label: __("Inactivos"), i: "lock", mod: "clientes-inactivos" },
			{ key: "total", label: __("Total clientes"), i: "user", mod: "clientes-total" },
			{ key: "datos_incompletos", label: __("Datos incompletos"), i: "triangle-alert", mod: "clientes-incompletos" },
		];

		const html = cards
			.map(
				(c) => `
				<div class="fg-kpi fg-kpi--${c.mod}">
					<div class="fg-kpi-icon">${icon(c.i)}</div>
					<div class="fg-kpi-number">${s[c.key] ?? 0}</div>
					<div class="fg-kpi-label">${c.label}</div>
				</div>
			`
			)
			.join("");

		return `<div class="fg-kpis fg-kpis--clientes">${html}</div>`;
	}

	// -- Lista: buscador (server-side, debounced) + tabs + tarjetas + paginación --

	render_list_section() {
		const tabs = ["all", "active", "inactive", "incomplete"];
		const tab_meta = {
			all: { label: __("Todos") },
			active: { label: __("Activos") },
			inactive: { label: __("Inactivos") },
			incomplete: { label: __("Datos incompletos") },
		};
		const tabs_html = tabs
			.map(
				(key) =>
					`<button type="button" class="fg-clientes-tab ${
						this.list_filter === key ? "is-active" : ""
					}" data-filter="${key}">${tab_meta[key].label}</button>`
			)
			.join("");

		return `
			<div class="fg-section-head">
				<div class="fg-section-title">${__("Clientes")}</div>
				<button type="button" class="fg-btn fg-btn--solid-primary fg-clientes-new-btn">
					${icon("plus")} ${__("NUEVO CLIENTE")}
				</button>
			</div>
			<div class="fg-clientes-toolbar">
				<div class="fg-clientes-search-wrap">
					${icon("search", "fg-clientes-search-icon")}
					<input type="text" class="fg-clientes-search-input" placeholder="${__(
						"Buscar por nombre, nombre comercial o documento..."
					)}" value="${frappe.utils.escape_html(this.list_search || "")}">
				</div>
			</div>
			<div class="fg-clientes-tabs">${tabs_html}</div>
			<div class="fg-clientes-cards">${this.render_cards_html()}</div>
			<div class="fg-clientes-pagination">${this.render_pagination_html()}</div>
		`;
	}

	render_cards_html() {
		if (!this.list_rows.length) {
			return `<div class="fg-empty">${__("No hay clientes que coincidan.")}</div>`;
		}
		return this.list_rows.map((c) => this.render_customer_card(c)).join("");
	}

	render_customer_card(c) {
		const status = c.disabled
			? { label: __("Inactivo"), mod: "clientes-inactivo" }
			: { label: __("Activo"), mod: "clientes-activo" };

		const commercial = c.access_nombre_comercial
			? frappe.utils.escape_html(c.access_nombre_comercial)
			: `<span class="fg-clientes-empty-field">${__("Sin nombre comercial")}</span>`;
		const doc = c.tax_id
			? frappe.utils.escape_html(c.tax_id)
			: `<span class="fg-clientes-empty-field">${__("Sin documento")}</span>`;

		return `
			<div class="fg-clientes-card" data-name="${frappe.utils.escape_html(c.name)}">
				<div class="fg-clientes-card-top">
					<div class="fg-clientes-card-name">${frappe.utils.escape_html(c.customer_name)}</div>
					<span class="fg-badge fg-badge--${status.mod}">${status.label}</span>
				</div>
				<div class="fg-clientes-card-meta">
					<span>${commercial}</span>
					<span>${icon("file-text", "fg-icon-sm")} ${doc}</span>
				</div>
				<div class="fg-clientes-card-actions">
					<button type="button" class="fg-clientes-card-action fg-clientes-card-view" data-name="${frappe.utils.escape_html(
						c.name
					)}">${icon("eye", "fg-icon-sm")} ${__("VER")}</button>
					<button type="button" class="fg-clientes-card-action fg-clientes-card-edit" data-name="${frappe.utils.escape_html(
						c.name
					)}">${icon("pencil", "fg-icon-sm")} ${__("EDITAR")}</button>
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
			<div class="fg-clientes-pagination-info">${__("Mostrando {0} a {1} de {2} clientes", [start, end, total])}</div>
			<div class="fg-clientes-pagination-controls">
				<button type="button" class="fg-clientes-pagination-btn fg-clientes-pagination-prev" ${
					this.list_page <= 1 ? "disabled" : ""
				}>${icon("chevron-left")}</button>
				<span class="fg-clientes-pagination-page">${this.list_page}</span>
				<button type="button" class="fg-clientes-pagination-btn fg-clientes-pagination-next" ${
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
				this.$body.find(".fg-clientes-cards").html(this.render_cards_html());
				this.$body.find(".fg-clientes-pagination").html(this.render_pagination_html());
			})
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	bind_list_section_events() {
		this.$body.find(".fg-clientes-new-btn").on("click", () => this.open_customer_form(null));

		this.$body.find(".fg-clientes-search-input").on("input", (e) => {
			const val = $(e.currentTarget).val();
			clearTimeout(this._search_debounce);
			this._search_debounce = setTimeout(() => {
				this.list_search = val;
				this.list_page = 1;
				this.refresh_list_cards();
			}, 300);
		});

		this.$body.find(".fg-clientes-tab").on("click", (e) => {
			this.list_filter = $(e.currentTarget).data("filter");
			this.list_page = 1;
			this.$body.find(".fg-clientes-tab").removeClass("is-active");
			$(e.currentTarget).addClass("is-active");
			this.refresh_list_cards();
		});

		// Delegado sobre el contenedor estable -- sobrevive al .html() de
		// refresh_list_cards() sin necesitar re-bind en cada búsqueda/página.
		this.$body.find(".fg-clientes-cards").on("click", ".fg-clientes-card-view", (e) => {
			e.stopPropagation();
			this.open_detail($(e.currentTarget).data("name"));
		});
		this.$body.find(".fg-clientes-cards").on("click", ".fg-clientes-card-edit", (e) => {
			e.stopPropagation();
			this.edit_customer($(e.currentTarget).data("name"));
		});

		this.$body.find(".fg-clientes-pagination").on("click", ".fg-clientes-pagination-prev", () => {
			this.list_page = Math.max(this.list_page - 1, 1);
			this.refresh_list_cards();
		});
		this.$body.find(".fg-clientes-pagination").on("click", ".fg-clientes-pagination-next", () => {
			this.list_page = this.list_page + 1;
			this.refresh_list_cards();
		});
	}

	// =====================================================================
	// Detalle ("VER")
	// =====================================================================
	open_detail(name) {
		if (!name) return;
		this.detail_name = name;
		this.detail = null;
		this.state.view = "detail";
		this.set_busy(true);
		this.render_detail_skeleton();

		this.call("get_customer_detail", { name: name })
			.then((detail) => {
				this.detail = detail;
				this.render_detail();
			})
			.catch(() => this.back_to_dashboard())
			.finally(() => this.set_busy(false));
	}

	back_to_dashboard() {
		this.detail = null;
		this.detail_name = null;
		this.load_dashboard();
	}

	render_detail_skeleton() {
		this.$body.html(`
			<div class="fg-np-header">
				<button type="button" class="fg-np-back">${icon("arrow-left")} ${__("Volver")}</button>
				<div class="fg-np-title">${__("Detalle de cliente")}</div>
			</div>
			<div class="fg-skeleton fg-clientes-detail-skeleton"></div>
			<div class="fg-skeleton fg-clientes-detail-skeleton"></div>
		`);
		this.$body.find(".fg-np-back").on("click", () => this.back_to_dashboard());
	}

	// Contacto/dirección primarios: si el backend los devuelve null (no
	// existen, o el usuario no tiene permiso de lectura sobre Contact/
	// Address -- ver get_customer_detail()'s propio docstring), se
	// muestra un texto vacío limpio ("Sin contacto registrado"/"Sin
	// dirección registrada"), nunca un error ni una sección rota.
	render_detail() {
		const d = this.detail;
		const status = d.disabled
			? { label: __("Inactivo"), mod: "clientes-inactivo" }
			: { label: __("Activo"), mod: "clientes-activo" };

		const contact_html = d.contact
			? `
				<div class="fg-clientes-detail-subrow">${frappe.utils.escape_html(
					[d.contact.first_name, d.contact.last_name].filter(Boolean).join(" ") || d.contact.name
				)}</div>
				${d.contact.email_id ? `<div class="fg-clientes-detail-subrow-muted">${frappe.utils.escape_html(d.contact.email_id)}</div>` : ""}
				${
					d.contact.mobile_no || d.contact.phone
						? `<div class="fg-clientes-detail-subrow-muted">${frappe.utils.escape_html(d.contact.mobile_no || d.contact.phone)}</div>`
						: ""
				}
			`
			: `<div class="fg-clientes-empty-field">${__("Sin contacto registrado")}</div>`;

		const address_html = d.address
			? `
				<div class="fg-clientes-detail-subrow">${frappe.utils.escape_html(d.address.address_line1 || "")}</div>
				<div class="fg-clientes-detail-subrow-muted">${frappe.utils.escape_html(
					[d.address.city, d.address.country].filter(Boolean).join(", ")
				)}</div>
			`
			: `<div class="fg-clientes-empty-field">${__("Sin dirección registrada")}</div>`;

		const toggle_label = d.disabled ? __("ACTIVAR CLIENTE") : __("DESACTIVAR CLIENTE");
		const toggle_class = d.disabled ? "fg-btn--outline-success" : "fg-btn--outline-danger";

		this.$body.html(`
			<div class="fg-np-header">
				<button type="button" class="fg-np-back">${icon("arrow-left")} ${__("Volver")}</button>
				<div class="fg-np-title">${__("Detalle de cliente")}</div>
			</div>

			<div class="fg-clientes-detail-card">
				<div class="fg-clientes-detail-top">
					<div class="fg-clientes-detail-name">${frappe.utils.escape_html(d.customer_name)}</div>
					<span class="fg-badge fg-badge--${status.mod}">${status.label}</span>
				</div>

				<div class="fg-clientes-detail-grid">
					<div class="fg-clientes-detail-field">
						<div class="fg-clientes-detail-label">${__("Nombre comercial")}</div>
						<div>${
							d.access_nombre_comercial
								? frappe.utils.escape_html(d.access_nombre_comercial)
								: `<span class="fg-clientes-empty-field">${__("Sin nombre comercial")}</span>`
						}</div>
					</div>
					<div class="fg-clientes-detail-field">
						<div class="fg-clientes-detail-label">${__("Documento")}</div>
						<div>${
							d.tax_id
								? frappe.utils.escape_html(d.tax_id)
								: `<span class="fg-clientes-empty-field">${__("Sin documento")}</span>`
						}</div>
					</div>
					<div class="fg-clientes-detail-field">
						<div class="fg-clientes-detail-label">${__("Tipo de cliente")}</div>
						<div>${frappe.utils.escape_html(d.customer_type || "—")}</div>
					</div>
				</div>

				<div class="fg-clientes-detail-section">
					<div class="fg-clientes-detail-label">${__("Contacto primario")}</div>
					${contact_html}
				</div>
				<div class="fg-clientes-detail-section">
					<div class="fg-clientes-detail-label">${__("Dirección primaria")}</div>
					${address_html}
				</div>

				<div class="fg-clientes-detail-actions">
					<button type="button" class="fg-btn fg-btn--solid-primary fg-clientes-detail-edit">
						${icon("pencil", "fg-icon-sm")} ${__("EDITAR")}
					</button>
					<button type="button" class="fg-btn ${toggle_class} fg-clientes-detail-toggle">
						${icon(d.disabled ? "check" : "lock", "fg-icon-sm")} ${toggle_label}
					</button>
				</div>
			</div>
		`);

		this.$body.find(".fg-np-back").on("click", () => this.back_to_dashboard());
		this.$body.find(".fg-clientes-detail-edit").on("click", () => this.open_customer_form(this.detail));
		this.$body.find(".fg-clientes-detail-toggle").on("click", () => this.confirm_toggle_disabled());
	}

	confirm_toggle_disabled() {
		if (!this.detail) return;
		const currently_disabled = !!this.detail.disabled;
		const next_disabled = !currently_disabled;
		const msg = currently_disabled ? __("¿Activar este cliente?") : __("¿Desactivar este cliente?");
		const name = this.detail.name;

		frappe.confirm(msg, () => {
			this.call("set_customer_disabled", { name: name, disabled: next_disabled })
				.then(() => {
					frappe.show_alert(
						{
							message: next_disabled ? __("Cliente desactivado.") : __("Cliente activado."),
							indicator: "green",
						},
						5
					);
					this.open_detail(name);
				})
				.catch(() => {
					// El error real (permiso/validación) ya lo mostró frappe.call().
				});
		});
	}

	// Editar desde la lista: el card solo trae customer_name/access_
	// nombre_comercial/tax_id/disabled (Commit 22.1's _LIST_FIELDS), no
	// customer_type -- se trae el detalle completo primero para no
	// precargar el formulario con un tipo de cliente adivinado.
	edit_customer(name) {
		if (!name) return;
		this.set_busy(true);
		this.call("get_customer_detail", { name: name })
			.then((detail) => this.open_customer_form(detail))
			.catch(() => {})
			.finally(() => this.set_busy(false));
	}

	// =====================================================================
	// Formulario Nuevo/Editar Cliente -- un solo diálogo reutilizado para
	// ambos flujos. Únicos campos que este formulario puede enviar:
	// customer_name, access_nombre_comercial, tax_id, customer_type --
	// nunca access_id_cliente, nunca disabled (ver _build_customer_payload()).
	// customer_type se resuelve desde la metadata real y viva del doctype
	// Customer (frappe.model.with_doctype + frappe.get_meta(), el mismo
	// mecanismo que ya usa page/bodega/bodega.js para "Motivo" en su
	// diálogo de faltante) -- no un endpoint nuevo, no una copia
	// hardcodeada que pudiera desalinearse silenciosamente.
	// =====================================================================
	open_customer_form(existing) {
		const is_edit = !!existing;

		frappe.model.with_doctype("Customer", () => {
			const field = frappe.get_meta("Customer").fields.find((f) => f.fieldname === "customer_type");
			const options = field && field.options ? field.options.split("\n").map((o) => o.trim()).filter(Boolean) : DEFAULT_CUSTOMER_TYPES;

			const dialog = new frappe.ui.Dialog({
				title: is_edit ? __("Editar cliente") : __("Nuevo cliente"),
				fields: [
					{
						fieldtype: "Data",
						fieldname: "customer_name",
						label: __("Nombre / Razón social"),
						reqd: 1,
						default: is_edit ? existing.customer_name : "",
					},
					{
						fieldtype: "Data",
						fieldname: "access_nombre_comercial",
						label: __("Nombre comercial"),
						default: is_edit ? existing.access_nombre_comercial || "" : "",
					},
					{
						fieldtype: "Data",
						fieldname: "tax_id",
						label: __("Documento / NIT"),
						default: is_edit ? existing.tax_id || "" : "",
					},
					{
						fieldtype: "Select",
						fieldname: "customer_type",
						label: __("Tipo de cliente"),
						options: options,
						reqd: 1,
						default: is_edit ? existing.customer_type || options[0] : "Company",
					},
				],
				primary_action_label: is_edit ? __("GUARDAR CAMBIOS") : __("CREAR CLIENTE"),
				primary_action: (values) => {
					dialog.disable_primary_action();
					const payload = this._build_customer_payload(values);

					const request = is_edit
						? this.call("update_customer", { name: existing.name, customer: payload })
						: this.call("create_customer", { customer: payload });

					request
						.then((res) => {
							dialog.hide();
							frappe.show_alert(
								{ message: is_edit ? __("Cliente actualizado.") : __("Cliente creado."), indicator: "green" },
								5
							);
							if (is_edit) {
								if (this.state.view === "detail") this.open_detail(existing.name);
								else this.refresh_list_cards();
							} else {
								this.load_dashboard();
							}
						})
						.catch(() => {
							dialog.enable_primary_action();
						});
				},
				secondary_action_label: __("CANCELAR"),
				secondary_action: () => dialog.hide(),
			});

			dialog.$wrapper.addClass("fg-clientes-form-dialog");
			dialog.show();
		});
	}

	// El único lugar donde se arma el payload de create_customer()/
	// update_customer() -- exactamente estas 4 claves, nunca más. No hay
	// forma de que este formulario envíe access_id_cliente o disabled:
	// ningún campo del Dialog los produce, y esta función no los agrega.
	_build_customer_payload(values) {
		return {
			customer_name: (values.customer_name || "").trim(),
			access_nombre_comercial: (values.access_nombre_comercial || "").trim() || null,
			tax_id: (values.tax_id || "").trim() || null,
			customer_type: values.customer_type,
		};
	}
};

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, no server calls, no state.
// Intentionally duplicated (not imported) from ventas.js/bodega.js/
// cotizaciones.js/facturacion.js, same reasoning as Commit 6.
// -------------------------------------------------------------------------
const PAGE_SIZE = 10;
const INCOMPLETE_FETCH_CAP = 8000; // margen amplio sobre el total real de clientes (~4091) -- ver comentario en load_list()
const DEFAULT_CUSTOMER_TYPES = ["Company", "Individual", "Partnership"];

function paginate(items, page, page_size) {
	const total = items.length;
	const page_count = Math.max(Math.ceil(total / page_size), 1);
	const safe_page = Math.min(Math.max(page || 1, 1), page_count);
	const start = (safe_page - 1) * page_size;
	return { page_items: items.slice(start, start + page_size), total, page_count, page: safe_page };
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
