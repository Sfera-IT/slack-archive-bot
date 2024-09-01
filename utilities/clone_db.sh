#! /bin/bash

ssh root@sfera-docker "cp /srv/sferait/archive/slack.sqlite /srv/sferait/archive/slack_backup.sqlite && gzip /srv/sferait/archive/slack_backup.sqlite"
scp root@sfera-docker:/srv/sferait/archive/slack_backup.sqlite.gz ./slack.sqlite.gz
ssh root@sfera-docker "rm /srv/sferait/archive/slack_backup.sqlite.gz"
gunzip slack.sqlite.gz
