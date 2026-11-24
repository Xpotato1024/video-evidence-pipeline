import { execFileSync } from 'node:child_process';
import { readFile, writeFile, readdir, mkdir, stat, realpath } from 'node:fs/promises';
import { dirname, resolve, relative, isAbsolute, join, posix, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { cropImage, imagePage, launchChromium, mermaidSource, mermaidSvg, offlineContext } from './browser.mjs';
import { renderChart, toCsv } from './chart.mjs';
import { hash, identifier, json, label, object, safeSvg, sourceIdentifier } from './common.mjs';
import { createDocument, mathml } from './document.mjs';
import { geometrySvg } from './geometry.mjs';

const rendererDirectory = dirname(fileURLToPath(import.meta.url));
const inputLimit = 4_000_000;
const outputLimit = 128_000_000;
const assetInputLimit = 12_000_000;
const stepParts = ['symbolic', 'substitution', 'intermediate', 'result'];

async function readLimited(path, limit) {
  const info = await stat(path);
  if (!info.isFile() || info.size > limit) throw new Error('Input is missing or exceeds its byte limit');
  const bytes = await readFile(path);
  if (bytes.length > limit) throw new Error('Input exceeds its byte limit');
  return bytes;
}

function rasterMime(bytes) {
  if (bytes.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex'))) return 'image/png';
  if (bytes.subarray(0, 3).equals(Buffer.from('ffd8ff', 'hex'))) return 'image/jpeg';
  if (bytes.subarray(0, 4).toString() === 'RIFF' && bytes.subarray(8, 12).toString() === 'WEBP') return 'image/webp';
  throw new Error('Source crops take PNG, JPEG or WebP bytes');
}

async function sourceCrop(context, spec, assetDirectory) {
  object(spec, ['path', 'bbox']);
  if (!assetDirectory) throw new Error('Provide --assets to allow source crops from a prepared resource directory');
  if (typeof spec.path !== 'string' || !spec.path || spec.path.length > 250 || spec.path.includes('\\') ||
      spec.path.split('/').some(part => !/^[\p{L}\p{N}_. -]+$/u.test(part) || part === '.' || part === '..') ||
      posix.isAbsolute(spec.path))
    throw new Error('Source crop path must be relative to the prepared asset directory');
  const source = await realpath(join(assetDirectory, spec.path));
  const fromRoot = relative(assetDirectory, source);
  if (!fromRoot || isAbsolute(fromRoot) || fromRoot.split(sep)[0] === '..')
    throw new Error('Source crop path escapes the prepared asset directory');
  const input = await readLimited(source, assetInputLimit);
  const crop = await cropImage(context, input, spec.bbox, rasterMime(input));
  return { ...crop, input, mediaType: 'image/png', extension: 'png' };
}

async function visual(context, figure, assetDirectory) {
  if (figure.kind === 'source_crop') return sourceCrop(context, figure.spec, assetDirectory);
  let drawing;
  switch (figure.kind) {
    case 'vega-lite': drawing = await renderChart(figure.spec); break;
    case 'geometry': drawing = { svg: geometrySvg(figure.spec, figure.alt) }; break;
    case 'mermaid': drawing = { svg: await mermaidSvg(context, mermaidSource(figure.spec), figure.id) }; break;
    default: throw new Error('Unknown figure kind ' + figure.kind);
  }
  const { svg, width, height } = safeSvg(drawing.svg);
  const png = await imagePage(context, svg, width, height);
  const data = drawing.headers ? { headers: drawing.headers, rows: drawing.rows } : null;
  return { bytes: Buffer.from(svg), png, width, height, data, mediaType: 'image/svg+xml', extension: 'svg' };
}

function equation(source, id) {
  try {
    return mathml(source);
  } catch (error) {
    throw new Error('Equation ' + id + ' could not be rendered: ' + error.message, { cause: error });
  }
}

async function buildFigures(content, context, write, inputAssetDirectory) {
  const figures = new Map();
  const copiedInputs = new Set();
  for (const figure of content.figures) {
    const id = identifier(figure.id);
    if (figures.has(id)) throw new Error('Duplicate figure ID ' + id);
    label(figure.alt, 450);
    label(figure.caption, 450);
    let rendered;
    try {
      rendered = await visual(context, figure, inputAssetDirectory);
    } catch (error) {
      throw new Error('Figure ' + id + ' could not be rendered: ' + error.message, { cause: error });
    }
    const name = id + '-' + hash(rendered.bytes).slice(0, 12);
    const path = 'figures/' + name + '.' + rendered.extension;
    await write(path, rendered.bytes);
    await write('sites/' + path, rendered.bytes);
    if (rendered.png) await write('figures/' + name + '.png', rendered.png);
    if (rendered.data) {
      const dataset = { ...rendered.data, specification: figure.spec, source_refs: figure.source_refs };
      await write('data/' + id + '.json', json(dataset));
      await write('data/' + id + '.csv', toCsv(dataset.headers, dataset.rows));
    }
    if (rendered.input && !copiedInputs.has(figure.spec.path)) {
      await write('inputs/' + figure.spec.path, rendered.input);
      copiedInputs.add(figure.spec.path);
    }
    const { width, height, data } = rendered;
    figures.set(id, {
      ...figure, width, height, data, path,
      dataUri: 'data:' + rendered.mediaType + ';base64,' + rendered.bytes.toString('base64'),
    });
  }
  return figures;
}

function makeWriter(root) {
  const files = {};
  let size = 0;
  return {
    files,
    async write(path, data, includeInReceipt = true) {
      const content = Buffer.from(data);
      size += content.length;
      if (size > outputLimit) throw new Error('Render output exceeds its byte limit');
      const destination = join(root, path);
      await mkdir(dirname(destination), { recursive: true });
      await writeFile(destination, content, { flag: 'wx' });
      if (includeInReceipt) files[path] = { sha256: hash(content), size_bytes: content.length };
    },
  };
}

function validateContent(content) {
  if (content.schema_version !== '2.0') throw new Error('Unsupported lecture content schema');
  for (const key of ['equations', 'figures', 'sources', 'blocks']) {
    const maximum = { figures: 55, sources: 10_000, blocks: 2048, equations: 512 }[key];
    if (!Array.isArray(content[key]) || content[key].length > maximum)
      throw new Error('Rendering requires a bounded ' + key + ' array');
    const validateId = key === 'sources' ? sourceIdentifier : identifier;
    const ids = content[key].map(item => validateId(item.id));
    if (new Set(ids).size !== ids.length) throw new Error('Duplicate item ID in ' + key);
  }
  if (!content.blocks.length) throw new Error('Lesson must have at least one block');
}

async function buildEquations(content, write) {
  const equations = new Map();
  const stepEquations = new Map();
  for (const item of content.equations) {
    const expression = equation(item.tex, item.id);
    equations.set(item.id, { ...item, expression });
    await write('math/' + item.id + '.tex', item.tex + '\n');
    await write('math/' + item.id + '.mml', expression + '\n');
  }
  for (const block of content.blocks.filter(item => item.type === 'steps' || item.type === 'procedure')) {
    if (!Array.isArray(block.steps) || !block.steps.length || block.steps.length > 128)
      throw new Error('A steps block requires 1–128 steps');
    if (block.type === 'procedure') {
      if (!['drawing', 'calculation'].includes(block.kind) ||
          block.steps.some(step => step.type !== 'operation' || !step.explanation?.trim()))
        throw new Error('A procedure requires a kind and an explanation for each operation');
      if (block.kind === 'drawing') {
        const stages = block.steps.map(step => step.figure_id);
        if (stages.some(id => !id) || new Set(stages).size !== stages.length)
          throw new Error('Each drawing step requires a separate stage figure');
      }
    }
    const entries = new Map();
    stepEquations.set(block.id, entries);
    for (const step of block.steps) {
      const stepId = identifier(step.id);
      if (entries.has(stepId)) throw new Error('Duplicate step ID in ' + block.id);
      const parts = new Map();
      entries.set(stepId, parts);
      const fields = step.type === 'operation' ? ['expression'] : stepParts;
      for (const part of fields) {
        if (part === 'expression' && step.expression == null) continue;
        if (step[part] == null && ['substitution', 'intermediate'].includes(part)) continue;
        const id = block.id + '-' + stepId + '-' + part;
        const expression = equation(step[part], id);
        parts.set(part, expression);
        await write('math/steps/' + block.id + '/' + stepId + '-' + part + '.tex', step[part] + '\n');
        await write('math/steps/' + block.id + '/' + stepId + '-' + part + '.mml', expression + '\n');
      }
    }
  }
  return { equations, stepEquations };
}

export async function checkPage(page) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all([...document.images].map(image => image.decode()));
  });
  return page.evaluate(() => {
    const fits = element => element.scrollWidth <= element.clientWidth + 1;
    return {
      viewport_fits: fits(document.documentElement),
      images_loaded: [...document.images].every(image => image.naturalWidth > 0 && image.naturalHeight > 0),
      equation_count: document.querySelectorAll('math').length,
      math_overflow_count: [...document.querySelectorAll('.equation')].filter(element => !fits(element)).length,
      external_resources: performance.getEntriesByType('resource').filter(item => /^https?:/u.test(item.name)).length,
    };
  });
}

