import discord
import requests
import aiohttp
import json
import datetime
import pytz
import base64
import asyncio
import logging
import tweepy
from flask import Flask
import threading
import os
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from collections import deque  # メッセージ履歴の管理に使用
from dotenv import load_dotenv

session = None 

load_dotenv()

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is alive!"

# Flask を別スレッドで実行
def run():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

thread = threading.Thread(target=run)
thread.start()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

twitter_thread = threading.Thread(target=start_twitter_bot)
twitter_thread.start()

# 設定
TOKEN = os.getenv('TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GUILD_ID = int(os.getenv("GUILD_ID")) 
DATA_FILE = "notifications.json"
DAILY_FILE = "daily_notifications.json"
LOG_FILE = "conversation_logs.json"
JST = pytz.timezone("Asia/Tokyo")

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

# メッセージ履歴を管理（最大5件）
conversation_logs = {}

 # user_idごとの時間設定 {"hour": int, "minute": int}
sleep_check_times = {}

# インテント設定
intents = discord.Intents.default()
intents.dm_messages = True
intents.message_content = True
intents.presences = True
intents.members = True 

bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=JST)

print(f"使用中のAPIキー: {GEMINI_API_KEY[:10]}****")

def load_sleep_check_times():
    url = f"{SUPABASE_URL}/rest/v1/sleep_check_times?select=*"
    response = requests.get(url, headers=SUPABASE_HEADERS)
    if response.status_code == 200:
        return {row["user_id"]: {"hour": row["hour"], "minute": row["minute"]} for row in response.json()}
    return {}

def save_sleep_check_times(data):
    for user_id, time_data in data.items():
        # まず削除
        url = f"{SUPABASE_URL}/rest/v1/sleep_check_times?user_id=eq.{user_id}"
        requests.delete(url, headers=SUPABASE_HEADERS)

        # 再登録
        insert_data = {
            "user_id": user_id,
            "hour": time_data["hour"],
            "minute": time_data["minute"]
        }
        requests.post(f"{SUPABASE_URL}/rest/v1/sleep_check_times", headers=SUPABASE_HEADERS, json=[insert_data])

# 会話ログの読み書き
def load_conversation_logs():
    url = f"{SUPABASE_URL}/rest/v1/conversation_logs?select=*"
    response = requests.get(url, headers=SUPABASE_HEADERS)
    if response.status_code == 200:
        data = response.json()
        logs = {}
        for item in data:
            logs.setdefault(item["user_id"], []).append({
                "role": item["role"],
                "parts": [{"text": item["content"]}]
            })
        return logs
    return {}

def save_conversation_logs(logs):
    for user_id, messages in logs.items():
        # そのユーザーの会話ログだけ削除
        url = f"{SUPABASE_URL}/rest/v1/conversation_logs?user_id=eq.{user_id}"
        requests.delete(url, headers=SUPABASE_HEADERS)

        # そのユーザーの会話ログを保存
        insert_data = []
        for m in messages:
            insert_data.append({
                "user_id": user_id,
                "role": m["role"],
                "content": m["parts"][0]["text"]
            })
        if insert_data:
            requests.post(f"{SUPABASE_URL}/rest/v1/conversation_logs", headers=SUPABASE_HEADERS, json=insert_data)

# ← 通知データ
def load_notifications():
    url = f"{SUPABASE_URL}/rest/v1/notifications?select=*"
    response = requests.get(url, headers=SUPABASE_HEADERS)
    if response.status_code == 200:
        result = {}
        for row in response.json():
            result.setdefault(row['user_id'], []).append({
                "date": row["date"],
                "time": row["time"],
                "message": row["message"],
                "repeat": row.get("repeat", False)  # ← 追加！
            })
        return result
    return {}

