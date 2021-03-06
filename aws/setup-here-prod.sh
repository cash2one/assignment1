#!/bin/bash

AWSHOST="sgprod.spinpunch.com"
GAME_ID="sg"
GAME_ID_LONG="summonersgate"
AWSKEY="$HOME/.ssh/${GAME_ID}prod.pem"

# run on mothership machine

SSHDEST="ec2-user@$AWSHOST"
SSHARGS="-i $AWSKEY"

echo "Building overlay tarball..."
(cd prod && find . -not -path '*.svn*' | cpio -o | gzip -c > /tmp/overlay-prod.cpio.gz)

# bash conveniences
FILESTOGO="$HOME/.bashrc \
           $HOME/.bash_profile \
           $HOME/.dir_colors"

# remote setup scripts
FILESTOGO+=" setup-there-common.sh setup-there-prod.sh fix-ec2-mail.py"

# overlay
FILESTOGO+=" /tmp/overlay-prod.cpio.gz"

echo "Copying files to cloud host..."
scp $SSHARGS $FILESTOGO $SSHDEST:/home/ec2-user

echo "Running setup script on cloud host..."
ssh $SSHARGS -t $SSHDEST /home/ec2-user/setup-there-prod.sh $GAME_ID $GAME_ID_LONG

rm -f /tmp/overlay-prod.cpio.gz
