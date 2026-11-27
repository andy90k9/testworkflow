# DSH 全链路测试优化改进方案

- 文档日期：2026-11-27
- 能力定位：**通用测试能力建设**，不绑定任何具体项目
- 当前样例目录：由运行环境提供，不在能力代码中固定
- 关联产物：`reports/case-asset-inventory.json`（机器可读资产清单）
- 核心目标：把“手写冒烟集”升级为“规格驱动、历史复用、可追溯、可回流”的**通用**用例资产体系

> **全局最高约束：禁止写死。**
>
> 本方案交付的是**可迁移的能力**，不是某个项目的专用脚本。任何业务数据、用例数量、ID 前缀、接口路径、账号凭证、环境地址、质量阈值、模块名称，都只能来自 spec / 配置 / 运行时输入，**不得写死在代码、脚本、报告模板或默认配置里**。本文档中出现的所有数字、路径、项目名均为当前样例的说明性证据，实现时必须参数化。

---

## 一、结论摘要

本轮 37 条用例全部通过是真实的，但**覆盖强度不足**：

- 后端 637 个 Java 文件、179 个 Mapping 注解、140 个去重路径，API 只覆盖 15 个路径（约 10.7%）。
- 前端 21 个 Vue view、12 个本地业务路由 + RBAC 动态路由，UI 只 `goto` 到 6 个不同路由。
- UI 动作中 `expect_text` 17 次、`click` 1 次、`skill` 1 次，无 `fill`/`select`/下载/表单/弹窗。
- 功能用例 15 条，与历史 `REG-SCN-*` 18 条场景**没有 ID 追溯关系**。

根因不是“用例产出匹配了真实页面所以少”，而是：

> **已有完整规格，但生成/转换链路断开。DSH 在 `--skip-gen` 下直接跑了一套手写最小集。**

已有的 `qa-regression/20261120/spec.json`（18 场景 / 50 API / 15 UI）和对应执行证据（50 API、15 UI、31 条语义校验全部通过）**没有被复用**。

按资产化原则重新梳理后：

| 处置 | 数量 | 含义 |
|---|---:|---|
| 复用转换 | 55 | 历史已有、已通过，转成 DSH 用例即可 |
| 修改合并 | 28 | 历史与 DSH 手写集重叠，需合并断言并补追溯 |
| 新增补齐 | 6 类 | 历史与 DSH 都没有的真实缺口 |

当前样例的目标用例规模：**18 场景 + 50 API + 15 UI = 83 条**（加上新增补齐后更多），而不是当前 37 条。

但要强调：83 条也不等于“完美覆盖”。本方案采用**风险驱动覆盖模型**（第七章）和**质量度量门禁**（第十章），以 P0/P1/P2 分层作为验收口径，不追求穷举所有组合。

> 上述数量、ID 前缀、路径和项目名均为**当前样例的说明性证据**。通用能力实现必须从 spec / 配置 / 运行时读取这些值，禁止写入代码默认值或报告模板。

---

## 二、现状盘点

### 2.1 平台规模 vs 当前覆盖

| 维度 | 平台实际 | DSH 当前覆盖 | 差距 |
|---|---:|---:|---|
| 后端接口 | 140 个去重路径 | 15 个去重路径 | 缺 32 个历史 spec 路径，更多管理端路径未测 |
| 写操作 | PUT 8 / DELETE 7 / PATCH 3 / POST 74 | 0 条 PUT/DELETE/PATCH | 写链路基本空白 |
| 前端页面 | 21 个 Vue view / 12 本地路由 + 动态路由 | 6 个实际 goto 路由 | 动态路由、MCP/创作中心/审批待办等未覆盖 |
| UI 交互 | 筛选/分页/弹窗/上传/审批等 | 1 次 click、1 次 skill | 静态渲染冒烟 |
| 功能场景 | 历史 spec 18 条 | 手写 FUNC 15 条 | 无 ID 追溯，场景集不同 |

### 2.2 历史资产清单