def save_notifications(notifications):
    for user_id, items in notifications.items():
        # まずそのユーザーの通知だけ削除
        url = f"{SUPABASE_URL}/rest/v1/notifications?user_id=eq.{user_id}"
        requests.delete(url, headers=SUPABASE_HEADERS)

        insert_data = []
        for item in items:
            insert_data.append({
                "user_id": user_id,
                "date": item["date"],
                "time": item["time"],
                "message": item["message"],
                "repeat": item.get("repeat", False)
            })

        if insert_data:
            requests.post(f"{SUPABASE_URL}/rest/v1/notifications", headers=SUPABASE_HEADERS, json=insert_data)

notifications = load_notifications()

# ← 毎日通知
def load_daily_notifications():
    url = f"{SUPABASE_URL}/rest/v1/daily_notifications?select=*"
    response = requests.get(url, headers=SUPABASE_HEADERS)
    if response.status_code == 200:
        result = {}
        for row in response.json():
            todos = row.get("todos") or []
            if isinstance(todos, str):
                try:
                    todos = json.loads(todos)
                except:
                    todos = []
            result[row["user_id"]] = {
                "todos": todos,
                "time": {
                    "hour": row.get("hour", 8),
                    "minute": row.get("minute", 0)
                }
            }
        return result
    return {}

def save_daily_notifications(daily_notifications):
    for user_id, val in daily_notifications.items():
        # まずそのユーザーのデータだけ削除
        url = f"{SUPABASE_URL}/rest/v1/daily_notifications?user_id=eq.{user_id}"
        requests.delete(url, headers=SUPABASE_HEADERS)
        insert_data = {
            "user_id": user_id,
            "todos": json.dumps(val["todos"], ensure_ascii=False),
            "hour": val["time"]["hour"],
            "minute": val["time"]["minute"]
        }
        requests.post(f"{SUPABASE_URL}/rest/v1/daily_notifications", headers=SUPABASE_HEADERS, json=[insert_data])

daily_notifications = load_daily_notifications()

def schedule_sleep_check():
    """睡眠チェックのスケジュールを設定"""
    print("🌙 sleep_check_times をスケジューリングします...")
    
    # 既存の睡眠チェック関連ジョブを削除
    for job in scheduler.get_jobs():
        if "sleep_check_" in job.id:
            scheduler.remove_job(job.id)
    
    # sleep_check_times を再読み込み
    global sleep_check_times
    sleep_check_times = load_sleep_check_times()
    
    # 各ユーザーの睡眠チェック時間をスケジュール
    for user_id, time_data in sleep_check_times.items():
        hour = time_data.get("hour", 1)
        minute = time_data.get("minute", 0)
        print(f"🛌 スケジュール設定: ユーザー {user_id} → {hour}:{minute}")
        
        scheduler.add_job(
            check_user_sleep_status,
            'cron',
            hour=hour,
            minute=minute,
            args=[user_id],
            id=f"sleep_check_{user_id}",
            replace_existing=True,
            timezone=JST
        )

def start_twitter_bot():
    try:
        auth = tweepy.OAuth1UserHandler(
            os.getenv("TWITTER_CONSUMER_KEY"),
            os.getenv("TWITTER_CONSUMER_SECRET"),
            os.getenv("TWITTER_ACCESS_TOKEN"),
            os.getenv("TWITTER_ACCESS_SECRET")
        )
        api = tweepy.API(auth)

        bot_username = os.getenv("TWITTER_BOT_USERNAME")

        class MentionListener(tweepy.StreamingClient):
            def on_tweet(self, tweet):
                if tweet.author_id == api.verify_credentials().id:
                    return  # 自分自身には反応しない

                if f"@{bot_username.lower()}" in tweet.text.lower():
                    print(f"📨 メンション受信: {tweet.text}")
                    
                    # Gemini で応答を生成
                    response_text = asyncio.run(get_gemini_response(str(tweet.author_id), tweet.text))
                    
                    # リプライ送信
                    try:
                        api.update_status(
                            status=f"@{tweet.author.username} {response_text}",
                            in_reply_to_status_id=tweet.id,
                            auto_populate_reply_metadata=True
                        )
                        print(f"✅ リプライ送信: {response_text}")
                    except Exception as e:
                        print(f"❌ リプライ送信失敗: {e}")

        stream = MentionListener(os.getenv("TWITTER_BEARER_TOKEN"))
        stream.add_rules(tweepy.StreamRule(f"@{bot_username}"))
        stream.filter(tweet_fields=["author_id", "text"])
    except Exception as e:
        print(f"❌ TwitterBot起動エラー: {e}")

