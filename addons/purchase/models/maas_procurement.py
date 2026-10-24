# Part of Odoo. See LICENSE file for full copyright and licensing details.

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


PURCHASE_MAAS_METRICS = [
    ("input", "Input Tokens"),
    ("output", "Output Tokens"),
    ("cache", "Cache Tokens"),
    ("cache_read", "Cache Read Tokens"),
    ("cache_write", "Cache Write Tokens"),
    ("api_usage", "API Service Usage"),
]


class PurchaseMaasChannel(models.Model):
    _name = "purchase.maas.channel"
    _description = "MaaS Supplier Sub-channel"
    _order = "partner_id, name, id"

    name = fields.Char(string="Sub-channel", required=True)
    partner_id = fields.Many2one(
        "res.partner", string="Supplier", required=True, ondelete="restrict", index=True,
        domain="[('supplier_rank', '>', 0)]",
    )
    channel_id = fields.Char(string="Channel ID", required=True, index=True)
    note = fields.Text(string="Internal Notes")
    active = fields.Boolean(default=True)
    offer_ids = fields.One2many("purchase.maas.offer", "channel_id", string="Model Offers")

    _supplier_channel_id_uniq = models.Constraint(
        "unique(partner_id, channel_id)", "Channel IDs must be unique per supplier."
    )

    @api.constrains("partner_id")
    def _check_supplier(self):
        for channel in self:
            if channel.partner_id and channel.partner_id.supplier_rank <= 0:
                raise ValidationError(_("A sub-channel must belong to a supplier."))


class ResPartner(models.Model):
    _inherit = "res.partner"

    maas_channel_ids = fields.One2many("purchase.maas.channel", "partner_id", string="MaaS Sub-channels")


class ProductTemplate(models.Model):
    _inherit = "product.template"

    maas_procurement_offer_ids = fields.One2many(
        "purchase.maas.offer", "product_tmpl_id", string="MaaS Supplier Offers",
    )


