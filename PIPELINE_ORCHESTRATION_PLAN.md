# DSH 工作流编排优化方案（v1.7）

> 目标：把 DSH 从“脚本串联”升级为“单一编排者、单一批次、单一用例范围、全阶段可审计、回流失败闭环”的测试资产流水线。
>
> 本文只描述机制与契约；环境地址、项目名、用例数量、阈值、账号凭据等均来自配置或运行时，不写入代码与模板。

---

## 0. 文档信息

| 项目 | 内容 |
| --- | --- |
| 文档版本 | v1.7 |
| 适用对象 | DSH 工作流维护者、测试平台开发者 |
| 文档状态 | v1.7 实施记录（阶段 A/B 已落地，待提交） |
| 非目标 | 不定义具体项目的业务用例、不定义测试环境地址、不替代项目级测试策略 |
| 前置假设 | 已有 `run_pipeline.py`、`spec_to_cases.py`、`run_tests.py`、`db_sync.py`、`asset_registry.py` 等基础脚本 |

### 0.1 v1.7 相对 v1.6 的修订

1. 纠正“阶段 A 已完成”的过度表述，明确“已实现但未提交/未最终复跑”。
2. 统一 `spec_hash`、`spec_bundle_hash`、manifest 路径命名。
3. 补 `scope-manifest` 的 `schema_version`、生成者、消费者、失败策略、幂等语义。
4. token 改为“仓库只留占位符，执行读临时渲染副本”。
5. 补全退出码，包括 EGO 的 `3` 与位掩码语义，明确 `release_ready` 判定。
6. 把并发锁、幂等从阶段 D 提升到阶段 0。
7. 补编排自身的单元/集成/负向/幂等测试。
8. 补报告 JSON schema、审计快照格式、安全要求。

---

## 1. 背景与基线

当前 DSH 已具备：规格转换、FUNC/API/UI 用例生成、ID 完整性校验、执行、报告、MySQL 回流、资产台账、核心基线等能力。

在一次全链路调用中暴露出编排层问题：

| 序号 | 问题 | 影响 |
| --- | --- | --- |
| 1 | 入口双轨：`run-safe.sh` 与 `run_pipeline.py` 都能触发生成 | 生成两次、token 被覆盖、配置被绕过 |
| 2 | run_id 多写：转换器、流水线、用例回写各自取 run_id | 同一批次多个 run_id，报告不可追溯 |
| 3 | 用例范围未贯穿：执行按 spec 筛选，校验/覆盖度/报告/台账仍按目录统计 | 报告与执行范围不一致 |
| 4 | 缺少硬门禁：转换失败或筛选为空仍继续 | 生成“正常”的空报告 |
| 5 | 回流失败不影响退出码 | 全链路“回流成功”没有被保证 |
| 6 | 报告依赖隐式 JUnit 产物 | 可能读到上一轮结果 |
| 7 | 审计快照晚于生成与回写 | 无法证明用例何时被改 |
| 8 | 人工上下文用例被当自动用例执行 | 产生假失败，污染失败率 |
| 9 | 并发写入无锁 | 流程基于中间态运行 |

### 1.1 基线与最新复跑

人工上下文修复前的问题规模基线：

```text
总用例：83
通过：51
失败：5
跳过：27
```

v1.7 编排改造后的最新复跑：

```text
总用例：83
通过：75
失败：2
跳过：6
退出码：9（测试失败 + 人工待办）
```

剩余 2 条失败为 UI 无障碍语义缺失（`role="region"` / `aria-label`），属于被测系统问题，不是流水线缺陷。6 条跳过均已在 manifest 与报告中登记原因。

---

## 2. 优化原则

