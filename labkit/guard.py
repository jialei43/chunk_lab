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
            # 收入该规则的反例。带上切片序号与文件名，
            # 界面才能从这里直接跳回原切片去看上下文
            groups.setdefault(det, []).append({
                "case_id": cid,
                "filename": case.get("filename", ""),
                "chunk_index": mark.get("chunk_index"),
                "excerpt": mark.get("excerpt", ""),
                "note": mark.get("note", ""),
                "batch": bool(mark.get("batch")),  # 批量判定的可信度低于逐条细看
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


def list_marks(verdict, detector=None, only=None):
    """按结论列出标注明细，供界面直接跳回原切片。

    统计只给出数量，看不到具体是哪些切片。要动手改规则就得能从数字点进去，
    落到那一块正文上——这里提供的就是这条通路。
    """
    # 收集明细
    out = []
    # 遍历有标注的样本
    for case in load_cases(only=only):
        # 样本标识
        cid = case["case_id"]
        # 逐条标注
        for mark in annotations.load(cid).values():
            # 只看指定结论
            if mark.get("verdict") != verdict:
                continue
            # 指定了问题类型时只看这一类
            det = mark.get("detector") or "(未指定)"
            if detector and det != detector:
                continue
            # 带上定位所需的全部信息
            out.append({
                "case_id": cid,
                "filename": case.get("filename", ""),
                "chunk_index": mark.get("chunk_index"),
                "detector": det,
                "excerpt": mark.get("excerpt", ""),
                "note": mark.get("note", ""),
                "batch": bool(mark.get("batch")),
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


def build_optimization_report(detector, only=None, parser_config=None):
    """为某条规则生成优化报告。

    报告要能直接拿去改代码，因此同时给出三样东西：规则的当前实现、
    人工判定为误报的全部反例、以及这些反例此刻的真实命中证据。
    只有摘要而没有证据的话，改规则时仍然要一条条去翻原文。
    """
    # 检测阈值取默认
    cfg = DetectorConfig()
    # 该规则的误报归纳
    groups = analyze_false_positives(detector=detector, only=only)
    # 没有反例时没什么可优化的
    if not groups:
        return {"ok": False, "message": f"规则 {detector} 没有被标为误报的切片，无从生成优化报告"}
    # 取这一条规则的归纳
    g = groups[0]

    # 重跑一遍拿当前证据：标注里只存了正文摘要，
    # 而改规则真正需要知道的是「这条规则在这块正文上匹配到了什么」
    cases_needed = sorted({s["case_id"] for s in g["samples"]})
    # 逐样本重切并取证据
    evidences = {}
    for case in load_cases(only=cases_needed):
        # 样本标识
        cid = case["case_id"]
        # 用当前代码切分并检测
        data = inspect_case(case, cfg=cfg, parser_config=parser_config)
        # 切分失败时跳过该样本，其余样本照常
        if data.get("error"):
            continue
        # 本样本内已认领的切片，避免多条标注落到同一个上
        used = set()
        # 逐条反例定位并取证据
        for s in g["samples"]:
            # 只处理本样本的
            if s["case_id"] != cid:
                continue
            # 按摘要找回切片
            found = _find_chunk(data.get("chunks") or [], s["excerpt"], used)
            # 找不回来时如实留空
            if found is None:
                continue
            # 认领
            used.add(found.get("index"))
            # 取该规则在这块上的命中证据
            hits = [f for f in (found.get("findings") or []) if f.get("detector") == detector]
            evidences[(cid, s["excerpt"][:30])] = {
                "index": found.get("index"),
                "content": found.get("content", ""),
                "evidence": hits[0].get("evidence", "") if hits else "",
                "still_hit": bool(hits),
            }

    # 把证据挂回反例
    samples = []
    for s in g["samples"]:
        # 按样本与摘要前缀取回
        ev = evidences.get((s["case_id"], s["excerpt"][:30]), {})
        samples.append({**s, **ev})

    # 规则的当前实现
    rule = _rule_source(detector)

    # 组装 Markdown 报告
    lines = [
        f"# 规则优化报告：{detector}",
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
            lines += ["```python"] + rule["consts"] + ["```", ""]
        lines += ["```python", rule["source"].rstrip(), "```", ""]
    else:
        lines += [
            "## 当前实现",
            "",
            f"未找到 `detect_{detector}` 函数——这条多半是人工提出、尚未实现的规则需求。",
            "",
        ]

    # 逐条反例，带当前证据
    lines += ["## 误报明细", ""]
    for i, s in enumerate(samples, 1):
        # 标题行给出定位信息
        lines += [
            f"### {i}. {s['case_id']} #{s.get('index', s.get('chunk_index'))}"
            f"（{s.get('filename', '')}）",
            "",
        ]
        # 当前是否仍在命中：改完规则后重新生成报告，这里应当变成「否」
        lines.append(f"- 当前仍命中：{'是' if s.get('still_hit') else '否'}")
        # 命中的具体内容是改规则的关键——知道匹配到了什么才知道怎么排除
        if s.get("evidence"):
            lines.append(f"- 命中证据：`{s['evidence']}`")
        # 人工备注说明为什么判为误报
        if s.get("note"):
            lines.append(f"- 人工判断：{s['note']}")
        # 批量判定的可信度低于逐条细看，如实标注
        if s.get("batch"):
            lines.append("- 来自批量判定（未逐条细看）")
        lines += ["", "正文：", "", "```text", (s.get("content") or s["excerpt"])[:600], "```", ""]

    # 收尾给出验证方式，避免改完不知道怎么确认
    lines += [
        "## 改完怎么验证",
        "",
        "```bash",
        "./run.sh guard --todo        # 这些条目应从「待修正」变为「符合预期」",
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
        "markdown": "\n".join(lines),
        "count": g["count"],
        "samples": samples,
        "rule": rule,
    }
