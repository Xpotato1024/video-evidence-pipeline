import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { lstat, mkdir, readFile, writeFile } from 'node:fs/promises';
import { extname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { webkit } from 'playwright';
import { launchChromium } from './browser.mjs';

const [bundleArgument, outputArgument] = process.argv.slice(2);
if (!bundleArgument || !outputArgument) {
  throw new Error('Usage: node renderer/check-layout.mjs REVIEW_BUNDLE QA_OUTPUT');
}
const bundle = resolve(bundleArgument);
const output = resolve(outputArgument);
await mkdir(output, { recursive: true });

const mime = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.webp': 'image/webp',
  '.pdf': 'application/pdf',
  '.woff2': 'font/woff2',
  '.json': 'application/json; charset=utf-8',
};

const site = createServer(async (request, response) => {
  try {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    assert.match(pathname, /^\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]*$/u);
    const parts = pathname.slice(1).split('/').filter(Boolean);
    assert.ok(parts.every(part => part !== '..' && part !== '.'));
    let file = join(bundle, 'sites');
    assert.ok((await lstat(file)).isDirectory());
    for (const part of parts.length ? parts : ['index.html']) {
      file = join(file, part);
      assert.ok(!(await lstat(file)).isSymbolicLink());
    }
    assert.ok((await lstat(file)).isFile());
    response.writeHead(200, { 'Content-Type': mime[extname(file)] || 'application/octet-stream' });
    response.end(await readFile(file));
  } catch {
    if (!response.headersSent) response.writeHead(404);
    response.end();
  }
});

await new Promise((accept, reject) => {
  site.once('error', reject);
  site.listen(0, '127.0.0.1', accept);
});
const origin = 'http://127.0.0.1:' + site.address().port;
const standalone = pathToFileURL(join(bundle, 'review.html')).href;
const sizes = [
  { name: '320', width: 320, height: 740 },
  { name: '390', width: 390, height: 844 },
  { name: '768', width: 768, height: 1024 },
  { name: '1440', width: 1440, height: 900 },
  { name: '390-at-200-percent', width: 195, height: 422, scale: 2 },
  { name: '1440-at-200-percent', width: 720, height: 450, scale: 2 },
];

async function inspect(browser, name, url, size, paper = false) {
  const offline = url === standalone;
  const requests = new Map();
  const blocked = [];
  const errors = [];
  const context = await browser.newContext({
    viewport: { width: size.width, height: size.height },
    deviceScaleFactor: size.scale || 1,
    isMobile: name === 'webkit' && size.width <= 390 && !paper,
    hasTouch: size.width <= 390 && !paper,
    offline,
    serviceWorkers: 'block',
    javaScriptEnabled: false,
  });
  try {
    await context.route('**/*', async route => {
      const requested = route.request().url();
      const allowed = offline
        ? requested === standalone
        : new URL(requested).origin === origin;
      if (allowed) return route.continue();
      blocked.push(requested);
      return route.abort();
    });
    const page = await context.newPage();
    page.on('response', response => requests.set(response.url(), response.status()));
    page.on('pageerror', error => errors.push(error.message));
    if (paper) await page.emulateMedia({ media: 'print' });
    const loadMethod = offline && name === 'webkit' ? 'offline-html-bytes' : 'navigation';
    if (loadMethod === 'offline-html-bytes') {
      await page.setContent(await readFile(join(bundle, 'review.html'), 'utf8'), { waitUntil: 'load' });
    } else {
      await page.goto(url, { waitUntil: 'load' });
    }
    const metrics = await page.evaluate(async () => {
      await document.fonts.ready;
      const images = [...document.images];
      const brokenImages = [];
      for (const image of images) {
        const bounds = image.getBoundingClientRect();
        if (!image.complete || !image.naturalWidth || !bounds.width || !bounds.height ||
            !image.alt.trim()) brokenImages.push(image.getAttribute('src')?.slice(0, 60) || '(no src)');
      }
      const figures = [...document.querySelectorAll('figure')];
      const invisibleFigures = figures.filter(figure => {
        const visual = figure.querySelector('img, svg');
        const bounds = visual?.getBoundingClientRect();
        return !bounds?.height || !bounds?.width;
      }).map(figure => figure.id);
      const croppedFigures = figures.filter(figure => {
        const viewport = figure.querySelector('.figure-scroll');
        return viewport && viewport.scrollWidth > viewport.clientWidth + 1;
      }).map(figure => figure.id);
      const equations = [...document.querySelectorAll('math')];
      const hiddenEquations = equations.filter(math => !math.getBoundingClientRect().height);
      return {
        viewport: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
        scale: devicePixelRatio,
        figures: figures.length,
        equations: equations.length,
        invisibleFigures,
        croppedFigures,
        brokenImages,
        hiddenEquations: hiddenEquations.length,
      };
    });
    const label = [name, offline ? 'offline' : 'site', paper ? 'print' : size.name].join('-');
    await page.screenshot({ path: join(output, label + '.png') });
    assert.ok(metrics.scrollWidth <= metrics.viewport + 1, label + ' has horizontal page overflow');
    assert.ok(metrics.figures > 0 && metrics.equations > 0, label + ' has no figures or math');
    assert.equal(metrics.hiddenEquations, 0, label + ' has hidden equations');
    assert.deepEqual(metrics.invisibleFigures, [], label + ' has invisible figures');
    if (!size.scale) assert.deepEqual(metrics.croppedFigures, [], label + ' clips figures at default zoom');
    assert.deepEqual(metrics.brokenImages, [], label + ' has missing images or alt text');
    assert.deepEqual(blocked, [], label + ' attempted external or standalone sidecar loads');
    assert.deepEqual(errors, []);
    assert.ok([...requests.values()].every(status => status < 400), label + ' has failed requests');
    return { browser: name, route: offline ? 'standalone' : 'same-origin', loadMethod, size: paper ? 'print' : size.name,
      ...metrics, externalRequests: blocked.length, httpRequests: offline ? 0 : requests.size };
  } finally {
    await context.close();
  }
}

try {
  const results = [];
  for (const [name, launch] of [['chromium', launchChromium], ['webkit', () => webkit.launch()]]) {
    const browser = await launch();
    try {
      for (const url of [standalone, origin]) {
        for (const size of sizes) results.push(await inspect(browser, name, url, size));
      }
      results.push(await inspect(browser, name, standalone, { width: 794, height: 1123 }, true));
    } finally {
      await browser.close();
    }
  }
  const report = JSON.stringify({ results }, null, 2) + '\n';
  await writeFile(join(output, 'layout-report.json'), report);
  process.stdout.write(report);
} finally {
  site.close();
}
