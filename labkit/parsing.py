"""解析任务管理：上传文件 → 调 MinerU 解析 → 产物入语料库。

解析耗时数分钟，必须异步执行并可查询进度，否则请求会超时、界面会假死。

批量提交的任务串行排队执行：MinerU 是本机单实例服务，一次并行跑十个解析
会把显存与内存打满，整体反而更慢，所以宁可排队也不并发。

与生产的隔离体现在两处：
  - 产物写入实验室自己的 mineru_out/，不碰生产的 MinerU 输出目录；
  - delete_output 固定为 False，产物保留下来供离线重放，
    而生产默认解析完就删。
"""

import queue  # 导入 queue 作为解析任务的串行队列
import logging  # 导入 logging 捕获解析过程的日志
import shutil  # 导入 shutil 复制回捞到的产物
import time  # 导入 time 判断产物新鲜度
import threading  # 导入 threading 在后台执行耗时解析
import uuid  # 导入 uuid 生成任务标识
from datetime import datetime  # 导入 datetime 记录任务时间

from pathlib import Path  # 导入 Path 处理文件路径

from .paths import MINERU_OUT, UPLOAD_DIR, ensure_ragflow_importable  # 导入目录常量与路径注入

ensure_ragflow_importable()  # 在导入 ragflow 模块之前注入源码路径

from chunklab_bridge.parse import parse_document, resolve_mineru_config  # noqa: E402  导入解析桥接


def mineru_defaults():
    """返回 MinerU 连接配置的当前默认值，供界面展示与表单预填。"""
    # 复用桥接层的解析逻辑，保证界面显示的与实际使用的一致
    return resolve_mineru_config()

# 需要放宽级别以便捕获日志的 logger。
# 刻意不用 root：实测放宽 root 会让 ragflow 的数据库查询、定时任务诊断等
# 海量日志一并涌入，解析日志反而被淹没。这里只列解析链路涉及的模块。
TARGET_LOGGERS = ("MinerUParser", "deepdoc", "rag", "chunklab_bridge", "labkit")

# 单个任务保留的日志条数上限。
# 解析大文件时日志可能上千条，全留会持续占用内存；
# 排查问题真正需要的是最近发生了什么，故只保留尾部。
MAX_LOG_LINES = 400

# 任务表：task_id -> 任务状态。仅存活于进程内，服务重启后清空，
# 这对开发时工具足够，不值得为此引入持久化。
_tasks = {}
# 保护任务表的锁，后台线程与请求线程都会访问它
_lock = threading.Lock()
# 待执行任务队列，元素是无参可调用对象；不设上限，排队多少都收下
_job_queue = queue.Queue()
# 唯一的执行线程引用，懒启动后长期存活
_worker = None
# 保护执行线程创建的锁，避免批量提交时并发创建出多条线程
_worker_lock = threading.Lock()


def _worker_loop():
    """队列消费循环：逐个取出任务执行，同一时刻只跑一个解析。"""
    # 常驻循环，服务存活期间一直等待新任务
    while True:
        # 取出一个待执行任务，队列为空时阻塞等待
        job = _job_queue.get()
        # 任何异常都不能让消费线程退出，否则后续任务将永远排队
        try:
            # 执行任务，其内部已自行捕获并记录失败
            job()
        finally:
            # 无论成败都标记该项完成，保持队列计数正确
            _job_queue.task_done()


def _ensure_worker():
    """确保消费线程已启动，首次提交任务时懒启动。"""
    # 用 global 声明以便重新赋值模块级引用
    global _worker
    # 加锁避免批量提交时并发创建多条线程
    with _worker_lock:
        # 线程不存在或已意外退出时重新创建
        if _worker is None or not _worker.is_alive():
            # 以守护线程运行，服务退出时不阻塞
            _worker = threading.Thread(target=_worker_loop, daemon=True, name="parse-worker")
            # 启动线程
            _worker.start()


def queue_size():
    """返回当前排队中（含正在执行）的任务数，供界面提示批量进度。"""
    # 直接取队列长度，近似值足够用于提示
    return _job_queue.qsize()


def _update(task_id, **fields):
    """更新任务状态。"""
    # 加锁保证并发安全
    with _lock:
        # 任务存在才更新，避免被删除后重建出幽灵条目
        if task_id in _tasks:
            _tasks[task_id].update(fields)


# MinerU 服务端自己的输出根目录。
# 客户端中断时服务端仍会把产物落在这里，是回捞的依据。
MINERU_SERVER_OUTPUTS = (
    Path.home() / "MinerU" / "uv_tools" / "output",
    Path.home() / "MinerU" / "result",
)