| 原则 | 说明 |
| --- | --- |
| 单一编排者 | 只有 `run_pipeline.py` 负责阶段编排；shell 只注入参数与凭据。 |
| 单一 run_id | 流水线开头初始化一次，所有阶段共享；禁止各阶段自行创建。 |
| 单一用例范围 | 生成后产出 `scope-manifest.json`，所有阶段只读该清单。 |
| 配置驱动 | 环境、阈值、用例选择、EGO 参数、超时均来自配置/运行时。 |
| 不污染仓库 | 过程数据、token 渲染、run_id 注入不写入仓库内正式用例。 |
| 失败闭环 | 转换、范围、执行、回流均影响最终退出码与报告结论。 |
| 可审计 | 每个阶段记录输入清单、产物路径、版本/哈希与耗时。 |
| 可重跑 | 同一 run_id 重跑幂等，不污染历史批次。 |

---

## 3. 目标编排流程

```text
[入口层] run-safe.sh / 外部调度
    │ 只传：DSH_TOKEN_FILE、环境 URL、配置覆盖
    ▼
[run_pipeline.py] 唯一编排者
    │
    ├─ 0  初始化 run_id + 获取执行锁
    ├─ 1  spec 转换 + token 临时渲染
    ├─ 2  生成 scope-manifest.json
    ├─ 3  硬门禁：ID 校验 + 用例数 + manifest 一致性
    ├─ 4  格式校验（仅 manifest 内用例）
    ├─ 5  审计快照：生成前 / 执行前
    ├─ 6  执行测试：FUNC / API / UI
    ├─ 7  审计收尾：执行后快照
    ├─ 8  报告：只消费当前 run_id 的产物与 manifest
    ├─ 9  数据回流：测试结果 + 报告
    ├─ 10 资产回流：资产台账 + 核心基线
    └─ 11 汇总退出码：测试 / 流程 / 回流 / 人工待办
```

### 3.1 当前阶段与目标阶段映射

| 当前阶段 | 目标阶段 | 说明 |
| --- | --- | --- |
| `run-safe.sh` 内转换 | 阶段 1 | 生成职责收回 pipeline |
| `run_pipeline.py` ①-③ | 阶段 1 | 支持 spec/harness 两种模式 |
| 格式校验 | 阶段 4 | 改为只校验 manifest |
| UI 覆盖度审计 | 阶段 5 | 改为只统计 manifest |
| ④⑤ 执行 | 阶段 6 | 增加状态分类 |
| ⑥ 报告 | 阶段 8 | 增加范围与回流结论 |
| ⑦ MySQL 回流 | 阶段 9 | 回流状态进入退出码 |
| ⑧ 资产回流 | 阶段 10 | 与 9 统一失败策略 |

---

## 4. 核心契约

### 4.1 run_id 生命周期

唯一合法来源：

1. 外部显式设置 `DSH_RUN_ID` 时，默认复用。
2. 未设置时，由流水线生成 `run-<timestamp>`。
3. 仅 `--new-run` 强制新建。

约束：

- 子进程通过环境变量继承同一个 `DSH_RUN_ID`。
- `current-run.json` 只作为“当前批次指针”，不作为唯一来源。
- 不再通过 `inject_run_id_into_cases()` 回写仓库内 YAML。
- run_id 进入 manifest、records、报告、DB，不进入正式用例文件。

### 4.2 `scope-manifest.json`

路径固定为：

```text
records/<run_id>/scope-manifest.json
```

消费者通过环境变量读取：

```text
DSH_SCOPE_MANIFEST=records/<run_id>/scope-manifest.json
```

缺失、不可读、`schema_version` 不兼容时，所有阶段直接失败，不得退回目录 glob。

示例：

```json
{
  "schema_version": "1.0",
  "run_id": "run-xxxx",
  "generated_at": "2026-01-01T00:00:00",
  "generator": "run_pipeline.py",
  "spec": {
    "file": "project/requirements/regression-spec.json",
    "hash": "<sha1>",
    "version": "r1.0"
  },
  "case_files": {
    "functional": ["cases/functional/xxx.yaml"],
    "api": ["cases/api/xxx.yaml"],
    "ui": ["cases/ui/xxx.yaml"]
  },
  "case_ids": {
    "functional": ["FUNC-001"],
    "api": ["API-001"],
    "ui": ["UI-001"]
  },
  "manual_case_ids": ["UI-001"],
  "runtime_binding_case_ids": ["API-015"],
  "env": {
    "api_base_url": "<masked>",
    "ui_base_url": "<masked>"
  },
  "manifest_hash": "<sha1 of canonical json>"
}
```