async function printReview(context, html, equationCount) {
  const page = await context.newPage();
  try {
    await page.setContent(html);
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      const check = await checkPage(page);
      if (!check.viewport_fits || !check.images_loaded || check.external_resources || check.equation_count !== equationCount)
        throw new Error('Review HTML did not pass offline layout checks at viewport ' + width);
    }
    await page.setViewportSize({ width: 390, height: 900 });
    await page.evaluate(() => { document.documentElement.style.fontSize = '200%'; });
    const enlarged = await checkPage(page);
    if (!enlarged.viewport_fits || !enlarged.images_loaded)
      throw new Error('Review HTML did not fit its viewport with text enlarged to 200%');
    await page.evaluate(() => { document.documentElement.style.fontSize = ''; });
    await page.setViewportSize({ width: 687, height: 1040 });
    await page.emulateMedia({ media: 'print' });
    const print = await checkPage(page);
    if (print.math_overflow_count || !print.viewport_fits)
      throw new Error('An equation or table exceeds the A4 print content width; split it into aligned lines');
    const pdf = await page.pdf({ format: 'A4', preferCSSPageSize: true, printBackground: true, tagged: true });
    // Normalize Chromium's build-time PDF metadata without moving byte offsets.
    const stablePdf = Buffer.from(pdf.toString('latin1').replace(
      /(\/(?:CreationDate|ModDate)\s*\()D:\d{14}/gu, '$1D:19700101000000'), 'latin1');
    return stablePdf;
  } finally {
    await page.close();
  }
}

