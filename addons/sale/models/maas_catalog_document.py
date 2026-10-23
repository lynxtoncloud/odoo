# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class MaasCatalogDocument(models.Model):
    _name = "maas.catalog.document"
    _description = "MaaS Manufacturer Catalog Attachment"
    _order = "sequence, date_start desc, name, id"
    _check_company_auto = True

    sequence = fields.Integer(default=10)
    name = fields.Char(string="Document Name", required=True)
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
        string="Price Card Version",
        required=True,
        ondelete="cascade",
        index=True,
        check_company=True,
    )
    company_id = fields.Many2one(related="product_tmpl_id.company_id", store=True, index=True)
    vendor_name = fields.Char(string="Manufacturer")
    filename = fields.Char(string="File Name", required=True)
    file_content = fields.Binary(string="Manufacturer Catalog File", required=True, attachment=True)
    date_start = fields.Date(string="Effective From")
    date_end = fields.Date(string="Effective To")
    active = fields.Boolean(default=True)

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            if values.get("price_card_id") and not values.get("product_tmpl_id"):
                card = self.env["maas.product.price.card"].browse(values["price_card_id"])
                values["product_tmpl_id"] = card.product_tmpl_id.id
            card = self.env["maas.product.price.card"].browse(values.get("price_card_id"))
            if card and card._is_referenced():
                raise UserError(_("A price card referenced by quotations or usage cannot be changed. Create a new version instead."))
        return super().create(vals_list)

    def write(self, vals):
        referenced_cards = self.mapped("price_card_id").filtered(lambda card: card._is_referenced())
        if referenced_cards:
            raise UserError(_("A price card referenced by quotations or usage cannot be changed. Create a new version instead."))
        return super().write(vals)

    def unlink(self):
        referenced_cards = self.mapped("price_card_id").filtered(lambda card: card._is_referenced())
        if referenced_cards:
            raise UserError(_("A price card referenced by quotations or usage cannot be changed. Create a new version instead."))
        return super().unlink()

    @api.onchange("price_card_id")
    def _onchange_price_card_id(self):
        for document in self:
            if document.price_card_id:
                document.product_tmpl_id = document.price_card_id.product_tmpl_id

    @api.constrains("product_tmpl_id", "price_card_id")
    def _check_product_business_line(self):
        for document in self:
            if document.product_tmpl_id.sale_business_line != "maas":
                raise ValidationError(_("Manufacturer catalog attachments can only be stored on MaaS products."))
            if document.price_card_id.product_tmpl_id != document.product_tmpl_id:
                raise ValidationError(_("A manufacturer price list must belong to the selected product card."))

    @api.constrains("date_start", "date_end")
    def _check_effective_dates(self):
        for document in self:
            if document.date_start and document.date_end and document.date_end < document.date_start:
                raise ValidationError(_("The effective end date cannot be before the start date."))
