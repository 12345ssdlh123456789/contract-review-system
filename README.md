# 企业合同智能审查系统

一个**完整可跑**的企业级 AI 应用示例：法务把合同传进来，AI 逐条审查风险点，法务确认或修改后，修改记录回流成评测数据。

不是 Demo —— 权限在 SQL 层、模型挂了会降级、prompt 有版本号能回滚、每次调用都记 token 和成本、有 20 份带标准答案的评测集能跑分。

---

## 快速开始

双击 **`启动.bat`**，浏览器会自动打开 `http://127.0.0.1:3000`。

首次启动需要装依赖（脚本会自动检测并 `pip install`）。

演示账号（密码统一 `demo123456`）：

| 用户名 | 角色 | 能看到什么 |
|---|---|---|
| `submitter` | 业务提交人 | 自己部门的合同（上传/提交，不能发起审查） |
| `reviewer` | 法务审查员 | 全部合同 + **发起审查** + 反馈 |
| `finance` | 财务 | 全部合同，但**只有付款/交付条款** |
| `admin` | 管理员 | 看板 + 评测 + Prompt 版本 + 审计日志（**看不到合同正文**，合同接口直接返回空列表，详情 404） |

**谁能发起审查**：只有 `reviewer`。提交人上传完等法务审，管理员只看统计，后端对其它角色直接返回 403——按钮在前端也会跟着隐藏。

**推荐的演示动作**：用 `reviewer` 打开一份合同记下条款数 → 换 `finance` 打开同一份 → 只剩 2 条，顶部提示「有 5 条条款因权限设置对你不可见」。这是权限真的做在数据层的证据。

---

## 技术栈

| 层 | 选型 | 为什么 |
|---|---|---|
| 后端 | FastAPI 0.143 | 自动生成 OpenAPI 文档，类型驱动 |
| ORM | SQLAlchemy 2.0 | 权限能写成 SQL 条件，而不是查出来再过滤 |
| 数据库 | SQLite | 单文件零配置，方便演示；生产应换 PostgreSQL |
| 认证 | JWT (HS256) + bcrypt(12轮) | 无状态；bcrypt 故意慢，抗暴力破解 |
| 模型 | 智谱 glm-4-flashx | OpenAI 兼容协议，换供应商只改 3 行 `.env` |
| 前端 | 原生 HTML/CSS/JS | 无构建步骤，数据流一目了然 |

---

## 目录结构

```
企业合同智能审查系统/
├── 启动.bat                     一键启动（自动开浏览器）
├── 停止.bat                     停掉两个端口
├── docs/
│   └── 学习手册.md              ★ 从零到跑通的完整讲解，含所有踩坑记录
├── backend/
│   ├── requirements.txt
│   ├── .env.example            配置模板（复制成 .env 后填 key）
│   ├── contract.db             SQLite（首次启动自动建表+种子数据）
│   ├── app/
│   │   ├── config.py           所有配置从 .env 读，密钥不写死
│   │   ├── database.py         引擎与 session
│   │   ├── models.py           ★ 数据模型：权限设计全在这里
│   │   ├── schemas.py          Pydantic 出入参
│   │   ├── security.py         ★ 三层权限 + 审计写入
│   │   ├── parser.py           ★ 抽文本→切条款→分类→质量自检
│   │   ├── prompts.py          ★ v1/v2 prompt 定义
│   │   ├── llm.py              超时分级 + 指数退避 + JSON 提取
│   │   ├── review_service.py   ★★ 核心：降级链 + 结果校验
│   │   ├── eval_dataset.py     20 份合成合同 + 埋雷标注
│   │   ├── eval_runner.py      评分逻辑（召回率/误报率/F1）
│   │   ├── seed.py             演示账号与示例合同
│   │   └── routers/
│   │       ├── auth.py         登录、发 token、写登录审计
│   │       ├── contracts.py    上传/解析/列表/详情/审查/反馈
│   │       ├── admin.py        审计查询、看板、Prompt 版本切换
│   │       └── eval.py         触发评测、查历史跑分
│   └── uploads/                上传的原始文件
├── frontend/
│   ├── index.html              单页四屏（登录 / 主界面 / 详情 / 弹窗）
│   ├── style.css
│   └── app.js                  约 1000 行，全在一个文件里
└── tests/
    ├── test_backend_e2e.py     27 项端到端 API 测试
    ├── test_ui.js              31 项 Playwright UI 测试
    └── run_eval.py             跑完整评测并打印分数
```

---

## 核心设计（为什么这么做）

### 1. 权限在 SQL 层，不在界面

```python
# security.py
def contract_visibility_filter(user):
    if user.role in ("reviewer", "admin"):
        return True
    return or_(Contract.dept_id == user.dept_id,
               Contract.submitter_id == user.id)
```

**数据库根本不返回越权行**，内存里也就不存在泄漏的可能。
在界面上把按钮藏起来不是权限，是装饰 —— 抓包就绕过了。

### 2. 条款按块存储，字段级权限才可能实现

财务只能看付款条款，看不到违约责任。如果整篇合同存成一个 `raw_text`，
你只能靠 prompt 说「别提违约责任」—— 模型完全可能顺口提一句。

所以 `Clause` 表带 `visibility` 字段，喂给模型**之前**就把不可见的块剔除。
模型连"知道"的机会都没有。

实测：财务打开 7 条条款的合同，API 只返回 2 条。

### 3. 模型挂了降级，但必须让用户知道

```
LLM 超时/5xx  → 规则引擎（8 条正则）→ 界面黄色横幅「降级模式，建议人工复核」
JSON 解析失败 → 同上
token 超限    → 直接拒单，不发请求
```

