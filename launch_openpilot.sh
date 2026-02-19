#!/usr/bin/bash

# custom op script
if [ -f /data/custom_op_script.sh ]; then
  source /data/custom_op_script.sh
fi

exec ./launch_chffrplus.sh
