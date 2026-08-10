"""评估任务：把一轮评估拆成可观察、可恢复的后台任务。

为什么不能继续用「一个 HTTP 请求同步跑完」：
  一轮评估要跑几十个样本、十几分钟，期间请求一直挂着——前端看不到任何进度，
  更要命的是这段时间里只要进程重启（改一次 labkit 下的代码就会触发热加载，
  开发时是家常便饭），已经跑完的几十个样本连同结果一起消失，只能从头再来。

三条约定：
  1. 每完成一个样本就把结果落盘，进度随之更新——进程没了，已完成的部分还在；
  2. 任务状态写在文件里而不是内存里，重启后能认出「这轮跑到一半被打断了」；
  3. 续跑沿用任务创建时锁定的参数，不允许中途改——一轮里混两套参数，
     出来的问题总数与任何一轮都不可比，那份快照就是废的。

与 parsing.py 的任务表刻意不同：那边是纯内存的（服务重启即清空），
对几分钟的解析够用；评估必须扛住重启，所以状态一律落盘。
"""

import json  # 导入 json 读写任务状态与单样本结果
import logging  # 导入 logging 记录任务失败的完整堆栈
import os  # 导入 os 取当前进程号，用于识别被重启打断的任务
import shutil  # 导入 shutil 删除任务目录及其中间产物
import threading  # 导入 threading 在后台执行耗时评估
import time  # 导入 time 计算逐样本耗时
import traceback  # 导入 traceback 把异常堆栈存进任务状态供界面展示
from datetime import datetime  # 导入 datetime 生成任务标识与时间戳

from . import annotations, runs  # 导入标注继承与运行历史
from .detectors import DetectorConfig  # 导入检测阈值配置
from .evaluate import build_report, evaluate_case, load_cases  # 导入报告汇总、单样本评估与语料加载
from .offline import build_parser_config  # 导入切分配置合并逻辑
from .paths import RUNS_DIR  # 导入历史轮次目录常量

# 任务目录：挂在 runs/ 下的隐藏子目录，与正式快照同处一地但不混在一起。
# 用点号开头，前端的历史轮次列表按 *.json 读索引，不会把它当成一轮快照。
JOBS_DIR = RUNS_DIR / ".jobs"

# 已结束任务保留的数量上限。成功的任务只剩一份几 KB 的 job.json（中间结果已清理），
# 留着是为了回答「这一轮是谁跑的、跑了多久、中间失败过几个样本」，但不必无限留。
KEEP_FINISHED = 30

# 视为「已经结束、不会再自己往前走」的状态集合
FINISHED_STATES = ("done", "failed", "cancelled", "interrupted")

# 保护任务状态文件读-改-写的锁。后台线程逐样本更新进度，请求线程同时在读，
# 不加锁会出现「读到写了一半的状态」或「两次更新互相覆盖」
_lock = threading.RLock()

# 取消标记：job_id -> Event。取消同时写进 job.json，
# 但内存标记能让正在跑的循环立刻看到，不必等下一次读文件
_cancel_events = {}


def _job_dir(job_id):
    """某个任务的目录。"""
    # 每个任务一个子目录，任务状态与中间结果都放在里面
    return JOBS_DIR / job_id


def _job_file(job_id):
    """某个任务的状态文件路径。"""
    # 状态文件固定名，便于扫描目录时识别
    return _job_dir(job_id) / "job.json"


def _case_file(job_id, case_id):
    """某个任务下单个样本的评估结果文件路径。"""
    # 逐样本一个文件：跑完一个存一个，这是断点续跑的本体
    return _job_dir(job_id) / "cases" / f"{case_id}.json"


def _now():
    """当前时间的可读字符串，与历史轮次的时间戳格式保持一致。"""
    # 精确到秒即可，任务粒度不需要毫秒
    return datetime.now().isoformat(timespec="seconds")


