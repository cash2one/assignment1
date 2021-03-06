#!/usr/bin/env python

# Copyright (c) 2015 SpinPunch. All rights reserved.
# Use of this source code is governed by an MIT-style license that can be
# found in the LICENSE file.

# MongoDB adaptor API

import time, sys, re, itertools, random
import datetime
import pymongo, bson
import SpinConfig
import SpinNoSQLId

# to check if remove() modified anything:
#        return .remove()['n'] > 0

# NOTE! when doing update(upsert=True) on a collection with manually-specified _id keys,
# you MUST use the '$set':{} operator in the update and not just supply the fields
# (otherwise MongoDB replaces _id with a server-generated one).

# connect to a MongoDB service (possibly multiple databases within it)
class NoSQLService (object):
    def __init__(self, host, port, username, password):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.con = None
        self.seen_dbnames = None
    def connect(self):
        if not self.con:
            self.con = pymongo.MongoClient(host='mongodb://%s:%d' % (self.host, self.port), max_pool_size=1)
            # only use 1 socket since we never need concurrent requests, and it will mess up auth since
            # authenticate() does not update other outstanding sockets.
            self.seen_dbnames = {}
    def get_db(self, dbname):
        assert self.con
        db = self.con[dbname]
        assert db
        if dbname not in self.seen_dbnames:
            db.authenticate(self.username, self.password)
        return db
    def shutdown(self):
        if self.con:
            self.con.close()
            self.con = None
            self.seen_dbnames = None
    # reconnect to get around timeout of idle connection
    def ping(self):
        if (not self.con) or (not self.con.alive()):
            # reconnect
            self.con = None
            self.seen_dbnames = None
            self.connect()
            return True
        return False

# share connections to different databases within the same MongoDB service
class NoSQLServicePool (object):
    def __init__(self):
        self.pool = {}
    def get_service(self, dbconfig):
        host = dbconfig['host']
        port = dbconfig['port']
        username = dbconfig['username']
        password = dbconfig['password']
        key = (host, port, username, password) # if db sharing does not turn out to work, just add dbconfig['dbname'] to the key
        if key not in self.pool:
            self.pool[key] = NoSQLService(host, port, username, password)
        return self.pool[key]

nosql_service_pool = NoSQLServicePool()

# one database within the MongoDB service
class NoSQLDatabase (object):
    def __init__(self, dbconfig, ident):
        self.dbconfig = dbconfig
        self.ident = ident
        self.con = None
        self.db = None
    def connect(self):
        assert self.con is None
        con = nosql_service_pool.get_service(self.dbconfig)
        con.connect()
        self.con = con # do this separately so if connect() throws, then self.con remains None
        self.db = self.con.get_db(self.dbconfig['dbname'])
        assert self.db
    def shutdown(self):
        if self.con:
            self.con.shutdown()
            self.con = None
            self.db = None
    def ping(self):
        if self.con:
            ret = self.con.ping()
            # re-acquire db in case we reconnected
            self.db = self.con.get_db(self.dbconfig['dbname'])
            return ret
        return False
    def table(self, name):
        if not self.con: self.connect()
        assert self.db
        try:
            return self.db[self.dbconfig['table_prefix']+name]
        except TypeError:
            raise pymongo.errors.AutoReconnect('NoSQLDatabase.table(): db became null after connect()')
    def table_exists(self, name):
        if not self.con: self.connect()
        return (self.dbconfig['table_prefix']+name) in self.db.collection_names()
    def table_names(self):
        if not self.con: self.connect()
        for name in self.db.collection_names():
            if name.startswith(self.dbconfig['table_prefix']):
                name = name[len(self.dbconfig['table_prefix']):]
            yield name

