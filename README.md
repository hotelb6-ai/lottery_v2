# 員工尾牙抽獎系統 v2

一套可以部署到雲端的員工抽獎網頁系統。**每位員工只能抽一次、每份獎品只會被派一次**，由資料庫交易保證。

員工用手機掃 QR Code → 用工號登入 → 按按鈕抽獎 → 看到結果。

主辦人用管理後台：新增員工、新增獎品、開放/暫停抽獎、匯出中獎名單 CSV。

---

## 目錄

- [快速預覽 URL 結構](#快速預覽-url-結構)
- [Part 1：本機測試（可選）](#part-1本機測試可選)
- [Part 2：部署到 Zeabur（正式）](#part-2部署到-zeabur正式)
- [Part 3：活動當天使用流程](#part-3活動當天使用流程)
- [附錄 A：預設帳號密碼](#附錄-a預設帳號密碼)
- [附錄 B：常見問題](#附錄-b常見問題)

---

## 快速預覽 URL 結構

假設部署後網址是 `https://your-app.zeabur.app`：

| 頁面 | 網址 | 給誰 |
|---|---|---|
| 員工登入 | `/login` | 員工 |
| 員工抽獎 | `/draw` | 員工（登入後）|
| 管理員登入 | `/admin/login` | 主辦人 |
| 後台總覽 | `/admin/` | 主辦人 |
| QR Code 頁 | `/admin/qrcode` | 主辦人（投影用）|

---

## Part 1：本機測試（可選）

如果你想先在自己電腦跑跑看再決定要不要部署，做這一段。**只想直接部署的話跳到 Part 2。**

### 1-1 安裝 Python

1. 到 https://www.python.org/downloads/ 下載 Python 3.11 或 3.12（Windows / macOS 都有）
2. **Windows 安裝時務必勾「Add Python to PATH」**
3. 安裝完打開 PowerShell（或終端機）輸入：
   ```
   python --version
   ```
   應該顯示 `Python 3.11.x` 或類似訊息

### 1-2 安裝套件

在此資料夾（`D:\抽獎\lottery_v2\`）打開終端機，輸入：

```
pip install -r requirements.txt
```

### 1-3 啟動

```
python app.py
```

看到訊息 `Running on http://0.0.0.0:8000/`，就開瀏覽器打開：
- 員工登入：http://127.0.0.1:8000/login
- 管理後台：http://127.0.0.1:8000/admin/login

預設管理員：帳號 `admin`、密碼 `admin1234`（見附錄 A）

### 1-4 手機連本機測試（同 WiFi 內）

1. 在筆電上開 PowerShell，輸入 `ipconfig`，找 IPv4 位址（像 `192.168.1.100`）
2. 手機連上同一個 WiFi
3. 手機瀏覽器輸入 `http://192.168.1.100:8000/login`

如果連不上，可能是 Windows 防火牆擋掉，允許「Python」通過即可。

---

## Part 2：部署到 Zeabur（正式）

Zeabur 是台灣的雲端服務，介面繁中、免費方案適合這種一次性活動。

### 2-1 註冊 GitHub 帳號

程式碼要先傳到 GitHub 才能給 Zeabur 部署。

1. 到 https://github.com/ 點右上 **Sign up**
2. 用 Email 註冊，跟著步驟到底

### 2-2 安裝 GitHub Desktop（免用指令）

1. 到 https://desktop.github.com/ 下載安裝
2. 打開 GitHub Desktop，用剛剛的帳號登入

### 2-3 把程式碼傳到 GitHub

1. GitHub Desktop 選單 **File → Add local repository**
2. 選 `D:\抽獎\lottery_v2\` 資料夾
3. 如果跳出「這不是 git repository」訊息，點 **create a repository**
4. Name 填 `lottery`，其他預設，按 **Create repository**
5. 上方點 **Publish repository**
   - **取消勾** 「Keep this code private」如果你希望免費部署（Zeabur 免費方案要 public repo）
   - 或維持私人也可，之後在 Zeabur 授權時勾對應 repo
6. 完成後，`https://github.com/你的帳號/lottery` 就會看到你的程式碼

### 2-4 註冊 Zeabur 並部署

1. 到 https://zeabur.com/ 點右上 **登入**，選 **用 GitHub 登入**
2. 進入儀表板，點 **建立專案（Create Project）**
3. 選 **Deploy from GitHub**，第一次會叫你授權 Zeabur 讀你的 repo
4. 選剛剛的 `lottery` repo
5. Zeabur 會自動偵測 Python 專案並部署，過幾分鐘就會顯示「Running」

### 2-5 設定環境變數

在 Zeabur 服務頁面找 **Variables** 分頁，新增：

| 變數 | 值 | 說明 |
|---|---|---|
| `SECRET_KEY` | 一長串隨機字（32 字元以上）| Session 加密用 |
| `ADMIN_USERNAME` | `admin` 或你想要的 | 首次啟動建立管理員 |
| `ADMIN_PASSWORD` | 一組強密碼 | 首次啟動建立管理員 |
| `FORCE_HTTPS_COOKIE` | `1` | 強制安全 cookie |

（產生隨機字：可以隨便打 32 個英文數字，或用線上 password generator）

**設完環境變數要點 Redeploy 讓它生效。**

### 2-6 綁定網址

Zeabur 服務頁面 **Domains** 分頁 → **Generate Domain** → 會給你 `xxxx.zeabur.app`。

打開這網址加 `/login`（例如 `https://xxxx.zeabur.app/login`）應該看到員工登入頁。

管理後台在 `https://xxxx.zeabur.app/admin/login`，用剛才設的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登入。

---

## Part 3：活動當天使用流程

### 3-1 準備階段（活動前一週）

1. 登入管理後台 `/admin/login`
2. **員工管理** → 用「批次匯入」把員工名單貼進去。格式：
   ```
   E001,王小明,櫃檯,pw001
   E002,陳小美,房務,pw002
   E003,林志強,餐飲,pw003
   ```
   > 建議密碼用 `工號 + 出生月日` 或隨意字串，把清單印出來給員工現場對照。

3. **獎品管理** → 逐一新增獎項。例如：
   - iPad mini（特獎）× 1
   - AirPods Pro（頭獎）× 2
   - 超商禮券 500 元（普獎）× 5
   
4. **設定** → 先按「暫停抽獎」，避免員工不小心先抽了。

### 3-2 測試階段（活動前一天）

1. 用你自己的帳號登入抽一次 → 確認流程
2. 記得到 **設定** 頁按「重置」把測試紀錄清空

### 3-3 活動當天

1. 登入後台，**設定** 頁按「開放抽獎」
2. 打開 **QR Code** 頁，投影到大螢幕，同時顯示網址
3. 員工掃 QR Code → 輸入工號密碼 → 抽獎
4. 主持人可以隨時開 **總覽** 頁看即時進度
5. 全部抽完後，**總覽 → 匯出 CSV** 得到中獎名單存檔

### 3-4 活動結束

- 帳號密碼下次要辦活動可以直接沿用；只需 **設定 → 重置** 就能再辦一次
- 或到 Zeabur 把服務停用停止計費（免費方案本來就免費，可不理）

---

## 附錄 A：預設帳號密碼

首次啟動時，如果沒設環境變數 `ADMIN_USERNAME` / `ADMIN_PASSWORD`，會用預設值：

- 帳號：`admin`
- 密碼：`admin1234`

**正式部署前務必改掉！** 部署到 Zeabur 時就在 Variables 設好新的。或登入後台後從 **設定 → 變更管理員密碼** 改。

---

## 附錄 B：常見問題

**Q1：員工可以重複抽嗎？**
不能。系統靠 `employees.has_drawn` 旗標 + `draws.employee_id` 唯一約束 + 資料庫交易，同一員工再按抽獎會被擋。

**Q2：同一份獎品會被兩人抽到嗎？**
不會。每份獎品在 `prize_units` 是一筆 row，status 從 AVAILABLE 改成 ASSIGNED 用條件式 UPDATE + UNIQUE 約束保證只成功一次。

**Q3：如果員工忘記密碼？**
主辦人到 **員工管理 → 編輯** 直接改新密碼即可。

**Q4：獎品全部抽完會怎樣？**
剩下沒抽的員工登入會看到「獎品已抽完」訊息，不會出錯。

**Q5：SQLite 資料會不會不見？**
Zeabur 免費方案的容器如果重啟，`lottery.db` 可能會被清空。**活動當天請勿重啟服務**。想更保險的話：
- 活動前先在後台把資料備好
- 活動結束立即匯出 CSV
- 若真要 100% 保險，可付費升級 Zeabur 或改用有 volume 的方案

**Q6：能不能改活動名稱？**
可以，**設定 → 活動名稱** 改。

**Q7：測試完想重來？**
**設定 → 重置**，輸入 `RESET` 確認即可。

**Q8：可以幾人同時抽？**
可以。SQLite 用 WAL 模式 + `BEGIN IMMEDIATE`，實測撐得住幾百人同時按抽獎按鈕。

---

## 檔案結構參考

```
lottery_v2/
├── app.py                     主程式
├── requirements.txt           Python 套件清單
├── Procfile                   雲端啟動指令
├── runtime.txt                Python 版本
├── seed_data.example.json     員工/獎品範例格式
├── README.md                  本檔案
├── templates/                 網頁樣板
└── static/                    CSS
```

有問題就找當初給你這份程式的人 🙏
