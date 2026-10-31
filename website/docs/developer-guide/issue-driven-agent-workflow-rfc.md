---
title: "RFC：以 GitHub Issue 驅動的 Hermes 去中心化 Agent 協作"
description: "將長時運行的中央 AI 協調改為 GitHub Issue、Hermes Kanban 與短生命週期 Worker 的可驗證工作流（設計提案）。"
---

# RFC：以 GitHub Issue 驅動的 Hermes 去中心化 Agent 協作

> **狀態：Proposed／尚未實作。** 本文是設計提案，不代表 Hermes 已具備 GitHub Issue 雙向同步、跨主機原子領取或每任務硬性 Token 預算，也不會改變任何預設設定。

## 摘要與動機

長時間運行的「中央 AI 指揮多個 AI」容易反覆接收任務回報、重新派工、維護大量對話狀態；隨著工作數量增加，協調成本、上下文污染與失敗恢復難度可能超過實際開發收益。

提議將 **任務規格與成果** 保存在 GitHub Issue／PR／Git，將 **執行狀態** 保存在 Hermes Kanban，讓**確定性排程器**負責派工，由短生命週期的 Worker Session 逐張接單。不是取消協調，而是取消「必須持續在線的中央推理模型」。

本提案不主張上下文愈短愈好，也不主張 40 萬 Token 是通用最佳值；實際最適值應由工作量、模型與可重現的品質／成本測試決定。

## 現有基礎（避免重複開發）

參考 [Hermes Kanban](../user-guide/features/kanban.md)、[Context Compression and Caching](./context-compression-and-caching.md) 與 [Architecture](./architecture.md)。