def _write_json_atomic(path, data):
    """原子地写出 JSON：先写临时文件再改名。

    必须原子：后台线程每跑完一个样本就重写 job.json，而前端每 1.5 秒轮询一次，
    直接覆盖写的话总有机会读到只写了一半的文件，界面就会莫名其妙报解析失败。
    """
    # 确保父目录存在
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时文件与目标同目录，保证 rename 在同一文件系统内是原子操作
    tmp = path.with_suffix(path.suffix + ".tmp")
    # 先写临时文件
    with tmp.open("w", encoding="utf-8") as fh:
        # 保留中文可读性，便于直接打开文件排查
        json.dump(data, fh, ensure_ascii=False, indent=2)
    # 改名替换，读方要么看到旧文件要么看到新文件，不会看到半截
    tmp.replace(path)


def load_job(job_id):
    """读取任务状态，不存在或损坏时返回 None。"""
    # 状态文件路径
    path = _job_file(job_id)
    # 文件不存在说明任务已被删除
    if not path.is_file():
        return None
    # 损坏的状态文件不应让整个功能不可用
    try:
        # 读取并解析
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        # 按不存在处理，界面会提示任务已失效
        return None


def _save_job(job):
    """写回任务状态，并刷新更新时间。"""
    # 每次写盘都更新时间戳，界面据此判断任务是否还在推进
    job["updated_at"] = _now()
    # 原子写出
    _write_json_atomic(_job_file(job["job_id"]), job)
    # 返回任务本身，便于链式使用
    return job


def _update_job(job_id, **fields):
    """加锁读-改-写任务状态，返回更新后的任务。"""
    # 全程持锁，避免与逐样本进度更新互相覆盖
    with _lock:
        # 读取当前状态
        job = load_job(job_id)
        # 任务已被删除时不再重建，避免留下幽灵任务
        if job is None:
            return None
        # 合并要更新的字段
        job.update(fields)
        # 写回
        return _save_job(job)


def _update_case(job_id, case_id, **fields):
    """更新任务中某个样本的状态。"""
    # 与整体状态共用一把锁，保证同一份文件的读改写是串行的
    with _lock:
        # 读取当前状态
        job = load_job(job_id)
        # 任务已被删除时直接返回
        if job is None:
            return None
        # 找到对应样本并更新
        for item in job.get("cases", []):
            # 按样本标识匹配
            if item.get("case_id") == case_id:
                # 合并字段
                item.update(fields)
                # 找到即可停止
                break
        # 重新统计整体进度，界面只读这几个数就能画进度条
        _recount(job)
        # 写回
        return _save_job(job)


def _recount(job):
    """按逐样本状态重算整体进度计数。"""
    # 取出样本清单
    items = job.get("cases", [])
    # 已产出结果的样本数（含切分失败但已记录的）
    job["done_count"] = sum(1 for c in items if c.get("status") == "done")
    # 未能产出结果、续跑时需要重跑的样本数
    job["failed_count"] = sum(1 for c in items if c.get("status") == "failed")
    # 样本总数
    job["total_count"] = len(items)
    # 返回任务本身
    return job


def _load_case_result(job_id, case_id):
    """读取某个样本已落盘的评估结果，不存在或损坏时返回 None。"""
    # 结果文件路径
    path = _case_file(job_id, case_id)
    # 文件不存在说明该样本尚未跑过
    if not path.is_file():
        return None
    # 损坏时按未跑过处理，续跑会重新评估它
    try:
        # 读取并解析
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        # 返回空触发重跑
        return None


def _save_case_result(job_id, case_id, result):
    """把单个样本的完整评估结果落盘。

    存的是**完整**结果（含 `_records` 切片记录），不是精简版：
    汇总时要靠它重建整份报告与切片快照，缺了记录，续跑出来的那一轮
    就没有文本快照，切片预览与规则质量页都会空着。
    """
    # 原子写出，避免汇总时读到写了一半的结果
    _write_json_atomic(_case_file(job_id, case_id), result)


