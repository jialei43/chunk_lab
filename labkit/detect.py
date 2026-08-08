"""列表页结构自动识别：省去手工填字段路径与地址前缀。

手工填这些很折磨：要先把接口响应拉下来、翻 JSON 找哪个字段是文件路径、
再猜静态资源域名，猜错了还得反复试。这些都能从响应本身推断出来。

做法是「探测 + 验证」而非纯猜测：候选前缀会实际发一次 HEAD 请求，
确认真能下载到文件才回报，避免给出一个看着合理却下不动的配置。
"""

import logging  # 导入 logging 输出探测过程
import re  # 导入 re 匹配文件路径与翻页参数
from urllib.parse import urljoin, urlparse  # 导入 URL 处理工具

import requests  # 导入 requests 发起探测请求
from bs4 import BeautifulSoup  # 导入 BeautifulSoup 处理 HTML 分支

# 与爬虫一致的 UA，避免探测能过而实际抓取被拒
USER_AGENT = "chunk-lab-crawler/1.0 (document collection for offline chunking tests)"
# 探测请求超时，探测应当很快，卡住不如尽早失败
TIMEOUT = (8, 15)
# 视为目标文件的扩展名
FILE_EXTS = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx")
# 常见的翻页参数名，按常见程度排序
PAGE_PARAMS = ("pageNum", "pageNo", "page", "pageIndex", "p", "curPage", "currentPage")
# 递归遍历 JSON 的深度上限，防止异常结构导致爆栈
MAX_DEPTH = 8

log = logging.getLogger("detect")


def _looks_like_file(value):
    """判断字符串是否像一个文件地址。"""
    # 非字符串或过长的一律排除，正文字段可能很长
    if not isinstance(value, str) or not value or len(value) > 500:
        return False
    # 去掉查询串后看扩展名
    path = value.split("?")[0].split("#")[0].lower()
    # 命中已知扩展名即认为是文件
    return path.endswith(FILE_EXTS)


def _walk(node, path, out, depth=0):
    """递归遍历 JSON，收集看起来像文件地址的字段及其点号路径。"""
    # 超过深度上限时停止，避免异常结构导致爆栈
    if depth > MAX_DEPTH:
        return
    # 字典逐键下钻
    if isinstance(node, dict):
        for k, v in node.items():
            _walk(v, path + [k], out, depth + 1)
    # 数组对元素下钻，但路径不增加层级——
    # 数组在点号路径里是透明的，announcements.adjunctUrl 已能表达
    elif isinstance(node, list):
        # 只看前若干个元素，足以判断结构且避免大数组拖慢探测
        for item in node[:5]:
            _walk(item, path, out, depth + 1)
    # 叶子节点判断是否像文件地址
    elif _looks_like_file(node):
        # 以点号路径为键累计，同一字段会被多个元素命中
        key = ".".join(path)
        out.setdefault(key, {"count": 0, "sample": node})
        out[key]["count"] += 1


def _candidate_prefixes(sample, page_url):
    """为相对路径生成候选前缀，按可能性排序。"""
    # 已是绝对地址时不需要前缀
    if sample.startswith(("http://", "https://")):
        return [""]
    # 解析列表页地址，用于构造同域候选
    u = urlparse(page_url)
    host = u.netloc
    # 去掉 www. 得到主域，用于拼常见的静态资源子域
    bare = host[4:] if host.startswith("www.") else host
    # 候选按经验排序：静态子域最常见，其次同域根，最后按当前路径相对拼接
    return [
        f"{u.scheme}://static.{bare}",
        f"{u.scheme}://{host}",
        f"{u.scheme}://file.{bare}",
        f"{u.scheme}://download.{bare}",
        "",  # 空前缀表示按 urljoin 相对当前地址拼接
    ]


