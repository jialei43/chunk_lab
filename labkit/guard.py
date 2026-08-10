"""回归护栏与规则质量：把人工标注变成可执行的断言。

标注本身只是记录，改规则时帮不上忙。这里把它变成一组能跑的断言：

  - 标为**误报**的切片，改完规则后该检测器不应再命中它；
  - 标为**确认**的切片，改完规则后该检测器仍应命中它；
  - 标为**漏报**的切片，是当前规则还没覆盖的问题，命中了就是进步。

没有这层护栏，改检测规则等于盲改——修好一条误报的同时碰坏另一条，
要等下一轮评估才发现，甚至根本发现不了。

**一切都以版本为单位**。标注按版本独立保存（见 annotations 模块），每条标注
写入或继承时就已经按该版本的快照算好了成立状态，所以统计、明细、误报归纳、
任务书全部只需读那个版本的目录，不必再拿当前代码把所有已标注样本重切一遍。

护栏则是唯一需要跨版本的动作：拿**来源版本**的判定，去**目标版本**的切分结果上
逐条核对。跨版本定位一律以正文摘要为准（见 annotations.find_chunk），
切片序号会随切分逻辑变化而整体错位，不能用来跨版本对齐。
"""

from .evaluate import load_cases  # 导入样本加载，用于取样本文件名
from . import annotations, runs  # 导入标注读写与历史轮次快照

# 断言结果的取值与含义。
# 「待修正」与「回归」必须分开：标完误报、规则还没改的时候，第一次跑必然
# 是仍在误报——那是待办事项，不是改坏了东西。混为一谈的话护栏永远是红的，
# 真出现回归时反而看不见。
OUTCOMES = {
    "pass": "符合预期",  # 断言成立
    "regressed": "回归",  # 曾经能检出的现在检不出，改动碰坏了东西
    "open": "待修正",  # 标为误报但规则还在报，等着改
    "improved": "改进",  # 曾经漏的现在抓到了
    "still_missing": "仍未覆盖",  # 人工标了漏报，规则还是没抓到
    "stale": "标注已失配",  # 按摘要找不回那个切片，标注需要重做
    # 该样本在本版本被停用、没参与评估。与失配严格分开：失配要人去重做标注，
    # 本项什么都不用做，重新启用样本后判定自动恢复生效
    "skipped": "本版本未评估",
}

# 标注在某版本下的成立状态到护栏结论的映射。
# 标注写入时就按当时的快照算好了状态，同版本核对无需再算一遍
STATUS_TO_OUTCOME = {
    "confirmed": "pass",  # 仍能检出，符合预期
    "false_positive": "open",  # 仍在误报，待修正
    "missed": "still_missing",  # 仍未覆盖
    "fixed": "pass",  # 误报已消除，符合预期
    "regressed": "regressed",  # 曾能检出，现在检不出
    "covered": "improved",  # 曾漏报，现在能抓到
    "stale": "stale",  # 找不回切片
    "skipped": "skipped",  # 样本本轮被停用，未参与评估
}


def _case_names(only=None):
    """样本标识到原始文件名的映射。

    统计与报告里要显示「这条误报出自哪个文件」，而标注本身只记了样本标识；
    文件类型分布也要靠文件名的扩展名算出来。
    """
    # 语料元信息里带文件名
    return {c["case_id"]: c.get("filename", "") for c in load_cases(only=only)}


def _iter_marks(run_id, only=None):
    """遍历某版本下的全部切片标注，逐条带上所属样本标识。

    统计、明细、归纳、任务书都从这里取数——只读该版本的标注目录，
    不重新切分，因此都是毫秒级。
    """
    # 该版本下有标注的样本；指定 only 时收窄
    cases = annotations.annotated_cases(run_id)
    # 按需过滤样本
    if only:
        wanted = set(only)
        cases = [c for c in cases if c in wanted]
    # 逐样本逐条产出
    for cid in cases:
        for mark in annotations.load(run_id, cid).values():
            yield cid, mark


