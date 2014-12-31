#!/usr/bin/env python

# Copyright (c) 2014 SpinPunch. All rights reserved.
# Use of this source code is governed by an MIT-style license that can be
# found in the LICENSE file.

# dump "fb_conversion_pixels" log events from MongoDB (or S3) to a MySQL database for analytics
# this runs once per game title, from the game analytics sandbox (like archive_mongodb_logs.py)

import sys, time, calendar, getopt, tempfile, subprocess, traceback
import SpinConfig
import SpinJSON
import SpinS3
import SpinUserDB
import SpinNoSQL
import SpinNoSQLId
import SpinSQLUtil
import SkynetLib
import MySQLdb

id_generator = SpinNoSQLId.Generator()

time_now = int(time.time())

conversions_schema = {
    'fields': [('_id', 'CHAR(24) NOT NULL PRIMARY KEY'),
               ('time', 'INT8 NOT NULL'),
               ('user_id', 'INT4 NOT NULL'),
               ('account_creation_time', 'INT8 NOT NULL'),
               ('kpi', 'VARCHAR(255) NOT NULL'),
               ('usd_receipts_cents', 'INT4')
               ] + SkynetLib.get_tgt_fields_for_sql(),
    'indices': {'by_time': {'keys': [('time','ASC')]},
                'by_account_creation_time': {'keys': [('account_creation_time','ASC')]},
                }
    }

# get missing denormalized fields by pulling userdb files
user_cache = {}
def get_user_data(user_id, verbose):
    if user_id in user_cache:
        return user_cache[user_id]
    #if verbose: print 'pulling user', user_id
    data = SpinJSON.loads(SpinUserDB.driver.sync_download_user(user_id))
    ret = user_cache[user_id] = {
        'account_creation_time': data['account_creation_time'],
        'acquisition_ad_skynet': data.get('acquisition_ad_skynet',None)
        }
    return ret

def iterate_from_mongodb(game_id, start_time, end_time):
    nosql_client = SpinNoSQL.NoSQLClient(SpinConfig.get_mongodb_config(game_id))
    end_time = min(end_time, time_now - 12*3600) # don't grab data that's *too* recent because the userdb files may be unavailable
    qs = {'time': {'$gt': start_time, '$lt': end_time}}

    for row in nosql_client.log_buffer_table('log_fb_conversion_pixels').find(qs):
        row['_id'] = nosql_client.decode_object_id(row['_id'])
        yield row

