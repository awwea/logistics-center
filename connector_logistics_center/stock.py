# coding: utf-8
# © 2015 David BEAL @ Akretion
# © 2015 Sebastien BEAU @ Akretion
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl.html).

import time
import logging

from openerp.osv import orm, fields
from openerp.tools.translate import _


_logger = logging.getLogger(__name__)


LOGISTIC_FIELDS = [
    'log_out_file_doc_id',
    'log_in_file_doc_id',
    'logistic_center',
]


class StockMove(orm.Model):
    _inherit = 'stock.move'

    def _select_location_dest_id(self, cr, uid, context=None):
        print context
        location_dest_id = False
        logistic_center = context.get('logistic_center', False)
        if context['picking_type'] == 'in' \
                and logistic_center and logistic_center != 'internal':
            backend = self.pool['logistic.backend'].browse(
                cr, uid, int(logistic_center), context=context)
            location_dest_id = backend.warehouse_id.lot_stock_id.id
        return location_dest_id

    _defaults = {
        'location_dest_id': _select_location_dest_id
    }

    # TODO : do it works : context don't pass
    # def onchange_product_id(self, cr, uid, ids, prod_id=False, loc_id=False,
    #                        loc_dest_id=False, partner_id=False, context=None):
    #    values = super(StockMove, self).onchange_product_id(
    #        cr, uid, ids, prod_id=prod_id, loc_id=loc_id,
    #        loc_dest_id=loc_dest_id, partner_id=partner_id)
    #    location_dest_id = self._select_location_dest_id(cr, uid, context)
    #    if location_dest_id:
    #        values.update({'location_dest_id': location_dest_id})
    #    return values


