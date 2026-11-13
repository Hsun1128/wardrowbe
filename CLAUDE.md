# CLAUDE.md

本檔案記錄這個專案（fork 自 [Anyesh/wardrowbe](https://github.com/Anyesh/wardrowbe)）的產品目標與協作慣例。前身是在 [leoz9/AIWardrobe](https://github.com/leoz9/AIWardrobe) 上做的原型，換底的完整理由見 [docs/adr/0001-switch-base-to-wardrowbe.md](docs/adr/0001-switch-base-to-wardrowbe.md)。

## 產品目標（Product Vision）

這是一個 **AI 電子衣櫃產品**。核心需求：上傳衣服照片 → 自動整理成衣櫃 → 自由搭配選擇 → 生成搭配預覽圖；以及提供真人試穿生成圖的能力。

**多使用者**：目標支援 4 位家庭成員，各自擁有獨立衣櫃、可自由切換。**這點現在是原生支援**——`backend/app/api/families.py` 有完整的建立/邀請碼/加入/離開/成員角色管理端點，不再是 AIWardrobe 原型裡那種 `owner:<name>` 標記佔位的權宜做法。

**關鍵設計原則：供應商中立（Vendor-Neutral）**
- 衣物「分類」不強制綁定任何特定付費 Vision API。wardrowbe 原生就支援 `AI_INTERNAL_ENABLED=false`（連同 `AI_VISION_ENABLED`/`AI_TEXT_ENABLED`），讓分類、推薦文案、搭配建議都能交給外部 agent——`GET /api/v1/capabilities` 會回報目前這三項是否交給外部代勞（`external_tagging`/`external_suggestions`/`external_pairings`）。
- 在這個基礎上，我們把**操作端 AI（Claude、ChatGPT 等）自己看圖判斷 metadata，直接入庫**這個模式延伸出來：`POST /api/v1/items` 搭配 `skip_ai=true` 跳過內部 Vision 分類，呼叫端直接帶上 `type`/`style`/`season`/`material`/`color` 等欄位（詳見下方「移植了什麼」）。
- 真人試穿生圖保持開放，可接任何可用的影像生成模型，不綁死單一供應商。
- 每位使用者也可以在 Settings 的「AI Endpoints」裡指向自己的（免費/本地）AI 端點（例如自己的 Ollama），目前**還不支援在這裡貼付費 API Key**——這是刻意暫緩的項目，見下方「暫緩事項」。

**介面策略：雙層設計**
1. **REST 引擎層**：`POST /api/v1/items` 原生支援的 `skip_ai` + 本次新增的 `image_url`/`source_url`/擴充標籤欄位，讓呼叫端可以完全不碰 wardrowbe 的內部 Vision 分類就把一件衣物（含商城連結來源）寫進衣櫃。
2. **MCP 介面層**：`mcp-server/` 下的獨立 MCP server，共 20 個工具，讓任何支援 MCP 的 AI 助理都能當作這個衣櫃的第二使用介面。入庫類（`add_clothing_item`、`add_from_product`、`update_item`）是我們自己的；穿著/洗滌/穿搭/統計類沿用 [saya6k/mcp-wardrowbe](https://github.com/saya6k/mcp-wardrowbe)（MIT）的工具設計，針對這個 fork 的 API 重寫。完整清單見 [mcp-server/README.md](mcp-server/README.md)。

**商城整合願景與路線（Marketplace Integration）**
- **技術地基**：`add_from_product`（`image_url` 後端自行下載 + SSRF 防護 + `source_url` 記錄在 `tags.source_url`，且 `source_url` 屬於 `USER_TAG_KEYS`，AI 重新分析不會覆蓋它）。
- **商城變現路線（由近到遠，分四階段）**：
  | 階段 | 內容 |
  |---|---|
  | ① | **聯盟行銷連結導購抽成**——`source_url` 已經是現成基礎建設，之後可在衣物詳情頁加「前往購買」連結並帶上聯盟代碼。 |
  | ② | **商城商品 feed / 聯盟目錄 API**——規模變大、走向公開版時，改接品牌/商城官方的商品 feed 或聯盟目錄 API，解決規模與資料合規問題。 |
  | ③ | **虛擬試穿整合**——結合供應商中立的試穿生圖設計，下單前先看到自己穿上該商品的樣子。 |
  | ④ | **App 內直接下單**——需要跟商城談商務合作、串金流，是最下游、門檻最高的一步。 |
- **定位提醒**：`source_url` 與供應商中立的試穿設計是為了讓①②③能順勢接上去而先鋪的技術地基；**下單整合（④）是有實際流量之後才需要認真談的下游問題，現階段不必預先規劃技術細節**。

## 換底後的現況（這次對話完成的事）

**已移植到這個分支（`feat/mcp-provider-neutral`）的三項**：
1. **開放的標籤欄位**：`POST /api/v1/items` 新增 `pattern`/`material`/`style`/`season`/`formality`/`fit`/`source_url` 這幾個 Form 參數，讓呼叫端搭配既有的 `skip_ai=true` 把完整判斷結果一次帶入，不再只能帶 `type`/`name`/`brand`。
2. **`image_url` 輸入** + **SSRF 防護**：`backend/app/utils/image_url_fetch.py`（從 AIWardrobe 原型的 `manual_ingest.py` 搬過來並改用這個專案的 logging 慣例）——只允許 http/https、解析 DNS 後擋掉私網/回環/連結本地/保留/組播位址、不跟隨重新導向、錯誤訊息一律通用化。`create_item` 現在 `image`/`image_url` 二擇一。
3. **MCP server**（`mcp-server/`）：打 wardrowbe 的真實 API——用 `POST /api/v1/auth/sync`（dev-mode 憑證登入）換 JWT，之後每次呼叫帶 `Authorization: Bearer`。支援兩種傳輸：stdio（單一本機使用者）與 Streamable HTTP（多人共用一個 server，每人一把 token，對應到各自的衣櫃，設定在 git-ignored 的 `mcp-server/users.json`）。

**內網部署**：`docker-compose.lan.yml` 把整套服務加上 MCP server 開給家裡內網（Web UI 與 `/mcp` 對外，後端 API／Postgres／Redis 只綁 `127.0.0.1`）。啟動方式與安全邊界見 [mcp-server/README.md](mcp-server/README.md)。這套仍是後端 dev-mode 登入（任何 email 都能登入），**只適用於可信任的家用內網，不能直接對外**；要對外或上架 Claude／ChatGPT 目錄，下一步是在 MCP 層加上每位使用者的 OAuth。

**AIWardrobe 原型現況**：保留在 `/Users/hsun/文件/Code/AIWardrobe`（分支 `feat/manual-ingest-mcp`，已有兩個 commit），**轉為參考實作**——不再是開發主線，但其 `manual_ingest.py`/`CLAUDE.md` 仍是這次移植的設計依據與對照組，之後遇到類似決策可以回頭查。

**暫緩事項（第 4 項，這次沒做）**：每位使用者在 Settings 的「AI Endpoints」貼自己的付費 API Key（目前的 `AIEndpoint` schema 只有 `name/url/vision_model/text_model/enabled`，沒有 `api_key` 欄位）。要做的話需要新增欄位＋設計加密存放機制（不能明文存 Postgres），刻意留到之後有需求時再處理，避免這次順便做出一個沒想清楚安全模型的半成品。

## Agent 使用時機

以下 13 個 subagent 定義已安裝於 `~/.claude/agents/`（來源：[msitarzewski/agency-agents](https://github.com/msitarzewski/agency-agents)，MIT License），在這個新基底上一樣適用：

### 架構設計 / 統籌編排
- **Agents Orchestrator**：統籌整個開發管線時。
- **Project Shepherd**：跨功能協調、時程/風險管理時。
- **Software Architect**：通用系統設計、重大架構取捨時。
- **Backend Architect**：改動 FastAPI 後端、SQLAlchemy/Alembic schema、API 設計把關時。
- **Multi-Agent Systems Architect**：設計/審查 MCP server 架構時。
- **Workflow Architect**：把一個功能的所有路徑寫成可實作規格時。
- **Master Plan Architect**：需要深度架構藍圖 + 紅隊風險批判時。

### 品質把關
- **Code Reviewer**：審查正確性、可維護性、安全性、效能時。
- **Test Automation Engineer**：補端對端測試時（這個專案的 `backend/tests`/`frontend/tests` 已有基礎，可以接著補）。
- **API Tester**：驗證 API（例如 `/api/v1/items` 的 `image_url` 分支）時。
- **Accessibility Auditor**：UI 做好之後要做 WCAG 無障礙稽核時。

### 安全
- **Application Security Engineer**：威脅建模、安全程式碼審查——尤其這個專案已經要走公開路線，K8s NetworkPolicy/JWT 之外的面向也該持續審查。
- **Secrets & Credential Hygiene Engineer**：之後真要做「第 4 項」per-user API Key 加密存放時，第一個該找的就是它。