| 资产 | 规模 | 状态 | 处置 |
|---|---:|---|---|
| `qa-regression/20261120/spec.json` | 18 场景 / 50 API / 15 UI | 完整规格 | 作为唯一事实源 |
| `qa-regression/20261120/api-plan.json` | 50 条 API 可执行计划 | 50/50 通过 | 复用 |
| `qa-regression/20261120/api-results-final.json` | 50 条结果 + 证据 | 全通过 | 复用为基线证据 |
| `qa-regression/20261120/ui-results-final.json` | 15 条 UI 结果 + 截图 | 全通过 | 复用为基线证据 |
| `qa-regression/20261120/semantic-results.json` | 31 条语义校验 | 全通过 | 复用为契约基线 |
| `qa-regression/20261120/cases.md` | 10 类场景分组 | 人类可读 | 复用为评审材料 |
| `qa-toolkit/plugin/dsh-qa-casepipe` | `gen_cases.py` 等 | 只输出 Markdown/Excel | 可辅助评审，不能直接产 DSH YAML |
| `dsh-runs/dsh-workflow/cases/*.yaml` | 15 FUNC / 27 API / 10 UI | 手写最小集 | 按分类合并或补充 |

### 2.3 断链证据

1. `run-safe.sh` 调用 `run_pipeline.py --skip-gen`，生成阶段被跳过。
2. `config/providers.yaml` 中 `harness.enabled: false`。
3. 本套 DSH 的 `scripts/` 中没有 `qa_casepipe.py`，`workflow.yaml` 中也没有 qa-casepipe 配置段。
4. `project/requirements/regression-spec.json` 与 `qa-regression/20261120/spec.json` 内容完全一致，但从未进入用例生成。
5. DSH 用例 ID 为 `API-001`/`UI-001`/`FUNC-001`，spec 为 `REG-API-*`/`REG-UI-*`/`REG-SCN-*`，无映射表。

结论：**转换器缺失 + 生成阶段跳过 = 规格被静默忽略。**

---

## 三、资产分类：复用 / 修改 / 新增

完整逐条清单见 `reports/case-asset-inventory.json`，本节给出原则与代表项。

### 3.1 复用转换（55 条）

历史已存在、已通过，直接转成 DSH 用例，不重新设计。

| 层级 | 复用数 | 代表项 |
|---|---:|---|
| API | 35 | 角色/菜单管理、Skill 字典/版本/下载/文件、知识库树/质量/同步/导入模板、MCP 广场、文件签名/预览、上下架分页、审核日志 |
| UI | 9 | 匿名 SSO 引流、模型详情与申请入口、模型试用与流式渲染、智能体广场、MCP 广场、文档预览新窗口、503 错误态与重试、最小权限菜单 |
| 场景 | 11 | 模型详情、试用、智能体广场、MCP 广场、上下架、文档预览、错误态、权限收回、文件签名/非法入参 |

这些不是“新增用例”，而是**把已验证的资产搬到 DSH 体系并保留来源 ID**。

### 3.2 修改合并（28 条）

历史 spec 与 DSH 手写集指向同一功能，但 DSH 断言更弱或口径不同，需要合并为一条权威用例。

| 层级 | 合并数 | 典型问题 |
|---|---:|---|
| API | 15 | DSH 只断言 `code=0` 或精确数量，历史 spec 有 `dataKeys`、负值/边界/异常分支；需合并为“成功契约 + 数据契约 + 异常契约” |
| UI | 6 | DSH 仅“页面可达/文字可见”，历史 spec 有卡片数与总数一致、页签与队列、导航菜单渲染、 Skill 列表等；需补交互和数值断言 |
| 场景 | 7 | `REG-SCN-002/003/010/020/021/030/041` 与 `FUNC-001/003/010/008/012/006/007` 对应，需建立双向追溯 |

重点修改项：

- `API-013/023/025` 精确断言 `23/63/44` → 改为范围/相对断言。
- `API-005/006/011/014` 等 hand-picked 端点 → 与 spec 中的 `model/square/page`、`agent/square/page` 等契约合并。
- `UI-003/004/005/006/007` → 从“可达”升级为“卡片数/页签/菜单/队列”等可判定断言。

### 3.3 新增补齐（6 类）

历史 spec 和 DSH 都没有的真实缺口，才允许新增：

1. **写操作链路**：申请提交/撤回/重提、审批通过/驳回、上下架、Skill 版本发布与下载、角色授权、知识库导入、文件上传落库。
2. **权限矩阵**：匿名 / 普通用户 / 管理员三身份 × 关键接口，覆盖 401/403/404 与越权访问。
3. **会话链路**：token 过期、刷新、退出登录、SSO 回调异常。
4. **前端动态路由**：RBAC 动态菜单、`/model-market/api-docs`、`/ai-creation`、`/approval-todos`、`/mcp-market`、`/agent-market/:id`。
5. **前端交互**：筛选、分页、弹窗、表单、上传下载、空态/加载态/503 错误态。
6. **数据契约治理**：把精确数量断言改为范围或相对断言，建立测试数据基线快照。

