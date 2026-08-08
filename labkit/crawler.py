"""列表页文件爬虫：从一个列表页出发，自动翻页并下载目标格式的文件。

命令行与 Web 控制台共用本模块，避免两边各写一份而逐渐走样。

设计取向是「稳」而非「快」：抓取公开站点时，把对方站点搞出问题比慢几分钟
严重得多，因此默认串行、带延时、遵守 robots.txt，并且可断点续跑。

用法：

    # 最简：抓列表页上的 PDF
    ./run.sh crawl https://example.com/reports/

    # 翻页：给出翻页链接的文字或 URL 特征
    ./run.sh crawl https://example.com/reports/ --next-text "下一页" --max-pages 20

    # 详情页在中间：先进详情页再找附件
    ./run.sh crawl https://example.com/list/ --detail-selector "a.title" --ext pdf,docx

    # 先看会抓到什么，不真的下载
    ./run.sh crawl https://example.com/reports/ --dry-run

依赖 requests 与 beautifulsoup4，二者都在 ragflow 的虚拟环境里。
"""

import argparse  # 导入 argparse 解析命令行参数
import hashlib  # 导入 hashlib 为重名文件生成短后缀
import threading  # 导入 threading 实现速率限制与线程本地会话
from concurrent.futures import ThreadPoolExecutor, as_completed  # 导入线程池并行下载
import json  # 导入 json 读写断点续跑的进度文件
import logging  # 导入 logging 输出抓取过程
import random  # 导入 random 给延时加抖动，避免固定节奏的请求特征
import re  # 导入 re 清理文件名
import sys  # 导入 sys 控制退出码
import time  # 导入 time 实现请求间隔
from pathlib import Path  # 导入 Path 处理路径
from urllib.parse import unquote, urljoin, urlparse  # 导入 URL 处理工具
from urllib.robotparser import RobotFileParser  # 导入 robots.txt 解析器

import requests  # 导入 requests 发起 HTTP 请求
from bs4 import BeautifulSoup  # 导入 BeautifulSoup 解析 HTML

# 默认抓取的文件扩展名，覆盖常见的文档格式
DEFAULT_EXTS = "pdf,doc,docx,ppt,pptx,xls,xlsx"
# 默认请求间隔（秒）。不要调到 0：连续无间隔请求既容易被封，也会给对方站点造成压力
DEFAULT_DELAY = 1.0
# 单个文件的下载超时（秒），分别是连接超时与读取超时
DOWNLOAD_TIMEOUT = (10, 120)
# 页面请求超时
PAGE_TIMEOUT = (10, 30)
# 下载分块大小，边下边写避免大文件占满内存
CHUNK_SIZE = 64 * 1024
# 单个文件的大小上限，超过则跳过；防止误抓到超大文件把磁盘写满
DEFAULT_MAX_MB = 200
# 请求失败时的重试次数
MAX_RETRIES = 3
# 声明真实的 UA 并留下用途说明，便于站点管理员识别与联系，比伪装成浏览器更妥当
USER_AGENT = "chunk-lab-crawler/1.0 (document collection for offline chunking tests)"

# 默认并发下载数。
#
# 并发与速率是两件事，必须分开控制：并发提升传输吞吐，delay 限制请求频率。
# 若不分开，一开并发就等于把请求频率乘以并发数，很容易把对方站点打挂。
#
# 由此也决定了并发的适用范围——只有当「单文件传输耗时」明显大于 delay 时才有收益：
#   · 大文件（几十 MB）：传输占主导，并发接近线性提速
#   · 小文件或详情页多：瓶颈在请求发起，并发无效，调小 delay 才有用
# 实测 arxiv 列表页（50+ 详情页、6 个约 1MB 文件）：并发 4 与串行耗时相同。
DEFAULT_WORKERS = 4


class RateLimiter:
    """全局请求速率限制：保证任意两次请求的发起间隔不小于给定值。

    与并发数正交：并发 8 且间隔 1 秒，仍然是每秒最多发起 1 个请求，
    只是允许 8 个文件同时在传输。这正是既提速又不加压的关键。
    """

    def __init__(self, min_interval):
        # 两次请求之间的最小间隔
        self.min_interval = min_interval
        # 保护上次请求时间的锁，多线程共用同一个节流器
        self.lock = threading.Lock()
        # 上次放行的时间点，用单调时钟避免系统时间调整造成异常
        self.last = 0.0

    def acquire(self):
        """阻塞到允许发起下一次请求为止。"""
        # 持锁计算等待时间并更新时间戳，保证全局有序
        with self.lock:
            # 当前时刻
            now = time.monotonic()
            # 距离允许发起还差多久
            wait = self.last + self.min_interval - now
            # 未到时间就睡；抖动放在这里，避免固定节奏的请求特征
            if wait > 0:
                time.sleep(wait * random.uniform(0.85, 1.15))
            # 记录本次放行时间
            self.last = time.monotonic()


