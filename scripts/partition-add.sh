#!/bin/bash
# partition-add.sh <partitionId> <status> <createTimeMillis|now> <bucketId>[,...]   (loginsight MUST be stopped)
LI=/usr/lib/loginsight
[ "$(systemctl is-active loginsight)" = active ] && { echo "refusing: loginsight is running"; exit 3; }
source $LI/application/etc/loginsight.conf
exec $LI/application/3rd_party/bin/java -DLOGINSIGHT_HOME=$LI -Xmx1024M -cp $LI/application/lib/ingestion-service.jar:/root PartitionBucketAdd "$@"