def salvage_product(stem, within_hours=6, copy_to=None):
    """从 MinerU 服务端输出目录回捞指定文件的产物。

    存在的理由：解析一份大 PDF 可能要几十分钟，客户端一旦中断，
    ragflow 侧的临时目录会留空，而服务端其实已经跑完。
    不回捞的话那份算力就白费了，还得从头再跑一遍。

    copy_to 给定时会把整套产物复制过去。这一步不能省：
    中断留下的空目录若原样保留，事后看到「解析过但目录是空的」
    只会让人以为解析失败，而实际上产物是有的。
    """
    # 截止时间，过旧的产物多半属于别的批次
    cutoff = time.time() - within_hours * 3600
    # 逐个候选根目录查找
    for root in MINERU_SERVER_OUTPUTS:
        # 目录不存在时跳过
        if not root.is_dir():
            continue
        # 按文件名匹配产物，取最近产生的一份
        matches = [p for p in root.rglob(f"{stem}_content_list.json")
                   if p.stat().st_mtime >= cutoff]
        # 有命中则取最新的一份
        if matches:
            found = max(matches, key=lambda p: p.stat().st_mtime)
            # 未要求复制时直接返回原位置
            if copy_to is None:
                return found
            # 把整套产物复制到指定目录，使产物目录与正常解析后一致
            dest = Path(copy_to)
            dest.mkdir(parents=True, exist_ok=True)
            # 复制同目录下的全部产物文件与图片目录
            for item in found.parent.iterdir():
                target = dest / item.name
                # 目录（images）整体复制，已存在则跳过避免重复写
                if item.is_dir():
                    if not target.exists():
                        shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
            # 返回复制后的产物路径
            return dest / found.name
    # 没找到
    return None


def append_log(task_id, line):
    """向任务追加一行日志，超出上限时丢弃最早的。"""
    # 加锁保证并发安全
    with _lock:
        # 任务不存在时忽略，可能已被清理
        task = _tasks.get(task_id)
        if task is None:
            return
        # 追加并截断，只保留最近的若干行
        logs = task.setdefault("logs", [])
        logs.append(line)
        if len(logs) > MAX_LOG_LINES:
            del logs[: len(logs) - MAX_LOG_LINES]


class TaskLogHandler(logging.Handler):
    """把解析过程中的日志收集到对应任务。

    按线程名过滤：每个解析任务跑在自己的线程里，不这样区分的话，
    并发解析时几个任务的日志会互相串到一起，反而更难排查。
    """

    def __init__(self, task_id, thread_name):
        # 初始化基类
        super().__init__()
        # 归属的任务
        self.task_id = task_id
        # 只收集该线程产生的日志
        self.thread_name = thread_name

    def emit(self, record):
        # 非本任务线程的日志直接忽略
        if record.threadName != self.thread_name:
            return
        # 格式化失败不应反过来影响解析
        try:
            # 时间 + 级别 + 消息，与终端日志格式接近便于对照
            ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            append_log(self.task_id, f"{ts} {record.levelname[:4]} {record.getMessage()[:500]}")
        except Exception:
            pass


def get_task(task_id):
    """查询任务状态。"""
    # 加锁读取并返回副本，避免调用方拿到会被后台改动的引用
    with _lock:
        task = _tasks.get(task_id)
        return dict(task) if task else None


def list_tasks(limit=100):
    """列出最近的解析任务，最新的在前。

    上限取 100 而非 20：批量上传一次就可能提交几十个文件，
    上限太低会让刚提交的任务看起来凭空消失。
    """
    # 加锁读取全部任务
    with _lock:
        items = [dict(t) for t in _tasks.values()]
    # 按开始时间倒序
    items.sort(key=lambda t: t.get("started_at", ""), reverse=True)
    # 截断到上限
    return items[:limit]


