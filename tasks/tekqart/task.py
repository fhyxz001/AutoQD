"""tekqart.com 每日打卡任务 (Discuz! + zqlj_sign 天天打卡签到插件)

流程:
  1. 访问 plugin.php?id=zqlj_sign, 从页面 <a class="btna"> 里取出动态 sign token
  2. GET plugin.php?id=zqlj_sign&sign=<token> 即完成打卡
  3. 页面出现 "打卡成功" 即成功, "已经打过卡" 视为今日已完成

站点前面有 Cloudflare 托管挑战, 纯 HTTP 请求可能被 403 拦截。因此提供两种凭据模式:
  - 账号密码 (默认): curl_cffi 模拟 Chrome 指纹, 走 Discuz member.php 登录后打卡
  - Cookie: 浏览器登录后复制整段 Cookie (建议把 cf_clearance 一起带上), 跳过登录直接打卡
被 Cloudflare 拦截时返回明确提示, 换 Cookie 模式即可。
"""
import html as html_lib
import re
import time
from urllib.parse import urljoin

try:
    from curl_cffi import requests as _http
except ImportError:  # 本地调试缺依赖时降级为普通 requests, 指纹伪装失效
    import requests as _http

BASE_URL = "https://www.tekqart.com"
PLUGIN_URL = f"{BASE_URL}/plugin.php?id=zqlj_sign"
LOGIN_URL = f"{BASE_URL}/member.php?mod=logging&action=login"
TIMEOUT = 30
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Cloudflare 挑战页特征
CF_MARKS = ("Just a moment", "cf-chl", "challenge-platform", "请稍候", "正在验证您是否是真人")


class Blocked(Exception):
    """被 Cloudflare 挑战拦截"""


def _headers(referer=None):
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _is_blocked(resp):
    if resp.status_code not in (403, 429, 503):
        return False
    body = resp.text or ""
    return any(mark in body for mark in CF_MARKS)


def _first(pattern, text):
    match = re.search(pattern, text or "", re.S | re.I)
    return match.group(1).strip() if match else ""


def _new_session():
    if hasattr(_http, "Session"):
        try:
            return _http.Session(impersonate="chrome")
        except TypeError:
            pass
    return _http.Session()


def _retry(func, attempts=3, delays=(2, 6)):
    """瞬时网络错误重试 (连接重置/超时等); Cloudflare 拦截不重试"""
    last = None
    for index in range(attempts):
        try:
            return func()
        except Blocked:
            raise
        except Exception as exc:
            last = exc
            if index < len(delays):
                time.sleep(delays[index])
    raise last


def _get(session, url, referer=None):
    resp = _retry(lambda: session.get(url, headers=_headers(referer), timeout=TIMEOUT))
    if _is_blocked(resp):
        raise Blocked(f"Cloudflare 拦截 ({resp.status_code})")
    return resp


def _load_cookie(session, cookie):
    for part in cookie.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if name:
            session.cookies.set(name, value, domain=".tekqart.com")


def _login(session, username, password):
    """Discuz 标准登录, 返回 (成功?, 失败原因)"""
    page = _get(session, LOGIN_URL)
    text = page.text
    formhash = _first(r'name="formhash"\s+value="([0-9a-f]+)"', text) or _first(
        r'formhash=([0-9a-f]+)', text
    )
    if not formhash:
        return False, "登录页解析失败(未取到 formhash), 站点可能改版或被拦截"
    loginhash = _first(r'loginhash=([\w]+)', text)
    data = {
        "formhash": formhash,
        "referer": f"{BASE_URL}/",
        "loginfield": "username",
        "username": username,
        "password": password,
        "questionid": "0",
        "answer": "",
        "cookietime": "2592000",
    }
    query = f"&loginhash={loginhash}" if loginhash else ""
    url = (
        f"{BASE_URL}/member.php?mod=logging&action=login&loginsubmit=yes"
        f"&handlekey=login{query}&inajax=1"
    )
    headers = _headers(referer=LOGIN_URL)
    headers.update({"X-Requested-With": "XMLHttpRequest", "Origin": BASE_URL})
    resp = _retry(lambda: session.post(url, data=data, headers=headers, timeout=TIMEOUT))
    body = resp.text or ""
    if _is_blocked(resp):
        raise Blocked(f"登录请求被 Cloudflare 拦截 ({resp.status_code})")
    if any(k in body for k in ("欢迎您回来", "登录成功", "succeedhandle_login", "现在将转入")):
        return True, ""
    plain = re.sub(r"<[^>]+>", " ", body)
    plain = re.sub(r"\s+", " ", plain).strip()
    return False, f"登录失败: {plain[:200] or f'HTTP {resp.status_code}'}"


def _is_logged_in(text):
    return "action=logout" in text or "action=logging&action=logout" in text


def _sign_href(text):
    """取打卡按钮链接, 兼容 class/href 属性顺序"""
    patterns = (
        r'<a[^>]+class="[^"]*btna[^"]*"[^>]+href="([^"]+)"',
        r'<a[^>]+href="([^"]+)"[^>]+class="[^"]*btna[^"]*"',
        r'href="([^"]*\?id=zqlj_sign[^"]*sign=[^"]+)"',
    )
    for pattern in patterns:
        href = _first(pattern, text)
        if href:
            return html_lib.unescape(href)
    return ""


