#!/bin/sh
rm /tmp/tail_log; sleep 3; if [ ! -f /tmp/tail_log ]; then while true; do tmux pipe-pane 'exec cat >> /tmp/tail_log'; sleep 1; done; fi & (sleep 3; tail -f /tmp/tail_log | grep -v "clocksd")
