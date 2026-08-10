"""chunk-lab 统一命令行入口。

子命令：
    ingest    把本机 MinerU 产物导入语料库
    eval      对语料库跑离线切分与全部检测器，产出报告
    smoke     单个产物的连通性验证（阶段一遗留的调试入口）
    cache     查看或清理 VLM/LLM 结果缓存
"""

import argparse  # 导入 argparse 构建子命令
import sys  # 导入 sys 控制退出码
from pathlib import Path  # 导入 Path 写出优化任务书


def cmd_ingest(args):
    """执行语料导入。"""
    # 延迟导入，使未使用的子命令不承担无谓的模块加载
    from .ingest import ingest_all
    # 批量导入，overwrite 决定是否覆盖已存在的样本
    results = ingest_all(overwrite=args.overwrite)
    # 逐条打印导入状态
    for case_id, status in results:
        # 对齐输出便于扫读
        print(f"  {case_id:<20} {status}")
    # 汇总提示
    print(f"\n共处理 {len(results)} 个样本")
    # 正常结束
    return 0


def cmd_eval(args):
    """执行全量评估并输出报告。"""
    # 延迟导入
    from . import annotations, runs
    from .detectors import DetectorConfig
    from .evaluate import compare_reports, evaluate_all, format_comparison, format_report
    # 启动时确保旧版基线已收编为历史轮次
    runs.migrate_legacy_baseline()
    # 同时把旧版不带版本的平铺标注收编进最新版本
    annotations.migrate_flat()
    # 记下评估前的最新一轮：它就是新版本要继承标注的来源。
    # 历史轮次被清理过时索引会是空的，此时回退到最近一个存有标注的版本——
    # 否则那些人工判定会因为找不到继承源而全部失效。
    prev_run = runs.latest_run_id() or annotations.latest_annotated_run()
    # 组装完整切分配置，与 Web 端同一口径；未指定项由默认值补齐
    from .offline import build_parser_config
    overrides = {"chunk_token_num": args.chunk_token_num}
    # 仅在显式指定时覆盖，避免空值冲掉默认
    if args.children_delimiter:
        overrides["children_delimiter"] = args.children_delimiter
        overrides["enable_children"] = True
    config = build_parser_config(overrides)
    # 检测阈值与切分预算同源
    cfg = DetectorConfig(chunk_token_num=int(config.get("chunk_token_num") or 512))
    # 执行评估
    report = evaluate_all(
        only=args.case,  # 指定样本时只评估这些
        cfg=cfg,  # 检测阈值
        parser_config=config,  # 完整切分配置
    )
    # 没有样本参与时明确提示，避免使用者误以为是零缺陷。
    # 语料库非空却评估了 0 个，说明样本全被停用了——这两种情况的处置完全不同
    if report["case_count"] == 0:
        from .evaluate import load_cases
        total = len(load_cases())
        if total:
            print(f"语料库有 {total} 个样本，但全部处于停用状态，本轮没有可评估的内容。")
            print("到 Web 控制台的「语料库」页勾选要参与评估的样本，或改 case.yaml 的 enabled 字段。")
        else:
            print("语料库为空，请先执行：./run.sh ingest")
        return 1
    # 存为不可变的历史轮次，全量评估才入历史以免局部评估污染趋势
    run_id = None
    # 本轮从上一版继承过来的标注概况，供结束时提示
    inherited = None
    if not args.case:
        # 落盘并取回带代码指纹的完整快照
        run_id = runs.save_run(report, label=args.label or "")
        # 把上一版的人工判定继承到新版本，并按新快照逐条重判
        inherited = annotations.inherit(prev_run, run_id)
        report = runs.load_run(run_id)
        # 人工已判定为误报的问题不再计入统计：判定过一次就不该每轮重新甄别同一批噪声，
        # 否则真正的回归会被它们淹没。必须放在继承之后——判定要先按本轮快照重新定位到切片。
        from .evaluate import apply_false_positive_marks
        ignored = apply_false_positive_marks(report, annotations.false_positive_marks(run_id))
        # 确有忽略时把新统计回写到本轮快照，界面与后续对比都以此为准
        if ignored:
            report = runs.update_run(run_id, report)
    # 渲染并打印终端报告
    print(format_report(report, top_findings=args.top))
    # 提示本轮的历史标识与代码指纹，便于日后回溯
    if run_id:
        code = report.get("code", {})
        print(f"\n本轮已存为历史：{run_id}　代码指纹 {code.get('hash', '?')}"
              f"{'（含未提交改动）' if code.get('git_dirty') else ''}")
        # 继承概况：让人知道这一版的标注是从哪来的、有没有真的搬过来
        if inherited and inherited["marks"]:
            print(f"已从 {prev_run} 继承 {inherited['marks']} 条标注、"
                  f"{inherited['regions']} 条区域标注，并按本轮结果重新判定")
        # 如实说明有多少问题因人工判定为误报而未计入，避免总数下降被误读为改善
        if report.get("ignored_total"):
            print(f"已忽略 {report['ignored_total']} 条人工判定为误报的问题（不计入上面的问题总数）")
        # 生成 Markdown 评估报告：逐条列出问题、证据、可能相关代码与复现命令，
        # 可直接交给编码助手作为修复依据
        from .report import save_markdown
        # 从文本快照取切片全文，供报告中的完整案例使用
        md = save_markdown(report, chunks_by_case=runs.load_chunks(run_id))
        # 提示报告位置
        print(f"评估报告：{md}")
    # 与指定轮次对比，这是判断一次代码改动优劣的核心依据
    if args.compare:
        # 解析对比目标，支持 baseline / latest / 具体 run_id
        target = runs.resolve_run(args.compare)
        # 目标不存在或就是本轮自己时给出提示
        if target is None or target.get("run_id") == run_id:
            print(f"\n没有可对比的轮次（{args.compare}）。先跑一轮并执行 ./run.sh baseline 设定基准。")
        else:
            # 计算并打印升降
            print(format_comparison(compare_reports(target, report)))
    # 按需把本轮设为新的对比基准
    if args.set_baseline and run_id:
        # 基线只是指向某轮的指针
        runs.set_baseline(run_id)
        # 提示已更新
        print(f"\n基准已指向本轮：{run_id}")
    # 正常结束
    return 0


