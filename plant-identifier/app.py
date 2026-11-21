import os
import io
import base64
import sqlite3
import re
import threading
import html
import hashlib
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
from PIL import Image, ImageOps
from flask import Flask, render_template, request, jsonify, send_from_directory

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload limit
DB_PATH = os.environ.get("PLANT_DB_PATH", os.path.join(app.instance_path, "plant_history.db"))
os.makedirs(app.instance_path, exist_ok=True)

# 优先读环境变量；没有就退回你原来的值，保证本地直接能跑。
# 生产环境请把这两个变量配到平台后台，别让密钥跟着代码走。
BAIDU_API_KEY = os.environ.get("BAIDU_API_KEY") or "kztmbUM17gXsxrov8mLc9wjW"
BAIDU_SECRET_KEY = os.environ.get("BAIDU_SECRET_KEY") or "XEdesSlhgu1UNJWYPfQJ1EbaTfROOSGU"

BAIDU_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
BAIDU_PLANT_URL = "https://aip.baidubce.com/rest/2.0/image-classify/v1/plant"

# 可选：百度翻译开放平台 appid / 密钥。配上这条通道最稳，国内机器也能通。
BAIDU_TRANS_APPID = os.environ.get("BAIDU_TRANSLATE_APPID")
BAIDU_TRANS_KEY = os.environ.get("BAIDU_TRANSLATE_KEY")

