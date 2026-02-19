#/bin/sh
if [ -f /EON ] || [ -f /AGNOS ]; then
  git pull && rm -rf /data/community/crashes/*; rm -rf /data/media/0/crash_logs/* && sync && sleep 3; sudo reboot
else
  echo "Not EON or AGNOS!"
fi