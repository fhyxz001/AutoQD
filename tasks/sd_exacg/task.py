from curl_cffi import requests as curl_requests

BASE_URL = "https://sd.exacg.cc"
# 该站有 Cloudflare 指纹检测, 必须模拟浏览器 TLS 指纹, 普通 requests 会被 403


def run(config):
    """执行一次签到尝试, 返回 {"ok": bool, "detail": str}"""
    settings = config.get("settings", {})
    username = (settings.get("username") or "").strip()
    password = (settings.get("password") or "").strip()
    if not username or not password:
        return {"ok": False, "detail": "未配置用户名或密码, 请在平台编辑任务设置"}

    session = curl_requests.Session(impersonate="chrome")
    try:
        resp = session.post(
            f"{BASE_URL}/login",
            json={"username": username, "password": password},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            return {"ok": False, "detail": f"登录失败: {data.get('message')}"}
        points = data.get("user", {}).get("points")

        resp = session.get(f"{BASE_URL}/checkin_status", timeout=30)
        resp.raise_for_status()
        if resp.json().get("has_checked_in"):
            return {"ok": True, "detail": f"今日已签到, 当前积分 {points}"}

        resp = session.get(f"{BASE_URL}/daily_checkin", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("success"):
            return {
                "ok": True,
                "detail": f"签到成功, 当前积分: {data.get('new_points')}",
            }
        return {"ok": False, "detail": f"签到失败: {data.get('message')}"}
    except Exception as exc:
        return {"ok": False, "detail": f"请求异常: {exc}"}
