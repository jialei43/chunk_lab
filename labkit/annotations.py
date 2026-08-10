"""人工标注：把人的判断沉淀成基准真值，并按版本独立保存。

规则检测器只能给出「疑似」，究竟是不是问题最终仍要人来判定。把这些判定
存下来，才能回答两个此前无法回答的问题：

  - **准确率**：检测器报出来的，有多少经人工确认是真问题；
  - **召回率**：人工发现的问题，有多少被检测器抓到了。

漏报（检测器没报但人工认为有问题）尤其宝贵——它直接指出下一条该写什么规则。

**标注归属于版本**。同一段正文在不同版本下会被切成不同的块、被不同的规则命中，
所以「这是不是问题」「这条规则报得对不对」只有绑定到具体某一轮评估才说得清。
存储上一个版本一个目录，互不影响：改完规则跑新一轮，旧版本的判定原样留在旧目录，
新版本从上一版继承过来、按新快照重新判定一遍。

每条标注记两样东西，必须分清：

  - `verdict` 是**人的判定**，只有人能改；
  - `status` 是这条判定**在本版本下的成立情况**（仍在误报？已修复？回归了？），
    由标注写入或继承时按该版本快照的检出情况算出来。

分开之后，规则质量页只要读这个版本的目录就能出全部数字，不必再拿当前代码
把所有已标注样本重切一遍——那正是此前那个页面一直转圈的原因。
"""

import json  # 导入 json 读写标注文件
import shutil  # 导入 shutil 迁移旧的平铺标注文件
from datetime import datetime  # 导入 datetime 记录标注时间

from . import runs  # 导入历史轮次，标注要回该版本的快照里核对命中
from .paths import DATA_ROOT  # 导入数据根目录

# 标注存放目录，与语料、历史轮次并列；其下再按版本分子目录
ANNOTATION_DIR = DATA_ROOT / "annotations"

# 旧的平铺标注文件迁移后的存放处。刻意不删：迁移是一次性动作，
# 万一归属判断有误，原始文件还在，能重来
MIGRATED_DIR = ANNOTATION_DIR / "_migrated_backup"

# 标注结论的取值与含义。这是**人的判定**，与版本无关
VERDICTS = {
    "confirmed": "确认是问题",  # 检测器报对了
    "false_positive": "误报",  # 检测器报错了，该块没问题
    "missed": "漏报",  # 检测器没报，但人工认为有问题
}

# 判定在某个版本下的成立情况。与 VERDICTS 的区别是它随版本变化：
# 同一条「误报」判定，在标注的那一版是待修正，改完规则跑新一版就成了已修复
STATUSES = {
    "confirmed": "仍能检出",  # 确认是问题，本版本仍报出来了
    "false_positive": "仍在误报",  # 判为误报，本版本还在报——这才是待办
    "missed": "仍未覆盖",  # 人工提出的问题，本版本仍没抓到
    "fixed": "已修复",  # 曾判为误报，本版本已不再报
    "regressed": "回归",  # 曾确认能检出，本版本检不出了
    "covered": "已覆盖",  # 曾是漏报，本版本已能抓到
    "stale": "标注已失配",  # 按正文摘要在本版本里找不回那个切片
    # 该样本在本版本被停用、压根没参与评估。必须与「失配」分开：失配是标注对不上切片、
    # 属于数据问题；本项只是这一轮没跑它，判定本身完好，重新启用后照常生效
    "skipped": "本版本未评估",
}

# 摘要匹配时参与比较的长度。标注写入时截了 60 字，
# 这里取更短一点：切分逻辑改动常影响块的结尾，开头相对稳定
MATCH_PREFIX = 30


def _normalize(text):
    """把正文压成可比较的形式。

    去掉全部空白：切分器的改动经常只是换行与空格的差异，
    这些差异不该让标注失配。
    """
    # 空值统一成空串
    if not text:
        return ""
    # 去掉所有空白字符
    return "".join(str(text).split())


def chunk_body(chunk):
    """剥离切片正文开头的标题面包屑，返回真正的正文。

    切分器产出的正文形如「面包屑\\n正文」，面包屑同时写在 important_kwd 里。
    跨版本定位必须基于剥离后的正文——**改进面包屑本身就是常见的优化目标**，
    若拿含面包屑的文本去比对，任何一次面包屑修复都会让全部历史标注失配。
    """
    # 面包屑各级标题
    kws = (chunk or {}).get("important_kwd") or []
    # 切片全文
    content = (chunk or {}).get("content") or ""
    # 没有面包屑时全文即正文
    if not kws:
        return content
    # 按切分器的拼接方式还原完整面包屑，多级标题必须整体剥离
    head = " > ".join(kws)
    # 正文以完整面包屑开头时整体剥离，否则原样返回
    return content[len(head):].lstrip("\n") if content.startswith(head) else content


