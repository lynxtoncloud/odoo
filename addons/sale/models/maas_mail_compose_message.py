# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, models


class MailComposeMessage(models.TransientModel):
    _inherit = "mail.compose.message"

    @api.depends("composition_mode", "model", "res_domain", "res_ids", "template_id")
    def _compute_attachment_ids(self):
        super()._compute_attachment_ids()
        for composer in self.filtered(
            lambda record: record.model == "sale.order" and record.composition_mode == "comment"
        ):
            order_ids = composer._evaluate_res_ids() or []
            orders = self.env["sale.order"].browse(order_ids).exists()
            if len(orders) != 1 or orders.business_line != "maas":
                continue
            order_date = orders.date_order.date()
            order_lines = orders.order_line.filtered("product_id")
            cards = order_lines.mapped("maas_price_card_id")
            documents = cards.mapped("catalog_document_ids").filtered(
                lambda document: document.active
                and (not document.date_start or document.date_start <= order_date)
                and (not document.date_end or document.date_end >= order_date)
            )
            attachments = self.env["ir.attachment"].search(
                [
                    ("res_model", "=", "maas.catalog.document"),
                    ("res_id", "in", documents.ids),
                    ("res_field", "=", "file_content"),
                ]
            )
            composer.attachment_ids |= attachments
