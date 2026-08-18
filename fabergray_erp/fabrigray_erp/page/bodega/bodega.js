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

// All server communication in this file goes through these six methods --
// nothing here queries Bin, edits Pick List directly, computes permissions,
// decides shortage rules, or touches Sales Order. That logic lives entirely
// in fabergray_erp.api.bodega (see Commit 4).
fabergray_erp.Bodega = class Bodega {
	constructor(page) {
		this.page = page;
		this.method_prefix = "fabergray_erp.api.bodega.";
		this.pending_rows = new Set();
		this.busy = false;
		this.state = {
			view: "queue",
			queue: null,
			filter_bucket: null,
			pick_list: null,
			detail: null,
		};

		this.inject_styles();
		this.$app = $('<div class="bodega-app">').appendTo(this.page.body);
		this.render_shell();
		this.load_queue();
	}

	// -------------------------------------------------------------------
	// Thin API wrappers -- the only place that talks to the server.
	// -------------------------------------------------------------------
	call(method, args) {
		return frappe.call({ method: this.method_prefix + method, args: args || {} }).then((r) => r.message);
	}

	load_queue() {
		this.set_shell_busy(true);
		return this.call("get_queue")
			.then((data) => {
				this.state.view = "queue";
				this.state.queue = data;
				this.state.pick_list = null;
				this.state.detail = null;
				this.render_body();
			})
			.finally(() => this.set_shell_busy(false));
	}

	load_detail(pick_list) {
		this.set_shell_busy(true);
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
	// Shell: header (logo, title, user, refresh) stays fixed across views.
	// -------------------------------------------------------------------
	render_shell() {
		this.$app.html(`
			<div class="bodega-header">
				<div class="bodega-header-brand">
					<span class="bodega-logo">FABRIGRAY</span>
					<span class="bodega-title">${__("Bodega")}</span>
				</div>
				<div class="bodega-header-user">
					<span class="bodega-username">${frappe.utils.escape_html(
						frappe.session.user_fullname || frappe.session.user
					)}</span>
					<button class="btn btn-default bodega-refresh-btn" title="${__("Actualizar")}">
						<svg class="icon icon-sm"><use href="#icon-refresh-cw"></use></svg>
					</button>
				</div>
			</div>
			<div class="bodega-body"></div>
		`);
		this.$body = this.$app.find(".bodega-body");
		this.$app.find(".bodega-refresh-btn").on("click", () => {
			if (this.state.view === "detail" && this.state.pick_list) {
				this.load_detail(this.state.pick_list);
			} else {
				this.load_queue();
			}
		});
	}

	set_shell_busy(is_busy) {
		this.$app.find(".bodega-refresh-btn").prop("disabled", is_busy);
		this.$app.toggleClass("bodega-loading", !!is_busy);
	}

	render_body() {
		if (this.state.view === "detail") {
			this.render_detail();
		} else {
			this.render_queue();
		}
	}

	// -------------------------------------------------------------------
	// Queue view
	// -------------------------------------------------------------------
	static bucket_meta() {
		return {
			pendientes: { label: __("Pendientes"), color: "gray" },
			en_alistamiento: { label: __("En alistamiento"), color: "orange" },
			con_faltantes: { label: __("Con faltantes"), color: "red" },
			listos: { label: __("Listos"), color: "green" },
		};
	}

	render_queue() {
		const queue = this.state.queue || {};
		const meta = Bodega.bucket_meta();
		const order = ["con_faltantes", "en_alistamiento", "pendientes", "listos"];

		const $indicators = $('<div class="bodega-indicators"></div>');
		order.forEach((bucket) => {
			const count = (queue[bucket] || []).length;
			const active = this.state.filter_bucket === bucket;
			$(`
				<button type="button" class="bodega-indicator bodega-indicator-${meta[bucket].color} ${
				active ? "is-active" : ""
			}" data-bucket="${bucket}">
					<span class="bodega-indicator-count">${count}</span>
					<span class="bodega-indicator-label">${meta[bucket].label}</span>
				</button>
			`).appendTo($indicators);
		});
		$indicators.on("click", ".bodega-indicator", (e) => {
			const bucket = $(e.currentTarget).data("bucket");
			this.state.filter_bucket = this.state.filter_bucket === bucket ? null : bucket;
			this.render_queue();
		});

		const buckets_to_show = this.state.filter_bucket ? [this.state.filter_bucket] : order;
		const total_shown = buckets_to_show.reduce((n, b) => n + (queue[b] || []).length, 0);

		let sections_html = "";
		if (!total_shown) {
			sections_html = `<div class="bodega-empty">${__("No hay pedidos en este estado.")}</div>`;
		} else {
			buckets_to_show.forEach((bucket) => {
				const items = queue[bucket] || [];
				if (!items.length) return;
				sections_html += `
					<div class="bodega-section">
						<div class="bodega-section-title">${meta[bucket].label} (${items.length})</div>
						<div class="bodega-cards">
							${items.map((pl) => this.render_queue_card(pl, bucket)).join("")}
						</div>
					</div>
				`;
			});
		}

		this.$body.html(`
			<div class="bodega-queue">
				${$indicators.prop("outerHTML")}
				<div class="bodega-sections">${sections_html}</div>
			</div>
		`);

		this.$body.find(".bodega-indicator").on("click", (e) => {
			const bucket = $(e.currentTarget).data("bucket");
			this.state.filter_bucket = this.state.filter_bucket === bucket ? null : bucket;
			this.render_queue();
		});

		this.$body.find(".bodega-card").on("click", (e) => {
			const $card = $(e.currentTarget);
			const pick_list = $card.data("name");
			const bucket = $card.data("bucket");
			this.open_pick_list(pick_list, bucket);
		});
	}

	render_queue_card(pl, bucket) {
		const meta = Bodega.bucket_meta()[bucket];
		const customer = pl.customer ? frappe.utils.escape_html(pl.customer) : __("Sin cliente");
		const order_line = pl.sales_order
			? `<div class="bodega-card-order">${__("Orden")}: ${frappe.utils.escape_html(pl.sales_order)}</div>`
			: "";
		const started_line =
			bucket !== "pendientes" && pl.fg_started_by
				? `<div class="bodega-card-started">${__("Iniciado por")} ${frappe.utils.escape_html(
						pl.fg_started_by
				  )}</div>`
				: "";

		let button_label = __("VER DETALLE");
		let button_class = "btn-default";
		if (bucket === "pendientes") {
			button_label = __("INICIAR ALISTAMIENTO");
			button_class = "btn-primary";
		} else if (bucket === "en_alistamiento" || bucket === "con_faltantes") {
			button_label = __("CONTINUAR ALISTAMIENTO");
			button_class = "btn-primary";
		}

		return `
			<div class="bodega-card" data-name="${frappe.utils.escape_html(pl.name)}" data-bucket="${bucket}">
				<div class="bodega-card-top">
					<span class="bodega-card-pedido">${__("PEDIDO")} #${frappe.utils.escape_html(pl.name)}</span>
					<span class="indicator-pill ${meta.color}">${meta.label}</span>
				</div>
				${order_line}
				<div class="bodega-card-customer">${customer}</div>
				<div class="bodega-card-meta">${pl.item_count} ${
			pl.item_count === 1 ? __("producto") : __("productos")
		}</div>
				${started_line}
				<button type="button" class="btn ${button_class} btn-block bodega-card-btn">${button_label}</button>
			</div>
		`;
	}

	open_pick_list(pick_list, bucket) {
		if (bucket === "pendientes") {
			this.set_shell_busy(true);
			this.call("start_picking", { name: pick_list })
				.then(() => this.load_detail(pick_list))
				.catch(() => this.load_queue())
				.finally(() => this.set_shell_busy(false));
		} else {
			this.load_detail(pick_list);
		}
	}

	// -------------------------------------------------------------------
	// Detail / picking view
	// -------------------------------------------------------------------
	render_detail() {
		const detail = this.state.detail;
		if (!detail) return;

		const is_open = detail.docstatus === 0;
		const rows = detail.rows || [];
		const picked_lines = rows.filter((r) => r.qty_alistada >= r.qty_solicitada).length;

		const started_banner = detail.fg_started_by
			? `
				<div class="bodega-started-banner">
					${__("Alistamiento iniciado por")} <b>${frappe.utils.escape_html(detail.fg_started_by)}</b>
					<br><span class="text-muted">${__("Hora")}: ${frappe.datetime.str_to_user(detail.fg_started_on)}</span>
				</div>
			`
			: "";

		const done_banner = !is_open
			? `<div class="bodega-done-banner">✓ ${__("Pedido alistado correctamente")}</div>`
			: "";

		this.$body.html(`
			<div class="bodega-detail">
				<div class="bodega-detail-header">
					<button type="button" class="btn btn-default bodega-back-btn">&larr; ${__("Volver")}</button>
					<div class="bodega-detail-title">${__("PEDIDO")} #${frappe.utils.escape_html(detail.name)}</div>
					${
						detail.sales_order
							? `<div class="bodega-detail-order">${__("Orden de venta")}: ${frappe.utils.escape_html(
									detail.sales_order
							  )}</div>`
							: ""
					}
					<div class="bodega-detail-customer">${__("Cliente")}: ${
			detail.customer ? frappe.utils.escape_html(detail.customer) : "—"
		}</div>
					<div class="bodega-detail-progress">${__("Progreso")}: ${picked_lines}/${rows.length} ${__(
			"líneas alistadas"
		)}</div>
					${started_banner}
					${done_banner}
				</div>
				<div class="bodega-item-cards">
					${rows.map((row) => this.render_item_card(row, is_open)).join("")}
				</div>
				${
					is_open
						? `<button type="button" class="btn btn-primary btn-block bodega-finish-btn">${__(
								"TERMINAR ALISTAMIENTO"
						  )}</button>`
						: ""
				}
			</div>
		`);

		this.$body.find(".bodega-back-btn").on("click", () => this.load_queue());
		if (is_open) {
			this.bind_item_card_events();
			this.$body.find(".bodega-finish-btn").on("click", () => this.finish_picking());
		}
	}

	render_item_card(row, is_open) {
		const shortfall = Math.max(flt(row.qty_solicitada) - flt(row.qty_disponible), 0);
		const short_of_requested = flt(row.qty_alistada) < flt(row.qty_solicitada);

		let shortage_html = "";
		if (row.has_shortage_report) {
			shortage_html = `<div class="bodega-shortage-badge">⚠ ${__("Faltante reportado")}</div>`;
		} else if (is_open && short_of_requested) {
			shortage_html = `<button type="button" class="btn btn-danger btn-sm bodega-report-shortage-btn">${__(
				"REPORTAR FALTANTE"
			)}</button>`;
		}

		return `
			<div class="bodega-item-card" data-row="${frappe.utils.escape_html(row.row_name)}">
				<div class="bodega-item-name">${frappe.utils.escape_html(row.item_name || row.item_code)}</div>
				<div class="bodega-item-code">${frappe.utils.escape_html(row.item_code)}</div>
				<div class="bodega-item-qty-info">
					<div><span class="text-muted">${__("Solicitado")}:</span> <b>${format_qty(row.qty_solicitada)} ${
			row.uom || ""
		}</b></div>
					<div><span class="text-muted">${__("Disponible")}:</span> <b>${format_qty(row.qty_disponible)} ${
			row.uom || ""
		}</b></div>
					${
						shortfall > 0
							? `<div class="bodega-item-missing"><span class="text-muted">${__(
									"Faltan"
							  )}:</span> <b>${format_qty(shortfall)} ${row.uom || ""}</b></div>`
							: ""
					}
				</div>
				${
					is_open
						? `
					<div class="bodega-qty-control">
						<button type="button" class="bodega-qty-btn bodega-qty-minus" ${flt(row.qty_alistada) <= 0 ? "disabled" : ""}>&minus;</button>
						<input type="number" inputmode="decimal" class="bodega-qty-input" value="${flt(row.qty_alistada)}" min="0">
						<button type="button" class="bodega-qty-btn bodega-qty-plus">&plus;</button>
					</div>
				`
						: `<div class="bodega-qty-readonly">${__("Alistado")}: <b>${format_qty(row.qty_alistada)} ${
								row.uom || ""
						  }</b></div>`
				}
				${shortage_html}
			</div>
		`;
	}

	bind_item_card_events() {
		this.$body.find(".bodega-item-card").each((i, el) => {
			const $card = $(el);
			const row_name = $card.data("row");
			const $input = $card.find(".bodega-qty-input");

			$card.find(".bodega-qty-minus").on("click", () => {
				const next = Math.max(flt($input.val()) - 1, 0);
				this.commit_picked_qty(row_name, next, $card);
			});
			$card.find(".bodega-qty-plus").on("click", () => {
				const next = flt($input.val()) + 1;
				this.commit_picked_qty(row_name, next, $card);
			});
			$input.on("change", () => {
				this.commit_picked_qty(row_name, flt($input.val()), $card);
			});
			$card.find(".bodega-report-shortage-btn").on("click", () => {
				this.open_report_shortage_dialog(row_name, $card);
			});
		});
	}

	commit_picked_qty(row_name, qty, $card) {
		if (this.pending_rows.has(row_name)) return;
		this.pending_rows.add(row_name);
		$card.addClass("bodega-row-busy");
		$card.find(".bodega-qty-btn, .bodega-qty-input").prop("disabled", true);

		this.call("set_picked_qty", { name: this.state.pick_list, row_name: row_name, qty: qty })
			.then(() => this.load_detail(this.state.pick_list))
			.catch(() => this.load_detail(this.state.pick_list))
			.finally(() => {
				this.pending_rows.delete(row_name);
			});
	}

	open_report_shortage_dialog(row_name, $card) {
		const row = (this.state.detail.rows || []).find((r) => r.row_name === row_name);
		if (!row) return;

		frappe.model.with_doctype("Reporte de Faltante", () => {
			const shortage_field = frappe.get_meta("Reporte de Faltante").fields.find(
				(f) => f.fieldname === "shortage_reason"
			);
			const reason_options = (shortage_field.options || "")
				.split("\n")
				.map((o) => o.trim())
				.filter(Boolean);

			const dialog = new frappe.ui.Dialog({
				title: __("Reportar faltante") + " — " + (row.item_name || row.item_code),
				fields: [
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
				primary_action_label: __("Reportar faltante"),
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
							frappe.show_alert({ message: "⚠ " + __("Faltante reportado"), indicator: "orange" });
							this.load_detail(this.state.pick_list);
						})
						.catch(() => {
							dialog.enable_primary_action();
						});
				},
			});
			dialog.show();
		});
	}

	finish_picking() {
		if (this.busy) return;
		frappe.confirm(__("¿Confirmas que terminaste de alistar este pedido?"), () => {
			this.busy = true;
			const $btn = this.$body.find(".bodega-finish-btn").prop("disabled", true).addClass("bodega-btn-loading");
			this.call("finish_picking", { name: this.state.pick_list })
				.then(() => {
					frappe.show_alert({ message: "✓ " + __("Pedido alistado correctamente"), indicator: "green" });
					this.load_queue();
				})
				.catch(() => {
					// Server already showed the exact validation/concurrency message;
					// reload so the screen reflects the current true state.
					this.load_detail(this.state.pick_list);
				})
				.finally(() => {
					this.busy = false;
					$btn.prop("disabled", false).removeClass("bodega-btn-loading");
				});
		});
	}

	// -------------------------------------------------------------------
	// Styles -- scoped to .bodega-app, reuses Frappe's own theme variables
	// and indicator-pill component instead of hardcoded colors.
	// -------------------------------------------------------------------
	inject_styles() {
		if (document.getElementById("bodega-page-styles")) return;
		const style = document.createElement("style");
		style.id = "bodega-page-styles";
		style.textContent = `
			.bodega-app { max-width: 960px; margin: 0 auto; }
			.bodega-header {
				display: flex; align-items: center; justify-content: space-between;
				padding: var(--padding-md); background: var(--card-bg);
				border: 1px solid var(--border-color); border-radius: var(--border-radius, 8px);
				margin-bottom: var(--margin-md); flex-wrap: wrap; gap: var(--margin-sm);
			}
			.bodega-header-brand { display: flex; align-items: baseline; gap: var(--margin-sm); }
			.bodega-logo { font-weight: 700; letter-spacing: 0.05em; color: var(--text-muted); font-size: 0.8rem; }
			.bodega-title { font-weight: 700; font-size: 1.25rem; color: var(--text-color); }
			.bodega-header-user { display: flex; align-items: center; gap: var(--margin-sm); }
			.bodega-username { color: var(--text-muted); font-size: 0.9rem; }
			.bodega-refresh-btn { min-height: 44px; min-width: 44px; }
			.bodega-loading { opacity: 0.6; pointer-events: none; }

			.bodega-indicators {
				display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--margin-sm);
				margin-bottom: var(--margin-md);
			}
			.bodega-indicator {
				display: flex; flex-direction: column; align-items: center; justify-content: center;
				padding: var(--padding-md) var(--padding-sm); border-radius: var(--border-radius, 8px);
				border: 2px solid transparent; cursor: pointer; min-height: 64px;
			}
			.bodega-indicator-count { font-size: 1.5rem; font-weight: 700; line-height: 1.1; }
			.bodega-indicator-label { font-size: 0.75rem; margin-top: 2px; text-align: center; }
			.bodega-indicator-gray { background: var(--bg-gray); color: var(--text-on-gray); }
			.bodega-indicator-orange { background: var(--bg-orange); color: var(--text-on-orange); }
			.bodega-indicator-red { background: var(--bg-red); color: var(--text-on-red); }
			.bodega-indicator-green { background: var(--bg-green); color: var(--text-on-green); }
			.bodega-indicator.is-active { border-color: currentColor; }

			.bodega-section-title { font-weight: 600; color: var(--text-muted); margin: var(--margin-md) 0 var(--margin-sm); }
			.bodega-empty { text-align: center; color: var(--text-muted); padding: var(--padding-2xl) 0; }

			.bodega-cards {
				display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: var(--margin-md);
			}
			.bodega-card {
				background: var(--card-bg); border: 1px solid var(--border-color); border-radius: var(--border-radius, 8px);
				box-shadow: var(--card-shadow); padding: var(--padding-md); cursor: pointer;
				display: flex; flex-direction: column; gap: 6px;
			}
			.bodega-card-top { display: flex; align-items: center; justify-content: space-between; gap: var(--margin-sm); }
			.bodega-card-pedido { font-weight: 700; }
			.bodega-card-order, .bodega-card-started { font-size: 0.8rem; color: var(--text-muted); }
			.bodega-card-customer { font-size: 1rem; }
			.bodega-card-meta { color: var(--text-muted); font-size: 0.85rem; }
			.bodega-card-btn { min-height: 44px; margin-top: var(--margin-sm); }

			.bodega-detail-header {
				background: var(--card-bg); border: 1px solid var(--border-color); border-radius: var(--border-radius, 8px);
				padding: var(--padding-md); margin-bottom: var(--margin-md);
			}
			.bodega-back-btn { min-height: 44px; margin-bottom: var(--margin-sm); }
			.bodega-detail-title { font-size: 1.3rem; font-weight: 700; }
			.bodega-detail-order, .bodega-detail-customer, .bodega-detail-progress { color: var(--text-muted); margin-top: 2px; }
			.bodega-started-banner { margin-top: var(--margin-sm); padding: var(--padding-sm); background: var(--bg-orange); color: var(--text-on-orange); border-radius: var(--border-radius, 8px); }
			.bodega-done-banner { margin-top: var(--margin-sm); padding: var(--padding-sm); background: var(--bg-green); color: var(--text-on-green); border-radius: var(--border-radius, 8px); font-weight: 600; }

			.bodega-item-cards { display: flex; flex-direction: column; gap: var(--margin-sm); margin-bottom: var(--margin-md); }
			.bodega-item-card {
				background: var(--card-bg); border: 1px solid var(--border-color); border-radius: var(--border-radius, 8px);
				box-shadow: var(--card-shadow); padding: var(--padding-md);
			}
			.bodega-item-card.bodega-row-busy { opacity: 0.5; }
			.bodega-item-name { font-weight: 700; font-size: 1.05rem; }
			.bodega-item-code { color: var(--text-muted); font-size: 0.8rem; margin-bottom: 6px; }
			.bodega-item-qty-info { display: flex; flex-wrap: wrap; gap: var(--margin-md); font-size: 0.95rem; margin-bottom: var(--margin-sm); }
			.bodega-item-missing { color: var(--text-on-red); }

			.bodega-qty-control { display: flex; align-items: center; gap: var(--margin-sm); }
			.bodega-qty-btn {
				min-width: 44px; min-height: 44px; font-size: 1.4rem; line-height: 1;
				border: 1px solid var(--border-color); background: var(--control-bg); border-radius: var(--border-radius, 8px);
				cursor: pointer;
			}
			.bodega-qty-btn:disabled { opacity: 0.4; cursor: not-allowed; }
			.bodega-qty-input {
				width: 90px; min-height: 44px; text-align: center; font-size: 1.3rem; font-weight: 700;
				border: 1px solid var(--border-color); border-radius: var(--border-radius, 8px); background: var(--fg-color); color: var(--text-color);
			}
			.bodega-qty-readonly { font-size: 1.1rem; }

			.bodega-shortage-badge { margin-top: var(--margin-sm); color: var(--text-on-red); font-weight: 600; }
			.bodega-report-shortage-btn { margin-top: var(--margin-sm); min-height: 44px; }

			.bodega-finish-btn { min-height: 56px; font-size: 1.1rem; font-weight: 700; }
			.bodega-btn-loading { opacity: 0.7; }

			@media (max-width: 600px) {
				.bodega-indicators { grid-template-columns: repeat(2, 1fr); }
				.bodega-cards { grid-template-columns: 1fr; }
				.bodega-header { flex-direction: column; align-items: flex-start; }
			}
		`;
		document.head.appendChild(style);
	}
};

function flt(v) {
	return frappe.utils.flt ? frappe.utils.flt(v) : parseFloat(v) || 0;
}

function format_qty(v) {
	const n = flt(v);
	return Number.isInteger(n) ? String(n) : n.toFixed(2);
}
