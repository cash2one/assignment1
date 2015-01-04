#!/usr/bin/env python

# Copyright (c) 2014 SpinPunch. All rights reserved.
# Use of this source code is governed by an MIT-style license that can be
# found in the LICENSE file.

# this script makes various procedurally-generated art asset permutations for inventory items
# needs units.json as input for unit names

import SpinConfig
import SpinJSON
import AtomicFileWrite
import sys, getopt, os

if __name__ == '__main__':
    opts, args = getopt.gnu_getopt(sys.argv[1:], 'g:', ['game-id='])
    game_id = SpinConfig.game()

    for key, val in opts:
        # allow override of game_id
        if key == '-g' or key == '--game-id': game_id = val

    units = SpinConfig.load(args[0])
    out_fd = AtomicFileWrite.AtomicFileWrite(args[1], 'w', ident=str(os.getpid()))

    print >>out_fd.fd, "// AUTO-GENERATED BY make_art_items.py"

    units = SpinConfig.load(args[0])

    out = {}
    outkeys = []

    # create unit inventory icons
    EXTRA_UNITS = [] # not included in units.json but icons are needed
    if game_id == 'tr':
        EXTRA_UNITS += [('ch47', {'art_asset': 'ch47'})]
    for unit_name, unit_data in units.items() + EXTRA_UNITS:
        key = 'inventory_%s' % unit_name
        outkeys.append(key)
        DEFAULT_OFFSET = [0,-5]
        # special-case pixel offsets for units where the default offset does not look good
        SPECIAL_OFFSETS = {'rifleman': [0,0],
                           'mortarman': [0,0],
                           'stinger_gunner': [0,0],
                           'cyclone': [0,5],
                           'saber': [-2,1],
                           'ch47': [0,0],

                           # BFM
                           'marine': [0,0],
                           'grenadier': [0,0],
                           'chaingunner': [0,3],
                           'boomer': [0,0],
                           'marksman': [0,-4],
                           'rainmaker': [0,5], 'elite_rainmaker': [0,5],
                           'wrecker': [0,0], 'elite_wrecker': [0,0],
                           'centurion': [0,7],
                           'mauler': [0,6],
                           'outrider':[0,8],
                           'tornado': [0,3],
                           'voodoo': [0,8],
                           'hellhound': [0,8],

                           # SG
                           'swordsman': [0,6],
                           'thief': [0,6],
                           'orc': [0,6],
                           'archer': [0,6],
                           'paladin': [0,6],
                           'airship': [-2,-1],
                           'sorceress': [0,6],
                           'fire_phantom': [0,6],
                           'golem': [0,4],
                           'dragon': [5,-5],
                           }
        out[key] = {"states": { "normal": { "dimensions": [50,50], "subassets": [{"name": unit_data['art_asset'],
                                                                                  "state": "icon",
                                                                                  "centered": 1,
                                                                                  "offset": SPECIAL_OFFSETS.get(unit_name, DEFAULT_OFFSET),
                                                                                  "clip_to": [0,0,50,50]}]
                                            } } }

    # create icons for DPS/environ/armor boost/armor equip/range/speed mods for units
    if game_id in ('mf','tr','mf2','dv','bfm'):
        buff_kinds = ['damage', 'armor', 'range', 'speed']
        if game_id in ('tr','dv','sg','bfm','mf2'):
            buff_kinds += ['damage_resist_equip','damage_boost_equip','range_boost_equip']
        if game_id != 'bfm':
            buff_kinds += ['radcold']
        for kind in buff_kinds:
            for unit_name, unit_data in units.iteritems():
                if unit_name == 'repair_droid': continue
                if unit_data.get('activation',{}).get('predicate',None) == 'ALWAYS_FALSE': continue # skip disabled units

                for rarity_color in ('black','gray', 'green', 'blue', 'purple', 'orange'):

                    #if kind == 'radcold' and rarity_color != 'purple': continue

                    key = 'inventory_%s_%s_%s' % (kind, unit_name, rarity_color)
                    outkeys.append(key)
                    out[key] = \
                                             { "states": { "normal": { "subassets": ["inventory_bg_%s" % rarity_color,
                                                                                     "inventory_%s" % unit_name,
                                                                                     "inventory_%s" % kind] } } }

    # create icons for DPS/armor/range/speed mods for manufacture categories
    if game_id in ('mf','tr','mf2','dv','sg','bfm'):
        buff_kinds = ['damage', 'armor', 'range', 'speed']
        if game_id != 'bfm':
            buff_kinds += ['radcold']
        for kind in buff_kinds:
            for (unit_type, unit_type_plural) in {'rover': 'rovers', 'transport': 'transports', 'starcraft': 'starcraft'}.iteritems():
                for rarity_color in ('black','gray', 'green', 'blue', 'purple', 'orange'):

                    key = 'inventory_%s_%s_%s' % (kind, unit_type, rarity_color)
                    outkeys.append(key)
                    out[key] = { "states": { "normal": { "subassets": ["inventory_bg_%s" % rarity_color,
                                                                       "inventory_%s" % unit_type_plural,
                                                                       "inventory_%s" % kind] } } }

    if game_id in ('tr','dv','sg','bfm'):
        # TR only - create icons for damage_vs mods for tr unit categories (infantry, armor, aircraft)
        for kind in ('damage_vs_rover', 'damage_vs_transport'):
            for unit_name, unit_data in units.iteritems():
                if unit_name == 'repair_droid': continue
                if unit_data.get('activation',{}).get('predicate',None) == 'ALWAYS_FALSE': continue # skip disabled units

                for rarity_color in ('black','gray', 'green', 'blue', 'purple', 'orange'):

                    key = 'inventory_%s_%s_%s' % (kind, unit_name, rarity_color)
                    outkeys.append(key)
                    out[key] = \
                                             { "states": { "normal": { "subassets": ["inventory_bg_%s" % rarity_color,
                                                                                     "inventory_%s" % unit_name,
                                                                                     "inventory_damage",
                                                                                     "inventory_%s" % kind] } } }

        # TR only - create icons for aoefire_shield mods for unit categories (infantry, armor, aircraft)
        for kind in ('aoefire_shield',):
            for unit_category_name in ('rovers', 'transports', 'starcraft'):
                for rarity_color in ('black','gray', 'green', 'blue', 'purple', 'orange'):
                    key = 'inventory_%s_%s_%s' % (kind, unit_category_name, rarity_color)
                    outkeys.append(key)
                    out[key] = \
                                             { "states": { "normal": { "subassets": ["inventory_bg_%s" % rarity_color,
                                                                                     "inventory_%s" % unit_category_name,
                                                                                     "inventory_%s" % kind] } } }

        # TR only - create icons for boosts that apply to individual units
        if game_id in ('tr','dv'):
            for kind in ('secteam','waterdrop'):
                for unit_name in ('gaz_tigr','humvee','btr90','stryker','brdm3','m109','m2bradley'):
                    for rarity_color in ('black','gray', 'green', 'blue', 'purple', 'orange'):
                        key = 'inventory_%s_%s_%s' % (kind, unit_name, rarity_color)
                        outkeys.append(key)
                        out[key] = \
                                 { "states": { "normal": { "subassets": ["inventory_bg_%s" % rarity_color,
                                                                         "inventory_%s" % unit_name,
                                                                         "inventory_%s" % kind] } } }

        # BFM only - create icons for boosts that apply to individual units
        if game_id == 'bfm':
            for kind in ('secteam','waterdrop'):
                for unit_name in ('outrider','saber','voodoo','curiosity','hellhound','gun_runner','punisher'):
                    for rarity_color in ('black','gray', 'green', 'blue', 'purple', 'orange'):
                        key = 'inventory_%s_%s_%s' % (kind, unit_name, rarity_color)
                        outkeys.append(key)
                        out[key] = \
                                 { "states": { "normal": { "subassets": ["inventory_bg_%s" % rarity_color,
                                                                         "inventory_%s" % unit_name,
                                                                         "inventory_%s" % kind] } } }

    count = 0
    for key in outkeys:
        val = out[key]
        print >>out_fd.fd, '"%s":' % key, SpinJSON.dumps(val),
        if count != len(outkeys)-1:
            print >>out_fd.fd, ','
        else:
            print >>out_fd.fd
        count += 1

    out_fd.complete()
