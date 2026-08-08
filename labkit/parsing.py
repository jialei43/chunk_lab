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
import threading  # 导入 threading 在后台执行耗时解析
import uuid  # 导入 uuid 生成任务标识
from datetime import datetime  # 导入 datetime 记录任务时间

from .paths import MINERU_OUT, UPLOAD_DIR, ensure_ragflow_importable  # 导入目录常量与路径注入

ensure_ragflow_importable()  # 在导入 ragflow 模块之前注入源码路径

from chunklab_bridge.parse import parse_document, resolve_mineru_config  # noqa: E402  导入解析桥接


def mineru_defaults():
    """返回 MinerU 连接配置的当前默认值，供界面展示与表单预填。"""
    # 复用桥接层的解析逻辑，保证界面显示的与实际使用的一致
    return resolve_mineru_config()

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
            "case_id": None,  # 入库后的样本标识
            "block_count": 0,  # 产物块数
            "error": None,  # 失败原因
        }

    # 后台执行解析，避免阻塞 HTTP 请求
    def _run():
        # 捕获全部异常，任何失败都要记录到任务状态而不是让线程静默死掉
        try:
            # 解析进度回调，写入任务状态供前端轮询
            def cb(prog=None, msg=""):
                # 进度值可能为 None 或负数（负数表示失败），统一做保护
                if isinstance(prog, (int, float)) and prog >= 0:
                    _update(task_id, progress=float(prog))
                # 有描述时一并更新
                if msg:
                    _update(task_id, message=str(msg)[:200])

            # 轮到本任务执行，从排队中切到运行中并记录真实开始时间
            _update(task_id, status="running", message="正在调用 MinerU 解析…",
                    run_started_at=datetime.now().isoformat(timespec="seconds"))
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