class NoSQLClient (object):
    @classmethod
    def encode_object_id(cls, sid):
        sid = str(sid)
        assert len(sid) == 24
        return bson.objectid.ObjectId(sid)
    @classmethod
    def decode_object_id(cls, oid):
        ret = str(oid)
        assert len(ret) == 24
        return ret

    # must match dbserver.py and server.py definitions of these lock states
    LOCK_OPEN = 0
    LOCK_LOGGED_IN = 1 # not used anymore
    LOCK_BEING_ATTACKED = 2

    LOCK_TIMEOUT = 600 # seconds after which a lock is considered stale and may be busted

    # how long to keep old lock generation counters around - this should be longer than
    # the longest period of time a server could reasonably have stale (spied but not locked) player/base data
    LOCK_GEN_TIME = 6*3600

    # XXXXXX temp - for compatibility with SpinSQL
    SCORE_FREQ_SEASON = 'season'
    SCORE_FREQ_WEEKLY = 'week'
    SCORE_RE = re.compile('(.+)_(wk|s)([0-9]+)$')

    ROLE_DEFAULT = 0
    ROLE_LEADER = 4

    def __init__(self, dbconfig, map_update_hook = None, latency_func = None, identity = 'unknown', log_exception_func = None,
                 max_retries = 10):
        self.dbconfig = dbconfig
        self.map_update_hook = map_update_hook
        self.latency_func = latency_func
        if log_exception_func is None:
            log_exception_func = lambda x: sys.stderr.write(x+'\n')
        self.log_exception_func = log_exception_func
        self.in_log_exception_func = False # flag to protect against infinite recursion
        self.ident = identity
        self.max_retries = max_retries
        self.seen_server_status = False
        self.seen_server_latency = False
        self.seen_visitors = False
        self.seen_sessions = False
        self.seen_client_perf = False
        self.seen_chat = False
        self.seen_logs = {}
        self.seen_battles = False
        self.seen_dau = {}
        self.seen_player_cache = False
        self.seen_player_cache_indexes = set()
        self.seen_facebook_ids = False
        self.seen_messages = False
        self.seen_regions = {}
        self.seen_alliances = False
        self.seen_turf = False
        self.seen_player_scores = False
        self.seen_alliance_score_cache = False
        self.time = 0 # need to be updated by caller!
        self.slaves = {}
        self.connect()

    def set_time(self, newtime): self.time = newtime

    def log_exception(self, msg):
        try:
            # protect against infinite recursion - since exception logs now also go through MongoDB!
            self.in_log_exception_func = True
            self.log_exception_func('SpinNoSQL(%s): %s' % (self.ident, msg))
        finally:
            self.in_log_exception_func = False

    def connect(self):
        assert len(self.slaves) == 0
        self.slaves['DEFAULT'] = NoSQLDatabase(self.dbconfig, self.ident)

    def shutdown(self):
        for name, slave in self.slaves.items():
            slave.shutdown()
            del self.slaves[name]

    def ping_connections(self):
        [slave.ping() for slave in self.slaves.itervalues()]

    def update_dbconfig(self, new_config):
        # note, this does NOT mess with any existing connections, it is only for adding new delegated tables on the fly
        self.dbconfig = new_config

    def slave_for_table(self, table_name):
        if table_name not in self.slaves:
            self.slaves[table_name] = self.slaves['DEFAULT']
            for delegate_re, delegate_name in self.dbconfig.get('delegate_tables',{}).iteritems():
                if re.compile(delegate_re).match(table_name):
                    self.slaves[table_name] = NoSQLDatabase(SpinConfig.get_mongodb_config(delegate_name), self.ident+':'+delegate_name)
                    break
        return self.slaves[table_name]

    def _table(self, name): return self.slave_for_table(name).table(name)
    def _table_exists(self, name): return self.slave_for_table(name).table_exists(name)

    def instrument(self, name, func, args, kwargs = {}):
        if self.latency_func: start_time = time.time()
        needs_ping = False
        attempt = 0
        last_exc = None

        while True:
            try:
                if needs_ping:
                    self.ping_connections()
                    needs_ping = False
                ret = func(*args, **kwargs)
                if attempt > 0 and (not self.in_log_exception_func): self.log_exception('recovered from exception.')
                break
            except pymongo.errors.AutoReconnect as e: # on line 95
                # attempt to reconnect and try a second time
                if self.in_log_exception_func: return None # fail silently
                self.log_exception('AutoReconnect exception, retrying...')
                needs_ping = True
                last_exc = e
                time.sleep(1)
            except pymongo.errors.ConnectionFailure as e:
                if self.in_log_exception_func: return None # fail silently
                self.log_exception('ConnectionFailure exception, retrying...')
                last_exc = e
                time.sleep(10)
            except:
                raise

            attempt += 1
            if self.max_retries >= 0 and attempt >= self.max_retries:
                raise Exception('too many MongoDB connection errors, last one was: %s' % repr(last_exc)) # give up

        if self.latency_func:
            end_time = time.time()
            elapsed = end_time - start_time
            self.latency_func('MongoDB:'+name, elapsed)
            self.latency_func('MongoDB:ALL', elapsed)
        return ret

    ###### GLOBAL SETTINGS #######

    # enforce leadership invariant.
    # returns: 0 if no problem. -1 if alliance needed to be disbanded. Otherwise, user_id of new leader.
    def do_maint_fix_leadership_problem(self, alliance_id, verbose = True):
        if verbose: print 'Fixing alliance leadership problem on alliance', alliance_id, '...'
        memberships = list(self.alliance_table('alliance_members').find({'alliance_id':alliance_id}))
        if len(memberships) < 1: # maint code doesn't get here
            if verbose: print '  Alliance', alliance_id, 'has no members left. Disbanding.'
            self.delete_alliance(alliance_id)
            return -1
        pcache_info = self.player_cache_lookup_batch([x['_id'] for x in memberships], fields = ['player_level'])
        pcache = dict((x['user_id'], x) for x in pcache_info if x)

        # sort by "pecking order"
        def member_sort_key(a, pcache):
            # role > player_level > (lower) join_time
            return (a.get('role',self.ROLE_DEFAULT), pcache.get(a['_id'],{}).get('player_level',0), -a.get('join_time',0))

        memberships.sort(key = lambda a,_pcache=pcache: member_sort_key(a,_pcache), reverse = True)

        leaders = []
        for entry in memberships:
            if entry.get('role', self.ROLE_DEFAULT) == self.ROLE_LEADER:
                leaders.append(entry)

        leaders.sort(key = lambda a,_pcache=pcache: member_sort_key(a,_pcache), reverse = True)

        if len(leaders) < 1: # no leader
            user_id = memberships[0]['_id']
            if verbose: print '  Alliance', alliance_id, 'has no leader. Elevating member', user_id, 'to leader.'
            self.alliance_table('alliances').update({'_id':alliance_id}, {'$set':{'leader_id':user_id}})
            self.alliance_table('alliance_members').update({'_id':user_id}, {'$set':{'role':self.ROLE_LEADER}})
            return user_id
        elif len(leaders) > 1: # more than one leader!
            user_id = leaders[0]['_id']
            if verbose: print '  Alliance', alliance_id, 'has multiple leaders:', repr([x['_id'] for x in leaders]), 'nominating', user_id, 'as true leader and demoting all others.'
            self.alliance_table('alliances').update({'_id':alliance_id}, {'$set':{'leader_id':user_id}})
            for pretender in leaders[1:]:
                self.alliance_table('alliance_members').update({'_id':pretender['_id']}, {'$set':{'role':self.ROLE_LEADER-1}})
            return user_id
        return 0

    def do_maint(self, time_now, cur_season, cur_week):
        print 'Busting stale player locks...'
        LOCK_IS_TAKEN = {'$and':[{'LOCK_STATE':{'$exists':True}},
                                 {'LOCK_STATE':{'$gt':0}}]}
        LOCK_IS_STALE = {'LOCK_TIME':{'$lte':self.time - self.LOCK_TIMEOUT}}
        n = self.player_cache().update({'$and':[LOCK_IS_TAKEN, LOCK_IS_STALE]},
                                       {'$unset':{'LOCK_STATE':1,'LOCK_OWNER':1,'LOCK_TIME':1,'LOCK_GENERATION':1,'LOCK_HOLDER':1}},
                                       multi=True)['n']
        if n > 0:
            print '  Busted', n, 'stale player locks'

        print 'Checking for expired player messages...'
        prune_msg_qs = {'$or': [{'expire_time': {'$gt': 0, '$lt': time_now}}]}
        FORCE_EXPIRE =  { # force expiration of stale messages
            "resource_gift": 864000, # expire gifts after 10 days
            "alliance_status_changed": 2592000,
            "donated_units": 864000,
            "i_attacked_you": 2592000 # expire battle notifications after 30 days
        }
        for msg_type, duration in FORCE_EXPIRE.iteritems():
            prune_msg_qs['$or'].append({'type':msg_type, 'time': {'$lt': time_now - duration}})
        n = self.message_table().remove(prune_msg_qs)['n']
        if n > 0:
            print '  Deleted', n, 'expired player messages'

        alliance_list = list(self.alliance_table('alliances').find({}, {'_id':1,'num_members_cache':1,'leader_id':1}))

        print 'Checking alliance member counts...'
        to_remove = []
        for entry in alliance_list:
            true_count = self.alliance_table('alliance_members').find({'alliance_id':entry['_id']}).count()
            if entry.get('num_members_cache',-1) != true_count:
                print '  Fixing alliance', entry['_id'], 'cached', entry.get('num_members_cache',-1), '-> true', true_count
                self.alliance_table('alliances').update({'_id':entry['_id']}, {'$set':{'num_members_cache':true_count}})
            if true_count < 1:
                print '  Deleting empty alliance', entry['_id']
                self.delete_alliance(entry['_id'])
                to_remove.append(entry)
        for entry in to_remove: alliance_list.remove(entry)

        alliance_dict = dict((x['_id'], x) for x in alliance_list)
        valid_alliance_ids = set((entry['_id'] for entry in alliance_list))

        print 'Checking for dangling member relationships and missing roles...'
        leadership_problems = []
        for row in list(self.alliance_table('alliance_members').find()):
            if row['alliance_id'] not in valid_alliance_ids:
                print '  Deleting dangling alliance_members row', row
                self.alliance_table('alliance_members').remove({'_id':row['_id']})
                continue
            true_role = None

            if False:
                # trust alliance leader_id
                if row['_id'] == alliance_dict[row['alliance_id']]['leader_id']:
                    true_role = self.ROLE_LEADER
                else:
                    true_role = row.get('role', self.ROLE_DEFAULT)
            else:
                # trust alliance_members role
                true_role = row.get('role', self.ROLE_DEFAULT)

            if row.get('role',self.ROLE_DEFAULT) != true_role:
                print '  Fixing role of user %d alliance %d -> %d' % (row['_id'], row['alliance_id'], true_role)
                self.alliance_table('alliance_members').update({'_id':row['_id']},{'$set':{'role':true_role}})
            if true_role == self.ROLE_LEADER:
                info = alliance_dict[row['alliance_id']]
                if 'maint_leader' in info: # more than one leader
                    leadership_problems.append(info['_id'])
                else:
                    info['maint_leader'] = row['_id']

        for info in alliance_dict.itervalues():
            if 'maint_leader' not in info: # leaderless alliance
                leadership_problems.append(info['_id'])

        for problem_id in leadership_problems:
            self.do_maint_fix_leadership_problem(problem_id)

        print 'Checking for dangling alliance role info...'
        for row in list(self.alliance_table('alliance_roles').find()):
            if row['alliance_id'] != -1 and (row['alliance_id'] not in valid_alliance_ids):
                print '  Deleting dangling alliance_roles row', row
                self.alliance_table('alliance_roles').remove({'_id':row['_id']})

        print 'Checking for dangling or stale invites/join requests...'
        for TABLE in ('alliance_invites', 'alliance_join_requests'):
            for row in list(self.alliance_table(TABLE).find()):
                if row['alliance_id'] not in valid_alliance_ids or row['expire_time'] < time_now:
                    print '  Deleting dangling or stale ', TABLE, 'row', row
                    self.alliance_table(TABLE).remove({'_id':row['_id']})

        print 'Checking for stale unit_donation_requests...'
        earliest = time_now - 24*60*60 # clear entries more than 1 day old
        n = self.unit_donation_requests_table().remove({'time':{'$lt':earliest}})['n']
        if n > 0:
            print '  Deleted', n, 'old unit_donation_requests'

        print 'Checking for old player_scores entries...'
        n = self.player_scores().remove({'$or': [ {'frequency': 'season', 'period': {'$ne': cur_season} },
                                                  {'frequency': 'week', 'period': {'$lt': cur_week-5} } ]})['n']
        if n > 0:
            print '  Deleted', n, 'old player_scores entries'

        print 'Checking for old or dangling alliance_score_cache entries...'
        n = 0
        for row in list(self.alliance_score_cache().find()):
            if row['alliance_id'] not in valid_alliance_ids or \
               (row['frequency'] == 'season' and row['period'] != cur_season) or \
               (row['frequency'] == 'week' and row['period'] < cur_week - 5):
                n += self.alliance_score_cache().remove({'_id':row['_id']})['n']
        if n > 0:
            print '  Deleted', n, 'old or dangling alliance_score_cache entries'

        print 'Dropping old DAU tables...'
        n = 0
        for tbl, timestamp in self.dau_tables():
            # get rid of tables that are more than 14 days old
            if timestamp < time_now - 14*86400:
                tbl.drop()
                n += 1
        if n > 0:
            print '  Dropped', n, 'old DAU table(s)'

    ###### SERVER STATUS ######

    def server_status_table(self):
        coll = self._table('server_status')
        if not self.seen_server_status:
            pass # no indices
            self.seen_server_status = True
        return coll

    def decode_server_status(self, status):
        TIMEOUT = 120 # if a server does not update for 2 minutes, consider it dead
        if ('server_time' in status) and (self.time - status['server_time']) >= TIMEOUT:
            self.server_status_table().remove({'_id':status['_id']}, w=0)
            return None
        status['server_name'] = status['_id']; del status['_id']
        return status
    def decode_server_status_list(self, status_list):
        for status in status_list:
            st = self.decode_server_status(status)
            if not st: continue
            yield st

    def server_status_update(self, server_name, props, reason=''):
        return self.instrument('server_status_update(%s)'%reason, self._server_status_update, (server_name, props))
    def _server_status_update(self, server_name, props):
        if props is None: # clean shutdown
            self.server_status_table().remove({'_id':server_name})
        else:
            self.server_status_table().update({'_id':server_name}, {'$set':props}, upsert = True, w=0)
    def server_status_query(self, qs={}, fields=None, sort=None, limit=None, reason=''):
        return self.instrument('server_status_query(%s)'%reason, self._server_status_query, (qs,fields,1,sort,limit))
    def server_status_query_one(self, qs={}, fields=None, reason=''):
        return self.instrument('server_status_query(%s)'%reason, self._server_status_query, (qs,fields,0,None,None))
    def _server_status_query(self, qs, fields, multi, sort, limit):
        if multi:
            cur = self.server_status_table().find(qs, fields).batch_size(999999)
            if sort == 'load':
                cur = cur.sort([('active_sessions',1)])
            if limit:
                cur = cur.limit(limit)
            # do not stream, do this all synchronously
            return list(self.decode_server_status_list(cur))
        else:
            entry = self.server_status_table().find_one(qs, fields)
            if entry:
                return self.decode_server_status(entry)
            return None

    def server_latency_table(self):
        if not self.seen_server_latency:
            if not self._table_exists('server_latency'):
                # race condition, but only used by one offline tool, so not a big deal.
                slave = self.slave_for_table('server_latency')
                # 75 bytes per record, about 120k records per day (6k/server/day) = 9MB/day
                coll = slave.db.create_collection(slave.dbconfig['table_prefix']+'server_latency', capped = True, size = 128*1024*1024)
                coll.ensure_index([('time',pymongo.ASCENDING)])
            self.seen_server_latency = True
        return self._table('server_latency')
    def server_latency_record(self, server_name, latency, reason=''):
        return self.instrument('server_latency_record(%s)'%reason, self._server_latency_record, (server_name, latency))
    def _server_latency_record(self, server_name, latency):
        self.server_latency_table().insert({'time':self.time, 'ident':server_name, 'latency':latency}, w=0)

    def client_perf_table(self):
        if not self.seen_client_perf:
            if not self._table_exists('client_perf'):
                # race condition, but only used by one offline tool, so not a big deal.
                slave = self.slave_for_table('client_perf')
                # ~512 bytes per record, ?? records per day
                coll = slave.db.create_collection(slave.dbconfig['table_prefix']+'client_perf', capped = True, size = 256*1024*1024)
                coll.ensure_index([('time',pymongo.ASCENDING)])
            self.seen_client_perf = True
        return self._table('client_perf')
    def client_perf_record(self, data, reason=''):
        return self.instrument('client_perf_record(%s)'%reason, self._client_perf_record, (data,))
    def _client_perf_record(self, data):
        data['time'] = self.time
        data['ident'] = self.ident
        self.client_perf_table().insert(data, w=0)


    ###### (PROXYSERVER) VISITORS ######

    def visitors_table(self):
        coll = self._table('visitors')
        if not self.seen_visitors:
            coll.ensure_index('last_active_time')
            self.seen_visitors = True
        return coll
    def visitors_prune(self, timeout, reason=''):
        return self.instrument('visitors_prune(%s)'%reason, self._visitors_prune, (timeout,))
    def _visitors_prune(self, timeout):
        return self.visitors_table().remove({'last_active_time':{'$lt':self.time - timeout}})['n']

    ###### (PROXYSERVER) SESSIONS ######
    # note: the primary key here is user_id, not session_id!

    def sessions_table(self):
        coll = self._table('sessions')
        if not self.seen_sessions:
            coll.ensure_index('session_id', unique=True)
            coll.ensure_index('ip')
            coll.ensure_index('social_id')
            coll.ensure_index('last_active_time')
            self.seen_sessions = True
        return coll

    def sessions_prune(self, timeout, reason=''):
        return self.instrument('sessions_prune(%s)'%reason, self._sessions_prune, (timeout,))
    def _sessions_prune(self, timeout):
        return self.sessions_table().remove({'last_active_time':{'$lt':self.time - timeout}})['n']

    def session_get_by_session_id(self, session_id, reason=''):
        return self.instrument('session_get_by_session_id(%s)'%reason, self._sessions_query_one, ({'session_id':session_id},))
    def session_get_by_user_id(self, user_id, reason=''):
        return self.instrument('session_get_by_user_id(%s)'%reason, self._sessions_query_one, ({'_id':user_id},))
    def session_get_by_social_id(self, social_id, reason=''):
        return self.instrument('session_get_by_social_id(%s)'%reason, self._sessions_query_one, ({'social_id':social_id},))
    def _sessions_query_one(self, qs):
        return self.sessions_table().find_one(qs)

    def sessions_get_users_by_ip(self, ip, reason=''):
        return [x['_id'] for x in self.instrument('sessions_get_users_by_ip(%s)'%reason, self._sessions_query, ({'ip':ip},{'_id':1}))]
    def _sessions_query(self, qs, fields):
        return list(self.sessions_table().find(qs, fields))

    def session_keepalive(self, id, reason=''):
        return self.instrument('session_keepalive(%s)'%reason, self._session_keepalive, ({'session_id':id},))
    def session_keepalive_batch(self, id_list, reason=''):
        return self.instrument('session_keepalive_batch(%s)'%reason, self._session_keepalive, ({'session_id':{'$in':id_list}},))
    def _session_keepalive(self, qs):
        self.sessions_table().update(qs, {'$set':{'last_active_time':self.time}}, multi=True, upsert=False, w=0)

    def session_drop_by_session_id(self, session_id, reason=''):
        return self.instrument('session_drop_by_session_id(%s)'%reason, self._session_drop_by_session_id, (session_id,))
    def _session_drop_by_session_id(self, session_id):
        return self.sessions_table().remove({'session_id':session_id})['n']

    # Attempt to create new session for this user_id.
    # If an existing session is in place, abort and return the old one.
    # If creation is successful, return None.
    def session_insert(self, id, user_id, social_id, ip, server_info, last_active_time, reason=''):
        return self.instrument('session_insert(%s)'%reason, self._session_insert, (id,user_id,social_id,ip,server_info,last_active_time))
    def _session_insert(self, id, user_id, social_id, ip, server_info, last_active_time):
        while True:
            try:
                self.sessions_table().insert({'_id':user_id, 'session_id':id, 'social_id':social_id, 'ip':ip,
                                              'server_info':server_info, 'last_active_time':last_active_time})
                return None
            except pymongo.errors.DuplicateKeyError:
                old_session = self.sessions_table().find_one({'_id':user_id})
                if old_session:
                    return old_session
                else:
                    continue # race, next iteration will return the blocker (or succeed)
            raise Exception('should not get here')

    ###### LOG BUFFERS ######

    def log_buffer_table(self, name):
        coll = self._table(name)
        if name not in self.seen_logs:
            coll.ensure_index([('time',pymongo.ASCENDING)])
            self.seen_logs[name] = 1
        return coll
    def log_record(self, log_name, t, data, safe = False, log_ident = True, reason=''):
        return self.instrument('log_record(%s)'%(log_name+':'+reason), self._log_record, (log_name, t, data, safe, log_ident))
    def _log_record(self, log_name, t, data, safe, log_ident):
        has_time = ('time' in data)
        has_ident = ('ident' in data)
        if not has_time: data['time'] = t
        if not has_ident and log_ident: data['ident'] = self.ident
        try:
            self.log_buffer_table(log_name).insert(data, w = 1 if safe else 0)
        finally:
            if not has_time: del data['time']
            if not has_ident and log_ident: del data['ident']

    def log_bookmark_set(self, user_name, key, ts, reason=''):
        return self.instrument('log_boomkark_set', self._log_bookmark_set, (user_name, key, ts))
    def log_bookmark_get(self, user_name, key, reason=''):
        return self.instrument('log_boomkark_get', self._log_bookmark_get, (user_name, key))
    def _log_bookmark_set(self, user_name, key, ts):
        self._table('log_bookmarks').update({'_id':user_name}, {'$set':{key: ts}}, upsert=True, multi=False)
    def _log_bookmark_get(self, user_name, key):
        row = self._table('log_bookmarks').find_one({'_id':user_name}, {'_id':0,key:1})
        if row:
            return row.get(key,None)
        return None

    # retrieve log entries either by time or ObjectID range (useful for paging)
    def log_retrieve(self, log_name, time_range = [-1,-1], id_range = [None, None], inclusive = True, code = None, reason=''):
        # convert strings to oids
        id_range = map(lambda x: self.encode_object_id(x) if x else None, id_range)
        # convert time range to ObjectID range
        time_id_range = map(lambda x: bson.objectid.ObjectId(SpinNoSQLId.creation_time_id(x)) if x >= 0 else None, time_range)
        # intersection of both ranges
        id_range[0] = time_id_range[0] if (id_range[0] is None) else max(id_range[0], time_id_range[0])
        id_range[1] = time_id_range[1] if (id_range[1] is None) else min(id_range[1], time_id_range[1])
        return self.instrument('log_retrieve(%s)'%(log_name+':'+reason), self._log_retrieve, (log_name, id_range, inclusive, code))
    def decode_log(self, x):
        if '_id' in x: x['_id'] = self.decode_object_id(x['_id']) # convert to plain strings
        return x
    def _log_retrieve(self, log_name, id_range, inclusive, code):
        qs = {}
        if id_range[0] or id_range[1]:
            qs['_id'] = {}
            if id_range[0]: qs['_id']['$gte' if inclusive else '$gt'] = id_range[0]
            if id_range[1]: qs['_id']['$lt'] = id_range[1]
        if code is not None:
            qs['code'] = code
        return (self.decode_log(x) for x in self.log_buffer_table(log_name).find(qs).sort([('_id',1)]).batch_size(999999))

    ###### DAU TABLES ######

    def dau_table_name_from_timestamp(self, t):
        st = time.gmtime(t)
        return 'dau_%04d%02d%02d' % (st.tm_year, st.tm_mon, st.tm_mday)
    def dau_table(self, t):
        name = self.dau_table_name_from_timestamp(t)
        coll = self._table(name)
        if name not in self.seen_dau:
            # no indices needed
            self.seen_dau[name] = 1
        return coll
    def dau_tables(self):
        dau_slave = self.slave_for_table('dau_00000000')
        dau_table_re = re.compile('^dau_([0-9]{8})$')
        for name in list(dau_slave.table_names()):
            match = dau_table_re.match(name)
            if match:
                ymd = match.groups()[0]
                year, month, day = int(ymd[0:4]), int(ymd[4:6]), int(ymd[6:8])
                timestamp = SpinConfig.cal_to_unix((year, month, day))
                yield dau_slave.table(name), timestamp

    def dau_record(self, t, user_id, country_tier, playtime, reason=''):
        return self.instrument('dau_record(%s)'%(reason), self._dau_record, (t, user_id, country_tier, playtime))
    def _dau_record(self, t, user_id, country_tier, playtime):
        self.dau_table(t).update({'_id':user_id}, {'$set':{'tier': int(country_tier)}, '$inc':{'playtime': playtime}}, w=0, upsert=True, multi=False)
    def dau_get(self, t, reason=''):
        return self.instrument('dau_get(%s)'%(reason), self._dau_get, (t,))
    def _dau_get(self, t):
        return self.dau_table(t).find().count()

    ###### BATTLE SUMMARIES ######

    def battles_table(self):
        coll = self._table('battles')
        if not self.seen_battles:
            coll.ensure_index([('time',pymongo.ASCENDING)])
            coll.ensure_index([('involved_players',pymongo.ASCENDING),('time',pymongo.ASCENDING)])
            self.seen_battles = True
        return coll
    def battle_record(self, summary, reason=''):
        return self.instrument('battle_record(%s)'%reason, self._battle_record, (summary,))
    def _battle_record(self, summary):
        assert 'time' in summary
        assert 'involved_players' in summary
        self.battles_table().insert(summary, manipulate=False, w=0)
        assert ('_id' not in summary)

    ###### CHAT BUFFER ######

    def chat_buffer_table(self):
        coll = self._table('chat_buffer')
        if not self.seen_chat:
            coll.ensure_index([('time',pymongo.ASCENDING)])
            coll.ensure_index([('channel',pymongo.ASCENDING),('time',pymongo.ASCENDING)])
            self.seen_chat = True
        return coll
    def chat_record(self, channel, sender, text, reason=''):
        return self.instrument('chat_record(%s)'%reason, self._chat_record, (channel, sender, text))
    def _chat_record(self, channel, sender, text):
        props = {'time':self.time,'channel':channel,'sender':sender}
        if text:
            props['text'] = unicode(text)
        self.chat_buffer_table().insert(props, w=0)
    def chat_catchup(self, channel, start_time = -1, end_time = -1, skip = 0, limit = -1, reason=''):
        return self.instrument('chat_catchup(%s)'%reason, self._chat_catchup, (channel,start_time,end_time,skip,limit))
    def _chat_catchup(self, channel, start_time, end_time, skip, limit):
        props = {'channel':channel}
        if start_time > 0 or end_time > 0:
            props['time'] = {}
            if start_time > 0: props['time']['$gte'] = start_time
            if end_time > 0: props['time']['$lt'] = end_time
        cur = self.chat_buffer_table().find(props, {'_id':0,'sender':1,'text':1,'time':1}).sort([('time',pymongo.DESCENDING)])
        if skip > 0: cur = cur.skip(skip)
        if limit > 0: cur = cur.limit(limit)
        ret = []
        for row in cur:
            # insert missing 'time' field on legacy chat messages
            if type(row['sender']) is dict and ('time' not in row['sender']):
                row['sender']['time'] = row['time']
            ret.append(row)
        ret.reverse() # oldest messages first
        return ret

    ###### SOCIAL (FACEBOOK/KONGREGATE) ID MAP ######

    # for legacy reasons, the table is called "facebook_id_map".
    # keys are either raw "10000234" Facebook IDs or "kg0000" Kongregate IDs
    def facebook_id_table(self):
        coll = self._table('facebook_id_map')
        if not self.seen_facebook_ids:
            coll.ensure_index('user_id', unique=True)
            self.seen_facebook_ids = True
            self.min_user_id = 1111 # hard-coded
        return coll

    def get_user_id_range(self):
        last_entry = list(self.facebook_id_table().find({}, {'_id':0,'user_id':1}).sort([('user_id',pymongo.DESCENDING)]).limit(1))
        return [self.min_user_id, max(self.min_user_id, last_entry[0]['user_id'] if last_entry else -1)]

    def social_id_key(self, id):
        id = str(id)
        if id.startswith('fb'):
            return id[2:]
        return id

    def facebook_id_to_spinpunch_single(self, facebook_id, intrusive, reason=''):
        return self.social_id_to_spinpunch_single('fb'+facebook_id, intrusive, reason=reason)
    def social_id_to_spinpunch_single(self, socid, intrusive, reason=''):
        return self.instrument('social_id_to_spinpunch_single(%s)'%reason, self._social_id_to_spinpunch_single, (socid, intrusive))
    def _social_id_to_spinpunch_single(self, socid, intrusive):
        socid = self.social_id_key(socid)
        tbl = self.facebook_id_table()
        row = tbl.find_one({'_id':socid})
        if row:
            return int(row['user_id'])
        elif intrusive:
            while True: # loop on insert to get next unique user_id
                last_entry = list(tbl.find({}, {'_id':0,'user_id':1}).sort([('user_id',pymongo.DESCENDING)]).limit(1))
                my_id = (last_entry[0]['user_id']+1) if last_entry else self.min_user_id
                try:
                    tbl.insert({'_id':socid, 'user_id':int(my_id)})
                    return my_id
                except pymongo.errors.DuplicateKeyError as e:
                    # E11000 duplicate key error index: trtest_dan.facebook_id_table.$user_id_1  dup key: { : 1 }
                    if e.code == 11000 and ('facebook_id_table.$user_id_' in e.args[0]):
                        # user_id race condition
                        continue
                    else:
                        # _id duplication
                        return int(tbl.find_one({'_id':socid})['user_id'])
        else:
            return -1

    def facebook_id_to_spinpunch_batch(self, fbid_list, reason=''):
        return self.social_id_to_spinpunch_batch(['fb'+str(x) for x in fbid_list], reason=reason)
    def social_id_to_spinpunch_batch(self, socid_list, reason=''):
        return self.instrument('social_id_to_spinpunch_batch(%s)'%reason, self._social_id_to_spinpunch_batch, (socid_list,))

    def _social_id_to_spinpunch_batch(self, socid_list):
        socid_list = map(self.social_id_key, socid_list)
        ret = [-1]*len(socid_list)
        rows = self.facebook_id_table().find({'_id':{'$in':socid_list}})
        for row in rows:
            ret[socid_list.index(row['_id'])] = int(row['user_id'])
        return ret


    ###### PLAYER CACHE (also embeds player locks) ######

    def player_cache(self):
        coll = self._table('player_cache')
        if not self.seen_player_cache:
            coll.ensure_index('ui_name_searchable') # help with search-by-name
            coll.ensure_index('player_level') # this is to help with the ladder rival query
            coll.ensure_index('last_mtime') # to help with get_users_modified_since()
            coll.ensure_index('account_creation_time')
            self.seen_player_cache = True
        return coll

    def player_cache_ensure_index(self, name):
        if name not in self.seen_player_cache_indexes:
            self.seen_player_cache_indexes.add(name)
            self.player_cache().ensure_index(name)

    def decode_player_cache(self, data):
        # XXX might be a lock-only stub, but how do we know? Will the client barf if it gets an empty entry?
        #if 'facebook_id' not in data: return None
        data['user_id'] = data['_id']; del data['_id']
        return data

    def get_users_modified_since(self, mintime, maxtime = None, reason=''):
        return self.instrument('get_users_modified_since(%s)'%reason, self._get_users_modified_since, (mintime, maxtime))
    def _get_users_modified_since(self, mintime, maxtime):
        # get list of IDs of users/players who have been modified since 'mintime'
        if maxtime is not None:
            qs = {'last_mtime': {'$gte':mintime, '$lte':maxtime}}
        else:
            qs = {'last_mtime': {'$gte':mintime}}
        result = self.player_cache().find(qs, {'_id':1})
        return map(lambda x: x['_id'], result)

    def player_cache_update(self, user_id, props, reason = None, overwrite = False):
        return self.instrument('player_cache_update(%s)'%reason, self._player_cache_update, (user_id,props,overwrite))
    def _player_cache_update(self, user_id, props, overwrite):
        if 'last_mtime' not in props: props['last_mtime'] = self.time
        if 'user_id' in props: del props['user_id']

        for k, v in props.iteritems():
            # temp - check for old score updates that should go to the player_scores table instead
            # assert not self.SCORE_RE.match(k)

            # coerce all booleans to ints for uniformity and ladder query syntax
            if type(v) is bool: v = int(v)

        if overwrite:
            # I'm not sure it's possible to update a document to a specific set of new fields atomically while also using a specific _id :(
            # this option is only used for offline maintenance scripts, so it's not a huge deal.
            # XXX ^ this can be fixed using .save()
            self.player_cache().remove({'_id':user_id})
            props['_id'] = user_id
            self.player_cache().insert(props)
            return True
        else:
            return self.player_cache().update({'_id':user_id}, {'$set':props}, upsert=True, multi=False)['n'] > 0

    def player_cache_lookup_batch(self, user_id_list, fields = None, reason = None):
        return self.instrument('player_cache_lookup_batch(%s)'%reason, self._player_cache_lookup_batch, (user_id_list, fields))
    def _player_cache_lookup_batch(self, user_id_list, pfields):
        ret = [{}] * len(user_id_list)
        backmap = {}
        for i in xrange(len(user_id_list)):
            if user_id_list[i] not in backmap:
                backmap[user_id_list[i]] = []
            backmap[user_id_list[i]].append(i)

        if pfields:
            # assume caller knows what they're doing
            fields = dict((field,1) for field in pfields)
        else:
            # blank out some fields that should not be sent to the client
            fields = {'LOCK_HOLDER':0,'LOCK_GENERATION':0,'LOCK_TIME':0}

        result = self.player_cache().find({'_id':{'$in':user_id_list}}, fields)
        for entry in result:
            entry = self.decode_player_cache(entry)
            if entry:
                for index in backmap[entry['user_id']]:
                    ret[index] = entry
        return ret

    # "player_cache_search" is for the player-accessible search-by-name/search-by-ID feature
    def player_cache_search(self, name, limit = -1, match_mode = 'prefix', name_field = 'ui_name_searchable', case_sensitive = False, reason=''): return self.instrument('player_cache_search(%s)'%reason, self._player_cache_search, (name, limit, name_field, match_mode, case_sensitive))
    def _player_cache_search(self, name, limit, name_field, match_mode, case_sensitive):
        assert len(name) >= 1
        qs = {}

        if name.isdigit():
            value = long(name)
            if value >= sys.maxint - 1: return [] # can't exceed 64 bits
            qs['_id'] = value
        else:
            if match_mode == 'prefix':
                qs[name_field] = {'$regex': '^'+name, '$options':'i' if (not case_sensitive) else ''}
            elif match_mode == 'full':
                if case_sensitive:
                    qs[name_field] = name
                else:
                    qs[name_field] = {'$regex': '^'+name+'$', '$options':'i'}
            else:
                raise Exception('unknown match_mode '+match_mode)

        qs['social_id'] = {'$ne': 'ai'} # no AIs
        cur = self.player_cache().find(qs, {'_id':1}).sort([('player_level', pymongo.DESCENDING)])
        if limit >= 1:
            cur = cur.limit(limit)
        return [x['_id'] for x in cur]

    # this is for internal use only
    # it is allowed to return an iterator
    def _player_cache_query_randomized(self, qs, maxret = -1, randomize_quality = 1, force_player_level_index = False):
        result = self.player_cache().find(qs, {'_id':1})

        if force_player_level_index:
            result = result.hint([('player_level',pymongo.ASCENDING)])

        if randomize_quality > 0:
            # accurate randomization - do full query (ignore maxret), then shuffle, then truncate
            result = list(result)
            random.shuffle(result)
            if maxret > 0:
                result = result[:maxret]
        else:
            # XXX do not use this anymore, becausesort({'$natural':1}) forces a full table scan as of MongoDB 2.4.6!

            # bad quick randomization - obey maxret
            result = result.sort([('$natural',-1)])
            if maxret > 0:
                result = result.limit(maxret)

            result = list(result)
            random.shuffle(result)

        return map(lambda x: x['_id'], result)

    # special case for use by notification checker
    def player_cache_query_tutorial_complete_and_mtime_between_or_ctime_between(self, mtime_ranges, ctime_ranges, reason = None):
        qs = {'$or': [{'tutorial_complete':1,'last_mtime':{'$gte':r[0], '$lt':r[1]}} for r in mtime_ranges] + \
                     [{'tutorial_complete':1,'account_creation_time':{'$gte':r[0], '$lt':r[1]}} for r in ctime_ranges]}
        return self.instrument('player_cache_query_tutorial_complete_and_mtime_between_or_ctime_between(%s)'%reason,
                               lambda qs: map(lambda x: x['_id'], self.player_cache().find(qs, {'_id':1})), (qs,))

    def player_cache_query_ladder_rival(self, query, maxret, randomize_quality = 1, reason = None):
        return self.instrument('player_cache_query_ladder_rival(%s)'%reason, self._player_cache_query_ladder_rival, (query,maxret,randomize_quality))
    def _player_cache_query_ladder_rival(self, query, maxret, randomize_quality):
        qand = []
        # translate legacy query syntax to MongoDB

        references_player_level = False

        # need to emulate a join on player_scores for the score range
        score_api = None
        score_first = False # whether to perform the score query before the player cache query
        score_range_qs = None
        score_qs = None
        score_stat = None
        score_axes = None

        s2 = None # scores2 API

        for item in query:
            key = '_id' if (item[0] == 'user_id') else item[0]

            if item[0] == 'player_level': references_player_level = True

            if type(key) is tuple:
                assert key[0] in ('scores1', 'scores2')
                assert score_api is None # once only
                score_api = key[0]

                # if score=0 is included, then do the player_cache query first, otherwise do the score query first
                score_first = (not (item[1] <= 0 and item[2] >= 0))
                score_range_qs = {'$gte':item[1], '$lte':item[2]}

                if score_api == 'scores1':
                    match = self.SCORE_RE.match(key[1])
                    assert match
                    if match:
                        gr = match.groups()
                        score_qs = self.parse_score_addr((gr[0], {'wk':'week','s':'season'}[gr[1]], int(gr[2])))
                        score_qs['score'] = score_range_qs

                elif score_api == 'scores2':
                    score_stat, score_axes = key[1]
                    import Scores2 # awkward
                    s2 = Scores2.MongoScores2(self)

                continue

            if len(item) == 4 and item[3] == '!in':
                assert item[1] == item[2]
                qand.append({key: {'$ne': item[1]}})
            elif len(item) == 3 and type(item[1]) in (int, float):
                if item[1] <= -1 and item[2] >= -1: # include missing elements
                    qand.append({'$or': [{key:{'$exists':False}}, {key:{'$gte':item[1],'$lte':item[2]}}]})
                elif item[1] == item[2]:
                    qand.append({key:item[1]})
                else:
                    qand.append({key:{'$gte':item[1],'$lte':item[2]}})
            else:
                raise Exception('cannot parse query item %s' % repr(item))

        # note: when it comes to handling maxret, the limit must be applied only to the FINAL
        # query, in order to guarantee that we will not miss any valid possible users to return.

        if score_first:
            # return list of candidates who have scores within the specified range
            if score_api == 'scores1':
                candidate_ids = [x['user_id'] for x in self.player_scores().find(score_qs,{'_id':0,'user_id':1})]
            elif score_api == 'scores2':
                candidate_ids = [x['user_id'] for x in s2._scores2_table('player', score_stat, score_axes).find({'key': s2._scores2_key(score_stat, score_axes),
                                                                                                                 'val': score_range_qs},
                                                                                                                {'_id':0,'user_id':1})]
            qand.append({'_id':{'$in':candidate_ids}})
            cache_qs = {'$and':qand}
            return self._player_cache_query_randomized(cache_qs, maxret = maxret, randomize_quality = randomize_quality)

        else:
            cache_qs = {'$and':qand}
            ret = []
            for candidate_id in self._player_cache_query_randomized(cache_qs, maxret = -1, randomize_quality = randomize_quality,
                                                                    force_player_level_index = references_player_level):
                # check if candidate's score is within the specified range
                if score_api == 'scores1':
                    score_qs['user_id'] = candidate_id
                    if self.player_scores().find_one(score_qs, {'_id':0,'user_id':1}) is None:
                        continue
                elif score_api == 'scores2':
                    if s2._scores2_table('player', score_stat, score_axes).find_one({'key': s2._scores2_key(score_stat, score_axes),
                                                                                     'val': score_range_qs,
                                                                                     'user_id': candidate_id},{'_id':0}) is None:
                        continue
                ret.append(candidate_id)
                if len(ret) >= maxret: break

            return ret

    ###### PLAYER LOCKING (embedded in player_cache) ######
    # note: extra unused parameters are for drop-in compatibility with old dbserver API

    def player_lock_keepalive_batch(self, player_ids, unused_generations, unused_expected_states, unused_check_messages, reason=''):
        self.instrument('player_lock_keepalive_batch(%s)'%reason, self._player_lock_keepalive_batch, (player_ids,))
    def _player_lock_keepalive_batch(self, player_ids):
        self.player_cache().update({'_id':{'$in':player_ids}}, {'$set':{'LOCK_TIME':self.time}}, multi=True)

    def player_lock_get_state_batch(self, player_ids, reason=''):
        return self.instrument('player_lock_get_state_batch(%s)'%reason, self._player_lock_get_state_batch, (player_ids,))
    def _player_lock_get_state_batch(self, player_ids):
        ret = [(self.LOCK_OPEN,-1)] * len(player_ids)
        states = self.player_cache().find({'_id':{'$in':player_ids}}, {'LOCK_STATE':1,'LOCK_OWNER':1})
        for state in states:
            ret[player_ids.index(state['_id'])] = (state.get('LOCK_STATE',self.LOCK_OPEN), state.get('LOCK_OWNER',-1))
        return ret

    def player_lock_release(self, player_id, generation, expected_state, expected_owner_id = -1, reason=''):
        return self.instrument('player_lock_release(%s)'%reason, self._player_lock_release, (player_id,generation))
    def _player_lock_release(self, player_id, generation):
        qs = {'$set':{'last_mtime':self.time},
              '$unset':{'LOCK_STATE':1,'LOCK_OWNER':1,'LOCK_TIME':1,'LOCK_HOLDER':1}}
        if generation >= 0:
            qs['$set']['LOCK_GENERATION'] = generation
        else:
            # do NOT unset lock generation, leave it alone
            pass
        return self.player_cache().update({'_id':player_id, 'LOCK_STATE':{'$gt':0}}, qs)['updatedExisting']


    def player_lock_acquire_login(self, player_id, owner_id = -1, reason=''):
        return self.instrument('player_lock_acquire_login(%s)'%reason, self._player_lock_acquire, (player_id,owner_id,-1,self.LOCK_LOGGED_IN))
    def player_lock_acquire_attack(self, player_id, generation, owner_id = -1, reason=''):
        return self.instrument('player_lock_acquire_attack(%s)'%reason, self._player_lock_acquire, (player_id,owner_id,generation,self.LOCK_BEING_ATTACKED))[0]

    # return (state, prev generation) where prev_generation is only valid for acquire_login
    def _player_lock_acquire(self, player_id, owner_id, generation, want_state):
        LOCK_IS_OPEN = {'$or':[{'LOCK_STATE':{'$exists':False}},
                               {'LOCK_STATE':{'$lte':0}}]}
        LOCK_IS_STALE = {'LOCK_TIME':{'$lte':self.time - self.LOCK_TIMEOUT}} # lock timed out

        if generation < 0:
            qs = {'$or':[LOCK_IS_OPEN, LOCK_IS_STALE]}

        else:
            LOCK_GEN_IS_GOOD = {'$or':[{'LOCK_GENERATION':{'$exists':False}},
                                       {'LOCK_GENERATION':{'$lte':generation}}]}
            qs = {'$or':[{'$and':[LOCK_IS_OPEN,LOCK_GEN_IS_GOOD]},
                         LOCK_IS_STALE]}

        update_fields = {'LOCK_STATE':want_state,
                         'LOCK_TIME':self.time,
                         'LOCK_OWNER':owner_id,
                         'LOCK_HOLDER':self.ident,
                         'last_mtime':self.time}

        if want_state == self.LOCK_LOGGED_IN and generation < 0:
            # use find_and_modify because we want to return the previous generation and lock time even in success case
            ret = self.player_cache().find_and_modify({'$and': [{'_id':player_id},qs]}, update = {'$set':update_fields}, fields = {'LOCK_GENERATION':1,'last_mtime':1})
            if ret:
                success = True
                # only return a valid prev_gen when the player was modified "recently"
                # otherwise we run the danger of a player getting permanently "stuck" when the lock generation gets skewed ahead of playerdb data
                if ret.get('last_mtime',-1) >= self.time - self.LOCK_TIMEOUT:
                    prev_gen = ret.get('LOCK_GENERATION',-1)
                else:
                    prev_gen = -1 # no need to compare generations
            else:
                success = False
        else:
            success = self.player_cache().update({'$and': [{'_id':player_id},qs]}, {'$set':update_fields}, upsert=False, multi=False, w=1)['n'] > 0
            prev_gen = -1

        if success: # acquired the lock
            return (want_state, prev_gen)

        else: # failed to acquire
            # either there's no entry, or it's locked, or it's a generation mismatch - figure out what's going on
            try:
                fields = update_fields.copy(); fields['_id'] = player_id
                self.player_cache().insert(fields)
                return (want_state, -1) # new player that wasn't in player_cache - created a new entry
            except pymongo.errors.DuplicateKeyError:
                # it was already locked. Check current state, but for informational purposes only, because this is subject to race condition.
                cur_state_racy = self.player_cache().find_one({'_id':player_id},
                                                              {'LOCK_STATE':1,'LOCK_GENERATION':1,'LOCK_OWNER':1,'LOCK_HOLDER':1})
                state_response = -self.LOCK_BEING_ATTACKED
                if cur_state_racy and cur_state_racy.get('LOCK_STATE',0) > 0:
                    if (owner_id > 0 and (cur_state_racy.get('LOCK_OWNER',-1) == owner_id)):
                        # this is only exceptional if the lock acquirer already has the lock.
                        # just failing because another player logged in or attacked is a normal occurrence.
                        self.log_exception('player_lock_acquire(%d want %d) by %s but lock is already taken (state %d), by %d on %s' % \
                                           (player_id, want_state, self.ident, cur_state_racy['LOCK_STATE'],
                                            cur_state_racy.get('LOCK_OWNER', -1), cur_state_racy.get('LOCK_HOLDER', 'unknown')))
                    state_response = -cur_state_racy['LOCK_STATE']
                elif cur_state_racy and generation >= 0 and cur_state_racy.get('LOCK_GENERATION',-1) > generation:
                    self.log_exception('player_lock_acquire(%d want %d) by %s but acquirer gen %d < lock gen %d' % \
                                       (player_id, want_state, self.ident, generation, cur_state_racy.get('LOCK_GENERATION',-1)))
                else:
                    self.log_exception('player_lock_acquire(%d want %d) by %s failed, unknown reason' % (player_id, want_state, self.ident))

                return (state_response, cur_state_racy.get('LOCK_GENERATION',-1) if cur_state_racy else -1)

    ###### PLAYER MESSAGE TABLE ######

    def message_table(self):
        coll = self._table('message_table')
        if not self.seen_messages:
            coll.ensure_index('recipient')
            self.seen_messages = True
        return coll
    def decode_message(self, msg):
        msg['msg_id'] = self.decode_object_id(msg['_id']); del msg['_id']
        return msg

    def msg_poll(self, recipient_list, reason=''):
        return self.instrument('msg_poll(%s)'%reason, self._msg_poll, (recipient_list,))
    def _msg_poll(self, recipient_list):
        ret = [0]*len(recipient_list)
        msg_list = self.message_table().find({'recipient':{'$in':recipient_list}},{'_id':0,'recipient':1})
        for msg in msg_list:
            ret[recipient_list.index(msg['recipient'])]=1
        return ret

    def msg_ack(self, recipient, msg_idlist, reason=''):
        return self.instrument('msg_ack(%s)'%reason, self._msg_ack, (recipient, msg_idlist))
    def _msg_ack(self, recipient, msg_idlist):
        self.message_table().remove({'recipient':recipient, '_id':{'$in': map(self.encode_object_id, msg_idlist)}})

    def msg_recv(self, recipient, type_filter = None, reason=''):
        return self.instrument('msg_recv(%s)'%reason, self._msg_recv, (recipient,type_filter))
    def _msg_recv(self, recipient, type_filter):
        qs = {'recipient':recipient}
        if type_filter: qs['type'] = {'$in': type_filter}
        return map(self.decode_message, self.message_table().find(qs))

    def msg_send(self, msglist, reason=''):
        return self.instrument('msg_send(%s)'%reason, self._msg_send, (msglist,))
    def _msg_send(self, msglist):
        for msg in msglist:
            assert 'type' in msg
            if 'msg_id' in msg: del msg['msg_id'] # we now use server-assigned _ids for this
            if 'time' not in msg:
                assert self.time > 0
                msg['time'] = self.time
            for recipient in msg['to']:
                msg['recipient'] = recipient
                if 'unique_per_sender' in msg:
                    # only allow one instance of this message for any recipient/unique_per_sender/from tuple
                    # overwrite other props if already exists, otherwise create
                    new_props = msg.copy()
                    for FIELD in ('recipient', 'unique_per_sender', 'from'):
                        if FIELD in new_props: del new_props[FIELD]
                    qs = {'recipient':msg['recipient'], 'unique_per_sender':msg['unique_per_sender']}
                    if 'from' in msg: qs['from'] = msg['from']
                    self.message_table().update(qs, {'$set':new_props}, upsert = True, multi = False)
                else:
                    self.message_table().insert(msg, manipulate = False)

    ###### ABTEST TABLE ######

    def abtest_table(self):
        coll = self._table('abtests')
        return coll
    def abtest_join_cohort(self, name, cohort, limit):
        if limit < 0: return True
        if limit == 0: return False
        table = self.abtest_table()
        key = name+':'+cohort
        # can't use a one-step update/upsert for this, because it would also try to upsert if the cohort is full
        try:
            table.insert({'_id':key, 'name':name, 'cohort':cohort, 'N':0})
        except pymongo.errors.DuplicateKeyError:
            pass
        ret = table.update({'_id':key, 'N': {'$lt':limit}}, {'$inc':{'N':1}}, upsert=False, w=1)
        if ret['updatedExisting']:
            return True
        return False

    ###### GLOBAL MAP SETTINGS ######

    def region_table_name(self, region, subdb): return 'region_%s_%s' % (region, subdb)
    def region_table(self, region, subdb):
        assert region and region != 'None' # some other code seems to be passing None/'None' here - stop it now!
        assert subdb
        # set up indices the first time we refer to a new region
        if region not in self.seen_regions:
            self._table(self.region_table_name(region, 'map')).ensure_index('base_landlord_id') # base_id is primary key for this
            self._table(self.region_table_name(region, 'map')).ensure_index([('base_map_loc',pymongo.GEO2D)], min=0, max=1024, bits=16)
            self._table(self.region_table_name(region, 'map')).ensure_index('base_landlord_id') # base_id is primary key for this
            self._table(self.region_table_name(region, 'map')).ensure_index('base_map_loc_flat') # used to speed up occupancy checks

            self._table(self.region_table_name(region, 'map_deletions')).ensure_index('millitime', expireAfterSeconds=3*3600) # should be about as long as max session length


            self._table(self.region_table_name(region, 'mobile')).ensure_index('owner_id') # ([('owner_id',pymongo.ASCENDING), ('squad_id',pymongo.ASCENDING)])
            self._table(self.region_table_name(region, 'mobile')).ensure_index('base_id')
            self._table(self.region_table_name(region, 'fixed')).ensure_index('base_id')

            self.seen_regions[region] = 1
        return self._table(self.region_table_name(region, subdb))

    def alliance_turf_table(self):
        coll = self._table('alliance_turf')
        if not self.seen_turf:
            # note: there can be multiple alliances at the same rank in the same region
            coll.ensure_index([('region_id',pymongo.ASCENDING),('rank',pymongo.ASCENDING)])
            coll.ensure_index([('alliance_id',pymongo.ASCENDING)])
            self.seen_turf = True
        return coll

    ###### MAP FEATURES (BASE) TABLES ######

    def alliance_turf_clear(self, region_id):
        self.alliance_turf_table().remove({'region_id':region_id}, multi=True)
    def alliance_turf_update(self, region_id, rank, alliance_id, props = {}):
        props['rank'] = rank
        self.alliance_turf_table().update({'region_id':region_id, 'alliance_id':alliance_id}, {'$set':props}, upsert=True, multi=False)

    def _decode_alliance_turf(self, props):
        if '_id' in props: del props['_id']
        return props
    def alliance_turf_get_by_region(self, region_id, reason=''):
        return [self._decode_alliance_turf(x) for x in self.instrument('alliance_turf_get_by_region(%s)'%reason, self._alliance_turf_query, ({'region_id':region_id},))]
    def _alliance_turf_query(self, qs):
        return self.alliance_turf_table().find(qs).sort([('rank',pymongo.ASCENDING)])


    def _decode_map_feature(self, props):
        props['base_id'] = props['_id']
        del props['_id']

        # mongo sometimes turns these into floats :(
        if 'base_map_loc' in props: props['base_map_loc'] = [int(props['base_map_loc'][0]), int(props['base_map_loc'][1])]

        # omit path data for moving features that have already arrived at their destination by now
        if props.get('base_map_path') and props['base_map_path'][-1]['eta'] < self.time:
            del props['base_map_path']

        # omit unwanted fields
        for UNWANTED in ('base_map_loc_flat','base_map_path_eta'):
            if UNWANTED in props: del props[UNWANTED]

        return props

    def flatten_map_loc(self, loc):
        # pack a 2d map location into a single 32-bit integer for fast queries
        BITS=12
        assert len(loc) == 2
        assert loc[0] >= 0 and loc[1] < (1<<BITS)
        assert loc[1] >= 0 and loc[1] < (1<<BITS)
        return (int(loc[0]) | (int(loc[1])<<BITS))

    def get_map_features_by_type(self, region, base_type, reason=''):
        return self.instrument('get_map_features_by_type(%s)'%reason, self._query_map_features, (region,{'base_type':base_type}))
    def count_map_features_by_type(self, region, base_type, reason=''):
        return self.instrument('count_map_features_by_type(%s)'%reason, self._count_map_features, (region,{'base_type':base_type}))
    def get_map_feature_ids(self, region, reason=''):
        return (x['base_id'] for x in self.instrument('get_map_feature_ids(%s)'%reason, self._query_map_features, (region,{},{'_id':1})))
    def get_map_feature_ids_by_type(self, region, base_type, reason=''):
        return (x['base_id'] for x in self.instrument('get_map_feature_ids_by_type(%s)'%reason, self._query_map_features, (region,{'base_type':base_type}, {'_id':1})))
    def get_map_features_by_loc(self, region, map_loc, reason=''):
        return self.instrument('get_map_features_by_loc(%s)'%reason, self._query_map_features, (region,{'base_map_loc':map_loc}))
    def get_map_features_by_landlord_and_type(self, region, landlord_id, base_type, reason=''):
        return self.instrument('get_map_features_by_landlord_and_type(%s)'%reason, self._query_map_features, (region,{'base_landlord_id':landlord_id,'base_type':base_type}))
    def get_expired_map_feature_ids_by_types(self, region, base_types, reason=''):
        return (x['base_id'] for x in \
                self.instrument('get_expired_map_features_by_types(%s)'%reason, self._query_map_features, (region,{'base_type':{'$in':base_types},
                                                                                                                   'base_expire_time':{'$exists':True,
                                                                                                                                       '$gt':0,
                                                                                                                                       '$lt':self.time}
                                                                                                                   }, {'_id':1})))

    def _query_map_features(self, region, query, fields = None, batch_size = None):
        cur = self.region_table(region, 'map').find(query, fields)
        if batch_size is not None: cur = cur.batch_size(batch_size)
        return (self._decode_map_feature(x) for x in cur)
    def _count_map_features(self, region, query):
        return self.region_table(region, 'map').find(query).count()

    def get_map_feature_by_base_id(self, region, base_id, reason=''):
        return self.instrument('get_map_feature_by_base_id(%s)'%reason, self._get_map_feature_by_base_id, (region,base_id))
    def _get_map_feature_by_base_id(self, region, base_id):
        x = self.region_table(region, 'map').find_one({'_id':base_id})
        if not x: return None
        return self._decode_map_feature(x)

    def get_map_feature_population(self, region, base_type, reason=''):
        return self.instrument('get_map_feature_population(%s)'%reason, self._get_map_feature_population, (region, base_type))
    def _get_map_feature_population(self, region, base_type):
        return self.region_table(region, 'map').find({'base_type':base_type}).count()

    def get_map_features(self, region, updated_since, batch_size = None, reason=''):
        if updated_since <= 0:
            if batch_size is None: batch_size = 999999 # use a large batch size for full queries
            return self.instrument('get_map_features(%s,full)'%reason, self._query_map_features, (region,{},None,batch_size))
        else:
            return self.instrument('get_map_features(%s,incr)'%reason, self._get_map_features_incr, (region,updated_since))
    def _get_map_features_incr(self, region, updated_since):
        ret = [self._decode_map_feature(x) for x in self.region_table(region, 'map').find({'last_mtime':{'$gte':updated_since}})]
        ret += [{'base_id':x['_id'],
                 'DELETED':1} for x in \
                self.region_table(region, 'map_deletions').find({'millitime':{'$gte':datetime.datetime.fromtimestamp(float(updated_since))}},
                                                                {'_id':1})]
        return ret

    def drop_map_feature(self, region, base_id, originator=None, reason=''):
        self.instrument('drop_map_feature(%s)'%reason, self._drop_map_feature, (region,base_id))
        if self.map_update_hook:
            self.map_update_hook(region, base_id, None, originator)

    def _drop_map_feature(self, region, base_id):
        self.region_table(region, 'map').remove({'_id':base_id})
        # time field must be converted for the expireAfterSeconds TTL thing to work
        self.region_table(region, 'map_deletions').save({'_id':base_id,'millitime':datetime.datetime.fromtimestamp(float(self.time))})

    def update_map_feature(self, region, base_id, props, originator=None, do_hook = True, reason=''):
        ret = self.instrument('update_map_feature(%s)'%reason, self._update_map_feature, (region,base_id,props))
        if self.map_update_hook and do_hook:
            self.map_update_hook(region, base_id, props, originator)
        return ret
    def _update_map_feature(self, region, base_id, caller_props):
        props = caller_props.copy()
        props['last_mtime'] = self.time
        if 'base_id' in props: del props['base_id']
        if 'base_map_loc' in props: props['base_map_loc_flat'] = self.flatten_map_loc(props['base_map_loc'])
        # arrival time at final waypoint - needed for dynamic occupancy query
        if 'base_map_path' in props: props['base_map_path_eta'] = props['base_map_path'][-1]['eta']
        self.region_table(region, 'map').update({'_id':base_id}, {'$set': props}, upsert = False)

    def create_map_feature(self, region, base_id, props, exclusive = -1, originator=None, do_hook = True, reason=''):
        ret = self.instrument('create_map_feature(%s,x=%d)'%(reason,exclusive), self._create_map_feature, (region,base_id,props,originator,exclusive,None,None))
        if ret and self.map_update_hook and do_hook:
            self.map_update_hook(region, base_id, props, originator)
        return ret
    def move_map_feature(self, region, base_id, props, exclusive = -1, old_loc=None, old_path=None, originator=None, do_hook = True, reason=''):
        assert old_loc
        ret = self.instrument('move_map_feature(%s,x=%d)'%(reason,exclusive), self._create_map_feature, (region,base_id,props,originator,exclusive,old_loc,old_path))
        if ret and self.map_update_hook and do_hook:
            self.map_update_hook(region, base_id, {'base_id':base_id,
                                                   'base_map_loc':props['base_map_loc'],
                                                   'base_map_path':props.get('base_map_path',None)}, originator)
        return ret

    def _create_map_feature(self, region, base_id, caller_props, originator, exclusive, old_loc, old_path):
        if (old_loc is None):
            props = caller_props.copy() # don't disturb caller's version
            props['_id'] = base_id
            # do not store base_id since it is redundant with _id
            if 'base_id' in props: del props['base_id']

            props['base_map_loc_flat'] = self.flatten_map_loc(props['base_map_loc'])
            if 'base_map_path' in props: props['base_map_path_eta'] = props['base_map_path'][-1]['eta']

            # when creating a new feature, create it in the "locked" state
            props['LOCK_STATE'] = self.LOCK_BEING_ATTACKED
            props['LOCK_TIME'] = self.time
            if originator is not None:
                props['LOCK_OWNER'] = originator
            props['last_mtime'] = self.time

        else:
            # when moving an existing feature, only update loc, path, and mtime
            move_update = {'$set': {'last_mtime':self.time,
                                    'base_map_loc': caller_props['base_map_loc'],
                                    'base_map_loc_flat': self.flatten_map_loc(caller_props['base_map_loc']),
                                    }}
            if caller_props.get('base_map_path',None):
                move_update['$set']['base_map_path'] = caller_props['base_map_path']
                move_update['$set']['base_map_path_eta'] = caller_props['base_map_path'][-1]['eta']
            else:
                move_update['$unset'] = {'base_map_path':1,'base_map_path_eta':1}

        if exclusive < 0:
            # easy case
            if (old_loc is None):
                self.region_table(region, 'map').save(props)
            else:
                self.region_table(region, 'map').update({'_id':base_id}, move_update, upsert = False)
            success = True

        else:
            if 0:
                # use a server-side function in order to make the insert exclusion zone atomic
                # unfortunately, this requires full admin privileges on the db, so we can't use it
                func ="""function(tname, loc, excl, db_time, props) {
                var excl_query;
                if(excl == 0) {
                    excl_query = [loc[0],loc[1]]; // exact match
                } else {
                    excl_query = {'$geoWithin':{'$box':[[loc[0]-excl,loc[1]-excl],[loc[0]+excl,loc[1]+excl]]}};
                }
                var doc = db[tname].findOne({'base_map_loc':excl_query});
                if(doc) {
                    return false;
                } else {
                    db[tname].save(props);
                    return true;
                }
                }"""
                success = self.db.eval(func, self.region_table_name(region, 'map'), props['base_map_loc'], exclusive, self.time, props)
            else:
                # place, but then roll back if we overlapped
                #NOT_EXPIRED = {'$or':[{'base_expire_time':{'$exists':False}},{'base_expire_time':{'$lte':0}},{'base_expire_time':{'$gt':self.time}}]}
                if exclusive == 0:
                    OVERLAP = {'base_map_loc':caller_props['base_map_loc']}
                    # XXX is this better? OVERLAP = {'base_map_loc_flat':self.flatten_map_loc(caller_props['base_map_loc'])}
                else:
                    loc = caller_props['base_map_loc']
                    OVERLAP = {'base_map_loc':{'$geoWithin':{'$box':[[loc[0]-exclusive,loc[1]-exclusive],[loc[0]+exclusive,loc[1]+exclusive]]}}}
                qs = {'$and': [OVERLAP, {'_id':{'$ne':base_id}}]} # , NOT_EXPIRED]}

                if (old_loc is None):
                    self.region_table(region, 'map').save(props)
                else:
                    self.region_table(region, 'map').update({'_id':base_id}, move_update, upsert = False)

                if bool(self.region_table(region, 'map').find_one(qs)):
                    # overlapped, roll back
                    success = False
                    if (old_loc is None):
                        self.region_table(region, 'map').remove({'_id':base_id})
                    else:
                        # possible race condition here. But what else are we gonna do?
                        rb_set = {'$set': {'base_map_loc':old_loc,
                                           'base_map_loc_flat':self.flatten_map_loc(old_loc)}}
                        if old_path is None:
                            rb_set['$unset'] = {'base_map_path':1,'base_map_path_eta':1}
                        else:
                            rb_set['$set']['base_map_path'] = old_path
                            rb_set['$set']['base_map_path_eta'] = old_path[-1]['eta']
                        self.region_table(region, 'map').update({'_id':base_id}, rb_set, upsert = False)
                else:
                    success = True

        if success:
            self.region_table(region, 'map_deletions').remove({'_id':base_id})
        return success

    def map_feature_occupancy_check(self, region, coord_list, reason=''):
        return self.instrument('map_feature_occupancy_check(%s)'%(reason), self._map_feature_occupancy_check, (region,coord_list))
    def _map_feature_occupancy_check(self, region, coord_list):
        #qs = {'base_map_loc':{'$in':coord_list}} # doesn't work due to limitations of MongoDB 2d indices
        #qs = {'$or': [{'base_map_loc':coord} for coord in coord_list]} # works, but slow
        #NOT_EXPIRED = {'$or':[{'base_expire_time':{'$exists':False}},{'base_expire_time':{'$lte':0}},{'base_expire_time':{'$gt':self.time}}]}
        qs = {'base_map_loc_flat': {'$in': [self.flatten_map_loc(c) for c in coord_list]}} # ,NOT_EXPIRED}
        return self.region_table(region, 'map').find_one(qs) is not None

    # waypoint_list is [{'xy':[x,y], 'eta':time}, ...]
    def map_feature_occupancy_check_dynamic(self, region, waypoint_list, reason=''):
        return self.instrument('map_feature_occupancy_check_dynamic(%s)'%(reason), self._map_feature_occupancy_check_dynamic, (region,waypoint_list))
    def _map_feature_occupancy_check_dynamic(self, region, waypoint_list):
        qs = {'$or': [ # at any waypoint
               {'$and': [ # is a feature present here before we'd pass it?
                 {'base_map_loc_flat': self.flatten_map_loc(w['xy'])},
                 {'$or':[{'base_map_path':{'$exists':False}},
                         {'base_map_path_eta':{'$exists':False}},
                         {'base_map_path_eta':{'$lte':w['eta']}}]}
               ] } \
               for w in waypoint_list
             ] }
        return self.region_table(region, 'map').find_one(qs) is not None


    def map_feature_lock_acquire(self, region, base_id, owner_id, generation = -1, do_hook = True, reason=''):
        state = self.instrument('map_feature_lock_acquire(%s)'%(reason), self._map_feature_lock_acquire, (region,base_id,owner_id,generation))
        if state > 0 and self.map_update_hook and do_hook:
            self.map_update_hook(region, base_id, {'LOCK_STATE':state,'LOCK_OWNER':owner_id}, owner_id)
        return state
    def _map_feature_lock_acquire(self, region, base_id, owner_id, generation):
        LOCK_IS_OPEN = {'$or':[{'LOCK_STATE':{'$exists':False}},
                               {'LOCK_STATE':{'$lte':0}}]}
        LOCK_IS_STALE = {'LOCK_TIME':{'$lte':self.time - self.LOCK_TIMEOUT}} # lock timed out

        if generation < 0:
            qs = {'$and':[{'_id':base_id},
                          {'$or':[LOCK_IS_OPEN, LOCK_IS_STALE]}]}

        else:
            LOCK_GEN_IS_GOOD = {'$or':[{'LOCK_GENERATION':{'$exists':False}},
                                       {'LOCK_GENERATION':{'$lte':generation}}]}
            qs = {'$and':[{'_id':base_id},
                          {'$or':[{'$and':[LOCK_IS_OPEN,LOCK_GEN_IS_GOOD]},
                                  LOCK_IS_STALE]}]}

        update = {'$set':{'LOCK_STATE':self.LOCK_BEING_ATTACKED,
                          'LOCK_TIME':self.time,
                          'LOCK_OWNER':owner_id,
                          'last_mtime':self.time}}

        # both of these work, update might be faster
        if 0:
            ret = self.region_table(region, 'map').find_and_modify(query = qs, update = update,
                                                                   fields = {'LOCK_STATE':1,'LOCK_OWNER':1},
                                                                   new = True)
            if ret and ret['LOCK_OWNER'] == owner_id:
                return ret['LOCK_STATE']
        else:
            ret = self.region_table(region, 'map').update(qs, update, w=1)
            if ret['updatedExisting']:
                return self.LOCK_BEING_ATTACKED

        return -self.LOCK_BEING_ATTACKED

    def map_feature_lock_release(self, region, base_id, owner_id, generation = -1, extra_props = None, do_hook = True, reason=''):
        ret = self.instrument('map_feature_lock_release(%s)'%reason, self._map_feature_lock_release, (region,base_id,owner_id,generation,extra_props))
        if self.map_update_hook and do_hook:
            self.map_update_hook(region, base_id, {'LOCK_STATE':0}, owner_id)
        return ret
    def _map_feature_lock_release(self, region, base_id, owner_id, generation, extra_props):
        qs = {'$set':{'last_mtime':self.time},
              '$unset':{'LOCK_STATE':1,'LOCK_OWNER':1,'LOCK_TIME':1}}
        if generation >= 0:
            qs['$set']['LOCK_GENERATION'] = generation
        else:
            # do NOT unset lock generation, leave it alone
            pass

        if extra_props:
            for k,v in extra_props.iteritems():
                qs['$set'][k] = v
        return self.region_table(region, 'map').update({'_id':base_id}, qs)

    def map_feature_lock_keepalive_batch(self, region, base_ids, reason=''):
        self.instrument('map_feature_lock_keepalive_batch(%s)'%reason, self._map_feature_lock_keepalive_batch, (region, base_ids))
    def _map_feature_lock_keepalive_batch(self, region, base_ids):
        self.region_table(region, 'map').update({'_id':{'$in':base_ids}}, {'$set':{'LOCK_TIME':self.time}}, multi=True)

    def map_feature_lock_get_state_batch(self, region, base_ids, reason=''):
        return self.instrument('map_feature_lock_get_state_batch(%s)'%reason, self._map_feature_lock_get_state_batch, (region, base_ids))
    def _map_feature_lock_get_state_batch(self, region, base_ids):
        ret = [(self.LOCK_OPEN,-1)] * len(base_ids)
        states = self.region_table(region, 'map').find({'_id':{'$in':base_ids}}, {'LOCK_STATE':1,'LOCK_OWNER':1})
        for state in states:
            ret[base_ids.index(state['_id'])] = (state.get('LOCK_STATE',self.LOCK_OPEN), state.get('LOCK_OWNER',-1))
        return ret

    # periodic region maintenance - bust stale or timed-out locks. (note, maptool's weeding of expired map features is separate from this).
    def do_region_maint(self, region):
        LOCK_IS_TAKEN = {'$and':[{'LOCK_STATE':{'$exists':True}},
                                 {'LOCK_STATE':{'$gt':0}}]}
        LOCK_IS_STALE = {'LOCK_TIME':{'$lte':self.time - self.LOCK_TIMEOUT}}
        self.region_table(region, 'map').update({'$and':[LOCK_IS_TAKEN, LOCK_IS_STALE]},
                                                {'$unset':{'LOCK_STATE':1,'LOCK_OWNER':1,'LOCK_TIME':1,'LOCK_GENERATION':1}},
                                                multi=True)
        # get rid of path data for moving features that have already arrived at their destination by now
        self.region_table(region, 'map').update({'base_map_path_eta':{'$exists':True, '$lt': self.time}},
                                                {'$unset':{'base_map_path':1,'base_map_path_eta':1}},
                                                multi=True)

        if 1: # clean up legacy features with no base_map_path_eta
            self.region_table(region, 'map').update({'$and':[{'base_map_path':{'$exists':True}},
                                                             {'base_map_path_eta':{'$exists':False}},
                                                             {'base_map_path':{'$all':[{'$elemMatch': {'eta': {'$lt': self.time}}}]}}
                                                             ]},
                                                    {'$unset':{'base_map_path':1}},
                                                    multi=True)

    ###### MAP OBJECTS (FIXED/MOBILE) TABLES ######

    def get_mobile_object_by_id(self, region, obj_id, reason=''):
        ret = self.instrument('get_mobile_object_by_id(%s)'%reason, self._find_objects, (region,'mobile',{'_id':self.encode_object_id(obj_id)},))
        return ret[0] if ret else None

    def get_mobile_objects_by_owner(self, region, user_id, reason=''): return self.instrument('get_mobile_objects_by_owner(%s)'%reason, self._find_objects, (region,'mobile',{'owner_id':user_id},))

    def get_mobile_objects_by_base(self, region, base_id, reason=''): return self.instrument('get_mobile_objects_by_base(%s)'%reason, self._find_objects, (region,'mobile',{'base_id':base_id},))
    def get_fixed_objects_by_base(self, region, base_id, reason=''): return self.instrument('get_fixed_objects_by_base(%s)'%reason, self._find_objects, (region,'fixed',{'base_id':base_id},))

    def _find_objects(self, region, table_name, query):
        ret = []
        for obj in self.region_table(region, table_name).find(query):
            assert '_id' in obj
            obj['obj_id'] = self.decode_object_id(obj['_id'])
            del obj['_id']
            ret.append(obj)
        return ret

    def get_base_ids_referenced_by_objects(self, region, reason=''): return self.instrument('get_base_ids_referenced_by_objects(%s)'%reason, self._get_base_ids_referenced_by_objects, (region,))
    def _get_base_ids_referenced_by_objects(self, region):
        return self.region_table(region, 'mobile').find().distinct('base_id') + self.region_table(region, 'fixed').find().distinct('base_id')

    def update_mobile_object(self, region, obj, partial=False, reason=''): return self.instrument('update_mobile_object(%s)'%reason, self._update_object, (region,'mobile',obj,partial))
    def update_fixed_object(self, region, obj, partial=False, reason=''): return self.instrument('update_fixed_object(%s)'%reason, self._update_object, (region,'fixed',obj,partial))
    def _update_object(self, region, table_name, obj, partial):
        if not partial:
            assert 'obj_id' in obj
            assert 'owner_id' in obj
            assert 'base_id' in obj
            # optional now: assert 'kind' in obj

        # temporary swap the obj_id field for the _id field
        obj['_id'] = self.encode_object_id(obj['obj_id'])
        temp = obj['obj_id']
        del obj['obj_id']
        try:
            if partial:
                _id = obj['_id']
                del obj['_id']
                self.region_table(region, table_name).update({'_id':_id}, {'$set': obj}, upsert = False)
            else:
                self.region_table(region, table_name).save(obj)
        finally:
            obj['obj_id'] = temp

    def _save_objects(self, region, table_name, objlist):
        for obj in objlist:
            assert 'obj_id' in obj
            assert 'owner_id' in obj
            assert 'base_id' in obj
        for obj in objlist:
            # temporary swap the obj_id field for the _id field
            obj['_id'] = self.encode_object_id(obj['obj_id'])
            del obj['obj_id']
        try:
            self.region_table(region, table_name).insert(objlist)
        finally:
            for obj in objlist:
                obj['obj_id'] = self.decode_object_id(obj['_id'])
                del obj['_id']

    def drop_fixed_objects_by_base(self, region, base_id, reason=''): return self.instrument('drop_fixed_objects_by_base(%s)'%reason, self._drop_objects, (region,'fixed',{'base_id':base_id}))
    def drop_mobile_objects_by_base(self, region, base_id, reason=''): return self.instrument('drop_mobile_objects_by_base(%s)'%reason, self._drop_objects, (region,'mobile',{'base_id':base_id}))
    def drop_mobile_objects_by_owner(self, region, user_id, reason=''): return self.instrument('drop_mobile_objects_by_owner(%s)'%reason, self._drop_objects, (region,'mobile',{'owner_id':user_id}))
    def drop_all_objects_by_base(self, *args, **kwargs):
        return self.drop_fixed_objects_by_base(*args, **kwargs) + self.drop_mobile_objects_by_base(*args, **kwargs)
    def drop_fixed_object_by_id(self, region, obj_id, reason=''):  return self.instrument('drop_fixed_object_by_id(%s)'%reason, self._drop_objects, (region,'fixed',{'_id':self.encode_object_id(obj_id)}))
    def drop_mobile_object_by_id(self, region, obj_id, reason=''):  return self.instrument('drop_mobile_object_by_id(%s)'%reason, self._drop_objects, (region,'mobile',{'_id':self.encode_object_id(obj_id)}))
    def _drop_objects(self, region, table_name, query):
        return self.region_table(region, table_name).remove(query)['n']

    def heal_mobile_object_by_id(self, region, obj_id, reason=''): return self.instrument('heal_mobile_object_by_id(%s)'%reason, self._heal_objects, (region,'mobile',{'_id':self.encode_object_id(obj_id)}))
    def _heal_objects(self, region, table_name, query):
        self.region_table(region, table_name).update(query, {'$unset': {'hp':1, 'hp_ratio':1}}, multi = True)

    ###### ALLIANCE TABLES ######

    # Note: some of these APIs take an explicit "time" parameter
    # instead of using self.time. This is a legacy artifact of the old
    # dbserver API, retained so that the functions below work as
    # drop-in replacements.

    def alliance_table(self, name):
        if not self.seen_alliances:
            self._table('alliances').ensure_index('ui_name', unique=True)
            self._table('alliances').ensure_index('chat_tag', unique=True, sparse=True)
            self._table('alliance_members').ensure_index('alliance_id')
            self._table('alliance_invites').ensure_index('user_id')
            self._table('alliance_invites').ensure_index('alliance_id')
            self._table('alliance_join_requests').ensure_index('user_id')
            self._table('alliance_join_requests').ensure_index('alliance_id')

            self._table('alliance_roles').ensure_index([('alliance_id',pymongo.ASCENDING),('role',pymongo.ASCENDING)], unique=True)
            assert self.ROLE_DEFAULT == 0
            assert self.ROLE_LEADER == 4
            self._table('alliance_roles').update({'alliance_id':-1,'role':0}, {'$set': {'ui_name':'Rank I', 'perms':[]}}, upsert = True, multi = False) # Rank I
            self._table('alliance_roles').update({'alliance_id':-1,'role':1}, {'$set': {'ui_name':'Rank II', 'perms':['invite']}}, upsert = True, multi = False) # Rank II
            self._table('alliance_roles').update({'alliance_id':-1,'role':2}, {'$set': {'ui_name':'Rank III', 'perms':['invite','kick',]}}, upsert = True, multi = False) # Rank III
            self._table('alliance_roles').update({'alliance_id':-1,'role':3}, {'$set': {'ui_name':'Rank IV', 'perms':['invite','kick','promote']}}, upsert = True, multi = False) # Rank IV
            self._table('alliance_roles').update({'alliance_id':-1,'role':4}, {'$set': {'ui_name':'Rank V', 'perms':['invite','kick','promote','admin','leader']}}, upsert = True, multi = False) # Rank V

            self.seen_alliances = True
        return self._table(name)

    def create_alliance(self, ui_name, ui_descr, join_type, founder_id, logo, creat, continent, chat_motd='', chat_tag=None, reason=''):
        return self.instrument('create_alliance(%s)'%reason, self._create_alliance, (ui_name,ui_descr,join_type,founder_id,logo,creat,continent,chat_motd,chat_tag))
    def _create_alliance(self, ui_name, ui_descr, join_type, founder_id, logo, creat, continent, chat_motd, chat_tag):
        assert join_type in ('anyone', 'invite_only')
        assert continent
        tbl = self.alliance_table('alliances')
        props = {'ui_name': ui_name, 'ui_description':ui_descr, 'join_type':join_type, 'founder_id':founder_id, 'leader_id':founder_id,
                 'logo':logo, 'creation_time':creat, 'continent': continent, 'chat_motd':chat_motd, 'num_members_cache': 0}
        if chat_tag and len(chat_tag) > 0:
            assert len(chat_tag) == 3
            props['chat_tag'] = unicode(chat_tag.upper())
        while True: # loop on insert to get next unique _id
            last_alliance = list(tbl.find({}, {'_id':1}).sort([('_id',pymongo.DESCENDING)]).limit(1))
            my_id = (last_alliance[0]['_id']+1) if last_alliance else 1
            props['_id'] = my_id
            try:
                tbl.insert(props)
                break
            except pymongo.errors.DuplicateKeyError as e:
                # E11000 duplicate key error index: trtest_dan.alliances.$_id_  dup key: { : 1 }
                if ('alliances.$_id_' in e.args[0]):
                    # _id race condition
                    continue
                elif ('alliances.$chat_tag_' in e.args[0]):
                    return -1, "CANNOT_CREATE_ALLIANCE_TAG_IN_USE"
                elif ('alliances.$ui_name_' in e.args[0]):
                    return -1, "CANNOT_CREATE_ALLIANCE_NAME_IN_USE"
                else:
                    raise
        return my_id, None

    def delete_alliance(self, id, reason=''): return self.instrument('delete_alliance(%s)'%reason, self._delete_alliance, (id,))
    def _delete_alliance(self, id):
        self.alliance_table('alliances').remove({'_id':id})
        self.alliance_table('alliance_roles').remove({'alliance_id':id})
        self.alliance_table('alliance_members').remove({'alliance_id':id})
        self.alliance_table('alliance_invites').remove({'alliance_id':id})
        self.alliance_table('alliance_join_requests').remove({'alliance_id':id})
        self.alliance_score_cache().remove({'alliance_id':id})

    # note: modifications are rejected unless modifier_id has permission
    def modify_alliance(self, alliance_id, modifier_id, ui_name = None, ui_description = None, join_type = None, logo = None, leader_id = None, continent = None, chat_motd = None, chat_tag = None, reason = ''):
        return self.instrument('modify_alliance(%s)'%reason, self._modify_alliance, (alliance_id, modifier_id, ui_name, ui_description, join_type, logo, leader_id, continent, chat_motd, chat_tag))
    def _modify_alliance(self, alliance_id, modifier_id, ui_name, ui_description, join_type, logo, leader_id, continent, chat_motd, chat_tag):
        if (not self._check_alliance_member_perm(alliance_id, modifier_id, 'admin')): return False, None

        props = {}
        unset = {}
        if ui_name is not None: props['ui_name'] = unicode(ui_name)
        if ui_description is not None: props['ui_description'] = unicode(ui_description)
        if chat_motd is not None: props['chat_motd'] = unicode(chat_motd)
        if logo is not None: props['logo'] = str(logo)
        if chat_tag is not None:
            if len(chat_tag)>0:
                assert len(chat_tag) == 3
                props['chat_tag'] = unicode(chat_tag.upper())
            else:
                unset['chat_tag'] = 1
        assert leader_id is None
        if join_type is not None:
            assert join_type in ('anyone', 'invite_only')
            props['join_type'] = join_type
        if continent is not None:
            props['continent'] = continent

        try:
            mods = {'$set': props}
            if unset: mods['$unset'] = unset
            ret = self.alliance_table('alliances').update({'_id':alliance_id}, mods)
            if ret['updatedExisting']:
                return True, None
        except pymongo.errors.DuplicateKeyError as e:
            if ('alliances.$chat_tag_' in e.args[0]):
                return False, "CANNOT_CREATE_ALLIANCE_TAG_IN_USE"
            elif ('alliances.$ui_name_' in e.args[0]):
                return False, "CANNOT_CREATE_ALLIANCE_NAME_IN_USE"
            else:
                raise
        return False, None

    def decode_alliance_infos(self, infos, get_roles = False):
        if get_roles:
            role_data = self.alliance_table('alliance_roles').find({'alliance_id':-1})

        for info in infos:
            info['id'] = info['_id']; del info['_id']
            info['num_members'] = info['num_members_cache']; del info['num_members_cache']
            if get_roles:
                info['roles'] = {}
                for r in role_data:
                    r = r.copy()
                    del r['_id']; del r['alliance_id']
                    info['roles'][str(r['role'])] = r
                    #del r['role']

        return infos

    @classmethod
    def alliance_query_fields(cls, member_access):
        FIELDS = ['ui_name', 'ui_description', 'chat_tag', 'join_type', 'founder_id', 'leader_id', 'logo', 'num_members_cache', 'continent']
        if member_access: FIELDS += ['chat_motd']
        return dict(((field,1) for field in FIELDS))

    def get_alliance_list(self, limit, members_fewer_than = -1, open_join_only = False, match_continent = None, reason=''): return self.instrument('get_alliance_list(%s)'%reason, self._get_alliance_list, (limit, members_fewer_than, open_join_only, match_continent))
    def _get_alliance_list(self, limit, members_fewer_than, open_join_only, match_continent):
        qs = {}
        if open_join_only:
            qs['join_type'] = 'anyone'
        if members_fewer_than > 0:
            qs['num_members_cache'] = {'$lt': members_fewer_than}
        if match_continent:
            qs['continent'] = match_continent

        cur = self.alliance_table('alliances').find(qs, self.alliance_query_fields(False))
        if limit >= 1:
            # only return first 'limit' alliances, ordered with largest number of vacancies first (or fewest, if members_fewer_than is active)
            cur = cur.sort([('join_type',pymongo.ASCENDING),
                            ('num_members_cache',pymongo.DESCENDING if members_fewer_than > 0 else pymongo.ASCENDING),
                            ]).limit(limit)
        return self.decode_alliance_infos(list(cur))

    def search_alliance(self, name, limit = -1, reason=''): return self.instrument('search_alliance(%s)'%reason, self._search_alliance, (name, limit))
    def _search_alliance(self, name, limit):
        qs = {'ui_name': {'$regex': '^'+name, '$options':'i'}}
        if name:
            qs = {'$or':[qs,
                         {'chat_tag': {'$regex': '^'+name.upper()}}]}

        cur = self.alliance_table('alliances').find(qs, self.alliance_query_fields(False))
        if limit >= 1:
            cur = cur.limit(limit)
        return self.decode_alliance_infos(list(cur))

    def get_alliance_info(self, alliance_id, member_access=False, get_roles=False, reason=''): return self.instrument('get_alliance_info(%s)'%reason, self._get_alliance_info, (alliance_id,member_access,get_roles))
    def _get_alliance_info(self, alliance_id, member_access, get_roles):
        query_fields = self.alliance_query_fields(member_access)
        if type(alliance_id) is list:
            ret = [None]*len(alliance_id)
            entries = self.decode_alliance_infos(list(self.alliance_table('alliances').find({'_id':{'$in':alliance_id}}, query_fields)), get_roles = get_roles)
            for entry in entries:
                ret[alliance_id.index(entry['id'])] = entry
            return ret
        else:
            ret = self.alliance_table('alliances').find_one({'_id':alliance_id}, query_fields)
            if ret:
                ret = self.decode_alliance_infos([ret], get_roles = get_roles)[0]
            return ret

    # MEMBERSHIP #

    # get ID of alliance user belongs to
    # can pass an array of user IDs to perform a batch query
    def get_users_alliance(self, user_id, reason=''): return self.instrument('get_users_alliance(%s)'%reason, self._get_users_alliance, (user_id,))
    def _get_users_alliance(self, user_id):
        if type(user_id) is list:
            ret = [-1]*len(user_id)
            backmap = {}
            for i in xrange(len(user_id)):
                if user_id[i] not in backmap:
                    backmap[user_id[i]] = []
                backmap[user_id[i]].append(i)
            entries = self.alliance_table('alliance_members').find({'_id':{'$in':user_id}},{'_id':1,'alliance_id':1})
            for entry in entries:
                for index in backmap[entry['_id']]:
                    ret[index] = entry['alliance_id']
            return ret
        else:
            ret = self.alliance_table('alliance_members').find_one({'_id':user_id},{'alliance_id':1})
            return ret['alliance_id'] if ret else -1

    def get_users_alliance_membership(self, user_id, reason=''): return self.instrument('get_users_alliance_membership(%s)'%reason, self._get_users_alliance_membership, (user_id,))
    def _get_users_alliance_membership(self, user_id):
        ret = self.alliance_table('alliance_members').find_one({'_id':user_id})
        if ret:
            ret['user_id'] = ret['_id']; del ret['_id']
        return ret

    def check_alliance_member_perm(self, alliance_id, user_id, perm, reason=''): return self.instrument('check_alliance_member_perm(%s)'%reason, self._check_alliance_member_perm, (alliance_id,user_id,perm))
    def _check_alliance_member_perm(self, alliance_id, user_id, perm):
        member = self.alliance_table('alliance_members').find_one({'_id':user_id, 'alliance_id':alliance_id})
        if not member: return False
        role = member.get('role',self.ROLE_DEFAULT)
        role_data = self.alliance_table('alliance_roles').find_one({'alliance_id':-1,'role':role})
        if not role_data: return False
        return (perm in role_data.get('perms',[]))

    def promote_alliance_member(self, alliance_id, promoter_id, user_id, old_role, new_role, force = False, reason=''):
        return self.instrument('promote_alliance_member(%s)'%reason, self._promote_alliance_member, (alliance_id,promoter_id,user_id,old_role,new_role,force))
    def _promote_alliance_member(self, alliance_id, promoter_id, user_id, old_role, new_role, force):
        assert old_role != new_role
        if (not force):
            if promoter_id == user_id: return False
            if (not self._check_alliance_member_perm(alliance_id, promoter_id, 'promote')):
                return False

            # make sure the new role actually exists
            assert self.alliance_table('alliance_roles').find_one({'alliance_id':-1,'role':new_role})

            promoter_mem = self.alliance_table('alliance_members').find_one({'_id':promoter_id})
            if not promoter_mem: return False
            promoter_role = promoter_mem.get('role', self.ROLE_DEFAULT)
            if new_role >= self.ROLE_LEADER:
                if promoter_role < new_role: return False
            else:
                if promoter_role <= old_role or promoter_role <= new_role: return False

        qs = {'_id':user_id, 'alliance_id':alliance_id}
        if old_role == self.ROLE_DEFAULT:
            qs['$or'] = [{'role':{'$exists':False}}, {'role':self.ROLE_DEFAULT}]
        else:
            qs['role'] = old_role
        success = self.alliance_table('alliance_members').update(qs, {'$set':{'role':new_role}})['updatedExisting']
        if success and (not force) and (new_role >= self.ROLE_LEADER):
            # demote promoter
            assert self.alliance_table('alliance_members').update({'_id':promoter_id,'alliance_id':alliance_id}, {'$set':{'role':self.ROLE_LEADER-1}})['updatedExisting']
            assert self.alliance_table('alliances').update({'_id':alliance_id}, {'$set':{'leader_id':user_id}})['updatedExisting']
        return success

    def get_alliance_member_ids(self, alliance_id, reason=''): return [x['user_id'] for x in self.get_alliance_members(alliance_id, reason=reason)]
    def get_alliance_members(self, alliance_id, reason=''): return self.instrument('get_alliance_members(%s)'%reason, self._get_alliance_members, (alliance_id,))
    def _get_alliance_members(self, alliance_id):
        return map(lambda x: {'user_id':x['_id'], 'role':x.get('role',self.ROLE_DEFAULT), 'join_time':x['join_time']},
                   self.alliance_table('alliance_members').find({'alliance_id':alliance_id}))

    # INVITES #

    def send_alliance_invite(self, sender_id, user_id, alliance_id, time_now, expire_time, force = False, reason=''): return self.instrument('send_alliance_invite(%s)'%reason, self._send_alliance_invite, (sender_id, user_id, alliance_id, time_now, expire_time, force))
    def _send_alliance_invite(self, sender_id, user_id, alliance_id, time_now, expire_time, force):
        # check that sender has permission. We DO allow sending invites even if the alliance is open-join.
        if (not force) and (not self._check_alliance_member_perm(alliance_id, sender_id, 'invite')): return False
        # get rid of any existing invites
        self.alliance_table('alliance_invites').remove({'user_id':user_id, 'alliance_id':alliance_id})
        # record invite
        self.alliance_table('alliance_invites').insert({'user_id':user_id, 'alliance_id':alliance_id, 'creation_time':time_now, 'expire_time': expire_time})
        return True

    def send_join_request(self, user_id, alliance_id, time_now, expire_time, reason=''): return self.instrument('send_join_request(%s)'%reason, self._send_join_request, (user_id, alliance_id, time_now, expire_time))
    def _send_join_request(self, user_id, alliance_id, time_now, expire_time):
        # ensure alliance exists and is invite-only
        if not bool(self.alliance_table('alliances').find_one({'_id':alliance_id, 'join_type': 'invite_only'})): return False
        self.alliance_table('alliance_join_requests').remove({'user_id':user_id})
        self.alliance_table('alliance_join_requests').insert({'user_id':user_id, 'alliance_id':alliance_id, 'creation_time':time_now, 'expire_time':expire_time})
        return True

    def poll_join_requests(self, poller_id, alliance_id, time_now, reason=''): return self.instrument('poll_join_requests(%s)'%reason, self._poll_join_requests, (poller_id, alliance_id, time_now))
    def _poll_join_requests(self, poller_id, alliance_id, time_now):
        # check that poller has permission
        if (not self._check_alliance_member_perm(alliance_id, poller_id, 'invite')): return False
        return map(lambda x: x['user_id'], self.alliance_table('alliance_join_requests').find({'alliance_id':alliance_id, 'expire_time':{'$gte':time_now}}))

    def ack_join_request(self, poller_id, alliance_id, user_id, accept, time_now, max_members, reason=''): return self.instrument('ack_join_request(%s)'%reason, self._ack_join_request, (poller_id, alliance_id, user_id, accept, time_now, max_members))
    def _ack_join_request(self, poller_id, alliance_id, user_id, accept, time_now, max_members):
        # check that poller has permission
        if (not self._check_alliance_member_perm(alliance_id, poller_id, 'invite')): return False
        success = True
        if accept:
            success = self._join_alliance(user_id, alliance_id, time_now, max_members, force = True)
        if (not success) or (not accept):
            # _join_alliance cleans up the request in the success case
            self.alliance_table('alliance_join_requests').remove({'user_id':user_id})
        return success

    def am_i_invited(self, alliance_id, user_id, time_now, reason=''): return self.instrument('am_i_invited(%s)'%reason, self._am_i_invited, (alliance_id,user_id,time_now))
    def _am_i_invited(self, alliance_id, user_id, time_now):
        return bool(self.alliance_table('alliance_invites').find_one({'user_id':user_id, 'alliance_id':alliance_id, 'expire_time':{'$gte':time_now}}))

    def join_alliance(self, user_id, alliance_id, time_now, max_members, force = False, role = None, reason=''): return self.instrument('join_alliance(%s)'%reason, self._join_alliance, (user_id,alliance_id,time_now, max_members, force, role))
    def _join_alliance(self, user_id, alliance_id, time_now, max_members, force = False, role = None):
        if role is None: role = self.ROLE_DEFAULT
        if role != self.ROLE_DEFAULT: assert force
        success = True

        # check if player is already in an alliance
        old_alliance_id = self.get_users_alliance(user_id)
        if old_alliance_id >= 0:
            # check if it's obsolete
            if self.get_alliance_info(old_alliance_id):
                # player has a valid alliance
                success = False
            else:
                # it was a bogus entry, delete it (no need to update alliances table)
                self.alliance_table('alliance_members').remove({'_id':user_id})

        if success:
            # check that destination alliance really exists
            info = self.get_alliance_info(alliance_id)
            if not info:
                # alliance does not exist
                success = False

        if success:
            if info['join_type'] != 'anyone' and (not force):
                # check for applicable invite
                if not bool(self.alliance_table('alliance_invites').find_one({'user_id':user_id, 'alliance_id':alliance_id, 'expire_time':{'$gte':time_now}})):
                    # we're not invited
                    success = False

        if success:
            # check alliance size
            if self.alliance_table('alliances').find_and_modify(query = {'_id':alliance_id, 'num_members_cache': { '$lt': max_members }},
                                                                update = {'$inc': {'num_members_cache': 1} }):
                # race in between here is protected by the player login lock
                self.alliance_table('alliance_members').insert({'_id':user_id, 'alliance_id':alliance_id, 'role':role, 'join_time':time_now})
            else:
                success = False

        if success:
            # get rid of all outstanding invites
            self.alliance_table('alliance_invites').remove({'user_id':user_id})
            self.alliance_table('alliance_join_requests').remove({'user_id':user_id})

        return success

    # return value is same as do_maint_fix_leadership_problem
    def leave_alliance(self, user_id, reason=''): return self.instrument('leave_alliance(%s)'%reason, self._leave_alliance, (user_id,))
    def _leave_alliance(self, user_id):
        # use find_and_modify so we get the alliance_id we were a member of
        result = self.alliance_table('alliance_members').find_and_modify(query = {'_id':user_id}, remove = True)
        if result:
            new_info = self.alliance_table('alliances').find_and_modify(query = {'_id':result['alliance_id']}, update = {'$inc':{'num_members_cache':-1}}, new = True)
            if new_info.get('num_members_cache',0) <= 0 or result.get('role', self.ROLE_DEFAULT) >= self.ROLE_LEADER:
                # handle succession crisis
                return self.do_maint_fix_leadership_problem(result['alliance_id'], verbose = False)
        return 0

    def kick_from_alliance(self, kicker_id, alliance_id, user_id, force=False, reason=''): return self.instrument('kick_from_alliance(%s)'%reason, self._kick_from_alliance, (kicker_id, alliance_id, user_id, force))
    def _kick_from_alliance(self, kicker_id, alliance_id, user_id, force):
        if (not force): # check permissions
            if (not self._check_alliance_member_perm(alliance_id, kicker_id, 'kick')):
                return False
            kicker_mem = self.alliance_table('alliance_members').find_one({'_id':kicker_id,'alliance_id':alliance_id})
            user_mem = self.alliance_table('alliance_members').find_one({'_id':user_id,'alliance_id':alliance_id})
            if (not kicker_mem) or (not user_mem): return False
            if kicker_mem.get('role', self.ROLE_DEFAULT) <= user_mem.get('role', self.ROLE_DEFAULT):
                return False

        success = False
        if self.alliance_table('alliance_members').remove({'_id':user_id, 'alliance_id':alliance_id})['n']>0:
            self.alliance_table('alliances').update({'_id':alliance_id}, {'$inc':{'num_members_cache':-1}})
            success = True
        self.alliance_table('alliance_invites').remove({'alliance_id':alliance_id, 'user_id':user_id})
        self.alliance_table('alliance_join_requests').remove({'alliance_id':alliance_id, 'user_id':user_id})
        return success

    ###### UNIT DONATIONS ######

    def unit_donation_requests_table(self):
        # no indexes are necessary other than the default _id index
        return self._table('unit_donation_requests')

    def request_unit_donation(self, *args): return self.instrument('request_unit_donation', self._request_unit_donation, args)
    def _request_unit_donation(self, user_id, alliance_id, time_now, tag, cur_space, max_space):
        self.unit_donation_requests_table().update({'_id':user_id}, {'$set':{'alliance_id':alliance_id, 'time':time_now, 'tag':tag, 'space_left':max_space - cur_space, 'max_space':max_space}}, upsert=True)
        return True

    def invalidate_unit_donation_request(self, *args): return self.instrument('invalidate_unit_donation_request', self._invalidate_unit_donation_request, args)
    def _invalidate_unit_donation_request(self, user_id): return bool(self.unit_donation_requests_table().remove({'_id':user_id})['n'])

    def make_unit_donation(self, *args): return self.instrument('make_unit_donation', self._make_unit_donation, args)
    def _make_unit_donation(self, recipient_id, alliance_id, tag, space_array):
        # If the outstanding donation request exists, attempt to increment cur_space (atomically) by each entry
        # of space_array in sequence (it should be sorted largest to smallest), stopping when we find an amount
        # of space such that cur_space + donated_space <= max_space.
        for donated_space in space_array:
            ret = self.unit_donation_requests_table().find_and_modify(query = {'_id':recipient_id, 'alliance_id':alliance_id, 'tag':tag,
                                                                               'space_left': { '$gte': donated_space } },
                                                                      update = {'$inc':{'space_left':-donated_space}},
                                                                      new=True, fields={'space_left':1,'max_space':1})
            if ret:
                return (ret['max_space'] - ret['space_left'], ret['max_space']) # return cur, max
        return None

    ###### SCORES ######

    def parse_score_addr(self, addr):
        field_name, frequency, period = addr
        assert frequency in ('season', 'week')
        assert period >= 0
        return {'field':field_name, 'frequency':frequency, 'period':period}
    def hash_score_addr(self, addr):
        return '%s_%s_%d' % (addr['field'], addr['frequency'], addr['period'])

    ###### PLAYER SCORE TABLE ######

    def player_scores(self):
        tbl = self._table('player_scores')
        if not self.seen_player_scores:
            # necessary for correctness (unique scores per user/board), and per-user lookups
            tbl.ensure_index([('user_id',pymongo.ASCENDING),('field',pymongo.ASCENDING),('frequency',pymongo.ASCENDING),('period',pymongo.ASCENDING)], unique=True)
            # used for the "Top 10" query
            tbl.ensure_index([('field',pymongo.ASCENDING),('frequency',pymongo.ASCENDING),('period',pymongo.ASCENDING),('score',pymongo.DESCENDING)])
            self.seen_player_scores = True
        return tbl

    def player_scores_rank_cache_update(self, cur_season = None, cur_week = None):
        self._table('player_scores_rank_cache_inprogress').drop()
        try:
            prog = self._table('player_scores_rank_cache_inprogress')

            # obtain all in-use combinations of field/freq/period
            all_addrs = [x['_id'] for x in self.player_scores().aggregate([{'$group':{'_id':{'field':'$field','frequency':'$frequency','period':'$period'}}}])['result']]

            if cur_season is not None: all_addrs = filter(lambda x: (x['frequency']!='season') or (x['period']==cur_season), all_addrs)
            if cur_week is not None: all_addrs = filter(lambda x: (x['frequency']!='week') or (x['period']==cur_week), all_addrs)

            for addr in all_addrs:
                print 'Caching ranks for %-80s...' % repr(addr),
                sys.stdout.flush()
                start_time = time.time()

                data = self.player_scores().find(addr,{'_id':0,'user_id':1,'score':1})
                all_scores = set()
                users_by_score = {}
                for x in data:
                    all_scores.add(x['score'])
                    if x['score'] not in users_by_score: users_by_score[x['score']] = []
                    users_by_score[x['score']].append(x['user_id'])

                # rank=0 for highest score
                all_scores = sorted(list(all_scores), reverse=True)

                def row_generator(addr, all_scores, users_by_score):
                    for rank in xrange(len(all_scores)):
                        for user_id in users_by_score[all_scores[rank]]:
                            row = addr.copy()
                            row['user_id'] = user_id
                            row['score'] = all_scores[rank]
                            row['rank'] = rank
                            row['n'] = len(all_scores)
                            yield row

                prog.insert(row_generator(addr, all_scores, users_by_score))

                end_time = time.time()
                print 'done (%.2f ms)' % (1000.0*(end_time-start_time))

            # build index for per-user percentile/rank queries
            # note, we need to index this by user_id rather than by score, because scores used for ranking become stale
            prog.ensure_index([('user_id',pymongo.ASCENDING),('field',pymongo.ASCENDING),('frequency',pymongo.ASCENDING),('period',pymongo.ASCENDING)], unique=True, background=True)

            # atomically replace old cache with the new one
            prog.rename(self.slave_for_table('player_scores_rank_cache').dbconfig['table_prefix']+'player_scores_rank_cache', dropTarget=True)
        except:
            # clean up inprogress table
            prog.drop()
            raise

    # the "updates" parameter here is a list of (address, score) tuples
    def update_player_scores(self, player_id, updates, reason=''): return self.instrument('update_player_scores(%s)'%reason, self._update_player_scores, (player_id, updates))
    def _update_player_scores(self, player_id, updates):
        for addr, score in updates:
            qs = self.parse_score_addr(addr)
            qs['user_id'] = player_id
            self.player_scores().update(qs, {'$set':{'score': score}}, upsert=True, multi=False, w=0) # send asynchronously - this is a performance hotspot
        return True

    def get_player_score_leaders(self, addr, num, start = 0, reason = ''): return self.instrument('get_player_score_leaders(%s)'%reason, self._get_player_score_leaders, (addr, num, start))
    def _get_player_score_leaders(self, addr, num, start):
        addr = self.parse_score_addr(addr)
        ret = list(self.player_scores().find(addr, {'_id':0, 'user_id':1, 'score':1}).sort([('score',pymongo.DESCENDING)]).skip(start).limit(num))
        for i in xrange(len(ret)):
            ret[i]['absolute'] = ret[i]['score']; del ret[i]['score']
            ret[i]['rank'] = start+i
        return ret

    def get_player_scores(self, player_ids, addrs, rank = False, reason=''): return self.instrument('get_player_scores' + '+RANK' if rank else '' + '(%s)'%reason, self._get_player_scores, (player_ids, addrs, rank))
    def _get_player_scores(self, player_ids, addrs, rank):
        ret = [[None,]*len(addrs) for u in xrange(len(player_ids))]
        addrs = map(self.parse_score_addr, addrs)
        need_totals = {}

