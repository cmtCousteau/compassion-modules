# -*- coding: utf-8 -*-
##############################################################################
#
#    Copyright (C) 2016 Compassion CH (http://www.compassion.ch)
#    Releasing children from poverty in Jesus' name
#    @author: Emanuel Cino <ecino@compassion.ch>
#
#    The licence is in the file __manifest__.py
#
##############################################################################
from odoo import models, fields, _


class GmcActionConnect(models.Model):
    """
    Maps an Action with a Connect Message Type
    """
    _name = 'gmc.action.connect'
    _rec_name = 'connect_schema'

    connect_schema = fields.Char(required=True)
    action_id = fields.Many2one('gmc.action', 'GMC Action', required=False)

    _sql_constraints = [
        ('connect_schema_uniq', 'UNIQUE(connect_schema)',
         _("You cannot have two actions with same connect schema."))]