def cmd_baseline(args):
    """把某一轮设为对比基准；未指定轮次时跑一轮新的并设为基准。"""
    # 延迟导入
    from . import runs
    from .detectors import DetectorConfig
    from .evaluate import evaluate_all
    # 先确保旧版基线已收编
    runs.migrate_legacy_baseline()
    # 指定了轮次则直接改指针，不必重跑
    if args.run_id:
        # 目标轮次必须存在
        if runs.load_run(args.run_id) is None:
            print(f"轮次不存在：{args.run_id}")
            return 1
        # 设定基准
        runs.set_baseline(args.run_id)
        print(f"基准已指向：{args.run_id}")
        return 0
    # 未指定则跑一轮新的
    from .offline import build_parser_config
    config = build_parser_config({"chunk_token_num": args.chunk_token_num})
    cfg = DetectorConfig(chunk_token_num=int(config.get("chunk_token_num") or 512))
    # 全量评估
    report = evaluate_all(cfg=cfg, parser_config=config)
    # 语料为空时不允许建立基准，否则后续对比毫无意义
    if report["case_count"] == 0:
        print("语料库为空，请先执行：./run.sh ingest")
        return 1
    # 存为历史轮次并设为基准
    run_id = runs.save_run(report, label=args.label or "基准")
    runs.set_baseline(run_id)
    # 打印摘要供确认
    print(f"基准已建立：{run_id}")
    print(f"  语料 {report['case_count']} 个   切片 {report['chunk_total']} 个   问题 {report['finding_total']} 条")
    # 正常结束
    return 0