def list_jobs(limit=KEEP_FINISHED):
    """返回全部任务摘要，按创建时间倒序。

    摘要不含逐样本明细，只有整体状态与进度计数——历史轮次表格顶上那一行
    只需要这些，没必要为渲染一行而读几十个样本的状态。
    """
    # 只取渲染列表所需的字段，逐样本明细留在各自的任务文件里
    items = [summary(job) for job in _iter_jobs()]
    # 按创建时间倒序，最新的排在最前
    items.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    # 截断到上限
    return items[:limit]


def summary(job):
    """把完整任务状态压成一行摘要。"""
    # 只保留渲染进度行需要的字段
    return {
        "job_id": job.get("job_id", ""),  # 任务标识
        "status": job.get("status", ""),  # 运行状态
        "created_at": job.get("created_at", ""),  # 创建时间
        "updated_at": job.get("updated_at", ""),  # 最后推进时间，界面据此判断是否卡住
        "label": job.get("label", ""),  # 用户备注，会随任务带到最终快照
        "done_count": job.get("done_count", 0),  # 已完成样本数
        "failed_count": job.get("failed_count", 0),  # 失败样本数
        "total_count": job.get("total_count", 0),  # 样本总数
        "config": job.get("parser_config", {}),  # 锁定的运行参数
        "code_hash": job.get("code_hash", ""),  # 创建时的被测代码指纹
        "code_mixed": job.get("code_mixed", False),  # 是否跨代码版本续跑过
        "run_id": job.get("run_id", ""),  # 成功后产出的正式轮次标识
        "error": job.get("error", ""),  # 失败原因摘要
        "stage": job.get("stage", ""),  # 失败发生在哪一段
        "current_case": job.get("current_case", ""),  # 正在评估的样本，界面显示「正在跑哪个」
    }


def unfinished_jobs():
    """返回尚未产出正式轮次的任务。

    这正是历史轮次表格顶部要额外插进去的那些行：正在跑的、被打断的、失败的。
    已经成功产出快照的任务不在此列——它们在表格里已经有自己的一行了。
    """
    # 过滤掉已成功产出轮次的任务
    return [j for j in list_jobs() if not j.get("run_id")]


def is_busy():
    """判断当前是否已有任务在跑，忙时返回占用者的 job_id。

    先自愈再判断：只有本进程的线程会推进任务，因此别的进程留下的 running
    一定是残留。不先清掉它，一个被打断的任务会一直挡住所有新评估，
    而且要等到下次重启服务才解得开——那正是这个工具最不该有的卡点。
    """
    # 先认领掉不属于本进程的残留，避免它们被误判成「有任务在跑」
    reconcile()
    # 当前进程号
    pid = os.getpid()
    # 逐个任务目录检查，读的是完整状态而非摘要（摘要不含 pid）
    for job in _iter_jobs():
        # 本进程正在推进的任务才算真的忙
        if job.get("status") == "running" and job.get("pid") == pid:
            return job.get("job_id", "")
    # 没有运行中的任务
    return ""


def _iter_jobs():
    """遍历全部任务的完整状态，跳过缺失与损坏的。"""
    # 任务目录尚未建立时没有任何任务
    if not JOBS_DIR.is_dir():
        return
    # 逐个任务目录读取
    for sub in sorted(JOBS_DIR.iterdir()):
        # 跳过非目录项
        if not sub.is_dir():
            continue
        # 读取任务状态
        job = load_job(sub.name)
        # 状态缺失或损坏的目录跳过
        if job is not None:
            yield job


