
-- 建议表结构（脚本不会自动建表；拿到真实 DDL 后只改 workflow.yaml -> db.columns）
CREATE TABLE IF NOT EXISTS dsh_test_runs (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_id VARCHAR(64) NOT NULL UNIQUE,
  run_name VARCHAR(255),
  demand_hash VARCHAR(64),
  workspace_hash VARCHAR(64),
  start_time DATETIME,
  end_time DATETIME,
  total INT DEFAULT 0,
  passed INT DEFAULT 0,
  failed INT DEFAULT 0,
  errors INT DEFAULT 0,
  skipped INT DEFAULT 0,
  pass_rate DECIMAL(5,2),
  duration_sec DECIMAL(10,2),
  status TINYINT COMMENT '0运行中 1完成 2中断',
  report_path VARCHAR(512),
  ext_json JSON,
  KEY idx_start_time (start_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='测试批次表';

CREATE TABLE IF NOT EXISTS dsh_test_cases (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_id VARCHAR(64) NOT NULL,
  case_id VARCHAR(64) NOT NULL,
  case_type VARCHAR(16) NOT NULL COMMENT 'functional/api/ui',
  title VARCHAR(512),
  module VARCHAR(128),
  priority VARCHAR(8),
  precondition TEXT,
  related_req VARCHAR(64),
  related_func VARCHAR(64),
  steps_json JSON,
  request_json JSON,
  assert_json JSON,
  created_at DATETIME,
  ext_json JSON,
  UNIQUE KEY uk_run_case (run_id, case_id),
  KEY idx_run_id (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用例表';

CREATE TABLE IF NOT EXISTS dsh_case_results (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_id VARCHAR(64) NOT NULL,
  case_id VARCHAR(64) NOT NULL,
  case_type VARCHAR(16),
  attempt INT DEFAULT 1,
  status VARCHAR(16) COMMENT 'passed/failed/errors/skipped',
  start_time DATETIME,
  end_time DATETIME,
  cost_ms INT,
  message TEXT,
  req_content LONGTEXT,
  resp_content LONGTEXT,
  assert_detail JSON,
  screenshot_path VARCHAR(512),
  ext_json JSON,
  UNIQUE KEY uk_run_case_attempt (run_id, case_id, attempt),
  KEY idx_run_id (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用例执行结果表';

CREATE TABLE IF NOT EXISTS dsh_test_reports (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_id VARCHAR(64) NOT NULL UNIQUE,
  total INT,
  passed INT,
  failed INT,
  skipped INT,
  pass_rate DECIMAL(5,2),
  report_md_path VARCHAR(512),
  report_json LONGTEXT,
  report_html_path VARCHAR(512),
  conclusion VARCHAR(1024),
  created_at DATETIME,
  ext_json JSON
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='测试报告表';
