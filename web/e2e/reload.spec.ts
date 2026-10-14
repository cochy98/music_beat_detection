import { test, expect } from "@playwright/test";
import { defaults } from "../../shared/config";

test("a queued job survives reload; its displayed result does not survive the next reload", async ({ page }) => {
  const id = "00000000-0000-4000-8000-000000000001";
  let status = "queued";
  const job = () => ({ id, filename: "test.wav", status, stage: status === "completed" ? "Elaborazione completata" : "In coda", progress: status === "completed" ? 100 : 0, config: defaults, attempts: 1, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url()).pathname;
    if (url.endsWith("/events")) return route.fulfill({ contentType: "text/event-stream", body: `event: job\ndata: ${JSON.stringify(job())}\n\n` });
    let body: unknown;
    if (url === "/api/config") body = { defaults, maxUploadBytes: 50 * 1024 ** 2 };
    else if (url === "/api/session") body = { ready: true };
    else if (url === "/api/health") body = { workerOnline: true };
    else if (url === "/api/active-job") body = null;
    else if (url.endsWith("/result")) body = { audio: { duration_seconds: 2, original_sample_rate: 44100 }, analysis: { estimated_bpm: 120, beats_detected: 0, bars_detected: 0, markers_exported: 0, markers_requiring_review: 0, warnings: [] }, timeline: { source_in_seconds: 0, source_out_seconds: 2, fps: 25 }, markers: [] };
    else if (url.endsWith("/preview")) body = { duration: 2, secondsPerPeak: 1, peaks: [[0, 0], [0, 0]] };
    else body = job();
    await route.fulfill({ status: url === "/api/jobs" ? 202 : 200, json: body });
  });
  await page.goto("/");
  await page.locator('input[type="file"]').setInputFiles({ name: "test.wav", mimeType: "audio/wav", buffer: Buffer.from("mock audio") });
  await page.getByRole("button", { name: /Rileva i marker/ }).click();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("beat-active-job"))).toBe(id);
  await page.reload();
  await expect(page.getByText("In coda", { exact: true })).toBeVisible();
  status = "completed";
  await expect(page).toHaveTitle("Marker pronti · Beat Studio", { timeout: 15000 });
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("beat-active-job"))).toBe(null);
  await page.reload();
  await expect(page).toHaveTitle("Beat Studio · Marker musicali");
  await expect(page.getByText("In coda", { exact: true })).toHaveCount(0);
  await expect(page.locator('a[href*="/files/zip"]')).toHaveCount(0);
});