def cmd_runs(args):
    """列出历史轮次，或对比其中任意两轮。"""
    # 延迟导入
    from . import runs
    from .evaluate import compare_reports, format_comparison
    # 确保旧版基线已收编
    runs.migrate_legacy_baseline()
    # 指定了两个轮次则执行对比
    if args.compare:
        # 解析两侧轮次
        a, b = runs.resolve_run(args.compare[0]), runs.resolve_run(args.compare[1])
        # 任一不存在都无法对比
        if a is None or b is None:
            print("指定的轮次不存在")
            return 1
        # 打印对比结果
        print(format_comparison(compare_reports(a, b)))
        return 0
    # 否则列出全部历史
    index = runs.list_runs()
    # 无历史时提示
    if not index:
        print("尚无历史轮次。执行 ./run.sh eval 跑一轮。")
        return 0
    # 当前基准标识
    baseline_id = runs.get_baseline_id()
    # 表头
    print(f"{'轮次':<17}{'时间':<20}{'问题':>6}{'切片':>7}  {'代码指纹':<14}备注")
    print("-" * 92)
    # 逐行输出
    for r in index:
        # 基准轮次加标记
        mark = "★" if r["run_id"] == baseline_id else " "
        # 代码指纹，含未提交改动时加星号提示
        code = r.get("code_hash", "?") + ("*" if r.get("git_dirty") else "")
        # 输出一行
        print(f"{mark}{r['run_id']:<16}{r.get('generated_at', '')[:19]:<20}"
              f"{r.get('finding_total', 0):>6}{r.get('chunk_total', 0):>7}  {code:<14}{r.get('label', '')}")
    # 图例
    print("\n★ = 当前基准　* = 含未提交改动")
    # 正常结束
    return 0


def cmd_guard(args):
    """拿来源版本的人工判定，去目标版本的切分结果上逐条核对。

    改检测规则的正确顺序是：改完跑一轮评估产生新版本，标注会自动继承并重判，
    再跑这条命令，用新版本核对上一版的判定。
    出现「回归」说明这次改动碰坏了本来能检出的东西。
    """
    # 延迟导入，其它子命令不承担切分依赖的加载开销
    from . import annotations, runs
    from .guard import OUTCOMES, analyze_false_positives, build_full_report, check

    # 先把旧版平铺标注收编进版本目录，否则这条命令看到的是空的
    annotations.migrate_flat()
    # 目标版本：未指定时取最新一轮
    run_id = args.run or runs.latest_run_id()
    # 一轮评估都没有时无从核对，明确提示下一步
    if not run_id:
        print("尚无任何评估版本。先跑一轮：./run.sh eval")
        return 1

    # 生成优化任务书：这是独立用途，出完就结束，不再跑核对
    if args.report:
        # 生成报告，内容取自该版本的人工标注
        rep = build_full_report(run_id)
        # 未指定路径时打印到标准输出，便于直接管道给别的命令
        if args.report == "-":
            print(rep["markdown"])
        else:
            # 写入指定文件
            out = Path(args.report).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(rep["markdown"], encoding="utf-8")
            # 回报落盘位置与内容概要，便于确认生成的是不是想要的
            c = rep["counts"]
            print(f"已写入 {out}")
            print(f"  误报 {c['false_positive']} 条（涉及 {c['false_positive_rules']} 条规则）、"
                  f"已有规则漏报 {c['missed_known']} 条、新需求 {c['missed_new']} 条、"
                  f"框选区域 {c['regions']} 块")
        return

    # 执行核对；base 未指定时取目标版本的上一轮
    r = check(run_id, base_run=args.base, only=[args.case] if args.case else None)
    # 说清这次是拿哪一版的判定、在哪一版上核对的
    origin = r.get("base_run") or run_id
    print(f"\n判定来自版本 {origin}，在版本 {run_id} 的切分结果上核对")
    # 汇总一行说清结果
    parts = [f"{OUTCOMES[k]} {v}" for k, v in r["summary"].items() if v]
    print(f"核对 {r['checked']} 个样本：{'、'.join(parts) or '没有可核对的标注'}")
    # 有回归时明确指出，这是唯一算失败的情况
    print("结论：" + ("通过，无回归" if r["ok"] else "不通过，出现回归"))

    # 只关心待办时过滤掉已通过的条目
    rows = ([x for x in r["items"] if x["outcome"] in ("regressed", "open", "stale")]
            if args.todo else r["items"])
    # 逐条打印
    for it in rows:
        # 找不回切片时只有标注时的序号可说
        if it.get("chunk_index") is None:
            where = f"标注时 #{it.get('marked_index')}"
        else:
            # 序号错位时同时给出新旧序号，便于理解标注为什么要靠摘要定位
            moved = ("" if it.get("marked_index") == it.get("chunk_index")
                     else f"（标注时 #{it.get('marked_index')}）")
            where = f"#{it['chunk_index']}{moved}"
        print(f"\n  [{OUTCOMES[it['outcome']]}] {it['case_id']} {where}　{it['detector']}")
        print(f"      {it['message']}")
        # 人工备注往往直接写了原因，是改规则最有用的线索
        if it.get("note"):
            print(f"      人工备注：{it['note']}")
        # 摘要帮助确认核对的是不是同一段文字
        if it.get("excerpt"):
            print(f"      正文：{it['excerpt'][:60].replace(chr(10), ' ')}")

    # 附带误报归纳，指出规则该往哪儿改
    if args.why:
        groups = analyze_false_positives(run_id, only=[args.case] if args.case else None)
        # 没有误报时说明这一点，避免以为是功能没跑
        if not groups:
            print("\n暂无标为误报的切片，无从归纳")
        for g in groups:
            print(f"\n  规则 {g['detector']}：{g['count']} 条误报")
            print(f"      文件类型分布：{g['by_ext']}")
            # 人工备注是最直接的线索
            for n in g["notes"][:5]:
                print(f"      人工判断：{n}")