### 3.4 DSH 手写用例处置

| 类型 | 处置 |
|---|---|
| API 中与 spec 路径一致的 15 条 | 合并进 `REG-API-*` 规范用例，保留 DSH 执行证据 |
| API 中不在 spec 的 4 条（`/agent/square`、`/mcp/list`、`/model/square/1`、`/skill/list`） | 逐条评审：若是有效补充则沉淀为新 spec 用例，否则废弃 |
| UI 10 条 | 6 条并入对应 `REG-UI-*`，`UI-009 用户菜单`、`UI-010 会话存活` 作为新场景补录进 spec |
| FUNC 15 条 | 7 条与 `REG-SCN-*` 建立映射，2 条作为新场景补录，其余保留评审 |

---

## 四、数据回流、沉淀、复用机制

### 4.1 四层资产模型

| 层 | 事实源 | 产物 | 负责人 |
|---|---|---|---|
| 规格层 | `regression-spec.json` | 18/50/15 规格 | 需求/测试 |
| 用例层 | `cases/*.yaml` | DSH 可执行用例 | 转换脚本生成，人工只评审 |
| 执行层 | `records/<run_id>/` | 明细、证据、截图 | 流水线 |
| 资产层 | `reports/case-asset-inventory.json` + MySQL | 追溯、复用统计、版本 | 自动回流 |

原则：**规格层是事实源，用例层是派生物，不允许长期手写旁路。**

### 4.2 用例来源追溯字段

所有 DSH 用例统一增加来源字段（可放在 YAML 顶层或 `ext_json`）：

| 字段 | 含义 |
|---|---|
| `source_id` | `REG-API-020` / `REG-UI-003` / `REG-SCN-010` |
| `source_file` | `qa-regression/20261120/spec.json` |
| `source_version` | spec 的 git commit 或生成日期 |
| `action` | `reuse_convert` / `modify_merge` / `add_new` |
| `spec_hash` | 规格条目 SHA1，检测规格变更 |
| `run_id` | 流水线注入，保持不变 |

### 4.3 转换器 `spec_to_cases.py`

新增脚本，在 `--skip-gen` 之前执行：

1. 读取 `project/requirements/regression-spec.json`。
2. 按 `REG-SCN-* → FUNC-*`、`REG-API-* → API-*`、`REG-UI-* → UI-*` 生成三类 YAML。
3. UI 的 `steps_playwright` 是 JS 片段，需解析/映射为 DSH DSL action，不能直接复制字符串。
4. 写入 `source_id`、`source_file`、`source_version`、`action`、`spec_hash`。
5. 输出 `reports/spec-conversion-report.json`，列出 reuse/modify/add 统计。

### 4.4 硬校验

转换完成后自动比对：

- spec 中声明的全部 ID 是否都出现在 cases 中；
- 每个 `source_id` 是否有且仅有一条 DSH 用例承接；
- 缺失或重复即退出码非 0，流水线中止。

这样不会再出现“完整规格静默降级为手写子集”。

### 4.5 MySQL 回流

现有 `dsh_test_cases.ext_json` 可直接承载来源追溯，无需改表结构：

```json
{
  "source_id": "REG-API-020",
  "source_file": "qa-regression/20261120/spec.json",
  "source_version": "20261120",
  "action": "reuse_convert",
  "spec_hash": "...",
  "historical_evidence": "api-results-final.json:REG-API-020"
}
```

回流时把 `action`、复用率、新增率写入 `dsh_test_runs.ext_json`，形成批次级质量趋势。

### 4.6 数据沉淀目录规范

```text
qa-regression/                 # 规格与历史执行证据（事实源）
  <date>/spec.json
  <date>/api-plan.json
  <date>/*-results*.json
dsh-runs/dsh-workflow/         # 派生与执行
  cases/                       # 由 spec 转换生成，人工评审
  records/<run_id>/            # 执行明细（含来源追溯）
  reports/case-asset-inventory.json
  reports/spec-conversion-report.json
  OPTIMIZATION.md
```

禁止在 `dsh-runs/` 下长期维护脱离 spec 的手写用例副本。

---

