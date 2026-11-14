# 指标口径与分析实验平台

面向业务指标定义、口径管理、聚合查询、分群分析、A/B实验设计、分析方案管理与数据服务发布的数据分析工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标口径 | `metric` | 指标定义 | 指标编号、指标名称、计算公式 |
| 聚合查询 | `queryboard` | 查询模板 | 模板编号、指标名称、维度字段 |
| 分群分析 | `segment` | 人群分群 | 分群编号、分群名称、筛选规则 |
| 实验设计 | `abtest` | 实验方案 | 方案编号、实验名称、对照组定义 |
| 分析方案 | `analysis` | 分析方案 | 方案编号、分析主题、数据集范围 |
| 看板配置 | `dashboard` | 看板页面 | 看板编号、看板名称、包含指标 |
| 数据服务 | `delivery` | 服务版本 | 服务编号、数据集名称、查询参数定义 |
| 漏斗分析 | `funnel` | 漏斗定义 | 漏斗编号、漏斗名称、步骤序列 |
| 同期群分析 | `cohort` | 同期群定义 | 群组编号、群组名称、划分事件 |
| 归因模型 | `attribution` | 归因规则 | 规则编号、归因类型、回溯窗口天 |
| 指标预警 | `alertconfig` | 预警规则 | 预警编号、关联指标、比较运算符 |
| 报表调度 | `reportsch` | 报表任务 | 任务编号、报表名称、包含指标 |
| 维度属性 | `dimprop` | 维度属性 | 属性编号、维度名称、属性名称 |
| 多维下钻 | `datadrill` | 下钻路径 | 路径编号、起始维度、下钻层级 |
| 预测模型 | `forecast` | 预测方案 | 方案编号、预测目标、算法名称 |
| 标签管理 | `tagmgr` | 标签定义 | 标签编号、标签名称、标签分类 |
| 洞察推送 | `insight` | 洞察报告 | 报告编号、报告标题、分析主题 |
| 数据验证 | `dataval` | 验证任务 | 任务编号、验证数据集、验证类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 维度属性版本准入（dimprop ↔ queryboard）

维度属性与聚合模板共享枚举版本，枚举/数据类型/数据源的唯一事实源是
`dim_versions` 版本注册表，而不是列表行上的展示字段：

- 每次「发布属性」生成不可变新版本（`v1`、`v2`……），旧发布版本同步退役；
  「下线属性」退役当前发布版本；「回滚」把历史版本内容复制成新版本，旧版本号不复活。
- 模板绑定具体发布版本，`snapshot_*` 字段只从该版本读取。属性下架或换版后，
  引用旧版本的已启用模板自动回到「模板验证中」，草稿字段保留并写入
  `validation_error`，聚合缓存按版本键清理，模板不可再启用。
- 准入错误码：`DIMENSION_PROPERTY_RETIRED`、`DIMENSION_PROPERTY_NOT_FOUND`、
  `DIMENSION_PROPERTY_EMPTY_ENUM`、`DIMENSION_PROPERTY_UNBOUND`，
  错误上下文随动作接口的 `error` 字段返回（含 `property_id`、`retired_version`）。
- 跨表写入走全局锁 + 快照事务（`app/services/locking.py`），任一步失败整体回滚，
  模板保持不可用；服务启动时 `reconcile()` 巡检并修复存量脏状态。
- 互查入口：`GET /api/dimprop/property/{属性编号}/versions`、
  `.../publications`，以及 `GET /api/queryboard/{id}/aggregate`。
- `LOCAL_TEST=1` 时额外挂载 `/api/_test/reset` 与 `/api/_test/faults`，
  配合 `backend/tests/acceptance_test.py` 做下架、枚举改名、空枚举、回滚、
  并发换版与写入失败回滚的验收。

领域：数据工程
子方向：数据分析与数据应用
