import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, resolve } from "node:path";

const port = Number(process.argv[2]);
const logPath = resolve(process.argv[3]);

mkdirSync(dirname(logPath), { recursive: true });
writeFileSync(logPath, "", "utf8");

const server = createServer((request, response) => {
  appendFileSync(logPath, `${request.method} ${request.url}\n`, "utf8");
  response.writeHead(503, { "Content-Type": "application/json" });
  response.end(JSON.stringify({ detail: "E2E sentinel: request escaped test isolation." }));
});

server.listen(port, "127.0.0.1");

function shutdown() {
  server.closeAllConnections();
  server.close(() => process.exit(0));
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