## 五、落地计划

| 阶段 | 工作 | 验收标准 |
|---|---|---|
| P0 立即 | 固化 spec 为事实源并锁定版本 | spec 中全部 ID 可列举，git 可追溯 |
| P1 | 实现 `spec_to_cases.py` | 三类用例按 spec 结构生成，ID 映射无缺失 |
| P2 | 合并 DSH 手写用例 | 重叠用例完成 modify_merge，歧义用例完成评审，缺口补录 spec |
| P3 | 补齐新增类别 | 写操作/权限矩阵/会话/动态路由/交互/数据契约均有用例 |
| P4 | 接入硬校验与回流 | 缺 ID 即失败；`ext_json` 含来源追溯；报告展示复用率/新增率 |
| P5 | 持续治理 | 每次需求变更后重跑转换器与差异报告，人工只评审增量 |
| P6 | Shadow 切换 | 新用例集与当前用例集并行跑 N 轮（配置），差异确认后切换；旧用例归档保留 |
| P7 | 用例分层与资产台账 | 核心基线用例冻结版本；全部资产记录项目/分支/环境/轮次编码并回流两新表 |

目标状态：

- DSH 用例总数 ≥ spec 全量 ID + 新增补齐项；
- API 路径覆盖达到 spec 声明的路径全集，再按配置扩展到平台其他路径；
- UI 覆盖 spec 中全部 UI 场景，交互型用例占比达到配置阈值；
- 每条用例可追溯到 `source_id` 或显式标注为 `add_new`，并声明 `tier`；
- 核心基线用例独立版本化，`core_pass_rate` 每轮输出且失败阻断发布；
- 精确数量断言数量为 0，改用基线快照 + 漂移范围；
- 全部资产在 `dsh_asset_registry` 中可反查项目/分支/环境/轮次；
- 连续 N 轮 `flaky_rate` 低于配置阈值，MySQL 回流完整率达到配置目标；
- 报告结论区分“冒烟通过 / 规格回归通过 / 风险模型全绿”。

---

## 六、风险与注意

1. **不要直接复制 `steps_playwright` 字符串**：它是 JS 代码，必须映射为 DSH DSL，否则 EGO 通道无法执行。
2. **不要把精确数据量写死**：`23/63/44` 这类断言必须改成范围或基线快照对比。
3. **token 不能进仓库**：继续沿用 `run-safe.sh` 的占位符 + 运行期注入 + trap 回填机制。
4. **历史证据只是基线，不等于当前通过**：复用前需在当次环境重跑一遍。
5. **写操作要隔离数据**：新增的提交/审批/上架用例必须使用独立测试数据，避免污染历史基线。
6. **转换失败要硬失败**：不能像这次一样静默用旧手写用例继续跑并报 100%。

---

## 七、风险驱动覆盖模型（不追求“完美覆盖”）

覆盖目标不是穷举所有组合，而是**关键风险充分覆盖、且覆盖状态可度量**。按业务影响面分三层：

| 层级 | 范围 | 覆盖要求 | 允许例外 |
|---|---|---|---|
| P0 | 认证会话、权限上下文、申请治理、写操作链路 | 100% 转换 + 100% 执行 + 连续 2 轮稳定；必须含 401/403/404 与越权负向 | 无 |
| P1 | 模型广场、Skill、知识库、智能体、MCP、模型试用 | 100% 转换；执行通过率 ≥95%；未跑用例必须写原因 | 外部三方依赖可标记跳过 |
| P2 | 管理端、上下架、文件签名、统计类接口 | 100% 转换；执行通过率 ≥80%；核心路径必跑 | 低频管理动作可抽样 |

覆盖维度至少覆盖四类交叉：

1. **身份维度**：匿名 / 普通用户 / 管理员。
2. **状态维度**：成功 / 空结果 / 越界 / 异常 / 服务错误。
3. **操作维度**：读 / 写 / 撤销 / 审批 / 重试。
4. **入口维度**：直接 URL / 导航跳转 / 刷新恢复 / 会话过期后重进。

明确**不纳入本轮目标**：全排列组合、像素级视觉回归、第三方 SSO 内部实现、性能压测、安全渗透。这些应另立专项，不塞进 DSH 用例数里制造“看似覆盖”的假象。

---

## 八、测试数据与隔离策略

### 8.1 三类数据