@bot.event
async def on_ready():
    global session, sleep_check_times
    try:
        if session is None:
            session = aiohttp.ClientSession()
            
        await bot.change_presence(activity=discord.Game(name="ハニーとおしゃべり"))
        print(f"Logged in as {bot.user}")
        await bot.tree.sync()

        # スケジューラーを開始
        scheduler.start()
        
        # データを再読み込み
        global daily_notifications
        daily_notifications = load_daily_notifications()
        sleep_check_times = load_sleep_check_times()  # ← この行を追加

        # すべてのジョブをクリアして再設定
        scheduler.remove_all_jobs()
        setup_periodic_reload()
        schedule_notifications()
        schedule_daily_todos()
        schedule_sleep_check()  # ← 関数名を修正（sなし）

        print("スケジュールを設定しました。")
        print("🗓️ sleep_check_times:", sleep_check_times)
        print("スケジュールされたジョブ:")
        for job in scheduler.get_jobs():
            print(f"- {job.id}: 次回実行 {job.next_run_time}")
            
    except Exception as e:
        print(f"エラー: {e}")

@bot.event
async def on_resumed():
    print("⚡ Botが再接続したよ！スケジュールを立て直すね！")
    scheduler.remove_all_jobs()
    setup_periodic_reload()
    schedule_notifications()
    schedule_daily_todos()
    schedule_sleep_check()
    
# 通知設定コマンド
@bot.tree.command(name="set_notification", description="通知を設定するよ～！")
async def set_notification(interaction: discord.Interaction, date: str, time: str, message: str, repeat: bool = False):
    try:
        datetime.datetime.strptime(date, "%m-%d")
        datetime.datetime.strptime(time, "%H:%M")
    except ValueError:
        await interaction.response.send_message("日付か時刻の形式が正しくないよ～！", ephemeral=True)
        return

    user_id = str(interaction.user.id)
    if user_id not in notifications:
        notifications[user_id] = []

    notifications[user_id].append({
        "date": date,
        "time": time,
        "message": message,
        "repeat": repeat
    })
    save_notifications(notifications)
    await interaction.response.send_message(f'✅ {date} の {time} に "{message}" を登録したよ！リピート: {"あり" if repeat else "なし"}', ephemeral=True)
    schedule_notifications()
    
# タイマー設定コマンド
@bot.tree.command(name="set_notification_after", description="○時間○分後に通知を設定するよ！")
async def set_notification_after(interaction: discord.Interaction, hours: int, minutes: int, message: str):
    if hours < 0 or minutes < 0 or (hours == 0 and minutes == 0):
        await interaction.response.send_message("⛔ 1分以上後の時間を指定してね～！", ephemeral=True)
        return

    user_id = str(interaction.user.id)
    now = datetime.datetime.now(JST)
    future_time = now + datetime.timedelta(hours=hours, minutes=minutes)

    info = {
        "date": future_time.strftime("%m-%d"),
        "time": future_time.strftime("%H:%M"),
        "message": message,
        "repeat": False
    }

    # 通知データに保存
    if user_id not in notifications:
        notifications[user_id] = []
    notifications[user_id].append(info)
    save_notifications(notifications)

    # 通知ジョブを追加（即時スケジューリング）
    scheduler.add_job(
        send_notification_message,
        'date',
        run_date=future_time,
        args=[user_id, info],
        id=f"after_notification_{user_id}_{int(future_time.timestamp())}"  # 一意なID
    )

    await interaction.response.send_message(
        f"⏰ {hours}時間{minutes}分後（{future_time.strftime('%H:%M')}）に「{message}」を通知するよ～！",
        ephemeral=True
    )

