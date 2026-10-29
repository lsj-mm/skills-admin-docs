"""
监控上一级文件夹，新放入的文件/软件会自动登记到其中 工具说明.md 的表格里。
在 config.json 里填入百度翻译 APPID 和密钥后，英文简介会译成中文；不填则保留原文。

用法:
    python update_tool_docs.py              # 扫描一次并更新文档
    python update_tool_docs.py --watch      # 持续监控（前台）
    python update_tool_docs.py --install    # 设置开机自启，并立即在后台启动
    python update_tool_docs.py --uninstall  # 取消开机自启，并停止后台进程
    python update_tool_docs.py --stop       # 仅停止后台进程

程序、配置（config.json）、日志（run.log）都放在本程序所在的文件夹中，该文件夹不会被登记。
已有的行不会被覆盖，可以放心手动修改说明和链接。
"""

import argparse
import ctypes
import hashlib
import json
import logging
import os
import random
import re
import struct
import sys
import time
import unicodedata
from logging.handlers import RotatingFileHandler

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
if IS_WIN:
    from ctypes import wintypes

SCRIPT_PATH = os.path.abspath(__file__)
APP_DIR = os.path.dirname(SCRIPT_PATH)
BASE_DIR = os.path.dirname(APP_DIR)
DOC_NAME = "工具说明.md"
DOC_PATH = os.path.join(BASE_DIR, DOC_NAME)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
LOG_PATH = os.path.join(APP_DIR, "run.log")
PID_PATH = os.path.join(APP_DIR, "watcher.pid")
HEADERS = ["名称", "简要说明", "github链接/官网链接"]
PLACEHOLDER = "待补充"
AUTORUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTORUN_NAME = "SkillsAdminToolDocs"
SERVICE_NAME = "skills-admin-tool-docs"
LAUNCHD_LABEL = "com.skillsadmin.tooldocs"
INSTALLER_SUFFIXES = (".exe", ".dll", ".msi", ".dmg", ".pkg", ".app", ".appimage", ".deb", ".rpm")
PE_KEYS = ("ProductName", "FileDescription", "CompanyName", "Comments", "LegalCopyright")

IGNORED_NAMES = {DOC_NAME, os.path.basename(APP_DIR), "desktop.ini", "Thumbs.db", "__pycache__"}
IGNORED_SUFFIXES = (".tmp", ".part", ".crdownload", ".download", ".partial", "~")
README_NAMES = ("readme.md", "readme_zh.md", "readme.zh-cn.md", "readme.rst", "readme.txt", "readme")
GITHUB_RE = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)", re.I)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
MAX_DESC_LEN = 80

DEFAULT_CONFIG = {
    "use_net": True,
    "translate": True,
    "baidu_appid": "",
    "baidu_key": "",
    "remove_missing": True,
}

log = logging.getLogger("tool_docs")


def setup_logging():
    log.setLevel(logging.INFO)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=256 * 1024, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    if sys.stdout is not None and sys.stdout.isatty():
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        log.addHandler(logging.StreamHandler(sys.stdout))


def load_config():
    config = dict(DEFAULT_CONFIG)
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return config
    try:
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            config.update(json.load(f))
    except (OSError, ValueError) as e:
        log.warning(f"[警告] 配置文件读取失败，使用默认配置: {e}")
    return config


# ---------------------------------------------------------------- 文档读写

def display_width(text):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text, width):
    return text + " " * (width - display_width(text))


def is_table_line(line):
    return line.strip().startswith("|")


def parse_row(line):
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return (cells + [""] * 3)[:3]