def reconcile():
    """把不属于本进程的「运行中」任务改判为「已中断」。

    热加载重启、Ctrl+C、崩溃，都会让 job.json 停在 running 这个状态上。
    不改判的话，界面会一直显示「进行中」等一个永远不会推进的任务，
    新评估也会因为「已有任务在跑」被一直挡住。

    服务启动时调一次，此后每次判断是否空闲时顺带再查一遍——只在真的发现
    残留时才写盘，正常情况下只是把已经读出来的状态过一遍，开销可以忽略。
    """
    # 当前进程号，用于区分「本进程正在跑的」与「别的进程遗留的」
    pid = os.getpid()
    # 记录被改判的任务，供启动日志说明
    fixed = []
    # 逐个任务检查
    for job in _iter_jobs():
        # 只处理停在运行中、且不属于本进程的任务
        if job.get("status") == "running" and job.get("pid") != pid:
            # 改判为中断，并说明原因，界面据此提供「续跑」
            _update_job(job["job_id"], status="interrupted",
                        error="评估进程重启，任务被中断（改动 labkit 下的代码会触发热加载重启）",
                        stage="evaluate", current_case="")
            # 记录被改判的任务
            fixed.append(job["job_id"])
    # 返回改判清单
    return fixed


def _prune_finished():
    """清理过多的历史任务记录，只保留最近若干条已结束任务。"""
    # 取出全部任务，按创建时间倒序
    items = list_jobs(limit=10 ** 6)
    # 已结束且已产出轮次的任务才是可清理对象——中断与失败的要留着续跑
    finished = [j for j in items if j.get("status") == "done"]
    # 超出保留上限的部分整目录删除
    for item in finished[KEEP_FINISHED:]:
        # 忽略删除失败，清理是尽力而为的维护动作
        try:
            shutil.rmtree(_job_dir(item["job_id"]), ignore_errors=True)
        except Exception:
            pass


def create_job(parser_config=None, only=None, compare="", set_baseline=False, label=""):
    """创建一轮评估任务并在后台开跑，返回任务状态。

    参数在这里一次性锁定并写进 job.json：续跑要用同一套参数，
    而顶栏的切分配置随时可能被改动，不锁死就会出现「一轮里两套参数」。
    """
    # 已有任务在跑时拒绝，评估会占满 CPU，两轮并行只会都变慢
    busy = is_busy()
    # 明确回报是哪个任务占着，界面可直接跳过去看进度
    if busy:
        return {"ok": False, "code": "busy", "job_id": busy, "message": f"已有评估正在运行：{busy}"}
    # 合并默认值并处理字段派生，得到本轮实际生效的完整切分配置
    config = build_parser_config(parser_config)
    # 加载本轮要跑的语料：未点名样本时按评估口径取样
    # （排除语料库里停用的，以及 pptx 这类切块契约不同、不参与评估的文件类型）
    cases = load_cases(only=only, for_eval=True)
    # 语料库为空时没有可评估的内容，直接回报而不是产出一轮空快照
    if not cases:
        return {"ok": False, "code": "empty", "message": "没有可评估的样本，请先到「语料库」页导入或启用语料"}
    # 以时间戳作为任务标识，天然有序且可读。带上毫秒有两个作用：
    # 一是同一秒内再建任务不会撞 id；二是与历史轮次的 run_id 区分开——
    # 语料少时一轮几秒就跑完，秒级时间戳会让任务与它产出的轮次同名，
    # 界面上「任务 X」和「轮次 X」并排出现只会让人以为是同一个东西
    job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    # 组装初始任务状态
    job = {
        "job_id": job_id,  # 任务标识
        "status": "running",  # 运行状态
        "created_at": _now(),  # 创建时间
        "updated_at": _now(),  # 最后推进时间
        "pid": os.getpid(),  # 执行进程号，重启后据此识别中断
        "parser_config": config,  # 锁定的切分配置，续跑沿用它
        "only": list(only) if only else [],  # 点名评估的样本，空表示全量
        "compare": compare,  # 完成后与哪一轮对比
        "set_baseline": bool(set_baseline),  # 完成后是否设为基准
        "label": label,  # 用户备注，会带到最终快照
        "code_hash": runs.code_fingerprint()["hash"],  # 创建时的被测代码指纹
        "code_mixed": False,  # 是否跨代码版本续跑过
        "code_hash_history": [],  # 续跑时经历过的代码指纹，混合时用于说明
        "resumed": 0,  # 续跑次数
        "run_id": "",  # 成功后产出的正式轮次标识
        "error": "",  # 失败原因摘要
        "error_type": "",  # 异常类型
        "traceback": "",  # 完整堆栈，界面可展开查看
        "stage": "",  # 失败发生在哪一段
        "current_case": "",  # 正在评估的样本
        # 逐样本状态清单。顺序即评估顺序，与 load_cases 的排序一致，
        # 界面照此顺序显示，续跑时也按同一顺序补跑
        "cases": [
            {
                "case_id": c["case_id"],  # 样本标识
                "filename": c.get("filename", ""),  # 原始文件名，界面显示这个
                "kind": c.get("kind", ""),  # 文档大类
                "status": "pending",  # 样本状态：pending/running/done/failed
                "chunk_count": 0,  # 切片数，跑完后填
                "finding_count": 0,  # 问题数，跑完后填
                "elapsed": 0,  # 耗时秒数
                "error": "",  # 失败原因
                "case_error": "",  # 切分本身报的错（已产出结果，但内容是空的）
            }
            for c in cases
        ],
    }
    # 初始化进度计数
    _recount(job)
    # 落盘，此后一切进度都以文件为准
    _save_job(job)
    # 清理过多的历史任务记录
    _prune_finished()
    # 后台开跑
    _start_worker(job_id)
    # 返回初始状态，前端据此开始轮询
    return {"ok": True, "job_id": job_id, "job": summary(job)}