def excerpt_body(excerpt):
    """取标注摘要中的正文部分。

    早期摘要是直接截取切片全文得到的，形如「面包屑\\n正文开头」；
    新写入的摘要已经只存正文。这里统一成正文，两种历史数据都能比对。
    """
    # 空摘要没有正文可取
    text = excerpt or ""
    # 含换行说明首行是面包屑，取其后的正文；否则整条即正文
    return text.split("\n", 1)[1] if "\n" in text else text


def find_chunk(chunks, excerpt, used=None):
    """按正文摘要在指定版本的切分结果里找回那个切片。

    切片序号会随切分逻辑变化而整体错位，第 10 条标注会落到完全不相干的第 10 个
    切片上，所以跨版本定位一律以正文摘要为准。

    used 传入已被其它标注认领的切片序号。相邻切片的开头常常一模一样
    （同一标题下的连续段落，面包屑前缀相同），只按前缀匹配会让好几条标注
    全落到同一个切片上，后面几条的核对结果就都是假的。

    返回命中的切片；找不到时返回 None，由调用方记为标注失配。
    """
    # 已被认领的切片，避免多条标注挤到同一个上
    used = used if used is not None else set()
    # 一律以剥离面包屑后的正文比对：面包屑随标题层级修复而变，拿它比对会让全部标注失配
    full = _normalize(excerpt_body(excerpt))
    # 没有摘要就无从定位——早期标注可能缺这个字段
    if not full:
        return None
    # 摘要前缀，用于降级匹配
    key = full[:MATCH_PREFIX]
    # 预先算好每个候选切片的正文，三轮匹配复用，避免重复剥离
    bodies = [(c, _normalize(chunk_body(c))) for c in chunks]

    # 三轮匹配，一轮比一轮宽松，每轮都先跳过已被认领的切片：
    # 完整摘要最可靠，能区分开头相同的相邻块
    for c, body in bodies:
        if c.get("index") in used:
            continue
        if body.startswith(full):
            return c
    # 其次是前缀一致：切分逻辑改动常影响块的结尾，开头相对稳定
    for c, body in bodies:
        if c.get("index") in used:
            continue
        if body[:MATCH_PREFIX] == key:
            return c
    # 最后找包含关系：切分器可能把该块并入了更大的块，
    # 这仍算找到了那段文字，只是归属变了
    for c, body in bodies:
        if c.get("index") in used:
            continue
        if key in body:
            return c
    # 确实找不回来
    return None


def locate(chunks, mark, used=None, by_index=None):
    """把一条标注对齐到给定版本的切片。

    **先按序号试**：切分逻辑没动的块，在两版里序号是相同的，正文摘要能对上
    就确定是同一块。这一步不能省——只靠摘要模糊匹配的话，相邻块开头相同
    （同一标题下的连续段落，面包屑前缀一致）会让标注认领到错误的块，
    进而把好端端的判定算成回归，护栏平白无故变红。

    只有序号对不上（那一块的切分确实变了）才退回摘要匹配。

    by_index 允许调用方传入现成的序号索引，避免逐条重建。
    """
    # 已被其它标注认领的切片
    used = used if used is not None else set()
    # 序号索引，缺省时现建
    by_index = by_index if by_index is not None else {c.get("index"): c for c in chunks}
    # 标注在来源版本里的序号
    idx = mark.get("chunk_index")
    # 目标版本里同序号的那一块
    cand = by_index.get(idx)
    # 摘要的正文部分，用于确认这一块确实是同一段文字（不含面包屑，见 chunk_body）
    full = _normalize(excerpt_body(mark.get("excerpt")))
    # 序号对得上、没被认领、且正文开头与摘要一致，即可确定是同一块
    if cand is not None and idx not in used and full:
        if _normalize(chunk_body(cand)).startswith(full):
            return cand
    # 序号对不上说明切分变了，退回按摘要模糊定位
    return find_chunk(chunks, mark.get("excerpt"), used)


def hit_in_chunk(chunk, detector):
    """判断某检测器是否命中了这个切片。"""
    # 没有切片时谈不上命中
    if not chunk:
        return False
    # 未指定规则时，只要这块上有任何命中就算命中
    if not detector:
        return bool(chunk.get("findings"))
    # 指定规则时逐条比对规则名
    return any(f.get("detector") == detector for f in (chunk.get("findings") or []))