# 日志格式：时间 + 级别 + 消息
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("crawler")


class Crawler:
    """列表页爬虫。逐页解析、收集文件链接、下载，并记录进度以便续跑。"""

    def __init__(self, start_url, out_dir, exts, delay=DEFAULT_DELAY, max_pages=10,
                 max_files=0, max_mb=DEFAULT_MAX_MB, next_text=None, next_selector=None,
                 detail_selector=None, same_host=True, obey_robots=True, dry_run=False,
                 on_progress=None, workers=DEFAULT_WORKERS, link_pattern=None):
        # 起始列表页
        self.start_url = start_url
        # 下载目录
        self.out_dir = Path(out_dir)
        # 目标扩展名集合，统一小写并带点，便于比对
        self.exts = {("." + e.strip().lower().lstrip(".")) for e in exts.split(",") if e.strip()}
        # 链接正则：不少站点的下载地址不带扩展名（如 /pdf/2401.12345、download?id=9），
        # 只看扩展名会把它们全部漏掉，故允许额外用正则指定
        self.link_pattern = re.compile(link_pattern) if link_pattern else None
        # 请求间隔
        self.delay = delay
        # 最多翻多少页，防止无限翻页
        self.max_pages = max_pages
        # 最多下载多少个文件，0 表示不限
        self.max_files = max_files
        # 单文件大小上限（字节）
        self.max_bytes = max_mb * 1024 * 1024
        # 翻页链接的文字特征，如「下一页」
        self.next_text = next_text
        # 翻页链接的 CSS 选择器，优先级高于文字特征
        self.next_selector = next_selector
        # 详情页链接的选择器；给定时先进详情页再找附件
        self.detail_selector = detail_selector
        # 是否只抓同域链接，避免顺着外链爬到无关站点
        self.same_host = same_host
        # 是否遵守 robots.txt
        self.obey_robots = obey_robots
        # 只解析不下载
        self.dry_run = dry_run
        # 进度回调，Web 端据此展示实时状态；命令行不传则只写日志
        self.on_progress = on_progress
        # 并发下载数；1 即完全串行
        self.workers = max(1, int(workers))
        # 全局速率限制器，所有线程共用，保证请求频率不随并发数放大
        self.limiter = RateLimiter(delay)
        # 保护统计与已完成集合的锁，多线程会同时更新它们
        self.state_lock = threading.Lock()
        # 线程本地存储：requests.Session 并非完全线程安全，每线程各持一个更稳妥
        self.local = threading.local()

        # 会话按线程创建，见 session 属性
        # 起始域名，用于同域判断
        self.host = urlparse(start_url).netloc
        # 已访问过的页面，避免翻页成环时死循环
        self.seen_pages = set()
        # 已下载的文件 URL，配合进度文件实现断点续跑
        self.done = set()
        # 统计
        self.stats = {"pages": 0, "found": 0, "downloaded": 0, "skipped": 0, "failed": 0}
        # robots 解析器
        self.robots = None

    @property
    def session(self):
        """当前线程的 HTTP 会话。

        requests.Session 并非完全线程安全，多线程共用同一个可能出现连接池竞争，
        因此每个线程各持一个；连接复用的收益在单线程内部依然保留。
        """
        # 该线程尚未创建过会话时新建
        if not hasattr(self.local, "session"):
            s = requests.Session()
            s.headers["User-Agent"] = USER_AGENT
            self.local.session = s
        # 返回本线程的会话
        return self.local.session

    def bump(self, key, n=1):
        """线程安全地累加统计项。"""
        # 多线程会同时更新统计，必须加锁
        with self.state_lock:
            self.stats[key] = self.stats.get(key, 0) + n

    # ---------- 准备 ----------

    def load_robots(self):
        """读取并解析 robots.txt。

        抓取公开站点前先看对方是否允许，这是基本规矩；
        读取失败时按「未禁止」处理，但会记录下来。
        """
        # 未要求遵守时跳过
        if not self.obey_robots:
            log.warning("已按参数跳过 robots.txt 检查")
            return
        # 拼出 robots.txt 地址
        parsed = urlparse(self.start_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        # 解析失败不应阻断整个任务
        try:
            rp = RobotFileParser()
            rp.set_url(robots_url)
            rp.read()
            self.robots = rp
            log.info(f"已读取 {robots_url}")
        except Exception as e:
            log.warning(f"读取 robots.txt 失败（按未禁止处理）：{e}")

    def allowed(self, url):
        """判断某个 URL 是否被 robots.txt 允许抓取。"""
        # 没有 robots 信息时按允许处理
        if self.robots is None:
            return True
        # 交给标准库判断
        return self.robots.can_fetch(USER_AGENT, url)

    def load_progress(self):
        """读取进度文件，跳过已下载的文件，实现断点续跑。"""
        # 进度文件与下载目录同级
        path = self.out_dir / ".crawl_progress.json"
        # 不存在说明是首次运行
        if not path.is_file():
            return
        # 损坏时从头开始，不因此中断
        try:
            with path.open("r", encoding="utf-8") as fh:
                self.done = set(json.load(fh).get("done", []))
            log.info(f"已加载进度：{len(self.done)} 个文件此前已下载")
        except Exception as e:
            log.warning(f"进度文件损坏，将从头开始：{e}")

    def save_progress(self):
        """写回进度文件。"""
        # 确保目录存在
        self.out_dir.mkdir(parents=True, exist_ok=True)
        # 写出已完成清单
        with (self.out_dir / ".crawl_progress.json").open("w", encoding="utf-8") as fh:
            json.dump({"done": sorted(self.done)}, fh, ensure_ascii=False, indent=2)

    # ---------- 网络 ----------

    def report(self, message):
        """上报进度。命令行走日志，Web 端走回调。"""
        # 始终记录日志，便于事后排查
        log.info(message)
        # 有回调时同步给调用方；取统计快照避免回调看到半更新状态
        if self.on_progress:
            # 回调异常不应中断抓取
            try:
                with self.state_lock:
                    snapshot = dict(self.stats)
                self.on_progress(snapshot, message)
            except Exception:
                pass

    def sleep(self):
        """等待到允许发起下一次请求。

        走全局限速器而非各线程各睡各的：后者在并发下会让实际请求频率
        变成「并发数 ÷ 间隔」，与设定值完全不符。
        """
        # 由限速器统一节流
        self.limiter.acquire()

    def get(self, url, **kwargs):
        """带重试的 GET。失败按指数退避重试，仍失败则返回 None。"""
        # 逐次重试
        for attempt in range(1, MAX_RETRIES + 1):
            # 网络异常与 5xx 都值得重试，4xx 通常重试也没用
            try:
                resp = self.session.get(url, timeout=PAGE_TIMEOUT, **kwargs)
                # 服务端错误时重试
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                # 客户端错误直接返回，交由调用方记录
                return resp
            except Exception as e:
                # 最后一次仍失败则放弃
                if attempt == MAX_RETRIES:
                    log.error(f"请求失败（已重试 {MAX_RETRIES} 次）{url}：{e}")
                    return None
                # 指数退避，给对方站点恢复的时间
                wait = self.delay * (2 ** attempt)
                log.warning(f"第 {attempt} 次失败，{wait:.1f}s 后重试：{e}")
                time.sleep(wait)

    # ---------- 解析 ----------

    def is_target(self, url):
        """判断链接是否指向目标格式的文件。

        两条判据取并集：扩展名匹配，或命中用户给的正则。
        只靠扩展名会漏掉大量不带后缀的下载地址。
        """
        # 正则优先：命中即认定为目标，不再看扩展名
        if self.link_pattern and self.link_pattern.search(url):
            return True
        # 去掉查询串后看扩展名；不少下载链接带参数
        path = urlparse(url).path
        # 扩展名匹配即为目标
        return Path(unquote(path)).suffix.lower() in self.exts

    def same_site(self, url):
        """判断是否与起始页同域。"""
        # 未要求同域时一律放行
        if not self.same_host:
            return True
        # 空 netloc 属相对链接，join 后必然同域
        netloc = urlparse(url).netloc
        return (not netloc) or netloc == self.host

    def extract_links(self, soup, base_url, selector=None):
        """从页面中提取链接，返回绝对 URL 列表。"""
        # 指定选择器时只在其范围内找，避免把导航栏、页脚的链接也收进来
        anchors = soup.select(selector) if selector else soup.find_all("a")
        # 收集绝对地址
        out = []
        # 逐个处理
        for a in anchors:
            # 选择器可能选中的是 <a> 本身，也可能是其容器
            href = a.get("href") if a.name == "a" else (a.find("a") or {}).get("href")
            # 无 href 或锚点链接直接跳过
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            # 相对地址转绝对
            out.append(urljoin(base_url, href))
        # 返回链接列表
        return out

    def find_next_page(self, soup, base_url):
        """定位下一页链接。选择器优先，其次按链接文字匹配。"""
        # 选择器方式最可靠
        if self.next_selector:
            el = soup.select_one(self.next_selector)
            # 找到且有 href 才返回
            if el and el.get("href"):
                return urljoin(base_url, el["href"])
            return None
        # 其次按文字匹配，覆盖「下一页」「下页」「Next」等写法
        if self.next_text:
            for a in soup.find_all("a"):
                # 文字包含指定内容即认为是翻页链接
                if self.next_text in a.get_text(strip=True) and a.get("href"):
                    return urljoin(base_url, a["href"])
        # 未配置翻页规则或未找到
        return None

    # ---------- 下载 ----------

    def safe_filename(self, url, resp=None):
        """由 URL 与响应头推导安全的本地文件名。"""
        # 优先用 Content-Disposition 里的文件名，它通常比 URL 更可读
        name = ""
        # 响应头存在时尝试解析
        if resp is not None:
            cd = resp.headers.get("Content-Disposition", "")
            # 兼容 filename*=UTF-8''xxx 与 filename="xxx" 两种写法
            m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
            # 命中则解码
            if m:
                name = unquote(m.group(1))
        # 回退到 URL 路径的最后一段
        if not name:
            name = unquote(Path(urlparse(url).path).name)
        # 仍为空时用 URL 摘要兜底，保证一定有文件名
        if not name:
            name = "file_" + hashlib.sha1(url.encode()).hexdigest()[:10]
        # 清理路径分隔符与控制字符，防止写到目录外
        name = re.sub(r"[/\\\x00-\x1f]", "_", name).strip() or "file"
        # 过长的文件名在部分文件系统上会失败，截断但保留扩展名
        if len(name) > 120:
            stem, suffix = Path(name).stem[:100], Path(name).suffix[:20]
            name = stem + suffix
        # 返回安全文件名
        return name

    def download_guarded(self, url):
        """下载的线程安全包装：先节流，再下载，异常不外泄到线程池。"""
        # 每个文件请求前统一节流
        self.sleep()
        # 单个文件失败不应中断其它下载
        try:
            self.download(url)
        except Exception as e:
            log.error(f"下载异常 {url}：{e}")
            self.bump("failed")

    def download(self, url):
        """下载单个文件。已存在同名同源文件时跳过。"""
        # 断点续跑：此前已下载过就不重复；多线程读写故加锁
        with self.state_lock:
            already = url in self.done
        if already:
            self.bump("skipped")
            return
        # robots 禁止时不下载
        if not self.allowed(url):
            log.warning(f"robots.txt 不允许，跳过：{url}")
            self.bump("skipped")
            return
        # 演练模式只报告不下载
        if self.dry_run:
            log.info(f"[dry-run] 将下载 {url}")
            self.bump("found")
            return

        # 用流式请求，先看响应头再决定是否真的读取内容
        try:
            resp = self.session.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
        except Exception as e:
            log.error(f"下载失败 {url}：{e}")
            self.bump("failed")
            return
        # 非 200 视为失败
        if resp.status_code != 200:
            log.error(f"下载失败 {url}：HTTP {resp.status_code}")
            self.bump("failed")
            return
        # 超过大小上限时放弃，避免误抓超大文件
        size = int(resp.headers.get("Content-Length") or 0)
        if size and size > self.max_bytes:
            log.warning(f"超过 {self.max_bytes // 1024 // 1024}MB 上限，跳过：{url}")
            self.bump("skipped")
            return

        # 推导文件名并处理重名
        self.out_dir.mkdir(parents=True, exist_ok=True)
        name = self.safe_filename(url, resp)
        dest = self.out_dir / name
        # 重名时附加 URL 摘要，既避免覆盖又保持可追溯
        if dest.exists():
            digest = hashlib.sha1(url.encode()).hexdigest()[:6]
            dest = self.out_dir / f"{Path(name).stem}_{digest}{Path(name).suffix}"

        # 边下边写，避免大文件占满内存；中途失败要清理半截文件
        written = 0
        try:
            with dest.open("wb") as fh:
                for chunk in resp.iter_content(CHUNK_SIZE):
                    # 空块跳过
                    if not chunk:
                        continue
                    fh.write(chunk)
                    written += len(chunk)
                    # 边下边校验大小，处理没有 Content-Length 的情况
                    if written > self.max_bytes:
                        raise IOError(f"超过 {self.max_bytes // 1024 // 1024}MB 上限")
        except Exception as e:
            # 半截文件毫无价值，且会让人误以为下载成功
            dest.unlink(missing_ok=True)
            log.error(f"下载中断 {url}：{e}")
            self.bump("failed")
            return

        # 记录完成；多线程写入故加锁
        with self.state_lock:
            self.done.add(url)
        self.bump("downloaded")
        self.report(f"已下载 {dest.name}（{written / 1024:.0f} KB）")

    # ---------- 主流程 ----------

    def crawl_page(self, url):
        """抓取一个列表页：收集文件链接、按需进详情页，返回下一页地址。"""
        # 翻页成环时避免重复抓取
        if url in self.seen_pages:
            log.info("该页已抓过，停止翻页（可能是翻页链接成环）")
            return None
        self.seen_pages.add(url)
        # robots 禁止时不抓
        if not self.allowed(url):
            log.warning(f"robots.txt 不允许，跳过页面：{url}")
            return None

        self.report(f"抓取列表页：{url}")
        resp = self.get(url)
        # 请求失败则终止本条链路
        if resp is None or resp.status_code != 200:
            log.error(f"列表页不可用：{url}")
            return None
        self.bump("pages")
        # 用响应声明的编码解析，中文站点常见 GBK
        resp.encoding = resp.apparent_encoding or resp.encoding
        soup = BeautifulSoup(resp.text, "html.parser")

        # 先收集本页直接指向文件的链接
        targets = [u for u in self.extract_links(soup, url)
                   if self.is_target(u) and self.same_site(u)]

        # 配置了详情页选择器时，进入详情页再找附件
        if self.detail_selector:
            details = [u for u in self.extract_links(soup, url, self.detail_selector)
                       if self.same_site(u) and self.allowed(u)]
            self.report(f"本页 {len(details)} 个详情页，并发 {self.workers} 抓取")

            # 抓取单个详情页并返回其中的目标文件链接
            def fetch_detail(d):
                # 每次请求前统一节流，保证全局频率不随并发放大
                self.sleep()
                sub = self.get(d)
                # 详情页取不到就跳过，不影响其它条目
                if sub is None or sub.status_code != 200:
                    return []
                sub.encoding = sub.apparent_encoding or sub.encoding
                sub_soup = BeautifulSoup(sub.text, "html.parser")
                # 收集该详情页里的目标文件
                return [u for u in self.extract_links(sub_soup, d)
                        if self.is_target(u) and self.same_site(u)]

            # 并行抓取详情页；它们彼此独立，是并发收益最明显的一段
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = [pool.submit(fetch_detail, d) for d in details]
                # 逐个收取结果；单个详情页失败不影响整体
                for fut in as_completed(futures):
                    try:
                        targets += fut.result()
                    except Exception as e:
                        log.warning(f"详情页抓取异常：{e}")

        # 去重，保持原顺序便于对照页面
        seen = set()
        ordered = [u for u in targets if not (u in seen or seen.add(u))]
        # 有文件数上限时先截断，避免并发下超量下载
        if self.max_files:
            with self.state_lock:
                remain = max(0, self.max_files - self.stats["downloaded"])
            ordered = ordered[:remain]
        self.report(f"本页命中 {len(ordered)} 个目标文件，并发 {self.workers} 下载")

        # 并行下载。请求发起仍由全局限速器串行节流，并发提升的是传输吞吐——
        # 大文件的耗时几乎都在传输上，因此这里的收益最直接。
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self.download_guarded, u) for u in ordered]
            # 等待全部完成；异常已在内部处理，这里只兜底
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    log.error(f"下载线程异常：{e}")
        # 每页结束后保存进度，中断后可续跑
        self.save_progress()
        # 返回下一页地址
        return self.find_next_page(soup, url)

    def run(self):
        """执行抓取。"""
        # 记录起始时间，用于报告实际耗时
        started = time.monotonic()
        # 准备 robots 与断点信息
        self.load_robots()
        self.load_progress()
        # 从起始页开始逐页抓取
        url = self.start_url
        page = 0
        # 翻页直到没有下一页或达到上限
        while url and page < self.max_pages:
            page += 1
            self.report(f"第 {page}/{self.max_pages} 页")
            url = self.crawl_page(url)
            # 达到文件数上限时不再翻页
            if self.max_files and self.stats["downloaded"] >= self.max_files:
                break
        # 收尾保存
        self.save_progress()
        # 打印统计
        log.info("—— 完成 ——")
        elapsed = time.monotonic() - started
        log.info(f"列表页 {self.stats['pages']}　下载 {self.stats['downloaded']}　"
                 f"跳过 {self.stats['skipped']}　失败 {self.stats['failed']}　"
                 f"耗时 {elapsed:.1f}s　并发 {self.workers}")
        log.info(f"文件位于：{self.out_dir.resolve()}")
        # 有失败时以非零码退出，便于脚本串联时感知
        return 1 if self.stats["failed"] else 0