| 类型 | 用途 | 策略 |
|---|---|---|
| 基线只读数据 | 数量统计、列表分页、字典 | 不写死绝对值；用 `baseline_snapshot` + 允许漂移范围 |
| 写操作沙箱数据 | 申请提交、审批、上架、授权、上传 | 统一前缀 `E2E_`，用例结束清理；失败也要清理 |
| 身份凭证 | 匿名/普通用户/管理员 | 由 `run-safe.sh` 运行期注入，仓库内只保留占位符 |

### 8.2 数据准备与清理

1. 每条写用例声明 `test_data.fixture` 与 `test_data.cleanup`。
2. 执行前创建独立数据，执行后（含失败）清理；清理失败记为用例失败，不允许静默残留。
3. 读用例遇到数据量断言时，优先断言 `data` 结构与分页契约，而不是精确总数。
4. 基线快照存在 `records/<run_id>/data-baseline.json`，作为相对断言依据。

### 8.3 禁止事项

- 禁止在用例里写死 `23/63/44` 这类业务数量。
- 禁止复用生产账号做写操作。
- 禁止把真实 token 写入 `cases/`、报告或数据库；继续沿用 `__DSH_API_TOKEN__` + trap 回填。

---

## 九、转换器详细设计：`spec_to_cases.py`

### 9.1 字段映射

| spec 字段 | DSH 字段 | 规则 |
|---|---|---|
| `scenarios[].id` | `FUNC-xxx` + `source_id` | DSH ID 顺序生成，`source_id` 保留权威 ID |
| `scenarios[].chain` | `module` | 直接映射，如 `A 认证与会话` |
| `scenarios[].name` | `title` | 直接映射 |
| `scenarios[].steps/expected` | `steps[].action/expect` | 自然语言需人工确认 DSL；无法映射的标记 `needs_manual_dsl` |
| `scenarios[].related` | `related_func` / 关联关系 | 建立 `REG-SCN-* → FUNC-* → API/UI` 三层链路 |
| `api[].id` | `API-xxx` + `source_id` | 保留 `REG-API-*` 追溯 |
| `api[].method/url` | `request.method/path/params` | `{base}` 去掉；`{{id}}` 转 path 模板或 params |
| `api[].headers` | `request.headers` | `Authorization` 转成 `__DSH_API_TOKEN__` |
| `api[].body` | `request.body` | JSON 字符串按对象解析，解析失败保留原串并告警 |
| `api[].expect` | `expect.status_code/json_path/equals/contains` | 正则解析；无法解析的降级为 `contains` 并标记待评审 |
| `ui[].id` | `UI-xxx` + `source_id` | 保留 `REG-UI-*` 追溯 |
| `ui[].steps_playwright` | `steps[].action` | JS 片段转 DSL；仅支持已登记动作，未知动作标 TODO |
| `ui[].precondition_ui` | `precondition` | 登录态/清 storageState 等前置条件显式化 |

### 9.2 转换原则

- **ID 是权威**：`source_id` 不可以丢；DSH 编号只是执行别名。
- **宁可标待办，不静默弱化**：无法解析的断言不能悄悄变成“只查状态码”。
- **人工只评审增量**：转换后自动对比上一版，只把变更项推给人看。
- **输出转换报告**：`reports/spec-conversion-report.json` 记录映射、告警、待办数量。
- **零硬编码**：ID 前缀、字段名、URL 变量语法、断言解析规则、输出目录全部从 `config/spec-adapter.yaml` 读取；不同项目通过配置适配，不改代码。
- **可插拔解析器**：`steps_playwright`、`expect` 文本等解析逻辑以插件形式注册；新项目新增解析器，不修改转换器主干。
- **禁止项目专属分支**：转换器里不允许出现 `if 项目名 == ...` 这类分支；项目差异一律走配置或插件。

---

## 十、增量更新与闭环

### 10.1 spec 差异驱动

对每批 spec 按 `id + spec_hash` 做差异：

| 差异 | 动作 |
|---|---|
| 新增 ID | 生成新用例，标记 `add_new` |
| 删除 ID | 用例标记 `deprecated`，不直接物理删除 |
| 内容变更 | 标记 `modify_merge`，进入人工评审 |
| 无变化 | 保持原追溯，不重复生成 |

差异产物：`reports/spec-diff.json`。

### 10.2 CI 门禁

spec 提交后自动执行：