规则：

- 用例级字段名为 `spec_bundle_hash`；报告级字段名为 `spec_hash`；两者必须相等。
- `example.*.yaml` 不进入 manifest，除非显式配置包含。
- 一轮只允许一个 active spec；多项目通过独立工作区或独立 run 隔离。
- `manifest_hash` 对规范化 JSON（键排序、无空白差异）计算。
- 任何阶段发现 manifest 与 YAML 内容 hash 不一致，直接失败。

### 4.3 幂等语义

- 同一 `run_id` 重跑时复用同一 records 目录。
- 报告、回流行数、资产台账行数保持不变或按 upsert 更新。
- 不生成 `run-...-2` 之类隐式新批次。
- 需要新批次时显式 `--new-run`。

### 4.4 执行锁

阶段 0 获取：

```text
records/.pipeline.lock
```

内容：

```json
{
  "pid": 12345,
  "run_id": "run-xxxx",
  "started_at": "2026-01-01T00:00:00",
  "host": "hostname"
}
```

规则：

- 锁存在且进程存活：拒绝启动，退出码 2。
- 锁存在但进程已退出：记录告警并清理 stale lock。
- 正常/异常退出均通过 finally 释放锁。

---

## 5. Token 处理

### 5.1 目标

仓库内用例文件永远只包含占位符：

```text
Authorization: Bearer __DSH_API_TOKEN__
```

执行时渲染到临时副本：

```text
records/<run_id>/rendered/cases/api.rendered.yaml
```

执行器通过 `DSH_API_CASE_FILE` 或配置指向该副本。

### 5.2 流程

```text
读取 DSH_TOKEN_FILE
  → 校验文件存在、权限、非空
  → 复制正式用例到 records/<run_id>/rendered/
  → 在副本中替换占位符
  → 执行
  → 结束钩子删除副本或回填占位符
```

### 5.3 安全要求

- token 文件权限建议 `600`。
- 日志不得输出完整 Authorization、cookie、query 中的敏感值。
- `mask_sensitive()` 需覆盖 header 字符串、query string、嵌套 dict/list。
- 提交前执行 secret scan；发现真实 token 直接阻断。
- 报告中只记录 `token_rendered: true/false` 与占位符数量，不记录 token 值。
- 临时副本在批次结束后删除，或写入权限受限的临时目录。

---

## 6. 退出码与发布判定

### 6.1 退出码位掩码

| 位 | 值 | 含义 |
| --- | --- | --- |
| bit0 | 1 | 存在 failed 用例 |
| bit1 | 2 | 流程异常/门禁失败 |
| bit2 | 4 | DB 或资产回流失败 |
| bit3 | 8 | 存在未清人工待办 |

保留兼容：

| 值 | 含义 |
| --- | --- |
| 0 | 全部通过且回流成功、无人工待办 |
| 1 | 仅有测试失败 |
| 2 | 流程/门禁失败 |
| 3 | EGO CDP 连接失败（兼容旧语义，不参与位掩码） |
| 4 | 测试通过但回流失败 |
| 5 | 测试失败且回流失败 |
| 8 | 测试通过、回流成功，但存在人工待办 |
| 9 | 测试失败且存在人工待办 |

实际组合按位 OR；`3` 单独保留，避免与 `1|2` 混淆时需在实现中显式判断。

### 6.2 `release_ready` 判定

```text
release_ready = (failed == 0)
                and (errors == 0)
                and (backflow_ok == true)
                and (scope_integrity == true)
                and (manual_pending == 0)
```

`skipped-runtime` 不计入 failed，但必须单列并进入报告；是否阻断发布由配置决定：

```yaml
pipeline:
  release:
    block_on_runtime_skip: false
    block_on_manual_pending: true
```

---

## 7. 阶段详细设计

### 7.1 阶段 0：初始化

