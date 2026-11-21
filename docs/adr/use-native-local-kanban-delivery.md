# 決策：LOCAL 看板通知沿用原生 admission FIFO

- 狀態：工程候選；尚未部署。
- 範圍：既有逐卡 `platform=local` 訂閱。來源是已核准的看板全階段通知修復需求。
- 基準：fork `1614ef0849628c4d16714ad5ab2c07537b7f19f8`；無既有 ADR 目錄，保留原生 SessionAuthority、profile scope 與 frozen policy 契約。

## 問題及選擇

`LocalSessionAdapter.send()` 只 ACK；現有 notifier 會推進 cursor 卻沒有 timeline 或 admission。改由專用 sibling 將訂閱的明確 LOCAL 來源送入既有 `admit_automation`，先 durable admission 再更新 board cursor。重送沿用同一需求版本的 request ID，因此 commit 後 cursor 寫入失敗也可恢復。既有原子 FIFO 處理忙碌、離線及 unknown fencing。

相較只增加 terminal kind，本方案也處理開始、明確進展、目前需求與執行時刷新；相較新增 outbox／排程，本方案重用 board events、subscription cursor 與原生 admissions，沒有第二 writer。

## 契約

1. 保留 subscription 的原 profile／chat／thread；LOCAL 來源需有原生 receipt，未知或活躍 legacy owner 保留待處理，不猜 worker Session。
2. `claimed` 為開始；`progress:`／`進度：` 留言為重要進展；終態及 review／needs_input 沿用現有事件。`commented` payload 增加原留言的 `comment_id`，讓選取與持久版本都由真實事件定位；普通留言與 edited 等 metadata 事件不覆蓋業務階段。連續相同正規化需求共用首個事件版本，A→B→A 的最後 A 則有新版本；commit 後 cursor 失敗沿用原收據及首次 payload。舊 payload 沒有 comment_id 時，只核對同作者／同時間且唯一的原留言，無法唯一核對時沿用已確認的狀態事件，不猜需求內容。
3. 送出前及執行前唯讀讀取目前卡片與訂閱；舊狀態改為目前快照。明確取消訂閱則略過；封存保留最後有效事件及原來源，直到對應原生 admission 為 `terminal/completed` 才移除訂閱，入列與待處理收據均不等於交付完成。
4. 保留 FIFO 與 unknown／審批，不加入社群 PR 的 human-first 排序，也不自動清除租約。

以上邊界依本次任務原文「原生單一 writer/FIFO」「舊狀態刷新、需求版本去重、多來源隔離、來源未知拒絕猜測」「不繞過 session 協調／審批、不清租約」；適用於本候選的 LOCAL 通知，其他平台沿用原行為。來源為 legacy 時，仍可由原生正常 resume／同 profile control ticket 在原 owner 釋放後收編，候選本身不代辦遷移。

## 相容性與回退

LOCAL `notify` 保留被動語意：在 FIFO 頭由 authority 寫入既有 completion note，沒有模型推論。`wake`／`notify+wake` 才使用原生報告回合與 internal notification card。LOCAL 訂閱保留到 archive 的最後有效通知有完成收據；cursor 僅證明 durable 排隊，30 天 GC 無法據此判斷模型是否已執行。封存等待期間保留未讀 archive 事件以重試，清理失敗也可重試；failed／cancelled／unknown 收據保留來源，循原生故障處理，不自動重新執行或解除 fencing。其他平台及 loop ticks 保持不變。此為向下相容錯誤修正，建議下一正式版本 PATCH；fork 沒有已發布 Release，查詢時上游最新 Release 為 `v0.23.3`，兩者不混稱；本分支不發版。

部署方案為：由協調者取得維運授權，核對現役來源版本與差異，備份程式及狀態，正常 drain／restart；依原 native 同 profile control ticket／resume 完成必要 legacy 收編，再逐卡核對 LOCAL 原來源訂閱。活躍舊 writer 仍回 `runtime_coordination_required`，等待原 owner 正常釋放；不代辦清租約。舊 helper 的重複投遞切換另由維運協調，本候選沒有修改服務、排程或憑證。

建議回退順序：先 drain 完候選的 queued／started 通知，再回到部署前來源；保留 receipt、admission、cursor 與原生 unknown 決策。舊版不理解 `kanban_notice` 的被動模式與刷新語意，因此有未解決通知時維持候選並循原生控制流程處理，不直接把舊版當相容的消費者。受控真實 Desktop 端到端另行驗證，本次沒有操控私人對話或 Computer Use。