def status_of(verdict, hit):
    """由人工判定与「本版本是否仍命中」推出这条判定在本版本下的成立情况。

    这张映射表是整套按版本统计的核心：同一个人工判定，在不同版本下会落到
    不同的计数项里，而那正是「改动有没有效果」的直接答案。
    """
    # 确认是问题：仍能检出才算正常，检不出说明改动碰坏了这条规则
    if verdict == "confirmed":
        return "confirmed" if hit else "regressed"
    # 误报：不再命中即为已修复，仍在命中就是还没改的待办
    if verdict == "false_positive":
        return "false_positive" if hit else "fixed"
    # 漏报：现在能抓到就是进步，抓不到说明规则仍未覆盖
    if verdict == "missed":
        return "covered" if hit else "missed"
    # 未知判定不做解释，如实记为失配
    return "stale"


def _run_dir(run_id):
    """某个版本的标注目录。"""
    # 一个版本一个目录，目录名即轮次标识
    return ANNOTATION_DIR / str(run_id)


def _path(run_id, case_id):
    """某个版本下某个样本的切片标注文件路径。"""
    # 按样本分文件，避免单文件随语料增长而膨胀
    return _run_dir(run_id) / f"{case_id}.json"


def _region_path(run_id, case_id):
    """某个版本下某个样本的区域标注文件路径。

    与切片标注分开存：切片标注以序号为键、一片一条，而区域标注是人直接在
    原文版面上圈出来的，同一份文档可以有很多条，且不属于任何一个切片——
    切分器压根没在那儿切出块来，正是要记录的事实。
    """
    # 与切片标注同目录，靠后缀区分
    return _run_dir(run_id) / f"{case_id}.regions.json"


def _read_json(path, fallback):
    """读取 JSON 文件，缺失或损坏时返回给定的兜底值。"""
    # 文件不存在属正常情况：尚未标注过
    if not path.is_file():
        return fallback
    # 文件损坏不应让整个页面不可用，降级为兜底值
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        # 结构必须与兜底值一致，否则按缺失处理
        return data if isinstance(data, type(fallback)) else fallback
    except Exception:
        return fallback


def _write_json(path, data):
    """写出 JSON 文件，自动建立所属版本目录。"""
    # 版本目录可能还不存在
    path.parent.mkdir(parents=True, exist_ok=True)
    # 保留中文可读性与缩进，便于直接查看标注文件
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def load(run_id, case_id):
    """读取某版本下某样本的全部切片标注，无标注时返回空字典。"""
    # 键为切片序号的字符串形式
    return _read_json(_path(run_id, case_id), {})


def load_regions(run_id, case_id):
    """读取某版本下某样本的全部区域标注，无标注时返回空列表。"""
    # 区域标注是列表，一条一块
    return _read_json(_region_path(run_id, case_id), [])


def annotated_cases(run_id):
    """列出某版本下已经有切片标注的样本标识。

    统计与任务书只需要遍历这些样本；没标过的样本没有任何判定可算，
    遍历它们纯属浪费。
    """
    # 版本目录还不存在说明这一版一条都没标
    d = _run_dir(run_id)
    if not d.is_dir():
        return []
    # 区域标注文件同在该目录，靠后缀排除掉
    return sorted(p.stem for p in d.glob("*.json")
                  if not p.name.endswith(".regions.json"))


def latest_annotated_run():
    """返回最近一个存有切片标注的版本标识；一条标注都没有时返回空串。

    继承源默认取「上一轮评估」，但历史轮次是可以被清理的，而标注目录不会跟着删。
    真发生了清理还按上一轮找继承源，就会拿到空值、人工判定全部失效——
    判定是人一条条看出来的，不该随历史清理一起报废。因此上一轮不可用时回退到这里。
    """
    if not ANNOTATION_DIR.is_dir():  # 标注根目录不存在说明一条都没标过
        return ""  # 返回空串表示无可继承
    candidates = []  # 收集确实存有切片标注的版本目录
    for run_dir in ANNOTATION_DIR.iterdir():  # 逐个版本目录检查
        if not run_dir.is_dir() or run_dir == MIGRATED_DIR:  # 跳过非目录与迁移备份
            continue  # 检查下一个
        # 只认切片标注文件，区域标注单独存放且不参与误报判定
        if any(not p.name.endswith(".regions.json") for p in run_dir.glob("*.json")):
            candidates.append(run_dir.name)  # 该版本确有标注，纳入候选
    return max(candidates) if candidates else ""  # 版本名是时间戳，取最大即最近


