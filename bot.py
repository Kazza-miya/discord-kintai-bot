import os
import logging
import asyncio
import hashlib
from datetime import datetime
import pytz

import discord
import aiohttp
import requests
from flask import Flask
from threading import Thread
from dotenv import load_dotenv
from functools import wraps

# ─── ログ設定 ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(message)s",
)

# ─── タイムゾーン設定 ───────────────────────────────────────
JST = pytz.timezone("Asia/Tokyo")

# ─── 環境変数読み込み ───────────────────────────────────────
load_dotenv()
DISCORD_TOKEN           = os.getenv("DISCORD_TOKEN")
SLACK_BOT_TOKEN         = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID        = os.getenv("SLACK_CHANNEL_ID")
DAILY_REPORT_CHANNEL_ID = os.getenv("DAILY_REPORT_CHANNEL_ID")
KINTAI_WEBHOOK_URL      = os.getenv("KINTAI_WEBHOOK_URL")      # e.g. https://your-app.vercel.app/api/discord/webhook
KINTAI_WEBHOOK_SECRET   = os.getenv("KINTAI_WEBHOOK_SECRET")    # Bearer token for auth

if not DISCORD_TOKEN or not SLACK_BOT_TOKEN:
    logging.error("DISCORD_TOKEN か SLACK_BOT_TOKEN が設定されていません。")
    exit(1)

# ─── リトライデコレータ ─────────────────────────────────────
def retry(max_retries: int = 3, backoff_factor: float = 2.0):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    logging.warning(f"{func.__name__} failed (attempt {attempt}/{max_retries}): {e}")
                    if attempt == max_retries:
                        logging.error(f"{func.__name__} giving up after {max_retries} attempts")
                        return None
                    await asyncio.sleep(backoff_factor ** (attempt - 1))
        return wrapper
    return decorator

# ─── 状態管理用変数（user_idキーに統一）────────────────────
last_sheet_events = {}   # 最終イベント時刻（key: user_id-xxx）
clock_in_times    = {}   # 出勤時刻（key: user_id）
rest_start_times  = {}   # 休憩開始時刻（key: user_id）
rest_durations    = {}   # 累積休憩時間（秒）（key: user_id）
last_events       = {}   # 多重発火抑制用（key: user_id）
clock_in_estimated = set()  # 出勤時刻が不明な自動補完分。退勤時はwork/breakを送らずwebhook側でDB開始時刻から算出させる

# ─── ユーティリティ関数 ───────────────────────────────────
def normalize(name: str) -> str:
    return name.lower().replace("　", " ").replace("・", " ").strip() if name else ""

def generate_event_hash(user_id, event_type, channel_name, timestamp):
    raw = f"{user_id}-{event_type}-{channel_name}-{timestamp.strftime('%Y%m%d%H%M%S%f')}"
    return hashlib.md5(raw.encode()).hexdigest()

def format_duration(seconds: int) -> str:
    minutes = seconds // 60
    hours   = minutes // 60
    minutes = minutes % 60
    return f"{hours:02d}:{minutes:02d}"

# ─── 通知除外ユーザー（Discord user_id指定）────────────────
BLOCKED_USER_IDS = {
    853919733165850654,
    1068155955910557758,
    807508354490040320,
    398693490399379458,
    1269543122078273559,
}

# ─── Slack ユーザーキャッシュ ──────────────────────────────
slack_user_cache = {}

def build_slack_user_cache():
    try:
        headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
        resp = requests.get("https://slack.com/api/users.list", headers=headers, timeout=10).json()
        for m in resp.get("members", []):
            if m.get("deleted"):
                continue
            uid  = m["id"]
            prof = m.get("profile", {})
            slack_user_cache[normalize(prof.get("real_name",""))]    = uid
            slack_user_cache[normalize(prof.get("display_name",""))] = uid
        logging.info("Slack user cache built.")
    except Exception as e:
        logging.error(f"build_slack_user_cache error: {e}")