## 驗證

可重現 ACK 遺失 → 原生 admission；commit 後重送；開始及進展；新舊需求；離線重啟；多來源與未知來源；queued 狀態刷新；聚焦與原生回歸。隔離測試不等於真實原對話驗收。

```sh
HERMES_PYTHON=/path/to/pm-test-env/bin/python scripts/run_tests.sh \
  tests/gateway/test_kanban_local_lifecycle.py tests/gateway/test_kanban_local_scope.py \
  tests/gateway/test_kanban_local_daemon.py tests/hermes_cli/test_kanban_local_pending_gc.py -j 3
python scripts/check
```

`test_kanban_local_daemon.py` 使用普通 gateway 程序、真實 WebSocket／SQLite 與 loopback 模型；驗證忙碌 FIFO、人類訊息先於後來通知、同卡需求替換、無 viewer 的離線事件重啟補送、被動 note 零推論。封存返修另驗證兩個停機時序，各涵蓋 `notify`／`wake`／`notify+wake`：事件尚未入列即離線封存，以及已 durable 入列後停機封存；後者只在隔離 owner 的 commit／schedule 間設測試停點，恢復端使用普通 daemon。讀回最後有效 completed 摘要、原來源 transcript、每卡一筆 admission 及完成收據後的訂閱清理；明確 unsubscribe 仍不觸發 note／模型。可用測試 runner 既有的 `HERMES_E2E_ARTIFACTS` 保存去私密 receipt。模型端點是隔離測試邊界，這不是對正式模型或 Desktop 畫面的驗收。

## 社群 PR 評估

以下先讀 GitHub 狀態、本文與相關來源 diff，再與本 fork 同一基準原碼核對；沒有 cherry-pick 或合併未審補丁。

| PR | 查詢時狀態 | 本版判定 |
|---|---|---|
| [#106742](https://github.com/NousResearch/hermes-agent/pull/106742) | MERGED；`eb75627faf532fa6ffbdae59a0d14b0b385fc780` | 已是 fork main 祖先；沿用 SessionAuthority、automation、generation fencing、legacy liveness guard。 |
| [#112262](https://github.com/NousResearch/hermes-agent/pull/112262) | OPEN；`ea6528fb338cb5343a2110f851f4276e46f4e3a5` | 修的是 legacy tui poller 冷 resume／agent 尚未建立的 buffer；本版 poller 已拆至 session_registry／session_notifications。它不解 LOCAL send ACK 或 durable FIFO，未套用。 |
| [#156499](https://github.com/NousResearch/hermes-agent/pull/156499) | OPEN；`40e1baf11fd02facb37a6e2cb8591ffc3a67b356` | 結構化引用、執行前刷新與合併排隊值得採用；其 persistence.authority／AuthorityRefusal 與本版 RuntimeStoreError／hermes_state_runtime 不相容。本候選採本版既有 `replaces` 原子交易，只合併同卡，保留普通 FIFO。 |
| [#158377](https://github.com/NousResearch/hermes-agent/pull/158377) | MERGED；head `366f6c9c1e87d2ffa8bf958537edcc21b8ed16f3` | fork 尚無這個 display card 修正；本候選直接在 LOCAL admission 寫入既有 internal_notification card，沒有修改其他平台 wake。 |
| [#72177](https://github.com/NousResearch/hermes-agent/pull/72177) | MERGED；`588b7059a8b57b0e3dea98b480048eb7199ce0b6` | 已是 fork main 祖先；legacy tui 訂閱 consumer 存在，不能據此推定 LOCAL notify 已有效或跨 writer 安全。 |

## 適用範圍及驗收界線

- 本候選處理 canonical LOCAL 訂閱；原生建卡／子卡來源繼承維持原機制，沒有從正文、worker Session 或其他對話猜來源。舊 `tui` 訂閱與非 LOCAL adapter 沿原路徑，不宣稱已遷移。
- 忙碌時同卡尚未開始的通知以新版本取代；原始事件仍留在 board。已開始的回合無法撤回，與更新競態時可能出現一次無害重複；unknown 頭仍會停止 FIFO，保留人工決策。
- `active_legacy_owner` 是活躍舊 writer 的協調狀態，不是永久禁止原對話接入。具體原對話狀態及 helper 差異留在本機私密交付，不提交 Session ID、資料庫或憑證。
