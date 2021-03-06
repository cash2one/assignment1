#!/usr/bin/env python

# Copyright (c) 2015 SpinPunch. All rights reserved.
# Use of this source code is governed by an MIT-style license that can be
# found in the LICENSE file.

# dump upcache out to a MySQL database for analytics

import sys, time, getopt, re
import SpinConfig
import SpinJSON
import SpinS3
import SpinUpcache
import SpinUpcacheIO
import SkynetLTV # for outboard LTV estimate table
import SpinSQLUtil
import SpinParallel
import SpinSingletonProcess
import MySQLdb

time_now = int(time.time())

def field_column(key, val):
    return "`%s` %s" % (key, val)

def achievement_table_schema(sql_util):
    return {'fields': sql_util.summary_out_dimensions()  + \
              [('kind', 'VARCHAR(16) NOT NULL'),
               ('spec', 'VARCHAR(64) NOT NULL'),
               ('level', 'INT2'),
               ('is_maxed', 'TINYINT(1) NOT NULL'), # flag that this is the max level of the spec
               ('num_players', 'INT8 NOT NULL')
               ],
    'indices': {'master': {'unique': True, 'keys': [(x[0],'ASC') for x in sql_util.summary_out_dimensions()] + [('kind','ASC'),('spec','ASC'),('level','ASC')]}}
    }

# detect A/B test names
abtest_filter = re.compile('^T[0-9]+')

# "ALL" mode
# always accept these keys
accept_filter = re.compile('^item:instant_.*repair|feature_used:playfield_speed|ai_tutorial.*_progress$')
# then, always reject these keys
reject_filter = re.compile('^T[0-9]+_|acquisition_game_version|account_creation_hour|account_creation_wday|^fb_notification:|^feature_used:|^achievement:|^quest:|_conquests$|_progress$|_attempted$|days_since_joined|days_since_last_login|lock_state|^visits_[0-9]+d$|^retained_[0-9]+d$|oauth_token|facebook_permissions_str|acquisition_type|^link$|_context$|^item:|^unit:.+:(killed|lost)|_times_(started|completed)$')

# "lite" mode
lite_accept_filter = re.compile('|'.join('^'+x+'$' for x in \
                                         ['account_creation_time', 'last_login_time', 'time_of_first_purchase',
                                          'acquisition_campaign',
                                          'acquisition_ad_skynet',
                                          'country', 'country_tier', 'currency', 'timezone',
                                          'completed_tutorial',
                                          'frame_platform', 'social_id', 'facebook_id', 'facebook_name', 'kg_id', 'kg_username',
                                          'money_spent', 'money_refunded', 'num_purchases', 'client:purchase_ui_opens', 'client:purchase_ui_opens_preftd', 'player_level', 'player_xp',
                                          'home_region', 'tutorial_state', 'upcache_time',
                                          'returned_[0-9]+-[0-9]+h',
                                          'reacquired_[0-9]+d', 'first_time_reacquired_[0-9]+d', 'last_time_reacquired_[0-9]+d',
                                          'townhall_level', # note: this is wishful thinking - upcache actually just uses toc_,central_computer_ etc
                                          'birth_year', 'gender', 'friends_in_game', 'email', 'logged_in_times', 'alliances_joined', 'time_in_game',
                                          'spend_[0-9]+d', 'attacks_launched_vs_human', 'attacks_launched_vs_ai',
                                          'chat_messages_sent',
                                          ]))