def false_positive_marks(run_id):
    """收集某版本下全部「误报」判定，返回 {case_id: {切片序号: 检测器名}}。

    检测器名为空串表示标注时没有指明是哪条规则报错——那就把该切片上的问题整条忽略；
    指明了规则则只忽略那一条，同一切片上别的规则报出的问题仍然算数。

    供评估流程把人工已判定为误报的问题排除在统计之外：判定过一次就不该再反复出现在
    问题总数里，否则每轮都要重新甄别同一批噪声，真正的回归会被淹没。
    """
    out = {}  # 汇总各样本的误报切片
    for case_id in annotated_cases(run_id):  # 只遍历有标注的样本
        picked = {}  # 该样本下「切片序号 -> 检测器名」的误报映射
        for mark in load(run_id, case_id).values():  # 逐条读取该样本的标注
            if mark.get("verdict") != "false_positive":  # 只认人工判定的误报
                continue  # 其余判定不影响统计
            index = mark.get("chunk_index")  # 取该标注对应的切片序号
            if isinstance(index, int):  # 序号缺失或异常的标注无法定位，跳过
                picked[index] = (mark.get("detector") or "").strip()  # 记录该切片被判误报的规则
        if picked:  # 该样本确有误报判定时才写入结果
            out[case_id] = picked  # 归入总表
    return out  # 返回全部误报判定


def delete_case(case_id):
    """删除某个样本在**全部版本**下的切片标注与区域标注。

    只在语料被删除时调用：语料没了，这些判定既无从复核，也不该继续计入
    规则准确率——那会让「已经不存在的样本」永远压着某条规则的指标。

    刻意不动 `_migrated_backup/`：那是旧版平铺标注一次性迁移时留下的原始备份，
    不参与任何统计，是最后一份可回溯的记录。

    返回 {"files": 删掉的文件数, "runs": 涉及的版本数}。
    """
    # 累计删掉的文件数
    files = 0
    # 涉及到的版本，用集合去重
    touched = set()
    # 标注根目录尚未建立时说明一条标注都没有
    if not ANNOTATION_DIR.is_dir():
        return {"files": 0, "runs": 0}
    # 逐个版本目录扫过去：标注按版本分目录存，一个样本可能在多版里都有判定
    for run_dir in ANNOTATION_DIR.iterdir():
        # 跳过非目录项，以及迁移备份目录
        if not run_dir.is_dir() or run_dir == MIGRATED_DIR:
            continue
        # 切片标注与区域标注两个文件，都按样本标识命名
        for path in (run_dir / f"{case_id}.json", run_dir / f"{case_id}.regions.json"):
            # 存在才删；两者缺哪个都正常（只圈过区域、或只标过切片）
            if path.is_file():
                path.unlink()
                files += 1
                touched.add(run_dir.name)
    # 回报实际删了多少，供界面如实展示而不是笼统说一句已删除
    return {"files": files, "runs": len(touched)}


def _chunk_index_of(run_id, case_id):
    """取某版本某样本的切片，按序号建索引，供写标注时核对命中。

    返回 {序号: 切片} 的字典；该版本没有这个样本时返回空字典。
    """
    # 读该版本的切分文本快照（runs 内部有缓存，重复调用不会反复解压）
    chunks = runs.load_chunks(run_id, case_id)
    # 快照里没有这个样本属正常情况：语料是在这一轮之后才导入的
    if not chunks:
        return {}
    # 以切片序号为键
    return {c.get("index"): c for c in chunks}


def save_one(run_id, case_id, chunk_index, verdict, detector="", note="", excerpt=""):
    """在指定版本下写入一条标注。同一切片重复标注时覆盖旧值。"""
    # 结论必须是已知取值，避免写入无法解释的数据
    if verdict not in VERDICTS:
        raise ValueError(f"未知的标注结论：{verdict}")
    # 版本必须明确：标注不挂在版本上就没法回答「这是在哪一版下的判断」
    if not run_id:
        raise ValueError("缺少版本标识，标注必须归属于某一轮评估")
    # 读取已有标注
    data = load(run_id, case_id)
    # 取该切片在本版本快照里的检出情况，据此算出这条判定当下的成立状态
    chunk = _chunk_index_of(run_id, case_id).get(chunk_index)
    # 摘要一律存剥离面包屑后的正文。界面传来的是切片全文，含面包屑；
    # 面包屑会随标题层级修复而变，存进去会让下一版继承时全部失配
    body = chunk_body(chunk) if chunk else excerpt_body(excerpt)
    # 以切片序号为键；JSON 的键必须是字符串
    data[str(chunk_index)] = {
        "chunk_index": chunk_index,  # 切片序号
        "verdict": verdict,  # 人工判定，只有人能改
        "detector": detector,  # 相关检测器；漏报时为人工选择的问题类型
        "note": note,  # 人工备注，说明为什么这么判
        # 正文摘要用于跨版本定位：版本一换序号就错位，靠它才能把这条判定
        # 继承到下一版对应的那个切片上
        "excerpt": (body or "")[:60],
        # 该判定在本版本下的成立情况，由本版本快照的命中情况算出
        "status": status_of(verdict, hit_in_chunk(chunk, detector)),
        "origin": "self",  # 本版本直接标的，区别于从上一版继承来的
        "at": datetime.now().isoformat(timespec="seconds"),  # 标注时间
    }
    # 写回文件
    _write_json(_path(run_id, case_id), data)
    # 返回写入的条目
    return data[str(chunk_index)]