def cmd_smoke(args):
    """执行单产物连通性验证，转发给阶段一的 smoke 模块。"""
    # 延迟导入
    from .smoke import main as smoke_main
    # 组装 smoke 需要的参数列表
    argv = [args.content_list, args.filename, "--show", str(args.show)]
    # 透传父子分隔符
    if args.children_delimiter:
        argv += ["--children-delimiter", args.children_delimiter]
    # 透传 slide_mode 开关
    if args.slide:
        argv.append("--slide")
    # 执行并返回其退出码
    return smoke_main(argv)


def cmd_inspect(args):
    """复现单个样本的问题：打印命中切片的完整正文与前后文。"""
    # 延迟导入
    from .detectors import DetectorConfig
    from .evaluate import inspect_case, load_cases
    from .report import DETECTOR_INFO
    # 加载目标样本
    cases = load_cases(only=[args.case_id])
    # 样本不存在时列出可选项，避免用户猜名字
    if not cases:
        all_ids = [c["case_id"] for c in load_cases()]
        print(f"样本不存在：{args.case_id}")
        print("可选样本：" + ", ".join(all_ids))
        return 1
    # 按指定参数切分，保证与报告口径一致
    from .offline import build_parser_config
    overrides = {"chunk_token_num": args.chunk_token_num}
    if args.children_delimiter:
        overrides["children_delimiter"] = args.children_delimiter
        overrides["enable_children"] = True
    config = build_parser_config(overrides)
    cfg = DetectorConfig(chunk_token_num=int(config.get("chunk_token_num") or 512))
    data = inspect_case(cases[0], cfg=cfg, parser_config=config)
    # 切分失败时直接报出
    if data.get("error"):
        print(f"切分失败：{data['error']}")
        return 1
    # 全部切片，按序号索引便于取前后文
    chunks = data["chunks"]
    by_idx = {c["index"]: c for c in chunks}

    # 指定切片序号时只看那一块及其前后文
    if args.chunk is not None:
        target = by_idx.get(args.chunk)
        # 序号不存在时提示范围
        if target is None:
            print(f"切片 #{args.chunk} 不存在（本样本共 {len(chunks)} 个切片，序号 0..{len(chunks)-1}）")
            return 1
        # 打印目标及其相邻切片，截断问题必须看前后文才能判断
        print(f"样本 {data['case_id']}　文件 {data['filename']}　共 {data['chunk_count']} 切片")
        print("=" * 78)
        for i in (args.chunk - 1, args.chunk, args.chunk + 1):
            c = by_idx.get(i)
            if c is None:
                continue
            mark = "▶ " if i == args.chunk else "  "
            print(f"{mark}#{i}　类型={c['doc_type_kwd'] or 'text'}　{c['content_len']} 字　"
                  f"页码={c['page_num_int']}　面包屑={' > '.join(c['important_kwd'] or [])}")
            print(c["content"])
            # 该切片的问题标注
            for f in c["findings"]:
                print(f"    [{f['severity']}] {f['detector']}: {f['message']}")
            print("-" * 78)
        return 0

    # 未指定切片时按检测器筛选，列出该类问题的全部命中
    hits = [c for c in chunks if c["findings"]
            and (not args.detector or any(f["detector"] == args.detector for f in c["findings"]))]
    # 无命中时说明情况
    if not hits:
        print(f"样本 {args.case_id} 中没有命中"
              + (f"「{args.detector}」的切片" if args.detector else "任何问题的切片"))
        return 0
    # 打印概要
    cn = (DETECTOR_INFO.get(args.detector) or {}).get("cn", args.detector) if args.detector else "全部问题"
    print(f"样本 {data['case_id']}　文件 {data['filename']}　共 {data['chunk_count']} 切片")
    print(f"筛选：{cn}　命中 {len(hits)} 个切片")
    print("=" * 78)
    # 逐个打印命中切片的完整正文
    for c in hits[:args.limit]:
        print(f"▶ #{c['index']}　类型={c['doc_type_kwd'] or 'text'}　{c['content_len']} 字　"
              f"页码={c['page_num_int']}")
        print(c["content"])
        # 只打印筛选到的那类问题，避免无关信息干扰
        for f in c["findings"]:
            if args.detector and f["detector"] != args.detector:
                continue
            print(f"    [{f['severity']}] {f['detector']}: {f['message']}")
            if f.get("evidence"):
                print(f"    证据: {f['evidence']}")
        print("-" * 78)
    # 超出上限时提示如何看全部
    if len(hits) > args.limit:
        print(f"（仅显示前 {args.limit} 个，共 {len(hits)} 个；用 --limit 调整）")
    return 0


