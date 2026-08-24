"use strict";

const path = require("path");
const { loadProcessEnv, resolveExecutable } = require("../pm2.env");

const root = path.resolve(__dirname, "..");
const envFile = process.env.TEUTONIC_ENV_FILE || path.join(root, ".env");
const fileEnv = loadProcessEnv(path.resolve(root, envFile));
const python = resolveExecutable(fileEnv.TEUTONIC_PYTHON || ".venv/bin/python", root);
const pythonBin = python.includes("/") ? path.dirname(python) : "";

module.exports = {
  apps: [
    {
      name: "teutonic-testnet-load",
      script: "tests/load/testnet_load.py",
      args: ["--start", "30", "--count", "100", "--concurrency", "10"],
      interpreter: python,
      cwd: root,
      exec_mode: "fork",
      instances: 1,
      autorestart: false,
      max_restarts: 0,
      kill_timeout: 30000,
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