def save_many(run_id, case_id, items, verdict, detector="", note="", skip_annotated=True):
    """在指定版本下整类批量写入标注，返回实际写入数与跳过数。

    与循环调用 save_one 的区别是只读写一次文件、只取一次快照：一条规则动辄
    命中上百个切片，逐条走 save_one 会把整个标注文件反复全量重写上百遍。

    skip_annotated 默认为真，即保留已有标注不动——批量是为了省去重复点击，
    不该悄悄推翻此前逐条细看后给出的判断。
    """
    # 结论必须是已知取值，避免写入无法解释的数据
    if verdict not in VERDICTS:
        raise ValueError(f"未知的标注结论：{verdict}")
    # 版本必须明确
    if not run_id:
        raise ValueError("缺少版本标识，标注必须归属于某一轮评估")
    # 一次性读入已有标注，后续修改只在内存里进行
    data = load(run_id, case_id)
    # 一次性取回本版本该样本的切片，逐条算状态时直接查表
    chunks = _chunk_index_of(run_id, case_id)
    # 全批共用一个时间戳，日后能看出这些条目出自同一次批量判定
    now = datetime.now().isoformat(timespec="seconds")
    # 实际写入条数
    written = 0
    # 因已有标注而跳过的条数
    skipped = 0
    # 逐条处理传入的切片
    for item in items:
        # 结构不对的条目直接忽略，不让一条脏数据毁掉整批
        if not isinstance(item, dict) or "chunk_index" not in item:
            continue
        # 切片序号必须能转成整数，否则忽略该条
        try:
            idx = int(item["chunk_index"])
        except (TypeError, ValueError):
            continue
        # JSON 的键必须是字符串
        key = str(idx)
        # 已标注过的保留原判断
        if skip_annotated and key in data:
            skipped += 1
            continue
        # 组装并写入本条标注
        data[key] = {
            "chunk_index": idx,  # 切片序号
            "verdict": verdict,  # 整批统一的人工判定
            # 检测器取调用方传入的当前筛选类型，而非切片命中的第一条规则：
            # 一个切片可能同时命中多条规则，按命中顺序归类会把判定算到别的规则头上
            "detector": detector,
            "note": note,  # 整批共用的备注
            # 正文摘要，供跨版本继承定位；同样剥离面包屑，避免标题层级一改就全部失配
            "excerpt": ((chunk_body(chunks[idx]) if idx in chunks
                         else excerpt_body(item.get("excerpt"))) or "")[:60],
            # 按本版本该切片的命中情况算出成立状态
            "status": status_of(verdict, hit_in_chunk(chunks.get(idx), detector)),
            "origin": "self",  # 本版本直接标的
            "at": now,  # 标注时间
            "batch": True,  # 标记来自批量操作，便于与逐条细看后的判定区分
        }
        # 累加写入计数
        written += 1
    # 一条都没写入时不必碰文件
    if written:
        # 整批改完后一次性写回
        _write_json(_path(run_id, case_id), data)
    # 返回写入与跳过的数量，供界面回报结果
    return {"written": written, "skipped": skipped}


def delete_one(run_id, case_id, chunk_index):
    """删除某版本下的一条标注。"""
    # 读取已有标注
    data = load(run_id, case_id)
    # 存在才删除
    if str(chunk_index) in data:
        del data[str(chunk_index)]
        # 写回
        _write_json(_path(run_id, case_id), data)
    # 返回是否发生了删除
    return True