def cmd_crawl(args):
    """从列表页抓取文件。转发给爬虫模块的命令行入口。"""
    # 延迟导入，未用到抓取时不加载 requests 与 bs4
    from .crawler import main as crawl_main
    # 直接复用其参数解析，避免两处维护同一组选项
    return crawl_main(args.rest)


def cmd_serve(args):
    """启动本地 Web 控制台。"""
    # 延迟导入，使不用 Web 界面时不必加载 Flask
    from .server import serve
    # 启动服务，阻塞直到手动中断；默认开启热加载，改代码即时生效
    serve(host=args.host, port=args.port, reload=not args.no_reload)
    # 正常结束
    return 0


def cmd_cache(args):
    """查看或清理 VLM/LLM 结果缓存。"""
    # 延迟导入，其余子命令不必承担缓存模块的加载开销
    from . import vlmcache

    # 清理动作：按样本或全清
    if args.action == "clear":
        # 执行清理，未指定 --sample 即全清
        result = vlmcache.cache_clear(args.sample)
        # 缓存不可用时如实说明并以非零退出，便于脚本判断
        if not result.get("available"):
            print(f"缓存不可用：{result.get('reason')}")
            return 1
        # 打印删除结果
        print(f"已清理 {result['deleted']} 条缓存（范围：{result['scope']}）")
        return 0

    # 统计动作：打印条目数、占用与样本分布
    result = vlmcache.cache_stats()
    # 缓存不可用时如实说明
    if not result.get("available"):
        print(f"缓存不可用：{result.get('reason')}")
        return 1
    # 条目总数与占用，字节转成 MB 便于阅读
    print(f"缓存条目：{result['entries']} 条，占用 {result['bytes'] / 1024 / 1024:.2f} MB")
    # 样本分布为空说明还没跑过带模型的切分
    if not result["per_sample"]:
        print("（暂无样本索引；开启图片描述或表格摘要跑一轮后才会产生）")
        return 0
    # 按用量倒序打印各样本的条目数
    print("\n各样本用量：")
    # 逐行输出，样本名左对齐便于扫读
    for name, count in result["per_sample"].items():
        print(f"  {name:<50} {count} 条")
    return 0