def resume_job(job_id, force=False):
    """从断点继续一轮被中断或失败的评估。

    force 为真表示使用者已确认「代码变了也要接着跑」。不给这个确认机会的话，
    中断往往正是因为改代码触发了重启，续跑就会悄悄产出一轮前半段用旧代码、
    后半段用新代码的快照——它的问题总数与任何一轮都不可比。
    """
    # 读取任务状态
    job = load_job(job_id)
    # 任务不存在时明确回报
    if job is None:
        return {"ok": False, "code": "not_found", "message": f"任务不存在：{job_id}"}
    # 已经成功产出轮次的任务无需续跑
    if job.get("run_id"):
        return {"ok": False, "code": "finished", "message": f"该任务已完成并产出轮次 {job['run_id']}"}
    # 正在跑的任务不必续，避免同一任务起两条线程
    if job.get("status") == "running" and job.get("pid") == os.getpid():
        return {"ok": False, "code": "running", "message": "该任务正在运行中"}
    # 别的任务占着时拒绝
    busy = is_busy()
    # 明确回报占用者
    if busy and busy != job_id:
        return {"ok": False, "code": "busy", "job_id": busy, "message": f"已有评估正在运行：{busy}"}
    # 当前被测代码指纹
    current = runs.code_fingerprint()["hash"]
    # 与创建任务时不一致，说明中断期间代码改过
    if current != job.get("code_hash") and not force:
        # 交回前端决定：接着跑（结果混合两套代码）还是放弃重跑
        return {
            "ok": False,  # 本次未执行
            "code": "code_changed",  # 前端据此弹确认框
            "job_code_hash": job.get("code_hash", ""),  # 中断时的代码指纹
            "current_code_hash": current,  # 当前代码指纹
            "done_count": job.get("done_count", 0),  # 已用旧代码跑完的样本数
            "total_count": job.get("total_count", 0),  # 样本总数
            "message": "被测代码在中断后已改动，续跑会让这一轮混合两套代码的结果",
        }
    # 记录本次续跑经历的代码指纹，混合时用于在快照里说明
    history = list(job.get("code_hash_history") or [])
    # 代码变了才记，没变的续跑不算混合
    if current != job.get("code_hash"):
        # 首次记录时把原始指纹也补进去，历史才完整
        if not history:
            history.append(job.get("code_hash", ""))
        # 追加当前指纹
        history.append(current)
    # 更新任务状态并开跑
    _update_job(
        job_id,
        status="running",  # 回到运行中
        pid=os.getpid(),  # 记录新的执行进程
        cancel_requested=False,  # 清掉上次的取消标记，否则续跑会立刻被判为已取消
        error="", error_type="", traceback="", stage="",  # 清掉上次的失败信息
        resumed=int(job.get("resumed", 0)) + 1,  # 续跑次数加一
        code_mixed=bool(history) or bool(job.get("code_mixed")),  # 代码变过即标记混合
        code_hash_history=history,  # 经历过的代码指纹
    )
    # 后台开跑，worker 会跳过已有结果的样本
    _start_worker(job_id)
    # 回报已恢复
    return {"ok": True, "job_id": job_id, "resumed": True}