def delete_many(run_id, case_id, chunk_indexes):
    """在某版本下批量删除标注，返回实际删除的条数。

    批量误标一整类之后，逐条撤销同样是上百次点击，因此撤销也要能整批做。
    """
    # 一次性读入已有标注
    data = load(run_id, case_id)
    # 实际删除条数
    removed = 0
    # 逐个序号尝试删除
    for raw in chunk_indexes:
        # 序号必须能转成整数，否则忽略该条
        try:
            key = str(int(raw))
        except (TypeError, ValueError):
            continue
        # 存在才删除，并累加计数
        if key in data:
            del data[key]
            removed += 1
    # 一条都没删掉时不必碰文件
    if removed:
        # 整批删完后一次性写回
        _write_json(_path(run_id, case_id), data)
    # 返回删除数量，供界面回报结果
    return removed


def save_region(run_id, case_id, region, detector="", note=""):
    """在指定版本下新增一条区域标注，返回写入的条目。

    region 形如 [页码, x0, x1, top, bottom]，单位与切片坐标一致（PDF 点），
    这样人工圈出的范围能直接和切分器给出的位置作比较。

    区域标注无法像切片标注那样自动核对——切分器压根没在那儿切出块，也就没有
    「该规则是否仍命中」可判——所以它只说明「这是在哪个版本下发现的问题」，
    是否已解决由人自己看。归属版本由所在目录表达，不再单独记代码指纹。
    """
    # 版本必须明确
    if not run_id:
        raise ValueError("缺少版本标识，标注必须归属于某一轮评估")
    # 坐标必须完整，否则日后无法还原到版面上
    if not isinstance(region, (list, tuple)) or len(region) < 5:
        raise ValueError("区域坐标应形如 [页码, x0, x1, top, bottom]")
    # 逐个转成整数，拒绝非数值
    try:
        pn, x0, x1, top, bottom = (int(v) for v in region[:5])
    except (TypeError, ValueError):
        raise ValueError("区域坐标必须是数值")
    # 零面积区域标了也没有意义
    if x1 <= x0 or bottom <= top:
        raise ValueError("区域范围为空")
    # 读取已有区域
    items = load_regions(run_id, case_id)
    # 组装条目
    item = {
        "id": f"r{len(items) + 1}_{int(datetime.now().timestamp())}",  # 稳定标识，供删除时定位
        "region": [pn, x0, x1, top, bottom],  # 区域坐标
        "detector": detector,  # 人工判定的问题类型
        "note": note,  # 备注，说明为什么圈这里
        "origin": "self",  # 本版本直接圈的，区别于从上一版继承来的
        "at": datetime.now().isoformat(timespec="seconds"),  # 标注时间
    }
    # 追加并写回
    items.append(item)
    _write_json(_region_path(run_id, case_id), items)
    # 返回写入的条目
    return item


def delete_region(run_id, case_id, region_id):
    """按标识删除某版本下的一条区域标注，返回是否删掉了东西。"""
    # 读取已有区域
    items = load_regions(run_id, case_id)
    # 过滤掉目标
    kept = [x for x in items if x.get("id") != region_id]
    # 数量没变说明没找到
    if len(kept) == len(items):
        return False
    # 写回
    _write_json(_region_path(run_id, case_id), kept)
    # 确实删掉了
    return True


def all_regions(run_id):
    """汇总某版本下全部样本在原文上圈出的异常区域。

    区域标注按样本分文件存，但看的时候需要横着看：哪些文档被圈得最多、
    人反复圈出的是同一类什么问题——这些都要跨样本才看得出来。
    """
    # 版本目录还不存在时直接返回空
    d = _run_dir(run_id)
    if not d.is_dir():
        return []
    # 逐个区域文件收集
    out = []
    for path in sorted(d.glob("*.regions.json")):
        # 文件名去掉 .regions 后缀即样本标识
        cid = path.name[:-len(".regions.json")]
        # 带上样本标识，界面才知道该跳回哪个文档
        for item in load_regions(run_id, cid):
            out.append({**item, "case_id": cid})
    # 按标注时间倒序，最近圈的排在前面
    return sorted(out, key=lambda x: x.get("at", ""), reverse=True)