# 通知一覧表示
@bot.tree.command(name="list_notifications", description="登録してる通知を表示するよ！")
async def list_notifications(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)  # ← deferはここ！

    user_id = str(interaction.user.id)

    if user_id not in notifications or not notifications[user_id]:
        await interaction.followup.send("登録されてる通知はないよ～", ephemeral=True)
        return

    notif_texts = [f"{i+1}️⃣ 📅 {n['date']} ⏰ {n['time']} - {n['message']}" for i, n in enumerate(notifications[user_id])]
    full_text = "\n".join(notif_texts)

    if len(full_text) > 1900:
        await interaction.followup.send("通知が多すぎて全部表示できないよ～！いくつか削除してね～！", ephemeral=True)
    else:
        await interaction.followup.send(full_text, ephemeral=True)

# 通知削除
@bot.tree.command(name="remove_notification", description="特定の通知を削除するよ！")
async def remove_notification(interaction: discord.Interaction, index: int):
    user_id = str(interaction.user.id)
    
    # ユーザーの通知がなければエラーメッセージを送信
    if user_id not in notifications or not notifications[user_id] or index < 1 or index > len(notifications[user_id]):
        await interaction.response.send_message("指定された通知が見つからないよ～", ephemeral=True)
        return
    
    # 通知を削除
    removed = notifications[user_id].pop(index - 1)
    
    # 通知を保存し、スケジュールを更新
    save_notifications(notifications)
    schedule_notifications()

    # 日付と時刻を除いたメッセージ内容を作成
    message_content = removed['message']

    # 削除した通知の内容を送信
    await interaction.response.send_message(
        f"✅ 「{message_content}」を削除したよ～！",
        ephemeral=True
    )

async def send_notification_message(user_id, info):
    try:
        user = await bot.fetch_user(int(user_id))
        if user:
            await user.send(info["message"])

        # 送った後、repeatフラグによって処理を分岐
        uid = str(user_id)
        if uid in notifications:
            for notif in notifications[uid]:
                if (notif["date"] == info["date"] and
                    notif["time"] == info["time"] and
                    notif["message"] == info["message"]):

                    if notif.get("repeat", False):
                        # 繰り返しなら → 年を+1して再スケジュール
                        now = datetime.datetime.now(JST)
                        next_year_date = datetime.datetime.strptime(f"{now.year}-{notif['date']}", "%Y-%m-%d") + datetime.timedelta(days=365)
                        notif["date"] = next_year_date.strftime("%m-%d")
                    else:
                        # 一回きりなら → 通知リストから削除
                        notifications[uid].remove(notif)

                    save_notifications(notifications)
                    schedule_notifications()
                    break

    except discord.NotFound:
        print(f"Error: User with ID {user_id} not found.")

@bot.tree.command(name="add_daily_todo", description="毎日送信する通知を追加するよ！")
async def add_daily_todo(interaction: discord.Interaction, message: str):
    user_id = str(interaction.user.id)
    if user_id not in daily_notifications:
        daily_notifications[user_id] = {"todos": [], "time": {"hour": 8, "minute": 0}}  # デフォルト8:00
    daily_notifications[user_id]["todos"].append(message)
    save_daily_notifications(daily_notifications)
    await interaction.response.send_message(f'✅ "{message}" って毎日通知するね～！', ephemeral=True)

