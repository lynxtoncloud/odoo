# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    maas_price_card_id = fields.Many2one(
        comodel_name="maas.product.price.card",
        string="Price Card Version",
        check_company=True,
        ondelete="restrict",
        copy=True,
        domain="[('product_tmpl_id', '=', product_template_id), ('state', '=', 'active'), ('active', '=', True)]",
    )
    maas_model_id = fields.Char(related="product_template_id.maas_model_id", string="Model ID", readonly=True)
    maas_cache_mode = fields.Selection(related="maas_price_card_id.cache_mode", string="Cache Pricing Mode")
    maas_input_rate = fields.Monetary(
        string="Input List Price", currency_field="currency_id", compute="_compute_maas_card_rates"
    )
    maas_output_rate = fields.Monetary(
        string="Output List Price", currency_field="currency_id", compute="_compute_maas_card_rates"
    )
    maas_cache_rate = fields.Monetary(
        string="Cache List Price", currency_field="currency_id", compute="_compute_maas_card_rates"
    )
    maas_cache_read_rate = fields.Monetary(
        string="Cache Read List Price", currency_field="currency_id", compute="_compute_maas_card_rates"
    )
    maas_cache_write_rate = fields.Monetary(
        string="Cache Write List Price", currency_field="currency_id", compute="_compute_maas_card_rates"
    )
    maas_api_rate = fields.Monetary(
        string="API List Price", currency_field="currency_id", compute="_compute_maas_card_rates"
    )
    maas_api_unit_name = fields.Char(string="API Rate Unit", compute="_compute_maas_card_rates")

    @api.depends(
        "maas_price_card_id",
        "maas_price_card_id.currency_id",
        "maas_price_card_id.rate_ids.rate",
        "maas_price_card_id.rate_ids.metric",
        "maas_price_card_id.rate_ids.pricing_role",
        "maas_price_card_id.rate_ids.active",
        "maas_price_card_id.rate_ids.unit_name",
        "currency_id",
        "order_id.date_order",
    )
    def _compute_maas_card_rates(self):
        for line in self:
            values = {
                "maas_input_rate": 0.0,
                "maas_output_rate": 0.0,
                "maas_cache_rate": 0.0,
                "maas_cache_read_rate": 0.0,
                "maas_cache_write_rate": 0.0,
                "maas_api_rate": 0.0,
                "maas_api_unit_name": False,
            }
            card = line.maas_price_card_id
            if card:
                for rate in card.rate_ids.filtered(
                    lambda item: item.active and item.pricing_role == "standard"
                ):
                    field_name = {
                        "input": "maas_input_rate",
                        "output": "maas_output_rate",
                        "cache": "maas_cache_rate",
                        "cache_read": "maas_cache_read_rate",
                        "cache_write": "maas_cache_write_rate",
                        "api_usage": "maas_api_rate",
                    }.get(rate.metric)
                    if field_name:
                        values[field_name] = rate.currency_id._convert(
                            rate.rate,
                            line.currency_id,
                            line.company_id,
                            line.order_id.date_order or fields.Date.context_today(line),
                            round=False,
                        )
                    if rate.metric == "api_usage":
                        values["maas_api_unit_name"] = rate.unit_name
            for field_name, value in values.items():
                line[field_name] = value

    @api.depends(
        "product_id", "product_uom_id", "product_uom_qty", "order_id.business_line",
        "maas_price_card_id", "maas_api_rate",
    )
    def _compute_price_unit(self):
        super()._compute_price_unit()
        for line in self.filtered(
            lambda item: item.order_id.business_line == "maas" and item.product_id
        ):
            template = line.product_id.product_tmpl_id
            if template.maas_product_type in ("model", "api"):
                # Both model tokens and API calls are billed from recorded usage.
                # Keep the quotation line itself at zero to prevent duplicate fixed charges.
                line.price_unit = 0.0

    @api.depends(
        "product_id",
        "product_uom_id",
        "product_uom_qty",
        "order_id.pricelist_id",
        "order_id.business_line",
        "maas_price_card_id",
    )
    def _compute_pricelist_item_id(self):
        super()._compute_pricelist_item_id()
        for line in self.filtered(lambda item: item.order_id.business_line == "maas"):
            line.pricelist_item_id = False

    @api.onchange("product_id")
    def _onchange_maas_product(self):
        for line in self:
            line.maas_price_card_id = False
            if line.order_id.business_line != "maas" or not line.product_template_id:
                continue
            order_date = fields.Date.to_date(line.order_id.date_order) or fields.Date.context_today(line)
            line.maas_price_card_id = line.product_template_id._get_maas_price_card(
                order_date
            )
            if line.product_template_id.maas_product_type in ("model", "api"):
                line.product_uom_qty = 1.0
                line.price_unit = 0.0

    @api.constrains("product_id", "maas_price_card_id", "order_id", "discount")
    def _check_maas_pricing(self):
        for line in self.filtered(lambda item: item.order_id.business_line == "maas" and item.product_id):
            template = line.product_id.product_tmpl_id
            if line.maas_price_card_id:
                if line.maas_price_card_id.product_tmpl_id != template:
                    raise ValidationError(_("The selected price card must belong to the product on this line."))
                if line.maas_price_card_id.state != "active" or not line.maas_price_card_id.active:
                    raise ValidationError(_("Select an active price card version."))
                card_date = fields.Date.to_date(line.order_id.date_order) or fields.Date.context_today(line)
                if (
                    line.maas_price_card_id.date_start and card_date < line.maas_price_card_id.date_start
                ) or (line.maas_price_card_id.date_end and card_date > line.maas_price_card_id.date_end):
                    raise ValidationError(_("The selected price card is not effective on the quotation date."))
            elif template.maas_product_type in ("model", "api") and template._get_maas_price_card(
                fields.Date.to_date(line.order_id.date_order) or fields.Date.context_today(line)
            ):
                raise ValidationError(_("Select the active price card version for this MaaS product."))
            if line.discount < 0 or line.discount > 100:
                raise ValidationError(_("The discount must be between 0 and 100 percent."))

        for line in self.filtered(lambda item: item.order_id.maas_product_family_id and item.product_id):
            if line.product_id.maas_product_family_id != line.order_id.maas_product_family_id:
                raise ValidationError(_("The product must belong to the product family selected on the quotation."))

    def _get_display_price_ignore_combo(self):
        self.ensure_one()
        if self.order_id.business_line == "maas" and self.product_id:
            if self.product_id.maas_product_type in ("model", "api"):
                return 0.0
        return super()._get_display_price_ignore_combo()