def inherit(from_run, to_run):
    """把上一版本的标注继承到新版本，并按新版本的快照重新判定。

    没有继承的话，每跑一轮评估几百条标注就要重标一遍，准确率曲线也会断档；
    但直接共用一份标注又说不清「这条误报到底改好了没有」。继承正是这两者的
    交点：判定还是人那一次的判定，成立与否按新版本重算。

    逐条做三件事：按正文摘要在新快照里找回对应切片、把序号与摘要更新成新版本的、
    按新版本是否仍命中重算 status。找不回的保留原样并记为失配，交给人去重做。

    新版本已有的标注一律不动——人在新版本上亲手标过的，比继承来的更可信。
    """
    # 两端都必须明确，且不能是同一版
    if not from_run or not to_run or from_run == to_run:
        return {"cases": 0, "marks": 0, "regions": 0}
    # 累计继承的样本数、切片标注数与区域数，供调用方回报
    result = {"cases": 0, "marks": 0, "regions": 0}
    # 逐个有标注的样本继承
    for case_id in annotated_cases(from_run):
        # 上一版的全部标注
        old = load(from_run, case_id)
        # 一条都没有时跳过
        if not old:
            continue
        # 新版本已有的标注，继承时不覆盖它们
        new = load(to_run, case_id)
        # 新版本该样本的切片；为 None 说明这一轮压根没评估它（多半是在语料库里被停用了）
        chunks = runs.load_chunks(to_run, case_id)
        # 样本不在本轮快照里：判定原样带过去并标记未评估，不做任何定位。
        # 若按普通流程走，locate 必然全部失败、整份记成「失配」——那是「标注对不上切片」的
        # 意思，会把「这轮没跑它」误报成数据问题，重新启用时也无从分辨哪些该重做
        # 本样本实际继承的条数
        moved = 0
        if chunks is None:
            # 该样本未参与本轮：判定原样搬过去，只改状态与来源标记，不做任何定位
            for key, mark in old.items():
                # 人在新版本上亲手标过的优先
                if key in new:
                    continue
                new[key] = {**mark, "status": "skipped", "origin": f"inherit:{from_run}"}
                moved += 1
        else:
            # 序号索引，逐条对齐时复用，避免每条都重建一遍
            by_index = {c.get("index"): c for c in chunks}
            # 已被认领的切片序号，避免多条标注挤到同一个切片上
            used = set()
            # 按标注时的序号排序继承，让相邻标注按原有先后认领切片，
            # 否则字典序会让 #10 排在 #9 前面，认领顺序与原文顺序不符
            for mark in sorted(old.values(), key=lambda m: m.get("chunk_index") or 0):
                # 人工判定与相关检测器
                verdict = mark.get("verdict")
                det = mark.get("detector") or ""
                # 在新版本里找回那个切片：先按序号对齐，切分变了才退回摘要匹配
                found = locate(chunks, mark, used, by_index)
                # 找不回来说明切分变化太大，原样留着并记为失配，由人决定是否重做
                if found is None:
                    # 新版本已有同序号标注时不覆盖
                    key = str(mark.get("chunk_index"))
                    if key in new:
                        continue
                    new[key] = {**mark, "status": "stale",
                                "origin": f"inherit:{from_run}"}
                    moved += 1
                    continue
                # 认领这个切片，后面的标注不会再落到它上面
                used.add(found.get("index"))
                # 新版本里的序号，与标注时的可能不同
                key = str(found.get("index"))
                # 人在新版本上亲手标过的优先，不被继承值覆盖
                if key in new:
                    continue
                # 组装继承后的条目：判定沿用，定位与状态按新版本重算
                new[key] = {
                    **mark,
                    "chunk_index": found.get("index"),  # 更新为新版本的序号
                    # 摘要也换成新版本的正文，否则再往下一版继承时会越飘越远
                    "excerpt": (chunk_body(found) or "")[:60],
                    # 按新版本是否仍命中重算成立状态，这就是「改动有没有效果」的答案
                    "status": status_of(verdict, hit_in_chunk(found, det)),
                    "origin": f"inherit:{from_run}",  # 标明来自继承，未经人在本版本复核
                    "inherited_index": mark.get("chunk_index"),  # 上一版的序号，便于回溯
                }
                moved += 1
        # 有变化才写文件
        if moved:
            _write_json(_path(to_run, case_id), new)
            result["cases"] += 1
            result["marks"] += moved
        # 区域标注无法自动核对，原样继承一份，只标明来自上一版
        old_regions = load_regions(from_run, case_id)
        # 上一版没圈过就不必处理
        if not old_regions:
            continue
        # 新版本已有的区域
        new_regions = load_regions(to_run, case_id)
        # 已存在的标识集合，避免重复继承
        seen = {r.get("id") for r in new_regions}
        # 本样本实际继承的区域条数
        added = 0
        # 逐条追加尚未继承过的
        for r in old_regions:
            if r.get("id") in seen:
                continue
            new_regions.append({**r, "origin": f"inherit:{from_run}"})
            added += 1
        # 有变化才写文件
        if added:
            _write_json(_region_path(to_run, case_id), new_regions)
            result["regions"] += added
    # 返回继承概况
    return result


