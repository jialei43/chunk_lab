"""人工标注：把人的判断沉淀成基准真值。

规则检测器只能给出「疑似」，究竟是不是问题最终仍要人来判定。把这些判定
存下来，才能回答两个此前无法回答的问题：

  - **准确率**：检测器报出来的，有多少经人工确认是真问题；
  - **召回率**：人工发现的问题，有多少被检测器抓到了。

漏报（检测器没报但人工认为有问题）尤其宝贵——它直接指出下一条该写什么规则。

标注按样本存成独立文件，与切片序号绑定。序号会随切分参数变化而错位，
因此同时记录正文摘要作为校验，避免参数一改标注就全对错了位置。
"""

import json  # 导入 json 读写标注文件
from datetime import datetime  # 导入 datetime 记录标注时间

from .paths import DATA_ROOT  # 导入数据根目录

# 标注存放目录，与语料、历史轮次并列
ANNOTATION_DIR = DATA_ROOT / "annotations"

# 标注结论的取值与含义
VERDICTS = {
    "confirmed": "确认是问题",  # 检测器报对了
    "false_positive": "误报",  # 检测器报错了，该块没问题
    "missed": "漏报",  # 检测器没报，但人工认为有问题
}


def _path(case_id):
    """某个样本的标注文件路径。"""
    # 按样本分文件，避免单文件随语料增长而膨胀
    return ANNOTATION_DIR / f"{case_id}.json"


def load(case_id):
    """读取某样本的全部标注，无标注时返回空字典。"""
    # 标注文件路径
    path = _path(case_id)
    # 尚未标注过属正常情况
    if not path.is_file():
        return {}
    # 文件损坏不应让整个预览不可用，降级为空
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        # 仅接受字典结构
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_one(case_id, chunk_index, verdict, detector="", note="", excerpt=""):
    """写入一条标注。同一切片重复标注时覆盖旧值。"""
    # 结论必须是已知取值，避免写入无法解释的数据
    if verdict not in VERDICTS:
        raise ValueError(f"未知的标注结论：{verdict}")
    # 确保目录存在
    ANNOTATION_DIR.mkdir(parents=True, exist_ok=True)
    # 读取已有标注
    data = load(case_id)
    # 以切片序号为键；JSON 的键必须是字符串
    data[str(chunk_index)] = {
        "chunk_index": chunk_index,  # 切片序号
        "verdict": verdict,  # 标注结论
        "detector": detector,  # 相关检测器；漏报时为人工选择的问题类型
        "note": note,  # 人工备注，说明为什么这么判
        # 正文摘要用于校验：切分参数一改序号就错位，
        # 靠它能发现标注与当前切片对不上
        "excerpt": (excerpt or "")[:60],
        "at": datetime.now().isoformat(timespec="seconds"),  # 标注时间
    }
    # 写回文件
    with _path(case_id).open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    # 返回写入的条目
    return data[str(chunk_index)]


def save_many(case_id, items, verdict, detector="", note="", skip_annotated=True):
    """整类批量写入标注，返回实际写入数与跳过数。

    与循环调用 save_one 的区别是只读写一次文件：一条规则动辄命中上百个切片，
    逐条走 save_one 会把整个标注文件反复全量重写上百遍。

    skip_annotated 默认为真，即保留已有标注不动——批量是为了省去重复点击，
    不该悄悄推翻此前逐条细看后给出的判断。
    """
    # 结论必须是已知取值，避免写入无法解释的数据
    if verdict not in VERDICTS:
        raise ValueError(f"未知的标注结论：{verdict}")
    # 确保目录存在
    ANNOTATION_DIR.mkdir(parents=True, exist_ok=True)
    # 一次性读入已有标注，后续修改只在内存里进行
    data = load(case_id)
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
            "verdict": verdict,  # 整批统一的标注结论
            # 检测器取调用方传入的当前筛选类型，而非切片命中的第一条规则：
            # 一个切片可能同时命中多条规则，按命中顺序归类会把判定算到别的规则头上
            "detector": detector,
            "note": note,  # 整批共用的备注
            "excerpt": (item.get("excerpt") or "")[:60],  # 正文摘要，用于日后校验序号是否错位
            "at": now,  # 标注时间
            "batch": True,  # 标记来自批量操作，便于与逐条细看后的判定区分
        }
        # 累加写入计数
        written += 1
    # 一条都没写入时不必碰文件
    if written:
        # 整批改完后一次性写回
        with _path(case_id).open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    # 返回写入与跳过的数量，供界面回报结果
    return {"written": written, "skipped": skipped}


def _region_path(case_id):
    """某个样本的区域标注文件路径。

    与切片标注分开存：切片标注以序号为键、一片一条，而区域标注是人直接在
    原文版面上圈出来的，同一份文档可以有很多条，且不属于任何一个切片——
    切分器压根没在那儿切出块来，正是要记录的事实。
    """
    # 与切片标注同目录，靠后缀区分
    return ANNOTATION_DIR / f"{case_id}.regions.json"