class PurchaseMaasOffer(models.Model):
    _name = "purchase.maas.offer"
    _description = "MaaS Supplier Model Offer"
    _order = "partner_id, product_tmpl_id, provider_model_name, date_start desc, id desc"
    _check_company_auto = True

    name = fields.Char(compute="_compute_name", store=True)
    channel_id = fields.Many2one(
        "purchase.maas.channel", string="Sub-channel", required=True, ondelete="restrict", index=True,
    )
    partner_id = fields.Many2one(related="channel_id.partner_id", store=True, string="Supplier", index=True)
    channel_code = fields.Char(related="channel_id.channel_id", store=True, string="Channel ID")
    company_id = fields.Many2one(
        "res.company", string="Company", required=True, default=lambda self: self.env.company, index=True,
    )
    product_tmpl_id = fields.Many2one(
        "product.template", string="MaaS Product", required=True, ondelete="restrict", index=True,
        domain="[('type', '=', 'service'), ('sale_ok', '=', True)]", check_company=True,
    )
    provider_model_name = fields.Char(string="Upstream Model / API Name", required=True, index=True)
    upstream_channel = fields.Char(string="Upstream Channel")
    billing_mode = fields.Selection(
        [("tokens", "Tokens"), ("per_call", "Per Call")], string="Billing Mode", required=True,
        default="tokens",
    )
    cache_mode = fields.Selection(
        [("single", "One Cache Rate"), ("read_write", "Read and Write Rates")],
        string="Cache Pricing", default="single", required=True,
    )
    tier_pricing_method = fields.Selection(
        [("all_units", "One Rate for the Whole Tier"), ("graduated", "Progressive by Tier")],
        string="Usage Tier Method", default="all_units", required=True,
    )
    currency_id = fields.Many2one(
        "res.currency", string="Currency", required=True,
        default=lambda self: self.env.company.currency_id,
    )
    supplier_discount = fields.Float(
        string="Supplier Discount (%)", default=0.0, digits=(16, 2),
        help=(
            "Discount applied by the supplier to its published rates. "
            "Customer quotation discounts are configured in Sales."
        ),
    )
    date_start = fields.Date(string="Effective From")
    date_end = fields.Date(string="Effective To")
    state = fields.Selection(
        [("draft", "Draft"), ("active", "Active"), ("archived", "Archived")],
        default="draft", required=True, index=True,
    )
    active = fields.Boolean(default=True)
    note = fields.Text(string="Internal Notes")
    rate_ids = fields.One2many("purchase.maas.rate", "offer_id", string="Purchase Rates and Rules", copy=True)
    input_price = fields.Monetary(
        currency_field="currency_id", compute="_compute_rate_summary", string="Input Pricing"
    )
    output_price = fields.Monetary(
        currency_field="currency_id", compute="_compute_rate_summary", string="Output Pricing"
    )
    cache_price_summary = fields.Char(compute="_compute_rate_summary", string="Cache Rate Summary")
    per_call_price = fields.Monetary(
        currency_field="currency_id", compute="_compute_rate_summary", string="Per-call Price"
    )

    @api.depends("partner_id.name", "channel_id.name", "product_tmpl_id.name", "provider_model_name")
    def _compute_name(self):
        for offer in self:
            offer.name = " / ".join(filter(None, [
                offer.partner_id.name, offer.channel_id.name, offer.product_tmpl_id.name, offer.provider_model_name,
            ]))

    @api.depends("cache_mode", "rate_ids.metric", "rate_ids.rate", "rate_ids.pricing_role", "rate_ids.active")
    def _compute_rate_summary(self):
        for offer in self:
            rates = {
                rate.metric: rate
                for rate in offer.rate_ids.filtered(lambda line: line.active and line.pricing_role == "standard")
            }
            offer.input_price = rates.get("input").rate if rates.get("input") else 0.0
            offer.output_price = rates.get("output").rate if rates.get("output") else 0.0
            offer.per_call_price = rates.get("api_usage").rate if rates.get("api_usage") else 0.0
            if offer.cache_mode == "read_write":
                read_rate, write_rate = rates.get("cache_read"), rates.get("cache_write")
                offer.cache_price_summary = _(
                    "Read %(read)s · Write %(write)s / 1M",
                    read=offer.currency_id.format(read_rate.rate) if read_rate else "—",
                    write=offer.currency_id.format(write_rate.rate) if write_rate else "—",
                )
            else:
                cache_rate = rates.get("cache")
                offer.cache_price_summary = (
                    _("%(price)s / 1M", price=offer.currency_id.format(cache_rate.rate)) if cache_rate else "—"
                )

    @api.onchange("product_tmpl_id")
    def _onchange_product_tmpl_id(self):
        for offer in self:
            if offer.product_tmpl_id and not offer.provider_model_name:
                offer.provider_model_name = offer.product_tmpl_id.name

    def _required_standard_metrics(self):
        self.ensure_one()
        if self.billing_mode == "per_call":
            return {"api_usage"}
        if self.cache_mode == "read_write":
            return {"input", "output", "cache_read", "cache_write"}
        return {"input", "output", "cache"}

    def action_activate(self):
        for offer in self:
            if not offer.channel_id.active or not offer.partner_id.active:
                raise ValidationError(_("Activate the supplier and sub-channel before activating this offer."))
            standard_rates = offer.rate_ids.filtered(lambda line: line.active and line.pricing_role == "standard")
            by_metric = {}
            for rate in standard_rates:
                if rate.metric in by_metric:
                    raise ValidationError(_("An offer can have only one standard rate for each billing metric."))
                by_metric[rate.metric] = rate
            missing = offer._required_standard_metrics() - set(by_metric)
            if missing:
                labels = dict(self.env["purchase.maas.rate"]._fields["metric"].selection)
                raise ValidationError(
                    _(
                        "Add standard rates for: %(metrics)s.",
                        metrics=", ".join(labels[metric] for metric in sorted(missing)),
                    )
                )
            if offer.date_start and offer.date_end and offer.date_end < offer.date_start:
                raise ValidationError(_("The effective end date cannot be before the start date."))
            offer.write({"state": "active", "active": True})
        return True

    def action_archive(self):
        self.write({"state": "archived", "active": False})
        return True

    def write(self, vals):
        protected_fields = {
            "channel_id", "product_tmpl_id", "provider_model_name", "upstream_channel", "billing_mode",
            "cache_mode", "tier_pricing_method", "currency_id", "supplier_discount", "date_start", "date_end",
        }
        if protected_fields.intersection(vals) and self.filtered(lambda offer: offer.state != "draft"):
            raise ValidationError(
                _("Supplier pricing that has left Draft is locked. Create a new draft version to change it.")
            )
        return super().write(vals)

    @api.constrains(
        "channel_id", "product_tmpl_id", "provider_model_name", "upstream_channel", "billing_mode",
        "supplier_discount", "date_start", "date_end", "state", "active",
    )
    def _check_offer(self):
        for offer in self:
            product = offer.product_tmpl_id
            if "sale_business_line" in product._fields and product.sale_business_line != "maas":
                raise ValidationError(_("Supplier offers can only be linked to MaaS sales products."))
            if "maas_product_type" in product._fields:
                expected_billing_mode = "tokens" if product.maas_product_type == "model" else "per_call"
                if offer.billing_mode != expected_billing_mode:
                    raise ValidationError(_("The offer billing mode must match the MaaS product type."))
            if not 0 <= offer.supplier_discount <= 100:
                raise ValidationError(_("Supplier discounts must be between 0 and 100 percent."))
            if offer.date_start and offer.date_end and offer.date_end < offer.date_start:
                raise ValidationError(_("The effective end date cannot be before the start date."))
            if offer.state != "active" or not offer.active:
                continue
            other_offers = self.search([
                ("id", "!=", offer.id),
                ("channel_id", "=", offer.channel_id.id),
                ("product_tmpl_id", "=", offer.product_tmpl_id.id),
                ("provider_model_name", "=", offer.provider_model_name),
                ("upstream_channel", "=", offer.upstream_channel or False),
                ("state", "=", "active"),
                ("active", "=", True),
            ])
            for other in other_offers:
                starts_before_other_ends = (
                    not other.date_end or not offer.date_start or offer.date_start <= other.date_end
                )
                other_starts_before_ends = (
                    not offer.date_end or not other.date_start or other.date_start <= offer.date_end
                )
                if starts_before_other_ends and other_starts_before_ends:
                    raise ValidationError(
                        _("Active offer dates cannot overlap for the same supplier route and model.")
                    )


