#!/bin/bash
# Finder starts this script in an arbitrary working directory.
cd -- "$(/usr/bin/dirname -- "$0")" || exit 1

# Double-click always opens the reader's own data, even from a test shell.
# Advanced command-line usage of python3 -m server still supports the override.
unset PAPERREAD_HOME

for candidate in \
    .venv/bin/python3 \
    python3 python3.15 python3.14 python3.13 python3.12 python3.11 python3.10 \
    /opt/homebrew/bin/python3 /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/3.*/bin/python3 \
    /usr/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
        "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
        # The foreground Python process owns the terminal and receives SIGHUP
        # when its window closes. Never detach a server into the background.
        exec "$candidate" -m server --open
    fi
done

printf '%s\n' '未找到可用的 Python，请先安装 Python 3.10 或更新版本，再双击此文件。'
if [ -t 0 ]; then
    read -r -p '按回车键关闭窗口。' ignored
fi
exit 1
