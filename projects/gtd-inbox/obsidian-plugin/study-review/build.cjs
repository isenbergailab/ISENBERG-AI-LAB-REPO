"use strict";
const fs = require("node:fs");
const path = require("node:path");

const root = __dirname;
const core = fs.readFileSync(path.join(root, "core.js"), "utf8");
const source = fs.readFileSync(path.join(root, "main.src.js"), "utf8");
const importLine = 'const gre = require("./core.js");';
if (!source.includes(importLine)) throw new Error("STUDY core import missing");
const embedded = `const gre = (() => {\nconst module = {exports:{}};\n${core}\nreturn module.exports;\n})();`;
const output = source.replace(importLine, () => embedded);
fs.writeFileSync(path.join(root, "main.js"), output);