# 空描述时不要塞一句废话，直接留空
EMPTY_DESCRIPTION_FALLBACK = ""


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recognition_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plant_name TEXT NOT NULL,
                score REAL,
                description TEXT,
                image_data TEXT,
                created_at TEXT NOT NULL
            )
        """)


init_db()


# ==========================================================================
# 翻译（本次唯一实质改动）
#
# 原来的问题：
#   1. 只走 Google gtx 一条通道，国内网络直接超时，全部退回
#      "Plant name unavailable in English"，所以看着就是"翻译不出来"。
#   2. 串行翻译 + 每段 900 字切块 + 12 秒超时，一次上传能拖到 60 秒，
#      超过 gunicorn 默认 30 秒 worker 超时 → 进程被杀 → 页面 502。
#
# 现在的做法：
#   1. 三通道降级：Google → 百度翻译官方 → MyMemory。
#   2. 通道熔断：某条通道网络层失败或额度耗尽，5 分钟内不再重试它，
#      后续文本立刻走下一通道，不再为死通道反复等超时。
#   3. 多条文本并行翻译（线程池），总耗时取决于最慢的一条而不是累加。
#   4. MyMemory 的额度警告文本会被识别并判失败，绝不当译文用。
#   5. 结果缓存，同一段文字只翻一次。
#   6. 全部失败时：名字回退成中文原名（比 "Plant name unavailable" 有用得多），
#      并带上 translated=false 标记；简介留空，前端不会显示乱码。
# ==========================================================================

_CJK_RE = re.compile(r"[\u3400-\u9fff]")

_trans_cache = {}
_trans_cache_lock = threading.Lock()
_TRANS_CACHE_MAX = 3000

# 通道熔断状态：值为冷却结束时间戳
_channel_cooldown = {"google": 0.0, "baidu": 0.0, "mymemory": 0.0}
_channel_lock = threading.Lock()

_CHANNEL_COOLDOWN_SECONDS = 300       # 普通失败：冷却 5 分钟
_QUOTA_COOLDOWN_SECONDS = 1800        # 额度耗尽：冷却 30 分钟

_BAD_TRANSLATION_MARKERS = (
    "MYMEMORY WARNING",
    "YOU USED ALL AVAILABLE FREE TRANSLATIONS",
    "QUERY LENGTH LIMIT",
    "INVALID SOURCE LANGUAGE",
)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="translate")


def _cache_get(key):
    with _trans_cache_lock:
        return _trans_cache.get(key)


def _cache_set(key, value):
    with _trans_cache_lock:
        if len(_trans_cache) > _TRANS_CACHE_MAX:
            _trans_cache.clear()
        _trans_cache[key] = value


def _is_bad_translation(text):
    if not text:
        return True
    upper = text.upper()
    return any(marker in upper for marker in _BAD_TRANSLATION_MARKERS)


def _channel_available(name):
    with _channel_lock:
        return time.time() >= _channel_cooldown.get(name, 0.0)


def _trip_channel(name, seconds=_CHANNEL_COOLDOWN_SECONDS):
    with _channel_lock:
        _channel_cooldown[name] = max(_channel_cooldown.get(name, 0.0), time.time() + seconds)
    app.logger.warning("translate channel %s tripped for %ss", name, seconds)


def _split_text(text, size=900):
    if len(text) <= size:
        return [text]
    chunks, buffer = [], ""
    for piece in re.split(r"(?<=[。！？!?\n])", text):
        if len(buffer) + len(piece) > size and buffer:
            chunks.append(buffer)
            buffer = piece
        else:
            buffer += piece
    if buffer:
        chunks.append(buffer)
    return chunks


def _via_google(chunk):
    response = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": chunk},
        timeout=5,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    data = response.json()
    output = "".join(part[0] for part in data[0] if part and part[0])
    if _is_bad_translation(output):
        raise ValueError("google returned empty output")
    return output


def _via_baidu_official(chunk):
    if not BAIDU_TRANS_APPID or not BAIDU_TRANS_KEY:
        raise LookupError("baidu translate credentials not configured")
    salt = str(random.randint(32768, 65536))
    sign = hashlib.md5(
        (BAIDU_TRANS_APPID + chunk + salt + BAIDU_TRANS_KEY).encode("utf-8")
    ).hexdigest()
    response = requests.post(
        "https://fanyi-api.baidu.com/api/trans/vip/translate",
        data={
            "q": chunk, "from": "zh", "to": "en",
            "appid": BAIDU_TRANS_APPID, "salt": salt, "sign": sign,
        },
        timeout=8,
    ).json()
    if "trans_result" not in response:
        # 54001 = 签名错误，54003 = 频率限制，54004 = 余额不足
        raise ValueError(f"baidu translate error: {str(response)[:120]}")
    return "".join(entry["dst"] for entry in response["trans_result"])


def _via_mymemory(chunk):
    response = requests.get(
        "https://api.mymemory.translated.net/get",
        params={"q": chunk, "langpair": "zh-CN|en"},
        timeout=4,
    ).json()
    output = html.unescape((response.get("responseData") or {}).get("translatedText", "") or "")
    if _is_bad_translation(output):
        raise ValueError("mymemory quota exhausted")
    return output


_TRANSLATORS = (
    ("google", _via_google),
    ("baidu", _via_baidu_official),
    ("mymemory", _via_mymemory),
)


def translate_text(text):
    """返回 (译文, 是否真的翻译成功)。失败时译文为中文原文，供前端回退显示。"""
    text = str(text or "").strip()
    if not text:
        return "", True

    # 已经是英文（含拉丁学名）就原样返回，不消耗接口
    if not _CJK_RE.search(text):
        return text, True

    cached = _cache_get(text)
    if cached is not None:
        return cached, True

    for name, translator in _TRANSLATORS:
        if not _channel_available(name):
            continue
        try:
            output = "".join(translator(chunk) for chunk in _split_text(text))
            output = re.sub(r"\s+", " ", output).strip()
            if _is_bad_translation(output) or _CJK_RE.search(output):
                raise ValueError("translation output rejected")
            _cache_set(text, output)
            return output, True
        except LookupError:
            continue  # 这条通道没配密钥，跳过，不需要熔断
        except (requests.RequestException, ValueError, TypeError, IndexError) as exc:
            app.logger.info("translate channel %s failed: %s", name, str(exc)[:100])
            # 网络层失败或额度耗尽都熔断，别让后面每段文字都再等一次超时
            _trip_channel(
                name,
                _QUOTA_COOLDOWN_SECONDS if "quota" in str(exc) else _CHANNEL_COOLDOWN_SECONDS,
            )
            continue

    app.logger.info("all translate channels failed, falling back to original text")
    return text, False


def translate_many(texts):
    """并行翻译多条文本，避免串行累加超时。返回 [(译文, 是否成功), ...]"""
    texts = [str(t or "") for t in texts]
    pending = [t for t in texts if t and _CJK_RE.search(t)]
    if not pending:
        return [(t, True) for t in texts]

    futures = [(_executor.submit(translate_text, t), t) for t in texts if t]
    results = {}
    for future, original in futures:
        try:
            results[original] = future.result(timeout=20)
        except Exception:
            results[original] = (original, False)

    return [results.get(t, (t, True)) for t in texts]


def get_access_token():
    response = requests.post(
        BAIDU_TOKEN_URL,
        params={
            "grant_type": "client_credentials",
            "client_id": BAIDU_API_KEY,
            "client_secret": BAIDU_SECRET_KEY,
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if "access_token" not in payload:
        raise RuntimeError(
            payload.get("error_description") or payload.get("error")
            or "Could not obtain a Baidu access token."
        )
    return payload["access_token"]


def prepare_image(file_storage):
    image = Image.open(file_storage.stream)
    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((1200, 1200))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=82, optimize=True)
    return buffer.getvalue(), image


def recognize_plant(image_bytes):
    token = get_access_token()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    response = requests.post(
        BAIDU_PLANT_URL,
        params={"access_token": token},
        data={"image": encoded, "baike_num": 1},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error_code"):
        raise RuntimeError(payload.get("error_msg", "The Baidu plant identification API returned an error."))
    return payload


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json", mimetype="application/manifest+json")


@app.route("/healthz")
def healthz():
    """排查用：哪条翻译通道可用、密钥有没有读到、缓存多大。"""
    return jsonify({
        "status": "ok",
        "translate_channels": {
            name: _channel_available(name)
            for name, _ in _TRANSLATORS
        },
        "baidu_translate_configured": bool(BAIDU_TRANS_APPID and BAIDU_TRANS_KEY),
        "baidu_credentials": bool(BAIDU_API_KEY and BAIDU_SECRET_KEY),
        "translate_cache_size": len(_trans_cache),
    })


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("img")
    if file is None or not file.filename:
        return jsonify({"error": "Please take or choose an image first."}), 400

    try:
        image_bytes, image = prepare_image(file)
        result = recognize_plant(image_bytes)
        results = result.get("result", [])
        if not results:
            return jsonify({"error": "No plant was identified. Try a clearer photo with the plant as the main subject."}), 422

        top = results[0]
        raw_names = [item.get("name", "") for item in results]
        raw_description = (top.get("baike_info") or {}).get("description", "") or ""

        # 所有候选名字 + 首条百科简介，一次性并行翻译
        jobs = raw_names + [raw_description[:1200]]
        translated = translate_many(jobs)
        name_results = translated[:len(raw_names)]
        desc_text, desc_ok = translated[len(raw_names)]

        top_name, top_ok = name_results[0]
        score = float(top.get("score", 0) or 0)
        description = desc_text[:4000] if desc_ok else ""
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")

        thumb = image.copy()
        thumb.thumbnail((480, 480))
        thumb_buffer = io.BytesIO()
        thumb.save(thumb_buffer, format="JPEG", quality=70, optimize=True)
        image_data = "data:image/jpeg;base64," + base64.b64encode(thumb_buffer.getvalue()).decode("ascii")

        with db_connect() as conn:
            cursor = conn.execute(
                """INSERT INTO recognition_history
                   (plant_name, score, description, image_data, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (top_name, score, description, image_data, created_at),
            )
            record_id = cursor.lastrowid

        english_results = []
        for index, item in enumerate(results):
            translated_name, name_ok = name_results[index]
            translated_item = dict(item)
            translated_item["name"] = translated_name
            translated_item["name_zh"] = raw_names[index]
            translated_item["name_translated"] = name_ok

            info = dict(item.get("baike_info") or {})
            if index == 0:
                info["description"] = description
                info["description_translated"] = desc_ok
            else:
                info["description"] = ""
                info["description_translated"] = True
            info["description_zh"] = (item.get("baike_info") or {}).get("description", "")
            translated_item["baike_info"] = info
            english_results.append(translated_item)

        return jsonify({
            "id": record_id,
            "result": english_results,
            "saved": True,
            "created_at": created_at,
            "translated": top_ok,
        })
    except (Image.UnidentifiedImageError, OSError):
        return jsonify({"error": "Could not read this image. Please try a JPG, PNG, or WebP file."}), 400
    except requests.RequestException:
        app.logger.exception("Plant recognition API request failed")
        return jsonify({"error": "Could not connect to the recognition service. Check your connection and try again."}), 502
    except Exception as exc:
        app.logger.exception("Plant recognition failed")
        return jsonify({"error": str(exc)}), 500


