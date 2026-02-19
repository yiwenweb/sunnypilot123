#!/bin/sh
tmux capture-pane -S -32768 -b %1; tmux save-buffer -b %1 /tmp/bug_log; cat /tmp/bug_log; ls -1d /data/community/crashes/*; ls -1d /data/media/0/crash_logs/*
