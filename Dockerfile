# 課程問答 RAG API 的容器映像檔
# 只用 Python 標準函式庫，不需要安裝任何套件，所以映像檔很小、建置很快。
FROM python:3.12-slim

# 不寫 .pyc、輸出不緩衝（Cloud Run 的紀錄才看得到即時訊息）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/tmp/qa_log.db \
    ASK_DAILY_LIMIT=50

WORKDIR /app
COPY rag.py api.py ./
COPY knowledge_base/ knowledge_base/
COPY eval/questions.json eval/mock_responses.json eval/

# 不用 root 執行，降低被入侵時的影響範圍
RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8080

# 有 ANTHROPIC_API_KEY 就實際呼叫 Claude；沒有就自動改用示範模式（免費）
CMD ["python", "api.py", "--mock-if-no-key"]
