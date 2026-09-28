# IPTV Auto Updater

IPTV 直播源自動更新工具:**採集 → 匹配 → 效驗 → 測速 → 擇優 → 生成 IPv4 可播放清單**,並自動補齊頻道名稱、國家地區、台標與節目單 (EPG),可直接以 GitHub Actions 定時運行。

## 功能

| 模組 | 說明 |
|---|---|
| 採集 `fetcher.py` | 多來源並行下載,支援遠端 / 本地、m3u / txt(DIYP、TVBox)格式自動判斷,支援 `#EXTVLCOPT` 與 Kodi `url\|User-Agent=` 標頭,網址去重 |
| 名稱清洗 `normalizer.py` | 全形→半形、繁→簡(OpenCC)、去除 `HD / 1080p / 高清 / [Geo-blocked]` 等標籤,CCTV 系列特別處理(`CCTV-1 綜合 HD` → `CCTV1`) |
| 匹配 `matcher.py` | 別名表 → `tvg-id` → iptv-org 資料庫名稱與 alt_names → 模糊比對(rapidfuzz,並檢查數字一致以避免 CCTV1↔CCTV11 誤配);自動補齊國家、分類、台標 |
| 效驗測速 `checker.py` | 強制 IPv4 (`AF_INET`) 連線、過濾 IPv6 與內網位址;HLS 自動選最高碼率子清單並下載分片測速、記錄解析度;直連 ts/flv 讀取 N 秒測速;依速度 / 延遲 / 解析度判定 |
| 節目單 `epg.py` | 多個 XMLTV 來源串流解析(可處理數百 MB 的 .gz),依頻道 ID / 名稱匹配,裁剪時間窗後合併為 `epg.xml.gz` |
| 輸出 `output.py` | `live.m3u`(含 `x-tvg-url`)、`live.txt`、`countries/xx.m3u`、`report.json`、`README.md` 統計;若有 GFW 封鎖資料另外輸出 `live_cn.m3u`/`live_cn.txt` |
| GFW 封鎖檢測 `gfw_check.py` | 每週(`discover-sources.yml`)查詢 GreatFire.org 公開 API,把確定被中國大陸網路封鎖的網域從 `live_cn.m3u` 排除;沒資料的網域一律當作未封鎖(不確定就保留) |

## 專案結構

```
├── .github/workflows/update.yml   # GitHub Actions:每 6 小時更新並發佈到 release 分支
├── config/
│   ├── config.yaml                # 主設定檔
│   ├── alias.yaml                 # 頻道別名表
│   └── custom.txt                 # 自訂來源
├── iptv/
│   ├── main.py                    # 流程編排
│   ├── fetcher.py  parser.py      # 採集 / 解析
│   ├── normalizer.py matcher.py   # 清洗 / 匹配
│   ├── checker.py                 # 效驗 / 測速
│   ├── epg.py                     # 節目單
│   └── output.py                  # 輸出
└── requirements.txt
```

## 快速開始

### GitHub Actions 部署

1. 把本專案推送到你自己的 GitHub 儲存庫(預設分支 `main`)。
2. 到 **Settings → Actions → General → Workflow permissions**,選 **Read and write permissions**。
3. 到 **Actions** 頁面啟用 workflow,點 **Update IPTV → Run workflow** 手動跑第一次。
4. 完成後,結果會強制推送到 `release` 分支(不累積歷史,儲存庫不會越來越大),訂閱地址:

```
https://raw.githubusercontent.com/<你的帳號>/<儲存庫>/release/live.m3u
https://raw.githubusercontent.com/<你的帳號>/<儲存庫>/release/live.txt
https://raw.githubusercontent.com/<你的帳號>/<儲存庫>/release/live_cn.m3u   # 大陸用戶:已排除確定被 GFW 封鎖的網域
https://raw.githubusercontent.com/<你的帳號>/<儲存庫>/release/epg.xml.gz
https://raw.githubusercontent.com/<你的帳號>/<儲存庫>/release/countries/tw.m3u
```

raw.githubusercontent.com 在部分地區不穩時,可改用 jsDelivr 鏡像:`https://cdn.jsdelivr.net/gh/<帳號>/<儲存庫>@release/live.m3u`

### 本地運行

```bash
pip install -r requirements.txt
python -m iptv.main -c config/config.yaml          # 完整流程
python -m iptv.main --skip-check                    # 只採集 + 匹配,不測速(除錯)
python -m iptv.main --limit 100 -v                  # 只測前 100 條,顯示詳細日誌
```

## 設定重點(`config/config.yaml`)

- **sources**:可以是字串或 `{url, country, group}`。iptv-org 的 `countries/xx.m3u` 會自動推測國家,讓未收錄於資料庫的頻道也能正確分組。
- **filter.countries**:例如 `[TW, HK]` 只保留台港頻道。
- **check.min_speed / max_latency / min_height**:可用門檻;`min_height: 720` 可只留 HD 以上的 HLS 線路。
- **output.max_per_channel**:每頻道保留最快的 N 條線路(多線路在播放器裡可切換)。
- **output.group_rules**:依名稱正則、國家、分類分組,由上而下先符合者勝;都不符時依 `group_fallback`。
- **output.min_channels**:可用頻道過少時直接失敗,避免來源異常時把舊清單覆蓋成空的。
- **epg.sources**:XMLTV 來源,依序優先;同一頻道 ID 以先出現的來源為準。

新增別名請編輯 `config/alias.yaml`,格式為 `標準名稱: [別名...]`。由於已自動做繁簡轉換與標籤清洗,只需列出寫法差異大的別名。

## 注意事項

- **測速結果取決於運行位置**。GitHub 托管的 runner 位於海外機房,且本身沒有 IPv6(剛好符合 IPv4 需求);但限制地區播放的來源在 runner 上會失敗而被過濾,海外 CDN 的速度也會偏樂觀。若要反映你所在地區的真實情況,可把 `runs-on` 改為部署在自家網路的 [self-hosted runner](https://docs.github.com/actions/hosting-your-own-runners),或在本地 / NAS 上用 cron 執行。
- 公開儲存庫的排程 workflow 在 60 天沒有提交時會被 GitHub 自動停用,需要手動重新啟用。
- 全球來源 (`index.m3u`) 有上萬條線路,請調高 `concurrency` 或降低 `speed_duration`,並注意 workflow 的 `timeout-minutes`。

## 參考的專案

設計時參考了以下 GitHub 專案的思路與資料格式:

- [iptv-org/iptv](https://github.com/iptv-org/iptv):公開直播源整理,採集來源預設使用其分國家清單
- [iptv-org/database](https://github.com/iptv-org/database) / [iptv-org/api](https://github.com/iptv-org/api):頻道、台標、國家、分類資料庫(匹配核心)
- [iptv-org/epg](https://github.com/iptv-org/epg):EPG 頻道 ID 規範(`CCTV1.cn` 形式),可自建 guide 後加入 `epg.sources`
- [Guovin/iptv-api](https://github.com/Guovin/iptv-api):測速擇優、分組模板、Actions 定時更新的整體流程
- [fanmingming/live](https://github.com/fanmingming/live):m3u / txt 輸出格式與台標命名方式

## 免責聲明

本工具僅做公開網路資源的整理與可用性檢測,不儲存、不提供任何影音內容。請只加入你有權使用的來源,並遵守各頻道的版權與所在地法律。