输入：`DSH_RUN_ID`、`--new-run`、配置。
输出：run_id、records 目录、执行锁。
失败：锁冲突、目录不可写、配置缺失 → exit 2。

### 7.2 阶段 1：规格转换与 token 渲染

输入：spec、`DSH_TOKEN_FILE`、run_id。
输出：三类用例、`spec-conversion-report.json`、渲染副本。
失败：spec 缺失、ID 校验失败、token 文件无效 → exit 2。

要求：

- 转换器只接收 run_id，不读取 token。
- 渲染器只操作临时副本。
- 报告中写入 `spec_hash`，用例中写入 `spec_bundle_hash`。

### 7.3 阶段 2：生成 manifest

输入：转换报告、选中用例文件、环境配置。
输出：`records/<run_id>/scope-manifest.json`。
失败：无法生成或 hash 计算失败 → exit 2。

### 7.4 阶段 3：硬门禁

| 检查 | 失败动作 |
| --- | --- |
| `spec-conversion-report.id_check.ok == true` | exit 2 |
| manifest 三类用例数均大于 0 | exit 2 |
| manifest case_id 数与转换报告 counts 一致 | exit 2 |
| manifest hash 可复算且一致 | exit 2 |
| 环境地址可解析且非空 | exit 2 |

### 7.5 阶段 4：格式校验

- 只校验 manifest 内文件。
- `example.*.yaml` 不参与。
- 校验失败 → exit 2。

### 7.6 阶段 5：审计快照

四个快照点：

1. 生成前：用例目录原始状态。
2. 生成后：转换产物。
3. 执行前：manifest 与 hash。
4. 执行后：变更与最终状态。

存储：

```text
records/<run_id>/audit/snapshot-<stage>.json
```

格式：

```json
{
  "stage": "pre_execute",
  "run_id": "run-xxxx",
  "created_at": "2026-01-01T00:00:00",
  "manifest_hash": "<sha1>",
  "files": {
    "cases/api/xxx.yaml": "<sha1>"
  }
}
```

### 7.7 阶段 6：执行

只消费 manifest。

状态分类：

| 状态 | 含义 | 计入失败率 | 默认阻断发布 |
| --- | --- | --- | --- |
| passed | 自动执行通过 | 否 | 否 |
| failed | 自动执行失败 | 是 | 是 |
| skipped-runtime | 运行时变量未就绪 | 否 | 可配置 |
| skipped-manual | 需人工上下文/route mock | 否 | 是 |
| error | 执行器异常 | 是 | 是 |

### 7.8 阶段 7：审计收尾

- 对比执行前后 manifest 与用例文件 hash。
- 生成 `case_changes`、`mid_run_changes`、`vs_previous_changes`。
- 变更只允许出现在预期范围内，否则报告告警。

### 7.9 阶段 8：报告

报告必须包含：

```json
{
  "run_id": "run-xxxx",
  "manifest_hash": "<sha1>",
  "spec_hash": "<sha1>",
  "status_counts": {
    "passed": 0,
    "failed": 0,
    "skipped_runtime": 0,
    "skipped_manual": 0,
    "error": 0
  },
  "skipped_reasons": {},
  "test_result": "passed|failed|error",
  "backflow": {
    "db": "ok|failed|skipped",
    "asset": "ok|failed|skipped"
  },
  "scope_integrity": true,
  "manual_pending": 0,
  "release_ready": false
}
```

Markdown 报告必须单列：

- 范围
- 自动执行结论
- 回流结论
- 人工待办
- 运行时绑定待就绪
- 是否可发布

### 7.10 阶段 9/10：回流

- 阶段 9：`db_sync.py`。
- 阶段 10：`asset_registry.py --sync-db`。
- 任一失败：记录 `backflow` 状态，设置 bit2，不静默。
- `--dry-db`：只演练，不写库，但本地产物仍生成。
- 回流幂等：同一 run_id 重跑不重复插入。

### 7.11 阶段 11：汇总

- 汇总测试、门禁、回流、人工待办。
- 写 `summary.json`。
- 按位掩码返回退出码。
- 输出最终结论与报告路径。

---

## 8. 配置变更

