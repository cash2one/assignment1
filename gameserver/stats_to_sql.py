#!/usr/bin/env python

# Copyright (c) 2015 SpinPunch. All rights reserved.
# Use of this source code is governed by an MIT-style license that can be
# found in the LICENSE file.

# dump gamedata unit/building/item/etc stats to SQL for analytics use

import sys, getopt
import SpinConfig
import SpinJSON
import SpinSQLUtil
import MySQLdb

stats_schema = {
    'fields': [('kind', 'VARCHAR(16) NOT NULL'),
               ('spec', 'VARCHAR(64) NOT NULL'),
               ('stat', 'VARCHAR(64) NOT NULL'),
               ('level', 'INT1'),
               ('value_num', 'FLOAT8'),
               ('value_str', 'VARCHAR(128)')],
    'indices': {'master': {'keys': [('kind','ASC'),('spec','ASC'),('stat','ASC'),('level','ASC')]}}
    }
crafting_recipes_schema = {
    'fields': [('recipe_id', 'VARCHAR(64) NOT NULL'),
               ('is_output', 'TINYINT(1) NOT NULL'),
               ('resource', 'VARCHAR(64) NOT NULL'),
               ('amount', 'INT4 NOT NULL')],
    'indices': {'master': {'keys': [('recipe_id','ASC'),('is_output','ASC')]}}
    }
fishing_slates_schema = {
    'fields': [('slate_id', 'VARCHAR(64) NOT NULL'),
               ('recipe_id', 'VARCHAR(64) NOT NULL')],
    'indices': {'by_slate': {'keys': [('slate_id','ASC')]}}
    }

# commit block of inserts to a table
def flush_keyvals(sql_util, cur, tbl, keyvals):
    if not dry_run:
        try:
            sql_util.do_insert_batch(cur, tbl, keyvals)
        except MySQLdb.Warning as e:
            raise Exception('while inserting into %s:\n' % tbl+'\n'.join(map(repr, keyvals))+'\n'+repr(e))
        con.commit()
    del keyvals[:]

