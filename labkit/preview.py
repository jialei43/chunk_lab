"""原文截图：把切片的位置坐标还原成 PDF 页面上的一块区域。

看正文只能判断「读起来是否完整」，看不出切分边界落在版面的什么位置——
是切在段落中间、跨了栏，还是把表格从视觉上劈开了。对照原文截图才能判断。

两个前置条件，缺一不可：
  - 样本要有**原始文件**。多数语料是从 MinerU 产物导入的，产物里不含原文，
    因此需要单独关联，界面上提供上传入口。
  - 切片要有 **position_int**。Office 文档的块没有 bbox，只有页码，
    这类样本只能定位到页、无法框出具体区域。

Office 原文通过 LibreOffice 转成 PDF 后复用同一套视图，转换结果按源文件的
修改时间缓存。
"""

import base64  # 导入 base64 把图片编码进 JSON 响应
import io  # 导入 io 在内存中保存图片
import shutil  # 导入 shutil 复制关联的原始文件、定位 LibreOffice
import subprocess  # 导入 subprocess 调用 LibreOffice 转换 Office 文档
import tempfile  # 导入 tempfile 承接 LibreOffice 的转换产物
import threading  # 导入 threading 串行化 LibreOffice 调用
from functools import lru_cache  # 导入 lru_cache 缓存整页渲染结果
from pathlib import Path  # 导入 Path 处理转换产物路径

from .paths import DATA_ROOT, ensure_ragflow_importable  # 导入目录常量与路径注入

ensure_ragflow_importable()  # 在导入 ragflow 依赖之前注入源码路径

# 原始文件存放目录，按样本归档
SOURCE_DIR = DATA_ROOT / "sources"
# 渲染分辨率倍数：过低看不清文字，过高图片体积和耗时都不划算
RENDER_SCALE = 2
# 截图在命中区域四周留出的边距（页面坐标下的比例），便于看清上下文
MARGIN_RATIO = 0.02
# Office 文档转换后的 PDF 存放目录，与原文分开放，便于整体清理
CONVERT_DIR = DATA_ROOT / "sources" / "converted"
# 可转换的格式，与 LibreOffice 的能力一致
CONVERTIBLE_SUFFIXES = {".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".odt", ".odp", ".ods", ".rtf"}
# 浏览器能直接显示的图片格式。这些刻意不转 PDF——转一道既糊又慢，
# 而且图片的 MinerU 块本来就带 bbox，直接显示才能做精确定位
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}
# 单次转换的超时。LibreOffice 偶尔会卡住，不设上限会让请求一直挂着
CONVERT_TIMEOUT = 120
# 转换互斥锁。LibreOffice 同一时刻只允许一个实例使用同一份用户配置，
# 并发调用会让它直接崩溃（libc++abi: terminating）——实测快速切换样本必现。
# 转换本身也是 CPU 密集的，串行执行没有损失
_convert_lock = threading.Lock()

# 整页图像的缓存页数。A4 在 144 DPI 下约 1191×1684 像素、单页约 6MB，
# 12 页约 70MB——够覆盖切片列表一屏内的跨页范围，又不至于把内存吃光
PAGE_CACHE_SIZE = 12

# 文字反查每次最多尝试的行数：切片正文可能很长（多个要点合并成一个 chunk），
# 逐行都去搜一遍既慢又没必要，命中足够多行就能拼出近似区域
LOCATE_MAX_LINES = 8
# 反查片段取每行开头的字符数：太长容易跨越 PDF 渲染时的折行断点（页面上排版换行
# 与切片正文的自然换行不是一回事，一个词可能被从中间断开）而搜不到；
# 太短则容易在页面上命中不止一处，无法判断该用哪个
LOCATE_SNIPPET_LEN = 16
# 反查片段的最短长度：短于此长度的行大概率是空行或纯符号，不具备唯一定位价值，直接跳过
LOCATE_SNIPPET_MIN_LEN = 6


