// The e2e Next.js server rewrites /api/agent/* here instead of to the development backend.
// A sentinel server on this port records anything that escapes test interception.
export const E2E_SENTINEL_PORT = 8198;
export const E2E_SENTINEL_LOG = "test-results/e2e-sentinel-requests.log";
export const E2E_PROCESS_FILE = "test-results/e2e-processes.json";
