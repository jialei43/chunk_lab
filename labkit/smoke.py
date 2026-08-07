"""阶段一连通性验证：确认离线重放生产切分逻辑确实可行。

用法：
    ./run.sh <content_list.json 路径> <原始文件名> [--slide]

该脚本只回答一个问题：不启 ragflow 服务、不连数据库、不调 MinerU，
能否用缓存产物跑出与生产一致的 chunk。它是后续所有工作的前提。
"""

import argparse  # 导入 argparse 解析命令行参数
import sys  # 导入 sys 用于在失败时返回非零退出码

from .offline import normalize_chunks, run_offline_chunking  # 导入离线驱动与规范化函数


def summarize(records):
    """对规范化后的 chunk 记录做统计，输出一份人可读的摘要。"""
    # 没有产出任何 chunk 时直接返回空统计，避免后续除零
    if not records:
        return {"count": 0}
    # 统计各类型块的数量，用于确认表格/图片确实被识别出来
    type_counts = {}
    # 遍历所有记录累计类型分布
    for r in records:
        # 生产仅对表格和图片写 doc_type_kwd，普通正文块该字段为空，这里归一为 text
        key = r["doc_type_kwd"] or "text"
        # 累加该类型计数
        type_counts[key] = type_counts.get(key, 0) + 1
    # 收集所有正文长度，用于观察切分粒度是否符合预期
    lengths = [r["content_len"] for r in records]
    # 组装统计结果
    return {
        "count": len(records),  # chunk 总数
        "types": type_counts,  # 类型分布
        "len_min": min(lengths),  # 最短 chunk 字符数
        "len_max": max(lengths),  # 最长 chunk 字符数
        "len_avg": round(sum(lengths) / len(lengths), 1),  # 平均字符数
        "with_position": sum(1 for r in records if r["position_int"]),  # 带坐标的 chunk 数，预览定位能力的体现
        "with_page": sum(1 for r in records if r["page_num_int"]),  # 带页码的 chunk 数
        "with_breadcrumb": sum(1 for r in records if r["important_kwd"]),  # 带标题面包屑的 chunk 数
        "with_image": sum(1 for r in records if r["has_image"]),  # 带截图的 chunk 数
        "child_chunks": sum(1 for r in records if r["is_child"]),  # 子块数量，父子模式下应等于总数
        "parent_chunks": len({r["mom_content"] for r in records if r["is_child"]}),  # 去重后的父块数量
    }


def main(argv=None):
    """命令行入口：跑一次离线切分并打印摘要与样例。"""
    # 构造参数解析器
    parser = argparse.ArgumentParser(description="离线重放 MinerU 切分逻辑")
    # 必填：MinerU 缓存产物路径
    parser.add_argument("content_list", help="MinerU content_list.json 的路径")
    # 必填：原始文件名，切分器会据此判断文档类型
    parser.add_argument("filename", help="原始文件名（含扩展名，如 a.pdf）")
    # 可选：启用 PPTX 的按页切分模式
    parser.add_argument("--slide", action="store_true", help="启用 PPTX slide_mode")
    # 可选：打印前 N 个 chunk 的正文预览
    parser.add_argument("--show", type=int, default=3, help="打印前 N 个 chunk 预览")
    # 可选：父子分块分隔符，为空时每个原始段落独立成块，非空时按标题合并成父块
    parser.add_argument("--children-delimiter", default="", help="父子分块分隔符，如 '\\n'")
    # 可选：覆盖 token 预算，用于观察切分粒度对该参数的敏感度
    parser.add_argument("--chunk-token-num", type=int, default=None, help="覆盖 chunk_token_num")
    # 解析参数
    args = parser.parse_args(argv)

    # 组装本次运行的切分配置覆盖项，未指定的项由驱动内部的生产默认值补齐
    overrides = {"children_delimiter": args.children_delimiter}
    # 仅在显式指定时覆盖 token 预算，避免 None 冲掉默认值
    if args.chunk_token_num is not None:
        overrides["chunk_token_num"] = args.chunk_token_num

    # 执行离线切分，任何异常都让它自然抛出，便于第一时间看到真实堆栈
    chunks = run_offline_chunking(
        args.content_list,  # 缓存产物路径
        args.filename,  # 原始文件名
        parser_config=overrides,  # 本次运行的配置覆盖
        slide_mode=args.slide,  # 是否按幻灯片切分
    )
    # 规范化为可序列化记录
    records = normalize_chunks(chunks)
    # 计算统计摘要
    stats = summarize(records)

    # 打印统计结果标题
    print("=" * 60)
    print(f"文件：{args.filename}")
    print(f"产物：{args.content_list}")
    print("=" * 60)
    # 逐项打印统计指标
    for key, value in stats.items():
        print(f"  {key:>16}: {value}")

    # 打印若干 chunk 预览，肉眼确认切分结果是否合理
    for r in records[: args.show]:
        print("-" * 60)
        # 打印该 chunk 的关键元信息
        print(f"[#{r['index']}] type={r['doc_type_kwd']} len={r['content_len']} "
              f"page={r['page_num_int']} breadcrumb={r['important_kwd']}")
        # 正文过长时截断显示，避免刷屏
        preview = r["content"][:300]
        # 打印正文预览，超长时补省略号
        print(preview + ("…" if len(r["content"]) > 300 else ""))

    # 没有产出任何 chunk 视为验证失败，返回非零退出码
    return 0 if records else 1


# 作为模块直接运行时执行 main，并把返回值作为进程退出码
if __name__ == "__main__":
    sys.exit(main())