@app.route("/history", methods=["GET"])
def history():
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT id, plant_name, score, description, image_data, created_at
               FROM recognition_history ORDER BY id DESC LIMIT 100"""
        ).fetchall()
    records = [dict(row) for row in rows]
    # 存进去的已经是英文，这里基本不会再打翻译接口（有缓存兜底）
    for record in records:
        record["plant_name"] = translate_text(record.get("plant_name"))[0]
        record["description"] = translate_text(record.get("description"))[0]
    return jsonify({"records": records})


@app.route("/history/<int:record_id>", methods=["DELETE"])
def delete_history(record_id):
    with db_connect() as conn:
        cursor = conn.execute("DELETE FROM recognition_history WHERE id = ?", (record_id,))
    if cursor.rowcount == 0:
        return jsonify({"error": "Record not found or already deleted."}), 404
    return jsonify({"ok": True})


@app.route("/history", methods=["DELETE"])
def clear_history():
    with db_connect() as conn:
        conn.execute("DELETE FROM recognition_history")
    return jsonify({"ok": True})


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "Image is larger than 10 MB. Please use a smaller photo."}), 413


if __name__ == "__main__":
    # 本地开发用；线上走 gunicorn，不会执行到这里
    app.run(debug=bool(os.environ.get("FLASK_DEBUG")), host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
