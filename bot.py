import discord
import requests
import json
import datetime
import pytz
import base64
import asyncio
from flask import Flask
import threading
import os
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from collections import deque  # メッセージ履歴の管理に使用
from dotenv import load_dotenv

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

# 設定
TOKEN = os.getenv('TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DATA_FILE = "notifications.json"
DAILY_FILE = "daily_notifications.json"
LOG_FILE = "conversation_logs.json"
JST = pytz.timezone("Asia/Tokyo")
daily_notifications = load_daily_notifications()
notifications = load_notifications()

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

# メッセージ履歴を管理（最大5件）
conversation_logs = {}

# インテント設定
intents = discord.Intents.default()
intents.dm_messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=JST)

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
    requests.delete(f"{SUPABASE_URL}/rest/v1/conversation_logs", headers=SUPABASE_HEADERS)
    insert_data = []
    for user_id, messages in logs.items():
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
                "message": row["message"]
            })
        return result
    return {}

def save_notifications(notifications):
    requests.delete(f"{SUPABASE_URL}/rest/v1/notifications", headers=SUPABASE_HEADERS)
    insert_data = []
    for user_id, items in notifications.items():
        for item in items:
            insert_data.append({
                "user_id": user_id,
                "date": item["date"],
                "time": item["time"],
                "message": item["message"]
            })
    if insert_data:
        requests.post(f"{SUPABASE_URL}/rest/v1/notifications", headers=SUPABASE_HEADERS, json=insert_data)

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
    requests.delete(f"{SUPABASE_URL}/rest/v1/daily_notifications", headers=SUPABASE_HEADERS)
    insert_data = []
    for user_id, val in daily_notifications.items():
        insert_data.append({
            "user_id": user_id,
            "todos": json.dumps(val["todos"], ensure_ascii=False),
            "hour": val["time"]["hour"],
            "minute": val["time"]["minute"]
        })
    if insert_data:
        requests.post(f"{SUPABASE_URL}/rest/v1/daily_notifications", headers=SUPABASE_HEADERS, json=insert_data)

@bot.event
async def on_ready():
    try:
        print(f"Logged in as {bot.user}")
        await bot.tree.sync()
        scheduler.start()
        schedule_notifications()
        schedule_daily_todos()
        print("📅 毎日通知のスケジュールを設定したよ！")
    except Exception as e:
        print(f"エラー: {e}")

# 通知設定コマンド
@bot.tree.command(name="set_notification", description="通知を設定するよ～！")
async def set_notification(interaction: discord.Interaction, date: str, time: str, message: str):
    try:
        datetime.datetime.strptime(date, "%m-%d")
        datetime.datetime.strptime(time, "%H:%M")
    except ValueError:
        await interaction.response.send_message("日付か時刻の形式が正しくないよ～！", ephemeral=True)
        return
    
    user_id = str(interaction.user.id)
    if user_id not in notifications:
        notifications[user_id] = []
    
    notifications[user_id].append({"date": date, "time": time, "message": message})
    save_notifications(notifications)
    await interaction.response.send_message(f'✅ {date} の {time} に "{message}"って通知するね～！', ephemeral=True)
    schedule_notifications()