### 8.1 `config/workflow.yaml`

```yaml
cases:
  selection:
    enabled: true
    spec_report: reports/spec-conversion-report.json
    manifest_template: records/{run_id}/scope-manifest.json
    include_globs: []
    exclude_globs: []

pipeline:
  init_run_id:
    force: false
  lock:
    enabled: true
    path: records/.pipeline.lock
    stale_after_sec: 7200
  gate:
    fail_on_empty_cases: true
    fail_on_count_mismatch: true
    fail_on_spec_hash_missing: true
    fail_on_manifest_mismatch: true
  release:
    block_on_runtime_skip: false
    block_on_manual_pending: true
  exit_code:
    test_failed: 1
    pipeline_failed: 2
    backflow_failed: 4
    manual_pending: 8
```

### 8.2 `config/providers.yaml`

```yaml
ego_browser:
  nodejs_timeout_sec: 30
  assert_timeout_sec: 8
  task_space_id: ""
  task_space_name_prefix: dsh
```

### 8.3 `config/spec-adapter.yaml`

```yaml
spec_adapter:
  ui_selector:
    role_aliases: {}
    name_attr: {}
    nth_suffix: ""
  ui_routes: {}
  ui_manual_patterns:
    - browser.newContext
    - storageState
    - page.route
    - page.unroute
    - waitForEvent
```

---

## 9. 安全要求

- token 文件权限检查，建议 `600`。
- 日志脱敏覆盖：Authorization、Cookie、query 敏感参数、嵌套对象。
- 禁止在报告中输出完整请求头。
- secret scan 作为提交前 gate。
- 临时渲染副本生命周期不超过一个 run。
- 失败截图、runtime JSON 不得包含 token。

---

## 10. 测试设计

### 10.1 单元测试

- manifest 生成与 hash 复算。
- gate 判定：空用例、数量不一致、hash 不一致。
- 退出码位掩码组合。
- token 渲染与回填。
- 状态分类统计。

### 10.2 集成测试

- 完整链路：转换 → manifest → 执行 → 报告 → 回流。
- 同一 run_id 重跑幂等。
- `--dry-db` 不写库。
- 回流失败时退出码与报告结论。

### 10.3 负向测试

- 转换 ID 校验失败。
- manifest 缺失/损坏。
- token 文件缺失/为空/权限错误。
- 并发执行锁冲突。
- 报告 JUnit 属于上一轮。

### 10.4 回归测试

- 旧报告格式可读。
- 旧 `run-safe.sh` 用法兼容或给出明确迁移提示。
- 既有 DB 表结构不被破坏。

---

## 11. 迁移步骤与状态

### 阶段 A：止血（已实现，待提交）

| 项目 | 状态 |
| --- | --- |
| 隔离跨项目陈旧用例 | 已实现 |
| 执行器按 `spec_bundle_hash` 筛选 | 已实现 |
| query/body/路径变量修复 | 已实现 |
| API 运行时绑定跳过/捕获 | 已实现 |
| UI 选择器/路由修复 | 已实现 |
| EGO 超时与 TaskSpace 配置 | 已实现 |
| 人工上下文用例标记与跳过 | 已实现，最终复跑通过 |

### 阶段 B：编排收口（已实现，待提交）

- pipeline 统一初始化 run_id，环境变量优先。
- token 渲染移入 pipeline，并写入运行期临时副本。
- 生成 `scope-manifest.json`。
- 校验、覆盖度、审计、报告、资产台账改读 manifest。
- 硬门禁：报告、hash、数量、文件、环境地址。
- 执行锁与 stale lock 清理。
- 同一 run_id 重跑复用 records 目录。

### 阶段 C：结论闭环（部分实现）

- `summary.json` 已输出状态计数、回流状态与 `release_ready`。
- 退出码已支持位掩码：测试失败、流程失败、回流失败、人工待办。
- 报告仍保留原有 Markdown/JSON 结构；五类状态在 `summary.json` 中单列。
- `release_ready` 已实现，默认人工待办阻断发布。

### 阶段 D：稳定性（待做）