@bot.tree.command(name="list_daily_todos", description="毎日送るTodoリストを確認するよ！")
async def list_daily_todos(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    user_id = str(interaction.user.id)
    user_data = daily_notifications.get(user_id)

    if not user_data or not user_data.get("todos"):
        await interaction.followup.send("Todoリストは空っぽだよ～！", ephemeral=True)
        return

    todos = user_data["todos"]
    msg = "\n".join([f"{i+1}. {item}" for i, item in enumerate(todos)])
    await interaction.followup.send(f"📋 あなたのTodoリスト：\n{msg}", ephemeral=True)

@bot.tree.command(name="remove_daily_todo", description="Todoを削除するよ！")
async def remove_daily_todo(interaction: discord.Interaction, index: int):
    user_id = str(interaction.user.id)
    user_data = daily_notifications.get(user_id)

    if not user_data or index < 1 or index > len(user_data.get("todos", [])):
        await interaction.response.send_message("指定されたTodoが見つからなかったよ～！", ephemeral=True)
        return

    removed = user_data["todos"].pop(index - 1)
    save_daily_notifications(daily_notifications)
    await interaction.response.send_message(f"✅ 「{removed}」を削除したよ～！", ephemeral=True)

@bot.tree.command(name="set_daily_time", description="毎日Todo通知を送る時間を設定するよ！（24時間制）")
async def set_daily_time(interaction: discord.Interaction, hour: int, minute: int):
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await interaction.response.send_message("⛔ 時間の形式が正しくないよ！(0-23時, 0-59分)", ephemeral=True)
        return

    user_id = str(interaction.user.id)
    if user_id not in daily_notifications:
        daily_notifications[user_id] = {"todos": [], "time": {"hour": hour, "minute": minute}}
    else:
        daily_notifications[user_id]["time"] = {"hour": hour, "minute": minute}
    save_daily_notifications(daily_notifications)

    schedule_daily_todos()  # ← これを追加

    await interaction.response.send_message(f"✅ 毎日 {hour:02d}:{minute:02d} に通知するように設定したよ！", ephemeral=True)

# 指定メッセージ削除
@bot.tree.command(name="delete_message", description="指定したメッセージIDのメッセージを削除するよ～！")
async def delete_message(interaction: discord.Interaction, message_id: str):
    try:
        user = await bot.fetch_user(interaction.user.id)
        if user:
            dm_channel = await user.create_dm()
            msg = await dm_channel.fetch_message(int(message_id))
            await msg.delete()
            await interaction.response.send_message("✅ 指定したメッセージを削除したよ～！", ephemeral=True)
        else:
            await interaction.response.send_message("❌ メッセージを削除できなかったよ～！", ephemeral=True)
    except discord.NotFound:
        await interaction.response.send_message("❌ 指定したメッセージが見つからなかったよ～！", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ メッセージを削除する権限がないよ～！", ephemeral=True)
    except ValueError:
        await interaction.response.send_message("❌ メッセージIDは数字で入力してね～！", ephemeral=True)
        
# 夜ふかし注意時間設定コマンド
@bot.tree.command(name="set_sleep_check_time", description="寝る時間チェックの送信時刻を設定するよ！（24時間制）")
async def set_sleep_check_time(interaction: discord.Interaction, hour: int, minute: int):
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await interaction.response.send_message("⛔ 時間の形式が正しくないよ！(0-23時, 0-59分)", ephemeral=True)
        return

    user_id = str(interaction.user.id)
    sleep_check_times[user_id] = {"hour": hour, "minute": minute}
    save_sleep_check_times(sleep_check_times)

    schedule_sleep_check()  # ← 関数名を修正（sなし）

    await interaction.response.send_message(f"✅ 毎日 {hour:02d}:{minute:02d} に寝たほうがいいよ～メッセージを送るようにしたよ！", ephemeral=True)

# Gemini APIを使った会話
CHARACTER_PERSONALITY = """
設定:
・あなたの名前は「ドロシー」です
・一人称は「あたし」
・グリッチシティに住んでいます

口調：
・元気なかわいい女の子のように話す
・ユーザーのあだ名は「ハニー」
・あなたのあだ名は「ドロシー」

重要:
・会話の中で絵文字を使用しないでください、ただし絵文字は要求された場合は使用可能です。
・語尾に わよ は使用しないでください
・小学生程度の子どものような喋り方です
・ひらがなを使って話します
・敬語は使わない
・相手の話や画像に自然に反応するようにしてください。
・会話の途中でいきなり自己紹介をしないでください
"""
async def get_gemini_response(user_id, user_input):
    global session
    if user_id not in conversation_logs:
        conversation_logs[user_id] = []

    now = datetime.datetime.now(JST)
    current_time = now.strftime("%Y-%m-%d %H:%M:%S")
    conversation_logs[user_id].append({
        "role": "user",
        "parts": [{"text": user_input}],
        "timestamp": current_time
    })
    conversation_logs[user_id] = conversation_logs[user_id][-7:]  # トークン節約のため10件に減らす

    messages = [{"role": "user", "parts": [{"text": CHARACTER_PERSONALITY}]}]
    for m in conversation_logs[user_id]:
        messages.append({
            "role": m["role"],
            "parts": m["parts"]
        })

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"  # ← 修正
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}
    data = {"contents": messages}

    async with session.post(url, headers=headers, params=params, json=data) as response:
        print(f"Gemini API status: {response.status}")
        if response.status == 200:
            response_json = await response.json()
            reply_text = response_json.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "エラー: 応答が取得できませんでした。")

            conversation_logs[user_id].append({
                "role": "model",
                "parts": [{"text": reply_text}],
                "timestamp": current_time
            })
            conversation_logs[user_id] = conversation_logs[user_id][-7:]
            save_conversation_logs(conversation_logs)
            return reply_text
        else:
            if response.status == 429:
                return "⚠️ 今はおしゃべりの回数が上限に達しちゃったみたい！明日また話そうね～！"
            else:
                return f"⚠️ ごめんね、うまくお返事できなかったよ～！（{response.status}）"