def load_doc():
    """返回 (表格前的行, 表格行列表, 表格后的行)。"""
    if not os.path.exists(DOC_PATH):
        return ["工具说明", ""], [], []
    with open(DOC_PATH, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()

    start = next((i for i, l in enumerate(lines) if is_table_line(l)), None)
    if start is None:
        return lines + [""], [], []
    end = start
    while end < len(lines) and is_table_line(lines[end]):
        end += 1

    rows = []
    for line in lines[start + 2:end]:  # 跳过表头和分隔行
        row = parse_row(line)
        if row[0]:
            rows.append(row)
    return lines[:start], rows, lines[end:]


def render_table(rows):
    all_rows = [HEADERS] + rows
    widths = [max(display_width(r[i]) for r in all_rows) for i in range(3)]

    def fmt(r):
        return "| " + " | ".join(pad(c, w) for c, w in zip(r, widths)) + " |"

    sep = "| " + " | ".join("-" * w for w in widths) + " |"
    return [fmt(HEADERS), sep] + [fmt(r) for r in rows]


def save_doc(before, rows, after):
    content = "\n".join(before + render_table(rows) + after).rstrip("\n") + "\n"
    with open(DOC_PATH, "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------- 信息提取

def clean_text(text):
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`>#]", "", text)
    text = text.replace("|", "/")
    return re.sub(r"\s+", " ", text).strip()


def shorten(text):
    text = clean_text(text)
    if display_width(text) <= MAX_DESC_LEN:
        return text
    for sep in ("。", ". ", "！", "；", "; "):
        idx = text.find(sep)
        if 0 < idx and display_width(text[:idx]) <= MAX_DESC_LEN:
            return text[:idx + 1].strip()
    out = ""
    for c in text:
        if display_width(out + c) > MAX_DESC_LEN - 3:
            break
        out += c
    return out + "..."


def readme_summary(text):
    """取 README 中第一段正文作为简介。"""
    paragraph = []
    in_code = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if not line:
            if paragraph:
                break
            continue
        skip = (
            line.startswith(("#", "!", "[!", "<", "|", "---", "===", "*", "-", "```"))
            or re.fullmatch(r"(\[!\[.*?\]\(.*?\)\]\(.*?\)\s*)+", line)
        )
        if skip and not paragraph:
            continue
        cleaned = clean_text(line)
        if len(cleaned) < 2:
            if paragraph:
                break
            continue
        paragraph.append(cleaned)
    return " ".join(paragraph)


def readme_title_desc(text):
    """有些 README 把简介写在一级标题之后的 blockquote 里。"""
    m = re.search(r"^>\s*(.+)$", text, re.M)
    return clean_text(m.group(1)) if m else ""


def find_github_link(texts, name_hint):
    hint = re.sub(r"[-_](main|master)$", "", name_hint.lower())
    for text in texts:
        for owner, repo in GITHUB_RE.findall(text or ""):
            repo = re.sub(r"\.git$", "", repo)
            if owner.lower() in ("sponsors", "orgs", "topics", "features", "apps", "marketplace"):
                continue
            if repo.lower() == hint:
                return f"https://github.com/{owner}/{repo}"
    return ""


def info_from_project_files(files, name_hint):
    """files: {相对路径(小写): 内容}，只含项目根目录下的文件。"""
    desc, link = "", ""

    pkg = files.get("package.json")
    if pkg:
        try:
            data = json.loads(pkg)
            desc = data.get("description", "") or desc
            repo = data.get("repository")
            repo_url = repo.get("url", "") if isinstance(repo, dict) else (repo or "")
            m = GITHUB_RE.search(repo_url)
            if m:
                link = f"https://github.com/{m.group(1)}/{re.sub(r'.git$', '', m.group(2))}"
            elif isinstance(repo, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
                link = f"https://github.com/{repo}"
            link = link or data.get("homepage", "")
        except (ValueError, AttributeError):
            pass

    for key in ("pyproject.toml", "setup.cfg", "cargo.toml"):
        text = files.get(key)
        if text:
            m = re.search(r'^\s*description\s*=\s*["\']?(.+?)["\']?\s*$', text, re.M)
            if m and not desc:
                desc = m.group(1)

    m = GITHUB_RE.search(files.get(".git/config", ""))
    if m and not link:
        link = f"https://github.com/{m.group(1)}/{re.sub(r'.git$', '', m.group(2))}"

    readme = next((files[n] for n in README_NAMES if n in files), "")
    if readme:
        desc = desc or readme_summary(readme) or readme_title_desc(readme)
        link = link or find_github_link([readme] + list(files.values()), name_hint)

    return desc, link


def read_archive_root_files(path):
    """读取压缩包中项目根目录（去掉统一的顶层文件夹）的关键文件。"""
    import tarfile
    import zipfile

    wanted = set(README_NAMES) | {"package.json", "pyproject.toml", "setup.cfg", "cargo.toml", ".git/config"}
    result = {}

    def take(names, reader):
        top = {n.split("/", 1)[0] for n in names if n}
        prefix = (top.pop() + "/") if len(top) == 1 and any("/" in n for n in names) else ""
        for n in names:
            rel = n[len(prefix):] if n.startswith(prefix) else n
            if rel.lower() in wanted:
                try:
                    result[rel.lower()] = reader(n).decode("utf-8", "ignore")
                except Exception:
                    pass

    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                take(z.namelist(), z.read)
        elif tarfile.is_tarfile(path):
            with tarfile.open(path) as t:
                members = {m.name: m for m in t.getmembers() if m.isfile()}
                take(list(members), lambda n: t.extractfile(members[n]).read())
    except Exception:
        pass
    return result


def read_dir_root_files(path):
    result = {}
    for entry in os.listdir(path):
        full = os.path.join(path, entry)
        if os.path.isfile(full) and (entry.lower() in README_NAMES or entry.lower() in
                                     ("package.json", "pyproject.toml", "setup.cfg", "cargo.toml")):
            with open(full, encoding="utf-8", errors="ignore") as f:
                result[entry.lower()] = f.read()
    git_cfg = os.path.join(path, ".git", "config")
    if os.path.isfile(git_cfg):
        with open(git_cfg, encoding="utf-8", errors="ignore") as f:
            result[".git/config"] = f.read()
    return result


def pe_version_info(path):
    """不依赖 Windows API，直接在 exe/dll 文件中查找版本信息字符串（Linux / macOS 使用）。"""
    import mmap
    info = {}
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            for key in PE_KEYS:
                pattern = key.encode("utf-16-le") + b"\0\0"
                end_bound = len(m)
                while True:
                    idx = m.rfind(pattern, 0, end_bound)
                    if idx < 6:
                        break
                    end_bound = idx
                    if m[idx - 2:idx] != b"\x01\x00":  # String 结构的 wType 必须为 1（文本）
                        continue
                    pos = idx + len(pattern)
                    pos += (4 - pos % 4) % 4
                    end = pos
                    while end + 1 < len(m) and m[end:end + 2] != b"\0\0" and end - pos < 1024:
                        end += 2
                    value = m[pos:end].decode("utf-16-le", "ignore").strip()
                    if value:
                        info[key] = value
                    break
    except (OSError, ValueError):
        pass
    return info


def app_bundle_info(path):
    """读取 macOS .app 包的 Info.plist。"""
    import plistlib
    try:
        with open(os.path.join(path, "Contents", "Info.plist"), "rb") as f:
            data = plistlib.load(f)
    except Exception:
        return {}
    return {
        "ProductName": data.get("CFBundleDisplayName") or data.get("CFBundleName", ""),
        "FileDescription": data.get("CFBundleGetInfoString", ""),
        "LegalCopyright": data.get("NSHumanReadableCopyright", ""),
    }


def deb_info(path):
    """读取 .deb 安装包 control 文件中的 Description 和 Homepage。"""
    import io
    import tarfile
    try:
        with open(path, "rb") as f:
            if f.read(8) != b"!<arch>\n":
                return {}
            while True:
                header = f.read(60)
                if len(header) < 60:
                    return {}
                name = header[:16].decode().strip().rstrip("/")
                size = int(header[48:58].decode().strip())
                if name.startswith("control.tar"):
                    data = f.read(size)
                    break
                f.seek(size + size % 2, 1)
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            member = next(m for m in t.getmembers() if m.name.lstrip("./") == "control")
            control = t.extractfile(member).read().decode("utf-8", "ignore")
    except Exception:
        return {}
    fields = dict(re.findall(r"^(\w[\w-]*):\s*(.*)$", control, re.M))
    return {"ProductName": fields.get("Package", ""), "Comments": fields.get("Description", ""),
            "Homepage": fields.get("Homepage", "")}


def exe_version_info(path):
    """读取 Windows 可执行文件的版本信息（产品名、描述、公司等）。"""
    if not IS_WIN:
        return pe_version_info(path)
    ver = ctypes.WinDLL("version")
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return {}
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, buf):
        return {}

    ptr = ctypes.c_void_p()
    length = wintypes.UINT()
    langs = []
    if ver.VerQueryValueW(buf, r"\VarFileInfo\Translation", ctypes.byref(ptr), ctypes.byref(length)):
        arr = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD * (length.value // 2))).contents
        langs = [f"{arr[i]:04x}{arr[i + 1]:04x}" for i in range(0, len(arr), 2)]
    langs += ["040904b0", "080404b0", "040904e4"]

    info = {}
    for key in PE_KEYS:
        for lang in langs:
            if ver.VerQueryValueW(buf, f"\\StringFileInfo\\{lang}\\{key}", ctypes.byref(ptr), ctypes.byref(length)) \
                    and length.value:
                value = ctypes.wstring_at(ptr.value, length.value).rstrip("\x00").strip()
                if value:
                    info[key] = value
                    break
    return info


# ---------------------------------------------------------------- 联网补充

def http_get(url, timeout=8):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 tool-docs-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def github_description(link):
    m = GITHUB_RE.match(link)
    if not m:
        return ""
    try:
        data = json.loads(http_get(f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}"))
        return data.get("description") or ""
    except Exception:
        return ""


def search_official_site(query):
    """用 DuckDuckGo 搜索，返回第一个结果链接（仅作参考，可能不准）。"""
    import urllib.parse
    try:
        html = http_get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query + " official site"))
    except Exception:
        return ""
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', html):
        if "uddg=" in href:
            href = urllib.parse.unquote(re.search(r"uddg=([^&]+)", href).group(1))
        if href.startswith("http") and "duckduckgo.com" not in href:
            parsed = urllib.parse.urlparse(href)
            return f"{parsed.scheme}://{parsed.netloc}/"
    return ""


# ---------------------------------------------------------------- 翻译

def needs_translation(text):
    return text and text != PLACEHOLDER and not CJK_RE.search(text) and re.search(r"[A-Za-z]{2}", text)


def baidu_ready(config):
    return bool(config.get("baidu_appid") and config.get("baidu_key"))


def translate_baidu(text, appid, key):
    import urllib.parse
    salt = str(random.randint(10000, 99999))
    sign = hashlib.md5((appid + text + salt + key).encode("utf-8")).hexdigest()
    query = urllib.parse.urlencode({"q": text, "from": "auto", "to": "zh", "appid": appid, "salt": salt, "sign": sign})
    data = json.loads(http_get("https://fanyi-api.baidu.com/api/trans/vip/translate?" + query))
    if "error_code" in data:
        raise RuntimeError(f"百度翻译错误 {data['error_code']}: {data.get('error_msg', '')}")
    return "".join(item["dst"] for item in data["trans_result"])


TRANSLATE_RETRY_SECONDS = 1800
_translate_failed_at = {}


def translate(text, config):
    """用百度翻译成中文，失败则返回 None（同一文本失败后 30 分钟内不再重试）。"""
    if not baidu_ready(config):
        return None
    if time.time() - _translate_failed_at.get(text, 0) < TRANSLATE_RETRY_SECONDS:
        return None
    try:
        result = clean_text(translate_baidu(text, config["baidu_appid"], config["baidu_key"]))
        if result:
            return result
    except Exception as e:
        log.warning(f"[翻译] {e}")
    _translate_failed_at[text] = time.time()
    return None


# ---------------------------------------------------------------- 主流程

def describe(name, use_net):
    path = os.path.join(BASE_DIR, name)
    stem = os.path.splitext(name)[0]
    desc, link = "", ""

    lower = name.lower()
    if os.path.isdir(path) and not lower.endswith(".app"):
        desc, link = info_from_project_files(read_dir_root_files(path), name)
    elif lower.endswith((".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
        desc, link = info_from_project_files(read_archive_root_files(path), re.sub(r"\.tar$", "", stem))
    elif lower.endswith(INSTALLER_SUFFIXES):
        if lower.endswith((".exe", ".dll")):
            info = exe_version_info(path)
        elif lower.endswith(".app"):
            info = app_bundle_info(path)
        elif lower.endswith(".deb"):
            info = deb_info(path)
        else:
            info = {}
        guessed = re.split(r"[-_ ]v?\d", stem, maxsplit=1)[0].strip()
        product = info.get("ProductName") or info.get("FileDescription") or guessed
        comments = info.get("Comments", "")
        desc = (comments if not re.match(r"https?://", comments) else "") or info.get("FileDescription") or ""
        if desc and product and desc.lower().replace("-", " ") == product.lower().replace("-", " "):
            desc = f"{product}（{info.get('CompanyName', '')}）".replace("（）", "")
        link = (re.search(r"https?://\S+", " ".join(info.values())) or [""])[0]
        if use_net and not link and product:
            link = search_official_site(product)

    if use_net and link and (not desc or re.fullmatch(r"[\x00-\x7f]*", desc) and len(desc) > 120):
        desc = github_description(link) or desc

    return shorten(desc) or PLACEHOLDER, link or PLACEHOLDER


def current_items():
    items = []
    for name in sorted(os.listdir(BASE_DIR)):
        if name in IGNORED_NAMES or name.startswith((".", "~$")) or name.lower().endswith(IGNORED_SUFFIXES):
            continue
        items.append(name)
    return items


def update(config):
    use_net = config["use_net"]
    before, rows, after = load_doc()
    items = current_items()
    known = {r[0] for r in rows}
    changed = False

    if config["remove_missing"]:
        kept = [r for r in rows if r[0] in items]
        for r in rows:
            if r not in kept:
                log.info(f"[移除] {r[0]}（文件已不在文件夹中）")
        changed = len(kept) != len(rows)
        rows = kept

    for name in items:
        if name in known:
            continue
        desc, link = describe(name, use_net)
        rows.append([name, desc, link])
        changed = True
        log.info(f"[新增] {name} | {desc} | {link}")

    if use_net and config["translate"] and baidu_ready(config):
        for row in rows:
            if needs_translation(row[1]):
                zh = translate(row[1], config)
                if zh:
                    log.info(f"[翻译] {row[0]}: {row[1]} -> {zh}")
                    row[1] = shorten(zh)
                    changed = True

    if changed or not os.path.exists(DOC_PATH):
        save_doc(before, rows, after)
        log.info(f"已更新 {DOC_NAME}")
    return changed


def snapshot():
    state = {}
    for name in current_items():
        try:
            st = os.stat(os.path.join(BASE_DIR, name))
            state[name] = (st.st_size, st.st_mtime)
        except OSError:
            pass
    return state


def wait_until_stable(interval=2):
    """等文件复制完成：两次检查大小和修改时间都不变。"""
    last = snapshot()
    while True:
        time.sleep(interval)
        now = snapshot()
        if now == last:
            return
        last = now


def is_relevant(name):
    first = name.split("\\", 1)[0]
    return not (first in IGNORED_NAMES or first.startswith((".", "~$")) or first.lower().endswith(IGNORED_SUFFIXES))


def directory_events():
    """阻塞等待目录变化（系统原生通知，空闲时不占 CPU），每次有相关变化时 yield。"""
    if IS_WIN:
        return windows_directory_events()
    if IS_LINUX:
        return linux_directory_events()
    if IS_MAC:
        return mac_directory_events()
    raise OSError("当前系统不支持目录通知")


def linux_directory_events():
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    fd = libc.inotify_init1(0o2000000)  # IN_CLOEXEC
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1 失败")
    # IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF
    mask = 0x8 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800
    if libc.inotify_add_watch(fd, os.fsencode(BASE_DIR), mask) < 0:
        err = ctypes.get_errno()
        os.close(fd)
        raise OSError(err, "inotify_add_watch 失败")
    try:
        while True:
            data = os.read(fd, 64 * 1024)
            offset, relevant = 0, False
            while offset + 16 <= len(data):
                _wd, ev_mask, _cookie, length = struct.unpack_from("iIII", data, offset)
                name = data[offset + 16:offset + 16 + length].rstrip(b"\0").decode("utf-8", "ignore")
                offset += 16 + length
                if ev_mask & (0x400 | 0x800):
                    raise OSError("被监控的文件夹已被删除或移动")
                if ev_mask & 0x4000 or is_relevant(name):  # IN_Q_OVERFLOW 时全量检查
                    relevant = True
            if relevant:
                yield
    finally:
        os.close(fd)


def mac_directory_events():
    import select
    fd = os.open(BASE_DIR, os.O_RDONLY | getattr(os, "O_EVTONLY", 0x8000))
    kq = select.kqueue()
    try:
        kq.control([select.kevent(
            fd, filter=select.KQ_FILTER_VNODE, flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME,
        )], 0, 0)
        while True:
            for event in kq.control(None, 1, None):
                if event.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                    raise OSError("被监控的文件夹已被删除或移动")
            yield
    finally:
        kq.close()
        os.close(fd)


def windows_directory_events():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
                                          wintypes.LPVOID]
    handle = k32.CreateFileW(BASE_DIR, 0x0001, 0x7, None, 3, 0x02000000, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())

    notify_filter = 0x1 | 0x2 | 0x8 | 0x10  # 文件名、目录名、大小、修改时间
    buf = ctypes.create_string_buffer(16 * 1024)
    returned = wintypes.DWORD()
    try:
        while True:
            if not k32.ReadDirectoryChangesW(handle, buf, len(buf), False, notify_filter,
                                             ctypes.byref(returned), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
            if returned.value == 0:  # 缓冲区溢出，变化太多，直接全量检查
                yield
                continue
            offset, relevant = 0, False
            while True:
                next_offset, _action, length = struct.unpack_from("III", buf, offset)
                name = buf.raw[offset + 12:offset + 12 + length].decode("utf-16-le")
                relevant = relevant or is_relevant(name)
                if not next_offset:
                    break
                offset += next_offset
            if relevant:
                yield
    finally:
        k32.CloseHandle(handle)


def current_process():
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    return wintypes.HANDLE(k32.GetCurrentProcess())


def trim_memory():
    if IS_WIN:
        ctypes.windll.kernel32.SetProcessWorkingSetSize(current_process(), ctypes.c_size_t(-1), ctypes.c_size_t(-1))
    elif IS_LINUX:
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except (OSError, AttributeError):
            pass


def run_update_safely():
    try:
        update(load_config())
    except Exception as e:
        log.exception(f"[错误] {e}")
    trim_memory()


_instance_lock = None


def acquire_single_instance():
    """确保同一文件夹只有一个监控进程，并降低本进程优先级。成功返回 True。"""
    global _instance_lock
    if IS_WIN:
        k32 = ctypes.windll.kernel32
        mutex_name = "Local\\ToolDocs_" + hashlib.md5(BASE_DIR.lower().encode()).hexdigest()
        _instance_lock = k32.CreateMutexW(None, False, mutex_name)
        if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        k32.SetPriorityClass(current_process(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
        with open(PID_PATH, "w") as f:
            f.write(str(os.getpid()))
        return True

    import fcntl
    fd = os.open(PID_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _instance_lock = fd
    try:
        os.nice(10)
    except OSError:
        pass
    return True


def watch():
    if not acquire_single_instance():
        log.info("已有监控进程在运行，本次退出")
        return

    log.info(f"开始监控 {BASE_DIR}")
    run_update_safely()
    last_state = snapshot()
    try:
        for _ in directory_events():
            wait_until_stable()
            state = snapshot()
            if state != last_state:
                run_update_safely()
                last_state = state
    except OSError as e:
        log.warning(f"[警告] 目录通知不可用，改为每 10 秒轮询: {e}")
        last = snapshot()
        while True:
            time.sleep(10)
            now = snapshot()
            if now != last:
                wait_until_stable()
                run_update_safely()
                last = snapshot()


# ---------------------------------------------------------------- 开机自启 / 后台进程管理

def background_python():
    """后台运行用的 Python：Windows 用不弹窗口的 pythonw.exe，其他系统用当前解释器。"""
    if IS_WIN:
        candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return sys.executable


def config_home():
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


SYSTEMD_UNIT_PATH = os.path.join(config_home(), "systemd", "user", f"{SERVICE_NAME}.service")
XDG_AUTOSTART_PATH = os.path.join(config_home(), "autostart", f"{SERVICE_NAME}.desktop")
LAUNCH_AGENT_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")


def run_cmd(*args):
    import subprocess
    try:
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError as e:
        return subprocess.CompletedProcess(args, 127, str(e))


def spawn_background():
    import subprocess
    kwargs = {"cwd": APP_DIR, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    if IS_WIN:
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([background_python(), SCRIPT_PATH, "--watch"], **kwargs)


def read_pid():
    try:
        with open(PID_PATH) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def running_pid():
    """返回正在运行的后台监控进程 PID，没有则返回 None。"""
    pid = read_pid()
    if not pid:
        return None
    if not IS_WIN:
        import fcntl
        try:
            fd = os.open(PID_PATH, os.O_RDONLY)
        except OSError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None  # 能拿到锁说明监控进程已不在
        except OSError:
            return pid
        finally:
            os.close(fd)

    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(handle, ctypes.byref(code))
        alive = code.value == 259  # STILL_ACTIVE
        return pid if alive and "python" in os.path.basename(buf.value).lower() else None
    finally:
        k32.CloseHandle(handle)


def stop():
    pid = running_pid()
    if not pid:
        log.info("没有正在运行的后台监控进程")
        return
    if IS_WIN:
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if handle:
            k32.TerminateProcess(handle, 0)
            k32.CloseHandle(handle)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(20):
            if not running_pid():
                break
            time.sleep(0.1)
    log.info(f"已停止后台监控进程（PID {pid}）")
    try:
        os.remove(PID_PATH)
    except OSError:
        pass


def write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def remove_file(path):
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def systemd_user_available():
    import shutil
    return bool(shutil.which("systemctl")) and run_cmd("systemctl", "--user", "show-environment").returncode == 0


def install_windows():
    import winreg
    command = f'"{background_python()}" "{SCRIPT_PATH}" --watch'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTORUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, AUTORUN_NAME, 0, winreg.REG_SZ, command)
    log.info(f"已设置开机自启: {command}")
    if running_pid():
        log.info("后台监控已在运行")
        return
    spawn_background()
    log.info("已在后台启动监控")


def install_linux():
    python = background_python()
    if systemd_user_available():
        write_text(SYSTEMD_UNIT_PATH, f"""[Unit]
Description=Skills_Admin 工具说明自动更新
After=network-online.target

[Service]
Type=simple
ExecStart="{python}" "{SCRIPT_PATH}" --watch
WorkingDirectory={APP_DIR}
Nice=10
IOSchedulingClass=idle
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
""")
        stop()
        run_cmd("systemctl", "--user", "daemon-reload")
        result = run_cmd("systemctl", "--user", "enable", f"{SERVICE_NAME}.service")
        if result.returncode != 0:
            log.error(f"启用 systemd 服务失败: {result.stdout.strip()}")
            return
        run_cmd("systemctl", "--user", "restart", f"{SERVICE_NAME}.service")
        log.info(f"已设置开机自启（systemd 用户服务 {SERVICE_NAME}），并已在后台启动监控")
        log.info("提示：默认在登录后启动；如需开机未登录也运行，执行 loginctl enable-linger $USER")
        return

    write_text(XDG_AUTOSTART_PATH, f"""[Desktop Entry]
Type=Application
Name=Skills_Admin 工具说明自动更新
Exec="{python}" "{SCRIPT_PATH}" --watch
Path={APP_DIR}
NoDisplay=true
Terminal=false
X-GNOME-Autostart-enabled=true
""")
    log.info(f"未检测到 systemd，已添加桌面登录自启: {XDG_AUTOSTART_PATH}")
    if not running_pid():
        spawn_background()
    log.info("已在后台启动监控")


def install_mac():
    import plistlib
    os.makedirs(os.path.dirname(LAUNCH_AGENT_PATH), exist_ok=True)
    with open(LAUNCH_AGENT_PATH, "wb") as f:
        plistlib.dump({
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [background_python(), SCRIPT_PATH, "--watch"],
            "WorkingDirectory": APP_DIR,
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Background",
            "LowPriorityIO": True,
            "Nice": 10,
        }, f)
    stop()
    domain = f"gui/{os.getuid()}"
    run_cmd("launchctl", "bootout", domain, LAUNCH_AGENT_PATH)
    result = run_cmd("launchctl", "bootstrap", domain, LAUNCH_AGENT_PATH)
    if result.returncode != 0:
        result = run_cmd("launchctl", "load", "-w", LAUNCH_AGENT_PATH)
    if result.returncode != 0:
        log.error(f"加载 LaunchAgent 失败: {result.stdout.strip()}")
        return
    log.info(f"已设置开机自启（LaunchAgent {LAUNCHD_LABEL}），并已在后台启动监控")
    log.info("提示：如果 Skills_Admin 在 桌面/文稿/下载 目录中，需要在 系统设置 > 隐私与安全性 > 完全磁盘访问权限 "
             f"中加入 {background_python()}，否则后台进程无权读取该文件夹")


def install():
    if IS_WIN:
        install_windows()
    elif IS_MAC:
        install_mac()
    elif IS_LINUX:
        install_linux()
    else:
        log.error(f"暂不支持在 {sys.platform} 上设置开机自启，可手动运行 --watch")


def uninstall():
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTORUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, AUTORUN_NAME)
            log.info("已取消开机自启")
        except FileNotFoundError:
            log.info("未设置过开机自启")
    elif IS_MAC:
        run_cmd("launchctl", "bootout", f"gui/{os.getuid()}", LAUNCH_AGENT_PATH)
        log.info("已取消开机自启" if remove_file(LAUNCH_AGENT_PATH) else "未设置过开机自启")
    elif IS_LINUX:
        removed = False
        if os.path.exists(SYSTEMD_UNIT_PATH):
            run_cmd("systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.service")
            removed = remove_file(SYSTEMD_UNIT_PATH)
            run_cmd("systemctl", "--user", "daemon-reload")
        removed = remove_file(XDG_AUTOSTART_PATH) or removed
        log.info("已取消开机自启" if removed else "未设置过开机自启")
    stop()


def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="自动更新 工具说明.md")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--watch", action="store_true", help="持续监控文件夹")
    group.add_argument("--install", action="store_true", help="设置开机自启并在后台启动")
    group.add_argument("--uninstall", action="store_true", help="取消开机自启并停止后台进程")
    group.add_argument("--stop", action="store_true", help="停止后台进程")
    args = parser.parse_args()

    if args.watch:
        watch()
    elif args.install:
        install()
    elif args.uninstall:
        uninstall()
    elif args.stop:
        stop()
    else:
        update(load_config())


if __name__ == "__main__":
    main()