def stats(run_id, only=None):
    """某个版本的规则质量统计。

    直接数该版本目录里的标注：每条标注的 status 在写入或继承时就按该版本的
    快照算好了，所以「仍在误报」「已修复」「回归」都是现成的事实，不必重算。

    这也是与旧实现最大的差别——旧实现每次都要用当前代码把全部已标注样本
    重切一遍才敢给数字，一个页面要等上几分钟。
    """
    # 逐检测器累计各类状态
    by_detector = {}
    # 总计
    total = {k: 0 for k in annotations.STATUSES}
    # 实际有标注的样本
    cases = set()
    # 逐条归入
    for cid, mark in _iter_marks(run_id, only=only):
        # 记下样本
        cases.add(cid)
        # 问题类型；未指定时归为一类
        det = mark.get("detector") or "(未指定)"
        # 该规则的计数槽，键与 annotations.STATUSES 对齐
        slot = by_detector.setdefault(det, {k: 0 for k in annotations.STATUSES})
        # 取该条在本版本下的成立状态；早期数据缺字段时按失配处理
        key = mark.get("status") if mark.get("status") in total else "stale"
        # 累加
        slot[key] += 1
        total[key] += 1

    # 准确率：仍能检出的确认数，占「确认 + 仍在误报」之比。
    # 已修复的误报不再计入分母——那正是改动带来的成绩
    judged = total["confirmed"] + total["false_positive"]
    precision = round(total["confirmed"] / judged, 3) if judged else None
    # 召回率：抓到的占「人工认定的全部真问题」之比
    real = total["confirmed"] + total["missed"]
    recall = round(total["confirmed"] / real, 3) if real else None

    # 返回统计
    return {
        "run_id": run_id,  # 统计基于哪个版本
        "cases": len(cases),  # 有标注的样本数
        "total": total,  # 各类计数
        "by_detector": by_detector,  # 逐规则计数
        "precision": precision,  # 准确率
        "recall": recall,  # 召回率
        "ok": total["regressed"] == 0,  # 是否无回归
    }


def check(run_id, base_run=None, only=None):
    """拿来源版本的人工判定，去目标版本的切分结果上逐条核对。

    这是唯一需要跨版本的动作：改完规则跑一轮新评估，就用新版本核对上一版的
    判定，逐条回答「这条误报修好了没有」「有没有把本来能检出的碰坏」。

    base_run 缺省取目标版本的上一轮。两端相同（或没有上一轮）时不必重新定位——
    标注写入时已按本版本算好状态，直接映射成护栏结论即可。
    """
    # 目标版本必须明确
    if not run_id:
        return {"ok": True, "summary": {k: 0 for k in OUTCOMES}, "items": [],
                "checked": 0, "run_id": "", "base_run": "",
                "message": "没有可核对的版本"}
    # 来源版本缺省取上一轮
    base = base_run if base_run is not None else runs.previous_run_id(run_id)
    # 两端相同视为「只看本版本自己的成立情况」
    same = (not base) or base == run_id
    # 收集逐条结果
    items = []
    # 同版本核对：直接读本版本标注已经算好的状态
    if same:
        # 逐条映射
        for cid, mark in _iter_marks(run_id, only=only):
            # 状态到护栏结论的映射；缺状态的按失配处理
            outcome = STATUS_TO_OUTCOME.get(mark.get("status"), "stale")
            # 组装一条结果
            items.append({
                "case_id": cid,
                "chunk_index": mark.get("chunk_index"),  # 本版本的序号
                "marked_index": mark.get("inherited_index", mark.get("chunk_index")),
                "detector": mark.get("detector") or "",
                "verdict": mark.get("verdict"),
                "excerpt": mark.get("excerpt", ""),
                "note": mark.get("note", ""),
                "outcome": outcome,
                "message": _outcome_message(mark.get("verdict"), outcome),
            })
    else:
        # 跨版本核对：把来源版本的判定拿到目标版本的快照上重新定位
        for cid in annotations.annotated_cases(base):
            # 指定 only 时收窄
            if only and cid not in set(only):
                continue
            # 来源版本该样本的全部标注
            marks = annotations.load(base, cid)
            # 没标注就跳过
            if not marks:
                continue
            # 目标版本该样本的切片
            chunks = runs.load_chunks(run_id, cid)
            # 目标版本里没有这个样本：多半是它在语料库里被停用了，本轮没跑。
            # 逐条记为「本版本未评估」而不是笼统一条失配——失配要人去重做标注，
            # 这里什么都不用做；逐条产出也让护栏的条数与统计口径对得上
            if chunks is None:
                for mark in marks.values():
                    items.append({
                        "case_id": cid,
                        "chunk_index": mark.get("chunk_index"),
                        "marked_index": mark.get("chunk_index"),
                        "detector": mark.get("detector") or "",
                        "verdict": mark.get("verdict"),
                        "excerpt": mark.get("excerpt", ""),
                        "note": mark.get("note", ""),
                        "outcome": "skipped",
                        "message": _outcome_message(mark.get("verdict"), "skipped"),
                    })
                continue
            # 序号索引，逐条对齐时复用
            by_index = {c.get("index"): c for c in chunks}
            # 本样本已被认领的切片序号，防止多条标注挤到同一个切片上
            used = set()
            # 按标注时的序号排序核对，让相邻标注按原有先后认领切片
            for mark in sorted(marks.values(), key=lambda m: m.get("chunk_index") or 0):
                # 人工判定与相关检测器
                verdict = mark.get("verdict")
                det = mark.get("detector") or ""
                # 找回切片：先按序号对齐，切分变了才退回摘要匹配
                found = annotations.locate(chunks, mark, used, by_index)
                # 找不回来说明切分变化太大，标注需要重做
                if found is None:
                    items.append({
                        "case_id": cid, "chunk_index": None,
                        "marked_index": mark.get("chunk_index"),
                        "detector": det, "verdict": verdict, "outcome": "stale",
                        "excerpt": mark.get("excerpt", ""),
                        "message": "按正文摘要找不回该切片，切分结果变化较大，标注需重做",
                    })
                    continue
                # 认领这个切片
                used.add(found.get("index"))
                # 目标版本下该规则是否仍命中，据此推出成立状态
                status = annotations.status_of(
                    verdict, annotations.hit_in_chunk(found, det))
                # 状态映射成护栏结论
                outcome = STATUS_TO_OUTCOME.get(status, "stale")
                # 组装一条结果
                items.append({
                    "case_id": cid,
                    "chunk_index": found.get("index"),  # 目标版本的序号，与来源版本可能不同
                    "marked_index": mark.get("chunk_index"),  # 来源版本的序号，便于看出错位
                    "detector": det,
                    "verdict": verdict,
                    "excerpt": mark.get("excerpt", ""),
                    "note": mark.get("note", ""),
                    "outcome": outcome,
                    "message": _outcome_message(verdict, outcome),
                })

    # 按结论汇总
    summary = {k: 0 for k in OUTCOMES}
    for it in items:
        summary[it["outcome"]] = summary.get(it["outcome"], 0) + 1
    # 通过与否：只有回归算失败——那是改动碰坏了本来能检出的东西。
    # 待修正、仍未覆盖是还没做到的事，改进是好事，都不该拦住一次改动
    ok = summary.get("regressed", 0) == 0

    # 返回完整结果
    return {
        "ok": ok,  # 是否没有回归
        "summary": summary,  # 各类结论的条数
        "items": items,  # 逐条明细
        "checked": len({it["case_id"] for it in items}),  # 实际核对的样本数
        "run_id": run_id,  # 用哪个版本的切分结果核对
        "base_run": "" if same else base,  # 判定来自哪个版本，空表示看本版本自己
    }


