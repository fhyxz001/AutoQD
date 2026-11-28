"""hikarinagi.org 每日签到.

该站自身不做密码登录, 而是 OIDC 授权码 + PKCE 的 RP (Relying Party), 密码在 IdP
id.hikarinagi.org 提交, 流程:

  1. POST www.hikarinagi.org/auth/login  (表单 return_to) -> 303 跳到
     id.hikarinagi.org/oidc/auth?client_id=consumer-web&code_challenge=...
  2. IdP 建立 interaction 会话 -> 303 /interaction/{uid}
  3. POST id.hikarinagi.org/interaction/{uid}/login  JSON {"account","password"}
     -> {"success":true,"data":{"redirect_to":...}}
  4. 回到 /oidc/auth/{uid} -> 303 /interaction/{uid2} (授权确认页)
     GET  /interaction/{uid2}/consent 取授权信息
     POST /interaction/{uid2}/consent 同意授权
  5. /oidc/auth/{uid2} -> 303 www.hikarinagi.org/auth/callback?code=...&state=...
  6. 回调换取站点会话 Cookie: hikari_access_token / hikari_refresh_token / hikari_id_token
  7. 带 Cookie 调 /api/v3/user/me 校验登录态, /api/v3/user/me/check-ins 签到
"""

import json
import re
from urllib.parse import urljoin

import requests

WWW_BASE = "https://www.hikarinagi.org"
IDP_BASE = "https://id.hikarinagi.org"
CALLBACK_PATH = "/auth/callback"
ACCESS_COOKIE = "hikari_access_token"

_INTERACTION_RE = re.compile(r"/interaction/([^/?#]+)")
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class LoginError(Exception):
    """登录/签到流程中的可读失败原因"""


def _payload(response):
    """解析站点统一响应体 {"success":..., "data":..., "error":...}"""
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _error_text(payload, fallback):
    """从响应里拼出错误原因 (error.message / field_errors)"""
    if not isinstance(payload, dict):
        return fallback
    parts = []
    error = payload.get("error")
    if isinstance(error, dict):
        parts.append(str(error.get("message") or error.get("code") or ""))
    elif error:
        parts.append(str(error))
    field_errors = payload.get("field_errors")
    if not field_errors and isinstance(payload.get("data"), dict):
        field_errors = payload["data"].get("field_errors")
    if isinstance(field_errors, list):
        for item in field_errors:
            if isinstance(item, dict) and item.get("message"):
                parts.append(f"{item.get('field', '')} {item['message']}".strip())
    text = "; ".join(part for part in parts if part)
    return text or fallback


def _interaction_uid(response):
    match = _INTERACTION_RE.search(response.headers.get("location") or "")
    return match.group(1) if match else None


def _begin_login(session):
    """第 1~2 步: 发起 OIDC 授权, 返回 IdP interaction uid"""
    resp = session.post(
        f"{WWW_BASE}/auth/login",
        data={"return_to": "/"},
        allow_redirects=False,
        timeout=30,
    )
    auth_url = resp.headers.get("location")
    if resp.status_code not in (301, 302, 303, 307, 308) or not auth_url:
        raise LoginError(f"站点未返回 OIDC 授权地址 (HTTP {resp.status_code})")
    resp = session.get(urljoin(WWW_BASE, auth_url), allow_redirects=False, timeout=30)
    uid = _interaction_uid(resp)
    if not uid:
        raise LoginError(f"IdP 未创建登录会话 (HTTP {resp.status_code})")
    return uid


def _submit_password(session, uid, account, password):
    """第 3 步: 在 IdP 提交账号密码, 返回下一步地址"""
    resp = session.post(
        f"{IDP_BASE}/interaction/{uid}/login",
        json={"account": account, "password": password},
        timeout=30,
    )
    payload = _payload(resp)
    if not isinstance(payload, dict) or not payload.get("success"):
        raise LoginError(
            "登录失败: " + _error_text(payload, f"HTTP {resp.status_code}")
        )
    data = payload.get("data") or {}
    next_url = data.get("redirect_to")
    if not next_url:
        raise LoginError(
            "登录响应缺少 redirect_to: " + json.dumps(payload, ensure_ascii=False)[:200]
        )
    if any(key in data for key in ("verification", "mfa", "action")) or "verify" in str(
        next_url
    ):
        raise LoginError("该账号需要额外验证(验证码/二次验证), 无法自动登录")
    return urljoin(IDP_BASE, next_url)


