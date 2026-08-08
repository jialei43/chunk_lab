"""原文截图：把切片的位置坐标还原成 PDF 页面上的一块区域。

看正文只能判断「读起来是否完整」，看不出切分边界落在版面的什么位置——
是切在段落中间、跨了栏，还是把表格从视觉上劈开了。对照原文截图才能判断。

两个前置条件，缺一不可：
  - 样本要有**原始文件**。多数语料是从 MinerU 产物导入的，产物里不含原文，
    因此需要单独关联，界面上提供上传入口。
  - 切片要有 **position_int**。Office 文档的块通常没有 bbox，只有页码，
    这类样本只能定位到页、无法框出具体区域。
"""

import base64  # 导入 base64 把图片编码进 JSON 响应
import io  # 导入 io 在内存中保存图片
import shutil  # 导入 shutil 复制关联的原始文件
from functools import lru_cache  # 导入 lru_cache 缓存整页渲染结果

from .paths import DATA_ROOT, ensure_ragflow_importable  # 导入目录常量与路径注入

ensure_ragflow_importable()  # 在导入 ragflow 依赖之前注入源码路径

# 原始文件存放目录，按样本归档
SOURCE_DIR = DATA_ROOT / "sources"
# 渲染分辨率倍数：过低看不清文字，过高图片体积和耗时都不划算
RENDER_SCALE = 2
# 截图在命中区域四周留出的边距（页面坐标下的比例），便于看清上下文
MARGIN_RATIO = 0.02
# 整页图像的缓存页数。A4 在 144 DPI 下约 1191×1684 像素、单页约 6MB，
# 12 页约 70MB——够覆盖切片列表一屏内的跨页范围，又不至于把内存吃光
PAGE_CACHE_SIZE = 12


@lru_cache(maxsize=PAGE_CACHE_SIZE)
def _page_image_cached(path_str, mtime, page_idx):
    """渲染并缓存单页图像。

    缓存键带上 mtime：文件被重新解析或替换后，旧图像必须失效，
    否则会拿着上一份原文的截图去解释新的切分结果。

    切片列表一屏就有几十条、且大量落在同一页，不缓存的话每条都要重渲整页，
    实测这是缩略图加载慢的主因。
    """
    # 延迟导入，未用到截图的场景不承担其加载开销
    import pdfplumber
    # 打开并渲染目标页
    with pdfplumber.open(path_str) as pdf:
        # 页码越界由调用方保证，这里直接取
        return pdf.pages[page_idx].to_image(resolution=72 * RENDER_SCALE).original


def _page_image(src, page_idx):
    """取某页的渲染图像，按文件修改时间自动失效。"""
    # 以修改时间参与缓存键，文件一变缓存即失效
    return _page_image_cached(str(src), src.stat().st_mtime_ns, page_idx)


def _page_scale(page, pw, ph):
    """求「PDF 点 → 渲染像素」的缩放比。

    page.width/height 是 PDF 点（1 点 = 1/72 英寸），与切分器写入的坐标同单位；
    pw/ph 是实际渲染出来的像素。两者相除即得比例，故换算不依赖 RENDER_SCALE，
    将来改渲染分辨率也不会让坐标错位。
    """
    # 页宽为 0 的畸形页面会让除法炸掉，退化为 1:1
    sx = pw / float(page.width) if page.width else 1.0
    # 页高同理
    sy = ph / float(page.height) if page.height else 1.0
    # 返回横纵比例
    return sx, sy


def source_path(case_id, filename):
    """某样本关联的原始文件路径。"""
    # 按样本建子目录，避免不同样本的同名文件互相覆盖
    return SOURCE_DIR / case_id / filename


def find_source(case_id, filename):
    """查找样本已关联的原始文件，未关联时返回 None。"""
    # 首选按样本归档的位置
    p = source_path(case_id, filename)
    # 存在即返回
    if p.is_file():
        return p
    # 其次在解析上传目录中按文件名查找，实验室自己解析的样本原文在那里
    from .paths import UPLOAD_DIR
    # 拼出候选路径
    q = UPLOAD_DIR / filename
    # 存在即返回
    return q if q.is_file() else None


def attach_source(case_id, filename, file_obj):
    """把上传的原始文件关联到样本。"""
    # 目标路径
    dest = source_path(case_id, filename)
    # 确保目录存在
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 落盘
    file_obj.save(dest)
    # 返回路径供调用方回报
    return dest


