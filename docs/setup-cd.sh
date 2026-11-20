#!/usr/bin/env bash
# 設定「GitHub 通過測試後自動部署到 Cloud Run」——在 Google Cloud Shell 執行一次即可。
#
# 做法：Workload Identity Federation（工作負載身分聯盟）
#   GitHub Actions 每次執行時，用 GitHub 發的一次性身分證明向 Google 換取短期憑證，
#   所以 GitHub 上不需要存放任何 Google 金鑰；而且只有 chunyang130/course-qa-rag 這個 repo 可以使用。
#
# 用法（在 Cloud Shell）：
#   cd ~/course-qa-rag && git pull && bash docs/setup-cd.sh
set -euo pipefail

REPO="chunyang130/course-qa-rag"
REGION="asia-east1"
POOL="github"
PROVIDER="course-qa-rag"
SA_NAME="github-deployer"

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
if [[ -z "$PROJECT_ID" || "$PROJECT_ID" =~ ^[0-9]+$ ]]; then
  echo "請先設定專案 ID（不是專案編號）：gcloud config set project 你的專案ID"; exit 1
fi
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format 'value(projectNumber)')
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
echo "專案：$PROJECT_ID（$PROJECT_NUMBER）"

echo "1/5 啟用需要的服務"
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  run.googleapis.com artifactregistry.googleapis.com --quiet

echo "2/5 建立部署專用的服務帳戶（只給部署需要的最小權限）"
gcloud iam service-accounts describe "$SA" > /dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" --display-name "GitHub Actions 部署 course-qa-rag"
for ROLE in roles/run.developer roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$SA" --role "$ROLE" \
    --condition None --quiet > /dev/null
done
# 部署時要以 Cloud Run 執行身分（預設運算服務帳戶）建立新版本，所以需要「使用」它的權限
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" --member "serviceAccount:$SA" \
  --role roles/iam.serviceAccountUser --quiet > /dev/null

echo "3/5 確認映像檔存放區存在（第一次用 --source 部署時已自動建立）"
gcloud artifacts repositories describe cloud-run-source-deploy --location "$REGION" > /dev/null 2>&1 || \
  gcloud artifacts repositories create cloud-run-source-deploy --repository-format docker --location "$REGION" --quiet

echo "4/5 建立身分聯盟：只信任 GitHub 上的 $REPO"
gcloud iam workload-identity-pools describe "$POOL" --location global > /dev/null 2>&1 || \
  gcloud iam workload-identity-pools create "$POOL" --location global --display-name "GitHub Actions"
gcloud iam workload-identity-pools providers describe "$PROVIDER" --location global \
  --workload-identity-pool "$POOL" > /dev/null 2>&1 || \
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --location global \
    --workload-identity-pool "$POOL" --display-name "course-qa-rag" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository" \
    --attribute-condition "assertion.repository == '$REPO'"

echo "5/5 允許這個 repo 以部署帳戶的身分執行"
gcloud iam service-accounts add-iam-policy-binding "$SA" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository/$REPO" \
  --quiet > /dev/null

cat <<EOF

完成！到 GitHub 的 $REPO →
Settings → Secrets and variables → Actions → 「Variables」分頁 → New repository variable，新增這 3 個：

  名稱               值
  GCP_PROJECT_ID     $PROJECT_ID
  GCP_DEPLOY_SA      $SA
  GCP_WIF_PROVIDER   projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER

（這些是識別名稱，不是密碼，放在 Variables 即可。）
新增後，到 Actions 頁面重新執行最近一次的 tests，或推送任何修改，就會看到 deploy 自動執行。
EOF