def cancel_job(job_id):
    """请求取消一个正在跑的任务。

    只能在样本之间生效：正在跑的那个样本会跑完（切分与检测跑到一半强行中断
    只会留下半截结果），随后循环退出。已完成的样本结果照常保留，之后仍可续跑。
    """
    # 读取任务状态
    job = load_job(job_id)
    # 任务不存在时回报
    if job is None:
        return {"ok": False, "message": f"任务不存在：{job_id}"}
    # 非运行中的任务无需取消
    if job.get("status") != "running":
        return {"ok": False, "message": "该任务不在运行中"}
    # 置内存标记，让正在跑的循环立刻看到
    _cancel_events.setdefault(job_id, threading.Event()).set()
    # 同时写进状态文件，跨进程也能识别
    _update_job(job_id, cancel_requested=True)
    # 回报已受理，实际停止发生在当前样本跑完之后
    return {"ok": True, "message": "已请求取消，当前样本跑完后停止"}


def delete_job(job_id):
    """删除一个任务及其全部中间结果。"""
    # 任务目录
    path = _job_dir(job_id)
    # 目录不存在时按已删除处理，保持幂等
    if not path.is_dir():
        return {"ok": True, "removed": False}
    # 运行中的任务不允许直接删，否则后台线程会对着不存在的目录写文件
    job = load_job(job_id)
    # 检查运行状态
    if job and job.get("status") == "running" and job.get("pid") == os.getpid():
        return {"ok": False, "message": "任务正在运行，请先取消"}
    # 整目录删除，中间结果一并清掉
    shutil.rmtree(path, ignore_errors=True)
    # 清掉可能残留的取消标记
    _cancel_events.pop(job_id, None)
    # 回报删除成功
    return {"ok": True, "removed": True}


def retry_case(job_id, case_id):
    """把某个样本置回待处理，使下一次续跑重新评估它。

    用于单样本失败：改完代码想只补跑那一个，而不是把几十个样本全部重来。
    """
    # 读取任务状态
    job = load_job(job_id)
    # 任务不存在时回报
    if job is None:
        return {"ok": False, "message": f"任务不存在：{job_id}"}
    # 运行中的任务不允许改样本状态，避免与正在跑的循环打架
    if job.get("status") == "running":
        return {"ok": False, "message": "任务正在运行，请先取消再重试单个样本"}
    # 删掉该样本已落盘的结果，续跑才会重新评估它
    path = _case_file(job_id, case_id)
    # 存在才删
    if path.is_file():
        path.unlink()
    # 状态回到待处理，清掉上次的错误
    _update_case(job_id, case_id, status="pending", error="", case_error="",
                 chunk_count=0, finding_count=0, elapsed=0)
    # 回报成功，实际重跑由续跑触发
    return {"ok": True, "message": "已标记为待处理，点「续跑」重新评估该样本"}


def _start_worker(job_id):
    """启动后台线程执行任务。"""
    # 清掉上一轮可能残留的取消标记，否则续跑会立刻被判为已取消
    _cancel_events.pop(job_id, None)
    # 以守护线程运行，服务退出时不阻塞
    threading.Thread(target=_worker, args=(job_id,), daemon=True, name=f"eval-{job_id}").start()