def setup_field(gamedata, key, val, field_mode = None):
    # always accept A/B tests that are still ongoing
    if abtest_filter.search(key) and \
       key in gamedata['abtests'] and \
       gamedata['abtests'][key].get('active', 0) and \
       gamedata['abtests'][key].get('show_in_analytics', True):
        return 'VARCHAR(64)'

    # always accept townhall level
    if key == gamedata['townhall']+'_level':
        return 'INT4'

    # always accept developer flag
    if key == 'developer': return 'TINYINT(1)'

    # check reject filter
    if field_mode == 'lite':
        if not lite_accept_filter.search(key):
            return None
    elif field_mode == 'ALL':
        if reject_filter.search(key) and (not accept_filter.search(key)):
            return None # reject
    else:
        raise Exception('unknown field_mode '+field_mode)

    if type(val) is float:
        return 'FLOAT4'

    elif type(val) is int:
        if key.endswith(':completed') or key.startswith('achievement:'):
            return 'INT4' # counters
        elif key.endswith('_time') or key == 'time_in_game' or ('time_reacquired' in key) or ('stolen' in key) or ('harvested' in key) or ('looted' in key) or key.startswith('peak_'):
            return 'INT8' # times / big resource amounts
        elif key.startswith('likes_') or key.startswith('returned_'):
            return 'TINYINT(1)' # booleans
        elif key.endswith('_level') or key.endswith('_level_started') or ('_townhall_L' in key):
            return 'INT1' # level numbers
        elif key.endswith('_concurrency'):
            return 'INT1' # build/upgrade/manufacture concurrency
        elif key.endswith('_num') and key[:-4] in gamedata['buildings']:
            return 'INT2' if key[:-4] == 'barrier' else 'INT1' # building quantities
        else:
            return 'INT4'

    elif type(val) in (str, unicode):
        if key == 'country_tier': return 'CHAR(1)'
        elif key == 'country': return 'CHAR(2)'
        elif key == 'frame_platform': return 'CHAR(2)'
        elif key == 'gender': return 'VARCHAR(7)'
        elif key == 'currency': return 'VARCHAR(8)'
        elif key == 'locale': return 'VARCHAR(8)'
        elif key == 'timezone': return 'INT4'
        elif key == 'facebook_id': return 'VARCHAR(24)'
        elif key == 'acquisition_campaign': return 'VARCHAR(64)'
        elif key == 'tutorial_state': return 'VARCHAR(32)'
        elif key == 'browser_os': return 'VARCHAR(16)'
        elif key == 'browser_name': return 'VARCHAR(16)'
        elif key == 'birthday': return 'VARCHAR(10)'
        else: return 'VARCHAR(128)'

    else: # not a recognized data type
        return None

def open_cache(game_id, info = None):
    bucket, name = SpinConfig.upcache_s3_location(game_id)
    return SpinUpcacheIO.S3Reader(SpinS3.S3(SpinConfig.aws_key_file()), bucket, name, verbose=False, info=info)

