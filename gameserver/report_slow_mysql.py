#!/usr/bin/env python

# Copyright (c) 2015 SpinPunch. All rights reserved.
# Use of this source code is governed by an MIT-style license that can be
# found in the LICENSE file.

# send out reminders about slow SQL queries

import sys, time, getopt, socket
import SpinReminders
import SpinConfig
import SpinSQLUtil
import MySQLdb

time_now = int(time.time())

if __name__ == '__main__':
    verbose = True
    do_prune = False
    dry_run = False
    min_sec = 75
    email_to = None

    opts, args = getopt.gnu_getopt(sys.argv[1:], 'q', ['prune','min-sec=','dry-run','email='])

    for key, val in opts:
        if key == '-q': verbose = False
        elif key == '--prune': do_prune = True
        elif key == '--min-sec': min_sec = int(val)
        elif key == '--dry-run': dry_run = True
        elif key == '--email': email_to = val

    if len(args) < 1:
        sys.stderr.write('please specify mysql_servers entry\n')
        sys.exit(1)
    mysql_server = args[0]

    sql_util = SpinSQLUtil.MySQLUtil()
    if not verbose: sql_util.disable_warnings()

    cfg = SpinConfig.get_mysql_config(mysql_server)
    con = MySQLdb.connect(*cfg['connect_args'], **cfg['connect_kwargs'])

    cur = con.cursor(MySQLdb.cursors.DictCursor)

    subject = 'Automated reminder from '+socket.gethostname()
    body = None

    cur.execute('''SELECT start_time, user_host, TIME_TO_SEC(query_time) AS total_sec, db, sql_text
                   FROM mysql.slow_log
                   WHERE start_time >= NOW() - INTERVAL 24 HOUR and TIME_TO_SEC(query_time) >= %s
                   ORDER BY total_sec DESC;''', [min_sec,])
    rows = cur.fetchall()
    for row in rows:
        if not body:
            body = 'Slow SQL queries during the last 24 hours, slowest ones listed first:\n'
        body += '\n------ Duration %d sec at %s on DB %s by "%s" ------\n%s\n' % (row['total_sec'], row['start_time'], row['db'], row['user_host'], row['sql_text'])

    con.commit()

    if dry_run:
        print "HERE", '\n', subject, '\n', body
    else:
        recip_list = [
                      #{"type":"hipchat", "room":"Analysis", "ats":[]},
                      {"type":"slack", "channel":"#analysis", "ats":[]}
                      ]
        if email_to:
            recip_list.append({'type': 'email', 'to':[{'name': 'Analytics Tech', 'address': email_to}]})

        if body:
            SpinReminders.send_reminders('SpinPunch', recip_list, subject, body, dry_run = dry_run)

    if do_prune:
        if verbose: print 'pruning', 'mysql.slow_log'
        cur.execute("TRUNCATE mysql.slow_log;")
        con.commit()