def iterate_from_s3(game_id, start_time, end_time):
    assert start_time > 0

    s3 = SpinS3.S3(SpinConfig.aws_key_file())
    bucket = 'spinpunch-logs'
    last_id_time = -1
    id_serial = 0

    for t in xrange(86400*(start_time//86400), 86400*(end_time//86400), 86400): # for each day
        y, m, d = SpinConfig.unix_to_cal(t)
        for entry in s3.list_bucket(bucket, prefix='%04d%02d/%s-%04d%02d%02d-fb_conversion_pixels.json' % (y, m, SpinConfig.game_id_long(override_game_id=game_id), y,m,d)):
            filename = entry['name'].split('/')[-1]
            if verbose: print 'reading', filename

            if entry['name'].endswith('.zip'):
                tf = tempfile.NamedTemporaryFile(prefix='skynet_conversion_pixels_to_sql-'+filename, suffix='.zip')
                s3.get_file(bucket, entry['name'], tf.name)
                unzipper = subprocess.Popen(['unzip', '-q', '-p', tf.name],
                                            stdout = subprocess.PIPE)
            else:
                raise Exception('unhandled file extension: '+entry['name'])

            for line in unzipper.stdout.xreadlines():
                row = SpinJSON.loads(line)
                if row['time'] < start_time: continue # skip ahead
                elif row['time'] > end_time: break

                if '_id' not in row:
                    # synthesize a fake MongoDB row ID
                    if row['time'] != last_id_time:
                        last_id_time = row['time']
                        id_serial = 0
                    fake_pid = {'mf':0, 'tr':1, 'mf2':2, 'dv':3, 'gg':4, 'bfm':5, 'sg':6}[game_id] # to protect against same-time collisions
                    row['_id'] = SpinNoSQLId.creation_time_id(row['time'], pid = fake_pid, serial = id_serial)
                    assert SpinNoSQLId.is_valid(row['_id'])
                    id_serial += 1

                # note: there's a small chance this could end up duplicating an event at the boundary of an S3 import and MongoDB import

                yield row

if __name__ == '__main__':
    game_id = SpinConfig.game()
    commit_interval = 1000
    verbose = True
    dry_run = False
    source = 'mongodb'

    opts, args = getopt.gnu_getopt(sys.argv[1:], 'g:c:q', ['dry-run','source='])

    for key, val in opts:
        if key == '-g': game_id = val
        elif key == '-c': commit_interval = int(val)
        elif key == '-q': verbose = False
        elif key == '--dry-run': dry_run = True
        elif key == '--source':
            assert val in ('mongodb', 's3')
            source = val

    sql_util = SpinSQLUtil.MySQLUtil()
    if not verbose: sql_util.disable_warnings()

    cfg = SpinConfig.get_mysql_config('skynet')
    con = MySQLdb.connect(*cfg['connect_args'], **cfg['connect_kwargs'])
    conversions_table = cfg['table_prefix']+'conversions'

    cur = con.cursor(MySQLdb.cursors.DictCursor)
    if not dry_run:
        sql_util.ensure_table(cur, conversions_table, conversions_schema)
    con.commit()

    # find most recent already-converted action
    start_time = calendar.timegm([2013,9,1,0,0,0]) # this is about when we started recording this data
    end_time = time_now - 60 # don't get too close to "now" since we assume if we have one event for a given second, we have them all

    if not dry_run:
        cur.execute("SELECT time FROM "+sql_util.sym(conversions_table)+" WHERE tgt_game = %s ORDER BY time DESC LIMIT 1", game_id)
        row = cur.fetchone()
        con.commit()
    else:
        row = None
    if row:
        start_time = row['time']

    if verbose:  print 'start_time', start_time, 'end_time', end_time

    batch = 0
    total = 0

    if source == 'mongodb':
        iter = iterate_from_mongodb(game_id, start_time, end_time)
    elif source == 's3':
        iter = iterate_from_s3(game_id, start_time, end_time)

    for row in iter:
        try:
            if row.get('event_name',None) == '7500_adnetwork_context_attached' or \
               row['kpi'] == 'context_attached': continue # ignore these

            keyvals = [('_id',row['_id']),
                       ('time',row['time']),
                       ('user_id',row['user_id']),
                       ('kpi',row['kpi'])]
            if 'post_fbtax_dollars' in row:
                keyvals.append(('usd_receipts_cents', int(100*row['post_fbtax_dollars'])))

            need_user = ('account_creation_time' not in row) or \
                        (('dtgt' not in row) and \
                         ((type(row['context']) is int) or \
                          (len(row['context']) < 5)))

            if need_user:
                user = get_user_data(row['user_id'], verbose)
            else:
                user = None

            if 'account_creation_time' in row:
                keyvals.append(('account_creation_time', row['account_creation_time']))
            else:
                keyvals.append(('account_creation_time', user['account_creation_time']))

            if 'dtgt' in row: # best - get dtgt from the log
                dtgt = row['dtgt']
            elif ('context' in row) and (type(row['context']) is not int) and len(row['context']) >= 5: # second best - parse context
                dtgt = SkynetLib.stgt_to_dtgt(row['context'])
            elif user['acquisition_ad_skynet']: # fallback - get from user
                dtgt = SkynetLib.stgt_to_dtgt(user['acquisition_ad_skynet'])
            else:
                print 'unable to get dtgt for user', row['user_id']
                continue

            assert dtgt and len(dtgt) > 2
            if 'a' not in dtgt: # fill in missing game for legacy ads
                assert game_id == 'tr'
                dtgt['a'] = 'tr'

            for k, v in dtgt.iteritems():
                full_name, full_val = SkynetLib.decode_one_param(SkynetLib.standin_spin_params, k+v)
                short_val = SkynetLib.encode_one_param(SkynetLib.standin_spin_params, full_name, full_val)[1:]
                keyvals.append(('tgt_%s' % full_name, short_val))

        except KeyboardInterrupt:
            break

        except:
            print traceback.format_exc()
            print 'problem row:', row
            continue

        if not dry_run:
            sql_util.do_insert(cur, conversions_table, keyvals)

        batch += 1
        total += 1
        if commit_interval > 0 and batch >= commit_interval:
            batch = 0
            con.commit()
            if verbose: print total, 'inserted'

    con.commit()
    if verbose: print 'total', total, 'inserted'