def do_slave(input):
    cache = open_cache(input['game_id'], input['cache_info'])
    batch = 0
    total = 0

    gamedata = SpinJSON.load(open(SpinConfig.gamedata_filename(override_game_id = input['game_id'])))
    gamedata['ai_bases'] = SpinJSON.load(open(SpinConfig.gamedata_component_filename('ai_bases_compiled.json', override_game_id = input['game_id'])))
    gamedata['loot_tables'] = SpinJSON.load(open(SpinConfig.gamedata_component_filename('loot_tables.json', override_game_id = input['game_id'])))

    if input['mode'] == 'get_fields':
        fields = {'money_spent': 'FLOAT4', # force this column into existence because analytics_views.sql depends on it
                  'account_creation_time': 'INT8', # same here
                  'country_tier': 'CHAR(1)', 'country': 'CHAR(2)',
                  'acquisition_campaign': 'VARCHAR(64)',
                  'acquisition_ad_skynet': 'VARCHAR(128)',

                  # these fields are extracted from compound objects inside of "user"
                  'connection_method': 'VARCHAR(32)',
                  'last_ping': 'FLOAT4',
                  'last_direct_ssl_ping': 'FLOAT4',
                  'playfield_speed': 'INT2',
                  }
        for user in cache.iter_segment(input['segnum']):
            for key, val in user.iteritems():
                if key not in fields:
                    field = setup_field(gamedata, key, val, field_mode = input['field_mode'])
                    if field is not None:
                        fields[key] = field
            batch += 1
            total += 1
            if batch >= 1000:
                batch = 0
                if input['verbose']: print >> sys.stderr, 'seg', input['segnum'], 'user', total
        return fields

    elif input['mode'] == 'get_rows':
        sql_util = SpinSQLUtil.MySQLUtil()
        if not input['verbose']: sql_util.disable_warnings()
        sorted_field_names = input['sorted_field_names']
        cfg = input['dbconfig']
        con = MySQLdb.connect(*cfg['connect_args'], **cfg['connect_kwargs'])
        cur = con.cursor()

        # buffer up keyvals to be updated in the achievement tables
        upgrade_achievement_counters = {}

        def flush():
            con.commit() # commit other tables first

            # MySQL often throws deadlock exceptions when doing upserts that reference existing rows (!)
            # in the upgrade_achievements table, so we need to loop on committing these updates
            deadlocks = 0

            while True:
                try:
                    cur.executemany("INSERT INTO "+sql_util.sym(input['upgrade_achievement_table']) + \
                                    " (" + ','.join([x[0] for x in sql_util.summary_out_dimensions()]) + ", kind, spec, level, is_maxed, num_players) " + \
                                    " VALUES (" + ','.join(['%s'] * len(sql_util.summary_out_dimensions())) + ", %s, %s, %s, %s, %s) " + \
                                    " ON DUPLICATE KEY UPDATE num_players = num_players + %s",
                                    [k + (v,v) for k,v in upgrade_achievement_counters.iteritems()])
                    con.commit()
                    upgrade_achievement_counters.clear()
                    break
                except MySQLdb.OperationalError as e:
                    if e.args[0] == 1213: # deadlock
                        con.rollback()
                        deadlocks += 1
                        continue
                    else:
                        raise

            if input['verbose']: print >> sys.stderr, 'seg', input['segnum'], total, 'flushed', deadlocks, 'deadlocks'

        for user in cache.iter_segment(input['segnum']):
            user_id = user['user_id']
            keys = [x for x in sorted_field_names if x in user]
            values = [user[x] for x in keys]

            # manual parsing of sprobe fields
            if ('last_sprobe_result' in user):
                connection_method = None
                if ('connection' in user['last_sprobe_result']['tests']):
                    connection_method = user['last_sprobe_result']['tests']['connection'].get('method',None)
                    if connection_method:
                        keys.append('connection_method')
                        values.append(connection_method)
                        if (connection_method in user['last_sprobe_result']['tests']) and ('ping' in user['last_sprobe_result']['tests'][connection_method]):
                            keys.append('last_ping')
                            values.append(user['last_sprobe_result']['tests'][connection_method]['ping'])

                if ('direct_ssl' in user['last_sprobe_result']['tests']) and ('ping' in user['last_sprobe_result']['tests']['direct_ssl']):
                    keys.append('last_direct_ssl_ping')
                    values.append(user['last_sprobe_result']['tests']['direct_ssl']['ping'])

            # manual parsing of other compound fields
            prefs = user.get('player_preferences', None)
            if prefs:
                if 'playfield_speed' in prefs:
                    keys.append('playfield_speed')
                    values.append(prefs['playfield_speed'])

            cur.execute("INSERT INTO " + input['upcache_table'] + \
                        "(user_id, "+', '.join(['`'+x+'`' for x in keys])+")"+ \
                        " VALUES (%s, "+', '.join(['%s'] * len(values)) +")",
                        [user_id,] + values)

            # we need the summary dimensions for achievement tables
            summary_keyvals = [('frame_platform', user.get('frame_platform',None)),
                               ('country_tier', str(user['country_tier']) if user.get('country_tier',None) else None),
                               ('townhall_level', user.get(gamedata['townhall']+'_level',1)),
                               ('spend_bracket', sql_util.get_spend_bracket(user.get('money_spent',0)))]

            # parse townhall progression
            if input['do_townhall'] and ('account_creation_time' in user):
                ts_key = gamedata['townhall']+'_level_at_time'
                if ts_key in user:
                    cur.executemany("INSERT INTO " +sql_util.sym(input['townhall_table']) + \
                                    " (user_id,townhall_level,time) VALUES (%s,%s,%s) ON DUPLICATE KEY UPDATE user_id=user_id;",
                                    [(user['user_id'], level, user['account_creation_time'] + int(sage)) for sage, level in user[ts_key].iteritems()]
                                    )

            # parse tech unlock timing
            if input['do_tech']:
                cur.executemany("INSERT INTO "+sql_util.sym(input['tech_table']) + " (user_id, tech_name, level, time) VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE user_id=user_id;",
                                [(user['user_id'], tech, level, user['account_creation_time'] + int(sage)) \
                                 for tech in gamedata['tech'] \
                                 for sage, level in user.get('tech:'+tech+'_at_time', {}).iteritems()
                                 ])

                # summary dimensions, kind, spec, level, is_maxed
                for spec, level in user.get('tech',{}).iteritems():
                    if spec in gamedata['tech']:
                        is_maxed = 1 if (len(gamedata['tech'][spec]['research_time']) > 1 and level >= len(gamedata['tech'][spec]['research_time'])) else 0
                        k = tuple(x[1] for x in summary_keyvals) + ('tech', spec, level, is_maxed)
                        upgrade_achievement_counters[k] = upgrade_achievement_counters.get(k,0) + 1
                        if is_maxed:
                            # one row for "any" maxed tech
                            km = tuple(x[1] for x in summary_keyvals) + ('tech', 'ANY', None, 1)
                            upgrade_achievement_counters[km] = upgrade_achievement_counters.get(km,0) + 1

            # parse building upgrade timing
            if input['do_buildings']:
                cur.executemany("INSERT INTO "+sql_util.sym(input['buildings_table']) + " (user_id, building, max_level, time) VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE user_id=user_id;",
                                [(user['user_id'], building, level, user['account_creation_time'] + int(sage)) \
                                 for building in gamedata['buildings'] \
                                 for sage, level in user.get(building+'_level_at_time', user.get('building:'+building+':max_level_at_time', {})).iteritems()
                                 ])

                # summary dimensions, kind, spec, level, is_maxed
                for spec in gamedata['buildings']:
                    level = max(user.get('building:'+spec+':max_level_at_time',{'asdf':0}).itervalues())
                    if level >= 1:
                        is_maxed = 1 if (len(gamedata['buildings'][spec]['build_time']) > 1 and level >= len(gamedata['buildings'][spec]['build_time'])) else 0
                        k = tuple(x[1] for x in summary_keyvals) + ('building', spec, level, is_maxed)
                        upgrade_achievement_counters[k] = upgrade_achievement_counters.get(k,0) + 1
                        if is_maxed:
                            # one row for "any" maxed building
                            km = tuple(x[1] for x in summary_keyvals) + ('building', 'ANY', None, 1)
                            upgrade_achievement_counters[km] = upgrade_achievement_counters.get(km,0) + 1

            # parse sessions
            if input['do_sessions'] and ('sessions' in user):
                cur.executemany("INSERT INTO "+sql_util.sym(input['sessions_table']) + " (user_id,start,end,frame_platform,country_tier,townhall_level,prev_receipts) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                                [(user['user_id'], s[0], s[1], user.get('frame_platform','fb'), user.get('country_tier',None),
                                  SpinUpcache.building_level_at_age(user, gamedata['townhall'], s[1] - user['account_creation_time']),
                                  SpinUpcache.receipts_at_age(user, s[1] - user['account_creation_time'])) for s in user['sessions'] if (s[0] > 0 and s[1] > 0 and s[1]>=s[0])])

            # parse activity
            ACTIVITY_MIN_CC_LEVEL = 5 # only record for CCL5+ players (same as ANALYTICS2)
            # note! the source data, from gameserver, omits gamebucks_spent for players who never paid. This is by design to reduce bloat.

            if input['do_activity'] and ('activity' in user) and ('account_creation_time' in user) and user.get(gamedata['townhall']+'_level',1) >= ACTIVITY_MIN_CC_LEVEL:
                def parse_activity(user, stime, data):
                    ntime = long(stime)
                    age = ntime - user['account_creation_time']
                    cc_level = SpinUpcache.building_level_at_age(user, gamedata['townhall'], age)
                    if cc_level < ACTIVITY_MIN_CC_LEVEL: return None
                    act = SpinUpcache.classify_activity(gamedata, data)
                    return (user['user_id'], ntime, act['state'], act.get('ai_tag', None) or act.get('ai_ui_name', None), data.get('gamebucks_spent',None), data.get('money_spent',None),
                            user.get('frame_platform','fb'), user.get('country_tier',None), cc_level, SpinUpcache.receipts_at_age(user, age))

                cur.executemany("INSERT INTO "+sql_util.sym(input['activity_table']) + \
                                " (user_id, time, state, ai_ui_name, gamebucks_spent, receipts, frame_platform, country_tier, townhall_level, prev_receipts)" + \
                                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                filter(lambda x: x is not None, (parse_activity(user, stime, data) for stime, data in user['activity'].iteritems() if data['state'] not in ('idle','harvest'))))

            # update LTV estimate
            if input['do_ltv']:
                ltv_est = SkynetLTV.ltv_estimate(input['game_id'], gamedata, user, cache.update_time(), use_post_install_data = 9999999)
                if ltv_est is not None:
                    cur.execute("INSERT INTO "+sql_util.sym(input['ltv_table']) + " (user_id, est_90d) VALUES (%s,%s) ON DUPLICATE KEY UPDATE user_id=user_id;",
                                (user['user_id'], ltv_est))

            batch += 1
            total += 1
            if input['commit_interval'] > 0 and batch >= input['commit_interval']:
                batch = 0
                flush()

        # flush last commits
        flush()

