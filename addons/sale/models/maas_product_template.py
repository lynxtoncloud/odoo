# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .maas_product_family import MAAS_PRODUCT_TYPES


BUSINESS_LINES = [
    ("maas", "TokensHub MaaS"),
]

class ProductTemplate(models.Model):
    _inherit = "product.template"

    sale_business_line = fields.Selection(
        selection=BUSINESS_LINES,
        string="Sales Business Line",
        index=True,
        tracking=True,
        help="Used by Sales to keep product selection within the quotation's business line.",
    )
    maas_product_family_id = fields.Many2one(
        comodel_name="maas.product.family",
        string="Product Family",
        index=True,
        ondelete="restrict",
        tracking=True,
        help="Select the MaaS service family. It determines which products and usage-rate rules are available on quotations.",
    )
    maas_product_type = fields.Selection(
        selection=MAAS_PRODUCT_TYPES,
        string="MaaS Product Type",
        readonly=True,
        tracking=True,
        help="Derived from the selected product family: model families use Token metrics and API families use API usage.",
    )
    maas_provider = fields.Char(string="Provider / Manufacturer", tracking=True)
    maas_model_id = fields.Char(
        string="Upstream Model ID",
        tracking=True,
        help="The exact model identifier used by the provider API, such as deepseek-v4-pro.",
    )
    maas_model_version = fields.Char(string="Model Version", tracking=True)
    maas_price_card_ids = fields.One2many(
        comodel_name="maas.product.price.card",
        inverse_name="product_tmpl_id",
        string="Price Card Versions",
        copy=True,
    )

    @api.onchange("maas_product_family_id")
    def _onchange_maas_product_family(self):
        for product in self:
            if product.maas_product_family_id:
                product.sale_business_line = "maas"
                product.maas_product_type = product.maas_product_family_id.product_type
                # MaaS products are non-stock services in Odoo. Keep this
                # separate from maas_product_type, which distinguishes model
                # and API service families.
                product.type = "service"
            else:
                product.sale_business_line = False
                product.maas_product_type = False
                product.maas_provider = False
                product.maas_model_id = False
                product.maas_model_version = False
            if product.maas_product_type != "model":
                product.maas_model_id = False
                product.maas_model_version = False

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            if values.get("maas_product_family_id") or values.get("sale_business_line") == "maas":
                values.setdefault("type", "service")
            if values.get("maas_product_family_id"):
                family = self.env["maas.product.family"].browse(values["maas_product_family_id"])
                values.setdefault("sale_business_line", "maas")
                values.setdefault("maas_product_type", family.product_type)
                if family.product_type != "model":
                    values.setdefault("maas_model_id", False)
                    values.setdefault("maas_model_version", False)
        return super().create(vals_list)

    def write(self, vals):
        values = dict(vals)
        if "maas_product_family_id" in values:
            target_family = self.env["maas.product.family"].browse(values["maas_product_family_id"])
            products_with_cards = self.filtered(
                lambda product: product.maas_product_family_id != target_family
                and product.maas_price_card_ids
            )
            if products_with_cards:
                raise UserError(
                    _("A product family cannot be changed after price cards are created. Create a separate product for a different service type.")
                )
        if "maas_model_id" in values:
            referenced_products = self.filtered(
                lambda product: product.maas_model_id != values["maas_model_id"]
                and any(card._is_referenced() for card in product.maas_price_card_ids)
            )
            if referenced_products:
                raise UserError(
                    _("A model ID used by quotations or usage cannot be changed. Create a separate product for a different upstream model ID.")
                )
        if values.get("maas_product_family_id") or values.get("sale_business_line") == "maas":
            values.setdefault("type", "service")
        if "maas_product_family_id" in values and not values["maas_product_family_id"]:
            values.setdefault("sale_business_line", False)
            values.setdefault("maas_product_type", False)
            values.setdefault("maas_provider", False)
            values.setdefault("maas_model_id", False)
            values.setdefault("maas_model_version", False)
        if values.get("maas_product_family_id"):
            family = self.env["maas.product.family"].browse(values["maas_product_family_id"])
            values.setdefault("sale_business_line", "maas")
            values.setdefault("maas_product_type", family.product_type)
            if family.product_type != "model":
                values.setdefault("maas_model_id", False)
                values.setdefault("maas_model_version", False)
        return super().write(values)

    @api.constrains(
        "sale_business_line",
        "type",
        "maas_product_family_id",
        "maas_product_type",
        "maas_price_card_ids",
        "maas_provider",
        "maas_model_id",
        "maas_model_version",
    )
    def _check_maas_product_definition(self):
        for product in self:
            if product.maas_product_family_id and product.sale_business_line != "maas":
                raise ValidationError(_("A MaaS product family can only be assigned to a MaaS product."))
            if product.sale_business_line == "maas" and not product.maas_product_family_id:
                raise ValidationError(_("Select a product family for every MaaS product."))
            if product.sale_business_line == "maas" and product.type != "service":
                raise ValidationError(_("MaaS products must use Odoo's Service product type."))
            if (
                product.maas_product_family_id
                and product.maas_product_type != product.maas_product_family_id.product_type
            ):
                raise ValidationError(_("The product type must match its MaaS product family."))
            if product.sale_business_line == "maas" and not product.maas_product_type:
                raise ValidationError(_("Select whether this MaaS product is a model or an API service."))
            if product.sale_business_line != "maas" and (
                product.maas_product_type
                or product.maas_price_card_ids
                or product.maas_provider
                or product.maas_model_id
                or product.maas_model_version
            ):
                raise ValidationError(
                    _("MaaS product type, rates, and manufacturer files can only be set on MaaS products.")
                )
            if product.maas_product_type != "model" and (product.maas_model_id or product.maas_model_version):
                raise ValidationError(_("Model IDs and versions can only be set on a model product."))

    def _get_maas_price_card(self, on_date=None):
        self.ensure_one()
        on_date = on_date or fields.Date.context_today(self)
        return self.env["maas.product.price.card"].search(
            [
                ("product_tmpl_id", "=", self.id),
                ("state", "=", "active"),
                ("active", "=", True),
                "|",
                ("date_start", "=", False),
                ("date_start", "<=", on_date),
                "|",
                ("date_end", "=", False),
                ("date_end", ">=", on_date),
            ],
            order="date_start desc, id desc",
            limit=1,
        )
