"""回归护栏：把人工标注变成可执行的断言。

标注本身只是记录，改规则时帮不上忙。这里把它变成一组能跑的断言：

  - 标为**误报**的切片，改完规则后该检测器不应再命中它；
  - 标为**确认**的切片，改完规则后该检测器仍应命中它；
  - 标为**漏报**的切片，是当前规则还没覆盖的问题，命中了就是进步。

没有这层护栏，改检测规则等于盲改——修好一条误报的同时碰坏另一条，
要等下一轮评估才发现，甚至根本发现不了。

**为什么不能用切片序号定位**：切分参数一改，序号就整体错位，
第 10 条标注会落到完全不相干的第 10 个切片上。所以标注写入时同时记下了
正文摘要，这里以摘要为准重新定位——切分逻辑改了以后，能不能找回那个切片
本身也是要检查的事实。
"""

from .detectors import DetectorConfig  # 导入检测阈值配置
from .evaluate import inspect_case, load_cases  # 导入样本加载与单样本切分
from . import annotations  # 导入标注读写

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


def _find_chunk(chunks, excerpt, used=None):
    """按正文摘要在新的切分结果里找回那个切片。

    used 传入已被其它标注认领的切片序号。相邻切片的开头常常一模一样
    （同一标题下的连续段落，面包屑前缀相同），只按前缀匹配会让好几条标注
    全落到同一个切片上，后面几条的核对结果就都是假的。

    返回命中的切片；找不到时返回 None，由调用方记为标注失配。
    """
    # 已被认领的切片，避免多条标注挤到同一个上
    used = used if used is not None else set()
    # 规范化后的完整摘要
    full = _normalize(excerpt)
    # 没有摘要就无从定位——早期标注可能缺这个字段
    if not full:
        return None
    # 摘要前缀，用于降级匹配
    key = full[:MATCH_PREFIX]

    # 三轮匹配，一轮比一轮宽松，每轮都先跳过已被认领的切片：
    # 完整摘要最可靠，能区分开头相同的相邻块
    for c in chunks:
        if c.get("index") in used:
            continue
        if _normalize(c.get("content")).startswith(full):
            return c
    # 其次是前缀一致：切分逻辑改动常影响块的结尾，开头相对稳定
    for c in chunks:
        if c.get("index") in used:
            continue
        if _normalize(c.get("content"))[:MATCH_PREFIX] == key:
            return c
    # 最后找包含关系：切分器可能把该块并入了更大的块，
    # 这仍算找到了那段文字，只是归属变了
    for c in chunks:
        if c.get("index") in used:
            continue
        if key in _normalize(c.get("content")):
            return c
    # 确实找不回来
    return None


def _hit(chunk, detector):
    """判断某检测器是否命中了这个切片。"""
    # 遍历该切片上挂着的全部命中
    return any(f.get("detector") == detector for f in (chunk.get("findings") or []))


def check(only=None, cfg=None, parser_config=None):
    """按当前代码重跑一遍，逐条核对人工标注是否仍然成立。

    only 给定时只检查指定样本，便于针对一个文档快速迭代。
    """
    # 检测阈值，未指定时用默认
    cfg = cfg or DetectorConfig()
    # 收集逐条结果
    items = []
    # 只处理有标注的样本：没标过的样本没有断言可查
    for case in load_cases(only=only):
        # 样本标识
        cid = case["case_id"]
        # 该样本的全部标注
        marks = annotations.load(cid)
        # 没标注就跳过，避免白跑一次切分
        if not marks:
            continue
        # 用当前代码重新切分并跑检测器
        data = inspect_case(case, cfg=cfg, parser_config=parser_config)
        # 切分失败时整份样本记为无法核对，而不是静默跳过
        if data.get("error"):
            items.append({
                "case_id": cid, "chunk_index": None, "detector": "",
                "verdict": "", "outcome": "stale",
                "message": f"切分失败，无法核对：{data['error']}",
            })
            continue
        # 新的切片列表
        chunks = data.get("chunks") or []
        # 本样本已被认领的切片序号，防止多条标注挤到同一个切片上
        used = set()
        # 按标注时的序号排序核对，让相邻标注按原有先后认领切片，
        # 否则字典序会让 #10 排在 #9 前面，认领顺序与原文顺序不符
        for mark in sorted(marks.values(), key=lambda m: m.get("chunk_index") or 0):
            # 人工结论与相关检测器
            verdict = mark.get("verdict")
            det = mark.get("detector") or ""
            # 按摘要找回切片，跳过已被认领的
            found = _find_chunk(chunks, mark.get("excerpt"), used)
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
            # 该检测器现在是否命中
            hit = _hit(found, det) if det else bool(found.get("findings"))
            # 组装一条结果
            row = {
                "case_id": cid,
                "chunk_index": found.get("index"),  # 新的序号，与标注时的可能不同
                "marked_index": mark.get("chunk_index"),  # 标注时的序号，便于看出错位
                "detector": det,
                "verdict": verdict,
                "excerpt": mark.get("excerpt", ""),
                "note": mark.get("note", ""),
            }
            # 逐类判定
            if verdict == "false_positive":
                # 误报应当不再命中。仍命中记为「待修正」而不是回归——
                # 规则还没改的时候本来就该仍在报，那是待办不是事故
                row["outcome"] = "pass" if not hit else "open"
                row["message"] = ("误报已消除" if not hit
                                  else "仍在误报，该规则尚待修正")
            elif verdict == "confirmed":
                # 确认的问题应当仍能检出；检不出说明改动碰坏了这条规则
                row["outcome"] = "pass" if hit else "regressed"
                row["message"] = ("仍能检出" if hit
                                  else "曾经能检出，现在检不出了")
            elif verdict == "missed":
                # 漏报是人工发现、规则没抓到的；现在抓到就是进步
                row["outcome"] = "improved" if hit else "still_missing"
                row["message"] = ("规则已能覆盖该问题" if hit
                                  else "规则仍未覆盖该问题")
            else:
                # 未知结论不做判定，如实标记
                row["outcome"] = "stale"
                row["message"] = f"未知的标注结论：{verdict}"
            # 收入结果
            items.append(row)

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
    }


def analyze_false_positives(detector=None, only=None):
    """归纳误报的共同特征，指出规则该往哪儿改。

    逐条看误报很难看出规律；把同一条规则的反例放在一起，
    命中的是什么子串、出现在什么上下文，往往一眼就能看出来。
    """
    # 按检测器归集误报
    groups = {}
    # 遍历有标注的样本
    for case in load_cases(only=only):
        # 样本标识
        cid = case["case_id"]
        # 逐条标注
        for mark in annotations.load(cid).values():
            # 只看误报
            if mark.get("verdict") != "false_positive":
                continue
            # 指定了检测器时只看这一条规则
            det = mark.get("detector") or "(未指定)"
            if detector and det != detector:
                continue
            # 收入该规则的反例
            groups.setdefault(det, []).append({
                "case_id": cid,
                "filename": case.get("filename", ""),
                "excerpt": mark.get("excerpt", ""),
                "note": mark.get("note", ""),
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
            "samples": cases[:8],  # 前若干条反例，供界面直接展示
        })
    # 反例多的排在前面，那是最该改的规则
    return out