def _cancelled(job_id):
    """判断任务是否已被请求取消。"""
    # 内存标记优先，正在跑的循环能立刻看到
    event = _cancel_events.get(job_id)
    # 已置位即为取消
    if event is not None and event.is_set():
        return True
    # 回落到状态文件，兼容跨进程的取消请求
    job = load_job(job_id)
    # 任务被删除时也按停止处理，避免线程对着不存在的目录继续写
    if job is None:
        return True
    # 读取取消标记
    return bool(job.get("cancel_requested"))


def _worker(job_id):
    """任务主循环：逐样本评估并落盘，跑完后汇总成正式轮次。"""
    # 读取任务状态
    job = load_job(job_id)
    # 任务已被删除时直接退出
    if job is None:
        return
    # 从锁定的配置重建检测阈值，与切分预算取同一来源，
    # 避免超长判据与实际预算脱节
    config = job.get("parser_config") or {}
    # 检测阈值只由 chunk_token_num 派生，故不必单独持久化
    cfg = DetectorConfig(chunk_token_num=int(config.get("chunk_token_num") or 512))
    # 重新加载语料，拿到含产物路径的完整样本描述。
    # 必须重新加载而不是存进 job.json：里面有 Path 对象，且中断期间语料可能变动
    # 这里刻意取全部样本而非评估口径：续跑要按 job.json 里已记录的样本清单走，
    # 中途若有样本被停用或被排除，也应按当时锁定的清单跑完，不改变本轮构成
    by_id = {c["case_id"]: c for c in load_cases(only=None)}
    # 逐样本评估
    for item in list(job.get("cases", [])):
        # 样本标识
        case_id = item["case_id"]
        # 已有结果的样本直接跳过，这正是断点续跑的关键
        if item.get("status") == "done" and _load_case_result(job_id, case_id) is not None:
            continue
        # 每个样本开跑前检查取消请求
        if _cancelled(job_id):
            # 标记为已取消并退出循环，已完成的结果照常保留
            _update_job(job_id, status="cancelled", current_case="",
                        error="已手动取消", stage="evaluate")
            return
        # 取出完整样本描述
        case = by_id.get(case_id)
        # 样本在中断期间被删除时记为失败，不中断整轮
        if case is None:
            _update_case(job_id, case_id, status="failed",
                         error="样本已从语料库中移除")
            continue
        # 标记为正在评估，界面据此显示「进行中」
        _update_job(job_id, current_case=case_id)
        # 更新样本状态
        _update_case(job_id, case_id, status="running")
        # 记录开始时间用于统计耗时
        started = time.time()
        # 单样本崩溃不应带走整轮：记为失败，续跑时可单独补跑
        try:
            # 执行单样本评估。切分失败已在内部被吸收成带 error 字段的结果，
            # 能走到 except 的是检测器或归因层面的意外崩溃
            result = evaluate_case(case, cfg=cfg, parser_config=config)
            # 结果落盘，进程此后随时可以死
            _save_case_result(job_id, case_id, result)
            # 更新该样本的统计与状态
            _update_case(
                job_id, case_id,
                status="done",  # 已产出结果
                chunk_count=result.get("chunk_count", 0),  # 切片数
                finding_count=result.get("finding_count", 0),  # 问题数
                elapsed=round(time.time() - started, 1),  # 耗时
                # 切分自身报的错：结果是有的，只是内容为空，界面要区别于「未跑」
                case_error=result.get("error", ""),
            )
        except Exception as e:
            # 记录完整堆栈到日志，界面只显示摘要
            logging.exception(f"[chunk-lab] 样本评估失败 job={job_id} case={case_id}")
            # 标记该样本失败，续跑时会重新评估它
            _update_case(job_id, case_id, status="failed",
                         error=f"{type(e).__name__}: {e}",
                         elapsed=round(time.time() - started, 1))
    # 全部样本处理完毕，清掉「正在跑哪个」
    _update_job(job_id, current_case="")
    # 汇总成正式轮次
    _finalize(job_id)