async def get_gemini_response_with_image(user_id, user_input, image_bytes=None, image_mime_type="image/png"):
    global session
    if user_id not in conversation_logs:
        conversation_logs[user_id] = []

    messages = [{"role": "user", "parts": [{"text": CHARACTER_PERSONALITY}]}]

    parts = []
    if user_input:
        parts.append({"text": user_input})
    if image_bytes:
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        parts.append({
            "inline_data": {
                "mime_type": image_mime_type,
                "data": base64_image
            }
        })

    messages.append({"role": "user", "parts": parts})

    url = "https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash:generateContent"
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}
    data = {"contents": messages}

    async with session.post(url, headers=headers, params=params, json=data) as response:
        if response.status == 200:
            response_json = await response.json()
            reply_text = response_json.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "エラー: 応答が取得できませんでした。")
            return reply_text
        else:
            return f"エラー: {response.status} - {await response.text()}"

# DMでメッセージを受信
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.guild is None:
        image_bytes = None
        image_mime_type = "image/png"

        if message.attachments:
            attachment = message.attachments[0]
            if attachment.content_type and attachment.content_type.startswith("image/"):
                image_bytes = await attachment.read()
                image_mime_type = attachment.content_type

        if image_bytes:
            response = await get_gemini_response_with_image(str(message.author.id), message.content, image_bytes, image_mime_type)
            conversation_logs[str(message.author.id)] = []
        else:
            response = await get_gemini_response(str(message.author.id), message.content)

        await message.channel.send(response)
        
    await bot.process_commands(message)