# main program
if __name__ == '__main__':
    if '--slave' in sys.argv:
        SpinParallel.slave(do_slave)
        sys.exit(0)

    game_id = SpinConfig.game()
    commit_interval = 1000
    verbose = True
    parallel = 1
    field_mode = 'ALL'
    do_townhall = True
    do_tech = True
    do_buildings = True
    do_sessions = False # taken over by sessions_to_sql.py
    do_activity = False # taken over by activity_to_sql.py
    do_ltv = True

    opts, args = getopt.gnu_getopt(sys.argv[1:], 'g:c:q', ['parallel=','lite','sessions','activity'])

    for key, val in opts:
        if key == '-g': game_id = val
        elif key == '-c': commit_interval = int(val)
        elif key == '-q': verbose = False
        elif key == '--parallel': parallel = int(val)
        elif key == '--lite':
            field_mode = 'lite'
            do_townhall = False
            do_sessions = False
            #do_tech = False
            #do_buildings = False
            do_activity = False
            #do_ltv = False
        elif key == '--activity': do_activity = True
        elif key == '--sessions': do_sessions = True

    sql_util = SpinSQLUtil.MySQLUtil()
    if not verbose: sql_util.disable_warnings()

    with SpinSingletonProcess.SingletonProcess('upcache_to_mysql-%s' % game_id):
        cache = open_cache(game_id)

        # mapping of upcache fields to MySQL
        fields = {}

        # PASS 1 - get field names
        if 1:
            tasks = [{'game_id':game_id, 'cache_info':cache.info,
                      'mode':'get_fields', 'field_mode': field_mode, 'segnum':segnum,
                      'commit_interval':commit_interval, 'verbose':verbose} for segnum in range(0, cache.num_segments())]

            if parallel <= 1:
                output = [do_slave(task) for task in tasks]
            else:
                output = SpinParallel.go(tasks, [sys.argv[0], '--slave'], on_error='continue', nprocs=parallel, verbose=False)

            # reduce
            for slave_fields in output:
                for key, val in slave_fields.iteritems():
                    if val is not None:
                        fields[key] = val

            # filter field names
            for key, val in fields.items():
                if val is None: del fields[key]
            if 'user_id' in fields: del fields['user_id'] # handled specially

            if verbose: print 'fields =', fields

        # PASS 2 - dump the rows
        if 1:
            cfg = SpinConfig.get_mysql_config(game_id+'_upcache')
            con = MySQLdb.connect(*cfg['connect_args'], **cfg['connect_kwargs'])
            cur = con.cursor()

            upcache_table = cfg['table_prefix']+game_id+'_upcache'
            if field_mode != 'ALL':
                upcache_table += '_'+field_mode
            sessions_table = cfg['table_prefix']+game_id+'_sessions'
            townhall_table = cfg['table_prefix']+game_id+'_townhall_at_time'
            tech_table = cfg['table_prefix']+game_id+'_tech_at_time'
            upgrade_achievement_table = cfg['table_prefix']+game_id+'_upgrade_achievement'
            buildings_table = cfg['table_prefix']+game_id+'_building_levels_at_time'
            activity_table = cfg['table_prefix']+game_id+'_activity_5min'
            facebook_campaign_map_table = cfg['table_prefix']+game_id+'_facebook_campaign_map'
            ltv_table = cfg['table_prefix']+game_id+'_user_ltv'

            # these are the tables that are replaced entirely each run
            atomic_tables = [upcache_table,facebook_campaign_map_table] + \
                            ([sessions_table] if do_sessions else []) + \
                            ([activity_table] if do_activity else []) + \
                            ([townhall_table] if do_townhall else []) + \
                            ([upgrade_achievement_table] if (do_tech or do_buildings) else []) + \
                            ([buildings_table] if do_buildings else []) + \
                            ([tech_table] if do_tech else []) + \
                            ([ltv_table] if do_ltv else [])

            for TABLE in atomic_tables:
                # get rid of temp tables
                cur.execute("DROP TABLE IF EXISTS "+sql_util.sym(TABLE+'_temp'))
                # ensure that non-temp version actually exists, so that the RENAME operation below will work
                sql_util.ensure_table(cur, TABLE, {'fields': [('unused','INT4')]})
            con.commit()

            # set up FACEBOOK_CAMPAIGN_MAP table
            for t in (facebook_campaign_map_table, facebook_campaign_map_table+'_temp'):
                sql_util.ensure_table(cur, t,
                                      {'fields': [('from', 'VARCHAR(64) NOT NULL PRIMARY KEY'),
                                                  ('to', 'VARCHAR(64)')]})
            sql_util.do_insert_batch(cur, facebook_campaign_map_table+'_temp',
                                     [[('from',k),('to',v)] for k,v in SpinUpcache.FACEBOOK_CAMPAIGN_MAP.iteritems()])

            sorted_field_names = sorted(fields.keys())

            sql_util.ensure_table(cur, upcache_table+'_temp', {'fields': [('user_id', 'INT4 NOT NULL PRIMARY KEY')] +
                                                                         [(key, fields[key]) for key in sorted_field_names],
                                                               'indices': {'by_account_creation_time': {'keys': [('account_creation_time','ASC')]},
                                                                           'by_last_login_time': {'keys': [('last_login_time','ASC')]}}
                                                               })

            if do_sessions: # keep in sync with sessions_to_sql.py
                sql_util.ensure_table(cur, sessions_table+'_temp',
                                      {'fields': [('user_id', 'INT4 NOT NULL'),
                                                  ('start', 'INT8 NOT NULL'),
                                                  ('end', 'INT8 NOT NULL')] + sql_util.summary_in_dimensions()}) # make index after load

            if do_activity: # keep in sync with activity_to_sql.py
                sql_util.ensure_table(cur, activity_table+'_temp',
                                      {'fields': [('user_id','INT4 NOT NULL'),
                                                  ('time','INT8 NOT NULL'),
                                                  ('gamebucks_spent','INT4'),
                                                  ('receipts','FLOAT4')] + \
                                                 sql_util.summary_in_dimensions() + \
                                                 [('state','VARCHAR(32) NOT NULL'),
                                                  ('ai_ui_name','VARCHAR(32)')]
                                       }) # make index after load

            if do_townhall:
                sql_util.ensure_table(cur, townhall_table+'_temp',
                                      {'fields': [('user_id','INT4 NOT NULL'),
                                                  ('time','INT8 NOT NULL'),
                                                  ('townhall_level','INT4 NOT NULL')]}) # make index after load

            if do_tech:
                sql_util.ensure_table(cur, tech_table+'_temp',
                                      {'fields': [('user_id','INT4 NOT NULL'),
                                                  ('level','INT4 NOT NULL'),
                                                  ('time','INT8 NOT NULL'),
                                                  ('tech_name','VARCHAR(200) NOT NULL')],
                                       # note: make index incrementally rather than all at once at the end, to avoid long lockup of the database
                                       'indices': {'lev_then_time': {'unique': False, # technically unique, but don't waste time enforcing it
                                                                     'keys': [('user_id','ASC'),('tech_name','ASC'),('level','ASC'),('time','ASC')]}}
                                       })

            if do_buildings:
                sql_util.ensure_table(cur, buildings_table+'_temp',
                                      {'fields': [('user_id','INT4 NOT NULL'),
                                                  ('max_level','INT4 NOT NULL'),
                                                  ('time','INT8 NOT NULL'),
                                                  ('building','VARCHAR(64) NOT NULL')],
                                       # note: make index incrementally rather than all at once at the end, to avoid long lockup of the database
                                       'indices': {'lev_then_time': {'unique': False, # technically unique, but don't waste time enforcing it
                                                                     'keys': [('user_id','ASC'),('building','ASC'),('max_level','ASC'),('time','ASC')]}}
                                       })

            if do_tech or do_buildings:
                sql_util.ensure_table(cur, upgrade_achievement_table+'_temp', achievement_table_schema(sql_util))

            if do_ltv:
                sql_util.ensure_table(cur, ltv_table+'_temp',
                                      {'fields': [('user_id','INT4 NOT NULL PRIMARY KEY'),
                                                  ('est_90d','FLOAT4 NOT NULL')]})

            con.commit()

            try:
                tasks = [{'game_id':game_id, 'cache_info':cache.info, 'dbconfig':cfg,
                          'do_townhall': do_townhall, 'do_sessions': do_sessions, 'do_tech': do_tech, 'do_buildings': do_buildings, 'do_activity': do_activity,
                          'do_ltv': do_ltv, 'ltv_table': ltv_table+'_temp',
                          'upcache_table': upcache_table+'_temp',
                          'sessions_table': sessions_table+'_temp',
                          'townhall_table': townhall_table+'_temp',
                          'tech_table': tech_table+'_temp',
                          'upgrade_achievement_table': upgrade_achievement_table+'_temp',
                          'buildings_table': buildings_table+'_temp',
                          'activity_table': activity_table+'_temp',
                          'mode':'get_rows', 'sorted_field_names':sorted_field_names,
                          'segnum':segnum,
                          'commit_interval':commit_interval, 'verbose':verbose} for segnum in range(0, cache.num_segments())]

                if parallel <= 1:
                    output = [do_slave(task) for task in tasks]
                else:
                    output = SpinParallel.go(tasks, [sys.argv[0], '--slave'], on_error='continue', nprocs=parallel, verbose=False)

            except:
                for TABLE in atomic_tables:
                    cur.execute("DROP TABLE IF EXISTS "+sql_util.sym(TABLE+'_temp'))
                con.commit()
                raise

            if verbose: print 'inserts done'

            if verbose: print 'replacing old tables'
            for TABLE in atomic_tables:
                # t -> t_old, t_temp -> t
                cur.execute("RENAME TABLE "+\
                            sql_util.sym(TABLE)+" TO "+sql_util.sym(TABLE+'_old')+","+\
                            sql_util.sym(TABLE+'_temp')+" TO "+sql_util.sym(TABLE))
                con.commit()

                # kill t_old
                cur.execute("DROP TABLE "+sql_util.sym(TABLE+'_old'))
                con.commit()

            # created incrementally now
            #if verbose: print 'building indices for', upcache_table
            #cur.execute("ALTER TABLE "+sql_util.sym(upcache_table)+" ADD INDEX by_account_creation_time (account_creation_time), ADD INDEX by_last_login_time (last_login_time)")

            if do_sessions:
                if verbose: print 'building indices for', sessions_table
                cur.execute("ALTER TABLE "+sql_util.sym(sessions_table)+" ADD INDEX by_start (start), ADD INDEX by_user_start (user_id, start)")
            if do_activity:
                if verbose: print 'building indices for', activity_table
                cur.execute("ALTER TABLE "+sql_util.sym(activity_table)+" ADD INDEX by_time (time)")
            if do_townhall:
                if verbose: print 'building indices for', townhall_table
                cur.execute("ALTER TABLE "+sql_util.sym(townhall_table)+" ADD INDEX ts (user_id, townhall_level, time), ADD INDEX ts2 (user_id, time, townhall_level)")

            if do_tech:
                pass # note: index is created incrementally now, to avoid long lockup of database
                #if verbose: print 'building indices for', tech_table
                #cur.execute("ALTER TABLE "+sql_util.sym(tech_table)+" ADD INDEX lev_then_time (user_id, tech_name, level, time)")
            if do_buildings:
                pass # note: index is created incrementally now, to avoid long lockup of database
                #if verbose: print 'building indices for', buildings_table
                #cur.execute("ALTER TABLE "+sql_util.sym(buildings_table)+" ADD INDEX lev_then_time (user_id, building, max_level, time)")

            con.commit()

            if verbose: print 'all done.'
