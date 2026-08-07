#!/bin/bash
# chunk-lab 统一入口：固定解释器与模块搜索路径，规避多克隆/多虚拟环境混用。
#
# 三条硬约束（对应历史踩过的坑）：
#   1. 写死 .venv/bin/python 绝对路径，不依赖当前终端激活了哪个虚拟环境；
#   2. 绝不使用 uv run，避免触发 uv.lock 解析并静默改写虚拟环境；
#   3. 只读 ragflow 源码，不向其安装任何依赖，pyproject.toml / uv.lock 保持零改动。

set -euo pipefail  # 任一命令失败即退出，未定义变量报错，管道错误不被吞掉

# 被测的 ragflow 仓库根目录（本实验室只读它）
RAGFLOW=/Users/jialei/Desktop/RagFlow/ragflow
# chunk-lab 自身根目录，取本脚本所在目录，保证从任意工作目录调用都正确
LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 指定解释器为 ragflow 的虚拟环境，切分逻辑的依赖（PIL、分词器等）都装在这里
PY="$RAGFLOW/.venv/bin/python"

# 启动前检查解释器存在，否则给出可读的报错而不是 command not found
if [ ! -x "$PY" ]; then
  echo "未找到 ragflow 虚拟环境解释器：$PY" >&2
  exit 1
fi

# PYTHONPATH 同时包含 ragflow 仓库根（供 rag.* / common.* 绝对导入）与 chunk-lab 根（供 labkit 包导入）
PYTHONPATH="$RAGFLOW:$LAB" exec "$PY" -m labkit.cli "$@"