def _outcome_message(verdict, outcome):
    """把护栏结论翻译成一句人话，界面与命令行直接展示。"""
    # 样本被停用时该轮压根没跑它，任何按判定去描述的话都不成立
    if outcome == "skipped":
        return "该样本在本版本被停用，未参与评估；重新启用后判定自动恢复生效"
    # 失配优先判断：这类条目压根没对上切片，谈不上「仍在误报」还是「已修复」，
    # 按人工判定去描述会给出一句与事实相反的话
    if outcome == "stale":
        return "按正文摘要找不回对应切片，标注需重做"
    # 误报类：不再命中即为修好了
    if verdict == "false_positive":
        return "误报已消除" if outcome == "pass" else "仍在误报，该规则尚待修正"
    # 确认类：检不出就是碰坏了
    if verdict == "confirmed":
        return "仍能检出" if outcome == "pass" else "曾经能检出，现在检不出了"
    # 漏报类：抓到就是进步
    if verdict == "missed":
        return "规则已能覆盖该问题" if outcome == "improved" else "规则仍未覆盖该问题"
    # 其余情况只可能是失配
    return "按正文摘要找不回该切片，标注需重做"


def analyze_false_positives(run_id, detector=None, only=None):
    """归纳某版本下仍在误报的共同特征，指出规则该往哪儿改。

    逐条看误报很难看出规律；把同一条规则的反例放在一起，命中的是什么、
    出现在什么格式的文档里，往往一眼就能看出来。

    只取该版本下**仍在误报**的（status 为 false_positive）：已经修好的列进来，
    就会出现「仍在误报 17 条、共性却写 24 条」这种自相矛盾的展示。
    """
    # 样本文件名映射，用于算文件类型分布
    names = _case_names()
    # 按检测器归集误报
    groups = {}
    # 逐条标注筛选
    for cid, mark in _iter_marks(run_id, only=only):
        # 只看该版本下仍在误报的
        if mark.get("status") != "false_positive":
            continue
        # 指定了检测器时只看这一条规则
        det = mark.get("detector") or "(未指定)"
        if detector and det != detector:
            continue
        # 收入该规则的反例。带上切片序号与文件名，
        # 界面才能从这里直接跳回原切片去看上下文
        groups.setdefault(det, []).append({
            "case_id": cid,
            "filename": names.get(cid, ""),
            "chunk_index": mark.get("chunk_index"),
            "excerpt": mark.get("excerpt", ""),
            "note": mark.get("note", ""),
            "batch": bool(mark.get("batch")),  # 批量判定的可信度低于逐条细看
            "origin": mark.get("origin", ""),  # 继承来的未经本版本复核
        })

    # 逐条规则归纳
    out = []
    for det, cases in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        # 反例涉及的文件类型：某类格式集中出问题是很强的线索
        exts = {}
        for c in cases:
            # 取扩展名，没有则记为未知
            ext = (c["filename"].rsplit(".", 1)[-1].lower() if "." in c["filename"] else "未知")
            exts[ext] = exts.get(ext, 0) + 1
        # 人工备注里往往直接写了原因，原样带出来最有价值
        notes = [c["note"] for c in cases if c.get("note")]
        # 组装一条归纳
        out.append({
            "detector": det,  # 规则名
            "count": len(cases),  # 反例条数
            "by_ext": exts,  # 按文件类型分布
            "notes": notes,  # 人工写下的原因
            # 全部反例。刻意不在这里截断——任务书要逐条列出，
            # 截断会让它悄悄漏掉待办；界面若嫌长，自己取前几条即可
            "samples": cases,
        })
    # 反例多的排在前面，那是最该改的规则
    return out