#        start_time = time.time()

        for i in xrange(len(addrs)):
            qs = addrs[i].copy(); qs['user_id'] = {'$in': player_ids}
            scores = list(self.player_scores().find(qs, {'_id':0, 'user_id':1, 'score':1}))
            for score in scores:
                u = player_ids.index(score['user_id'])
                ret[u][i] = {'absolute': score['score']}
                if rank and score['score'] > 0:
                    need_totals[self.hash_score_addr(addrs[i])] = True

#        end_time = time.time(); print "A %.2fms" % (1000.0*(end_time-start_time)); start_time = end_time

        if rank:
            n_totals = {}
            for addr in addrs:
                if need_totals.get(self.hash_score_addr(addr),False):
                    # this is actually the slowest part of the query - getting the total number of scores this addr
                    qs = addr.copy()
                    n_totals[self.hash_score_addr(addr)] = self.player_scores().find(qs,{'_id':0}).count()

#            end_time = time.time(); print "B %d of %d %.2fms" % (len(n_totals), len(addrs), 1000.0*(end_time-start_time)); start_time = end_time
            for u in xrange(len(player_ids)):
                for i in xrange(len(addrs)):
                    if ret[u][i]:
                        addr = addrs[i]
                        if ret[u][i]['absolute'] <= 0:
                            # if absolute score is zero, don't bother querying
                            total = 1000000 # use a fictional total so that the rank is like #999,999
                            ret[u][i]['rank'] = max(total-1, 0)
                            ret[u][i]['percentile'] = 1.0
                        else:
                            total = n_totals[self.hash_score_addr(addr)]
                            if total > 0:
                                qs = addr.copy()
                                qs['score'] = {'$gt': ret[u][i]['absolute']}
                                n_above_me = self.player_scores().find(qs,{'_id':0}).count()
                                ret[u][i]['rank'] = n_above_me
                                ret[u][i]['percentile'] = n_above_me/float(total)

