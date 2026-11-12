# ADR 0001：把產品基底從 AIWardrobe 原型換成 wardrowbe

- **狀態**：已採納
- **日期**：2026-11-12

## 背景

我們原本在 [leoz9/AIWardrobe](https://github.com/leoz9/AIWardrobe)（FastAPI + React + SQLite 的簡單原型）上做開發，已經完成並 commit 過兩樣東西：
1. `POST /api/manual-ingest`——帶 metadata、跳過 Vision 分類的入庫端點，支援 `image_base64`/`image_url`/`source_url`，含 SSRF 防護。
2. 一個獨立的 MCP server（`add_clothing_item`/`add_from_product`/`list_wardrobe`/`search_wardrobe`），讓外部 AI 助理能操作衣櫃。

在評估「要不要繼續在這個原型上蓋多人衣櫃、帳號系統、公開基礎設施」時，調查了兩個現成的開源衣櫃專案（[Anyesh/wardrowbe](https://github.com/Anyesh/wardrowbe)、[zebangeth/ai-closet](https://github.com/zebangeth/ai-closet)）當候選基底，詳細比較記錄在對話歷史中，重點結論如下。

## 決策

**把產品基底換成 wardrowbe**（fork 到 [Hsun1128/wardrowbe](https://github.com/Hsun1128/wardrowbe)），AIWardrobe 原型保留但轉為參考實作，不再是開發主線。

## 理由

wardrowbe 是一個成熟、持續維護的真實產品（543+ commits、已上架 Google Play / App Store），實測確認它已經原生具備我們原本要從零開始蓋的東西：

1. **帳號/登入**：標準 JWT Bearer（`backend/app/utils/auth.py`），支援 OIDC/forward-auth，也有 dev-mode 憑證登入（`POST /api/v1/auth/sync`）方便本機開發——這是 AIWardrobe 完全沒有、但公開上線前必須有的東西。
2. **多人/家庭衣櫃**：`backend/app/api/families.py` 有完整 11 個端點（建立/邀請碼/加入/離開/成員角色管理），**實測跑過**：建立家庭、產生邀請碼、查成員列表全部成功。這是 AIWardrobe 原本只能用 `owner:<name>` 標記權宜解決的最大缺口。
3. **推薦/搭配**：Suggest Outfit、Pairings 功能完整，有 AI 推理理由 + Tip。
4. **連結/來源記錄**：`ItemTags.source_url` 欄位**已經存在**於 schema 裡（`backend/app/schemas/item.py`），而且被列進 `USER_TAG_KEYS`（AI 重新分析不會覆蓋使用者填的值）——跟我們在 AIWardrobe 上自己加的 `source_url` 欄位目的完全一致，只是它原生就有。
5. **供應商中立的雛形已經存在**：`AI_INTERNAL_ENABLED=false` 是官方文件化的功能，`GET /api/v1/capabilities` 會回報 `external_tagging`/`external_suggestions`/`external_pairings` 是否交給外部代勞——**範圍比我們原本只做到「入庫跳過分類」還大**，連推薦文案、搭配建議都設計成可以外包。
6. **`skip_ai` 已經原生存在**：`POST /api/v1/items` 本來就接受 `skip_ai: bool = Form(False)`，實測 `skip_ai=true` 直接成功入庫、狀態為 `ready`，不進 AI 佇列——這跟我們的 `manual-ingest` 概念幾乎一樣，不用重新發明。
7. **公開基礎設施**：K8s 清單含 `network-policy.yaml`（預設 deny-all ingress）、完整 nginx/ingress 設定、Secrets 範本——這些都是 AIWardrobe 完全沒有、但公開前必須有的東西，直接省掉從零建置的工作量。
8. **授權**：MIT，可以 fork/商用/公開，沒有限制。

## 代價與取捨

- **換底不是零成本**：前端是 Next.js（跟 AIWardrobe 的 Vite+React 不同生態），要大幅客製 UI 視覺風格是全新的前端工作，不是移植。
- **wardrowbe 缺的東西，我們要補上**（見下方「要移植什麼」）：`image_url` 輸入、SSRF 防護、MCP server、擴充的標籤 Form 欄位——這些原本在 AIWardrobe 上已經寫好測過，這次是**搬過來、改接新 API**，不是重新設計。
- **一個明確暫緩的缺口**：使用者想要的「每人在前端貼自己的付費 API Key」，wardrowbe 現有的 `AIEndpoint`（Settings 的「AI Endpoints」）schema 只有 `name/url/vision_model/text_model/enabled`，**沒有 `api_key` 欄位**——這次決定不做（見下方）。

## 這次移植了什麼（在 `feat/mcp-provider-neutral` 分支）

1. **開放標籤欄位**：`backend/app/api/items.py` 的 `create_item` 新增 `pattern`/`material`/`style`/`season`/`formality`/`fit`/`source_url` Form 參數，讓呼叫端搭配既有的 `skip_ai=true` 一次把判斷結果填好，不用事後再 PATCH。
2. **`image_url` 輸入 + SSRF 防護**：新增 `backend/app/utils/image_url_fetch.py`（整段從 AIWardrobe 的 `backend/api/manual_ingest.py` 搬過來，改用這個專案的 `logging` 慣例而非 `print()`），`create_item` 現在 `image`/`image_url` 二擇一；`image_url` 分支會把下載到的圖走原有的驗證/縮圖流程。
3. **MCP server**（`mcp-server/`）：沿用 AIWardrobe 版本的架構（FastMCP、四個工具），改成：
   - 先 `POST /api/v1/auth/sync` 換 JWT（dev-mode 憑證登入），快取在進程記憶體裡，之後每次呼叫帶 `Authorization: Bearer`。
   - `add_clothing_item`/`add_from_product` 改成打 multipart `/api/v1/items`（不是 JSON body），因為 wardrowbe 的端點是 `Form()`/`File()` 而不是 Pydantic JSON body。
   - `list_wardrobe`/`search_wardrobe` 直接用 wardrowbe 原生的分頁/`search`/`type` 查詢參數，不再像 AIWardrobe 版本那樣自己在 client 端做篩選。

## 暫緩事項（第 4 項，明確不做）

「每位使用者在前端貼自己的付費 API Key」——現有 `AIEndpoint` schema 沒有 `api_key` 欄位，真要做需要：新增欄位、設計不能明文存 Postgres 的加密/存放機制、前端輸入框的遮蔽處理。這次刻意不做，避免倉促做出一個沒想清楚安全模型的半成品；等真的有需求時再由 **Secrets & Credential Hygiene Engineer**（已安裝的 subagent）帶頭設計。

## 後續待辦（不在這次範圍內）

- MCP server 目前用的 dev-mode 憑證登入（`auth/sync` 沒有密碼）只適合本機開發；正式環境一旦 wardrowbe 開了 OIDC/forward-auth，MCP server 需要換一套真正的服務帳號/token 發放機制。
- 這次只驗證過最小化 dev stack（跳過 `worker`/`image-worker`/`caddy`），背景工作佇列與正式版 `docker-compose.prod.yml`/K8s 部署還沒實測。
- AIWardrobe 原型維持現狀（已有兩個 commit），不會再往下開發，但保留當作設計對照與歷史紀錄。
