# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


MAAS_PRODUCT_TYPES = [
    ("model", "Model Service (Tokens)"),
    ("api", "API Service"),
]


class MaasProductFamily(models.Model):
    _name = "maas.product.family"
    _description = "MaaS Product Family"
    _order = "sequence, name, id"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True)
    sequence = fields.Integer(default=10)
    product_type = fields.Selection(selection=MAAS_PRODUCT_TYPES, required=True, default="model")
    description = fields.Text(translate=True)
    active = fields.Boolean(default=True)

    _code_unique = models.Constraint("UNIQUE(code)", "The MaaS product family code must be unique.")

    @api.constrains("product_type")
    def _check_product_type(self):
        for family in self:
            mismatched_products = self.env["product.template"].search_count(
                [
                    ("maas_product_family_id", "=", family.id),
                    ("maas_product_type", "!=", family.product_type),
                ]
            )
            if mismatched_products:
                raise ValidationError(
                    _("The product type must match its MaaS product family.")
                )