def get_slack_user_id_sync(discord_name: str):
    norm = normalize(discord_name)
    if norm in slack_user_cache:
        return slack_user_cache[norm]
    try:
        headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
        resp = requests.get("https://slack.com/api/users.list", headers=headers, timeout=10).json()
        for m in resp.get("members", []):
            if m.get("deleted"):
                continue
            uid  = m["id"]
            prof = m.get("profile", {})
            slack_user_cache[normalize(prof.get("real_name",""))]    = uid
            slack_user_cache[normalize(prof.get("display_name",""))] = uid
            if norm in normalize(prof.get("real_name","")) or norm in normalize(prof.get("display_name","")):
                return uid
    except Exception as e:
        logging.error(f"get_slack_user_id_sync error: {e}")
    return None

# ─── 非同期 Slack 通知 with Retry ────────────────────────────
@retry(max_retries=3, backoff_factor=2.0)
async def send_slack_message(text, mention_user_id=None, thread_ts=None, use_daily_channel=False):
    channel = DAILY_REPORT_CHANNEL_ID if use_daily_channel else SLACK_CHANNEL_ID
    msg     = f"<@{mention_user_id}>\n{text}" if mention_user_id else text
    payload = {"channel": channel, "text": msg}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json"
            },
            json=payload
        ) as resp:
            data = await resp.json()
            if not data.get("ok"):
                raise Exception(f"Slack API error: {data}")
            return data.get("ts")

