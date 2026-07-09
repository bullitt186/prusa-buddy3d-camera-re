#!/bin/sh
set -e
COMMON_DIR=`dirname $(realpath $0)`
TOP_DIR=$(realpath $COMMON_DIR/../..)
cd $TOP_DIR
echo "Start to write partitions"
for image in $(ls /dev/block/by-name)
do
	if [ -f $COMMON_DIR/${image}.img ];then
		echo "Writing $image..."
		mtd_path=$(realpath /dev/block/by-name/${image})
		flash_eraseall $mtd_path
		nandwrite -p $mtd_path $COMMON_DIR/${image}.img
		if [ $? -ne 0 ];then
			echo "Error: $image write failed."
			exit 1
		fi
	fi
done
echo "Erase misc partition"
flash_eraseall /dev/block/by-name/misc
if [ $? -ne 0 ];then
	echo "Error: Erase misc partition failed."
	exit 2
fi