def main(argv=None):
    """命令行入口。"""
    p = argparse.ArgumentParser(
        description="从列表页翻页抓取文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法：")[1] if "用法：" in __doc__ else "")
    # 起始列表页
    p.add_argument("url", help="起始列表页 URL")
    # 下载目录
    p.add_argument("-o", "--out", default=None, help="下载目录，默认为数据目录下的 downloads/")
    # 目标扩展名
    p.add_argument("--ext", default=DEFAULT_EXTS, help=f"目标扩展名，逗号分隔，默认 {DEFAULT_EXTS}")
    p.add_argument("--link-pattern",
                   help="下载链接的正则，用于识别不带扩展名的地址，如 '/pdf/' 或 'download\\?id='")
    # 翻页规则
    p.add_argument("--next-text", help="翻页链接的文字，如「下一页」")
    p.add_argument("--next-selector", help="翻页链接的 CSS 选择器，优先于 --next-text")
    # 详情页规则
    p.add_argument("--detail-selector", help="详情页链接的 CSS 选择器；给定时先进详情页再找附件")
    # 限额
    p.add_argument("--max-pages", type=int, default=10, help="最多翻多少页，默认 10")
    p.add_argument("--max-files", type=int, default=0, help="最多下载多少个文件，0 为不限")
    p.add_argument("--max-mb", type=int, default=DEFAULT_MAX_MB, help=f"单文件大小上限 MB，默认 {DEFAULT_MAX_MB}")
    # 节奏
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                   help=f"两次请求的全局最小间隔秒数，默认 {DEFAULT_DELAY}。与并发数无关，"
                        f"并发再高请求频率也不超过它")
    p.add_argument("-w", "--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"并发下载数，默认 {DEFAULT_WORKERS}。提升的是传输吞吐而非请求频率；"
                        f"设为 1 即完全串行")
    # 范围与合规
    p.add_argument("--cross-host", action="store_true", help="允许抓取外域链接，默认只抓同域")
    p.add_argument("--ignore-robots", action="store_true",
                   help="忽略 robots.txt。仅在你确认有权抓取该站点时使用")
    # 演练
    p.add_argument("--dry-run", action="store_true", help="只解析并列出会下载什么，不实际下载")
    args = p.parse_args(argv)

    # 未指定下载目录时用数据目录下的 downloads，与其它数据一致放在仓库之外
    out = args.out
    if not out:
        from .paths import DATA_ROOT
        out = DATA_ROOT / "downloads"
    # 组装爬虫并运行
    crawler = Crawler(
        args.url,  # 起始页
        out,  # 下载目录
        args.ext,  # 目标扩展名
        delay=args.delay,  # 请求间隔
        max_pages=args.max_pages,  # 翻页上限
        max_files=args.max_files,  # 文件数上限
        max_mb=args.max_mb,  # 单文件大小上限
        next_text=args.next_text,  # 翻页文字
        next_selector=args.next_selector,  # 翻页选择器
        detail_selector=args.detail_selector,  # 详情页选择器
        same_host=not args.cross_host,  # 是否限制同域
        obey_robots=not args.ignore_robots,  # 是否遵守 robots
        dry_run=args.dry_run,  # 是否只演练
        workers=args.workers,  # 并发下载数
        link_pattern=args.link_pattern,  # 下载链接正则
    )
    return crawler.run()


# 作为脚本直接运行时执行 main
if __name__ == "__main__":
    sys.exit(main())