def load_regions(case_id):
    """读取某样本的全部区域标注，无标注时返回空列表。"""
    # 区域标注文件路径
    path = _region_path(case_id)
    # 尚未标注过属正常情况
    if not path.is_file():
        return []
    # 文件损坏不应让整个预览不可用，降级为空
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        # 仅接受列表结构
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_region(case_id, region, detector="", note=""):
    """新增一条区域标注，返回写入的条目。

    region 形如 [页码, x0, x1, top, bottom]，单位与切片坐标一致（PDF 点），
    这样人工圈出的范围能直接和切分器给出的位置作比较。
    """
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
    # 确保目录存在
    ANNOTATION_DIR.mkdir(parents=True, exist_ok=True)
    # 读取已有区域
    items = load_regions(case_id)
    # 组装条目
    item = {
        "id": f"r{len(items) + 1}_{int(datetime.now().timestamp())}",  # 稳定标识，供删除时定位
        "region": [pn, x0, x1, top, bottom],  # 区域坐标
        "detector": detector,  # 人工判定的问题类型
        "note": note,  # 备注，说明为什么圈这里
        "at": datetime.now().isoformat(timespec="seconds"),  # 标注时间
    }
    # 追加并写回
    items.append(item)
    with _region_path(case_id).open("w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=2)
    # 返回写入的条目
    return item


def all_regions():
    """汇总全部样本在原文上圈出的异常区域。

    区域标注按样本分文件存，但看的时候需要横着看：哪些文档被圈得最多、
    人反复圈出的是同一类什么问题——这些都要跨样本才看得出来。
    """
    # 目录还没建立时直接返回空
    if not ANNOTATION_DIR.is_dir():
        return []
    # 逐个区域文件收集
    out = []
    for path in sorted(ANNOTATION_DIR.glob("*.regions.json")):
        # 文件名去掉 .regions 后缀即样本标识
        cid = path.name[:-len(".regions.json")]
        # 带上样本标识，界面才知道该跳回哪个文档
        for item in load_regions(cid):
            out.append({**item, "case_id": cid})
    # 按标注时间倒序，最近圈的排在前面
    return sorted(out, key=lambda x: x.get("at", ""), reverse=True)


def delete_region(case_id, region_id):
    """按标识删除一条区域标注，返回是否删掉了东西。"""
    # 读取已有区域
    items = load_regions(case_id)
    # 过滤掉目标
    kept = [x for x in items if x.get("id") != region_id]
    # 数量没变说明没找到
    if len(kept) == len(items):
        return False
    # 写回
    with _region_path(case_id).open("w", encoding="utf-8") as fh:
        json.dump(kept, fh, ensure_ascii=False, indent=2)
    # 确实删掉了
    return True


def delete_one(case_id, chunk_index):
    """删除一条标注。"""
    # 读取已有标注
    data = load(case_id)
    # 存在才删除
    if str(chunk_index) in data:
        del data[str(chunk_index)]
        # 写回
        with _path(case_id).open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    # 返回是否发生了删除
    return True


def delete_many(case_id, chunk_indexes):
    """批量删除标注，返回实际删除的条数。

    批量误标一整类之后，逐条撤销同样是上百次点击，因此撤销也要能整批做。
    """
    # 一次性读入已有标注
    data = load(case_id)
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
        with _path(case_id).open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    # 返回删除数量，供界面回报结果
    return removed


def stats(case_id=None):
    """统计标注情况，并据此计算检测器的准确率与漏报数。"""
    # 待统计的样本列表
    if case_id:
        cases = [case_id]
    else:
        # 未指定时统计全部已标注样本
        cases = ([p.stem for p in ANNOTATION_DIR.glob("*.json")]
                 if ANNOTATION_DIR.is_dir() else [])
    # 累计各类结论
    total = {"confirmed": 0, "false_positive": 0, "missed": 0}
    # 按检测器分别累计，用于找出误报率最高的规则
    by_detector = {}
    # 逐样本累加
    for cid in cases:
        for item in load(cid).values():
            # 累加总计
            v = item.get("verdict")
            if v in total:
                total[v] += 1
            # 累加到对应检测器
            d = item.get("detector") or "(未指定)"
            slot = by_detector.setdefault(d, {"confirmed": 0, "false_positive": 0, "missed": 0})
            if v in slot:
                slot[v] += 1

    # 准确率：确认为真的占「已判定的检测器命中」之比
    judged = total["confirmed"] + total["false_positive"]
    precision = round(total["confirmed"] / judged, 3) if judged else None
    # 召回率：检测器抓到的占「人工认定的全部问题」之比
    all_real = total["confirmed"] + total["missed"]
    recall = round(total["confirmed"] / all_real, 3) if all_real else None

    # 返回统计结果
    return {
        "cases": len(cases),  # 已标注的样本数
        "total": total,  # 各结论的总数
        "by_detector": by_detector,  # 逐检测器的结论分布
        "precision": precision,  # 准确率，报出来的有多少是真的
        "recall": recall,  # 召回率，真问题有多少被抓到
    }
