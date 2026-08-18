"use strict";

const fs = require("fs");
const path = require("path");

function parseValue(rawValue, filePath, lineNumber) {
  const value = rawValue.trim();
  if (!value) return "";

  if (value.startsWith('"')) {
    try {
      return JSON.parse(value);
    } catch (error) {
      throw new Error(`${filePath}:${lineNumber}: invalid double-quoted value: ${error.message}`);
    }
  }

  if (value.startsWith("'")) {
    if (!value.endsWith("'") || value.length < 2) {
      throw new Error(`${filePath}:${lineNumber}: unterminated single-quoted value`);
    }
    return value.slice(1, -1);
  }

  const comment = value.search(/\s+#/);
  return (comment === -1 ? value : value.slice(0, comment)).trimEnd();
}

function loadEnvFile(filePath) {
  if (!fs.existsSync(filePath)) {
    throw new Error(`missing environment file: ${filePath}`);
  }

  const values = {};
  const lines = fs.readFileSync(filePath, "utf8").replace(/^\uFEFF/, "").split(/\r?\n/);
  lines.forEach((sourceLine, index) => {
    let line = sourceLine.trim();
    if (!line || line.startsWith("#")) return;
    if (line.startsWith("export ")) line = line.slice(7).trimStart();

    const match = line.match(/^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!match) {
      throw new Error(`${filePath}:${index + 1}: expected KEY=value`);
    }
    values[match[1]] = parseValue(match[2], filePath, index + 1);
  });
  return values;
}

function loadProcessEnv(filePath) {
  return { ...process.env, ...loadEnvFile(filePath) };
}

function resolveExecutable(value, cwd) {
  if (!value.includes("/") && !value.includes(path.sep)) return value;
  return path.isAbsolute(value) ? value : path.resolve(cwd, value);
}

module.exports = { loadEnvFile, loadProcessEnv, resolveExecutable };