#            end_time = time.time(); print "C %.2fms" % (1000.0*(end_time-start_time)); start_time = end_time
        return ret

    ###### ALLIANCE SCORE CACHE ######

    def alliance_score_cache(self):
        tbl = self._table('alliance_score_cache')
        if not self.seen_alliance_score_cache:
            tbl.ensure_index([('alliance_id',pymongo.ASCENDING),('field',pymongo.ASCENDING),('frequency',pymongo.ASCENDING),('period',pymongo.ASCENDING)], unique=True)
            tbl.ensure_index([('field',pymongo.ASCENDING),('frequency',pymongo.ASCENDING),('period',pymongo.ASCENDING),('score',pymongo.DESCENDING)])
            self.seen_alliance_score_cache = True
        return tbl

    def update_alliance_score_cache(self, alliance_id, addrs, weights, offset, reason = ''):
        return self.instrument('update_alliance_score_cache(%s)'%reason, self._update_alliance_score_cache, (alliance_id, addrs, weights, offset))
    def _update_alliance_score_cache(self, alliance_id, addrs, weights, offset):
        member_ids = self.get_alliance_member_ids(alliance_id)
        if len(member_ids) <= 0: return True
        addrs = map(self.parse_score_addr, addrs)
        for addr in addrs:
            if len(member_ids) > 0:
                qs = addr.copy(); qs['user_id'] = {'$in': member_ids}
                player_scores = list(self.player_scores().find(qs, {'user_id':1, 'score':1}))
            else:
                player_scores = []

            score_map = {}
            for row in player_scores:
                score_map[row['user_id']] = row['score']
            member_ids.sort(key = lambda id: -score_map.get(id,0))

            #print "SCORE_MAP", score_map

            total = 0.0
            for i in xrange(min(len(member_ids), len(weights))):
                sc = score_map.get(member_ids[i],0)
                total += weights[i] * (sc + offset.get(addr['field'],0))
            total = int(total)

            qs = addr.copy(); qs['alliance_id'] = alliance_id
            self.alliance_score_cache().update(qs, {'$set': {'score': total}}, upsert = True, multi = False)
        return True

    def get_alliance_score_leaders(self, addr, num, start = 0, reason = ''): return self.instrument('get_alliance_score_leaders(%s)'%reason, self._get_alliance_score_leaders, (addr, num, start))
    def _get_alliance_score_leaders(self, addr, num, start):
        addr = self.parse_score_addr(addr)
        ret = list(self.alliance_score_cache().find(addr, {'_id':0, 'alliance_id':1, 'score':1}).sort([('score',pymongo.DESCENDING)]).skip(start).limit(num))
        for i in xrange(len(ret)):
            ret[i]['absolute'] = ret[i]['score']; del ret[i]['score']
            ret[i]['rank'] = start+i
        return ret

    def get_alliance_score(self, alliance_id, addr, rank = False, reason=''): return self.instrument('get_alliance_score' + '+RANK' if rank else '' + '(%s)'%reason, self._get_alliance_score, (alliance_id, addr, rank))
    def _get_alliance_score(self, alliance_id, addr, rank):
        addr = self.parse_score_addr(addr)
        ret = None
        myscore = self.alliance_score_cache().find_one({'alliance_id':alliance_id, 'field':addr['field'], 'frequency':addr['frequency'], 'period':addr['period']},{'score':1})
        if myscore:
            ret = {'absolute':myscore['score']}
            if rank:
                n_total = self.alliance_score_cache().find({'field':addr['field'], 'frequency':addr['frequency'], 'period':addr['period']}).count()
                if n_total > 0:
                    n_above_me = self.alliance_score_cache().find({'field':addr['field'], 'frequency':addr['frequency'], 'period':addr['period'], 'score':{'$gt':myscore['score']}}).count()
                    ret['rank'] = n_above_me
                    ret['percentile'] = n_above_me/float(n_total)
        return ret

