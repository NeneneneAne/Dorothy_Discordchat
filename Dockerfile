FROM python:3.11-slim

# 必要なパッケージとffmpegをインストール
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean

# 作業ディレクトリ
WORKDIR /app

# 依存関係のインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ソースコードのコピー
COPY . .

# Bot起動
CMD ["python", "bot.py"]