def build_parser():
    """构建命令行解析器。"""
    # 顶层解析器
    parser = argparse.ArgumentParser(prog="chunk-lab", description="MinerU 切分离线实验室")
    # 子命令容器，required 保证必须指定子命令
    sub = parser.add_subparsers(dest="command", required=True)

    # ingest 子命令
    p_ingest = sub.add_parser("ingest", help="导入语料库")
    # 是否覆盖已导入的样本
    p_ingest.add_argument("--overwrite", action="store_true", help="覆盖已存在的样本")
    # 绑定处理函数
    p_ingest.set_defaults(func=cmd_ingest)

    # eval 子命令
    p_eval = sub.add_parser("eval", help="全量评估")
    # 限定评估的样本，可重复指定
    p_eval.add_argument("--case", action="append", help="只评估指定 case_id，可重复")
    # token 预算，同时作为超长判据基准
    p_eval.add_argument("--chunk-token-num", type=int, default=512, help="token 预算")
    # 父子分块分隔符，对切分粒度影响极大
    p_eval.add_argument("--children-delimiter", default="", help="父子分块分隔符，如 '\\n'")
    # 每类问题展示的样例条数
    p_eval.add_argument("--top", type=int, default=3, help="每类问题展示的样例条数")
    # 指定报告文件名
    p_eval.add_argument("--out", help="报告文件名")
    # 保留该开关以兼容既有用法，历史轮次始终落盘，此开关不再影响行为
    p_eval.add_argument("--no-save", action="store_true", help="（已废弃）历史轮次始终落盘")
    # 与指定轮次对比：baseline / latest / 具体 run_id
    p_eval.add_argument("--compare", nargs="?", const="baseline", default=None,
                        help="与指定轮次对比，可填 baseline（默认）/ latest / 具体 run_id")
    # 确认改动有效后把本轮设为新基准
    p_eval.add_argument("--set-baseline", action="store_true", help="把本轮设为新的对比基准")
    # 给本轮打备注，便于日后辨认做了什么改动
    p_eval.add_argument("--label", default="", help="本轮备注，如「修复 DOCX markdown 残留」")
    # 绑定处理函数
    p_eval.set_defaults(func=cmd_eval)

    # baseline 子命令
    p_base = sub.add_parser("baseline", help="设定对比基准")
    # 直接把已有轮次设为基准，不必重跑
    p_base.add_argument("run_id", nargs="?", help="把该轮次设为基准；省略则跑一轮新的")
    # token 预算
    p_base.add_argument("--chunk-token-num", type=int, default=512, help="token 预算")
    # 父子分块分隔符
    p_base.add_argument("--children-delimiter", default="", help="父子分块分隔符")
    # 备注
    p_base.add_argument("--label", default="", help="轮次备注")
    # 绑定处理函数
    p_base.set_defaults(func=cmd_baseline)

    # runs 子命令
    p_runs = sub.add_parser("runs", help="查看历史轮次或对比任意两轮")
    # 对比两轮：接受两个引用
    p_runs.add_argument("--compare", nargs=2, metavar=("基准", "对比"),
                        help="对比两轮，如 --compare baseline latest")
    # 绑定处理函数
    p_runs.set_defaults(func=cmd_runs)

    # inspect 子命令
    p_ins = sub.add_parser("inspect", help="复现单个样本的问题，打印切片全文")
    # 样本标识
    p_ins.add_argument("case_id", help="样本 ID，如 annual_cnipa")
    # 按检测器筛选
    p_ins.add_argument("--detector", help="只看该类问题，如 truncated_sentence")
    # 直接看某个切片及其前后文
    p_ins.add_argument("--chunk", type=int, help="查看该序号的切片及其相邻切片")
    # 展示条数上限
    p_ins.add_argument("--limit", type=int, default=10, help="最多展示多少个切片")
    # 切分参数需与报告一致，否则序号对不上
    p_ins.add_argument("--chunk-token-num", type=int, default=512, help="token 预算")
    # 父子分块分隔符
    p_ins.add_argument("--children-delimiter", default="", help="父子分块分隔符")
    # 绑定处理函数
    p_ins.set_defaults(func=cmd_inspect)

    # crawl 子命令：其余参数原样转交爬虫模块
    p_crawl = sub.add_parser("crawl", help="从列表页抓取文件",
                             add_help=False)
    # 收集全部剩余参数，由爬虫模块自行解析
    p_crawl.add_argument("rest", nargs=argparse.REMAINDER,
                         help="爬虫参数，用 ./run.sh crawl --help 查看")
    # 绑定处理函数
    p_crawl.set_defaults(func=cmd_crawl)

    # serve 子命令
    p_serve = sub.add_parser("serve", help="启动 Web 控制台")
    # 监听地址，默认只绑回环，属开发时工具不对外暴露
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址")
    # 监听端口
    p_serve.add_argument("--port", type=int, default=5099, help="监听端口")
    # 关闭热加载：重载器会多起一个进程、模块加载一次十几秒，不需要时可关
    p_serve.add_argument("--no-reload", action="store_true", help="关闭热加载")
    # 绑定处理函数
    p_serve.set_defaults(func=cmd_serve)

    # smoke 子命令
    p_guard = sub.add_parser("guard", help="回归护栏：用目标版本核对来源版本的人工判定")
    # 目标版本：用哪一轮的切分结果来核对，默认最新一轮
    p_guard.add_argument("--run", default="", help="目标版本（默认最新一轮）")
    # 来源版本：判定出自哪一轮，默认取目标版本的上一轮；传空串表示只看目标版本自己
    p_guard.add_argument("--base", default=None,
                         help="来源版本（默认目标版本的上一轮；传空串只看目标版本自己）")
    # 可只核对一个样本，便于针对某个文档快速迭代
    p_guard.add_argument("--case", default="", help="只核对指定样本")
    # 只列出仍需处理的条目，改规则时更省事
    p_guard.add_argument("--todo", action="store_true", help="只列出回归与待修正")
    # 顺带打印误报归纳
    p_guard.add_argument("--why", action="store_true", help="附带误报共性分析")
    # 生成可直接交给模型的优化任务书
    p_guard.add_argument("--report", nargs="?", const="-", metavar="路径",
                         help="生成优化任务书 Markdown；不给路径则打印到标准输出")
    # 绑定处理函数
    p_guard.set_defaults(func=cmd_guard)

    p_smoke = sub.add_parser("smoke", help="单产物连通性验证")
    # 产物路径
    p_smoke.add_argument("content_list", help="content_list.json 路径")
    # 原始文件名
    p_smoke.add_argument("filename", help="原始文件名（含扩展名）")
    # PPTX 按页切分
    p_smoke.add_argument("--slide", action="store_true", help="启用 slide_mode")
    # 父子分块分隔符
    p_smoke.add_argument("--children-delimiter", default="", help="父子分块分隔符")
    # 预览条数
    p_smoke.add_argument("--show", type=int, default=3, help="打印前 N 个 chunk")
    # 绑定处理函数
    p_smoke.set_defaults(func=cmd_smoke)

    p_cache = sub.add_parser("cache", help="查看或清理 VLM/LLM 结果缓存")
    # 动作：默认查看统计，clear 执行清理
    p_cache.add_argument("action", nargs="?", default="stats", choices=["stats", "clear"],
                         help="stats 查看用量（默认），clear 清理缓存")
    # 清理范围：不指定则全清。缓存永久保留，改了提示词后旧条目不会自动消失，需在此手动清
    p_cache.add_argument("--sample", default="", metavar="样本名",
                         help="仅清理该样本用到的条目；不指定则全清")
    # 绑定处理函数
    p_cache.set_defaults(func=cmd_cache)

    # 返回构建好的解析器
    return parser


def main(argv=None):
    """命令行主入口。"""
    # 解析参数
    args = build_parser().parse_args(argv)
    # 分发到对应子命令并返回其退出码
    return args.func(args)


# 作为模块直接运行时执行 main
if __name__ == "__main__":
    sys.exit(main())
