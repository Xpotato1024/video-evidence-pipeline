import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { chromium } from 'playwright';
import { escape, label, number, object } from './common.mjs';

const require = createRequire(import.meta.url);

export async function offlineContext(browser, viewport = { width: 1100, height: 950 }) {
  const context = await browser.newContext({ offline: true, serviceWorkers: 'block', viewport });
  await context.route('**/*', route => route.abort());
  return context;
}

export function launchChromium() {
  return chromium.launch({
    executablePath: process.env.VEP_CHROMIUM_PATH || undefined,
    headless: true,
    ...(process.env.VEP_CHROMIUM_SANDBOX === '1' ? { chromiumSandbox: true } : {}),
  });
}

// Mermaid uses a declaration-only subset. Its user content can neither specify
// a URL nor set up HTML labels, configuration, lifecycle callbacks, or CSS.
export function mermaidSource(raw) {
  const { code } = object(raw, ['code']);
  if (typeof code !== 'string' || !code.trim() || Buffer.byteLength(code) > 32_000)
    throw new Error('Invalid Mermaid source length');
  const [header, ...lines] = code.trim().split(/\r?\n/u).map(line => line.trim());
  if (!/^flowchart (?:TB|TD|LR|RL|BT)$/u.test(header)) throw new Error('Expected a Mermaid flowchart');
  const nodes = new Set();
  const edges = [];
  const accessibleMetadata = new Map();
  const safeText = (text, length) => {
    if (/[^\p{L}\p{N}\p{M}\p{Zs}.,:;?!()/_+\-–—・、。，．：？！%％°]/u.test(label(text, length)))
      throw new Error('Unsupported Mermaid label character');
  };
  for (const line of lines) {
    if (!line) continue;
    const node = /^([A-Za-z]\w{0,23})\["([^"]+)"\]$/u.exec(line);
    const edge = /^([A-Za-z]\w{0,23}) -->\s*(?:\|([^|]+)\|\s*)?([A-Za-z]\w{0,23})$/u.exec(line);
    const info = /^(accTitle|accDescr):\s*(.+)$/u.exec(line);
    if (node) {
      if (nodes.has(node[1])) throw new Error('Repeated Mermaid node ID');
      safeText(node[2], 48);
      nodes.add(node[1]);
    } else if (edge) {
      if (edge[2]) safeText(edge[2], 32);
      edges.push([edge[1], edge[3]]);
    } else if (info) {
      if (accessibleMetadata.has(info[1])) throw new Error('Repeated Mermaid accessible metadata');
      safeText(info[2], 350);
      accessibleMetadata.set(info[1], info[2]);
    } else {
      throw new Error('Unsupported Mermaid syntax');
    }
  }
  if (nodes.size < 2 || nodes.size > 40 || edges.length < 1 || edges.length > 80 ||
      edges.some(edge => edge.some(id => !nodes.has(id))))
    throw new Error('Invalid Mermaid node and edge set');
  return code;
}

export async function mermaidSvg(context, code, id) {
  const page = await context.newPage();
  try {
    const dist = dirname(require.resolve('mermaid'));
    await page.addScriptTag({ path: join(dist, 'mermaid.js') });
    return await page.evaluate(async ({ source, id }) => {
      const config = {
        startOnLoad: false, securityLevel: 'strict', htmlLabels: false,
        deterministicIds: true, deterministicIDSeed: id,
        theme: 'neutral', fontFamily: 'Noto Sans CJK JP, sans-serif',
        flowchart: { htmlLabels: false, useMaxWidth: false },
      };
      await document.fonts.ready;
      window.mermaid.initialize(config);
      const result = await window.mermaid.render('figure-' + id, source);
      return result.svg;
    }, { source: code, id });
  } finally {
    await page.close();
  }
}

export async function imagePage(context, svg, width, height) {
  const page = await context.newPage({ viewport: { width, height } });
  try {
    await page.setViewportSize({ width, height });
    await page.setContent('<!doctype html><meta charset="UTF-8"><body style="margin:0">' + svg + '</body>');
    await page.evaluate(() => document.fonts.ready);
    const clippedText = await page.locator('svg').first().evaluate(root => {
      const image = root.getBoundingClientRect();
      return [...root.querySelectorAll('text')].filter(text => {
        const bounds = text.getBoundingClientRect();
        return bounds.width > 0 && (bounds.left < image.left - 1 || bounds.right > image.right + 1 ||
          bounds.top < image.top - 1 || bounds.bottom > image.bottom + 1);
      }).map(text => text.textContent);
    });
    if (clippedText.length) throw new Error('SVG text outside its viewBox: ' + clippedText.join(', '));
    return await page.screenshot({ omitBackground: false, animations: 'disabled' });
  } finally {
    await page.close();
  }
}

export async function cropImage(context, file, bbox, mime) {
  if (!Array.isArray(bbox) || bbox.length !== 4 || !bbox.every(Number.isSafeInteger))
    throw new Error('Expected four integer crop coordinates');
  const x = number(bbox[0], 0, 16_000);
  const y = number(bbox[1], 0, 16_000);
  const width = number(bbox[2], 1, 4096);
  const height = number(bbox[3], 1, 4096);
  if (width * height > 12_000_000) throw new Error('Crop output exceeds the pixel budget');
  const page = await context.newPage();
  try {
    const src = `data:${mime};base64,${file.toString('base64')}`;
    await page.setContent('<!doctype html><meta charset="UTF-8"><img src="' + escape(src) + '">');
    const encoded = await page.locator('img').evaluate(async (image, crop) => {
      await image.decode();
      if (image.naturalWidth * image.naturalHeight > 64_000_000 ||
          crop.x + crop.width > image.naturalWidth || crop.y + crop.height > image.naturalHeight)
        throw new Error('Crop exceeds the source bounds or image size limit');
      const canvas = document.createElement('canvas');
      canvas.width = crop.width;
      canvas.height = crop.height;
      canvas.getContext('2d').drawImage(image, crop.x, crop.y, crop.width, crop.height, 0, 0, crop.width, crop.height);
      return canvas.toDataURL('image/png').split(',')[1];
    }, { x, y, width, height });
    return { bytes: Buffer.from(encoded, 'base64'), width, height };
  } finally {
    await page.close();
  }
}