if __name__ == '__main__':
    import getopt
    import codecs
    import SpinSingletonProcess

    sys.stdout = codecs.getwriter('utf8')(sys.stdout)

    opts, args = getopt.gnu_getopt(sys.argv[1:], 'g:', ['reset', 'init', 'console', 'maint', 'region-maint=', 'clear-locks',
                                                      'winners', 'leaders', 'tournament-stat=', 'week=', 'season=', 'score-api=', 'game-id=',
                                                      'score-scope=', 'score-loc=', 'spend-week=',
                                                      'recache-player-ranks',
                                                      'recache-alliance-scores', 'test'])
    game_instance = SpinConfig.config['game_id']
    mode = None
    week = -1
    season = -1
    tournament_stat = None
    score_api = 'scores2'
    score_space_scope = None
    score_space_loc = None
    spend_week = None
    time_now = int(time.time())
    maint_region = None

    for key, val in opts:
        if key == '--reset': mode = 'reset'
        elif key == '--init': mode = 'init'
        elif key == '--console': mode = 'console'
        elif key == '--clear-locks': mode = 'clear-locks'
        elif key == '--maint': mode = 'maint' # global maintenance
        elif key == '--region-maint': mode = 'region-maint'; maint_region = val # region maintenance
        elif key == '--winners': mode = 'winners'
        elif key == '--leaders': mode = 'leaders'
        elif key == '--week': week = int(val)
        elif key == '--season': season = int(val)
        elif key == '--tournament-stat':
            tournament_stat = val
        elif key == '--score-api':
            assert val in ('scores1', 'scores2')
            score_api = val
        elif key == '--score-scope':
            import Scores2
            if val == 'ALL':
                score_space_scope = Scores2.SPACE_ALL
                score_space_loc = Scores2.SPACE_ALL_LOC
            else:
                score_space_scope = Scores2.SPACE_CONTINENT
        elif key == '--score-loc':
            import Scores2
            assert score_space_scope == Scores2.SPACE_CONTINENT
            score_space_loc = val
        elif key == '--spend-week': spend_week = int(val)
        elif key == '--recache-alliance-scores': mode = 'recache-alliance-scores'
        elif key == '--recache-player-ranks': mode = 'recache-player-ranks'
        elif key == '--test': mode = 'test'
        elif key == '-g' or key == '--game-id':
            game_instance = val

    game_id = game_instance[:-4] if game_instance.endswith('test') else game_instance

    if mode is None:
        print 'usage: SpinNoSQL.py MODE'
        print 'Modes:'
        print '    --maint                             Prune stale/invalid data from global tables'
        print '    --region-maint REGION_ID            Prune stale/invalid data from region tables'
        print '    --recache-player-ranks              Update player score rank cache'
        print '    --recache-alliance-scores --week N --season S  Recalculate all alliance scores for week N season S'
        print '    --winners --week N --season N --tournament-stat STAT  Report Alliance Tournament winners for week N (or season N) and trophy type TYPE (pve or pvp)'
        print '                       ^ if season is missing or < 0, then weekly score is used for standings, otherwise seasonal score is used'
        print '    --test                     Run regression test code (DESTROYS DATA)'
        sys.exit(1)

    client = NoSQLClient(SpinConfig.get_mongodb_config(game_instance))
    client.set_time(time_now)
    id_generator = SpinNoSQLId.Generator()
    id_generator.set_time(time_now)

    if mode in ('maint', 'winners', 'leaders', 'recache-player-ranks', 'recache-alliance-scores', 'test'):
        # these modes need access to gamedata for season/week or gamebucks info
        import SpinJSON
        gamedata = SpinJSON.load(open(SpinConfig.gamedata_filename(override_game_id = game_id)))
        gamedata['server'] = SpinJSON.load(open(SpinConfig.gamedata_component_filename('server_compiled.json', override_game_id = game_id)))
        cur_season = SpinConfig.get_pvp_season(gamedata['matchmaking']['season_starts'], time_now)
        cur_week = SpinConfig.get_pvp_week(gamedata['matchmaking']['week_origin'], time_now)

    if mode == 'region-maint':
        with SpinSingletonProcess.SingletonProcess('SpinNoSQL-region-maint-%s-%s' % (game_id, maint_region)):
            client.do_region_maint(maint_region)
    elif mode == 'maint':
        with SpinSingletonProcess.SingletonProcess('SpinNoSQL-global-maint-%s' % (game_id)):
            client.do_maint(time_now, cur_season, cur_week)

    elif mode == 'clear-locks':
        ID_LIST = [1112,1113,1114,1115]
        LOCK_FIELDS = {'LOCK_HOLDER':1,'LOCK_TIME':1,'LOCK_STATE':1,'LOCK_OWNER':1}
        client.player_cache().update({'_id':{'$in':ID_LIST}}, {'$unset':LOCK_FIELDS}, upsert=False, multi=True)
        for region in set(x['home_region'] for x in client.player_cache().find({'_id':{'$in':ID_LIST}},{'home_region':1}) if x.get('home_region',None)):
            client.region_table(region, 'map').update({'_id':{'$in':['h%d'%x for x in ID_LIST]}}, {'$unset': LOCK_FIELDS}, upsert=False, multi=True)

    elif mode == 'console':
        import code
        client.latency_func = lambda name, elapsed: sys.stdout.write('latency: %s %0.2fms\n' % (name, 1000.0*elapsed))
        console = code.InteractiveConsole({'client':client})
        if args:
            for arg in args:
                console.push(arg)
        else:
            console.interact()

    elif mode == 'recache-player-ranks':
        client.player_scores_rank_cache_update(season if season >= 0 else cur_season, week if week >= 0 else cur_week)

    elif mode == 'recache-alliance-scores':
        assert cur_week >= 0 and cur_season >= 0
        s2 = None
        if gamedata['server'].get('enable_scores2', False):
            import Scores2
            s2 = Scores2.MongoScores2(client)

        alliance_list = client.get_alliance_list(-1)
        for alliance in alliance_list:
            print 'Scores1: updating alliance', alliance['id'], 'week', cur_week, 'scores'
            addrs = [(field, freq, period) for field in ('trophies_pvp', 'trophies_pve', 'trophies_pvv') for freq in ('week',) for period in (cur_week,)]
            offsets = {'trophies_pvp':gamedata['trophy_display_offset']['pvp'],
                       'trophies_pve':gamedata['trophy_display_offset']['pve'],
                       'trophies_pvv':gamedata['trophy_display_offset'].get('pvv',0),
                                                }
            client.update_alliance_score_cache(alliance['id'], addrs, gamedata['alliances']['trophy_weights'][0:gamedata['alliances']['max_members']], offsets)

            if gamedata['server'].get('enable_scores2', False):
                it_a = [{'stat': stat, 'axes': {'space':[space_scope,space_loc], 'time':[time_scope,time_loc]}} \
                        for stat in ('trophies_pvp', 'trophies_pve', 'trophies_pvv') \
                        for space_scope in (Scores2.SPACE_ALL, Scores2.SPACE_CONTINENT) \
                        for space_loc in {Scores2.SPACE_ALL:[Scores2.SPACE_ALL_LOC], Scores2.SPACE_CONTINENT:gamedata['continents'].keys()}[space_scope] \
                        for time_scope in (Scores2.FREQ_ALL, Scores2.FREQ_SEASON, Scores2.FREQ_WEEK) \
                        for time_loc in {Scores2.FREQ_ALL:[0], Scores2.FREQ_SEASON:[cur_season], Scores2.FREQ_WEEK:[cur_week]}[time_scope]]
                print 'Scores2: updating alliance', alliance['id'], 'scores' # , '\n'.join(map(repr,it_a))
                s2.alliance_scores2_update_weighted(alliance['id'], it_a,
                                                    gamedata['alliances']['trophy_weights'][0:gamedata['alliances']['max_members']],
                                                    offsets)

    elif mode in ('winners', 'leaders'):
        import SpinUserDB

        s2 = None # scores2 API
        if score_api == 'scores2':
            import Scores2
            s2 = Scores2.MongoScores2(client)

        def display_point_count(gamedata, raw, tournament_stat):
            if tournament_stat in ('trophies_pvp', 'trophies_pve', 'trophies_pvv'):
                return raw + gamedata['trophy_display_offset'].get(tournament_stat.split('_')[1], 0)
            return raw

        freq = 'season' if season >= 0 else 'week'
        freq_name = 'SEASON'  if season >= 0 else 'WEEK'
        period = season if season >= 0 else week

        addr = (tournament_stat, freq, period)
        if s2:
            time_scope = {'season':Scores2.FREQ_SEASON, 'week':Scores2.FREQ_WEEK}[freq]
            stat_axes = (tournament_stat, Scores2.make_point(time_scope, period, score_space_scope, score_space_loc))
        else:
            assert tournament_stat in ('trophies_pvp', 'trophies_pve', 'trophies_pvv')

        if mode == 'leaders':
            # for STAT in conquests damage_inflicted resources_looted xp havoc_caused quarry_resources tokens_looted trophies_pvp hive_kill_points strongpoint_resources; do ./SpinNoSQL.py --leaders --season 3 --tournament-stat $STAT --score-scope continent --score-loc fb >> /tmp/`date +%Y%m%d`-tr-stat-leaders.txt; done

            leader_data = s2.player_scores2_get_leaders([stat_axes], 10)[0]
            assert time_scope == Scores2.FREQ_SEASON
            print 'TOP %s PLAYERS FOR SEASON %d' % (tournament_stat, season + gamedata['matchmaking']['season_ui_offset'])
            pc = dict((x['user_id'], x) for x in client.player_cache_lookup_batch([d['user_id'] for d in leader_data]) if x)
            #print leader_data

            for j in xrange(len(leader_data)):
                data = leader_data[j]
                user = pc[data['user_id']]
                if ('ui_name' not in user) and ('facebook_name' in user) and (' ' in user['facebook_name']) and len(user['facebook_name'].split(' ')[1]) > 0:
                    name = user['facebook_name'].split(' ')[0] + user['facebook_name'].split(' ')[1][0]
                else:
                    name = user.get('ui_name', user.get('facebook_name','Unknown'))

                detail = '%s L%2d' % (name, user.get('player_level',1))
                assert data['rank'] == j
                print "    #%2d %-24s with %5d (id %7d)" % (j+1, detail, display_point_count(gamedata, data['absolute'], tournament_stat), user['user_id'])


        elif mode == 'winners':
            assert tournament_stat in ('trophies_pvp', 'trophies_pve', 'trophies_pvv', 'strongpoint_resources')

            if s2:
                top_alliances = s2.alliance_scores2_get_leaders([stat_axes], 5)[0]
            else:
                top_alliances = client.get_alliance_score_leaders(addr, 5, 0)

            print '[color="#FFFF00"]TOP %s ALLIANCES FOR %sWEEK %d%s[/color]' % \
                  (tournament_stat, 'SEASON %d ' % (season+gamedata['matchmaking']['season_ui_offset']) if season >= 0 else '', week,
                   (' IN '+gamedata['continents'][score_space_loc]['ui_name']) if score_space_scope == Scores2.SPACE_CONTINENT else '')

            data = client.get_alliance_info([x['alliance_id'] for x in top_alliances])
            PRIZES = [10*x for x in gamedata['events']['challenge_pvp_ladder_with_prizes']['prizes']] # multiply by 10x to convert from FB Credits to gamebucks # [10000, 5000, 3000]
            WINNERS = 10 # number of players to split the prize within each winning alliance
            commands = []

            for i in xrange(len(top_alliances)):
                rank = top_alliances[i]
                info = data[i]

                if not info: continue

                members = client.get_alliance_member_ids(rank['alliance_id'])
                if not members: continue

                print ''
                print '%d) [COLOR="#FFD700"]%s[/COLOR] with %d points (id %5d)' % (rank['rank']+1, info['ui_name'], rank['absolute'], rank['alliance_id'])

                if i >= len(PRIZES): continue
                alliance_prize = PRIZES[i]

                if s2:
                    scores = s2.player_scores2_get(members, [stat_axes], rank = False)
                else:
                    scores = client.get_player_scores(members, [addr], rank = False)

                scored_members = [{'user_id': members[x], 'absolute': scores[x][0]['absolute'] if scores[x][0] is not None else 0} for x in xrange(len(members))]

                pc = client.player_cache_lookup_batch([member['user_id'] for member in scored_members])
                for j in xrange(len(pc)):
                    if pc[j]:
                        for FIELD in ('facebook_name', 'ui_name', 'player_level', 'home_region', 'ladder_player', 'money_spent'):
                            if FIELD in pc[j]:
                                scored_members[j][FIELD] = pc[j][FIELD]

                # note: use player level as tiebreaker, higher level wins
                scored_members.sort(key = lambda x: 100*x['absolute'] + x.get('player_level',1), reverse = True)

                player_prize = alliance_prize / min(len(scored_members), WINNERS)
                print '[COLOR="#FFFFFF"]Winners receive %s %s each:[/COLOR]' % (player_prize, gamedata['store']['gamebucks_ui_name'])

                for j in xrange(len(scored_members)):
                    member = scored_members[j]

                    is_tie = (j >= WINNERS and (member['absolute'] == scored_members[WINNERS-1]['absolute']) and (member.get('player_level',1) >= scored_members[WINNERS-1].get('player_level',1)))
                    if j < WINNERS or is_tie:
                        my_prize = player_prize
                    else:
                        my_prize = 0

                    name = member.get('ui_name', member.get('facebook_name','Unknown'))
                    if ('ui_name' not in member) and ('facebook_name' in member) and (' ' in member['facebook_name']) and len(member['facebook_name'].split(' ')[1]) > 0:
                        name = member['facebook_name'].split(' ')[0] + member['facebook_name'].split(' ')[1][0]
                    else:
                        name = member.get('ui_name', member.get('facebook_name','Unknown'))

                    if 0: # desperation
                        try:
                            name = name.encode('ascii')
                        except UnicodeEncodeError:
                            name = repr(name)

                    detail = '%s L%2d' % (name, member.get('player_level',1))
                    ladder_player = member.get('ladder_player',0)
                    if member.get('home_region',None) in gamedata['regions']:
                        ui_continent = gamedata['regions'][member['home_region']]['continent_id']
                    else:
                        ui_continent = 'UNKNOWN'

                    if spend_week:
                        spend_total = member.get('money_spent',0)
                        if spend_total > 0:
                            player = SpinJSON.loads(SpinUserDB.driver.sync_download_player(member['user_id']))
                            ONE_WEEK = 7*24*60*60
                            before = (spend_week - 1) * ONE_WEEK + gamedata['matchmaking']['week_origin'] - player['creation_time']
                            during = (spend_week + 0) * ONE_WEEK + gamedata['matchmaking']['week_origin'] - player['creation_time']
                            after  = (spend_week + 1) * ONE_WEEK + gamedata['matchmaking']['week_origin'] - player['creation_time']
                            spend_before, spend_during, spend_after = [
                                sum((value for key, value in player['history']['money_spent_at_time'].iteritems() \
                                     if int(key) >= start_age and int(key) < start_age + ONE_WEEK), 0) \
                                for start_age in before, during, after]
                        else: # use dummy to save time
                            spend_before = spend_during = spend_after = 0
                        spend_data = 'TOTAL $%05.02f week before $%05.02f week of $%05.02f week after $%05.02f' % (spend_total, spend_before, spend_during, spend_after)
                    else:
                        spend_data = '$%05.02f' % member.get('money_spent',0)

                    if my_prize <= 0: # or (not ladder_player):
                        print "    #%2d %-24s with %5d points does not win %s (id %7d continent %s spend %s)" % (j+1, detail, display_point_count(gamedata, member['absolute'], tournament_stat), gamedata['store']['gamebucks_ui_name'], member['user_id'], ui_continent, spend_data)
                    else:
                        print "    #%2d%s %-24s with %5d points WINS %6d %s (id %7d continent %s spend %s)" % (j+1 if (not is_tie) else WINNERS, '(tie)' if is_tie else '',
                                                                                        detail, display_point_count(gamedata, member['absolute'], tournament_stat), my_prize, gamedata['store']['gamebucks_ui_name'], member['user_id'], ui_continent, spend_data)
                        commands.append("./check_player.py %d --give-item gamebucks --melt-hours -1 --item-stack %d --give-item-subject 'Tournament Prize' --give-item-body 'Congratulations, here is your Tournament prize for %sWeek %d!  Click the prize to collect it.' --item-log-reason 'tournament_prize_s%d_w%d'" % (member['user_id'], my_prize, ('Season %d ' % (season+gamedata['matchmaking']['season_ui_offset'])) if season >= 0 else '', week, season, week))

            print "COMMANDS"
            print '\n'.join(commands)

    elif mode == 'test':
        print 'TEST'

        # TEST FACEBOOK ID TABLE
        client.facebook_id_table().drop(); client.seen_facebook_ids = False
        assert client.facebook_id_to_spinpunch_single('6', True) == 1111
        assert client.facebook_id_to_spinpunch_single('example1', True) == 1112
        assert client.facebook_id_to_spinpunch_single('6', False) == 1111
        assert client.facebook_id_to_spinpunch_single('example1', False) == 1112
        assert client.facebook_id_to_spinpunch_single('99999', False) == -1
        assert client.facebook_id_to_spinpunch_batch(['example1','6','99999']) == [1112,1111,-1]
        assert client.get_user_id_range() == [1111,1112]

        # TEST DAU
        client.dau_table(time_now).drop(); client.seen_dau = {}
        assert client.dau_get(time_now) == 0
        client.dau_record(time_now, 123, 1, 100)
        client.dau_record(time_now, 456, 1, 100)
        client.dau_record(time_now, 123, 2, 100)
        client.dau_record(time_now, 777, 2, 100)
        assert client.dau_get(time_now) == 3
        client.dau_record(time_now - 3*86400, 777, 4, 100)

        # TEST LOCKS
        OWNER = 567
        client.player_cache().drop(); client.seen_player_cache = False
        client.player_cache().insert({'_id':1112, 'facebook_id':'example1', 'ui_name':'Dan', 'last_mtime': time_now - 60, 'LOCK_GENERATION':5})
        assert client.player_lock_acquire_login(1112, OWNER) == (client.LOCK_LOGGED_IN,5)
        assert client.player_lock_acquire_login(1112, 9999)[0] < 0
        assert client.player_lock_acquire_attack(1112, 665, -1) < 0 # already locked
        client.player_cache().update({'_id':1112},{'$set':{'LOCK_TIME':client.time-2*client.LOCK_TIMEOUT}})
        assert client.player_lock_acquire_login(1112, OWNER) == (client.LOCK_LOGGED_IN,5) # stale lock is busted

        assert client.player_lock_get_state_batch([1112,9999]) == [(client.LOCK_LOGGED_IN,OWNER),(client.LOCK_OPEN,-1)]
        assert client.player_lock_acquire_login(1113, OWNER) == (client.LOCK_LOGGED_IN,-1) # try uncreated player entry
        assert client.player_lock_release(1113, 666, -1, 6)
        assert not client.player_lock_release(1113, 666, -1, 6) # already released
        assert client.player_lock_acquire_attack(1113, 665, -1) < 0 # stale generation
        assert client.player_lock_acquire_attack(1113, 666, -1) == client.LOCK_BEING_ATTACKED
        assert client.player_lock_release(1113, 667, -1, -1)
        assert client.player_lock_release(1112, -1, -1, -1)

        # TEST PLAYER CACHE
        assert client.player_cache_update(1112, {'ui_name':'Dr. Gonzo 1112'})
        assert client.player_cache_update(1117, {'ui_name':'Dr. Gonzo 1117', 'player_level':99})
        assert client.player_cache_update(1113, {'ui_name':'Dr. Gonzo 1113', 'facebook_id': 'example2'}, overwrite=True)
        assert client.player_cache_lookup_batch([1112,1113,9999], fields = ['ui_name']) == [{'user_id': 1112, u'ui_name': u'Dr. Gonzo 1112'}, {'user_id': 1113, u'ui_name': u'Dr. Gonzo 1113'}, {}]
        assert client.player_cache_lookup_batch([1112,1113,9999]) == [{u'last_mtime': client.time, 'user_id': 1112, u'ui_name': u'Dr. Gonzo 1112', u'facebook_id': u'example1'}, {u'last_mtime': client.time, 'user_id': 1113, u'ui_name': u'Dr. Gonzo 1113', u'facebook_id': u'example2'}, {}]
        assert sorted(client.get_users_modified_since(1234)) == sorted([1112,1113,1117])

        # TEST MESSAGES
        client.message_table().drop(); client.seen_messages = False
        assert client.msg_poll([9999,1112]) == [0,0]
        client.msg_send([{'to':[1112],
                          'type':'donated_units',
                          'attachments':[{'spec':'blaster_droid'},{'spec':'blaster_droid'},{'spec':'elevation_droid'}],
                          'from':1115}])
        assert client.msg_poll([9999,1112]) == [0,1]
        gotten = client.msg_recv(1112)
        assert len(gotten) == 1
        client.msg_ack(1112, [gotten[0]['msg_id']])
        assert client.msg_poll([9999,1112]) == [0,0]
        assert client.msg_recv(1112) == []

        client.msg_send([{'to': [1112,1113],
                          'type': 'resource_gift',
                          'from': 6666,
                          'from_fbid': u"Ersan K\u00f6rpe",
                          'unique_per_sender': 'resource_gift'}])
        client.msg_send([{'to': [1112,1113],
                          'type': 'resource_gift',
                          'from': 6666,
                          'from_fbid': u"Ersan K\u00f6rpe",
                          'unique_per_sender': 'resource_gift'}])
        gotten = client.msg_recv(1113, type_filter=['resource_gift'])
        assert len(gotten) == 1
        client.msg_ack(1113,[gotten[0]['msg_id']])

        # TEST ABTESTS

        testname = 'scratch'
        client.abtest_table().remove({'name':testname})
        for cohort in ('cohortA','cohortB'):
            assert client.abtest_join_cohort(testname, cohort, -1)
            assert not client.abtest_join_cohort(testname, cohort, 0)
            assert client.abtest_join_cohort(testname, cohort, 10)
            assert client.abtest_join_cohort(testname, cohort, 10)
            assert client.abtest_join_cohort(testname, cohort, 3)
            assert not client.abtest_join_cohort(testname, cohort, 3)
            assert client.abtest_join_cohort(testname, cohort, 5)
            assert not client.abtest_join_cohort(testname, cohort, 2)

        # TEST REGION MAP

        region = 'scratch'
        for TABLE in ('map', 'map_deletions', 'fixed', 'mobile'):
            client._table(client.region_table_name(region, TABLE)).drop()

        # test objects
        obj1 = {'obj_id': id_generator.generate(), 'owner_id': 1112, 'base_id': 'h1112', 'kind': 'mobile', 'spec': 'mining_droid'}
        obj2 = {'obj_id': id_generator.generate(), 'owner_id': 1112, 'base_id': 'h1113', 'kind': 'mobile',  'spec': 'heavy_miner'}
        client.update_mobile_object(region, obj1)
        client.update_mobile_object(region, obj2)
        obj1['spec'] = 'blaster_droid'
        client.update_mobile_object(region, obj1)
        print client.get_mobile_objects_by_owner(region, 1112)
        client.drop_mobile_objects_by_base(region, 'h1112')
        client.drop_mobile_objects_by_base(region, 'h1113')

        # test features
