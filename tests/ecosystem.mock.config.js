"use strict";

const path = require("path");
const { loadProcessEnv, resolveExecutable } = require("../pm2.env");

const root = path.resolve(__dirname, "..");
const envFile = process.env.TEUTONIC_MOCK_ENV_FILE || path.join(root, ".env.local-mock");
const fileEnv = loadProcessEnv(path.resolve(root, envFile));
const python = resolveExecutable(fileEnv.TEUTONIC_PYTHON || ".venv/bin/python", root);
const pythonBin = python.includes("/") ? path.dirname(python) : "";

module.exports = {
  apps: [
    {
      name: "teutonic-mock-evaluator",
      script: "tests/mocks/mock_evaluator.py",
      interpreter: python,
      cwd: root,
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 1000,
      kill_timeout: 5000,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      env: {
        ...fileEnv,
        PYTHONUNBUFFERED: fileEnv.PYTHONUNBUFFERED || "1",
        PYTHONPATH: [root, fileEnv.PYTHONPATH].filter(Boolean).join(path.delimiter),
        ...(pythonBin ? { PATH: `${pythonBin}:${fileEnv.PATH || ""}` } : {}),
      },
    },
  ],
};