class PurchaseMaasRate(models.Model):
    _name = "purchase.maas.rate"
    _description = "MaaS Supplier Purchase Rate"
    _order = "sequence, metric, min_quantity, id"

    name = fields.Char(compute="_compute_name", store=True)
    sequence = fields.Integer(default=10)
    offer_id = fields.Many2one("purchase.maas.offer", required=True, ondelete="cascade", index=True)
    pricing_role = fields.Selection(
        [("standard", "Standard Price"), ("rule", "Conditional Rule")],
        string="Rate Type", required=True, default="standard", index=True,
    )
    metric = fields.Selection(PURCHASE_MAAS_METRICS, required=True, index=True)
    unit_name = fields.Char(string="Rate Unit", required=True, default="Per million Tokens")
    quantity_divisor = fields.Float(string="Raw Quantity per Rate Unit", required=True, default=1_000_000.0)
    currency_id = fields.Many2one(related="offer_id.currency_id", store=True, readonly=True)
    company_id = fields.Many2one(related="offer_id.company_id", store=True, index=True)
    rate = fields.Monetary(string="Supplier List Rate", currency_field="currency_id", required=True, default=0.0)
    use_time_window = fields.Boolean(string="Time-Based Rate")
    time_from = fields.Float(string="From")
    time_to = fields.Float(string="To")
    timezone = fields.Char(string="Time Zone", default="UTC", required=True)
    is_tiered = fields.Boolean(string="Tiered Rate")
    tier_basis = fields.Selection(
        [("usage_line", "Usage Line Quantity"), ("billing_period", "Billing Period Quantity"), ("context_length", "Request Context Length")],
        string="Tier Basis", default="usage_line", required=True,
    )
    min_quantity = fields.Float(string="Minimum Quantity", default=0.0)
    max_quantity = fields.Float(string="Maximum Quantity")
    date_start = fields.Date(string="Effective From")
    date_end = fields.Date(string="Effective To")
    active = fields.Boolean(default=True)

    @api.model_create_multi
    def create(self, vals_list):
        offers = self.env["purchase.maas.offer"].browse([vals.get("offer_id") for vals in vals_list if vals.get("offer_id")])
        if offers.filtered(lambda offer: offer.state != "draft"):
            raise ValidationError(_("Rates on active or archived supplier offers are locked. Create a new draft version to change them."))
        for values in vals_list:
            if values.get("metric") == "api_usage":
                values.setdefault("unit_name", "API usage unit")
                values.setdefault("quantity_divisor", 1.0)
        return super().create(vals_list)

    def write(self, vals):
        if self.mapped("offer_id").filtered(lambda offer: offer.state != "draft"):
            raise ValidationError(_("Rates on active or archived supplier offers are locked. Create a new draft version to change them."))
        return super().write(vals)

    def unlink(self):
        if self.mapped("offer_id").filtered(lambda offer: offer.state != "draft"):
            raise ValidationError(_("Rates on active or archived supplier offers are locked. Create a new draft version to change them."))
        return super().unlink()

    @api.depends("metric", "pricing_role", "unit_name", "use_time_window", "time_from", "time_to", "timezone", "is_tiered", "min_quantity", "max_quantity", "date_start", "date_end")
    def _compute_name(self):
        labels = dict(self.fields_get(["metric"])["metric"]["selection"])
        for rate in self:
            parts = [labels.get(rate.metric, ""), rate.unit_name or ""]
            if rate.pricing_role == "rule":
                parts.insert(0, _("Rule"))
            if rate.is_tiered:
                parts.append(_("Tier %(minimum)s–%(maximum)s", minimum=rate.min_quantity, maximum=rate.max_quantity or _("and above")))
            if rate.use_time_window:
                parts.append(_("%(start)s–%(end)s %(timezone)s", start=rate.time_from, end=rate.time_to, timezone=rate.timezone))
            if rate.date_start or rate.date_end:
                parts.append(_("%(start)s to %(end)s", start=rate.date_start or _("Any date"), end=rate.date_end or _("Open ended")))
            rate.name = " — ".join(filter(None, parts))

    @api.onchange("metric")
    def _onchange_metric(self):
        for rate in self:
            if rate.metric == "api_usage":
                rate.unit_name = "API usage unit"
                rate.quantity_divisor = 1.0
            else:
                rate.unit_name = "Per million Tokens"
                rate.quantity_divisor = 1_000_000.0

    @api.constrains("offer_id", "pricing_role", "metric", "quantity_divisor", "rate", "use_time_window", "time_from", "time_to", "timezone", "is_tiered", "min_quantity", "max_quantity", "date_start", "date_end")
    def _check_rate(self):
        for rate in self:
            offer = rate.offer_id
            if (offer.billing_mode == "tokens") == (rate.metric == "api_usage"):
                raise ValidationError(_("The rate metric must match the offer billing mode."))
            if rate.rate < 0 or rate.min_quantity < 0 or rate.max_quantity < 0 or rate.quantity_divisor <= 0:
                raise ValidationError(_("Rates and tier quantities must be non-negative and the unit divisor must be positive."))
            if rate.use_time_window and not (0 <= rate.time_from < 24 and 0 <= rate.time_to <= 24 and rate.time_from != rate.time_to):
                raise ValidationError(_("Time-based rates must use a non-empty time window between 0 and 24 hours."))
            try:
                pytz.timezone(rate.timezone)
            except pytz.UnknownTimeZoneError as error:
                raise ValidationError(_("Enter a valid time zone.")) from error
            if rate.is_tiered and rate.max_quantity and rate.max_quantity <= rate.min_quantity:
                raise ValidationError(_("The tier maximum must be greater than its minimum."))
            if rate.date_start and rate.date_end and rate.date_end < rate.date_start:
                raise ValidationError(_("The effective end date cannot be before the start date."))
            if rate.pricing_role == "standard" and (rate.use_time_window or rate.is_tiered or rate.date_start or rate.date_end):
                raise ValidationError(_("Standard rates cannot have time, tier, or date conditions."))
            if rate.is_tiered and rate.pricing_role != "rule":
                raise ValidationError(_("Tiered rates must be configured as conditional rules."))