if __name__ == '__main__':
    game_id = SpinConfig.game()
    commit_interval = 1000
    verbose = True
    dry_run = False

    opts, args = getopt.gnu_getopt(sys.argv[1:], 'g:c:q', ['dry-run'])

    for key, val in opts:
        if key == '-g': game_id = val
        elif key == '-c': commit_interval = int(val)
        elif key == '-q': verbose = False
        elif key == '--dry-run': dry_run = True

    sql_util = SpinSQLUtil.MySQLUtil()
    if verbose or True:
        from warnings import filterwarnings
        filterwarnings('error', category = MySQLdb.Warning)
    else:
        sql_util.disable_warnings()

    gamedata = SpinJSON.load(open(SpinConfig.gamedata_filename(override_game_id = game_id)))
    fishing_json_file = SpinConfig.gamedata_component_filename('fishing_slates.json', override_game_id = game_id)
    try:
        fishing_json_fd = open(fishing_json_file)
    except IOError:
        fishing_json_fd = None # no fishing in this game

    fishing_slates = SpinConfig.load_fd(fishing_json_fd, stripped=True) if fishing_json_fd else None

    cfg = SpinConfig.get_mysql_config(game_id+'_upcache')
    con = MySQLdb.connect(*cfg['connect_args'], **cfg['connect_kwargs'])
    stats_table = cfg['table_prefix']+game_id+'_stats'
    recipes_table = cfg['table_prefix']+game_id+'_crafting_recipes'
    fishing_slates_table = cfg['table_prefix']+game_id+'_fishing_slates'

    cur = con.cursor(MySQLdb.cursors.DictCursor)
    if not dry_run:
        filterwarnings('ignore', category = MySQLdb.Warning)
        for tbl, schema in ((stats_table, stats_schema),
                            (recipes_table, crafting_recipes_schema),
                            (fishing_slates_table, fishing_slates_schema)):
            cur.execute("DROP TABLE IF EXISTS "+sql_util.sym(tbl+'_temp'))
            sql_util.ensure_table(cur, tbl, schema)
            sql_util.ensure_table(cur, tbl+'_temp', schema)

        # TIME PRICING FORMULA
        # has to be created dynamically because gamedata.store influences the formula
        cur.execute("DROP FUNCTION IF EXISTS time_price") # obsolete
        min_per_cred = gamedata['store']['speedup_minutes_per_credit']
        if type(min_per_cred) is dict:
            min_per_cred = min_per_cred['default']
        bucks_per_min = float(gamedata['store']['gamebucks_per_fbcredit'])/min_per_cred
        cur.execute("CREATE FUNCTION time_price (amount INT8) RETURNS INT8 DETERMINISTIC RETURN IF(amount=0, 0, IF(amount>0,1,-1) * FLOOR(%f *ABS(amount)/60)+1)" % bucks_per_min)

        # RESOURCE PRICING FORMULAS
        # returns the gamebucks price of an amount of fungible resources

        # these have to be created dynamically because gamedata.store influences the formulas

        cur.execute("DROP FUNCTION IF EXISTS iron_water_price") # obsolete

        for res in ('iron', 'water'):
            cur.execute("DROP FUNCTION IF EXISTS "+res+"_price")
            def get_parameter(p, resname):
                ret = gamedata['store'][p]
                if type(ret) is dict:
                    ret = ret[resname]
                return ret
            formula = get_parameter('resource_price_formula', res)
            scale_factor = get_parameter('resource_price_formula_scale', res)
            if formula == 'legacy_exp_log':
                gamebucks_per_fbcredit = gamedata['store']['gamebucks_per_fbcredit']
                func = "IF(ABS(amount)<2, 1, %f*0.06*EXP(0.75*(LOG10(ABS(amount))-2.2*POW(LOG10(ABS(amount)),-1.25))))" % (scale_factor * gamebucks_per_fbcredit)
            elif formula == 'piecewise_linear':
                points = get_parameter('resource_price_formula_piecewise_linear_points', res)
                func = ""
                for i in xrange(1, len(points)):
                    slope = float(points[i][1] - points[i - 1][1]) / (points[i][0] - points[i - 1][0])
                    seg = "%f * (%f + %f * (amount - %f))" % (scale_factor, points[i-1][1], slope, points[i-1][0])
                    if i != len(points) - 1:
                        seg = "IF(amount < %f,%s," % (points[i][0], seg)
                    func += seg
                func += ")" * (len(points)-2)
            else:
                raise Exception('unknown resource_price_formula '+formula)
            final = "CREATE FUNCTION "+res+"_price (amount INT8) RETURNS INT8 DETERMINISTIC RETURN IF(amount=0, 0, IF(amount>0,1,-1) * GREATEST(1, CEIL("+func+")))"
            cur.execute(final)

        filterwarnings('error', category = MySQLdb.Warning)
        con.commit()

    # OBJECT STATS

    total = 0
    keyvals = []

    for objects, kind in ((gamedata['units'],'unit'),
                          (gamedata['buildings'],'building'),
                          (gamedata['tech'],'tech'),
                          (gamedata['items'],'item'),
                          (gamedata['crafting']['recipes'],'recipe')
                          ):
        for specname, data in objects.iteritems():
            if kind == 'unit': num_levels = len(data['max_hp'])
            elif kind == 'building': num_levels = len(data['build_time'])
            elif kind == 'tech': num_levels = len(data['research_time'])
            elif kind == 'item': num_levels = 1
            elif kind == 'recipe': num_levels = 1


            # add max_level stat (redundant, but makes writing other queries easier)
            if num_levels > 1 or kind in ('unit','building','tech'):
                keyvals.append([('kind',kind),
                                ('spec',specname),
                                ('stat','max_level'),
                                ('level',None),
                                ('value_num',num_levels),
                                ('value_str',None)])

            for key, val in data.iteritems():
                val_type = None # 'num' or 'str'
                val_levels = None

                if type(val) in (int, float): # single number
                    val_type = 'num'
                    val_list = [float(val)]
                elif type(val) in (str, unicode): # single string
                    val_type = 'str'
                    val_list = [val]

                elif type(val) is list and len(val) == num_levels:
                    val_levels = num_levels
                    if type(val[0]) in (int, float): # per-level number
                        val_type = 'num'
                        val_list = map(float, val)
                    elif type(val[0]) in (str, unicode): # per-level string
                        val_type = 'str'
                        val_list = val

                if val_type == 'str':
                    if key == 'name' or key == 'icon' or (key.startswith('ui_') and key != 'ui_name'):
                        # filter out unnecessary strings
                        continue

                if not val_type: continue # not a recognized value type


                for i in xrange(len(val_list)):
                    v = val_list[i]
                    if val_type == 'num':
                        if v < -2**31 or v > 2**31:
                            print 'value out of range: %s %s %s L%d: %s' % (kind, specname, key, i+1, repr(v))
                    elif val_type == 'str':
                        if len(v) > 64:
                            print 'value out of range: %s %s %s L%d: %s (len %d)' % (kind, specname, key, i+1, v, len(v))

                keyvals += [[('kind',kind),
                             ('spec',specname),
                             ('stat',key),
                             ('level',i+1 if val_levels else None),
                             ('value_num',val_list[i] if val_type == 'num' else None),
                             ('value_str',val_list[i] if val_type == 'str' else None)] \
                            for i in (xrange(val_levels) if val_levels else [0,])]

                total += len(keyvals)
                if commit_interval > 0 and len(keyvals) >= commit_interval:
                    flush_keyvals(sql_util, cur, stats_table+'_temp', keyvals)
                    if verbose: print total, 'object stats inserted'

    flush_keyvals(sql_util, cur, stats_table+'_temp', keyvals)
    con.commit()
    if verbose: print 'total', total, 'object stats inserted'

    # CRAFTING RECIPES

    total = 0
    keyvals = []

    for specname, data in gamedata['crafting']['recipes'].iteritems():
        keyvals.append((('recipe_id', specname),
                        ('is_output', 0),
                        ('resource', 'time'),
                        ('amount', data['craft_time'])))
        for res, amt in data['cost'].iteritems():
            keyvals.append((('recipe_id', specname),
                            ('is_output', 0),
                            ('resource', res),
                            ('amount', amt)))
        for entry in data.get('ingredients',[]):
            keyvals.append((('recipe_id', specname),
                            ('is_output', 0),
                            ('resource', entry['spec']),
                            ('amount', entry.get('stack',1))))

        for entry in data['product']:
            if 'spec' not in entry:
                raise Exception('cannot parse crafting recipe product: %s' % repr(entry))
            res = entry['spec']
            amt = entry.get('stack',1)
            keyvals.append((('recipe_id', specname),
                            ('is_output', 1),
                            ('resource', res),
                            ('amount', amt)))

        total += len(keyvals)
        if commit_interval > 0 and len(keyvals) >= commit_interval:
            flush_keyvals(sql_util, cur, recipes_table+'_temp', keyvals)
            if verbose: print total, 'crafting recipe inputs/outputs inserted'

    flush_keyvals(sql_util, cur, recipes_table+'_temp', keyvals)
    con.commit()
    if verbose: print 'total', total, 'crafting recipe inputs/outputs inserted'

    # FISHING SLATES
    if fishing_slates:
        total = 0
        keyvals = []

        for slate_name, data in fishing_slates.iteritems():
            for recipe_id in data['recipes']:
                keyvals.append((('slate_id', slate_name),
                                ('recipe_id', recipe_id)))
                total += 1
        flush_keyvals(sql_util, cur, fishing_slates_table+'_temp', keyvals)
        con.commit()
        if verbose: print 'total', total, 'fishing slate entries inserted'

    if not dry_run:
        filterwarnings('ignore', category = MySQLdb.Warning)
        for tbl in (stats_table, recipes_table, fishing_slates_table):
            cur.execute("RENAME TABLE "+\
                        sql_util.sym(tbl)+" TO "+sql_util.sym(tbl+'_old')+","+\
                        sql_util.sym(tbl+'_temp')+" TO "+sql_util.sym(tbl))
            cur.execute("DROP TABLE IF EXISTS "+sql_util.sym(tbl+'_old'))

        con.commit()