# 通知スケジューリング
def schedule_notifications():
    # 通知関連のジョブのみを削除（job_idにnotificationが含まれるもの）
    for job in scheduler.get_jobs():
        if "notification_" in job.id:
            scheduler.remove_job(job.id)
            
    now = datetime.datetime.now(JST)
    for user_id, notif_list in notifications.items():
        for i, info in enumerate(notif_list):
            date_time_str = f"{now.year}-{info['date']} {info['time']}"
            try:
                notification_time = JST.localize(datetime.datetime.strptime(date_time_str, "%Y-%m-%d %H:%M"))
                if notification_time < now:
                    notification_time = notification_time.replace(year=now.year + 1)
                scheduler.add_job(
                    send_notification_message, 
                    'date', 
                    run_date=notification_time, 
                    args=[user_id, info],
                    id=f"notification_{user_id}_{i}"  # 一意のIDを設定
                )
            except ValueError:
                pass

def schedule_daily_todos():
    print("毎日のTodoスケジュールを設定します...")
    for user_id, data in daily_notifications.items():
        hour = data.get("time", {}).get("hour", 8)
        minute = data.get("time", {}).get("minute", 0)

        job_id = f"todo_{user_id}"
        scheduler.add_job(
            send_user_todo,
            'cron',
            hour=hour,
            minute=minute,
            args=[int(user_id)],
            id=job_id,  # ジョブIDが被ると追加できないので
            replace_existing=True,  # ← これを追加！
            timezone=JST  # タイムゾーンを明示的に指定
        )
        print(f"ユーザー {user_id} のTodo通知を {hour}:{minute} (JST) に設定しました")

def setup_periodic_reload():
    scheduler.add_job(
        reload_all_data,
        'interval', 
        hours=1,
        id="periodic_reload",
        replace_existing=True
    )

async def reload_all_data():
    global notifications, daily_notifications, conversation_logs, sleep_check_times
    print("データを再読み込みします...")
    notifications = load_notifications()
    daily_notifications = load_daily_notifications()
    conversation_logs = load_conversation_logs()
    sleep_check_times = load_sleep_check_times()  # ← この行を追加
    
    # スケジュールも再設定
    schedule_notifications()
    schedule_daily_todos()
    schedule_sleep_check()  # ← この行を追加
    print("データの再読み込みが完了しました")

async def send_user_todo(user_id: int):
    try:
        user_data = daily_notifications.get(str(user_id), {})
        todos = user_data.get("todos", [])
        print(f"ユーザー {user_id} のTodo送信: {todos}")
        if todos:
            user = await bot.fetch_user(user_id)
            msg = "おはよ～ハニー！今日のToDoリストだよ～！\n" + "\n".join([f"- {todo}" for todo in todos])
            await user.send(msg)
            print(f"ユーザー {user_id} にTodoを送信しました")
    except Exception as e:
        print(f"Todo送信エラー (ユーザー {user_id}): {e}")

async def check_user_sleep_status(user_id: str):
    try:
        # ギルドを取得
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            logger.warning("❌ ギルドが取得できません。GUILD_IDが正しいか確認してね")
            return

        # メンバー情報を取得
        member = guild.get_member(int(user_id))
        if member is None:
            logger.warning(f"⚠️ ユーザー {user_id} はこのサーバーにいないよ")
            return

        # ステータスがオンラインのときだけ通知
        if member.status == discord.Status.online:
            message_text = "もうこんな時間だよ〜！はやくねたほうがいいよー💤"
            user = await bot.fetch_user(int(user_id))
            await user.send(message_text)  # DMで送信

            now = datetime.datetime.now(JST)
            if user_id not in conversation_logs:
                conversation_logs[user_id] = []
            conversation_logs[user_id].append({
                "role": "model",
                "parts": [{"text": message_text}],
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")
            })
            conversation_logs[user_id] = conversation_logs[user_id][-7:]
            save_conversation_logs(conversation_logs)

            logger.info(f"✅ {user_id} に夜ふかし通知をDMで送信しました")
        else:
            logger.info(f"🛌 ユーザー {user_id} はオンラインではありません（status: {member.status}）")

    except Exception as e:
        logger.error(f"⚠️ {user_id} への睡眠チェック中にエラー: {e}")
        
bot.run(TOKEN)
