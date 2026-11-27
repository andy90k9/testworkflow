-- 通用资产台账与核心基线用例表（脚本不会自动建表）
-- 说明：所有项目/分支/环境/轮次编码均由配置或运行时写入，DDL 不含任何项目专属值。
-- 与 suggested-ddl.sql 配套使用；拿到真实表结构后只改 workflow.yaml -> db.columns。

CREATE TABLE IF NOT EXISTS dsh_asset_registry (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  asset_id VARCHAR(128) NOT NULL COMMENT '资产稳定标识，由项目+仓库+路径+版本计算',
  project_code VARCHAR(64) NOT NULL COMMENT '项目编码，来自配置',
  git_repo VARCHAR(255) COMMENT '代码仓库地址，来自配置或 git remote',
  git_branch VARCHAR(128) NOT NULL COMMENT '分支，运行时读取',
  git_commit VARCHAR(64) COMMENT '提交号，运行时读取',
  env_code VARCHAR(64) NOT NULL COMMENT '测试环境编码，来自配置',
  run_code VARCHAR(64) NOT NULL COMMENT '测试轮次编码，按配置模板生成',
  run_id VARCHAR(64) COMMENT '关联 dsh_test_runs.run_id',
  asset_type VARCHAR(32) NOT NULL COMMENT 'spec/case/script/report/screenshot/ddl/baseline/record',
  asset_name VARCHAR(255),
  asset_path VARCHAR(512) NOT NULL COMMENT '资产路径或仓库内路径',
  asset_version VARCHAR(64) COMMENT '资产版本',
  asset_hash VARCHAR(64) COMMENT '内容哈希，用于一致性校验',
  source_asset_id VARCHAR(128) COMMENT '派生来源资产 ID',
  owner VARCHAR(64),
  status VARCHAR(16) DEFAULT 'active' COMMENT 'active/deprecated/archived',
  created_at DATETIME,
  ext_json JSON COMMENT '扩展字段：spec_hash、转换器版本、上下文等',
  UNIQUE KEY uk_asset_run (run_code, asset_type, asset_path),
  KEY idx_asset_identity (project_code, git_branch, asset_type, asset_path),
  KEY idx_run_id (run_id),
  KEY idx_run_code (run_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='通用资产台账表：记录项目/分支/环境/轮次维度的全部资产';

CREATE TABLE IF NOT EXISTS dsh_case_baseline (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  baseline_id VARCHAR(64) NOT NULL COMMENT '基线集合标识，按配置生成',
  project_code VARCHAR(64) NOT NULL COMMENT '项目编码，来自配置',
  git_repo VARCHAR(255),
  git_branch VARCHAR(128) NOT NULL COMMENT '分支，运行时读取',
  env_code VARCHAR(64) COMMENT '测试环境编码，来自配置',
  run_code VARCHAR(64) COMMENT '首次冻结该基线的轮次编码',
  case_id VARCHAR(64) NOT NULL,
  case_type VARCHAR(16) NOT NULL COMMENT 'functional/api/ui',
  tier VARCHAR(16) NOT NULL DEFAULT 'extension' COMMENT 'core/extension/temporary',
  is_core TINYINT DEFAULT 0 COMMENT '是否核心基线用例',
  source_id VARCHAR(64) COMMENT '历史 spec ID 或 add_new',
  source_version VARCHAR(64),
  contract_json JSON COMMENT '锁定的断言契约',
  drift_policy VARCHAR(32) DEFAULT 'snapshot' COMMENT 'exact/range/snapshot，由配置决定默认值',
  baseline_version VARCHAR(64) NOT NULL COMMENT '基线版本号，变更需升级',
  enabled TINYINT DEFAULT 1,
  created_at DATETIME,
  ext_json JSON COMMENT '扩展字段：评审人、变更原因、关联轮次等',
  UNIQUE KEY uk_baseline_case (baseline_id, project_code, case_id, baseline_version),
  KEY idx_tier (tier),
  KEY idx_core (is_core),
  KEY idx_run_code (run_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='核心基线用例表：分层、版本化、可冻结可回滚';