def start_cloud_parse(file_paths, auto_import=True, kind="", note="", lang="ch"):
    """启动一次云端批量解析。

    与本地解析的关键差异是「批量」：官方精准解析 API 一次可提交至多 200 个文件，
    共用一个批次轮询。逐个提交既慢又浪费配额，故这里整批走。
    """
    # 生成任务标识
    task_id = uuid.uuid4().hex[:12]
    # 统一为 Path 列表
    paths = [Path(p) for p in file_paths]
    # 初始化任务状态
    with _lock:
        _tasks[task_id] = {
            "task_id": task_id,  # 任务标识
            "filename": f"{len(paths)} 个文件（云端批量）",  # 展示名
            "backend": "cloud/vlm",  # 标明走云端，便于与本地任务区分
            "parse_method": "auto",  # 解析方法
            "status": "queued",  # 运行状态
            "progress": 0.0,  # 进度比例
            "message": "排队中",  # 进度描述
            "started_at": datetime.now().isoformat(timespec="seconds"),  # 提交时间
            "output_dir": str(MINERU_OUT),  # 产物目录
            "logs": [],  # 过程日志
            "case_id": None,  # 入库后的样本标识
            "block_count": 0,  # 产物块数
            "error": None,  # 失败原因
            "cloud_files": [],  # 逐文件云端状态
        }

    # 后台执行
    def _run():
        # 安装日志处理器，云端解析的过程日志同样需要可见
        handler = TaskLogHandler(task_id, threading.current_thread().name)
        handler.setLevel(logging.INFO)
        root = logging.getLogger()
        root.addHandler(handler)
        # 只放宽解析链路相关 logger，不动 root——
        # 放宽 root 会让 ragflow 的数据库查询与定时任务日志把解析日志淹没
        old_levels = {}
        for name in TARGET_LOGGERS:
            lg = logging.getLogger(name)
            old_levels[name] = lg.level
            if lg.level == logging.NOTSET or lg.level > logging.INFO:
                lg.setLevel(logging.INFO)
        try:
            # 延迟导入，未用云端时不承担其加载
            from .mineru_cloud import parse_files
            # 进入运行态
            _update(task_id, status="running", message="正在上传到 MinerU 云端…")

            # 逐文件状态回调，写入任务供界面展示
            def on_progress(name, state, item):
                # 汇总当前各文件状态
                with _lock:
                    task = _tasks.get(task_id)
                    if task is None:
                        return
                    files = {f["name"]: f for f in task.get("cloud_files", [])}
                    files[name] = {"name": name, "state": state,
                                   "message": item.get("err_msg") or ""}
                    task["cloud_files"] = list(files.values())
                    # 已完成数用于粗略进度
                    done = sum(1 for f in files.values() if f["state"] in ("done", "failed"))
                    task["progress"] = done / max(1, len(paths))
                    task["message"] = f"云端解析中：{done}/{len(paths)} 已结束"
                append_log(task_id, f"{datetime.now().strftime('%H:%M:%S')} 云端 {name}: {state}")

            # 执行批量解析
            results = parse_files(paths, MINERU_OUT, on_progress=on_progress)
            # 统计成功数
            ok = [r for r in results if r.get("ok")]
            # 回填产物清单。云端一批对应多个产物，与本地「一任务一产物」不同，
            # 只记一个的话其余文件解析完了却不知道落在哪，故逐个列全
            products = [{"name": r["name"], "path": str(r["product"])} for r in ok]
            _update(task_id, message=f"云端解析完成：成功 {len(ok)}/{len(results)}",
                    progress=1.0,
                    products=products,  # 全部产物，供界面列出
                    # 同时填首个产物，兼容按单产物取值的既有逻辑
                    product=products[0]["path"] if products else None)

            # 按需入库
            if auto_import:
                from .ingest import ingest_from_path
                case_ids = []
                # 逐个入库；单个失败不影响其它
                for r in ok:
                    try:
                        cid, status = ingest_from_path(
                            r["product"], r["name"], kind=kind,
                            note=note or "MinerU 云端解析（vlm）导入", overwrite=True)
                        case_ids.append(cid)
                        append_log(task_id, f"入库 {cid}：{status}")
                    except Exception as e:
                        append_log(task_id, f"入库失败 {r['name']}：{e}")
                # 记录最后一个样本，便于界面提供跳转
                if case_ids:
                    _update(task_id, case_id=case_ids[-1])
                _update(task_id, message=f"完成：解析 {len(ok)}/{len(results)}，入库 {len(case_ids)}")

            # 失败项写进日志，便于逐个排查
            for r in results:
                if not r.get("ok"):
                    append_log(task_id, f"失败 {r['name']}：{r.get('message')}")
            # 标记完成
            _update(task_id, status="done")
        except Exception as e:
            _update(task_id, status="failed", error=f"{type(e).__name__}: {e}",
                    message="云端解析失败")
            import traceback
            for line in traceback.format_exc().splitlines()[-12:]:
                append_log(task_id, line)
        finally:
            # 还原各 logger 级别并摘掉处理器
            for name, lv in old_levels.items():
                logging.getLogger(name).setLevel(lv)
            root.removeHandler(handler)

    # 云端解析不占本机资源，无需排入本地串行队列，直接起线程
    threading.Thread(target=_run, daemon=True, name=f"cloud-{task_id}").start()
    # 返回任务标识
    return task_id


