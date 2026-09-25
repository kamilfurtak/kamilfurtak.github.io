import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  use: {
    baseURL: process.env["DEMO_URL"] || "http://127.0.0.1:4300",
    viewport: { width: 1360, height: 900 },
    trace: "retain-on-failure",
  },
  webServer: process.env["DEMO_URL"]
    ? undefined
    : {
        command: "npm start -- --port 4300",
        url: "http://127.0.0.1:4300",
        reuseExistingServer: false,
        timeout: 120_000,
      },
});
