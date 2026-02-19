#!/bin/bash

trap "echo 'Script interrupted'; exit 1" SIGINT

if [ -z "$1" ]; then
  echo "Usage: $0 <directory>"
  exit 1
fi

# 定义要遍历的目录
directory="$1"

# 遍历目录中的所有文件
find "$directory" -type f | while read -r file; do
  # 获取文件的目录和文件名
  dir=$(dirname "$file")
  base=$(basename "$file")

  if [ "$base" != "rlog" ]; then
    continue
  fi

  echo "Processing $dir $file"
  rm -f "$dir/rlog.bz2"
  pbzip2 -v -6 "$file"
done