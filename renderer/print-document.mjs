import { readFile, writeFile } from 'node:fs/promises';
import { launchChromium, offlineContext } from './browser.mjs';

const [input, output] = process.argv.slice(2);
if (!input || !output || process.argv.length !== 4)
  throw new Error('Provide the generated HTML path and a new PDF path.');

const html = await readFile(input, 'utf8');
const browser = await launchChromium();
try {
  const context = await offlineContext(browser);
  const page = await context.newPage();
  await page.setContent(html);
  await page.evaluate(() => document.fonts.ready);
  const pdf = await page.pdf({ format: 'A4', preferCSSPageSize: true, printBackground: true, tagged: true });
  // Preserve PDF byte offsets while stabilizing Chromium's generated timestamps.
  const stable = Buffer.from(pdf.toString('latin1').replace(
    /(\/(?:CreationDate|ModDate)\s*\()D:\d{14}/gu, '$1D:19700101000000'), 'latin1');
  await writeFile(output, stable, { flag: 'wx', mode: 0o600 });
} finally {
  await browser.close();
}
