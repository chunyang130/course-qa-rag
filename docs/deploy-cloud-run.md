# 部署到 Google Cloud Run

把課程問答 API 包成容器，部署到 Google Cloud Run，得到一個公開的 HTTPS 網址。
全部在瀏覽器裡的 **Cloud Shell** 完成，自己的電腦不需要安裝 Docker 或 gcloud。

## 設計重點

| 項目 | 做法 | 為什麼 |
| --- | --- | --- |
| 容器 | `Dockerfile`：python:3.12-slim，只複製需要的檔案，用非 root 使用者執行 | 程式只用標準函式庫，映像檔小、建置快；降低被入侵時的影響 |
| 設定 | 埠號、資料庫位置、每日上限都用環境變數 | 同一個映像檔在本機、CI、雲端都能跑，不用改程式 |
| 金鑰 | 放在 Secret Manager，部署時掛成環境變數 | 金鑰不進程式碼、不進映像檔、不進 GitHub |
| 費用控制 | 最多 1 台、沒人用時縮到 0 台；`/ask` 每天上限 50 題（超過回 429）；設定預算警示 | 公開網址可能被任何人呼叫，要避免意外產生 API 與雲端費用 |
| 沒有金鑰時 | 自動改用示範模式 | 可以先免費部署測試，確認流程正確再接上 Claude |
| 自動測試 | GitHub Actions 每次推送都建置映像檔、啟動容器、實際送出請求 | 確認容器可以正常部署，不是只有本機能跑 |

## 步驟

### 1. 建立專案（只做一次）

1. 到 [Google Cloud Console](https://console.cloud.google.com/) 登入，建立新專案，例如 `course-qa-rag`
2. 依畫面指示綁定帳單帳戶（新帳戶通常有免費試用額度）
3. **設定預算警示**：左側選單「帳單」→「預算與快訊」→ 建立預算，金額填小一點（例如 NT$100），超過時寄信通知

### 2. 開啟 Cloud Shell

點 Console 右上角的 `>_` 圖示，下方會開出一個終端機。以下指令都在這裡貼上執行。

```bash
gcloud config set project 你的專案ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com

git clone https://github.com/chunyang130/course-qa-rag.git
cd course-qa-rag
```

### 3. 先部署示範模式（不需要金鑰、不呼叫 Claude）

```bash
gcloud run deploy course-qa-rag --source . --region asia-east1 \
  --allow-unauthenticated --max-instances 1 --memory 512Mi
```

- 問「要不要建立 Artifact Registry 存放區」時回答 `Y`
- 完成後會顯示網址，例如 `https://course-qa-rag-xxxxx.asia-east1.run.app`

測試：

```bash
URL=$(gcloud run services describe course-qa-rag --region asia-east1 --format 'value(status.url)')
curl "$URL/health"
curl -X POST "$URL/ask" -H 'Content-Type: application/json' -d '{"question": "多益衝刺班一期多少錢？"}'
```

`/health` 顯示 `"mode": "mock"` 就代表部署成功。

### 4. 接上 Claude（需要 API 金鑰）

把金鑰存進 Secret Manager。下面的 `read -s` 會讓你貼上金鑰時**不顯示在畫面上，也不會留在指令紀錄裡**：

```bash
read -s -p "貼上 Anthropic API 金鑰後按 Enter：" KEY; echo
printf %s "$KEY" | gcloud secrets create anthropic-api-key --data-file=-
unset KEY

# 讓 Cloud Run 可以讀取這個金鑰
PROJECT_NUMBER=$(gcloud projects describe $(gcloud config get-value project) --format 'value(projectNumber)')
gcloud secrets add-iam-policy-binding anthropic-api-key \
  --member "serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role roles/secretmanager.secretAccessor
```

重新部署，掛上金鑰並設定每日上限：

```bash
gcloud run deploy course-qa-rag --source . --region asia-east1 \
  --allow-unauthenticated --max-instances 1 --memory 512Mi \
  --set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest \
  --set-env-vars ASK_DAILY_LIMIT=50
```

再測一次：`/health` 的 `mode` 會變成模型名稱，`/ask` 可以問知識庫裡的任何問題。

### 5. 用完怎麼關

```bash
gcloud run services delete course-qa-rag --region asia-east1   # 刪除服務
gcloud secrets delete anthropic-api-key                          # 刪除金鑰
```

也可以直接刪除整個專案，所有資源一起移除。

## 常見問題

- **部署時出現權限錯誤（build 失敗、permission denied）**：新專案的建置帳戶權限可能不夠，執行下面指令後再部署一次：
  ```bash
  PROJECT_NUMBER=$(gcloud projects describe $(gcloud config get-value project) --format 'value(projectNumber)')
  gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
    --member "serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" --role roles/run.builder
  ```
- **問答紀錄（`/stats`）重新部署後歸零**：資料庫放在容器的暫存空間（`/tmp`），容器重啟就清空。正式上線要改用 Cloud SQL 等外部資料庫；這個專案是示範，刻意保持簡單。
- **每日上限是每台容器各自計算**：因為設定最多 1 台，所以等於整個服務的上限。
