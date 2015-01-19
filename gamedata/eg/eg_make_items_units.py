#!/usr/bin/env python

# Copyright (c) 2015 SpinPunch. All rights reserved.
# Use of this source code is governed by an MIT-style license that can be
# found in the LICENSE file.

# automatically generates items_units.json from units.json

# this script makes the inventory items that represent "packaged" units, security guard equipment, and unit mods

import SpinConfig
import SpinJSON
import AtomicFileWrite
import sys, getopt, os

if __name__ == '__main__':
    opts, args = getopt.gnu_getopt(sys.argv[1:], '', [])
    game_id = SpinConfig.game()

    units = SpinConfig.load(args[0])
    out_fd = AtomicFileWrite.AtomicFileWrite(args[1], 'w', ident=str(os.getpid()))

    print >>out_fd.fd, "// AUTO-GENERATED BY make_items_units.py"

    out = []

    # create packaged-unit items
    for spec_name, spec in units.iteritems():
        for min_level in (1,4,8):
            if spec_name in ('curiosity','phantom'):
                rarity = 3 # epic
            else:
                rarity = {1:1, 4:1, 8:2}[min_level]

            out.append({
                'name': 'packaged_'+spec_name + ('_L%d' % min_level if min_level > 1 else ''),
                'ui_name': spec['ui_name'] + ((' L%d+' % min_level) if (True or min_level > 1 )else ''),
                'ui_description': 'Adds unit to your army when activated.',
                #'unit_icon': spec_name,
                'icon': 'inventory_%s' % spec_name,
                'stack_max': spec.get('item_stack_max', 5),
                'use': { 'spellname': 'GIVE_UNITS', 'spellarg':
                         { spec_name: {'qty':1,'min_level':min_level} if min_level > 1 else 1 }
                         },
                'rarity': rarity,
                'category': 'unit'
                })


    # classify units as basic/intermediate/advanced for purposes of equip item level requirements
    BASIC = 0
    INTERMEDIATE = 1
    ADVANCED = 2
    UNIT_CLASSES = {'rifleman': BASIC,
                    'stryker': INTERMEDIATE,
                    'oh58': BASIC,
                    }

    # create unit equips
    UNIT_EQUIP_DATA = {}
    # create manufacture class equips
    MANUFACTURE_CLASS_EQUIP_DATA = {}
    MANUFACTURE_CLASS_EQUIP_DATA_OLD = {
                                  'aoefire_shield': {'slot_type': 'utility',
                                  'ui_name_pattern': "%s %.0f%% FASCAM Protection Gear",
                                  'ui_description_pattern': "Protects Level %d or higher %s against FASCAM defenses, reducing damage by %.0f%%.",
                                  'stats': ['damage_taken_from:aoefire'], 'method': '*=(1-strength)',
                                  'strength': {'worst': 0.40, 'normal': 0.50, 'fair': 0.60, 'good': 0.70, 'excellent': 0.80, 'best': 0.90},
                                  'min_level': {'worst': 1, 'normal': 2, 'fair': 3, 'good': 4, 'excellent': 5, 'best': 6}}
    }

    UNIT_MANUFACTURE_CLASSES = {
                              'rovers' : {'ui_name': 'Infantry'},
                              'transports': {'ui_name': 'Armor'},
                              'starcraft': {'ui_name': 'Aircraft'}
    }

    for kind, data in MANUFACTURE_CLASS_EQUIP_DATA.iteritems():
        for spec_name, actual_name in UNIT_MANUFACTURE_CLASSES.iteritems():
            for goodness in data['strength'].iterkeys():

                strength = data['strength'][goodness]
                pct = 100.0*strength
                min_level = data['min_level'][goodness]

                out.append({'name': "%s_%s_%s" % (spec_name, kind, goodness),
                            'ui_name': data['ui_name_pattern'] % (actual_name['ui_name'], pct),
                            'rarity': {'worst': 1, 'normal': 1, 'fair': 2, 'good': 2, 'excellent': 3, 'best': 3}[goodness],
                            'ui_description': data['ui_description_pattern'] % (min_level, actual_name['ui_name'], pct),
                            'icon': 'inventory_%s_%s_%s' % (kind, spec_name, {'worst': 'black', 'normal': 'gray', 'fair': 'blue', 'good': 'green', 'excellent': 'purple', 'best': 'orange'}[goodness]),
                            "equip": { "kind": "mobile",
                                       "manufacture_category": spec_name,
                                       "min_level": min_level,
                                       "slot_type": data['slot_type'],

                                       "effects": [{ "code": 'modstat', "stat": stat, "method": data['method'], "strength": strength } for stat in data['stats']]
                                       }
                            })



    # create security guard items
    GUARD_DATA = {'generator': {'history_category': 'generators', 'ui_name': "Generator", 'ui_name_equips': 'Generator', 'icon': 'energy',

                                   # minimum building level to equip an item of this level
                                   # L1,L2 items are for CC Level 3/4 players
                                   # L3,L4 items are for (new) CC Level 5 players
                                   # L5,L6 items are for (elder) CC Level 5 players, and up
                                   'min_level': [0, 4,4, 6,6, 9,9],

                                   # note that for ALL energy plants, storages, harvesters, and turrets (except LRAT cannons), the progression goes like this:
                                   # CC1: 1,2
                                   # CC2: 3,4
                                   # CC3: 5,6
                                   # CC4: 7,8
                                   # CC5: 9,10

                                   # the idea is that L2 items (highest drop on Normal difficulty) will encourage upgrades to level 7 (i.e. CC4)
                                   # L4 items (highest drop on Heroic) will encourage upgrades to level 9
                                   # L5 items (highest drop on Epic) will encourage upgrades to level 10

                                   # 'units' tells what units will spawn when the guard is triggered
                                   # the array is indexed by item level (up to and including GUARD_MAX_LEVEL. There is no L0 item).

                                   # simple form is {'rifleman': 3, 'stryker': 1} to make 3x Mining Droids and 1x Muscle Box

                                   # if you want to spawn units with a certain minimum level, do this:
                                   # {'rifleman': {'qty': 5, 'min_level': 8}, ... } to make 8x Mining Droid at L8

                                   'units': [None, # L0
                                             {'rifleman': 1}, # L1
                                             {'rifleman': 2}, # L2
                                             {'rifleman': 3}, # L3
                                             {'rifleman': 4}, # L4
                                             {'rifleman': 5}, # L5
                                             {'rifleman': 6}, # L6
                                             ]
                                   },
                  }
    GUARD_MAX_LEVEL = 6
    GUARD_ICON_COLORS = [None, 'green', 'green', 'blue', 'blue', 'purple', 'purple'] # indexed by item level
    GUARD_RARITIES = [0,1,1,2,2,3,3] # indexed by item level

    for category, data in GUARD_DATA.iteritems():
        for level in range(1,GUARD_MAX_LEVEL+1):

            unit_dic = data['units'][level]
            if not unit_dic: continue

            ui_units_list = []
            for name, udat in unit_dic.iteritems():
                if type(udat) is dict:
                    qty = udat['qty']
                    min_level = udat.get('min_level',1)
                else:
                    qty = udat
                    min_level = 1
                if min_level > 1:
                    level_string = ' L%d+' % min_level
                else:
                    level_string = ''
                ui_units_list.append('%dx %s' % (qty, units[name]['ui_name']) + level_string)
            if len(ui_units_list) > 1:
                # join the unit descriptions in a gramatically-correct way, with Oxford comma :)
                ui_units = ', '.join(ui_units_list[:-1]) + (', and ' if len(ui_units_list) > 2 else ' and ') + ui_units_list[-1]
            else:
                ui_units = ui_units_list[0]

            min_level = data['min_level'][level]
            if min_level > 1:
                ui_level_req = 'Level %d or higher' % min_level
            else:
                ui_level_req = ''

            out.append({'name': '%s_secteam_L%d' % (category, level),
                        'ui_name': '%s Guards L%d' % (data['ui_name'], level),
                        'rarity': GUARD_RARITIES[level],
                        'ui_description': "Equips a %s %s with %s guards. Guards spawn each time building is destroyed and disappear after battle." % (ui_level_req, data['ui_name_equips'], ui_units),
                        'icon': 'inventory_secteam_%s_%s' % (data['icon'], GUARD_ICON_COLORS[level]),
                        'equip': { 'kind': 'building',
                                   'history_category': data['history_category'],
                                   'slot_type': 'defense',
                                   'min_level': data['min_level'][level],
                                   'effects': [
                                        {"code": "modstat", "stat": "on_destroy", "method": "concat", "strength": [{"consequent": "SPAWN_SECURITY_TEAM", "units": unit_dic}]},
                                        ]
                                   }
                        })

    count = 0
    for data in out:
        print >>out_fd.fd, '"%s":' % data['name'], SpinJSON.dumps(data, pretty=True),
        if count != len(out)-1:
            print >>out_fd.fd,  ','
        else:
            print >>out_fd.fd
        count += 1

    out_fd.complete()
