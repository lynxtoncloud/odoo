# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class MaasProductPriceCard(models.Model):
    _name = "maas.product.price.card"
    _description = "MaaS Product Price Card"
    _order = "product_tmpl_id, date_start desc, id desc"
    _check_company_auto = True

    name = fields.Char(string="Version", required=True, default="New")
    product_tmpl_id = fields.Many2one(
        comodel_name="product.template",
        string="MaaS Product",
        required=True,
        ondelete="cascade",
        index=True,
        check_company=True,
    )
    company_id = fields.Many2one(related="product_tmpl_id.company_id", store=True, index=True)
    product_type = fields.Selection(related="product_tmpl_id.maas_product_type", store=True)
    currency_id = fields.Many2one(
        comodel_name="res.currency",
        string="Currency",
        required=True,
        default=lambda self: self.env.company.currency_id,
    )
    date_start = fields.Date(string="Effective From")
    date_end = fields.Date(string="Effective To")
    state = fields.Selection(
        selection=[("draft", "Draft"), ("active", "Active"), ("archived", "Archived")],
        default="draft",
        required=True,
        index=True,
    )
    active = fields.Boolean(default=True)
    cache_mode = fields.Selection(
        selection=[("single", "One Cache Rate"), ("read_write", "Read and Write Rates")],
        string="Cache Pricing",
        default="single",
        required=True,
    )
    tier_pricing_method = fields.Selection(
        selection=[("all_units", "One Rate for the Whole Tier"), ("graduated", "Progressive by Tier")],
        string="Usage Tier Method",
        default="all_units",
        required=True,
        help="All-units pricing applies the matched tier rate to the whole usage quantity. Progressive pricing calculates each tier segment separately.",
    )
    rate_ids = fields.One2many(
        comodel_name="maas.product.rate",
        inverse_name="price_card_id",
        string="Price Lines and Rules",
        copy=True,
    )
    catalog_document_ids = fields.One2many(
        comodel_name="maas.catalog.document",
        inverse_name="price_card_id",
        string="Manufacturer Price Lists",
        copy=True,
    )
    note = fields.Text(string="Internal Notes")

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            if values.get("product_tmpl_id") and not values.get("currency_id"):
                product = self.env["product.template"].browse(values["product_tmpl_id"])
                values["currency_id"] = product.currency_id.id
        return super().create(vals_list)

    @api.onchange("product_tmpl_id")
    def _onchange_product_tmpl_id(self):
        for card in self:
            if card.product_tmpl_id:
                card.currency_id = card.product_tmpl_id.currency_id

    @api.constrains("product_tmpl_id", "date_start", "date_end", "state", "active")
    def _check_product_and_dates(self):
        for card in self:
            if card.product_tmpl_id.sale_business_line != "maas":
                raise ValidationError(_("A MaaS price card must belong to a MaaS product."))
            if card.date_start and card.date_end and card.date_end < card.date_start:
                raise ValidationError(_("The effective end date cannot be before the start date."))
            if card.state != "active" or not card.active:
                continue
            other_cards = self.search(
                [
                    ("id", "!=", card.id),
                    ("product_tmpl_id", "=", card.product_tmpl_id.id),
                    ("state", "=", "active"),
                    ("active", "=", True),
                ]
            )
            for other in other_cards:
                starts_before_other_ends = not other.date_end or not card.date_start or card.date_start <= other.date_end
                other_starts_before_ends = not card.date_end or not other.date_start or other.date_start <= card.date_end
                if starts_before_other_ends and other_starts_before_ends:
                    raise ValidationError(
                        _("Active price-card effective dates cannot overlap for the same product.")
                    )
            card._validate_for_activation()

    def _required_standard_metrics(self):
        self.ensure_one()
        if self.product_tmpl_id.maas_product_type == "api":
            return {"api_usage"}
        if self.cache_mode == "read_write":
            return {"input", "output", "cache_read", "cache_write"}
        return {"input", "output", "cache"}

    def _validate_for_activation(self):
        for card in self:
            if card.product_tmpl_id.maas_product_type == "model" and not card.product_tmpl_id.maas_model_id:
                raise ValidationError(
                    _("Set the upstream Model ID on the product before activating its price card.")
                )
            standard_rates = card.rate_ids.filtered(
                lambda rate: rate.active and rate.pricing_role == "standard"
            )
            by_metric = {}
            for rate in standard_rates:
                if rate.metric in by_metric:
                    raise ValidationError(
                        _("A price card can have only one standard rate for each billing metric.")
                    )
                by_metric[rate.metric] = rate
            missing = card._required_standard_metrics() - set(by_metric)
            if missing:
                labels = dict(self.env["maas.product.rate"]._fields["metric"].selection)
                raise ValidationError(
                    _(
                        "Add standard price lines for: %(metrics)s.",
                        metrics=", ".join(labels.get(metric, metric) for metric in sorted(missing)),
                    )
                )
            if card.tier_pricing_method == "graduated":
                tiered_rules = card.rate_ids.filtered(
                    lambda rate: rate.active and rate.pricing_role == "rule" and rate.is_tiered
                )
                if any(rate.tier_basis == "context_length" for rate in tiered_rules):
                    raise ValidationError(
                        _("Context-length tiers select one rate for a request and cannot use progressive pricing.")
                    )
                tier_groups = {}
                for rate in tiered_rules:
                    group_key = (
                        rate.metric,
                        rate.tier_basis,
                        rate.use_time_window,
                        rate.time_from,
                        rate.time_to,
                        rate.timezone,
                        rate.date_start,
                        rate.date_end,
                    )
                    tier_groups.setdefault(group_key, self.env["maas.product.rate"])
                    tier_groups[group_key] |= rate
                for tier_group in tier_groups.values():
                    ordered_tiers = tier_group.sorted(lambda rate: rate.min_quantity)
                    divisors = set(ordered_tiers.mapped("quantity_divisor"))
                    if len(divisors) > 1:
                        raise ValidationError(_("All tiers in a progressive tier group must use the same quantity unit."))
                    if not ordered_tiers or ordered_tiers[0].min_quantity != 0:
                        raise ValidationError(_("Progressive tiers must start at zero."))
                    for current, following in zip(ordered_tiers, ordered_tiers[1:]):
                        if not current.max_quantity or current.max_quantity != following.min_quantity:
                            raise ValidationError(_("Progressive tier ranges must be continuous and non-overlapping."))
                    if ordered_tiers[-1].max_quantity:
                        raise ValidationError(_("The last progressive tier must be open-ended."))

    def action_activate(self):
        for card in self:
            card._validate_for_activation()
            card.write({"state": "active", "active": True})
        return True

    def action_archive(self):
        self.write({"state": "archived", "active": False})
        return True

    def _is_referenced(self):
        self.ensure_one()
        order_line_model = self.env["sale.order.line"].sudo()
        usage_model = self.env["maas.usage.event"].sudo()
        return bool(
            order_line_model.search_count([("maas_price_card_id", "=", self.id)])
            or usage_model.search_count([("price_card_id", "=", self.id)])
        )

    def write(self, vals):
        protected_fields = {
            "name", "product_tmpl_id", "currency_id", "date_start", "date_end",
            "tier_pricing_method", "cache_mode",
        }
        if protected_fields.intersection(vals):
            used_cards = self.filtered(lambda card: card._is_referenced())
            if used_cards:
                raise UserError(
                    _("A price card referenced by quotations or usage cannot be edited. Create a new version instead.")
                )
        return super().write(vals)