async function toolchain(browser) {
  const manifest = JSON.parse(await readFile(join(rendererDirectory, 'package.json'), 'utf8'));
  const files = ['browser.mjs', 'chart.mjs', 'common.mjs', 'document.mjs', 'geometry.mjs', 'lesson.css', 'package.json', 'package-lock.json', 'render.mjs'];
  const hashes = await Promise.all(files.map(async path => [path, hash(await readFile(join(rendererDirectory, path)))]));
  const fonts = {};
  for (const name of ['sans-serif', 'math', 'Noto Sans CJK JP']) {
    try {
      const file = execFileSync('fc-match', ['-f', '%{file}', name], { encoding: 'utf8', timeout: 5000 });
      if (file) fonts[name] = { filename: file.split(/[\\/]/u).at(-1), sha256: hash(await readFile(file)) };
    } catch {
      // Fontconfig is optional on non-Linux hosts; the browser itself still
      // uses local system fonts, and the exact PDF bytes are always fingerprinted.
      fonts[name] = null;
    }
  }
  return {
    version: manifest.version, source_sha256: hash(json(hashes)), files: Object.fromEntries(hashes),
    node: process.versions.node, chromium: browser.version(), dependencies: manifest.dependencies, fonts,
  };
}

async function main() {
  const { values } = parseArgs({ options: {
    input: { type: 'string' }, output: { type: 'string' }, assets: { type: 'string' },
  } });
  if (!values.input || !values.output) throw new Error('Use --input CONTENT_JSON --output EMPTY_DIRECTORY [--assets INPUT_DIRECTORY]');
  const input = await readLimited(resolve(values.input), inputLimit);
  const output = resolve(values.output);
  await mkdir(output, { recursive: true });
  if ((await readdir(output)).length) throw new Error('Rendering requires an empty output directory');
  const assetDirectory = values.assets ? await realpath(resolve(values.assets)) : null;
  const content = JSON.parse(input);
  validateContent(content);
  const { files, write } = makeWriter(output);
  const browser = await launchChromium();
  let renderer;
  try {
    const context = await offlineContext(browser);
    const { equations, stepEquations } = await buildEquations(content, write);
    const figures = await buildFigures(content, context, write, assetDirectory);
    const document = createDocument(content, equations, figures, stepEquations);
    const css = await readFile(join(rendererDirectory, 'lesson.css'), 'utf8');
    const html = document.renderHtml('standalone', css);
    const equationCount = (html.match(/<math\b/gu) ?? []).length;
    const pdf = await printReview(context, html, equationCount);
    await write('lesson.md', document.renderMarkdown('md'));
    await write('lesson.mdx', document.renderMarkdown('mdx'));
    await write('review.html', html);
    await write('review.pdf', pdf);
    await write('sites/index.html', document.renderHtml('site', css));
    await write('sites/lesson.css', css);
    await write('sites/review.pdf', pdf);
    await write('sites/robots.txt', 'User-agent: *\nDisallow: /\n');
    renderer = await toolchain(browser);
  } finally {
    await browser.close();
  }
  await write('render-receipt.json', json({
    schema_version: '2.0', content_sha256: hash(input), renderer, files,
    validation: {
      chromium_viewport_px: [320, 390, 768, 1440], text_zoom: 2, print_format: 'A4',
      javascript_in_viewer: false, external_network: false, approval: 'not_evaluated',
    },
  }), false);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
