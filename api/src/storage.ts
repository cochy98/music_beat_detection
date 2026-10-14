import { readdir, lstat, statfs } from "node:fs/promises";
import path from "node:path";
export async function storageAvailable(directory: string, reserve: number, minFree: number, maxData: number) {
  const fs = await statfs(directory, { bigint: true });
  if (fs.bavail * fs.bsize < BigInt(minFree + reserve)) return false;
  let used = 0;
  async function visit(dir: string): Promise<void> {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      if (used + reserve > maxData) return;
      const file = path.join(dir, entry.name);
      try {
        const info = await lstat(file);
        if (info.isDirectory()) await visit(file);
        else if (info.isFile()) used += info.size;
      } catch (e) {
        if ((e as NodeJS.ErrnoException).code !== "ENOENT") throw e;
      }
    }
  }
  await visit(directory);
  return used + reserve <= maxData;
}
