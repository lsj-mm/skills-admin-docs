#!/bin/sh
# Linux / macOS：在终端中执行 sh 安装开机自启.sh
cd "$(dirname "$0")" || exit 1
python3 update_tool_docs.py --install