def start_parse(file_path, filename, backend="pipeline", parse_method="auto",
                auto_import=True, kind="", note="", mineru_api="", output_dir=""):
    """把一次解析放入队列，立即返回任务标识。

    任务不会马上执行：前面还有排队任务时会等待，界面据 status 显示「排队中」。
    """
    # 生成短任务标识，便于在界面与日志中引用
    task_id = uuid.uuid4().hex[:12]
    # 初始化任务状态
    with _lock:
        _tasks[task_id] = {
            "task_id": task_id,  # 任务标识
            "filename": filename,  # 原始文件名
            "backend": backend,  # 使用的 backend
            "parse_method": parse_method,  # 解析方法
            "status": "queued",  # 运行状态：queued / running / done / failed
            "progress": 0.0,  # 进度比例
            "message": "排队中",  # 进度描述
            "started_at": datetime.now().isoformat(timespec="seconds"),  # 提交时间，列表按它倒序
            "output_dir": str(output_dir or MINERU_OUT),  # 本次产物落地目录
            "logs": [],  # 解析过程日志，供界面展开查看
            "case_id": None,  # 入库后的样本标识
            "block_count": 0,  # 产物块数
            "error": None,  # 失败原因
        }

    # 后台执行解析，避免阻塞 HTTP 请求
    def _run():
        # 安装日志处理器，捕获本线程内 MinerU 解析产生的全部日志。
        # 挂在 root 上是因为解析链路涉及多个模块的 logger，逐个挂容易遗漏。
        handler = TaskLogHandler(task_id, threading.current_thread().name)
        # INFO 级别足以覆盖解析进度与告警
        handler.setLevel(logging.INFO)
        root = logging.getLogger()
        root.addHandler(handler)
        # 还必须放宽级别：logger 默认级别高于 INFO 时，消息在传到 handler
        # 之前就被丢弃，只给 handler 设级别没有任何作用。
        #
        # 但不能放宽 root——实测那会让 ragflow 内部的数据库查询、定时任务诊断
        # 等海量日志一并涌入，把解析日志淹没。只提升解析链路相关的 logger。
        old_levels = {}
        for name in TARGET_LOGGERS:
            lg = logging.getLogger(name)
            old_levels[name] = lg.level
            if lg.level == logging.NOTSET or lg.level > logging.INFO:
                lg.setLevel(logging.INFO)
        # 捕获全部异常，任何失败都要记录到任务状态而不是让线程静默死掉
        try:
            # 解析进度回调，写入任务状态供前端轮询
            def cb(prog=None, msg=""):
                # 进度值可能为 None 或负数（负数表示失败），统一做保护
                if isinstance(prog, (int, float)) and prog >= 0:
                    _update(task_id, progress=float(prog))
                # 有描述时一并更新，并记入日志形成完整时间线
                if msg:
                    _update(task_id, message=str(msg)[:200])
                    append_log(task_id, f"{datetime.now().strftime('%H:%M:%S')} 进度 {msg}")

            # 轮到本任务执行，从排队中切到运行中并记录真实开始时间
            _update(task_id, status="running", message="正在调用 MinerU 解析…",
                    run_started_at=datetime.now().isoformat(timespec="seconds"))
            # 记录本次解析的输出目录与文件名，供失败时回捞产物
            _update(task_id, message="正在调用 MinerU 解析（大文件可能数十分钟）…")
            # 执行解析，产物落在实验室目录
            product = parse_document(
                file_path,  # 待解析文件
                # 输出目录：调用方可覆盖，默认落在实验室目录，与生产分开
                output_dir=output_dir or MINERU_OUT,
                backend=backend,  # 处理后端类型
                parse_method=parse_method,  # 解析方法
                # MinerU 服务地址，留空则按环境变量与默认值解析
                config={"mineru_api": mineru_api} if mineru_api else None,
                callback=cb,  # 进度回调
            )
            # 统计原始块数
            import json
            with open(product, "r", encoding="utf-8") as fh:
                block_count = len(json.load(fh))
            # 记录产物信息
            _update(task_id, product=str(product), block_count=block_count,
                    message="解析完成", progress=1.0)

            # 按需把产物导入语料库，省去手工再走一遍导入
            if auto_import:
                # 延迟导入避免循环依赖
                from .ingest import ingest_from_path
                # 标记进入入库阶段
                _update(task_id, message="正在导入语料库…")
                # 执行导入；backend 由产物所在目录名推断，与扫描导入口径一致
                case_id, status = ingest_from_path(
                    product,  # 产物路径
                    filename,  # 原始文件名
                    kind=kind,  # 文档大类
                    note=note or f"实验室解析导入，backend={backend}",  # 备注
                    overwrite=True,  # 同名样本直接覆盖，重解析的目的就是更新它
                )
                # 记录入库结果
                _update(task_id, case_id=case_id, message=f"已入库：{status}")

            # 标记完成
            _update(task_id, status="done")
        except Exception as e:
            # 失败时记录类型与信息，前端据此展示可读的原因；
            # 单个任务失败不影响队列中的其他任务，它们照常依次执行
            _update(task_id, status="failed", error=f"{type(e).__name__}: {e}",
                    message="解析失败")

    # 确保消费线程已就绪
    _ensure_worker()
    # 入队等待执行，而不是直接起线程并发跑
    _job_queue.put(_run)
    # 返回任务标识供前端轮询
    return task_id
