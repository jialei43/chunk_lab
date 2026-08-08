"""爬取任务管理：把列表页抓取放到后台执行并可查询进度。

抓取动辄几分钟甚至更久，同步等待会让请求超时、界面假死，
因此与解析任务采用同一套「提交任务 + 轮询进度」的模式。
"""

import threading  # 导入 threading 在后台执行耗时抓取
import uuid  # 导入 uuid 生成任务标识
from datetime import datetime  # 导入 datetime 记录任务时间

from .crawler import Crawler  # 导入爬虫本体，与命令行共用同一份实现
from pathlib import Path  # 导入 Path 校验与解析下载目录

from .paths import DATA_ROOT  # 导入数据根目录

# 下载目录，与语料、产物等一并放在数据目录下（仓库之外）
DOWNLOAD_DIR = DATA_ROOT / "downloads"

# 任务表：task_id -> 状态。仅存活于进程内，服务重启后清空。
# 注意热加载：改动 labkit/ 下的代码或前端页面会触发自动重启，
# 正在运行的抓取线程随之终止、任务记录也一并丢失。
# 开发时若发现任务凭空消失，多半是这个原因而非 bug。
_tasks = {}
# 保护任务表的锁
_lock = threading.Lock()


def _update(task_id, **fields):
    """更新任务状态。"""
    # 加锁保证并发安全
    with _lock:
        # 任务存在才更新
        if task_id in _tasks:
            _tasks[task_id].update(fields)


def get_task(task_id):
    """查询任务状态。"""
    # 返回副本，避免调用方拿到会被后台改动的引用
    with _lock:
        t = _tasks.get(task_id)
        return dict(t) if t else None


def list_tasks(limit=20):
    """列出最近的抓取任务，最新的在前。"""
    # 读取全部任务
    with _lock:
        items = [dict(t) for t in _tasks.values()]
    # 按开始时间倒序
    items.sort(key=lambda t: t.get("started_at", ""), reverse=True)
    # 截断到上限
    return items[:limit]


def start_crawl(url, out_dir=None, exts="pdf,doc,docx,ppt,pptx,xls,xlsx",
                next_text="", next_selector="", detail_selector="",
                max_pages=10, max_files=0, delay=1.0,
                obey_robots=True, dry_run=False, workers=4, link_pattern=""):
    """启动一次后台抓取，立即返回任务标识。"""
    # 生成短任务标识
    task_id = uuid.uuid4().hex[:12]
    # 下载目录，默认放在数据目录下
    dest = out_dir or DOWNLOAD_DIR
    # 初始化任务状态
    with _lock:
        _tasks[task_id] = {
            "task_id": task_id,  # 任务标识
            "url": url,  # 起始列表页
            "out_dir": str(dest),  # 下载目录
            "dry_run": bool(dry_run),  # 是否只演练
            "workers": int(workers),  # 并发下载数
            "status": "running",  # 运行状态
            "message": "已排队",  # 当前进度描述
            "stats": {"pages": 0, "found": 0, "downloaded": 0, "skipped": 0, "failed": 0},  # 统计
            "started_at": datetime.now().isoformat(timespec="seconds"),  # 开始时间
            "error": None,  # 失败原因
        }

    # 后台执行，避免阻塞 HTTP 请求
    def _run():
        # 任何异常都要落到任务状态里，而不是让线程静默死掉
        try:
            # 进度回调：把爬虫的统计与消息同步到任务状态
            def on_progress(stats, message):
                _update(task_id, stats=stats, message=message)

            # 组装爬虫；参数与命令行完全一致，两边共用同一实现
            crawler = Crawler(
                url,  # 起始页
                dest,  # 下载目录
                exts,  # 目标扩展名
                delay=delay,  # 请求间隔
                max_pages=max_pages,  # 翻页上限
                max_files=max_files,  # 文件数上限
                next_text=next_text or None,  # 翻页文字
                next_selector=next_selector or None,  # 翻页选择器
                detail_selector=detail_selector or None,  # 详情页选择器
                obey_robots=obey_robots,  # 是否遵守 robots.txt
                dry_run=dry_run,  # 是否只演练
                workers=workers,  # 并发下载数
                link_pattern=link_pattern or None,  # 下载链接正则
                on_progress=on_progress,  # 进度回调
            )
            # 执行抓取
            crawler.run()
            # 完成后写入最终统计
            _update(task_id, status="done", stats=dict(crawler.stats),
                    message="抓取完成")
        except Exception as e:
            # 失败时记录可读原因
            _update(task_id, status="failed", error=f"{type(e).__name__}: {e}",
                    message="抓取失败")

    # 守护线程运行，服务退出时不阻塞
    threading.Thread(target=_run, daemon=True, name=f"crawl-{task_id}").start()
    # 返回任务标识供前端轮询
    return task_id


# 拒绝写入的路径：系统目录与根目录。
# 这是本地开发工具，不必做严格沙箱，但把文件写进系统目录几乎一定是误操作。
FORBIDDEN_ROOTS = ("/", "/etc", "/usr", "/bin", "/sbin", "/System", "/Library", "/var", "/private")


def resolve_out_dir(raw):
    """校验并解析下载目录，非法时抛出可读的错误。"""
    # 未指定时用默认目录
    if not raw or not str(raw).strip():
        return DOWNLOAD_DIR
    # 展开 ~ 并转绝对路径
    path = Path(str(raw).strip()).expanduser()
    # 相对路径以数据目录为基准，避免落到当前工作目录这种不确定的位置
    if not path.is_absolute():
        path = DATA_ROOT / path
    # 规范化后再校验，防止用 .. 绕过
    path = path.resolve()
    # 系统目录与根目录一律拒绝
    if str(path) in FORBIDDEN_ROOTS:
        raise ValueError(f"不允许写入系统目录：{path}")
    # 位于系统目录之下同样拒绝
    for root in FORBIDDEN_ROOTS:
        if root != "/" and str(path).startswith(root + "/"):
            raise ValueError(f"不允许写入系统目录：{path}")
    # 返回校验通过的目录
    return path


def list_downloads(limit=200, out_dir=None):
    """列出已下载的文件，供界面查看与后续解析。"""
    # 未指定时看默认目录
    target = Path(out_dir) if out_dir else DOWNLOAD_DIR
    # 目录不存在说明尚未抓取过
    if not target.is_dir():
        return []
    # 收集文件信息，跳过进度文件等隐藏项
    items = [
        {
            "name": p.name,  # 文件名
            "path": str(p),  # 完整路径，解析时直接引用
            "size": p.stat().st_size,  # 字节数
            "mtime": p.stat().st_mtime,  # 修改时间，用于排序
        }
        for p in target.iterdir()
        if p.is_file() and not p.name.startswith(".")
    ]
    # 最新的排在前面
    items.sort(key=lambda x: -x["mtime"])
    # 截断
    return items[:limit]