# ─── 勤怠システム Webhook 連携 ────────────────────────────
@retry(max_retries=2, backoff_factor=1.0)
async def notify_kintai_webhook(event_type: str, discord_user_id: int, timestamp: datetime, break_seconds: int = 0, session_work_seconds: int = 0):
    """勤怠管理システムに出退勤イベントを送信（DB記録用）"""
    if not KINTAI_WEBHOOK_URL or not KINTAI_WEBHOOK_SECRET:
        return  # 未設定時はスキップ
    timeout = aiohttp.ClientTimeout(total=5)
    payload = {
        "event": event_type,
        "discordUserId": str(discord_user_id),
        "timestamp": timestamp.isoformat(),
        "notifySlack": False,
    }
    if event_type == "VOICE_LEAVE":
        if break_seconds > 0:
            payload["breakMinutes"] = break_seconds // 60
        if session_work_seconds > 0:
            payload["sessionWorkSeconds"] = session_work_seconds
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.post(
            KINTAI_WEBHOOK_URL,
            headers={
                "Authorization": f"Bearer {KINTAI_WEBHOOK_SECRET}",
                "Content-Type": "application/json",
            },
            json=payload,
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                logging.warning(f"Kintai webhook returned {resp.status}: {data}")
            else:
                logging.info(f"Kintai webhook OK: {event_type} for {discord_user_id}")

# ─── Discord クライアント設定 ─────────────────────────────
intents = discord.Intents.default()
intents.voice_states = True
intents.members      = True
client = discord.Client(intents=intents)

@client.event
async def on_voice_state_update(member, before, after):
    try:
        now  = datetime.now(JST)
        uid  = member.id
        name = member.display_name

        if uid in BLOCKED_USER_IDS:
            return

        before_ch = before.channel
        after_ch  = after.channel

        # チャンネル変化が無いイベント（ミュート/画面共有/挙手等）は無視
        if before_ch == after_ch:
            return

        # 3秒以内の同一遷移の重複発火を抑制
        ehash = generate_event_hash(
            uid, "transition",
            f"{before_ch.id if before_ch else 0}->{after_ch.id if after_ch else 0}", now,
        )
        prev = last_events.get(uid)
        if prev and (now - prev["timestamp"]).total_seconds() < 3 and prev["event_hash"] == ehash:
            return
        last_events[uid] = {"timestamp": now, "event_hash": ehash}

        # 休憩室の入退室（勤務中のみ休憩として加算）
        if after_ch and after_ch.name == "休憩室":
            rest_start_times[uid] = now
        if before_ch and before_ch.name == "休憩室":
            start = rest_start_times.pop(uid, None)
            if start and uid in clock_in_times:
                rest_durations[uid] = rest_durations.get(uid, 0) + (now - start).total_seconds()

        now_in_work = after_ch is not None and after_ch.name != "休憩室"

        # 出勤: 勤務部屋に入った瞬間。新規 join だけでなく「休憩室→勤務部屋」等の"移動"でも成立させる
        # （これが無いと最初の着地が休憩室だった日は終日打刻されない＝今回の取りこぼしの主因）
        if now_in_work and uid not in clock_in_times:
            rest_durations[uid] = 0
            clock_in_times[uid] = now
            clock_in_estimated.discard(uid)
            last_sheet_events[f"{uid}-clock_in"] = now
            await send_slack_message(
                f"{name} が「{after_ch.name}」に出勤しました。\n"
                f"出勤時間\n{now.strftime('%Y/%m/%d %H:%M:%S')}"
            )
            await notify_kintai_webhook("VOICE_JOIN", uid, now)
            return

        # 退勤: 全 voice チャンネルから離脱（勤務中のみ）
        if after_ch is None and uid in clock_in_times:
            clock_in  = clock_in_times.pop(uid, None)
            estimated = uid in clock_in_estimated
            clock_in_estimated.discard(uid)
            rest_sec  = rest_durations.pop(uid, 0)
            work_sec  = int((now - clock_in).total_seconds() - rest_sec) if clock_in else 0

            msg = (
                f"{name} が「{before_ch.name}」を退出しました。\n"
                f"退勤時間\n{now.strftime('%Y/%m/%d %H:%M:%S')}\n\n"
                f"勤務時間\n{format_duration(work_sec)}"
            )
            ts = await send_slack_message(msg)
            if ts:
                await asyncio.sleep(2)
                slack_uid = await asyncio.to_thread(get_slack_user_id_sync, name)
                mention = f"<@{slack_uid}>\n" if slack_uid else ""
                thread_msg = (
                    f"{mention}以下のテンプレを <#{DAILY_REPORT_CHANNEL_ID}> に記載してください：\n"
                    "◆日報一言テンプレート\nやったこと\n・\n次にやること\n・\nひとこと\n・"
                )
                await send_slack_message(thread_msg, thread_ts=ts)
            # 出勤時刻が不明（再起動後の自動補完）の時は work/break を送らず、webhook 側で
            # DB のオープンセッション開始時刻から算出させる（短時間に誤算出されるのを防ぐ）
            if estimated:
                await notify_kintai_webhook("VOICE_LEAVE", uid, now)
            else:
                await notify_kintai_webhook("VOICE_LEAVE", uid, now, break_seconds=rest_sec, session_work_seconds=work_sec)
            return

        # 移動: voice 内のチャンネル間移動（勤務中のみ Slack 通知）
        if before_ch and after_ch and uid in clock_in_times:
            last = last_sheet_events.get(f"{uid}-move")
            if not last or (now - last).total_seconds() >= 3:
                last_sheet_events[f"{uid}-move"] = now
                await send_slack_message(f"{name} が「{after_ch.name}」に移動しました。")
            return

    except Exception as e:
        logging.error(f"on_voice_state_update error: {e}")

async def monitor_voice_channels():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            now = datetime.now(JST)
            for guild in client.guilds:
                for member in guild.members:
                    uid  = member.id
                    name = member.display_name

                    if uid in BLOCKED_USER_IDS:
                        continue

                    voice   = member.voice
                    in_work = voice is not None and voice.channel is not None and voice.channel.name != "休憩室"

                    # リコンサイル(出勤補完): 勤務部屋に居るのに未追跡＝イベント取りこぼし/再起動。
                    # 開始時刻は不明なので estimated 印を付け、退勤時は webhook 側でDB開始時刻から算出させる。
                    if in_work and uid not in clock_in_times:
                        clock_in_times[uid] = now
                        rest_durations[uid] = 0
                        clock_in_estimated.add(uid)
                        last_sheet_events[f"{uid}-clock_in"] = now
                        logging.info(f"[reconcile] auto clock-in {name} ({uid})")
                        await notify_kintai_webhook("VOICE_JOIN", uid, now)
                        continue

                    # 強制退勤: 追跡中なのに voice から消えている（退勤イベント取りこぼし対策）
                    if uid in clock_in_times and (voice is None or voice.channel is None):
                        clock_in = clock_in_times.pop(uid)
                        elapsed  = (now - clock_in).total_seconds()
                        if elapsed < 60:
                            clock_in_times[uid] = clock_in   # 一時的なキャッシュ欠落の可能性 → 戻して次周期で再判定
                            continue

                        estimated = uid in clock_in_estimated
                        clock_in_estimated.discard(uid)
                        rest_sec = rest_durations.pop(uid, 0)
                        work_sec = int((now - clock_in).total_seconds() - rest_sec)

                        msg = (
                            f"{name} の接続が切れました（強制退勤と見なします）。\n"
                            f"退勤時間\n{now.strftime('%Y/%m/%d %H:%M:%S')}\n\n"
                            f"勤務時間\n{format_duration(work_sec)}"
                        )
                        ts = await send_slack_message(msg)
                        if ts:
                            await asyncio.sleep(2)
                            slack_uid = await asyncio.to_thread(get_slack_user_id_sync, name)
                            mention = f"<@{slack_uid}>\n" if slack_uid else ""
                            thread_msg = (
                                f"{mention}以下のテンプレを <#{DAILY_REPORT_CHANNEL_ID}> に記載してください：\n"
                                "◆日報一言テンプレート\nやったこと\n・\n次にやること\n・\nひとこと\n・"
                            )
                            await send_slack_message(thread_msg, thread_ts=ts)
                        if estimated:
                            await notify_kintai_webhook("VOICE_LEAVE", uid, now)
                        else:
                            await notify_kintai_webhook("VOICE_LEAVE", uid, now, break_seconds=rest_sec, session_work_seconds=work_sec)

        except Exception as e:
            logging.error(f"monitor_voice_channels error: {e}")

        await asyncio.sleep(15)

# ─── Flask アプリ（ヘルスチェック）───────────────────────
app = Flask(__name__)

@app.route("/")
def health_check():
    return "OK"

# ─── Discord クライアント起動（レートリミット対策付き）─────
async def start_discord_client_with_retry():
    backoff = 10        # 初期待機秒
    max_backoff = 600   # 最大待機秒（10分）

    while True:
        try:
            logging.info("Starting Discord client...")
            await client.start(DISCORD_TOKEN)
        except discord.HTTPException as e:
            if getattr(e, "status", None) == 429:
                logging.error(f"Discord login rate limited (HTTP 429). Waiting {backoff} seconds before retry.")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue
            logging.exception("Discord HTTPException occurred. Waiting 60 seconds before retry.")
            await asyncio.sleep(60)
        except Exception:
            logging.exception("Unexpected error in Discord client. Waiting 60 seconds before retry.")
            await asyncio.sleep(60)

def run_discord_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_discord_client_with_retry())

@client.event
async def on_ready():
    logging.info(f"{client.user} is ready. Starting monitoring task.")
    client.loop.create_task(monitor_voice_channels())

if __name__ == "__main__":
    build_slack_user_cache()
    Thread(target=run_discord_bot, daemon=True).start()
    from waitress import serve
    port = int(os.environ.get("PORT", 5000))
    serve(app, host="0.0.0.0", port=port)

