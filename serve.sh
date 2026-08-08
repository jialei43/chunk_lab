#!/bin/bash
# chunk-lab 服务管理：启动、停止、重启、查看状态与日志。
#
# 为什么不用简单的 nohup + kill：
#   开发中反复遇到「旧进程没杀掉 → 新服务因端口占用启动失败 → 却对着旧服务
#   排查为什么改动没生效」。因此这里用 PID 文件与端口占用双重校验，
#   停止后确认端口真的释放，再启动。

set -u  # 未定义变量视为错误；不用 -e，各命令的失败要单独处理

# 脚本所在目录，用绝对路径以免从任意工作目录调用时出错
LAB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 监听端口，与 labkit/cli.py 中 serve 子命令的默认值保持一致
PORT="${CHUNKLAB_PORT:-5099}"
# 热加载开关，默认开启，与前台 ./run.sh serve 的默认行为一致；
# 关掉可省去重载器多起一个进程、模块重复加载十几秒的开销
RELOAD="${CHUNKLAB_RELOAD:-1}"
# 归一化环境变量取值：不归一化的话 CHUNKLAB_RELOAD=false 会被当成开启
case "$(printf '%s' "$RELOAD" | tr '[:upper:]' '[:lower:]')" in
  0|false|no|off) RELOAD=0 ;;  # 明确表示关闭的几种写法
  *) RELOAD=1 ;;  # 其余一律视为开启
esac
# 运行时文件目录：跟随数据目录放在仓库之外，避免污染代码目录
RUNTIME_DIR="${CHUNKLAB_DATA_DIR:-$HOME/MinerU/chunk_lab}/runtime"
# PID 文件与日志文件
PID_FILE="$RUNTIME_DIR/serve.pid"
LOG_FILE="$RUNTIME_DIR/serve.log"

# 返回当前占用端口的全部进程号，未占用时输出为空。
# 必须取全部而非第一个：热加载模式下 Flask 重载器会 fork 出父子两个进程，
# 只杀子进程的话父进程会立刻把它重新拉起，stop 表面成功实际没停。
port_pids() {
  lsof -ti tcp:"$PORT" 2>/dev/null
}

# 取其中一个进程号用于展示
port_pid() {
  port_pids | head -1
}

# 判断服务是否在运行：以端口占用为准而非仅看 PID 文件，
# PID 文件可能因异常退出而残留
is_running() {
  [ -n "$(port_pid)" ]
}

cmd_start() {
  # 已在运行时不重复启动，否则会因端口占用而失败并留下误导性日志
  if is_running; then
    echo "服务已在运行（PID $(port_pid)，端口 ${PORT}）"
    echo "如需重启：${0} restart"
    return 0
  fi
  # 确保运行时目录存在
  mkdir -p "$RUNTIME_DIR"
  # 后台启动；cd 到脚本目录保证 run.sh 与相对路径可用
  cd "$LAB" || return 1
  # 关闭热加载时追加 --no-reload；变量为空则不产生多余参数，故此处有意不加引号
  local reload_flag=""
  # 仅在显式关闭时带上该参数，保持默认命令行与改动前一致
  [ "$RELOAD" = "0" ] && reload_flag="--no-reload"
  # 启动服务；日志重定向到固定文件，便于 log 子命令跟随查看
  # shellcheck disable=SC2086
  nohup "$LAB/run.sh" serve --port "$PORT" $reload_flag >"$LOG_FILE" 2>&1 &
  # 记录 PID
  echo $! >"$PID_FILE"
  # 等待端口就绪：加载 ragflow 模块较慢，固定 sleep 要么不够要么浪费
  local i
  for i in $(seq 1 40); do
    if is_running; then
      echo "已启动　http://127.0.0.1:${PORT}"
      # 明确回报本次的热加载模式，避免改了代码不生效却不知道原因
      if [ "$RELOAD" = "0" ]; then
        echo "热加载：已关闭（改代码需 ${0} restart 才生效）"
      else
        echo "热加载：已开启，改 labkit/ 下的代码会自动重启"
      fi
      echo "日志：$LOG_FILE"
      return 0
    fi
    sleep 0.5
  done
  # 超时说明启动失败，直接把日志尾部打出来，省去再去翻文件
  echo "启动超时，日志末尾："
  tail -20 "$LOG_FILE"
  return 1
}