def list_marks(status, run_id, detector=None, only=None):
    """按成立状态列出某版本的标注明细，供界面直接跳回原切片。

    统计只给出数量，看不到具体是哪些切片。要动手改规则就得能从数字点进去，
    落到那一块正文上——这里提供的就是这条通路。

    按 status 而不是 verdict 过滤：点开「仍在误报 17」应当正好看到 17 条，
    掺进已经修好的就与上面的数字对不上了。
    """
    # 状态必须是已知取值
    if status not in annotations.STATUSES:
        raise ValueError(f"未知的成立状态：{status}")
    # 样本文件名映射
    names = _case_names()
    # 收集明细
    out = []
    # 逐条筛选
    for cid, mark in _iter_marks(run_id, only=only):
        # 只看指定状态
        if mark.get("status") != status:
            continue
        # 指定了问题类型时只看这一类
        det = mark.get("detector") or "(未指定)"
        if detector and det != detector:
            continue
        # 带上定位所需的全部信息
        out.append({
            "case_id": cid,
            "filename": names.get(cid, ""),
            "chunk_index": mark.get("chunk_index"),
            "detector": det,
            "verdict": mark.get("verdict"),  # 人工判定
            "status": mark.get("status"),  # 本版本下的成立状态
            "excerpt": mark.get("excerpt", ""),
            "note": mark.get("note", ""),
            "batch": bool(mark.get("batch")),
            "origin": mark.get("origin", ""),  # 本版本自标还是继承来的
            "at": mark.get("at", ""),
        })
    # 按样本与序号排序，同一文档的条目挨在一起便于连着看
    return sorted(out, key=lambda x: (x["case_id"], x.get("chunk_index") or 0))


def _rule_source(detector):
    """取某条检测规则的源码与所在位置。

    报告里带上当前实现，看报告的人（或模型）不必再去翻代码就能动手改；
    也避免报告与实现脱节——源码是从运行中的模块直接取的，不会写错。
    """
    # 延迟导入，避免与模块级导入形成循环
    import inspect
    from . import detectors
    # 检测器函数按 detect_<名字> 命名
    fn = getattr(detectors, f"detect_{detector}", None)
    # 找不到说明这不是已实现的规则（多半是人工提出的需求）
    if fn is None:
        return None
    # 源码与行号一并取出，异常时不让报告生成失败
    try:
        src = inspect.getsource(fn)
        _, line = inspect.getsourcelines(fn)
    except (OSError, TypeError):
        return None
    # 规则常依赖模块级的正则常量，一并带出来——
    # 只给函数体的话，看到 PATTERN.search() 仍然不知道匹配的是什么
    consts = []
    for name in dir(detectors):
        # 只收大写命名的模块级常量，且名字与该规则相关
        if not name.isupper():
            continue
        # 名字里含规则名的片段即认为相关，例如 MARKDOWN_RESIDUE_PATTERN
        if any(part and part.upper() in name for part in detector.split("_")):
            val = getattr(detectors, name)
            # 正则对象打印出模式本身，其它类型直接转字符串
            consts.append(f"{name} = {getattr(val, 'pattern', val)!r}")
    # 返回位置、源码与相关常量
    return {"file": "labkit/detectors.py", "line": line, "source": src, "consts": consts}


def _collect_chunk_texts(run_id, marks):
    """为一批标注取回它们在该版本下的切片正文与命中情况。

    标注里只有 60 字摘要，改代码时不够用——要判断一条规则该怎么写，
    得看到整块正文长什么样、以及该版本的规则在它上面匹配到了什么。

    标注按版本存之后，序号在本版本内就是准的，直接按序号取即可；
    只有失配的条目才需要退回摘要匹配。
    """
    # 按样本归集，一个样本只读一次快照
    by_case = {}
    for m in marks:
        by_case.setdefault(m["case_id"], []).append(m)
    # 逐样本取回正文
    out = {}
    for cid, items in by_case.items():
        # 该版本该样本的切片
        chunks = runs.load_chunks(run_id, cid)
        # 快照里没有这个样本时跳过，其余样本照常
        if not chunks:
            continue
        # 按序号建索引，直接查表
        by_index = {c.get("index"): c for c in chunks}
        # 已认领的切片，退回摘要匹配时避免多条落到同一块上
        used = set()
        # 逐条取回
        for m in items:
            # 先按序号取；这是本版本自己的序号，正常情况下必然命中
            found = by_index.get(m.get("chunk_index"))
            # 序号取不到（失配条目）时退回摘要匹配
            if found is None:
                found = annotations.find_chunk(chunks, m.get("excerpt"), used)
            # 仍找不回时留空，报告里会退回用摘要
            if found is None:
                continue
            # 认领
            used.add(found.get("index"))
            # 记下正文与该版本的全部命中，键用标注在本版本的序号
            out[(cid, m.get("chunk_index"))] = {
                "index": found.get("index"),
                "content": found.get("content", ""),
                "findings": found.get("findings") or [],
            }
    return out