def viewable_pdf(src):
    """返回可供 pdf.js 渲染的 PDF 路径；Office 文档先转换。

    Office 文档没法直接在浏览器里渲染，转成 PDF 是最省事的路子——转完就能
    复用已有的整套原文视图（缩放、翻页、定位、截图），不必为每种格式再写
    一遍渲染逻辑。

    转换结果按源文件的修改时间缓存：LibreOffice 启动一次就要几秒，
    每次预览都转会让打开 Office 样本变得难以忍受。源文件一变，缓存即失效。

    返回 (路径, 说明)。转换不可用时路径为 None，说明里写清原因，
    由界面如实提示而不是显示一个空白框。
    """
    # PDF 与图片都可直接下发，无需转换
    if src.suffix.lower() == ".pdf" or src.suffix.lower() in IMAGE_SUFFIXES:
        return src, ""
    # 支持转换的格式，与 LibreOffice 的能力一致
    if src.suffix.lower() not in CONVERTIBLE_SUFFIXES:
        return None, f"暂不支持预览 {src.suffix or '未知格式'} 的原文"
    # 缺少 LibreOffice 时如实告知安装方式，而不是留下一个无解的空白
    exe = _soffice()
    if exe is None:
        return None, ("需要 LibreOffice 才能预览 Office 原文，"
                      "可执行 brew install --cask libreoffice 安装")
    # 转换结果按「文件名 + 源文件修改时间」命名，源文件一变即产生新文件
    CONVERT_DIR.mkdir(parents=True, exist_ok=True)
    # 用修改时间做版本，避免源文件更新后仍拿到旧转换结果
    stamp = src.stat().st_mtime_ns
    # 目标路径
    dest = CONVERT_DIR / f"{src.stem}_{stamp}.pdf"
    # 已转换过就直接用
    if dest.is_file():
        return dest, ""
    # 转换到临时目录再改名：LibreOffice 只能指定输出目录、不能指定文件名，
    # 直接输出到缓存目录会与「按时间戳命名」的约定冲突
    with _convert_lock, tempfile.TemporaryDirectory() as tmp:
        # 等锁期间别的请求可能已经转好了同一份，再查一次省下一次转换
        if dest.is_file():
            return dest, ""
        # 执行转换；超时与失败都要转成可读信息，不能让界面拿到 500
        try:
            proc = subprocess.run(
                [exe, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(src)],
                capture_output=True, text=True, timeout=CONVERT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return None, f"转换超时（超过 {CONVERT_TIMEOUT} 秒），该文档可能过大"
        except OSError as e:
            return None, f"调用 LibreOffice 失败：{e}"
        # 找出转换产物：LibreOffice 按源文件名生成，扩展名换成 pdf
        out = Path(tmp) / f"{src.stem}.pdf"
        # 产物不存在说明转换失败，把 stderr 带出来便于排查
        if not out.is_file():
            detail = (proc.stderr or proc.stdout or "").strip()[:200]
            return None, f"转换失败{('：' + detail) if detail else ''}"
        # 移入缓存目录
        shutil.move(str(out), str(dest))
    # 顺带清掉同一文档的旧版本，避免源文件反复更新后堆积
    for old in CONVERT_DIR.glob(f"{src.stem}_*.pdf"):
        # 保留本次结果
        if old != dest:
            # 删除失败忽略，下次还会再试
            try:
                old.unlink()
            except OSError:
                pass
    # 返回转换结果
    return dest, ""


def _soffice():
    """定位 LibreOffice 可执行文件，找不到返回 None。"""
    # 优先用 PATH 里的
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    # 找到即用
    if exe:
        return exe
    # macOS 上常见的应用内路径，装了 app 但没配 PATH 时也能用
    mac = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    # 存在即用
    return str(mac) if mac.is_file() else None


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
    # Office 文档先转 PDF 再裁图，转换结果有缓存，只有首次要等几秒
    src, why = viewable_pdf(src)
    # 转换不可用时如实回报原因，而不是返回一张空图
    if src is None:
        return {"ok": False, "reason": "not_pdf", "message": why}
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


def locate_chunk_text(case_id, filename, page_idx, text):
    """在 Office 转换出的 PDF 页面里按切片正文反查其大致区域，供无真实 bbox 的切片补高亮框。

    Office 格式的 MinerU 块本身没有版面坐标，只有页码（见模块头部说明）；但 LibreOffice
    转出的 PDF 通常保留真实文字层，可以用切片正文去该页面做文字匹配，反推出一个近似区域，
    而不必等 MinerU 哪天给 Office 格式补上真实坐标。

    做法：把正文按自然行拆开，取每行开头一小段做精确搜索——片段太长容易跨越 PDF 渲染时
    的折行断点（版式换行与正文的段落换行不是一回事）而搜不到，片段太短又容易在页面上
    命中不止一处、无法判断该用哪个，命中恰好一处才采信。多行命中取外接矩形，近似覆盖
    整段切片在页面上的范围；一行都没命中就如实返回失败，前端退回纯翻页，不猜测坐标。
    """
    # 复用与截图同一套原文定位逻辑，保证「关联了原文才能定位」这条前提一致
    src = find_source(case_id, filename)
    if src is None:
        return {"ok": False, "reason": "no_source", "message": "该样本尚未关联原始文件"}
    # Office 文档同样先转 PDF，转换结果与截图功能共用同一份缓存
    src, why = viewable_pdf(src)
    if src is None:
        return {"ok": False, "reason": "not_pdf", "message": why}
    # 延迟导入 pdfplumber，未触发反查的场景不承担其加载开销
    try:
        import pdfplumber
    except ImportError:
        return {"ok": False, "reason": "no_pdfplumber", "message": "环境缺少 pdfplumber，无法反查文字位置"}
    # 入参页码与 position_int 同口径，从 1 开始，这里转成 pdfplumber 要的 0 基下标
    idx = int(page_idx) - 1
    try:
        with pdfplumber.open(str(src)) as pdf:
            # 页码越界时明确回报，而不是让后续索引抛异常
            if idx < 0 or idx >= len(pdf.pages):
                return {"ok": False, "reason": "bad_page", "message": "页码超出范围"}
            page = pdf.pages[idx]
            # 收集每一行命中的区域，最后取外接矩形近似覆盖整段正文
            hits = []
            # 按切片正文的自然换行拆分成候选行
            lines = [ln.strip() for ln in (text or "").split("\n")]
            for line in lines:
                # 命中行数够用即停：整段正文可能有几十行，没必要每行都搜一遍
                if len(hits) >= LOCATE_MAX_LINES:
                    break
                # 取行首短片段，太长的整行大概率跨越 PDF 折行断点而搜不到
                snippet = line[:LOCATE_SNIPPET_LEN]
                # 片段太短（含空行）在页面上大概率不止命中一处，跳过以免选错区域
                if len(snippet) < LOCATE_SNIPPET_MIN_LEN:
                    continue
                try:
                    found = page.search(snippet, regex=False, case=True)
                except Exception:
                    # 单行搜索异常（如片段含 pdfplumber 不接受的字符）不影响其余行
                    continue
                # 零命中或命中多处都无法确定唯一区域，宁可跳过也不猜
                if len(found) != 1:
                    continue
                hits.append(found[0])
    except Exception as e:
        # 打开/搜索失败如实回报原因，而不是让界面拿到裸 500
        return {"ok": False, "reason": "render_failed", "message": f"{type(e).__name__}: {e}"}
    # 一行都没命中，说明提取出的文字与页面对不上（常见于艺术字、复杂版式）
    if not hits:
        return {"ok": False, "reason": "no_match",
                "message": "正文与页面文字对不上，可能是版式或艺术字导致提取的文字不一致"}
    # 外接矩形：取全部命中行坐标的并集
    x0 = min(h["x0"] for h in hits)
    x1 = max(h["x1"] for h in hits)
    top = min(h["top"] for h in hits)
    bottom = max(h["bottom"] for h in hits)
    # 返回格式与 render_chunk 的 positions 参数一致，前端可直接喂给同一套高亮逻辑
    return {"ok": True, "position": [int(page_idx), x0, x1, top, bottom]}
