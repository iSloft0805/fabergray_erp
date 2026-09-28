// Copyright (c) 2026, Fabrigray SAS and contributors
// For license information, please see license.txt

frappe.provide("fabergray_erp");

frappe.pages["reporte-faltantes"].on_page_load = function (wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Reporte de Faltantes"),
		single_column: true,
	});
	wrapper.reporte_faltantes = new fabergray_erp.ReporteFaltantes(page);
};

// Hotfix "Reporte PDF de faltantes" -- Jefe de Bodega > REPORTES. Read-only:
// every rule (period -> dates in the SITE's timezone, company, roles,
// status whitelist, resolution date) lives in api/reporte_faltantes.py.
// This Page only sends the chosen period/status, shows the server's
// summary and opens the server-generated PDF -- it never computes a date
// range or a total itself.
fabergray_erp.ReporteFaltantes = class ReporteFaltantes {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.reporte_faltantes.";
		this.busy = false;

		this.period = "hoy";
		this.status = "";
		this.from_date = "";
		this.to_date = "";
		this.report = null;
		this.generating = false;

		this.$app = $('<div class="fg-shell fg-reporte-faltantes">').appendTo(this.page.body);
		this.render_shell();
		this.render_body();
		this.load();
	}

	// frappe.call() returns a jQuery promise (jQuery 3.7): it has
	// .then/.catch but NO .finally -- chaining .finally() on it threw a
	// TypeError, so set_busy(false) never ran and VISTA PREVIA / GENERAR PDF
	// stayed disabled after a successful load. Wrapped in a native Promise
	// (same idiom as page/recorridos/recorridos.js::_frappe_call()).
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

	set_busy(is_busy) {
		this.busy = !!is_busy;
		this.$app.find(".fg-refresh-btn").prop("disabled", this.busy);
		this.$app.toggleClass("fg-loading", !!is_busy);
		this.update_actions();
	}

	// VISTA PREVIA / GENERAR PDF: enabled once a valid report for the
	// current filters is loaded -- any status, any number of rows (an empty
	// period still yields a valid "sin faltantes" PDF) -- and disabled only
	// while loading/generating or without a valid report (incomplete custom
	// range, or the last query failed).
	update_actions() {
		const enabled = !this.busy && !this.generating && !!this.report;
		this.$app.find(".fg-rf-generate, .fg-rf-preview").prop("disabled", !enabled);
	}

	render_shell() {
		const fullname = frappe.session.user_fullname || frappe.session.user;
		this.$app.html(`
			<div class="fg-header">
				<div class="fg-header-brand">
					<button type="button" class="fg-back-btn">${icon("arrow-left")} ${__("Jefe de Bodega")}</button>
					<span class="fg-header-logo">FABRIGRAY</span>
					<span class="fg-header-sep">|</span>
					<span class="fg-header-title">${__("REPORTE DE FALTANTES")}</span>
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
		this.$app.find(".fg-refresh-btn").on("click", () => this.load());
	}

	// The period/status the server needs -- null when a custom range is
	// still incomplete (nothing is requested until both dates exist).
	query_args() {
		const args = { period: this.period, status: this.status || null };
		if (this.period === "rango") {
			if (!this.from_date || !this.to_date) return null;
			args.from_date = this.from_date;
			args.to_date = this.to_date;
		}
		return args;
	}

	load() {
		const args = this.query_args();
		if (!args) {
			this.report = null;
			this.render_results();
			return Promise.resolve();
		}
		this.set_busy(true);
		return this.call("get_shortage_report", args)
			.then((report) => {
				this.report = report;
			})
			.catch(() => {
				// The server's own message (invalid range, permissions...) was
				// already shown by frappe.call. No valid report for these
				// filters -> nothing to print.
				this.report = null;
			})
			.finally(() => {
				this.set_busy(false);
				this.render_results();
			});
	}

	render_body() {
		const periods = [
			{ key: "hoy", label: __("HOY") },
			{ key: "7_dias", label: __("7 DÍAS") },
			{ key: "30_dias", label: __("30 DÍAS") },
			{ key: "este_mes", label: __("ESTE MES") },
			{ key: "rango", label: __("RANGO PERSONALIZADO") },
		];
		const statuses = [
			{ key: "", label: __("TODOS") },
			{ key: "Abierto", label: __("ABIERTOS") },
			{ key: "En Proceso", label: __("EN PROCESO") },
			{ key: "Resuelto", label: __("RESUELTOS") },
		];
		const chips = (items, attr, current) =>
			items
				.map(
					(i) =>
						`<button type="button" class="fg-rf-chip ${current === i.key ? "is-active" : ""}" data-${attr}="${
							i.key
						}">${i.label}</button>`
				)
				.join("");

		this.$body.html(`
			<div class="fg-rf-filters">
				<div class="fg-rf-filter-group">
					<div class="fg-rf-filter-label">${__("Período")}</div>
					<div class="fg-rf-chips">${chips(periods, "period", this.period)}</div>
					<div class="fg-rf-range ${this.period === "rango" ? "" : "is-hidden"}">
						<label>${__("Desde")} <input type="date" class="fg-rf-from" value="${frappe.utils.escape_html(this.from_date)}"></label>
						<label>${__("Hasta")} <input type="date" class="fg-rf-to" value="${frappe.utils.escape_html(this.to_date)}"></label>
					</div>
				</div>
				<div class="fg-rf-filter-group">
					<div class="fg-rf-filter-label">${__("Estado")}</div>
					<div class="fg-rf-chips">${chips(statuses, "status", this.status)}</div>
				</div>
			</div>
			<div class="fg-rf-results"></div>
			<div class="fg-rf-actions">
				<button type="button" class="fg-btn fg-btn--ghost fg-rf-preview">${icon("eye", "fg-icon-sm")} ${__("VISTA PREVIA")}</button>
				<button type="button" class="fg-btn fg-btn--solid-primary fg-rf-generate">${icon("file-down", "fg-icon-sm")} ${__(
					"GENERAR PDF"
				)}</button>
			</div>
		`);
		this.bind_events();
		this.render_results();
	}

	render_results() {
		const $r = this.$body.find(".fg-rf-results");
		if (!this.report) {
			const message = this.busy
				? __("Cargando...")
				: this.period === "rango" && !this.query_args()
				? __("Elige la fecha desde y la fecha hasta.")
				: __("No se pudo cargar el reporte para este período.");
			$r.html(`<div class="fg-empty">${message}</div>`);
			this.update_actions();
			return;
		}
		const r = this.report;
		const s = r.summary || {};
		const kpis = [
			{ label: __("Total"), value: s.total_reports, mod: "rf-total", i: "list" },
			{ label: __("Abiertos"), value: s.open, mod: "rf-abiertos", i: "triangle-alert" },
			{ label: __("En proceso"), value: s.in_progress, mod: "rf-proceso", i: "clock" },
			{ label: __("Resueltos"), value: s.resolved, mod: "rf-resueltos", i: "circle-check-big" },
		]
			.map(
				(k) => `
				<div class="fg-kpi fg-kpi--${k.mod}">
					<div class="fg-kpi-icon">${icon(k.i)}</div>
					<div class="fg-kpi-number">${k.value ?? 0}</div>
					<div class="fg-kpi-label">${k.label}</div>
				</div>`
			)
			.join("");
		const items = r.items || [];
		const cards = items.length
			? items.map((row) => this.render_card(row)).join("")
			: `<div class="fg-empty">${__("No hay faltantes reportados en este período.")}</div>`;
		$r.html(`
			<div class="fg-rf-period">${__("Período")}: <strong>${format_d(r.from_date)} – ${format_d(r.to_date)}</strong>
				· ${__("Cantidad total faltante")}: <strong>${format_qty(s.total_shortage_qty)}</strong></div>
			<div class="fg-kpis fg-kpis--rf">${kpis}</div>
			<div class="fg-rf-cards">${cards}</div>
		`);
		this.update_actions();
	}

	render_card(row) {
		const status = STATUS_META[row.status] || { label: row.status || "—", mod: "rf-abierto" };
		const text = (v) => frappe.utils.escape_html(v == null || v === "" ? "—" : String(v));
		return `
			<div class="fg-rf-card">
				<div class="fg-rf-card-top">
					<span class="fg-rf-card-order">${text(row.order_number)}</span>
					<span class="fg-status-pill fg-status-pill--${status.mod}">${status.label}</span>
				</div>
				<div class="fg-rf-card-item">${text(row.item_name || row.item_code)}</div>
				<div class="fg-rf-card-meta">${text(row.item_code)} · ${text(row.customer_name)}</div>
				<div class="fg-rf-card-meta">${__("Reportado")}: ${format_d(row.reported_on)} · ${text(row.warehouse)}</div>
				<div class="fg-rf-card-qty">
					<span>${__("Solicitado")} <strong>${format_qty(row.qty_solicitada)}</strong></span>
					<span>${__("Disponible")} <strong>${format_qty(row.qty_disponible)}</strong></span>
					<span>${__("Faltante")} <strong class="fg-rf-card-shortage">${format_qty(row.qty_faltante)}</strong></span>
				</div>
				${row.resolved_on ? `<div class="fg-rf-card-meta">${__("Solucionado")}: ${format_d(row.resolved_on)}</div>` : ""}
			</div>
		`;
	}

	bind_events() {
		this.$body.find(".fg-rf-chips").on("click", ".fg-rf-chip", (e) => {
			const $chip = $(e.currentTarget);
			if ($chip.data("period") !== undefined) {
				this.period = String($chip.data("period"));
			} else {
				this.status = String($chip.data("status") || "");
			}
			$chip.addClass("is-active").siblings().removeClass("is-active");
			this.$body.find(".fg-rf-range").toggleClass("is-hidden", this.period !== "rango");
			this.load();
		});
		this.$body.find(".fg-rf-from, .fg-rf-to").on("change", () => {
			this.from_date = this.$body.find(".fg-rf-from").val() || "";
			this.to_date = this.$body.find(".fg-rf-to").val() || "";
			this.load();
		});
		this.$body.find(".fg-rf-preview").on("click", () => this.generate_pdf(true));
		this.$body.find(".fg-rf-generate").on("click", () => this.generate_pdf(false));
	}

	// The server validates everything again and builds the PDF; the client
	// only passes the same period/status it already queried. fetch() + Blob
	// (never frappe.call(), which would parse the body as JSON): the PDF bytes
	// are never turned into text. A failure shows a clear message instead of
	// opening a tab with raw JSON.
	generate_pdf(preview) {
		const args = this.query_args();
		if (!args || !this.report || this.generating) return;

		const params = new URLSearchParams();
		Object.entries(args).forEach(([k, v]) => {
			if (v) params.set(k, v);
		});
		if (preview) params.set("preview", "1");
		const url = "/api/method/fabergray_erp.api.reporte_faltantes.download_shortage_report_pdf?" + params.toString();

		// VISTA PREVIA: the tab is opened NOW, inside the click, or the
		// browser's popup blocker would stop a window.open() made after the
		// await (same reason as facturacion.js::open_fabrigray_invoice_pdf()).
		const tab = preview ? window.open("about:blank") : null;

		this.generating = true;
		this.update_actions();
		return fetch(url, { method: "GET", credentials: "same-origin", headers: { Accept: "application/pdf" } })
			.then((response) => {
				const type = response.headers.get("Content-Type") || "";
				if (!response.ok || !type.includes("application/pdf")) {
					return response.text().then((body) => {
						throw new Error(`HTTP ${response.status} ${type}: ${body.slice(0, 500)}`);
					});
				}
				const filename =
					filename_from_disposition(response.headers.get("Content-Disposition")) ||
					`Reporte-Faltantes-${this.report.from_date}-a-${this.report.to_date}.pdf`;
				return response.blob().then((blob) => ({ blob, filename }));
			})
			.then(({ blob, filename }) => {
				const blob_url = URL.createObjectURL(new Blob([blob], { type: "application/pdf" }));
				if (preview) {
					if (tab) tab.location = blob_url;
					else window.open(blob_url);
				} else {
					const a = document.createElement("a");
					a.href = blob_url;
					a.download = filename;
					document.body.appendChild(a);
					a.click();
					a.remove();
				}
				setTimeout(() => URL.revokeObjectURL(blob_url), 60000);
			})
			.catch((error) => {
				if (tab) tab.close();
				console.error("Reporte de Faltantes: PDF generation failed", error);
				frappe.msgprint({
					title: __("Reporte de Faltantes"),
					indicator: "red",
					message: __("No pudimos generar el PDF."),
				});
			})
			.finally(() => {
				this.generating = false;
				this.update_actions();
			});
	}
};

// -------------------------------------------------------------------------
// Small render helpers -- pure presentation, intentionally duplicated
// (same convention as every other Page of this app).
// -------------------------------------------------------------------------
const STATUS_META = {
	Abierto: { label: __("ABIERTO"), mod: "rf-abierto" },
	"En Proceso": { label: __("EN PROCESO"), mod: "rf-proceso" },
	Resuelto: { label: __("RESUELTO"), mod: "rf-resuelto" },
};

// filename="..." (or RFC 5987 filename*=UTF-8''...) from the server's
// Content-Disposition header; null when absent.
function filename_from_disposition(header) {
	if (!header) return null;
	const star = /filename\*=UTF-8''([^;]+)/i.exec(header);
	if (star) return decodeURIComponent(star[1]);
	const plain = /filename="?([^";]+)"?/i.exec(header);
	return plain ? plain[1] : null;
}

function format_d(value) {
	return value ? frappe.datetime.str_to_user(String(value).split(" ")[0]) : "—";
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

function flt(v) {
	return frappe.utils.flt ? frappe.utils.flt(v) : parseFloat(v) || 0;
}

function format_qty(v) {
	const n = flt(v);
	return Number.isInteger(n) ? String(n) : n.toFixed(2);
}