- manifest 与报告签名/哈希。
- 跨批次 diff 报告。
- 历史报告迁移策略。
- 指标与告警。

---

## 12. 验收标准

1. 只跑 manifest 内用例；目录额外文件不进入执行、报告、覆盖度、台账。
2. 同一 run_id 重跑，报告与回流行数幂等。
3. token 不出现在仓库用例文件中，只存在于临时渲染副本。
4. 转换 ID 校验失败时 exit 2，不生成正常报告。
5. 空用例集时 exit 2。
6. 人工上下文用例只出现在 manual 区，不进入失败率分母。
7. 运行时变量未就绪用例单独统计，不发出脏请求。
8. DB 或资产回流失败时退出码体现 bit2。
9. 报告中的 run_id、spec_hash、manifest_hash、case_id 集合完全一致。
10. 全流程不再回写仓库内用例文件。
11. 并发执行被锁拒绝，stale lock 可恢复。
12. `--dry-db` 不写库但本地产物完整。

---

## 13. 指标

建议采集：

| 指标 | 说明 |
| --- | --- |
| `scope_integrity` | manifest 与 YAML 是否一致 |
| `stale_case_ratio` | 被筛选掉的非本轮用例比例 |
| `manual_pending` | 人工待办数 |
| `runtime_skip_ratio` | 运行时变量未就绪比例 |
| `backflow_success_rate` | 回流成功率 |
| `false_failure_count` | 因编排/用例缺陷导致的假失败数 |
| `run_duration_sec` | 全链路耗时 |

---

## 14. 风险与回滚

| 风险 | 缓解 |
| --- | --- |
| 改造涉及多个脚本 | 分阶段提交，每阶段可独立回滚 |
| manifest 与 YAML 不一致 | 生成后 hash 校验与硬门禁 |
| token 临时副本泄漏 | 权限控制、结束删除、secret scan |
| EGO taskSpace 复用错误 | `task_space_id` 留空保持新建 |
| 历史报告不兼容 | 新字段向后兼容，旧报告只读 |
| 并发执行 | 阶段 0 执行锁 |
| 回滚后旧脚本仍写仓库用例 | 保留兼容开关，默认关闭旧写入 |

回滚方式：Git 回退对应阶段提交；临时产物保留在仓库外临时目录，不依赖仓库内 records。

---

## 15. 待决策问题

1. 一轮是否允许同时存在多个 active spec？
2. `skipped-runtime` 是否阻断发布，默认值是否按项目配置？
3. 人工待办是否必须清零才允许 `release_ready`？
4. 退出码 `3` 是否保留，还是统一并入位掩码？
5. 历史无 manifest 的报告是否允许只读展示？
6. token 临时副本是执行后删除，还是保留到批次审计结束？

---

## 16. 当前实现状态

| 项目 | 状态 |
| --- | --- |
| 跨项目用例隔离 | 已实现 |
| spec_hash 筛选 | 已实现 |
| query/body/路径变量修复 | 已实现 |
| API 运行时绑定跳过/捕获 | 已实现 |
| UI 选择器/路由修复 | 已实现 |
| EGO 超时与 TaskSpace 配置 | 已实现 |
| 人工上下文标记与跳过 | 已实现，最终复跑通过 |
| scope-manifest | 已实现 |
| run_id 单一生命周期 | 已实现 |
| token 渲染移入 pipeline | 已实现，仓库只保留占位符 |
| 全阶段 manifest 化 | 已实现（校验、覆盖度、审计、资产基线） |
| 回流失败影响退出码 | 已实现 |
| 报告五类状态 | 部分实现：summary.json 已单列，Markdown/JSON 报告沿用原结构 |
| 执行锁与幂等 | 已实现锁与同 run_id 复用；跨批次 diff 仍待完善 |
| 编排单元测试 | 已实现基础覆盖 |
| 跨批次 diff / 指标告警 | 待实现 |
| 编排自身测试 | 待实现 |

> 本方案文档应随阶段 B/C 的实现同步更新状态与证据，不单独散落在聊天记录中。