def _verify(prefix, sample, page_url, session):
    """验证某个前缀能否真的下载到文件。

    只发 HEAD 请求，不下载正文；HEAD 不被支持时退回 GET 但只读响应头。
    这一步是必要的——猜出来的前缀看着合理却下不动的情况很常见。
    """
    # 拼出完整地址
    if sample.startswith(("http://", "https://")):
        full = sample
    elif prefix:
        full = f"{prefix.rstrip('/')}/{sample.lstrip('/')}"
    else:
        full = urljoin(page_url, sample)
    # 网络异常一律视为该前缀不可用
    try:
        # 先试 HEAD，开销最小
        r = session.head(full, timeout=TIMEOUT, allow_redirects=True)
        # 部分站点不支持 HEAD，用 GET 但只取响应头
        if r.status_code >= 400:
            r = session.get(full, timeout=TIMEOUT, stream=True, allow_redirects=True)
            # 及时关闭，不读取正文
            r.close()
        # 2xx 即认为可用
        ok = 200 <= r.status_code < 300
        # 返回验证结果与实际地址，便于界面展示
        return ok, full, r.status_code, r.headers.get("Content-Type", "")
    except Exception as e:
        return False, full, 0, str(e)[:60]


def _build_page_url(url, name, page):
    """把翻页参数设为指定页码，已存在则替换、不存在则追加。"""
    # 已带该参数时直接替换数值
    if re.search(rf"[?&]{re.escape(name)}=\d+", url):
        return re.sub(rf"([?&]{re.escape(name)}=)\d+", rf"\g<1>{page}", url)
    # 否则追加，注意是否已有查询串
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{name}={page}"


def _page_fingerprint(data, field):
    """取一页内容的指纹，用于判断翻页是否真的换了内容。

    用文件地址集合而非整页哈希：接口常带时间戳、请求 ID 等易变字段，
    整页比对会把「内容没变」误判成「变了」。
    """
    # 收集该页的文件地址
    found = {}
    _walk(data, [], found)
    # 指定字段时只取该字段，否则取全部候选
    if field and field in found:
        return found[field]["sample"]
    # 退化情况：用所有候选的样本拼成指纹
    return "|".join(sorted(v["sample"] for v in found.values()))


def _detect_page_param(url, data, session=None, field=""):
    """推断并验证翻页参数名。

    只按名字猜是不够的：参数名对但站点不认、或者认了却返回同一页内容，
    都会让「翻页」变成反复抓第一页。因此逐个候选实际请求第二页，
    确认返回的文件与第一页不同才算数。
    """
    # 收集候选：地址里已带的参数最可靠，排在最前
    candidates = []
    # 地址中已出现的参数
    for name in PAGE_PARAMS:
        if re.search(rf"[?&]{re.escape(name)}=\d+", url):
            candidates.append(name)
    # 其次是 JSON 顶层出现的同名字段
    if isinstance(data, dict):
        for name in PAGE_PARAMS:
            if name in data and name not in candidates:
                candidates.append(name)
    # 最后补上常见名，用于地址里没带参数的情况
    for name in PAGE_PARAMS:
        if name not in candidates:
            candidates.append(name)

    # 无法发请求时退回「取第一个候选」，至少给出一个可用值
    if session is None:
        return candidates[0] if candidates else ""

    # 第一页的内容指纹，作为比对基准
    base = _page_fingerprint(data, field)
    # 逐个候选实际验证
    for name in candidates:
        # 构造第二页地址
        probe_url = _build_page_url(url, name, 2)
        # 请求失败或不是 JSON 都说明该候选不可用
        try:
            r = session.get(probe_url, timeout=TIMEOUT)
            # 非 2xx 说明站点不认这个参数
            if r.status_code != 200:
                continue
            page2 = r.json()
        except Exception:
            continue
        # 第二页的指纹
        fp2 = _page_fingerprint(page2, field)
        # 内容为空说明翻过头或参数无效
        if not fp2:
            continue
        # 与第一页不同才说明翻页确实生效
        if fp2 != base:
            log.info(f"翻页参数验证通过：{name}")
            return name
    # 全部候选都没能翻出不同内容，返回空而不是给个假的
    return ""


