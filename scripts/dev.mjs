// Runs the Python engine and `next dev` together; Ctrl-C stops both.
import { spawn } from "node:child_process";

const python = process.env.PYTHON ?? "python3";
const enginePort = process.env.ENGINE_PORT ?? "7070";

const children = [
  spawn(python, ["engine/server.py", "--port", enginePort], { stdio: "inherit" }),
  spawn("npx", ["next", "dev"], {
    stdio: "inherit",
    env: { ...process.env, ENGINE_URL: `http://127.0.0.1:${enginePort}` },
  }),
];

let stopping = false;
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const child of children) child.kill("SIGTERM");
  process.exit(code);
}

for (const child of children) {
  child.on("exit", (code) => stop(code ?? 0));
}
process.on("SIGINT", () => stop(0));
process.on("SIGTERM", () => stop(0));
