# Part of Odoo. See LICENSE file for full copyright and licensing details.

from collections import defaultdict
import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Command

from .maas_rate import RATE_METRICS


class MaasUsageEvent(models.Model):
    _name = "maas.usage.event"
    _description = "MaaS Usage Event"
    _order = "event_datetime desc, id desc"
    _check_company_auto = True

    name = fields.Char(string="Reference", required=True, copy=False, default="New", index=True)
    external_reference = fields.Char(string="Source Reference", index=True, copy=False)
    company_id = fields.Many2one(
        comodel_name="res.company", required=True, default=lambda self: self.env.company, index=True
    )
    partner_id = fields.Many2one(comodel_name="res.partner", string="Customer", required=True, index=True)
    product_id = fields.Many2one(
        comodel_name="product.product",
        string="MaaS Product",
        required=True,
        domain="[('sale_business_line', '=', 'maas'), ('sale_ok', '=', True)]",
        check_company=True,
        index=True,
    )
    product_tmpl_id = fields.Many2one(related="product_id.product_tmpl_id", store=True, index=True)
    order_id = fields.Many2one(
        comodel_name="sale.order",
        string="Sales Order",
        domain="[('business_line', '=', 'maas'), ('partner_id', '=', partner_id)]",
        check_company=True,
        index=True,
    )
    sale_line_id = fields.Many2one(
        comodel_name="sale.order.line",
        string="Sales Order Line",
        domain="[('order_id', '=', order_id), ('product_id', '=', product_id)]",
        check_company=True,
        index=True,
    )
    event_datetime = fields.Datetime(string="Usage Time", required=True, default=fields.Datetime.now, index=True)
    metric = fields.Selection(selection=RATE_METRICS, required=True, index=True)
    quantity = fields.Float(string="Quantity", required=True, digits=(24, 6))
    context_length = fields.Float(
        string="Request Context Length",
        digits=(24, 6),
        help="Prompt context size used to match context-length pricing tiers.",
    )
    billing_period_quantity = fields.Float(
        string="Billing Period Quantity",
        digits=(24, 6),
        help="Total quantity in the billing period, used by billing-period tiers.",
    )
    source_unit_name = fields.Char(string="Source Unit", required=True, default="Tokens")
    billed_unit_name = fields.Char(string="Billable Unit", readonly=True, copy=False)
    billed_quantity = fields.Float(string="Billable Quantity", digits=(24, 6), readonly=True, copy=False)
    price_card_id = fields.Many2one(
        comodel_name="maas.product.price.card",
        string="Applied Price Card",
        readonly=True,
        copy=False,
        check_company=True,
    )
    currency_id = fields.Many2one(
        comodel_name="res.currency", compute="_compute_currency", readonly=True
    )
    matched_rate_id = fields.Many2one(
        comodel_name="maas.product.rate",
        string="Matched Catalog Rate",
        readonly=True,
        copy=False,
        check_company=True,
    )
    discount_percent = fields.Float(string="Quote Discount (%)", readonly=True, copy=False)
    unit_price = fields.Monetary(string="List Rate", currency_field="currency_id", readonly=True, copy=False)
    amount = fields.Monetary(string="Net Amount", currency_field="currency_id", readonly=True, copy=False)
    pricing_error = fields.Char(string="Pricing Error", readonly=True, copy=False)
    state = fields.Selection(
        selection=[("draft", "Unrated"), ("rated", "Rated"), ("invoiced", "Invoiced")],
        default="draft",
        required=True,
        index=True,
        copy=False,
    )
    invoice_id = fields.Many2one(
        comodel_name="account.move",
        string="Invoice",
        readonly=True,
        copy=False,
        check_company=True,
    )

    @api.depends("order_id.currency_id", "price_card_id.currency_id", "product_tmpl_id.currency_id")
    def _compute_currency(self):
        for event in self:
            event.currency_id = (
                event.order_id.currency_id
                or event.price_card_id.currency_id
                or event.product_tmpl_id.currency_id
            )

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            if values.get("name") in (False, "New"):
                values["name"] = self.env["ir.sequence"].next_by_code("maas.usage.event") or _("New")
            product = self.env["product.product"].browse(values.get("product_id"))
            if product and product.sale_business_line == "maas":
                values.setdefault("metric", "api_usage" if product.maas_product_type == "api" else "input")
                values.setdefault(
                    "source_unit_name",
                    product.uom_id.name if product.maas_product_type == "api" else "Tokens",
                )
        return super().create(vals_list)

    @api.onchange("product_id")
    def _onchange_product_id(self):
        for event in self:
            template = event.product_id.product_tmpl_id
            if template.maas_product_type == "api":
                event.metric = "api_usage"
                event.source_unit_name = template.uom_id.name
            elif template.maas_product_type == "model":
                event.metric = "input"
                event.source_unit_name = "Tokens"

    @api.onchange("order_id", "product_id")
    def _onchange_sale_line_id(self):
        for event in self:
            matching_lines = event.order_id.order_line.filtered(
                lambda line: line.product_id == event.product_id
            )
            if event.sale_line_id and event.sale_line_id not in matching_lines:
                event.sale_line_id = False
            if len(matching_lines) == 1:
                event.sale_line_id = matching_lines

    @api.constrains(
        "product_id", "metric", "order_id", "sale_line_id", "partner_id", "company_id",
        "quantity", "billing_period_quantity", "context_length",
    )
    def _check_usage_event(self):
        for event in self:
            product = event.product_id.product_tmpl_id
            if product.sale_business_line != "maas":
                raise ValidationError(_("Usage events can only be recorded against a TokensHub MaaS product."))
            if product.maas_product_type == "model" and event.metric not in ("input", "output", "cache", "cache_read", "cache_write"):
                raise ValidationError(_("Model service usage must use a Token metric."))
            if product.maas_product_type == "api" and event.metric != "api_usage":
                raise ValidationError(_("API services must use the API service usage metric."))
            if event.order_id and event.order_id.partner_id != event.partner_id:
                raise ValidationError(_("The sales order and usage event must have the same customer."))
            if event.order_id:
                if event.order_id.business_line != "maas":
                    raise ValidationError(_("Link a usage event to a MaaS sales order."))
                matching_lines = event.order_id.order_line.filtered(
                    lambda line: line.product_id == event.product_id
                )
                if not matching_lines:
                    raise ValidationError(_("The MaaS product must be included in the linked sales order."))
                if event.sale_line_id and event.sale_line_id not in matching_lines:
                    raise ValidationError(_("The selected sales order line must match the usage product and order."))
                if len(matching_lines) > 1 and not event.sale_line_id:
                    raise ValidationError(
                        _("Select the sales order line for usage when the product appears more than once on the order.")
                    )
            elif event.sale_line_id:
                raise ValidationError(_("Select a sales order before selecting a sales order line."))
            if event.quantity <= 0:
                raise ValidationError(_("Usage quantity must be greater than zero."))
            if event.billing_period_quantity < 0 or event.context_length < 0:
                raise ValidationError(_("Tier quantities cannot be negative."))

    def _local_event_datetime(self, rate):
        self.ensure_one()
        timezone = pytz.timezone(rate.timezone or "UTC")
        utc_datetime = pytz.utc.localize(self.event_datetime) if self.event_datetime.tzinfo is None else self.event_datetime
        return utc_datetime.astimezone(timezone)

    def _rate_matches(self, rate, include_tier=True):
        self.ensure_one()
        local_datetime = self._local_event_datetime(rate)
        event_date = local_datetime.date()
        if rate.date_start and event_date < rate.date_start:
            return False
        if rate.date_end and event_date > rate.date_end:
            return False
        if rate.use_time_window:
            local_time = local_datetime.hour + local_datetime.minute / 60 + local_datetime.second / 3600
            if rate.time_from < rate.time_to:
                if not rate.time_from <= local_time < rate.time_to:
                    return False
            elif not (local_time >= rate.time_from or local_time < rate.time_to):
                return False
        if include_tier and rate.is_tiered:
            if rate.tier_basis == "billing_period":
                tier_quantity = self.billing_period_quantity
            elif rate.tier_basis == "context_length":
                tier_quantity = self.context_length
            else:
                tier_quantity = self.quantity
            if rate.tier_basis == "billing_period" and not tier_quantity:
                return False
            if rate.tier_basis != "context_length":
                tier_quantity /= rate.quantity_divisor
            if tier_quantity < rate.min_quantity:
                return False
            if rate.max_quantity and tier_quantity >= rate.max_quantity:
                return False
        return True

    def _get_sale_order_line(self):
        self.ensure_one()
        if self.sale_line_id:
            return self.sale_line_id
        if not self.order_id:
            return self.env["sale.order.line"]
        candidates = self.order_id.order_line.filtered(lambda line: line.product_id == self.product_id)
        return candidates if len(candidates) == 1 else self.env["sale.order.line"]

    def _get_pricing_context(self):
        self.ensure_one()
        sale_line = self._get_sale_order_line()
        if sale_line and sale_line.maas_price_card_id:
            return sale_line.maas_price_card_id, sale_line.discount
        card = self.product_tmpl_id._get_maas_price_card(self.event_datetime.date())
        return card, sale_line.discount if sale_line else 0.0

    def _select_rate(self, card):
        self.ensure_one()
        if card:
            rules = card.rate_ids.filtered(
                lambda rate: rate.active and rate.pricing_role == "rule" and rate.metric == self.metric
            ).filtered(lambda rate: self._rate_matches(rate))
            if rules:
                return rules.sorted(
                    key=lambda rate: (
                        not rate.is_tiered,
                        not rate.use_time_window,
                        not bool(rate.date_start or rate.date_end),
                        -rate.min_quantity if rate.is_tiered else 0,
                        rate.sequence,
                    )
                )[0]
            return card.rate_ids.filtered(
                lambda rate: rate.active and rate.pricing_role == "standard" and rate.metric == self.metric
            )[:1]
        return self.env["maas.product.rate"]

    @staticmethod
    def _progressive_charge(rates, normalized_quantity):
        total = 0.0
        remaining = normalized_quantity
        for rate in rates.sorted(key=lambda row: row.min_quantity):
            if remaining <= 0:
                break
            start = rate.min_quantity
            end = rate.max_quantity or normalized_quantity
            portion = max(0.0, min(normalized_quantity, end) - start)
            if rate.max_quantity:
                portion = min(portion, rate.max_quantity - start)
            total += portion * rate.rate
        return total

    def action_rate(self):
        for event in self.filtered(lambda record: record.state != "invoiced"):
            card, discount = event._get_pricing_context()
            rate = event._select_rate(card)
            if not rate:
                event.write({
                    "price_card_id": card.id if card else False,
                    "matched_rate_id": False,
                    "discount_percent": discount,
                    "unit_price": 0,
                    "billed_quantity": 0,
                    "billed_unit_name": False,
                    "amount": 0,
                    "pricing_error": _("No active price line matches this metric, time, and quantity."),
                    "state": "draft",
                })
                continue

            divisor = rate.quantity_divisor
            billed_quantity = event.quantity / divisor
            list_unit_price = rate.rate
            if card and card.tier_pricing_method == "graduated" and rate.is_tiered:
                if rate.tier_basis == "context_length":
                    event.write({"pricing_error": _("Context-length tiers cannot use progressive pricing."), "state": "draft"})
                    continue
                tier_rates = card.rate_ids.filtered(
                    lambda row: row.active and row.pricing_role == "rule" and row.metric == event.metric
                    and row.is_tiered and row.tier_basis == rate.tier_basis
                    and self._rate_matches(row, include_tier=False)
                )
                total_quantity = (
                    event.billing_period_quantity if rate.tier_basis == "billing_period" else event.quantity
                ) / divisor
                total_charge = self._progressive_charge(tier_rates, total_quantity)
                list_unit_price = total_charge / total_quantity if total_quantity else rate.rate

            if rate.currency_id != event.currency_id:
                list_unit_price = rate.currency_id._convert(
                    list_unit_price,
                    event.currency_id,
                    event.company_id,
                    event.event_datetime.date(),
                    round=False,
                )

            net_amount = billed_quantity * list_unit_price * (1 - discount / 100.0)
            event.write({
                "price_card_id": card.id if card else False,
                "matched_rate_id": rate.id,
                "discount_percent": discount,
                "billed_unit_name": rate.unit_name,
                "unit_price": list_unit_price,
                "billed_quantity": billed_quantity,
                "amount": net_amount,
                "pricing_error": False,
                "state": "rated",
            })
        return True

    def action_create_invoices(self):
        if not self.env.user.has_group("account.group_account_invoice"):
            raise UserError(_("You need invoicing access to create invoices from MaaS usage."))
        events = self.filtered(lambda event: event.state == "rated")
        if not events:
            raise UserError(_("Rate the selected usage events before creating invoices."))
        if events.filtered("invoice_id"):
            raise UserError(_("Some selected usage events already have an invoice."))

        grouped_events = defaultdict(lambda: self.env["maas.usage.event"])
        for event in events:
            grouped_events[(event.partner_id.id, event.company_id.id, event.currency_id.id)] |= event

        invoices = self.env["account.move"]
        for event_group in grouped_events.values():
            partner, company, currency = event_group[0].partner_id, event_group[0].company_id, event_group[0].currency_id
            invoice_lines = []
            for event in event_group:
                metric_label = dict(RATE_METRICS).get(event.metric, event.metric)
                line_name = _(
                    "%(product)s — %(metric)s — %(date)s (%(quantity)s %(unit)s)",
                    product=event.product_id.display_name,
                    metric=metric_label,
                    date=fields.Datetime.to_string(event.event_datetime),
                    quantity=event.billed_quantity,
                    unit=event.billed_unit_name,
                )
                invoice_lines.append(Command.create({
                    "product_id": event.product_id.id,
                    "name": line_name,
                    "quantity": event.billed_quantity,
                    "price_unit": event.unit_price,
                    "discount": event.discount_percent,
                }))
            invoice = self.env["account.move"].with_company(company).create({
                "move_type": "out_invoice",
                "partner_id": partner.id,
                "company_id": company.id,
                "currency_id": currency.id,
                "invoice_date": fields.Date.context_today(self),
                "invoice_line_ids": invoice_lines,
                "ref": _("TokensHub MaaS usage"),
            })
            event_group.write({"invoice_id": invoice.id, "state": "invoiced"})
            invoices |= invoice

        return {
            "type": "ir.actions.act_window",
            "name": _("MaaS Usage Invoices"),
            "res_model": "account.move",
            "view_mode": "list,form",
            "domain": [("id", "in", invoices.ids)],
            "target": "current",
        }
