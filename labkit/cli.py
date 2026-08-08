"""chunk-lab 统一命令行入口。

子命令：
    ingest    把本机 MinerU 产物导入语料库
    eval      对语料库跑离线切分与全部检测器，产出报告
    smoke     单个产物的连通性验证（阶段一遗留的调试入口）
"""

import argparse  # 导入 argparse 构建子命令
import sys  # 导入 sys 控制退出码


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
    from . import runs
    from .detectors import DetectorConfig
    from .evaluate import compare_reports, evaluate_all, format_comparison, format_report
    # 启动时确保旧版基线已收编为历史轮次
    runs.migrate_legacy_baseline()
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
    # 语料为空时明确提示，避免使用者误以为是零缺陷
    if report["case_count"] == 0:
        print("语料库为空，请先执行：./run.sh ingest")
        return 1
    # 存为不可变的历史轮次，全量评估才入历史以免局部评估污染趋势
    run_id = None
    if not args.case:
        # 落盘并取回带代码指纹的完整快照
        run_id = runs.save_run(report, label=args.label or "")
        report = runs.load_run(run_id)
    # 渲染并打印终端报告
    print(format_report(report, top_findings=args.top))
    # 提示本轮的历史标识与代码指纹，便于日后回溯
    if run_id:
        code = report.get("code", {})
        print(f"\n本轮已存为历史：{run_id}　代码指纹 {code.get('hash', '?')}"
              f"{'（含未提交改动）' if code.get('git_dirty') else ''}")
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
    """核对人工标注在当前代码下是否仍然成立。

    改检测规则的正确顺序是：先跑一次记下基准，改完再跑一次比对。
    出现「回归」说明这次改动碰坏了本来能检出的东西。
    """
    # 延迟导入，其它子命令不承担切分依赖的加载开销
    from .guard import OUTCOMES, analyze_false_positives, check
    # 执行核对
    r = check(only=[args.case] if args.case else None)
    # 汇总一行说清结果
    parts = [f"{OUTCOMES[k]} {v}" for k, v in r["summary"].items() if v]
    print(f"\n核对 {r['checked']} 个样本：{'、'.join(parts) or '没有可核对的标注'}")
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
        groups = analyze_false_positives(only=[args.case] if args.case else None)
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
    p_guard = sub.add_parser("guard", help="回归护栏：核对人工标注是否仍然成立")
    # 可只核对一个样本，便于针对某个文档快速迭代
    p_guard.add_argument("--case", default="", help="只核对指定样本")
    # 只列出仍需处理的条目，改规则时更省事
    p_guard.add_argument("--todo", action="store_true", help="只列出回归与待修正")
    # 顺带打印误报归纳
    p_guard.add_argument("--why", action="store_true", help="附带误报共性分析")
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
