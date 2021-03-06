#!/bin/sh

. ./setup-there-common.sh

YUMPACKAGES="nscd xfsprogs subversion git"
YUMPACKAGES+=" python-twisted python-simplejson strace"
YUMPACKAGES+=" python-imaging python-imaging-devel numpy"
YUMPACKAGES+=" libxml2 libxml2-devel gcc"
YUMPACKAGES+=" sendmail-cf patch fail2ban screen"
YUMPACKAGES+=" mysql MySQL-python" # note: MySQL server + client + python libs
YUMPACKAGES+=" postgresql python-psycopg2" # note: Postgres client + python libs only

echo "SETUP(remote): Installing additional packages..."
sudo yum -y -q install $YUMPACKAGES

sudo chkconfig --add nscd
sudo chkconfig nscd on
sudo /etc/init.d/nscd start
sudo chkconfig fail2ban on
sudo /etc/init.d/fail2ban start

sudo chkconfig mysqld on

echo "SETUP(remote): Unpacking filesystem overlay..."
(cd / && gunzip -c /home/ec2-user/overlay-analytics1.cpio.gz | sudo cpio -iuvd -R root:root)

echo "SETUP(remote): Installing mongodb from mongodb.org repo..."
sudo yum -y -q install mongodb-org mongodb-org-server mongodb-org-shell mongodb-org-tools # note: MongoDB server + client + python libs (below)

# fix permissions
sudo chown -R root:root /etc
sudo chmod 0755 / /etc /etc/mail /etc/ssh /etc/security /etc/sysconfig
sudo chmod 0664 /etc/sysctl.conf
sudo chmod 0644 /etc/security/limits.conf
sudo chmod 0775 /etc/rc.d/rc.local
sudo chmod 0440 /etc/sudoers
sudo chmod 0644 /etc/ssh/*
sudo chmod 0600 /etc/ssh/sshd_config /etc/ssh/*_key
sudo chmod 0700 /home/*
sudo chmod 0644 /etc/mf.cnf
sudo sh -c 'chown -R ec2-user:ec2-user /home/ec2-user'
sudo sh -c 'chmod 0700 /home/ec2-user/.ssh'
sudo sh -c 'chmod 0600 /home/ec2-user/.ssh/*'
sudo newaliases

echo "SETUP(remote): Mounting all volumes..."
sudo mount -a

echo "SETUP(remote): Fixing mail configuration..."
sudo ./fix-ec2-mail.py

echo "SETUP(remote): analytics1 setup done!"

echo "MISSING: /etc/fstab, /etc/sysconfig/network hostname"
echo "MISSING: /etc/cron.spinpunch.mysql-daily/99-report-slow-queries.sh email"
echo "MISSING: compile/install ujson library"
echo "MISSING: SVN: /home/ec2-user/.ssh/spsvnaccess.pem (also .ssh/config with Host/User/IdentityFile)"
echo "MISSING: GIT: /home/ec2-user/.ssh/analytics1.pem (also .ssh/config with Host/User/IdentityFile)"
echo "MISSING: GIT: git config --global user.name " # 'SpinPunch Deploy'
echo "MISSING: GIT: git config --global user.email " # 'awstech@spinpunch.com'
echo "MISSING: /home/ec2-user/.ssh/slack.token (with incoming webhook for analytics channel) for automated messages"
echo "MISSING: /home/ec2-user/.ssh/host-awssecret"
echo "MISSING: set up scratch space in /media/aux/tmp for backup script"
echo "MISSING: set up swap space"
echo "MISSING: mysql config for analytics (use /usr/bin/mysql_secure_installation to set root password)"
# NOTE! be sure to set new databases to utf8 collate utf8_general_ci !
# my.cnf settings: see aws/analytics1/etc/my.cnf
echo "MISSING: easy_install pymongo - then see setup-there-prod.sh for MongoDB setup instructions"
echo "MISSING: /etc/aliases: add 'root: awstech@example.com' mail alias"