def build_optimization_report(detector, run_id, only=None):
    """为某条规则生成优化报告。

    报告要能直接拿去改代码，因此同时给出三样东西：规则的当前实现、
    该版本下仍在误报的全部反例、以及这些反例在该版本里的真实命中证据。
    只有摘要而没有证据的话，改规则时仍然要一条条去翻原文。
    """
    # 该规则在该版本下的误报归纳
    groups = analyze_false_positives(run_id, detector=detector, only=only)
    # 没有反例时没什么可优化的
    if not groups:
        return {"ok": False,
                "message": f"版本 {run_id} 下规则 {detector} 没有仍在误报的切片，无从生成优化报告"}
    # 取这一条规则的归纳
    g = groups[0]

    # 取回反例在该版本下的正文与命中证据
    texts = _collect_chunk_texts(run_id, g["samples"])

    # 把证据挂回反例
    samples = []
    for s in g["samples"]:
        # 按样本与序号取回该条的正文记录
        rec = texts.get((s["case_id"], s.get("chunk_index")), {})
        # 取该规则在这块上的命中证据
        hits = [f for f in rec.get("findings", []) if f.get("detector") == detector]
        samples.append({
            **s,
            "index": rec.get("index", s.get("chunk_index")),  # 该版本的序号
            "content": rec.get("content", ""),  # 整块正文
            "evidence": hits[0].get("evidence", "") if hits else "",  # 命中的具体内容
            "still_hit": bool(hits),  # 该版本下是否仍在命中
        })

    # 规则的当前实现
    rule = _rule_source(detector)

    # 组装 Markdown 报告
    lines = [
        f"# 规则优化报告：{detector}",
        "",
        f"**基于版本**：{run_id}。以下反例都是在这一版下重新核对过、确认仍在误报的。",
        "",
        f"人工判定的误报 **{g['count']} 条**，文件类型分布 "
        f"{'、'.join(f'{k} {v}' for k, v in g['by_ext'].items())}。",
        "",
    ]
    # 人工写下的原因是最直接的线索，放在最前
    if g["notes"]:
        lines += ["## 人工判断的原因", ""]
        # 去重后列出，重复的备注说明是同一类问题
        seen = []
        for n in g["notes"]:
            if n not in seen:
                seen.append(n)
        lines += [f"- {n}" for n in seen] + [""]

    # 当前实现，供直接动手改
    if rule:
        lines += [
            "## 当前实现",
            "",
            f"`{rule['file']}` 第 {rule['line']} 行起：",
            "",
        ]
        # 相关常量单独列出，正则模式往往才是问题所在
        if rule["consts"]:
            lines += _fenced("\n".join(rule["consts"]), "python")
        lines += _fenced(rule["source"].rstrip(), "python")
    else:
        lines += [
            "## 当前实现",
            "",
            f"未找到 `detect_{detector}` 函数——这条多半是人工提出、尚未实现的规则需求。",
            "",
        ]

    # 逐条反例，带该版本的证据
    lines += ["## 误报明细", ""]
    for i, s in enumerate(samples, 1):
        # 标题行给出定位信息
        lines += [
            f"### {i}. {s['case_id']} #{s.get('index')}（{s.get('filename', '')}）",
            "",
        ]
        # 该版本是否仍在命中：改完规则后跑新一轮，这里应当变成「否」
        lines.append(f"- 该版本仍命中：{'是' if s.get('still_hit') else '否'}")
        # 命中的具体内容是改规则的关键——知道匹配到了什么才知道怎么排除
        if s.get("evidence"):
            lines.append(f"- 命中证据：`{s['evidence']}`")
        # 人工备注说明为什么判为误报
        if s.get("note"):
            lines.append(f"- 人工判断：{s['note']}")
        # 批量判定的可信度低于逐条细看，如实标注
        if s.get("batch"):
            lines.append("- 来自批量判定（未逐条细看）")
        lines += ["", "正文：", ""] + _fenced((s.get("content") or s["excerpt"])[:600])

    # 收尾给出验证方式，避免改完不知道怎么确认
    lines += [
        "## 改完怎么验证",
        "",
        "```bash",
        "./run.sh eval                # 跑一轮新评估，标注会自动继承并重新判定",
        "./run.sh guard --todo        # 用新版本核对上一版的判定，这些条目应从「待修正」变为「符合预期」",
        "```",
        "",
        "出现「回归」说明改动碰坏了本来能检出的东西；"
        "「待修正」条数减少而回归为 0，才算这次修改成立。",
        "",
    ]

    # 返回报告与结构化数据，界面可两种方式使用
    return {
        "ok": True,
        "detector": detector,
        "run_id": run_id,
        "markdown": "\n".join(lines),
        "count": g["count"],
        "samples": samples,
        "rule": rule,
    }


