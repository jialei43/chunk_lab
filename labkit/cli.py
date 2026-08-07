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
    # 构造检测配置，允许命令行覆盖关键阈值
    cfg = DetectorConfig(chunk_token_num=args.chunk_token_num)
    # 执行评估
    report = evaluate_all(
        only=args.case,  # 指定样本时只评估这些
        cfg=cfg,  # 检测阈值
        children_delimiter=args.children_delimiter,  # 父子分块分隔符
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
    cfg = DetectorConfig(chunk_token_num=args.chunk_token_num)
    # 全量评估
    report = evaluate_all(cfg=cfg, children_delimiter=args.children_delimiter)
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


def cmd_serve(args):
    """启动本地 Web 控制台。"""
    # 延迟导入，使不用 Web 界面时不必加载 Flask
    from .server import serve
    # 启动服务，阻塞直到手动中断
    serve(host=args.host, port=args.port)
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

    # serve 子命令
    p_serve = sub.add_parser("serve", help="启动 Web 控制台")
    # 监听地址，默认只绑回环，属开发时工具不对外暴露
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址")
    # 监听端口
    p_serve.add_argument("--port", type=int, default=5099, help="监听端口")
    # 绑定处理函数
    p_serve.set_defaults(func=cmd_serve)

    # smoke 子命令
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
