# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .maas_product_template import BUSINESS_LINES


class SaleOrder(models.Model):
    _inherit = "sale.order"

    business_line = fields.Selection(
        selection=BUSINESS_LINES,
        string="Sales Business Line",
        tracking=True,
        index=True,
        copy=True,
    )
    maas_product_family_id = fields.Many2one(
        comodel_name="maas.product.family",
        string="Product Family",
        index=True,
        ondelete="restrict",
        copy=True,
        domain="[('active', '=', True)]",
        help="Select Model Service (Tokens) or API Service to filter quotation products and apply the matching MaaS rate metrics.",
    )
    maas_product_type = fields.Selection(
        related="maas_product_family_id.product_type",
        string="MaaS Product Type",
    )
    maas_usage_only = fields.Boolean(compute="_compute_maas_usage_only")

    @api.depends(
        "business_line", "order_line.product_id", "order_line.product_id.maas_product_type",
        "order_line.display_type",
    )
    def _compute_maas_usage_only(self):
        for order in self:
            product_lines = order.order_line.filtered(lambda line: line.product_id and not line.display_type)
            order.maas_usage_only = bool(
                order.business_line == "maas"
                and product_lines
                and all(line.product_id.maas_product_type in ("model", "api") for line in product_lines)
            )

    @api.onchange("maas_product_family_id")
    def _onchange_maas_product_family(self):
        for order in self:
            if order.maas_product_family_id:
                order.business_line = "maas"

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            if values.get("maas_product_family_id"):
                values.setdefault("business_line", "maas")
        return super().create(vals_list)

    def write(self, vals):
        values = dict(vals)
        if values.get("maas_product_family_id"):
            values.setdefault("business_line", "maas")
        return super().write(values)

    def action_confirm(self):
        for order in self.filtered(lambda record: record.business_line == "maas"):
            order_date = fields.Date.to_date(order.date_order) or fields.Date.context_today(order)
            invalid_lines = order.order_line.filtered(
                lambda line: line.product_id
                and line.product_id.maas_product_type in ("model", "api")
                and (
                    not line.maas_price_card_id
                    or line.maas_price_card_id.state != "active"
                    or not line.maas_price_card_id.active
                    or line.maas_price_card_id.date_start and order_date < line.maas_price_card_id.date_start
                    or line.maas_price_card_id.date_end and order_date > line.maas_price_card_id.date_end
                )
            )
            if invalid_lines:
                products = ", ".join(invalid_lines.mapped("product_id.display_name"))
                raise UserError(
                    _(
                        "Select an active MaaS price card effective on the quotation date for each model or API product before confirming: %(products)s",
                        products=products,
                    )
                )
        return super().action_confirm()

    @api.constrains("business_line", "maas_product_family_id", "order_line")
    def _check_order_line_business_line(self):
        for order in self:
            if order.business_line == "maas" and not order.maas_product_family_id:
                raise ValidationError(_("Select a MaaS product family on the quotation."))
            if order.maas_product_family_id and order.business_line != "maas":
                raise ValidationError(_("A MaaS product family requires the TokensHub MaaS sales business line."))
            maas_lines = order.order_line.filtered(
                lambda line: line.product_id.sale_business_line == "maas"
            )
            if maas_lines and order.business_line != "maas":
                raise ValidationError(
                    _("Select TokensHub MaaS as the sales business line for MaaS products.")
                )
            if not order.business_line:
                continue
            mismatched_lines = order.order_line.filtered(
                lambda line: line.product_id
                and line.product_id.sale_business_line != order.business_line
            )
            if mismatched_lines:
                product_names = ", ".join(mismatched_lines.mapped("product_id.display_name"))
                raise ValidationError(
                    _(
                        "All products on this order must belong to %(line)s. "
                        "Mismatched products: %(products)s",
                        line=order.business_line,
                        products=product_names,
                    )
                )
            if order.maas_product_family_id:
                mismatched_family_lines = order.order_line.filtered(
                    lambda line: line.product_id
                    and line.product_id.maas_product_family_id != order.maas_product_family_id
                )
                if mismatched_family_lines:
                    product_names = ", ".join(mismatched_family_lines.mapped("product_id.display_name"))
                    raise ValidationError(
                        _(
                            "All products on this quotation must belong to %(family)s. Mismatched products: %(products)s",
                            family=order.maas_product_family_id.display_name,
                            products=product_names,
                        )
                    )