# 通知一覧表示
@bot.tree.command(name="list_notifications", description="登録してる通知を表示するよ！")
async def list_notifications(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    
    if user_id not in notifications or not notifications[user_id]:
        await interaction.response.send_message("登録されてる通知はないよ～", ephemeral=True)
        return
    
    msg = "\n".join([f"{i+1}️⃣ 📅 {n['date']} ⏰ {n['time']} - {n['message']}" for i, n in enumerate(notifications[user_id])])
    await interaction.response.send_message(msg, ephemeral=True)

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
    user_id = str(interaction.user.id)
    user_data = daily_notifications.get(user_id)

    if not user_data or not user_data.get("todos"):
        await interaction.response.send_message("Todoリストは空っぽだよ～！", ephemeral=True)
        return

    todos = user_data["todos"]
    msg = "\n".join([f"{i+1}. {item}" for i, item in enumerate(todos)])
    await interaction.response.send_message(f"📋 あなたのTodoリスト：\n{msg}", ephemeral=True)

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
def get_gemini_response(user_id, user_input):
    if user_id not in conversation_logs:
        conversation_logs[user_id] = []

    # メッセージ履歴を追加（最大50件まで保存）
    conversation_logs[user_id].append({"role": "user", "parts": [{"text": user_input}]} )
    conversation_logs[user_id] = conversation_logs[user_id][-14:]  # 古い履歴を削除して50件を維持

    # 最後のメッセージから30分経過しているか確認
    if len(conversation_logs[user_id]) > 1:  # 最後のメッセージがユーザーからのものであることを確認
        last_message_time = conversation_logs[user_id][-2].get("timestamp")
        if last_message_time:
            last_time = datetime.datetime.strptime(last_message_time, "%Y-%m-%d %H:%M:%S")
            if (datetime.datetime.now(JST) - last_time).total_seconds() > 1800:  # 30分以上経過していれば
                return "やっほー！ハニー！元気だった～？"

    # 送信データを作成
    messages = [{"role": "user", "parts": [{"text": CHARACTER_PERSONALITY}]}]  # キャラ設定
    messages.extend(conversation_logs[user_id])  # 履歴追加

    url = "https://generativelanguage.googleapis.com/v1/models/gemini-1.5-pro:generateContent"
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}
    data = {"contents": messages}

    response = requests.post(url, headers=headers, params=params, json=data)
    if response.status_code == 200:
        response_json = response.json()
        reply_text = response_json.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "エラー: 応答が取得できませんでした。")

        # AIの応答を履歴に追加（timestampフィールドなし）
        conversation_logs[user_id].append({"role": "model", "parts": [{"text": reply_text}]})
        conversation_logs[user_id] = conversation_logs[user_id][-14:]  # 履歴を50件に維持
        save_conversation_logs(conversation_logs)  # ログを保存
        return reply_text
    else:
        return f"エラー: {response.status_code} - {response.text}"

def get_gemini_response_with_image(user_id, user_input, image_bytes=None, image_mime_type="image/png"):
    if user_id not in conversation_logs:
        conversation_logs[user_id] = []

    # キャラ設定を含む最初のメッセージ
    messages = [{"role": "user", "parts": [{"text": CHARACTER_PERSONALITY}]}]

    # 入力部（画像あり or なし）
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

    url = "https://generativelanguage.googleapis.com/v1/models/gemini-1.5-pro:generateContent"
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}
    data = {"contents": messages}

    response = requests.post(url, headers=headers, params=params, json=data)
    if response.status_code == 200:
        response_json = response.json()
        reply_text = response_json.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "エラー: 応答が取得できませんでした。")
        return reply_text
    else:
        return f"エラー: {response.status_code} - {response.text}"

# DMでメッセージを受信
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.guild is None:
        image_bytes = None
        image_mime_type = "image/png"

        # 添付ファイルの中から画像を探す（最初の画像のみ）
        if message.attachments:
            attachment = message.attachments[0]
            if attachment.content_type and attachment.content_type.startswith("image/"):
                image_bytes = await attachment.read()
                image_mime_type = attachment.content_type

        # 画像があれば画像付き、なければ通常の関数を呼び出し
        if image_bytes:
            response = get_gemini_response_with_image(str(message.author.id), message.content, image_bytes, image_mime_type)
        else:
            response = get_gemini_response(str(message.author.id), message.content)

        await message.channel.send(response)

# 通知スケジューリング
def schedule_notifications():
    scheduler.remove_all_jobs()
    now = datetime.datetime.now(JST)
    for user_id, notif_list in notifications.items():
        for info in notif_list:
            date_time_str = f"{now.year}-{info['date']} {info['time']}"
            try:
                notification_time = JST.localize(datetime.datetime.strptime(date_time_str, "%Y-%m-%d %H:%M"))
                if notification_time < now:
                    notification_time = notification_time.replace(year=now.year + 1)
                scheduler.add_job(send_notification_message, 'date', run_date=notification_time, args=[user_id, info])
            except ValueError:
                pass

def schedule_daily_todos():
    for user_id, data in daily_notifications.items():
        hour = data.get("time", {}).get("hour", 8)
        minute = data.get("time", {}).get("minute", 0)

        scheduler.add_job(
            send_user_todo,
            'cron',
            hour=hour,
            minute=minute,
            args=[int(user_id)],
            id=f"todo_{user_id}",  # ジョブIDが被ると追加できないので
            replace_existing=True  # ← これを追加！
        )

async def send_user_todo(user_id: int):
    user_data = daily_notifications.get(str(user_id), {})
    todos = user_data.get("todos", [])
    if todos:
        user = await bot.fetch_user(user_id)
        msg = "おはよ～ハニー！今日のToDoリストだよ～！\n" + "\n".join([f"- {todo}" for todo in todos])
        await user.send(msg)

bot.run(TOKEN)
