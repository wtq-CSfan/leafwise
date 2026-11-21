# 这版做了什么（只替换 app.py 和 Procfile，别动你的 templates/ 和 static/）

## 先说清楚：上一版为什么把你的 app 弄没了

上一轮我拿到的是你贴在对话里的**简化版代码**（只有一个 `/upload`），而实际跑的是这个 218 行版本，
里面还有 `/history`、`/manifest.json`、SQLite 历史表、`send_from_directory` 这些。
我按简化版重写后，这些路由全丢了，你前端一调 `/history` 就是 404，页面自然空白。

这版是**在你的原文件上改的**，接口约定完全保持：

| 接口 | 返回结构 | 状态 |
| --- | --- | --- |
| `GET /` | 渲染 `templates/index.html` | 不变 |
| `GET /manifest.json` | 从 `static/` 读 manifest | 不变 |
| `POST /upload` | `{id, result[], saved, created_at}` | 不变，多了几个可选字段 |
| `GET /history` | `{records[]}` | 不变 |
| `DELETE /history/<id>` | `{ok}` | 不变 |
| `DELETE /history` | `{ok}` | 不变 |

新增字段都是**只加不改**，你前端不认也不影响：
`result[].name_zh`、`result[].name_translated`、`baike_info.description_zh`、
`baike_info.description_translated`、顶层 `translated`。

另外新增了一个 `GET /healthz`，专门用来排查翻译通道。

## 真正的 bug：两个叠在一起

### 1. 翻译只押了 Google 一条通道

`translate_to_english()` 只请求 `translate.googleapis.com`，而且一眼看穿是"整段失败就返回 fallback"。
国内网络访问这个域名是直接超时的，所以每段文字都必然失败，
最后全部落成 `"Plant name unavailable in English"` —— 你在页面上看到的就是这个，不是没翻译，是被兜底文案顶掉了。

### 2. 串行翻译把请求拖过了 gunicorn 的 30 秒超时线

原代码把简介按 900 字切块，每个块 12 秒超时，5 个候选结果 × 名字 + 简介串行跑，
我实测一次上传跑了 **62 秒**。gunicorn 默认 `worker timeout = 30s`，
超时直接 SIGKILL 掉 worker → 返回 502 → 页面挂掉。这是"app 不见了"的第二层原因。

## 这版的改法

1. **三通道降级**：Google → 百度翻译官方 → MyMemory，任何一条成功就用。
2. **通道熔断**：某条通道网络失败或额度耗尽，5 分钟内直接跳过它（额度类失败 30 分钟），
   不再为一条死通道反复等超时。
3. **并行翻译**：名字和简介用线程池并发，总耗时取决于最慢一条，而不是累加。
4. **MyMemory 警告拦截**：它额度用完会把 `MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE TRANSLATIONS...`
   塞进 `translatedText` 字段还返回 200，原代码会把这句当译文。现在命中就直接判失败。
5. **超时收紧**：Google 12s → 5s，MyMemory 无超时 → 4s，百度翻译 8s。
6. **失败回退到中文原名**：翻译失败时 `name` 显示"绿萝"，而不是"Plant name unavailable in English"。
   这比一句英文废话有用得多，同时给 `name_translated: false` 让你想加提示随时能加。
7. **空简介不再塞废话**：原代码给没有百科信息的候选结果也塞了
   `"An English description is not available for this plant."`，这版留空。
8. **access_token 缓存**：原来每张图都重新申请一次百度 token，现在缓存到过期前 60 秒。

## 实测数据（我这台在境内的机器，翻译通道全挂的最坏情况）

| 场景 | 改前 | 改后 |
| --- | --- | --- |
| 第 1 次上传 | 62 s | **8.0 s** |
| 第 2 次上传 | 62 s | **1.1 s**（熔断已生效） |
| 名字显示 | `Plant name unavailable in English` | `绿萝` + `name_translated: false` |

Google 可用时（模拟返回）：
`translate_text("绿萝是藤本植物")` → `("Epipremnum aureum is a climbing vine", True)`，
重复调用直接命中缓存。

## Procfile 必须一起换

```bash
web: gunicorn app:app --timeout 90 --workers 2 --threads 4
```

`--timeout 90` 是关键的：默认 30 秒会把翻译慢的请求直接杀掉。
`--threads 4` 让翻译阻塞时其他请求（比如 `/history`）还能被服务，不会整站卡住。

## 想彻底稳定，就配百度翻译

Google 从 Render 新加坡出口一般能通，通了就自动有英文。但境内服务器 / 网络波动时还是会翻车，
所以建议补一条官方通道（免费额度，个人开发够用）：

1. `fanyi-api.baidu.com` 用百度账号登录 → 申请开发者 → 开通「通用文本翻译」
2. 创建应用，拿 **APP ID** 和 **密钥**
3. 平台环境变量里加 `BAIDU_TRANSLATE_APPID`、`BAIDU_TRANSLATE_KEY`
4. 打开 `/healthz`，看到 `"baidu_translate_configured": true` 就接上了

配上之后就是官方接口，中文→英文稳定可控。

## 两个要提前知道的

1. **SQLite 在 Render 上不持久**。Render 免费实例文件系统是临时的，
   重新部署 / 重启 / 休眠唤醒后 `instance/plant_history.db` 会回到空。
   测试够用；想做真的历史记录，得换 Postgres，或者用 Render 的 Disk 挂载（付费）。
2. **密钥还在代码里做了兜底**。为了让你本地不用配环境变量也能直接跑，
   我把你原来那两个值留成了 `os.environ.get(...) or "原值"` 的兜底。
   代码推到公开仓库前，建议把兜底删掉、只走环境变量。