1. `spec_to_cases.py` 转换；
2. `validate_cases.py` 校验；
3. ID 完整性硬校验：spec 中声明的全部 `source_id` 缺失或重复即失败，**数量由 spec 决定，不写死**；
4. 生成 `quality-metrics.json` 并在报告展示。

### 10.3 灰度切换

- 转换后的用例集先以 **shadow 模式**与当前用例集并行跑 N 轮（N 由配置决定）。
- 对比两者结果差异，确认无误后再切换正式用例集。
- 旧用例归档保留，不立即删除，便于回滚。

### 10.4 质量度量

所有阈值必须从 `config/quality.yaml` 读取，**代码不得写死**；下表仅为默认建议值，按项目配置覆盖。

| 指标 | 默认建议 | 配置键 |
|---|---:|---|
| `spec_conversion_rate` | 100% | `quality.spec_conversion_rate` |
| `traceability_rate` | 100% | `quality.traceability_rate` |
| `api_path_coverage_spec` | 100%（spec 路径全集） | `quality.api_path_coverage` |
| `ui_interaction_case_ratio` | ≥50% | `quality.ui_interaction_ratio` |
| `exact_count_assertions` | 0 | `quality.exact_count_assertions` |
| `reuse_rate` | ≥65% | `quality.reuse_rate` |
| `flaky_rate` | <2%（轮数可配） | `quality.flaky_rounds` / `quality.flaky_rate` |
| `db_backflow_completeness` | 100% | `quality.db_backflow_completeness` |
| `core_pass_rate` | 100% | `quality.core_pass_rate` |
| `asset_registry_completeness` | 100% | `quality.asset_registry_completeness` |
| `baseline_coverage` | 按配置 | `quality.baseline_coverage` |

报告结论按覆盖等级输出，不再统一写“可进入发布评估环节”：

- 仅冒烟通过 → “仅证明主链路可达”；
- 规格回归通过 → “覆盖历史 spec 范围，可进入发布评估”；
- 风险模型全绿 → “覆盖 P0/P1 关键风险，可作为发布依据之一”。

---

## 十一、用例分层：核心基线 / 扩展 / 临时

所有用例必须显式声明层级，禁止把所有用例混为一谈。

| 层级 | `tier` | 定义 | 门禁规则 |
|---|---|---|---|
| 核心基线 | `core` | 认证会话、权限上下文、主业务 Happy Path、关键数据契约 | 每轮必跑；任一失败即阻断发布；变更需评审并升级 `baseline_version` |
| 扩展回归 | `extension` | spec 中的 P1/P2 场景、负向、边界、错误态 | 按配置频率跑；失败计入质量指标，是否阻断由配置决定 |
| 临时验证 | `temporary` | 问题复现、探索性验证、一次性排查 | 必须声明 `expire_at`；到期自动归档，不进入发布门禁 |

### 11.1 用例层字段

| 字段 | 含义 |
|---|---|
| `tier` | `core` / `extension` / `temporary` |
| `is_core` | 是否核心基线，布尔值 |
| `baseline_version` | 基线契约版本；`core` 用例必填 |
| `source_id` | 历史 spec ID 或 `add_new` |
| `drift_policy` | `exact` / `range` / `snapshot`，数据断言策略 |
| `expire_at` | 仅 `temporary` 必填 |

### 11.2 核心基线管理

1. `core` 用例单独形成基线集合，锁定 `baseline_version` 与 `contract_json`。
2. 基线变更必须走评审：谁改、为什么改、影响哪些轮次，全部入 `ext_json`。
3. 报告分开统计：`core_pass_rate` 与全量 `pass_rate`，避免扩展用例掩盖核心回归。
4. 核心基线的失败优先定位，不允许用“重跑一次就好了”直接放行。

---

## 十二、资产台账与新增库表

### 12.1 资产范围

所有进入链路的资产都必须登记，不只是用例：

`spec` 规格、`case` 用例 YAML、`script` 执行/转换脚本、`report` 报告、`screenshot` 截图、`ddl` 建表语句、`baseline` 基线契约、`record` 执行明细。

### 12.2 统一维度

每条资产记录以下维度，全部来自配置或运行时，不允许写死：

