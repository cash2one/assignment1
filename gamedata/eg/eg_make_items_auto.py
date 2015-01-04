#!/usr/bin/env python

# Copyright (c) 2014 SpinPunch. All rights reserved.
# Use of this source code is governed by an MIT-style license that can be
# found in the LICENSE file.

# this script makes various procedurally-generated inventory items

import SpinConfig
import SpinJSON
import AtomicFileWrite
import sys, re, traceback, os, getopt

if __name__ == '__main__':
    opts, args = getopt.gnu_getopt(sys.argv[1:], '', [])
    ident = str(os.getpid())

    gamedata = {'resources': SpinConfig.load(args[0])}
    items_out = AtomicFileWrite.AtomicFileWrite(args[1], 'w', ident=ident); items_fd = items_out.fd
    spells_out = AtomicFileWrite.AtomicFileWrite(args[2], 'w', ident=ident); spells_fd = spells_out.fd

    for fd in items_fd, spells_fd:
        print >> fd, "// AUTO-GENERATED BY make_items_auto.py"

    out = {'items':[], 'spells': []}

    # create all iron/water items
    for resource, resdata in gamedata['resources'].iteritems():
        RESOURCE = resource.upper()

        # names seen by player
        ui_resource = resdata['ui_name_lower']
        ui_Resource = resdata['ui_name']

        # percentage-based boosts
        if resdata.get('allow_instant', True):
            for percent in (10, 50, 100):
                out['items'].append({
                    'name': 'boost_%s_%dpct' % (resource, percent),
                    'ui_name': '%d%% %s Boost' % (percent, ui_Resource),
                    'ui_description': 'Activate to fill your %s storage to 100%%' % ui_resource if percent == 100 else \
                    'Activate to add %d%% of your total %s storage capacity' % (percent, ui_resource),
                    'icon': 'inventory_boost_%s_%dpct' % (resource, percent),
                    'rarity': {100:3,50:2,40:2,30:1,20:0,10:0,5:-1}[percent],
                    'category': 'resource_boost_percent',
                    'use': { 'spellname': 'BOOST_%s_%dPCT' % (RESOURCE, percent) }
                    })

        # flat-amount boosts
        for data in ({'amount':1000, 'ui_short': '1K', 'ui_long': '1,000', 'icon_pct': 5, 'rarity': -1},
                     {'amount':5000, 'ui_short': '5K', 'ui_long': '5,000', 'icon_pct': 5, 'rarity': -1},
                     {'amount':10000, 'ui_short': '10K', 'ui_long': '10,000', 'icon_pct': 20, 'rarity': 0},
                     {'amount':15000, 'ui_short': '15K', 'ui_long': '15,000', 'icon_pct': 20, 'rarity': 0},
                     {'amount':20000, 'ui_short': '20K', 'ui_long': '20,000', 'icon_pct': 20, 'rarity': 0},
                     {'amount':25000, 'ui_short': '25K', 'ui_long': '25,000', 'icon_pct': 20, 'rarity': 0},
                     {'amount':30000, 'ui_short': '30K', 'ui_long': '30,000', 'icon_pct': 20, 'rarity': 0},
                     {'amount':40000, 'ui_short': '40K', 'ui_long': '40,000', 'icon_pct': 20, 'rarity': 0},
                     {'amount':50000, 'ui_short': '50K', 'ui_long': '50,000', 'icon_pct': 30, 'rarity': 1},
                     {'amount':75000, 'ui_short': '75K', 'ui_long': '75,000', 'icon_pct': 30, 'rarity': 1},
                     {'amount':100000, 'ui_short': '100K', 'ui_long': '100,000', 'icon_pct': 40, 'rarity': 2},
                     {'amount':250000, 'ui_short': '250K', 'ui_long': '250,000', 'icon_pct': 40, 'rarity': 2},
                     {'amount':500000, 'ui_short': '500K', 'ui_long': '500,000', 'icon_pct': 50, 'rarity': 2},
                     {'amount':1000000, 'ui_short': '1M', 'ui_long': '1,000,000', 'icon_pct': 100, 'rarity': 3},
                     {'amount':2000000, 'ui_short': '2M', 'ui_long': '2,000,000', 'icon_pct': 100, 'rarity': 3},
                     ):
            out['items'].append({
                'name': 'boost_%s_%d' % (resource, data['amount']),
                'ui_name': '%s %s' % (data['ui_short'], ui_Resource),
                'ui_description': 'Activate to add %s %s' % (data['ui_long'], ui_Resource),
                'icon': 'inventory_boost_%s_%dpct' % (resource, data['icon_pct']),
                'rarity': data['rarity'],
                'category': 'resource_boost_flat',
                'use': { 'spellname': 'BOOST_%s_%d' % (RESOURCE, data['amount']) }
                })

            # spell
            out['spells'].append({
                'name': 'BOOST_%s_%d' % (RESOURCE, data['amount']),
                'ui_name': '%s %s Boost' % (data['ui_short'], ui_Resource),
                'ui_description': '%s %s Boost' % (data['ui_short'], ui_Resource),
                'resource': resource, 'give_amount': data['amount'], 'activation': 'instant'
                })

            # gift-sender items
            out['items'].append({
                'name': 'boost_%s_%d_gift' % (resource, data['amount']),
                'ui_name': 'Clan Gift: %s %s' % (data['ui_short'], ui_Resource),
                'ui_description': 'Activate to send %s %s to another member of your Clan.' % (data['ui_long'], ui_Resource),
                'icon': 'inventory_boost_%s_%dpct_gift' % (resource, data['icon_pct']),
                'rarity': data['rarity'],
                'category': 'resource_boost_flat',
                # gifts one of the flat resource boost items from above
                'use': { 'spellname': 'ALLIANCE_GIFT_LOOT', 'spellarg': [{'loot':[{'spec':'boost_%s_%d' % (resource, data['amount'])}]}] }
                })

    # create building armor items
    # (unfortunately ui_name is redundant with buildings.json
    ARMOR_DATA = [#{'name': 'generator', 'ui_name': 'Generator', 'min_level': [0,4,4,6,6,9,9], 'strength': [0,0.05,0.10,0.15,0.20,0.25,0.30]},
                  #{'name': 'supply_depot', 'ui_name': 'Supply Depot', 'min_level': [0,4,4,6,6,9,10], 'strength': [0,0.05,0.10,0.15,0.20,0.25,0.30]},
                  #{'name': 'supply_yard', 'ui_name': 'Supply Yard', 'min_level': [0,4,4,6,6,9,10], 'strength': [0,0.05,0.10,0.15,0.20,0.25,0.30]},
                  #{'name': 'fuel_depot', 'ui_name': 'Fuel Depot', 'min_level': [0,4,4,6,6,9,10], 'strength': [0,0.05,0.10,0.15,0.20,0.25,0.30]},
                  #{'name': 'fuel_yard', 'ui_name': 'Fuel Yard', 'min_level': [0,4,4,6,6,9,10], 'strength': [0,0.05,0.10,0.15,0.20,0.25,0.30]}
                  ]
    for DATA in ARMOR_DATA:
        MAX_LEVEL = 6
        ICON_COLORS = [None, 'green', 'green', 'blue', 'blue', 'purple', 'purple'] # indexed by item level
        RARITIES = [0,1,1,2,2,3,3]

        for level in range(1,MAX_LEVEL+1):
            out['items'].append({'name': '%s_armor_L%d' % (DATA['name'], level),
                        'rarity': RARITIES[level],
                        'ui_name': '%s Armor L%d' % (DATA['ui_name'], level),
                        'ui_description': 'Equips a Level %d or higher %s with armor, reducing damage taken by %.0f%%.' % (DATA['min_level'][level], DATA['ui_name'], 100.0*DATA['strength'][level]),
                        'icon': 'inventory_armor_%s_%s' % (DATA['name'], ICON_COLORS[level]),
                        'equip': { 'min_level': DATA['min_level'][level],
                                   'kind': 'building',
                                   'name': DATA['name'],
                                   'slot_type': 'defense',
                                   'dev_only': 1, # disable slot presence check

                                   'effects': [{'code': 'modstat',
                                                'stat': 'damage_taken',
                                                'method': '*=(1-strength)',
                                                'strength': DATA['strength'][level]}]
                }})

    for name, fd in (('items', items_fd), ('spells', spells_fd)):
        count = 0
        for data in out[name]:
            print >> fd, '"%s":' % data['name'], SpinJSON.dumps(data, pretty=True),
            if count != len(out[name])-1:
                print >> fd, ','
            else:
                print >> fd
            count += 1

    items_out.complete()
    spells_out.complete()