#        old_time = client.time
#        client.set_time(old_time+600)
        assert -client.LOCK_BEING_ATTACKED == client.map_feature_lock_acquire(region, 'h1112', 1112, generation = -1)
        assert True == client.create_map_feature(region, 'h1112', {'base_type':'home','base_landlord_id':1112,'base_map_loc':[50,50]}, originator=1112, exclusive = -1)

        # base should be created with lock taken
        assert -client.LOCK_BEING_ATTACKED == client.map_feature_lock_acquire(region, 'h1112', 1112, generation = -1)
        client.map_feature_lock_release(region, 'h1112', 1112)

        # exclusion zone should work
        assert False == client.create_map_feature(region, 'h1112b', {'base_type':'home','base_landlord_id':1112,'base_map_loc':[51,51]}, exclusive = 3)
        assert False == client.create_map_feature(region, 'h1112b', {'base_type':'home','base_landlord_id':1112,'base_map_loc':[50,50]}, exclusive = 0)
        assert True == client.create_map_feature(region, 'h1113', {'base_type':'home','base_landlord_id':1113,'base_map_loc':[40,40]}, originator=1113, exclusive = -1)
        assert False == client.move_map_feature(region, 'h1113', {'base_map_loc':[51,51]}, old_loc = [40,40], exclusive=3)
        assert False == client.move_map_feature(region, 'h1113', {'base_map_loc':[50,50]}, old_loc = [40,40], exclusive=0)
        assert client.get_map_feature_by_base_id(region, 'h1113')['base_map_loc'] == [40,40]
        assert True == client.move_map_feature(region, 'h1113', {'base_map_loc':[30,30]}, old_loc = [40,40], exclusive=0)
        assert client.get_map_feature_by_base_id(region, 'h1113')['base_map_loc'] == [30,30]
        client.map_feature_lock_release(region, 'h1113', 1113, generation = 50)

        client.update_map_feature(region, 'h1112', {'protection_end_time':1234})

        print 'GET', client.get_map_feature_by_base_id(region, 'h1112')
        print 'GET', list(client.get_map_features_by_landlord_and_type(region, 1112, 'home'))
        print 'GET ALL', list(client.get_map_features(region, -1))
        assert not list(client.get_map_features(region, client.time+50))
        print 'GET INCR', list(client.get_map_features(region, client.time+50))

        test_coords = [(x,x) for x in xrange(60,200)]
        CHECKS=100
        start_time = time.time()
        for i in xrange(CHECKS):
            assert not client.map_feature_occupancy_check(region, [[49,49],[51,51],[54,54]] + test_coords)
            assert client.map_feature_occupancy_check(region, [[49,49],[50,50],[54,54]] + test_coords)
        end_time = time.time()
        print 'occupancy check in: %.2fms' % (((end_time-start_time)/CHECKS)*1000)

        assert client.LOCK_BEING_ATTACKED == client.map_feature_lock_acquire(region, 'h1112', 1112, generation = -1)
        assert client.map_feature_lock_get_state_batch(region, ['h1112','h1113','asdf']) == [(client.LOCK_BEING_ATTACKED,1112),(client.LOCK_OPEN,-1),(client.LOCK_OPEN,-1)]

        # mutex should prevent second lock
        assert -client.LOCK_BEING_ATTACKED == client.map_feature_lock_acquire(region, 'h1112', 1112, generation = -1)

        # stale lock should be acquirable
        client.region_table(region, 'map').update({'_id':'h1112'},{'$set':{'LOCK_TIME':client.time-2*client.LOCK_TIMEOUT}})
        assert client.LOCK_BEING_ATTACKED == client.map_feature_lock_acquire(region, 'h1112', 1112, generation = -1)

        # prune should clear stale locks
        client.region_table(region, 'map').update({'_id':'h1112'},{'$set':{'LOCK_TIME':client.time-2*client.LOCK_TIMEOUT}})
        client.do_region_maint(region)
        assert client.LOCK_BEING_ATTACKED == client.map_feature_lock_acquire(region, 'h1112', 1112, generation = -1)

        # keepalive should refresh locks
        client.region_table(region, 'map').update({'_id':'h1112'},{'$set':{'LOCK_TIME':client.time-2*client.LOCK_TIMEOUT}})
        client.map_feature_lock_keepalive_batch(region, ['h1112','h1113'])
        assert -client.LOCK_BEING_ATTACKED == client.map_feature_lock_acquire(region, 'h1112', 1112, generation = -1)

        # acquire with old generation should fail
        client.map_feature_lock_release(region, 'h1112', 1112, generation = 50)
        assert -client.LOCK_BEING_ATTACKED == client.map_feature_lock_acquire(region, 'h1112', 1112, generation = 49)

        if 1:
            client.drop_map_feature(region, 'h1112')
            client.drop_map_feature(region, 'h1113')
            assert not list(client.get_map_features(region, -1))
            print 'deletions:', list(client.get_map_features(region, client.time-50))

        # test alliances
        for TABLE in ('alliances', 'alliance_members', 'alliance_invites', 'alliance_join_requests'):
            client.alliance_table(TABLE).drop()
        client.seen_alliances = False

        alliance_1 = client.create_alliance(u'Democratic Mars Union', "We are awesome", 'anyone', 1112, 'tetris_red', time_now, 'fb')[0]
        assert alliance_1 > 0
        assert client.create_alliance(u'Democratic Mars Union', "We are awesomer", 'anyone', 1112, 'tetris_red', time_now, 'fb')[0] < 0 # duplicate name
        alliance_2 = client.create_alliance(u'Mars Federation', "We are cool", 'anyone', 1113, 'bullseye_tan', time_now, 'fb', chat_tag='123')[0]
        assert alliance_2 > 0
        assert client.create_alliance(u'Temp Alliance', "We are dead", 'anyone', 1120, 'star_orange', time_now, 'fb')[0] > 0

        print "ALLIANCE INFO (single)", client.get_alliance_info(alliance_2, reason = 'test')
        print "ALLIANCE INFO (single, with roles)", client.get_alliance_info(alliance_2, get_roles = True, reason = 'test')
        print "ALLIANCE INFO (multi)\n", '\n'.join(map(repr, client.get_alliance_info([alliance_1,alliance_2,999], reason = 'test2')))

        MAX_MEMBERS = 2

        assert not client.join_alliance(1120, 999, time_now, MAX_MEMBERS) # alliance does not exist
        assert client.join_alliance(1112, alliance_1, time_now, MAX_MEMBERS, role = client.ROLE_LEADER, force = True)
        assert not client.join_alliance(1112, alliance_2, time_now, MAX_MEMBERS) # already in alliance
        assert not client.join_alliance(1112, alliance_1, time_now, MAX_MEMBERS) # already in alliance
        assert client.join_alliance(1115, alliance_1, time_now, MAX_MEMBERS)
        assert not client.join_alliance(1114, alliance_1, time_now, MAX_MEMBERS) # too many members

        assert client.join_alliance(1113, alliance_2, time_now, MAX_MEMBERS, role = client.ROLE_LEADER, force = True)

        assert client.modify_alliance(alliance_1, 1112, ui_name = 'New Democratic Mars Union', chat_tag='ABC')[0]
        assert not client.modify_alliance(alliance_1, 1119, ui_name = 'New Democratic Mars Union')[0] # permissions fail
        assert not client.modify_alliance(alliance_1, 1112, ui_name = 'Mars Federation')[0] # duplicate name
        assert not client.modify_alliance(alliance_2, 1113, chat_tag='ABC')[0] # duplicate tag

        assert not client.send_alliance_invite(9999, 1117, alliance_2, time_now, time_now+3600) # not sent by leader
        assert client.send_alliance_invite(1113, 1117, alliance_2, time_now, time_now+3600)
        assert client.join_alliance(1117, alliance_2, time_now, MAX_MEMBERS)
        assert not client.join_alliance(1118, alliance_2, time_now, MAX_MEMBERS) # too many members
        assert client.leave_alliance(1117) == 0
        assert client.leave_alliance(1117) == 0
        assert client.join_alliance(1118, alliance_2, time_now, MAX_MEMBERS)
        assert client.kick_from_alliance(1113, alliance_2, 1118)

        assert sorted(client.get_alliance_member_ids(alliance_1)) == [1112,1115]
        assert client.get_alliance_members(alliance_2) == [{'user_id':1113,'role':client.ROLE_LEADER,'join_time':time_now}]
        assert client.get_alliance_info(alliance_1)['num_members'] == 2
        assert client.get_alliance_info(alliance_2)['num_members'] == 1

        assert client.modify_alliance(alliance_2, 1113, join_type='invite_only')
        assert not client.join_alliance(1118, alliance_2, time_now, MAX_MEMBERS) # not invited
        assert not client.am_i_invited(alliance_2, 1118, time_now)
        assert client.send_alliance_invite(1113, 1118, 2, time_now, time_now+300)
        assert client.am_i_invited(alliance_2, 1118, time_now)
        assert client.join_alliance(1118, alliance_2, time_now, MAX_MEMBERS)
        assert client.leave_alliance(1118) == 0
        assert not client.join_alliance(1118, alliance_2, time_now, MAX_MEMBERS) # not invited
        assert client.send_join_request(1118, alliance_2, time_now, time_now+300)
        assert client.poll_join_requests(1113, alliance_2, time_now) == [1118]
        assert client.ack_join_request(1113, alliance_2, 1118, True, time_now, MAX_MEMBERS)

        print "ALLIANCE LIST (unlimited)", client.get_alliance_list(-1)
        print "ALLIANCE LIST (limited)", client.get_alliance_list(1)
        print "ALLIANCE LIST (open join only)", client.get_alliance_list(1, open_join_only = True)
        print "SEARCH (unlimited)", client.search_alliance('mar')
        print "SEARCH (limited)", client.search_alliance('', limit = 1)


        assert client.get_users_alliance(1112, reason = 'hello') == alliance_1
        assert client.get_users_alliance_membership(1112, reason = 'hello') == {'user_id':1112, 'alliance_id':alliance_1, 'role':client.ROLE_LEADER, 'join_time':time_now}
        assert client.get_users_alliance(1113, reason = 'hello') == alliance_2
        assert client.get_users_alliance([1112,1113,1115,9999]) == [1, 2, 1, -1]

        # deliberately create messed-up leadership situations
        alliance_4 = client.create_alliance(u'Leaderless Alliance', "", 'anyone', 1120, 'star_orange', time_now, 'fb')[0]
        assert alliance_4 > 0
        assert client.join_alliance(1114, alliance_4, time_now, MAX_MEMBERS)
        alliance_5 = client.create_alliance(u'Contested Alliance', "", 'anyone', 1116, 'star_orange', time_now, 'fb')[0]
        assert alliance_5 > 0
        assert client.join_alliance(1116, alliance_5, time_now, MAX_MEMBERS, role = client.ROLE_LEADER, force = True)
        assert client.join_alliance(1117, alliance_5, time_now, MAX_MEMBERS, role = client.ROLE_LEADER, force = True)

        assert client.do_maint_fix_leadership_problem(alliance_1) == 0
        assert client.do_maint_fix_leadership_problem(alliance_2) == 0
        assert client.do_maint_fix_leadership_problem(alliance_4) == 1114
        assert client.do_maint_fix_leadership_problem(alliance_5) == 1117

        # alliance_4 now has single leader 1114
        # alliance_5 now has single leader 1117 and members 1116
        assert not client.promote_alliance_member(alliance_5, 9999, 1116, client.ROLE_DEFAULT, client.ROLE_LEADER) # no permissions
        assert not client.promote_alliance_member(alliance_5, 1116, 1117, client.ROLE_DEFAULT, client.ROLE_LEADER) # no permissions

        assert client._check_alliance_member_perm(alliance_5, 1117, 'promote')
        assert client.promote_alliance_member(alliance_5, 1117, 1116, client.ROLE_LEADER-1, client.ROLE_DEFAULT+1)
        assert not client.promote_alliance_member(alliance_5, 1117, 1116, client.ROLE_DEFAULT, client.ROLE_DEFAULT+1) # old_role mismatch
        assert client.promote_alliance_member(alliance_5, 1117, 1116, client.ROLE_DEFAULT+1, client.ROLE_DEFAULT)

        assert client.promote_alliance_member(alliance_5, 1117, 1116, client.ROLE_DEFAULT, client.ROLE_LEADER) # trade leadership
        assert client.get_users_alliance_membership(1116) == {'user_id':1116, 'alliance_id':alliance_5, 'role':client.ROLE_LEADER, 'join_time': time_now}
        assert client.get_users_alliance_membership(1117) == {'user_id':1117, 'alliance_id':alliance_5, 'role':client.ROLE_LEADER-1, 'join_time': time_now}
        assert client.do_maint_fix_leadership_problem(alliance_5, verbose=False) == 0 # make sure transition was smooth

        assert client.leave_alliance(1116) == 1117 # leadership should pass to 1117
        assert client.get_users_alliance_membership(1117) == {'user_id':1117, 'alliance_id':alliance_5, 'role':client.ROLE_LEADER, 'join_time': time_now}

        # test unit donations
        client.unit_donation_requests_table().drop()
        TAG = 1234
        assert client.request_unit_donation(1112, alliance_1, time_now, TAG, 10, 100)
        assert client.make_unit_donation(1112, alliance_1, TAG, [10]) == (20, 100)
        assert client.make_unit_donation(1112, alliance_1, TAG, [100]) is None
        assert client.make_unit_donation(1112, alliance_1, TAG, [100,80,10]) == (100, 100)
        assert client.make_unit_donation(1112, alliance_1, TAG, [1]) is None

        # test scores
        client.player_scores().drop(); client.seen_player_scores = False
        client.alliance_score_cache().drop(); client.seen_alliance_score_cache = False

        client.update_player_scores(1112, [[('trophies_pvp','season',0),150],[('trophies_pvp','week',0),100],[('trophies_pvp','week',1),50]])
        client.update_player_scores(1113, [[('trophies_pvp','season',0),170],[('trophies_pvp','week',0),110],[('trophies_pvp','week',1),60]])
        client.update_player_scores(1114, [[('trophies_pvp','season',0),75],[('trophies_pvp','week',0),50],[('trophies_pvp','week',1),25]])
        client.update_player_scores(1115, [[('trophies_pvp','season',0),10],[('trophies_pvp','week',0),10],[('trophies_pvp','week',1),5]])
        assert client.get_player_score_leaders(('trophies_pvp','season',0), 2, start = 1) == \
               [{'user_id': 1112, 'rank': 1, 'absolute': 150}, {'user_id': 1114, 'rank': 2, 'absolute': 75}]
        assert client.get_player_scores([1112,1114], [('trophies_pvp','season',0),('trophies_pvp','week',1)], rank = True) == \
               [[{'percentile': 0.25, 'rank': 1, 'absolute': 150}, {'percentile': 0.25, 'rank': 1, 'absolute': 50}], [{'percentile': 0.5, 'rank': 2, 'absolute': 75}, {'percentile': 0.5, 'rank': 2, 'absolute': 25}]]

        client.update_player_scores(1115, [[('trophies_pvp','season',0),12],[('trophies_pvp','week',0),12],[('trophies_pvp','week',1),6]])
        for ALLIANCE in (alliance_1, alliance_2):
            client.update_alliance_score_cache(ALLIANCE, [('trophies_pvp','season',0), ('trophies_pvp','week',1)],
                                               gamedata['alliances']['trophy_weights'][0:gamedata['alliances']['max_members']],
                                               {'trophies_pvp':gamedata['trophy_display_offset']['pvp'],
                                                'trophies_pve':gamedata['trophy_display_offset']['pve']})
        assert client.get_alliance_score_leaders(('trophies_pvp','week',1), 5) == \
               [{'rank': 0, 'alliance_id': 2, 'absolute': 30}, {'rank': 1, 'alliance_id': 1, 'absolute': 28}]
        assert client.get_alliance_score(alliance_1, ('trophies_pvp','week',1), rank = True) == \
               {'percentile': 0.5, 'rank': 1, 'absolute': 28}
        assert client.get_alliance_score(alliance_2, ('trophies_pvp','week',1), rank = True) == \
               {'percentile': 0, 'rank': 0, 'absolute': 30}

        # test maintenance
        #client.do_maint(time_now, cur_season, cur_week)

        print 'OK!'
