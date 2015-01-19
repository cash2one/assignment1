#!/usr/bin/env python

# Copyright (c) 2015 SpinPunch. All rights reserved.
# Use of this source code is governed by an MIT-style license that can be
# found in the LICENSE file.

# automatically generate ai_bases_client.json from ai_bases_compiled.json

import SpinJSON
import AtomicFileWrite
import sys, os, getopt

def copy_display_message_consequent(cons):
    if cons['consequent'] == "DISPLAY_MESSAGE":
        return cons
    elif 'subconsequents' in cons:
        for sub in cons['subconsequents']:
            ret = copy_display_message_consequent(sub)
            if ret: return ret
    return None

if __name__ == '__main__':
    opts, args = getopt.gnu_getopt(sys.argv[1:], '', [])

    ai_bases = SpinJSON.load(open(args[0]), 'ai_bases_compiled.json')
    out_fd = AtomicFileWrite.AtomicFileWrite(args[1], 'w', ident=str(os.getpid()))

    print >> out_fd.fd, "// AUTO-GENERATED BY make_ai_bases_client.py"

    out = {'bases':{}}

    for strid, base in ai_bases['bases'].iteritems():
        client_base =  {'portrait': base['portrait'],
                        'resources': base['resources'],
                        'ui_name': base['ui_name'],
                        'activation': base['activation'] }

        for FIELD in ('kind', 'show_if', 'persistent', 'attack_time', 'base_resource_loot',
                      'ui_priority', 'ui_category', 'ui_info', 'ui_info_url', 'ui_resets', 'ui_instance_cooldown', 'ui_spy_button', 'ui_map_name', 'ui_progress',
                      'ui_battle_stars_key',
                      'challenge_icon',
                      ):
            if FIELD in base: client_base[FIELD] = base[FIELD]

        # awkward - on_visit DISPLAY_MESSAGE consequents must be known client-side for
        # AI attacks, since the code to round-trip them from the sever would be ugly.
        if base.get('kind', 'ai_base') == 'ai_attack':
            if 'on_visit' in base:
                cons = copy_display_message_consequent(base['on_visit'])
                if cons:
                    client_base['on_visit'] = cons

        out['bases'][strid] = client_base

    SpinJSON.dump(out, out_fd.fd, pretty = True, newline = True)
    out_fd.complete()
