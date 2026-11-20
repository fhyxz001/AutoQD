import json

import requests

API_URL = "https://breezeblue.ai/api/credits/daily-claim"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def run(config):
    """执行一次每日领取尝试, 返回 {"ok": bool, "detail": str}"""
    settings = config.get("settings", {})
    cookie = (settings.get("cookie") or "").strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[7:].strip()
    if not cookie:
        return {"ok": False, "detail": "未配置 Cookie, 请在平台编辑任务设置"}
    user_agent = (settings.get("user_agent") or "").strip() or DEFAULT_UA

    headers = {
        "Cookie": cookie,
        "User-Agent": user_agent,
        "Origin": "https://breezeblue.ai",
        "Referer": "https://breezeblue.ai/zh/app/home",
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    try:
        response = requests.post(API_URL, headers=headers, timeout=30)
    except requests.RequestException as exc:
        return {"ok": False, "detail": f"请求异常: {exc}"}

    try:
        payload = response.json()
        detail = json.dumps(payload, ensure_ascii=False)
    except ValueError:
        payload = None
        detail = response.text[:500]

    ok = response.status_code == 200 and not (
        isinstance(payload, dict) and payload.get("ok") is False
    )
    return {"ok": ok, "detail": detail[:500]}