cmd_stop() {
  # 未在运行时直接返回，保持命令幂等
  if ! is_running; then
    echo "服务未在运行"
    rm -f "$PID_FILE"
    return 0
  fi
  local pids
  pids="$(port_pids | tr '\n' ' ')"
  echo "正在停止（PID ${pids}）…"
  # 先发 TERM 让进程有机会正常退出；父子进程一并处理
  # shellcheck disable=SC2086
  kill ${pids} 2>/dev/null
  # 等待端口释放
  local i
  for i in $(seq 1 20); do
    if ! is_running; then
      rm -f "$PID_FILE"
      echo "已停止"
      return 0
    fi
    sleep 0.5
  done
  # 仍未退出则强制结束；不强制的话端口一直被占，后续启动都会失败
  echo "进程未响应，强制结束"
  # shellcheck disable=SC2086
  kill -9 $(port_pids | tr '\n' ' ') 2>/dev/null
  sleep 1
  rm -f "$PID_FILE"
  # 确认端口确实释放，否则如实报告而不是假装成功
  if is_running; then
    echo "端口 ${PORT} 仍被占用，请手动检查：lsof -i tcp:${PORT}"
    return 1
  fi
  echo "已停止"
}

cmd_status() {
  if is_running; then
    echo "运行中　PID $(port_pid)　http://127.0.0.1:${PORT}"
    # 模式从正在运行的进程命令行反查，而不是看本次调用的 CHUNKLAB_RELOAD——
    # 后者只反映「现在打算怎么启」，不是「正在跑的是什么」
    local cmdlines=""
    local pid
    # 逐个取进程命令行，热加载模式下父子两个进程都要看
    for pid in $(port_pids); do
      cmdlines="${cmdlines}$(ps -o command= -p "$pid" 2>/dev/null)
"
    done
    # 命令行里带 --no-reload 即为关闭状态
    if printf '%s' "$cmdlines" | grep -q -- "--no-reload"; then
      echo "热加载：已关闭"
    else
      echo "热加载：已开启"
    fi
    echo "日志：$LOG_FILE"
  else
    echo "未运行"
  fi
}

cmd_log() {
  # 日志文件不存在说明从未启动过
  if [ ! -f "$LOG_FILE" ]; then
    echo "暂无日志：$LOG_FILE"
    return 1
  fi
  # 跟随输出，便于观察启动过程与请求
  tail -f "$LOG_FILE"
}

# 打印用法说明，未知子命令与未知选项都走这里
usage() {
  echo "用法：${0} {start|stop|restart|status|log} [--reload|--no-reload]"
  echo
  echo "  start    后台启动服务"
  echo "  stop     停止服务（先 TERM，超时后 KILL，并确认端口释放）"
  echo "  restart  重启"
  echo "  status   查看是否在运行，以及实际的热加载模式"
  echo "  log      跟随查看日志"
  echo
  echo "  --no-reload  关闭热加载：省去重载器多起一个进程、模块重复加载十几秒"
  echo "  --reload     开启热加载（默认），改 labkit/ 下的代码自动重启"
  echo
  echo "前台运行（Ctrl+C 停止）：./run.sh serve [--no-reload]"
  echo "换端口：CHUNKLAB_PORT=6000 ${0} start"
  echo "长期关闭热加载：export CHUNKLAB_RELOAD=0（命令行参数优先级更高）"
}

# 取出子命令，剩余部分作为选项解析
CMD="${1:-}"
# 有参数时移除子命令，使 $@ 只剩选项
[ $# -gt 0 ] && shift

# 逐个解析选项；命令行显式指定时覆盖 CHUNKLAB_RELOAD
while [ $# -gt 0 ]; do
  case "$1" in
    --no-reload) RELOAD=0 ;;  # 显式关闭热加载
    --reload)    RELOAD=1 ;;  # 显式开启热加载
    *)
      # 未知选项不静默忽略，否则拼错参数会以为已生效
      echo "未知选项：$1" >&2
      echo >&2
      usage >&2
      exit 1
      ;;
  esac
  shift
done

# 分发子命令
case "$CMD" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop && cmd_start ;;
  status)  cmd_status ;;
  log)     cmd_log ;;
  *)
    usage
    exit 1
    ;;
esac
