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
    from .detectors import DetectorConfig
    from .evaluate import (compare_reports, evaluate_all, format_comparison,
                           format_report, load_baseline, save_baseline, save_report)
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
    # 渲染并打印终端报告
    print(format_report(report, top_findings=args.top))
    # 与基线对比，这是判断一次代码改动优劣的核心依据
    if args.compare:
        # 读取基线
        baseline = load_baseline()
        # 基线不存在时提示先建立
        if baseline is None:
            print("\n尚无基线，请先执行：./run.sh baseline")
        else:
            # 计算并打印升降
            print(format_comparison(compare_reports(baseline, report)))
    # 按需把本次结果固化为新基线
    if args.set_baseline:
        # 写出基线文件
        path = save_baseline(report)
        # 提示保存位置
        print(f"\n基线已更新：{path}")
    # 按需保存 JSON 报告
    if not args.no_save:
        # 写出报告文件
        path = save_report(report, name=args.out)
        # 提示保存位置
        print(f"\n报告已保存：{path}")
    # 正常结束
    return 0


def cmd_baseline(args):
    """跑一次完整评估并把结果固化为回归基线。"""
    # 延迟导入
    from .detectors import DetectorConfig
    from .evaluate import evaluate_all, save_baseline
    # 基线固定使用默认阈值，保证不同次基线之间口径一致
    cfg = DetectorConfig(chunk_token_num=args.chunk_token_num)
    # 全量评估
    report = evaluate_all(cfg=cfg, children_delimiter=args.children_delimiter)
    # 语料为空时不允许建立基线，否则后续对比毫无意义
    if report["case_count"] == 0:
        print("语料库为空，请先执行：./run.sh ingest")
        return 1
    # 写出基线
    path = save_baseline(report)
    # 打印摘要供确认
    print(f"基线已建立：{path}")
    print(f"  语料 {report['case_count']} 个   chunk {report['chunk_total']} 个   命中 {report['finding_total']} 条")
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
    # 不保存报告，用于快速查看
    p_eval.add_argument("--no-save", action="store_true", help="不保存 JSON 报告")
    # 与基线对比，改代码后的核心用法
    p_eval.add_argument("--compare", action="store_true", help="与基线对比并显示指标升降")
    # 确认改动有效后把当前结果固化为新基线
    p_eval.add_argument("--set-baseline", action="store_true", help="把本次结果保存为新基线")
    # 绑定处理函数
    p_eval.set_defaults(func=cmd_eval)

    # baseline 子命令
    p_base = sub.add_parser("baseline", help="建立回归基线")
    # token 预算，基线与后续评估必须一致
    p_base.add_argument("--chunk-token-num", type=int, default=512, help="token 预算")
    # 父子分块分隔符
    p_base.add_argument("--children-delimiter", default="", help="父子分块分隔符")
    # 绑定处理函数
    p_base.set_defaults(func=cmd_baseline)

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