def attach_source_from_path(case_id, filename, src):
    """从本机已有路径关联原始文件，避免大文件重复上传。"""
    # 目标路径
    dest = source_path(case_id, filename)
    # 确保目录存在
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 复制文件
    shutil.copy2(src, dest)
    # 返回路径
    return dest


def _encode(image):
    """把 PIL 图片编码成可直接嵌入 HTML 的 data URI。"""
    # 在内存中保存为 PNG，避免落盘产生临时文件
    buf = io.BytesIO()
    # PNG 无损，文字截图不宜用 JPEG
    image.save(buf, format="PNG")
    # 编码为 base64 并拼成 data URI
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def render_chunk(case_id, filename, positions, page_hint=None):
    """按切片的位置坐标裁出原文区域，返回可直接展示的图片列表。

    positions 形如 [[page, x0, x1, top, bottom], ...]，
    与生产 add_positions 写入 position_int 的结构一致。
    """
    # 定位原始文件
    src = find_source(case_id, filename)
    # 未关联原文时明确回报，由界面提示去关联
    if src is None:
        return {"ok": False, "reason": "no_source", "message": "该样本尚未关联原始文件"}
    # 仅 PDF 可渲染页面；Office 文档需要先转 PDF，成本高且会拖慢流程，暂不支持
    if src.suffix.lower() != ".pdf":
        return {"ok": False, "reason": "not_pdf",
                "message": f"仅支持 PDF 截图，该样本是 {src.suffix or '未知格式'}"}
    # 无坐标时无法框出区域
    if not positions:
        return {"ok": False, "reason": "no_position",
                "message": "该切片没有坐标信息，无法定位到版面区域"}

    # 延迟导入 pdfplumber，未用到截图的场景不承担其加载开销
    try:
        import pdfplumber
    except ImportError:
        return {"ok": False, "reason": "no_pdfplumber", "message": "环境缺少 pdfplumber，无法渲染 PDF"}

    # 逐个坐标裁图
    images = []
    # 打开 PDF；异常一律转成可读信息返回，不让界面拿到 500
    try:
        with pdfplumber.open(str(src)) as pdf:
            # 逐条位置处理
            for pos in positions:
                # 位置至少需要页码与四个坐标
                if not pos or len(pos) < 5:
                    continue
                # 拆出页码与坐标；页码在 position_int 中从 1 开始
                page_no, x0, x1, top, bottom = pos[0], pos[1], pos[2], pos[3], pos[4]
                # 坐标退化（全零或零面积）说明该块没有真实 bbox，
                # 常见于 Office 文档与封面块。裁出来是空图，必须跳过而不是返回 0KB 图片。
                if x1 <= x0 or bottom <= top:
                    continue
                # 页码越界时跳过该条而不是整体失败
                idx = int(page_no) - 1
                if idx < 0 or idx >= len(pdf.pages):
                    continue
                # 取目标页
                page = pdf.pages[idx]
                # 取整页图像。同一页往往被几十个切片同时命中，
                # 每次都重渲一遍会让缩略图列表慢得没法用，故走缓存
                pil = _page_image(src, idx)
                # 页面像素尺寸
                pw, ph = pil.size
                # 坐标单位是 PDF 点（72 DPI 像素）——生产的 add_positions 直接存
                # 原值、不做归一化，而 bridge 用 zoomin=1 渲染页面。按页面尺寸求
                # 比例即可换算到当前渲染分辨率，与 RENDER_SCALE 解耦。
                sx, sy = _page_scale(page, pw, ph)
                # 换算成像素，并向四周留出边距便于看清上下文
                left = max(0, int(x0 * sx - pw * MARGIN_RATIO))
                right = min(pw, int(x1 * sx + pw * MARGIN_RATIO))
                upper = max(0, int(top * sy - ph * MARGIN_RATIO))
                lower = min(ph, int(bottom * sy + ph * MARGIN_RATIO))
                # 区域退化时跳过，避免产生零尺寸图片
                if right <= left or lower <= upper:
                    continue
                # 裁剪并编码
                images.append({
                    "page": int(page_no),  # 页码，便于界面标注
                    "data_uri": _encode(pil.crop((left, upper, right, lower))),  # 图片本身
                })
    except Exception as e:
        # 渲染失败如实回报原因
        return {"ok": False, "reason": "render_failed", "message": f"{type(e).__name__}: {e}"}

    # 一张都没裁出来时说明坐标不可用
    if not images:
        return {"ok": False, "reason": "no_region", "message": "坐标无法映射到有效页面区域"}
    # 返回全部截图
    return {"ok": True, "images": images, "source": str(src)}
