"""运行历史：每次评估存成不可变快照，任意两轮之间都能对比。

替代原先「单一基线文件」的设计。那个设计有结构性缺陷：基线一旦更新，
之前的历史就没了，无法回答「这几次改动累计效果如何」「哪一次引入了这个回归」。

三条核心约定：
  1. 每轮评估是不可变快照，存成独立文件，永不原地修改（打标签除外）；
  2. 基线不再是一份数据，而是指向某一轮的指针，随时可改指向；
  3. 每轮记录被测代码的内容指纹——没有它，历史轮次不知对应什么代码状态，对比就没有意义。
"""

import gzip  # 导入 gzip 压缩切片文本快照，中文文本压缩率高且属标准库
import hashlib  # 导入 hashlib 计算被测代码的内容指纹
import json  # 导入 json 读写快照
import subprocess  # 导入 subprocess 读取 ragflow 的 git 版本信息
from datetime import datetime  # 导入 datetime 生成轮次标识与时间戳

from .paths import BASELINE_DIR, RAGFLOW_ROOT, RUNS_DIR  # 导入目录常量

# 轻量索引文件：只存每轮摘要，避免为渲染列表而读取全部大文件
INDEX_FILE = RUNS_DIR / "index.json"
# 基线指针文件：记录当前作为对比基准的轮次
POINTER_FILE = BASELINE_DIR / "pointer.json"
# 参与代码指纹计算的源文件，是切分行为的实际决定者
FINGERPRINT_SOURCES = [
    "rag/app/mineru_chunker.py",  # 切分主逻辑
    "deepdoc/parser/mineru_parser.py",  # 解析与位置标签逻辑
    "common/mineru_noise.py",  # 噪声治理逻辑
]