class AbstractStockPicking(orm.AbstractModel):
    _inherit = 'abstract.logistic.flow'
    _name = 'abstract.stock.picking'

    def copy(self, cr, uid, id, default=None, context=None):
        if default is None:
            default = {}
        for field in ['log_out_file_doc_id', 'log_in_file_doc_id']:
            default[field] = False
        default['logistic_center'] = self.browse(cr, uid, id).logistic_center
        return super(AbstractStockPicking, self).copy(cr, uid, id,
                                                      default, context=context)

    _columns = {
        'log_out_file_doc_id': fields.many2one(
            'file.document',
            'Logistics Doc. Out',
            readonly=True,
            help="Refers the 'File document' object which "
                 "contains informations to send to "
                 "Logistics center for synchronisation purpose."),
        'log_in_file_doc_id': fields.many2one(
            'file.document',
            'Logistics Doc. In',
            readonly=True,
            help="Refers the 'File document' object which "
                 "contains informations sent by "
                 "Logistics center in response from original message."),
        # Implement your method in your module to check if required
        'logistics_exception': fields.boolean(
            'Logistics Exception',
            help="Checked if a wrong data prevent you to send "
                 "the order to your logistics center"),
        'logistics_blocked': fields.boolean(
            'Logistics Blocked',
            help="Logistics Delivery Orders can be blocked manually"),
    }

    def run_job(self, cr, uid, picking_id, buffer_id, file_doc_id, moves,
                picking_vals=None, context=None):
        # picking_id is extracted
        picking_id = int(picking_id)
        qty_by_prod = {}
        for move in moves:
            product_id = move["product_id"]
            qty = abs(float(move["product_qty"]))
            if qty_by_prod.get(product_id):
                qty_by_prod[product_id] += qty
            else:
                qty_by_prod[product_id] = qty
        self.validate_from_data(
            cr, uid, picking_id, qty_by_prod, context=context)
        if not picking_vals:
            picking_vals = {}
        picking_vals['log_in_file_doc_id'] = file_doc_id
        picking = self.browse(cr, uid, picking_id, context=context)
        linked_picking = picking
        if picking.log_out_file_doc_id:
            picking_vals['log_out_file_doc_id'] = (
                picking.log_out_file_doc_id.id)
        if picking.state != 'done' and picking.backorder_id:
            # we put file doc in backorder if picking validation is partial
            linked_picking = picking.backorder_id
        linked_picking.write(picking_vals)
        return True

    def validate_from_data(
            self, cr, uid, picking_id, qty_by_prod, context=None):
        # create partial delivery by validating picking move line
        # with qty for product taken from the buffer
        partial_datas = {}
        picking = self.browse(cr, uid, picking_id, context=context)
        origin, dest = '', ''
        for move in picking.move_lines:
            origin = move.location_id.id
            dest = move.location_dest_id.id
            if move.state in ('draft', 'waitting', 'confirmed', 'assigned') \
                    and qty_by_prod.get(move.product_id.id):
                # delivery qty >= expected qty
                if qty_by_prod[move.product_id.id] >= move.product_qty:
                    partial_datas["move" + str(move.id)] = {
                        'product_qty': move.product_qty,
                        'product_uom': move.product_uom.id
                    }
                    qty_by_prod[move.product_id.id] -= move.product_qty
                else:
                    partial_datas["move" + str(move.id)] = {
                        'product_qty': qty_by_prod[move.product_id.id],
                        'product_uom': move.product_uom.id}
                    qty_by_prod[move.product_id.id] = 0
        # Check if still some products in the buffer
        for item in qty_by_prod:
            if qty_by_prod[item] > 0:
                # move in are validated automatically
                if picking.type in ['in', 'internal']:
                    product_details = self.pool['product.product'].read(
                        cr, uid, [item], ['uom_id', 'name'])[0]
                    # use read instead of building a uom dict in
                    # for movelines because extra product
                    # can be in no planned move
                    move_to_create = {
                        "name": "EXTRA DELIVERY : " + str(item) + " : " +
                        product_details['name'],
                        "date": time.strftime('%Y-%m-%d %H:%M:%S'),
                        "date_expected": time.strftime('%Y-%m-%d %H:%M:%S'),
                        "product_id": item,
                        "product_qty": qty_by_prod[item],
                        "product_uom": product_details['uom_id'][0],
                        "location_id": origin,
                        "location_dest_id": dest,
                        "picking_id": picking.id,
                        "company_id": picking.company_id.id}
                    move_id = self.pool['stock.move'].create(
                        cr, uid, move_to_create)
                    partial_datas["move" + str(move_id)] = {
                        'product_qty': qty_by_prod[item],
                        'product_uom': move.product_uom.id}
                # move out raise an alert
                else:
                    raise orm.except_orm(
                        _('Warning !'),
                        _("Too much product delivered for picking '%s' (id %s)"
                          % (picking.name, picking.id)))
        self.do_partial(cr, uid, [picking.id], partial_datas)
        return True


class StockPicking(orm.Model):
    _inherit = ['stock.picking', 'abstract.stock.picking']
    _name = 'stock.picking'

    # Due to this bug https://bugs.launchpad.net/openobject-addons/+bug/1169998
    # you need do declare new fields in both picking models
    def __init__(self, pool, cr):
        super(StockPicking, self).__init__(pool, cr)
        for field in LOGISTIC_FIELDS:
            self._columns[field] = \
                self.pool['abstract.stock.picking']._columns[field]


class StockPickingIn(orm.Model):
    _inherit = ['stock.picking.in', 'abstract.stock.picking']
    _name = 'stock.picking.in'

    # Due to this bug https://bugs.launchpad.net/openobject-addons/+bug/1169998
    # you need do declare new fields in both picking models
    def __init__(self, pool, cr):
        super(StockPickingIn, self).__init__(pool, cr)
        for field in LOGISTIC_FIELDS:
            self._columns[field] = \
                self.pool['abstract.stock.picking']._columns[field]
        self._columns['partner_id'].required = True


class StockPickingOut(orm.Model):
    _inherit = ['stock.picking.out', 'abstract.stock.picking']
    _name = 'stock.picking.out'

    # Due to this bug https://bugs.launchpad.net/openobject-addons/+bug/1169998
    # you need do declare new fields in both picking models
    def __init__(self, pool, cr):
        super(StockPickingOut, self).__init__(pool, cr)
        for field in LOGISTIC_FIELDS:
            self._columns[field] = \
                self.pool['abstract.stock.picking']._columns[field]
