#!/usr/bin/env python

# Copyright (c) 2014 SpinPunch. All rights reserved.
# Use of this source code is governed by an MIT-style license that can be
# found in the LICENSE file.

# automatically generate hives_client.json from hives.json

import SpinJSON
import AtomicFileWrite
import sys, os, getopt

def _get_kill_points(cons):
    if cons['consequent'] == 'SESSION_LOOT' and cons['key'] == 'hive_kill_points':
        return cons['value']
    elif cons['consequent'] == 'AND':
        return max([_get_kill_points(sub) for sub in cons['subconsequents']])
    return 0

def get_kill_points(template):
    if 'completion' in template:
        return _get_kill_points(template['completion'])
    return 0

if __name__ == '__main__':
    opts, args = getopt.gnu_getopt(sys.argv[1:], '', [])

    hives = SpinJSON.load(open(args[0]), 'hives_compiled.json')
    out_fd = AtomicFileWrite.AtomicFileWrite(args[1], 'w', ident=str(os.getpid()))

    print >> out_fd.fd, "// AUTO-GENERATED BY make_hives_client.py"

    out = {'templates':{}}

    ids = sorted(hives['templates'].keys())
    for id in ids:
        template = hives['templates'][id]
        out['templates'][id] = {'ui_name': template['ui_name'],
                                'icon': template['icon']}
        ui_loot_rarity = template.get('ui_loot_rarity',-1)
        if ui_loot_rarity >= 0:
            out['templates'][id]['ui_loot_rarity'] = ui_loot_rarity
        for FIELD in ('activation', 'show_if', 'ui_tokens', 'base_resource_loot'):
            if FIELD in template: out['templates'][id][FIELD] = template[FIELD]
        kill_points = get_kill_points(template)
        if kill_points > 0:
            out['templates'][id]['kill_points'] = kill_points

    SpinJSON.dump(out, out_fd.fd, pretty=True)
    out_fd.complete()
