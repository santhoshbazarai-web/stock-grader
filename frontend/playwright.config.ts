// End-to-end tests against a running stack seeded with the synthetic demo stocks:
//   backend:  python -m app.devtools.demo && uvicorn app.main:app --port 8000
//   frontend: API_URL=http://localhost:8000 npm run dev
//   run:      E2E_PASSWORD=<APP_PASSWORD> npm run e2e
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  // `next dev` compiles each route on first visit, which can take several seconds.
  expect: { timeout: 20_000 },
  retries: 0,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    launchOptions: process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : undefined,
    viewport: { width: 1360, height: 900 },
  },
});