def code_fingerprint():
    """采集被测代码的版本信息：内容哈希 + git 提交 + 是否有未提交改动。

    内容哈希是主依据：改了没提交时 git commit 不变，只有哈希会变。
    git 信息作为辅助，便于把某一轮关联回具体提交。
    """
    # 累积各源文件内容用于统一哈希
    hasher = hashlib.sha256()
    # 记录参与计算的文件及其单独哈希，便于定位是哪个文件变了
    files = {}
    # 逐个读取源文件
    for rel in FINGERPRINT_SOURCES:
        # 拼出绝对路径
        path = RAGFLOW_ROOT / rel
        # 文件缺失时记录状态而非中断，保证实验室在源码结构变动时仍可运行
        if not path.is_file():
            files[rel] = "missing"
            continue
        # 以二进制读取，避免换行符差异影响哈希
        data = path.read_bytes()
        # 累加到总哈希
        hasher.update(data)
        # 记录该文件自身的短哈希
        files[rel] = hashlib.sha256(data).hexdigest()[:12]

    # 读取 ragflow 的 git 状态作为辅助信息
    commit, dirty = "", False
    # git 不可用或非仓库时静默降级，不影响评估
    try:
        # 取当前提交的短哈希
        commit = subprocess.run(
            ["git", "-C", str(RAGFLOW_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        # 检查工作区是否有未提交改动，仅关注参与指纹的源文件
        status = subprocess.run(
            ["git", "-C", str(RAGFLOW_ROOT), "status", "--porcelain", *FINGERPRINT_SOURCES],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        # 输出非空即存在未提交改动
        dirty = bool(status)
    except Exception:
        # 保持默认值，代码指纹仍然有效
        pass

    # 返回完整的版本信息
    return {
        "hash": hasher.hexdigest()[:12],  # 全部源文件的联合内容哈希，判断代码是否相同的唯一依据
        "files": files,  # 逐文件短哈希，用于定位差异来源
        "git_commit": commit,  # 关联的 git 提交
        "git_dirty": dirty,  # 是否含未提交改动
    }


def corpus_fingerprint(report):
    """由报告中的样本构成计算语料指纹，用于判断两轮是否跑在同一套语料上。"""
    # 取样本标识与原始块数，语料增删或产物更新都会改变它
    parts = sorted(f"{c.get('case_id')}:{c.get('block_count', 0)}" for c in report.get("cases", []))
    # 拼接后哈希，得到定长指纹
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _load_index():
    """读取轮次索引，文件缺失或损坏时返回空列表。"""
    # 索引不存在说明尚无历史
    if not INDEX_FILE.is_file():
        return []
    # 损坏的索引不应让整个功能不可用，降级为空并在后续写入时重建
    try:
        # 读取并解析
        with INDEX_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        # 返回空列表触发重建
        return []


def _save_index(index):
    """写回轮次索引。"""
    # 确保目录存在
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    # 按生成时间倒序存储，前端无需再排序
    index.sort(key=lambda r: r.get("generated_at", ""), reverse=True)
    # 写出索引
    with INDEX_FILE.open("w", encoding="utf-8") as fh:
        # indent 便于人工查看与版本管理下的 diff
        json.dump(index, fh, ensure_ascii=False, indent=2)


def chunks_path(run_id):
    """某一轮的切片文本快照路径。"""
    # 与报告同目录，用 .chunks.gz 后缀区分
    return RUNS_DIR / f"{run_id}.chunks.gz"


def save_chunks(run_id, chunks_by_case):
    """保存该轮的切分文本快照。

    这是历史可回溯的前提：离线驱动调的是当前代码，代码一改，旧轮次的切分
    结果就再也无法重现。不存下来，历史轮次就只剩统计数字，看不到那时候
    文本被切成了什么样，也就无法比较切分边界的变化。

    只保留比较所需的字段并 gzip 压缩，中文文本压缩率约 3-4 倍。
    """
    # 确保目录存在
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    # 精简字段：正文与定位信息是比较所需，图像对象等一律不留
    slim = {}
    # 逐样本精简
    for case_id, records in (chunks_by_case or {}).items():
        # 每个切片只保留比较与展示必需的字段
        slim[case_id] = [
            {
                "index": r["index"],  # 切片序号
                "content": r["content"],  # 正文全文，比较切分边界的核心依据
                "doc_type_kwd": r.get("doc_type_kwd", ""),  # 块类型
                "important_kwd": r.get("important_kwd", []),  # 标题面包屑
                "page_num_int": r.get("page_num_int", []),  # 页码
                "is_child": r.get("is_child", False),  # 是否子块
            }
            for r in records
        ]
    # 以 gzip 写出，text mode 便于直接 json.dump
    with gzip.open(chunks_path(run_id), "wt", encoding="utf-8") as fh:
        # 不缩进以进一步减小体积，本文件面向程序读取
        json.dump(slim, fh, ensure_ascii=False)
    # 返回文件路径供调用方记录体积
    return chunks_path(run_id)


def load_chunks(run_id, case_id=None):
    """读取某一轮的切分文本快照；指定样本时只返回该样本。"""
    # 快照路径
    path = chunks_path(run_id)
    # 文件不存在说明该轮是旧版本产生的，没有文本快照
    if not path.is_file():
        return None
    # 读取并解析
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        # 快照损坏时按缺失处理，不影响其它功能
        return None
    # 指定样本时只取该样本
    if case_id:
        return data.get(case_id)
    # 否则返回全部
    return data


def save_run(report, label=""):
    """把一次评估结果存为不可变快照，返回其 run_id。"""
    # 以时间戳作为轮次标识，天然有序且可读
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 补充版本与语料指纹，这是历史轮次可解释的前提
    report = dict(report)
    # 取出切分文本并单独压缩存储，不随报告 JSON 落盘以免撑大文件
    chunks_by_case = report.pop("_chunks", None)
    # 有文本时写出快照并记录体积，便于观察磁盘占用
    if chunks_by_case:
        # 写出压缩快照
        path = save_chunks(run_id, chunks_by_case)
        # 记录压缩后大小，前端可展示
        report["chunks_bytes"] = path.stat().st_size
        # 标记该轮含文本快照，供界面判断能否做文本对比
        report["has_chunks"] = True
    else:
        # 无文本快照时明确标记，旧轮次即属此类
        report["has_chunks"] = False
    # 记录被测代码的指纹
    report["code"] = code_fingerprint()
    # 记录语料指纹
    report["corpus_hash"] = corpus_fingerprint(report)
    # 记录轮次标识
    report["run_id"] = run_id
    # 记录用户备注，便于日后辨认这一轮做了什么
    report["label"] = label

    # 确保目录存在
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    # 写出完整快照
    with (RUNS_DIR / f"{run_id}.json").open("w", encoding="utf-8") as fh:
        # 保留中文可读性
        json.dump(report, fh, ensure_ascii=False, indent=2)

    # 更新索引：只存渲染列表所需的摘要字段
    index = _load_index()
    # 追加本轮摘要
    index.append({
        "run_id": run_id,  # 轮次标识
        "generated_at": report.get("generated_at", ""),  # 生成时间
        "label": label,  # 用户备注
        "case_count": report.get("case_count", 0),  # 样本数
        "chunk_total": report.get("chunk_total", 0),  # 切片总数
        "finding_total": report.get("finding_total", 0),  # 问题总数
        "config": report.get("config", {}),  # 运行参数
        "code_hash": report["code"]["hash"],  # 代码指纹
        "git_commit": report["code"]["git_commit"],  # 关联提交
        "git_dirty": report["code"]["git_dirty"],  # 是否含未提交改动
        "corpus_hash": report["corpus_hash"],  # 语料指纹
        "totals_by_detector": report.get("totals_by_detector", {}),  # 各检测器命中，供趋势图使用
        "has_chunks": report.get("has_chunks", False),  # 是否含切分文本快照
        "chunks_bytes": report.get("chunks_bytes", 0),  # 文本快照压缩后体积
    })
    # 写回索引
    _save_index(index)
    # 返回轮次标识
    return run_id


def migrate_legacy_baseline():
    """把旧版单文件基线收编成第一个历史轮次。

    旧设计把基线存在 baselines/current.json。升级到运行历史后若直接忽略它，
    既有的对比基准就断了，因此在历史为空时把它作为首轮导入并设为基准。
    仅执行一次：一旦历史非空就不再触碰。
    """
    # 旧基线文件路径
    legacy = BASELINE_DIR / "current.json"
    # 文件不存在说明无需迁移
    if not legacy.is_file():
        return None
    # 历史非空说明已经迁移过或已有新数据，不重复导入
    if _load_index():
        return None
    # 读取旧基线；损坏时放弃迁移而不是让服务起不来
    try:
        with legacy.open("r", encoding="utf-8") as fh:
            report = json.load(fh)
    except Exception:
        return None
    # 存为首个历史轮次，备注说明来源
    run_id = save_run(report, label="迁移自旧版基线")
    # 设为当前基准，保持升级前后的对比基准一致
    set_baseline(run_id)
    # 返回新轮次标识供调用方提示
    return run_id


def list_runs():
    """返回全部轮次摘要，按时间倒序。"""
    # 索引本身已按时间倒序存储
    return _load_index()


def load_run(run_id):
    """读取某一轮的完整快照，不存在时返回 None。"""
    # 快照路径
    path = RUNS_DIR / f"{run_id}.json"
    # 不存在直接返回空
    if not path.is_file():
        return None
    # 读取并返回
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def set_label(run_id, label):
    """给某一轮打备注。这是快照唯一允许的原地修改。"""
    # 读取完整快照
    report = load_run(run_id)
    # 轮次不存在时返回失败
    if report is None:
        return False
    # 更新备注
    report["label"] = label
    # 写回快照
    with (RUNS_DIR / f"{run_id}.json").open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    # 同步更新索引中的备注
    index = _load_index()
    # 找到对应条目并更新
    for item in index:
        if item.get("run_id") == run_id:
            item["label"] = label
            break
    # 写回索引
    _save_index(index)
    # 返回成功
    return True


def delete_run(run_id):
    """删除某一轮快照。若它正是当前基准，同时清空基线指针。"""
    # 快照路径
    path = RUNS_DIR / f"{run_id}.json"
    # 删除文件（存在才删）
    if path.is_file():
        path.unlink()
    # 一并删除切分文本快照，避免留下孤儿文件占用磁盘
    cpath = chunks_path(run_id)
    if cpath.is_file():
        cpath.unlink()
    # 从索引中移除
    index = [r for r in _load_index() if r.get("run_id") != run_id]
    # 写回索引
    _save_index(index)
    # 被删除的轮次若是当前基准，必须清空指针，否则后续对比会指向不存在的数据
    if get_baseline_id() == run_id:
        clear_baseline()
    # 返回成功
    return True


def set_baseline(run_id):
    """把某一轮设为对比基准。基线只是一个指针，不复制数据。"""
    # 确保目录存在
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    # 写出指针文件
    with POINTER_FILE.open("w", encoding="utf-8") as fh:
        # 同时记录设定时间，便于了解基准的新鲜度
        json.dump({"run_id": run_id, "set_at": datetime.now().isoformat(timespec="seconds")}, fh, ensure_ascii=False, indent=2)
    # 返回成功
    return True


def get_baseline_id():
    """返回当前基准轮次的 run_id，未设定时返回 None。"""
    # 指针文件不存在说明尚未设定基准
    if not POINTER_FILE.is_file():
        return None
    # 指针损坏时按未设定处理
    try:
        # 读取指针
        with POINTER_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh).get("run_id")
    except Exception:
        # 返回空表示未设定
        return None


def clear_baseline():
    """清空基线指针。"""
    # 存在才删除
    if POINTER_FILE.is_file():
        POINTER_FILE.unlink()
    # 返回成功
    return True


def resolve_run(ref):
    """把轮次引用解析成完整快照。

    支持三种写法，便于前端与命令行统一处理：
      "baseline" 当前基准、"latest" 最新一轮、具体的 run_id。
    """
    # 空引用直接返回空
    if not ref:
        return None
    # 解析基准引用
    if ref == "baseline":
        # 取基准指向的轮次
        run_id = get_baseline_id()
        # 未设定基准时返回空
        return load_run(run_id) if run_id else None
    # 解析最新一轮
    if ref == "latest":
        # 索引已按时间倒序，首项即最新
        index = _load_index()
        # 无历史时返回空
        return load_run(index[0]["run_id"]) if index else None
    # 其余按具体 run_id 处理
    return load_run(ref)
