import { readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";

for (const directory of ["web/js", "agentgui/web/js", "lingchat/shared", "tests/js"]) {
  for (const file of readdirSync(directory).filter(file => file.endsWith(".js"))) {
    const result = spawnSync(process.execPath, ["--check", `${directory}/${file}`], { stdio: "inherit" });
    if (result.status !== 0) process.exit(result.status || 1);
  }
}
