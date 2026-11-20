# AutoQD 签到管理平台

统一管理多个网站签到任务的轻量平台：Web UI 管理、每日定时执行、失败自动重试、记录持久化。

## 功能

- **任务插件化**：`tasks/` 下一个文件夹一个签到任务，平台启动时自动发现并接管
- **Web 管理界面**（端口 8080，每 10 秒自动刷新）
  - 查看：下次运行时间 / 最近结果 / 今日是否完成 / 最近签到记录
  - 操作：立即运行、启用/禁用任务
  - 配置：每日签到时刻、失败重试次数、重试间隔、任务凭据（账号密码 / Cookie）
- **统一调度**：每天按配置时刻签到；容器重启时当天未签到会立即补签
- **重试机制**：失败后按"重试次数 × 重试间隔"自动重试，提高成功率
- **记录持久化**：签到记录保存在 `data/` 目录，重启不丢失

## 内置任务

| 任务 | 说明 | 默认时刻 |
|---|---|---|
| `sd_exacg` | [sd.exacg.cc](https://sd.exacg.cc/) AI 绘图站每日签到（内置 Cloudflare 指纹绕过） | 08:30 |
| `breezeblue` | [breezeblue.ai](https://breezeblue.ai/) 每日积分领取 | 00:05 |

## 快速部署（拉取镜像）

```bash
mkdir -p AutoQD/tasks AutoQD/data && cd AutoQD
```

创建 `docker-compose.yml`：

```yaml
services:
  autoqd:
    image: registry.cn-hangzhou.aliyuncs.com/docker-hexb/auto-qd:latest
    container_name: autoqd
    ports:
      - "8080:8080"
    environment:
      - TZ=Asia/Shanghai
      - TASKS_DIR=/app/tasks
      - DATA_DIR=/app/data
    volumes:
      - ./tasks:/app/tasks
      - ./data:/app/data
    restart: unless-stopped
```

启动：

```bash
docker compose up -d
docker compose pull && docker compose up -d   # 以后更新
```

## 首次使用

1. 浏览器打开 `http://服务器IP:8080`
2. 每个任务点击 **编辑设置**，填写凭据：
   - `sd_exacg`：填写 `username` / `password`
   - `breezeblue`：填写浏览器登录后复制的 `cookie`（整段 Cookie 即可）
3. 保存后点 **立即运行** 验证，之后每天自动签到
4. 也可调整签到时刻、重试次数与间隔，保存即生效无需重启

> 出于安全考虑，仓库中的任务配置不包含凭据；首次在 UI 中填写后会持久化到 `tasks/<任务名>/config.json`（该文件已被 `.gitignore` 排除）。

## 源码构建

```bash
git clone https://github.com/fhyxz001/AutoQD.git && cd AutoQD
docker compose up -d --build
```

## 添加新任务

1. 在 `tasks/` 下新建文件夹，放入 `task.py`，实现：

```python
def run(config):
    """执行一次签到尝试"""
    settings = config.get("settings", {})
    # ... 请求目标网站 ...
    return {"ok": True, "detail": "签到成功"}
```

2. （可选）放入 `config.json` 定义默认配置，`settings` 中的键会自动出现在 Web UI 的编辑表单里
3. 重启容器（`docker compose restart`），平台自动加载新任务

## 自动构建

推送到 `master` 分支后，GitHub Actions 自动构建镜像并推送到阿里云容器镜像服务
`registry.cn-hangzhou.aliyuncs.com/docker-hexb/auto-qd`，标签为 `latest` 与 commit SHA。
需在仓库 Secrets 中配置 `ACR_USERNAME` / `ACR_PASSWORD`。