def _finalize(job_id):
    """把逐样本结果汇总成一轮正式快照。

    这一段与逐样本评估分开处理失败：汇总失败（例如写快照时磁盘满、
    继承标注时出意外）不该让几十个样本的结果白跑，任务留在 failed 状态，
    修好之后点续跑会跳过已完成的样本，直接重来这一段。
    """
    # 读取任务状态
    job = load_job(job_id)
    # 任务已被删除时直接退出
    if job is None:
        return
    # 汇总过程中的任何异常都要落到任务状态里，而不是让线程静默死掉
    try:
        # 收集逐样本结果，顺序与任务清单一致，保证报告可比
        results = []
        # 逐个取出已落盘的结果
        for item in job.get("cases", []):
            # 读取该样本的完整结果
            result = _load_case_result(job_id, item["case_id"])
            # 有结果直接采用
            if result is not None:
                results.append(result)
            else:
                # 没结果的样本（失败或被取消前未跑到）补一条错误记录。
                # 不补的话 case_count 会少于语料数，看报告的人会以为语料变少了
                results.append({
                    "case_id": item["case_id"],  # 样本标识
                    "filename": item.get("filename", ""),  # 原始文件名
                    "note": "",  # 无关注点说明
                    "kind": item.get("kind", ""),  # 文档大类
                    "error": item.get("error") or "未完成评估",  # 失败原因
                    "chunk_count": 0,  # 无产出
                    "findings": [],  # 无命中
                })
        # 重建检测阈值，与评估阶段口径一致
        config = job.get("parser_config") or {}
        # 只由切分预算派生
        cfg = DetectorConfig(chunk_token_num=int(config.get("chunk_token_num") or 512))
        # 汇总成完整报告，与一次跑完的路径共用同一处实现
        report = build_report(results, cfg, config)
        # 跨代码版本续跑过的轮次要在快照里标明，否则代码指纹会说谎——
        # 它只记录汇总那一刻的代码状态，而部分样本实际是用旧代码跑的
        if job.get("code_mixed"):
            # 标记混合
            report["code_mixed"] = True
            # 附上经历过的代码指纹，便于日后判断这一轮能与谁比较
            report["code_hash_history"] = job.get("code_hash_history") or []
        # 记下汇总前的最新一轮：它就是新版本要继承标注的来源
        prev_run = runs.latest_run_id()
        # 存为不可变的历史轮次快照
        run_id = runs.save_run(report, label=job.get("label", ""))
        # 把上一版的人工判定继承到新版本，并按新快照逐条重判
        inherited = annotations.inherit(prev_run, run_id)
        # 按需把本轮设为新的对比基准
        if job.get("set_baseline"):
            runs.set_baseline(run_id)
        # 标记任务完成，并记下产出的轮次
        _update_job(job_id, status="done", run_id=run_id, inherited=inherited,
                    inherited_from=prev_run, finished_at=_now())
        # 成功后清理中间结果：数据已经存进正式快照与压缩的切片文本，
        # 这份未压缩的副本再留着就是重复占盘（一轮 6-10MB）
        cases_dir = _job_dir(job_id) / "cases"
        # 存在才删，只删中间结果目录，job.json 留着作为任务履历
        if cases_dir.is_dir():
            shutil.rmtree(cases_dir, ignore_errors=True)
    except Exception as e:
        # 完整堆栈进日志，便于事后排查
        logging.exception(f"[chunk-lab] 评估汇总失败 job={job_id}")
        # 任务标记为失败，中间结果保留，修好后续跑会直接重来汇总这一段
        _update_job(job_id, status="failed", stage="finalize",
                    error=f"{type(e).__name__}: {e}",
                    error_type=type(e).__name__,
                    traceback=traceback.format_exc())