| 维度 | 字段 | 来源 |
|---|---|---|
| 项目 | `project_code` | 配置 |
| 代码仓库 | `git_repo` | 配置或 `git remote` |
| 分支 | `git_branch` | 运行时读取 |
| 提交 | `git_commit` | 运行时读取 |
| 测试环境 | `env_code` | 配置 |
| 测试轮次编码 | `run_code` | 按配置模板生成，如 `{project}-{env}-{date}-{seq}` |
| 关联批次 | `run_id` | 流水线生成 |
| 资产身份 | `asset_type/path/version/hash` | 文件系统计算 |
| 归属与状态 | `owner/status` | 配置 + 生命周期 |

`run_code` 的生成模板放在 `config/asset-registry.yaml`，历史资产回填时用配置指定的 legacy 前缀，**不写死具体项目或日期**。

### 12.3 新增表

新增两张表，DDL 见 `reports/suggested-ddl-assets.sql`：

**`dsh_asset_registry`（统一资产台账表）**

- 粒度：一次轮次中的一个资产一条记录。
- 关键字段：`asset_id`、`project_code`、`git_repo`、`git_branch`、`git_commit`、`env_code`、`run_code`、`run_id`、`asset_type`、`asset_path`、`asset_version`、`asset_hash`、`source_asset_id`、`owner`、`status`、`ext_json`。
- 唯一键：`(run_code, asset_type, asset_path)`。
- 用途：任何报告、问题、审计都能反查“当时用的是哪个项目/分支/环境/轮次的哪份资产”。

**`dsh_case_baseline`（核心基线用例表）**

- 粒度：一个基线版本下的一条用例。
- 关键字段：`baseline_id`、`project_code`、`git_branch`、`env_code`、`run_code`、`case_id`、`case_type`、`tier`、`is_core`、`source_id`、`source_version`、`contract_json`、`drift_policy`、`baseline_version`、`enabled`、`ext_json`。
- 唯一键：`(baseline_id, project_code, case_id, baseline_version)`。
- 用途：把核心基线用例从普通用例中独立出来，支持版本化、冻结、回滚与差异审计。

### 12.4 与现有表的关系

- `dsh_test_runs.ext_json`：记录本批次的 `project_code/git_branch/env_code/run_code`。
- `dsh_test_cases.source_id`：指向 `dsh_asset_registry` 中的源资产。
- `dsh_case_baseline`：按 `case_id + baseline_version` 为核心用例提供契约快照。
- `dsh_case_results`：通过 `run_id + case_id` 关联结果，按 `tier` 聚合核心/扩展通过率。

### 12.5 回流要求

1. 每轮执行结束后，把本轮使用的全部资产写入 `dsh_asset_registry`。
2. 核心基线集合写入 `dsh_case_baseline`，并冻结 `baseline_version`。
3. 报告展示：`core_pass_rate`、`asset_registry_completeness`、`baseline_coverage`。
4. 资产缺失、哈希不一致、分支/环境未记录，均视为回流不完整。

---

## 十三、产物索引

- 本文档：`OPTIMIZATION.md`
- 机器可读资产清单：`reports/case-asset-inventory.json`
- 规格差异（P1 产出）：`reports/spec-diff.json`
- 转换报告（P1 产出）：`reports/spec-conversion-report.json`
- 质量度量（P4 产出）：`reports/quality-metrics.json`
- 资产台账快照（回流产物）：`records/<run_id>/asset-registry.json`
- 数据基线（执行期）：`records/<run_id>/data-baseline.json`
- 历史规格：`project/requirements/regression-spec.json`
- 历史执行证据：`qa-regression/20261120/*-results*.json`
- 当前 DSH 用例：`cases/{functional,api,ui}/portal.*.yaml`
- 当前报告：`reports/final-report.md`
- 建议 DDL：`reports/suggested-ddl.sql`
- 新增表 DDL：`reports/suggested-ddl-assets.sql`

一句话总结：**把历史用例当资产管，规格是源头，转换是必经之路，回流是闭环；核心基线单独冻结，全部资产记录项目/分支/环境/轮次；该复用的不重写，该新增的不遗漏，该修改的有依据；用风险分层和质量度量代替“完美覆盖”的幻想。**

---

## 十四、落地实施记录（2026-11-27）

### 14.1 已实现

