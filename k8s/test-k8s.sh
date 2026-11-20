#!/usr/bin/env bash
# 在 kind 叢集上實際部署並驗證 Kubernetes 的核心行為。
# GitHub Actions 每次推送都會執行；本機要先安裝 Docker、kind、kubectl。
#
#   1. 部署：2 份同時執行，健康檢查通過才接流量
#   2. 功能：透過 Service 呼叫網頁、/health、/search、/ask
#   3. 負載分配：連續送請求，確認 2 份都有收到
#   4. 自我修復：刪掉其中一份，確認 Kubernetes 自動補回 2 份
#   5. 零停機更新：持續送請求的同時更新版本，確認沒有任何請求失敗
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER=course-qa-rag
URL=http://localhost:8080
step() { echo; echo "========== $* =========="; }
# 檢查：條件成立印 ✓，不成立印 ✗ 並結束（讓 GitHub Actions 顯示失敗）
check() { if eval "$1"; then echo "✓ $2"; else echo "✗ $2"; exit 1; fi; }
ready() { kubectl get deployment course-qa-rag -o jsonpath='{.status.readyReplicas}'; }

step "0. 建立 kind 叢集、建置映像檔並載入叢集"
kind get clusters | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" --config k8s/kind-config.yaml --wait 120s
docker build -q -t course-qa-rag:ci .
kind load docker-image course-qa-rag:ci --name "$CLUSTER"

step "1. 部署（ConfigMap、Deployment、Service）"
kubectl apply -f k8s/manifests/
kubectl rollout status deployment/course-qa-rag --timeout=180s
kubectl get pods -l app=course-qa-rag -o wide
check '[ "$(ready)" = "2" ]' "2 份都通過健康檢查，開始接收流量"

step "2. 透過 Service 呼叫 API"
for i in $(seq 1 30); do curl -sf "$URL/health" > /dev/null && break; sleep 1; done
curl -sf "$URL/health"; echo
check 'curl -sf "$URL/" | grep -q 課程問答' "問答網頁"
check 'curl -sf "$URL/search?q=%E9%80%80%E8%B2%BB" | grep -q refund' "/search 找到退費段落"
check 'curl -sf -X POST "$URL/ask" -H "Content-Type: application/json" -d "{\"question\": \"多益衝刺班一期多少錢？\"}" | grep -q 18,000' "/ask 回答正確金額"

step "3. 負載分配：Service 把請求分給 2 份"
# Pod 就緒後，Service 的後端名單（EndpointSlice）與網路規則要再過幾秒才更新；先等 2 份都列入，避免測試時好時壞
for i in $(seq 1 30); do
  n=$(kubectl get endpointslices -l kubernetes.io/service-name=course-qa-rag \
      -o jsonpath='{range .items[*].endpoints[?(@.conditions.ready==true)]}{.addresses[0]}{"\n"}{end}' | grep -c . || true)
  [ "$n" -ge 2 ] && break
  sleep 1
done
echo "Service 後端已就緒：$n 份"
hosts=$(for i in $(seq 1 60); do curl -sf "$URL/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["host"])'; sleep 0.05; done | sort | uniq -c)
echo "$hosts"
check '[ "$(echo "$hosts" | wc -l)" -ge 2 ]' "60 個請求分配到 2 份"

step "4. 自我修復：刪掉一份，Kubernetes 自動補回"
victim=$(kubectl get pods -l app=course-qa-rag -o jsonpath='{.items[0].metadata.name}')
echo "刪除：$victim"
kubectl delete pod "$victim" --wait=false
for i in $(seq 1 60); do
  names=$(kubectl get pods -l app=course-qa-rag --field-selector=status.phase=Running -o jsonpath='{.items[*].metadata.name}')
  if [ "$(ready)" = "2" ] && ! echo "$names" | grep -qw "$victim"; then break; fi
  sleep 2
done
kubectl get pods -l app=course-qa-rag
check '[ "$(ready)" = "2" ] && ! echo "$names" | grep -qw "$victim"' "已自動建立新的一份，恢復成 2 份"

step "5. 零停機更新：持續送請求，同時把版本從 v1 換成 v2"
rm -f /tmp/k8s_codes.txt
( for i in $(seq 1 400); do
    curl -s -o /dev/null -w '%{http_code}\n' --max-time 3 "$URL/health" >> /tmp/k8s_codes.txt || echo "000" >> /tmp/k8s_codes.txt
    sleep 0.1
  done ) &
traffic=$!
sleep 2
kubectl set env deployment/course-qa-rag APP_VERSION=v2
kubectl rollout status deployment/course-qa-rag --timeout=180s
sleep 3
kill "$traffic" 2> /dev/null || true
wait "$traffic" 2> /dev/null || true
total=$(wc -l < /tmp/k8s_codes.txt)
failed=$(grep -vc '^200$' /tmp/k8s_codes.txt || true)
echo "更新期間送出 $total 個請求，失敗 $failed 個"
curl -sf "$URL/health"; echo
check 'curl -sf "$URL/health" | grep -q "\"version\": \"v2\""' "已更新到 v2"
check '[ "$failed" = "0" ] && [ "$total" -gt 50 ]' "更新過程沒有任何請求失敗"

kubectl rollout history deployment/course-qa-rag
echo
echo "全部通過"
