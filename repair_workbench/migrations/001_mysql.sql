CREATE TABLE IF NOT EXISTS backend_schema (version INT PRIMARY KEY, applied_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS repair_tasks (
 id CHAR(32) PRIMARY KEY, idempotency_key VARCHAR(128) COLLATE utf8mb4_bin NOT NULL UNIQUE,
 request_digest CHAR(64) NOT NULL, request JSON NOT NULL,
 state VARCHAR(24) NOT NULL DEFAULT 'queued', phase VARCHAR(32) NOT NULL DEFAULT 'queued',
 generation BIGINT NOT NULL DEFAULT 0, lease_owner CHAR(32), lease_until DATETIME(6),
 calls_reserved INT NOT NULL DEFAULT 0, candidates_reserved INT NOT NULL DEFAULT 0,
 agent_deadline DATETIME(6), result JSON, error_type VARCHAR(100),
 created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 INDEX task_claim (state,lease_until,created_at), INDEX task_created(created_at)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS task_attempts (
 task_id CHAR(32) NOT NULL, generation BIGINT NOT NULL, owner CHAR(32) NOT NULL,
 state VARCHAR(24) NOT NULL, started_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 heartbeat_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), finished_at DATETIME(6),
 PRIMARY KEY(task_id,generation), FOREIGN KEY(task_id) REFERENCES repair_tasks(id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS model_calls (
 task_id CHAR(32) NOT NULL, number INT NOT NULL, generation BIGINT NOT NULL,
 state VARCHAR(24) NOT NULL, usage_json JSON, reason VARCHAR(100),
 created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 PRIMARY KEY(task_id,number), FOREIGN KEY(task_id) REFERENCES repair_tasks(id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS repair_candidates (
 task_id CHAR(32) NOT NULL, number INT NOT NULL, generation BIGINT NOT NULL,
 state VARCHAR(24) NOT NULL, artifact_path VARCHAR(2048), artifact_sha CHAR(64), result JSON,
 PRIMARY KEY(task_id,number), FOREIGN KEY(task_id) REFERENCES repair_tasks(id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS stage_artifacts (
 task_id CHAR(32) NOT NULL, generation BIGINT NOT NULL, kind VARCHAR(32) NOT NULL,
 artifact_path VARCHAR(2048) NOT NULL, artifact_sha CHAR(64) NOT NULL, metadata JSON NOT NULL,
 PRIMARY KEY(task_id,generation,kind), FOREIGN KEY(task_id) REFERENCES repair_tasks(id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS test_runs (
 id CHAR(32) PRIMARY KEY, task_id CHAR(32) NOT NULL, generation BIGINT NOT NULL,
 candidate_number INT, kind VARCHAR(32) NOT NULL, artifact_path VARCHAR(2048) NOT NULL,
 artifact_sha CHAR(64) NOT NULL, result JSON NOT NULL,
 FOREIGN KEY(task_id) REFERENCES repair_tasks(id), INDEX test_task(task_id,generation)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS task_events (
 sequence BIGINT PRIMARY KEY AUTO_INCREMENT, task_id CHAR(32) NOT NULL, generation BIGINT NOT NULL,
 created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), payload JSON NOT NULL,
 FOREIGN KEY(task_id) REFERENCES repair_tasks(id), INDEX event_task(task_id,sequence)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS execution_resources (
 task_id CHAR(32) NOT NULL, generation BIGINT NOT NULL, container_name VARCHAR(64) NOT NULL,
 state VARCHAR(24) NOT NULL, PRIMARY KEY(task_id,container_name),
 FOREIGN KEY(task_id) REFERENCES repair_tasks(id)
) ENGINE=InnoDB;
INSERT IGNORE INTO backend_schema(version) VALUES(1);
