# Part of Odoo. See LICENSE file for full copyright and licensing details.

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


RATE_METRICS = [
    ("cache", "Cache Tokens"),
    ("input", "Input Tokens"),
    ("output", "Output Tokens"),
    ("cache_read", "Cache Read Tokens"),
    ("cache_write", "Cache Write Tokens"),
    ("api_usage", "API Service Usage"),
]


class MaasProductRate(models.Model):
    _name = "maas.product.rate"
    _description = "MaaS Product Catalog Rate"
    _order = "sequence, metric, min_quantity, id"
    _check_company_auto = True

    name = fields.Char(compute="_compute_name", store=True, translate=True)
    sequence = fields.Integer(default=10)
    product_tmpl_id = fields.Many2one(
        comodel_name="product.template",
        string="Product",
        required=True,
        ondelete="cascade",
        index=True,
        check_company=True,
    )
    price_card_id = fields.Many2one(
        comodel_name="maas.product.price.card",
        string="Price Card",
        required=True,
        ondelete="cascade",
        index=True,
        check_company=True,
    )
    pricing_role = fields.Selection(
        selection=[("standard", "Standard Price"), ("rule", "Conditional Rule")],
        string="Price Line Type",
        required=True,
        default="standard",
        index=True,
    )
    company_id = fields.Many2one(related="product_tmpl_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(
        comodel_name="res.currency", compute="_compute_currency", readonly=True
    )
    metric = fields.Selection(selection=RATE_METRICS, required=True, index=True)
    unit_name = fields.Char(
        string="Rate Unit",
        required=True,
        default="Per million Tokens",
        help="Quantity unit used by this catalog rate, such as per million Tokens or per request.",
    )
    quantity_divisor = fields.Float(
        string="Raw Quantity per Rate Unit",
        required=True,
        default=1_000_000.0,
        help="For a rate per million Tokens, set 1,000,000. For a per-request API rate, set 1.",
    )
    rate = fields.Monetary(
        string="Catalog Rate",
        currency_field="currency_id",
        required=True,
        default=0.0,
        help="Published selling rate for this usage metric and condition.",
    )
    use_time_window = fields.Boolean(string="Time-Based Rate")
    time_from = fields.Float(string="From", help="Local time in the selected time zone.")
    time_to = fields.Float(string="To", help="Local time in the selected time zone.")
    timezone = fields.Char(string="Time Zone", default="UTC", required=True)
    is_tiered = fields.Boolean(string="Tiered Rate")
    tier_basis = fields.Selection(
        selection=[
            ("usage_line", "Usage Line Quantity"),
            ("billing_period", "Billing Period Quantity"),
            ("context_length", "Request Context Length"),
        ],
        string="Tier Basis",
        default="usage_line",
        required=True,
    )
    min_quantity = fields.Float(
        string="Minimum Quantity",
        default=0.0,
        help="Tier limits use the rate unit after quantity normalization; context-length tiers use raw prompt tokens.",
    )
    max_quantity = fields.Float(
        string="Maximum Quantity",
        help="Leave empty for the last/open-ended tier.",
    )
    date_start = fields.Date(string="Effective From")
    date_end = fields.Date(string="Effective To")
    active = fields.Boolean(default=True)

    @api.depends("price_card_id.currency_id", "product_tmpl_id.currency_id")
    def _compute_currency(self):
        for rate in self:
            rate.currency_id = rate.price_card_id.currency_id or rate.product_tmpl_id.currency_id

    @api.depends(
        "metric",
        "pricing_role",
        "unit_name",
        "use_time_window",
        "time_from",
        "time_to",
        "timezone",
        "is_tiered",
        "min_quantity",
        "max_quantity",
        "date_start",
        "date_end",
    )
    def _compute_name(self):
        metric_labels = dict(self.fields_get(["metric"])["metric"]["selection"])

        def format_time(value):
            minutes = round(value * 60)
            return f"{minutes // 60:02d}:{minutes % 60:02d}"

        for rate in self:
            parts = [metric_labels.get(rate.metric, ""), rate.unit_name or ""]
            if rate.pricing_role == "rule":
                parts.insert(0, _("Rule"))
            if rate.is_tiered:
                tier_end = rate.max_quantity or _("and above")
                parts.append(
                    _(
                        "Tier %(minimum)s–%(maximum)s",
                        minimum=rate.min_quantity,
                        maximum=tier_end,
                    )
                )
            if rate.use_time_window:
                parts.append(
                    _(
                        "%(start)s–%(end)s %(timezone)s",
                        start=format_time(rate.time_from),
                        end=format_time(rate.time_to),
                        timezone=rate.timezone,
                    )
                )
            if rate.date_start or rate.date_end:
                parts.append(
                    _(
                        "%(start)s to %(end)s",
                        start=rate.date_start or _("Any date"),
                        end=rate.date_end or _("Open ended"),
                    )
                )
            rate.name = " — ".join(part for part in parts if part)

    @api.onchange("metric")
    def _onchange_metric(self):
        for rate in self:
            if rate.metric == "api_usage":
                if rate.unit_name == "Per million Tokens":
                    rate.unit_name = "API usage unit"
                rate.quantity_divisor = 1.0
            else:
                if rate.unit_name == "API usage unit":
                    rate.unit_name = "Per million Tokens"
                rate.quantity_divisor = 1_000_000.0

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            if values.get("metric") == "api_usage":
                values.setdefault("unit_name", "API usage unit")
                values.setdefault("quantity_divisor", 1.0)
            card = self.env["maas.product.price.card"].browse(values.get("price_card_id"))
            if card and (card.state == "active" or card._is_referenced()):
                raise UserError(_("Active or referenced price cards are locked. Create a new draft version to change their rates."))
        return super().create(vals_list)

    def write(self, vals):
        locked_cards = self.mapped("price_card_id").filtered(
            lambda card: card.state == "active" or card._is_referenced()
        )
        if locked_cards:
            raise UserError(_("Active or referenced price cards are locked. Create a new draft version to change their rates."))
        return super().write(vals)

    def unlink(self):
        locked_cards = self.mapped("price_card_id").filtered(
            lambda card: card.state == "active" or card._is_referenced()
        )
        if locked_cards:
            raise UserError(_("Active or referenced price cards are locked and their rates cannot be deleted."))
        return super().unlink()

    @api.constrains(
        "product_tmpl_id",
        "price_card_id",
        "pricing_role",
        "metric",
        "tier_basis",
        "rate",
        "quantity_divisor",
        "use_time_window",
        "time_from",
        "time_to",
        "timezone",
        "is_tiered",
        "min_quantity",
        "max_quantity",
        "date_start",
        "date_end",
    )
    def _check_rate_rule(self):
        for rate in self:
            product = rate.product_tmpl_id
            if product.sale_business_line != "maas":
                raise ValidationError(_("Catalog rate rules can only be attached to MaaS products."))
            if product.maas_product_type == "model" and rate.metric == "api_usage":
                raise ValidationError(_("Model products need a Token metric, not an API usage metric."))
            if product.maas_product_type == "api" and rate.metric != "api_usage":
                raise ValidationError(_("API service products can only use the API service usage metric."))
            if rate.rate < 0 or rate.min_quantity < 0 or rate.max_quantity < 0:
                raise ValidationError(_("Rates and tier quantities cannot be negative."))
            if rate.quantity_divisor <= 0:
                raise ValidationError(_("Raw quantity per rate unit must be greater than zero."))
            if rate.use_time_window and not (0 <= rate.time_from < 24 and 0 <= rate.time_to <= 24):
                raise ValidationError(_("Time rates must use hours between 0 and 24."))
            if rate.use_time_window and rate.time_from == rate.time_to:
                raise ValidationError(_("A time-based rate must have a non-empty time window."))
            try:
                pytz.timezone(rate.timezone)
            except pytz.UnknownTimeZoneError as error:
                raise ValidationError(_("Enter a valid time zone.")) from error
            if rate.is_tiered and rate.max_quantity and rate.max_quantity <= rate.min_quantity:
                raise ValidationError(_("The tier maximum must be greater than its minimum."))
            if rate.date_start and rate.date_end and rate.date_end < rate.date_start:
                raise ValidationError(_("The effective end date cannot be before the start date."))
            if rate.price_card_id and rate.price_card_id.product_tmpl_id != product:
                raise ValidationError(_("A price line must belong to the same product as its price card."))
            if rate.pricing_role == "standard" and (
                rate.use_time_window or rate.is_tiered or rate.date_start or rate.date_end
            ):
                raise ValidationError(_("Standard prices cannot have time, tier, or date conditions. Add a conditional rule instead."))
            if rate.is_tiered and rate.pricing_role != "rule":
                raise ValidationError(_("Tiered prices must be configured as conditional rules."))
            if rate.tier_basis == "context_length" and not rate.is_tiered:
                raise ValidationError(_("Request context length must be configured as a tier rule."))
