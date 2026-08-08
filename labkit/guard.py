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
            evidences[(cid, s.get("chunk_index"))] = {
                "index": found.get("index"),
                "content": found.get("content", ""),
                "evidence": hits[0].get("evidence", "") if hits else "",
                "still_hit": bool(hits),
            }

    # 把证据挂回反例
    samples = []
    for s in g["samples"]:
        # 按样本与摘要前缀取回
        ev = evidences.get((s["case_id"], s.get("chunk_index")), {})
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
            lines += _fenced("\n".join(rule["consts"]), "python")
        lines += _fenced(rule["source"].rstrip(), "python")
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
        lines += ["", "正文：", ""] + _fenced((s.get("content") or s["excerpt"])[:600])

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


def _collect_chunk_texts(marks, parser_config=None):
    """为一批标注取回它们此刻对应的切片正文与命中情况。

    标注里只有 60 字摘要，改代码时不够用——要判断一条规则该怎么写，
    得看到整块正文长什么样、以及当前规则在它上面匹配到了什么。
    """
    # 检测阈值取默认
    cfg = DetectorConfig()
    # 按样本归集，一个样本只切一次
    by_case = {}
    for m in marks:
        by_case.setdefault(m["case_id"], []).append(m)
    # 逐样本重切并取回正文
    out = {}
    for case in load_cases(only=sorted(by_case)):
        # 样本标识
        cid = case["case_id"]
        # 用当前代码切分并检测
        data = inspect_case(case, cfg=cfg, parser_config=parser_config)
        # 切分失败时跳过该样本，其余样本照常
        if data.get("error"):
            continue
        # 本样本内已认领的切片
        used = set()
        # 逐条标注定位
        for m in by_case[cid]:
            # 按摘要找回切片
            found = _find_chunk(data.get("chunks") or [], m.get("excerpt"), used)
            # 找不回来时留空，报告里会退回用摘要
            if found is None:
                continue
            # 认领
            used.add(found.get("index"))
            # 记下正文与当前全部命中
            # 用标注时的序号作键：它在样本内唯一，而摘要前缀会重复
            out[(cid, m.get("chunk_index"))] = {
                "index": found.get("index"),
                "content": found.get("content", ""),
                "findings": found.get("findings") or [],
            }
    return out


def build_full_report(parser_config=None):
    """生成一份完整的优化任务书，可直接交给模型动手改。

    与单条规则的报告的区别是「完整」：把当前全部待办放在一起，并按能否
    自动验证排序——误报和已实现规则的问题有护栏兜底，改完立刻知道对不对；
    人工提出的需求和框选区域需要新写代码，风险更高，放在后面。

    刻意把代码位置、注册方式、验证命令都写进去：拿到报告的人（或模型）
    不该还要回头问「检测器写在哪」「改完怎么验证」。
    """
    # 标注总体统计
    st = annotations.stats()
    # 误报归纳，按条数降序
    fp_groups = analyze_false_positives()
    # 人工提出的需求：结论为漏报、且问题类型不是已实现的检测器
    from . import detectors as _det
    missed = list_marks("missed")
    # 已实现的检测器名集合，用于区分「规则漏了」与「规则还不存在」
    implemented = {n[len("detect_"):] for n in dir(_det) if n.startswith("detect_")}
    # 拆成两类：已有规则漏报（调阈值即可）与全新需求（要写代码）
    missed_known = [m for m in missed if m["detector"] in implemented]
    missed_new = [m for m in missed if m["detector"] not in implemented]
    # 框选区域
    regions = annotations.all_regions()

    # 一次性取回所有相关切片的正文，避免每块各切一遍
    texts = _collect_chunk_texts(
        [s for g in fp_groups for s in g["samples"]] + missed,
        parser_config=parser_config,
    )

    # 百分比展示
    def pct(v):
        return "—" if v is None else f"{round(v * 100)}%"

    # 取某条标注对应的正文，找不回时退回摘要
    def body_of(m):
        rec = texts.get((m["case_id"], m.get("chunk_index")))
        return (rec or {}).get("content") or m.get("excerpt", "")

    # 取某条标注上某规则的当前命中证据
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
        "## 现状",
        "",
        f"- 检测器准确率 **{pct(st.get('precision'))}**"
        f"（报出来的有多少经人工确认是真问题）",
        f"- 召回率 **{pct(st.get('recall'))}**（人工发现的问题有多少被规则抓到）",
        f"- 已标注 {st.get('cases', 0)} 个样本，"
        f"确认 {st['total']['confirmed']} 条、误报 {st['total']['false_positive']} 条、"
        f"漏报 {st['total']['missed']} 条",
        f"- 人工在原文上圈出 {len(regions)} 块切分器没切出来的区域",
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
        "./run.sh guard --todo",
        "```",
        "",
        "护栏会用改后的代码重跑已标注的样本，逐条核对人工判定是否仍然成立：",
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

    # 没有任何待办时明确说明，避免拿到一份空文件不知道是不是出错了
    if not (fp_groups or missed_known or missed_new or regions):
        lines += [
            "## 暂无待办",
            "",
            "还没有任何人工标注。请先到「切片预览」里判定检测结果，",
            "或在原文视图上框出规则没覆盖到的区域。",
            "",
        ]

    # 返回报告与关键计数，界面据此展示概要
    return {
        "ok": True,
        "markdown": "\n".join(lines),
        "counts": {
            "false_positive_rules": len(fp_groups),  # 有误报的规则数
            "false_positive": sum(g["count"] for g in fp_groups),  # 误报总条数
            "missed_known": len(missed_known),  # 已有规则的漏报
            "missed_new": len(missed_new),  # 全新需求
            "regions": len(regions),  # 框选区域
        },
    }