def detect(url):
    """探测列表页结构，返回可直接填入表单的配置。"""
    # 复用连接并声明 UA
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    # 请求失败时如实回报，便于区分「地址不对」与「结构识别不了」
    try:
        resp = session.get(url, timeout=TIMEOUT)
    except Exception as e:
        return {"ok": False, "message": f"请求失败：{e}"}
    # 非 2xx 直接返回状态码
    if resp.status_code != 200:
        return {"ok": False, "message": f"列表页返回 HTTP {resp.status_code}"}

    # 先判断是 JSON 接口还是 HTML 页面
    data = None
    # 解析失败即视为 HTML
    try:
        data = resp.json()
    except Exception:
        data = None

    # ---------- HTML 分支 ----------
    if data is None:
        # 用响应声明的编码解析，中文站点常见 GBK
        resp.encoding = resp.apparent_encoding or resp.encoding
        soup = BeautifulSoup(resp.text, "html.parser")
        # 统计直接指向文件的链接
        links = [urljoin(url, a["href"]) for a in soup.find_all("a", href=True)]
        files = [u for u in links if _looks_like_file(u)]
        # 有直接链接时无需额外配置
        if files:
            return {
                "ok": True, "mode": "html",
                "message": f"HTML 页面，直接命中 {len(files)} 个文件链接，无需额外配置",
                "config": {}, "samples": files[:5],
            }
        # 没有直接链接时，多半要进详情页，给出提示而非空结果
        return {
            "ok": True, "mode": "html",
            "message": f"HTML 页面，但本页没有直接的文件链接（共 {len(links)} 个链接）。"
                       f"文件可能在详情页里，请填写「详情页选择器」，"
                       f"或用浏览器开发者工具确认列表是否由接口加载",
            "config": {}, "samples": [],
        }

    # ---------- JSON 分支 ----------
    found = {}
    _walk(data, [], found)
    # 一个候选都没有说明该响应里没有文件地址
    if not found:
        return {
            "ok": True, "mode": "json",
            "message": "响应是 JSON，但没找到形似文件地址的字段。"
                       "可能需要换一个接口地址，或该接口只返回列表元数据",
            "config": {}, "samples": [],
        }

    # 命中次数多的字段更可能是正解，优先验证
    ranked = sorted(found.items(), key=lambda kv: -kv[1]["count"])
    # 逐个候选字段尝试，直到验证通过
    for field, info in ranked:
        sample = info["sample"]
        # 逐个候选前缀验证
        for prefix in _candidate_prefixes(sample, url):
            ok, full, code, ctype = _verify(prefix, sample, url, session)
            # 验证通过即返回该组合
            if ok:
                return {
                    "ok": True, "mode": "json",
                    "message": f"识别成功：字段 {field}，命中 {info['count']} 条，"
                               f"已验证可下载（{ctype or 'HTTP ' + str(code)}）",
                    "config": {
                        "json_field": field,  # 文件路径字段
                        "url_prefix": prefix,  # 地址前缀，空串表示相对拼接
                        # 翻页参数经实际请求第二页验证，确认内容确实变化
                        "page_param": _detect_page_param(url, data, session, field),
                    },
                    "samples": [full],
                }

    # 字段找到了但没有一个前缀能下载成功，如实说明而不是给个下不动的配置
    best_field, best_info = ranked[0]
    return {
        "ok": True, "mode": "json",
        "message": f"找到疑似字段 {best_field}（{best_info['count']} 条），"
                   f"但自动拼出的地址都无法下载。请手工填写「地址前缀」，"
                   f"示例路径：{best_info['sample']}",
        "config": {
            "json_field": best_field,  # 字段仍然填上，只差前缀
            "url_prefix": "",  # 前缀待人工确认
            # 翻页参数同样经实际验证
            "page_param": _detect_page_param(url, data, session, best_field),
        },
        "samples": [best_info["sample"]],
    }