def _submit_forms(session, text, referer):
    """兜底: 插件改版成表单提交时, 按表单默认值提交含"打卡/签到"字样的表单"""
    for form in re.finditer(r"<form\b([^>]*)>(.*?)</form>", text, re.S | re.I):
        attrs, inner = form.group(1), form.group(2)
        if not any(k in inner for k in ("打卡", "签到")):
            continue
        action = _first(r'action="([^"]*)"', attrs)
        method = (_first(r'method="([^"]*)"', attrs) or "post").lower()
        data = {}
        for inp in re.finditer(r"<input\b([^>]*)>", inner, re.I):
            tag = inp.group(1)
            name = _first(r'name="([^"]*)"', tag)
            if not name:
                continue
            typ = (_first(r'type="([^"]*)"', tag) or "text").lower()
            if typ in ("submit", "button", "reset", "image"):
                continue
            if typ in ("checkbox", "radio") and "checked" not in tag.lower():
                continue
            data[name] = html_lib.unescape(_first(r'value="([^"]*)"', tag))
        if not data:
            continue
        url = urljoin(referer, html_lib.unescape(action) or referer)
        headers = _headers(referer=referer)
        if method == "post":
            resp = session.post(url, data=data, headers=headers, timeout=TIMEOUT)
        else:
            resp = session.get(url, params=data, headers=headers, timeout=TIMEOUT)
        if _is_blocked(resp):
            raise Blocked(f"打卡请求被 Cloudflare 拦截 ({resp.status_code})")
        if "打卡成功" in resp.text or "已经打过卡" in resp.text:
            return resp.text
    return ""


def _stats(text):
    """从打卡页提取连续/累计天数, 做记录详情"""
    plain = re.sub(r"<[^>]+>", " ", text)
    plain = re.sub(r"\s+", " ", plain)
    parts = []
    for label, pattern in (("连续", r"连续打卡\D{0,8}(\d+)\s*天"), ("累计", r"累计打卡\D{0,8}(\d+)\s*天")):
        value = _first(pattern, plain)
        if value:
            parts.append(f"{label} {value} 天")
    return " · ".join(parts)


def _do_sign(session):
    page = _get(session, PLUGIN_URL)
    text = page.text
    if not _is_logged_in(text):
        return {"ok": False, "detail": "登录状态无效(Cookie 过期或账号登录失败), 请更新配置"}

    stats = _stats(text)
    suffix = f" · {stats}" if stats else ""

    if any(k in text for k in ("已经打过卡", "今日已打卡", "今天已打卡", "已完成打卡")):
        return {"ok": True, "detail": f"今日已打卡, 无需重复{suffix}"}

    href = _sign_href(text)
    if href:
        target = urljoin(f"{BASE_URL}/", href)
        resp = _get(session, target, referer=PLUGIN_URL)
        body = resp.text
        if "打卡成功" in body:
            return {"ok": True, "detail": f"打卡成功{suffix}"}
        if "已经打过卡" in body:
            return {"ok": True, "detail": f"今日已打卡(提交时已存在记录){suffix}"}
        plain = re.sub(r"<[^>]+>", " ", body)
        plain = re.sub(r"\s+", " ", plain).strip()
        return {"ok": False, "detail": f"打卡返回异常: {plain[:200]}"}

    body = _submit_forms(session, text, PLUGIN_URL)
    if "打卡成功" in body:
        return {"ok": True, "detail": f"打卡成功(表单提交){suffix}"}
    if "已经打过卡" in body:
        return {"ok": True, "detail": f"今日已打卡{suffix}"}
    return {"ok": False, "detail": "未在打卡页找到打卡入口, 插件可能改版或被拦截"}


def run(config):
    """执行一次打卡尝试, 返回 {"ok": bool, "detail": str}"""
    settings = config.get("settings", {})
    cookie = (settings.get("cookie") or "").strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[7:].strip()
    username = (settings.get("username") or "").strip()
    password = (settings.get("password") or "").strip()
    proxy = (settings.get("proxy") or "").strip()

    if not cookie and not (username and password):
        return {"ok": False, "detail": "未配置凭据, 请填写账号密码或在平台填写 Cookie"}

    session = _new_session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    try:
        if cookie:
            _load_cookie(session, cookie)
            page = _get(session, PLUGIN_URL)
            if not _is_logged_in(page.text):
                # Cookie 里的登录态失效, 有账号密码则再登录一次
                if username and password:
                    ok, reason = _login(session, username, password)
                    if not ok:
                        return {"ok": False, "detail": f"{reason}(Cookie 已失效)"}
                else:
                    return {"ok": False, "detail": "Cookie 已失效, 请重新复制浏览器 Cookie"}
        else:
            ok, reason = _login(session, username, password)
            if not ok:
                return {"ok": False, "detail": reason}
        return _do_sign(session)
    except Blocked as exc:
        return {
            "ok": False,
            "detail": f"{exc}: 该站有 Cloudflare 人机校验, 请在浏览器登录后复制整段 Cookie "
                      f"(含 cf_clearance) 填入任务设置的 cookie 字段",
        }
    except Exception as exc:  # 网络异常/解析异常
        text = str(exc)
        if "curl: (35)" in text or "Connection reset" in text or "reset by peer" in text:
            return {
                "ok": False,
                "detail": "连接被重置(TLS 握手阶段对端 RST): 多为 Cloudflare/WAF 直接阻断或出口 IP 受限, "
                          "不是账号问题。建议改用含 cf_clearance 的 cookie 模式, 或在任务设置填 proxy 换出口。"
                          f"原始错误: {text[:120]}",
            }
        if "curl: (56)" in text or "curl: (28)" in text:
            return {"ok": False, "detail": f"网络连接中断/超时(已重试 3 次), 稍后自动重试。原始错误: {text[:120]}"}
        return {"ok": False, "detail": f"请求异常: {text[:200]}"}