def migrate_flat(run_id=""):
    """把旧版平铺存放的标注收编进版本目录。

    旧设计把标注存成 annotations/<样本>.json，不带版本。升级到按版本保存后
    若直接忽略它们，几百条人工判定就等于凭空消失，因此一次性搬进目标版本，
    并按该版本的快照重算每条的成立状态。

    原文件移到 _migrated_backup/ 而不是删除：归属判断万一有误还能重来。
    仅在存在平铺文件时执行，重复调用是安全的。
    """
    # 目录还不存在说明从来没标过
    if not ANNOTATION_DIR.is_dir():
        return None
    # 平铺文件即直接位于 annotations/ 下的 .json，不含版本子目录
    flat = sorted(p for p in ANNOTATION_DIR.glob("*.json"))
    # 没有平铺文件说明已经迁移过
    if not flat:
        return None
    # 未指定目标版本时归到最新一轮
    target = run_id or runs.latest_run_id()
    # 一轮评估都没跑过时无处可归，保持原样等有版本了再迁
    if not target:
        return None
    # 迁移概况
    result = {"run_id": target, "cases": 0, "marks": 0, "regions": 0}
    # 逐个平铺文件处理
    for path in flat:
        # 区域标注文件单独处理
        if path.name.endswith(".regions.json"):
            # 文件名去掉后缀即样本标识
            cid = path.name[:-len(".regions.json")]
            # 读入原有区域
            items = _read_json(path, [])
            # 目标版本已有的区域，避免重复迁入
            exist = load_regions(target, cid)
            # 已有标识集合
            seen = {r.get("id") for r in exist}
            # 逐条追加，去掉旧的 code_hash 字段——归属版本已由目录表达
            for r in items:
                if r.get("id") in seen:
                    continue
                exist.append({k: v for k, v in r.items() if k != "code_hash"})
                result["regions"] += 1
            # 写入版本目录
            _write_json(_region_path(target, cid), exist)
            # 原文件移入备份目录
            MIGRATED_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(MIGRATED_DIR / path.name))
            continue
        # 切片标注：样本标识即文件名去掉扩展名
        cid = path.stem
        # 读入原有标注
        data = _read_json(path, {})
        # 目标版本已有的标注，不覆盖
        exist = load(target, cid)
        # 该版本该样本的切片，用于对齐并重算每条的成立状态
        chunks = runs.load_chunks(target, cid) or []
        # 序号索引，逐条对齐时复用
        by_index = {c.get("index"): c for c in chunks}
        # 已被认领的切片序号，避免多条标注挤到同一块上
        used = set()
        # 按序号排序迁入，让相邻标注按原有先后认领切片
        for mark in sorted(data.values(), key=lambda m: m.get("chunk_index") or 0):
            # 人工判定与相关检测器
            verdict = mark.get("verdict")
            det = mark.get("detector") or ""
            # 对齐到目标版本的切片：先按序号，序号对不上再按摘要找。
            # 只按序号查表是不够的——旧标注是在别的版本上标的，序号未必还指向
            # 同一段文字，不校验就会把判定算到不相干的块上
            found = locate(chunks, mark, used, by_index)
            # 找不回来说明那一版的切分与目标版本差别较大，记为失配交给人重做
            if found is None:
                # 保留原序号，人工重做时还能看出当初标的是第几片
                key = str(mark.get("chunk_index"))
                # 已存在的不覆盖
                if key in exist:
                    continue
                exist[key] = {**mark, "status": "stale", "origin": "migrated"}
                result["marks"] += 1
                continue
            # 认领这一块
            used.add(found.get("index"))
            # 目标版本里的序号
            key = str(found.get("index"))
            # 已存在的不覆盖
            if key in exist:
                continue
            # 写入：判定沿用，定位与状态按目标版本重算，并标明来自迁移
            exist[key] = {
                **mark,
                "chunk_index": found.get("index"),  # 目标版本的序号
                "excerpt": (chunk_body(found) or "")[:60],  # 目标版本的正文摘要
                "status": status_of(verdict, hit_in_chunk(found, det)),  # 本版本下的成立状态
                "origin": "migrated",  # 标明来自旧版平铺标注
            }
            result["marks"] += 1
        # 写入版本目录
        _write_json(_path(target, cid), exist)
        # 计入迁移的样本数
        result["cases"] += 1
        # 原文件移入备份目录
        MIGRATED_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(MIGRATED_DIR / path.name))
    # 返回迁移概况，供启动时打印
    return result