def _confirm_consent(session, uid):
    """第 4 步: 同意 OIDC 授权, 返回后续地址"""
    session.get(f"{IDP_BASE}/interaction/{uid}/consent", timeout=30)
    resp = session.post(f"{IDP_BASE}/interaction/{uid}/consent", timeout=30)
    payload = _payload(resp)
    if isinstance(payload, dict) and payload.get("success"):
        next_url = (payload.get("data") or {}).get("redirect_to")
        if next_url:
            return urljoin(IDP_BASE, next_url)
    # consent 接口未给出下一步时, 重新请求该 interaction 让 IdP 自行推进
    return f"{IDP_BASE}/interaction/{uid}"


def _complete_oidc(session, url, max_steps=8):
    """第 4~5 步: 跟随 IdP 跳转(含授权确认), 返回站点回调地址"""
    current = url
    for _ in range(max_steps):
        resp = session.get(current, allow_redirects=False, timeout=30)
        location = resp.headers.get("location")
        if not location:
            raise LoginError(f"OIDC 流程中断于 {current} (HTTP {resp.status_code})")
        target = urljoin(current, location)
        if target.startswith(WWW_BASE) and target.split("?")[0].endswith(CALLBACK_PATH):
            return target
        match = _INTERACTION_RE.search(target)
        current = _confirm_consent(session, match.group(1)) if match else target
    raise LoginError("OIDC 跳转次数超限, 未能回到站点回调")


def _finish_login(session, callback_url):
    """第 6 步: 访问回调, 换取站点会话 Cookie"""
    resp = session.get(callback_url, allow_redirects=False, timeout=30)
    if resp.status_code >= 400:
        raise LoginError(f"回调失败 (HTTP {resp.status_code}): {resp.text[:150]}")
    if ACCESS_COOKIE not in session.cookies:
        raise LoginError("回调未下发站点会话 Cookie, 登录未生效")


def _api(session, method, path, **kwargs):
    kwargs.setdefault("timeout", 30)
    resp = session.request(method, f"{WWW_BASE}{path}", **kwargs)
    return resp.status_code, _payload(resp)


def _expect_ok(status, payload, what):
    if not isinstance(payload, dict) or not payload.get("success"):
        raise LoginError(f"{what}失败: " + _error_text(payload, f"HTTP {status}"))
    return payload.get("data") or {}


def run(config):
    """执行一次登录+签到尝试, 返回 {"ok": bool, "detail": str}"""
    settings = config.get("settings", {}) or {}
    account = (settings.get("account") or "").strip()
    password = settings.get("password") or ""
    if not account or not password:
        return {
            "ok": False,
            "detail": "未配置 account 或 password, 请在平台编辑任务设置",
        }

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (settings.get("user_agent") or "").strip() or DEFAULT_UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )
    try:
        uid = _begin_login(session)
        next_url = _submit_password(session, uid, account, password)
        callback_url = _complete_oidc(session, next_url)
        _finish_login(session, callback_url)

        status, payload = _api(session, "GET", "/api/v3/user/me")
        user = _expect_ok(status, payload, "读取用户信息")
        who = f"{user.get('name') or user.get('nickname') or account}#{user.get('id')}"

        status, payload = _api(session, "GET", "/api/v3/user/me/check-ins/status")
        check = _expect_ok(status, payload, "读取签到状态")
        if check.get("checked_in_today"):
            return {
                "ok": True,
                "detail": f"登录成功({who}); 今日已签到, 连续 {check.get('streak')} 天, "
                f"积分 {check.get('points')}",
            }

        status, payload = _api(session, "POST", "/api/v3/user/me/check-ins")
        result = _expect_ok(status, payload, "签到")

        parts = [
            f"登录成功({who})",
            f"签到成功 {result.get('date') or ''} +{result.get('points')} 积分".replace(
                "  ", " "
            ),
        ]
        milestone = result.get("milestone")
        if isinstance(milestone, dict) and milestone.get("bonus"):
            parts.append(f"连续 {milestone.get('days')} 天额外 +{milestone['bonus']}")
        status, payload = _api(session, "GET", "/api/v3/user/me/check-ins/status")
        latest = (
            payload.get("data")
            if isinstance(payload, dict) and payload.get("success")
            else {}
        )
        if isinstance(latest, dict) and latest.get("points") is not None:
            parts.append(f"当前积分 {latest['points']}")
        return {"ok": True, "detail": "; ".join(parts)}
    except LoginError as exc:
        return {"ok": False, "detail": str(exc)[:400]}
    except requests.RequestException as exc:
        return {"ok": False, "detail": f"请求异常: {exc}"[:400]}
    except Exception as exc:  # 任务异常不应打断平台调度
        return {"ok": False, "detail": f"任务异常: {exc}"[:400]}