| 项 | 产物 | 状态 |
|---|---|---|
| 通用配置 | `config/spec-adapter.yaml`、`config/quality.yaml`、`config/asset-registry.yaml` | 已新增 |
| 转换器 | `scripts/spec_to_cases.py` | 已实现并按 spec 转换 |
| ID 硬校验 | 转换器内置 | 缺失/重复/多余即退出码 2 |
| 流水线接入 | `run_pipeline.py --from-spec` + 资产台账阶段 | 已接入 |
| 安全入口 | `run-safe.sh` 在 token 渲染前先转换 | 已修改 |
| 资产回流 | `scripts/asset_registry.py` | 已实现，支持 `--sync-db` |
| 新表 DDL | `reports/suggested-ddl-assets.sql` | 已在本地 `dsh_test` 建表 |

### 14.2 实测结果

- 转换产出：`FUNC 18 / API 50 / UI 15`，ID 校验 `缺失 0 / 重复 0 / 多余 0`。
- 用例校验：通过；API 中 token 占位符 49 处，无明文凭证。
- UI 动作：`goto 15 / expect_visible 18 / screenshot 15 / click 5 / fill 1 / expect_text 1 / wait 1`；23 条复杂 JS 断言标记待人工补 DSL。
- 分层统计：核心基线 `FUNC 5 / API 19 / UI 4`，其余为扩展。
- 运行时变量：`contentId / skillId / uploadUrl` 3 个变量无法静态解析，已在 4 条用例上标记 `needs_runtime_binding` 与提取路径，不伪造静态值；`clientId` 从历史备份回退成功。
- 资产回流：`dsh_asset_registry` 45 行，`dsh_case_baseline` 28 行；同一 `run_id` 重复回流行数与 `run_code` 不变（幂等）。
- 原手写用例已备份到仓库外的 `../dsh-workflow-tmp/backups/cases/<timestamp>/`，未删除。
- 运行记录、批次状态、EGO 缓存、Harness 中间产物已全部迁到仓库外 `../dsh-workflow-tmp/`，仓库内不再产生 `records/` / `backups/` 目录。

### 14.3 尚未完成

- 转换后的 83 条用例**尚未做端到端执行**（需要有效 SSO token 与 EGO 登录态）。
- 23 条复杂 UI 断言（路由模拟、计数、弹窗等）仍是人工待办，未伪造通过。
- 3 个运行时绑定变量需在执行链路中从上一步响应提取（spec 已给出提取路径），当前仅标记未实现提取器。
- `qa_casepipe` provider 本身未接成插件；本方案用通用 `spec_to_cases.py` 替代了它的转换职责。

---

## 十五、临时数据隔离约束（强制）

> **过程产生的临时数据不得污染分支代码。** 本仓库只保留正式配置、脚本、用例与报告；备份、运行记录、缓存、中间产物统一放仓库外同层级目录。

### 15.1 临时根目录

配置在 `config/workflow.yaml`：

```yaml
paths:
  temp_root: ../dsh-workflow-tmp
records_dir: ../dsh-workflow-tmp/records
```

目录结构：

```text
dsh-runs/
├── dsh-workflow/          # 仓库：只放正式代码/配置/用例/报告
└── dsh-workflow-tmp/      # 同层级临时目录：不进 git
    ├── backups/cases/     # 被替换的手写用例备份
    ├── records/<run_id>/  # 执行明细、runtime、截图、批次状态
    ├── ego-browser-skill-guide.md
    └── harness-output/    # 生成引擎中间产物
```

### 15.2 已改造的写入点

| 写入点 | 改造 |
|---|---|
| 用例备份 | `spec-adapter.yaml → backup_dir` 指向仓库外临时目录 |
| 批次状态 | `dsh_common.run_id_file()` 跟随 `records_dir` 配置 |
| 执行记录 | `ensure_dirs()` 的 `records` 指向临时目录 |
| EGO 指南缓存 | `ego_client.guide_path()` 默认走 `temp_root()` |
| Harness 中间产物 | `harness_adapter._harness_output_dir()` 默认走 `temp_root()` |
| `.gitignore` | 防御性忽略 `records/` 与 `backups/`，即使误写也不进分支 |

### 15.3 强制规则

1. 新增任何临时产物，必须通过 `dsh_common.temp_root()` 或配置项解析路径，**不得在代码里写死仓库内临时目录**。
2. 历史数据迁移只搬不改语义；`records/<run_id>/` 的路径引用由配置拼接，不依赖仓库内物理目录。
3. 每次回归结束检查：仓库根下不得出现 `records/`、`backups/`、`*.tmp`、`*-scratch*` 等临时目录。
4. 资产台账登记 `asset_path` 时以仓库相对路径为准；仓库外临时产物不作为版本库资产。