def _fenced(text, lang="text"):
    """把正文包成代码块，围栏长度按内容自动调整。

    切片正文本身可能含有 ```（Markdown 文档、代码示例都会），固定用三个反引号
    会被正文提前闭合，后面的内容全部串位。围栏取比正文中最长反引号序列多一个。
    """
    # 正文统一成字符串
    body = str(text or "")
    # 找出正文里最长的连续反引号
    longest = 0
    run = 0
    for ch in body:
        # 累计当前连续长度
        run = run + 1 if ch == "`" else 0
        # 记录最大值
        longest = max(longest, run)
    # 围栏至少三个，且必须比正文里最长的多一个
    fence = "`" * max(3, longest + 1)
    # 返回完整代码块的各行
    return [f"{fence}{lang}", body, fence, ""]


def build_full_report(run_id):
    """为某个版本生成一份完整的优化任务书，可直接交给模型动手改。

    与单条规则的报告的区别是「完整」：把该版本全部待办放在一起，并按能否
    自动验证排序——误报和已实现规则的问题有护栏兜底，改完立刻知道对不对；
    人工提出的需求和框选区域需要新写代码，风险更高，放在后面。

    **只列该版本下仍然成立的问题**。每条标注的成立状态在这一版里已经算过，
    修好的误报不会再出现——否则拿到任务书的人会去改一个已经不存在的问题，
    还会把「改完验证」的结论搅浑。

    刻意把代码位置、注册方式、验证命令都写进去：拿到报告的人（或模型）
    不该还要回头问「检测器写在哪」「改完怎么验证」。
    """
    # 该版本的统计，直接读标注目录
    st = stats(run_id)
    # 该版本下仍在误报的归纳，按剩余条数降序，最该改的排前面
    fp_groups = analyze_false_positives(run_id)

    # 人工提出的需求：判定为漏报、且该版本下仍未覆盖的
    from . import detectors as _det
    # 仍未覆盖的漏报明细
    missed = list_marks("missed", run_id)
    # 已实现的检测器名集合，用于区分「规则漏了」与「规则还不存在」
    implemented = {n[len("detect_"):] for n in dir(_det) if n.startswith("detect_")}
    # 拆成两类：已有规则漏报（调阈值即可）与全新需求（要写代码）
    missed_known = [m for m in missed if m["detector"] in implemented]
    missed_new = [m for m in missed if m["detector"] not in implemented]
    # 该版本下的框选区域
    regions = annotations.all_regions(run_id)
    # 该版本下按摘要找不回切片的标注，需要人工重做，单列出来提醒
    stale = list_marks("stale", run_id)

    # 一次性取回所有相关切片的正文，避免每块各读一遍快照
    texts = _collect_chunk_texts(
        run_id, [s for g in fp_groups for s in g["samples"]] + missed)

    # 百分比展示
    def pct(v):
        return "—" if v is None else f"{round(v * 100)}%"

    # 取某条标注对应的正文，找不回时退回摘要
    def body_of(m):
        rec = texts.get((m["case_id"], m.get("chunk_index")))
        return (rec or {}).get("content") or m.get("excerpt", "")

    # 取某条标注上某规则在该版本的命中证据
    def evidence_of(m, det):
        rec = texts.get((m["case_id"], m.get("chunk_index")))
        hits = [f for f in (rec or {}).get("findings", []) if f.get("detector") == det]
        return hits[0].get("evidence", "") if hits else ""

    lines = [
        "# chunk-lab 切分检测规则优化任务",
        "",
        "本文件由 `规则质量` 页自动生成，内容全部来自人工标注——每一条都是人对着",
        "真实文档判定的结果。请按下面的任务清单修改检测规则。",
        "",
        f"**基于版本**：{run_id}。",
        "标注按版本独立保存，下面每一条都是在这一版的切分结果上核对过、",
        "确认仍然存在的问题；已经修好的不会出现在这里。",
        "",
        "## 现状",
        "",
        f"- 检测器准确率 **{pct(st.get('precision'))}**"
        f"（报出来的有多少经人工确认是真问题）",
        f"- 召回率 **{pct(st.get('recall'))}**（人工发现的问题有多少被规则抓到）",
        f"- 该版本 {st.get('cases', 0)} 个样本有标注：仍能检出 {st['total']['confirmed']} 条、"
        f"仍在误报 {st['total']['false_positive']} 条、仍未覆盖 {st['total']['missed']} 条",
        f"- 已修复 {st['total']['fixed']} 条误报，已覆盖 {st['total']['covered']} 条漏报"
        + (f"，**出现 {st['total']['regressed']} 条回归**" if st['total']['regressed'] else ""),
        f"- 人工在原文上圈出 {len(regions)} 块切分器没切出来的区域",
    ] + ([
        f"- 另有 {st['total']['skipped']} 条标注所属样本在本版本被停用、未参与评估，"
        f"已原样保留但不计入上面任何数字",
    ] if st["total"].get("skipped") else []) + [
        "",
        "## 代码位置与约定",
        "",
        "- 检测规则全部在 `labkit/detectors.py`，每条是一个 `detect_<名字>` 函数，",
        "  返回 `Finding` 列表，并在文件末尾的 `ALL_DETECTORS` 登记表中注册。",
        "- `Finding` 字段：`detector`（规则名）、`severity`（high/medium/low）、",
        "  `case_id`、`chunk_index`、`message`（一句话描述）、`evidence`（证据片段）。",
        "- 阈值集中在 `DetectorConfig`，不要在函数里写死数字。",
        "- 判定正文时先调 `strip_breadcrumb(record)` 剥掉面包屑，",
        "  否则标题文字会混进正文判据。",
        "",
        "## 改完怎么验证",
        "",
        "```bash",
        "./run.sh eval          # 跑一轮新评估：产生新版本，标注自动继承并按新结果重判",
        "./run.sh guard --todo  # 用新版本核对上一版的判定",
        "```",
        "",
        "护栏拿上一版的人工判定，在新版本的切分结果上逐条核对：",
        "",
        "- **待修正 → 符合预期**：这次修改生效了；",
        "- **出现回归**：改动碰坏了本来能检出的东西，必须修好才算完成；",
        "- 仍未覆盖、标注已失配不算失败。",
        "",
        "**只有回归为 0 才算改动成立。**",
        "",
    ]

    # ---------- 任务一：误报 ----------
    if fp_groups:
        lines += [
            "## 任务一：修正误报（有护栏兜底，改完可自动验证）",
            "",
            f"共 {len(fp_groups)} 条规则存在误报。按误报条数降序，先改影响最大的。",
            "",
        ]
        for gi, g in enumerate(fp_groups, 1):
            det = g["detector"]
            # 该规则的准确率单独算出来，说明问题有多严重
            s = (st.get("by_detector") or {}).get(det, {})
            judged = s.get("confirmed", 0) + s.get("false_positive", 0)
            p = pct(s.get("confirmed", 0) / judged) if judged else "—"
            lines += [
                f"### {gi}. `{det}`",
                "",
                f"误报 {g['count']} 条，准确率 {p}，"
                f"文件类型分布 {'、'.join(f'{k} {v}' for k, v in g['by_ext'].items())}。",
                "",
            ]
            # 人工写下的原因去重后列出，往往直接说明了该怎么改
            if g["notes"]:
                seen = []
                for n in g["notes"]:
                    if n not in seen:
                        seen.append(n)
                lines += ["人工判断的原因：", ""] + [f"- {n}" for n in seen] + [""]
            # 当前实现
            rule = _rule_source(det)
            if rule:
                lines += [f"当前实现（`{rule['file']}` 第 {rule['line']} 行）：", ""]
                if rule["consts"]:
                    lines += _fenced("\n".join(rule["consts"]), "python")
                lines += _fenced(rule["source"].rstrip(), "python")
            # 反例明细
            lines += ["误报的切片：", ""]
            for si, sample in enumerate(g["samples"], 1):
                ev = evidence_of(sample, det)
                rec = texts.get((sample["case_id"], sample.get("chunk_index")), {})
                lines += [
                    f"**{si}) {sample['case_id']} #{rec.get('index', sample.get('chunk_index'))}"
                    f"（{sample.get('filename', '')}）**",
                    "",
                ]
                # 命中了什么是改规则的关键
                if ev:
                    lines.append(f"- 当前命中：`{ev}`")
                if sample.get("note"):
                    lines.append(f"- 人工判断：{sample['note']}")
                if sample.get("batch"):
                    lines.append("- 来自批量判定（未逐条细看，可信度低于逐条判定）")
                lines += [""] + _fenced(body_of(sample)[:500])

    # ---------- 任务二：已有规则漏报 ----------
    if missed_known:
        # 按规则归集
        by_det = {}
        for m in missed_known:
            by_det.setdefault(m["detector"], []).append(m)
        lines += [
            "## 任务二：已有规则漏报（调判据或阈值）",
            "",
            "这些切片人工认为有问题，规则已存在但没报出来。",
            "",
        ]
        for det, ms in sorted(by_det.items(), key=lambda kv: -len(kv[1])):
            lines += [f"### `{det}`（漏报 {len(ms)} 条）", ""]
            rule = _rule_source(det)
            if rule:
                lines += [
                    f"当前实现（`{rule['file']}` 第 {rule['line']} 行）：", "",
                ] + _fenced(rule["source"].rstrip(), "python")
            for m in ms:
                rec = texts.get((m["case_id"], m.get("chunk_index")), {})
                lines += [f"**{m['case_id']} #{rec.get('index', m.get('chunk_index'))}**", ""]
                if m.get("note"):
                    lines.append(f"- 人工说明：{m['note']}")
                lines += [""] + _fenced(body_of(m)[:500])

    # ---------- 任务三：全新需求 ----------
    if missed_new:
        # 按需求描述归集：同一句话被反复提出说明是共性问题
        by_want = {}
        for m in missed_new:
            by_want.setdefault(m["detector"], []).append(m)
        lines += [
            "## 任务三：人工提出的新规则需求（需要新写代码，无护栏兜底）",
            "",
            "标注时填的问题类型不在现有规则里，说明这是规则还没覆盖的情况。",
            "描述是人工手写的，可能是一句要求而非规则名——请先判断它到底要解决什么，",
            "再决定是新增检测规则、还是该改切分逻辑（后者会影响生产切分结果，",
            "属于另一个层面的改动，不要顺手就改）。",
            "",
        ]
        for det, ms in sorted(by_want.items(), key=lambda kv: -len(kv[1])):
            lines += [f"### 「{det}」（提出 {len(ms)} 次）", ""]
            for m in ms:
                rec = texts.get((m["case_id"], m.get("chunk_index")), {})
                lines += [
                    f"**{m['case_id']} #{rec.get('index', m.get('chunk_index'))}"
                    f"（{m.get('filename', '')}）**", "",
                ]
                if m.get("note"):
                    lines.append(f"- 人工说明：{m['note']}")
                # 当前这块上已有的命中，说明别的规则是怎么看它的
                other = [f"{f['detector']}" for f in rec.get("findings", [])]
                if other:
                    lines.append(f"- 当前已有规则命中：{'、'.join(sorted(set(other)))}")
                lines += [""] + _fenced(body_of(m)[:500])

    # ---------- 任务四：框选区域 ----------
    if regions:
        lines += [
            "## 任务四：切分器漏切的版面区域（仅供参考，不必直接改规则）",
            "",
            "人在原文上直接圈出来的地方——切分器压根没在那儿切出块，",
            "所以它不属于任何切片，也无法用现有的切片级规则表达。",
            "这类问题多半出在解析或切分阶段，而不是检测规则；",
            "列在这里是为了让你知道还有哪些问题没被规则覆盖到。",
            "",
            "| 样本 | 页码 | 人工判定 | 备注 |",
            "|---|---|---|---|",
        ]
        for r in regions:
            reg = r.get("region") or []
            lines.append(
                f"| {r.get('case_id', '')} | 第 {reg[0] if reg else '?'} 页 | "
                f"{r.get('detector') or '未指定'} | {r.get('note') or ''} |"
            )
        lines.append("")

    # ---------- 附：需要重做的标注 ----------
    if stale:
        lines += [
            "## 附：需要重做的标注（不是待办，是数据问题）",
            "",
            f"有 {len(stale)} 条标注从上一版继承过来时，按正文摘要在这一版里找不回对应切片，",
            "多半是切分边界变化较大所致。它们不计入上面的任何统计，也不必据此改规则，",
            "但下次标注时最好回到「切片预览」里重新判定一遍。",
            "",
        ]
        # 只列样本与摘要，够定位即可
        for m in stale[:50]:
            lines.append(f"- {m['case_id']} #{m.get('chunk_index')}　"
                         f"{(m.get('excerpt') or '')[:40]}")
        # 太多时不全列，避免任务书被这类条目淹没
        if len(stale) > 50:
            lines.append(f"- …另有 {len(stale) - 50} 条")
        lines.append("")

    # 没有任何待办时明确说明，避免拿到一份空文件不知道是不是出错了
    if not (fp_groups or missed_known or missed_new or regions):
        lines += [
            "## 暂无待办",
            "",
            f"版本 {run_id} 下还没有任何人工标注，或标注的问题都已修好。",
            "请到「切片预览」里判定检测结果，或在原文视图上框出规则没覆盖到的区域。",
            "",
        ]

    # 返回报告与关键计数，界面据此展示概要
    return {
        "ok": True,
        "run_id": run_id,
        "markdown": "\n".join(lines),
        "counts": {
            "false_positive_rules": len(fp_groups),  # 有误报的规则数
            "false_positive": sum(g["count"] for g in fp_groups),  # 误报总条数
            "missed_known": len(missed_known),  # 已有规则的漏报
            "missed_new": len(missed_new),  # 全新需求
            "regions": len(regions),  # 框选区域
            "stale": len(stale),  # 需要重做的标注
        },
    }
