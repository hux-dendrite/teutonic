"use strict";

const path = require("path");
const { loadProcessEnv, resolveExecutable } = require("./pm2.env");

const root = __dirname;
const envFile = process.env.TEUTONIC_GPU_ENV_FILE || path.join(root, ".gpu.env");
const fileEnv = loadProcessEnv(path.resolve(root, envFile));
const python = resolveExecutable(fileEnv.TEUTONIC_PYTHON || ".venv/bin/python", root);
const pythonBin = python.includes("/") ? path.dirname(python) : "";
const commonEnv = {
  ...fileEnv,
  PYTHONUNBUFFERED: fileEnv.PYTHONUNBUFFERED || "1",
  ...(pythonBin ? { PATH: `${pythonBin}:${fileEnv.PATH || ""}` } : {}),
};

const scheduled = {
  interpreter: python,
  cwd: root,
  exec_mode: "fork",
  autorestart: false,
  log_date_format: "YYYY-MM-DD HH:mm:ss",
};

module.exports = {
  apps: [
    {
      name: "teutonic-eval-quasar",
      script: "./start_eval_quasar.sh",
      interpreter: "/bin/bash",
      cwd: root,
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 1000,
      kill_timeout: 30000,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      env: commonEnv,
    },
    {
      ...scheduled,
      name: "teutonic-model-cache-cleanup",
      script: "scripts/cleanup_model_cache.py",
      cron_restart: fileEnv.TEUTONIC_MODEL_CACHE_CLEANUP_CRON || "*/30 * * * *",
      env: commonEnv,
    },
    {
      ...scheduled,
      name: "teutonic-shard-cache-cleanup",
      script: "scripts/cleanup_shard_cache.py",
      cron_restart: fileEnv.TEUTONIC_SHARD_CACHE_CLEANUP_CRON || "*/30 * * * *",
      env: commonEnv,
    },
  ],
};
