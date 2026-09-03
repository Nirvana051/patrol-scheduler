#!/usr/bin/env bash
# 把主管理文档（Sample_web_api/docs/task.md）同步一份进本仓库 docs/，让它也有版本历史。
set -e
SRC="$(dirname "$0")/../../Sample_web_api/docs/task.md"
DST="$(dirname "$0")/../docs/task.md"
cp "$SRC" "$DST" && echo "synced $(wc -l < "$DST") lines → $DST"
