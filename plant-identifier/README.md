# Plant Identifier · 完整包

这是可以直接部署的整包，只有一处技术改动（翻译改为三通道降级 + 熔断），
详细差异见 `CHANGES.md`。

## 目录结构

```
plant-identifier/
├─ app.py               # 后端（在你的原文件上改的，接口约定全部保留）
├─ Procfile             # 启动命令，已把 worker 超时调到 90 秒
├─ requirements.txt     # 依赖（沿用你原来的版本约束）
├─ .env.example         # 环境变量示例
├─ CHANGES.md           # 本次改了什么、为什么
├─ templates/
│  └─ 把你的 index.html 放这里（文件名必须是 index.html）
└─ static/
   └─ 把你的 manifest.json + 图标放这里
```

**注意**：`templates/index.html` 和 `static/manifest.json` 这两样我没放进包，
因为你没有上传给我，我这里没有它们的真实内容。硬造一个放进去，
一解压就会覆盖你正在用的版本 —— 上次就是这么把你的前端搞丢的。
所以留了空目录和说明文件，你把原来的两个文件拖进去即可。

## 本地跑起来

```bash
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000
```

密钥不用配也能跑：`app.py` 里保留了 `os.environ.get(...) or "你原来的值"` 的兜底。
但推到公开仓库前，最好把兜底删掉，只走环境变量。

## 部署到 Render

1. 整个目录推到 GitHub。
2. Render → New → **Web Service** → 连接仓库。
3. 配置：

   | 项 | 值 |
   | --- | --- |
   | Runtime | Python |
   | Region | Singapore |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `gunicorn app:app --timeout 90 --workers 2 --threads 4` |

   `Procfile` 已经有同样的命令，Render 会自动读，不填 Start Command 也行。

4. 环境变量：

   | Key | 必填 | 说明 |
   | --- | --- | --- |
   | `BAIDU_API_KEY` | 是 | 百度 AI 的 API Key |
   | `BAIDU_SECRET_KEY` | 是 | 百度 AI 的 Secret Key |
   | `PYTHON_VERSION` | 建议 | `3.11.9` |
   | `BAIDU_TRANSLATE_APPID` | 可选 | 百度翻译开放平台，配上翻译最稳 |
   | `BAIDU_TRANSLATE_KEY` | 可选 | 同上密钥 |

5. 部署完访问 `/healthz` 自查：

```json
{
  "status": "ok",
  "translate_channels": {"google": true, "baidu": true, "mymemory": true},
  "baidu_translate_configured": false,
  "baidu_credentials": true,
  "translate_cache_size": 0
}
```

`translate_channels` 里哪条变成 `false`，说明它刚失败过、正在冷却期。
`baidu_translate_configured: true` 表示百度翻译通道已就绪。

## 已知限制

- **历史记录在 Render 免费版不持久**：文件系统是临时的，重新部署或休眠唤醒后
  `instance/plant_history.db` 会清空。要做真历史记录得换 Postgres。
- 免费实例会休眠，冷启动要等 30~60 秒。
- 首次上传如果在境外通道全部不可用，会有几秒试错时间（实测 8 秒），
  之后通道熔断，后续请求 1 秒左右返回。