规则引擎召回率只有 25%（vs 模型 88.9%），但永不超时、永不花钱，能兜住最硬的几类问题。

**偷偷降级、界面看起来一切正常是最糟的设计。**

### 4. 模型会撒谎，结果必须校验

`normalize_findings()` 做三件事：

- 编造的条款号（合同只有 12 条，它说第 15 条）→ **直接丢弃**
- `level` 写成中文「高」→ 映射成 `high`
- `category` 写成「违约责任」→ 映射成 `liability`（30+ 条别名表）

最后一条不做的话，评测的 category 匹配全部失效，召回率会莫名掉一大截。

### 5. Prompt 存数据库，带版本号

改三个字可能让召回率掉 30 个点。没有版本号，你连「改之前是什么样」都找不回来。
每次修改新增一行，不覆盖；`is_active` 标记当前生效；切换 = 改两个布尔值。

### 6. 先做评测集，再写代码

20 份合成合同，覆盖采购/服务/租赁/保密四类，每份埋 0–5 个已知风险点，
其中 4 份是**故意不埋雷的干净合同**（用来测误报）。

---

## 评测结果（真实跑分）

20 份合同 / 36 个埋雷 / 132 条正常条款，模型 `glm-4-flashx`：

| 跑次 | 配置 | 召回率 | 误报率 | F1 | 精确率 | 平均耗时 | token |
|---|---|---|---|---|---|---|---|
| #1 | 规则引擎基线 | 25.0% | 8.3% | 32.1% | 45.0% | 0ms | 0 |
| #2 | v1 初版 prompt | **88.9%** | 31.1% | 58.7% | 43.8% | 6265ms | 14601 |
| #3 | v2 强化 prompt | 83.3% | **20.4%** | **64.5%** | **52.6%** | 5583ms | 22792 |

**怎么读**：v1 抓得全但报得多（每份合同 3 条假问题，法务会被噪音淹没）；
v2 用 5.6 个点的召回换回 10.7 个点的精确度，F1 从 58.7% 涨到 64.5% —— 这是正向优化。

企业取舍明确：**宁可误报，不可漏报**。漏一条无限连带责任可能赔掉一年利润；
误报一条，法务多看一眼而已。

跑分：`python tests/run_eval.py`（约 2 分钟，会真实调 20 次模型）

---

## 测试

```powershell
# 后端端到端（27 项）
python tests/test_backend_e2e.py

# 前端 UI（31 项，需要 playwright + chromium）
node tests/test_ui.js
```

实测：**后端 27/27 通过，前端 31/31 通过。**

UI 测试覆盖：登录校验、错误密码、四个角色登录、合同列表与详情、
条款权限过滤、看板 7 个统计卡、评测页、审计日志、新建合同弹窗、
tab 按角色隐藏、退出登录。

---

## API

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/auth/login` | 公开 | 返回 JWT |
| GET | `/api/auth/me` | 登录 | 当前用户 |
| POST | `/api/auth/logout` | 登录 | 写审计 |
| GET | `/api/contracts` | 登录 | 列表（SQL 层过滤） |
| POST | `/api/contracts/upload` | submitter+ | 上传文件 |
| POST | `/api/contracts/text` | submitter+ | 粘贴文本创建 |
| GET | `/api/contracts/{id}` | 登录 | 详情（条款已过滤） |
| POST | `/api/contracts/{id}/review` | reviewer | 跑审查（仅法务） |
| POST | `/api/contracts/{id}/feedback` | reviewer+admin | 采纳/改后采纳/驳回 |
| GET | `/api/dashboard` | admin | 看板统计 |
| GET | `/api/audit` | admin | 审计日志（只读，无写接口） |
| GET | `/api/prompts` | reviewer+admin | prompt 版本列表 |
| POST | `/api/prompts/{id}/activate` | admin | 切换生效版本 |
| POST | `/api/eval/run` | admin | 跑评测 |
| GET | `/api/eval/runs` | admin | 历史跑分 |
| GET | `/api/health` | 公开 | 健康检查 |

完整文档：`http://127.0.0.1:8210/docs`

---

## 配置

复制 `backend/.env.example` 为 `backend/.env`，填 `LLM_API_KEY`。

```ini
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
LLM_API_KEY=你的智谱key
LLM_MODEL=glm-4-flashx

# 成本护栏
MAX_TOKENS_PER_CONTRACT=30000
MAX_CONTRACTS_PER_USER_PER_DAY=50
MAX_CONTRACT_CHARS=60000
```

`JWT_SECRET` 首次启动会自动生成并写回 `.env`，不用手动填。

**没配 key 也能跑** —— 审查会自动走规则引擎降级，界面会标黄提示。

---

## 这个项目**不是**生产系统

差距清单见 `docs/学习手册.md` 第十五节。最关键的几条：

- SQLite → PostgreSQL
- `.env` 明文密钥 → KMS/Vault
- HTTP → HTTPS
- `allow_origins=["*"]` → 收窄域名
- 同步审查（5–9 秒阻塞）→ 队列 + SSE
- 无速率限制 → 三层限流
- 审计日志同库 → 独立存储 + 哈希链

---

## 想学的话

读 **`docs/学习手册.md`**。它不是 API 手册，而是每一步「为什么这么做、不做会怎样」，
包含十三个真实踩过的坑（依赖冲突、中文类别没映射、后端没重启、
`required` 挡住自定义校验……）。

如果只看一个源文件，看 `backend/app/review_service.py` ——
它把「调模型 → 出错 → 降级 → 校验 → 落库」整条链路串起来了。