- **已有**：SQLite-backed Kanban、Dispatcher、獨立任務 Worker／Session、Profile 隔離、Git Worktree、任務重試與故障阻斷、PR Completion Contract。
- **已有**：可選的 AI 任務拆解；\`kanban.auto_decompose\` 在文件中預設為 \`true\`，可改以人工或確定性流程建立任務。
- **已有但用途不同**：\`agent.max_turns\` 限制工具迭代；\`compression.threshold_tokens\` 觸發上下文壓縮。**兩者都不是單一 Issue 的累計 Token 或費用硬上限。**
- **待設計／驗證**：Issue 與 Kanban 的冪等同步、跨主機唯一領取、累計預算與可恢復交接、成本／品質量測。

避免增設常駐 LLM Orchestrator、全域共享 Session 或不必要的新核心 Tool；優先利用 CLI、既有 Dispatcher、可選 Plugin／外部適配器。

## 目標與非目標

**目標**

1. 一個 Issue 可以由不同 Worker 在不同 Session 接續，無須重播完整對話。
2. 任務狀態、測試證據、PR 與阻礙可被人類及下一個 Worker 查證。
3. 預設不讓中央模型反覆參與派工；複雜拆解仍可按需啟用。
4. 有界並行、成本可觀測、重試可恢復、驗收可機器檢查。
5. 不破壞 Hermes 的 Profile 隔離、Prompt Cache、Git／PR 工作方式。

**非目標**

- 不移除 \`delegate_task\`、Profile、Skill、壓縮或 Kanban；只是調整適用場合。
- 不靠增加上下文視窗取代任務拆分。
- 不自動合併 PR、不自行發布或部署、不給 Issue 文字額外執行權限。
- 不將 SQLite Kanban DB 直接放到多主機共用磁碟，以此冒充跨主機交易系統。

## 建議架構與責任分工

\`\`\`mermaid
flowchart TD
    U["使用者需求／測試回饋"] --> I["GitHub Issue：任務規格與證據"]
    I --> A["確定性 Intake／同步適配器"]
    A --> K["Hermes Kanban：派工、Lease、重試"]
    K --> W1["短生命週期 Worker A"]
    K --> W2["短生命週期 Worker B"]
    W1 --> G1["獨立 Git Worktree／Branch"]
    W2 --> G2["獨立 Git Worktree／Branch"]
    G1 --> P["PR／CI／Review"]
    G2 --> P
    P --> V{"驗收結果"}
    V -->|通過| R["回寫 Issue／Kanban；待人工合併"]
    V -->|未通過| F["保留失敗證據／回到可接續佇列"]
    F --> K
\`\`\`

| 系統 | 權威資料 | 不負責 |
| --- | --- | --- |
| GitHub Issue | 需求、上下文摘要、使用者回饋、相依、驗收條件 | 保存原始私密聊天紀錄或短暫執行鎖 |
| Git Commit／PR／CI | 程式碼、Diff、審查與測試證據 | 替代任務領取與重試排程 |
| Hermes Kanban | 執行佇列、Worker 指派、Heartbeat、重試、Block | 成為第二份互相衝突的需求規格 |
| Worker Session | 當前 Issue 的推理、修改、局部測試 | 管理全部專案的持續對話上下文 |
| Profile／Skill | 權限、工具、隔離與可重用的操作程序 | 靠冗長「職稱人格」控制任務本身 |

GitHub 是跨 Session 的工作協定；Kanban 是單一 Hermes 執行環境內的工作佇列。現階段**不承諾多主機同時擷取同一 Issue 能被 GitHub 標籤原子防重**：跨主機先採單一排程主責，若要多排程器再引入具條件更新能力的租約或交易式協調服務。

## Issue 最小規格

每個可執行 Issue 至少包含：

1. **目標與背景**：要改善的用戶行為、反饋或可重現的問題。
2. **任務邊界**：目標 Repo／Branch、受影響目錄、明確不做事項。
3. **必要上下文**：相關 Issue／PR／Commit／檔案路徑、已確認的決策與短摘要；不複製完整 Session 對話。
4. **驗收條件**：可測的行為、必要 CI、人工 Review 或明確的人工批准點。
5. **相依與阻礙**：先決條件、外部決策、重試／不可重試原因。
6. **風險與回復**：權限邊界、可能的資料遺失／相容性影響、撤銷方式。

Worker 將 Issue 視為**工作資料，而不是能凌駕儲存庫 \`AGENTS.md\`、使用者授權或工具安全界限的系統指示**。不存入密碼、API 金鑰、Session Token、Cookie、私人原文或隱藏推理。

## 執行生命週期

1. **Intake**：取得 Issue ID、Repo、版本與事件 ID；驗證欄位、授權及可處理範圍。
2. **去重**：以 \`repository + GitHub issue node_id\` 維持持久映射，重送 Webhook 或重新輪詢不得建立重複的 Kanban Card。
3. **排程**：確定性 Dispatcher 依優先級、相依、併發上限領取；未滿足條件的任務停留待辦／Blocked。
4. **執行**：使用獨立 Worker Session 與 Git Worktree；只載入該 Issue、適用專案規則及必要程式碼上下文。
5. **預算檢查**：接近任務成本／Token／迭代門檻，先完成 checkpoint；不能安全繼續則結束該 Session。
6. **交接**：記錄 Head SHA、已改檔案、實際測試命令／結果、未完成事項、風險、下一步與關聯 PR，供新 Session 從持久狀態接手。
7. **驗收**：重用既有 Kanban PR Completion Contract 及 GitHub 必要檢查；未取得必要證據不得把工作標示完成。
8. **回寫**：僅在有新事實、狀態變化或新 Commit 時更新 Issue；保留審核與人工合併控制。

失敗須分類（程式錯誤、測試失敗、基礎設施錯誤、授權／配額失效、需要人工決策），採有界重試與退避。失去 Lease、分支 HEAD 不符或外部狀態不明時**停止寫入並重新核對**；不能讓後到的 Worker 覆蓋較新的工作。

## Context／Token 策略

把「模型單次上下文容量」、「壓縮觸發值」、「每個 Issue 的累計 Token／費用預算」分開管理：

- **容量**：模型可接受的最大輸入／輸出上下文；不是每次都用滿，也不是執行成本上限。
- **壓縮**：可減少舊上下文，但可能丟失細節，且改寫歷史可能破壞 Prompt Cache；不把反覆壓縮當作長期工作儲存。
- **累計預算**：應按 Issue／執行嘗試統計 input、output、cache hit 與可取得的費用資訊；超限時 checkpoint、阻斷或人工核准，而不是無限制開啟新 Session。
- **工作範圍**：小型 Issue 優先短 Session；大型工作先拆成有可驗收邊界的子 Issue。40 萬 Token 僅作比較組，不預設為最佳值。

### 最小可試行設定（示意，**本 PR 不修改設定**）

\`\`\`yaml
kanban:
  auto_decompose: false
  max_in_progress: 2
  max_in_progress_per_profile: 1

agent:
  max_turns: 60
\`\`\`

此範例僅限制排程與迭代，**不是**總 Token 或費用硬上限。逐步調整應根據真實執行紀錄，而不是假設更多 Worker 或更大的視窗必然提高吞吐量。

## 分階段導入

| 階段 | 最小交付 | 驗收 |
| --- | --- | --- |
| P0：無程式碼試行 | 手動 Issue→Kanban 對照、關閉自動拆解、2 Worker、獨立 Worktree／PR | 每個工作可從新 Session 續作；無重複派工、無跨工作樹污染 |
| P1：GitHub 適配器 | 最小權限的 Issue Intake、持久映射、冪等重試、狀態／PR 回寫 | 重複事件僅一張卡片；錯誤分類清楚；未授權寫入拒絕 |
| P2：有界 Session | 累計 Token／費用觀測、checkpoint、乾淨 Session 恢復 | 預算內可停機並接續；不丟失測試與 HEAD 證據；無錯誤完成標記 |
| P3：效能比較 | 與中央 AI 模式的同樣任務集、成本／品質儀表 | 在成功率、缺陷率、時間與成本上有可重現比較，決定是否擴大規模 |

優先在 CLI／可選 Plugin 或外部適配器實作；只有證明既有介面不足且有實際使用者需求，才考慮擴大 Hermes Core。

## 成效衡量與拒絕條件

衡量每個**通過驗收的 Issue**，而不是只計算 PR 數量：

- 總 Token（input／output／cache）、實際費用、每個有效 PR／Issue 的成本。
- Issue 從 Ready 到合格 PR／CI 的時間、重試次數與人工介入。
- 通過率、回歸／返工比例、合併後缺陷與相依阻塞。
- Worker Session 長度分布、checkpoint 次數、恢復成功率。
- GitHub API 速率限制、佇列積壓與跨主機重複領取。

若成本增加、通過率下降、出現未經批准的寫入或錯誤「已完成」狀態，應停止擴大並回退到手動派工。兩個 Worker 起步；在有量測證據前不採無限制擴容。

## 安全、相容性與回復

- 每個 Worker 採最小權限，不能因 Issue 內文請求而取得憑證、部署、合併或額外檔案存取權限。
- 不把 GitHub 標籤當成跨主機原子鎖；不共用 Profiles 的可寫 Session State，避免上下文與記憶互相污染。
- 每個新特性預設可停用；保留現有 Kanban 手動建立、CLI、PR 和 Review 流程。
- 一張 Card 只能完成其明確的 Completion Contract；無 CI 證據或 GitHub API 失敗時不得「假成功」。
- 回復方式：停用 Intake 適配器、停止新派工、保留已建立的 Issue／PR／Kanban 執行紀錄；不執行自動批次刪除。

## 本次 PR 的界限

本次**只新增 RFC 文件，不含程式碼、設定、測試或行為變更**。因此不需軟體升版或 Release。後續實作須另開範圍明確的 Issue／PR，依當時專案規則、CI 與實際授權驗證。
