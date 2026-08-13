import { defineConfig } from '@playwright/test';
import dotenv from 'dotenv';
dotenv.config();
export default defineConfig({
  testDir: './tests', timeout: 120_000, expect: { timeout: 10_000 },
  fullyParallel: false, workers: 1,
  reporter: [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]],
  use: {
    baseURL: process.env.MESFLOW_BASE_URL,
    viewport: { width: 1920, height: 1080 },
    trace: process.env.PLAYWRIGHT_TRACE === 'off' ? 'off' : 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: process.env.PLAYWRIGHT_VIDEO === 'false' ? 'off' : 'retain-on-failure'
  }
});
